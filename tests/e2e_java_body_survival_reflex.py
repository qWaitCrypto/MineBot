#!/usr/bin/env python3
"""Bounded Java-only proof for automatic survival preemption and recovery."""

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
from minebot.body import ObjectiveNavigationTransactions
from minebot.contract import Action, Region
from minebot.game.rcon import RconClient, RconConfig


BOT = "JavaReflexFresh"
BODY_URL = "ws://127.0.0.1:8767"
REGION = Region("java-reflex-probe", (-32, 190, -16), (96, 220, 16))
ROUTE_GOAL_X = 24


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


def rcon_position(rcon: RconClient) -> tuple[float, float, float] | None:
    raw = command(rcon, f"data get entity {BOT} Pos", delay=0.0)
    match = re.search(
        r"\[\s*(-?\d+(?:\.\d+)?)[dDfF]?,\s*"
        r"(-?\d+(?:\.\d+)?)[dDfF]?,\s*"
        r"(-?\d+(?:\.\d+)?)[dDfF]?\s*\]",
        raw,
    )
    if match is None:
        return None
    return tuple(float(value) for value in match.groups())


def events_after(body, seq: int):
    body.poll_events()
    return [event for event in body.event_log if event.seq > seq]


def wait_for_reflex(body, start_seq: int, kind: str, *, timeout_s: float = 20.0):
    def completed():
        for event in events_after(body, start_seq):
            if event.name == "reflexCompleted" and event.data.get("kind") == kind:
                return event
        return None

    return wait_until(completed, timeout_s=timeout_s, label=f"{kind} reflex completion")


def teleport(rcon: RconClient, body, x: int, y: int, z: int):
    command(rcon, f"tp {BOT} {x} {y} {z}")
    return wait_until(
        lambda: body.get_state()
        if math.dist(body.get_state().pos, (x, y, z)) <= 1.0
        else None,
        timeout_s=5.0,
        label=f"teleport to {(x, y, z)}",
    )


def prepare_flat(rcon: RconClient) -> None:
    clear = command(rcon, "fill -8 199 -8 32 206 8 air")
    floor = command(rcon, "fill -8 199 -8 32 199 8 stone")
    assert "filled" in clear.lower(), f"route clear failed: {clear}"
    assert "filled" in floor.lower(), f"route floor failed: {floor}"


def prepare_spawn_area(rcon: RconClient) -> None:
    clear = command(rcon, "fill -8 199 -8 8 205 8 air")
    floor = command(rcon, "fill -8 199 -8 8 199 8 stone")
    assert "filled" in clear.lower(), f"spawn clear failed: {clear}"
    assert "filled" in floor.lower(), f"spawn floor failed: {floor}"


def run_lava_preempt_and_resume(rcon, body) -> dict:
    prepare_flat(rcon)
    teleport(rcon, body, 0, 200, 0)
    command(rcon, f"effect give {BOT} minecraft:fire_resistance 60 0 true")
    hazard_cells = [
        [x, y, z]
        for y in (199, 200)
        for x in range(-1, 2)
        for z in range(-1, 2)
    ]
    hazard_snapshot = body.perceive(
        "blockCells",
        {"cells": hazard_cells, "start": 0, "limit": len(hazard_cells)},
    )
    assert hazard_snapshot.ok and hazard_snapshot.complete, hazard_snapshot
    lava_cells = [
        [cell["x"], cell["y"], cell["z"]]
        for cell in hazard_snapshot.data.get("cells") or []
        if str(cell.get("type") or "").endswith("lava")
    ]
    assert not lava_cells, {
        "lava_cells": lava_cells,
        "state": body.get_state(),
        "snapshot": hazard_snapshot,
    }
    start_seq = body.last_seq
    navigator = ObjectiveNavigationTransactions(body)

    with ThreadPoolExecutor(max_workers=1) as pool:
        moving = pool.submit(
            navigator.navigate_to,
            (ROUTE_GOAL_X, 200, 0),
            timeout_s=25.0,
        )

        def movement_started():
            if moving.done():
                raise AssertionError(
                    f"Java navigation finished before movement was observed: {moving.result()!r}"
                )
            pos = rcon_position(rcon)
            return pos if pos is not None and pos[0] >= 1.5 else None

        moving_pos = wait_until(
            movement_started,
            timeout_s=8.0,
            label="long Java navigation to begin",
        )
        hx = math.floor(moving_pos[0]) - 1
        hz = math.floor(moving_pos[2])
        command(rcon, f"setblock {hx} 200 {hz} lava")
        result = moving.result(timeout=35.0)

    completed = wait_for_reflex(body, start_seq, "lava", timeout_s=8.0)
    recent = events_after(body, start_seq)
    preempted = [
        event for event in recent
        if event.name == "action_terminal"
        and event.data.get("reason") == "preempted"
    ]
    handoffs = [event for event in recent if event.name == "ownerPreempted"]
    final = body.get_state()

    assert result.success and result.reason == "arrived", result
    assert preempted, recent
    assert handoffs, recent
    assert completed.data.get("escaped_hazard") is True, completed
    assert completed.data.get("final_is_dry_stand") is True, completed
    assert final.body_owner is None and final.pending_action_count == 0, final
    assert final.hazard_unresolved is None, final
    assert final.pos[0] >= ROUTE_GOAL_X - 2.0, final

    command(rcon, f"setblock {hx} 200 {hz} air")
    return {
        "hazard_block": [hx, 200, hz],
        "preempted_terminals": len(preempted),
        "owner_handoffs": len(handoffs),
        "reflex": dict(completed.data),
        "continued_goal_reason": result.reason,
        "continued_final_pos": list(final.pos),
        "reflex_handoffs": (result.metrics or {}).get("reflex_handoffs"),
    }


