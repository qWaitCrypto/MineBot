#!/usr/bin/env python3
"""Bounded dry-land proof for Java Body inventory perception. Not a gate."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.body.inventory_read import read_inventory_slots
from minebot.contract import InventorySlot
from minebot.game.java_body import JavaBody
from minebot.game.java_body_adapter import JavaBodyClient, websocket_transport
from minebot.game.rcon import RconClient, RconConfig


BOT = "JavaInvProbe"
BODY_URL = "ws://127.0.0.1:8767"


def command(rcon: RconClient, value: str, delay: float = 0.05) -> str:
    response = rcon.command(value)
    if delay:
        time.sleep(delay)
    return response


def wait_for_presence(body: JavaBody, *, present: bool, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if body.get_state().missing == (not present):
            return
        time.sleep(0.1)
    raise AssertionError(f"FakePlayer presence did not become {present}")


def despawn_if_present(rcon: RconClient, body: JavaBody) -> None:
    if not body.get_state().missing:
        command(rcon, f"player {BOT} kill")
        wait_for_presence(body, present=False)


def main() -> int:
    rcon = RconClient(RconConfig(host="127.0.0.1", port=25576, password="test"))
    rcon.connect()
    client = JavaBodyClient(BOT, websocket_transport(BODY_URL))
    body = JavaBody(client, BOT)
    try:
        command(rcon, "carpet commandPlayer true")
        command(rcon, "carpet allowSpawningOfflinePlayers true")
        despawn_if_present(rcon, body)
        command(rcon, "fill -1 59 -1 1 61 1 air")
        command(rcon, "fill -1 58 -1 1 58 1 stone")
        command(rcon, f"player {BOT} spawn")
        wait_for_presence(body, present=True)
        command(rcon, f"tp {BOT} 0 59 0")
        command(rcon, f"clear {BOT}")
        command(rcon, f"item replace entity {BOT} hotbar.0 with stone 3")
        command(rcon, f"item replace entity {BOT} inventory.0 with iron_chestplate")
        command(rcon, f"item replace entity {BOT} weapon.offhand with shield")
        command(rcon, f"item replace entity {BOT} hotbar.1 with diamond_helmet[damage=7] 1")
        command(rcon, f"player {BOT} hotbar 2")
        command(rcon, f"enchant {BOT} minecraft:protection 2")
        command(rcon, f"item replace entity {BOT} armor.head from entity {BOT} hotbar.1")
        command(rcon, f"item replace entity {BOT} hotbar.1 with air")

        inventory = read_inventory_slots(body, page_size=7)
        assert inventory.ok and inventory.complete, inventory
        slots = {
            slot.slot: slot
            for slot in (
                InventorySlot.from_payload(payload)
                for payload in inventory.data.get("slots") or []
            )
        }
        assert len(slots) == 46
        assert (slots[0].item, slots[0].count, slots[0].slot_label) == (
            "minecraft:stone", 3, "hotbar.0"
        )
        assert (slots[9].item, slots[9].slot_label) == (
            "minecraft:iron_chestplate", "inventory.0"
        )
        assert (slots[39].item, slots[39].slot_label) == (
            "minecraft:diamond_helmet", "armor.head"
        )
        assert (slots[40].item, slots[40].slot_label) == (
            "minecraft:shield", "offhand"
        )
        helmet_raw = slots[39].stack_raw or ""
        assert "minecraft:damage" in helmet_raw and "7" in helmet_raw
        assert "minecraft:enchantments" in helmet_raw and "minecraft:protection" in helmet_raw
        assert all(slots[index].empty for index in (43, 44, 45))
        rcon_inventory = command(rcon, f"data get entity {BOT} Inventory")
        rcon_equipment = command(rcon, f"data get entity {BOT} equipment")
        assert all(item in rcon_inventory for item in ("stone", "iron_chestplate"))
        assert all(item in rcon_equipment for item in ("diamond_helmet", "shield"))

        command(rcon, f"player {BOT} kill")
        wait_for_presence(body, present=False)
        missing = body.perceive("inventory", {"start": 0, "limit": 7})
        assert not missing.ok and missing.complete
        assert missing.error == "missing_body"
        assert missing.uncertainty == [{"reason": "missing_body"}]

        artifact = {
            "scope": "java_body_inventory",
            "formal_gate": False,
            "bounded": True,
            "environment": "dry_land",
            "pages": 7,
            "total_slots": len(slots),
            "facts": {
                "hotbar_0": {"item": slots[0].item, "count": slots[0].count},
                "inventory_9": {"item": slots[9].item, "count": slots[9].count},
                "armor_head_39": {
                    "item": slots[39].item,
                    "metadata_has_damage": "minecraft:damage" in helmet_raw,
                    "metadata_has_protection": "minecraft:protection" in helmet_raw,
                },
                "offhand_40": {"item": slots[40].item, "count": slots[40].count},
                "padded_aux_empty": all(slots[index].empty for index in (43, 44, 45)),
                "rcon_cross_check": True,
            },
            "missing_body": {
                "ok": missing.ok,
                "complete": missing.complete,
                "error": missing.error,
                "uncertainty": missing.uncertainty,
            },
        }
        out = Path("logs/agentic-runtime/java-body-inventory-20260727.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
        print(json.dumps(artifact, indent=2))
        return 0
    finally:
        try:
            despawn_if_present(rcon, body)
        except Exception:
            pass
        client.close()
        rcon.close()


if __name__ == "__main__":
    raise SystemExit(main())
