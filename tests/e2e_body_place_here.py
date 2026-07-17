#!/usr/bin/env python3
"""place_here transaction e2e against the local Carpet test server."""

from __future__ import annotations

import math
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.body import BlockWork, NavigationRunConfig, NavigationTransactions
from minebot.game import GovernancePolicy, RconClient, Region, ScarpetBody
from minebot.game.errors import RconError
from minebot.game.governance import BreakContext, PlaceContext
from minebot.game.rcon import RconConfig
from tests.e2e_support import spawn_or_fail


BOT = "E2EPlaceHere"
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
        "fill -5 59 -5 5 66 5 air",
        "fill -5 59 -5 5 66 5 air replace water",
        "fill -5 59 -5 5 66 5 air replace flowing_water",
        "fill -5 59 -5 5 66 5 air replace lava",
        "fill -5 59 -5 5 66 5 air replace flowing_lava",
        "fill -5 58 -5 5 58 5 stone",
    ]:
        command(rcon, cmd)


def make_runtime(body: ScarpetBody, *, protect_adjacent_targets: bool = False) -> BlockWork:
    protected = []
    if protect_adjacent_targets:
        protected = [
            Region("adjacent_north", (0, 59, -1), (0, 59, -1)),
            Region("adjacent_west", (-1, 59, 0), (-1, 59, 0)),
            Region("adjacent_east", (1, 59, 0), (1, 59, 0)),
            Region("adjacent_south", (0, 59, 1), (0, 59, 1)),
        ]
    policy = GovernancePolicy(
        natural_regions=[Region("place_here", (-5, 0, -5), (5, 100, 5))],
        protected_regions=protected,
    )
    navigator = NavigationTransactions.server_side(body, policy)
    return BlockWork(body, policy, navigator=navigator)


def make_natural_runtime(body: ScarpetBody) -> BlockWork:
    policy = GovernancePolicy(
        natural_regions=[Region("fixed_natural_place_here", (48, 0, -72), (72, 120, -40))],
    )
    navigator = NavigationTransactions.server_side(body, policy)
    return BlockWork(body, policy, navigator=navigator)


def reset_position(rcon: RconClient, body: ScarpetBody | None = None) -> None:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill -5 59 -5 5 66 5 air")
    command(rcon, "fill -5 58 -5 5 58 5 stone")
    command(rcon, f"tp {BOT} 0.5 59 0.5 -90 0")
    command(rcon, f"item replace entity {BOT} weapon.mainhand with cobblestone 8")
    if body is not None:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if math.dist(body.get_state().pos, (0.5, 59.0, 0.5)) < 1.25:
                return
            time.sleep(0.05)
        raise AssertionError(f"body did not reach reset position after teleport: final={body.get_state().pos}")


