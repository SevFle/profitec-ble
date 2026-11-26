import asyncio
import json
import subprocess
import sys
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

from profitec_ble import (
    ProfitecClient, ReadyTracker, ShotRecorder, brew_by_weight, decode_settings, decode_status,
    list_shots, metrics, profile_diff, restore_plan, annotate_shot,
)
from profitec_ble.bridge import Bridge

ROOT = Path(__file__).resolve().parents[1]
PRESSURE_UUID = "873ae82b-4c5a-4342-b539-9d900bf7ebd0"
STATUS_UUID = "873ae833-4c5a-4342-b539-9d900bf7ebd0"
DOSES = bytes.fromhex("07d000fa010e0000000017700001001e00320001001e0032000000320032")
GENERAL = bytes.fromhex("0bb9005c007f000000050050fffe001e00010001000100100018005a000f0014001400000016000a0014000100010001")


def status_hex(coffee, state, steam=128):
    packet = bytearray(22)
    packet[:2] = b"\x00\x0c"
    packet[10:12] = coffee.to_bytes(2, "big", signed=True)
    packet[12:14] = steam.to_bytes(2, "big", signed=True)
    packet[17] = state
    return packet.hex()


def shot_rows():
    rows = [("12:00:00", STATUS_UUID, status_hex(92, 0)), ("12:00:01", STATUS_UUID, status_hex(92, 1))]
    for second, bar in ((2, 0.5), (3, 2.0), (4, 7.0), (6, 8.0), (8, 5.0)):
        rows.append((f"12:00:{second:02}", PRESSURE_UUID, int(bar * 1000).to_bytes(2, "big").hex()))
    rows.append(("12:00:10", STATUS_UUID, status_hex(91, 0)))
    return [{"time": f"2026-11-25T{t}+00:00", "uuid": u, "hex": h} for t, u, h in rows]


async def wait_until(condition, timeout=5):
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


def cli(*args):
    return subprocess.run([sys.executable, "-m", "profitec_ble", *args], cwd=ROOT, text=True, capture_output=True, timeout=20)


