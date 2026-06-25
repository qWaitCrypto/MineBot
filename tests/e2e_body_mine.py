#!/usr/bin/env python3
"""Physical mineBlock e2e against the local Carpet test server."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.body import BlockWork
from minebot.game import RconClient, ScarpetBody
from minebot.game.errors import RconError
from minebot.game.governance import BreakContext, GovernancePolicy, Region
from minebot.game.rcon import RconConfig


BOT = "E2EMineBot"
TARGET = (110, 70, 3)
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
        "fill 104 70 -2 116 76 6 air",
        "fill 104 69 -2 116 69 6 stone",
        f"setblock {TARGET[0]} {TARGET[1]} {TARGET[2]} stone",
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
        body.spawn((110, 70, 0))
        command(rcon, f"tp {BOT} 110 70 0 0 0")
        command(rcon, f"gamemode survival {BOT}")
        command(rcon, f"effect clear {BOT}")
        command(rcon, f"item replace entity {BOT} weapon.mainhand with diamond_pickaxe")
        command(rcon, "script in minebot run minebot_reset()")

        policy = GovernancePolicy(natural_regions=[Region("e2e-mine", (104, 60, -2), (116, 80, 6))])
        runtime = BlockWork(body, policy)
        result = runtime.mine_block(TARGET, context=BreakContext.TRAVEL, timeout_s=15.0)
        block_after = body.perceive("blockAt", {"x": TARGET[0], "y": TARGET[1], "z": TARGET[2]})
        state_after = body.get_state()
        events_after = [event.data | {"name": event.name, "seq": event.seq, "tick": event.tick} for event in body.poll_events()]

        if not result.success:
            raise AssertionError(
                f"mine_block failed: result={result.to_payload()} state_after={state_after} "
                f"block_after={block_after} events_after={events_after}"
            )
        if not result.metrics.get("block_gone", False):
            raise AssertionError(
                f"mineDone did not report block_gone: result={result.to_payload()} "
                f"state_after={state_after} block_after={block_after} events_after={events_after}"
            )
        if block_after.data.get("state") != "CLEAR":
            raise AssertionError(
                f"target block still not clear: result={result.to_payload()} "
                f"state_after={state_after} block_after={block_after} events_after={events_after}"
            )

        print(
            {
                "target": TARGET,
                "tool_result": result.to_payload(),
                "state_after": state_after,
                "block_after": block_after.data,
                "events_after": events_after,
            }
        )


if __name__ == "__main__":
    main()
