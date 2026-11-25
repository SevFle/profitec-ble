import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "captured-telemetry.jsonl"
PRESSURE_UUID = "873ae82b-4c5a-4342-b539-9d900bf7ebd0"
STATUS_UUID = "873ae833-4c5a-4342-b539-9d900bf7ebd0"
UNKNOWN_UUID = "00000000-0000-0000-0000-000000000000"


def run_cli(*args, input_text=None, timeout=10):
    env = os.environ.copy()
    return subprocess.run(
        [sys.executable, "-m", "profitec_ble", *args],
        cwd=ROOT,
        env=env,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def decode(uuid, hex_data):
    return run_cli("decode", uuid, hex_data)


def json_object(result):
    if result.returncode != 0:
        raise AssertionError(f"CLI failed: {result.stderr}")
    lines = result.stdout.splitlines()
    if len(lines) != 1:
        raise AssertionError(f"expected one JSON line, got {lines!r}")
    return json.loads(lines[0])


def decoded_payload(result):
    return json_object(result)["decoded"]


def assert_human_error(testcase, result):
    testcase.assertNotEqual(result.returncode, 0)
    combined = result.stdout + result.stderr
    testcase.assertNotIn("Traceback", combined)
    testcase.assertTrue(result.stderr.strip(), combined)


def status_hex(coffee, steam, state):
    packet = bytearray(22)
    packet[:2] = b"\x00\x0c"
    packet[10:12] = int(coffee).to_bytes(2, "big", signed=True)
    packet[12:14] = int(steam).to_bytes(2, "big", signed=True)
    packet[17] = state
    return packet.hex()


def settings_hex(address, words):
    packet = bytearray(address.to_bytes(2, "big"))
    for word in words:
        packet.extend(int(word).to_bytes(2, "big", signed=True))
    return packet.hex()


def write_jsonl(path, rows):
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )


def run_python(code, timeout=10):
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


