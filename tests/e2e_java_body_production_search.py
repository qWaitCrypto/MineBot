#!/usr/bin/env python3
"""Bounded dry-land proof that canonical block search uses Java. Not a gate."""

from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.app.body_provider import build_body_provider
from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_registry
from minebot.contract import Region
from minebot.game import RconClient
from minebot.game.rcon import RconConfig


BOT = "JavaSearchProbe"
BODY_URL = "ws://127.0.0.1:8767"
BASE = (32, 200, 32)
TARGETS = ((38, 200, 32), (42, 200, 32))
REGION = Region("java-production-search", (16, 0, 16), (64, 320, 64))


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


def distance(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return math.dist(a, b)


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
    registry = build_phase1_registry(
        provider.body,
        Phase1RuntimeConfig(
            natural_region=REGION,
            body_provider="java",
            governance_policy=provider.governance,
        ),
    )
    search = registry.get("search_for_block")

    try:
        command(rcon, "carpet commandPlayer true")
        command(rcon, "carpet allowSpawningOfflinePlayers true")
        if not provider.body.get_state().missing:
            command(rcon, f"player {BOT} kill")
            wait_for_presence(provider.body, present=False)

        command(rcon, "fill 30 200 30 44 203 34 air")
        command(rcon, "fill 30 199 30 44 199 34 stone")
        for target in TARGETS:
            command(rcon, f"setblock {target[0]} {target[1]} {target[2]} oak_log")
        command(rcon, f"player {BOT} spawn")
        wait_for_presence(provider.body, present=True)
        command(rcon, f"execute in minecraft:overworld run tp {BOT} {BASE[0]} {BASE[1]} {BASE[2]}")

        before = provider.body.get_state()
        started = time.monotonic()
        found = search.callable(
            {
                "block_types": ["oak_log"],
                "search_radius": 16,
                "find_limit": 1,
                "max_pages": 2,
            }
        )
        found_wall_ms = (time.monotonic() - started) * 1000.0
        after_found = provider.body.get_state()

        assert found.success, found.to_payload()
        assert found.reason == "block_candidates_found", found.to_payload()
        assert found.metrics["pages_read"] == 2, found.to_payload()
        assert found.metrics["total_matches"] == 2, found.to_payload()
        assert [tuple(item["pos"]) for item in found.metrics["candidates"]] == list(TARGETS)
        assert distance(before.pos, after_found.pos) < 0.25

        missing = search.callable(
            {
                "block_types": ["diamond_block"],
                "search_radius": 16,
                "find_limit": 8,
                "max_pages": 1,
            }
        )
        after_missing = provider.body.get_state()
        assert not missing.success, missing.to_payload()
        assert missing.reason == "search_block_not_found", missing.to_payload()
        assert distance(before.pos, after_missing.pos) < 0.25

        artifact = {
            "scope": "canonical_production_block_search",
            "formal_gate": False,
            "bounded": True,
            "environment": "dry_land",
            "body_provider": "java",
            "canonical_tool": "search_for_block",
            "rcon_role": "fixture_setup_only",
            "found": {
                "reason": found.reason,
                "pages_read": found.metrics["pages_read"],
                "total_matches": found.metrics["total_matches"],
                "candidates": found.metrics["candidates"],
                "wall_ms": round(found_wall_ms, 3),
            },
            "not_found": {
                "reason": missing.reason,
                "can_retry": missing.can_retry,
            },
            "movement": {
                "before": before.pos,
                "after_found": after_found.pos,
                "after_not_found": after_missing.pos,
            },
            "scarpet_body_constructed": False,
        }
        out = Path("logs/agentic-runtime/java-body-production-search-20260727.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
        print(json.dumps(artifact, indent=2))
        return 0
    finally:
        try:
            if not provider.body.get_state().missing:
                command(rcon, f"player {BOT} kill")
                wait_for_presence(provider.body, present=False)
        except Exception:
            pass
        provider.java_body._client.close()
        rcon.close()


if __name__ == "__main__":
    raise SystemExit(main())
