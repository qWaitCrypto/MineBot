#!/usr/bin/env python3
"""Bounded Java-only proof for collecting a buried resource. Not a gate.

RCON builds and inspects a disposable dry underground fixture. The canonical
``collect_resource`` tool must either leave a protected fixture untouched or,
in natural terrain, clear a player-sized approach, mine iron ore, and finish
only after raw iron enters the player's inventory.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
import time

from minebot.app.body_provider import build_body_provider
from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_agent_runtime
from minebot.contract import Region
from minebot.game.rcon import RconClient, RconConfig


BOT = "JavaBuried"
BODY_URL = "ws://127.0.0.1:8767"
X = 160
Y = 100
Z = 160
TARGET = (X + 4, Y, Z)
NATURAL = Region("buried-collect-natural", (144, 80, 144), (180, 120, 180))
PROTECTED = Region("buried-collect-protected", (152, 92, 152), (172, 112, 168))
PALETTE = ("stone", "deepslate", "dirt", "sandstone", "clay")
ARTIFACT = Path("logs/agentic-runtime/java-body-buried-collect-20260730.json")


def command(rcon: RconClient, value: str, delay: float = 0.02) -> str:
    result = rcon.command(value)
    if delay:
        time.sleep(delay)
    return result


def wait_for_presence(body, *, present: bool, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if body.get_state().missing == (not present):
            return
        time.sleep(0.1)
    raise AssertionError(f"FakePlayer presence did not become {present}")


def wait_for_position(body, expected: tuple[float, float, float], timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if math.dist(body.get_state().pos, expected) <= 0.25:
            return
        time.sleep(0.05)
    raise AssertionError(f"player did not settle at {expected}: {body.get_state().pos}")


def block_is(rcon: RconClient, pos: tuple[int, int, int], block: str) -> bool:
    result = command(
        rcon,
        f"execute if block {pos[0]} {pos[1]} {pos[2]} minecraft:{block}",
        delay=0.0,
    )
    return "passed" in result.lower()


def prepare_fixture(rcon: RconClient, body) -> None:
    state = body.get_state()
    if state.missing:
        command(rcon, f"player {BOT} spawn")
        wait_for_presence(body, present=True)
    command(rcon, f"gamemode spectator {BOT}")
    command(rcon, f"tp {BOT} {X + 0.5} {Y + 12} {Z + 0.5}")
    command(rcon, f"clear {BOT}")
    command(
        rcon,
        f"execute positioned {X} {Y} {Z} run kill @e[type=minecraft:item,distance=..24]",
    )
    command(rcon, f"fill {X - 6} {Y - 5} {Z - 6} {X + 8} {Y + 7} {Z + 6} stone")

    # Break up the local palette so the fixture carries natural voxel evidence
    # rather than the symmetry of a player-built stone wall.
    for px in range(X - 1, TARGET[0] + 2):
        for py in range(Y - 3, Y + 5):
            for pz in range(Z - 1, Z + 2):
                block = PALETTE[(px + 2 * py + 3 * pz) % len(PALETTE)]
                command(rcon, f"setblock {px} {py} {pz} {block}", delay=0.0)

    command(rcon, f"setblock {X} {Y} {Z} air", delay=0.0)
    command(rcon, f"setblock {X} {Y + 1} {Z} air", delay=0.0)
    command(rcon, f"setblock {TARGET[0]} {TARGET[1]} {TARGET[2]} iron_ore", delay=0.0)
    command(rcon, f"item replace entity {BOT} hotbar.0 with minecraft:stone_pickaxe 1")
    command(rcon, f"player {BOT} hotbar 1")
    command(rcon, f"gamemode survival {BOT}")
    expected = (X + 0.5, float(Y), Z + 0.5)
    command(rcon, f"tp {BOT} {expected[0]} {expected[1]} {expected[2]} -90 0")
    wait_for_position(body, expected)


def inventory_count(body, item: str) -> int:
    state = body.get_state()
    counts = state.inventory_counts or {}
    return int(counts.get(item, counts.get(f"minecraft:{item}", 0)))


def collect(parts) -> object:
    return parts.registry.get("collect_resource").callable(
        {
            "item": "raw_iron",
            "count": 1,
            "constraints": {
                "radius": 16,
                "max_candidates": 1,
                "max_mutating_calls": 1,
                "max_wall_s": 120,
                "auto_prerequisites": False,
            },
        }
    )


def first_action_metrics(result) -> dict[str, object]:
    attempts = list((result.metrics or {}).get("attempts") or [])
    if not attempts:
        return {}
    return dict(attempts[0].get("metrics") or {})


def event_payload(event) -> dict[str, object]:
    return {
        "seq": event.seq,
        "tick": event.tick,
        "name": event.name,
        "data": dict(event.data),
    }


def main() -> int:
    provider = build_body_provider(
        "java",
        bot_name=BOT,
        natural_region=NATURAL,
        java_body_url=BODY_URL,
    )
    assert provider.scarpet_body is None
    assert provider.java_body is not None
    body = provider.body
    executed_actions: list[str] = []
    original_execute = body.execute

    def record_execute(action):
        executed_actions.append(action.name)
        return original_execute(action)

    body.execute = record_execute  # type: ignore[method-assign]
    parts = build_phase1_agent_runtime(
        body=body,
        goal_text="collect 1 raw iron",
        model_provider=None,
        config=Phase1RuntimeConfig(
            natural_region=NATURAL,
            body_provider="java",
            governance_policy=provider.governance,
        ),
        agent_name=BOT,
    )
    artifact: dict[str, object] = {
        "scope": "java_body_buried_collect",
        "formal_gate": False,
        "bounded": True,
        "environment": "dry_underground",
        "body_provider": "java",
        "scarpet_body_constructed": False,
        "rcon_role": "fixture_setup_and_verification_only",
        "cases": {},
    }

    with RconClient(
        RconConfig(host="127.0.0.1", port=25576, password="test", timeout_s=20)
    ) as rcon:
        try:
            command(rcon, "carpet commandPlayer true")
            command(rcon, "carpet allowSpawningOfflinePlayers true")
            command(rcon, "gamerule doTileDrops true")
            command(rcon, f"player {BOT} kill")
            prepare_fixture(rcon, body)

            provider.governance.protected_regions.append(PROTECTED)
            protected_start_seq = int(body.event_head("buried-collect-deny")["event_seq"])
            body.last_seq = protected_start_seq
            denied = collect(parts)
            body.poll_events()
            denied_events = [event for event in body.event_log if event.seq > protected_start_seq]
            denied_metrics = first_action_metrics(denied)
            denied_case = {
                "result": denied.to_payload(),
                "target_preserved": block_is(rcon, TARGET, "iron_ore"),
                "approach_broken": list(denied_metrics.get("approach_broken") or []),
                "events": [event_payload(event) for event in denied_events],
            }
            artifact["cases"]["protected_deny"] = denied_case

            provider.governance.protected_regions.clear()
            prepare_fixture(rcon, body)
            success_start_seq = int(body.event_head("buried-collect-allow")["event_seq"])
            body.last_seq = success_start_seq
            before = body.get_state()
            raw_iron_before = inventory_count(body, "raw_iron")
            started = time.monotonic()
            collected = collect(parts)
            elapsed = time.monotonic() - started
            body.poll_events()
            after = body.get_state()
            raw_iron_after = inventory_count(body, "raw_iron")
            collected_events = [event for event in body.event_log if event.seq > success_start_seq]
            collected_metrics = first_action_metrics(collected)
            approach_broken = list(collected_metrics.get("approach_broken") or [])
            broken_cells_are_air = all(
                block_is(
                    rcon,
                    (int(entry["x"]), int(entry["y"]), int(entry["z"])),
                    "air",
                )
                for entry in approach_broken
            )
            event_names = [event.name for event in collected_events]
            allow_case = {
                "result": collected.to_payload(),
                "start_pos": list(before.pos),
                "final_pos": list(after.pos),
                "raw_iron_before": raw_iron_before,
                "raw_iron_after": raw_iron_after,
                "target_is_air": block_is(rcon, TARGET, "air"),
                "approach_broken": approach_broken,
                "broken_cells_are_air": broken_cells_are_air,
                "excavation_proposals": event_names.count("collect_excavation_mutation_proposed"),
                "excavation_allows": event_names.count("collect_excavation_mutation_allowed"),
                "excavation_verified": event_names.count("collect_excavation_block_verified"),
                "events": [event_payload(event) for event in collected_events],
                "elapsed_wall_s": round(elapsed, 3),
            }
            artifact["cases"]["natural_allow"] = allow_case

            denied_ok = (
                not denied.success
                and denied_case["target_preserved"] is True
                and denied_case["approach_broken"] == []
                and not any(event.name == "collect_excavation_block_verified" for event in denied_events)
            )
            excavation_breaks = int(collected_metrics.get("approach_excavation_breaks") or 0)
            collected_ok = (
                collected.success
                and collected_metrics.get("approach_excavation_attempted") is True
                and collected_metrics.get("approach_excavation_used") is True
                and 1 <= excavation_breaks <= 8
                and len(approach_broken) == excavation_breaks
                and broken_cells_are_air
                and allow_case["excavation_proposals"] == excavation_breaks
                and allow_case["excavation_allows"] == excavation_breaks
                and allow_case["excavation_verified"] == excavation_breaks
                and raw_iron_after > raw_iron_before
                and allow_case["target_is_air"] is True
                and after.pos[0] >= X + 2.5
            )
            artifact["executed_actions"] = executed_actions
            artifact["success"] = bool(
                denied_ok
                and collected_ok
                and executed_actions == ["collectBlock", "collectBlock"]
            )
        finally:
            try:
                body.interrupt("bounded_probe_cleanup")
                command(rcon, f"player {BOT} kill")
            except Exception:
                pass
            provider.java_body._client.close()
            provider.java_body._read_client.close()

    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(json.dumps(artifact, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(artifact, indent=2, sort_keys=True))
    return 0 if artifact.get("success") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
