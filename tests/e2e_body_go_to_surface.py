#!/usr/bin/env python3
"""go_to_surface transaction e2e against the local Carpet test server."""

from __future__ import annotations

import math
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.body import BlockWork, NavigationTransactions
from minebot.game import GovernancePolicy, GridCell, GridWorld, NavigationCostModel, RconClient, Region, ScarpetBody
from minebot.game.errors import RconError
from minebot.game.governance import BreakContext
from minebot.game.navigation import SegmentedNavigator
from minebot.game.rcon import RconConfig
from tests.e2e_support import spawn_or_fail


BOT = "E2ESurfaceBot"
ORIGIN = (170, 64, 0)
SURFACE = (171, 65, 0)
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
        "fill 166 63 -4 174 72 4 air",
        "fill 166 63 -4 174 63 4 stone",
    ]:
        command(rcon, cmd)


def flat_world(x_min: int, x_max: int, z_min: int, z_max: int, *, y_min: int = 64, y_max: int = 66) -> GridWorld:
    return GridWorld({(x, y, z): GridCell() for x in range(x_min, x_max + 1) for y in range(y_min, y_max + 1) for z in range(z_min, z_max + 1)})


def make_runtime(body: ScarpetBody, world: GridWorld | None = None) -> BlockWork:
    policy = GovernancePolicy(natural_regions=[Region("go_to_surface", (166, 55, -4), (174, 90, 4))])
    navigator = NavigationTransactions(
        body,
        SegmentedNavigator(world or flat_world(166, 174, -4, 4), NavigationCostModel(policy)),
    )
    return BlockWork(body, policy, navigator=navigator)


def reset_happy_world(rcon: RconClient) -> None:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill 166 63 -4 174 72 4 air")
    command(rcon, "setblock 170 63 0 stone")
    command(rcon, "setblock 170 65 0 dirt")
    command(rcon, "setblock 171 64 0 stone")
    command(rcon, f"tp {BOT} {ORIGIN[0]} {ORIGIN[1]} {ORIGIN[2]} 0 0")
    command(rcon, f"item replace entity {BOT} hotbar.0 with cobblestone 16")


def run_happy_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_happy_world(rcon)
    runtime = make_runtime(body)

    result = runtime.go_to_surface(
        current_pos=ORIGIN,
        context=BreakContext.DIRECT,
        scaffold_blocks=("cobblestone",),
        timeout_s=20.0,
        max_steps=2,
        surface_scan_height=3,
        world_top_y=70,
    )
    payload = result.to_payload()
    final = body.get_state()
    surface_block = body.perceive("blockAt", {"x": SURFACE[0], "y": SURFACE[1], "z": SURFACE[2]})
    pillar = body.perceive("blockAt", {"x": ORIGIN[0], "y": ORIGIN[1], "z": ORIGIN[2]})

    if not result.success or result.reason != "surface_reached":
        raise AssertionError(f"go_to_surface happy path failed: {payload} final={final}")
    if result.metrics.get("target_surface") != list(SURFACE):
        raise AssertionError(f"go_to_surface selected wrong surface target: {payload}")
    if result.metrics.get("final_pos") != list(SURFACE):
        raise AssertionError(f"go_to_surface did not verify final surface position: {payload} final={final}")
    ascent = result.metrics.get("ascent") or {}
    if ascent.get("reason") != "dig_up_target_reached" or ascent.get("metrics", {}).get("steps_completed") != 1:
        raise AssertionError(f"go_to_surface did not use guarded ascent: {payload}")
    approach = result.metrics.get("approach") or {}
    if approach.get("navigated") is not True or approach.get("result", {}).get("reason") != "arrived":
        raise AssertionError(f"go_to_surface did not use shared navigation to the surface exit: {payload}")
    terminal = result.metrics.get("terminal_surface") or {}
    if terminal.get("candidate") is not True:
        raise AssertionError(f"go_to_surface terminal surface truth not verified: {payload}")
    if surface_block.data.get("state") != "CLEAR":
        raise AssertionError(f"surface feet is not clear: {surface_block.data} result={payload}")
    if pillar.data.get("type") not in {"cobblestone", "minecraft:cobblestone"}:
        raise AssertionError(f"pillar block from ascent missing: {pillar.data} result={payload}")
    if math.dist(final.pos, (SURFACE[0] + 0.5, SURFACE[1], SURFACE[2] + 0.5)) > 1.25:
        raise AssertionError(f"final body position is not near surface target: final={final.pos} result={payload}")

    return {
        "reason": result.reason,
        "target_surface": result.metrics.get("target_surface"),
        "final": final.pos,
        "ascent_steps": ascent.get("metrics", {}).get("steps_completed"),
        "navigation_reason": approach.get("result", {}).get("reason"),
        "terminal_candidate": terminal.get("candidate"),
    }