class OfflineFeatureTests(unittest.TestCase):
    def test_metrics_and_plot_from_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shot.jsonl"
            path.write_text("".join(json.dumps(r) + "\n" for r in shot_rows()), encoding="utf-8")
            shot = metrics(path)["shots"][0]
            self.assertEqual(shot["duration_seconds"], 9.0)
            self.assertEqual(shot["time_to_pressure_seconds"], 2.0)  # first sample >= 1 bar at 12:00:03
            self.assertEqual(shot["seconds_above_6_bar"], 4.0)  # 7 bar held 4->6, 8 bar held 6->8
            self.assertEqual(shot["mean_bar"], 4.5)
            self.assertEqual((shot["coffee_temperature_start"], shot["coffee_temperature_end"]), (92, 91))
            out = Path(directory) / "plot.html"
            result = cli("plot", str(path), str(out), "--compare", str(path))
            self.assertEqual(result.returncode, 0, result.stderr)
            html = out.read_text(encoding="utf-8")
            self.assertEqual(html.count("<polyline"), 2)
            self.assertNotEqual(cli("plot", str(path), str(out)).returncode, 0)  # never overwrites

    def test_shot_recorder_splits_indexes_and_annotates(self):
        rows = shot_rows()
        with tempfile.TemporaryDirectory() as directory:
            recorder = ShotRecorder(directory)
            finished = [recorder.feed(row) for row in rows]
            recorder.close()
            paths = [p for p in finished if p]
            self.assertEqual(len(paths), 1)
            self.assertEqual(len(paths[0].read_text().splitlines()), len(rows))  # includes idle status before start
            self.assertEqual(metrics(paths[0])["shots"][0]["complete"], True)
            annotate_shot(directory, paths[0].name, bean="Kenya", final_weight_g=36.4)
            [row] = list_shots(directory)
            self.assertEqual((row["bean"], row["final_weight_g"], row["duration_seconds"]), ("Kenya", 36.4, 9.0))
            result = cli("shots-index", "--shots-dir", directory)
            self.assertEqual(json.loads(result.stdout), {"indexed": 1})
            self.assertEqual(list_shots(directory)[0]["bean"], "Kenya")  # reindex keeps notes

    def test_profile_diff_and_restore_plan(self):
        blocks = {"doses": decode_settings(DOSES), "general": decode_settings(GENERAL)}
        self.assertEqual(profile_diff(blocks, {"coffee_temperature_c": 92, "dose1_seconds": 26.0}),
                         {"dose1_seconds": {"machine": 25.0, "profile": 26.0}})
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "j.jsonl"
            journal.write_text(json.dumps({"event": "before", "blocks": {"general": GENERAL.hex()},
                                           "requested": {"coffee_temperature_c": 94},
                                           "commands": {"coffee_temperature_c": "0bb9005e"}}) + "\n")
            self.assertEqual(restore_plan(journal), {"settings": {"coffee_temperature_c": 92.0},
                                                     "commands": {"coffee_temperature_c": "0bb9005c"}})
            result = cli("journal-restore", str(journal))
            self.assertEqual(json.loads(result.stdout)["preview"], True)

    def test_ready_needs_hold_within_band_and_idle(self):
        tracker = ReadyTracker(93)
        status = lambda c, s=0: decode_status(bytes.fromhex(status_hex(c, s)))
        self.assertFalse(tracker.update(status(95), 0))     # overshoot outside band
        self.assertFalse(tracker.update(status(93), 1))      # in band; hold starts
        self.assertFalse(tracker.update(status(94), 60))     # only 59 s in band
        self.assertTrue(tracker.update(status(92), 61))
        self.assertFalse(tracker.update(status(92, 8), 62))  # brewing turns it off
        self.assertFalse(tracker.update(status(93), 63))     # hold restarts

class FakeScale:
    def __init__(self):
        self.weight, self.age, self.tares = 0.0, 0.1, 0

    async def tare(self):
        self.tares += 1
        self.weight = 0.0


class FakeMachine:
    device = "fake"
    allow_experimental_controls = True

    def __init__(self):
        self.commands = []
        self.queue = asyncio.Queue()
        self.on_stop = lambda: None

    async def events(self, seconds=None, log=False):
        while True:
            yield await self.queue.get()

    def status(self, state):
        self.queue.put_nowait({"time": "t", "uuid": STATUS_UUID, "hex": status_hex(93, state)})

    async def brew(self, dose):
        self.commands.append(dose)
        if dose is None:
            self.on_stop()
        self.status(1 if dose is not None else 0)


