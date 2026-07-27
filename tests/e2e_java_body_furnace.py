#!/usr/bin/env python3
"""Bounded dry-land Java-only proof for ordinary furnace smelting. Not a gate."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.app.body_provider import build_body_provider
from minebot.body import FurnaceTransactions
from minebot.body.inventory_read import read_inventory_slots
from minebot.contract import Action, InventorySlot, Region, perception_next_cursor
from minebot.game.rcon import RconClient, RconConfig


BOT = "JavaFurnProbe"
BODY_URL = "ws://127.0.0.1:8767"
FURNACE = (2, 200, 0)
DENIED_FURNACE = (22, 200, 0)
REGION = Region("java-furnace-probe", (-4, 190, -4), (8, 212, 4))


def command(rcon: RconClient, value: str, delay: float = 0.05) -> str:
    response = rcon.command(value)
    if delay:
        time.sleep(delay)
    return response


def wait_for_presence(body, *, present: bool, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if body.get_state().missing == (not present):
            return
        time.sleep(0.1)
    raise AssertionError(f"FakePlayer presence did not become {present}")


def inventory(body) -> dict[int, InventorySlot]:
    deadline = time.monotonic() + 3.0
    while True:
        result = read_inventory_slots(body, page_size=46)
        if result.error != "rate_limited" or time.monotonic() >= deadline:
            break
        time.sleep(0.1)
    assert result.ok and result.complete, result
    return {
        slot.slot: slot
        for slot in (
            InventorySlot.from_payload(payload)
            for payload in result.data.get("slots") or []
        )
    }


def furnace(body, pos: tuple[int, int, int]) -> dict[int, InventorySlot]:
    start: int | None = 0
    slots: dict[int, InventorySlot] = {}
    deadline = time.monotonic() + 3.0
    while start is not None:
        result = body.perceive(
            "container",
            {"pos": list(pos), "start": start, "limit": 3},
        )
        if result.error == "rate_limited" and time.monotonic() < deadline:
            time.sleep(0.1)
            continue
        assert result.ok, result
        assert int(result.data.get("totalSlots") or 0) == 3, result
        for payload in result.data.get("slots") or []:
            slot = InventorySlot.from_payload(payload)
            slots[slot.slot] = slot
        cursor = perception_next_cursor(result)
        start = None if cursor is None else int(cursor)
    return slots


def item_count(slots: dict[int, InventorySlot], item: str) -> int:
    return sum(slot.count for slot in slots.values() if slot.item == item)


def main() -> int:
    rcon = RconClient(RconConfig(host="127.0.0.1", port=25576, password="test"))
    rcon.connect()
    provider = build_body_provider(
        "java",
        bot_name=BOT,
        natural_region=REGION,
        java_body_url=BODY_URL,
    )
    assert provider.java_body is not None
    body = provider.body
    try:
        command(rcon, "carpet commandPlayer true")
        command(rcon, "carpet allowSpawningOfflinePlayers true")
        command(rcon, f"player {BOT} kill")
        command(rcon, "fill -4 200 -4 24 204 4 air")
        command(rcon, "fill -4 199 -4 24 199 4 stone")
        command(rcon, f"setblock {FURNACE[0]} {FURNACE[1]} {FURNACE[2]} furnace")
        command(rcon, f"setblock {DENIED_FURNACE[0]} {DENIED_FURNACE[1]} {DENIED_FURNACE[2]} furnace")
        command(rcon, f"player {BOT} spawn")
        wait_for_presence(body, present=True)
        command(rcon, f"tp {BOT} 0 200 0")
        command(rcon, f"gamemode survival {BOT}")
        command(rcon, f"clear {BOT}")
        command(rcon, f"item replace entity {BOT} hotbar.0 with raw_iron 1")
        command(rcon, f"item replace entity {BOT} hotbar.1 with coal 1")

        recipe = body.perceive(
            "recipeData",
            {"item": "minecraft:iron_ingot", "type": "smelting"},
        )
        assert recipe.ok and any(
            "minecraft:raw_iron" in group
            for variant in recipe.data.get("variants") or []
            for group in variant.get("ingredient_groups") or []
            if group
        ), recipe

        before_inventory = inventory(body)
        before_furnace = furnace(body, FURNACE)
        assert item_count(before_inventory, "minecraft:raw_iron") == 1
        assert item_count(before_inventory, "minecraft:coal") == 1
        assert all(slot.empty for slot in before_furnace.values())

        result = FurnaceTransactions(body, governance=provider.governance).smelt_once(
            FURNACE,
            input_item="minecraft:raw_iron",
            input_count=1,
            fuel_item="minecraft:coal",
            fuel_count=1,
            output_item="minecraft:iron_ingot",
            output_count=1,
            output_slot=2,
            poll_interval_s=0.25,
            smelt_timeout_s=18.0,
            transfer_timeout_s=8.0,
        )
        after_inventory = inventory(body)
        after_furnace = furnace(body, FURNACE)
        assert result.success and result.reason == "completed", result
        assert item_count(after_inventory, "minecraft:raw_iron") == 0
        assert item_count(after_inventory, "minecraft:iron_ingot") == 1
        assert after_furnace[0].empty and after_furnace[2].empty
        executed = result.metrics.get("executed") or []
        assert [step.get("kind") for step in executed] == [
            "deposit_input", "deposit_fuel", "collect_output"
        ], result

        command(rcon, f"tp {BOT} 20 200 0")
        command(rcon, f"item replace entity {BOT} hotbar.3 with raw_iron 1")
        denied_before_inventory = inventory(body)
        denied_before_furnace = furnace(body, DENIED_FURNACE)
        denied_action = Action.create("furnaceTransfer", {
            "pos": list(DENIED_FURNACE),
            "direction": "bot_to_furnace",
            "furnace_slot": "input",
            "bot_slot": 3,
            "count": 1,
            "max_stack": 64,
        })
        denied_accepted = body.execute(denied_action)
        denied_terminal = body.await_action_terminal(denied_action.id)
        denied_after_inventory = inventory(body)
        denied_after_furnace = furnace(body, DENIED_FURNACE)
        assert denied_accepted.ok and denied_accepted.accepted
        assert denied_terminal.data.get("success") is False, denied_terminal
        assert str(denied_terminal.data.get("stopped_reason", "")).startswith(
            "governance_denied:"
        ), denied_terminal
        assert denied_before_inventory == denied_after_inventory
        assert denied_before_furnace == denied_after_furnace

        artifact = {
            "scope": "java_body_furnace_transfer",
            "formal_gate": False,
            "bounded": True,
            "environment": "dry_land",
            "provider": "java",
            "scarpet_body_constructed": False,
            "scarpet_action_calls": 0,
            "smelt": {
                "reason": result.reason,
                "recipe_raw_iron_supported": True,
                "raw_iron_before": 1,
                "raw_iron_after": 0,
                "iron_ingot_before": 0,
                "iron_ingot_after": 1,
                "executed": [step.get("kind") for step in executed],
                "poll_count": len(result.metrics.get("polls") or []),
            },
            "governance_inverse": {
                "reason": denied_terminal.data.get("stopped_reason"),
                "inventory_unchanged": True,
                "furnace_unchanged": True,
            },
        }
        output = Path("logs/agentic-runtime/java-body-furnace-transfer-20260727.json")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
        print(json.dumps(artifact, indent=2))
        return 0
    finally:
        try:
            command(rcon, f"player {BOT} kill")
            wait_for_presence(body, present=False)
        except Exception:
            pass
        provider.java_body._client.close()
        rcon.close()


if __name__ == "__main__":
    raise SystemExit(main())