def reset_no_surface_world(rcon: RconClient) -> None:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill 166 63 -4 174 72 4 air")
    command(rcon, "setblock 170 63 0 stone")
    command(rcon, "fill 168 66 -2 172 66 2 stone")
    command(rcon, f"tp {BOT} {ORIGIN[0]} {ORIGIN[1]} {ORIGIN[2]} 0 0")
    command(rcon, f"item replace entity {BOT} hotbar.0 with cobblestone 16")


def reset_same_level_exit_world(rcon: RconClient) -> None:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill 166 63 -4 174 72 4 air")
    command(rcon, "setblock 170 63 0 stone")
    command(rcon, "setblock 170 65 0 dirt")
    command(rcon, "setblock 170 66 0 dirt")
    command(rcon, "setblock 171 63 0 stone")
    command(rcon, f"tp {BOT} {ORIGIN[0]} {ORIGIN[1]} {ORIGIN[2]} 0 0")
    command(rcon, f"item replace entity {BOT} hotbar.0 with cobblestone 16")


def reset_alternate_column_world(rcon: RconClient) -> None:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill 166 63 -4 174 72 4 air")
    command(rcon, "setblock 170 63 0 stone")
    command(rcon, "setblock 170 65 0 dirt")
    command(rcon, "setblock 170 66 0 dirt")
    command(rcon, "setblock 171 63 0 stone")
    command(rcon, "setblock 171 65 0 air")
    command(rcon, "setblock 171 66 0 dirt")
    command(rcon, f"tp {BOT} {ORIGIN[0]} {ORIGIN[1]} {ORIGIN[2]} 0 0")
    command(rcon, f"item replace entity {BOT} hotbar.0 with cobblestone 16")


def reset_route_to_exit_world(rcon: RconClient) -> None:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill 166 63 -4 174 72 4 air")
    command(rcon, "fill 168 63 -2 172 63 2 stone")
    command(rcon, "fill 168 64 -2 172 64 2 stone")
    command(rcon, "fill 168 67 -2 172 67 2 dirt")
    command(rcon, "setblock 170 64 0 air")
    command(rcon, "setblock 170 65 0 air")
    command(rcon, "setblock 170 66 0 air")
    command(rcon, "setblock 171 65 0 air")
    command(rcon, "setblock 171 66 0 air")
    command(rcon, "setblock 172 65 0 air")
    command(rcon, "setblock 172 66 0 air")
    command(rcon, "setblock 172 67 0 air")
    command(rcon, f"tp {BOT} {ORIGIN[0]} {ORIGIN[1]} {ORIGIN[2]} 0 0")
    command(rcon, f"effect give {BOT} saturation 5 20 true")
    command(rcon, f"item replace entity {BOT} hotbar.0 with cobblestone 16")


def reset_staircase_fallback_world(rcon: RconClient) -> None:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill 166 63 -4 174 72 4 air")
    command(rcon, "fill 168 63 -2 172 63 2 stone")
    command(rcon, "fill 168 66 -2 172 66 2 chest")
    command(rcon, "fill 168 67 -2 172 67 2 chest")
    command(rcon, "setblock 171 64 0 stone")
    command(rcon, "setblock 170 66 0 air")
    command(rcon, "setblock 171 66 0 air")
    command(rcon, "setblock 171 67 0 air")
    command(rcon, "setblock 172 67 0 air")
    command(rcon, f"tp {BOT} {ORIGIN[0]} {ORIGIN[1]} {ORIGIN[2]} 0 0")
    command(rcon, f"item replace entity {BOT} hotbar.0 with cobblestone 16")


