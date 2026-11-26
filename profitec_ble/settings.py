"""Named settings recovered from app 3.8. Temperatures on the wire are Celsius."""
from dataclasses import dataclass
from decimal import Decimal
from .protocol import SettingsBlock, decode_settings


@dataclass(frozen=True)
class PreInfusion:
    enabled: bool
    pause_start_seconds: float
    pause_seconds: float


@dataclass(frozen=True)
class DoseSettings:
    dose1_seconds: float
    dose2_seconds: float
    dose1_preinfusion: PreInfusion
    dose2_preinfusion: PreInfusion
    weight_preinfusion: PreInfusion


@dataclass(frozen=True)
class GeneralSettings:
    coffee_temperature_c: int
    steam_temperature_c: int
    cleaning_reminder_shots: int
    water_filter_litres: int
    eco_minutes: int
    temperature_unit: str
    spotlight: str
    fast_heat_up: bool
    steam_enabled: bool


@dataclass(frozen=True)
class ScheduleWindow:
    start: str
    stop: str


@dataclass(frozen=True)
class TimeSettings:
    weekday_code: int
    hour: int
    minute: int
    second: int
    # Wire day order retained; weekday naming must be checked against machine UI.
    days: tuple[tuple[ScheduleWindow | None, ...], ...]


def _boolean(value):
    if value not in (0, 1):
        raise ValueError(f"Unknown boolean encoding: {value}")
    return value == 1


def named_settings(block: SettingsBlock) -> DoseSettings | GeneralSettings | TimeSettings | str:
    block = decode_settings(block.raw)
    words = block.words
    if block.address == 1:
        return block.firmware
    if block.address == 2000:
        groups = [PreInfusion(_boolean(words[i]), words[i+1] / 10, words[i+2] / 10) for i in (5, 8, 11)]
        return DoseSettings(words[0] / 10, words[1] / 10, *groups)
    if block.address == 3001:
        if words[7] not in (0, 1) or words[8] not in (0, 1, 2):
            raise ValueError("Unknown temperature-unit or spotlight encoding")
        return GeneralSettings(words[0], words[1], words[3] * 10, words[4], words[6],
                               "C" if words[7] == 1 else "F",
                               ("off", "always_on", "during_shot")[words[8]],
                               _boolean(words[9]), _boolean(words[22]))
    days = []
    for day in range(7):
        windows = []
        for slot in range(2):
            offset = 14 + day * 8 + slot * 4
            stop_h, stop_m, start_h, start_m = block.raw[offset:offset+4]
            if stop_h > 23:
                windows.append(None)
            else:
                if start_h > 23 or stop_m > 59 or start_m > 59:
                    raise ValueError("Invalid schedule time")
                windows.append(ScheduleWindow(f"{start_h:02}:{start_m:02}", f"{stop_h:02}:{stop_m:02}"))
        days.append(tuple(windows))
    if not (0 <= words[1] <= 23 and 0 <= words[2] <= 59 and 0 <= words[3] <= 59):
        raise ValueError("Invalid machine clock")
    return TimeSettings(words[0], words[1], words[2], words[3], tuple(days))


@dataclass(frozen=True)
class Setting:
    block: str
    register: int
    minimum: float
    maximum: float
    step: float = 1
    scale: float = 1
    choices: tuple = ()

    def encode(self, value) -> int:
        if self.choices:
            # bool and int compare equal in Python; preserve the public value type.
            for wire, choice in enumerate(self.choices):
                if type(value) is type(choice) and value == choice:
                    return wire
            raise ValueError(f"Expected one of {self.choices}")
        if type(value) not in (int, float):
            raise ValueError("Setting must be a number")
        number = Decimal(str(value))
        if not number.is_finite() or not self.minimum <= number <= self.maximum:
            raise ValueError(f"Setting must be between {self.minimum} and {self.maximum}")
        if number % Decimal(str(self.step)):
            raise ValueError(f"Setting must use steps of {self.step}")
        encoded = number * Decimal(str(self.scale))
        if encoded != encoded.to_integral_value():
            raise ValueError("Setting cannot be represented exactly")
        return int(encoded)


