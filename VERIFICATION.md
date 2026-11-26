# Verification scope

## Hardware observations

Supervised checks on 2026-11-25 used one Profitec MOVE 2.0 with firmware 2.02 and macOS. Only one Bluetooth owner was connected at a time.

- Read firmware, dose, general, and clock/schedule blocks through the library.
- Received pressure and both boiler temperatures; closed a telemetry stream and reconnected successfully.
- Recorded a manually initiated shot with both state transitions. Its approximately 21-second interval agreed with the operator's machine-timer reading. This is not pressure calibration or millisecond timing evidence.
- Changed all 20 allowlisted scalar settings, verified the complete affected blocks after each change, and restored the initial settings.
- Enabled one raw-index schedule window, verified it, disabled it, and verified restoration. All 14 windows ended disabled. The independently ticking clock was excluded from equality.
- Performed one supervised remote manual start/stop. Notifications reported state 8 during the run and state 0 after stop. No retry was sent.

Private capture sessions, device identifiers, and local mutation journals are not published. These statements summarize maintainer observations; they are not independently reproducible hardware certification. The repository includes a minimal three-packet fixture containing timestamps, characteristic UUIDs, and bytes, without a device address or account information.

## Automated checks

The existing suite contains 41 subprocess/CLI and simulated-transport checks. It covers signed and malformed packets, offline command previews, setting/profile validation, file overwrite protection, journal-before-write behavior, changed-baseline rejection, readback mismatch, failed writes, and schedule comparison while the clock ticks. These tests do not require a physical machine.

Run from an installed checkout:

```sh
python -m unittest discover -s tests -v
```

Public-release preparation on 2026-11-25:

- All 41 tests passed from the standalone checkout on Python 3.14.4/macOS.
- Built a wheel and source distribution with `uv build`, excluding build dependencies published within the last 48 hours.
- Installed the wheel into a fresh virtual environment outside the source tree. Verified the import resolves to that environment, the installed CLI decodes a pressure packet, and metadata declares the SPDX license expression `MIT`.
- Inspected the wheel for `LICENSE`, `NOTICE.md`, and `py.typed`.
- Extracted the source distribution, verified its test fixture is present, and ran all 41 tests successfully from the extracted tree.
- Scanned release source for credential patterns, personal paths, device identifiers, and private reverse-engineering artifact references; no matches remained. This is a bounded pattern scan, not a guarantee against every possible secret.
- GitHub CI is configured for Python 3.11 and 3.14 on Linux, macOS, and Windows. Its results are reported separately from hardware compatibility.

## Feature suite (0.3)

- `python -m unittest discover -s tests` runs 55 tests (41 existing plus 14 new) and passes on Python 3.11.15 and 3.15.1 on macOS; the new file also passed 30 repeated runs on 3.15 and 10 on 3.11. The new tests cover metrics, plots, the shot recorder and index, profile diff, journal restore, the ready hold, and brew-by-weight with a simulated weight source. Brew-by-weight cases: stop at target minus offset, scale loss letting the timed dose finish, dose end before the target, and refusal on a stale weight source or manual dose. Bridge cases: read-back shown after a mismatch with no retry, reconnect without resend, the remote-brew gate, retained commands ignored, release refused while brewing, and brew buttons hidden without the flag. One test runs a stream, a settings read and a stop on one simulated connection. Two tests run the bridge and brew-by-weight through the real `ProfitecClient` with a patched Bleak transport: read-back 93 shown for a requested 94 after one control write and one reconnect, an invalid value rejected without a reconnect, and start and stop on one connection.
- Python 3.11 regression found and fixed: `asyncio.wait_for` in 3.11 can turn an outside cancel into `TimeoutError` (CPython gh-86296). `events()` swallowed it and a cancelled stream never ended. `events()` now uses `asyncio.timeout`.
- The bridge ran against a local amqtt 0.12.2 broker with a simulated machine. It published 31 discovery configs, went online, and executed one live brew command. A retained `cmd/brew` already stored on the broker was replayed on subscribe and ignored. A live publish with the retain flag reaches existing subscribers as non-retained (MQTT 3.1.1 §3.3.1.3), so only broker replays can be detected.
- Hardware, 2026-11-26, MOVE 2.0 firmware 2.02, macOS, read-only:
  - `scan` needed a fix: the machine advertises as `MOVE2-<serial>` with no service UUIDs.
  - `report` read all four blocks; named decoding succeeded for each. The clock weekday code was 5 on a Thursday (host time). Device-log characteristic `873ae829` exists with `indicate`; it was not subscribed.
  - `watch` streamed 20 s: state 1 at 110 °C coffee, then state 0 as the boiler fell to 89 °C (setpoint 92). Ready stayed off, as expected outside ±1 °C. Idle pressure read -0.35 bar, an offset that has not been explained.
  - Supervised remote timed dose 1: start acknowledged after 0.15 s, state 1 reported 0.07 s later. Stop sent 5 s after start, with a general-settings read in flight on the same connection. The stop and the read were both acknowledged, and state 0 followed within 1 s. No retry was sent. This was one run, not a reliability measurement.
  - Supervised remote timed dose 2, three runs: state 2 while brewing, and the stop returned state 0 every time.
    - Runs 1 and 2 sent a spotlight write during the brew. Each write took effect, but its ATT response never came. Run 1 hung (control writes had no timeout); run 2 raised `TimeoutError` after the new 5 s write timeout. Spotlight was restored and verified after each run.
    - The device log showed `Modbus RX … CRC error` and `Modbus response timeout` lines in both runs, and status notifications stopped for 5 s in run 2. Run 2's errors began before the write was sent.
    - Run 3 sent no settings write: no Modbus errors and no status gaps.
    - Cause not settled. `apply_settings` and `apply_schedule` now refuse to write while the machine reports brewing. An idle no-op write on hardware passed the new check.
  - Device log (`873ae829`): NUL-terminated ASCII lines from the sensor module (module firmware v3.17, battery 3000 mV), now decoded as `DeviceLog`.
  - Pressure zero: on every connection the module runs `ZeroCalculation` and takes the absolute pressure then as zero (`Zero press: 1014mbar`). The -0.35 bar idle reading came from a connection made mid-brew. Connected while idle, the machine reads 0.00 bar.
  - The machine accepts one Bluetooth central. Other local apps holding it make it stop advertising.
- No Home Assistant instance has consumed the discovery messages yet.
- None of the 0.3 features have run against the machine or a scale yet. Hardware checklist: device-log subscribe, remote timed-dose start, stop during a timed dose, and one bridge session with Home Assistant.

## Unverified

Linux/Windows hardware, other firmware, calibration accuracy, automatic Flush/Ready Go detection, active calendar-triggered scheduling, weekday names, timed-dose command execution, scale/tare/weight-stop, firmware update, and official cloud services. Readback success must not be described as proof of those behaviors.

## Windows fixture portability

The first public CI run passed Linux and macOS on Python 3.11 and 3.14. Both Windows jobs failed only `test_replay_preserves_unknown_notification`: the subprocess could not reopen a `NamedTemporaryFile` that the parent test still held open. The exception was `PermissionError: [Errno 13]` on the temporary JSONL file. Other replay tests and all other fixture cases passed, ruling out a general CLI decoding or installation failure.

The fixture now uses a regular file inside `TemporaryDirectory` and closes the writer before launching the CLI. Payload and preservation assertions are unchanged. This is a test portability correction; runtime code is unchanged.
