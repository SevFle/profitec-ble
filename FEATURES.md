# Compatibility and scope

This is a Bluetooth interoperability library, not a full replacement for the official application. Hardware observations below are from one Profitec MOVE 2.0, firmware 2.02, on macOS. Other firmware and host Bluetooth stacks remain unverified.

| Capability | Implemented | Evidence and limit |
|---|---|---|
| Pressure, both boiler temperatures, brewing state | Typed telemetry and raw packets | Live telemetry and a complete manually initiated shot recorded; pressure calibration not verified |
| Firmware, dose, general and time settings | Sequential reads, raw bytes and named fields | All four blocks read on the test machine |
| 20 scalar settings | Validated preview and opt-in mutation | Each field changed, read back in full, and restored on hardware |
| Local presets | Save, load, plan and apply | Own JSON format; not an official account export; writes are not atomic |
| Schedule windows | Read and opt-in per-window write | One window changed, read back and restored; weekday naming and calendar-triggered starts unverified |
| Manual brew start/stop | Opt-in Python API | One supervised start/stop with state 8 then 0 observed |
| Timed dose 1/2 start | Encoded in the experimental API | Observed dose-1 protocol traffic; remote timed-dose execution not verified |
| Shot summaries and CSV | Offline tools | CLI tests and recorded shot checked; timestamp resolution is about one second for state transitions |
| Scale, tare and automatic weight stop | Not exposed as supported controls | No compatible scale available for verification |
| Clock setting, calibration and firmware updates | Not implemented | Require separate protocol and recovery evidence |
| Cloud sync, accounts, community and application history | Outside this library | Not machine BLE commands |

All control APIs retain explicit opt-in. Readback, a BLE acknowledgement, and physical completion are different observations. A failed or timed-out command can have taken effect. Do not retry automatically; inspect the machine and journal first. Do not equate status state 0 with brew readiness.

The package has no dependency on Coffee Room, OpenAI, a browser, or a cloud account. It uses Bleak for transport. Offline protocol functions do not open a Bluetooth connection.
