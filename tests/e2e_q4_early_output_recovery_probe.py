#!/usr/bin/env python3
"""Bounded Q4 Debug Reset probe for the M8 early-output/recovery blocker.

This is not an AG-FP30/Q4/Q5 long-run gate.  It replays the early M8 production
tool sequence from the golden spawn through the shared registry/progress weld:

* ``collect_resource(oak_log)`` with the first M8 constraints;
* ``explore_for`` with the M8 logs/flowers/animals descriptor;
* ``search_for_block`` with the M8 local read-only descriptor;
* one opaque ``explore_for`` continuation when the Body returns a resume cursor;
* a second wider ``collect_resource(logs)`` probe from the reached state.  The
  family follow-up is intentional: the exploration result may contain any
  authoritative log species, so the handoff must preserve equivalence rather
  than silently narrowing it back to ``oak_log``.

The probe exists to classify why the long run spent too much active time before
its first authoritative prerequisite output, and why natural-obstacle recovery
did not return to output.  It uses the golden spawn coordinate only as a fixture
contract and never writes target coordinates, fixed routes, model guidance, or
item/terrain special cases into production behavior.
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

from minebot.app.autonomy_quality import AG_FP30_YARDSTICK  # noqa: E402
from minebot.app.phase1_runtime import Phase1RuntimeConfig, _recipe_lookup, build_phase1_registry  # noqa: E402
from minebot.app.runner import _collapsed_epoch_progress_steps, _tool_exception_payload  # noqa: E402
from minebot.app.runtime_identity import ensure_world_identity  # noqa: E402
from minebot.brain.composition import (  # noqa: E402
    CompositionContext,
    register_collect_resource_tool,
    register_ensure_tool_for_tool,
)
from minebot.brain.lifecycle import LifecycleState  # noqa: E402
from minebot.brain.modes import ModeRuntime  # noqa: E402
from minebot.brain.progress import ProgressAuthority  # noqa: E402
from minebot.brain.registry import RegisteredTool, WeldContext, execute_tool  # noqa: E402
from minebot.contract import ProgressAbort, Region  # noqa: E402
from minebot.game import RconClient, ScarpetBody  # noqa: E402
from minebot.game.rcon import RconConfig  # noqa: E402
from tests.e2e_support import connect_or_skip, spawn_or_fail  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
BOT = "Q4EarlyProbe"
START = (0, 70, 0)
NATURAL_REGION = Region("golden-natural", (-256, 0, -256), (256, 128, 256))

M8_NARROW_BLOCK_TARGETS = [
    "oak_log",
    "birch_log",
    "spruce_log",
    "dandelion",
    "poppy",
    "blue_orchid",
    "allium",
    "azure_bluet",
    "oxeye_daisy",
    "cornflower",
    "lily_of_the_valley",
]
M8_WIDE_BLOCK_TARGETS = [
    "oak_log",
    "birch_log",
    "spruce_log",
    "jungle_log",
    "acacia_log",
    "dark_oak_log",
    "mangrove_log",
    "cherry_log",
    "pale_oak_log",
    "dandelion",
    "poppy",
    "blue_orchid",
    "allium",
    "azure_bluet",
    "oxeye_daisy",
    "cornflower",
    "lily_of_the_valley",
]
M8_ENTITY_TARGETS = ["pig", "cow", "sheep"]

FIRST_COLLECT = {
    "item": "oak_log",
    "count": 4,
    "constraints": {
        "radius": 32,
        "max_candidates": 24,
        "max_mutating_calls": 16,
        "max_wall_s": 120,
    },
}
FIRST_EXPLORE = {
    "block_targets": M8_NARROW_BLOCK_TARGETS,
    "entity_targets": M8_ENTITY_TARGETS,
    "max_distance": 256,
    "max_regions": 12,
    "return_policy": "first_match",
    "scan_radius": 24,
}
FIRST_SEARCH = {
    "block_types": M8_NARROW_BLOCK_TARGETS,
    "search_radius": 32,
    "find_limit": 32,
    "max_pages": 4,
}
SECOND_COLLECT = {
    "item": "logs",
    "count": 3,
    "constraints": {
        "radius": 48,
        "max_candidates": 32,
        "max_mutating_calls": 24,
        "max_wall_s": 180,
    },
}
WIDE_EXPLORE = {
    "block_targets": M8_WIDE_BLOCK_TARGETS,
    "entity_targets": M8_ENTITY_TARGETS,
    "max_distance": 512,
    "max_regions": 32,
    "return_policy": "first_match",
    "scan_radius": 32,
}
WIDE_SEARCH = {
    "block_types": [target for target in M8_WIDE_BLOCK_TARGETS if target.endswith("_log")],
    "search_radius": 32,
    "find_limit": 64,
    "max_pages": 8,
}
LOG_SURVEY_TYPES = [target for target in M8_WIDE_BLOCK_TARGETS if target.endswith("_log")]
FLOWER_SURVEY_TYPES = [target for target in M8_NARROW_BLOCK_TARGETS if not target.endswith("_log")]


def _command(rcon: RconClient, command: str, *, delay_s: float = 0.05) -> str:
    result = rcon.command(command)
    if delay_s:
        time.sleep(delay_s)
    return result


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return _jsonable(dataclasses.asdict(value))
    if hasattr(value, "to_payload"):
        return _jsonable(value.to_payload())
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


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


def _inventory_counts(body: ScarpetBody) -> dict[str, int]:
    counts: dict[str, int] = {}
    for slot in body.get_inventory():
        if slot.item is None:
            continue
        item = str(slot.item).removeprefix("minecraft:")
        counts[item] = counts.get(item, 0) + int(slot.count or 0)
    return counts


def _equipment(body: ScarpetBody) -> dict[str, Any]:
    state = body.get_state()
    return {
        "mainhand": state.selected_item,
        "offhand": state.offhand_item,
        "selected_slot": state.selected_slot,
    }


def _horizontal_distance(
    a: list[float] | tuple[float, float, float],
    b: list[float] | tuple[float, float, float],
) -> float:
    return math.hypot(float(a[0]) - float(b[0]), float(a[2]) - float(b[2]))


def _as_payload(value: Any) -> dict[str, Any]:
    payload = _jsonable(value)
    if isinstance(payload, dict):
        return payload
    return {"success": False, "reason": "unexpected_result_type", "value_type": type(value).__name__}


def _compact_result(payload: dict[str, Any]) -> dict[str, Any]:
    metrics = payload.get("metrics")
    metrics = metrics if isinstance(metrics, dict) else {}
    compact_metrics: dict[str, Any] = {}
    for key in (
        "item",
        "requested_item",
        "target_count",
        "before_count",
        "after_count",
        "current_count",
        "remaining_count",
        "collected_delta",
        "resume_hint",
        "block_types",
        "expected_drops",
        "budget",
        "complete",
        "coverage_revision",
        "covered_regions",
        "final_pos",
        "origin",
        "blocks",
        "entities",
        "candidate_failures",
        "resume_cursor",
        "continuation",
        "error_type",
        "message",
        "await_diagnostics",
    ):
        if key in metrics:
            compact_metrics[key] = _limit_payload(metrics[key])
    body_process = metrics.get("body_process")
    if isinstance(body_process, dict):
        compact_metrics["body_process"] = _compact_result(body_process)
    last_failure = metrics.get("last_failure")
    if isinstance(last_failure, dict):
        compact_metrics["last_failure"] = _limit_payload(last_failure)
    skipped = metrics.get("skipped")
    if isinstance(skipped, list):
        compact_metrics["skipped"] = _limit_payload(skipped)
    return {
        "success": payload.get("success"),
        "reason": payload.get("reason"),
        "canRetry": payload.get("canRetry"),
        "nextSuggestion": payload.get("nextSuggestion"),
        "metrics": compact_metrics,
    }


def _limit_payload(value: Any, *, max_list: int = 16) -> Any:
    value = _jsonable(value)
    if isinstance(value, dict):
        return {str(key): _limit_payload(item, max_list=max_list) for key, item in value.items()}
    if isinstance(value, list):
        items = [_limit_payload(item, max_list=max_list) for item in value[:max_list]]
        if len(value) > max_list:
            items.append({"truncated": len(value) - max_list})
        return items
    return value


def _resume_params(payload: dict[str, Any], base: dict[str, Any]) -> dict[str, Any] | None:
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
    params = dict(base)
    params["resume_cursor"] = dict(cursor)
    return params


def _target_fact_count(payload: dict[str, Any]) -> int:
    metrics = payload.get("metrics")
    if not isinstance(metrics, dict):
        return 0
    blocks = metrics.get("blocks") or []
    entities = metrics.get("entities") or []
    return len(blocks) + len(entities)


def _candidate_failure_reasons(payload: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    metrics = payload.get("metrics")
    if not isinstance(metrics, dict):
        return reasons
    for failure in metrics.get("candidate_failures") or []:
        if isinstance(failure, dict):
            reasons.append(str(failure.get("reason") or "unknown"))
    body_process = metrics.get("body_process")
    if isinstance(body_process, dict):
        reasons.extend(_candidate_failure_reasons(body_process))
    return reasons


def _yardstick_points(counts: dict[str, int], equipment: dict[str, Any]) -> dict[str, Any]:
    total = 0
    families: dict[str, Any] = {}
    for family in AG_FP30_YARDSTICK.inventory_families:
        if family.distinct:
            observed = sorted(item for item in family.accepted_items if counts.get(item, 0) > 0)
            points = 1 if len(observed) >= family.minimum else 0
            families[family.key] = {
                "observed": observed,
                "minimum": family.minimum,
                "points": points,
            }
        else:
            observed_count = sum(int(counts.get(item, 0)) for item in family.accepted_items)
            points = observed_count // max(1, family.score_unit)
            if family.max_points is not None:
                points = min(points, family.max_points)
            if observed_count < family.minimum:
                points = 0
            families[family.key] = {
                "observed_count": observed_count,
                "minimum": family.minimum,
                "score_unit": family.score_unit,
                "points": points,
            }
        total += points
    equipment_points: dict[str, Any] = {}
    for requirement in AG_FP30_YARDSTICK.equipment:
        observed = equipment.get(requirement.slot)
        normalized = None if observed is None else str(observed).removeprefix("minecraft:")
        points = requirement.points if normalized == requirement.item else 0
        equipment_points[requirement.key] = {
            "slot": requirement.slot,
            "item": requirement.item,
            "observed": normalized,
            "points": points,
        }
        total += points
    return {"points": total, "families": families, "equipment": equipment_points}


def _run_tool(
    *,
    registry,
    weld: WeldContext,
    body: ScarpetBody,
    label: str,
    tool: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    before_state = _state_payload(body)
    before_counts = _inventory_counts(body)
    before_equipment = _equipment(body)
    before_fingerprint = weld.authority.fingerprint(body.get_state())
    started = time.monotonic()
    progress_abort: dict[str, Any] | None = None
    exception: dict[str, Any] | None = None
    progress_commit: dict[str, Any] | None = None
    registered_tool = registry.get(tool)
    try:
        result, progress_commit = _run_tool_with_probe_epoch(
            registered_tool,
            params,
            weld,
            before_fingerprint=before_fingerprint,
        )
        payload = _as_payload(result)
    except ProgressAbort as exc:
        progress_abort = {"type": type(exc).__name__, "message": str(exc)}
        payload = {
            "success": False,
            "reason": "progress_abort",
            "canRetry": True,
            "metrics": {"facts": _limit_payload(getattr(exc, "facts", None))},
        }
    except Exception as exc:  # keep Debug Reset evidence typed and production-aligned
        payload = _tool_exception_payload(exc)
        metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
        exception = {
            "type": type(exc).__name__,
            "message": str(exc),
            "mapped_reason": str(payload.get("reason") or ""),
            "metrics": _limit_payload(metrics),
        }
    elapsed_s = time.monotonic() - started
    after_state = _state_payload(body)
    after_counts = _inventory_counts(body)
    after_equipment = _equipment(body)
    before_points = _yardstick_points(before_counts, before_equipment)["points"]
    after_yardstick = _yardstick_points(after_counts, after_equipment)
    return {
        "label": label,
        "tool": tool,
        "params": params,
        "elapsed_s": round(elapsed_s, 3),
        "before_state": before_state,
        "after_state": after_state,
        "horizontal_displacement": round(_horizontal_distance(before_state["pos"], after_state["pos"]), 3),
        "inventory_before": before_counts,
        "inventory_after": after_counts,
        "yardstick_points_before": before_points,
        "yardstick_points_after": after_yardstick["points"],
        "yardstick_delta_points": after_yardstick["points"] - before_points,
        "yardstick_after": after_yardstick,
        "target_fact_count": _target_fact_count(payload),
        "candidate_failure_reasons": _candidate_failure_reasons(payload),
        "progress_commit": progress_commit,
        "progress_abort": progress_abort,
        "exception": exception,
        "result": _compact_result(payload),
    }


def _run_tool_with_probe_epoch(
    tool: RegisteredTool,
    params: dict[str, Any],
    weld: WeldContext,
    *,
    before_fingerprint: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Execute one probe tool with production-like progress epoch semantics.

    The real Agent runner captures inner Body progress steps for each SDK tool
    batch and commits a collapsed epoch once the batch settles.  This bounded
    probe calls tools directly, so without this local epoch shim a sequence of
    diagnostic calls can trip the failure-storm guard before the probe has
    classified the post-frontier handoff.  The shim preserves fail-closed
    ProgressAbort behavior while keeping the evidence path aligned with the
    production runner's progress accounting.
    """

    pending_abort: ProgressAbort | None = None
    with weld.authority.capture_steps() as captured:
        try:
            result = execute_tool(tool, params, weld)
        except ProgressAbort as exc:
            pending_abort = exc
            result = {
                "success": False,
                "reason": "progress_yielded",
                "canRetry": True,
                "metrics": {"facts": _limit_payload(getattr(exc, "facts", None))},
            }
    payload = _as_payload(result)
    committed = _collapsed_epoch_progress_steps(
        [
            type(
                "ProbeProgressMember",
                (),
                {"progress_steps": tuple(captured)},
            )()
        ]
    )
    after_fingerprint = weld.authority.fingerprint(weld.body.get_state())
    material_changed = bool(before_fingerprint and after_fingerprint and before_fingerprint != after_fingerprint)
    try:
        weld.authority.commit_steps(
            committed,
            weld.goal_text,
            material_changed=material_changed,
        )
    except ProgressAbort as exc:
        if pending_abort is None:
            pending_abort = exc
    commit = {
        "captured_progress_step_count": len(captured),
        "committed_progress_step_count": len(committed),
        "material_changed": material_changed,
        "pending_abort": None
        if pending_abort is None
        else {
            "type": type(pending_abort).__name__,
            "message": str(pending_abort),
            "facts": _limit_payload(getattr(pending_abort, "facts", None)),
        },
    }
    if pending_abort is not None and payload.get("reason") != "progress_yielded":
        raise pending_abort
    return payload, commit


