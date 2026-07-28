#!/usr/bin/env python3
"""Bounded Java-only proof for canonical entity and untargeted item use."""

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
from minebot.contract import InventorySlot, Region
from minebot.game.rcon import RconClient, RconConfig


BOT = "JavaUseForms"
BODY_URL = "ws://127.0.0.1:8767"
REGION = Region("java-use-forms", (-8, 190, -8), (28, 220, 8))
COW = (10, 200, 0)


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

    def record_execute(action):
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
        command(rcon, "kill @e[type=minecraft:cow]")
        command(rcon, "fill -4 200 -8 24 210 8 air")
        command(rcon, "fill -4 199 -8 24 199 8 stone")
        command(rcon, f"summon minecraft:cow {COW[0]} {COW[1]} {COW[2]} {{NoAI:1b}}")
        command(rcon, f"player {BOT} spawn")
        wait_for_presence(body, present=True)
        command(rcon, f"gamemode survival {BOT}")
        command(rcon, f"clear {BOT}")
        teleport(rcon, body, (0, 200, 0))

        command(rcon, f"item replace entity {BOT} hotbar.0 with bucket 1")
        milk_before = inventory_count(body, "minecraft:milk_bucket")
        entity_start = body.get_state().pos
        entity_result = registry.get("use_on_entity").callable(
            {
                "item": "minecraft:bucket",
                "entity_types": ["minecraft:cow"],
                "search_radius": 24,
                "min_distance": 0.0,
                "max_distance": 4.5,
                "vertical_tolerance": 1.5,
                "watched_items": ["minecraft:milk_bucket"],
                "required_watched_item_deltas": {"minecraft:milk_bucket": 1},
                "timeout_s": 12.0,
            }
        )
        entity_end = body.get_state().pos
        milk_after = inventory_count(body, "minecraft:milk_bucket")
        assert entity_result.success and entity_result.reason == "completed", entity_result
        assert milk_after - milk_before == 1, (milk_before, milk_after, entity_result)
        assert (entity_result.metrics or {}).get("target", {}).get("type") == "cow", entity_result
        assert dist(entity_start, entity_end) > 4.0, (entity_start, entity_end, entity_result)

        command(rcon, f"clear {BOT}")
        command(rcon, f"item replace entity {BOT} hotbar.0 with ender_pearl 2")
        teleport(rcon, body, (0, 200, 0))
        item_start = body.get_state().pos
        item_result = registry.get("use_item").callable(
            {
                "item": "minecraft:ender_pearl",
                "look_target": [20.0, 202.0, 0.0],
                "use_mode": "once",
                "use_ticks": 1,
                "min_position_delta": 5.0,
                "timeout_s": 12.0,
            }
        )
        item_end = body.get_state().pos
        pearl_after = inventory_count(body, "minecraft:ender_pearl")
        assert item_result.success and item_result.reason == "completed", item_result
        assert dist(item_start, item_end) >= 5.0, (item_start, item_end, item_result)
        assert pearl_after == 1, (pearl_after, item_result)

        assert "navigate" in action_names, action_names
        assert "selectItem" in action_names, action_names
        assert "lookAt" in action_names, action_names
        assert "useItem" in action_names, action_names
        assert "navigateTo" not in action_names, action_names
        artifact = {
            "scope": "java_body_use_forms",
            "formal_gate": False,
            "bounded": True,
            "environment": "dry_land",
            "provider": "java",
            "scarpet_body_constructed": False,
            "scarpet_action_calls": 0,
            "provider_actions": action_names,
            "entity_use": {
                "reason": entity_result.reason,
                "target": (entity_result.metrics or {}).get("target"),
                "start": list(entity_start),
                "end": list(entity_end),
                "milk_before": milk_before,
                "milk_after": milk_after,
            },
            "untargeted_use": {
                "reason": item_result.reason,
                "start": list(item_start),
                "end": list(item_end),
                "moved_distance": dist(item_start, item_end),
                "ender_pearl_after": pearl_after,
            },
        }
        output = Path("logs/agentic-runtime/java-body-use-forms-20260728.json")
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
