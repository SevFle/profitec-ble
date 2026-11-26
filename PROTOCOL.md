# Bluetooth protocol notes

These wire mappings were inferred from Bluetooth captures and reverse-engineering research against Make Coffee Yours app version 3.8, then checked where noted on a MOVE 2.0 with firmware 2.02. APKs, disassembly, localization tables, and private captures are not distributed. This is not an official protocol specification. See [NOTICE.md](NOTICE.md) and [VERIFICATION.md](VERIFICATION.md).

All proprietary UUIDs below end in `-4c5a-4342-b539-9d900bf7ebd0`. Use UUIDs, never captured ATT handles, in clients.

| UUID prefix | Meaning | Evidence |
|---|---|---|
| 873ae82b | Pressure notifications | Live pressure capture |
| 873ae829 | Device log indications | GATT inventory; high-level log API not implemented |
| 873ae833 | Status/settings notifications and read-request writes | Settings request/reply captures |
| 873ae834 | Machine register writes | Shot capture and scalar write/readback checks |
| 873ae839 | Dose-2 stop-on-weight characteristic | Research only; not exposed as a supported scale control |

## Pressure and status

Pressure is exactly two bytes, signed big-endian, divided by 1000 for bar. Original capture peak `0f69` is 3.945 bar; app displayed 3.92. App filtering/rounding has not been replicated.

Status is 22 bytes beginning `000c`. Coffee temperature is signed BE16 at offset 10; steam temperature at offset 12. State is byte 17. App treats states 1 through 5 and 8 as brewing. Unknown state values remain visible. The library does not infer boiler-ready flags or elapsed shot time.

## Settings

Read requests are a BE16 register address followed by a **register count**, not byte count. Notifications contain the address followed by two bytes per register. Strict known lengths prevent misinterpreting changed firmware layouts.

| Block | Address | Request hex | Response bytes |
|---|---:|---|---:|
| firmware | 1 | 000101 | 4 |
| doses/pre-infusion | 2000 | 07d00e | 30 |
| general | 3001 | 0bb917 | 48 |
| clock/schedules | 4000 | 0fa022 | 70 |

These requests and response lengths were observed in connection captures. Firmware `00010202` formats as 2.02 (two independent version bytes). Doses and general settings include additional fields; the library preserves them as words rather than assigning unverified names. BLE framing here does not carry a Modbus CRC; do not add one based solely on the app's internal Modbus terminology.

## Brew commands (experimental)

Start first writes `00660001`, then `00650001` (dose 1), `00650002` (dose 2), or `00650008` (manual). Stop writes `0065000a`. The captured dose-1 shot contains the first, second and stop payloads. Library manual start/stop was exercised once on hardware; one supervised remote dose-1 start and stop reported state 1, then 0; dose 2 remains unexercised. Multi-command start is not atomic; a timeout may occur after a command took effect.

## Device log

`873ae829` indications are captured raw by `record --log` and published raw by the bridge. Their format is unknown.

## Remaining features

Recovered function names cover temperature settings, pre-infusion, doses, schedules, standby, eco mode, lights, cleaning reminders, per-dose stop-on-weight and scale pairing. Names alone do not establish payload semantics. Future additions should retain an exact capture, source offset, decoded before/after values, and independent regression coverage. App history, notes and graphs may be app-local rather than machine protocol features.

## Named settings and write mapping (0.2)

Named fields and bounds were inferred from application behavior and reverse-engineering research. All 20 scalar write mappings were subsequently exercised with complete-block readback and restoration on the test machine.

| Setting | Register | Encoding |
|---|---:|---|
| Coffee / steam targets | 3001 / 3002 | Celsius BE16, app bounds 85–99 / 115–134 |
| Dose 1 / 2 time | 2000 / 2001 | tenths of a second, 0–600 seconds |
| Dose 1 pre-infusion | 2005–2007 | enabled, pause-start tenths, pause-duration tenths |
| Dose 2 pre-infusion | 2008–2010 | same |
| Weight pre-infusion | 2011–2013 | same |
| Cleaning reminder | 3004 | tens of shots; app range 0–200 shots |
| Filter reminder | 3005 | litres; app range 0–200 |
| Eco timer | 3007 | 0 or 30–600 minutes, step 10 |
| Temperature display unit | 3008 | 0 Fahrenheit, 1 Celsius |
| Spotlight | 3009 | 0 off, 1 always on, 2 during shot |
| Fast heat-up | 3010 | 0/1 |
| Steam enabled | 3023 | 0/1 |

Pre-infusion pause start has app bounds 0–10 seconds; pause duration 0–30.

Clock words at byte offsets 2/4/6/8 are weekday code/hour/minute/second. Schedules begin at byte 14. Each of seven days has two four-byte windows in **stop hour, stop minute, start hour, start minute** order. A stop hour above 23 is disabled in the app; its writer uses `64 64 64 64`. Schedule register base is 4006; each window occupies two registers. Weekday names remain unverified.

The app writes some adjacent fields in batches. The library's scalar writer instead sends one recovered register and value, preserving unrelated bytes and checking the entire readback block. All 20 allowlisted scalar writes were checked on one test machine; this does not establish compatibility with other firmware. No CRC is added.
