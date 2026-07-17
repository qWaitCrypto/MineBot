#!/usr/bin/env python3
"""Craft transaction e2e against the local Carpet test server."""

from __future__ import annotations

import math
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.body import BlockWork, InventoryTransactions, NavigationTransactions
from minebot.game import GovernancePolicy, RconClient, Region, ScarpetBody
from minebot.game.errors import RconError
from minebot.game.governance import PlaceContext
from minebot.game.rcon import RconConfig
from tests.e2e_support import spawn_or_fail


BOT = "E2ECraftBot"
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
        "kill @e[type=!player]",
        f"player {BOT} kill",
        "fill -8 59 -8 8 66 8 air",
        "fill -8 58 -8 8 58 8 stone",
    ]:
        command(rcon, cmd)


def set_inventory_slot(rcon: RconClient, slot: int, item: str | None, count: int = 1) -> None:
    if item is None:
        command(rcon, f"script in minebot run inventory_set('{BOT}', {slot}, 0)")
        return
    command(rcon, f"script in minebot run inventory_set('{BOT}', {slot}, {count}, '{item}')")


def inventory_by_slot(body: ScarpetBody) -> dict[int, object]:
    return {slot.slot: slot for slot in body.get_inventory()}


def clear_bot_inventory(rcon: RconClient) -> None:
    command(rcon, f"clear {BOT}")
    for slot in range(46):
        set_inventory_slot(rcon, slot, None)


def make_runtime(body: ScarpetBody) -> InventoryTransactions:
    policy = GovernancePolicy(natural_regions=[Region("craft", (-8, 0, -8), (8, 100, 8))])
    navigator = NavigationTransactions.server_side(body, policy)
    work = BlockWork(body, policy, navigator=navigator)
    return InventoryTransactions(body, navigator=navigator, governance=policy, work=work)


def reset_position(rcon: RconClient) -> None:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill -8 59 -8 8 66 8 air")
    command(rcon, "fill -8 58 -8 8 58 8 stone")
    command(rcon, f"tp {BOT} 0.5 59 0.5 -90 0")


def run_craft_item_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    clear_bot_inventory(rcon)
    reset_position(rcon)
    command(rcon, f"item replace entity {BOT} hotbar.0 with oak_log 1")
    command(rcon, f"item replace entity {BOT} hotbar.1 with air")
    command(rcon, "script in minebot run minebot_reset()")

    event = body.craft_item(
        inputs=[{"slot": 0, "item": "minecraft:oak_log", "count": 1}],
        output={"slot": 1, "item": "minecraft:oak_planks", "count": 4},
    )
    if event.name != "craftDone" or not event.data.get("success"):
        raise AssertionError(f"craftItem failed: {event}")
    if event.data.get("item") not in {"oak_planks", "minecraft:oak_planks"}:
        raise AssertionError(f"wrong crafted item: {event.data}")
    if int(event.data.get("count") or 0) != 4:
        raise AssertionError(f"wrong crafted count: {event.data}")

    return {
        "event": event.name,
        "item": event.data.get("item"),
        "count": event.data.get("count"),
        "output_slot": event.data.get("output_slot"),
    }


