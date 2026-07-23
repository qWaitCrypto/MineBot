#!/usr/bin/env python3
"""Bounded Q4 Debug Reset probe for the M7 hard-terrain recovery blocker.

This is not AG-FP30/Q4/Q5 evidence. It recreates the late M7 state where the
Agent was near lava around ``[-42.7, 64, -25.49]`` and repeated nearby
``move_to`` / ``explore_for`` / ``move_away`` attempts returned ``no_path`` or
``mobility_blocked`` with little or no expansion.

The probe answers a narrower question: is the blocker reproducible at the Body
mechanism layer, and does Scarpet classify the start/neighbor/nearby-goal graph
as clipped before the model enters the loop again?
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_registry  # noqa: E402
from minebot.brain.progress import ProgressAuthority  # noqa: E402
from minebot.brain.registry import WeldContext, execute_tool  # noqa: E402
from minebot.contract import Region  # noqa: E402
from minebot.game import RconClient, ScarpetBody  # noqa: E402
from minebot.game.errors import RconError  # noqa: E402
from minebot.game.rcon import RconConfig  # noqa: E402
from tests.e2e_support import SKIP_EXIT_CODE, spawn_or_fail  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
BOT = "Q4HardProbe"
START = (-43, 64, -26)
DANGER = (-42, 63, -25)
NEAR_GOAL = (-35, 64, -25)
FAR_GOAL = (-55, 64, -26)
NATURAL_REGION = Region("golden-natural", (-256, 0, -256), (256, 128, 256))

MOVE_CONTEXT = "{'allow_swim'->true,'aquatic_replan_attempts'->2,'allow_break'->true,'break_budget'->8,'allow_open'->true,'open_budget'->8}"
GOVERNED_CONTEXT = (
    "{'allow_swim'->true,'aquatic_traversal'->true,"
    "'allow_break'->true,'break_budget'->8,'break_pickaxe'->'stone_pickaxe',"
    "'allow_place'->true,'place_budget'->8,'scaffold_item'->'cobblestone','scaffold_count'->64,"
    "'allow_pillar'->true,'pillar_budget'->8,"
    "'allow_downward'->true,'downward_budget'->8}"
)


def _command(rcon: RconClient, command: str, *, delay_s: float = 0.05) -> str:
    result = rcon.command(command)
    if delay_s:
        time.sleep(delay_s)
    return result


def _value(raw: str) -> str:
    if " = " in raw:
        return raw.split(" = ", 1)[1].rsplit(" (", 1)[0].strip()
    return raw.strip()


def _script(rcon: RconClient, expr: str) -> str:
    return _value(rcon.request(f"script in minebot run {expr}"))


def _jsonable(value: Any) -> Any:
    if hasattr(value, "to_payload"):
        return _jsonable(value.to_payload())
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def _compact_result(payload: dict[str, Any]) -> dict[str, Any]:
    metrics = payload.get("metrics")
    metrics = metrics if isinstance(metrics, dict) else {}
    compact_metrics: dict[str, Any] = {}
    for key in (
        "goal",
        "goal_dist",
        "navigation_goal",
        "segment_count",
        "budget",
        "initial_distance",
        "required_distance",
        "hazard_radius",
        "desired_distance",
        "error_type",
        "message",
    ):
        if key in metrics:
            compact_metrics[key] = metrics[key]
    segments = [_compact_segment(item) for item in metrics.get("segments") or [] if isinstance(item, dict)]
    if segments:
        compact_metrics["segments"] = segments[:8]
    attempts = [_compact_move_away_attempt(item) for item in metrics.get("attempts") or [] if isinstance(item, dict)]
    if attempts:
        compact_metrics["attempts"] = attempts[:8]
    failures = [_compact_candidate_failure(item) for item in metrics.get("candidate_failures") or [] if isinstance(item, dict)]
    if failures:
        compact_metrics["candidate_failures"] = failures[:8]
    for key in ("blocks", "entities"):
        value = metrics.get(key)
        if isinstance(value, list):
            compact_metrics[key] = value[:8]
    return {
        "success": payload.get("success"),
        "reason": payload.get("reason"),
        "canRetry": payload.get("canRetry"),
        "nextSuggestion": payload.get("nextSuggestion"),
        "metrics": compact_metrics,
    }


def _compact_segment(segment: dict[str, Any]) -> dict[str, Any]:
    diagnostics = segment.get("diagnostics")
    diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
    event_data = diagnostics.get("event_data")
    event_data = event_data if isinstance(event_data, dict) else {}
    return {
        "index": segment.get("index"),
        "status": segment.get("status"),
        "success": segment.get("success"),
        "target": segment.get("target"),
        "terminal_reason": segment.get("terminal_reason"),
        "expanded": diagnostics.get("expanded"),
        "raw_reason": diagnostics.get("raw_reason"),
        "selected_goal": diagnostics.get("selected_goal"),
        "partial_distance": diagnostics.get("partial_distance"),
        "movement_counts": event_data.get("movement_counts") or diagnostics.get("movement_counts"),
        "path_length": _first_event_field(diagnostics, "path_length"),
    }


def _first_event_field(diagnostics: dict[str, Any], key: str) -> Any:
    for event in diagnostics.get("navigation_events") or []:
        if not isinstance(event, dict):
            continue
        data = event.get("data")
        if isinstance(data, dict) and key in data:
            return data[key]
    return None


def _compact_move_away_attempt(attempt: dict[str, Any]) -> dict[str, Any]:
    result = attempt.get("result")
    result = result if isinstance(result, dict) else {}
    metrics = result.get("metrics")
    metrics = metrics if isinstance(metrics, dict) else {}
    return {
        "check": attempt.get("check"),
        "chosen_goal": attempt.get("chosen_goal"),
        "initial_distance": attempt.get("initial_distance"),
        "final_distance": attempt.get("final_distance"),
        "required_distance": attempt.get("required_distance"),
        "result": {
            "success": result.get("success"),
            "reason": result.get("reason"),
            "segment_count": metrics.get("segment_count"),
            "segments": [_compact_segment(item) for item in metrics.get("segments") or [] if isinstance(item, dict)][:4],
        },
    }


def _compact_candidate_failure(failure: dict[str, Any]) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    for attempt in failure.get("navigation_attempts") or []:
        if not isinstance(attempt, dict):
            continue
        egress = attempt.get("mobility_egress")
        egress = egress if isinstance(egress, dict) else {}
        attempts.append(
            {
                "success": attempt.get("success"),
                "reason": attempt.get("reason"),
                "mode": attempt.get("mode"),
                "selected_goal": attempt.get("selected_goal"),
                "stand_count": len(attempt.get("stands") or []),
                "mobility_egress_reason": egress.get("reason"),
            }
        )
    return {
        "region": failure.get("region"),
        "reason": failure.get("reason"),
        "candidate_stands": failure.get("candidate_stands"),
        "navigation_attempts": attempts[:4],
    }


def _block_fact(body: ScarpetBody, pos: tuple[int, int, int]) -> dict[str, Any]:
    result = body.perceive("blockAt", {"x": pos[0], "y": pos[1], "z": pos[2]})
    return {
        "pos": list(pos),
        "ok": result.ok,
        "data": dict(result.data or {}),
        "error": result.error,
    }


def _state(body: ScarpetBody) -> dict[str, Any]:
    state = body.get_state()
    return {
        "pos": [round(float(item), 3) for item in state.pos],
        "missing": state.missing,
        "health": state.health,
        "food": state.food,
        "oxygen": state.oxygen,
        "inventory_counts": dict(state.inventory_counts or {}),
        "selected_item": state.selected_item,
        "offhand_item": state.offhand_item,
        "body_owner": state.body_owner,
        "pending_action_count": state.pending_action_count,
    }


def _reset_bot(body: ScarpetBody, rcon: RconClient, *, give_tools: bool = False) -> dict[str, Any]:
    _command(rcon, f"player {BOT} kill", delay_s=0.1)
    spawn_or_fail(body, START, timeout_s=15.0)
    _command(rcon, f"gamemode survival {BOT}")
    _command(rcon, f"clear {BOT}")
    if give_tools:
        _command(rcon, f"give {BOT} minecraft:cobblestone 64")
        _command(rcon, f"give {BOT} minecraft:stone_pickaxe 1")
    time.sleep(0.2)
    return _state(body)


def _plan_probe(rcon: RconClient, *, context: str, goal: tuple[int, int, int], radius: int) -> str:
    sx, sy, sz = START
    gx, gy, gz = goal
    expr = (
        f"(ctx = navigation_context_from_params({context});"
        f" plan = navigate_to_goals_plan({sx},{sy},{sz},l(l({gx},{gy},{gz},{radius})),"
        f" 48, 1200, 8, 8, null, 1, ctx);"
        f" str('status=%s|steps=%s|expanded=%s|selected=%s|partial_dist=%s',"
        f" plan:1, length(plan:3), plan:2, plan:4, plan:6))"
    )
    return _script(rcon, expr)


def _neighbor_probe(rcon: RconClient, *, context: str) -> str:
    sx, sy, sz = START
    expr = (
        f"(ctx = navigation_context_from_params({context});"
        f" n = navigation_neighbors({sx},{sy},{sz},ctx);"
        f" str('count=%s|neighbors=%s', length(n), n))"
    )
    return _script(rcon, expr)


def _run_tool(
    *,
    label: str,
    body: ScarpetBody,
    registry,
    weld: WeldContext,
    tool: str,
    params: dict[str, Any],
    give_tools: bool = False,
    rcon: RconClient,
) -> dict[str, Any]:
    before = _reset_bot(body, rcon, give_tools=give_tools)
    started = time.monotonic()
    try:
        result = execute_tool(registry.get(tool), params, weld)
        payload = _compact_result(_jsonable(result))
    except Exception as exc:
        payload = {"success": False, "reason": "exception", "metrics": {"error_type": type(exc).__name__, "message": str(exc)}}
    elapsed = time.monotonic() - started
    after = _state(body)
    return {
        "label": label,
        "tool": tool,
        "params": params,
        "before_state": before,
        "after_state": after,
        "elapsed_s": round(elapsed, 3),
        "result": payload,
    }


def _classify(report: dict[str, Any]) -> dict[str, Any]:
    tool_runs = report["tool_runs"]
    reasons = [str(run.get("result", {}).get("reason") or "") for run in tool_runs]
    reproduced = (
        any(reason == "no_path" for reason in reasons)
        and any("mobility_blocked" in reason for reason in reasons)
        and any("move_away_failed:no_path" in reason for reason in reasons)
    )
    neighbor_text = " ".join(str(item.get("raw") or "") for item in report["navigation"]["neighbors"].values())
    start_walkability = report["navigation"]["walkability"].get("start")
    start_clipped = "count=0" in neighbor_text or start_walkability in {"LAVA", "SOLID", "NO_FLOOR"}
    return {
        "verdict": "reproduced" if reproduced else "not_reproduced",
        "reason": "m7_hard_terrain_no_path_reproduced" if reproduced else "m7_signature_not_reproduced",
        "start_walkability": start_walkability,
        "start_or_neighbors_clipped": start_clipped,
        "tool_reasons": reasons,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    try:
        rcon = RconClient(RconConfig())
        rcon.connect()
    except (OSError, PermissionError, RconError) as exc:
        if os.environ.get("MINEBOT_E2E_REQUIRED") == "1":
            raise
        print(f"SKIP: local RCON unavailable: {type(exc).__name__}: {exc}")
        raise SystemExit(SKIP_EXIT_CODE)

    with rcon:
        for command in (
            "script unload minebot",
            "script load minebot global",
            "carpet commandPlayer true",
            "carpet allowSpawningOfflinePlayers true",
            "gamerule doDaylightCycle false",
            "time set day",
            "weather clear",
            "kill @e[type=minecraft:item]",
            f"player {BOT} kill",
        ):
            _command(rcon, command)

        body = ScarpetBody(BOT, rcon)
        registry = build_phase1_registry(body, Phase1RuntimeConfig(natural_region=NATURAL_REGION))
        weld = WeldContext(body=body, authority=ProgressAuthority(), goal_text="debug hard terrain recovery")
        _reset_bot(body, rcon)

        sx, sy, sz = START
        block_positions = [
            START,
            (sx, sy + 1, sz),
            (sx, sy - 1, sz),
            DANGER,
            (sx + 1, sy, sz),
            (sx - 1, sy, sz),
            (sx, sy, sz + 1),
            (sx, sy, sz - 1),
        ]
        navigation = {
            "walkability": {
                "start": _script(rcon, f"probe_walkability({sx},{sy},{sz})"),
                "danger": _script(rcon, f"probe_walkability({DANGER[0]},{DANGER[1]},{DANGER[2]})"),
                "lava_unsafe_start": _script(rcon, f"navigation_lava_unsafe({sx},{sy},{sz})"),
                "lava_unsafe_danger": _script(rcon, f"navigation_lava_unsafe({DANGER[0]},{DANGER[1]},{DANGER[2]})"),
            },
            "neighbors": {
                "move_context": {"raw": _neighbor_probe(rcon, context=MOVE_CONTEXT)},
                "governed_context": {"raw": _neighbor_probe(rcon, context=GOVERNED_CONTEXT)},
            },
            "plans": {
                "near_move_context": _plan_probe(rcon, context=MOVE_CONTEXT, goal=NEAR_GOAL, radius=4),
                "far_move_context": _plan_probe(rcon, context=MOVE_CONTEXT, goal=FAR_GOAL, radius=4),
                "near_governed_context": _plan_probe(rcon, context=GOVERNED_CONTEXT, goal=NEAR_GOAL, radius=4),
                "far_governed_context": _plan_probe(rcon, context=GOVERNED_CONTEXT, goal=FAR_GOAL, radius=4),
            },
        }

        tool_runs = [
            _run_tool(
                label="near_move_to",
                body=body,
                registry=registry,
                weld=weld,
                tool="move_to",
                params={"pos": list(NEAR_GOAL), "radius": 4, "timeout_s": 60},
                rcon=rcon,
            ),
            _run_tool(
                label="far_move_to",
                body=body,
                registry=registry,
                weld=weld,
                tool="move_to",
                params={"pos": list(FAR_GOAL), "radius": 4, "timeout_s": 60},
                rcon=rcon,
            ),
            _run_tool(
                label="explore_logs_animals",
                body=body,
                registry=registry,
                weld=weld,
                tool="explore_for",
                params={
                    "block_targets": ["spruce_log", "oak_log", "birch_log"],
                    "entity_targets": ["pig", "cow", "sheep"],
                    "max_distance": 128,
                    "max_regions": 8,
                    "return_policy": "first_match",
                    "scan_radius": 16,
                },
                rcon=rcon,
            ),
            _run_tool(
                label="move_away_from_lava",
                body=body,
                registry=registry,
                weld=weld,
                tool="move_away",
                params={
                    "danger_pos": list(DANGER),
                    "hazard_radius": 4,
                    "maintenance_checks": 4,
                    "min_distance": 6,
                },
                rcon=rcon,
            ),
            _run_tool(
                label="governed_explore_with_scaffold",
                body=body,
                registry=registry,
                weld=weld,
                tool="explore_for",
                params={
                    "block_targets": ["spruce_log", "oak_log", "birch_log"],
                    "max_distance": 128,
                    "max_regions": 8,
                    "return_policy": "first_match",
                    "scan_radius": 16,
                },
                give_tools=True,
                rcon=rcon,
            ),
        ]

        report: dict[str, Any] = {
            "schema_version": 1,
            "scope": "Q4_M7_hard_terrain_recovery_debug_reset",
            "bounded": True,
            "formal_gate": False,
            "bot": BOT,
            "world_fixture": "world-golden",
            "start": list(START),
            "danger": list(DANGER),
            "near_goal": list(NEAR_GOAL),
            "far_goal": list(FAR_GOAL),
            "block_facts": [_block_fact(body, pos) for pos in block_positions],
            "navigation": navigation,
            "tool_runs": tool_runs,
            "evidence_limits": [
                "This is a bounded Debug Reset probe, not a production long-run gate.",
                "It uses historical coordinates only in the probe artifact; production code must not read them.",
                "Reproducing no_path/mobility_blocked is evidence for Q4 Debug Reset, not success.",
            ],
        }
        report["classification"] = _classify(report)

        output = args.output or ROOT / "logs" / "agentic-runtime" / f"q4-hard-terrain-recovery-{int(time.time())}.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        _command(rcon, f"player {BOT} kill", delay_s=0.1)
        _command(rcon, "kill @e[type=minecraft:item]", delay_s=0.05)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
