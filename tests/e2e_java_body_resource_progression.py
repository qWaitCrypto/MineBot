#!/usr/bin/env python3
"""Bounded no-model rehearsal for wood-to-stone-to-iron progression."""

from __future__ import annotations

import json
import time
from pathlib import Path

from minebot.app.body_provider import build_body_provider
from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_agent_runtime
from minebot.contract import Region
from minebot.game.rcon import RconClient, RconConfig


BOT = "JavaProgProbe"
BODY_URL = "ws://127.0.0.1:8767"
X = 192
Y = 180
Z = 160
START = (X + 0.5, float(Y), Z + 0.5)
NATURAL = Region("formal-world", (-256, -128, -256), (256, 320, 256))
ARTIFACT = Path("logs/agentic-runtime/java-body-resource-progression-20260730.json")


def wait_for_player(rcon: RconClient) -> None:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if "No entity was found" not in rcon.command(f"data get entity {BOT} Pos"):
            return
        time.sleep(0.25)
    raise RuntimeError(f"{BOT} did not join the test world")


def prepare_fixture(rcon: RconClient) -> None:
    rcon.command(f"player {BOT} spawn")
    wait_for_player(rcon)
    rcon.command(f"gamemode spectator {BOT}")
    rcon.command(f"tp {BOT} {X + 0.5} {Y + 20} {Z + 0.5}")
    time.sleep(0.5)
    rcon.command(f"fill {X - 9} {Y} {Z - 6} {X + 9} {Y + 8} {Z + 6} air")
    rcon.command(f"fill {X - 9} {Y - 1} {Z - 6} {X + 9} {Y - 1} {Z + 6} grass_block")
    tree_x = X + 5
    for y in range(Y, Y + 5):
        rcon.command(f"setblock {tree_x} {y} {Z} spruce_log")
    for x in range(tree_x - 2, tree_x + 3):
        for y in range(Y + 3, Y + 6):
            for z in range(Z - 2, Z + 3):
                if x == tree_x and z == Z and y <= Y + 4:
                    continue
                if abs(x - tree_x) + abs(z - Z) <= 3:
                    rcon.command(f"setblock {x} {y} {z} spruce_leaves")
    for stone_x, stone_z in (
        (X - 7, Z - 3),
        (X - 7, Z - 1),
        (X - 7, Z + 1),
        (X - 7, Z + 3),
        (X - 5, Z - 3),
        (X - 5, Z - 1),
        (X - 5, Z + 1),
        (X - 5, Z + 3),
        (X - 3, Z - 3),
        (X - 3, Z - 1),
        (X - 3, Z + 1),
        (X - 3, Z + 3),
    ):
        rcon.command(f"setblock {stone_x} {Y} {stone_z} stone")
    for iron_x, iron_z in (
        (X + 2, Z - 4),
        (X + 4, Z - 4),
        (X + 6, Z - 4),
    ):
        rcon.command(f"setblock {iron_x} {Y} {iron_z} iron_ore")
    rcon.command(f"clear {BOT}")
    rcon.command(f"effect clear {BOT}")
    rcon.command(f"gamemode survival {BOT}")
    rcon.command(f"tp {BOT} {START[0]} {START[1]} {START[2]}")
    time.sleep(0.5)


def cleanup_fixture(rcon: RconClient) -> None:
    rcon.command(f"player {BOT} kill")
    rcon.command(f"fill {X - 9} {Y - 1} {Z - 6} {X + 9} {Y + 8} {Z + 6} air")


