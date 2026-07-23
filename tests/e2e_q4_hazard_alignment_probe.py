#!/usr/bin/env python3
"""Bounded fixed-world replay for the planner/reflex lava vertical alignment.

The golden-world route contains a liquid navigation cell whose head layer is
inside the feet-centered lava clearance neighborhood.  The planner must reject
that cell with the same predicate used by the survival reflex.  This probe is
mechanism evidence only: a typed route budget failure remains a failure.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.body.navigation import (  # noqa: E402
    NavigationRunConfig,
    NavigationTransactions,
    pure_movement_navigation_config,
)
from minebot.contract import LocalProgressController  # noqa: E402
from minebot.game import RconClient, ScarpetBody  # noqa: E402
from minebot.game.navigation import GoalNear  # noqa: E402
from minebot.game.rcon import RconConfig  # noqa: E402
from tests.e2e_support import connect_or_skip, spawn_or_fail  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
BOT = "Q4HazardAlign"
START = (-40, 63, -27)
TARGET = (-60, 66, -24)
LIQUID_CELL = (-41, 62, -25)
LAVA_CELL = (-42, 63, -25)


def _command(rcon: RconClient, command: str, *, delay_s: float = 0.05) -> str:
    result = rcon.command(command)
    if delay_s:
        time.sleep(delay_s)
    return result


def _block_fact(body: ScarpetBody, pos: tuple[int, int, int]) -> dict[str, object]:
    result = body.perceive("blockAt", {"x": pos[0], "y": pos[1], "z": pos[2]})
    if not result.ok:
        raise AssertionError(f"block fact failed at {pos}: {result}")
    return dict(result.data or {})


def _segments(result: object) -> list[dict[str, object]]:
    payload = result.to_payload()
    metrics = payload.get("metrics") if isinstance(payload, dict) else None
    if not isinstance(metrics, dict):
        return []
    output: list[dict[str, object]] = []
    for segment in metrics.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        diagnostics = segment.get("diagnostics")
        diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
        event_data = diagnostics.get("event_data")
        event_data = event_data if isinstance(event_data, dict) else {}
        output.append(
            {
                "index": segment.get("index"),
                "status": segment.get("status"),
                "success": segment.get("success"),
                "expanded": diagnostics.get("expanded"),
                "raw_reason": diagnostics.get("raw_reason"),
                "movement_counts": event_data.get("movement_counts"),
                "partial_distance": event_data.get("partial_distance"),
            }
        )
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()

    with connect_or_skip(RconConfig()) as rcon:
        for command in (
            "script unload minebot",
            "script load minebot global",
            "carpet commandPlayer true",
            "carpet allowSpawningOfflinePlayers true",
            f"player {BOT} kill",
        ):
            _command(rcon, command)

        body = ScarpetBody(BOT, rcon)
        spawn_or_fail(body, START, timeout_s=15.0)
        try:
            alignment_raw = _command(
                rcon,
                f"script in minebot run navigation_lava_unsafe({LIQUID_CELL[0]}, {LIQUID_CELL[1]}, {LIQUID_CELL[2]})",
            )
            alignment_true = "= true" in alignment_raw.lower()
            liquid_fact = _block_fact(body, LIQUID_CELL)
            lava_fact = _block_fact(body, LAVA_CELL)

            config = pure_movement_navigation_config(
                NavigationRunConfig(
                    max_segments=4,
                    max_partial_segments=4,
                    segment_timeout_s=12.0,
                    server_grid_radius=64,
                    server_max_expand=1200,
                    allow_swim=True,
                    aquatic_traversal=True,
                    recovery_attempts=0,
                )
            )
            navigator = NavigationTransactions.server_side(
                body,
                None,
                progress=LocalProgressController(),
            )
            result = navigator.navigate_to(
                GoalNear(TARGET, radius=0),
                config=config,
                timeout_s=60.0,
            )
            payload = result.to_payload()
            reflex_events = [
                {"name": event.name, "seq": event.seq, "data": dict(event.data)}
                for event in body.event_log
                if event.name in {"ownerPreempted", "reflexTriggered", "reflexCompleted"}
            ]
            if not alignment_true:
                raise AssertionError({"alignment_raw": alignment_raw})
            if any(event["name"] == "ownerPreempted" for event in reflex_events):
                raise AssertionError(
                    "aligned route was still preempted by a survival reflex: "
                    + json.dumps(reflex_events, ensure_ascii=False)
                )

            report = {
                "scope": "Q4_hazard_vertical_alignment",
                "bounded": True,
                "bot": BOT,
                "start": list(START),
                "target": list(TARGET),
                "liquid_cell": list(LIQUID_CELL),
                "lava_cell": list(LAVA_CELL),
                "alignment": {
                    "navigation_lava_unsafe": alignment_true,
                    "raw": alignment_raw,
                    "liquid_fact": liquid_fact,
                    "lava_fact": lava_fact,
                },
                "navigation_profile": {
                    "server_max_expand": config.server_max_expand,
                    "allow_swim": config.allow_swim,
                    "aquatic_traversal": config.aquatic_traversal,
                    "mutation": False,
                },
                "result": payload,
                "segments": _segments(result),
                "final_position": list(body.get_state().pos),
                "reflex_events": reflex_events,
                "evidence_limits": [
                    "This is a fixed-world Body mechanism replay, not an Agent or Q4 material pass.",
                    "budget_exceeded/no_path remains an honest route failure.",
                ],
            }
        finally:
            _command(rcon, f"player {BOT} kill", delay_s=0.2)

    output = arguments.output or ROOT / "logs" / "agentic-runtime" / f"q4-hazard-alignment-{int(time.time())}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