def run_residue_cleanup_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    clear_bot_inventory(rcon)
    reset_position(rcon)
    set_inventory_slot(rcon, 0, "minecraft:stick", 60)
    set_inventory_slot(rcon, 41, "minecraft:stick", 6)
    set_inventory_slot(rcon, 42, "minecraft:string", 2)
    command(rcon, "script in minebot run minebot_reset()")

    runtime = InventoryTransactions(body)
    result = runtime.cleanup_crafting_residue(residue_slots=(41, 42), destination_slots=tuple(range(0, 36)), timeout_s=6.0)
    after = inventory_by_slot(body)
    payload = result.to_payload()

    if not result.success or result.reason != "completed":
        raise AssertionError(f"residue cleanup failed: {payload}")
    if after[41].item is not None or after[42].item is not None:
        raise AssertionError(f"residue slots not empty after cleanup: slot41={after[41]} slot42={after[42]} result={payload}")
    if after[0].count != 64 or after[0].item not in {"stick", "minecraft:stick"}:
        raise AssertionError(f"residue cleanup did not merge sticks into slot 0: slot0={after[0]} result={payload}")
    if not any(slot.item in {"stick", "minecraft:stick"} and slot.count == 2 for slot in after.values() if slot.slot != 0):
        raise AssertionError(f"residue cleanup did not preserve leftover sticks: after={after} result={payload}")
    if not any(slot.item in {"string", "minecraft:string"} and slot.count == 2 for slot in after.values()):
        raise AssertionError(f"residue cleanup did not preserve strings: after={after} result={payload}")

    return {
        "reason": result.reason,
        "executed": result.metrics.get("executed"),
        "slot0": {"item": after[0].item, "count": after[0].count},
        "slot41_empty": after[41].empty,
        "slot42_empty": after[42].empty,
    }


def run_residue_no_space_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    clear_bot_inventory(rcon)
    reset_position(rcon)
    set_inventory_slot(rcon, 0, "minecraft:stone", 64)
    set_inventory_slot(rcon, 1, "minecraft:dirt", 64)
    set_inventory_slot(rcon, 41, "minecraft:stick", 1)
    command(rcon, "script in minebot run minebot_reset()")

    runtime = InventoryTransactions(body)
    result = runtime.cleanup_crafting_residue(residue_slots=(41,), destination_slots=(0, 1), timeout_s=6.0)
    after = inventory_by_slot(body)
    payload = result.to_payload()

    if result.success or result.reason != "crafting_residue_no_space":
        raise AssertionError(f"residue no-space inverse returned wrong result: {payload}")
    if result.metrics.get("executed"):
        raise AssertionError(f"residue no-space inverse executed moves: {payload}")
    if after[41].item not in {"stick", "minecraft:stick"} or after[41].count != 1:
        raise AssertionError(f"residue no-space inverse lost residue: slot41={after[41]} result={payload}")
    if after[0].item not in {"stone", "minecraft:stone"} or after[1].item not in {"dirt", "minecraft:dirt"}:
        raise AssertionError(f"residue no-space inverse changed destinations: after={after} result={payload}")

    return {
        "reason": result.reason,
        "remaining_residue": result.metrics.get("remaining_residue"),
        "executed": result.metrics.get("executed"),
    }


def run_existing_table_auto_equip_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    clear_bot_inventory(rcon)
    reset_position(rcon)
    command(rcon, "setblock 5 59 0 crafting_table")
    set_inventory_slot(rcon, 0, "minecraft:leather", 5)

    runtime = make_runtime(body)
    result = runtime.craft_recipe(
        item="minecraft:leather_helmet",
        count=1,
        search_radius=8,
        auto_equip=True,
        output_slot=1,
        craft_timeout_s=8.0,
        approach_timeout_s=18.0,
    )
    payload = result.to_payload()
    after = inventory_by_slot(body)
    table_after = body.perceive("blockAt", {"x": 5, "y": 59, "z": 0})
    workspace = (result.metrics or {}).get("workspace") or {}
    equip = (result.metrics or {}).get("equip") or {}

    if not result.success or result.reason != "completed":
        raise AssertionError(f"existing table auto-equip craft failed: {payload}")
    if after[39].item not in {"leather_helmet", "minecraft:leather_helmet"} or after[39].count != 1:
        raise AssertionError(f"crafted helmet was not equipped: slot39={after[39]} result={payload}")
    if table_after.data.get("type") not in {"crafting_table", "minecraft:crafting_table"}:
        raise AssertionError(f"existing crafting table was mutated: block={table_after.data} result={payload}")
    if workspace.get("mode") != "existing_table":
        raise AssertionError(f"existing table craft did not use nearby table path: {payload}")
    if not ((workspace.get("approach") or {}).get("navigated") is True):
        raise AssertionError(f"existing table craft did not navigate into interaction range: {payload}")
    if equip.get("reason") != "completed":
        raise AssertionError(f"existing table craft did not expose completed auto-equip: {payload}")

    return {
        "reason": result.reason,
        "workspace_mode": workspace.get("mode"),
        "table_pos": workspace.get("table_pos"),
        "equipped_slot": {"item": after[39].item, "count": after[39].count},
    }


