#!/usr/bin/env python3
"""open_openable/close_openable Body transactions e2e against the local Carpet server."""

from __future__ import annotations

import math
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.body import InteractionTransactions, NavigationTransactions
from minebot.game import GovernancePolicy, RconClient, Region, ScarpetBody
from minebot.game.errors import RconError
from minebot.game.rcon import RconConfig
from tests.e2e_support import spawn_or_fail


BOT = "E2EOpenableBot"
GATE = (8, 59, 0)
DOOR = (6, 59, 0)
TRAPDOOR_BOTTOM = (4, 60, 0)
TRAPDOOR_TOP = (2, 60, 0)
IRON_DOOR = (10, 59, 0)
IRON_TRAPDOOR = (0, 60, 0)
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
    with_gate: bool = False,
    with_door: bool = False,
    with_bottom_trapdoor: bool = False,
    with_top_trapdoor: bool = False,
    with_iron_door: bool = False,
    with_iron_trapdoor: bool = False,
) -> None:
    for cmd in [
        "script in minebot run minebot_reset()",
        "fill -2 59 -3 12 66 3 air",
        "fill -2 58 -3 12 58 3 stone",
        f"clear {BOT}",
        f"tp {BOT} 0 59 0 -90 0",
    ]:
        command(rcon, cmd)
    if with_gate:
        command(rcon, f"setblock {GATE[0]} {GATE[1]} {GATE[2]} oak_fence_gate[facing=east,in_wall=false,open=false,powered=false]")
    if with_door:
        command(rcon, f"setblock {DOOR[0]} {DOOR[1]} {DOOR[2]} oak_door[facing=east,half=lower,hinge=left,open=false,powered=false]")
        command(rcon, f"setblock {DOOR[0]} {DOOR[1] + 1} {DOOR[2]} oak_door[facing=east,half=upper,hinge=left,open=false,powered=false]")
    if with_bottom_trapdoor:
        command(rcon, f"setblock {TRAPDOOR_BOTTOM[0]} {TRAPDOOR_BOTTOM[1] - 1} {TRAPDOOR_BOTTOM[2]} stone")
        command(rcon, f"setblock {TRAPDOOR_BOTTOM[0]} {TRAPDOOR_BOTTOM[1]} {TRAPDOOR_BOTTOM[2]} oak_trapdoor[facing=east,half=bottom,open=false,powered=false,waterlogged=false]")
    if with_top_trapdoor:
        command(rcon, f"setblock {TRAPDOOR_TOP[0]} {TRAPDOOR_TOP[1] - 1} {TRAPDOOR_TOP[2]} stone")
        command(rcon, f"setblock {TRAPDOOR_TOP[0]} {TRAPDOOR_TOP[1]} {TRAPDOOR_TOP[2]} oak_trapdoor[facing=east,half=top,open=false,powered=false,waterlogged=false]")
    if with_iron_door:
        command(rcon, f"setblock {IRON_DOOR[0]} {IRON_DOOR[1]} {IRON_DOOR[2]} iron_door[facing=east,half=lower,hinge=left,open=false,powered=false]")
        command(rcon, f"setblock {IRON_DOOR[0]} {IRON_DOOR[1] + 1} {IRON_DOOR[2]} iron_door[facing=east,half=upper,hinge=left,open=false,powered=false]")
    if with_iron_trapdoor:
        command(rcon, f"setblock {IRON_TRAPDOOR[0]} {IRON_TRAPDOOR[1] - 1} {IRON_TRAPDOOR[2]} stone")
        command(rcon, f"setblock {IRON_TRAPDOOR[0]} {IRON_TRAPDOOR[1]} {IRON_TRAPDOOR[2]} iron_trapdoor[facing=east,half=top,open=false,powered=false,waterlogged=false]")


def make_runtime(body: ScarpetBody) -> InteractionTransactions:
    policy = GovernancePolicy(natural_regions=[Region("openable_nav", (-2, 0, -3), (12, 100, 3))])
    navigator = NavigationTransactions.server_side(body, policy)
    return InteractionTransactions(body, navigator=navigator, governance=policy)


def block_open_property(body: ScarpetBody, pos: tuple[int, int, int]) -> str:
    block = body.perceive("blockAt", {"x": pos[0], "y": pos[1], "z": pos[2]})
    if not block.ok or not block.complete:
        raise AssertionError(f"blockAt failed for openable: {block}")
    return str((block.data.get("properties") or {}).get("open") or "false").lower()


def assert_approached(result_payload: dict[str, object]) -> tuple[str, tuple[float, float, float]]:
    approach = ((result_payload.get("metrics") or {}).get("approach") or {})
    if approach.get("navigated") is not True:
        raise AssertionError(f"openable transaction did not use shared navigation: {result_payload}")
    attempts = approach.get("attempts") or []
    if not attempts or attempts[-1].get("result", {}).get("reason") != "arrived":
        raise AssertionError(f"openable approach navigation did not arrive: {result_payload}")
    return str(attempts[-1]["result"].get("reason")), tuple()


