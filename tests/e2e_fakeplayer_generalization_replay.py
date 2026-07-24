#!/usr/bin/env python3
"""No-model Q0 replay for the water/vertical/governance intersection.

The scene is deliberately small and deterministic. It records the three
co-occurring facts before calling the normal production resource transaction:
water separates the body from the tree domain, the target logs occupy a raised
vertical trunk, and a natural-looking stone barrier is denied by governance.
The resource result remains an honest Body result; this probe never turns it
into an Agent success or changes the production planner.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.body import BlockWork, VoxelStructureRiskAssessor  # noqa: E402
from minebot.contract import BreakContext  # noqa: E402
from minebot.game import GovernancePolicy, RconClient, ScarpetBody  # noqa: E402
from tests.e2e_agent_collect_resource import REGION, inventory_count, make_registry  # noqa: E402
from tests.e2e_support import connect_or_skip, spawn_or_fail  # noqa: E402


BOT = "Q0CompositeProbe"
START = (-1, 70, 0)
WATER = [(x, 70, z) for x in range(1, 7) for z in range(-2, 3)]
TREE = [(10, 73, 0), (10, 74, 0), (10, 75, 0)]
GOVERNANCE_TARGET = (7, 70, 0)


def command(rcon: RconClient, text: str, delay: float = 0.05) -> str:
    output = rcon.command(text)
    if delay:
        time.sleep(delay)
    return output


def setup_scene(rcon: RconClient) -> None:
    for text in (
        "script unload minebot",
        "script load minebot global",
        "carpet commandPlayer true",
        "carpet allowSpawningOfflinePlayers true",
        "gamerule doDaylightCycle false",
        "gamerule doWeatherCycle false",
        "gamerule doMobSpawning false",
        "time set day",
        "weather clear",
        "difficulty normal",
        f"player {BOT} kill",
        "forceload add 1 -64 6 64",
        "fill -10 70 -64 16 78 64 air",
        "fill -10 69 -64 16 69 64 stone",
        "script in minebot run minebot_reset()",
    ):
        command(rcon, text)

    # The water band spans the whole bounded search width.  There is no dry
    # route around it inside the provider's grid, so dry-first must terminate
    # with zero progress before the governed mobility profile is tried.
    command(rcon, "fill 1 70 -64 6 70 64 water", delay=0.0)
    # Keep fluid updates from flooding the dry starting stand.  The provider
    # must route around this strongly protected block before entering water.
    command(rcon, "setblock 0 70 0 oak_planks", delay=0.0)

    # A raised natural bank and a vertical trunk give the candidate selector a
    # valid target above the water exit without requiring a coordinate/tree
    # special case in the production planner.
    command(rcon, "fill 8 70 -2 12 71 2 dirt", delay=0.0)
    for x, y, z in TREE:
        command(rcon, f"setblock {x} {y} {z} oak_log", delay=0.0)
    command(rcon, "setblock 10 72 0 dirt", delay=0.0)
    for text in (
        f"setblock {GOVERNANCE_TARGET[0]} {GOVERNANCE_TARGET[1]} {GOVERNANCE_TARGET[2]} stone",
        "setblock 7 70 1 oak_planks",
        "setblock 8 70 -1 andesite",
        "setblock 7 71 0 stone",
    ):
        command(rcon, text, delay=0.0)


def read_types(body: ScarpetBody, positions: list[tuple[int, int, int]]) -> dict[str, str]:
    facts: dict[str, str] = {}
    start = 0
    for _page in range(4):
        perception = body.perceive(
            "blockCells",
            {"cells": [list(pos) for pos in positions], "start": start, "limit": 24},
        )
        if not perception.ok:
            raise AssertionError(f"composite scene read failed: {perception}")
        for item in perception.data.get("cells") or []:
            position = item.get("pos") or [item.get("x"), item.get("y"), item.get("z")]
            facts[":".join(str(value) for value in position)] = str(
                item.get("type") or "unknown"
            ).removeprefix("minecraft:")
        next_cursor = perception.data.get("nextStart")
        if next_cursor is None:
            if not perception.complete:
                raise AssertionError(f"composite scene read incomplete without cursor: {perception}")
            return facts
        next_start = int(next_cursor)
        if next_start <= start:
            raise AssertionError(f"composite scene read cursor did not advance: {perception}")
        start = next_start
    raise AssertionError(f"composite scene read exceeded replay page budget: {len(facts)}/{len(positions)}")


def navigation_summary(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        return {}
    metrics = payload.get("metrics")
    if not isinstance(metrics, dict) and any(
        key in payload for key in ("movement_counts", "path_length", "waypoints", "final_pos")
    ):
        metrics = payload
    metrics = metrics if isinstance(metrics, dict) else {}
    movement_counts = metrics.get("movement_counts")
    final_pos = metrics.get("final_pos")
    path_length = metrics.get("path_length")
    waypoints = metrics.get("waypoints")
    capability = metrics.get("capability_snapshot")
    segments = metrics.get("segments")
    if isinstance(segments, list):
        aggregate: dict[str, int] = {}
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            diagnostics = segment.get("diagnostics")
            diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
            segment_counts = diagnostics.get("movement_counts")
            if not isinstance(segment_counts, dict):
                event_data = diagnostics.get("event_data")
                segment_counts = event_data.get("movement_counts") if isinstance(event_data, dict) else None
            if isinstance(segment_counts, dict):
                for key, value in segment_counts.items():
                    try:
                        aggregate[str(key)] = aggregate.get(str(key), 0) + int(value or 0)
                    except (TypeError, ValueError):
                        continue
            event_data = diagnostics.get("event_data")
            if isinstance(event_data, dict):
                if final_pos is None and event_data.get("final_pos") is not None:
                    final_pos = event_data.get("final_pos")
                if path_length is None and event_data.get("path_length") is not None:
                    path_length = event_data.get("path_length")
                if waypoints is None and event_data.get("waypoints") is not None:
                    waypoints = event_data.get("waypoints")
            navigation_events = diagnostics.get("navigation_events")
            if isinstance(navigation_events, list):
                for event in navigation_events:
                    event_data = event.get("data") if isinstance(event, dict) else None
                    if not isinstance(event_data, dict):
                        continue
                    if path_length is None and event_data.get("path_length") is not None:
                        path_length = event_data.get("path_length")
                    if waypoints is None and event_data.get("waypoints") is not None:
                        waypoints = event_data.get("waypoints")
            if capability is None and diagnostics.get("capability_snapshot") is not None:
                capability = diagnostics.get("capability_snapshot")
        if movement_counts is None and aggregate:
            movement_counts = aggregate
    capability = capability if isinstance(capability, dict) else {}
    return {
        "success": payload.get("success"),
        "reason": payload.get("reason"),
        "movement_counts": movement_counts,
        "path_length": path_length,
        "waypoints": waypoints,
        "final_pos": final_pos,
        "allow_swim": capability.get("allow_swim"),
        "aquatic_traversal": capability.get("aquatic_traversal"),
    }


def main() -> None:
    with connect_or_skip() as rcon:
        setup_scene(rcon)
        body = ScarpetBody(BOT, rcon)
        spawn_or_fail(body, START)
        # The server only guarantees large fill operations after the fake
        # player has loaded the surrounding chunks.  Re-assert the wall here
        # so the replay's sampled liquid facts and the navigation grid agree.
        command(rcon, "forceload add 1 -64 6 64")
        water_fill = command(rcon, "fill 1 70 -64 6 70 64 water")
        if "filled" not in water_fill.lower():
            raise AssertionError({"water_fill": water_fill})
        command(rcon, f"clear {BOT}")
        command(rcon, f"gamemode survival {BOT}")
        command(rcon, f"item replace entity {BOT} weapon.mainhand with diamond_pickaxe")
        command(rcon, "script in minebot run minebot_reset()")

        scene = read_types(body, WATER + TREE + [GOVERNANCE_TARGET])
        water_present = all(scene.get(":".join(str(value) for value in pos)) == "water" for pos in WATER)
        tree_present = all(scene.get(":".join(str(value) for value in pos)) == "oak_log" for pos in TREE)
        if not water_present or not tree_present:
            raise AssertionError({"water_present": water_present, "tree_present": tree_present, "scene": scene})

        # Governance is still evaluated against the same world snapshot, but
        # the Body must be within interaction range for the result to be about
        # provenance rather than a distance precondition.
        command(rcon, f"tp {BOT} 8 70 0 -90 0")
        command(rcon, "script in minebot run minebot_reset()")
        policy = GovernancePolicy(
            natural_regions=[REGION],
            structure_risk_assessor=VoxelStructureRiskAssessor(body),
            require_structure_assessment=True,
        )
        governance = BlockWork(body, policy).mine_block(
            GOVERNANCE_TARGET,
            context=BreakContext.DIRECT,
            approach=False,
            timeout_s=15.0,
        )
        after_governance = read_types(body, [GOVERNANCE_TARGET])
        governance_type = after_governance[":".join(str(value) for value in GOVERNANCE_TARGET)]
        legality = (governance.metrics or {}).get("legality") if hasattr(governance, "metrics") else None
        if governance.success or governance_type != "stone" or not isinstance(legality, dict) or legality.get("allowed") is not False:
            raise AssertionError({"governance": governance.to_payload(), "after": governance_type})

        command(rcon, f"tp {BOT} {START[0]} {START[1]} {START[2]} -90 0")
        command(rcon, "script in minebot run minebot_reset()")
        registry, context = make_registry(body, protected=True)
        result = registry.get("collect_resource").callable(
            {"item": "logs", "count": 1, "constraints": {"radius": 16, "max_candidates": 8, "max_mutating_calls": 8}}
        )
        payload = result.to_payload() if hasattr(result, "to_payload") else result
        event_head = body.event_head("q0-composite-replay")
        resource_metrics = payload.get("metrics") if isinstance(payload, dict) else {}
        last_failure = resource_metrics.get("last_failure") if isinstance(resource_metrics, dict) else {}
        attempts = resource_metrics.get("attempts") if isinstance(resource_metrics, dict) else []
        body_process = resource_metrics.get("body_process") if isinstance(resource_metrics, dict) else {}
        body_process = body_process if isinstance(body_process, dict) else {}
        body_process_metrics = body_process.get("metrics")
        body_process_metrics = body_process_metrics if isinstance(body_process_metrics, dict) else {}
        navigation_attempts = []
        if isinstance(attempts, list):
            for attempt in attempts:
                if not isinstance(attempt, dict):
                    continue
                navigation = attempt.get("navigation")
                fallback = attempt.get("navigation_fallback")
                navigation_attempts.append(
                    {
                        "profile": attempt.get("navigation_profile"),
                        "selected_goal": attempt.get("selected_goal"),
                        "dry_reason": (
                            ((fallback or {}).get("dry_result") or {}).get("reason")
                            if isinstance(fallback, dict)
                            else (navigation or {}).get("reason")
                            if isinstance(navigation, dict)
                            else None
                        ),
                        "fallback_attempted": isinstance(fallback, dict),
                        "fallback_reason": (
                            ((fallback or {}).get("result") or {}).get("reason")
                            if isinstance(fallback, dict)
                            else None
                        ),
                        "dry_navigation": navigation_summary(
                            (fallback or {}).get("dry_result")
                            if isinstance(fallback, dict)
                            else navigation
                        ),
                        "fallback_navigation": navigation_summary(
                            (fallback or {}).get("result") if isinstance(fallback, dict) else None
                        ),
                    }
                )
        final_scene = read_types(body, TREE)
        final_tree = {
            ":".join(str(value) for value in pos): final_scene.get(":".join(str(value) for value in pos))
            for pos in TREE
        }
        final_oak_logs = sum(1 for block_type in final_tree.values() if block_type == "oak_log")
        fallback_count = body_process_metrics.get("navigation_fallback_attempts")
        dry_reason = str(navigation_attempts[0].get("dry_reason") or "") if navigation_attempts else ""
        if dry_reason.startswith("recovery_exhausted:"):
            dry_reason = dry_reason.split(":", 1)[1]
        if (
            payload.get("success") is not True
            or payload.get("reason") != "collected"
            or fallback_count != 1
            or len(navigation_attempts) != 1
            or dry_reason not in {"no_path", "budget_exceeded"}
            or navigation_attempts[0].get("fallback_attempted") is not True
            or int((navigation_attempts[0].get("dry_navigation") or {}).get("path_length") or 0) != 0
            or any(
                int(value or 0) > 0
                for value in ((navigation_attempts[0].get("dry_navigation") or {}).get("movement_counts") or {}).values()
            )
            or navigation_attempts[0].get("fallback_reason") != "arrived"
            or (navigation_attempts[0].get("fallback_navigation") or {}).get("allow_swim") is not True
            or (navigation_attempts[0].get("fallback_navigation") or {}).get("aquatic_traversal") is not True
            or int(((navigation_attempts[0].get("fallback_navigation") or {}).get("movement_counts") or {}).get("swim") or 0) < 1
            or int(((navigation_attempts[0].get("fallback_navigation") or {}).get("movement_counts") or {}).get("ascend") or 0) < 1
            or final_oak_logs != len(TREE) - 1
            or inventory_count(body, "oak_log") < 1
        ):
            raise AssertionError(
                {
                    "resource_payload": payload,
                    "body_process_metrics": body_process_metrics,
                    "navigation_attempts": navigation_attempts,
                    "final_tree": final_tree,
                    "inventory_oak_log": inventory_count(body, "oak_log"),
                }
            )
        output = {
            "scenario": "r68-water-vertical-governance-intersection",
            "start": list(START),
            "water_cells": len(WATER),
            "tree_cells": TREE,
            "governance": governance.to_payload(),
            "governance_after": governance_type,
            "resource_result": {
                "success": payload.get("success"),
                "reason": payload.get("reason"),
                "collected_delta": resource_metrics.get("collected_delta") if isinstance(resource_metrics, dict) else None,
                "candidates_tried": resource_metrics.get("candidates_tried") if isinstance(resource_metrics, dict) else None,
                "body_reason": last_failure.get("reason") if isinstance(last_failure, dict) else None,
                "body_process_reason": body_process.get("reason"),
                "navigation_fallback_attempts": fallback_count,
                "navigation_attempts": navigation_attempts,
                "navigation_failure_reasons": resource_metrics.get("navigation_failure_reasons") if isinstance(resource_metrics, dict) else None,
                "final_tree": final_tree,
                "inventory_oak_log": inventory_count(body, "oak_log"),
            },
            "owner": event_head.get("owner"),
            "weld_last_action": context.weld_context.authority.last_action,
        }
        print(json.dumps(output, sort_keys=True))
        command(rcon, f"player {BOT} kill", delay=0.2)


if __name__ == "__main__":
    main()
