#!/usr/bin/env python3
"""Observability/performance e2e against the local Carpet test server."""

from __future__ import annotations

import math
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.contract import Action
from minebot.game import ScarpetBody
from tests.e2e_support import connect_or_skip, spawn_or_fail


BOT = "E2EObsBot"
BOT2 = "E2EObsBot2"
BASE = (360, 60, 0)


def command(rcon, command: str, delay: float = 0.05) -> str:
    out = rcon.command(command)
    if delay:
        time.sleep(delay)
    return out


def setup_world(rcon) -> None:
    x, y, z = BASE
    for cmd in [
        "script unload minebot",
        "script load minebot global",
        "carpet commandPlayer true",
        "carpet allowSpawningOfflinePlayers true",
        "gamerule doDaylightCycle false",
        "gamerule doMobSpawning false",
        "gamerule doWeatherCycle false",
        "weather clear",
        "difficulty normal",
        "time set day",
        "kill @e[type=!player]",
        f"player {BOT} kill",
        f"player {BOT2} kill",
        "script in minebot run minebot_reset()",
        f"fill {x-4} {y-1} {z-4} {x+10} {y+4} {z+4} air",
        f"fill {x-4} {y-2} {z-4} {x+10} {y-2} {z+4} stone",
        f"setblock {x+1} {y-1} {z} oak_log[axis=y]",
        f"setblock {x+2} {y-1} {z} lever[face=floor,facing=north,powered=false]",
    ]:
        command(rcon, cmd)


def wait_for_terminal(body: ScarpetBody, action_id: str, *, timeout_s: float = 10.0):
    return body.await_action_terminal(action_id, timeout_s=timeout_s)


def sample_tick_health(rcon, *, samples: int = 3, delay_s: float = 0.2) -> dict[str, object]:
    avg_mspt: list[float] = []
    p95_mspt: list[float] = []
    raw_samples: list[str] = []
    for _ in range(samples):
        raw = command(rcon, "tick query", delay=0.0)
        raw_samples.append(raw)
        avg = re.search(r"Average time per tick:\s*([0-9.]+)ms", raw)
        p95 = re.search(r"P95:\s*([0-9.]+)ms", raw)
        if avg:
            avg_mspt.append(float(avg.group(1)))
        if p95:
            p95_mspt.append(float(p95.group(1)))
        time.sleep(delay_s)
    if not avg_mspt:
        raise AssertionError(f"tick query did not expose parsable MSPT diagnostics: {raw_samples}")
    mean_avg_mspt = sum(avg_mspt) / len(avg_mspt)
    if mean_avg_mspt >= 50.0:
        raise AssertionError(f"single-server observability sample exceeded 50ms tick budget: {raw_samples}")
    return {
        "avg_mspt_samples": avg_mspt,
        "p95_mspt_samples": p95_mspt,
        "avg_mspt_mean": round(mean_avg_mspt, 3),
        "p95_mspt_mean": round(sum(p95_mspt) / len(p95_mspt), 3) if p95_mspt else None,
        "raw_tail": raw_samples[-1][:400],
    }


def run_debug_blocks_happy_and_budget_inverse(body: ScarpetBody) -> dict[str, object]:
    full = body.perceive("debugBlocks", {"radius": 1, "limit": 64})
    if not full.ok or not full.complete:
        raise AssertionError(f"debugBlocks happy path failed: {full}")
    blocks = full.data.get("blocks") or []
    if not any(item.get("type") in {"oak_log", "minecraft:oak_log"} for item in blocks):
        raise AssertionError(f"debugBlocks missing oak_log evidence: {full.data}")
    cursor = full.data.get("cursor") or {}
    feet = full.data.get("feet") or {}
    if cursor.get("state") != "CLEAR":
        raise AssertionError(f"debugBlocks cursor state drifted: {full.data}")
    if feet.get("type") not in {"stone", "minecraft:stone"}:
        raise AssertionError(f"debugBlocks feet block drifted: {full.data}")

    degraded = body.perceive("debugBlocks", {"radius": 2, "limit": 4})
    if not degraded.ok or degraded.complete:
        raise AssertionError(f"debugBlocks budget inverse did not degrade honestly: {degraded}")
    uncertainty = degraded.uncertainty or []
    if not any(item.get("reason") in {"limit_exceeded", "page_limit"} for item in uncertainty):
        raise AssertionError(f"debugBlocks budget inverse lost pagination truth: {degraded}")
    if degraded.next is None or int(degraded.next) <= 0:
        raise AssertionError(f"debugBlocks budget inverse lost numeric resume cursor truth: {degraded}")
    if degraded.data.get("nextStart") is None or int(degraded.data["nextStart"]) != int(degraded.next):
        raise AssertionError(f"debugBlocks budget inverse nextStart/next drifted: {degraded}")
    if int(degraded.data.get("count") or 0) != 4:
        raise AssertionError(f"debugBlocks budget inverse count drifted: {degraded.data}")

    return {
        "happy_count": len(blocks),
        "budget_count": degraded.data.get("count"),
        "budget_uncertainty": uncertainty,
    }


