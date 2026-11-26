"""Telemetry, profiles, and settings. Setting writes require --apply and a journal."""
import argparse
import asyncio
from contextlib import aclosing
from dataclasses import asdict, is_dataclass
import json
from .protocol import decode, SETTINGS
from .shots import summarize, export_csv, metrics, plot_html
from .settings import named_settings, setting_command, setting_values, SETTABLE, ScheduleWindow
from .profiles import load_profile, save_profile, profile_diff, restore_plan
from .client import ProfitecClient, positive, scan
from .monitor import ReadyTracker, ShotRecorder, annotate_shot, list_shots, reindex
from .protocol import MachineStatus, Pressure


def decoded_record(packet: dict) -> dict:
    if not isinstance(packet, dict) or not isinstance(packet.get("uuid"), str) or not isinstance(packet.get("hex"), str):
        raise ValueError("Packet must have string uuid and hex fields")
    result = dict(packet)
    try:
        decoded = decode(packet["uuid"], bytes.fromhex(packet["hex"]))
        values = asdict(decoded)
        values.pop("raw")
        result["decoded"] = {"type": type(decoded).__name__, **values}
    except ValueError as error:
        result["decode_error"] = str(error)
    return result


def emit(value):
    print(json.dumps(value), flush=True)


async def run(args):
    if args.command == "options":
        emit({name: asdict(spec) for name, spec in SETTABLE.items()})
        return
    if args.command == "profile-plan":
        profile = load_profile(args.input)
        emit({"name": profile["name"], "settings": profile["settings"],
              "commands": {name: setting_command(name, value).hex() for name, value in profile["settings"].items()}})
        return
    if args.command in ("set", "profile-apply"):
        values = {args.name: json.loads(args.value)} if args.command == "set" else load_profile(args.input)["settings"]
        plan = {name: setting_command(name, value).hex() for name, value in values.items()}
        if not args.apply:
            emit({"preview": True, "settings": values, "commands": plan})
            return
        if not args.journal:
            raise ValueError("--journal is required with --apply")
        async with ProfitecClient(args.device, allow_experimental_settings=True) as machine:
            blocks = await machine.apply_settings(values, args.journal)
            emit({"verified": True, "blocks": {name: block.raw.hex() for name, block in blocks.items()}})
        return
    if args.command == "weekday-check":
        import sys
        from pathlib import Path
        restore_journal = args.journal + ".restore.jsonl"
        for path in (args.journal, restore_journal):
            if Path(path).exists():
                raise FileExistsError(path)
        async with ProfitecClient(args.device, allow_experimental_settings=True) as machine:
            if named_settings(await machine.read_settings("time")).days[args.day_index][0] is not None:
                raise ValueError("Window 0 of that day index is in use; pick another index")
            await machine.apply_schedule(args.day_index, 0, ScheduleWindow(start="03:33", stop="03:34"), args.journal)
            try:
                print("Open the schedule on the machine display or official app. Which weekday shows 03:33-03:34?",
                      file=sys.stderr, flush=True)
                answer = (await asyncio.to_thread(sys.stdin.readline)).strip()
            finally:
                await machine.apply_schedule(args.day_index, 0, None, restore_journal)
        emit({"day_index": args.day_index, "weekday": answer})
        return
    if args.command == "profile-diff":
        profile = load_profile(args.input)
        async with ProfitecClient(args.device) as machine:
            blocks = {name: await machine.read_settings(name) for name in {SETTABLE[n].block for n in profile["settings"]}}
        emit(profile_diff(blocks, profile["settings"]))
        return
    if args.command == "profile-save":
        from pathlib import Path
        if Path(args.output).exists():
            raise FileExistsError(args.output)
        async with ProfitecClient(args.device) as machine:
            blocks = {name: await machine.read_settings(name) for name in ("doses", "general")}
        save_profile(args.output, args.name, setting_values(blocks))
        emit({"saved": args.output})
        return
    if args.command == "summary":
        emit(summarize(args.input))
        return
    if args.command == "metrics":
        emit(metrics(args.input))
        return
    if args.command == "plot":
        plot_html([args.input, *args.compare], args.output)
        return
    if args.command == "shots":
        for row in list_shots(args.shots_dir):
            emit(row)
        return
    if args.command == "shots-index":
        emit({"indexed": reindex(args.shots_dir)})
        return
    if args.command == "shot-note":
        notes = {key: getattr(args, key) for key in ("bean", "grind", "ratio", "notes") if getattr(args, key) is not None}
        annotate_shot(args.shots_dir, args.shot, **notes)
        return
    if args.command == "journal-restore":
        plan = restore_plan(args.input)
        if not args.apply:
            emit({"preview": True, **plan})
            return
        if not (args.device and args.journal):
            raise ValueError("--apply needs --device and a new --journal")
        async with ProfitecClient(args.device, allow_experimental_settings=True) as machine:
            if "schedule" in plan:
                s = plan["schedule"]
                window = ScheduleWindow(**s["window"]) if s["window"] else None
                await machine.apply_schedule(s["day_index"], s["slot"], window, args.journal)
            else:
                await machine.apply_settings(plan["settings"], args.journal)
        emit({"verified": True, **plan})
        return
    if args.command == "bridge":
        from .bridge import serve
        import os
        await serve(args.device, args.mqtt_host, args.mqtt_port, username=args.mqtt_user,
                    password=os.environ.get(args.mqtt_password_env) if args.mqtt_password_env else None,
                    prefix=args.prefix, allow_remote_brew=args.allow_remote_brew, journal_dir=args.journal_dir,
                    shots_dir=args.shots_dir, log=args.log)
        return
    if args.command == "export-csv":
        export_csv(args.input, args.output)
        return
    if args.command == "scan":
        for device in await scan():
            emit(device)
        return
    if args.command == "decode":
        packet = decoded_record({"uuid": args.uuid, "hex": args.hex})
        emit(packet)
        if "decode_error" in packet:
            raise ValueError(packet["decode_error"])
        return
    if args.command == "replay":
        errors = 0
        with open(args.input, encoding="utf-8") as source:
            for number, line in enumerate(source, 1):
                try:
                    packet = decoded_record(json.loads(line))
                    emit(packet)
                    errors += "decode_error" in packet
                except (ValueError, TypeError) as error:
                    raise ValueError(f"Line {number}: {error}") from error
        if errors:
            raise ValueError(f"{errors} packet(s) could not be decoded; raw data preserved")
        return
    if args.command == "record-shots":
        recorder = ShotRecorder(args.shots_dir)
        try:
            async with ProfitecClient(args.device) as machine:
                async with aclosing(machine.events(args.seconds)) as stream:
                    async for packet in stream:
                        finished = recorder.feed(packet)
                        if finished:
                            emit({"shot": str(finished)})
        finally:
            recorder.close()
        return
    if args.command == "record":
        positive(args.seconds)
        # Reserve the output before connecting; never truncate an existing capture.
        with open(args.output, "x", encoding="utf-8") as output:
            async with ProfitecClient(args.device) as machine:
                async with aclosing(machine.events(args.seconds, log=args.log)) as stream:
                    async for packet in stream:
                        output.write(json.dumps(decoded_record(packet)) + "\n")
                        output.flush()
        return
    async with ProfitecClient(args.device) as machine:
        if args.command == "inspect":
            emit(machine.services())
        elif args.command == "report":
            from datetime import datetime
            from importlib.metadata import version
            blocks = {name: await machine.read_settings(name) for name in SETTINGS}
            mapped = {"firmware": {0}, "time": set(range(4)) | set(range(6, 34)),
                      # general word 21 is the advanced pre-infusion mode checked before pre-infusion writes.
                      "doses": set(), "general": {21}}
            for spec in SETTABLE.values():
                mapped[spec.block].add(spec.register - SETTINGS[spec.block][0])

            def named(block):
                try:
                    named_settings(block)
                    return "ok"
                except ValueError as error:
                    return str(error)
            emit({"library": version("profitec-ble"), "firmware": blocks["firmware"].firmware,
                  "blocks": {name: {"bytes": len(block.raw), "named": named(block),
                                    "unmapped_words": {str(i): w for i, w in enumerate(block.words) if i not in mapped[name]}}
                             for name, block in blocks.items()},
                  "clock_weekday_code": blocks["time"].words[0], "host_weekday": datetime.now().strftime("%A"),
                  "characteristics": [{"uuid": c["uuid"], "properties": c["properties"]}
                                      for service in machine.services() for c in service["characteristics"]]})
        elif args.command == "watch":
            tracker = ReadyTracker(named_settings(await machine.read_settings("general")).coffee_temperature_c)
            loop = asyncio.get_running_loop()
            bar = None
            async with aclosing(machine.events(args.seconds)) as stream:
                async for packet in stream:
                    try:
                        value = decode(packet["uuid"], bytes.fromhex(packet["hex"]))
                    except ValueError:
                        continue
                    if isinstance(value, Pressure):
                        bar = value.bar
                    elif isinstance(value, MachineStatus):
                        ready = tracker.update(value, loop.time())
                        print(f"{packet['time'][11:19]}  coffee {value.coffee_temperature}°C (set {tracker.setpoint})  "
                              f"steam {value.steam_temperature}°C  state {value.state}{' brewing' if value.brewing else ''}  "
                              f"{'-' if bar is None else f'{bar:.2f}'} bar{'  READY' if ready else ''}", flush=True)
        elif args.command == "settings":
            for name in SETTINGS:
                block = await machine.read_settings(name)
                typed = named_settings(block)
                emit({"name": name, "address": block.address, "words": block.words,
                      "firmware": block.firmware, "hex": block.raw.hex(),
                      "settings": asdict(typed) if is_dataclass(typed) else typed})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("scan")
    for name in ("inspect", "settings", "record", "report", "watch", "record-shots"):
        command = commands.add_parser(name)
        command.add_argument("device")
        if name == "record":
            command.add_argument("output")
            command.add_argument("--seconds", type=float, default=60)
            command.add_argument("--log", action="store_true", help="also capture raw device-log indications")
        if name in ("watch", "record-shots"):
            command.add_argument("--seconds", type=float, help="stop after N seconds (default: until interrupted)")
        if name == "record-shots":
            command.add_argument("--shots-dir", default="shots")
    command = commands.add_parser("metrics")
    command.add_argument("input")
    command = commands.add_parser("plot")
    command.add_argument("input")
    command.add_argument("output")
    command.add_argument("--compare", nargs="*", default=[], help="up to five earlier captures")
    for name in ("shots", "shots-index"):
        commands.add_parser(name).add_argument("--shots-dir", default="shots")
    command = commands.add_parser("shot-note")
    command.add_argument("shot")
    command.add_argument("--shots-dir", default="shots")
    for name in ("bean", "grind", "ratio", "notes"):
        command.add_argument(f"--{name}")
    command = commands.add_parser("profile-diff")
    command.add_argument("device")
    command.add_argument("input")
    command = commands.add_parser("journal-restore")
    command.add_argument("input")
    command.add_argument("--device")
    command.add_argument("--apply", action="store_true")
    command.add_argument("--journal")
    command = commands.add_parser("weekday-check", help="writes then restores one schedule window")
    command.add_argument("device")
    command.add_argument("day_index", type=int, choices=range(7))
    command.add_argument("--journal", required=True)
    command = commands.add_parser("bridge", help="MQTT / Home Assistant daemon (needs the [mqtt] extra)")
    command.add_argument("device")
    command.add_argument("--mqtt-host", required=True)
    command.add_argument("--mqtt-port", type=int, default=1883)
    command.add_argument("--mqtt-user")
    command.add_argument("--mqtt-password-env", help="name of the environment variable holding the password")
    command.add_argument("--prefix", default="profitec")
    command.add_argument("--journal-dir", help="enables settings writes; one new journal per write")
    command.add_argument("--shots-dir", help="record each shot to this directory")
    command.add_argument("--log", action="store_true", help="publish raw device-log indications (unverified)")
    command.add_argument("--allow-remote-brew", action="store_true",
                         help="EXPERIMENTAL: expose brew start/stop; gated on idle state and temperature")
    command = commands.add_parser("decode")
    command.add_argument("uuid")
    command.add_argument("hex")
    command = commands.add_parser("replay")
    command.add_argument("input")
    for name in ("summary", "export-csv"):
        command = commands.add_parser(name)
        command.add_argument("input")
        if name == "export-csv":
            command.add_argument("output")
    commands.add_parser("options")
    command = commands.add_parser("set")
    command.add_argument("device")
    command.add_argument("name", choices=SETTABLE)
    command.add_argument("value", help='JSON value, e.g. 93, true, or \'"off"\'')
    command.add_argument("--apply", action="store_true")
    command.add_argument("--journal")
    command = commands.add_parser("profile-save")
    command.add_argument("device")
    command.add_argument("output")
    command.add_argument("--name", required=True)
    command = commands.add_parser("profile-plan")
    command.add_argument("input")
    command = commands.add_parser("profile-apply")
    command.add_argument("device")
    command.add_argument("input")
    command.add_argument("--apply", action="store_true")
    command.add_argument("--journal")
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        parser.exit(130, "Interrupted; any capture is partial.\n")
    except Exception as error:
        parser.exit(1, f"{type(error).__name__}: {error}\n")


if __name__ == "__main__":
    main()