class CliTests(unittest.TestCase):
    def test_help_lists_public_commands(self):
        result = run_cli("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        help_text = result.stdout.lower()
        for command in (
            "decode",
            "replay",
            "scan",
            "inspect",
            "record",
            "settings",
            "summary",
            "export-csv",
            "options",
            "set",
            "profile-save",
            "profile-plan",
            "profile-apply",
        ):
            self.assertIn(command, help_text)

    def test_pyproject_has_build_metadata(self):
        metadata_path = ROOT / "pyproject.toml"
        self.assertTrue(metadata_path.is_file())
        try:
            import tomllib
        except ModuleNotFoundError:  # pragma: no cover - supported Python has tomllib
            self.skipTest("tomllib unavailable")
        metadata = tomllib.loads(metadata_path.read_text())
        self.assertTrue(metadata.get("build-system", {}).get("build-backend"))
        project = metadata.get("project", {})
        self.assertTrue(project.get("name"))
        self.assertTrue(project.get("version"))

    def test_decode_pressure_two_byte_signed_value(self):
        result = decode(PRESSURE_UUID, "f448")  # -3000 millibar
        decoded = decoded_payload(result)
        self.assertEqual(decoded["type"], "Pressure")
        self.assertEqual(decoded["bar"], -3.0)

    def test_decode_pressure_rejects_wrong_lengths(self):
        for hex_data in ("", "00", "000000", "00000000", "0000000000"):
            with self.subTest(hex_data=hex_data):
                assert_human_error(self, decode(PRESSURE_UUID, hex_data))

    def test_decode_status_uses_captured_packet_fields(self):
        decoded = decoded_payload(
            decode(
                STATUS_UUID,
                "000c0001000000000000005c00800000000000010001",
            )
        )
        self.assertEqual(decoded["type"], "MachineStatus")
        self.assertEqual(decoded["coffee_temperature"], 92)
        self.assertEqual(decoded["steam_temperature"], 128)
        self.assertEqual(decoded["state"], 0)
        self.assertFalse(decoded["brewing"])

    def test_decode_status_brewing_state_set(self):
        for state in (1, 2, 3, 4, 5, 8):
            with self.subTest(state=state):
                decoded = decoded_payload(decode(STATUS_UUID, status_hex(-10, 128, state)))
                self.assertEqual(decoded["coffee_temperature"], -10)
                self.assertEqual(decoded["steam_temperature"], 128)
                self.assertEqual(decoded["state"], state)
                self.assertTrue(decoded["brewing"])

        for state in (0, 6, 7, 9, 255):
            with self.subTest(state=state):
                decoded = decoded_payload(decode(STATUS_UUID, status_hex(-10, 128, state)))
                self.assertFalse(decoded["brewing"])

    def test_decode_status_unknown_header_is_preserved(self):
        decoded = decoded_payload(
            decode(STATUS_UUID, "000d" + "00" * 20)
        )
        self.assertEqual(decoded["type"], "RawPacket")
        self.assertEqual(decoded["uuid"], STATUS_UUID)

    def test_decode_status_rejects_wrong_length_for_known_header(self):
        malformed = [
            "000c" + "00" * 19,
            "000c" + "00" * 21,
        ]
        for hex_data in malformed:
            with self.subTest(hex_data=hex_data):
                assert_human_error(self, decode(STATUS_UUID, hex_data))

    def test_decode_firmware_reply_is_settings_block(self):
        decoded = decoded_payload(decode(STATUS_UUID, "00010202"))
        self.assertEqual(decoded["type"], "SettingsBlock")
        self.assertEqual(decoded["address"], 1)
        self.assertEqual(decoded["words"], [514])

    def test_decode_firmware_rejects_wrong_lengths(self):
        for hex_data in ("000102", "0001020200"):
            with self.subTest(hex_data=hex_data):
                assert_human_error(self, decode(STATUS_UUID, hex_data))

    def test_decode_settings_returns_signed_registers_for_all_blocks(self):
        cases = ((2000, 14), (3001, 23), (4000, 34))
        seed = (-32768, -1, 0, 1, 32767)
        for address, count in cases:
            words = [seed[index % len(seed)] for index in range(count)]
            with self.subTest(address=hex(address)):
                decoded = decoded_payload(decode(STATUS_UUID, settings_hex(address, words)))
                self.assertEqual(decoded["type"], "SettingsBlock")
                self.assertEqual(decoded["address"], address)
                self.assertEqual(decoded["words"], words)

    def test_decode_settings_rejects_wrong_block_lengths(self):
        for address, count in ((2000, 13), (3001, 22), (4000, 33)):
            with self.subTest(address=hex(address)):
                assert_human_error(
                    self,
                    decode(STATUS_UUID, settings_hex(address, [0] * count)),
                )

    def test_unknown_notification_is_unknown(self):
        decoded = decoded_payload(decode(UNKNOWN_UUID, "0102ff"))
        self.assertEqual(decoded["type"], "RawPacket")
        self.assertEqual(decoded["uuid"], UNKNOWN_UUID)

    def test_replay_preserves_captured_raw_fields_and_adds_decoded(self):
        input_rows = [json.loads(line) for line in FIXTURE.read_text().splitlines()]
        result = run_cli("replay", str(FIXTURE))
        self.assertEqual(result.returncode, 0, result.stderr)
        output_rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(output_rows), len(input_rows))
        for source, output in zip(input_rows, output_rows):
            self.assertEqual(
                {key: output[key] for key in ("time", "uuid", "hex")},
                source,
            )
            self.assertIn("decoded", output)
            self.assertIn(output["decoded"]["type"], ("Pressure", "MachineStatus"))

    def test_replay_preserves_unknown_notification(self):
        rows = [
            {
                "time": "2026-11-25T12:50:00+00:00",
                "uuid": UNKNOWN_UUID,
                "hex": "deadbeef",
            }
        ]
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl") as input_file:
            input_file.write(json.dumps(rows[0]) + "\n")
            input_file.flush()
            result = run_cli("replay", input_file.name)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(
            {key: output[key] for key in ("time", "uuid", "hex")},
            rows[0],
        )
        self.assertEqual(output["decoded"]["type"], "RawPacket")

    def test_replay_rejects_invalid_json(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl") as input_file:
            input_file.write("{not json}\n")
            input_file.flush()
            result = run_cli("replay", input_file.name)
        assert_human_error(self, result)

    def test_replay_rejects_malformed_known_packet(self):
        row = {"time": "t", "uuid": PRESSURE_UUID, "hex": "00"}
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl") as input_file:
            input_file.write(json.dumps(row) + "\n")
            input_file.flush()
            result = run_cli("replay", input_file.name)
        assert_human_error(self, result)

    def test_replay_rejects_missing_uuid_or_hex(self):
        rows = (
            {"time": "t", "hex": "0000"},
            {"time": "t", "uuid": PRESSURE_UUID},
        )
        for row in rows:
            with self.subTest(row=row), tempfile.NamedTemporaryFile(
                "w", suffix=".jsonl"
            ) as input_file:
                input_file.write(json.dumps(row) + "\n")
                input_file.flush()
                result = run_cli("replay", input_file.name)
            assert_human_error(self, result)

    def test_summary_complete_shot_uses_only_window_pressure(self):
        rows = [
            {"time": "2026-11-25T12:00:00+00:00", "uuid": STATUS_UUID, "hex": status_hex(92, 128, 0)},
            {"time": "2026-11-25T12:00:01+00:00", "uuid": PRESSURE_UUID, "hex": "03e8"},
            {"time": "2026-11-25T12:00:02+00:00", "uuid": STATUS_UUID, "hex": status_hex(92, 128, 1)},
            {"time": "2026-11-25T12:00:03+00:00", "uuid": PRESSURE_UUID, "hex": "0bb8"},
            {"time": "2026-11-25T12:00:04+00:00", "uuid": UNKNOWN_UUID, "hex": "deadbeef"},
            {"time": "2026-11-25T12:00:05+00:00", "uuid": PRESSURE_UUID, "hex": "07d0"},
            {"time": "2026-11-25T12:00:06+00:00", "uuid": STATUS_UUID, "hex": status_hex(92, 128, 0)},
            {"time": "2026-11-25T12:00:07+00:00", "uuid": PRESSURE_UUID, "hex": "0fa0"},
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "shot.jsonl"
            write_jsonl(input_path, rows)
            result = run_cli("summary", str(input_path))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "shots": [
                    {
                        "start_time": "2026-11-25T12:00:02+00:00",
                        "end_time": "2026-11-25T12:00:06+00:00",
                        "duration_seconds": 4.0,
                        "peak_bar": 3.0,
                        "pressure_samples": 2,
                        "complete": True,
                    }
                ],
                "packet_count": 8,
                "decode_errors": 0,
            },
        )

    def test_summary_marks_already_brewing_start_incomplete(self):
        rows = [
            {"time": "2026-11-25T12:01:00+00:00", "uuid": STATUS_UUID, "hex": status_hex(92, 128, 1)},
            {"time": "2026-11-25T12:01:01+00:00", "uuid": PRESSURE_UUID, "hex": "05dc"},
            {"time": "2026-11-25T12:01:02+00:00", "uuid": STATUS_UUID, "hex": status_hex(92, 128, 0)},
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "already-brewing.jsonl"
            write_jsonl(input_path, rows)
            result = run_cli("summary", str(input_path))
        self.assertEqual(result.returncode, 0, result.stderr)
        shot = json.loads(result.stdout)["shots"]
        self.assertEqual(
            shot,
            [
                {
                    "start_time": None,
                    "end_time": "2026-11-25T12:01:02+00:00",
                    "duration_seconds": None,
                    "peak_bar": 1.5,
                    "pressure_samples": 1,
                    "complete": False,
                }
            ],
        )

    def test_summary_marks_missing_stop_incomplete(self):
        rows = [
            {"time": "2026-11-25T12:02:00+00:00", "uuid": STATUS_UUID, "hex": status_hex(92, 128, 0)},
            {"time": "2026-11-25T12:02:01+00:00", "uuid": STATUS_UUID, "hex": status_hex(92, 128, 8)},
            {"time": "2026-11-25T12:02:02+00:00", "uuid": PRESSURE_UUID, "hex": "07d0"},
            {"time": "2026-11-25T12:02:03+00:00", "uuid": UNKNOWN_UUID, "hex": "deadbeef"},
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "missing-stop.jsonl"
            write_jsonl(input_path, rows)
            result = run_cli("summary", str(input_path))
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["packet_count"], 4)
        self.assertEqual(payload["decode_errors"], 0)
        self.assertEqual(
            payload["shots"],
            [
                {
                    "start_time": "2026-11-25T12:02:01+00:00",
                    "end_time": None,
                    "duration_seconds": None,
                    "peak_bar": 2.0,
                    "pressure_samples": 1,
                    "complete": False,
                }
            ],
        )

    def test_summary_captured_fixture_has_no_fabricated_shot(self):
        result = run_cli("summary", str(FIXTURE))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {"shots": [], "packet_count": 3, "decode_errors": 0},
        )

    def test_summary_rejects_invalid_timestamp_nonmonotonic_and_malformed_input(self):
        cases = (
            [
                {"time": "not-a-timestamp", "uuid": PRESSURE_UUID, "hex": "0000"},
            ],
            [
                {"time": "2026-11-25T12:03:01+00:00", "uuid": PRESSURE_UUID, "hex": "0000"},
                {"time": "2026-11-25T12:03:00+00:00", "uuid": PRESSURE_UUID, "hex": "0000"},
            ],
            [
                {"time": "2026-11-25T12:03:00+00:00", "uuid": PRESSURE_UUID, "hex": "00"},
            ],
        )
        for rows in cases:
            with self.subTest(rows=rows), tempfile.TemporaryDirectory() as temp_dir:
                input_path = Path(temp_dir) / "invalid.jsonl"
                write_jsonl(input_path, rows)
                result = run_cli("summary", str(input_path))
            assert_human_error(self, result)
            self.assertNotIn('"shots"', result.stdout)

    def test_export_csv_redecodes_source_and_preserves_rows(self):
        rows = [
            {
                "time": "2026-11-25T12:04:00+00:00",
                "uuid": PRESSURE_UUID,
                "hex": "05dc",
                "decoded": {"type": "MachineStatus", "bar": 999},
            },
            {
                "time": "2026-11-25T12:04:01+00:00",
                "uuid": STATUS_UUID,
                "hex": status_hex(92, 128, 0),
                "decoded": {"type": "Pressure", "bar": 999},
            },
            {
                "time": "2026-11-25T12:04:02+00:00",
                "uuid": UNKNOWN_UUID,
                "hex": "deadbeef",
                "decoded": {"type": "Pressure", "bar": 999},
            },
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "input.jsonl"
            output_path = Path(temp_dir) / "output.csv"
            write_jsonl(input_path, rows)
            before = input_path.read_text()
            result = run_cli("export-csv", str(input_path), str(output_path))
            after = input_path.read_text()
            self.assertEqual(before, after)
            self.assertEqual(result.returncode, 0, result.stderr)
            with output_path.open(newline="", encoding="utf-8") as output:
                reader = csv.DictReader(output)
                self.assertEqual(
                    reader.fieldnames,
                    [
                        "time",
                        "uuid",
                        "hex",
                        "type",
                        "pressure_bar",
                        "coffee_temperature",
                        "steam_temperature",
                        "state",
                        "brewing",
                    ],
                )
                output_rows = list(reader)
        self.assertEqual(len(output_rows), len(rows))
        pressure, status, unknown = output_rows
        self.assertEqual(pressure["time"], rows[0]["time"])
        self.assertEqual(pressure["uuid"], PRESSURE_UUID)
        self.assertEqual(pressure["hex"], "05dc")
        self.assertEqual(pressure["type"], "Pressure")
        self.assertEqual(pressure["pressure_bar"], "1.5")
        self.assertEqual(pressure["coffee_temperature"], "")
        self.assertEqual(status["type"], "MachineStatus")
        self.assertEqual(status["pressure_bar"], "")
        self.assertEqual(status["coffee_temperature"], "92")
        self.assertEqual(status["steam_temperature"], "128")
        self.assertEqual(status["state"], "0")
        self.assertEqual(status["brewing"].lower(), "false")
        self.assertEqual(unknown["type"], "RawPacket")
        for field in ("pressure_bar", "coffee_temperature", "steam_temperature", "state", "brewing"):
            self.assertEqual(unknown[field], "")

    def test_export_csv_refuses_to_overwrite_and_keeps_source(self):
        rows = [{"time": "2026-11-25T12:05:00+00:00", "uuid": PRESSURE_UUID, "hex": "0000"}]
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "input.jsonl"
            output_path = Path(temp_dir) / "output.csv"
            write_jsonl(input_path, rows)
            before = input_path.read_text()
            output_path.write_text("keep-me\n")
            result = run_cli("export-csv", str(input_path), str(output_path))
            self.assertEqual(input_path.read_text(), before)
            self.assertEqual(output_path.read_text(), "keep-me\n")
        assert_human_error(self, result)
        self.assertRegex(result.stderr.lower(), r"exist|overwrite|replace|already")

    def test_record_refuses_to_overwrite_existing_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "capture.jsonl"
            output_path.write_text("keep-me\n")
            result = run_cli("record", "not-a-device", str(output_path), "--seconds", "1")
            assert_human_error(self, result)
            self.assertRegex(
                result.stderr.lower(),
                r"exist|overwrite|replace|already",
            )
            self.assertEqual(output_path.read_text(), "keep-me\n")

    def test_public_settings_requests_match_captured_wire(self):
        result = run_python(
            """
import json
from profitec_ble import settings_request
names = ("firmware", "doses", "general", "time")
print(json.dumps({name: settings_request(name).hex() for name in names}, sort_keys=True))
"""
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "doses": "07d00e",
                "firmware": "000101",
                "general": "0bb917",
                "time": "0fa022",
            },
        )

    def test_public_firmware_block_and_status_decoder_validation(self):
        result = run_python(
            """
import json
from profitec_ble import decode_settings, decode_status
block = decode_settings(bytes.fromhex("00010202"))
try:
    decode_status(bytes.fromhex("000d" + "00" * 20))
except ValueError as error:
    status_error = type(error).__name__
else:
    raise SystemExit("decode_status accepted an unknown header")
print(json.dumps({"address": block.address, "words": list(block.words), "firmware": block.firmware, "status_error": status_error}))
"""
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "address": 1,
                "words": [514],
                "firmware": "2.02",
                "status_error": "ValueError",
            },
        )

    def test_public_named_settings_match_saved_live_blocks(self):
        result = run_python(
            """
import json
from dataclasses import asdict
from profitec_ble import decode_settings, named_settings

blocks = {
    "firmware": "00010202",
    "doses": "07d000fa010e0000000017700001001e00320001001e0032000000320032",
    "general": "0bb9005c007f000000050050fffe001e00010001000100100018005a000f0014001400000016000a0014000100010001",
    "time": "0fa00004000f001b0018000000006464646464646464646464646464646464646464646464646464646464646464646464646464646464646464646464646464646464646464",
}
values = {}
for name, raw in blocks.items():
    value = named_settings(decode_settings(bytes.fromhex(raw)))
    values[name] = value if isinstance(value, str) else asdict(value)
print(json.dumps(values, sort_keys=True))
"""
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "firmware": "2.02",
                "doses": {
                    "dose1_seconds": 25.0,
                    "dose2_seconds": 27.0,
                    "dose1_preinfusion": {
                        "enabled": True,
                        "pause_start_seconds": 3.0,
                        "pause_seconds": 5.0,
                    },
                    "dose2_preinfusion": {
                        "enabled": True,
                        "pause_start_seconds": 3.0,
                        "pause_seconds": 5.0,
                    },
                    "weight_preinfusion": {
                        "enabled": False,
                        "pause_start_seconds": 5.0,
                        "pause_seconds": 5.0,
                    },
                },
                "general": {
                    "coffee_temperature_c": 92,
                    "steam_temperature_c": 127,
                    "cleaning_reminder_shots": 50,
                    "water_filter_litres": 80,
                    "eco_minutes": 30,
                    "temperature_unit": "C",
                    "spotlight": "always_on",
                    "fast_heat_up": True,
                    "steam_enabled": True,
                },
                "time": {
                    "weekday_code": 4,
                    "hour": 15,
                    "minute": 27,
                    "second": 24,
                    "days": [[None, None] for _ in range(7)],
                },
            },
        )

    def test_public_setting_commands_match_wire_encodings(self):
        result = run_python(
            """
import json
from profitec_ble import setting_command

values = {
    "coffee_temperature_c": 93,
    "steam_temperature_c": 127,
    "dose1_seconds": 25.0,
    "dose2_seconds": 27.0,
    "cleaning_reminder_shots": 50,
    "water_filter_litres": 80,
    "eco_minutes": 30,
    "temperature_unit": "C",
    "spotlight": "always_on",
    "fast_heat_up": True,
    "steam_enabled": True,
    "dose1_preinfusion_enabled": True,
    "dose1_pause_start_seconds": 3.0,
    "dose1_pause_seconds": 5.0,
    "dose2_preinfusion_enabled": True,
    "dose2_pause_start_seconds": 3.0,
    "dose2_pause_seconds": 5.0,
    "weight_preinfusion_enabled": False,
    "weight_pause_start_seconds": 5.0,
    "weight_pause_seconds": 5.0,
}
print(json.dumps({name: setting_command(name, value).hex() for name, value in values.items()}, sort_keys=True))
"""
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "coffee_temperature_c": "0bb9005d",
                "steam_temperature_c": "0bba007f",
                "dose1_seconds": "07d000fa",
                "dose2_seconds": "07d1010e",
                "cleaning_reminder_shots": "0bbc0005",
                "water_filter_litres": "0bbd0050",
                "eco_minutes": "0bbf001e",
                "temperature_unit": "0bc00001",
                "spotlight": "0bc10001",
                "fast_heat_up": "0bc20001",
                "steam_enabled": "0bcf0001",
                "dose1_preinfusion_enabled": "07d50001",
                "dose1_pause_start_seconds": "07d6001e",
                "dose1_pause_seconds": "07d70032",
                "dose2_preinfusion_enabled": "07d80001",
                "dose2_pause_start_seconds": "07d9001e",
                "dose2_pause_seconds": "07da0032",
                "weight_preinfusion_enabled": "07db0000",
                "weight_pause_start_seconds": "07dc0032",
                "weight_pause_seconds": "07dd0032",
            },
        )

    def test_public_setting_commands_reject_invalid_values(self):
        result = run_python(
            """
from math import nan
from profitec_ble import setting_command

cases = (
    ("fast_heat_up", 1),
    ("dose1_preinfusion_enabled", 1),
    ("dose1_seconds", nan),
    ("coffee_temperature_c", 84),
    ("steam_temperature_c", 135),
    ("eco_minutes", 20),
    ("dose1_seconds", 1.05),
)
for name, value in cases:
    try:
        setting_command(name, value)
    except ValueError:
        continue
    raise SystemExit(f"accepted invalid setting: {name}={value!r}")
print("all rejected")
"""
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "all rejected")

    def test_public_schedule_commands_match_wire_order_and_disabled_encoding(self):
        result = run_python(
            """
import json
from profitec_ble import ScheduleWindow, schedule_command
print(json.dumps({
    "first": schedule_command(0, 0, ScheduleWindow(start="07:30", stop="09:45")).hex(),
    "last": schedule_command(6, 1, ScheduleWindow(start="23:59", stop="00:00")).hex(),
    "disabled": schedule_command(0, 0, None).hex(),
}, sort_keys=True))
"""
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "first": "0fa6092d071e",
                "last": "0fc00000173b",
                "disabled": "0fa664646464",
            },
        )

    def test_public_schedule_command_rejects_invalid_time_and_indices(self):
        result = run_python(
            """
from profitec_ble import ScheduleWindow, schedule_command

cases = (
    lambda: schedule_command(-1, 0, None),
    lambda: schedule_command(7, 0, None),
    lambda: schedule_command(0, 2, None),
    lambda: schedule_command(0, 0, ScheduleWindow(start="7:30", stop="09:45")),
    lambda: schedule_command(0, 0, ScheduleWindow(start="24:00", stop="09:45")),
    lambda: schedule_command(0, 0, ScheduleWindow(start="07:60", stop="09:45")),
    lambda: schedule_command(0, 0, "07:30-09:45"),
)
for make_command in cases:
    try:
        make_command()
    except ValueError:
        continue
    raise SystemExit("accepted invalid schedule")
print("all rejected")
"""
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "all rejected")

    def test_public_apply_schedule_gates_and_verifies_with_clock_drift(self):
        result = run_python(
            """
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import bleak
from profitec_ble import ProfitecClient, ScheduleWindow

TIME = bytes.fromhex("0fa00004000f001b0018000000006464646464646464646464646464646464646464646464646464646464646464646464646464646464646464646464646464646464646464")
STATUS_UUID = "873ae833-4c5a-4342-b539-9d900bf7ebd0"
CONTROL_UUID = "873ae834-4c5a-4342-b539-9d900bf7ebd0"
INSTANCES = []
JOURNAL = None

class Sender:
    def __init__(self, uuid):
        self.uuid = uuid

class FakeBleakClient:
    def __init__(self, device, timeout):
        self.is_connected = False
        self.callbacks = {}
        self.block = bytearray(TIME)
        self.reads = 0
        self.writes = []
        self.durable = []
        INSTANCES.append(self)

    async def connect(self):
        self.is_connected = True

    async def disconnect(self):
        self.is_connected = False

    async def start_notify(self, uuid, callback):
        self.callbacks[uuid] = callback

    async def stop_notify(self, uuid):
        self.callbacks.pop(uuid, None)

    async def write_gatt_char(self, uuid, data, response=True):
        data = bytes(data)
        if uuid == STATUS_UUID:
            self.reads += 1
            block = bytearray(self.block)
            if self.reads >= 2:
                block[2:10] = bytes((0, 4, 0, 16, 0, 28, 0, 25))
            self.callbacks[STATUS_UUID](Sender(STATUS_UUID), bytes(block))
        elif uuid == CONTROL_UUID:
            self.writes.append(data.hex())
            self.durable.append(bool(Path(JOURNAL).read_text()))
            self.block[14:18] = data[2:6]

bleak.BleakClient = FakeBleakClient

async def main():
    global JOURNAL
    with TemporaryDirectory() as directory:
        root = Path(directory)
        default_path = root / "default-schedule-journal.jsonl"
        try:
            await ProfitecClient("default").apply_schedule(0, 0, None, str(default_path))
        except PermissionError:
            default_gate = "PermissionError"
        else:
            default_gate = "accepted"
        path = root / "schedule.jsonl"
        JOURNAL = str(path)
        async with ProfitecClient("fake", allow_experimental_settings=True) as machine:
            await machine.apply_schedule(0, 0, ScheduleWindow(start="07:30", stop="09:45"), str(path))
        fake = INSTANCES[-1]
        events = [json.loads(line)["event"] for line in path.read_text().splitlines()]
        print(json.dumps({
            "default_gate": default_gate,
            "clients_before_gate": len(INSTANCES) - 1,
            "writes": fake.writes,
            "durable": fake.durable,
            "events": events,
        }, sort_keys=True))

asyncio.run(main())
"""
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "default_gate": "PermissionError",
                "clients_before_gate": 0,
                "writes": ["0fa6092d071e"],
                "durable": [True],
                "events": ["before", "sending", "readback", "verified"],
            },
        )

    def test_profile_plan_and_apply_preview_are_offline(self):
        profile = {
            "schema": 1,
            "name": "morning",
            "settings": {
                "coffee_temperature_c": 93,
                "dose1_seconds": 25.0,
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            profile_path = Path(temp_dir) / "morning.json"
            profile_path.write_text(json.dumps(profile) + "\n", encoding="utf-8")
            plan = run_cli("profile-plan", str(profile_path))
            preview = run_cli("profile-apply", "not-a-device", str(profile_path))
        expected = {
            "name": "morning",
            "settings": profile["settings"],
            "commands": {
                "coffee_temperature_c": "0bb9005d",
                "dose1_seconds": "07d000fa",
            },
        }
        self.assertEqual(plan.returncode, 0, plan.stderr)
        self.assertEqual(json.loads(plan.stdout), expected)
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertEqual(
            json.loads(preview.stdout),
            {"preview": True, "settings": profile["settings"], "commands": expected["commands"]},
        )

    def test_set_preview_is_offline_and_encodes_json_value(self):
        result = run_cli("set", "not-a-device", "fast_heat_up", "true")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "preview": True,
                "settings": {"fast_heat_up": True},
                "commands": {"fast_heat_up": "0bc20001"},
            },
        )

    def test_profile_validation_rejects_duplicate_and_unknown_settings(self):
        cases = (
            '{"schema":1,"name":"a","name":"b","settings":{"coffee_temperature_c":93}}\n',
            json.dumps({"schema": 1, "name": "a", "settings": {"not_a_setting": 1}}) + "\n",
            json.dumps({"schema": 2, "name": "a", "settings": {"coffee_temperature_c": 93}}) + "\n",
            json.dumps({"schema": 1, "name": "", "settings": {"coffee_temperature_c": 93}}) + "\n",
            json.dumps({"schema": 1, "name": "a", "settings": {}}) + "\n",
        )
        for text in cases:
            with self.subTest(text=text), tempfile.TemporaryDirectory() as temp_dir:
                profile_path = Path(temp_dir) / "invalid.json"
                profile_path.write_text(text, encoding="utf-8")
                result = run_cli("profile-plan", str(profile_path))
            assert_human_error(self, result)

    def test_public_profile_save_load_and_overwrite_rejection(self):
        result = run_python(
            """
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from profitec_ble import load_profile, save_profile

with TemporaryDirectory() as directory:
    path = Path(directory) / "profile.json"
    save_profile(path, "morning", {"coffee_temperature_c": 93})
    loaded = load_profile(path)
    try:
        save_profile(path, "again", {"coffee_temperature_c": 94})
    except FileExistsError:
        overwrite = "rejected"
    else:
        overwrite = "accepted"
    print(json.dumps({"loaded": loaded, "overwrite": overwrite}, sort_keys=True))
"""
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "loaded": {
                    "schema": 1,
                    "name": "morning",
                    "settings": {"coffee_temperature_c": 93},
                },
                "overwrite": "rejected",
            },
        )

    def test_public_apply_settings_transaction_guards_and_ordering(self):
        result = run_python(
            """
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import bleak
from profitec_ble import CONTROL_UUID, ProfitecClient

DOSES = bytes.fromhex("07d000fa010e0000000017700001001e00320001001e0032000000320032")
GENERAL = bytes.fromhex("0bb9005c007f000000050050fffe001e00010001000100100018005a000f0014001400000016000a0014000100010001")
STATUS_UUID = "873ae833-4c5a-4342-b539-9d900bf7ebd0"
CONTROL_UUID = "873ae834-4c5a-4342-b539-9d900bf7ebd0"
INSTANCES = []
JOURNAL = None

class Sender:
    def __init__(self, uuid):
        self.uuid = uuid

class FakeBleakClient:
    def __init__(self, device, timeout):
        self.mode = device
        self.is_connected = False
        self.callbacks = {}
        self.blocks = {"doses": bytearray(DOSES), "general": bytearray(GENERAL)}
        self.reads = {"doses": 0, "general": 0}
        self.control_writes = []
        self.durable_before_write = []
        INSTANCES.append(self)

    async def connect(self):
        self.is_connected = True

    async def disconnect(self):
        self.is_connected = False

    async def start_notify(self, uuid, callback):
        self.callbacks[uuid] = callback

    async def stop_notify(self, uuid):
        self.callbacks.pop(uuid, None)

    async def write_gatt_char(self, uuid, data, response=True):
        data = bytes(data)
        if uuid == STATUS_UUID:
            address = int.from_bytes(data[:2], "big")
            name = {2000: "doses", 3001: "general"}[address]
            self.reads[name] += 1
            block = self.blocks[name]
            if self.mode == "changed" and name == "doses" and self.reads[name] >= 2:
                block = bytearray(block)
                block[2] ^= 1
            self.callbacks[STATUS_UUID](Sender(STATUS_UUID), bytes(block))
            return
        if uuid == CONTROL_UUID:
            self.control_writes.append(data.hex())
            self.durable_before_write.append(bool(Path(JOURNAL).read_text()))
            if self.mode != "mismatch":
                register = int.from_bytes(data[:2], "big")
                name, base = ("doses", 2000) if register < 3000 else ("general", 3001)
                offset = 2 + (register - base) * 2
                self.blocks[name][offset:offset + 2] = data[2:4]

bleak.BleakClient = FakeBleakClient

async def apply(mode, values, path):
    global JOURNAL
    JOURNAL = str(path)
    try:
        async with ProfitecClient(mode, allow_experimental_settings=True) as machine:
            await machine.apply_settings(values, str(path))
    except Exception as error:
        outcome = {"error": type(error).__name__}
    else:
        outcome = {"ok": True}
    fake = INSTANCES[-1]
    outcome["writes"] = fake.control_writes
    outcome["durable_before_write"] = fake.durable_before_write
    outcome["reads"] = fake.reads
    outcome["journal_events"] = [json.loads(line)["event"] for line in Path(path).read_text().splitlines() if line.startswith("{")] if Path(path).exists() else []
    return outcome

async def main():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        default_path = root / "default.jsonl"
        invalid_path = root / "invalid.jsonl"
        try:
            await ProfitecClient("default").apply_settings({"dose1_seconds": 26.0}, str(default_path))
        except PermissionError:
            default_gate = "PermissionError"
        else:
            default_gate = "accepted"
        try:
            await ProfitecClient("invalid", allow_experimental_settings=True).apply_settings({"dose1_seconds": 1.05}, str(invalid_path))
        except ValueError:
            invalid_preflight = "ValueError"
        else:
            invalid_preflight = "accepted"

        existing_path = root / "existing.jsonl"
        existing_path.write_text("keep\\n")
        existing = await apply("existing", {"dose1_seconds": 26.0}, existing_path)
        existing["journal_unchanged"] = existing_path.read_text() == "keep\\n"

        success = await apply("success", {"dose1_seconds": 26.0}, root / "success.jsonl")
        changed = await apply("changed", {"dose1_seconds": 26.0}, root / "changed.jsonl")
        mismatch = await apply("mismatch", {"dose1_seconds": 26.0, "dose2_seconds": 27.0}, root / "mismatch.jsonl")
        print(json.dumps({
            "default_gate": default_gate,
            "invalid_preflight": invalid_preflight,
            "ble_clients_created_before_guards": len(INSTANCES) - 4,
            "existing": existing,
            "success": success,
            "changed": changed,
            "mismatch": mismatch,
        }, sort_keys=True))

asyncio.run(main())
"""
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["default_gate"], "PermissionError")
        self.assertEqual(output["invalid_preflight"], "ValueError")
        self.assertEqual(output["ble_clients_created_before_guards"], 0)
        self.assertEqual(output["existing"]["error"], "FileExistsError")
        self.assertEqual(output["existing"]["writes"], [])
        self.assertTrue(output["existing"]["journal_unchanged"])
        self.assertEqual(output["success"]["ok"], True)
        self.assertEqual(output["success"]["writes"], ["07d00104"])
        self.assertEqual(output["success"]["durable_before_write"], [True])
        self.assertEqual(output["success"]["journal_events"], ["before", "sending", "readback", "verified"])
        self.assertEqual(output["changed"]["error"], "RuntimeError")
        self.assertEqual(output["changed"]["writes"], [])
        self.assertEqual(output["changed"]["journal_events"], ["before"])
        self.assertEqual(output["mismatch"]["error"], "RuntimeError")
        self.assertEqual(output["mismatch"]["writes"], ["07d00104"])
        self.assertEqual(output["mismatch"]["durable_before_write"], [True])
        self.assertEqual(output["mismatch"]["journal_events"], ["before", "sending", "readback"])

    def test_public_brew_commands_match_captured_encodings(self):
        result = run_python(
            """
import json
from profitec_ble import brew_commands
print(json.dumps({
    "none": [command.hex() for command in brew_commands(None)],
    "0": [command.hex() for command in brew_commands(0)],
    "1": [command.hex() for command in brew_commands(1)],
    "2": [command.hex() for command in brew_commands(2)],
}, sort_keys=True))
"""
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "none": ["0065000a"],
                "0": ["00660001", "00650008"],
                "1": ["00660001", "00650001"],
                "2": ["00660001", "00650002"],
            },
        )

    def test_default_brew_gate_blocks_before_connection(self):
        result = run_python(
            """
import asyncio
from profitec_ble import ProfitecClient

async def main():
    try:
        await ProfitecClient("not-a-device").brew(1)
    except PermissionError as error:
        print(type(error).__name__)
    else:
        raise SystemExit("default brew gate did not reject")

asyncio.run(main())
"""
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "PermissionError")


if __name__ == "__main__":
    unittest.main()
