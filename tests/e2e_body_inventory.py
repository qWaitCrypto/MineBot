#!/usr/bin/env python3
"""inventory perception e2e against the local Carpet test server."""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.game import RconClient, ScarpetBody
from tests.e2e_support import connect_or_skip, spawn_or_fail


BOT = "E2EInventoryBot"


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
    ]:
        command(rcon, cmd)


def normalize_item(item: str | None) -> str | None:
    if item is None:
        return None
    return item.removeprefix("minecraft:")


def slot_map(body: ScarpetBody) -> dict[int, object]:
    return {slot.slot: slot for slot in body.get_inventory(page_size=7)}


def run_happy_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    spawn_or_fail(body, (0, 59, 0))
    command(rcon, f"tp {BOT} 0 59 0 -90 0")
    command(rcon, f"clear {BOT}")
    command(rcon, f"item replace entity {BOT} hotbar.0 with stone 3")
    command(rcon, f"item replace entity {BOT} weapon.offhand with shield")
    command(rcon, f"item replace entity {BOT} hotbar.1 with diamond_helmet[damage=7] 1")
    command(rcon, f"player {BOT} hotbar 2")
    command(rcon, f"enchant {BOT} minecraft:protection 2")
    command(rcon, f"item replace entity {BOT} armor.head from entity {BOT} hotbar.1")
    command(rcon, f"item replace entity {BOT} hotbar.1 with air")
    command(rcon, f"item replace entity {BOT} inventory.0 with iron_chestplate")
    command(rcon, "script in minebot run minebot_reset()")

    slots = slot_map(body)
    hotbar = slots[0]
    backpack = slots[9]
    head = slots[39]
    offhand = slots[40]

    if normalize_item(hotbar.item) != "stone" or hotbar.count != 3:
        raise AssertionError(f"hotbar slot 0 wrong: {hotbar}")
    if hotbar.slot_type != "hotbar" or hotbar.slot_label != "hotbar.0":
        raise AssertionError(f"hotbar classification missing: {hotbar}")
    if hotbar.stack_raw is None or "minecraft:stone" not in hotbar.stack_raw:
        raise AssertionError(f"hotbar stackRaw missing item metadata: {hotbar}")

    if normalize_item(backpack.item) != "iron_chestplate":
        raise AssertionError(f"inventory slot 9 wrong: {backpack}")
    if backpack.slot_type != "inventory" or backpack.slot_label != "inventory.0":
        raise AssertionError(f"inventory classification missing: {backpack}")

    if normalize_item(head.item) != "diamond_helmet":
        raise AssertionError(f"armor head slot wrong: {head}")
    if head.slot_type != "armor" or head.slot_label != "armor.head":
        raise AssertionError(f"armor classification missing: {head}")
    if head.stack_raw is None or '"minecraft:damage":7' not in head.stack_raw:
        raise AssertionError(f"armor stackRaw missing damage metadata: {head}")
    if '"minecraft:enchantments":{"minecraft:protection":2}' not in head.stack_raw:
        raise AssertionError(f"armor stackRaw missing enchant metadata: {head}")

    if normalize_item(offhand.item) != "shield":
        raise AssertionError(f"offhand slot wrong: {offhand}")
    if offhand.slot_type != "offhand" or offhand.slot_label != "offhand":
        raise AssertionError(f"offhand classification missing: {offhand}")

    return {
        "hotbar0": {"item": hotbar.item, "slotType": hotbar.slot_type, "slotLabel": hotbar.slot_label},
        "inventory9": {"item": backpack.item, "slotType": backpack.slot_type, "slotLabel": backpack.slot_label},
        "head39": {"item": head.item, "slotType": head.slot_type, "slotLabel": head.slot_label, "stackRaw": head.stack_raw},
        "offhand40": {"item": offhand.item, "slotType": offhand.slot_type, "slotLabel": offhand.slot_label},
    }


def run_missing_body_inverse(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    command(rcon, f"player {BOT} kill")
    perception = body.perceive("inventory", {"start": 0, "limit": 7})
    if perception.ok or perception.error != "missing_body":
        raise AssertionError(f"missing-body inventory inverse returned wrong truth: {perception}")
    uncertainty = perception.uncertainty or []
    if {"reason": "missing_body"} not in uncertainty:
        raise AssertionError(f"missing-body uncertainty missing: {perception}")
    return {
        "ok": perception.ok,
        "complete": perception.complete,
        "error": perception.error,
        "uncertainty": uncertainty,
    }


def main() -> int:
    with connect_or_skip() as rcon:
        setup_world(rcon)
        body = ScarpetBody(BOT, rcon)
        happy = run_happy_path(rcon, body)
        inverse = run_missing_body_inverse(rcon, body)
        print({"happy": happy, "missing_body": inverse})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
