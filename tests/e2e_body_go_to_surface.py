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
from minebot.game import GovernancePolicy, RconClient, Region, ScarpetBody
from minebot.game.errors import RconError
from minebot.game.governance import BreakContext
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


def make_runtime(body: ScarpetBody) -> BlockWork:
    policy = GovernancePolicy(natural_regions=[Region("go_to_surface", (166, 55, -4), (174, 90, 4))])
    navigator = NavigationTransactions.server_side(body, policy)
    return BlockWork(body, policy, navigator=navigator)


def navigation_facts(result) -> tuple[dict[str, object], list[dict[str, object]], dict[str, int]]:
    navigation = result.metrics.get("navigation") or {}
    segments = (navigation.get("metrics") or {}).get("segments") or []
    kinds = ("walk", "diagonal", "ascend", "descend", "swim", "fall", "break", "place", "pillar")
    movement_counts = {
        kind: sum(int((segment.get("diagnostics", {}).get("movement_counts") or {}).get(kind, 0)) for segment in segments)
        for kind in kinds
    }
    return navigation, segments, movement_counts


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
    source_head = body.perceive("blockAt", {"x": ORIGIN[0], "y": ORIGIN[1] + 1, "z": ORIGIN[2]})

    if not result.success or result.reason != "surface_reached":
        raise AssertionError(f"go_to_surface happy path failed: {payload} final={final}")
    if result.metrics.get("target_surface") != list(SURFACE):
        raise AssertionError(f"go_to_surface selected wrong surface target: {payload}")
    if result.metrics.get("final_pos") != list(SURFACE):
        raise AssertionError(f"go_to_surface did not verify final surface position: {payload} final={final}")
    navigation, segments, movement_counts = navigation_facts(result)
    if navigation.get("success") is not True or navigation.get("reason") != "arrived":
        raise AssertionError(f"go_to_surface did not use shared navigation to the surface exit: {payload}")
    mutation_events = [
        event
        for segment in segments
        for event in segment.get("diagnostics", {}).get("mutation_events") or []
    ]
    source_clearance = any(
        event.get("event") == "navigateMutationDone"
        and event.get("data", {}).get("kind") == "break"
        and event.get("data", {}).get("pos") == [ORIGIN[0], ORIGIN[1] + 1, ORIGIN[2]]
        and event.get("data", {}).get("success") is True
        for event in mutation_events
    )
    if movement_counts["break"] < 1 or movement_counts["ascend"] < 1 or not source_clearance:
        raise AssertionError(f"go_to_surface did not clear source headroom then ascend through shared navigation: {payload}")
    terminal = result.metrics.get("terminal_surface") or {}
    if terminal.get("candidate") is not True:
        raise AssertionError(f"go_to_surface terminal surface truth not verified: {payload}")
    if surface_block.data.get("state") != "CLEAR":
        raise AssertionError(f"surface feet is not clear: {surface_block.data} result={payload}")
    if source_head.data.get("state") != "CLEAR":
        raise AssertionError(f"source headroom was not cleared: {source_head.data} result={payload}")
    if math.dist(final.pos, (SURFACE[0] + 0.5, SURFACE[1], SURFACE[2] + 0.5)) > 1.25:
        raise AssertionError(f"final body position is not near surface target: final={final.pos} result={payload}")

    return {
        "reason": result.reason,
        "target_surface": result.metrics.get("target_surface"),
        "final": final.pos,
        "movement_counts": movement_counts,
        "navigation_reason": navigation.get("reason"),
        "terminal_candidate": terminal.get("candidate"),
    }


def reset_no_surface_world(rcon: RconClient) -> None:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill 166 63 -4 174 72 4 air")
    command(rcon, "setblock 170 63 0 stone")
    command(rcon, "fill 168 66 -2 172 66 2 chest")
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
    command(rcon, "setblock 170 66 0 dirt")
    command(rcon, "setblock 171 64 0 stone")
    command(rcon, "setblock 171 66 0 air")
    command(rcon, "setblock 171 67 0 dirt")
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
    navigation, _segments, movement_counts = navigation_facts(result)
    if navigation.get("success") is not True or navigation.get("reason") != "arrived":
        raise AssertionError(f"go_to_surface same-level-exit did not use shared navigation: {payload}")
    if movement_counts["walk"] < 1 or any(movement_counts[kind] for kind in ("break", "place", "pillar")):
        raise AssertionError(f"go_to_surface same-level-exit used the wrong movement profile: {payload}")
    terminal = result.metrics.get("terminal_surface") or {}
    if terminal.get("candidate") is not True:
        raise AssertionError(f"go_to_surface same-level-exit terminal truth not verified: {payload}")
    if math.dist(final.pos, (171.5, 64.0, 0.5)) > 1.0:
        raise AssertionError(f"go_to_surface same-level-exit final body position wrong: final={final.pos} result={payload}")
    return {
        "reason": result.reason,
        "target_surface": result.metrics.get("target_surface"),
        "final": final.pos,
        "movement_counts": movement_counts,
    }