def run_open_close_happy(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_world(rcon, with_gate=True)
    runtime = make_runtime(body)

    opened = runtime.open_openable(pos=GATE, approach_timeout_s=18.0, use_timeout_s=6.0)
    opened_payload = opened.to_payload()
    if not opened.success or opened.reason != "opened":
        raise AssertionError(f"open_openable failed: {opened_payload}")
    if block_open_property(body, GATE) != "true":
        raise AssertionError(f"oak fence gate was not open after open_openable: {opened_payload}")
    open_nav_reason, _ = assert_approached(opened_payload)
    after_open = body.get_state()
    if math.dist(after_open.pos, (GATE[0] + 0.5, GATE[1] + 0.5, GATE[2] + 0.5)) > 4.5:
        raise AssertionError(f"body not in openable interaction range after open: final={after_open.pos} result={opened_payload}")

    closed = runtime.close_openable(pos=GATE, approach_timeout_s=18.0, use_timeout_s=6.0)
    closed_payload = closed.to_payload()
    if not closed.success or closed.reason != "closed":
        raise AssertionError(f"close_openable failed: {closed_payload}")
    if block_open_property(body, GATE) != "false":
        raise AssertionError(f"oak fence gate was not closed after close_openable: {closed_payload}")

    return {
        "open_reason": opened.reason,
        "close_reason": closed.reason,
        "target": list(GATE),
        "navigation_reason": open_nav_reason,
        "final": body.get_state().pos,
    }


def run_door_open_close_happy(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_world(rcon, with_door=True)
    runtime = make_runtime(body)

    opened = runtime.open_openable(pos=DOOR, approach_timeout_s=18.0, use_timeout_s=6.0)
    opened_payload = opened.to_payload()
    if not opened.success or opened.reason != "opened":
        raise AssertionError(f"open_openable door failed: {opened_payload}")
    if block_open_property(body, DOOR) != "true":
        raise AssertionError(f"oak door was not open after open_openable: {opened_payload}")

    closed = runtime.close_openable(pos=DOOR, approach_timeout_s=18.0, use_timeout_s=6.0)
    closed_payload = closed.to_payload()
    if not closed.success or closed.reason != "closed":
        raise AssertionError(f"close_openable door failed: {closed_payload}")
    if block_open_property(body, DOOR) != "false":
        raise AssertionError(f"oak door was not closed after close_openable: {closed_payload}")

    return {
        "open_reason": opened.reason,
        "close_reason": closed.reason,
        "target": list(DOOR),
        "final": body.get_state().pos,
    }


def run_trapdoor_open_close_happy(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_world(rcon, with_bottom_trapdoor=True)
    runtime = make_runtime(body)

    opened = runtime.open_openable(pos=TRAPDOOR_BOTTOM, approach_timeout_s=18.0, use_timeout_s=6.0)
    opened_payload = opened.to_payload()
    if not opened.success or opened.reason != "opened":
        raise AssertionError(f"open_openable trapdoor failed: {opened_payload}")
    if block_open_property(body, TRAPDOOR_BOTTOM) != "true":
        raise AssertionError(f"oak trapdoor was not open after open_openable: {opened_payload}")

    closed = runtime.close_openable(pos=TRAPDOOR_BOTTOM, approach_timeout_s=18.0, use_timeout_s=6.0)
    closed_payload = closed.to_payload()
    if not closed.success or closed.reason != "closed":
        raise AssertionError(f"close_openable trapdoor failed: {closed_payload}")
    if block_open_property(body, TRAPDOOR_BOTTOM) != "false":
        raise AssertionError(f"oak trapdoor was not closed after close_openable: {closed_payload}")

    return {
        "open_reason": opened.reason,
        "close_reason": closed.reason,
        "target": list(TRAPDOOR_BOTTOM),
        "half": "bottom",
        "final": body.get_state().pos,
    }


def run_top_trapdoor_open_close_happy(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_world(rcon, with_top_trapdoor=True)
    runtime = make_runtime(body)

    opened = runtime.open_openable(pos=TRAPDOOR_TOP, approach_timeout_s=18.0, use_timeout_s=6.0)
    opened_payload = opened.to_payload()
    if not opened.success or opened.reason != "opened":
        raise AssertionError(f"open_openable top trapdoor failed: {opened_payload}")
    if block_open_property(body, TRAPDOOR_TOP) != "true":
        raise AssertionError(f"top-half oak trapdoor was not open after open_openable: {opened_payload}")

    closed = runtime.close_openable(pos=TRAPDOOR_TOP, approach_timeout_s=18.0, use_timeout_s=6.0)
    closed_payload = closed.to_payload()
    if not closed.success or closed.reason != "closed":
        raise AssertionError(f"close_openable top trapdoor failed: {closed_payload}")
    if block_open_property(body, TRAPDOOR_TOP) != "false":
        raise AssertionError(f"top-half oak trapdoor was not closed after close_openable: {closed_payload}")

    return {
        "open_reason": opened.reason,
        "close_reason": closed.reason,
        "target": list(TRAPDOOR_TOP),
        "half": "top",
        "final": body.get_state().pos,
    }


def run_already_open_closed_truth(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_world(rcon, with_door=True)
    command(rcon, f"setblock {DOOR[0]} {DOOR[1]} {DOOR[2]} oak_door[facing=east,half=lower,hinge=left,open=true,powered=false]")
    command(rcon, f"setblock {DOOR[0]} {DOOR[1] + 1} {DOOR[2]} oak_door[facing=east,half=upper,hinge=left,open=true,powered=false]")
    runtime = make_runtime(body)

    already_open = runtime.open_openable(pos=DOOR, approach_timeout_s=18.0, use_timeout_s=6.0)
    already_open_payload = already_open.to_payload()
    if not already_open.success or already_open.reason != "already_open":
        raise AssertionError(f"open_openable already-open truth failed: {already_open_payload}")
    if block_open_property(body, DOOR) != "true":
        raise AssertionError(f"door did not stay open on already_open truth: {already_open_payload}")

    closed = runtime.close_openable(pos=DOOR, approach_timeout_s=18.0, use_timeout_s=6.0)
    closed_payload = closed.to_payload()
    if not closed.success or closed.reason != "closed":
        raise AssertionError(f"close_openable after already-open failed: {closed_payload}")
    if block_open_property(body, DOOR) != "false":
        raise AssertionError(f"door did not close after already-open path: {closed_payload}")

    already_closed = runtime.close_openable(pos=DOOR, approach_timeout_s=18.0, use_timeout_s=6.0)
    already_closed_payload = already_closed.to_payload()
    if not already_closed.success or already_closed.reason != "already_closed":
        raise AssertionError(f"close_openable already-closed truth failed: {already_closed_payload}")
    if block_open_property(body, DOOR) != "false":
        raise AssertionError(f"door did not stay closed on already_closed truth: {already_closed_payload}")

    return {
        "already_open_reason": already_open.reason,
        "close_after_open_reason": closed.reason,
        "already_closed_reason": already_closed.reason,
        "target": list(DOOR),
    }


def run_not_found_inverse(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_world(rcon)
    runtime = make_runtime(body)
    before = body.get_state()
    result = runtime.open_openable(search_radius=6, approach_timeout_s=8.0, use_timeout_s=4.0)
    after = body.get_state()
    payload = result.to_payload()
    if result.success or result.reason != "openable_not_found":
        raise AssertionError(f"open_openable not-found inverse returned wrong truth: {payload}")
    if math.dist(before.pos, after.pos) > 0.75:
        raise AssertionError(f"open_openable not-found inverse moved the body: before={before.pos} after={after.pos} result={payload}")
    return {"reason": result.reason, "can_retry": result.can_retry, "before": before.pos, "after": after.pos}


def run_redstone_rejection(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_world(rcon, with_iron_door=True, with_iron_trapdoor=True)
    runtime = make_runtime(body)
    before_open = block_open_property(body, IRON_DOOR)
    door_result = runtime.open_openable(pos=IRON_DOOR, approach_timeout_s=8.0, use_timeout_s=4.0)
    after_open = block_open_property(body, IRON_DOOR)
    door_payload = door_result.to_payload()
    if door_result.success or door_result.reason != "openable_requires_redstone":
        raise AssertionError(f"open_openable iron-door rejection returned wrong truth: {door_payload}")
    if before_open != after_open:
        raise AssertionError(f"iron door changed despite redstone-only rejection: before={before_open} after={after_open} result={door_payload}")

    before_trapdoor = block_open_property(body, IRON_TRAPDOOR)
    trapdoor_result = runtime.open_openable(pos=IRON_TRAPDOOR, approach_timeout_s=8.0, use_timeout_s=4.0)
    after_trapdoor = block_open_property(body, IRON_TRAPDOOR)
    trapdoor_payload = trapdoor_result.to_payload()
    if trapdoor_result.success or trapdoor_result.reason != "openable_requires_redstone":
        raise AssertionError(f"open_openable iron-trapdoor rejection returned wrong truth: {trapdoor_payload}")
    if before_trapdoor != after_trapdoor:
        raise AssertionError(f"iron trapdoor changed despite redstone-only rejection: before={before_trapdoor} after={after_trapdoor} result={trapdoor_payload}")

    return {
        "iron_door_reason": door_result.reason,
        "iron_door_before_open": before_open,
        "iron_door_after_open": after_open,
        "iron_trapdoor_reason": trapdoor_result.reason,
        "iron_trapdoor_before_open": before_trapdoor,
        "iron_trapdoor_after_open": after_trapdoor,
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
                "gate_open_close": run_open_close_happy(rcon, body),
                "door_open_close": run_door_open_close_happy(rcon, body),
                "trapdoor_open_close": run_trapdoor_open_close_happy(rcon, body),
                "top_trapdoor_open_close": run_top_trapdoor_open_close_happy(rcon, body),
                "already_open_closed": run_already_open_closed_truth(rcon, body),
                "not_found": run_not_found_inverse(rcon, body),
                "redstone": run_redstone_rejection(rcon, body),
            }
        )


if __name__ == "__main__":
    main()
