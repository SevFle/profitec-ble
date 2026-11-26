"""EXPERIMENTAL brew-by-weight. A timed dose is always the backstop: when the weight source goes silent
the shot continues and the machine ends it at its dose time. One stop command at most; never retried.

No scale driver is included. `scale` is any object with `weight` (grams), `age` (seconds since the
last reading, infinite when none) and `async tare()`."""
import asyncio
import json
import math
import os
import warnings
from contextlib import aclosing, suppress
from datetime import datetime, timezone
from .protocol import STATUS_UUID, decode_status


async def brew_by_weight(machine, scale, target_g: float, dose: int, journal, *, stop_offset_g: float = 2.0,
                         stale_seconds: float = 1.0, settle_seconds: float = 5.0, start_timeout: float = 10.0,
                         tare_timeout: float = 3.0) -> dict:
    if type(dose) is not int or dose not in (1, 2):
        raise ValueError("Brew-by-weight needs timed dose 1 or 2 as its backstop")
    if not all(type(v) in (int, float) and math.isfinite(v) for v in (target_g, stop_offset_g)) \
            or not 0 <= stop_offset_g < target_g <= 500:
        raise ValueError("Need 0 <= stop_offset_g < target_g <= 500")
    if not machine.allow_experimental_controls:
        raise PermissionError("Experimental controls disabled; opt in explicitly at construction")
    if scale.age > stale_seconds:
        raise RuntimeError("No fresh scale reading; refusing to start")
    loop = asyncio.get_running_loop()
    stop_at = target_g - stop_offset_g
    with open(journal, "x", encoding="utf-8") as log:
        def record(event, **fields):
            log.write(json.dumps({"event": event, "time": datetime.now(timezone.utc).isoformat(), **fields}) + "\n")
            log.flush()
            os.fsync(log.fileno())

        record("before", device=machine.device, target_g=target_g, dose=dose, stop_offset_g=stop_offset_g, weight_g=scale.weight)
        await scale.tare()
        deadline = loop.time() + tare_timeout
        while not (scale.age <= stale_seconds and abs(scale.weight) < 0.5):
            if loop.time() > deadline:
                record("tare_failed", weight_g=scale.weight)
                raise RuntimeError("Scale did not settle near zero after tare; nothing brewed")
            await asyncio.sleep(0.05)
        record("tared")

        state = {"brewing": None, "seen": False}

        async def follow(stream):
            async for packet in stream:
                if packet["uuid"] == STATUS_UUID and packet["hex"].startswith("000c"):
                    status = decode_status(bytes.fromhex(packet["hex"]))
                    state["brewing"] = status.brewing
                    state["seen"] |= status.brewing

        async with aclosing(machine.events(None)) as stream:
            follower = asyncio.create_task(follow(stream))
            try:
                record("starting")
                await machine.brew(dose)
                record("started")
                deadline = loop.time() + start_timeout
                lost, reason, warning = False, None, None
                while True:
                    await asyncio.sleep(0.05)
                    if follower.done():
                        follower.result()
                        raise ConnectionError("Machine stream ended during brew-by-weight")
                    if not lost and scale.age > stale_seconds:
                        lost = True
                        warning = "Scale stopped reporting; shot continues to its timed dose"
                        record("scale_lost", weight_g=scale.weight)
                        warnings.warn(warning, RuntimeWarning, stacklevel=2)
                    if not lost and scale.weight >= stop_at:
                        record("stopping", weight_g=scale.weight)
                        await machine.brew(None)
                        record("stop_sent")
                        reason = "target"
                        break
                    if state["seen"] and state["brewing"] is False:
                        reason = "scale_lost" if lost else "target_not_reached"
                        record("dose_ended", weight_g=scale.weight, reason=reason)
                        break
                    if not state["seen"] and loop.time() > deadline:
                        record("start_unconfirmed")
                        raise RuntimeError("Brewing not observed after start; no retry sent, inspect the machine")
                await asyncio.sleep(settle_seconds)
                final = scale.weight if scale.age <= stale_seconds else None
                result = {"reason": reason, "target_g": target_g, "stop_sent": reason == "target",
                          "final_weight_g": final, "stopped_observed": state["brewing"] is False, "warning": warning}
                record("final", **result)
                return result
            finally:
                follower.cancel()
                with suppress(asyncio.CancelledError):
                    await follower
