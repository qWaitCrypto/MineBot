#!/usr/bin/env python3
"""Bounded dry-land proof for Java select/look/use player primitives."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.body import InventoryTransactions, UseTransactions
from minebot.body.inventory_read import read_inventory_slots
from minebot.contract import Action, InventorySlot
from minebot.game.java_body import JavaBody
from minebot.game.java_body_adapter import JavaBodyClient, websocket_transport
from minebot.game.rcon import RconClient, RconConfig


BOT = "JavaActionProbe"
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


def read_complete_inventory(body: JavaBody):
    deadline = time.monotonic() + 2.0
    while True:
        result = read_inventory_slots(body, page_size=46)
        if result.ok and result.complete:
            return result
        if result.error != "rate_limited" or time.monotonic() >= deadline:
            raise AssertionError(result)
        time.sleep(0.1)


def inventory_count(body: JavaBody, item: str) -> int:
    result = read_complete_inventory(body)
    return sum(
        slot.count
        for slot in (
            InventorySlot.from_payload(payload)
            for payload in result.data.get("slots") or []
        )
        if slot.item == item
    )


def inventory_slots(body: JavaBody) -> dict[int, InventorySlot]:
    result = read_complete_inventory(body)
    return {
        slot.slot: slot
        for slot in (
            InventorySlot.from_payload(payload)
            for payload in result.data.get("slots") or []
        )
    }


def despawn_if_present(rcon: RconClient, body: JavaBody) -> None:
    if not body.get_state().missing:
        command(rcon, f"player {BOT} kill")
        wait_for_presence(body, present=False)


def lower_food(rcon: RconClient, body: JavaBody) -> int:
    command(rcon, "difficulty normal")
    command(rcon, f"effect give {BOT} minecraft:hunger 30 255 true")
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        food = body.get_state().food
        if food <= 14:
            command(rcon, f"effect clear {BOT}")
            return food
        time.sleep(0.25)
    raise AssertionError(f"hunger fixture did not lower food: {body.get_state().food}")


def main() -> int:
    rcon = RconClient(RconConfig(host="127.0.0.1", port=25576, password="test"))
    rcon.connect()
    client = JavaBodyClient(BOT, websocket_transport(BODY_URL))
    body = JavaBody(client, BOT)
    try:
        command(rcon, "carpet commandPlayer true")
        command(rcon, "carpet allowSpawningOfflinePlayers true")
        despawn_if_present(rcon, body)
        command(rcon, "fill -2 59 -2 12 62 12 air")
        command(rcon, "fill -2 58 -2 12 58 12 stone")
        command(rcon, "setblock 3 60 0 stone")
        command(rcon, "setblock 2 60 0 lever[face=wall,facing=west,powered=false]")
        command(rcon, f"player {BOT} spawn")
        wait_for_presence(body, present=True)
        command(rcon, f"tp {BOT} 0 59 0 0 0")
        command(rcon, f"gamemode survival {BOT}")
        command(rcon, f"clear {BOT}")
        command(rcon, f"item replace entity {BOT} inventory.0 with bread 2")

        look = Action.create("lookAt", {"target": [10.0, 60.62, 10.0]})
        look_accepted = body.execute(look)
        look_terminal = body.await_action_terminal(look.id)
        assert look_accepted.ok and look_accepted.accepted
        assert look_terminal.data.get("success") is True, look_terminal
        assert float(look_terminal.data.get("alignment", 0.0)) >= 0.99, look_terminal

        food_before = lower_food(rcon, body)
        bread_before = inventory_count(body, "minecraft:bread")
        result = UseTransactions(body).consume_item(
            item="minecraft:bread",
            use_ticks=80,
            timeout_s=8.0,
        )
        bread_after = inventory_count(body, "minecraft:bread")
        food_after = body.get_state().food
        assert result.success and result.reason == "completed", result
        assert bread_before == 2 and bread_after == 1, (bread_before, bread_after)
        assert food_after > food_before, (food_before, food_after)
        assert result.metrics.get("item_delta") == 1, result.metrics
        assert result.metrics.get("food_delta", 0) > 0, result.metrics
        assert (result.metrics.get("select") or {}).get("moved_to_hotbar") is True, result.metrics

        missing = InventoryTransactions(body).equip_item(
            item="minecraft:diamond_sword",
            target="mainhand",
            timeout_s=2.0,
        )
        assert not missing.success and missing.reason == "item_not_available", missing

        switch = UseTransactions(body).use_on_block(
            pos=(2, 60, 0),
            item=None,
            expected_block_types=("lever",),
            expected_properties={"powered": "true"},
            look_target=(2.5, 60.5, 0.5),
            use_ticks=1,
            timeout_s=4.0,
        )
        assert switch.success and switch.reason == "completed", switch
        switch_after = body.perceive("blockAt", {"x": 2, "y": 60, "z": 0})
        assert switch_after.data.get("properties", {}).get("powered") == "true", switch_after

        command(rcon, f"clear {BOT}")
        command(rcon, f"item replace entity {BOT} inventory.0 with diamond_helmet[damage=7] 1")
        equip = InventoryTransactions(body).equip_item(
            item="minecraft:diamond_helmet",
            target="head",
            timeout_s=3.0,
        )
        equipped_slots = inventory_slots(body)
        assert equip.success and equip.reason == "completed", equip
        assert equipped_slots[39].item == "minecraft:diamond_helmet", equipped_slots[39]
        assert "minecraft:damage" in (equipped_slots[39].stack_raw or ""), equipped_slots[39]
        assert "7" in (equipped_slots[39].stack_raw or ""), equipped_slots[39]

        command(rcon, f"clear {BOT}")
        command(rcon, f"item replace entity {BOT} inventory.9 with diamond 3")
        diamond_before = inventory_count(body, "minecraft:diamond")
        discard = InventoryTransactions(body).discard_item(
            item="minecraft:diamond",
            count=3,
            timeout_s=4.0,
        )
        diamond_after = inventory_count(body, "minecraft:diamond")
        drops = body.perceive(
            "nearbyEntities",
            {"radius": 8, "types": ["item"], "name": "Diamond", "limit": 16},
        )
        assert discard.success and discard.reason == "completed", discard
        assert diamond_before == 3 and diamond_after == 0, (diamond_before, diamond_after)
        assert any(
            entity.get("type") == "minecraft:item" and entity.get("name") == "Diamond"
            for entity in drops.data.get("entities") or []
        ), drops

        command(rcon, f"clear {BOT}")
        command(rcon, f"item replace entity {BOT} hotbar.0 with cobblestone 1")
        command(rcon, f"item replace entity {BOT} inventory.9 with diamond 1")
        inverse_before = inventory_slots(body)
        inverse_action = Action.create(
            "moveItem",
            {"from_slot": 18, "to_slot": 0, "count": 1},
        )
        inverse_accepted = body.execute(inverse_action)
        inverse_terminal = body.await_action_terminal(inverse_action.id)
        inverse_after = inventory_slots(body)
        assert inverse_accepted.ok and inverse_accepted.accepted
        assert inverse_terminal.data.get("success") is False, inverse_terminal
        assert inverse_terminal.data.get("stopped_reason") == "destination_occupied", inverse_terminal
        assert inverse_before[18].item == inverse_after[18].item == "minecraft:diamond"
        assert inverse_before[18].count == inverse_after[18].count == 1
        assert inverse_before[0].item == inverse_after[0].item == "minecraft:cobblestone"
        assert inverse_before[0].count == inverse_after[0].count == 1

        artifact = {
            "scope": "java_body_player_actions",
            "formal_gate": False,
            "bounded": True,
            "environment": "dry_land",
            "provider": "java",
            "scarpet_action_calls": 0,
            "look": {
                "success": look_terminal.data.get("success"),
                "alignment": look_terminal.data.get("alignment"),
                "yaw": look_terminal.data.get("yaw"),
                "pitch": look_terminal.data.get("pitch"),
            },
            "consume": {
                "reason": result.reason,
                "bread_before": bread_before,
                "bread_after": bread_after,
                "food_before": food_before,
                "food_after": food_after,
                "selected_slot": (result.metrics.get("select") or {}).get("slot"),
                "moved_to_hotbar": (result.metrics.get("select") or {}).get("moved_to_hotbar"),
            },
            "missing_item": {
                "success": missing.success,
                "reason": missing.reason,
            },
            "switch": {
                "success": switch.success,
                "reason": switch.reason,
                "powered_after": switch_after.data.get("properties", {}).get("powered"),
                "stop_provider": "java",
                "look_provider": "java",
                "use_provider": "java",
            },
            "equip": {
                "success": equip.success,
                "reason": equip.reason,
                "target_slot": 39,
                "item": equipped_slots[39].item,
                "damage_metadata_preserved": "minecraft:damage" in (equipped_slots[39].stack_raw or ""),
            },
            "discard": {
                "success": discard.success,
                "reason": discard.reason,
                "diamond_before": diamond_before,
                "diamond_after": diamond_after,
                "dropped_count": (discard.metrics or {}).get("dropped_count"),
                "world_item_observed": True,
            },
            "occupied_destination": {
                "success": inverse_terminal.data.get("success"),
                "reason": inverse_terminal.data.get("stopped_reason"),
                "source_unchanged": True,
                "destination_unchanged": True,
            },
        }
        out = Path("logs/agentic-runtime/java-body-player-actions-20260727.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
        print(json.dumps(artifact, indent=2))
        return 0
    finally:
        command(rcon, "difficulty peaceful", delay=0.0)
        try:
            despawn_if_present(rcon, body)
        except Exception:
            pass
        client.close()
        rcon.close()


if __name__ == "__main__":
    raise SystemExit(main())
