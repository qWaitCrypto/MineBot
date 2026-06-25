#!/usr/bin/env python3
"""activate_switch/deactivate_switch Body transactions e2e against the local Carpet server."""

from __future__ import annotations

import math
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.body import InteractionTransactions, NavigationTransactions
from minebot.game import GovernancePolicy, GridCell, GridWorld, NavigationCostModel, RconClient, Region, ScarpetBody
from minebot.game.errors import RconError
from minebot.game.navigation import SegmentedNavigator
from minebot.game.rcon import RconConfig
from tests.e2e_support import spawn_or_fail


BOT = "E2ESwitchBot"
LEVER_FLOOR = (8, 60, 0)
LEVER_WALL = (6, 60, 0)
BUTTON_STONE_FLOOR = (10, 60, 0)
BUTTON_OAK_CEILING = (4, 60, 0)
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
    ]:
        command(rcon, cmd)


def reset_world(
    rcon: RconClient,
    *,
    with_floor_lever: bool = False,
    with_wall_lever: bool = False,
    with_floor_button: bool = False,
    with_ceiling_button: bool = False,
) -> None:
    for cmd in [
        "script in minebot run minebot_reset()",
        f"player {BOT} stop",
        "fill -2 59 -3 12 66 3 air",
        "fill -2 58 -3 12 58 3 stone",
        f"clear {BOT}",
        f"tp {BOT} 0 59 0 -90 0",
    ]:
        command(rcon, cmd)
    if with_floor_lever:
        command(rcon, f"setblock {LEVER_FLOOR[0]} {LEVER_FLOOR[1] - 1} {LEVER_FLOOR[2]} stone")
        command(rcon, f"setblock {LEVER_FLOOR[0]} {LEVER_FLOOR[1]} {LEVER_FLOOR[2]} lever[face=floor,facing=east,powered=false]")
    if with_wall_lever:
        command(rcon, f"setblock {LEVER_WALL[0] + 1} {LEVER_WALL[1]} {LEVER_WALL[2]} stone")
        command(rcon, f"setblock {LEVER_WALL[0]} {LEVER_WALL[1]} {LEVER_WALL[2]} lever[face=wall,facing=east,powered=false]")
    if with_floor_button:
        command(rcon, f"setblock {BUTTON_STONE_FLOOR[0]} {BUTTON_STONE_FLOOR[1] - 1} {BUTTON_STONE_FLOOR[2]} stone")
        command(rcon, f"setblock {BUTTON_STONE_FLOOR[0]} {BUTTON_STONE_FLOOR[1]} {BUTTON_STONE_FLOOR[2]} stone_button[face=floor,facing=east,powered=false]")
    if with_ceiling_button:
        command(rcon, f"setblock {BUTTON_OAK_CEILING[0]} {BUTTON_OAK_CEILING[1] + 1} {BUTTON_OAK_CEILING[2]} stone")
        command(rcon, f"setblock {BUTTON_OAK_CEILING[0]} {BUTTON_OAK_CEILING[1]} {BUTTON_OAK_CEILING[2]} oak_button[face=ceiling,facing=east,powered=false]")


def flat_world(x_min: int, x_max: int, z_min: int, z_max: int, *, y: int = 59) -> GridWorld:
    return GridWorld({(x, y, z): GridCell() for x in range(x_min, x_max + 1) for z in range(z_min, z_max + 1)})


def make_runtime(body: ScarpetBody) -> InteractionTransactions:
    policy = GovernancePolicy(natural_regions=[Region("switch_nav", (-2, 0, -3), (12, 100, 3))])
    navigator = NavigationTransactions(
        body,
        SegmentedNavigator(flat_world(-2, 12, -3, 3), NavigationCostModel(policy)),
    )
    return InteractionTransactions(body, navigator=navigator, governance=policy)


def powered_property(body: ScarpetBody, pos: tuple[int, int, int]) -> str:
    block = body.perceive("blockAt", {"x": pos[0], "y": pos[1], "z": pos[2]})
    if not block.ok or not block.complete:
        raise AssertionError(f"blockAt failed for switch: {block}")
    return str((block.data.get("properties") or {}).get("powered") or "false").lower()


def assert_shared_navigation(result_payload: dict[str, object]) -> str:
    approach = ((result_payload.get("metrics") or {}).get("approach") or {})
    if approach.get("navigated") is not True:
        raise AssertionError(f"switch transaction did not use shared navigation: {result_payload}")
    attempts = approach.get("attempts") or []
    if not attempts or attempts[-1].get("result", {}).get("reason") != "arrived":
        raise AssertionError(f"switch approach navigation did not arrive: {result_payload}")
    return str(attempts[-1]["result"].get("reason"))


