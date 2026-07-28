#!/usr/bin/env python3
"""Replay the formal run's first wood attempt through the Java-only registry."""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

from minebot.app.body_provider import build_body_provider
from minebot.app.phase1_runtime import (
    Phase1RuntimeConfig,
    build_phase1_agent_runtime,
)
from minebot.contract import Region
from minebot.game.rcon import RconClient, RconConfig


BOT = "Bot1"
BODY_URL = "ws://127.0.0.1:8767"
START = (61.3309294104469, 70.0, -39.349480975786186)
NATURAL = Region("formal-world", (-256, -128, -256), (256, 320, 256))
ARTIFACT = Path("logs/agentic-runtime/java-body-natural-tree-replay-20260728.json")


def wait_for_position(body, expected: tuple[float, float, float], timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if math.dist(body.get_state().pos, expected) <= 0.25:
            return
        time.sleep(0.05)
    raise AssertionError(f"Bot1 did not settle at replay start: {body.get_state().pos}")


def event_payload(event) -> dict[str, object]:
    return {
        "seq": event.seq,
        "tick": event.tick,
        "name": event.name,
        "data": dict(event.data),
    }


def wood_counts(counts: dict[str, int] | None) -> dict[str, int]:
    return {
        item: count
        for item, count in (counts or {}).items()
        if item.endswith("_log") or item.endswith("_stem")
    }


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
        goal_text="collect 1 logs",
        model_provider=None,
        config=Phase1RuntimeConfig(
            natural_region=NATURAL,
            body_provider="java",
            governance_policy=provider.governance,
        ),
        agent_name="JavaNaturalCollectReplay",
    )

    with RconClient(
        RconConfig(host="127.0.0.1", port=25576, password="test", timeout_s=20)
    ) as rcon:
        before_setup = body.get_state()
        if before_setup.missing:
            spawned = body.spawn(START, gamemode="survival", timeout_s=10.0)
            if not spawned.ok:
                raise AssertionError(f"Java Body could not spawn replay player: {spawned}")
            before_setup = body.get_state()
        if before_setup.body_owner is not None or before_setup.pending_action_count != 0:
            raise AssertionError(f"replay started with a busy body: {before_setup}")
        rcon.command(f"clear {BOT}")
        rcon.command(f"effect clear {BOT}")
        rcon.command(f"gamemode survival {BOT}")
        rcon.command(f"tp {BOT} {START[0]} {START[1]} {START[2]}")
        wait_for_position(body, START, 5.0)

        head = body.event_head("java-natural-collect-replay")
        start_seq = int(head["event_seq"])
        body.last_seq = start_seq
        before = body.get_state()
        started = time.monotonic()
        result = parts.registry.get("collect_resource").callable(
            {
                "item": "logs",
                "count": 1,
                "constraints": {
                    "radius": 64,
                    "max_candidates": 8,
                    "max_mutating_calls": 8,
                    "max_wall_s": 120,
                },
            }
        )
        elapsed = time.monotonic() - started
        body.poll_events()
        after = body.get_state()

    before_wood = sum(wood_counts(before.inventory_counts).values())
    after_wood = sum(wood_counts(after.inventory_counts).values())
    events = [event for event in body.event_log if event.seq > start_seq]
    artifact = {
        "scope": "java_body_natural_tree_replay",
        "formal_gate": False,
        "bounded": True,
        "production_provider": "java",
        "scarpet_constructed": provider.scarpet_body is not None,
        "start_pos": list(before.pos),
        "end_pos": list(after.pos),
        "before_inventory": before.inventory_counts or {},
        "after_inventory": after.inventory_counts or {},
        "elapsed_wall_s": round(elapsed, 3),
        "result": result.to_payload(),
        "path_events": [event_payload(event) for event in events],
        "wood_delta": after_wood - before_wood,
        "success": bool(result.success and after_wood > before_wood),
    }
    ARTIFACT.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    print(json.dumps(artifact, indent=2))
    return 0 if artifact["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
