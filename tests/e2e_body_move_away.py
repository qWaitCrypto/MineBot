#!/usr/bin/env python3
"""move_away transaction e2e against the local Carpet test server."""

from __future__ import annotations

import math
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.body import NavigationRunConfig, NavigationTransactions
from minebot.game import GovernancePolicy, GridCell, GridWorld, NavigationCostModel, RconClient, Region, ScarpetBody
from minebot.game.errors import RconError
from minebot.game.navigation import SegmentedNavigator
from minebot.game.rcon import RconConfig
from tests.e2e_support import spawn_or_fail


BOT = "E2EMoveAwayBot"
SKIP_EXIT_CODE = 77


def command(rcon: RconClient, command: str, delay: float = 0.05) -> str:
    out = rcon.command(command)
    if delay:
        time.sleep(delay)
    return out


def setup_world(rcon: RconClient) -> None:
    for cmd in [
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
        "kill @e[type=!player]",
        f"player {BOT} kill",
        "fill -10 59 -10 10 66 10 air",
        "fill -10 59 -10 10 66 10 air replace water",
        "fill -10 59 -10 10 66 10 air replace flowing_water",
        "fill -10 59 -10 10 66 10 air replace lava",
        "fill -10 59 -10 10 66 10 air replace flowing_lava",
        "fill -10 58 -10 10 58 10 stone",
    ]:
        command(rcon, cmd)


def flat_world(x_min: int, x_max: int, z_min: int, z_max: int, *, y: int = 59) -> GridWorld:
    return GridWorld({(x, y, z): GridCell() for x in range(x_min, x_max + 1) for z in range(z_min, z_max + 1)})


def make_runtime(body: ScarpetBody) -> NavigationTransactions:
    policy = GovernancePolicy(natural_regions=[Region("move_away", (-10, 0, -10), (10, 100, 10))])
    return NavigationTransactions(
        body,
        SegmentedNavigator(flat_world(-10, 10, -10, 10), NavigationCostModel(policy)),
    )


