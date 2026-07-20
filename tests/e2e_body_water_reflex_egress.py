#!/usr/bin/env python3
"""Live water-reflex egress and navigation-mutation preemption regressions."""

from __future__ import annotations

import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.game import Action, ScarpetBody
from tests.e2e_support import connect_or_skip, spawn_or_fail


BOT = "E2EWaterEgress"
BASE = (480, 90, 480)


def command(rcon, text: str, *, delay: float = 0.05) -> str:
    result = rcon.command(text)
    if delay:
        time.sleep(delay)
    return result


def setup_world(rcon) -> None:
    for text in (
        "script unload minebot",
        "script load minebot global",
        "script in minebot run minebot_reset()",
        "carpet commandPlayer true",
        "carpet allowSpawningOfflinePlayers true",
        "gamerule doMobSpawning false",
        "gamerule doDaylightCycle false",
        "difficulty peaceful",
        f"player {BOT} kill",
        "script in minebot run global_reflex_scan = false",
    ):
        command(rcon, text)


def arm_high_bank_fixture(rcon) -> tuple[int, int, int]:
    x, y, z = BASE
    water_y = y - 8
    bank_x = x + 7
    command(rcon, f"fill {x - 2} {water_y - 6} {z - 2} {bank_x + 3} {y + 3} {z + 2} air")
    command(rcon, f"fill {x + 1} {water_y - 5} {z - 1} {bank_x - 1} {water_y - 5} {z + 1} stone")
    command(rcon, f"fill {x + 1} {water_y - 4} {z - 1} {bank_x - 1} {water_y} {z + 1} water")
    command(rcon, f"fill {bank_x} {water_y} {z - 1} {bank_x + 3} {water_y} {z + 1} stone")
    command(rcon, f"fill {x - 2} {water_y - 5} {z - 2} {bank_x + 3} {y + 2} {z - 2} stone")
    command(rcon, f"fill {x - 2} {water_y - 5} {z + 2} {bank_x + 3} {y + 2} {z + 2} stone")
    command(rcon, f"tp {BOT} {x + 2.5} {water_y - 2} {z + 0.5} -90 0")
    command(rcon, f"gamemode survival {BOT}")
    command(rcon, f"player {BOT} stop")
    return (bank_x + 1, water_y + 1, z)


