#!/usr/bin/env python3
"""search_for_block transaction e2e against the local Carpet test server."""

from __future__ import annotations

import math
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.body import BlockWork, NavigationRunConfig, NavigationTransactions
from minebot.game import GovernancePolicy, RconClient, Region, ScarpetBody
from minebot.game.errors import RconError
from minebot.game.rcon import RconConfig
from minebot.game.navigation import GridCell, GridWorld, NavigationCostModel, SegmentedNavigator
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


def flat_world(x_min: int, x_max: int, z_min: int, z_max: int, *, y: int = 59) -> GridWorld:
    return GridWorld({(x, y, z): GridCell() for x in range(x_min, x_max + 1) for z in range(z_min, z_max + 1)})


def distance(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def make_runtime(body: ScarpetBody) -> BlockWork:
    policy = GovernancePolicy(natural_regions=[Region("search_block", (-2, 0, -3), (12, 100, 3))])
    navigator = NavigationTransactions(
        body,
        SegmentedNavigator(flat_world(-2, 12, -3, 3), NavigationCostModel(policy)),
    )
    return BlockWork(body, policy, navigator=navigator)


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

    if not result.success or result.reason != "block_in_range":
        raise AssertionError(
            "search_for_block did not reach target range: "
            f"result={payload} final={final} stand_probe={stand_probe(body, TARGET)}"
        )
    if final_distance > 4.5:
        raise AssertionError(f"search_for_block final distance too far: final={final.pos} dist={final_distance:.3f} result={payload}")
    if block.data.get("type") not in {"oak_log", "minecraft:oak_log"}:
        raise AssertionError(f"search target changed unexpectedly: block={block.data} result={payload}")
    attempts = (result.metrics or {}).get("attempts") or []
    if not attempts or not attempts[0].get("result", {}).get("success"):
        raise AssertionError(f"search_for_block did not expose successful navigation attempt: {payload}")
    nav_result = attempts[0]["result"]
    if nav_result.get("reason") != "arrived":
        raise AssertionError(f"search_for_block navigation did not arrive: {payload}")

    return {
        "reason": result.reason,
        "target": (result.metrics or {}).get("target"),
        "initial_distance": round(float((result.metrics or {}).get("initial_distance", 0.0)), 3),
        "final_distance": round(final_distance, 3),
        "body_pos": final.pos,
        "navigation_reason": nav_result.get("reason"),
    }


def stand_probe(body: ScarpetBody, target: tuple[int, int, int]) -> list[dict[str, object]]:
    probes: list[dict[str, object]] = []
    for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        feet = (target[0] + dx, target[1], target[2] + dz)
        head = (feet[0], feet[1] + 1, feet[2])
        below = (feet[0], feet[1] - 1, feet[2])
        probes.append(
            {
                "feet": list(feet),
                "feet_block": body.perceive("blockAt", {"x": feet[0], "y": feet[1], "z": feet[2]}).data,
                "head_block": body.perceive("blockAt", {"x": head[0], "y": head[1], "z": head[2]}).data,
                "below_block": body.perceive("blockAt", {"x": below[0], "y": below[1], "z": below[2]}).data,
            }
        )
    return probes


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


def run_candidate_fallback_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill -2 59 -4 14 66 4 air")
    command(rcon, "fill -2 58 -4 14 58 4 stone")
    command(rcon, "setblock 6 59 0 oak_log")
    command(rcon, "setblock 11 59 0 oak_log")
    command(rcon, f"tp {BOT} 0 59 0 -90 0")
    cells = flat_world(-2, 14, -4, 4).cells
    cells[(6, 59, 1)] = GridCell(block_type="stone", walkable=False)
    cells[(6, 59, -1)] = GridCell(block_type="stone", walkable=False)
    cells[(5, 59, 0)] = GridCell(block_type="stone", walkable=False)
    cells[(7, 59, 0)] = GridCell(block_type="stone", walkable=False)
    policy = GovernancePolicy(
        natural_regions=[Region("search_block_fallback", (-2, 0, -4), (14, 100, 4))],
        protected_regions=[Region("first_candidate_ring", (5, 0, -1), (7, 100, 1))],
    )
    navigator = NavigationTransactions(body, SegmentedNavigator(GridWorld(cells), NavigationCostModel(policy)))
    runtime = BlockWork(body, policy, navigator=navigator)

    result = runtime.search_for_block(
        block_types=("oak_log",),
        search_radius=12,
        interaction_radius=4.5,
        timeout_s=18.0,
        find_limit=8,
    )
    payload = result.to_payload()
    if not result.success or result.reason != "block_in_range":
        raise AssertionError(f"candidate fallback did not reach a later target: {payload}")
    target = (result.metrics or {}).get("target") or {}
    if target.get("pos") != [11, 59, 0]:
        raise AssertionError(f"candidate fallback did not skip the blocked nearest target: {payload}")
    attempts = (result.metrics or {}).get("attempts") or []
    if not any((attempt.get("target") or {}).get("pos") == [6, 59, 0] and not attempt.get("result", {}).get("success") for attempt in attempts):
        raise AssertionError(f"candidate fallback did not expose failed nearest-target attempts: {payload}")
    return {
        "reason": result.reason,
        "target": target,
        "candidate_count": len((result.metrics or {}).get("candidates") or []),
        "attempt_count": len(attempts),
    }


def run_target_lost_inverse(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill -2 59 -3 12 66 3 air")
    command(rcon, "fill -2 58 -3 12 58 3 stone")
    command(rcon, f"setblock {TARGET[0]} {TARGET[1]} {TARGET[2]} oak_log")
    command(rcon, f"tp {BOT} 0 59 0 -90 0")
    runtime = make_runtime(body)

    def remove_target() -> None:
        time.sleep(0.25)
        remover = RconClient(RconConfig())
        remover.connect()
        try:
            remover.command(f"setblock {TARGET[0]} {TARGET[1]} {TARGET[2]} air")
        finally:
            remover.close()

    remover = threading.Thread(target=remove_target, daemon=True)
    remover.start()
    result = runtime.search_for_block(
        block_types=("oak_log",),
        search_radius=12,
        interaction_radius=4.5,
        timeout_s=18.0,
        find_limit=8,
    )
    remover.join(timeout=2.0)
    payload = result.to_payload()
    if result.success:
        raise AssertionError(f"target-lost inverse unexpectedly succeeded: {payload}")
    if result.reason != "search_block_target_lost" or not result.can_retry:
        raise AssertionError(f"target-lost inverse returned wrong truth: {payload}")
    refreshed = (result.metrics or {}).get("refreshed_block") or {}
    if refreshed.get("type") in {"oak_log", "minecraft:oak_log"}:
        raise AssertionError(f"target-lost inverse still saw the requested target block: {payload}")
    return {"reason": result.reason, "can_retry": result.can_retry, "refreshed_block": refreshed}


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
            "candidate_fallback": lambda: run_candidate_fallback_path(rcon, body),
            "target_lost": lambda: run_target_lost_inverse(rcon, body),
        }
        selected_raw = os.environ.get("MINEBOT_SEARCH_BLOCK_CASES")
        selected = [name.strip() for name in selected_raw.split(",") if name.strip()] if selected_raw else list(cases.keys())
        unknown = [name for name in selected if name not in cases]
        if unknown:
            raise AssertionError(f"unknown search-for-block e2e cases: {unknown}")
        print({name: cases[name]() for name in selected})


if __name__ == "__main__":
    main()
