# profitec-ble

An unofficial Python interoperability library for the **Profitec MOVE 2.0**, based on reverse-engineering research and observed Bluetooth traffic. Python 3.11+. Async Bluetooth transport uses Bleak 3.1.0. Protocol decoders also work offline without a Bluetooth adapter.

## Install

```sh
python -m venv .venv
# macOS / Linux
source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install "git+https://github.com/SevFle/profitec-ble.git"
profitec-ble --help
```

For development, clone this repository and run `python -m pip install -e .` in the virtual environment. This project is not yet published on PyPI. Install only on a computer whose Bluetooth adapter and operating system are supported by Bleak.

Use a separate virtual environment on another computer. Bleak supports macOS, Linux and Windows; only this Mac and this machine have been exercised. macOS uses a device UUID; Linux and Windows generally use an address. Close the phone app before connecting. Grant Bluetooth permission to your terminal when prompted.

## Commands

```sh
profitec-ble scan
profitec-ble inspect DEVICE
profitec-ble record DEVICE shot.jsonl --seconds 120
profitec-ble replay shot.jsonl
profitec-ble decode 873ae82b-4c5a-4342-b539-9d900bf7ebd0 0f69
profitec-ble settings DEVICE
```

`record` subscribes to pressure and machine status. Every line contains UTC `time`, characteristic `uuid`, original `hex`, and decoded values or a `decode_error`. Existing output files are never overwritten. Interrupted or failed recordings are partial and remain available. Unknown packets remain raw. `replay` accepts this format and the earlier `ble_capture.py` JSONL format. Malformed known packets are retained with an error. `decode` exits nonzero on malformed data. `replay` processes all valid JSON records and exits nonzero if any packet has a decoding error.

`settings` requests firmware, doses/pre-infusion, general settings and clock/schedules sequentially. It writes **read requests** to characteristic 833, then waits for notifications. It does not change settings. Blocks expose their original bytes, signed 16-bit words, and named settings recovered from the app. The firmware block additionally provides a formatted version.

## Python API

```python
import asyncio
from contextlib import aclosing
from profitec_ble import ProfitecClient, decode

async def main():
    async with ProfitecClient("DEVICE") as machine:
        firmware = await machine.read_settings("firmware")
        print(firmware.firmware)
        async with aclosing(machine.events(seconds=30)) as events:
            async for packet in events:
                value = decode(packet["uuid"], bytes.fromhex(packet["hex"]))
                print(value)

asyncio.run(main())
```

Use `aclosing` when a consumer may stop early. Each connection permits one settings read or control operation at a time, beside any number of telemetry streams; a stop is always sent. Failures propagate; request timeouts are not retried. Reconnect after a failed request to avoid accepting a late response as a new one. A slow telemetry consumer raises `BufferError` instead of silently discarding packets.

Pure functions: `decode_pressure`, `decode_status`, `decode_settings`, `settings_request`, `decode`, and `brew_commands`. Results are frozen dataclasses with raw bytes. `MachineStatus` exposes `coffee_temperature`, `steam_temperature`, `state`, and `brewing`. The app converts wire temperatures from Celsius for Fahrenheit display; the telemetry fields retain raw Celsius values.

## Experimental controls

`brew_commands(dose)` generates bytes offline: `1` and `2` select doses, `0` selects manual brew, and `None` stops. `ProfitecClient.brew(dose)` refuses execution unless constructed with `allow_experimental_controls=True`. A stop (`None`) is sent even while a settings request is running or after a failed request. There is no direct control CLI; remote brewing exists only through the opt-in MQTT bridge below. Bluetooth acknowledgement does not prove that the machine started or stopped. Do not retry an uncertain brew command automatically. Keep physical control of the machine when testing.

The package does not implement arbitrary register writes, firmware updates, scale pairing, calibration, or app history/cloud services. See FEATURES.md for the precise coverage and gaps. No automatic initialization writes or auto-reconnect loops are hidden in the client; only the `bridge` daemon reconnects, and it never resends a command.

## Evidence and development

See [PROTOCOL.md](PROTOCOL.md) for wire mappings and [NOTICE.md](NOTICE.md) for provenance and third-party rights. APKs, reverse-engineering artifacts, and private capture sessions are not published. The Android app is not bundled or executed.

Run independent CLI tests from this directory:

```sh
python -m unittest discover -s tests -v
```

This is version 0.3.0, an unofficial research library. Research against app version 3.8 and recordings from one MOVE 2.0 with firmware 2.02 support the current mapping; they do not establish compatibility with every firmware version. See [VERIFICATION.md](VERIFICATION.md) for executed checks.

