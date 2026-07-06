#!/usr/bin/env python3
"""Probe whether craft_recipe handles 2x2 recipes without a crafting table."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.body import InventoryTransactions
from minebot.game import RconClient, ScarpetBody
from minebot.game.errors import RconError
from minebot.game.rcon import RconConfig
from tests.e2e_support import SKIP_EXIT_CODE, spawn_or_fail


BOT = "ProbeCraft2x2"


def command(rcon: RconClient, command_text: str, delay: float = 0.05) -> str:
    output = rcon.command(command_text)
    if delay:
        time.sleep(delay)
    return output


def setup_world(rcon: RconClient) -> None:
    for command_text in [
        "script unload minebot",
        "script load minebot global",
        "carpet commandPlayer true",
        "carpet allowSpawningOfflinePlayers true",
        "gamerule doDaylightCycle false",
        "gamerule doWeatherCycle false",
        "gamerule doMobSpawning false",
        "time set day",
        "weather clear",
        "kill @e[type=!player]",
        f"player {BOT} kill",
        "fill -8 59 -8 8 66 8 air",
        "fill -8 58 -8 8 58 8 stone",
    ]:
        command(rcon, command_text)


def set_inventory_slot(rcon: RconClient, slot: int, item: str | None, count: int = 1) -> None:
    if item is None:
        command(rcon, f"script in minebot run inventory_set('{BOT}', {slot}, 0)")
        return
    command(rcon, f"script in minebot run inventory_set('{BOT}', {slot}, {count}, '{item}')")


def clear_bot_inventory(rcon: RconClient) -> None:
    command(rcon, f"clear {BOT}")
    for slot in range(46):
        set_inventory_slot(rcon, slot, None)


def inventory_by_slot(body: ScarpetBody) -> dict[int, object]:
    return {slot.slot: slot for slot in body.get_inventory()}


def main() -> None:
    if len(BOT) > 16:
        raise AssertionError(f"probe bot name too long for Carpet fake players: {BOT!r}")
    config = RconConfig()
    try:
        with RconClient(config) as rcon:
            setup_world(rcon)
            body = ScarpetBody(BOT, rcon)
            spawn_or_fail(body, (0, 59, 0))
            results = {
                "oak_planks": _probe_case(
                    rcon,
                    body,
                    setup_slots=[("minecraft:oak_log", 1)],
                    item="minecraft:oak_planks",
                    count=4,
                    expected_slot=1,
                ),
                "crafting_table": _probe_case(
                    rcon,
                    body,
                    setup_slots=[("minecraft:oak_planks", 4)],
                    item="minecraft:crafting_table",
                    count=1,
                    expected_slot=1,
                ),
                "stick": _probe_case(
                    rcon,
                    body,
                    setup_slots=[("minecraft:oak_planks", 2)],
                    item="minecraft:stick",
                    count=4,
                    expected_slot=1,
                ),
            }
    except (OSError, PermissionError, RconError) as exc:
        if os.environ.get("MINEBOT_E2E_REQUIRED") == "1":
            raise
        print(f"SKIP: local RCON unavailable at {config.host}:{config.port}: {type(exc).__name__}: {exc}")
        raise SystemExit(SKIP_EXIT_CODE)

    supported = all(bool(row["success"]) and row["workspace"] is None for row in results.values())
    payload = {"supported": supported, "results": results}
    print(json.dumps(payload, sort_keys=True))
    if not supported:
        raise SystemExit(2)


def _probe_case(
    rcon: RconClient,
    body: ScarpetBody,
    *,
    setup_slots: list[tuple[str, int]],
    item: str,
    count: int,
    expected_slot: int,
) -> dict[str, object]:
    clear_bot_inventory(rcon)
    command(rcon, "fill -8 59 -8 8 66 8 air")
    command(rcon, "fill -8 58 -8 8 58 8 stone")
    command(rcon, "fill -8 59 -8 8 61 8 air")
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, f"tp {BOT} 0.5 59 0.5 -90 0")
    for index, (slot_item, slot_count) in enumerate(setup_slots):
        set_inventory_slot(rcon, index, slot_item, slot_count)
    time.sleep(0.05)

    result = InventoryTransactions(body).craft_recipe(
        item=item,
        count=count,
        output_slot=expected_slot,
        search_radius=8,
        craft_timeout_s=8.0,
    )
    after = inventory_by_slot(body)
    output = after[expected_slot]
    return {
        "success": result.success,
        "reason": result.reason,
        "workspace": (result.metrics or {}).get("workspace"),
        "output_item": output.item,
        "output_count": output.count,
        "payload": result.to_payload(),
    }


if __name__ == "__main__":
    main()