def distance(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def reset_position(rcon: RconClient, x: int, y: int, z: int) -> None:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill -10 59 -10 10 66 10 air")
    command(rcon, "fill -10 58 -10 10 58 10 stone")
    command(rcon, f"tp {BOT} {x} {y} {z} -90 0")


def run_happy_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_position(rcon, 0, 59, 0)
    runtime = make_runtime(body)
    danger = (0.5, 59.0, 0.5)
    initial = body.get_state()
    initial_distance = distance(initial.pos, danger)

    result = runtime.move_away(
        danger,
        min_distance=5.0,
        candidate_radii=(4, 6),
        max_candidates=8,
        config=NavigationRunConfig(max_segments=2, segment_timeout_s=18.0, min_partial_progress=1),
    )
    final = body.get_state()
    final_distance = distance(final.pos, danger)
    payload = result.to_payload()

    if not result.success or result.reason != "moved_away":
        raise AssertionError(f"move_away happy path failed: result={payload} initial={initial} final={final}")
    if final_distance < 5.0 or final_distance <= initial_distance:
        raise AssertionError(
            f"move_away did not increase distance enough: initial={initial_distance:.3f} final={final_distance:.3f} result={payload}"
        )
    nav_goal = (result.metrics or {}).get("navigation_goal") or {}
    if nav_goal.get("kind") != "avoid":
        raise AssertionError(f"move_away did not preserve avoid goal payload: {payload}")
    attempts = (result.metrics or {}).get("attempts") or []
    if not attempts or attempts[-1].get("result", {}).get("reason") != "arrived":
        raise AssertionError(f"move_away did not expose a successful navigation attempt: {payload}")

    return {
        "reason": result.reason,
        "initial_distance": round(initial_distance, 3),
        "final_distance": round(final_distance, 3),
        "final": final.pos,
        "navigation_reason": attempts[-1]["result"].get("reason"),
    }


def run_already_safe_inverse(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_position(rcon, 8, 59, 0)
    runtime = make_runtime(body)
    danger = (0.5, 59.0, 0.5)
    initial = body.get_state()

    result = runtime.move_away(
        danger,
        min_distance=5.0,
        candidate_radii=(4, 6),
        max_candidates=8,
        config=NavigationRunConfig(max_segments=2, segment_timeout_s=10.0, min_partial_progress=1),
    )
    final = body.get_state()
    payload = result.to_payload()

    if not result.success or result.reason != "already_safe":
        raise AssertionError(f"move_away already-safe inverse returned wrong truth: {payload}")
    if distance(final.pos, initial.pos) > 0.75:
        raise AssertionError(f"move_away already-safe inverse moved the body: initial={initial.pos} final={final.pos} result={payload}")
    if (result.metrics or {}).get("attempts") != []:
        raise AssertionError(f"move_away already-safe inverse should not dispatch navigation: {payload}")

    return {
        "reason": result.reason,
        "initial": initial.pos,
        "final": final.pos,
        "distance_from_danger": round(distance(final.pos, danger), 3),
    }


def run_moving_hazard_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_position(rcon, 0, 59, 0)
    runtime = make_runtime(body)
    initial_danger = [0.5, 59.0, 0.5]
    moved_danger = [-2.5, 59.0, -2.5]

    danger_lock = threading.Lock()
    current_danger = {"value": tuple(initial_danger)}

    def danger_refresh() -> tuple[float, float, float]:
        with danger_lock:
            return tuple(current_danger["value"])

    def move_hazard() -> None:
        time.sleep(0.35)
        with danger_lock:
            current_danger["value"] = tuple(moved_danger)

    mover = threading.Thread(target=move_hazard, daemon=True)
    mover.start()
    result = runtime.move_away(
        tuple(initial_danger),
        min_distance=5.0,
        candidate_radii=(4, 6),
        max_candidates=8,
        maintenance_checks=2,
        maintenance_interval_s=0.45,
        danger_refresh=danger_refresh,
        config=NavigationRunConfig(max_segments=2, segment_timeout_s=18.0, min_partial_progress=1),
    )
    mover.join(timeout=2.0)
    final = body.get_state()
    payload = result.to_payload()
    final_danger = danger_refresh()
    final_distance = distance(final.pos, final_danger)

    if not result.success or result.reason != "moved_away":
        raise AssertionError(f"move_away moving-hazard path did not stay safe: {payload}")
    attempts = (result.metrics or {}).get("attempts") or []
    if len(attempts) < 2:
        raise AssertionError(f"move_away moving-hazard path did not record both maintenance checks: {payload}")
    if attempts[0].get("result", {}).get("reason") != "arrived":
        raise AssertionError(f"move_away moving-hazard path did not complete the first escape move: {payload}")
    second_result = attempts[1].get("result", {})
    if second_result.get("reason") not in {"arrived", "already_safe", "segment_budget_exhausted"}:
        raise AssertionError(f"move_away moving-hazard path did not expose maintenance result honestly: {payload}")
    if (result.metrics or {}).get("maintenance_checks") != 2:
        raise AssertionError(f"move_away moving-hazard path lost maintenance metadata: {payload}")
    if final_distance < 5.0:
        raise AssertionError(
            f"move_away moving-hazard path ended inside the safety band: final={final.pos} danger={final_danger} result={payload}"
        )

    return {
        "reason": result.reason,
        "attempt_count": len(attempts),
        "final": final.pos,
        "final_distance": round(final_distance, 3),
        "attempts": attempts,
    }


def run_no_candidate_inverse(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_position(rcon, 0, 59, 0)
    runtime = make_runtime(body)
    danger = (0.5, 59.0, 0.5)
    initial = body.get_state()

    result = runtime.move_away(
        danger,
        min_distance=100.0,
        candidate_radii=(1,),
        max_candidates=4,
        config=NavigationRunConfig(max_segments=1, segment_timeout_s=8.0, min_partial_progress=1),
    )
    final = body.get_state()
    payload = result.to_payload()

    if result.success:
        raise AssertionError(f"move_away no-candidate inverse unexpectedly succeeded: {payload}")
    if result.reason != "move_away_no_candidate" or not result.can_retry:
        raise AssertionError(f"move_away no-candidate inverse returned wrong truth: {payload}")
    if distance(final.pos, initial.pos) > 0.75:
        raise AssertionError(f"move_away no-candidate inverse moved the body: initial={initial.pos} final={final.pos} result={payload}")
    if (result.metrics or {}).get("attempts") != []:
        raise AssertionError(f"move_away no-candidate inverse should not dispatch navigation: {payload}")

    return {
        "reason": result.reason,
        "initial": initial.pos,
        "final": final.pos,
        "required_distance": (result.metrics or {}).get("required_distance"),
    }


def main() -> None:
    config = RconConfig()
    try:
        rcon = RconClient(config)
        rcon.connect()
    except (OSError, PermissionError, RconError) as exc:
        if os.environ.get("MINEBOT_E2E_REQUIRED") == "1":
            raise
        print(f"SKIP: local RCON unavailable at {config.host}:{config.port}: {type(exc).__name__}: {exc}")
        raise SystemExit(SKIP_EXIT_CODE)

    with rcon:
        setup_world(rcon)
        body = ScarpetBody(BOT, rcon)
        spawn_or_fail(body, (0, 59, 0))
        command(rcon, f"gamemode survival {BOT}")
        command(rcon, f"effect clear {BOT}")

        print(
            {
                "happy": run_happy_path(rcon, body),
                "already_safe": run_already_safe_inverse(rcon, body),
                "moving_hazard": run_moving_hazard_path(rcon, body),
                "no_candidate": run_no_candidate_inverse(rcon, body),
            }
        )


if __name__ == "__main__":
    main()
