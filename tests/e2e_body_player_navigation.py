#!/usr/bin/env python3
"""go_to_player/follow_player transactions e2e against the local Carpet server."""

from __future__ import annotations

import math
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.body import InteractionTransactions, NavigationTransactions
from minebot.game import GovernancePolicy, RconClient, Region, ScarpetBody
from minebot.game.errors import RconError
from minebot.game.rcon import RconConfig
from tests.e2e_support import spawn_or_fail


BOT = "E2EGoBot"
TARGET = "E2ETargetBot"
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
        f"player {TARGET} kill",
        "fill -3 59 -3 12 66 3 air",
        "fill -3 59 -3 12 66 3 air replace water",
        "fill -3 59 -3 12 66 3 air replace flowing_water",
        "fill -3 59 -3 12 66 3 air replace lava",
        "fill -3 59 -3 12 66 3 air replace flowing_lava",
        "fill -3 58 -3 12 58 3 stone",
    ]:
        command(rcon, cmd)


def make_runtime(body: ScarpetBody) -> InteractionTransactions:
    policy = GovernancePolicy(natural_regions=[Region("player_nav", (-3, 0, -3), (12, 100, 3))])
    navigator = NavigationTransactions.server_side(body, policy)
    return InteractionTransactions(body, navigator=navigator)