def run_lever_activate_deactivate(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_world(rcon, with_floor_lever=True)
    runtime = make_runtime(body)

    activated = runtime.activate_switch(pos=LEVER_FLOOR, approach_timeout_s=18.0, use_timeout_s=6.0)
    activated_payload = activated.to_payload()
    if not activated.success or activated.reason != "powered":
        raise AssertionError(f"activate_switch failed: {activated_payload}")
    if powered_property(body, LEVER_FLOOR) != "true":
        raise AssertionError(f"lever was not powered after activate_switch: {activated_payload}")
    navigation_reason = assert_shared_navigation(activated_payload)

    deactivated = runtime.deactivate_switch(pos=LEVER_FLOOR, approach_timeout_s=18.0, use_timeout_s=6.0)
    deactivated_payload = deactivated.to_payload()
    if not deactivated.success or deactivated.reason != "unpowered":
        raise AssertionError(f"deactivate_switch failed: {deactivated_payload}")
    if powered_property(body, LEVER_FLOOR) != "false":
        raise AssertionError(f"lever was not unpowered after deactivate_switch: {deactivated_payload}")

    return {
        "activate_reason": activated.reason,
        "deactivate_reason": deactivated.reason,
        "target": list(LEVER_FLOOR),
        "navigation_reason": navigation_reason,
        "final": body.get_state().pos,
    }


def run_wall_lever_activate_deactivate(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_world(rcon, with_wall_lever=True)
    runtime = make_runtime(body)

    activated = runtime.activate_switch(pos=LEVER_WALL, approach_timeout_s=18.0, use_timeout_s=6.0)
    activated_payload = activated.to_payload()
    if not activated.success or activated.reason != "powered":
        raise AssertionError(f"activate_switch wall lever failed: {activated_payload}")
    if powered_property(body, LEVER_WALL) != "true":
        raise AssertionError(f"wall lever was not powered after activate_switch: {activated_payload}")

    deactivated = runtime.deactivate_switch(pos=LEVER_WALL, approach_timeout_s=18.0, use_timeout_s=6.0)
    deactivated_payload = deactivated.to_payload()
    if not deactivated.success or deactivated.reason != "unpowered":
        raise AssertionError(f"deactivate_switch wall lever failed: {deactivated_payload}")
    if powered_property(body, LEVER_WALL) != "false":
        raise AssertionError(f"wall lever was not unpowered after deactivate_switch: {deactivated_payload}")

    return {
        "activate_reason": activated.reason,
        "deactivate_reason": deactivated.reason,
        "target": list(LEVER_WALL),
        "final": body.get_state().pos,
    }


def run_button_press_release(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_world(rcon, with_floor_button=True)
    runtime = make_runtime(body)

    pressed = runtime.activate_switch(pos=BUTTON_STONE_FLOOR, approach_timeout_s=18.0, use_timeout_s=6.0)
    pressed_payload = pressed.to_payload()
    if not pressed.success or pressed.reason != "powered":
        raise AssertionError(f"activate_switch button failed: {pressed_payload}")

    released = runtime.deactivate_switch(pos=BUTTON_STONE_FLOOR, release_timeout_s=3.0, release_poll_s=0.05)
    released_payload = released.to_payload()
    if not released.success or released.reason != "released":
        raise AssertionError(f"deactivate_switch button release failed: {released_payload}")
    if powered_property(body, BUTTON_STONE_FLOOR) != "false":
        raise AssertionError(f"button was not released after wait: {released_payload}")

    return {
        "press_reason": pressed.reason,
        "release_reason": released.reason,
        "target": list(BUTTON_STONE_FLOOR),
        "waited_for_release": released.metrics.get("waited_for_release") if released.metrics else None,
    }


def run_ceiling_oak_button_press_release(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_world(rcon, with_ceiling_button=True)
    runtime = make_runtime(body)

    pressed = runtime.activate_switch(pos=BUTTON_OAK_CEILING, approach_timeout_s=18.0, use_timeout_s=6.0)
    pressed_payload = pressed.to_payload()
    if not pressed.success or pressed.reason != "powered":
        raise AssertionError(f"activate_switch ceiling oak button failed: {pressed_payload}")

    released = runtime.deactivate_switch(pos=BUTTON_OAK_CEILING, release_timeout_s=3.0, release_poll_s=0.05)
    released_payload = released.to_payload()
    if not released.success or released.reason != "released":
        raise AssertionError(f"deactivate_switch ceiling oak button release failed: {released_payload}")
    if powered_property(body, BUTTON_OAK_CEILING) != "false":
        raise AssertionError(f"ceiling oak button was not released after wait: {released_payload}")

    return {
        "press_reason": pressed.reason,
        "release_reason": released.reason,
        "target": list(BUTTON_OAK_CEILING),
        "waited_for_release": released.metrics.get("waited_for_release") if released.metrics else None,
    }


def run_not_found_inverse(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_world(rcon)
    runtime = make_runtime(body)
    before = body.get_state()
    result = runtime.activate_switch(search_radius=6, approach_timeout_s=8.0, use_timeout_s=4.0)
    after = body.get_state()
    payload = result.to_payload()
    if result.success or result.reason != "switch_not_found":
        raise AssertionError(f"activate_switch not-found inverse returned wrong truth: {payload}")
    if math.dist(before.pos, after.pos) > 0.75:
        raise AssertionError(f"activate_switch not-found inverse moved the body: before={before.pos} after={after.pos} result={payload}")
    return {"reason": result.reason, "can_retry": result.can_retry, "before": before.pos, "after": after.pos}


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
                "floor_lever": run_lever_activate_deactivate(rcon, body),
                "wall_lever": run_wall_lever_activate_deactivate(rcon, body),
                "stone_button": run_button_press_release(rcon, body),
                "oak_button_ceiling": run_ceiling_oak_button_press_release(rcon, body),
                "not_found": run_not_found_inverse(rcon, body),
            }
        )


if __name__ == "__main__":
    main()
