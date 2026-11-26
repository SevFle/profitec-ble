# Project status and third-party rights

profitec-ble is an unofficial Python interoperability library for the Profitec MOVE 2.0. It is not affiliated with, endorsed by, or supported by ECM or Profitec. It is not an official SDK or a replacement for the manufacturer instructions.

The Python implementation is based on observed Bluetooth traffic and reverse-engineering research. This is not a claim of clean-room development. Manufacturer applications, firmware, APKs, decompiled or disassembled output, logos, and other manufacturer assets are not distributed in this repository.

All rights in ECM/Profitec products, original software, firmware, branding, and trademarks remain with their respective owners. Product names identify compatibility only. The MIT license applies to this project's original code and documentation; it does not grant rights in third-party software, firmware, or trademarks.

Protocol documentation describes the messages needed for interoperability. Compatibility has been exercised on one MOVE 2.0 with firmware 2.02 on macOS. Other machines, firmware versions, and host Bluetooth stacks require verification. No calibration, firmware flashing, access-control bypass, or official cloud-account integration is implemented.

Dependencies are installed separately and retain their own licenses. Bleak is MIT-licensed; its macOS PyObjC dependencies are also MIT-licensed. The optional `mqtt` extra installs aiomqtt (BSD-3-Clause) and its paho-mqtt dependency (EPL-2.0 / EDL-1.0). No dependency source is vendored here.
