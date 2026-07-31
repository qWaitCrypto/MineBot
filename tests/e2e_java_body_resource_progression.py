#!/usr/bin/env python3
"""Measure Java-only progression from collected wood to an equipped stone pickaxe."""

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
        rcon.command(f"setblock {tree_x} {y} {Z} oak_log")
    for x in range(tree_x - 2, tree_x + 3):
        for y in range(Y + 3, Y + 6):
            for z in range(Z - 2, Z + 3):
                if x == tree_x and z == Z and y <= Y + 4:
                    continue
                if abs(x - tree_x) + abs(z - Z) <= 3:
                    rcon.command(f"setblock {x} {y} {z} oak_leaves")
    for stone_x, stone_z in (
        (X - 5, Z - 2),
        (X - 5, Z),
        (X - 5, Z + 2),
        (X - 3, Z - 1),
    ):
        rcon.command(f"setblock {stone_x} {Y} {stone_z} stone")
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
        goal_text="acquire and equip a stone pickaxe",
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
            started = time.monotonic()
            logs_result = parts.registry.get("collect_resource").callable(
                {
                    "item": "logs",
                    "count": 3,
                    "constraints": {
                        "radius": 64,
                        "max_candidates": 8,
                        "max_mutating_calls": 8,
                        "max_wall_s": 180,
                    },
                }
            )
            logs_elapsed = time.monotonic() - started
            ensure_started = time.monotonic()
            result = parts.registry.get("ensure_tool_for").callable({"resource": "stone_pickaxe"})
            ensure_elapsed = time.monotonic() - ensure_started
            elapsed = time.monotonic() - started
            after = body.get_state()
        finally:
            try:
                body.interrupt("bounded_probe_cleanup")
            finally:
                cleanup_fixture(rcon)

    completed_steps = list((result.metrics or {}).get("completed_steps") or [])
    steps = [
        {
            "kind": (entry.get("step") or {}).get("kind"),
            "item": (entry.get("step") or {}).get("item"),
            "reason": (entry.get("result") or {}).get("reason"),
        }
        for entry in completed_steps
    ]
    inventory = after.inventory_counts or {}
    stone_pickaxe_count = sum(
        int(count)
        for item, count in inventory.items()
        if str(item).removeprefix("minecraft:") == "stone_pickaxe"
    )
    success = bool(
        logs_result.success
        and result.success
        and stone_pickaxe_count >= 1
        and str(after.selected_item or "").removeprefix("minecraft:") == "stone_pickaxe"
        and steps
        and steps[-1]["kind"] == "equip"
        and elapsed < 300.0
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
        "collect_logs_wall_s": round(logs_elapsed, 3),
        "ensure_stone_pickaxe_wall_s": round(ensure_elapsed, 3),
        "collect_logs_reason": logs_result.reason,
        "steps": steps,
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