class BrewByWeightTests(unittest.IsolatedAsyncioTestCase):
    async def run_bbw(self, scale, machine, dose, script):
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "bbw.jsonl"
            task = asyncio.create_task(brew_by_weight(machine, scale, 36, dose, journal, settle_seconds=0, start_timeout=1))
            await wait_until(lambda: machine.commands)
            await script()
            result = await asyncio.wait_for(task, 5)
            return result, [json.loads(line)["event"] for line in journal.read_text().splitlines()]

    async def test_a4_stops_once_at_target_minus_offset(self):
        scale, machine = FakeScale(), FakeMachine()

        machine.on_stop = lambda: setattr(scale, "weight", scale.weight + 1.5)  # drips after stop

        async def pour():
            while None not in machine.commands:
                scale.weight += 1
                await asyncio.sleep(0.01)

        result, events = await self.run_bbw(scale, machine, 2, pour)
        self.assertEqual(machine.commands, [2, None])
        self.assertEqual(scale.tares, 1)
        self.assertEqual(result["reason"], "target")
        self.assertGreaterEqual(result["final_weight_g"], 35.5)
        self.assertEqual(events, ["before", "tared", "starting", "started", "stopping", "stop_sent", "final"])

    async def test_a1_scale_loss_lets_timed_dose_finish(self):
        scale, machine = FakeScale(), FakeMachine()

        async def drop():
            scale.weight = 20.0
            await asyncio.sleep(0.05)
            scale.age = float("inf")
            await asyncio.sleep(0.1)
            machine.status(0)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result, events = await self.run_bbw(scale, machine, 1, drop)
        self.assertEqual(machine.commands, [1])  # no stop sent
        self.assertEqual(result["reason"], "scale_lost")
        self.assertIsNone(result["final_weight_g"])
        self.assertEqual(len([w for w in caught if issubclass(w.category, RuntimeWarning)]), 1)
        self.assertIn("scale_lost", events)

    async def test_dose_end_before_target_sends_nothing(self):
        scale, machine = FakeScale(), FakeMachine()

        async def short():
            scale.weight = 20.0
            await asyncio.sleep(0.05)
            machine.status(0)

        result, _ = await self.run_bbw(scale, machine, 1, short)
        self.assertEqual((machine.commands, result["reason"]), ([1], "target_not_reached"))

    async def test_refuses_stale_scale_and_manual_dose(self):
        scale, machine = FakeScale(), FakeMachine()
        scale.age = 5
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(RuntimeError):
                await brew_by_weight(machine, scale, 36, 1, Path(directory) / "j")
            with self.assertRaises(ValueError):
                await brew_by_weight(machine, FakeScale(), 36, 0, Path(directory) / "j")
            self.assertFalse((Path(directory) / "j").exists())
        self.assertEqual(machine.commands, [])