## Named settings and local presets

`settings DEVICE` now includes named dose, pre-infusion, temperature, reminder, Eco Mode, spotlight, steam and fast-heat-up values. It also decodes the machine clock and two on/off schedule windows per wire day. `named_settings(block)` provides the corresponding frozen dataclass. Raw blocks remain available. Wire schedule day order is deliberately exposed as indices 0–6 until weekday naming is verified against the machine UI. An inactive window is `None`.

`options` lists the 20 supported scalar settings, wire registers, bounds and choices. Temperature values are Celsius even when the machine display is Fahrenheit. Dose times exclude pre-infusion. Decimal duration steps are 0.1 second; invalid values are rejected rather than rounded. Eco minutes are 0 (off) or 30–600 in steps of 10. Changing reminders may affect when maintenance prompts appear; it does not perform maintenance.

```sh
profitec-ble options
profitec-ble profile-save DEVICE baseline.json --name 'My baseline'
profitec-ble profile-plan baseline.json
profitec-ble set DEVICE spotlight '"off"'
```

The `set` command above only previews bytes. To actually apply a setting, add `--apply --journal NEW-FILE.jsonl`. Similarly, `profile-apply DEVICE FILE` previews a preset; `--apply --journal NEW-FILE.jsonl` applies it. All presets are local library files, not official app account exports. A preset contains only the 20 supported scalar settings; schedules, clock, scale pairing and calibration are not included. A preset may contain a subset of those settings. `load_profile` rejects unsupported fields, duplicate keys and out-of-range values. Saves and journals never overwrite existing files.

Python mutation API:

```python
async with ProfitecClient(device, allow_experimental_settings=True) as machine:
    await machine.apply_settings({"spotlight": "off"}, "spotlight-change.jsonl")
```

Before mutation, the client reads the affected block and fsyncs a journal containing its original bytes and the requested change. Before each write it checks for changes since that snapshot. After each write it reads the complete block back and requires an exact match. It stops on a mismatch or failed request. Settings and schedule writes are refused while the machine reports brewing; the client waits up to 2 s for a status notification to check. On hardware, a write sent during a brew took effect but its acknowledgement was lost. Every Bluetooth write times out after 5 s. Preset application is **not atomic**: earlier settings can remain applied after a later failure. Reconnect, read actual values, and consult the journal before restoring. There is no automatic rollback or retry. The comparison cannot prevent an external writer racing between the final read and write; keep the phone app disconnected during changes.

`apply_schedule(day_index, slot, ScheduleWindow(start="07:00", stop="09:00"), journal)` sets one window through the same opt-in and journal mechanism; pass `None` to disable it. `schedule_command` encodes the request offline. Schedule readback checks all schedule bytes and excludes the independently ticking clock. A schedule-window write/readback/restore was exercised on hardware. Calendar-triggered operation and weekday-name mapping remain unverified; schedule writes remain experimental. Enabling pre-infusion requires the machine's advanced pre-infusion mode already enabled (as in the captured baseline); the library refuses to silently enable that separate mode.

## Shot summaries and CSV

```sh
profitec-ble summary shot.jsonl
profitec-ble export-csv shot.jsonl shot.csv
```

`summarize(path)` detects brewing transitions from raw status packets and reports each shot's observed boundaries, duration, peak pressure and sample count. A capture that begins during brewing or ends before a stop has `complete: false` and no duration. Completeness means both boundaries were observed; it does not guarantee uninterrupted radio delivery. Pressure samples are included only inside the observed brewing window. State notifications are roughly one second apart on the tested machine, so this is not millisecond timing or a calibrated peak measurement.

`export_csv(source, destination)` emits one row per packet with UTC/offset timestamp, UUID, original hex and decoded telemetry columns. It validates the complete input before creating an output, rejects malformed known packets or backwards timestamps, and never overwrites a file. Both tools decode raw bytes again instead of trusting a saved `decoded` field. CSV export currently loads the capture into memory.

## Live view, per-shot recording and plots

```sh
profitec-ble watch DEVICE
profitec-ble record-shots DEVICE --shots-dir shots
profitec-ble metrics shots/2026-11-25T12-00-01+00-00.jsonl
profitec-ble plot shot.jsonl shot.html --compare older1.jsonl older2.jsonl
profitec-ble shots --shots-dir shots
profitec-ble shot-note 2026-11-25T12-00-01+00-00.jsonl --bean Kenya --grind 2.5 --ratio 1:2
profitec-ble shots-index --shots-dir shots
```

