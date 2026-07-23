#!/usr/bin/env python3
"""Machine-readable Q0/Q1 replay for golden spawn autonomous frontier egress.

This is a bounded Body replay, not a formal AG-FP30/Q4/Q5 long run:

* it starts the FakePlayer empty-handed at the golden-world spawn contract;
* it calls the production ``explore_for`` tool through the shared registry and
  progress weld;
* it may continue only with the opaque ``resume_cursor`` returned by that same
  Body transaction;
* it never injects route hints, coordinates, model guidance, scaffold, tools, or
  artificial disturbances.

The replay closes only the spawn-to-frontier evidence debt when the Body reaches
the historical frontier anchor, or makes an equivalent autonomous frontier
advance with target facts, from the same production-style target descriptor.
Typed failure remains evidence, not success.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import time
from pathlib import Path
from typing import Any

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_registry  # noqa: E402
from minebot.app.runtime_identity import ensure_world_identity  # noqa: E402
from minebot.brain.progress import ProgressAuthority  # noqa: E402
from minebot.brain.registry import WeldContext, execute_tool  # noqa: E402
from minebot.contract import ProgressAbort, Region  # noqa: E402
from minebot.game import RconClient, ScarpetBody  # noqa: E402
from minebot.game.rcon import RconConfig  # noqa: E402
from tests.e2e_support import connect_or_skip, spawn_or_fail  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
BOT = "Q1SpawnFrontier"
START = (0, 70, 0)
HISTORICAL_FRONTIER_ANCHOR = (-44.002, 67.0, -25.543)
HISTORICAL_FRONTIER_REGION = (-4, -2)
NATURAL_REGION = Region("golden-natural", (-256, 0, -256), (256, 128, 256))
TARGET_DESCRIPTOR: dict[str, Any] = {
    "block_targets": ["#logs", "#flowers"],
    "entity_targets": ["pig", "cow", "sheep"],
    "max_distance": 192,
    "max_regions": 12,
    "return_policy": "region_budget",
    "scan_radius": 24,
}


def _command(rcon: RconClient, command: str, *, delay_s: float = 0.05) -> str:
    result = rcon.command(command)
    if delay_s:
        time.sleep(delay_s)
    return result


def _state_payload(body: ScarpetBody) -> dict[str, Any]:
    state = body.get_state()
    return {
        "bot": state.bot,
        "pos": [round(float(item), 3) for item in state.pos],
        "missing": state.missing,
        "health": state.health,
        "food": state.food,
        "oxygen": state.oxygen,
        "dimension": state.dimension,
        "inventory_hash": state.inventory_hash,
        "inventory_counts": dict(state.inventory_counts or {}),
        "selected_item": state.selected_item,
        "offhand_item": state.offhand_item,
        "body_owner": state.body_owner,
        "pending_action_count": state.pending_action_count,
    }


def _horizontal_distance(a: list[float] | tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return math.hypot(float(a[0]) - float(b[0]), float(a[2]) - float(b[2]))


def _distance_3d(a: list[float] | tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return math.dist((float(a[0]), float(a[1]), float(a[2])), b)


def _as_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "to_payload"):
        payload = value.to_payload()
        if isinstance(payload, dict):
            return payload
    return {"success": False, "reason": "unexpected_result_type", "value_type": type(value).__name__}


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return _jsonable(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def _candidate_failures(payload: dict[str, Any]) -> list[dict[str, Any]]:
    metrics = payload.get("metrics")
    if not isinstance(metrics, dict):
        return []
    return [dict(item) for item in metrics.get("candidate_failures") or [] if isinstance(item, dict)]


def _covered_regions(payload: dict[str, Any]) -> list[list[int]]:
    metrics = payload.get("metrics")
    if not isinstance(metrics, dict):
        return []
    return [list(item) for item in metrics.get("covered_regions") or [] if isinstance(item, list)]


def _budget(payload: dict[str, Any]) -> dict[str, Any]:
    metrics = payload.get("metrics")
    if not isinstance(metrics, dict):
        return {}
    value = metrics.get("budget")
    return dict(value) if isinstance(value, dict) else {}


def _target_fact_count(payload: dict[str, Any]) -> int:
    metrics = payload.get("metrics")
    if not isinstance(metrics, dict):
        return 0
    blocks = metrics.get("blocks") or []
    entities = metrics.get("entities") or []
    return len(blocks) + len(entities)


def _next_params(payload: dict[str, Any]) -> dict[str, Any] | None:
    metrics = payload.get("metrics")
    if not isinstance(metrics, dict):
        return None
    cursor = metrics.get("resume_cursor")
    if not isinstance(cursor, dict):
        continuation = metrics.get("continuation")
        if isinstance(continuation, dict):
            maybe_cursor = continuation.get("resume_cursor")
            if isinstance(maybe_cursor, dict):
                cursor = maybe_cursor
    if not isinstance(cursor, dict):
        return None
    next_call = dict(TARGET_DESCRIPTOR)
    next_call["resume_cursor"] = dict(cursor)
    return next_call


def _event_summary(body: ScarpetBody) -> dict[str, Any]:
    snapshot = body.observability_snapshot(max_events=256, max_traces=128, max_requests=128)
    events = snapshot.get("events") or []
    counts: dict[str, int] = {}
    mutation_events: list[dict[str, Any]] = []
    governance_events: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        name = str(event.get("name") or "")
        counts[name] = counts.get(name, 0) + 1
        lowered = name.lower()
        if any(token in lowered for token in ("mutation", "mine", "break", "place", "pillar", "downward")):
            mutation_events.append(event)
        if any(token in lowered for token in ("governance", "denied", "protect", "risk")):
            governance_events.append(event)
    return {
        "event_counts": counts,
        "mutation_events": mutation_events[-32:],
        "governance_events": governance_events[-32:],
        "transport": snapshot.get("transport"),
    }


def _classify(
    *,
    calls: list[dict[str, Any]],
    start_state: dict[str, Any],
    final_state: dict[str, Any],
) -> dict[str, Any]:
    state_positions = [start_state["pos"]]
    state_positions.extend(call["after_state"]["pos"] for call in calls)
    anchor_distances = [_distance_3d(pos, HISTORICAL_FRONTIER_ANCHOR) for pos in state_positions]
    min_anchor_distance = min(anchor_distances) if anchor_distances else float("inf")
    final_displacement = _horizontal_distance(final_state["pos"], START)
    target_facts = sum(int(call.get("target_fact_count") or 0) for call in calls)
    saw_historical_region = any(
        failure.get("region") == list(HISTORICAL_FRONTIER_REGION)
        for call in calls
        for failure in call.get("candidate_failures", [])
    )
    progressed_regions = sum(len(call.get("covered_regions") or []) for call in calls)
    terminal_reasons = [str(call.get("result", {}).get("reason") or "") for call in calls]
    failures = [
        failure
        for call in calls
        for failure in call.get("candidate_failures", [])
        if isinstance(failure, dict)
    ]
    no_path_failures = [
        failure
        for failure in failures
        if "no_path" in str(failure.get("reason") or "")
        or any(
            "no_path" in str(attempt.get("reason") or "")
            for attempt in failure.get("navigation_attempts") or []
            if isinstance(attempt, dict)
        )
    ]

    anchor_closed = min_anchor_distance <= 8.0 and saw_historical_region
    equivalent_advance = (
        final_displacement >= 64.0
        and progressed_regions >= 8
        and target_facts > 0
    )
    if anchor_closed:
        verdict = "pass"
        reason = "historical_frontier_replayed"
    elif equivalent_advance:
        verdict = "pass"
        reason = "equivalent_autonomous_frontier_advance_with_target_facts"
    elif final_displacement >= 32.0 and no_path_failures:
        verdict = "fail"
        reason = "typed_mobility_blocker_before_required_frontier_closure"
    else:
        verdict = "fail"
        reason = "insufficient_spawn_frontier_progress"

    return {
        "verdict": verdict,
        "reason": reason,
        "historical_anchor": list(HISTORICAL_FRONTIER_ANCHOR),
        "historical_frontier_region": list(HISTORICAL_FRONTIER_REGION),
        "min_anchor_distance": round(min_anchor_distance, 3),
        "saw_historical_frontier_region": saw_historical_region,
        "final_horizontal_displacement": round(final_displacement, 3),
        "covered_region_count": progressed_regions,
        "target_fact_count": target_facts,
        "terminal_reasons": terminal_reasons,
        "evidence_limits": [
            "This proves only a bounded production Body replay, not a formal AG-FP30/Q4/Q5 long run.",
            "A pass does not lower governance or material yardsticks.",
            "A failure is typed mechanism evidence and must be handled by Debug Reset, not by long-run patching.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--max-calls", type=int, default=3)
    args = parser.parse_args()
    if args.max_calls < 1 or args.max_calls > 6:
        raise SystemExit("--max-calls must be between 1 and 6")

    started_at = time.time()
    with connect_or_skip(RconConfig()) as rcon:
        world_id = ensure_world_identity(rcon)
        for command in (
            "script unload minebot",
            "script load minebot global",
            "carpet commandPlayer true",
            "carpet allowSpawningOfflinePlayers true",
            "gamerule doDaylightCycle false",
            "gamerule doWeatherCycle false",
            "gamerule doMobSpawning false",
            "difficulty peaceful",
            "time set day",
            "weather clear",
            f"player {BOT} kill",
        ):
            _command(rcon, command)

        body = ScarpetBody(BOT, rcon)
        spawn_or_fail(body, START, timeout_s=30.0)
        _command(rcon, f"tp {BOT} {START[0]} {START[1]} {START[2]} 0 0")
        _command(rcon, f"gamemode survival {BOT}")
        _command(rcon, f"clear {BOT}")
        _command(rcon, "kill @e[type=minecraft:item]")
        _command(rcon, "script in minebot run minebot_reset()")

        registry = build_phase1_registry(body, Phase1RuntimeConfig(natural_region=NATURAL_REGION))
        weld = WeldContext(
            body=body,
            authority=ProgressAuthority(),
            goal_text=(
                "Q0/Q1 bounded replay: from golden spawn, autonomously explore for "
                "logs, flowers, pig, cow, and sheep using the Body exploration contract."
            ),
        )

        start_state = _state_payload(body)
        calls: list[dict[str, Any]] = []
        params: dict[str, Any] | None = dict(TARGET_DESCRIPTOR)
        progress_abort: dict[str, Any] | None = None
        for ordinal in range(args.max_calls):
            if params is None:
                break
            before_state = _state_payload(body)
            call_started = time.monotonic()
            try:
                payload = execute_tool(registry.get("explore_for"), params, weld)
            except ProgressAbort as exc:
                payload = {
                    "success": False,
                    "reason": "progress_abort",
                    "canRetry": True,
                    "metrics": {"facts": _jsonable(getattr(exc, "facts", None))},
                }
                progress_abort = {"type": type(exc).__name__, "message": str(exc)}
            except Exception as exc:  # keep the report typed and machine-readable
                payload = {
                    "success": False,
                    "reason": "probe_exception",
                    "canRetry": False,
                    "metrics": {"error_type": type(exc).__name__, "error": str(exc)},
                }
            elapsed_s = time.monotonic() - call_started
            after_state = _state_payload(body)
            result = _as_payload(payload)
            call = {
                "ordinal": ordinal,
                "params": params,
                "elapsed_s": round(elapsed_s, 3),
                "before_state": before_state,
                "after_state": after_state,
                "horizontal_displacement": round(_horizontal_distance(after_state["pos"], before_state["pos"]), 3),
                "anchor_distance_after": round(_distance_3d(after_state["pos"], HISTORICAL_FRONTIER_ANCHOR), 3),
                "result": result,
                "budget": _budget(result),
                "covered_regions": _covered_regions(result),
                "candidate_failures": _candidate_failures(result),
                "target_fact_count": _target_fact_count(result),
            }
            calls.append(call)
            historical_region_seen = any(
                failure.get("region") == list(HISTORICAL_FRONTIER_REGION)
                for failure in call["candidate_failures"]
            )
            if call["anchor_distance_after"] <= 8.0 and historical_region_seen:
                break
            if progress_abort is not None:
                break
            if result.get("reason") in {"found", "frontier_exhausted"} and not result.get("canRetry"):
                break
            params = _next_params(result)

        final_state = _state_payload(body)
        event_summary = _event_summary(body)
        classification = _classify(calls=calls, start_state=start_state, final_state=final_state)

        report = {
            "schema_version": 1,
            "scope": "Q0_Q1_golden_spawn_autonomous_frontier_egress",
            "bounded": True,
            "formal_gate": False,
            "requires_reset_command": "tools/reset-world.sh",
            "world_fixture": "world-golden",
            "world_id": world_id,
            "bot": BOT,
            "start_contract": {
                "pos": list(START),
                "inventory": "empty",
                "gamemode": "survival",
                "body_owner": None,
                "pending_action_count": 0,
            },
            "target_descriptor": TARGET_DESCRIPTOR,
            "start_state": start_state,
            "final_state": final_state,
            "calls": calls,
            "event_summary": event_summary,
            "progress_abort": progress_abort,
            "classification": classification,
            "elapsed_wall_s": round(time.time() - started_at, 3),
        }
        try:
            _command(rcon, f"player {BOT} kill", delay_s=0.2)
        finally:
            pass

    output = args.output or ROOT / "logs" / "agentic-runtime" / f"q1-spawn-frontier-replay-{int(started_at)}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["classification"]["verdict"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