def wait_for_reflex_completed(body: ScarpetBody, *, timeout_s: float = 8.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for event in body.poll_events():
            if event.name == "reflexCompleted":
                return event
        time.sleep(0.05)
    raise AssertionError("timed out waiting for water reflex completion")


def wait_for_action_event(
    body: ScarpetBody,
    action_id: str,
    name: str,
    *,
    event_start: int,
    timeout_s: float = 8.0,
):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for event in body.event_log[event_start:]:
            if event.name == name and event.data.get("action_id") == action_id:
                return event
        body.poll_events()
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {name} on action {action_id}")


def assert_dry_stand(rcon, body: ScarpetBody) -> None:
    final = body.get_state()
    x, y, z = (math.floor(axis) for axis in final.pos)
    raw = command(rcon, f"script in minebot run is_dry_stand_cell({x},{y},{z})", delay=0.0)
    if "true" not in raw:
        raise AssertionError(f"water reflex did not exit to a dry stand: final={final.pos} raw={raw}")


def run_delayed_move_cancel_egress(rcon, body: ScarpetBody) -> tuple[int, int, int]:
    expected_shore = arm_high_bank_fixture(rcon)
    body.poll_events()
    event_start = len(body.event_log)
    action = Action.create(
        "moveTo",
        {
            "target": list(expected_shore),
            "waypoints": [list(expected_shore)],
            "path_moves": ["swim"],
            "cancel_policies": ["egress_to_dry"],
            "arrival_radius": 0.35,
            "timeout_ticks": 240,
            "no_progress_ticks": 120,
        },
    )
    dispatched = body.execute(action)
    if not (dispatched.ok and dispatched.accepted):
        raise AssertionError(f"high-bank move was not accepted: {dispatched}")
    interrupted = body.interrupt("water-reflex-high-bank")
    if not (interrupted.ok and interrupted.accepted):
        raise AssertionError(f"high-bank move interrupt was not accepted: {interrupted}")
    terminal = body.await_action_terminal(action.id, timeout_s=8.0)
    recent = body.event_log[event_start:]
    egress = next((event for event in recent if event.name == "moveCancelEgress"), None)
    completed = next((event for event in recent if event.name == "reflexCompleted"), None)
    if terminal.name != "moveDone" or terminal.data.get("stopped_reason") != "interrupted":
        raise AssertionError(f"delayed egress returned the wrong terminal: {terminal}")
    if egress is None or egress.data.get("phase") != "started" or egress.data.get("target_dry") is not True:
        raise AssertionError(f"delayed egress did not start with a dry target: {recent}")
    if completed is None or completed.data.get("escaped_hazard") is not True:
        raise AssertionError(f"delayed egress did not complete the water reflex: {recent}")
    assert_dry_stand(rcon, body)
    head = body.event_head("water-reflex-delayed-egress")
    if head["owner"] is not None:
        raise AssertionError(f"delayed egress left an owner after terminal truth: {head}")
    return expected_shore


def arm_waiting_break_mutation_fixture(rcon) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    x, y, z = BASE
    lane_x = x + 20
    command(rcon, f"fill {lane_x - 8} {y - 3} {z - 2} {lane_x + 7} {y + 4} {z + 2} air")
    command(rcon, f"fill {lane_x - 8} {y - 1} {z} {lane_x + 7} {y - 1} {z} stone")
    command(rcon, f"fill {lane_x - 8} {y} {z - 1} {lane_x + 7} {y + 2} {z - 1} stone_bricks")
    command(rcon, f"fill {lane_x - 8} {y} {z + 1} {lane_x + 7} {y + 2} {z + 1} stone_bricks")
    blocked = (lane_x + 2, y, z)
    command(rcon, f"fill {blocked[0]} {blocked[1]} {blocked[2]} {blocked[0]} {blocked[1] + 1} {blocked[2]} stone")
    command(rcon, f"tp {BOT} {lane_x + 0.5} {y} {z + 0.5} -90 0")
    command(rcon, f"gamemode survival {BOT}")
    command(rcon, f"player {BOT} stop")
    command(rcon, f"clear {BOT}")
    command(rcon, f"item replace entity {BOT} hotbar.0 with diamond_pickaxe")
    return blocked, (lane_x + 5, y, z)


def run_waiting_navigation_mutation_preempt(rcon, body: ScarpetBody) -> None:
    blocked, target = arm_waiting_break_mutation_fixture(rcon)
    body.poll_events()
    event_start = len(body.event_log)
    action = Action.create(
        "navigateTo",
        {
            "target": list(target),
            "goals": [list(target)],
            "grid_radius": 16,
            "max_expand": 1200,
            "y_below": 4,
            "y_above": 4,
            "arrival_radius": 0.25,
            "goal_radius": 0,
            "timeout_ticks": 240,
            "no_progress_ticks": 120,
            "min_partial_progress": 1,
            "partial_replans": 0,
            "segment_index": 0,
            "allow_diagonal": False,
            "allow_ascend": False,
            "allow_descend": False,
            "allow_swim": False,
            "max_fall_depth": 0,
            "max_water_drop_depth": 1,
            "recheck_lookahead": 5,
            "allow_break": True,
            "break_budget": 2,
            "break_timeout_ticks": 240,
            "break_pickaxe": "minecraft:diamond_pickaxe",
            "break_axe": None,
            "break_shovel": None,
            "allow_place": False,
            "scaffold_item": None,
            "scaffold_count": 0,
            "place_budget": 0,
            "allow_pillar": False,
            "pillar_budget": 0,
            "allow_downward": False,
            "downward_budget": 0,
            "allow_open": False,
            "open_budget": 0,
            "denied_mutations": [],
        },
    )
    dispatched = body.execute(action)
    if not (dispatched.ok and dispatched.accepted):
        raise AssertionError(f"navigation mutation fixture was not accepted: {dispatched}")

    proposed = wait_for_action_event(
        body,
        action.id,
        "navigateMutationProposed",
        event_start=event_start,
    )
    if proposed.data.get("kind") != "break":
        raise AssertionError(f"fixture did not stage a waiting break mutation: {proposed.data}")

    arm_high_bank_fixture(rcon)
    started = command(rcon, f"script in minebot run start_water_reflex('{BOT}')", delay=0.1)
    if "true" not in started:
        raise AssertionError(f"water reflex did not preempt waiting mutation: {started}")

    mutation_done = wait_for_action_event(
        body,
        action.id,
        "navigateMutationDone",
        event_start=event_start,
    )
    navigate_done = wait_for_action_event(
        body,
        action.id,
        "navigateDone",
        event_start=event_start,
    )
    if mutation_done.data.get("success") is not False or mutation_done.data.get("reason") != "preempted":
        raise AssertionError(f"waiting mutation did not settle as preempted: {mutation_done.data}")
    if navigate_done.data.get("arrived") is not False or navigate_done.data.get("reason") != "preempted":
        raise AssertionError(f"navigation did not settle as preempted: {navigate_done.data}")
    if body.perceive("blockAt", {"x": blocked[0], "y": blocked[1], "z": blocked[2]}).data.get("type") not in {
        "stone",
        "minecraft:stone",
    }:
        raise AssertionError("unapproved navigation mutation changed the blocked stone")

    completed = wait_for_reflex_completed(body)
    if completed.data.get("kind") != "water" or completed.data.get("escaped_hazard") is not True:
        raise AssertionError(f"water reflex did not complete after mutation cancellation: {completed.data}")
    cleared = command(
        rcon,
        f"script in minebot run global_navigations:'{BOT}' == null && global_navigation_mutations:'{BOT}' == null",
        delay=0.0,
    )
    if "true" not in cleared:
        raise AssertionError(f"preempted navigation state was retained: {cleared}")
    head = body.event_head("water-reflex-mutation-preempt")
    if head["owner"] is not None:
        raise AssertionError(f"water reflex retained owner after mutation cancellation: {head}")


def main() -> int:
    with connect_or_skip() as rcon:
        setup_world(rcon)
        body = ScarpetBody(BOT, rcon)
        try:
            spawn_or_fail(body, BASE)
            expected_shore = arm_high_bank_fixture(rcon)
            body.poll_events()
            started = command(rcon, f"script in minebot run start_water_reflex('{BOT}')", delay=0.1)
            if "true" not in started:
                raise AssertionError(f"water reflex was not accepted: {started}")
            completed = wait_for_reflex_completed(body)
            if completed.data.get("kind") != "water":
                raise AssertionError(f"high-bank reflex completed with wrong kind: {completed.data}")
            if completed.data.get("target_is_dry_stand") is not True:
                raise AssertionError(f"high-bank reflex did not select a dry target: {completed.data}")
            if completed.data.get("escaped_hazard") is not True or completed.data.get("final_is_dry_stand") is not True:
                raise AssertionError(f"high-bank reflex did not reach dry ground: {completed.data}")
            assert_dry_stand(rcon, body)
            head = body.event_head("water-reflex-high-bank")
            if head["owner"] is not None:
                raise AssertionError(f"water reflex left an owner after dry egress: {head}")
            delayed_shore = run_delayed_move_cancel_egress(rcon, body)
            run_waiting_navigation_mutation_preempt(rcon, body)
            print(
                f"PASS water_reflex_high_bank: final={body.get_state().pos} "
                f"shore={expected_shore} delayed_shore={delayed_shore}"
            )
        finally:
            command(rcon, "script in minebot run global_reflex_scan = false", delay=0.0)
            command(rcon, "script in minebot run global_water_reflex_air_threshold = 80", delay=0.0)
            command(rcon, "script in minebot run global_water_reflex_damage_budget = null", delay=0.0)
            command(rcon, f"player {BOT} kill", delay=0.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
