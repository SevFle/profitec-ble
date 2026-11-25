"""Bleak transport. No automatic reconnect, retries, calibration, or startup writes."""
import asyncio
import math
import json
import os
from datetime import datetime, timezone
from .protocol import PRESSURE_UUID, STATUS_UUID, CONTROL_UUID, decode_settings, settings_request, brew_commands
from .settings import SETTABLE, setting_command, schedule_command


def positive(value: float) -> float:
    if not math.isfinite(value) or value <= 0:
        raise ValueError("Duration must be finite and positive")
    return value


async def apply_verified_changes(commands, requested, read, write, record, device, preinfusion=False):
    """Apply validated commands on the caller's existing connection and journal full readbacks."""
    block_names = list(dict.fromkeys([block for block, _ in commands.values()] + (["general"] if preinfusion else [])))
    blocks = {name: await read(name) for name in block_names}
    record({"event": "before", "device": device, "time": datetime.now(timezone.utc).isoformat(),
            "blocks": {name: block.raw.hex() for name, block in blocks.items()},
            "requested": requested, "commands": {name: value.hex() for name, (_, value) in commands.items()}})
    if preinfusion and blocks["general"].words[21] != 1:
        raise ValueError("Machine advanced pre-infusion mode is not enabled; enable it in the official app first")

    def comparable(block_name, raw):
        # Clock fields tick independently; compare the complete schedule region.
        return raw[14:] if block_name == "time" else raw

    for name, (block_name, command) in commands.items():
        before = blocks[block_name]
        register = int.from_bytes(command[:2], "big")
        offset = 2 + (register - before.address) * 2
        expected = before.raw[:offset] + command[2:] + before.raw[offset+len(command)-2:]
        if before.raw == expected:
            record({"event": "unchanged", "name": name})
            continue
        fresh = await read(block_name)
        if comparable(block_name, fresh.raw) != comparable(block_name, before.raw):
            raise RuntimeError("Settings changed since snapshot; refusing to overwrite")
        record({"event": "sending", "name": name})
        await write(command)
        after = await read(block_name)
        record({"event": "readback", "name": name, "hex": after.raw.hex()})
        if comparable(block_name, after.raw) != comparable(block_name, expected):
            raise RuntimeError(f"Readback mismatch for {name}; outcome uncertain, inspect journal")
        blocks[block_name] = after
    record({"event": "verified"})
    return blocks


async def scan(timeout: float = 10) -> list[dict]:
    from bleak import BleakScanner
    devices = await BleakScanner.discover(timeout=positive(timeout), return_adv=True)
    return [
        {"device": device.address, "name": adv.local_name or device.name, "services": adv.service_uuids}
        for device, adv in devices.values()
        if any(s.startswith("873ae8") for s in adv.service_uuids)
        or any(word in (adv.local_name or device.name or "").lower() for word in ("profitec", "ecm", "pressensor"))
    ]


