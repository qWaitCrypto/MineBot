#!/usr/bin/env python3
"""search_for_block transaction e2e against the local Carpet test server."""

from __future__ import annotations

import math
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.body import BlockApproachTransactions, BlockWork, GetToBlockConfig, NavigationTransactions
from minebot.game import GovernancePolicy, RconClient, Region, ScarpetBody
from minebot.game.errors import RconError
from minebot.game.rcon import RconConfig
from tests.e2e_support import spawn_or_fail


BOT = "E2ESearchBot"
TARGET = (8, 59, 0)
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
        "fill -2 59 -3 12 66 3 air",
        "fill -2 58 -3 12 58 3 stone",
        f"setblock {TARGET[0]} {TARGET[1]} {TARGET[2]} oak_log",
    ]:
        command(rcon, cmd)


def distance(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def make_runtime(body: ScarpetBody) -> BlockWork:
    policy = GovernancePolicy(natural_regions=[Region("search_block", (-2, 0, -3), (12, 100, 3))])
    navigator = NavigationTransactions.server_side(body, policy)
    return BlockWork(body, policy, navigator=navigator)


def make_approach_runtime(body: ScarpetBody) -> BlockApproachTransactions:
    policy = GovernancePolicy(natural_regions=[Region("search_block", (-2, 0, -4), (14, 100, 4))])
    return BlockApproachTransactions(body, NavigationTransactions.server_side(body, policy))


def run_happy_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, f"tp {BOT} 0 59 0 -90 0")
    runtime = make_runtime(body)

    result = runtime.search_for_block(
        block_types=("oak_log",),
        search_radius=12,
        interaction_radius=4.5,
        timeout_s=18.0,
        find_limit=8,
    )
    final = body.get_state()
    block = body.perceive("blockAt", {"x": TARGET[0], "y": TARGET[1], "z": TARGET[2]})
    payload = result.to_payload()
    target_center = (TARGET[0] + 0.5, TARGET[1] + 0.5, TARGET[2] + 0.5)
    final_distance = distance(final.pos, target_center)

    if not result.success or result.reason != "block_candidates_found":
        raise AssertionError(
            "search_for_block did not return target facts: "
            f"result={payload} final={final}"
        )
    if distance(final.pos, (0.5, 59.0, 0.5)) > 1.0:
        raise AssertionError(f"search_for_block perception moved the body: final={final.pos} result={payload}")
    if block.data.get("type") not in {"oak_log", "minecraft:oak_log"}:
        raise AssertionError(f"search target changed unexpectedly: block={block.data} result={payload}")
    return {
        "reason": result.reason,
        "target": (result.metrics or {}).get("target"),
        "initial_distance": round(float((result.metrics or {}).get("initial_distance", 0.0)), 3),
        "final_distance": round(final_distance, 3),
        "body_pos": final.pos,
        "candidate_count": len((result.metrics or {}).get("candidates") or []),
    }


def run_not_found_inverse(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, f"tp {BOT} 0 59 0 -90 0")
    runtime = make_runtime(body)

    result = runtime.search_for_block(
        block_types=("diamond_block",),
        search_radius=6,
        interaction_radius=4.5,
        timeout_s=8.0,
        find_limit=8,
    )
    final = body.get_state()
    payload = result.to_payload()
    if result.success:
        raise AssertionError(f"search_for_block unexpectedly found diamond_block: {payload}")
    if result.reason != "search_block_not_found" or not result.can_retry:
        raise AssertionError(f"search_for_block not-found inverse returned wrong truth: {payload}")
    if distance(final.pos, (0.5, 59.0, 0.5)) > 1.0:
        raise AssertionError(f"search_for_block not-found inverse moved the body: final={final.pos} result={payload}")
    if (result.metrics or {}).get("block_types") != ["diamond_block"]:
        raise AssertionError(f"search_for_block not-found metrics lost filter truth: {payload}")
    return {"reason": result.reason, "can_retry": result.can_retry, "final": final.pos, "metrics": result.metrics}


