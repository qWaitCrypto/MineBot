#!/usr/bin/env python3
"""Physical placeBlock e2e against the local Carpet test server."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.body import BlockWork
from minebot.game import RconClient, ScarpetBody
from minebot.game.errors import RconError
from minebot.game.governance import BreakContext, GovernancePolicy, PlaceContext, Region
from minebot.game.rcon import RconConfig


BOT = "E2EPlaceBot"
TARGET = (112, 70, 2)
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
        "fill 106 70 -2 118 76 6 air",
        "fill 106 69 -2 118 69 6 stone",
        f"setblock {TARGET[0]} {TARGET[1]} {TARGET[2]} air",
        f"setblock {TARGET[0]} {TARGET[1] - 1} {TARGET[2]} stone",
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
        body.spawn((112, 70, 0))
        command(rcon, f"tp {BOT} 112 70 0 0 0")
        command(rcon, f"gamemode survival {BOT}")
        command(rcon, f"effect clear {BOT}")
        command(rcon, f"item replace entity {BOT} weapon.mainhand with cobblestone 8")
        command(rcon, "script in minebot run minebot_reset()")

        policy = GovernancePolicy(natural_regions=[Region("e2e-place", (106, 60, -2), (118, 80, 6))])
        runtime = BlockWork(body, policy)
        result = runtime.place_block(
            TARGET,
            "minecraft:cobblestone",
            face="up",
            context=PlaceContext.WORK,
            purpose="bridge",
            timeout_s=10.0,
        )
        block_after = body.perceive("blockAt", {"x": TARGET[0], "y": TARGET[1], "z": TARGET[2]})
        cleanup = policy.can_break(TARGET, "minecraft:cobblestone", BreakContext.BOT_CLEANUP)

        if not result.success:
            raise AssertionError(f"place_block failed: {result}")
        if result.metrics.get("block_at_target") not in {"cobblestone", "minecraft:cobblestone"}:
            raise AssertionError(f"placeDone did not report cobblestone at target: {result}")
        if block_after.data.get("type") not in {"cobblestone", "minecraft:cobblestone"}:
            raise AssertionError(f"target block was not placed: {block_after}")
        if not cleanup.allowed:
            raise AssertionError(f"bot placement ledger did not allow cleanup: {cleanup}")

        print(
            {
                "target": TARGET,
                "tool_result": result.metrics,
                "block_after": block_after.data,
                "cleanup": cleanup.reason,
            }
        )


if __name__ == "__main__":
    main()
