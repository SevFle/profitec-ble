"""Local machine presets. This format is not an official-app account/profile export."""
import json
from .settings import setting_command


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


def save_profile(path, name: str, settings: dict):
    profile = validate_profile({"schema": 1, "name": name, "settings": settings})
    with open(path, "x", encoding="utf-8") as output:
        json.dump(profile, output, indent=2, allow_nan=False)
        output.write("\n")