class ProfitecClient:
    def __init__(self, device: str, *, allow_experimental_controls: bool = False, allow_experimental_settings: bool = False, timeout: float = 20):
        self.device = device
        self.timeout = positive(timeout)
        self.allow_experimental_controls = allow_experimental_controls
        self.allow_experimental_settings = allow_experimental_settings
        self._client = None
        self._busy = False
        self._uncertain = False

    async def __aenter__(self):
        from bleak import BleakClient
        if self._client is not None:
            raise RuntimeError("Client already entered")
        client = BleakClient(self.device, timeout=self.timeout)
        try:
            await client.connect()
        except BaseException:
            if client.is_connected:
                await client.disconnect()
            raise
        self._client = client
        self._uncertain = False
        return self

    async def __aexit__(self, *exc):
        client, self._client = self._client, None
        if client is not None:
            await client.disconnect()

    def _connected(self):
        if self._client is None or not self._client.is_connected:
            raise ConnectionError("Use an active 'async with ProfitecClient(...)' connection")
        return self._client

    def _begin(self):
        client = self._connected()
        if self._busy:
            raise RuntimeError("Only one stream, settings request, or control operation may run at a time")
        if self._uncertain:
            raise RuntimeError("Previous request failed; reconnect before another operation")
        self._busy = True
        return client

    def services(self) -> list[dict]:
        return [{"uuid": s.uuid, "characteristics": [
            {"uuid": c.uuid, "properties": list(c.properties)} for c in s.characteristics
        ]} for s in self._connected().services]

    async def events(self, seconds: float = 60):
        """Yield raw timestamped packets. Slow consumers fail instead of silently losing data."""
        positive(seconds)
        client = self._begin()
        queue = asyncio.Queue(maxsize=1024)
        overflow = False
        subscribed = []

        def receive(sender, data):
            nonlocal overflow
            try:
                queue.put_nowait({"time": datetime.now(timezone.utc).isoformat(), "uuid": sender.uuid, "hex": bytes(data).hex()})
            except asyncio.QueueFull:
                overflow = True

        try:
            for uuid in (PRESSURE_UUID, STATUS_UUID):
                await client.start_notify(uuid, receive)
                subscribed.append(uuid)
            end = asyncio.get_running_loop().time() + seconds
            while True:
                if overflow:
                    raise BufferError("Telemetry consumer fell behind; capture is incomplete")
                if not client.is_connected:
                    raise ConnectionError("Disconnected; capture is incomplete")
                remaining = end - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    packet = await asyncio.wait_for(queue.get(), min(remaining, 0.5))
                except TimeoutError:
                    continue
                yield packet
        finally:
            try:
                if client.is_connected:
                    for uuid in subscribed:
                        await client.stop_notify(uuid)
            finally:
                self._busy = False

    async def read_settings(self, name: str, *, timeout: float = 5):
        """Request a register block. No automatic retries after timeout."""
        settings_request(name)
        positive(timeout)
        self._begin()
        try:
            return await self._request_settings(name, timeout)
        except BaseException:
            self._uncertain = True
            raise
        finally:
            self._busy = False

    async def _request_settings(self, name, timeout=5):
        request = settings_request(name)
        client = self._connected()
        future = asyncio.get_running_loop().create_future()
        subscribed = False

        def receive(sender, data):
            if bytes(data[:2]) == request[:2] and not future.done():
                try:
                    future.set_result(decode_settings(bytes(data)))
                except ValueError as error:
                    future.set_exception(error)

        try:
            await client.start_notify(STATUS_UUID, receive)
            subscribed = True
            async with asyncio.timeout(timeout):
                await client.write_gatt_char(STATUS_UUID, request, response=True)
                return await future
        finally:
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()  # Consume a callback error if the request write itself failed.
            if subscribed and client.is_connected:
                await client.stop_notify(STATUS_UUID)

    async def apply_settings(self, values: dict, journal):
        """Apply allowlisted settings, preserving a durable before-image and verifying each write.

        Not atomic: failure can leave earlier settings applied. Inspect the journal;
        reconnect and read actual values before deciding how to restore them.
        """
        if not self.allow_experimental_settings:
            raise PermissionError("Experimental settings writes disabled; opt in explicitly")
        if not isinstance(values, dict) or not values:
            raise ValueError("Provide a nonempty settings object")
        commands = {name: (SETTABLE[name].block, setting_command(name, value))
                    for name, value in values.items() if name in SETTABLE}
        if len(commands) != len(values):
            raise ValueError("Unknown setting")
        preinfusion = any(name.endswith("preinfusion_enabled") and value is True for name, value in values.items())
        return await self._apply_changes(commands, journal, values, preinfusion)

    async def apply_schedule(self, day_index: int, slot: int, window, journal):
        """Set one scheduled on/off window. Wire day order has not been UI-verified."""
        if not self.allow_experimental_settings:
            raise PermissionError("Experimental settings writes disabled; opt in explicitly")
        command = schedule_command(day_index, slot, window)
        return await self._apply_changes({"schedule": ("time", command)}, journal,
                                         {"day_index": day_index, "slot": slot,
                                          "window": None if window is None else {"start": window.start, "stop": window.stop}})

    async def _apply_changes(self, commands, journal, requested, preinfusion=False):
        client = self._begin()
        try:
            # Exclusive creation happens before any machine request or mutation.
            with open(journal, "x", encoding="utf-8") as log:
                def record(entry):
                    log.write(json.dumps(entry) + "\n")
                    log.flush()
                    os.fsync(log.fileno())

                async def write(command):
                    await client.write_gatt_char(CONTROL_UUID, command, response=True)

                return await apply_verified_changes(commands, requested, self._request_settings, write, record, self.device, preinfusion)
        except BaseException:
            self._uncertain = True
            raise
        finally:
            self._busy = False

    async def brew(self, dose: int | None):
        """EXPERIMENTAL: start/stop a physical brew. ATT acknowledgement is not machine confirmation."""
        commands = brew_commands(dose)
        if not self.allow_experimental_controls:
            raise PermissionError("Experimental controls disabled; opt in explicitly at construction")
        client = self._begin()
        try:
            for command in commands:
                await client.write_gatt_char(CONTROL_UUID, command, response=True)
        except BaseException:
            self._uncertain = True
            raise
        finally:
            self._busy = False