def reset_multi_step_staircase_fallback_world(rcon: RconClient) -> None:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill 166 63 -4 174 72 4 air")
    command(rcon, "fill 168 63 -2 172 63 2 stone")
    command(rcon, "fill 168 66 -2 172 66 2 chest[facing=north]")
    command(rcon, "setblock 171 64 0 stone")
    command(rcon, "setblock 171 66 0 air")
    command(rcon, "setblock 171 67 0 chest")
    command(rcon, "setblock 172 65 0 stone")
    command(rcon, "setblock 172 66 0 air")
    command(rcon, "setblock 172 67 0 air")
    command(rcon, f"tp {BOT} {ORIGIN[0]} {ORIGIN[1]} {ORIGIN[2]} 0 0")
    command(rcon, f"item replace entity {BOT} hotbar.0 with cobblestone 16")


def run_same_level_exit_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_same_level_exit_world(rcon)
    runtime = make_runtime(body)

    result = runtime.go_to_surface(
        current_pos=ORIGIN,
        context=BreakContext.DIRECT,
        scaffold_blocks=("cobblestone",),
        timeout_s=20.0,
        max_steps=2,
        surface_scan_height=0,
        world_top_y=70,
    )
    payload = result.to_payload()
    final = body.get_state()
    if not result.success or result.reason != "surface_reached":
        raise AssertionError(f"go_to_surface same-level-exit path failed: {payload} final={final}")
    if result.metrics.get("target_surface") != [171, 64, 0]:
        raise AssertionError(f"go_to_surface same-level-exit selected wrong target: {payload}")
    approach = result.metrics.get("approach") or {}
    if approach.get("navigated") is not True or approach.get("final_pos") != [171, 64, 0]:
        raise AssertionError(f"go_to_surface same-level-exit did not approach the alternate surface: {payload}")
    if result.metrics.get("column_approach") is not None:
        raise AssertionError(f"go_to_surface same-level alternate surface should not need column approach: {payload}")
    ascent = result.metrics.get("ascent") or {}
    if ascent.get("reason") != "dig_up_target_reached" or ascent.get("metrics", {}).get("steps_completed") != 0:
        raise AssertionError(f"go_to_surface same-level-exit should not pillar on same-Y surface: {payload}")
    terminal = result.metrics.get("terminal_surface") or {}
    if terminal.get("candidate") is not True:
        raise AssertionError(f"go_to_surface same-level-exit terminal truth not verified: {payload}")
    if math.dist(final.pos, (171.5, 64.0, 0.5)) > 1.0:
        raise AssertionError(f"go_to_surface same-level-exit final body position wrong: final={final.pos} result={payload}")
    return {
        "reason": result.reason,
        "target_surface": result.metrics.get("target_surface"),
        "ascent_origin": result.metrics.get("ascent_origin"),
        "approach_final": approach.get("final_pos"),
        "final": final.pos,
        "ascent_steps": ascent.get("metrics", {}).get("steps_completed"),
    }


