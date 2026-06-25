#!/usr/bin/env python3
"""Mechanism probe for fake-player bed sleep on the local Carpet server.

This is intentionally **not** a closure gate. It captures the current live
fact pattern behind the still-open bed row:

- the production-style `go_to_bed(...)` path really can enter sleep at night;
- the daytime honesty path (`bed_not_night`) remains true;
- the older direct-occupant helper (`player <name> use`) is not a reliable way
  to create an occupied-bed fixture on this server and should not be used as
  proof that `bed_occupied` is live-closed.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from minebot.body import InteractionTransactions, NavigationTransactions
from minebot.game import GovernancePolicy, GridCell, GridWorld, NavigationCostModel, RconClient, Region, ScarpetBody
from minebot.game.errors import RconError
from minebot.game.navigation import SegmentedNavigator
from minebot.game.rcon import RconConfig
from tests.e2e_support import spawn_or_fail


BOT = "E2EBedBot"
BED = (8, 59, 0)
OCCUPANT = "E2EBedOccupant"
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
        "weather clear",
        "difficulty normal",
        "kill @e[type=!player]",
        f"player {BOT} kill",
        f"player {OCCUPANT} kill",
        "fill -2 59 -3 12 66 3 air",
        "fill -2 58 -3 12 58 3 stone",
    ]:
        command(rcon, cmd)


def reset_world(rcon: RconClient, *, spawn_occupant: bool = False, time_command: str = "time set night") -> None:
    for cmd in [
        "script in minebot run minebot_reset()",
        f"player {BOT} stop",
        f"player {OCCUPANT} stop",
        "fill -2 59 -3 12 66 3 air",
        "fill -2 58 -3 12 58 3 stone",
        f"clear {BOT}",
        f"clear {OCCUPANT}",
        f"tp {BOT} 0 59 0 -90 0",
        time_command,
    ]:
        command(rcon, cmd)
    command(rcon, f"setblock {BED[0]} {BED[1]} {BED[2]} red_bed[facing=east,part=foot,occupied=false]")
    command(rcon, f"setblock {BED[0] + 1} {BED[1]} {BED[2]} red_bed[facing=east,part=head,occupied=false]")


def flat_world(x_min: int, x_max: int, z_min: int, z_max: int, *, y: int = 59) -> GridWorld:
    return GridWorld({(x, y, z): GridCell() for x in range(x_min, x_max + 1) for z in range(z_min, z_max + 1)})


def make_runtime(body: ScarpetBody) -> InteractionTransactions:
    policy = GovernancePolicy(natural_regions=[Region("bed_nav", (-2, 0, -3), (12, 100, 3))])
    navigator = NavigationTransactions(
        body,
        SegmentedNavigator(flat_world(-2, 12, -3, 3), NavigationCostModel(policy)),
    )
    return InteractionTransactions(body, navigator=navigator, governance=policy)


def probe_direct_occupant_use(rcon: RconClient) -> dict[str, object]:
    occupant = ScarpetBody(OCCUPANT, rcon)
    spawn_or_fail(occupant, (7, 59, 0))
    command(rcon, f"gamemode survival {OCCUPANT}")
    command(rcon, f"effect clear {OCCUPANT}")
    command(rcon, f"tp {OCCUPANT} 7 59 0 -90 0")
    command(rcon, f"player {OCCUPANT} use")
    time.sleep(0.3)
    props = bed_properties(occupant)
    state = occupant.get_state()
    return {
        "sleeping_after": state.sleeping,
        "body_pos": state.pos,
        "occupied_property": str(props.get("occupied") or "false").lower(),
    }


def bed_properties(body: ScarpetBody) -> dict[str, object]:
    block = body.perceive("blockAt", {"x": BED[0], "y": BED[1], "z": BED[2]})
    if not block.ok or not block.complete:
        raise AssertionError(f"blockAt failed for bed: {block}")
    return dict(block.data.get("properties") or {})


def assert_shared_navigation(result_payload: dict[str, object]) -> str:
    approach = ((result_payload.get("metrics") or {}).get("approach") or {})
    if approach.get("navigated") is not True:
        raise AssertionError(f"bed transaction did not use shared navigation: {result_payload}")
    attempts = approach.get("attempts") or []
    if not attempts or attempts[-1].get("result", {}).get("reason") != "arrived":
        raise AssertionError(f"bed approach navigation did not arrive: {result_payload}")
    return str(attempts[-1]["result"].get("reason"))


def run_sleep_happy(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_world(rcon, time_command="time set night")
    runtime = make_runtime(body)

    result = runtime.go_to_bed(search_radius=12, approach_timeout_s=18.0, use_timeout_s=6.0)
    payload = result.to_payload()
    if not result.success or result.reason != "sleeping":
        raise AssertionError(f"go_to_bed happy path failed: {payload}")
    navigation_reason = assert_shared_navigation(payload)
    after = body.get_state()
    if after.sleeping is not True:
        raise AssertionError(f"body state did not report sleeping=true after go_to_bed: {payload}")
    return {
        "reason": result.reason,
        "navigation_reason": navigation_reason,
        "sleeping_after": after.sleeping,
        "body_pos": after.pos,
        "payload": payload,
    }


def run_not_night_inverse(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_world(rcon, time_command="time set day")
    runtime = make_runtime(body)
    before = body.get_state()
    result = runtime.go_to_bed(search_radius=12, approach_timeout_s=18.0, use_timeout_s=6.0)
    after = body.get_state()
    payload = result.to_payload()
    if result.success or result.reason != "bed_not_night":
        raise AssertionError(f"go_to_bed not-night inverse returned wrong truth: {payload}")
    if after.sleeping is True:
        raise AssertionError(f"go_to_bed not-night inverse unexpectedly entered sleep: {payload}")
    return {
        "reason": result.reason,
        "sleeping_before": before.sleeping,
        "sleeping_after": after.sleeping,
        "body_pos": after.pos,
    }


def run_direct_occupant_observation(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_world(rcon, time_command="time set night")
    occupant = probe_direct_occupant_use(rcon)
    props = bed_properties(body)
    return {
        "occupied_property": str(props.get("occupied") or "false").lower(),
        "occupant_sleeping": occupant["sleeping_after"],
        "occupant_pos": occupant["body_pos"],
        "direct_occupant_property": occupant["occupied_property"],
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
                "sleep_happy": run_sleep_happy(rcon, body),
                "not_night": run_not_night_inverse(rcon, body),
                "direct_occupant_observation": run_direct_occupant_observation(rcon, body),
            }
        )


if __name__ == "__main__":
    main()
