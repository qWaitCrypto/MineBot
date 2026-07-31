#!/usr/bin/env python3
"""Measure Java-only exploration yielding at the first requested natural target."""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

from minebot.app.body_provider import build_body_provider
from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_agent_runtime
from minebot.contract import Region
from minebot.game.rcon import RconClient, RconConfig


BOT = "Bot1"
BODY_URL = "ws://127.0.0.1:8767"
START = (0.0, 70.0, 0.0)
NATURAL = Region("formal-world", (-256, -128, -256), (256, 320, 256))
ARTIFACT = Path("logs/agentic-runtime/java-body-explore-yield-20260730.json")


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
        goal_text="find logs, flowers, pigs, cows, or sheep and act on the first useful target",
        model_provider=None,
        config=Phase1RuntimeConfig(
            natural_region=NATURAL,
            body_provider="java",
            governance_policy=provider.governance,
        ),
        agent_name="JavaExploreYieldProbe",
    )

    try:
        with RconClient(
            RconConfig(host="127.0.0.1", port=25576, password="test", timeout_s=20)
        ) as rcon:
            if body.get_state().missing:
                spawned = body.spawn(START, gamemode="survival", timeout_s=10.0)
                if not spawned.ok:
                    raise AssertionError(f"spawn failed: {spawned}")
            rcon.command(f"clear {BOT}")
            rcon.command(f"effect clear {BOT}")
            rcon.command(f"gamemode survival {BOT}")
            rcon.command(f"tp {BOT} {START[0]} {START[1]} {START[2]}")
            time.sleep(0.5)
            before = body.get_state()

            started = time.monotonic()
            result = parts.registry.get("explore_for").callable(
                {
                    "block_targets": ["#logs", "#flowers"],
                    "entity_targets": ["pig", "cow", "sheep"],
                    "max_distance": 256,
                    "max_regions": 12,
                    "return_policy": "first_match",
                    "scan_radius": 24,
                }
            )
            elapsed = time.monotonic() - started
            after = body.get_state()
    finally:
        parts.runtime.close()

    metrics = dict(result.metrics or {})
    budget = dict(metrics.get("budget") or {})
    blocks = list(metrics.get("blocks") or [])
    entities = list(metrics.get("entities") or [])
    regions_consumed = int(budget.get("regions_consumed") or 0)
    success = bool(
        result.success
        and result.reason == "found"
        and (blocks or entities)
        and metrics.get("resume_cursor")
        and regions_consumed < 12
    )
    artifact = {
        "scope": "java_body_explore_yield",
        "formal_gate": False,
        "bounded": True,
        "production_provider": "java",
        "scarpet_constructed": provider.scarpet_body is not None,
        "elapsed_wall_s": round(elapsed, 3),
        "start_pos": list(before.pos),
        "end_pos": list(after.pos),
        "player_distance": round(math.dist(before.pos, after.pos), 3),
        "regions_consumed": regions_consumed,
        "max_regions": 12,
        "block_match_count": len(blocks),
        "entity_match_count": len(entities),
        "first_block": blocks[0] if blocks else None,
        "first_entity": entities[0] if entities else None,
        "resume_cursor": metrics.get("resume_cursor"),
        "result_reason": result.reason,
        "success": success,
    }
    ARTIFACT.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    print(json.dumps(artifact, indent=2))
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
