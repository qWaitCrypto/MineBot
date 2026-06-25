#!/usr/bin/env python3
"""equip_item Body transaction e2e against the local Carpet test server."""

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


BOT = "E2EEquipBot"
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
    command(rcon, f"item replace entity {BOT} weapon.offhand with air")
    command(rcon, f"item replace entity {BOT} armor.head with air")
    for slot in range(46):
        set_inventory_slot(rcon, slot, None)


def scarpet_slot_raw(rcon: RconClient, slot: int) -> str:
    return command(rcon, f"script in minebot run inventory_get('{BOT}', {slot})")


def run_happy_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    clear_bot_inventory(rcon)
    command(rcon, f"tp {BOT} 0 59 0")
    command(rcon, f"item replace entity {BOT} inventory.9 with arrow 16")
    command(rcon, f"item replace entity {BOT} inventory.10 with diamond_helmet")
    command(rcon, f"item replace entity {BOT} armor.head with iron_helmet")
    command(rcon, "script in minebot run minebot_reset()")

    runtime = InventoryTransactions(body)
    offhand_before = inventory_by_slot(body)
    offhand_result = runtime.equip_item(item="minecraft:arrow", target="offhand", timeout_s=6.0)
    offhand_after = inventory_by_slot(body)
    if not offhand_result.success or offhand_result.reason not in {"completed", "already_equipped"}:
        raise AssertionError(f"offhand equip failed: {offhand_result}")
    if not same_item(offhand_after[40].item, "minecraft:arrow"):
        raise AssertionError(f"offhand slot 40 not updated: {offhand_after[40]}")

    head_result = runtime.equip_item(item="minecraft:diamond_helmet", target="head", timeout_s=6.0)
    head_after = inventory_by_slot(body)
    if not head_result.success or head_result.reason not in {"completed", "already_equipped"}:
        raise AssertionError(f"head equip failed: {head_result}")
    if not same_item(head_after[39].item, "minecraft:diamond_helmet"):
        raise AssertionError(f"head slot 39 not updated: {head_after[39]}")

    staged_slot = next(
        (
            idx
            for idx, slot in head_after.items()
            if idx not in {39, 40} and same_item(slot.item, "minecraft:iron_helmet")
        ),
        None,
    )
    if staged_slot is None:
        raise AssertionError(f"replaced helmet was not preserved in carry inventory: {head_after}")

    return {
        "offhand_reason": offhand_result.reason,
        "offhand_before_slot_40": getattr(offhand_before.get(40), "item", None),
        "offhand_after_slot_40": getattr(offhand_after.get(40), "item", None),
        "head_reason": head_result.reason,
        "head_after_slot_39": head_after[39].item,
        "staged_old_helmet_slot": staged_slot,
    }


def run_no_swap_space_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    clear_bot_inventory(rcon)
    command(rcon, f"tp {BOT} 0 59 0")
    for slot in range(36):
        set_inventory_slot(rcon, slot, "minecraft:cobblestone", 1)
    set_inventory_slot(rcon, 22, "minecraft:diamond_helmet", 1)
    command(rcon, f"item replace entity {BOT} armor.head with iron_helmet")
    command(rcon, "script in minebot run minebot_reset()")

    runtime = InventoryTransactions(body)
    before = inventory_by_slot(body)
    result = runtime.equip_item(item="minecraft:diamond_helmet", target="head", timeout_s=6.0)
    after = inventory_by_slot(body)
    payload = result.to_payload()

    if result.success or result.reason != "no_swap_space":
        raise AssertionError(f"equip no-swap inverse returned wrong result: {payload}")
    if "executed" in (result.metrics or {}):
        raise AssertionError(f"equip no-swap inverse should not execute moves: {payload}")
    target_before = (result.metrics or {}).get("target_before") or {}
    if not same_item(target_before.get("item"), "minecraft:iron_helmet"):
        raise AssertionError(f"equip no-swap inverse did not record occupied target truth: {payload}")
    if not same_item(after[39].item, "minecraft:iron_helmet"):
        raise AssertionError(f"existing helmet was overwritten despite no swap space: before={before[39]} after={after[39]}")
    if not same_item(after[22].item, "minecraft:diamond_helmet") or after[22].count != 1:
        raise AssertionError(f"source helmet moved despite no swap space: before={before[22]} after={after[22]}")
    for slot in range(36):
        if before[slot].item != after[slot].item or before[slot].count != after[slot].count or before[slot].empty != after[slot].empty:
            raise AssertionError(f"carry slot {slot} changed despite no swap space: before={before[slot]} after={after[slot]}")

    return {
        "reason": result.reason,
        "target_before": target_before,
        "source_slot": result.metrics.get("source_slot"),
        "stage_slot": result.metrics.get("stage_slot"),
        "head_after": after[39].item,
        "source_after": after[22].item,
    }


