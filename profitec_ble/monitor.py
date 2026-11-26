"""Live helpers: derived readiness, per-shot recording, and a rebuildable shot index."""
import json
import sqlite3
from pathlib import Path
from .protocol import MachineStatus, decode
from .shots import metrics

INDEX_COLUMNS = ("start_time", "end_time", "duration_seconds", "complete", "peak_bar", "mean_bar",
                 "seconds_above_6_bar", "time_to_pressure_seconds", "coffee_temperature_start",
                 "coffee_temperature_end")
NOTE_COLUMNS = ("bean", "grind", "ratio", "notes", "target_g", "final_weight_g")


class ReadyTracker:
    """Derived signal, not a machine flag: idle state and coffee temperature held near the setpoint."""

    def __init__(self, setpoint: float, band: float = 1.0, hold_seconds: float = 60.0):
        self.setpoint, self.band, self.hold_seconds = setpoint, band, hold_seconds
        self.since = None
        self.ready = False

    def update(self, status: MachineStatus, now: float) -> bool:
        ok = status.state == 0 and abs(status.coffee_temperature - self.setpoint) <= self.band
        if not ok:
            self.since = None
        elif self.since is None:
            self.since = now
        self.ready = ok and now - self.since >= self.hold_seconds
        return self.ready


def _connect(directory):
    db = sqlite3.connect(Path(directory) / "shots.db")
    db.execute(f"create table if not exists shots (file text primary key, {', '.join(INDEX_COLUMNS + NOTE_COLUMNS)})")
    return db


def index_shot(directory, path) -> dict:
    """Upsert derived metrics for one capture; notes and weights already stored are kept."""
    shot = metrics(path)["shots"][0]
    values = [shot[column] for column in INDEX_COLUMNS]
    db = _connect(directory)
    try:
        with db:
            db.execute(f"insert into shots (file, {', '.join(INDEX_COLUMNS)}) values ({', '.join('?' * (len(INDEX_COLUMNS) + 1))}) "
                       f"on conflict(file) do update set {', '.join(f'{c}=excluded.{c}' for c in INDEX_COLUMNS)}",
                       [Path(path).name, *values])
    finally:
        db.close()
    return shot


def reindex(directory) -> int:
    paths = sorted(Path(directory).glob("*.jsonl"))
    for path in paths:
        index_shot(directory, path)
    return len(paths)


def annotate_shot(directory, name: str, **notes):
    if not notes or set(notes) - set(NOTE_COLUMNS):
        raise ValueError(f"Notes must be some of {NOTE_COLUMNS}")
    db = _connect(directory)
    try:
        with db:
            changed = db.execute(f"update shots set {', '.join(f'{k}=?' for k in notes)} where file=?",
                                 [*notes.values(), Path(name).name]).rowcount
    finally:
        db.close()
    if not changed:
        raise ValueError(f"Shot not indexed: {name}")


def list_shots(directory) -> list[dict]:
    db = _connect(directory)
    db.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in db.execute("select * from shots order by start_time")]
    finally:
        db.close()


class ShotRecorder:
    """Write each observed brewing window, with the idle status before it, to its own JSONL file."""

    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.output = None
        self.path = None
        self.last_idle = None

    def feed(self, packet: dict) -> Path | None:
        """Returns the finished capture path once a shot ends and is indexed."""
        try:
            value = decode(packet["uuid"], bytes.fromhex(packet["hex"]))
        except ValueError:
            value = None
        line = json.dumps({key: packet[key] for key in ("time", "uuid", "hex")}) + "\n"
        if isinstance(value, MachineStatus):
            if value.brewing and self.output is None and self.last_idle is not None:
                self.path = self.directory / (packet["time"].replace(":", "-") + ".jsonl")
                self.output = open(self.path, "x", encoding="utf-8")
                self.output.write(self.last_idle)
            if not value.brewing:
                self.last_idle = line
                if self.output is not None:
                    self.output.write(line)
                    return self.close()
        if self.output is not None:
            self.output.write(line)
            self.output.flush()
        return None

    def close(self) -> Path | None:
        if self.output is None:
            return None
        self.output.close()
        self.output = None
        index_shot(self.directory, self.path)
        return self.path