def run_alternate_column_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_alternate_column_world(rcon)
    runtime = make_runtime(body)

    result = runtime.go_to_surface(
        current_pos=ORIGIN,
        context=BreakContext.DIRECT,
        scaffold_blocks=("cobblestone",),
        timeout_s=20.0,
        max_steps=2,
        surface_scan_height=2,
        world_top_y=70,
    )
    payload = result.to_payload()
    final = body.get_state()
    if not result.success or result.reason != "surface_reached":
        raise AssertionError(f"go_to_surface alternate-column path failed: {payload} final={final}")
    if result.metrics.get("target_surface") != [171, 65, 0]:
        raise AssertionError(f"go_to_surface alternate-column selected wrong target: {payload}")
    if result.metrics.get("ascent_origin") != [171, 64, 0]:
        raise AssertionError(f"go_to_surface alternate-column selected wrong ascent origin: {payload}")
    column = result.metrics.get("column_approach") or {}
    if column.get("navigated") is not True or column.get("final_pos") != [171, 64, 0]:
        raise AssertionError(f"go_to_surface alternate-column did not approach ascent column: {payload}")
    ascent = result.metrics.get("ascent") or {}
    if ascent.get("reason") != "dig_up_target_reached" or ascent.get("metrics", {}).get("steps_completed") != 1:
        raise AssertionError(f"go_to_surface alternate-column should pillar once after column approach: {payload}")
    if result.metrics.get("approach") is not None:
        raise AssertionError(f"go_to_surface alternate-column should not need final surface approach: {payload}")
    terminal = result.metrics.get("terminal_surface") or {}
    if result.metrics.get("terminal_surface_verified") is not True:
        raise AssertionError(f"go_to_surface alternate-column terminal truth not verified: {payload}")
    legality = terminal.get("support_legality") or {}
    if legality.get("reason") != "allowed_bot_owned":
        raise AssertionError(f"go_to_surface alternate-column terminal support was not bot-owned scaffold: {payload}")
    if math.dist(final.pos, (171.5, 65.0, 0.5)) > 1.0:
        raise AssertionError(f"go_to_surface alternate-column final body position wrong: final={final.pos} result={payload}")
    return {
        "reason": result.reason,
        "target_surface": result.metrics.get("target_surface"),
        "ascent_origin": result.metrics.get("ascent_origin"),
        "column_final": column.get("final_pos"),
        "final": final.pos,
        "ascent_steps": ascent.get("metrics", {}).get("steps_completed"),
    }


def run_route_to_exit_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_route_to_exit_world(rcon)
    runtime = make_runtime(body)

    result = runtime.go_to_surface(
        current_pos=ORIGIN,
        context=BreakContext.DIRECT,
        scaffold_blocks=("cobblestone",),
        timeout_s=20.0,
        max_steps=2,
        surface_scan_height=2,
        surface_scan_radius=2,
        world_top_y=70,
    )
    payload = result.to_payload()
    final = body.get_state()
    if not result.success or result.reason != "surface_reached":
        raise AssertionError(f"go_to_surface route-to-exit path failed: {payload} final={final}")
    if result.metrics.get("target_surface") != [172, 65, 0]:
        raise AssertionError(f"go_to_surface route-to-exit selected wrong target: {payload}")
    if result.metrics.get("ascent_origin") != list(ORIGIN):
        raise AssertionError(f"go_to_surface route-to-exit should ascend from origin: {payload}")
    if result.metrics.get("column_approach") is not None:
        raise AssertionError(f"go_to_surface route-to-exit should not use alternate-column approach: {payload}")
    ascent = result.metrics.get("ascent") or {}
    if ascent.get("reason") != "dig_up_target_reached" or ascent.get("metrics", {}).get("steps_completed") != 1:
        raise AssertionError(f"go_to_surface route-to-exit should ascend once before routing: {payload}")
    approach = result.metrics.get("approach") or {}
    if approach.get("navigated") is not True or approach.get("final_pos") != [172, 65, 0]:
        raise AssertionError(f"go_to_surface route-to-exit did not route to the wider exit: {payload}")
    terminal = result.metrics.get("terminal_surface") or {}
    if terminal.get("candidate") is not True:
        raise AssertionError(f"go_to_surface route-to-exit terminal truth not verified: {payload}")
    if math.dist(final.pos, (172.5, 65.0, 0.5)) > 1.0:
        raise AssertionError(f"go_to_surface route-to-exit final body position wrong: final={final.pos} result={payload}")
    return {
        "reason": result.reason,
        "target_surface": result.metrics.get("target_surface"),
        "ascent_origin": result.metrics.get("ascent_origin"),
        "approach_final": approach.get("final_pos"),
        "final": final.pos,
        "ascent_steps": ascent.get("metrics", {}).get("steps_completed"),
    }