def run_multiple_candidate_truth(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill -2 59 -4 14 66 4 air")
    command(rcon, "fill -2 58 -4 14 58 4 stone")
    command(rcon, "setblock 6 59 0 oak_log")
    command(rcon, "setblock 11 59 0 oak_log")
    command(rcon, f"tp {BOT} 0 59 0 -90 0")
    runtime = make_runtime(body)
    before = body.get_state()

    result = runtime.search_for_block(
        block_types=("oak_log",),
        search_radius=12,
        interaction_radius=4.5,
        timeout_s=18.0,
        find_limit=8,
    )
    payload = result.to_payload()
    if not result.success or result.reason != "block_candidates_found":
        raise AssertionError(f"multi-candidate search returned wrong truth: {payload}")
    candidates = (result.metrics or {}).get("candidates") or []
    if [candidate.get("pos") for candidate in candidates] != [[6, 59, 0], [11, 59, 0]]:
        raise AssertionError(f"multi-candidate search lost deterministic candidate facts: {payload}")
    after = body.get_state()
    if distance(before.pos, after.pos) > 0.75:
        raise AssertionError(f"multi-candidate perception moved the body: before={before.pos} after={after.pos}")
    return {
        "reason": result.reason,
        "candidates": candidates,
        "before": before.pos,
        "after": after.pos,
    }


def run_get_to_block_happy(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill -2 59 -4 14 66 4 air")
    command(rcon, "fill -2 58 -4 14 58 4 stone")
    command(rcon, f"setblock {TARGET[0]} {TARGET[1]} {TARGET[2]} oak_log")
    command(rcon, f"tp {BOT} 0 59 0 -90 0")
    runtime = make_approach_runtime(body)
    before = body.get_state()

    result = runtime.get_to_block(
        block_types=("oak_log",),
        config=GetToBlockConfig(
            search_radius=12,
            candidate_budget=4,
            find_limit=8,
            max_segments=4,
            segment_timeout_s=8.0,
        ),
    )
    payload = result.to_payload()
    after = body.get_state()
    target_center = (TARGET[0] + 0.5, TARGET[1] + 0.5, TARGET[2] + 0.5)
    if not result.success or result.reason != "block_reached":
        raise AssertionError(f"get_to_block did not reach target: {payload}")
    if distance(before.pos, after.pos) < 2.0:
        raise AssertionError(f"get_to_block did not physically approach: before={before.pos} after={after.pos}")
    if distance(after.pos, target_center) > 4.5:
        raise AssertionError(f"get_to_block terminal range false: final={after.pos} result={payload}")
    metrics = result.metrics or {}
    if metrics.get("target") != list(TARGET) or not metrics.get("identity_verified") or not metrics.get("range_verified"):
        raise AssertionError(f"get_to_block terminal identity/range truth missing: {payload}")
    nav_metrics = ((metrics.get("navigation") or {}).get("metrics") or {})
    capability = nav_metrics.get("capability_snapshot") or {}
    if any(capability.get(key) for key in ("allow_break", "allow_place", "allow_pillar", "allow_downward")):
        raise AssertionError(f"get_to_block escalated terrain mutation capability: {payload}")
    if nav_metrics.get("goal_set_preserved") is not True:
        raise AssertionError(f"get_to_block stand goal set was truncated: {payload}")
    return {
        "reason": result.reason,
        "target": metrics.get("target"),
        "before": before.pos,
        "after": after.pos,
        "final_distance": round(distance(after.pos, target_center), 3),
        "capability_snapshot": capability,
    }


def run_get_to_block_blacklist_replan(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    near = (6, 59, 0)
    near_upper = (6, 60, 0)
    far = (11, 59, 0)
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill -2 59 -4 14 66 4 air")
    command(rcon, "fill -2 58 -4 14 58 4 stone")
    command(rcon, f"setblock {near[0]} {near[1]} {near[2]} oak_log")
    command(rcon, f"setblock {near_upper[0]} {near_upper[1]} {near_upper[2]} oak_log")
    command(rcon, f"setblock {far[0]} {far[1]} {far[2]} oak_log")
    command(rcon, "fill 4 59 -2 8 60 -2 stone")
    command(rcon, "fill 4 59 2 8 60 2 stone")
    command(rcon, "fill 4 59 -2 4 60 2 stone")
    command(rcon, "fill 8 59 -2 8 60 2 stone")
    command(rcon, f"tp {BOT} 0 59 0 -90 0")
    runtime = make_approach_runtime(body)

    result = runtime.get_to_block(
        block_types=("oak_log",),
        config=GetToBlockConfig(
            search_radius=14,
            candidate_budget=2,
            candidate_batch_size=1,
            find_limit=8,
            max_segments=4,
            segment_timeout_s=8.0,
        ),
    )
    payload = result.to_payload()
    metrics = result.metrics or {}
    attempts = metrics.get("attempts") or []
    if not result.success or result.reason != "block_reached" or metrics.get("target") != list(far):
        raise AssertionError(f"get_to_block did not replan to reachable candidate: {payload}")
    if metrics.get("candidate_blacklist") != [list(near)] or len(attempts) != 2:
        raise AssertionError(f"get_to_block blacklist/replan truth missing: {payload}")
    first_navigation = attempts[0].get("navigation") or {}
    if first_navigation.get("success") is not False or first_navigation.get("reason") not in {
        "no_path",
        "budget_exceeded",
        "recovery_exhausted:no_path",
    }:
        raise AssertionError(f"get_to_block first candidate did not fail physically: {payload}")
    if not attempts[1].get("verification", {}).get("success"):
        raise AssertionError(f"get_to_block second candidate lacked terminal verification: {payload}")
    return {
        "reason": result.reason,
        "target": metrics.get("target"),
        "candidate_blacklist": metrics.get("candidate_blacklist"),
        "attempt_reasons": [
            (attempt.get("navigation") or {}).get("reason")
            for attempt in attempts
        ],
        "final": body.get_state().pos,
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

        cases = {
            "happy": lambda: run_happy_path(rcon, body),
            "not_found": lambda: run_not_found_inverse(rcon, body),
            "multiple_candidates": lambda: run_multiple_candidate_truth(rcon, body),
            "get_to_block_happy": lambda: run_get_to_block_happy(rcon, body),
            "get_to_block_blacklist_replan": lambda: run_get_to_block_blacklist_replan(rcon, body),
        }
        selected_raw = os.environ.get("MINEBOT_SEARCH_BLOCK_CASES")
        selected = [name.strip() for name in selected_raw.split(",") if name.strip()] if selected_raw else list(cases.keys())
        unknown = [name for name in selected if name not in cases]
        if unknown:
            raise AssertionError(f"unknown search-for-block e2e cases: {unknown}")
        print({name: cases[name]() for name in selected})


if __name__ == "__main__":
    main()
