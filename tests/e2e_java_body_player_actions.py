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


def inventory_count(body: JavaBody, item: str) -> int:
    result = read_inventory_slots(body, page_size=9)
    assert result.ok and result.complete, result
    return sum(
        slot.count
        for slot in (
            InventorySlot.from_payload(payload)
            for payload in result.data.get("slots") or []
        )
        if slot.item == item
    )


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