def run_existing_table_multi_count_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    clear_bot_inventory(rcon)
    reset_position(rcon)
    command(rcon, "setblock 5 59 0 crafting_table")
    set_inventory_slot(rcon, 0, "minecraft:oak_planks", 16)

    runtime = make_runtime(body)
    result = runtime.craft_recipe(
        item="minecraft:chest",
        count=2,
        search_radius=8,
        output_slot=1,
        craft_timeout_s=8.0,
        approach_timeout_s=18.0,
    )
    payload = result.to_payload()
    after = inventory_by_slot(body)
    workspace = (result.metrics or {}).get("workspace") or {}
    plan = (result.metrics or {}).get("craft_plan") or {}

    if not result.success or result.reason != "completed":
        raise AssertionError(f"existing table multi-count craft failed: {payload}")
    if after[1].item not in {"chest", "minecraft:chest"} or after[1].count != 2:
        raise AssertionError(f"existing table multi-count craft did not produce two chests: slot1={after[1]} result={payload}")
    if after[0].item is not None or after[0].count != 0:
        raise AssertionError(f"existing table multi-count craft did not consume sixteen planks: slot0={after[0]} result={payload}")
    if plan.get("crafted_count") != 2 or (plan.get("inputs") or [{}])[0].get("count") != 16:
        raise AssertionError(f"existing table multi-count craft exposed wrong aggregated plan: {payload}")
    if workspace.get("mode") != "existing_table":
        raise AssertionError(f"existing table multi-count craft did not use nearby table path: {payload}")

    return {
        "reason": result.reason,
        "workspace_mode": workspace.get("mode"),
        "crafted_count": plan.get("crafted_count"),
        "input_count": (plan.get("inputs") or [{}])[0].get("count"),
        "output_slot": {"item": after[1].item, "count": after[1].count},
    }


def run_temporary_table_remainder_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    clear_bot_inventory(rcon)
    reset_position(rcon)
    set_inventory_slot(rcon, 0, "minecraft:crafting_table", 1)
    set_inventory_slot(rcon, 1, "minecraft:milk_bucket", 1)
    set_inventory_slot(rcon, 2, "minecraft:milk_bucket", 1)
    set_inventory_slot(rcon, 3, "minecraft:milk_bucket", 1)
    set_inventory_slot(rcon, 4, "minecraft:sugar", 1)
    set_inventory_slot(rcon, 5, "minecraft:egg", 1)
    set_inventory_slot(rcon, 6, "minecraft:sugar", 1)
    set_inventory_slot(rcon, 7, "minecraft:wheat", 1)
    set_inventory_slot(rcon, 8, "minecraft:wheat", 1)
    set_inventory_slot(rcon, 9, "minecraft:wheat", 1)

    runtime = make_runtime(body)
    result = runtime.craft_recipe(
        item="minecraft:cake",
        count=1,
        search_radius=8,
        output_slot=10,
        temporary_table_radius=2,
        temporary_table_context=PlaceContext.DIRECT,
        craft_timeout_s=8.0,
        place_timeout_s=18.0,
        reclaim_timeout_s=18.0,
    )
    payload = result.to_payload()
    after = inventory_by_slot(body)
    workspace = (result.metrics or {}).get("workspace") or {}
    reclaim = (result.metrics or {}).get("reclaim") or {}

    if not result.success or result.reason != "completed":
        raise AssertionError(f"temporary table remainder craft failed: {payload}")
    if after[10].item not in {"cake", "minecraft:cake"} or after[10].count != 1:
        raise AssertionError(f"temporary table craft did not produce cake: slot10={after[10]} result={payload}")
    for slot_index in (1, 2, 3):
        if after[slot_index].item not in {"bucket", "minecraft:bucket"} or after[slot_index].count != 1:
            raise AssertionError(f"temporary table craft did not return buckets: slot{slot_index}={after[slot_index]} result={payload}")
    if workspace.get("mode") != "temporary_table":
        raise AssertionError(f"temporary table craft did not use temporary workstation path: {payload}")
    table_pos = workspace.get("table_pos")
    if not table_pos:
        raise AssertionError(f"temporary table craft did not expose placed table position: {payload}")
    block_after = body.perceive("blockAt", {"x": table_pos[0], "y": table_pos[1], "z": table_pos[2]})
    if block_after.data.get("state") != "CLEAR":
        raise AssertionError(f"temporary crafting table was not reclaimed: block={block_after.data} result={payload}")
    if reclaim.get("reason") != "completed":
        raise AssertionError(f"temporary table reclaim did not complete: {payload}")
    if not any(slot.item in {"crafting_table", "minecraft:crafting_table"} and slot.count >= 1 for slot in after.values()):
        raise AssertionError(f"temporary table reclaim did not return crafting table to inventory: after={after} result={payload}")

    return {
        "reason": result.reason,
        "workspace_mode": workspace.get("mode"),
        "table_pos": table_pos,
        "reclaim_reason": reclaim.get("reason"),
        "output_slot": {"item": after[10].item, "count": after[10].count},
    }


