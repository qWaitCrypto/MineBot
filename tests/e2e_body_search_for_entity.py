#!/usr/bin/env python3
"""search_for_entity transaction e2e against the local Carpet server."""

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


BOT = "E2EEntSearchBot"
TARGET = "E2EEntTargetBot"
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
        "fill -3 58 -3 12 58 3 stone",
    ]:
        command(rcon, cmd)


def make_runtime(body: ScarpetBody) -> InteractionTransactions:
    policy = GovernancePolicy(natural_regions=[Region("entity_search", (-3, 0, -3), (12, 100, 3))])
    navigator = NavigationTransactions.server_side(body, policy)
    return InteractionTransactions(body, navigator=navigator)


def reset_positions(rcon: RconClient) -> None:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill -3 59 -3 12 66 3 air")
    command(rcon, "fill -3 58 -3 12 58 3 stone")
    command(rcon, f"tp {BOT} 0 59 0 -90 0")
    command(rcon, f"tp {TARGET} 8 59 0 90 0")


def distance(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def run_happy_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_positions(rcon)
    runtime = make_runtime(body)

    result = runtime.search_for_entity(entity_types=("player",), entity_name=TARGET, search_radius=16, timeout_s=18.0)
    payload = result.to_payload()
    final = body.get_state()
    target = result.metrics.get("target") if result.metrics else {}
    entity_id = target.get("id") if isinstance(target, dict) else None
    if not result.success or result.reason != "entity_in_range":
        raise AssertionError(f"search_for_entity did not reach target: {payload}")
    if not entity_id:
        raise AssertionError(f"search_for_entity did not expose stable entity id: {payload}")
    final_distance = float((result.metrics or {}).get("final_distance", 999.0))
    if final_distance > 4.5:
        raise AssertionError(f"search_for_entity final distance too far: final={final.pos} result={payload}")
    attempts = ((result.metrics or {}).get("approach") or {}).get("attempts") or []
    navigation_metrics = ((attempts[0].get("result") or {}).get("metrics") or {}) if attempts else {}
    capability_snapshot = navigation_metrics.get("capability_snapshot") or {}
    for flag in ("allow_break", "allow_place", "allow_pillar", "allow_downward"):
        if capability_snapshot.get(flag) is not False:
            raise AssertionError(f"search_for_entity enabled terrain mutation {flag}: {payload}")
    return {
        "reason": result.reason,
        "entity_id": entity_id,
        "final_distance": round(final_distance, 3),
        "body_pos": final.pos,
        "target": target,
    }


def run_missing_inverse(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_positions(rcon)
    runtime = make_runtime(body)

    result = runtime.search_for_entity(entity_types=("player",), entity_name="MissingEntityBot", search_radius=8, timeout_s=8.0)
    payload = result.to_payload()
    final = body.get_state()
    if result.success:
        raise AssertionError(f"search_for_entity unexpectedly found missing player: {payload}")
    if result.reason != "search_entity_not_found" or not result.can_retry:
        raise AssertionError(f"search_for_entity missing inverse returned wrong truth: {payload}")
    if distance(final.pos, (0.5, 59.0, 0.5)) > 1.0:
        raise AssertionError(f"search_for_entity missing inverse moved the body: final={final.pos} result={payload}")
    return {"reason": result.reason, "can_retry": result.can_retry, "final": final.pos}


def run_target_lost_inverse(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
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
    result = runtime.search_for_entity(entity_types=("player",), entity_name=TARGET, search_radius=16, timeout_s=18.0)
    remover.join(timeout=2.0)
    payload = result.to_payload()
    if result.success:
        raise AssertionError(f"search_for_entity target-lost inverse unexpectedly succeeded: {payload}")
    if result.reason != "search_entity_target_lost" or not result.can_retry:
        raise AssertionError(f"search_for_entity target-lost inverse returned wrong truth: {payload}")
    original_id = ((result.metrics or {}).get("target") or {}).get("id")
    if not original_id:
        raise AssertionError(f"search_for_entity target-lost inverse lost original id: {payload}")
    return {"reason": result.reason, "can_retry": result.can_retry, "original_id": original_id}


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
            "happy": lambda: run_happy_path(rcon, body),
            "missing": lambda: run_missing_inverse(rcon, body),
            "target_lost": lambda: run_target_lost_inverse(rcon, body),
        }
        selected_raw = os.environ.get("MINEBOT_SEARCH_ENTITY_CASES")
        selected = [name.strip() for name in selected_raw.split(",") if name.strip()] if selected_raw else list(cases.keys())
        unknown = [name for name in selected if name not in cases]
        if unknown:
            raise AssertionError(f"unknown search-for-entity e2e cases: {unknown}")
        print({name: cases[name]() for name in selected})


if __name__ == "__main__":
    main()
