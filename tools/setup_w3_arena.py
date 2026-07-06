#!/usr/bin/env python3
"""Prepare the fixed W3 diamond-ladder arena on the local test server."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.game import RconClient  # noqa: E402
from minebot.game.errors import RconError  # noqa: E402
from minebot.game.rcon import RconConfig  # noqa: E402
from tests.e2e_support import SKIP_EXIT_CODE  # noqa: E402

ARENA_MIN = (-24, 68, -28)
ARENA_MAX = (28, 78, 28)
SPAWN_POS = (0, 70, 0)


def command(rcon: RconClient, command_text: str, delay: float = 0.03) -> str:
    out = rcon.command(command_text)
    if delay:
        time.sleep(delay)
    return out


def setup_w3_arena(rcon: RconClient) -> dict[str, object]:
    for cmd in [
        "script load minebot global",
        "carpet commandPlayer true",
        "carpet allowSpawningOfflinePlayers true",
        "gamerule doDaylightCycle false",
        "gamerule doWeatherCycle false",
        "gamerule doMobSpawning false",
        "gamerule random_tick_speed 0",
        "time set day",
        "weather clear",
        "difficulty peaceful",
        "kill @e[type=!minecraft:player,type=!minecraft:item]",
        f"fill {ARENA_MIN[0]} {ARENA_MIN[1]} {ARENA_MIN[2]} {ARENA_MAX[0]} {ARENA_MAX[1]} {ARENA_MAX[2]} air",
        f"fill {ARENA_MIN[0]} 69 {ARENA_MIN[2]} {ARENA_MAX[0]} 69 {ARENA_MAX[2]} smooth_stone",
    ]:
        command(rcon, cmd)

    log_positions = [(2 + x, 70, -3 - z) for x in range(3) for z in range(3)]
    stone_positions = [(8 + i, 70, -6) for i in range(16)]
    iron_positions = [(12 + i, 70, -3) for i in range(6)]
    diamond_positions = [(12 + i, 70, 3) for i in range(6)]

    for pos in log_positions:
        _setblock(rcon, pos, "oak_log")
    for pos in stone_positions:
        _setblock(rcon, pos, "stone")
    for pos in iron_positions:
        _setblock(rcon, pos, "iron_ore")
    for pos in diamond_positions:
        _setblock(rcon, pos, "deepslate_diamond_ore")

    reset = command(rcon, "script in minebot run minebot_reset()")
    if "true" not in reset.lower():
        raise RuntimeError(f"minebot_reset failed: {reset}")

    return {
        "spawn": list(SPAWN_POS),
        "arena_min": list(ARENA_MIN),
        "arena_max": list(ARENA_MAX),
        "logs": [list(pos) for pos in log_positions],
        "stone_count": len(stone_positions),
        "iron_ore": [list(pos) for pos in iron_positions],
        "deepslate_diamond_ore": [list(pos) for pos in diamond_positions],
    }


def _setblock(rcon: RconClient, pos: tuple[int, int, int], block: str) -> None:
    command(rcon, f"setblock {pos[0]} {pos[1]} {pos[2]} {block}")


def main() -> None:
    config = RconConfig()
    try:
        with RconClient(config) as rcon:
            print(setup_w3_arena(rcon))
    except (OSError, PermissionError, RconError) as exc:
        if os.environ.get("MINEBOT_E2E_REQUIRED") == "1":
            raise
        print(f"SKIP: local RCON unavailable at {config.host}:{config.port}: {type(exc).__name__}: {exc}")
        raise SystemExit(SKIP_EXIT_CODE)


if __name__ == "__main__":
    main()
