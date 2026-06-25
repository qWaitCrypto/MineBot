#!/usr/bin/env python3
"""dropItem e2e against the local Carpet test server."""

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


BOT = "E2EDropBot"
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
        "time set day",
        "kill @e[type=!player]",
        f"player {BOT} kill",
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
        command(rcon, f"item replace entity {BOT} hotbar.2 with cobblestone 16")
        command(rcon, "script in minebot run minebot_reset()")

        event = body.drop_item(slot=2, mode="all")
        if event.name != "dropDone" or not event.data.get("success"):
            raise AssertionError(f"dropItem failed: {event}")
        if event.data.get("item") not in {"cobblestone", "minecraft:cobblestone"}:
            raise AssertionError(f"wrong dropped item: {event.data}")
        if int(event.data.get("count_after") or 0) >= int(event.data.get("count_before") or 0):
            raise AssertionError(f"dropItem did not reduce slot count: {event.data}")

        print(
            {
                "event": event.name,
                "item": event.data.get("item"),
                "mode": event.data.get("mode"),
                "count_before": event.data.get("count_before"),
                "count_after": event.data.get("count_after"),
            }
        )


if __name__ == "__main__":
    main()
