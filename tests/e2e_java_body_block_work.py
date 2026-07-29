#!/usr/bin/env python3
"""Bounded dry-land proof for governed Java mine/place/jump primitives."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.app.body_provider import build_body_provider
from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_registry
from minebot.body import BlockWork
from minebot.body.inventory_read import read_inventory_slots
from minebot.contract import Action, BreakContext, InventorySlot, PlaceContext, Region
from minebot.game.rcon import RconClient, RconConfig


BOT = "JavaBlockProbe"
BODY_URL = "ws://127.0.0.1:8767"
MINE_TARGET = (1, 200, 0)
DENIED_MINE = (12, 200, 0)
DENIED_PLACE = (13, 200, 0)
NATURAL_REGION = Region("java-block-work", (-4, 190, -4), (8, 212, 4))
PROTECTED_REGION = Region("java-block-work-protected", (10, 190, -4), (16, 212, 4))


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


def inventory_count(body, item: str) -> int:
    deadline = time.monotonic() + 3.0
    while True:
        result = read_inventory_slots(body, page_size=46)
        if result.error != "rate_limited" or time.monotonic() >= deadline:
            break
        time.sleep(0.1)
    assert result.ok and result.complete, result
    return sum(
        slot.count
        for slot in (
            InventorySlot.from_payload(payload)
            for payload in result.data.get("slots") or []
        )
        if slot.item == item
    )


def item_stack_raw(body, item: str) -> str:
    deadline = time.monotonic() + 3.0
    while True:
        result = read_inventory_slots(body, page_size=46)
        if result.error != "rate_limited" or time.monotonic() >= deadline:
            break
        time.sleep(0.1)
    assert result.ok and result.complete, result
    matches = [
        slot
        for slot in (
            InventorySlot.from_payload(payload)
            for payload in result.data.get("slots") or []
        )
        if slot.item == item
    ]
    assert len(matches) == 1, matches
    return str(matches[0].stack_raw or "")


def read_block(body, pos: tuple[int, int, int]) -> str:
    fact = body.perceive("blockAt", {"x": pos[0], "y": pos[1], "z": pos[2]})
    assert fact.ok and fact.complete, fact
    return str(fact.data.get("type"))


def run_action(body, action: Action):
    accepted = body.execute(action)
    assert accepted.ok and accepted.accepted, accepted
    return body.await_action_terminal(action.id)


def main() -> int:
    rcon = RconClient(RconConfig(host="127.0.0.1", port=25576, password="test"))
    rcon.connect()
    provider = build_body_provider(
        "java",
        bot_name=BOT,
        natural_region=NATURAL_REGION,
        java_body_url=BODY_URL,
    )
    assert provider.scarpet_body is None and provider.java_body is not None
    provider.governance.protected_regions.append(PROTECTED_REGION)
    body = provider.body
    work = BlockWork(body, provider.governance)
    try:
        command(rcon, "carpet commandPlayer true")
        command(rcon, "carpet allowSpawningOfflinePlayers true")
        command(rcon, f"player {BOT} kill")
        command(rcon, "fill -4 200 -4 16 204 4 air")
        command(rcon, "fill -4 199 -4 16 199 4 stone")
        command(rcon, f"setblock {MINE_TARGET[0]} {MINE_TARGET[1]} {MINE_TARGET[2]} stone")
        command(rcon, f"setblock {DENIED_MINE[0]} {DENIED_MINE[1]} {DENIED_MINE[2]} stone")
        command(rcon, f"setblock {DENIED_PLACE[0]} {DENIED_PLACE[1]} {DENIED_PLACE[2]} air")
        command(rcon, f"player {BOT} spawn")
        wait_for_presence(body, present=True)
        command(rcon, f"tp {BOT} 0.5 200 0.5 0 0")
        command(rcon, f"gamemode survival {BOT}")
        command(rcon, f"clear {BOT}")
        command(rcon, f"item replace entity {BOT} hotbar.0 with iron_pickaxe 1")

        select_pickaxe = run_action(body, Action.create("selectItem", {"item": "minecraft:iron_pickaxe"}))
        assert select_pickaxe.data.get("success") is True, select_pickaxe
        cobble_before_mine = inventory_count(body, "minecraft:cobblestone")
        pickaxe_before = item_stack_raw(body, "minecraft:iron_pickaxe")
        mined = work.mine_block(
            MINE_TARGET,
            context=BreakContext.DIRECT,
            timeout_s=10.0,
            approach=False,
            explicit_target=True,
        )
        assert mined.success and mined.reason == "completed", mined
        mine_block_after = read_block(body, MINE_TARGET)
        assert mine_block_after == "minecraft:air"
        pickaxe_after = item_stack_raw(body, "minecraft:iron_pickaxe")
        cobble_after_mine = inventory_count(body, "minecraft:cobblestone")
        assert pickaxe_after != pickaxe_before
        assert "minecraft:damage" in pickaxe_after

        command(rcon, f"item replace entity {BOT} hotbar.1 with cobblestone 2")
        select_block = run_action(body, Action.create("selectItem", {"item": "minecraft:cobblestone"}))
        assert select_block.data.get("success") is True, select_block
        cobble_before_place = inventory_count(body, "minecraft:cobblestone")
        placed = work.place_block(
            MINE_TARGET,
            "minecraft:cobblestone",
            face="up",
            context=PlaceContext.WORK,
            purpose="scaffold",
            timeout_s=5.0,
        )
        cobble_after_place = inventory_count(body, "minecraft:cobblestone")
        assert placed.success and placed.reason == "completed", placed
        place_block_after = read_block(body, MINE_TARGET)
        assert place_block_after == "minecraft:cobblestone"
        assert cobble_after_place == cobble_before_place - 1

        jump = run_action(body, Action.create("jump", {}))
        assert jump.data.get("success") is True, jump
        assert float(jump.data.get("gained_y") or 0.0) >= 1.0, jump
        time.sleep(0.5)

        denied_mine_before = read_block(body, DENIED_MINE)
        denied_mine = run_action(body, Action.create("mineBlock", {
            "target": list(DENIED_MINE),
            "block_type": "minecraft:stone",
            "context": "direct",
            "timeout_ticks": 100,
        }))
        denied_mine_after = read_block(body, DENIED_MINE)
        assert denied_mine.data.get("success") is False, denied_mine
        assert str(denied_mine.data.get("stopped_reason", "")).startswith("governance_denied:"), denied_mine
        assert denied_mine_before == denied_mine_after == "minecraft:stone"

        denied_place_before = read_block(body, DENIED_PLACE)
        denied_inventory_before = inventory_count(body, "minecraft:cobblestone")
        denied_place = run_action(body, Action.create("placeBlock", {
            "target": list(DENIED_PLACE),
            "block_type": "minecraft:cobblestone",
            "face": "up",
            "context": "work",
            "timeout_ticks": 100,
        }))
        denied_place_after = read_block(body, DENIED_PLACE)
        denied_inventory_after = inventory_count(body, "minecraft:cobblestone")
        assert denied_place.data.get("success") is False, denied_place
        assert str(denied_place.data.get("stopped_reason", "")).startswith("governance_denied:"), denied_place
        assert denied_place_before == denied_place_after == "minecraft:air"
        assert denied_inventory_before == denied_inventory_after

        command(rcon, "setblock 0 200 -1 stone")
        command(rcon, "setblock -1 200 0 stone")
        command(rcon, "setblock 0 200 1 stone")
        command(rcon, "setblock 1 200 0 air")
        command(rcon, f"tp {BOT} 0.82 200 0.5 0 0")
        command(rcon, f"clear {BOT}")
        command(rcon, f"item replace entity {BOT} hotbar.0 with crafting_table 1")
        command(rcon, f"item replace entity {BOT} hotbar.1 with oak_planks 3")
        command(rcon, f"item replace entity {BOT} hotbar.2 with stick 2")
        registry = build_phase1_registry(
            body,
            Phase1RuntimeConfig(
                natural_region=NATURAL_REGION,
                body_provider="java",
                governance_policy=provider.governance,
            ),
        )
        place_here_tool = registry.get("place_here").callable
        craft_item_tool = registry.get("craft_item").callable
        selected_table = run_action(
            body,
            Action.create("selectItem", {"item": "minecraft:crafting_table"}),
        )
        assert selected_table.data.get("success") is True, selected_table
        placement_started = time.monotonic()
        table_place = place_here_tool(
            {
                "block_type": "minecraft:crafting_table",
                "radius": 1,
                "purpose": "workstation",
                "timeout_s": 8.0,
            }
        )
        placement_elapsed_s = time.monotonic() - placement_started
        place_here = (table_place.metrics or {}).get("place_here") or {}
        table_target = place_here.get("chosen_target")
        assert table_place.success and table_place.reason == "completed", table_place
        assert table_target == [1, 200, 0], table_place
        assert read_block(body, tuple(table_target)) == "minecraft:crafting_table"
        approach = (place_here.get("attempts") or [{}])[-1].get("approach") or {}
        assert approach.get("navigated") is True, table_place
        centered = body.get_state().pos
        assert abs(centered[0] - 0.5) <= BlockWork.PLACE_STAND_CENTER_RADIUS, centered
        assert abs(centered[2] - 0.5) <= BlockWork.PLACE_STAND_CENTER_RADIUS, centered

        crafted = craft_item_tool(
            {
                "item": "minecraft:wooden_pickaxe",
                "count": 1,
                "auto_equip": True,
            }
        )
        assert crafted.success and crafted.reason == "completed", crafted
        assert inventory_count(body, "minecraft:wooden_pickaxe") == 1
        assert body.get_state().selected_item == "minecraft:wooden_pickaxe"

        artifact = {
            "scope": "java_body_block_work",
            "formal_gate": False,
            "bounded": True,
            "environment": "dry_land",
            "provider": "java",
            "scarpet_body_constructed": False,
            "scarpet_action_calls": 0,
            "canonical_tools": ["place_here", "craft_item"],
            "mine": {
                "reason": mined.reason,
                "block_after": mine_block_after,
                "cobblestone_before": cobble_before_mine,
                "cobblestone_after": cobble_after_mine,
                "tool_durability_changed": True,
                "drop_pickup_owned_by_collect_block": True,
            },
            "place": {
                "reason": placed.reason,
                "block_after": place_block_after,
                "cobblestone_before": cobble_before_place,
                "cobblestone_after": cobble_after_place,
            },
            "jump": {
                "reason": jump.data.get("stopped_reason"),
                "position_before": jump.data.get("position_before"),
                "position_after": jump.data.get("position_after"),
                "gained_y": jump.data.get("gained_y"),
            },
            "governance_inverse": {
                "mine_reason": denied_mine.data.get("stopped_reason"),
                "mine_world_unchanged": True,
                "place_reason": denied_place.data.get("stopped_reason"),
                "place_world_unchanged": True,
                "place_inventory_unchanged": True,
            },
            "off_center_workstation_replay": {
                "start_pos": [0.82, 200.0, 0.5],
                "stand_center": [0.5, 200.0, 0.5],
                "table_target": table_target,
                "placement_reason": table_place.reason,
                "placement_elapsed_s": round(placement_elapsed_s, 3),
                "navigated_to_stable_stand": approach.get("navigated"),
                "final_pos": list(centered),
                "crafted": "minecraft:wooden_pickaxe",
                "equipped": body.get_state().selected_item,
            },
        }
        output = Path("logs/agentic-runtime/java-body-block-work-20260729.json")
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