def run_lava_no_escape_latch(rcon, body) -> dict:
    command(rcon, "fill -10 199 -10 10 203 10 air")
    command(rcon, "fill -8 199 -8 8 199 8 lava")
    command(rcon, "setblock 0 199 0 stone")
    teleport(rcon, body, 0, 200, 0)
    command(rcon, f"effect give {BOT} minecraft:fire_resistance 60 0 true")
    start_seq = body.last_seq

    unresolved = wait_until(
        lambda: body.get_state().hazard_unresolved,
        timeout_s=5.0,
        label="unresolved lava latch",
    )
    completed = wait_for_reflex(body, start_seq, "lava", timeout_s=5.0)
    before = body.get_state().pos
    blocked = body.execute(Action.create("navigate", {
        "goal": {"kind": "near", "x": 4, "y": 200, "z": 0, "range": 0.5},
        "timeout_ticks": 200,
    }))
    after_blocked = body.get_state().pos

    assert completed.data.get("escaped_hazard") is False, completed
    assert unresolved.get("kind") == "lava", unresolved
    assert unresolved.get("recovery_target") is None, unresolved
    assert blocked.ok is False and blocked.error == "hazard_unresolved", blocked
    assert math.dist(before, after_blocked) < 0.1, (before, after_blocked)

    command(rcon, "fill -8 199 -8 8 199 8 stone")
    wait_until(
        lambda: body.get_state() if body.get_state().hazard_unresolved is None else None,
        timeout_s=5.0,
        label="hazard latch to clear after world change",
    )
    continued = body.execute(Action.create("navigate", {
        "goal": {"kind": "near", "x": 4, "y": 200, "z": 0, "range": 0.5},
        "timeout_ticks": 300,
    }))
    final = body.get_state()
    assert continued.ok and continued.complete, continued
    assert final.pos[0] >= 3.0, final

    return {
        "reflex": dict(completed.data),
        "hazard_unresolved": dict(unresolved),
        "blocked_reason": blocked.error,
        "position_unchanged_while_blocked": math.dist(before, after_blocked) < 0.1,
        "continuation_ok_after_world_change": continued.ok,
        "continuation_final_pos": list(final.pos),
    }


def run_low_air_water_escape(rcon, body) -> dict:
    command(rcon, "fill -10 199 -8 12 205 8 air")
    command(rcon, "fill -10 199 -8 12 199 8 stone")
    command(rcon, "fill -4 200 -4 4 202 4 water")
    teleport(rcon, body, 0, 200, 0)
    command(rcon, f"effect clear {BOT} minecraft:water_breathing")
    start_seq = body.last_seq
    started = time.monotonic()
    try:
        completed = wait_for_reflex(body, start_seq, "water", timeout_s=22.0)
    except AssertionError as error:
        state = body.get_state()
        cells = body.perceive(
            "blockCells",
            {
                "cells": [
                    [math.floor(state.pos[0]), math.floor(state.pos[1]), math.floor(state.pos[2])],
                    [math.floor(state.pos[0]), math.floor(state.pos[1]) + 1, math.floor(state.pos[2])],
                ],
                "start": 0,
                "limit": 2,
            },
        )
        raise AssertionError({
            "cause": str(error),
            "state": state,
            "cells": cells,
            "events": events_after(body, start_seq),
        }) from error
    assert completed.data.get("escaped_hazard") is True, completed
    assert completed.data.get("final_is_dry_stand") is True, completed
    final = wait_until(
        lambda: (state if (state := body.get_state()).oxygen is not None and state.oxygen > 80 else None),
        timeout_s=5.0,
        label="air to recover after reaching dry ground",
    )
    assert final.hazard_unresolved is None, final

    return {
        "elapsed_s": round(time.monotonic() - started, 3),
        "reflex": dict(completed.data),
        "final_pos": list(final.pos),
        "final_air": final.oxygen,
    }


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
        command(rcon, "difficulty normal")
        command(rcon, "gamerule doMobSpawning false")
        command(rcon, f"player {BOT} kill")
        prepare_spawn_area(rcon)
        body.event_head("java-reflex-live")
        spawned = body.spawn((0, 200, 0), gamemode="survival", timeout_s=10.0)
        assert spawned.ok and spawned.accepted, spawned
        body.poll_events()

        artifact = {
            "scope": "java_body_survival_reflex",
            "formal_gate": False,
            "bounded": True,
            "provider": "java",
            "scarpet_body_constructed": False,
            "scarpet_action_calls": 0,
            "cases": {
                "lava_preempt_resume": run_lava_preempt_and_resume(rcon, body),
                "lava_no_escape_latch": run_lava_no_escape_latch(rcon, body),
                "low_air_water_escape": run_low_air_water_escape(rcon, body),
            },
        }
        output = Path("logs/agentic-runtime/java-body-survival-reflex-20260728.json")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
        print(json.dumps(artifact, indent=2))
        return 0
    finally:
        try:
            body.despawn()
            command(rcon, "difficulty peaceful")
        except Exception:
            pass
        provider.java_body._client.close()
        rcon.close()


if __name__ == "__main__":
    raise SystemExit(main())