def run_pickaxe_capability_paths(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_happy_world(rcon)
    command(rcon, "setblock 170 65 0 stone")
    command(rcon, f"clear {BOT}")
    runtime = make_runtime(body)

    bare = runtime.go_to_surface(
        current_pos=ORIGIN,
        context=BreakContext.DIRECT,
        scaffold_blocks=(),
        timeout_s=20.0,
        max_steps=2,
        surface_scan_height=3,
        world_top_y=70,
    )
    bare_payload = bare.to_payload()
    bare_navigation, bare_segments, _bare_counts = navigation_facts(bare)
    bare_events = [
        event
        for segment in bare_segments
        for event in segment.get("diagnostics", {}).get("mutation_events") or []
    ]
    blocked = body.perceive("blockAt", {"x": ORIGIN[0], "y": ORIGIN[1] + 1, "z": ORIGIN[2]})
    expected_bare_reasons = {"surface_navigation_failed:unreachable", "surface_navigation_failed:budget_exceeded"}
    if bare.success or bare.reason not in expected_bare_reasons:
        raise AssertionError(f"go_to_surface did not reject the stone path without a pickaxe: {bare_payload}")
    if bare_navigation.get("reason") not in {"unreachable", "budget_exceeded"}:
        raise AssertionError(f"pickaxe capability failure did not remain planner-terminal: {bare_payload}")
    if bare_events:
        raise AssertionError(f"pickaxe capability failure proposed a mutation: {bare_payload}")
    if blocked.data.get("type") not in {"stone", "minecraft:stone"}:
        raise AssertionError(f"pickaxe capability failure changed stone: block={blocked.data} result={bare_payload}")

    reset_happy_world(rcon)
    command(rcon, "setblock 170 65 0 stone")
    command(rcon, f"clear {BOT}")
    command(rcon, f"item replace entity {BOT} hotbar.0 with wooden_pickaxe")
    equipped = runtime.go_to_surface(
        current_pos=ORIGIN,
        context=BreakContext.DIRECT,
        scaffold_blocks=(),
        timeout_s=20.0,
        max_steps=2,
        surface_scan_height=3,
        world_top_y=70,
    )
    equipped_payload = equipped.to_payload()
    _equipped_navigation, _equipped_segments, equipped_counts = navigation_facts(equipped)
    cleared = body.perceive("blockAt", {"x": ORIGIN[0], "y": ORIGIN[1] + 1, "z": ORIGIN[2]})
    if not equipped.success or equipped.reason != "surface_reached":
        raise AssertionError(f"go_to_surface did not use a wooden pickaxe for stone: {equipped_payload}")
    if equipped_counts["break"] < 1:
        raise AssertionError(f"go_to_surface reached surface without recording the stone break: {equipped_payload}")
    if cleared.data.get("type") not in {"air", "minecraft:air"}:
        raise AssertionError(f"go_to_surface did not break the stone with a wooden pickaxe: block={cleared.data} result={equipped_payload}")
    return {
        "bare_reason": bare.reason,
        "equipped_reason": equipped.reason,
        "equipped_break_count": equipped_counts["break"],
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
    navigation, _segments, movement_counts = navigation_facts(result)
    if navigation.get("success") is not True or navigation.get("reason") != "arrived":
        raise AssertionError(f"go_to_surface alternate-column did not use shared navigation: {payload}")
    if movement_counts["pillar"] < 1:
        raise AssertionError(f"go_to_surface alternate-column did not create a governed pillar step: {payload}")
    domain_entry = next(
        (entry for entry in (result.metrics.get("surface_domain") or {}).get("candidates") or [] if entry.get("feet_pos") == [171, 65, 0]),
        None,
    )
    if domain_entry is None or domain_entry.get("support_mode") != "constructible_pillar":
        raise AssertionError(f"go_to_surface alternate-column lost constructible surface facts: {payload}")
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
        "final": final.pos,
        "movement_counts": movement_counts,
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
    navigation, _segments, movement_counts = navigation_facts(result)
    if navigation.get("success") is not True or navigation.get("reason") != "arrived":
        raise AssertionError(f"go_to_surface route-to-exit did not use shared navigation: {payload}")
    if movement_counts["ascend"] < 1 or movement_counts["walk"] < 1:
        raise AssertionError(f"go_to_surface route-to-exit did not ascend then route to the exit: {payload}")
    terminal = result.metrics.get("terminal_surface") or {}
    if terminal.get("candidate") is not True:
        raise AssertionError(f"go_to_surface route-to-exit terminal truth not verified: {payload}")
    if math.dist(final.pos, (172.5, 65.0, 0.5)) > 1.0:
        raise AssertionError(f"go_to_surface route-to-exit final body position wrong: final={final.pos} result={payload}")
    return {
        "reason": result.reason,
        "target_surface": result.metrics.get("target_surface"),
        "final": final.pos,
        "movement_counts": movement_counts,
    }


def run_staircase_fallback_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_staircase_fallback_world(rcon)
    policy = GovernancePolicy(natural_regions=[Region("go_to_surface", (166, 55, -4), (174, 90, 4))])
    navigator = NavigationTransactions.server_side(body, policy)
    runtime = BlockWork(body, policy, navigator=navigator)

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
        raise AssertionError(f"go_to_surface staircase-fallback path failed: {payload} final={final}")
    if result.metrics.get("target_surface") != [171, 65, 0]:
        raise AssertionError(f"go_to_surface staircase-fallback selected wrong target: {payload}")
    navigation, _segments, movement_counts = navigation_facts(result)
    if navigation.get("success") is not True or navigation.get("reason") != "arrived":
        raise AssertionError(f"go_to_surface staircase fixture did not use shared navigation: {payload}")
    if movement_counts["ascend"] < 1 or movement_counts["pillar"] != 0:
        raise AssertionError(f"go_to_surface staircase fixture did not use the expected ascend route: {payload}")
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
        "final": final.pos,
        "movement_counts": movement_counts,
    }


def run_multi_step_staircase_fallback_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_multi_step_staircase_fallback_world(rcon)
    runtime = make_runtime(body)

    result = runtime.go_to_surface(
        current_pos=ORIGIN,
        context=BreakContext.DIRECT,
        scaffold_blocks=("cobblestone",),
        timeout_s=20.0,
        max_steps=3,
        surface_scan_height=3,
        surface_scan_radius=3,
        world_top_y=70,
    )
    payload = result.to_payload()
    final = body.get_state()
    if not result.success or result.reason != "surface_reached":
        raise AssertionError(f"go_to_surface multi-step staircase-fallback path failed: {payload} final={final}")
    if result.metrics.get("target_surface") != [172, 66, 0]:
        raise AssertionError(f"go_to_surface multi-step staircase-fallback selected wrong target: {payload}")
    navigation, _segments, movement_counts = navigation_facts(result)
    if navigation.get("success") is not True or navigation.get("reason") != "arrived":
        raise AssertionError(f"go_to_surface multi-step staircase fixture did not use shared navigation: {payload}")
    if movement_counts["break"] < 2 or movement_counts["ascend"] < 2 or movement_counts["pillar"] != 0:
        raise AssertionError(f"go_to_surface multi-step staircase fixture did not clear both caps and ascend twice: {payload}")
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
        "final": final.pos,
        "movement_counts": movement_counts,
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
            "pickaxe_capability": lambda: run_pickaxe_capability_paths(rcon, body),
            "same_level_exit": lambda: run_same_level_exit_path(rcon, body),
            "alternate_column": lambda: run_alternate_column_path(rcon, body),
            "route_to_exit": lambda: run_route_to_exit_path(rcon, body),
            "staircase_fallback": lambda: run_staircase_fallback_path(rcon, body),
            "multi_step_staircase_fallback": lambda: run_multi_step_staircase_fallback_path(rcon, body),
            "not_found": lambda: run_not_found_inverse(rcon, body),
        }
        default_cases = [
            "happy",
            "pickaxe_capability",
            "same_level_exit",
            "alternate_column",
            "route_to_exit",
            "staircase_fallback",
            "multi_step_staircase_fallback",
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
