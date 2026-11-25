"""Telemetry, profiles, and settings. Setting writes require --apply and a journal."""
import argparse
import asyncio
from contextlib import aclosing
from dataclasses import asdict, is_dataclass
import json
from .protocol import decode, SETTINGS
from .shots import summarize, export_csv
from .settings import named_settings, setting_command, setting_values, SETTABLE
from .profiles import load_profile, save_profile
from .client import ProfitecClient, positive, scan


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
    if args.command == "record":
        positive(args.seconds)
        # Reserve the output before connecting; never truncate an existing capture.
        with open(args.output, "x", encoding="utf-8") as output:
            async with ProfitecClient(args.device) as machine:
                async with aclosing(machine.events(args.seconds)) as stream:
                    async for packet in stream:
                        output.write(json.dumps(decoded_record(packet)) + "\n")
                        output.flush()
        return
    async with ProfitecClient(args.device) as machine:
        if args.command == "inspect":
            emit(machine.services())
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
    for name in ("inspect", "settings", "record"):
        command = commands.add_parser(name)
        command.add_argument("device")
        if name == "record":
            command.add_argument("output")
            command.add_argument("--seconds", type=float, default=60)
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
