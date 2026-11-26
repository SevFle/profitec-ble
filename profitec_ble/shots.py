"""Offline shot analysis from raw captured notifications, without interpolation."""
import csv
import json
from datetime import datetime
from html import escape
from pathlib import Path
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


SUMMARY_KEYS = ("start_time", "end_time", "duration_seconds", "peak_bar", "pressure_samples", "complete")
FIRST_PRESSURE_BAR = 1.0


def _shots(path):
    """One pass over a capture; each shot keeps its in-window pressure samples."""
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
                          "peak_bar": None, "pressure_samples": 0, "complete": False,
                          "samples": [], "start": start, "end": None,
                          "coffee_temperature_start": value.coffee_temperature}
            elif not value.brewing and active is not None:
                active["coffee_temperature_end"] = value.coffee_temperature
                active["end_time"] = packet["time"]
                active["end"] = timestamp
                active["complete"] = start is not None
                if start is not None:
                    active["duration_seconds"] = (timestamp - start).total_seconds()
                shots.append(active)
                active = None
            if active is not None:
                active["coffee_temperature_end"] = value.coffee_temperature
            previous_state = value.brewing
        elif isinstance(value, Pressure) and active is not None:
            active["pressure_samples"] += 1
            active["peak_bar"] = value.bar if active["peak_bar"] is None else max(active["peak_bar"], value.bar)
            active["samples"].append((timestamp, value.bar))
    if active is not None:
        shots.append(active)
    return shots, count


def summarize(path) -> dict:
    shots, count = _shots(path)
    return {"shots": [{key: shot[key] for key in SUMMARY_KEYS} for shot in shots],
            "packet_count": count, "decode_errors": 0}


def metrics(path) -> dict:
    """Summary plus derived figures. Sample-and-hold over ~notification spacing, not calibrated."""
    shots, count = _shots(path)
    result = []
    for shot in shots:
        samples, start = shot["samples"], shot["start"]
        above = 0.0
        for (time, bar), (following, _) in zip(samples, samples[1:] + ([(shot["end"], None)] if shot["end"] else [])):
            if bar >= 6:
                above += (following - time).total_seconds()
        first = next((time for time, bar in samples if bar >= FIRST_PRESSURE_BAR), None)
        result.append({key: shot[key] for key in SUMMARY_KEYS} | {
            "mean_bar": round(sum(bar for _, bar in samples) / len(samples), 3) if samples else None,
            "seconds_above_6_bar": round(above, 3),
            "time_to_pressure_seconds": (first - start).total_seconds() if first and start else None,
            "coffee_temperature_start": shot["coffee_temperature_start"],
            "coffee_temperature_end": shot.get("coffee_temperature_end"),
        })
    return {"shots": result, "packet_count": count}


COLORS = ("#d62728", "#1f77b4", "#2ca02c", "#9467bd", "#8c564b", "#7f7f7f")


def plot_html(paths, destination):
    """Write a standalone HTML/SVG pressure plot: the first capture plus up to five to compare."""
    if not 1 <= len(paths) <= 6:
        raise ValueError("Plot one capture plus at most five comparisons")
    series = []
    for path in paths:
        shots, _ = _shots(path)
        shot = next((shot for shot in shots if shot["start"] is not None), None)
        if shot is None:
            raise ValueError(f"{path}: no shot with an observed start")
        series.append((Path(path).name, [((time - shot["start"]).total_seconds(), bar) for time, bar in shot["samples"]]))
    width, height, pad = 800, 400, 40
    max_t = max([t for _, points in series for t, _ in points] + [30])
    max_bar = max([bar for _, points in series for _, bar in points] + [12])
    x = lambda t: pad + t / max_t * (width - 2 * pad)
    y = lambda bar: height - pad - max(bar, 0) / max_bar * (height - 2 * pad)
    lines = [f'<polyline fill="none" stroke="{COLORS[i]}" stroke-width="{3 if i == 0 else 1.5}" '
             f'points="{" ".join(f"{x(t):.1f},{y(bar):.1f}" for t, bar in points)}"><title>{escape(name)}</title></polyline>'
             for i, (name, points) in enumerate(series)]
    grid = [f'<line x1="{pad}" x2="{width-pad}" y1="{y(b):.1f}" y2="{y(b):.1f}" stroke="#ddd"/>'
            f'<text x="4" y="{y(b)+4:.1f}" font-size="11">{b}</text>' for b in range(0, int(max_bar) + 1, 3)]
    legend = "".join(f'<li style="color:{COLORS[i]}">{escape(name)}</li>' for i, (name, _) in enumerate(series))
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-label="Pressure over time">'
           + "".join(grid) + f'<text x="{width/2}" y="{height-8}" font-size="12" text-anchor="middle">seconds since brew start (0–{max_t:.0f})</text>'
           + '<text x="4" y="14" font-size="12">bar</text>' + "".join(lines) + "</svg>")
    with open(destination, "x", encoding="utf-8") as output:
        output.write(f'<!doctype html><meta charset="utf-8"><title>Shot pressure</title>'
                     f'<body style="font-family:sans-serif;max-width:{width}px">{svg}<ul>{legend}</ul></body>\n')


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