Pressure is zeroed by the sensor module when a connection opens. Connect while the machine is idle; a connection made during a shot shifts zero. Several telemetry streams can run on one connection, beside one settings request or control operation. `watch` prints one line per status packet with the latest pressure and a derived **ready** flag. Ready means state 0 and coffee temperature within ±1 °C of the configured setpoint for 60 s. It is not a machine flag. `record-shots` writes each brewing window to its own JSONL file. The idle status packet before the window is included, so the shot counts as complete. Each file is indexed in `shots.db` (stdlib SQLite). `shots-index` rebuilds the metrics from the JSONL files and keeps notes. Notes and brew-by-weight weights live only in the database. `metrics` adds mean pressure, seconds above 6 bar, time to the first sample at or above 1 bar, and the coffee temperature at start and end. These are sample-and-hold estimates at notification spacing, not calibrated measurements. `plot` writes a standalone HTML/SVG file and never overwrites one. `record --log` also captures device-log indications (`873ae829`), decoded as text lines (`DeviceLog`).

## Profile diff, journal restore, report and weekday check

```sh
profitec-ble profile-diff DEVICE baseline.json
profitec-ble journal-restore spotlight-change.jsonl
profitec-ble journal-restore spotlight-change.jsonl --device DEVICE --apply --journal restore.jsonl
profitec-ble report DEVICE
profitec-ble weekday-check DEVICE 0 --journal weekday-0.jsonl
```

`profile-diff` lists the settings whose machine value differs from a preset. `journal-restore` previews the original values from a journal's before-image. `--apply` sends them through the normal journaled, read-back path. `report` prints a shareable compatibility report: firmware, block lengths, whether named decoding succeeded, the words without names, the clock weekday code next to the host weekday, and the GATT characteristic UUIDs. It contains no device address. `weekday-check` **writes**: it sets window 0 of one wire day index to 03:33–03:34, asks which weekday the machine or app shows, and restores the window under a second journal. It refuses a window that is already in use. Clock setting is not implemented. It needs a capture of the official app setting the clock.

## MQTT / Home Assistant bridge (experimental)

```sh
python -m pip install "profitec-ble[mqtt] @ git+https://github.com/SevFle/profitec-ble.git"
MQTT_PASSWORD=... profitec-ble bridge DEVICE --mqtt-host homeassistant.local --mqtt-user ha --mqtt-password-env MQTT_PASSWORD \
  --journal-dir journals --shots-dir shots [--allow-remote-brew]
```

The bridge publishes Home Assistant discovery for pressure, both temperatures, state, brewing, ready, and all 20 settings. Settings are editable only with `--journal-dir`. Each write gets a new journal file and shows the read-back value, not the requested one. Writes run one at a time. A value outside the allowed range is rejected before the machine is contacted. A failed write is published to `profitec/<device>/error`, and the bridge reconnects when the client reports an uncertain outcome. Nothing is retried. `--log` also publishes device-log lines to `profitec/<device>/log`. Retained command messages are ignored, so a broker cannot replay a brew. The bridge reconnects with a 2 s to 60 s backoff and publishes availability. A command arriving while disconnected is rejected, not queued. The **Release Bluetooth** switch disconnects the machine for 10 minutes so the official app can connect. It is refused while brewing.

`--allow-remote-brew` adds brew dose 1/2, manual, and stop buttons. A start is refused unless state is 0 and the coffee temperature is within 2 °C of the setpoint. Remote brewing can start a physical machine with nobody present. Use it only with a cup in place and the machine supervised.

## Brew-by-weight logic (experimental, unverified on hardware)

This library contains no scale driver. `brew_by_weight(machine, scale, target_g, dose, journal, stop_offset_g=2.0)` takes any weight source: an object with `weight` (grams), `age` (seconds since its last reading, infinite when none), and `async tare()`. It needs a fresh reading. It tares, waits for a reading near zero, then starts **timed** dose 1 or 2. It sends one stop when the weight reaches `target_g - stop_offset_g`, and logs the final weight after 5 s. The timed dose is the backstop. If the weight source stops reporting, the shot continues until the machine's dose time ends, and the function warns once. If the dose ends before the target, the result says `target_not_reached` and no command is sent. Each event is fsynced to a new journal. Set the dose time longer than the expected shot. Remote timed-dose start and stop were exercised once on hardware; a stop issued by the weight logic was not.

## License and manufacturer attribution

Original project code and documentation are provided under the [MIT License](LICENSE). All rights in ECM/Profitec products, original software, firmware, branding, and trademarks remain with their respective owners. This unofficial project has no manufacturer affiliation or endorsement. See [NOTICE.md](NOTICE.md).
