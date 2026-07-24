#!/usr/bin/env python3
"""Bounded provider-to-Python survival recovery reconciliation probe.

This is mechanism evidence, not an AG-FP30/Q4 material or autonomy gate.  It
injects a real provider hazard latch with no recovery target, changes only the
fixture world to make a safe lane available, and proves the production
``NavigationTransactions`` path:

    unresolved hazard -> provider target refresh -> bounded recovery ->
    authoritative hazard clear -> ordinary navigation handoff

The latch injection is deliberately test-only.  Production code still obtains
the hazard and target from Scarpet and never reconstructs the safety predicate
in Python.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.body.navigation import NavigationRunConfig, NavigationTransactions  # noqa: E402
from minebot.contract import LocalProgressController  # noqa: E402
from minebot.game import RconClient, ScarpetBody  # noqa: E402
from minebot.game.navigation import GoalNear  # noqa: E402
from minebot.game.rcon import RconConfig  # noqa: E402
from tests.e2e_support import connect_or_skip, spawn_or_fail  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
BOT = "Q4HazardRec"
BASE = (700, 70, 0)


def _command(rcon: RconClient, command: str, *, delay_s: float = 0.03) -> str:
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


def _state(body: ScarpetBody) -> dict[str, Any]:
    state = body.get_state()
    return {
        "pos": [round(float(value), 3) for value in state.pos],
        "hazard_unresolved": _jsonable(state.hazard_unresolved),
        "body_owner": state.body_owner,
        "pending_action_count": state.pending_action_count,
        "health": state.health,
    }


def _setup(rcon: RconClient) -> None:
    for command in (
        "script unload minebot",
        "script load minebot global",
        "carpet commandPlayer true",
        "carpet allowSpawningOfflinePlayers true",
        "difficulty peaceful",
        "gamerule doMobSpawning false",
        "gamerule doDaylightCycle false",
        "script in minebot run minebot_reset()",
        f"player {BOT} kill",
        "script in minebot run global_reflex_scan = false",
    ):
        _command(rcon, command)


def _arm_latched_lava(rcon: RconClient, body: ScarpetBody) -> None:
    x, y, z = BASE
    # A solid room with one lava-adjacent spawn cell has no safe target in the
    # provider's bounded search neighborhood.  The lane is opened only after
    # the failed latch is observed.
    _command(rcon, f"fill {x - 8} {y - 1} {z - 4} {x + 8} {y - 1} {z + 4} stone")
    _command(rcon, f"fill {x - 8} {y} {z - 4} {x + 8} {y + 1} {z + 4} stone")
    _command(rcon, f"setblock {x} {y} {z} air")
    _command(rcon, f"setblock {x} {y + 1} {z} air")
    _command(rcon, f"setblock {x - 1} {y} {z} lava")
    _command(rcon, f"tp {BOT} {x} {y} {z} -90 0")
    _command(rcon, f"gamemode survival {BOT}")
    _command(rcon, f"effect give {BOT} minecraft:fire_resistance 60 0 true")
    _command(
        rcon,
        f"script in minebot run global_reflex_failure_latches:'{BOT}' = "
        f"l('lava', {x + 0.5}, {y}, {z + 0.5}, global_tick, null)",
    )
    # Hold the refresh clock at the failed observation so the first state read
    # proves the target was genuinely unavailable, not merely delayed.
    _command(rcon, f"script in minebot run global_reflex_recovery_probe_ticks:'{BOT}' = global_tick")


def _open_recovery_lane(rcon: RconClient) -> None:
    x, y, z = BASE
    for lane_x in range(x + 1, x + 7):
        _command(rcon, f"setblock {lane_x} {y} {z} air", delay_s=0.0)
        _command(rcon, f"setblock {lane_x} {y + 1} {z} air", delay_s=0.0)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    with connect_or_skip(RconConfig()) as rcon:
        _setup(rcon)
        body = ScarpetBody(BOT, rcon)
        spawn_or_fail(body, BASE, timeout_s=15.0)
        try:
            _arm_latched_lava(rcon, body)
            initial = _state(body)
            hazard = initial.get("hazard_unresolved")
            if not isinstance(hazard, dict) or hazard.get("kind") != "lava":
                raise AssertionError(f"fixture did not expose a lava latch: {initial}")
            if hazard.get("recovery_target") is not None:
                raise AssertionError(f"fixture unexpectedly had a recovery target: {initial}")

            _open_recovery_lane(rcon)
            refreshed: dict[str, Any] | None = None
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                candidate = _state(body)
                candidate_hazard = candidate.get("hazard_unresolved")
                if isinstance(candidate_hazard, dict) and candidate_hazard.get("recovery_target") is not None:
                    refreshed = candidate
                    break
                time.sleep(0.1)
            if refreshed is None:
                raise AssertionError(f"provider did not refresh a recovery target: {_state(body)}")

            refreshed_hazard = refreshed["hazard_unresolved"]
            assert isinstance(refreshed_hazard, dict)
            target = tuple(int(value) for value in refreshed_hazard["recovery_target"])
            navigator = NavigationTransactions.server_side(
                body,
                None,
                progress=LocalProgressController(),
            )
            result = navigator.navigate_to(
                GoalNear(target, radius=0),
                config=NavigationRunConfig(
                    max_segments=2,
                    max_partial_segments=2,
                    segment_timeout_s=8.0,
                    server_grid_radius=16,
                    server_max_expand=300,
                    allow_swim=False,
                    allow_break=False,
                    allow_place=False,
                    allow_pillar=False,
                    allow_downward=False,
                    allow_open=False,
                    recovery_attempts=0,
                ),
                timeout_s=20.0,
            )
            final = _state(body)
            if not result.success:
                raise AssertionError(f"ordinary navigation did not resume after recovery: {result.to_payload()}")
            if final["hazard_unresolved"] is not None:
                raise AssertionError(f"recovery returned without authoritative hazard clear: {final}")
            if final["body_owner"] is not None or final["pending_action_count"] not in (None, 0):
                raise AssertionError(f"recovery left an active Body lifecycle: {final}")

            report = {
                "schema_version": 1,
                "scope": "Q4_survival_recovery_reconciliation",
                "bounded": True,
                "formal_gate": False,
                "bot": BOT,
                "initial": initial,
                "refreshed": refreshed,
                "recovery_target": list(target),
                "result": _jsonable(result),
                "final": final,
                "evidence_limits": [
                    "Provider latch injection is test-only; production hazard detection remains server-owned.",
                    "This proves one bounded recovery lifecycle, not Q4 autonomy quality or material output.",
                ],
            }
        finally:
            _command(rcon, f"player {BOT} kill", delay_s=0.0)

    output = args.output or ROOT / "logs" / "agentic-runtime" / f"q4-survival-recovery-{int(time.time())}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