class FakeClient:
    instances = []
    applies = []

    def __init__(self, device, allow_experimental_settings=False, allow_experimental_controls=False):
        self.brews = []
        self.queue = asyncio.Queue()
        self.uncertain = False
        FakeClient.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass

    async def read_settings(self, name):
        return decode_settings({"doses": DOSES, "general": GENERAL}[name])

    async def events(self, seconds=None, log=False):
        while True:
            yield await self.queue.get()

    def status(self, coffee, state):
        self.queue.put_nowait({"time": "2026-11-25T12:00:00+00:00", "uuid": STATUS_UUID, "hex": status_hex(coffee, state)})

    async def apply_settings(self, values, journal):
        FakeClient.applies.append(values)
        readback = bytearray(GENERAL)
        readback[2:4] = (93).to_bytes(2, "big")
        with open(journal, "x") as log:
            for entry in ({"event": "before"}, {"event": "sending"}, {"event": "readback", "hex": readback.hex()}):
                log.write(json.dumps(entry) + "\n")
        self.uncertain = True
        raise RuntimeError("Readback mismatch for coffee_temperature_c; outcome uncertain, inspect journal")

    async def brew(self, dose):
        self.brews.append(dose)


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def start(self, directory, **options):
        FakeClient.instances.clear()
        FakeClient.applies.clear()
        self.published = []

        async def publish(topic, payload, retain):
            self.published.append((topic, payload, retain))

        bridge = Bridge("AA:BB", publish, client=FakeClient, journal_dir=directory, max_backoff=0.01, **options)
        self.runner = asyncio.create_task(bridge.run())
        await wait_until(lambda: bridge.machine is not None)
        return bridge

    async def asyncTearDown(self):
        self.runner.cancel()

    def states(self, name):
        return [p for t, p, _ in self.published if t.endswith(f"/state/{name}")]

    def errors(self):
        return [p for t, p, _ in self.published if t.endswith("/error")]

    async def settle(self, bridge):
        await asyncio.sleep(0.05)

    async def test_a2_readback_mismatch_shows_machine_value_without_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = await self.start(directory)
            first = bridge.machine
            await bridge.handle("profitec/aa_bb/set/coffee_temperature_c", "94")
            self.assertEqual(self.states("coffee_temperature_c")[-1], "93.0")
            self.assertIn("RuntimeError", self.errors()[-1])
            self.assertEqual(len(list(Path(directory).glob("*-coffee_temperature_c.jsonl"))), 1)
            await wait_until(lambda: bridge.machine not in (None, first))  # a6: reconnects after the uncertain write
            await self.settle(bridge)
            self.assertEqual(FakeClient.applies, [{"coffee_temperature_c": 94}])  # never resent

    async def test_a5_remote_brew_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = await self.start(directory)
            bridge.machine.status(92, 0)
            await self.settle(bridge)
            await bridge.handle("profitec/aa_bb/cmd/brew", "1")
            self.assertIn("PermissionError", self.errors()[-1])
            bridge.allow_remote_brew = True
            for coffee, state in ((92, 1), (80, 0)):
                bridge.machine.status(coffee, state)
                await self.settle(bridge)
                await bridge.handle("profitec/aa_bb/cmd/brew", "1")
                self.assertIn("refusing remote brew", self.errors()[-1])
            await bridge.handle("profitec/aa_bb/cmd/brew", "1", retain=True)
            self.assertIn("Retained command ignored", self.errors()[-1])
            self.assertEqual(bridge.machine.brews, [])
            bridge.machine.status(92, 0)
            await self.settle(bridge)
            await bridge.handle("profitec/aa_bb/cmd/brew", "1")
            self.assertEqual(bridge.machine.brews, [1])

    async def test_release_refused_while_brewing_and_discovery_hides_brew_without_flag(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = await self.start(directory)
            bridge.machine.status(92, 8)
            await self.settle(bridge)
            await bridge.handle("profitec/aa_bb/cmd/release", "ON")
            self.assertIn("Bluetooth not released", self.errors()[-1])
            self.assertFalse(bridge._release.is_set())
            self.assertFalse(any("/button/" in topic for topic, _ in bridge.discovery()))
            bridge.allow_remote_brew = True
            self.assertTrue(any("/button/" in topic for topic, _ in bridge.discovery()))


class Sender:
    def __init__(self, uuid):
        self.uuid = uuid


CONTROL_UUID = "873ae834-4c5a-4342-b539-9d900bf7ebd0"


class MachineBleak:
    """Patched in for bleak.BleakClient so the real ProfitecClient runs underneath."""
    instances = []

    def __init__(self, device, timeout):
        self.is_connected = False
        self.callbacks = {}
        self.blocks = {2000: bytearray(DOSES), 3001: bytearray(GENERAL)}
        self.control = []
        MachineBleak.instances.append(self)

    async def connect(self):
        self.is_connected = True

    async def disconnect(self):
        self.is_connected = False

    async def start_notify(self, uuid, callback):
        if uuid in self.callbacks:
            raise RuntimeError("already subscribed")
        self.callbacks[uuid] = callback

    async def stop_notify(self, uuid):
        del self.callbacks[uuid]

    def notify(self, uuid, data):
        if uuid in self.callbacks:
            self.callbacks[uuid](Sender(uuid), bytes(data))

    def status(self, coffee, state):
        self.notify(STATUS_UUID, bytes.fromhex(status_hex(coffee, state)))

    async def write_gatt_char(self, uuid, data, response=True):
        data = bytes(data)
        loop = asyncio.get_running_loop()
        if uuid == STATUS_UUID:
            self.notify(STATUS_UUID, self.blocks[int.from_bytes(data[:2], "big")])
        elif uuid == CONTROL_UUID:
            self.control.append(data.hex())
            if data[:2] == (3001).to_bytes(2, "big"):
                self.blocks[3001][2:4] = (93).to_bytes(2, "big")  # machine stores 93 whatever was asked
            elif data.hex() in ("00650001", "00650002"):
                loop.call_soon(self.status, 93, 1)
            elif data.hex() == "0065000a":
                loop.call_soon(self.status, 93, 0)


class RealClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_bridge_shows_readback_reconnects_once_and_skips_invalid(self):
        MachineBleak.instances.clear()
        published = []

        async def publish(topic, payload, retain):
            published.append((topic, payload))

        with patch("bleak.BleakClient", MachineBleak), tempfile.TemporaryDirectory() as directory:
            bridge = Bridge("AA:BB", publish, journal_dir=directory, max_backoff=0.01)
            runner = asyncio.create_task(bridge.run())
            try:
                await wait_until(lambda: bridge.machine is not None)
                first = bridge.machine
                MachineBleak.instances[-1].status(92, 0)
                await wait_until(lambda: bridge.status is not None)
                await bridge.handle("profitec/aa_bb/set/coffee_temperature_c", "94")
                self.assertEqual([p for t, p in published if t.endswith("state/coffee_temperature_c")][-1], "93.0")
                self.assertEqual(MachineBleak.instances[0].control, ["0bb9005e"])
                await wait_until(lambda: bridge.machine not in (None, first))
                await bridge.handle("profitec/aa_bb/set/eco_minutes", "10")
                await asyncio.sleep(0.1)
                self.assertIn("ValueError", [p for t, p in published if t.endswith("/error")][-1])
                self.assertEqual(len(MachineBleak.instances), 2)  # invalid value: no reconnect
                self.assertEqual(MachineBleak.instances[1].control, [])  # nothing resent
            finally:
                runner.cancel()

    async def test_bbw_start_and_stop_on_one_real_connection(self):
        MachineBleak.instances.clear()
        scale = FakeScale()
        with patch("bleak.BleakClient", MachineBleak), tempfile.TemporaryDirectory() as directory:
            async with ProfitecClient("fake", allow_experimental_controls=True) as machine:
                fake = MachineBleak.instances[-1]

                async def pour():
                    await wait_until(lambda: "00650002" in fake.control)
                    while "0065000a" not in fake.control:
                        scale.weight += 1
                        await asyncio.sleep(0.01)

                pourer = asyncio.create_task(pour())
                result = await brew_by_weight(machine, scale, 36, 2, Path(directory) / "bbw.jsonl",
                                              settle_seconds=0.3, start_timeout=2)
                await pourer
        self.assertEqual(fake.control, ["00660001", "00650002", "0065000a"])
        self.assertEqual((result["reason"], result["stopped_observed"]), ("target", True))


class SharedNotifyBleak:
    """Like Bleak: one callback per characteristic, so a second start_notify would replace the first."""

    def __init__(self):
        self.is_connected = True
        self.callbacks = {}
        self.starts = []
        self.writes = []

    async def start_notify(self, uuid, callback):
        if uuid in self.callbacks:
            raise RuntimeError("already subscribed")
        self.starts.append(uuid)
        self.callbacks[uuid] = callback

    async def stop_notify(self, uuid):
        del self.callbacks[uuid]

    async def write_gatt_char(self, uuid, data, response=True):
        self.writes.append(bytes(data).hex())
        if uuid == STATUS_UUID:
            self.callbacks[STATUS_UUID](Sender(STATUS_UUID), GENERAL)


class ClientConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_read_and_stop_share_one_connection(self):
        machine = ProfitecClient("fake", allow_experimental_controls=True)
        fake = machine._client = SharedNotifyBleak()
        received = []

        async def consume():
            async for packet in machine.events(None):
                received.append(packet["hex"])

        stream = asyncio.create_task(consume())
        await asyncio.sleep(0.01)
        block = await machine.read_settings("general")
        self.assertEqual(block.raw, GENERAL)
        fake.callbacks[STATUS_UUID](Sender(STATUS_UUID), bytes.fromhex(status_hex(92, 0)))
        machine._busy = True  # a settings write in progress must not block stop
        await machine.brew(None)
        await asyncio.sleep(0.6)
        stream.cancel()
        self.assertEqual(fake.starts.count(STATUS_UUID), 1)
        self.assertIn(status_hex(92, 0), received)
        self.assertEqual(fake.writes[-1], "0065000a")


if __name__ == "__main__":
    unittest.main()
