"""Local machine presets. This format is not an official-app account/profile export."""
import json
from dataclasses import asdict
from .protocol import decode_settings
from .settings import SETTABLE, named_settings, schedule_command, setting_command, setting_value


def validate_profile(profile: dict) -> dict:
    if not isinstance(profile, dict) or set(profile) != {"schema", "name", "settings"}:
        raise ValueError("Profile requires exactly schema, name, settings")
    if type(profile["schema"]) is not int or profile["schema"] != 1:
        raise ValueError("Unsupported profile schema")
    if not isinstance(profile["name"], str) or not profile["name"].strip():
        raise ValueError("Profile name must not be empty")
    if not isinstance(profile["settings"], dict) or not profile["settings"]:
        raise ValueError("Profile settings must be a nonempty object")
    for name, value in profile["settings"].items():
        setting_command(name, value)
    return profile


def load_profile(path) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate profile key: {key}")
            result[key] = value
        return result
    with open(path, encoding="utf-8") as source:
        return validate_profile(json.load(source, object_pairs_hook=unique))


def profile_diff(blocks: dict, settings: dict) -> dict:
    """Settings whose machine value differs from the preset value."""
    current = {name: setting_value(name, blocks[SETTABLE[name].block]) for name in settings}
    return {name: {"machine": current[name], "profile": value}
            for name, value in settings.items() if current[name] != value}


def restore_plan(journal) -> dict:
    """Values from a mutation journal's before-image; preview only, apply through the journaled path."""
    with open(journal, encoding="utf-8") as source:
        first = json.loads(source.readline() or "{}")
    if first.get("event") != "before" or not isinstance(first.get("blocks"), dict):
        raise ValueError("Journal has no before-image")
    blocks = {name: decode_settings(bytes.fromhex(raw)) for name, raw in first["blocks"].items()}
    requested = first["requested"]
    if "schedule" in first.get("commands", {}):
        day, slot = requested["day_index"], requested["slot"]
        window = named_settings(blocks["time"]).days[day][slot]
        return {"schedule": {"day_index": day, "slot": slot, "window": asdict(window) if window else None},
                "commands": {"schedule": schedule_command(day, slot, window).hex()}}
    settings = {name: setting_value(name, blocks[SETTABLE[name].block]) for name in requested}
    return {"settings": settings, "commands": {name: setting_command(name, value).hex() for name, value in settings.items()}}


def save_profile(path, name: str, settings: dict):
    profile = validate_profile({"schema": 1, "name": name, "settings": settings})
    with open(path, "x", encoding="utf-8") as output:
        json.dump(profile, output, indent=2, allow_nan=False)
        output.write("\n")
