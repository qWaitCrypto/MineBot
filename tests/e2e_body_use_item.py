#!/usr/bin/env python3
"""useItem e2e against the local Carpet test server."""

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


BOT = "E2EUseItemBot"
SKIP_EXIT_CODE = 77


def command(rcon: RconClient, command: str, delay: float = 0.05) -> str:
    out = rcon.command(command)
    if delay:
        time.sleep(delay)
    return out


def entity_int(rcon: RconClient, path: str) -> int:
    raw = command(rcon, f"data get entity {BOT} {path}", delay=0.0)
    return int(raw.rsplit(":", 1)[-1].strip())


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
        command(rcon, f"gamemode survival {BOT}")
        command(rcon, f"effect give {BOT} minecraft:hunger 30 255 true", delay=6.0)
        command(rcon, f"effect clear {BOT}")
        command(rcon, f"item replace entity {BOT} hotbar.0 with bread 2")
        command(rcon, "script in minebot run minebot_reset()")

        food_before = entity_int(rcon, "foodLevel")
        before = body.get_inventory()
        event = body.use_item(mode="continuous", ticks=80, item="minecraft:bread", slot=0, timeout_s=8.0)
        after = body.get_inventory()
        food_after = entity_int(rcon, "foodLevel")
        if event.name != "useDone" or not event.data.get("success"):
            raise AssertionError(f"useItem failed: {event}")
        for key in ("inventory_before", "inventory_after", "start_pos", "final_pos"):
            if key not in event.data:
                raise AssertionError(f"useItem event missing {key}: {event.data}")
        before_slot = before[0]
        after_slot = after[0]
        if before_slot.item != "bread" or before_slot.count != 2:
            raise AssertionError(f"unexpected bread setup before use: {before_slot}")
        if after_slot.count >= before_slot.count:
            raise AssertionError(f"useItem did not consume one bread: before={before_slot} after={after_slot}")
        if food_after <= food_before:
            raise AssertionError(f"useItem did not increase food: before={food_before} after={food_after}")

        print(
            {
                "event": event.name,
                "mode": event.data.get("mode"),
                "item": event.data.get("item"),
                "ticks": event.data.get("ticks"),
                "before_count": before_slot.count,
                "after_count": after_slot.count,
                "food_before": food_before,
                "food_after": food_after,
            }
        )


if __name__ == "__main__":
    main()
