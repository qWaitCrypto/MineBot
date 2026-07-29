#!/usr/bin/env python3
"""Replay the formal run's blocked multi-target exploration through Java only."""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

from minebot.app.body_provider import build_body_provider
from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_agent_runtime
from minebot.contract import Region


BOT = "Bot1"
BODY_URL = "ws://127.0.0.1:8767"
START = (0, 70, 0)
NATURAL = Region("formal-world", (-256, -128, -256), (256, 320, 256))
ARTIFACT = Path("logs/agentic-runtime/java-body-natural-explore-replay-20260729.json")


def main() -> int:
    provider = build_body_provider(
        "java",
        bot_name=BOT,
        natural_region=NATURAL,
        java_body_url=BODY_URL,
    )
    parts = build_phase1_agent_runtime(
        body=provider.body,
        goal_text="find logs, flowers, pigs, cows, or sheep",
        model_provider=None,
        config=Phase1RuntimeConfig(
            natural_region=NATURAL,
            body_provider="java",
            governance_policy=provider.governance,
        ),
        agent_name="JavaNaturalExploreReplay",
    )
    try:
        before = provider.body.get_state()
        if before.missing:
            spawned = provider.body.spawn(START, gamemode="survival", timeout_s=10.0)
            if not spawned.ok:
                raise AssertionError(f"Java Body could not spawn replay player: {spawned}")
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                before = provider.body.get_state()
                if not before.missing:
                    break
                time.sleep(0.05)
        if before.missing or before.body_owner is not None or before.pending_action_count:
            raise AssertionError(f"exploration replay requires an idle live player: {before}")

        started = time.monotonic()
        result = parts.registry.get("explore_for").callable(
            {
                "block_targets": ["#logs", "#flowers"],
                "entity_targets": ["pig", "cow", "sheep"],
                "max_distance": 256,
                "max_regions": 12,
                "return_policy": "region_budget",
                "scan_radius": 24,
            }
        )
        elapsed = time.monotonic() - started
        after = provider.body.get_state()
    finally:
        parts.runtime.close()

    metrics = dict(result.metrics or {})
    player_distance = math.dist(before.pos, after.pos)
    regions_consumed = int((metrics.get("budget") or {}).get("regions_consumed") or 0)
    found_count = len(metrics.get("blocks") or []) + len(metrics.get("entities") or [])
    expanded_world = player_distance >= 1.0 or regions_consumed >= 2 or found_count > 0
    success = bool(result.reason != "perception_incomplete" and expanded_world)
    artifact = {
        "scope": "java_body_natural_explore_failure_replay",
        "formal_gate": False,
        "bounded": True,
        "production_provider": "java",
        "scarpet_constructed": provider.scarpet_body is not None,
        "start_pos": list(before.pos),
        "end_pos": list(after.pos),
        "player_distance": round(player_distance, 3),
        "elapsed_wall_s": round(elapsed, 3),
        "regions_consumed": regions_consumed,
        "found_count": found_count,
        "result": result.to_payload(),
        "success": success,
    }
    ARTIFACT.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    print(json.dumps(artifact, indent=2))
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