def run_staircase_fallback_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_staircase_fallback_world(rcon)
    policy = GovernancePolicy(natural_regions=[Region("go_to_surface", (166, 55, -4), (174, 90, 4))])
    navigator = NavigationTransactions(
        body,
        SegmentedNavigator(flat_world(166, 174, -4, 4), NavigationCostModel(policy)),
    )
    runtime = BlockWork(body, policy, navigator=navigator)

    result = runtime.go_to_surface(
        current_pos=ORIGIN,
        context=BreakContext.DIRECT,
        scaffold_blocks=("cobblestone",),
        timeout_s=20.0,
        max_steps=2,
        surface_scan_height=2,
        surface_scan_radius=2,
        allow_staircase_fallback=True,
        world_top_y=70,
    )
    payload = result.to_payload()
    final = body.get_state()
    if not result.success or result.reason != "surface_reached":
        raise AssertionError(f"go_to_surface staircase-fallback path failed: {payload} final={final}")
    if result.metrics.get("target_surface") != [171, 65, 0]:
        raise AssertionError(f"go_to_surface staircase-fallback selected wrong target: {payload}")
    if result.metrics.get("ascent") is not None:
        raise AssertionError(f"go_to_surface staircase-fallback should not pillar when shared navigation can ascend: {payload}")
    if result.metrics.get("column_approach") is not None:
        raise AssertionError(f"go_to_surface staircase-fallback should not use column approach: {payload}")
    approach = result.metrics.get("approach") or {}
    if approach.get("navigated") is not True or approach.get("final_pos") != [171, 65, 0]:
        raise AssertionError(f"go_to_surface staircase-fallback did not route to the surface step: {payload}")
    fallback = result.metrics.get("staircase_fallback") or {}
    if fallback.get("attempted") is not True or fallback.get("success") is not True:
        raise AssertionError(f"go_to_surface staircase-fallback metrics did not record success: {payload}")
    terminal = result.metrics.get("terminal_surface") or {}
    if terminal.get("candidate") is not True:
        raise AssertionError(f"go_to_surface staircase-fallback terminal truth not verified: {payload}")
    pillar = body.perceive("blockAt", {"x": ORIGIN[0], "y": ORIGIN[1], "z": ORIGIN[2]})
    if pillar.data.get("type") in {"cobblestone", "minecraft:cobblestone"}:
        raise AssertionError(f"go_to_surface staircase-fallback placed a pillar: pillar={pillar.data} result={payload}")
    if math.dist(final.pos, (171.0, 65.0, 0.0)) > 1.25:
        raise AssertionError(f"go_to_surface staircase-fallback final body position wrong: final={final.pos} result={payload}")
    return {
        "reason": result.reason,
        "target_surface": result.metrics.get("target_surface"),
        "approach_final": approach.get("final_pos"),
        "final": final.pos,
        "staircase_fallback": fallback.get("success"),
    }


