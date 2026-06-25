#!/usr/bin/env python3
"""containerTransfer e2e against the local Carpet test server."""

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


BOT = "E2EContainerBot"
SKIP_EXIT_CODE = 77
CHEST = (1, 59, 0)


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
        "time set day",
        "kill @e[type=!player]",
        f"player {BOT} kill",
        "fill -2 59 -2 2 62 2 air",
        "fill -2 58 -2 2 58 2 stone",
        f"setblock {CHEST[0]} {CHEST[1]} {CHEST[2]} chest",
        f"item replace block {CHEST[0]} {CHEST[1]} {CHEST[2]} container.0 with diamond 3",
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
        spawn_or_fail(body, (0, 59, 0))
        command(rcon, f"tp {BOT} 0 59 0")
        command(rcon, f"item replace entity {BOT} hotbar.1 with air")
        command(rcon, f"item replace entity {BOT} hotbar.2 with diamond 1")
        command(rcon, "script in minebot run minebot_reset()")

        event = body.container_transfer(
            pos=CHEST,
            direction="container_to_bot",
            container_slot=0,
            bot_slot=1,
            count=2,
        )
        if event.name != "containerDone" or not event.data.get("success"):
            raise AssertionError(f"containerTransfer failed: {event}")
        if event.data.get("item") not in {"diamond", "minecraft:diamond"}:
            raise AssertionError(f"wrong transferred item: {event.data}")
        if event.data.get("count") != 2:
            raise AssertionError(f"wrong transferred count: {event.data}")

        merged = body.container_transfer(
            pos=CHEST,
            direction="container_to_bot",
            container_slot=0,
            bot_slot=2,
        )
        if merged.name != "containerDone" or not merged.data.get("success"):
            raise AssertionError(f"containerTransfer merge failed: {merged}")

        print(
            {
                "event": event.name,
                "item": event.data.get("item"),
                "count": event.data.get("count"),
                "merge_count": merged.data.get("count"),
            }
        )


if __name__ == "__main__":
    main()