def run_event_order_and_trace(body: ScarpetBody) -> dict[str, object]:
    action = Action.create("moveTo", {"target": [BASE[0] + 5, BASE[1], BASE[2]]})
    result = body.execute(action)
    if not result.ok or not result.accepted:
        raise AssertionError(f"moveTo dispatch failed for observability trace: {result}")
    terminal = wait_for_terminal(body, action.id)
    if terminal.name != "moveDone" or not terminal.data.get("arrived"):
        raise AssertionError(f"moveTo did not complete truthfully for observability trace: {terminal}")

    snapshot = body.observability_snapshot()
    traces = snapshot.get("action_traces") or []
    if not traces:
        raise AssertionError(f"observability snapshot missing action traces: {snapshot}")
    trace = traces[-1]
    if trace.get("action_id") != action.id or trace.get("action_name") != "moveTo":
        raise AssertionError(f"observability trace identity drifted: {trace}")
    if trace.get("terminal_event") != "moveDone":
        raise AssertionError(f"observability trace terminal event drifted: {trace}")
    if trace.get("terminal_data", {}).get("stopped_reason") != "arrived":
        raise AssertionError(f"observability trace terminal data drifted: {trace}")

    events = snapshot.get("events") or []
    move_started = next((event for event in events if event.get("name") == "moveStarted"), None)
    move_done = next((event for event in events if event.get("name") == "moveDone" and event.get("data", {}).get("action_id") == action.id), None)
    if move_started is None or move_done is None:
        raise AssertionError(f"observability snapshot missing ordered move events: {snapshot}")
    if int(move_started.get("seq") or 0) >= int(move_done.get("seq") or 0):
        raise AssertionError(f"event order drifted: moveStarted={move_started} moveDone={move_done}")

    transport = snapshot.get("transport") or {}
    if int(transport.get("count") or 0) < 3:
        raise AssertionError(f"transport snapshot missing requests: {snapshot}")
    if (transport.get("max_request_ms") or 0.0) <= 0.0:
        raise AssertionError(f"transport snapshot missing positive latency: {snapshot}")

    return {
        "trace": trace,
        "transport": transport,
        "move_started_seq": move_started.get("seq"),
        "move_done_seq": move_done.get("seq"),
    }


def run_multi_bot_isolation(rcon, body: ScarpetBody) -> dict[str, object]:
    other = ScarpetBody(BOT2, body.transport)
    spawn_or_fail(other, (BASE[0], BASE[1], BASE[2] + 2))
    command(rcon, f"tp {BOT2} {BASE[0]} {BASE[1]} {BASE[2] + 2} -90 0")
    command(rcon, f"gamemode survival {BOT2}")

    first = Action.create("moveTo", {"target": [BASE[0] + 6, BASE[1], BASE[2]]})
    second = Action.create("moveTo", {"target": [BASE[0] + 6, BASE[1], BASE[2] + 2]})
    first_result = body.execute(first)
    second_result = other.execute(second)
    if not first_result.ok or not first_result.accepted:
        raise AssertionError(f"first bot move dispatch failed: {first_result}")
    if not second_result.ok or not second_result.accepted:
        raise AssertionError(f"second bot move dispatch failed: {second_result}")
    tick_health = sample_tick_health(rcon)

    first_terminal = wait_for_terminal(body, first.id)
    second_terminal = wait_for_terminal(other, second.id)
    if first_terminal.bot != BOT or second_terminal.bot != BOT2:
        raise AssertionError(f"multi-bot terminal bot identity drifted: first={first_terminal} second={second_terminal}")
    if first_terminal.data.get("action_id") != first.id or second_terminal.data.get("action_id") != second.id:
        raise AssertionError(f"multi-bot terminal action identity drifted: first={first_terminal} second={second_terminal}")
    if not first_terminal.data.get("arrived") or not second_terminal.data.get("arrived"):
        raise AssertionError(f"multi-bot movement did not arrive truthfully: first={first_terminal} second={second_terminal}")

    first_state = body.get_state()
    second_state = other.get_state()
    first_terminal_pos = tuple(first_terminal.data.get("final_pos") or ())
    second_terminal_pos = tuple(second_terminal.data.get("final_pos") or ())
    if len(first_terminal_pos) != 3 or math.dist(first_terminal_pos, (BASE[0] + 6.0, BASE[1], BASE[2])) > 1.5:
        raise AssertionError(f"first bot terminal final_pos drifted: terminal={first_terminal} target={(BASE[0] + 6.0, BASE[1], BASE[2])}")
    if len(second_terminal_pos) != 3 or math.dist(second_terminal_pos, (BASE[0] + 6.0, BASE[1], BASE[2] + 2.0)) > 1.5:
        raise AssertionError(f"second bot terminal final_pos drifted: terminal={second_terminal} target={(BASE[0] + 6.0, BASE[1], BASE[2] + 2.0)}")
    if math.dist(first_state.pos, first_terminal_pos) > 2.5:
        raise AssertionError(f"first bot post-terminal state diverged too far: state={first_state} terminal={first_terminal}")
    if math.dist(second_state.pos, second_terminal_pos) > 2.5:
        raise AssertionError(f"second bot post-terminal state diverged too far: state={second_state} terminal={second_terminal}")

    return {
        "first_terminal": first_terminal.data,
        "second_terminal": second_terminal.data,
        "first_final": first_state.pos,
        "second_final": second_state.pos,
        "tick_health": tick_health,
    }


def main() -> int:
    with connect_or_skip() as rcon:
        setup_world(rcon)
        body = ScarpetBody(BOT, rcon)
        spawn_or_fail(body, BASE)
        command(rcon, f"tp {BOT} {BASE[0]} {BASE[1]} {BASE[2]} -90 0")
        command(rcon, f"gamemode survival {BOT}")
        debug = run_debug_blocks_happy_and_budget_inverse(body)
        trace = run_event_order_and_trace(body)
        isolation = run_multi_bot_isolation(rcon, body)
        print(
            {
                "debug_blocks": debug,
                "trace": trace,
                "multi_bot": isolation,
            }
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