def run_happy_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_position(rcon, body)
    runtime = make_runtime(body, protect_adjacent_targets=True)

    result = runtime.place_here(
        "minecraft:cobblestone",
        radius=2,
        context=PlaceContext.WORK,
        purpose="bridge",
        timeout_s=18.0,
    )
    payload = result.to_payload()
    place_here = (result.metrics or {}).get("place_here") or {}
    chosen = place_here.get("chosen_target")
    if not result.success or result.reason != "completed":
        raise AssertionError(f"place_here happy path failed: {payload}")
    if not chosen:
        raise AssertionError(f"place_here did not expose chosen target: {payload}")

    block_after = body.perceive("blockAt", {"x": chosen[0], "y": chosen[1], "z": chosen[2]})
    if block_after.data.get("type") not in {"cobblestone", "minecraft:cobblestone"}:
        raise AssertionError(f"place_here target was not placed: target={chosen} block={block_after.data} result={payload}")

    attempts = place_here.get("attempts") or []
    if not attempts:
        raise AssertionError(f"place_here did not expose attempts: {payload}")
    approach = attempts[-1].get("approach") or {}
    if approach.get("navigated") is not True:
        raise AssertionError(f"place_here did not use shared navigation before placement: {payload}")
    nav_attempts = approach.get("attempts") or []
    if not nav_attempts or nav_attempts[-1].get("result", {}).get("reason") != "arrived":
        raise AssertionError(f"place_here navigation did not arrive: {payload}")
    if not any(attempt.get("result", {}).get("reason") == "place_denied:protected_region" for attempt in attempts):
        raise AssertionError(f"place_here did not skip protected adjacent targets before navigating: {payload}")

    cleanup = runtime.governance.can_break(tuple(chosen), "minecraft:cobblestone", BreakContext.BOT_CLEANUP)
    if not cleanup.allowed:
        raise AssertionError(f"place_here bot placement ledger did not allow cleanup: target={chosen} cleanup={cleanup} result={payload}")

    final = body.get_state()
    chosen_center = (float(chosen[0]) + 0.5, float(chosen[1]), float(chosen[2]) + 0.5)
    return {
        "reason": result.reason,
        "chosen_target": chosen,
        "final": final.pos,
        "distance_to_target": round(math.dist(final.pos, chosen_center), 3),
        "navigation_reason": nav_attempts[-1]["result"].get("reason"),
        "block_after": block_after.data,
    }