def run_metadata_preservation_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    clear_bot_inventory(rcon)
    command(rcon, f"tp {BOT} 0 59 0")
    command(rcon, f"item replace entity {BOT} hotbar.0 with diamond_helmet[damage=7] 1")
    command(rcon, f"player {BOT} hotbar 1")
    command(rcon, f"enchant {BOT} minecraft:protection 2")
    command(rcon, f"item replace entity {BOT} hotbar.1 with iron_helmet[damage=3] 1")
    command(rcon, f"player {BOT} hotbar 2")
    command(rcon, f"enchant {BOT} minecraft:unbreaking 1")
    command(rcon, f"item replace entity {BOT} armor.head from entity {BOT} hotbar.1")
    command(rcon, f"item replace entity {BOT} hotbar.1 with air")
    command(rcon, "script in minebot run minebot_reset()")

    runtime = InventoryTransactions(body)
    result = runtime.equip_item(item="minecraft:diamond_helmet", target="head", timeout_s=6.0)
    after = inventory_by_slot(body)
    payload = result.to_payload()

    if not result.success or result.reason != "completed":
        raise AssertionError(f"damaged equip failed: {payload}")
    if not same_item(after[39].item, "minecraft:diamond_helmet"):
        raise AssertionError(f"damaged diamond helmet not equipped: slot39={after[39]} result={payload}")
    head_raw = scarpet_slot_raw(rcon, 39)
    if '"minecraft:damage":7' not in head_raw:
        raise AssertionError(f"equipped diamond helmet lost damage metadata: raw={head_raw} result={payload}")
    if '"minecraft:enchantments":{"minecraft:protection":2}' not in head_raw:
        raise AssertionError(f"equipped diamond helmet lost enchantment metadata: raw={head_raw} result={payload}")

    stage_slot = result.metrics.get("stage_slot")
    if not isinstance(stage_slot, int) or stage_slot < 0:
        raise AssertionError(f"damaged equip did not expose staging slot: {payload}")
    staged_raw = scarpet_slot_raw(rcon, stage_slot)
    if "iron_helmet" not in staged_raw or '"minecraft:damage":3' not in staged_raw:
        raise AssertionError(f"staged iron helmet lost damage metadata: raw={staged_raw} result={payload}")
    if '"minecraft:enchantments":{"minecraft:unbreaking":1}' not in staged_raw:
        raise AssertionError(f"staged iron helmet lost enchantment metadata: raw={staged_raw} result={payload}")

    return {
        "reason": result.reason,
        "stage_slot": stage_slot,
        "head_raw": head_raw,
        "staged_raw": staged_raw,
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
            "happy": lambda: run_happy_path(rcon, body),
            "no_swap_space": lambda: run_no_swap_space_path(rcon, body),
            "metadata_preservation": lambda: run_metadata_preservation_path(rcon, body),
        }
        selected_raw = os.environ.get("MINEBOT_EQUIP_CASES")
        selected = [name.strip() for name in selected_raw.split(",") if name.strip()] if selected_raw else list(cases.keys())
        unknown = [name for name in selected if name not in cases]
        if unknown:
            raise AssertionError(f"unknown MINEBOT_EQUIP_CASES entries: {unknown}; valid={list(cases)}")
        results = {name: cases[name]() for name in selected}
        print(results)


if __name__ == "__main__":
    main()