def run_table_missing_inverse(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    clear_bot_inventory(rcon)
    reset_position(rcon)
    set_inventory_slot(rcon, 0, "minecraft:leather", 5)

    runtime = make_runtime(body)
    before = body.get_state()
    result = runtime.craft_recipe(
        item="minecraft:leather_helmet",
        count=1,
        search_radius=8,
        output_slot=1,
        craft_timeout_s=8.0,
        approach_timeout_s=12.0,
    )
    after = body.get_state()
    payload = result.to_payload()
    inventory_after = inventory_by_slot(body)

    if result.success:
        raise AssertionError(f"table-missing inverse unexpectedly succeeded: {payload}")
    if not str(result.reason).startswith("crafting_table_select_failed:"):
        raise AssertionError(f"table-missing inverse returned wrong reason: {payload}")
    if math.dist(after.pos, before.pos) > 0.25:
        raise AssertionError(f"table-missing inverse moved the body: before={before.pos} after={after.pos} result={payload}")
    if inventory_after[39].item in {"leather_helmet", "minecraft:leather_helmet"}:
        raise AssertionError(f"table-missing inverse crafted/equipped a helmet: after={inventory_after} result={payload}")
    if any(slot.item in {"crafting_table", "minecraft:crafting_table"} for slot in inventory_after.values()):
        raise AssertionError(f"table-missing inverse somehow created a crafting table: after={inventory_after} result={payload}")

    return {
        "reason": result.reason,
        "before": before.pos,
        "after": after.pos,
        "workspace": (result.metrics or {}).get("workspace"),
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
            "craft_item": lambda: run_craft_item_path(rcon, body),
            "residue_cleanup": lambda: run_residue_cleanup_path(rcon, body),
            "residue_no_space": lambda: run_residue_no_space_path(rcon, body),
            "craft_existing_table_auto_equip": lambda: run_existing_table_auto_equip_path(rcon, body),
            "craft_existing_table_multi_count": lambda: run_existing_table_multi_count_path(rcon, body),
            "craft_temporary_table_remainder": lambda: run_temporary_table_remainder_path(rcon, body),
            "craft_table_missing": lambda: run_table_missing_inverse(rcon, body),
        }
        selected_raw = os.environ.get("MINEBOT_CRAFT_CASES")
        selected = [name.strip() for name in selected_raw.split(",") if name.strip()] if selected_raw else list(cases.keys())
        unknown = [name for name in selected if name not in cases]
        if unknown:
            raise AssertionError(f"unknown MINEBOT_CRAFT_CASES entries: {unknown}; valid={list(cases)}")
        results = {name: cases[name]() for name in selected}
        print(results)


if __name__ == "__main__":
    main()
