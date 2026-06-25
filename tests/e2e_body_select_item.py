#!/usr/bin/env python3
"""selectItem e2e against the local Carpet test server."""

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


BOT = "E2ESelectItemBot"
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
        command(rcon, f"item replace entity {BOT} hotbar.2 with stone 4")
        command(rcon, f"item replace entity {BOT} inventory.9 with bread 3")
        command(rcon, f"item replace entity {BOT} hotbar.0 with air")
        command(rcon, "script in minebot run minebot_reset()")

        event = body.select_item("minecraft:stone")
        if not event.data.get("success"):
            raise AssertionError(f"selectItem failed: {event}")
        if event.data.get("slot") != 2:
            raise AssertionError(f"selectItem chose wrong slot: {event.data}")

        moved = body.select_item("minecraft:bread")
        if not moved.data.get("success"):
            raise AssertionError(f"inventory selectItem failed: {moved}")
        if moved.data.get("slot") != 0:
            raise AssertionError(f"inventory selectItem chose wrong slot: {moved.data}")
        if moved.data.get("stopped_reason") != "moved_to_hotbar":
            raise AssertionError(f"inventory selectItem did not report staging: {moved.data}")

        print(
            {
                "hotbar_event": event.name,
                "hotbar_slot": event.data.get("slot"),
                "inventory_event": moved.name,
                "inventory_slot": moved.data.get("slot"),
                "inventory_reason": moved.data.get("stopped_reason"),
            }
        )


if __name__ == "__main__":
    main()
