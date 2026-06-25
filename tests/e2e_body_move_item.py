#!/usr/bin/env python3
"""moveItem e2e against the local Carpet test server."""

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


BOT = "E2EMoveItemBot"
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
        command(rcon, f"item replace entity {BOT} inventory.9 with stone 8")
        command(rcon, f"item replace entity {BOT} hotbar.0 with air")
        command(rcon, f"item replace entity {BOT} hotbar.1 with stone 2")
        command(rcon, "script in minebot run minebot_reset()")

        inventory = body.get_inventory()
        source_slot = next(
            (
                slot.slot
                for slot in inventory
                if slot.item in {"stone", "minecraft:stone"} and slot.count == 8
            ),
            None,
        )
        if source_slot is None:
            raise AssertionError(f"could not find staged stone stack in inventory snapshot: {[slot.__dict__ for slot in inventory]}")

        event = body.move_item(from_slot=source_slot, to_slot=0, count=3)
        if event.name != "moveItemDone" or not event.data.get("success"):
            raise AssertionError(f"moveItem failed: {event}")
        if event.data.get("item") not in {"stone", "minecraft:stone"}:
            raise AssertionError(f"wrong moved item: {event.data}")
        if int(event.data.get("count") or 0) != 3:
            raise AssertionError(f"wrong moved count: {event.data}")

        merged = body.move_item(from_slot=0, to_slot=1, count=2)
        if merged.name != "moveItemDone" or not merged.data.get("success"):
            raise AssertionError(f"moveItem merge failed: {merged}")
        if int(merged.data.get("count") or 0) != 2:
            raise AssertionError(f"wrong merged count: {merged.data}")

        print(
            {
                "event": event.name,
                "item": event.data.get("item"),
                "count": event.data.get("count"),
                "from_slot": event.data.get("from_slot"),
                "to_slot": event.data.get("to_slot"),
                "merge_count": merged.data.get("count"),
            }
        )


if __name__ == "__main__":
    main()