def distance(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def assert_pure_movement_attempt(attempt: dict[str, object]) -> None:
    result = dict(attempt.get("result") or {})
    metrics = dict(result.get("metrics") or {})
    if metrics.get("goal_set_preserved") is not True:
        raise AssertionError(f"player navigation truncated its stand domain: {attempt}")
    snapshot = dict(metrics.get("capability_snapshot") or {})
    for flag in ("allow_break", "allow_place", "allow_pillar", "allow_downward"):
        if snapshot.get(flag) is not False:
            raise AssertionError(f"player navigation enabled terrain mutation {flag}: {attempt}")


def reset_positions(rcon: RconClient) -> None:
    command(rcon, "script in minebot run minebot_reset()")
    for cmd in [
        "fill -3 59 -3 12 66 3 air",
        "fill -3 59 -3 12 66 3 air replace water",
        "fill -3 59 -3 12 66 3 air replace flowing_water",
        "fill -3 59 -3 12 66 3 air replace lava",
        "fill -3 59 -3 12 66 3 air replace flowing_lava",
        "fill -3 58 -3 12 58 3 stone",
    ]:
        command(rcon, cmd)
    command(rcon, f"tp {BOT} 0 59 0 -90 0")
    command(rcon, f"tp {TARGET} 8 59 0 90 0")


def run_go_to_player_happy_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_positions(rcon)
    runtime = make_runtime(body)

    result = runtime.go_to_player(
        player_name=TARGET,
        search_radius=16,
        min_distance=1.0,
        max_distance=4.5,
        timeout_s=18.0,
    )
    payload = result.to_payload()
    final = body.get_state()
    target = target_position(body)
    final_distance = distance(final.pos, target)

    if not result.success or result.reason != "player_reached":
        raise AssertionError(
            "go_to_player did not reach target band: "
            f"result={payload} final={final} target={target} stand_probe={stand_probe(body, target)}"
        )
    if final_distance < 1.0 or final_distance > 4.5:
        raise AssertionError(f"go_to_player final distance out of band: final={final.pos} target={target} result={payload}")
    approach = (result.metrics or {}).get("approach") or {}
    attempts = approach.get("attempts") or []
    if not attempts or attempts[0].get("result", {}).get("reason") != "arrived":
        raise AssertionError(f"go_to_player did not expose successful navigation attempt: {payload}")
    assert_pure_movement_attempt(attempts[0])
    return {
        "reason": result.reason,
        "final_distance": round(final_distance, 3),
        "body_pos": final.pos,
        "target_pos": target,
        "navigation_reason": attempts[0]["result"].get("reason"),
    }


def run_follow_player_happy_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_positions(rcon)
    runtime = make_runtime(body)

    result = runtime.follow_player(
        player_name=TARGET,
        search_radius=16,
        min_distance=2.0,
        max_distance=4.5,
        timeout_s=18.0,
    )
    payload = result.to_payload()
    final = body.get_state()
    target = target_position(body)
    final_distance = distance(final.pos, target)

    if not result.success or result.reason != "distance_band_reached":
        raise AssertionError(
            "follow_player did not reach distance band: "
            f"result={payload} final={final} target={target} stand_probe={stand_probe(body, target)}"
        )
    if final_distance < 2.0 or final_distance > 4.5:
        raise AssertionError(f"follow_player final distance out of band: final={final.pos} target={target} result={payload}")
    approach = (result.metrics or {}).get("approach") or {}
    attempts = approach.get("attempts") or []
    if not attempts or attempts[0].get("result", {}).get("reason") != "arrived":
        raise AssertionError(f"follow_player did not expose successful navigation attempt: {payload}")
    assert_pure_movement_attempt(attempts[0])
    return {
        "reason": result.reason,
        "final_distance": round(final_distance, 3),
        "body_pos": final.pos,
        "target_pos": target,
        "navigation_reason": attempts[0]["result"].get("reason"),
    }


def run_missing_player_inverse(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_positions(rcon)
    runtime = make_runtime(body)

    result = runtime.go_to_player(player_name="MissingBot", search_radius=8, timeout_s=8.0)
    final = body.get_state()
    payload = result.to_payload()
    if result.success:
        raise AssertionError(f"go_to_player unexpectedly found MissingBot: {payload}")
    if result.reason != "goto_player_target_not_found" or not result.can_retry:
        raise AssertionError(f"go_to_player missing-player inverse returned wrong truth: {payload}")
    if distance(final.pos, (0.5, 59.0, 0.5)) > 1.0:
        raise AssertionError(f"go_to_player missing-player inverse moved the body: final={final.pos} result={payload}")
    return {"reason": result.reason, "can_retry": result.can_retry, "final": final.pos, "metrics": result.metrics}


def run_follow_player_moving_target_liveness(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_positions(rcon)
    runtime = make_runtime(body)

    def move_target() -> None:
        time.sleep(0.35)
        mover_rcon = RconClient(RconConfig())
        mover_rcon.connect()
        try:
            mover_rcon.command(f"tp {TARGET} 11 59 0 90 0")
        finally:
            mover_rcon.close()

    mover = threading.Thread(target=move_target, daemon=True)
    mover.start()
    result = runtime.follow_player(
        player_name=TARGET,
        search_radius=16,
        min_distance=2.0,
        max_distance=4.5,
        timeout_s=18.0,
        maintenance_checks=2,
        maintenance_interval_s=0.6,
    )
    mover.join(timeout=2.0)
    payload = result.to_payload()
    final = body.get_state()
    target = target_position(body)
    final_distance = distance(final.pos, target)

    if not result.success or result.reason != "distance_band_reached":
        raise AssertionError(f"moving follow did not maintain band: result={payload} final={final} target={target}")
    if final_distance < 2.0 or final_distance > 4.5:
        raise AssertionError(f"moving follow final distance out of band: final={final.pos} target={target} result={payload}")
    attempts = (result.metrics or {}).get("maintenance_attempts") or []
    if len(attempts) != 2:
        raise AssertionError(f"moving follow did not expose two maintenance checks: {payload}")
    if not any(attempt.get("approach", {}).get("navigated") for attempt in attempts):
        raise AssertionError(f"moving follow never used shared navigation: {payload}")
    target_positions = [
        tuple((attempt.get(key) or {}).get("pos") or [])
        for attempt in attempts
        for key in ("target_before", "target_after")
    ]
    if len({pos for pos in target_positions if pos}) < 2:
        raise AssertionError(f"moving follow did not observe target movement: {payload}")
    return {
        "reason": result.reason,
        "final_distance": round(final_distance, 3),
        "body_pos": final.pos,
        "target_pos": target,
        "maintenance_checks": len(attempts),
        "observed_targets": target_positions,
    }


def run_follow_player_target_lost_runtime_truth(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_positions(rcon)
    runtime = make_runtime(body)

    def remove_target() -> None:
        time.sleep(0.35)
        remover_rcon = RconClient(RconConfig())
        remover_rcon.connect()
        try:
            remover_rcon.command(f"player {TARGET} kill")
        finally:
            remover_rcon.close()

    remover = threading.Thread(target=remove_target, daemon=True)
    remover.start()
    result = runtime.follow_player(
        player_name=TARGET,
        search_radius=16,
        min_distance=2.0,
        max_distance=4.5,
        timeout_s=18.0,
        maintenance_checks=2,
        maintenance_interval_s=0.6,
    )
    remover.join(timeout=2.0)
    payload = result.to_payload()
    if result.success:
        raise AssertionError(f"follow target-lost inverse unexpectedly succeeded: {payload}")
    if result.reason != "follow_target_lost" or not result.can_retry:
        raise AssertionError(f"follow target-lost inverse returned wrong truth: {payload}")
    attempts = (result.metrics or {}).get("maintenance_attempts") or []
    if len(attempts) != 1:
        raise AssertionError(f"follow target-lost inverse should expose one completed check: {payload}")
    if not attempts[0].get("lost_after_navigation"):
        raise AssertionError(f"follow target-lost inverse did not identify post-navigation loss: {payload}")
    return {
        "reason": result.reason,
        "can_retry": result.can_retry,
        "completed_checks": len(attempts),
        "attempts": attempts,
    }


def target_position(body: ScarpetBody) -> tuple[float, float, float]:
    entities = body.perceive("nearbyEntities", {"radius": 20, "limit": 32})
    if not entities.ok or not entities.complete:
        raise AssertionError(f"nearbyEntities failed while locating target: {entities}")
    for item in entities.data.get("entities") or []:
        if item.get("name") == TARGET:
            pos = item.get("pos") or []
            return (float(pos[0]), float(pos[1]), float(pos[2]))
    raise AssertionError(f"target player missing from nearbyEntities: {entities.data}")


def stand_probe(body: ScarpetBody, target: tuple[float, float, float]) -> list[dict[str, object]]:
    base = (math.floor(target[0]), math.floor(target[1]), math.floor(target[2]))
    probes: list[dict[str, object]] = []
    for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        feet = (base[0] + dx, base[1], base[2] + dz)
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
        target = ScarpetBody(TARGET, rcon)
        spawn_or_fail(body, (0, 59, 0))
        spawn_or_fail(target, (8, 59, 0))
        command(rcon, f"gamemode survival {BOT}")
        command(rcon, f"gamemode survival {TARGET}")
        command(rcon, f"effect clear {BOT}")
        command(rcon, f"effect clear {TARGET}")

        cases = {
            "go_to_player": lambda: run_go_to_player_happy_path(rcon, body),
            "follow_player": lambda: run_follow_player_happy_path(rcon, body),
            "missing": lambda: run_missing_player_inverse(rcon, body),
            "moving_follow": lambda: run_follow_player_moving_target_liveness(rcon, body),
            "target_lost": lambda: run_follow_player_target_lost_runtime_truth(rcon, body),
        }
        selected_raw = os.environ.get("MINEBOT_PLAYER_NAV_CASES")
        selected = [name.strip() for name in selected_raw.split(",") if name.strip()] if selected_raw else list(cases.keys())
        unknown = [name for name in selected if name not in cases]
        if unknown:
            raise AssertionError(f"unknown player-navigation e2e cases: {unknown}")
        print({name: cases[name]() for name in selected})


if __name__ == "__main__":
    main()