SETTABLE = {
    "coffee_temperature_c": Setting("general", 3001, 85, 99),
    "steam_temperature_c": Setting("general", 3002, 115, 134),
    "dose1_seconds": Setting("doses", 2000, 0, 600, .1, 10),
    "dose2_seconds": Setting("doses", 2001, 0, 600, .1, 10),
    "cleaning_reminder_shots": Setting("general", 3004, 0, 200, 10, .1),
    "water_filter_litres": Setting("general", 3005, 0, 200),
    "eco_minutes": Setting("general", 3007, 0, 600, 10),
    "temperature_unit": Setting("general", 3008, 0, 1, choices=("F", "C")),
    "spotlight": Setting("general", 3009, 0, 2, choices=("off", "always_on", "during_shot")),
    "fast_heat_up": Setting("general", 3010, 0, 1, choices=(False, True)),
    "steam_enabled": Setting("general", 3023, 0, 1, choices=(False, True)),
}
for name, address in (("dose1", 2005), ("dose2", 2008), ("weight", 2011)):
    SETTABLE[f"{name}_preinfusion_enabled"] = Setting("doses", address, 0, 1, choices=(False, True))
    SETTABLE[f"{name}_pause_start_seconds"] = Setting("doses", address+1, 0, 10, .1, 10)
    SETTABLE[f"{name}_pause_seconds"] = Setting("doses", address+2, 0, 30, .1, 10)


def setting_command(name: str, value) -> bytes:
    if name not in SETTABLE:
        raise ValueError(f"Unknown setting: {name}")
    spec = SETTABLE[name]
    if name == "eco_minutes" and value != 0 and (type(value) not in (int, float) or value < 30):
        raise ValueError("Eco timer is 0 (off) or 30–600 minutes")
    return spec.register.to_bytes(2, "big") + spec.encode(value).to_bytes(2, "big", signed=True)


def setting_value(name: str, block: SettingsBlock):
    spec = SETTABLE[name]
    word = block.words[spec.register - block.address]
    if spec.choices:
        if not 0 <= word < len(spec.choices):
            raise ValueError(f"Unknown value for {name}: {word}")
        value = spec.choices[word]
    else:
        value = float(Decimal(word) / Decimal(str(spec.scale)))
    # Profiles must contain values that the library can safely write back.
    setting_command(name, value)
    return value


def setting_values(blocks: dict[str, SettingsBlock]) -> dict:
    return {name: setting_value(name, blocks[spec.block]) for name, spec in SETTABLE.items()
            if spec.block in blocks}


def schedule_command(day_index: int, slot: int, window: ScheduleWindow | None) -> bytes:
    """Encode one of the machine's 14 windows; day_index retains wire order."""
    if type(day_index) is not int or not 0 <= day_index <= 6 or type(slot) is not int or slot not in (0, 1):
        raise ValueError("day_index must be 0..6 and slot 0..1")
    if window is None:
        payload = bytes([100] * 4)
    else:
        if not isinstance(window, ScheduleWindow):
            raise ValueError("Window must be ScheduleWindow or None")
        def clock(value):
            if not isinstance(value, str) or len(value) != 5 or value[2] != ":" or not (value[:2] + value[3:]).isascii() or not (value[:2] + value[3:]).isdigit():
                raise ValueError("Use HH:MM")
            hour, minute = int(value[:2]), int(value[3:])
            if hour > 23 or minute > 59:
                raise ValueError("Invalid schedule time")
            return bytes((hour, minute))
        payload = clock(window.stop) + clock(window.start)
    return (4006 + day_index * 4 + slot * 2).to_bytes(2, "big") + payload
