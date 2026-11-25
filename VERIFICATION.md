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

## Unverified

Linux/Windows hardware, other firmware, calibration accuracy, automatic Flush/Ready Go detection, active calendar-triggered scheduling, weekday names, timed-dose command execution, scale/tare/weight-stop, firmware update, and official cloud services. Readback success must not be described as proof of those behaviors.