def _nearest_blocks(body: ScarpetBody, *, label: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        perception = body.perceive("findBlocks", params)
    except Exception as exc:
        return {
            "label": label,
            "ok": False,
            "complete": False,
            "error": str(exc),
            "error_type": type(exc).__name__,
            "params": params,
            "count": 0,
            "nearest": [],
        }
    data = dict(perception.data or {})
    blocks = data.get("blocks")
    block_list = [block for block in blocks if isinstance(block, dict)] if isinstance(blocks, list) else []
    state = body.get_state()
    pos = state.pos
    ranked: list[tuple[float, dict[str, Any]]] = []
    for block in block_list:
        try:
            x = float(block["x"])
            y = float(block["y"])
            z = float(block["z"])
        except (KeyError, TypeError, ValueError):
            continue
        ranked.append((math.dist((float(pos[0]), float(pos[1]), float(pos[2])), (x, y, z)), block))
    ranked.sort(key=lambda item: item[0])
    return {
        "label": label,
        "ok": perception.ok,
        "complete": perception.complete,
        "error": perception.error,
        "uncertainty": list(perception.uncertainty),
        "params": params,
        "count": len(block_list),
        "nearest": [
            {
                "distance": round(distance, 3),
                "pos": [block.get("x"), block.get("y"), block.get("z")],
                "type": block.get("type"),
            }
            for distance, block in ranked[:8]
        ],
    }


def _nearest_entities(rcon: RconClient, *, kinds: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for kind in kinds:
        expr = (
            f"(es = entity_selector('@e[type=minecraft:{kind}]');"
            f" best=null; nd=1e18;"
            f" for(es, p=query(_,'pos'); d=(p:0)*(p:0)+(p:1-70)*(p:1-70)+(p:2)*(p:2);"
            f"   if(d<nd, nd=d; best=l(round(p:0),round(p:1),round(p:2))));"
            f" l(length(es), best, if(best==null,-1,round(sqrt(nd)))))"
        )
        try:
            out[kind] = rcon.request(f"script in minebot run {expr}")
        except Exception as exc:
            out[kind] = {"ok": False, "error": str(exc), "error_type": type(exc).__name__}
    return out


def _survey(body: ScarpetBody, rcon: RconClient, *, radius: int) -> dict[str, Any]:
    return {
        f"logs_r{radius}": _nearest_blocks(
            body,
            label=f"logs_r{radius}",
            params={"types": LOG_SURVEY_TYPES, "radius": radius, "y_radius": min(40, radius), "limit": 8},
        ),
        f"flowers_r{radius}": _nearest_blocks(
            body,
            label=f"flowers_r{radius}",
            params={"types": FLOWER_SURVEY_TYPES, "radius": radius, "y_radius": min(40, radius), "limit": 8},
        ),
        "entities": _nearest_entities(rcon, kinds=M8_ENTITY_TARGETS),
    }


def _event_summary(body: ScarpetBody) -> dict[str, Any]:
    snapshot = body.observability_snapshot(max_events=512, max_traces=256, max_requests=256)
    events = snapshot.get("events") or []
    counts: dict[str, int] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        name = str(event.get("name") or "")
        counts[name] = counts.get(name, 0) + 1
    return {
        "event_counts": counts,
        "transport": snapshot.get("transport"),
        "recent_events": _limit_payload(events[-32:]),
    }


def _classify(report: dict[str, Any]) -> dict[str, Any]:
    calls = report["calls"]
    by_label = {call["label"]: call for call in calls}
    total_delta = sum(int(call.get("yardstick_delta_points") or 0) for call in calls)
    final_points = int(report["final_yardstick"]["points"])
    total_target_facts = sum(int(call.get("target_fact_count") or 0) for call in calls)
    reasons = [str(call.get("result", {}).get("reason") or "") for call in calls]
    candidate_reasons = [
        reason
        for call in calls
        for reason in call.get("candidate_failure_reasons") or []
    ]
    final_displacement = _horizontal_distance(report["start_state"]["pos"], report["final_state"]["pos"])
    first_output_call = next((call for call in calls if int(call.get("yardstick_delta_points") or 0) > 0), None)
    first_output_elapsed = None
    if first_output_call is not None:
        first_output_elapsed = sum(float(call.get("elapsed_s") or 0.0) for call in calls[: calls.index(first_output_call) + 1])

    first_collect = by_label.get("m8_first_collect_oak_log")
    first_collect_reason = str((first_collect or {}).get("result", {}).get("reason") or "")
    first_collect_body_reason = ""
    if first_collect:
        metrics = first_collect.get("result", {}).get("metrics")
        body_process = metrics.get("body_process") if isinstance(metrics, dict) else None
        if isinstance(body_process, dict):
            first_collect_body_reason = str(body_process.get("reason") or "")

    recovery_like_failures = [
        reason
        for reason in reasons + candidate_reasons
        if reason in {"mobility_blocked", "budget_exceeded", "no_path"}
        or "no_path" in reason
        or "recovery_exhausted" in reason
    ]

    if total_delta > 0 and first_output_elapsed is not None and first_output_elapsed <= 900.0:
        verdict = "tooling_available"
        reason = "production_tool_sequence_can_reach_early_output_within_quality_window"
    elif total_target_facts > 0 and total_delta == 0:
        verdict = "tool_contract_or_followup_gap"
        reason = "exploration_found_target_facts_but_sequence_did_not_convert_them_to_output"
    elif recovery_like_failures and final_displacement >= 16.0:
        verdict = "frontier_recovery_or_mobility_gap"
        reason = "body_moved_but_recovery_frontiers_did_not_return_to_output"
    elif recovery_like_failures:
        verdict = "start_mobility_gap"
        reason = "early_sequence_hit_mobility_failures_without_enough displacement"
    elif first_collect_reason == "target_not_found" and first_collect_body_reason == "resource_candidates_not_found":
        verdict = "initial_radius_discovery_gap"
        reason = "local_collect_radius_has_no_log_candidates_at_golden_spawn"
    else:
        verdict = "unclassified"
        reason = "probe_needs_deeper_trace_or_model_sequencing_audit"

    return {
        "verdict": verdict,
        "reason": reason,
        "total_delta_points": total_delta,
        "final_yardstick_points": final_points,
        "first_output_label": None if first_output_call is None else first_output_call["label"],
        "first_output_elapsed_s": None if first_output_elapsed is None else round(first_output_elapsed, 3),
        "total_target_fact_count": total_target_facts,
        "final_horizontal_displacement": round(final_displacement, 3),
        "tool_reasons": reasons,
        "candidate_failure_reasons": candidate_reasons,
        "evidence_limits": [
            "This is bounded Debug Reset evidence only, not a Q4 rehearsal or Q5 gate.",
            "Historical tool parameters are replayed only to classify the M8 blocker.",
            "A tooling_available verdict does not pass Q4; it redirects work to model/tool contract evidence.",
            "A mechanism-gap verdict must be fixed with provider-local general logic, not long-run patching.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--include-wide-explore", action="store_true")
    parser.add_argument("--skip-survey", action="store_true")
    parser.add_argument("--survey-radius", type=int, default=64)
    args = parser.parse_args()

    started_at = time.time()
    trace_events: list[dict[str, Any]] = []

    def emit_trace(event: str, payload: dict[str, object]) -> None:
        trace_events.append(
            {
                "seq": len(trace_events) + 1,
                "ts": time.time(),
                "event": event,
                **_jsonable(payload),
            }
        )

    with connect_or_skip(RconConfig()) as rcon:
        world_id = ensure_world_identity(rcon)
        for command in (
            "script unload minebot",
            "script load minebot global",
            "carpet commandPlayer true",
            "carpet allowSpawningOfflinePlayers true",
            "gamerule spawn_mobs false",
            "gamerule doMobSpawning false",
            "gamerule advance_time false",
            "gamerule advance_weather false",
            "time set day",
            "weather clear",
            "difficulty peaceful",
            "kill @e[type=minecraft:item]",
            f"player {BOT} kill",
        ):
            _command(rcon, command)

        body = ScarpetBody(BOT, rcon)
        spawn_or_fail(body, START, timeout_s=30.0)
        _command(rcon, f"tp {BOT} {START[0]} {START[1]} {START[2]} 0 0")
        _command(rcon, f"gamemode survival {BOT}")
        _command(rcon, f"clear {BOT}")
        _command(rcon, "script in minebot run minebot_reset()")
        time.sleep(0.2)

        authority = ProgressAuthority()
        registry = build_phase1_registry(
            body,
            Phase1RuntimeConfig(natural_region=NATURAL_REGION),
            authority=authority,
        )
        weld = WeldContext(
            body=body,
            authority=authority,
            goal_text=(
                "Q4 bounded Debug Reset: classify early-output and natural-obstacle recovery "
                "from the golden spawn without model guidance or fixed routes."
            ),
        )
        mode_runtime = ModeRuntime()
        context = CompositionContext(
            registry=registry,
            weld_context=weld,
            runtime_profile=mode_runtime.profile_for(LifecycleState.ACTIVE),
            budget=Phase1RuntimeConfig(natural_region=NATURAL_REGION).budget,
            recipe_lookup=_recipe_lookup(body),
            trace=emit_trace,
        )
        register_collect_resource_tool(registry, context)
        register_ensure_tool_for_tool(registry, context, _recipe_lookup(body))

        start_state = _state_payload(body)
        start_counts = _inventory_counts(body)
        start_yardstick = _yardstick_points(start_counts, _equipment(body))
        survey_radius = max(16, min(int(args.survey_radius), 128))
        start_survey = (
            {"skipped": True}
            if args.skip_survey
            else _survey(body, rcon, radius=survey_radius)
        )

        calls: list[dict[str, Any]] = []
        calls.append(
            _run_tool(
                registry=registry,
                weld=weld,
                body=body,
                label="m8_first_collect_oak_log",
                tool="collect_resource",
                params=dict(FIRST_COLLECT),
            )
        )
        calls.append(
            _run_tool(
                registry=registry,
                weld=weld,
                body=body,
                label="m8_first_explore",
                tool="explore_for",
                params=dict(FIRST_EXPLORE),
            )
        )
        calls.append(
            _run_tool(
                registry=registry,
                weld=weld,
                body=body,
                label="m8_first_search_logs_flowers",
                tool="search_for_block",
                params=dict(FIRST_SEARCH),
            )
        )

        resume_params = _resume_params(calls[-2]["result"], FIRST_EXPLORE)
        if resume_params is not None:
            calls.append(
                _run_tool(
                    registry=registry,
                    weld=weld,
                    body=body,
                    label="m8_resume_explore",
                    tool="explore_for",
                    params=resume_params,
                )
            )

        calls.append(
            _run_tool(
                registry=registry,
                weld=weld,
                body=body,
                label="m8_second_collect_oak_log",
                tool="collect_resource",
                params=dict(SECOND_COLLECT),
            )
        )
        if args.include_wide_explore:
            calls.append(
                _run_tool(
                    registry=registry,
                    weld=weld,
                    body=body,
                    label="m8_wide_explore",
                    tool="explore_for",
                    params=dict(WIDE_EXPLORE),
                )
            )
            calls.append(
                _run_tool(
                    registry=registry,
                    weld=weld,
                    body=body,
                    label="m8_wide_search_logs",
                    tool="search_for_block",
                    params=dict(WIDE_SEARCH),
                )
            )

        final_state = _state_payload(body)
        final_counts = _inventory_counts(body)
        final_yardstick = _yardstick_points(final_counts, _equipment(body))
        final_survey = (
            {"skipped": True}
            if args.skip_survey
            else _survey(body, rcon, radius=survey_radius)
        )
        report: dict[str, Any] = {
            "schema_version": 1,
            "scope": "Q4_M8_early_output_recovery_debug_reset",
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
            "m8_reference": {
                "run_dir": "logs/agentic-runtime/ag-q4-m8-20260724-hard-terrain-fixed",
                "blocker": "no_output_window_exceeded:1423.97s and recovery fail 0/11/9",
            },
            "start_state": start_state,
            "start_inventory": start_counts,
            "start_yardstick": start_yardstick,
            "start_survey": start_survey,
            "calls": calls,
            "final_state": final_state,
            "final_inventory": final_counts,
            "final_yardstick": final_yardstick,
            "final_survey": final_survey,
            "composition_trace": _limit_payload(trace_events, max_list=128),
            "event_summary": _event_summary(body),
            "elapsed_wall_s": round(time.time() - started_at, 3),
        }
        report["classification"] = _classify(report)

        try:
            _command(rcon, f"player {BOT} kill", delay_s=0.2)
            _command(rcon, "kill @e[type=minecraft:item]", delay_s=0.05)
        finally:
            pass

    output = args.output or ROOT / "logs" / "agentic-runtime" / f"q4-early-output-recovery-{int(started_at)}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
