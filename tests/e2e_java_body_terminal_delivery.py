#!/usr/bin/env python3
"""Prove repeated Java collection terminals track physical action time."""

from __future__ import annotations

import json
import time
from pathlib import Path

from minebot.app.body_provider import build_body_provider
from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_agent_runtime
from minebot.contract import Region
from minebot.game.rcon import RconClient, RconConfig


BOT = "JavaTermProbe"
BODY_URL = "ws://127.0.0.1:8767"
X = 160
Y = 180
Z = 160
START = (X + 0.5, float(Y), Z + 0.5)
NATURAL = Region("formal-world", (-256, -128, -256), (256, 320, 256))
ARTIFACT = Path("logs/agentic-runtime/java-body-terminal-delivery-20260730.json")


def log_count(counts: dict[str, int] | None) -> int:
    return sum(
        count
        for item, count in (counts or {}).items()
        if item.endswith("_log") or item.endswith("_stem")
    )


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
    rcon.command(f"fill {X - 5} {Y} {Z - 5} {X + 9} {Y + 8} {Z + 5} air")
    rcon.command(f"fill {X - 5} {Y - 1} {Z - 5} {X + 9} {Y - 1} {Z + 5} grass_block")
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
    rcon.command(f"clear {BOT}")
    rcon.command(f"effect clear {BOT}")
    rcon.command(f"gamemode survival {BOT}")
    rcon.command(f"tp {BOT} {START[0]} {START[1]} {START[2]}")
    time.sleep(0.5)


def cleanup_fixture(rcon: RconClient) -> None:
    rcon.command(f"player {BOT} kill")
    rcon.command(f"fill {X - 5} {Y - 1} {Z - 5} {X + 9} {Y + 8} {Z + 5} air")


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
        goal_text="collect 3 logs",
        model_provider=None,
        config=Phase1RuntimeConfig(
            natural_region=NATURAL,
            body_provider="java",
            governance_policy=provider.governance,
        ),
        agent_name="JavaTerminalDeliveryProbe",
    )

    with RconClient(
        RconConfig(host="127.0.0.1", port=25576, password="test", timeout_s=20)
    ) as rcon:
        prepare_fixture(rcon)
        try:
            before = body.get_state()
            started = time.monotonic()
            result = parts.registry.get("collect_resource").callable(
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
            elapsed = time.monotonic() - started
            after = body.get_state()
        finally:
            try:
                body.interrupt("bounded_probe_cleanup")
            finally:
                cleanup_fixture(rcon)

    attempts = list((result.metrics or {}).get("attempts") or [])
    deliveries = [
        dict((attempt.get("metrics") or {}).get("terminal_delivery") or {})
        for attempt in attempts
    ]
    delta = log_count(after.inventory_counts) - log_count(before.inventory_counts)
    artifact = {
        "scope": "java_body_terminal_delivery",
        "formal_gate": False,
        "bounded": True,
        "production_provider": "java",
        "scarpet_constructed": provider.scarpet_body is not None,
        "rcon_role": "fixture_setup_and_verification_only",
        "before_inventory": before.inventory_counts or {},
        "after_inventory": after.inventory_counts or {},
        "elapsed_wall_s": round(elapsed, 3),
        "log_delta": delta,
        "attempt_count": len(attempts),
        "terminal_deliveries": deliveries,
        "result": result.to_payload(),
        "success": bool(
            result.success
            and delta >= 3
            and len(deliveries) == 3
            and all(
                delivery.get("source") in {"event", "query", "reconnect_query"}
                and float(delivery.get("action_wall_ms") or 0.0) < 30_000.0
                for delivery in deliveries
            )
            and elapsed < 60.0
        ),
    }
    ARTIFACT.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    print(json.dumps(artifact, indent=2))
    return 0 if artifact["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
