#!/usr/bin/env python3
"""discard_item Body transaction e2e against the local Carpet test server."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.body import InventoryTransactions
from minebot.game import RconClient, ScarpetBody
from minebot.game.errors import RconError
from minebot.game.rcon import RconConfig
from tests.e2e_support import spawn_or_fail


BOT = "E2EDiscardBot"
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


def inventory_by_slot(body: ScarpetBody) -> dict[int, object]:
    return {slot.slot: slot for slot in body.get_inventory()}


def same_item(actual: str | None, expected: str | None) -> bool:
    if actual is None or expected is None:
        return actual == expected
    return actual == expected or actual == f"minecraft:{expected}" or f"minecraft:{actual}" == expected


def set_inventory_slot(rcon: RconClient, slot: int, item: str | None, count: int = 1) -> None:
    if item is None:
        command(rcon, f"script in minebot run inventory_set('{BOT}', {slot}, 0)")
        return
    command(rcon, f"script in minebot run inventory_set('{BOT}', {slot}, {count}, '{item}')")


def clear_bot_inventory(rcon: RconClient) -> None:
    command(rcon, f"clear {BOT}")
    for slot in range(46):
        set_inventory_slot(rcon, slot, None)


def run_happy_non_hotbar_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    clear_bot_inventory(rcon)
    command(rcon, f"tp {BOT} 0 59 0")
    set_inventory_slot(rcon, 0, None)
    set_inventory_slot(rcon, 18, "minecraft:diamond", 3)
    command(rcon, "script in minebot run minebot_reset()")

    runtime = InventoryTransactions(body)
    before = inventory_by_slot(body)
    result = runtime.discard_item(item="minecraft:diamond", count=3, timeout_s=6.0)
    after = inventory_by_slot(body)

    if not result.success or result.reason != "completed":
        raise AssertionError(f"discard_item failed: {result}")
    if int((result.metrics or {}).get("dropped_count") or 0) != 3:
        raise AssertionError(f"wrong dropped count: {result.metrics}")
    executed = list((result.metrics or {}).get("executed") or [])
    if [step.get("kind") for step in executed] != ["stage", "drop"]:
        raise AssertionError(f"unexpected execution plan: {executed}")
    if not same_item(before[18].item, "minecraft:diamond") or before[18].count != 3:
        raise AssertionError(f"unexpected setup inventory slot 18: {before[18]}")
    if after[18].count != 0:
        raise AssertionError(f"staged source slot was not cleared: {after[18]}")
    if after[0].count != 0:
        raise AssertionError(f"temporary hotbar slot was not emptied by drop: {after[0]}")

    return {
        "reason": result.reason,
        "dropped_count": (result.metrics or {}).get("dropped_count"),
        "executed_kinds": [step.get("kind") for step in executed],
        "source_slot": 18,
        "staging_slot": 0,
    }


def run_hotbar_full_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    clear_bot_inventory(rcon)
    command(rcon, f"tp {BOT} 0 59 0")
    for slot in range(9):
        set_inventory_slot(rcon, slot, "minecraft:cobblestone", 1)
    set_inventory_slot(rcon, 18, "minecraft:diamond", 2)
    command(rcon, "script in minebot run minebot_reset()")

    runtime = InventoryTransactions(body)
    before = inventory_by_slot(body)
    result = runtime.discard_item(item="minecraft:diamond", count=1, timeout_s=6.0)
    after = inventory_by_slot(body)
    payload = result.to_payload()

    if result.success or result.reason != "hotbar_full":
        raise AssertionError(f"discard full-hotbar inverse returned wrong result: {payload}")
    if "executed" in (result.metrics or {}):
        raise AssertionError(f"discard full-hotbar inverse should not execute moves: {payload}")
    if int((result.metrics or {}).get("available_count") or 0) != 2:
        raise AssertionError(f"discard full-hotbar inverse did not report available count: {payload}")
    if int((result.metrics or {}).get("planned_count") or 0) != 0:
        raise AssertionError(f"discard full-hotbar inverse planned a destructive move: {payload}")
    for slot in range(9):
        if not same_item(after[slot].item, "minecraft:cobblestone") or after[slot].count != 1:
            raise AssertionError(f"hotbar slot {slot} changed despite hotbar_full: before={before[slot]} after={after[slot]}")
    if not same_item(after[18].item, "minecraft:diamond") or after[18].count != 2:
        raise AssertionError(f"source diamonds changed despite hotbar_full: before={before[18]} after={after[18]}")

    return {
        "reason": result.reason,
        "available_count": result.metrics.get("available_count"),
        "planned_count": result.metrics.get("planned_count"),
        "source_after": {"item": after[18].item, "count": after[18].count},
    }


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
        cases = {
            "happy_non_hotbar": lambda: run_happy_non_hotbar_path(rcon, body),
            "hotbar_full": lambda: run_hotbar_full_path(rcon, body),
        }
        selected_raw = os.environ.get("MINEBOT_DISCARD_CASES")
        selected = [name.strip() for name in selected_raw.split(",") if name.strip()] if selected_raw else list(cases.keys())
        unknown = [name for name in selected if name not in cases]
        if unknown:
            raise AssertionError(f"unknown MINEBOT_DISCARD_CASES entries: {unknown}; valid={list(cases)}")
        results = {name: cases[name]() for name in selected}
        print(results)


if __name__ == "__main__":
    main()
