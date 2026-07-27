#!/usr/bin/env python3
"""Bounded dry-land proof for Java recipe truth and ordinary crafting. Not a gate."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.app.body_provider import build_body_provider
from minebot.body import InventoryTransactions
from minebot.body.inventory_read import read_inventory_slots
from minebot.contract import Action, InventorySlot, Region
from minebot.game.rcon import RconClient, RconConfig


BOT = "JavaCraftProbe"
BODY_URL = "ws://127.0.0.1:8767"
REGION = Region("java-craft-probe", (-8, 190, -8), (8, 212, 8))


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


def slots(body) -> dict[int, InventorySlot]:
    deadline = time.monotonic() + 3.0
    while True:
        result = read_inventory_slots(body, page_size=46)
        if result.ok and result.complete:
            return {
                slot.slot: slot
                for slot in (
                    InventorySlot.from_payload(payload)
                    for payload in result.data.get("slots") or []
                )
            }
        if result.error != "rate_limited" or time.monotonic() >= deadline:
            raise AssertionError(result)
        time.sleep(0.1)


def count(inventory: dict[int, InventorySlot], item: str) -> int:
    return sum(slot.count for slot in inventory.values() if slot.item == item)


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
        command(rcon, "fill -8 200 -8 8 204 8 air")
        command(rcon, "fill -8 199 -8 8 199 8 stone")
        command(rcon, f"player {BOT} spawn")
        wait_for_presence(body, present=True)
        command(rcon, f"tp {BOT} 0 200 0")
        command(rcon, f"gamemode survival {BOT}")
        command(rcon, f"clear {BOT}")
        command(rcon, f"item replace entity {BOT} hotbar.0 with oak_log 2")

        recipe = body.perceive("recipeData", {"item": "minecraft:oak_planks"})
        assert recipe.ok and recipe.complete, recipe
        assert any(
            variant.get("output_item") == "minecraft:oak_planks"
            and variant.get("output_count") == 4
            and "minecraft:oak_log" in (variant.get("ingredient_groups") or [[]])[0]
            for variant in recipe.data.get("variants") or []
        ), recipe

        before = slots(body)
        result = InventoryTransactions(body).craft_recipe(
            item="minecraft:oak_planks",
            count=8,
            output_slot=1,
            craft_timeout_s=8.0,
        )
        after = slots(body)
        assert result.success and result.reason == "completed", result
        assert count(before, "minecraft:oak_log") == 2
        assert count(before, "minecraft:oak_planks") == 0
        assert count(after, "minecraft:oak_log") == 0
        assert count(after, "minecraft:oak_planks") == 8
        craft_terminal = (
            (((result.metrics.get("craft") or {}).get("metrics") or {}).get("craft") or {})
        )
        assert craft_terminal.get("recipe_id") == "minecraft:oak_planks", craft_terminal

        smelting = body.perceive(
            "recipeData",
            {"item": "minecraft:iron_ingot", "type": "smelting"},
        )
        assert smelting.ok and any(
            "minecraft:raw_iron" in group
            for variant in smelting.data.get("variants") or []
            for group in variant.get("ingredient_groups") or []
            if group
        ), smelting

        command(rcon, f"clear {BOT}")
        command(rcon, f"item replace entity {BOT} hotbar.0 with cobblestone 1")
        forged_before = slots(body)
        forged = Action.create("craftItem", {
            "inputs": [{"slot": 0, "item": "minecraft:cobblestone", "count": 1}],
            "output": {"slot": 1, "item": "minecraft:diamond", "count": 64},
            "remainders": [],
            "max_stack": 64,
        })
        accepted = body.execute(forged)
        terminal = body.await_action_terminal(forged.id)
        forged_after = slots(body)
        assert accepted.ok and accepted.accepted
        assert terminal.data.get("success") is False, terminal
        assert terminal.data.get("stopped_reason") == "recipe_mismatch", terminal
        assert forged_before == forged_after

        artifact = {
            "scope": "java_body_recipe_craft",
            "formal_gate": False,
            "bounded": True,
            "environment": "dry_land",
            "provider": "java",
            "scarpet_body_constructed": False,
            "scarpet_action_calls": 0,
            "recipe": {
                "variant_count": recipe.data.get("variantCount"),
                "server_cost_micros": recipe.data.get("serverCostMicros"),
            },
            "craft": {
                "reason": result.reason,
                "oak_log_before": 2,
                "oak_log_after": 0,
                "oak_planks_before": 0,
                "oak_planks_after": 8,
                "recipe_id": craft_terminal.get("recipe_id"),
            },
            "smelting_recipe_read": {
                "variant_count": smelting.data.get("variantCount"),
                "raw_iron_supported": True,
            },
            "forged_output": {
                "reason": terminal.data.get("stopped_reason"),
                "inventory_unchanged": True,
            },
        }
        output = Path("logs/agentic-runtime/java-body-recipe-craft-20260727.json")
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
