#!/usr/bin/env python3
"""Bounded Java-only proof for static and moving-target follow behavior."""

from __future__ import annotations

import json
import math
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.app.body_provider import build_body_provider
from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_registry
from minebot.body import ObjectiveNavigationTransactions, PickupConfig, PickupTransactions
from minebot.body.inventory_read import read_inventory_counts
from minebot.contract import Region
from minebot.game.rcon import RconClient, RconConfig


BOT = "JavaFollowLive"
TARGET = "JavaFollowTarget"
BODY_URL = "ws://127.0.0.1:8767"
REGION = Region("java-follow-probe", (-16, 190, -16), (40, 220, 16))


def command(rcon: RconClient, value: str, delay: float = 0.05) -> str:
    response = rcon.command(value)
    if delay:
        time.sleep(delay)
    return response


def wait_until(predicate, *, timeout_s: float, label: str):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {label}")


def rcon_position(rcon: RconClient, name: str) -> tuple[float, float, float] | None:
    raw = command(rcon, f"data get entity {name} Pos", delay=0.0)
    match = re.search(
        r"\[\s*(-?\d+(?:\.\d+)?)[dDfF]?,\s*"
        r"(-?\d+(?:\.\d+)?)[dDfF]?,\s*"
        r"(-?\d+(?:\.\d+)?)[dDfF]?\s*\]",
        raw,
    )
    if match is None:
        return None
    return tuple(float(value) for value in match.groups())


def spawn_target(rcon: RconClient, pos: tuple[int, int, int]) -> None:
    command(rcon, f"player {TARGET} kill")
    command(rcon, f"player {TARGET} spawn at {pos[0]} {pos[1]} {pos[2]}")
    wait_until(
        lambda: rcon_position(rcon, TARGET),
        timeout_s=10.0,
        label="target FakePlayer spawn",
    )
    command(rcon, f"tp {TARGET} {pos[0]} {pos[1]} {pos[2]}")


def distance(rcon: RconClient) -> float:
    bot = rcon_position(rcon, BOT)
    target = rcon_position(rcon, TARGET)
    assert bot is not None and target is not None, (bot, target)
    return math.dist(bot, target)


def main() -> int:
    rcon = RconClient(RconConfig(host="127.0.0.1", port=25576, password="test"))
    rcon.connect()
    provider = build_body_provider(
        "java",
        bot_name=BOT,
        natural_region=REGION,
        java_body_url=BODY_URL,
    )
    assert provider.scarpet_body is None
    assert provider.java_body is not None
    body = provider.body

    try:
        command(rcon, "carpet commandPlayer true")
        command(rcon, "carpet allowSpawningOfflinePlayers true")
        command(rcon, f"player {BOT} kill")
        command(rcon, f"player {TARGET} kill")
        command(rcon, "fill -8 199 -8 32 205 8 air")
        command(rcon, "fill -8 199 -8 32 199 8 stone")
        spawned = body.spawn((0, 200, 0), gamemode="survival", timeout_s=10.0)
        assert spawned.ok and spawned.accepted, spawned
        spawn_target(rcon, (7, 200, 0))

        registry = build_phase1_registry(
            body,
            Phase1RuntimeConfig(
                natural_region=REGION,
                body_provider="java",
                governance_policy=provider.governance,
            ),
        )
        follow = registry.get("follow_entity").callable

        static = follow({"target": TARGET, "keep_distance": 2.5, "timeout_s": 2.0})
        static_distance = distance(rcon)
        assert static.success and static.reason == "arrived", static
        assert static_distance <= 3.25, static_distance

        command(rcon, f"tp {BOT} 0 200 0")
        command(rcon, f"tp {TARGET} 7 200 0")
        with ThreadPoolExecutor(max_workers=1) as pool:
            moving = pool.submit(
                follow,
                {"target": TARGET, "keep_distance": 2.5, "timeout_s": 4.0},
            )
            wait_until(
                lambda: (pos if (pos := rcon_position(rcon, BOT)) is not None and pos[0] >= 1.0 else None),
                timeout_s=2.0,
                label="Java follow movement to begin",
            )
            command(rcon, f"tp {TARGET} 15 200 0")
            moved = moving.result(timeout=10.0)

        moved_distance = distance(rcon)
        assert moved.success and moved.reason == "arrived", moved
        assert moved_distance <= 3.25, moved_distance
        assert int((moved.metrics or {}).get("target_replans") or 0) >= 1, moved.metrics
        assert static.metrics.get("target_id") == moved.metrics.get("target_id"), (
            static.metrics,
            moved.metrics,
        )

        command(rcon, f"player {TARGET} kill")
        command(rcon, f"clear {BOT}")
        command(rcon, "fill -8 199 -8 8 206 8 air")
        command(rcon, "fill -8 199 -8 8 199 8 stone")
        command(rcon, "fill -2 200 -2 5 204 2 water")
        command(rcon, f"tp {BOT} 0.5 200 0.5")
        command(
            rcon,
            'summon item 3.5 203.2 0.5 '
            '{Tags:["minebot.java.pickup"],PickupDelay:0,Item:{id:"minecraft:raw_iron",count:1}}',
        )
        pickup = PickupTransactions(body, ObjectiveNavigationTransactions(body))
        picked = pickup.pickup_items(
            expected_items=("raw_iron",),
            minimum_count=1,
            config=PickupConfig(
                radius=8,
                max_scan_rounds=2,
                candidate_budget=2,
                max_wall_s=12.0,
                poll_timeout_s=1.0,
                segment_timeout_s=4.0,
                max_segments=3,
            ),
        )
        counts = read_inventory_counts(body)
        assert picked.success, picked
        assert isinstance(counts, dict) and counts.get("raw_iron", 0) >= 1, counts
        pickup_plan = (picked.metrics or {})["pickup_process"]["plans"][0]
        assert pickup_plan["mode"] == "follow_entity", pickup_plan

        artifact = {
            "scope": "java_body_follow",
            "formal_gate": False,
            "bounded": True,
            "provider": "java",
            "scarpet_body_constructed": False,
            "scarpet_action_calls": 0,
            "static": {
                "reason": static.reason,
                "final_distance": static_distance,
                "target_id": static.metrics.get("target_id"),
            },
            "moving": {
                "reason": moved.reason,
                "final_distance": moved_distance,
                "target_id": moved.metrics.get("target_id"),
                "target_replans": moved.metrics.get("target_replans"),
                "elapsed_ticks": moved.metrics.get("elapsed_ticks"),
            },
            "moving_item_pickup": {
                "reason": picked.reason,
                "raw_iron": counts.get("raw_iron", 0),
                "mode": pickup_plan["mode"],
                "tracking_reason": pickup_plan["tracking"]["reason"],
            },
        }
        output = Path("logs/agentic-runtime/java-body-follow-20260729.json")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
        print(json.dumps(artifact, indent=2))
        return 0
    finally:
        try:
            body.despawn()
            command(rcon, f"player {TARGET} kill")
        except Exception:
            pass
        provider.java_body._client.close()
        rcon.close()


if __name__ == "__main__":
    raise SystemExit(main())
