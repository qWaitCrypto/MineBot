#!/usr/bin/env python3
"""Bounded dry-land proof for Java container reads and governed transfers. Not a gate."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.app.body_provider import build_body_provider
from minebot.body import ContainerTransactions
from minebot.body.inventory_read import read_inventory_slots
from minebot.contract import Action, InventorySlot, Region, perception_next_cursor
from minebot.game.rcon import RconClient, RconConfig


BOT = "JavaContProbe"
BODY_URL = "ws://127.0.0.1:8767"
CHEST = (2, 200, 0)
DOUBLE_CHEST = (5, 200, 0)
DENIED_CHEST = (22, 200, 0)
REGION = Region("java-container-probe", (-4, 190, -4), (12, 212, 4))


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


def container(body, pos: tuple[int, int, int], page_size: int = 12) -> tuple[dict[int, InventorySlot], int]:
    start: int | None = 0
    slots: dict[int, InventorySlot] = {}
    total_slots = 0
    deadline = time.monotonic() + 3.0
    while start is not None:
        result = body.perceive(
            "container",
            {"pos": list(pos), "start": start, "limit": page_size},
        )
        if result.error == "rate_limited" and time.monotonic() < deadline:
            time.sleep(0.1)
            continue
        assert result.ok, result
        total_slots = int(result.data.get("totalSlots") or 0)
        for payload in result.data.get("slots") or []:
            slot = InventorySlot.from_payload(payload)
            slots[slot.slot] = slot
        cursor = perception_next_cursor(result)
        start = None if cursor is None else int(cursor)
    return slots, total_slots


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
        command(rcon, f"setblock {CHEST[0]} {CHEST[1]} {CHEST[2]} chest")
        command(rcon, f"item replace block {CHEST[0]} {CHEST[1]} {CHEST[2]} container.0 with diamond 3")
        command(rcon, f"item replace block {CHEST[0]} {CHEST[1]} {CHEST[2]} container.1 with cobblestone 8")
        command(rcon, f"setblock {DOUBLE_CHEST[0]} {DOUBLE_CHEST[1]} {DOUBLE_CHEST[2]} chest[facing=north,type=left]")
        command(rcon, f"setblock {DOUBLE_CHEST[0] + 1} {DOUBLE_CHEST[1]} {DOUBLE_CHEST[2]} chest[facing=north,type=right]")
        command(rcon, f"item replace block {DOUBLE_CHEST[0]} {DOUBLE_CHEST[1]} {DOUBLE_CHEST[2]} container.0 with gold_ingot 2")
        command(rcon, f"setblock {DENIED_CHEST[0]} {DENIED_CHEST[1]} {DENIED_CHEST[2]} chest")
        command(rcon, f"item replace block {DENIED_CHEST[0]} {DENIED_CHEST[1]} {DENIED_CHEST[2]} container.0 with emerald 2")
        command(rcon, f"player {BOT} spawn")
        wait_for_presence(body, present=True)
        command(rcon, f"tp {BOT} 0 200 0")
        command(rcon, f"gamemode survival {BOT}")
        command(rcon, f"clear {BOT}")

        viewed, viewed_total = container(body, CHEST)
        assert viewed_total == 27 and len(viewed) == 27, (viewed_total, viewed)
        assert viewed[0].item == "minecraft:diamond" and viewed[0].count == 3
        assert viewed[1].item == "minecraft:cobblestone" and viewed[1].count == 8

        runtime = ContainerTransactions(body, governance=provider.governance)
        take = runtime.transfer_item(
            CHEST,
            item="minecraft:diamond",
            count=2,
            direction="container_to_bot",
            timeout_s=8.0,
        )
        after_take_inventory = inventory(body)
        after_take_chest, _ = container(body, CHEST)
        assert take.success and take.reason == "completed", take
        assert item_count(after_take_inventory, "minecraft:diamond") == 2
        assert after_take_chest[0].count == 1

        put = runtime.transfer_item(
            CHEST,
            item="minecraft:diamond",
            count=1,
            direction="bot_to_container",
            timeout_s=8.0,
        )
        after_put_inventory = inventory(body)
        after_put_chest, _ = container(body, CHEST)
        assert put.success and put.reason == "completed", put
        assert item_count(after_put_inventory, "minecraft:diamond") == 1
        assert after_put_chest[0].count == 2

        double_slots, double_total = container(body, DOUBLE_CHEST, page_size=13)
        assert double_total == 54 and len(double_slots) == 54, (double_total, len(double_slots))
        assert item_count(double_slots, "minecraft:gold_ingot") == 2

        command(rcon, f"item replace entity {BOT} hotbar.0 with cobblestone 1")
        occupied_before_inventory = inventory(body)
        occupied_before_chest, _ = container(body, CHEST)
        occupied = Action.create("containerTransfer", {
            "pos": list(CHEST),
            "direction": "container_to_bot",
            "container_slot": 0,
            "bot_slot": 0,
            "count": 1,
        })
        occupied_accepted = body.execute(occupied)
        occupied_terminal = body.await_action_terminal(occupied.id)
        occupied_after_inventory = inventory(body)
        occupied_after_chest, _ = container(body, CHEST)
        assert occupied_accepted.ok and occupied_accepted.accepted
        assert occupied_terminal.data.get("success") is False, occupied_terminal
        assert occupied_terminal.data.get("stopped_reason") == "destination_occupied", occupied_terminal
        assert occupied_before_inventory[0] == occupied_after_inventory[0]
        assert occupied_before_chest[0] == occupied_after_chest[0]

        command(rcon, f"tp {BOT} 20 200 0")
        denied_before_inventory = inventory(body)
        denied_before_chest, _ = container(body, DENIED_CHEST)
        denied_runtime = ContainerTransactions(body, governance=None)
        denied = denied_runtime.transfer_item(
            DENIED_CHEST,
            item="minecraft:emerald",
            count=1,
            direction="container_to_bot",
            timeout_s=8.0,
        )
        denied_after_inventory = inventory(body)
        denied_after_chest, _ = container(body, DENIED_CHEST)
        assert not denied.success
        assert denied.reason == "container_transfer_failed:governance_denied:unknown_provenance", denied
        assert item_count(denied_before_inventory, "minecraft:emerald") == item_count(denied_after_inventory, "minecraft:emerald") == 0
        assert denied_before_chest[0] == denied_after_chest[0]

        artifact = {
            "scope": "java_body_container",
            "formal_gate": False,
            "bounded": True,
            "environment": "dry_land",
            "provider": "java",
            "scarpet_body_constructed": False,
            "scarpet_action_calls": 0,
            "view": {"total_slots": viewed_total, "diamond": viewed[0].count, "cobblestone": viewed[1].count},
            "take": {"reason": take.reason, "inventory_diamond": 2, "chest_diamond": 1},
            "put": {"reason": put.reason, "inventory_diamond": 1, "chest_diamond": 2},
            "double_chest": {"total_slots": double_total, "gold_ingots": 2},
            "occupied_destination": {"reason": occupied_terminal.data.get("stopped_reason"), "unchanged": True},
            "governance_denial": {"reason": denied.reason, "unchanged": True},
        }
        out = Path("logs/agentic-runtime/java-body-container-20260727.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
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