def run_multi_step_staircase_fallback_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_multi_step_staircase_fallback_world(rcon)
    cells = {
        (x, y, z): GridCell()
        for x in range(168, 173)
        for y in range(64, 67)
        for z in range(-2, 3)
    }
    for x in range(168, 173):
        for z in range(-2, 3):
            cells[(x, 66, z)] = GridCell(block_type="chest", walkable=False)
    cells[(171, 64, 0)] = GridCell(block_type="stone", walkable=False)
    cells[(172, 65, 0)] = GridCell(block_type="stone", walkable=False)
    cells[(172, 66, 0)] = GridCell()
    cells[(171, 66, 0)] = GridCell()
    runtime = make_runtime(body, GridWorld(cells))

    result = runtime.go_to_surface(
        current_pos=ORIGIN,
        context=BreakContext.DIRECT,
        scaffold_blocks=("cobblestone",),
        timeout_s=20.0,
        max_steps=3,
        surface_scan_height=3,
        surface_scan_radius=3,
        allow_staircase_fallback=True,
        world_top_y=70,
    )
    payload = result.to_payload()
    final = body.get_state()
    if not result.success or result.reason != "surface_reached":
        raise AssertionError(f"go_to_surface multi-step staircase-fallback path failed: {payload} final={final}")
    if result.metrics.get("target_surface") != [172, 66, 0]:
        raise AssertionError(f"go_to_surface multi-step staircase-fallback selected wrong target: {payload}")
    if result.metrics.get("ascent") is not None:
        raise AssertionError(f"go_to_surface multi-step staircase-fallback should not pillar when shared navigation can ascend: {payload}")
    if result.metrics.get("column_approach") is not None:
        raise AssertionError(f"go_to_surface multi-step staircase-fallback should not use column approach: {payload}")
    approach = result.metrics.get("approach") or {}
    if approach.get("navigated") is not True or approach.get("final_pos") != [172, 66, 0]:
        raise AssertionError(f"go_to_surface multi-step staircase-fallback did not route to the higher surface: {payload}")
    fallback = result.metrics.get("staircase_fallback") or {}
    if fallback.get("attempted") is not True or fallback.get("success") is not True:
        raise AssertionError(f"go_to_surface multi-step staircase-fallback metrics did not record success: {payload}")
    terminal = result.metrics.get("terminal_surface") or {}
    if terminal.get("candidate") is not True:
        raise AssertionError(f"go_to_surface multi-step staircase-fallback terminal truth not verified: {payload}")
    pillar = body.perceive("blockAt", {"x": ORIGIN[0], "y": ORIGIN[1], "z": ORIGIN[2]})
    if pillar.data.get("type") in {"cobblestone", "minecraft:cobblestone"}:
        raise AssertionError(f"go_to_surface multi-step staircase-fallback placed a pillar: pillar={pillar.data} result={payload}")
    if math.dist(final.pos, (172.5, 66.0, 0.5)) > 1.25:
        raise AssertionError(f"go_to_surface multi-step staircase-fallback final body position wrong: final={final.pos} result={payload}")
    return {
        "reason": result.reason,
        "target_surface": result.metrics.get("target_surface"),
        "approach_final": approach.get("final_pos"),
        "final": final.pos,
        "staircase_fallback": fallback.get("success"),
    }


def run_not_found_inverse(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_no_surface_world(rcon)
    runtime = make_runtime(body)
    before = body.get_state()

    result = runtime.go_to_surface(
        current_pos=ORIGIN,
        context=BreakContext.DIRECT,
        scaffold_blocks=("cobblestone",),
        timeout_s=8.0,
        max_steps=2,
        surface_scan_height=1,
        world_top_y=70,
    )
    after = body.get_state()
    payload = result.to_payload()
    if result.success or result.reason != "surface_not_found_in_column":
        raise AssertionError(f"go_to_surface not-found inverse returned wrong truth: {payload}")
    if math.dist(before.pos, after.pos) > 0.75:
        raise AssertionError(f"go_to_surface not-found inverse moved the body: before={before.pos} after={after.pos} result={payload}")
    pillar = body.perceive("blockAt", {"x": ORIGIN[0], "y": ORIGIN[1], "z": ORIGIN[2]})
    if pillar.data.get("type") in {"cobblestone", "minecraft:cobblestone"}:
        raise AssertionError(f"go_to_surface not-found inverse placed a pillar: pillar={pillar.data} result={payload}")
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
        spawn_or_fail(body, ORIGIN)
        command(rcon, f"gamemode survival {BOT}")
        command(rcon, f"effect clear {BOT}")

        cases = {
            "happy": lambda: run_happy_path(rcon, body),
            "same_level_exit": lambda: run_same_level_exit_path(rcon, body),
            "alternate_column": lambda: run_alternate_column_path(rcon, body),
            "route_to_exit": lambda: run_route_to_exit_path(rcon, body),
            "staircase_fallback": lambda: run_staircase_fallback_path(rcon, body),
            "multi_step_staircase_fallback": lambda: run_multi_step_staircase_fallback_path(rcon, body),
            "not_found": lambda: run_not_found_inverse(rcon, body),
        }
        default_cases = [
            "happy",
            "same_level_exit",
            "alternate_column",
            "route_to_exit",
            "staircase_fallback",
            "not_found",
        ]
        selected_raw = os.environ.get("MINEBOT_SURFACE_CASES")
        selected = [name.strip() for name in selected_raw.split(",") if name.strip()] if selected_raw else default_cases
        unknown = [name for name in selected if name not in cases]
        if unknown:
            raise AssertionError(f"unknown go-to-surface e2e cases: {unknown}")
        print({name: cases[name]() for name in selected})


if __name__ == "__main__":
    main()
