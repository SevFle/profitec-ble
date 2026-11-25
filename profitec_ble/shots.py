"""Offline shot analysis from raw captured notifications, without interpolation."""
import csv
import json
from datetime import datetime
from .protocol import decode, Pressure, MachineStatus


def read_capture(path):
    previous = None
    with open(path, encoding="utf-8") as source:
        for number, line in enumerate(source, 1):
            try:
                packet = json.loads(line)
                timestamp = datetime.fromisoformat(packet["time"])
                if timestamp.tzinfo is None:
                    raise ValueError("Timestamp must include a timezone")
                if previous is not None and timestamp < previous:
                    raise ValueError("Timestamps must not go backwards")
                value = decode(packet["uuid"], bytes.fromhex(packet["hex"]))
            except (KeyError, TypeError, ValueError, AttributeError) as error:
                raise ValueError(f"Line {number}: {error}") from error
            previous = timestamp
            yield packet, timestamp, value


def summarize(path) -> dict:
    shots = []
    active = None
    previous_state = None
    start = None
    count = 0
    for packet, timestamp, value in read_capture(path):
        count += 1
        if isinstance(value, MachineStatus):
            if value.brewing and active is None:
                start = timestamp if previous_state is False else None
                active = {"start_time": packet["time"] if start is not None else None,
                          "end_time": None, "duration_seconds": None,
                          "peak_bar": None, "pressure_samples": 0, "complete": False}
            elif not value.brewing and active is not None:
                active["end_time"] = packet["time"]
                active["complete"] = start is not None
                if start is not None:
                    active["duration_seconds"] = (timestamp - start).total_seconds()
                shots.append(active)
                active = None
            previous_state = value.brewing
        elif isinstance(value, Pressure) and active is not None:
            active["pressure_samples"] += 1
            active["peak_bar"] = value.bar if active["peak_bar"] is None else max(active["peak_bar"], value.bar)
    if active is not None:
        shots.append(active)
    return {"shots": shots, "packet_count": count, "decode_errors": 0}


def export_csv(source, destination):
    # Validate first so a bad input cannot leave a plausible partial export.
    rows = list(read_capture(source))
    columns = ("time", "uuid", "hex", "type", "pressure_bar", "coffee_temperature",
               "steam_temperature", "state", "brewing")
    with open(destination, "x", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=columns)
        writer.writeheader()
        for packet, _, value in rows:
            row = {key: packet[key] for key in ("time", "uuid", "hex")}
            row["type"] = type(value).__name__
            if isinstance(value, Pressure):
                row["pressure_bar"] = value.bar
            elif isinstance(value, MachineStatus):
                row.update(coffee_temperature=value.coffee_temperature,
                           steam_temperature=value.steam_temperature,
                           state=value.state, brewing=value.brewing)
            writer.writerow(row)
