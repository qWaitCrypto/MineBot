#!/usr/bin/env python3
"""Bounded Java-only proof for production interaction approach routing. Not a gate."""

from __future__ import annotations

import json
import sys
import time
from math import dist
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.app.body_provider import build_body_provider
from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_registry
from minebot.body.inventory_read import read_inventory_slots
from minebot.contract import Action, InventorySlot, Region
from minebot.game.rcon import RconClient, RconConfig


BOT = "JavaApproach"
BODY_URL = "ws://127.0.0.1:8767"
CHEST = (12, 200, 0)
FURNACE = (12, 200, 8)
LEVER = (12, 200, -8)
REGION = Region("java-approach-probe", (-4, 190, -16), (20, 212, 16))


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


def teleport(rcon: RconClient, body, pos: tuple[int, int, int]) -> tuple[float, float, float]:
    command(rcon, f"tp {BOT} {pos[0]} {pos[1]} {pos[2]}")
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        observed = body.get_state().pos
        if dist(observed, pos) < 1.0:
            return observed
        time.sleep(0.1)
    raise AssertionError(f"teleport did not settle at {pos}: {body.get_state().pos}")


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


def movement_facts(start, end, target) -> dict[str, object]:
    return {
        "start": list(start),
        "end": list(end),
        "target": list(target),
        "moved_distance": dist(start, end),
        "final_target_distance": dist(end, target),
    }


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
    action_names: list[str] = []
    original_execute = body.execute

    def record_execute(action: Action):
        action_names.append(action.name)
        return original_execute(action)

    body.execute = record_execute  # type: ignore[method-assign]
    registry = build_phase1_registry(
        body,
        Phase1RuntimeConfig(
            natural_region=REGION,
            body_provider="java",
            governance_policy=provider.governance,
        ),
    )
    try:
        command(rcon, "carpet commandPlayer true")
        command(rcon, "carpet allowSpawningOfflinePlayers true")
        command(rcon, f"player {BOT} kill")
        command(rcon, "fill -4 200 -16 20 204 16 air")
        command(rcon, "fill -4 199 -16 20 199 16 stone")
        command(rcon, f"setblock {CHEST[0]} {CHEST[1]} {CHEST[2]} chest")
        command(
            rcon,
            f"item replace block {CHEST[0]} {CHEST[1]} {CHEST[2]} container.0 with diamond 1",
        )
        command(rcon, f"setblock {FURNACE[0]} {FURNACE[1]} {FURNACE[2]} furnace")
        command(
            rcon,
            f"item replace block {FURNACE[0]} {FURNACE[1]} {FURNACE[2]} container.2 with iron_ingot 1",
        )
        command(
            rcon,
            f"setblock {LEVER[0]} {LEVER[1]} {LEVER[2]} lever[face=floor,facing=north,powered=false]",
        )
        command(rcon, f"player {BOT} spawn")
        wait_for_presence(body, present=True)
        command(rcon, f"gamemode survival {BOT}")
        command(rcon, f"clear {BOT}")

        chest_start = teleport(rcon, body, (0, 200, 0))
        chest_result = registry.get("transfer_container_item").callable(
            {
                "item": "minecraft:diamond",
                "count": 1,
                "direction": "container_to_bot",
                "search_radius": 16,
                "container_types": ["chest"],
                "timeout_s": 8.0,
                "approach_timeout_s": 30.0,
            }
        )
        chest_end = body.get_state().pos
        assert chest_result.success and chest_result.reason == "completed", chest_result
        assert inventory_count(body, "minecraft:diamond") == 1
        assert dist(chest_start, chest_end) > 5.0
        assert dist(chest_end, CHEST) <= 4.5

        furnace_start = teleport(rcon, body, (0, 200, 8))
        furnace_result = registry.get("clear_furnace").callable(
            {
                "search_radius": 16,
                "furnace_types": ["furnace"],
                "timeout_s": 8.0,
                "approach_timeout_s": 30.0,
            }
        )
        furnace_end = body.get_state().pos
        assert furnace_result.success and furnace_result.reason == "completed", furnace_result
        assert inventory_count(body, "minecraft:iron_ingot") == 1
        assert dist(furnace_start, furnace_end) > 5.0
        assert dist(furnace_end, FURNACE) <= 4.5

        lever_start = teleport(rcon, body, (0, 200, -8))
        lever_result = registry.get("set_switch_state").callable(
            {"powered": True, "pos": list(LEVER)}
        )
        lever_end = body.get_state().pos
        lever_after = body.perceive(
            "blockAt",
            {"x": LEVER[0], "y": LEVER[1], "z": LEVER[2]},
        )
        assert lever_result.success and lever_result.reason == "powered", lever_result
        assert lever_after.data.get("properties", {}).get("powered") == "true", lever_after
        assert dist(lever_start, lever_end) > 5.0
        assert dist(lever_end, LEVER) <= 4.5

        navigate_count = action_names.count("navigate")
        assert navigate_count == 3, action_names
        assert "navigateTo" not in action_names, action_names
        artifact = {
            "scope": "java_body_interaction_approach",
            "formal_gate": False,
            "bounded": True,
            "environment": "dry_land",
            "provider": "java",
            "scarpet_body_constructed": False,
            "scarpet_action_calls": 0,
            "provider_actions": action_names,
            "navigate_count": navigate_count,
            "chest": {
                **movement_facts(chest_start, chest_end, CHEST),
                "reason": chest_result.reason,
                "diamond_after": 1,
            },
            "furnace": {
                **movement_facts(furnace_start, furnace_end, FURNACE),
                "reason": furnace_result.reason,
                "iron_ingot_after": 1,
            },
            "lever": {
                **movement_facts(lever_start, lever_end, LEVER),
                "reason": lever_result.reason,
                "powered_after": lever_after.data.get("properties", {}).get("powered"),
            },
        }
        output = Path("logs/agentic-runtime/java-body-interaction-approach-20260727.json")
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
