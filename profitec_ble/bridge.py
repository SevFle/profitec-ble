"""MQTT / Home Assistant bridge daemon. Reconnects with backoff but never resends a command that was
in flight. Remote brewing needs --allow-remote-brew and passes a readiness gate. Needs the [mqtt] extra."""
import asyncio
import json
import re
from contextlib import aclosing, suppress
from datetime import datetime, timezone
from pathlib import Path
from .client import ProfitecClient
from .monitor import ReadyTracker, ShotRecorder
from .protocol import DeviceLog, MachineStatus, Pressure, decode, decode_settings
from .settings import SETTABLE, setting_command, setting_value

UNITS = {"temperature_c": "°C", "seconds": "s", "shots": "shots", "litres": "L", "minutes": "min"}
BREW_GATE_C = 2


def _stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _parse(name, payload):
    spec = SETTABLE[name]
    if spec.choices:
        if isinstance(spec.choices[0], bool):
            if payload not in ("ON", "OFF"):
                raise ValueError("Expected ON or OFF")
            return payload == "ON"
        return payload
    number = float(payload)
    return int(number) if number.is_integer() else number


def _state(value):
    return ("ON" if value else "OFF") if isinstance(value, bool) else str(value)


class Bridge:
    def __init__(self, device, publish, *, prefix="profitec", allow_remote_brew=False, journal_dir=None,
                 shots_dir=None, release_seconds=600, max_backoff=60, client=ProfitecClient, log=False):
        self.device, self.publish = device, publish
        self.node = re.sub(r"[^A-Za-z0-9_]", "_", device).lower()
        self.base = f"{prefix}/{self.node}"
        self.allow_remote_brew = allow_remote_brew
        self.journal_dir = Path(journal_dir) if journal_dir else None
        self.recorder = ShotRecorder(shots_dir) if shots_dir else None
        self.release_seconds, self.max_backoff = release_seconds, max_backoff
        self.client = client
        self.log = log
        self._write_lock = asyncio.Lock()
        self.machine = None
        self.status = None
        self.setpoint = None
        self.ready = None
        self._release = asyncio.Event()
        self._reconnect = asyncio.Event()
        self._tasks = set()

    async def _send(self, topic, payload, retain=False):
        await self.publish(f"{self.base}/{topic}", payload if isinstance(payload, str) else json.dumps(payload), retain)

    async def error(self, message):
        await self._send("error", message)

    # Discovery -------------------------------------------------------------------------------
    def discovery(self):
        device = {"identifiers": [f"profitec_{self.node}"], "name": "Profitec MOVE", "model": "MOVE 2.0",
                  "manufacturer": "Profitec (unofficial integration)"}
        common = {"availability_topic": f"{self.base}/availability", "device": device}
        entities = [
            ("sensor", "pressure", {"unit_of_measurement": "bar", "device_class": "pressure", "state_class": "measurement"}),
            ("sensor", "coffee_temperature", {"unit_of_measurement": "°C", "device_class": "temperature"}),
            ("sensor", "steam_temperature", {"unit_of_measurement": "°C", "device_class": "temperature"}),
            ("sensor", "machine_state", {}),
            ("binary_sensor", "brewing", {}),
            ("binary_sensor", "ready", {}),
            ("switch", "release_bluetooth", {"command_topic": f"{self.base}/cmd/release"}),
        ]
        for name, spec in SETTABLE.items():
            writable = self.journal_dir is not None
            if not writable:
                entities.append(("sensor", name, {}))
            elif spec.choices and isinstance(spec.choices[0], bool):
                entities.append(("switch", name, {"command_topic": f"{self.base}/set/{name}"}))
            elif spec.choices:
                entities.append(("select", name, {"command_topic": f"{self.base}/set/{name}", "options": list(spec.choices)}))
            else:
                unit = next((u for suffix, u in UNITS.items() if name.endswith(suffix)), None)
                entities.append(("number", name, {"command_topic": f"{self.base}/set/{name}", "min": spec.minimum,
                                                  "max": spec.maximum, "step": spec.step, "mode": "box",
                                                  **({"unit_of_measurement": unit} if unit else {})}))
        if self.allow_remote_brew:
            for label in ("1", "2", "manual", "stop"):
                entities.append(("button", f"brew_{label}", {"command_topic": f"{self.base}/cmd/brew", "payload_press": label}))
        result = []
        for component, name, extra in entities:
            config = {"name": name.replace("_", " "), "unique_id": f"profitec_{self.node}_{name}", **common, **extra}
            if component != "button":
                config["state_topic"] = f"{self.base}/state/{name}"
            result.append((f"homeassistant/{component}/profitec_{self.node}/{name}/config", json.dumps(config)))
        return result

    async def announce(self):
        for topic, payload in self.discovery():
            await self.publish(topic, payload, True)

    # Telemetry -------------------------------------------------------------------------------
    async def publish_block(self, block):
        for name, spec in SETTABLE.items():
            if spec.block == block.name:
                value = setting_value(name, block)
                if name == "coffee_temperature_c":
                    self.setpoint = value
                    self.ready = ReadyTracker(value)
                await self._send(f"state/{name}", _state(value))

    async def on_packet(self, packet, now):
        if self.recorder:
            self.recorder.feed(packet)
        try:
            value = decode(packet["uuid"], bytes.fromhex(packet["hex"]))
        except ValueError as error:
            await self.error(f"decode: {error}")
            return
        if isinstance(value, DeviceLog):
            await self._send("log", value.text)
        elif isinstance(value, Pressure):
            await self._send("state/pressure", str(value.bar))
        elif isinstance(value, MachineStatus):
            self.status = value
            await self._send("state/coffee_temperature", str(value.coffee_temperature))
            await self._send("state/steam_temperature", str(value.steam_temperature))
            await self._send("state/machine_state", str(value.state))
            await self._send("state/brewing", _state(value.brewing))
            if self.ready:
                await self._send("state/ready", _state(self.ready.update(value, now)))

    # Commands --------------------------------------------------------------------------------
    def handle(self, topic, payload, retain=False):
        """Schedule one command; a stop never waits behind a running settings write."""
        task = asyncio.create_task(self._handle(topic, payload, retain))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _handle(self, topic, payload, retain):
        try:
            if retain:
                # A retained command would replay on every broker reconnect.
                raise ValueError(f"Retained command ignored on {topic}")
            command = topic.removeprefix(self.base + "/")
            if command == "cmd/release":
                await self._release_bluetooth(payload)
            elif command == "cmd/brew":
                await self._brew(payload)
            elif command.startswith("set/") and command[4:] in SETTABLE:
                await self._set(command[4:], payload)
            else:
                raise ValueError(f"Unknown command topic {topic}")
        except Exception as error:
            # The client refuses work after a failed request; reconnect, never resend.
            if self.machine is not None and self.machine.uncertain:
                self._reconnect.set()
            await self.error(f"{type(error).__name__}: {error}")

    def _require_machine(self):
        if self.machine is None:
            raise ConnectionError("Machine not connected; command not queued")
        return self.machine

    async def _set(self, name, payload):
        if self.journal_dir is None:
            raise PermissionError("Settings writes need --journal-dir")
        machine = self._require_machine()
        value = _parse(name, payload)
        setting_command(name, value)  # reject before touching the machine
        self.journal_dir.mkdir(parents=True, exist_ok=True)
        async with self._write_lock:
            journal = self.journal_dir / f"{_stamp()}-{name}.jsonl"
            try:
                blocks = await machine.apply_settings({name: value}, journal)
            except Exception:
                # Show what the machine reported instead of the requested value.
                for line in reversed(journal.read_text(encoding="utf-8").splitlines() if journal.exists() else []):
                    entry = json.loads(line)
                    if entry.get("event") == "readback":
                        await self.publish_block(decode_settings(bytes.fromhex(entry["hex"])))
                        break
                raise
        for block in blocks.values():
            await self.publish_block(block)

    def _gate(self):
        if not self.allow_remote_brew:
            raise PermissionError("Remote brewing disabled; start the bridge with --allow-remote-brew")
        machine = self._require_machine()
        if self.status is None or self.setpoint is None:
            raise RuntimeError("Machine status unknown; refusing remote brew")
        if self.status.state != 0:
            raise RuntimeError(f"Machine busy (state {self.status.state}); refusing remote brew")
        if abs(self.status.coffee_temperature - self.setpoint) > BREW_GATE_C:
            raise RuntimeError("Coffee boiler not within 2 °C of setpoint; refusing remote brew")
        return machine

    async def _brew(self, payload):
        if payload == "stop":
            if not self.allow_remote_brew:
                raise PermissionError("Remote brewing disabled; start the bridge with --allow-remote-brew")
            await self._require_machine().brew(None)
            return
        dose = {"1": 1, "2": 2, "manual": 0}.get(payload)
        if dose is None:
            raise ValueError("Brew payload must be 1, 2, manual or stop")
        await self._gate().brew(dose)

    async def _release_bluetooth(self, payload):
        if payload != "ON":
            return
        if self.status is not None and self.status.brewing:
            raise RuntimeError("Brewing; Bluetooth not released")
        self._release.set()

    # Connection loops ------------------------------------------------------------------------
    async def run(self):
        """Hold the machine connection forever; reconnect with backoff, never replaying commands."""
        backoff = min(2, self.max_backoff)
        while True:
            try:
                async with self.client(self.device, allow_experimental_settings=self.journal_dir is not None,
                                       allow_experimental_controls=self.allow_remote_brew) as machine:
                    for name in ("doses", "general"):
                        await self.publish_block(await machine.read_settings(name))
                    self.machine = machine
                    await self._send("availability", "online", True)
                    backoff = 2
                    await self._stream(machine)
            except Exception as error:
                await self.error(f"connection: {type(error).__name__}: {error}")
            finally:
                self.machine = None
                self.status = None  # never gate a brew on status from an earlier connection
                if self.recorder:
                    self.recorder.close()
                await self._send("availability", "offline", True)
            self._reconnect.clear()
            if self._release.is_set():
                await self._send("state/release_bluetooth", "ON")
                await asyncio.sleep(self.release_seconds)
                self._release.clear()
                await self._send("state/release_bluetooth", "OFF")
                continue
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self.max_backoff)

    async def _stream(self, machine):
        loop = asyncio.get_running_loop()

        async def consume():
            async with aclosing(machine.events(None, log=self.log)) as stream:
                async for packet in stream:
                    await self.on_packet(packet, loop.time())

        consumer = asyncio.create_task(consume())
        waiters = [asyncio.create_task(event.wait()) for event in (self._release, self._reconnect)]
        try:
            await asyncio.wait([consumer, *waiters], return_when=asyncio.FIRST_COMPLETED)
            if consumer.done():
                consumer.result()
        finally:
            for task in (consumer, *waiters):
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

async def serve(device, host, port=1883, *, username=None, password=None, **options):
    import aiomqtt
    backoff = 2
    while True:
        bridge = Bridge(device, None, **options)
        try:
            will = aiomqtt.Will(f"{bridge.base}/availability", "offline", retain=True)
            async with aiomqtt.Client(host, port, username=username, password=password, will=will,
                                      clean_session=True) as mqtt:
                async def publish(topic, payload, retain):
                    await mqtt.publish(topic, payload, retain=retain)
                bridge.publish = publish
                await bridge.announce()
                await mqtt.subscribe(f"{bridge.base}/set/#")
                await mqtt.subscribe(f"{bridge.base}/cmd/#")
                backoff = 2
                async def receive():
                    async for message in mqtt.messages:
                        payload = message.payload.decode() if isinstance(message.payload, bytes) else str(message.payload)
                        bridge.handle(str(message.topic), payload, message.retain)

                tasks = [asyncio.create_task(job) for job in (receive(), bridge.run())]
                try:
                    # Any loop ending (a publish failing inside run(), say) tears everything down and retries.
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as error:
            print(f"bridge: {type(error).__name__}: {error}; retrying in {backoff} s", flush=True)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60)
