#!/usr/bin/env python3
"""Real Body frontier exploration probe through the shared tool registry."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_registry
from minebot.contract import Region
from minebot.game import RconClient, ScarpetBody
from minebot.game.errors import RconError
from minebot.game.rcon import RconConfig
from tests.e2e_support import spawn_or_fail


BOT = "E2EExploreBot"
SKIP_EXIT_CODE = 77


def command(rcon: RconClient, value: str, delay: float = 0.05) -> str:
    result = rcon.command(value)
    if delay:
        time.sleep(delay)
    return result


def setup_world(rcon: RconClient) -> None:
    for value in (
        "script unload minebot",
        "script load minebot global",
        "carpet commandPlayer true",
        "carpet allowSpawningOfflinePlayers true",
        "gamerule doDaylightCycle false",
        "gamerule doWeatherCycle false",
        "gamerule doMobSpawning false",
        "difficulty peaceful",
        "time set day",
        "weather clear",
        "kill @e[type=!player]",
        "fill -15 50 -15 15 62 15 air",
        "fill -40 63 -40 40 63 40 stone",
        "fill -40 64 -40 40 67 40 air",
        "setblock -8 64 -8 dandelion",
        "script in minebot run minebot_reset()",
    ):
        command(rcon, value)


def main() -> None:
    config = RconConfig()
    try:
        rcon = RconClient(config)
        rcon.connect()
    except (OSError, PermissionError, RconError) as exc:
        if os.environ.get("MINEBOT_E2E_REQUIRED") == "1":
            raise
        print(
            f"SKIP: local RCON unavailable at {config.host}:{config.port}: "
            f"{type(exc).__name__}: {exc}"
        )
        raise SystemExit(SKIP_EXIT_CODE)

    with rcon:
        setup_world(rcon)
        body = ScarpetBody(BOT, rcon)
        spawn_or_fail(body, (0, 64, 0))
        command(rcon, f"tp {BOT} 0 64 0 0 0")
        registry = build_phase1_registry(
            body,
            Phase1RuntimeConfig(
                natural_region=Region("exploration-probe", (-48, 60, -48), (48, 80, 48))
            ),
        )

        result = registry.get("explore_for").callable(
            {
                "block_targets": ["#flowers"],
                "max_distance": 64,
                "max_regions": 3,
                "scan_radius": 8,
                "return_policy": "first_match",
            }
        )

        if not result.success or result.reason != "found":
            raise AssertionError(f"explore_for did not find frontier target: {result}")
        blocks = result.metrics.get("blocks") or []
        if not any(
            item.get("type") == "dandelion" and item.get("pos") == [-8, 64, -8]
            for item in blocks
            if isinstance(item, dict)
        ):
            raise AssertionError(f"explore_for returned wrong target facts: {blocks}")
        if int(result.metrics["budget"]["regions_consumed"]) < 2:
            raise AssertionError(f"explore_for did not cross a frontier: {result.metrics}")
        final_pos = body.get_state().pos
        if int(final_pos[0]) // 16 == 0 and int(final_pos[2]) // 16 == 0:
            raise AssertionError(f"explore_for did not leave the initial region: {final_pos}")

        print(
            {
                "reason": result.reason,
                "final_pos": list(final_pos),
                "regions_consumed": result.metrics["budget"]["regions_consumed"],
                "covered_regions": result.metrics["covered_regions"],
                "match": blocks[0],
            }
        )


if __name__ == "__main__":
    main()
