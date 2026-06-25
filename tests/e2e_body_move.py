#!/usr/bin/env python3
"""First production Body e2e against the local Carpet test server."""

from __future__ import annotations

import math
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.game import Action, RconClient, ScarpetBody
from minebot.game.errors import RconError
from minebot.game.rcon import RconConfig
from tests.e2e_support import spawn_or_fail


BOT = "E2EBot"
SKIP_EXIT_CODE = 77


def command(rcon: RconClient, command: str, delay: float = 0.05) -> str:
    out = rcon.command(command)
    if delay:
        time.sleep(delay)
    return out


def setup_world(rcon: RconClient) -> None:
    for cmd in [
        "script unload minebot",
        "script load minebot global",
        "carpet commandPlayer true",
        "carpet allowSpawningOfflinePlayers true",
        "gamerule doDaylightCycle false",
        "gamerule doWeatherCycle false",
        "gamerule doMobSpawning false",
        "time set day",
        "weather clear",
        "difficulty normal",
        "kill @e[type=!player]",
        "fill -4 59 -4 20 66 4 air",
        "fill -4 58 -4 20 58 4 stone",
    ]:
        command(rcon, cmd)


def distance(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def main() -> None:
    config = RconConfig()
    try:
        rcon = RconClient(config)
        rcon.connect()
    except (OSError, PermissionError, RconError) as exc:
        if os.environ.get("MINEBOT_E2E_REQUIRED") == "1":
            raise
        print(f"SKIP: local RCON unavailable at {config.host}:{config.port}: {type(exc).__name__}: {exc}")
        raise SystemExit(SKIP_EXIT_CODE)

    with rcon:
        setup_world(rcon)
        body = ScarpetBody(BOT, rcon)
        spawn_or_fail(body, (0, 59, 0))
        command(rcon, f"tp {BOT} 0 59 0 -90 0")
        command(rcon, f"gamemode survival {BOT}")
        command(rcon, f"effect clear {BOT}")
        command(rcon, "script in minebot run minebot_reset()")

        start = body.get_state()
        target = (8, 59, 0)
        action = Action.create("moveTo", {"target": list(target)})
        accepted = body.execute(action)
        if not accepted.ok or not accepted.accepted:
            raise AssertionError(f"moveTo was not accepted: {accepted}")

        terminal = body.await_action_terminal(action.id, timeout_s=15.0)
        final = body.get_state()
        dist = distance(final.pos, target)

        if terminal.name != "moveDone":
            raise AssertionError(f"expected moveDone, got {terminal}")
        if not terminal.data.get("arrived"):
            raise AssertionError(f"moveDone did not report arrived: {terminal}")
        if dist > 1.0:
            raise AssertionError(f"final position too far from target: {final.pos} target={target} dist={dist:.3f}")

        print(
            {
                "start": start.pos,
                "target": target,
                "final": final.pos,
                "dist": round(dist, 3),
                "terminal": terminal.data,
            }
        )


if __name__ == "__main__":
    main()