def main() -> int:
    provider = build_body_provider(
        "java",
        bot_name=BOT,
        natural_region=NATURAL,
        java_body_url=BODY_URL,
    )
    body = provider.body
    parts = build_phase1_agent_runtime(
        body=body,
        goal_text="acquire and equip an iron pickaxe",
        model_provider=None,
        config=Phase1RuntimeConfig(
            natural_region=NATURAL,
            body_provider="java",
            governance_policy=provider.governance,
        ),
        agent_name="JavaResourceProgressionProbe",
    )

    with RconClient(
        RconConfig(host="127.0.0.1", port=25576, password="test", timeout_s=20)
    ) as rcon:
        prepare_fixture(rcon)
        try:
            start_seq = int(body.event_head("java-resource-progression")["event_seq"])
            body.last_seq = start_seq
            started = time.monotonic()
            stone_started = time.monotonic()
            stone_result = parts.registry.get("ensure_tool_for").callable(
                {"resource": "stone_pickaxe"}
            )
            stone_elapsed = time.monotonic() - stone_started
            stone_state = body.get_state()
            iron_started = time.monotonic()
            result = parts.registry.get("ensure_tool_for").callable(
                {"resource": "iron_pickaxe"}
            )
            iron_elapsed = time.monotonic() - iron_started
            elapsed = time.monotonic() - started
            body.poll_events()
            after = body.get_state()
        finally:
            try:
                body.interrupt("bounded_probe_cleanup")
            finally:
                cleanup_fixture(rcon)

    stone_completed_steps = list((stone_result.metrics or {}).get("completed_steps") or [])
    iron_completed_steps = list((result.metrics or {}).get("completed_steps") or [])
    completed_steps = [*stone_completed_steps, *iron_completed_steps]
    steps = [
        {
            "kind": (entry.get("step") or {}).get("kind"),
            "item": (entry.get("step") or {}).get("item"),
            "reason": (entry.get("result") or {}).get("reason"),
        }
        for entry in completed_steps
    ]
    inventory = after.inventory_counts or {}
    planned_items = [str((entry.get("step") or {}).get("item") or "") for entry in completed_steps]
    designated_targets = [
        dict(event.data)
        for event in body.event_log
        if event.seq > start_seq and event.name == "target_committed"
    ]
    forbidden_strategy_events = [
        event.name
        for event in body.event_log
        if event.seq > start_seq
        and (
            event.name.startswith("collection_candidate_")
            or event.name.startswith("collection_frontier_")
            or event.name == "collection_objective_domain_planned"
        )
    ]
    stone_pickaxe_count = sum(
        int(count)
        for item, count in inventory.items()
        if str(item).removeprefix("minecraft:") == "stone_pickaxe"
    )
    iron_pickaxe_count = sum(
        int(count)
        for item, count in inventory.items()
        if str(item).removeprefix("minecraft:") == "iron_pickaxe"
    )
    first_output_s = stone_elapsed if stone_result.success else None
    max_no_output_window_s = max(stone_elapsed, iron_elapsed)
    success = bool(
        stone_result.success
        and result.success
        and stone_pickaxe_count >= 1
        and iron_pickaxe_count >= 1
        and str(after.selected_item or "").removeprefix("minecraft:") == "iron_pickaxe"
        and steps
        and steps[0]["kind"] == "collect"
        and steps[0]["item"] == "logs"
        and "spruce_planks" in planned_items
        and "oak_planks" not in planned_items
        and designated_targets
        and not forbidden_strategy_events
        and steps[-1]["kind"] == "equip"
        and steps[-1]["item"] == "iron_pickaxe"
        and first_output_s is not None
        and first_output_s < 342.0
        and max_no_output_window_s < 900.0
    )
    artifact = {
        "scope": "java_body_resource_progression",
        "formal_gate": False,
        "bounded": True,
        "production_provider": "java",
        "scarpet_constructed": provider.scarpet_body is not None,
        "rcon_role": "fixture_setup_and_verification_only",
        "model_calls": 0,
        "high_level_calls": 2,
        "elapsed_wall_s": round(elapsed, 3),
        "first_output_s": None if first_output_s is None else round(first_output_s, 3),
        "max_no_output_window_s": round(max_no_output_window_s, 3),
        "milestones": [
            {
                "name": "stone_pickaxe_equipped",
                "elapsed_wall_s": round(stone_elapsed, 3),
                "result_reason": stone_result.reason,
                "success": stone_result.success,
                "inventory": stone_state.inventory_counts or {},
                "selected_item": stone_state.selected_item,
            },
            {
                "name": "iron_pickaxe_equipped",
                "elapsed_wall_s": round(iron_elapsed, 3),
                "result_reason": result.reason,
                "success": result.success,
                "inventory": inventory,
                "selected_item": after.selected_item,
            },
        ],
        "steps": steps,
        "designated_targets": designated_targets,
        "forbidden_java_strategy_events": forbidden_strategy_events,
        "inventory": inventory,
        "selected_item": after.selected_item,
        "result_reason": result.reason,
        "success": success,
    }
    ARTIFACT.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    print(json.dumps(artifact, indent=2))
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