def run_no_supported_spot_inverse(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_position(rcon, body)
    command(rcon, "fill -2 58 -2 2 58 2 water")
    command(rcon, "setblock 0 58 0 stone")
    runtime = make_runtime(body)

    before = body.get_state()
    result = runtime.place_here(
        "minecraft:cobblestone",
        radius=1,
        context=PlaceContext.WORK,
        purpose="bridge",
        timeout_s=8.0,
    )
    after = body.get_state()
    payload = result.to_payload()

    if result.success or result.reason != "place_here_no_supported_spot":
        raise AssertionError(f"place_here no-support inverse returned wrong truth: {payload}")
    if math.dist(before.pos, after.pos) > 0.75:
        raise AssertionError(f"place_here no-support inverse moved the body: before={before.pos} after={after.pos} result={payload}")
    for candidate in (result.metrics or {}).get("candidates") or []:
        target = candidate.get("target") or []
        block = body.perceive("blockAt", {"x": target[0], "y": target[1], "z": target[2]})
        if block.data.get("type") in {"cobblestone", "minecraft:cobblestone"}:
            raise AssertionError(f"place_here no-support inverse placed a block: target={target} block={block.data} result={payload}")

    return {
        "reason": result.reason,
        "can_retry": result.can_retry,
        "before": before.pos,
        "after": after.pos,
        "candidate_count": len((result.metrics or {}).get("candidates") or []),
        "scan": (result.metrics or {}).get("scan"),
    }


def run_vertical_surface_happy_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_position(rcon, body)
    command(rcon, "fill -2 58 -2 2 58 2 water")
    command(rcon, "setblock 0 58 0 stone")
    command(rcon, "setblock 1 61 0 grass_block")
    command(rcon, "setblock 2 61 0 grass_block")
    command(rcon, "fill 1 62 0 2 63 0 air")
    runtime = make_runtime(body)

    result = runtime.place_here(
        "minecraft:cobblestone",
        radius=2,
        context=PlaceContext.WORK,
        purpose="bridge",
        timeout_s=18.0,
    )
    payload = result.to_payload()
    place_here = (result.metrics or {}).get("place_here") or {}
    chosen = place_here.get("chosen_target")
    scan = place_here.get("scan") or {}
    if not result.success or result.reason != "completed":
        raise AssertionError(f"place_here vertical-surface happy path failed: {payload}")
    if chosen != [1, 62, 0]:
        raise AssertionError(f"place_here chose the wrong elevated target: {payload}")
    if scan.get("vertical_fallback") is not True:
        raise AssertionError(f"place_here did not expose vertical fallback truth: {payload}")
    placed = body.perceive("blockAt", {"x": 1, "y": 62, "z": 0})
    if placed.data.get("type") not in {"cobblestone", "minecraft:cobblestone"}:
        raise AssertionError(f"place_here did not place on the elevated surface: block={placed.data} result={payload}")
    return {
        "reason": result.reason,
        "chosen_target": chosen,
        "vertical_delta": place_here.get("candidates", [{}])[0].get("vertical_delta"),
        "final": body.get_state().pos,
        "scan": scan,
    }


def run_fixed_natural_crafting_table_surface(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, f"clear {BOT}")
    command(rcon, f"item replace entity {BOT} weapon.mainhand with crafting_table 1")
    command(rcon, f"tp {BOT} 59.62 63 -58.7 -90 0")
    runtime = make_natural_runtime(body)

    result = runtime.place_here(
        "minecraft:crafting_table",
        radius=2,
        context=PlaceContext.DIRECT,
        purpose="workstation",
        timeout_s=18.0,
    )
    payload = result.to_payload()
    place_here = (result.metrics or {}).get("place_here") or {}
    chosen = place_here.get("chosen_target")
    scan = place_here.get("scan") or {}
    if not result.success or result.reason != "completed":
        raise AssertionError(f"fixed-world crafting-table placement failed: {payload}")
    if not chosen or int(scan.get("candidate_count") or 0) < 1:
        raise AssertionError(f"fixed-world placement did not expose a bounded surface candidate: {payload}")
    if int(scan.get("columns_scanned") or 0) > BlockWork.PLACE_HERE_COLUMN_LIMIT:
        raise AssertionError(f"fixed-world placement exceeded its column budget: {payload}")
    placed = body.perceive("blockAt", {"x": chosen[0], "y": chosen[1], "z": chosen[2]})
    if placed.data.get("type") not in {"crafting_table", "minecraft:crafting_table"}:
        raise AssertionError(f"fixed-world crafting table lacks terminal block truth: block={placed.data} result={payload}")

    cleanup = runtime.mine_block(tuple(chosen), context=BreakContext.BOT_CLEANUP, timeout_s=18.0)
    if not cleanup.success:
        raise AssertionError(f"fixed-world crafting table cleanup failed: {cleanup.to_payload()}")
    return {
        "reason": result.reason,
        "chosen_target": chosen,
        "vertical_delta": place_here.get("candidates", [{}])[0].get("vertical_delta"),
        "vertical_fallback": scan.get("vertical_fallback"),
        "scan": scan,
        "cleanup_reason": cleanup.reason,
    }


def _block_pos(body: ScarpetBody) -> tuple[int, int, int]:
    pos = body.get_state().pos
    return (math.floor(pos[0]), math.floor(pos[1]), math.floor(pos[2]))


def _place_here_targets(origin: tuple[int, int, int], radius: int) -> tuple[tuple[int, int, int], ...]:
    candidates: list[tuple[int, float, tuple[int, int, int]]] = []
    for dz in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            if dx == 0 and dz == 0:
                continue
            target = (origin[0] + dx, origin[1], origin[2] + dz)
            manhattan = abs(dx) + abs(dz)
            distance = math.dist(
                (float(origin[0]), float(origin[1]), float(origin[2])),
                (float(target[0]), float(target[1]), float(target[2])),
            )
            candidates.append((manhattan, distance, target))
    candidates.sort(key=lambda item: (item[0], item[1], item[2][2], item[2][0]))
    return tuple(target for _manhattan, _distance, target in candidates)


def _stand_points(target: tuple[int, int, int]) -> tuple[tuple[int, int, int], ...]:
    return (
        (target[0] + 1, target[1], target[2]),
        (target[0] - 1, target[1], target[2]),
        (target[0], target[1], target[2] + 1),
        (target[0], target[1], target[2] - 1),
    )


def _block_state(body: ScarpetBody, pos: tuple[int, int, int]) -> str:
    block = body.perceive("blockAt", {"x": pos[0], "y": pos[1], "z": pos[2]})
    return str(block.data.get("state") or "UNKNOWN")


def _assert_no_stand_points(body: ScarpetBody, target: tuple[int, int, int]) -> None:
    valid = []
    for stand_y in (target[1], target[1] - 1):
        for point in _stand_points((target[0], stand_y, target[2])):
            state = _block_state(body, point)
            head = _block_state(body, (point[0], point[1] + 1, point[2]))
            below = _block_state(body, (point[0], point[1] - 1, point[2]))
            if state == "CLEAR" and head == "CLEAR" and below == "SOLID":
                valid.append(point)
    if valid:
        raise AssertionError(f"place_here recovery preflight left valid stand points: target={target} valid={valid}")


def _prepare_single_remote_candidate(
    rcon: RconClient,
    body: ScarpetBody,
    *,
    offset: tuple[int, int],
) -> tuple[tuple[int, int, int], tuple[int, int, int], tuple[int, int, int]]:
    reset_position(rcon, body)
    origin = _block_pos(body)
    y = origin[1]
    command(rcon, f"fill {origin[0] - 3} {y} {origin[2] - 3} {origin[0] + 3} {y + 1} {origin[2] + 3} air")
    command(rcon, f"fill {origin[0] - 3} {y - 1} {origin[2] - 3} {origin[0] + 3} {y - 1} {origin[2] + 3} stone")
    target = (origin[0] + offset[0], y, origin[2] + offset[1])
    stand = (target[0] + 1, y, target[2])

    for candidate in _place_here_targets(origin, 1):
        command(rcon, f"setblock {candidate[0]} {candidate[1]} {candidate[2]} air")
        command(rcon, f"setblock {candidate[0]} {candidate[1] - 1} {candidate[2]} air")
        command(rcon, f"setblock {candidate[0]} {candidate[1] - 2} {candidate[2]} air")

    command(rcon, f"setblock {target[0]} {target[1] - 1} {target[2]} stone")
    command(rcon, f"setblock {target[0]} {target[1]} {target[2]} air")
    for point in _stand_points(target):
        command(rcon, f"setblock {point[0]} {point[1]} {point[2]} air")
        command(rcon, f"setblock {point[0]} {point[1] - 1} {point[2]} air")
        command(rcon, f"setblock {point[0]} {point[1] - 2} {point[2]} air")
        command(rcon, f"setblock {point[0]} {point[1] + 1} {point[2]} air")
    command(rcon, f"setblock {stand[0]} {stand[1] - 1} {stand[2]} stone")
    command(rcon, f"setblock {stand[0]} {stand[1] - 2} {stand[2]} air")
    command(rcon, f"setblock {stand[0]} {stand[1] + 1} {stand[2]} air")

    supported = []
    for candidate in _place_here_targets(origin, 1):
        target_state = _block_state(body, candidate)
        support_state = _block_state(body, (candidate[0], candidate[1] - 1, candidate[2]))
        if target_state == "CLEAR" and support_state == "SOLID":
            supported.append(candidate)
    if supported != [target]:
        raise AssertionError(f"place_here recovery preflight expected one supported target: origin={origin} supported={supported} target={target}")
    return origin, target, stand


def run_stand_position_recovery(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    _origin, target, stand = _prepare_single_remote_candidate(rcon, body, offset=(1, 1))
    command(rcon, f"setblock {stand[0]} {stand[1]} {stand[2]} dirt")
    _assert_no_stand_points(body, target)
    runtime = make_runtime(body)

    result = runtime.place_here(
        "minecraft:cobblestone",
        radius=1,
        context=PlaceContext.WORK,
        purpose="bridge",
        timeout_s=18.0,
    )
    payload = result.to_payload()
    place_here = (result.metrics or {}).get("place_here") or {}
    recovery = place_here.get("stand_position_recovery") or {}
    chosen = place_here.get("chosen_target")

    if not result.success or result.reason != "completed":
        raise AssertionError(f"place_here stand-position recovery failed: {payload}")
    if recovery.get("recovered") is not True or recovery.get("stand_pos") != list(stand):
        raise AssertionError(f"place_here stand-position recovery was not recorded: {payload}")
    cleared = body.perceive("blockAt", {"x": stand[0], "y": stand[1], "z": stand[2]})
    if cleared.data.get("state") != "CLEAR":
        raise AssertionError(f"place_here stand-position recovery did not clear stand feet: block={cleared.data} result={payload}")
    if chosen != list(target):
        raise AssertionError(f"place_here stand-position recovery chose wrong target: {payload}")
    placed = body.perceive("blockAt", {"x": target[0], "y": target[1], "z": target[2]})
    if placed.data.get("type") not in {"cobblestone", "minecraft:cobblestone"}:
        raise AssertionError(f"place_here stand-position recovery did not place target: block={placed.data} result={payload}")
    attempts = place_here.get("attempts") or []
    approach = attempts[-1].get("approach") if attempts else {}
    if not approach or approach.get("navigated") is not True:
        raise AssertionError(f"place_here stand-position recovery did not navigate to recovered stand: {payload}")

    return {
        "reason": result.reason,
        "chosen_target": chosen,
        "stand_position_recovery": recovery,
        "stand_block_after": cleared.data,
        "target_block_after": placed.data,
    }


def run_headroom_recovery(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    _origin, target, stand = _prepare_single_remote_candidate(rcon, body, offset=(1, 1))
    command(rcon, f"setblock {stand[0]} {stand[1]} {stand[2]} air")
    head = (stand[0], stand[1] + 1, stand[2])
    command(rcon, f"setblock {head[0]} {head[1]} {head[2]} dirt")
    _assert_no_stand_points(body, target)
    runtime = make_runtime(body)

    result = runtime.place_here(
        "minecraft:cobblestone",
        radius=1,
        context=PlaceContext.WORK,
        purpose="bridge",
        timeout_s=18.0,
    )
    payload = result.to_payload()
    place_here = (result.metrics or {}).get("place_here") or {}
    recovery = place_here.get("headroom_recovery") or {}
    chosen = place_here.get("chosen_target")

    if not result.success or result.reason != "completed":
        raise AssertionError(f"place_here headroom recovery failed: {payload}")
    if recovery.get("recovered") is not True or recovery.get("head_pos") != list(head):
        raise AssertionError(f"place_here headroom recovery was not recorded: {payload}")
    cleared = body.perceive("blockAt", {"x": head[0], "y": head[1], "z": head[2]})
    if cleared.data.get("state") != "CLEAR":
        raise AssertionError(f"place_here headroom recovery did not clear stand head: block={cleared.data} result={payload}")
    if chosen != list(target):
        raise AssertionError(f"place_here headroom recovery chose wrong target: {payload}")
    placed = body.perceive("blockAt", {"x": target[0], "y": target[1], "z": target[2]})
    if placed.data.get("type") not in {"cobblestone", "minecraft:cobblestone"}:
        raise AssertionError(f"place_here headroom recovery did not place target: block={placed.data} result={payload}")
    attempts = place_here.get("attempts") or []
    approach = attempts[-1].get("approach") if attempts else {}
    if not approach or approach.get("navigated") is not True:
        raise AssertionError(f"place_here headroom recovery did not navigate to recovered stand: {payload}")

    return {
        "reason": result.reason,
        "chosen_target": chosen,
        "headroom_recovery": recovery,
        "head_block_after": cleared.data,
        "target_block_after": placed.data,
    }


def run_stand_point_creation(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    _origin, target, stand = _prepare_single_remote_candidate(rcon, body, offset=(1, 1))
    command(rcon, f"setblock {stand[0]} {stand[1]} {stand[2]} dirt")
    head = (stand[0], stand[1] + 1, stand[2])
    command(rcon, f"setblock {head[0]} {head[1]} {head[2]} dirt")
    _assert_no_stand_points(body, target)
    runtime = make_runtime(body)

    result = runtime.place_here(
        "minecraft:cobblestone",
        radius=1,
        context=PlaceContext.WORK,
        purpose="bridge",
        timeout_s=18.0,
    )
    payload = result.to_payload()
    place_here = (result.metrics or {}).get("place_here") or {}
    stand_recovery = place_here.get("stand_position_recovery") or {}
    head_recovery = place_here.get("headroom_recovery") or {}
    chosen = place_here.get("chosen_target")

    if not result.success or result.reason != "completed":
        raise AssertionError(f"place_here stand-point creation failed: {payload}")
    if stand_recovery.get("recovered") is not True or stand_recovery.get("stand_pos") != list(stand):
        raise AssertionError(f"place_here did not record stand-position recovery during stand-point creation: {payload}")
    cleared_stand = body.perceive("blockAt", {"x": stand[0], "y": stand[1], "z": stand[2]})
    if cleared_stand.data.get("state") != "CLEAR":
        raise AssertionError(
            f"place_here stand-point creation did not clear stand feet: block={cleared_stand.data} result={payload}"
        )
    cleared_head = body.perceive("blockAt", {"x": head[0], "y": head[1], "z": head[2]})
    if cleared_head.data.get("state") != "CLEAR":
        raise AssertionError(
            f"place_here stand-point creation did not clear stand head: block={cleared_head.data} result={payload}"
        )
    if chosen != list(target):
        raise AssertionError(f"place_here stand-point creation chose wrong target: {payload}")
    placed = body.perceive("blockAt", {"x": target[0], "y": target[1], "z": target[2]})
    if placed.data.get("type") not in {"cobblestone", "minecraft:cobblestone"}:
        raise AssertionError(f"place_here stand-point creation did not place target: block={placed.data} result={payload}")
    attempts = place_here.get("attempts") or []
    approach = attempts[-1].get("approach") if attempts else {}
    if not approach or approach.get("navigated") is not True:
        raise AssertionError(f"place_here stand-point creation did not navigate to created stand point: {payload}")

    return {
        "reason": result.reason,
        "chosen_target": chosen,
        "stand_position_recovery": stand_recovery,
        "headroom_recovery": head_recovery,
        "stand_block_after": cleared_stand.data,
        "head_block_after": cleared_head.data,
        "target_block_after": placed.data,
    }


def run_stand_point_creation_illegal_inverse(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    _origin, target, stand = _prepare_single_remote_candidate(rcon, body, offset=(1, 1))
    command(rcon, f"setblock {stand[0]} {stand[1]} {stand[2]} chest[facing=north]")
    head = (stand[0], stand[1] + 1, stand[2])
    command(rcon, f"setblock {head[0]} {head[1]} {head[2]} chest[facing=north]")
    _assert_no_stand_points(body, target)
    runtime = make_runtime(body)

    before = body.get_state()
    result = runtime.place_here(
        "minecraft:cobblestone",
        radius=1,
        context=PlaceContext.WORK,
        purpose="bridge",
        timeout_s=18.0,
    )
    after = body.get_state()
    payload = result.to_payload()

    if result.success or result.reason != "place_here_no_stand_point":
        raise AssertionError(f"place_here illegal stand-point creation inverse returned wrong truth: {payload}")
    if math.dist(before.pos, after.pos) > 0.75:
        raise AssertionError(
            f"place_here illegal stand-point creation inverse moved the body: before={before.pos} after={after.pos} result={payload}"
        )
    placed = body.perceive("blockAt", {"x": target[0], "y": target[1], "z": target[2]})
    if placed.data.get("type") in {"cobblestone", "minecraft:cobblestone"}:
        raise AssertionError(
            f"place_here illegal stand-point creation inverse placed target anyway: block={placed.data} result={payload}"
        )
    stand_block = body.perceive("blockAt", {"x": stand[0], "y": stand[1], "z": stand[2]})
    head_block = body.perceive("blockAt", {"x": head[0], "y": head[1], "z": head[2]})
    if stand_block.data.get("type") not in {"chest", "minecraft:chest"} or head_block.data.get("type") not in {
        "chest",
        "minecraft:chest",
    }:
        raise AssertionError(
            f"place_here illegal stand-point creation inverse mutated blockers: stand={stand_block.data} head={head_block.data} result={payload}"
        )

    return {
        "reason": result.reason,
        "before": before.pos,
        "after": after.pos,
        "stand_block": stand_block.data,
        "head_block": head_block.data,
        "target_block_after": placed.data,
    }


def run_step_support_happy_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_position(rcon, body)
    command(rcon, "fill -5 59 -5 5 66 5 air")
    command(rcon, "fill -5 58 -5 5 58 5 air")
    command(rcon, "setblock 0 58 0 stone")
    command(rcon, "setblock 1 58 0 smooth_stone_slab[type=bottom]")
    command(rcon, "setblock -1 58 0 oak_stairs[facing=east,half=bottom,shape=straight]")
    command(rcon, f"tp {BOT} 0.5 59 0.5 -90 0")
    runtime = make_runtime(body)

    result = runtime.place_here(
        "minecraft:cobblestone",
        radius=1,
        context=PlaceContext.WORK,
        purpose="bridge",
        timeout_s=18.0,
    )
    payload = result.to_payload()
    place_here = (result.metrics or {}).get("place_here") or {}
    chosen = place_here.get("chosen_target")
    if not result.success or result.reason != "completed":
        raise AssertionError(f"place_here step-support happy path failed: {payload}")
    if chosen not in ([1, 59, 0], [-1, 59, 0]):
        raise AssertionError(f"place_here did not choose one of the step-supported targets: {payload}")
    block_after = body.perceive("blockAt", {"x": chosen[0], "y": chosen[1], "z": chosen[2]})
    if block_after.data.get("type") not in {"cobblestone", "minecraft:cobblestone"}:
        raise AssertionError(f"place_here step-support target was not placed: target={chosen} block={block_after.data} result={payload}")
    support = body.perceive("blockAt", {"x": chosen[0], "y": chosen[1] - 1, "z": chosen[2]})
    support_type = str(support.data.get("type") or "")
    if support_type not in {
        "smooth_stone_slab",
        "minecraft:smooth_stone_slab",
        "oak_stairs",
        "minecraft:oak_stairs",
    }:
        raise AssertionError(f"place_here step-support support block changed unexpectedly: support={support.data} result={payload}")
    attempts = place_here.get("attempts") or []
    if not attempts or attempts[-1].get("result", {}).get("reason") != "completed":
        raise AssertionError(f"place_here step-support attempts did not record completion: {payload}")
    return {
        "reason": result.reason,
        "chosen_target": chosen,
        "support_block": support.data,
        "target_block_after": block_after.data,
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
            "stand_position_recovery": lambda: run_stand_position_recovery(rcon, body),
            "headroom_recovery": lambda: run_headroom_recovery(rcon, body),
            "stand_point_creation": lambda: run_stand_point_creation(rcon, body),
            "stand_point_creation_illegal": lambda: run_stand_point_creation_illegal_inverse(rcon, body),
            "step_support": lambda: run_step_support_happy_path(rcon, body),
            "vertical_surface": lambda: run_vertical_surface_happy_path(rcon, body),
            "fixed_natural_surface": lambda: run_fixed_natural_crafting_table_surface(rcon, body),
            "no_supported_spot": lambda: run_no_supported_spot_inverse(rcon, body),
        }
        selected_raw = os.environ.get("MINEBOT_PLACE_HERE_CASES")
        selected = [name.strip() for name in selected_raw.split(",") if name.strip()] if selected_raw else list(cases.keys())
        unknown = [name for name in selected if name not in cases]
        if unknown:
            raise AssertionError(f"unknown place-here e2e cases: {unknown}")
        print({name: cases[name]() for name in selected})


if __name__ == "__main__":
    main()
