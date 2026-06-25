#!/usr/bin/env python3
"""Direct igniteBlock primitive probe against the local Carpet test server."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.game import RconClient, ScarpetBody
from minebot.game.errors import RconError
from minebot.game.rcon import RconConfig
from tests.e2e_support import spawn_or_fail


BOT = "IgniteProbe"
TARGET = (0, 70, 2)
STAND = (-1, 70, 2)
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
        "gamerule doFireTick false",
        "time set day",
        "weather clear",
        "difficulty normal",
        "kill @e[type=!player]",
        f"player {BOT} kill",
        "fill -4 70 -2 4 76 6 air",
        "fill -4 69 -2 4 69 6 stone",
        f"setblock {TARGET[0]} {TARGET[1] - 1} {TARGET[2]} netherrack",
        f"setblock {TARGET[0]} {TARGET[1]} {TARGET[2]} air",
    ]:
        command(rcon, cmd)


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
        spawn_or_fail(body, STAND)
        command(rcon, f"tp {BOT} {STAND[0]} {STAND[1]} {STAND[2]} 0 0")
        command(rcon, f"gamemode survival {BOT}")
        command(rcon, f"effect give {BOT} minecraft:fire_resistance 9999 5 true")
        command(rcon, f"item replace entity {BOT} hotbar.0 with flint_and_steel")
        command(rcon, f"player {BOT} hotbar 1")
        command(rcon, "script in minebot run minebot_reset()")

        before = body.perceive("blockAt", {"x": TARGET[0], "y": TARGET[1], "z": TARGET[2]})
        terminal = body.ignite_block(TARGET, item="minecraft:flint_and_steel", timeout_s=8.0)
        after = body.perceive("blockAt", {"x": TARGET[0], "y": TARGET[1], "z": TARGET[2]})

        print(
            {
                "event": terminal.data,
                "method": terminal.data.get("method"),
                "stand": STAND,
                "success": terminal.data.get("success"),
                "stopped_reason": terminal.data.get("stopped_reason"),
                "block_at_target": terminal.data.get("block_at_target"),
                "before": before.data,
                "after": after.data,
            }
        )


if __name__ == "__main__":
    main()
