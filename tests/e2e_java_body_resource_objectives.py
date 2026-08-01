#!/usr/bin/env python3
"""Bounded Java-only proof for governed dig-down and quantity collection.

RCON only prepares and verifies disposable fixtures. Both player objectives
run through the canonical production registry and one Java Body writer.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

from minebot.app.body_provider import build_body_provider
from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_agent_runtime
from minebot.contract import Region
from minebot.game.rcon import RconClient, RconConfig


BOT = "JavaResObj"
BODY_URL = "ws://127.0.0.1:8767"
NATURAL = Region("resource-objectives", (192, 80, 192), (288, 160, 288))
DIG_ORIGIN = (220.82, 120.0, 220.18)
DIG_TARGET_Y = 118
COLLECT_ORIGIN = (240.5, 100.0, 240.5)
ORE_TARGETS = ((220, 100, 248), (216, 100, 248), (212, 100, 248))
ARTIFACT = Path("logs/agentic-runtime/java-body-zero-candidate-frontier-objective-20260801.json")


def command(rcon: RconClient, value: str, delay: float = 0.03) -> str:
    result = rcon.command(value)
    if delay:
        time.sleep(delay)
    return result


def wait_for_presence(body, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not body.get_state().missing:
            return
        time.sleep(0.1)
    raise AssertionError("JavaResObj did not spawn")


def wait_for_position(body, expected: tuple[float, float, float], timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if math.dist(body.get_state().pos, expected) <= 0.3:
            return
        time.sleep(0.05)
    raise AssertionError(f"player did not settle at {expected}: {body.get_state().pos}")


def inventory_count(body, item: str) -> int:
    counts = body.get_state().inventory_counts or {}
    return int(counts.get(item, counts.get(f"minecraft:{item}", 0)))


def block_is(rcon: RconClient, pos: tuple[int, int, int], block: str) -> bool:
    result = command(
        rcon,
        f"execute if block {pos[0]} {pos[1]} {pos[2]} minecraft:{block}",
        delay=0.0,
    )
    return "passed" in result.lower()


def event_payload(event) -> dict[str, object]:
    return {
        "seq": event.seq,
        "tick": event.tick,
        "name": event.name,
        "action_id": event.data.get("action_id"),
        "data": dict(event.data),
    }


def prepare_player(rcon: RconClient, body) -> None:
    command(rcon, "carpet commandPlayer true")
    command(rcon, "carpet allowSpawningOfflinePlayers true")
    command(rcon, f"player {BOT} kill")
    command(rcon, f"player {BOT} spawn")
    wait_for_presence(body)
    command(rcon, f"gamemode survival {BOT}")
    command(rcon, f"effect clear {BOT}")


def prepare_dig_fixture(rcon: RconClient, body) -> None:
    command(rcon, f"gamemode spectator {BOT}")
    command(rcon, f"tp {BOT} 220.5 132 220.5")
    time.sleep(0.5)
    command(rcon, "fill 214 116 214 226 123 226 stone")
    command(rcon, "fill 214 120 214 226 123 226 air")
    assert block_is(rcon, (220, 119, 220), "stone")
    assert block_is(rcon, (220, 118, 220), "stone")
    command(rcon, f"clear {BOT}")
    command(rcon, f"item replace entity {BOT} hotbar.0 with minecraft:iron_pickaxe 1")
    command(rcon, f"player {BOT} hotbar 1")
    command(rcon, f"gamemode survival {BOT}")
    command(rcon, f"tp {BOT} {DIG_ORIGIN[0]} {DIG_ORIGIN[1]} {DIG_ORIGIN[2]} 0 0")
    wait_for_position(body, DIG_ORIGIN)


def prepare_collect_fixture(rcon: RconClient, body) -> None:
    command(rcon, f"gamemode spectator {BOT}")
    command(rcon, f"tp {BOT} 240.5 112 240.5")
    force_loaded = command(rcon, "forceload add 208 224 272 264")
    assert "marked" in force_loaded.lower(), force_loaded
    try:
        filled = command(rcon, "fill 208 96 224 272 104 264 stone")
        cleared = command(rcon, "fill 208 100 224 272 104 264 air")
        assert "successfully filled" in filled.lower(), filled
        assert "successfully filled" in cleared.lower(), cleared
        for x, y, z in ORE_TARGETS:
            command(rcon, f"setblock {x} {y} {z} minecraft:iron_ore")
        assert block_is(rcon, (232, 99, 248), "stone")
        assert block_is(rcon, (232, 100, 248), "air")
        assert all(block_is(rcon, target, "iron_ore") for target in ORE_TARGETS)
    finally:
        force_removed = command(rcon, "forceload remove 208 224 272 264")
        assert "unmarked" in force_removed.lower(), force_removed
    command(
        rcon,
        "execute positioned 220 100 248 run kill @e[type=minecraft:item,distance=..48]",
    )
    command(rcon, f"clear {BOT}")
    command(rcon, f"item replace entity {BOT} hotbar.0 with minecraft:stone_pickaxe 1")
    command(rcon, f"player {BOT} hotbar 1")
    command(rcon, f"gamemode survival {BOT}")
    command(
        rcon,
        f"tp {BOT} {COLLECT_ORIGIN[0]} {COLLECT_ORIGIN[1]} {COLLECT_ORIGIN[2]} -90 0",
    )
    wait_for_position(body, COLLECT_ORIGIN)


def main() -> int:
    provider = build_body_provider(
        "java",
        bot_name=BOT,
        natural_region=NATURAL,
        java_body_url=BODY_URL,
    )
    assert provider.scarpet_body is None and provider.java_body is not None
    body = provider.body
    parts = build_phase1_agent_runtime(
        body=body,
        goal_text="collect 3 raw iron",
        model_provider=None,
        config=Phase1RuntimeConfig(
            natural_region=NATURAL,
            body_provider="java",
            governance_policy=provider.governance,
        ),
        agent_name=BOT,
    )
    registry = parts.registry
    executed_actions: list[str] = []
    original_execute = body.execute

    def record_execute(action):
        executed_actions.append(action.name)
        return original_execute(action)

    body.execute = record_execute  # type: ignore[method-assign]
    artifact: dict[str, object] = {
        "scope": "java_body_resource_objectives",
        "formal_gate": False,
        "bounded": True,
        "production_provider": "java",
        "scarpet_body_constructed": False,
        "rcon_role": "fixture_setup_and_verification_only",
    }

    try:
        with RconClient(
            RconConfig(host="127.0.0.1", port=25576, password="test", timeout_s=20)
        ) as rcon:
            prepare_player(rcon, body)

            prepare_dig_fixture(rcon, body)
            dig_start_seq = int(body.event_head("java-dig-down-objective")["event_seq"])
            body.last_seq = dig_start_seq
            dig_before = body.get_state()
            dig_started = time.monotonic()
            dig_result = registry.get("dig_down").callable(
                {"target_y": DIG_TARGET_Y, "max_steps": 4, "max_clear_fall": 1}
            )
            dig_elapsed = time.monotonic() - dig_started
            body.poll_events()
            dig_after = body.get_state()
            dig_events = [event for event in body.event_log if event.seq > dig_start_seq]
            dig_case = {
                "result": dig_result.to_payload(),
                "start_pos": list(dig_before.pos),
                "final_pos": list(dig_after.pos),
                "elapsed_wall_s": round(dig_elapsed, 3),
                "opened_floor_blocks": [
                    block_is(rcon, (220, 119, 220), "air"),
                    block_is(rcon, (220, 118, 220), "air"),
                ],
                "events": [event_payload(event) for event in dig_events],
            }
            artifact["dig_down"] = dig_case

            prepare_collect_fixture(rcon, body)
            collect_start_seq = int(body.event_head("java-quantity-collect")["event_seq"])
            body.last_seq = collect_start_seq
            collect_before = body.get_state()
            raw_before = inventory_count(body, "raw_iron")
            action_index = len(executed_actions)
            collect_started = time.monotonic()
            collect_result = registry.get("collect_resource").callable(
                {
                    "item": "raw_iron",
                    "count": 3,
                    "constraints": {
                        "radius": 16,
                        "max_candidates": 6,
                        "max_mutating_calls": 6,
                        "max_wall_s": 120,
                        "auto_prerequisites": False,
                    },
                }
            )
            collect_elapsed = time.monotonic() - collect_started
            body.poll_events()
            collect_after = body.get_state()
            raw_after = inventory_count(body, "raw_iron")
            collect_events = [event for event in body.event_log if event.seq > collect_start_seq]
            candidate_refreshes = [
                event_payload(event)
                for event in collect_events
                if event.name == "collection_candidate_domain_refreshed"
            ]
            frontier_events = [
                event_payload(event)
                for event in collect_events
                if event.name in {
                    "collection_frontier_planned",
                    "collection_frontier_reached",
                    "collection_frontier_failed",
                }
            ]
            collect_terminal = next(
                event_payload(event)
                for event in reversed(collect_events)
                if event.name == "action_terminal"
            )
            terminal_data = dict(collect_terminal["data"])
            terminal_search = dict(terminal_data.get("search") or {})
            attempt_failures = list(terminal_data.get("attempt_failures") or [])
            drop_losses = list(terminal_data.get("drop_losses") or [])
            collect_actions = executed_actions[action_index:]
            collect_case = {
                "result": collect_result.to_payload(),
                "start_pos": list(collect_before.pos),
                "final_pos": list(collect_after.pos),
                "raw_iron_before": raw_before,
                "raw_iron_after": raw_after,
                "elapsed_wall_s": round(collect_elapsed, 3),
                "targets_are_air": [block_is(rcon, target, "air") for target in ORE_TARGETS],
                "body_actions": collect_actions,
                "candidate_refreshes": candidate_refreshes,
                "frontier_events": frontier_events,
                "initial_matches": int(terminal_search.get("matches", -1)),
                "attempt_failures": attempt_failures,
                "drop_losses": drop_losses,
                "terminal_search": terminal_search,
                "events": [event_payload(event) for event in collect_events],
            }
            artifact["collect_quantity"] = collect_case

            dig_ok = bool(
                dig_result.success
                and dig_after.pos[1] <= DIG_TARGET_Y + 0.08
                and all(dig_case["opened_floor_blocks"])
                and executed_actions[0:1] == ["digDown"]
            )
            collect_ok = bool(
                collect_result.success
                and raw_after - raw_before >= 3
                and all(collect_case["targets_are_air"])
                and collect_actions == ["collectBlock"]
                and int(terminal_search.get("matches", -1)) == 0
                and any(event["name"] == "collection_frontier_reached" for event in frontier_events)
                and not attempt_failures
                and not drop_losses
                and sum(
                    int(refresh["data"].get("new_candidates") or 0)
                    for refresh in candidate_refreshes
                ) >= 3
            )
            artifact["success"] = dig_ok and collect_ok
    finally:
        try:
            body.interrupt("bounded_resource_objectives_cleanup")
        except Exception:
            pass
        provider.java_body._client.close()
        provider.java_body._read_client.close()
        parts.runtime.close()

    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(json.dumps(artifact, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(artifact, indent=2, sort_keys=True))
    return 0 if artifact.get("success") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
