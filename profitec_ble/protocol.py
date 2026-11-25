"""Pure decoders. Offsets refer to the complete BLE value, including its header."""
from dataclasses import dataclass
from uuid import UUID

PRESSURE_UUID = "873ae82b-4c5a-4342-b539-9d900bf7ebd0"
STATUS_UUID = "873ae833-4c5a-4342-b539-9d900bf7ebd0"
CONTROL_UUID = "873ae834-4c5a-4342-b539-9d900bf7ebd0"
LOG_UUID = "873ae829-4c5a-4342-b539-9d900bf7ebd0"
SETTINGS = {"firmware": (1, 1), "doses": (2000, 14), "general": (3001, 23), "time": (4000, 34)}


@dataclass(frozen=True)
class Pressure:
    bar: float
    raw: bytes


@dataclass(frozen=True)
class MachineStatus:
    coffee_temperature: int
    steam_temperature: int
    state: int
    brewing: bool
    raw: bytes


@dataclass(frozen=True)
class SettingsBlock:
    address: int
    words: tuple[int, ...]
    raw: bytes

    @property
    def name(self) -> str:
        return next(name for name, (address, _) in SETTINGS.items() if address == self.address)

    @property
    def firmware(self) -> str | None:
        return f"{self.raw[2]}.{self.raw[3]:02d}" if self.address == 1 else None


@dataclass(frozen=True)
class RawPacket:
    uuid: str
    raw: bytes


def _word(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset:offset + 2], "big", signed=True)


def decode_pressure(data: bytes) -> Pressure:
    data = bytes(data)
    if len(data) != 2:
        raise ValueError("Pressure requires exactly 2 bytes")
    return Pressure(_word(data, 0) / 1000, data)


def decode_status(data: bytes) -> MachineStatus:
    data = bytes(data)
    if len(data) != 22 or data[:2] != b"\x00\x0c":
        raise ValueError("Status requires 22 bytes with header 000c")
    state = data[17]
    return MachineStatus(_word(data, 10), _word(data, 12), state, state in (1, 2, 3, 4, 5, 8), data)


def settings_request(name: str) -> bytes:
    try:
        address, count = SETTINGS[name]
    except KeyError:
        raise ValueError(f"Unknown settings block: {name}") from None
    return address.to_bytes(2, "big") + bytes([count])


def decode_settings(data: bytes) -> SettingsBlock:
    data = bytes(data)
    address = int.from_bytes(data[:2], "big")
    sizes = dict(SETTINGS.values())
    if address not in sizes or len(data) != 2 + sizes[address] * 2:
        raise ValueError("Unknown or malformed settings block")
    return SettingsBlock(address, tuple(_word(data, i) for i in range(2, len(data), 2)), data)


def decode(uuid: str, data: bytes) -> Pressure | MachineStatus | SettingsBlock | RawPacket:
    uuid = str(UUID(uuid))
    if uuid == PRESSURE_UUID:
        return decode_pressure(data)
    if uuid == STATUS_UUID:
        if data[:2] == b"\x00\x0c":
            return decode_status(data)
        if int.from_bytes(data[:2], "big") in dict(SETTINGS.values()):
            return decode_settings(data)
    return RawPacket(uuid, bytes(data))


def brew_commands(dose: int | None) -> tuple[bytes, ...]:
    """Offline experimental encoder: None stops; 1/2 selects dose; 0 is manual.

    Recovered from app 3.8; no hardware execution is implied.
    """
    if dose is None:
        return (bytes.fromhex("0065000a"),)
    if type(dose) is not int or dose not in (0, 1, 2):
        raise ValueError("Dose must be 0 (manual), 1, 2, or None (stop)")
    return (bytes.fromhex("00660001"), bytes.fromhex("006500") + bytes([8 if dose == 0 else dose]))
