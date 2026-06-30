#!/usr/bin/env python3
"""Navigation transaction e2e against the local Carpet test server."""

from __future__ import annotations

import math
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.body.navigation import NavigationRunConfig, NavigationTransactions, make_block_at_prism_world_update
from minebot.game import (
    BreakContext,
    GovernancePolicy,
    RconClient,
    Region,
    ScarpetBody,
)
from minebot.game.navigation import (
    GoalNear,
    GoalYLevel,
    GridCell,
    GridWorld,
    MoveKind,
    NavigationCostModel,
    NavigationSegment,
    PathResult,
    PathStep,
    RecheckResult,
    SegmentedNavigator,
)
from minebot.game.errors import RconError
from minebot.game.rcon import RconConfig
from tests.e2e_support import spawn_or_fail


BOT = "E2ENavBot"
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
        "fill -4 59 -4 12 66 4 air",
        "fill -4 58 -4 12 58 4 stone",
    ]:
        command(rcon, cmd)


def flat_world(x_min: int, x_max: int, z_min: int, z_max: int, *, y: int = 59) -> GridWorld:
    return GridWorld({(x, y, z): GridCell() for x in range(x_min, x_max + 1) for z in range(z_min, z_max + 1)})


def corridor_world(x_min: int, x_max: int, *, y: int = 59) -> GridWorld:
    return GridWorld({
        (x, y + dy, z): GridCell()
        for x in range(x_min, x_max + 1)
        for dy in (-1, 0, 1)
        for z in (-1, 0, 1)
    })


def distance(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def navigation_runtime(body: ScarpetBody, world: GridWorld, policy: GovernancePolicy) -> NavigationTransactions:
    costs = NavigationCostModel(policy)
    navigator = SegmentedNavigator(world, costs)
    return NavigationTransactions(body, navigator)


def wait_for_named_event(body: ScarpetBody, name: str, timeout_s: float = 8.0):
    deadline = time.monotonic() + timeout_s
    seen = 0
    while time.monotonic() < deadline:
        events = body.event_log[seen:]
        seen = len(body.event_log)
        events.extend(body.poll_events())
        for event in events:
            if event.name == name:
                return event
        time.sleep(0.05)
    raise TimeoutError(f"timed out waiting for event {name}")


class FakeNavigator:
    def __init__(self, segments: list[NavigationSegment], *, world=None, costs=None):
        self.segments = list(segments)
        self.world = world
        self.costs = costs

    def next_segment(self, start, goal, **kwargs):
        if len(self.segments) == 1:
            return self.segments[0]
        return self.segments.pop(0)


def fake_segment(
    status: str,
    target: tuple[int, int, int] | None,
    *,
    success: bool,
    reason: str,
    path: tuple[PathStep, ...] | None = None,
) -> NavigationSegment:
    if path is None:
        path = ()
    if target is not None and not path:
        path = (PathStep(pos=target, move=MoveKind.WALK, cost=1.0, reason="walk"),)
    return NavigationSegment(
        status=status,
        target=target,
        plan=PathResult(success=success, reason=reason, path=tuple(path), cost=1.0, expanded=1),
        recheck=RecheckResult(ok=True, reason="valid", checked=len(path)),
    )


def run_typed_goal_happy_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, f"tp {BOT} 0 59 0 -90 0")
    policy = GovernancePolicy(natural_regions=[Region("nav_test", (-4, 0, -4), (12, 100, 4))])
    runtime = navigation_runtime(body, flat_world(-4, 12, -4, 4), policy)
    goal = GoalNear((8, 59, 0), radius=1)

    result = runtime.navigate_to(
        goal,
        config=NavigationRunConfig(max_segments=2, segment_timeout_s=20.0, min_partial_progress=2),
    )
    final = body.get_state()
    dist = distance(final.pos, (8, 59, 0))
    if not result.success or result.reason != "arrived":
        raise AssertionError(f"typed-goal navigation did not arrive: {result.to_payload()}")
    if dist > 1.75:
        raise AssertionError(f"typed-goal final position too far: final={final.pos} dist={dist:.3f}")
    metrics = result.metrics or {}
    if metrics.get("navigation_goal", {}).get("kind") != "near":
        raise AssertionError(f"typed goal payload not preserved: {result.to_payload()}")
    return {
        "reason": result.reason,
        "final": final.pos,
        "dist": round(dist, 3),
        "metrics": result.to_payload(),
    }


def run_diagonal_happy_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill -2 59 -2 4 66 4 air")
    command(rcon, "fill -2 58 -2 4 58 4 stone")
    command(rcon, f"tp {BOT} 0.2 59 0.2 -45 0")
    cells = {
        (0, 59, 0): GridCell(),
        (1, 59, 0): GridCell(),
        (0, 59, 1): GridCell(),
        (1, 59, 1): GridCell(),
    }
    policy = GovernancePolicy(natural_regions=[Region("nav_diagonal", (-2, 0, -2), (4, 100, 4))])
    runtime = navigation_runtime(body, GridWorld(cells), policy)

    result = runtime.navigate_to(
        (1, 59, 1),
        config=NavigationRunConfig(max_segments=1, segment_timeout_s=12.0, min_partial_progress=1),
    )
    final = body.get_state()
    payload = result.to_payload()
    if not result.success or result.reason != "arrived":
        raise AssertionError(f"diagonal navigation did not arrive: result={payload} final={final}")
    if distance(final.pos, (1, 59, 1)) > 1.25:
        raise AssertionError(f"diagonal final position too far: final={final.pos} result={payload}")
    segments = (result.metrics or {}).get("segments", [])
    segment = segments[0]["diagnostics"]["segment"] if segments else {}
    if segment.get("path_moves") != ["diagonal"]:
        raise AssertionError(f"diagonal path did not expose diagonal move: {payload}")
    if segment.get("movement_waypoints") != [[1, 59, 1]]:
        raise AssertionError(f"diagonal waypoint was not preserved: {payload}")
    return {
        "reason": result.reason,
        "final": final.pos,
        "path_moves": segment.get("path_moves"),
        "movement_waypoints": segment.get("movement_waypoints"),
        "metrics": payload,
    }


def run_diagonal_protected_corner_failure(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill -2 59 -2 4 66 4 air")
    command(rcon, "fill -2 58 -2 4 58 4 stone")
    command(rcon, "setblock 1 59 0 stone")
    command(rcon, f"tp {BOT} 0.2 59 0.2 -45 0")
    cells = {
        (0, 59, 0): GridCell(),
        (1, 59, 0): GridCell(block_type="stone", walkable=False),
        (1, 59, 1): GridCell(),
    }
    policy = GovernancePolicy(
        natural_regions=[Region("nav_diagonal_corner", (-2, 0, -2), (4, 100, 4))],
        protected_regions=[Region("protected_corner", (1, 59, 0), (1, 59, 0))],
    )
    runtime = navigation_runtime(body, GridWorld(cells), policy)

    result = runtime.navigate_to(
        (1, 59, 1),
        break_context=BreakContext.TRAVEL,
        config=NavigationRunConfig(max_segments=1, segment_timeout_s=8.0, min_partial_progress=1),
    )
    final = body.get_state()
    payload = result.to_payload()
    if result.success:
        raise AssertionError(f"diagonal protected-corner unexpectedly succeeded: {payload} final={final}")
    if result.reason != "navigation_blocked:no_path" or result.can_retry:
        raise AssertionError(f"diagonal protected-corner returned wrong reason: {payload} final={final}")
    if distance(final.pos, (0, 59, 0)) > 0.9:
        raise AssertionError(f"diagonal protected-corner moved the bot before dispatch denial: final={final.pos} result={payload}")
    segments = (result.metrics or {}).get("segments", [])
    if len(segments) != 1:
        raise AssertionError(f"diagonal protected-corner should report one planned segment: {payload}")
    if segments[0].get("action_id") is not None or segments[0].get("terminal_reason") is not None:
        raise AssertionError(f"diagonal protected-corner dispatched a body action: {payload}")
    path_update = (result.metrics or {}).get("path_update", {})
    blocked_reasons = path_update.get("blocked_reasons", {})
    if blocked_reasons.get("diagonal_corner_blocked:break_denied:protected_region", 0) < 1:
        raise AssertionError(f"diagonal protected-corner did not expose diagonal corner denial: {payload}")
    block = body.perceive("blockAt", {"x": 1, "y": 59, "z": 0})
    if block.data.get("type") not in {"stone", "minecraft:stone"} or block.data.get("state") != "SOLID":
        raise AssertionError(f"diagonal protected-corner mutated protected block: block={block.data} result={payload}")
    return {
        "reason": result.reason,
        "final": final.pos,
        "path_update": path_update,
        "server_block": block.data,
        "metrics": payload,
    }


def run_recheck_diagonal_corner_headroom_blocks_dispatch(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill -2 59 -2 4 66 4 air")
    command(rcon, "fill -2 58 -2 4 58 4 stone")
    command(rcon, "setblock 1 60 0 stone")
    command(rcon, f"tp {BOT} 0.2 59 0.2 -45 0")
    planned_cells = {
        (0, 59, 0): GridCell(),
        (1, 59, 0): GridCell(),
        (0, 59, 1): GridCell(),
        (1, 59, 1): GridCell(),
    }
    recheck_cells = dict(planned_cells)
    recheck_cells[(1, 59, 0)] = GridCell(headroom_block="stone")
    recheck_cells[(1, 60, 0)] = GridCell(block_type="stone", walkable=False)
    policy = GovernancePolicy(natural_regions=[Region("nav_diagonal_corner_headroom", (-2, 0, -2), (4, 100, 4))])
    runtime = NavigationTransactions(
        body,
        SegmentedNavigator(GridWorld(planned_cells), NavigationCostModel(policy)),
    )

    result = runtime.navigate_to(
        (1, 59, 1),
        config=NavigationRunConfig(
            max_segments=1,
            segment_timeout_s=8.0,
            min_partial_progress=1,
            recheck_world=GridWorld(recheck_cells),
        ),
    )
    final = body.get_state()
    payload = result.to_payload()
    if result.success:
        raise AssertionError(f"recheck diagonal corner headroom unexpectedly succeeded: {payload} final={final}")
    if result.reason != "navigation_replan_required:diagonal_corner_headroom_blocked" or not result.can_retry:
        raise AssertionError(f"recheck diagonal corner headroom returned wrong reason: {payload} final={final}")
    if distance(final.pos, (0, 59, 0)) > 0.9:
        raise AssertionError(f"recheck diagonal corner headroom moved the bot before denial: final={final.pos} result={payload}")
    segments = (result.metrics or {}).get("segments", [])
    if len(segments) != 1:
        raise AssertionError(f"recheck diagonal corner headroom should report one planned segment: {payload}")
    if segments[0].get("action_id") is not None or segments[0].get("terminal_reason") is not None:
        raise AssertionError(f"recheck diagonal corner headroom dispatched a body action: {payload}")
    segment = segments[0]["diagnostics"]["segment"]
    if segment.get("plan_reason") != "arrived" or segment.get("recheck_reason") != "diagonal_corner_headroom_blocked":
        raise AssertionError(f"recheck diagonal corner headroom did not expose plan/recheck contrast: {payload}")
    if segment.get("path_moves") != ["diagonal"]:
        raise AssertionError(f"recheck diagonal corner headroom did not preserve diagonal path shape: {payload}")
    path_update = (result.metrics or {}).get("path_update", {})
    if path_update.get("source") != "recheck" or path_update.get("category") != "goal_changed_or_world_changed":
        raise AssertionError(f"recheck diagonal corner headroom path_update classification wrong: {payload}")
    ceiling = body.perceive("blockAt", {"x": 1, "y": 60, "z": 0})
    if ceiling.data.get("type") not in {"stone", "minecraft:stone"} or ceiling.data.get("state") != "SOLID":
        raise AssertionError(f"recheck diagonal corner headroom mutated ceiling block: block={ceiling.data} result={payload}")
    return {
        "reason": result.reason,
        "final": final.pos,
        "path_update": path_update,
        "ceiling_block": ceiling.data,
        "segment": segment,
        "metrics": payload,
    }


def run_protected_wall_failure(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, f"tp {BOT} 0 59 0 -90 0")
    cells = {
        (0, 59, 0): GridCell(),
        (1, 59, 0): GridCell(block_type="stone", walkable=False),
        (2, 59, 0): GridCell(),
    }
    policy = GovernancePolicy(
        natural_regions=[Region("nav_test", (0, 0, 0), (2, 100, 0))],
        protected_regions=[Region("protected_wall", (1, 0, 0), (1, 100, 0))],
    )
    runtime = navigation_runtime(body, GridWorld(cells), policy)

    result = runtime.navigate_to(
        (2, 59, 0),
        config=NavigationRunConfig(max_segments=1, segment_timeout_s=5.0, min_partial_progress=1),
    )
    if result.success:
        raise AssertionError(f"protected-wall navigation unexpectedly succeeded: {result.to_payload()}")
    if result.reason != "navigation_blocked:no_path":
        raise AssertionError(f"expected protected no-path failure, got: {result.to_payload()}")
    path_update = (result.metrics or {}).get("path_update", {})
    blocked_reasons = path_update.get("blocked_reasons", {})
    if blocked_reasons.get("break_denied:protected_region") != 1:
        raise AssertionError(f"failure did not expose protected break denial: {result.to_payload()}")
    return result.to_payload()


def run_break_wall_happy_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill -4 59 -4 8 66 4 air")
    command(rcon, "fill -4 58 -4 8 58 4 stone")
    command(rcon, "setblock 2 59 0 stone")
    command(rcon, f"tp {BOT} 0 59 0 -90 0")
    command(rcon, f"item replace entity {BOT} weapon.mainhand with diamond_pickaxe")
    cells = flat_world(-1, 4, -1, 1).cells
    cells[(2, 59, 0)] = GridCell(block_type="stone", walkable=False)
    policy = GovernancePolicy(natural_regions=[Region("nav_break", (-1, 0, -1), (4, 100, 1))])
    runtime = navigation_runtime(body, GridWorld(cells), policy)

    result = runtime.navigate_to(
        (4, 59, 0),
        break_context=BreakContext.TRAVEL,
        config=NavigationRunConfig(max_segments=5, segment_timeout_s=35.0, min_partial_progress=1),
    )
    final = body.get_state()
    block_after = body.perceive("blockAt", {"x": 2, "y": 59, "z": 0})
    dist = distance(final.pos, (4, 59, 0))
    payload = result.to_payload()
    if not result.success or result.reason != "arrived":
        raise AssertionError(f"break-wall navigation did not arrive: result={payload} final={final} block={block_after.data}")
    if block_after.data.get("state") != "CLEAR":
        raise AssertionError(f"break-wall navigation did not clear obstacle: result={payload} block={block_after.data}")
    if dist > 1.25:
        raise AssertionError(f"break-wall final position too far: final={final.pos} dist={dist:.3f} result={payload}")
    actions = [
        action.get("terminal_reason")
        for action in (result.metrics or {}).get("segments", [])
    ]
    return {
        "reason": result.reason,
        "final": final.pos,
        "dist": round(dist, 3),
        "block_after": block_after.data,
        "segment_terminal_reasons": actions,
        "metrics": payload,
    }


def run_open_gate_path_happy_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill -2 59 -2 4 66 2 air")
    command(rcon, "fill -2 58 -2 4 58 2 stone")
    command(rcon, "setblock 1 59 0 oak_fence_gate[facing=east,in_wall=false,open=false,powered=false]")
    command(rcon, f"tp {BOT} 0 59 0 -90 0")
    cells = {
        (0, 59, 0): GridCell(),
        (1, 59, 0): GridCell(block_type="oak_fence_gate", walkable=False),
        (2, 59, 0): GridCell(),
    }
    policy = GovernancePolicy(natural_regions=[Region("nav_open_gate", (-2, 0, -2), (4, 100, 2))])
    runtime = navigation_runtime(body, GridWorld(cells), policy)

    result = runtime.navigate_to((2, 59, 0), config=NavigationRunConfig(max_segments=2, segment_timeout_s=12.0))
    final = body.get_state()
    gate = body.perceive("blockAt", {"x": 1, "y": 59, "z": 0})
    payload = result.to_payload()
    if not result.success or result.reason != "arrived":
        raise AssertionError(f"open-gate navigation did not arrive: result={payload} final={final}")
    if distance(final.pos, (2, 59, 0)) > 1.25:
        raise AssertionError(f"open-gate navigation final position too far: final={final.pos} result={payload}")
    if str((gate.data.get("properties") or {}).get("open") or "false").lower() != "true":
        raise AssertionError(f"open-gate navigation did not open gate: block={gate.data} result={payload}")
    segment = (result.metrics or {}).get("segments", [])[0]
    if segment.get("status") != "terrain_open":
        raise AssertionError(f"open-gate navigation did not report terrain_open segment: {payload}")
    return {
        "reason": result.reason,
        "final": final.pos,
        "gate": gate.data,
        "segment": segment,
        "metrics": payload,
    }


def run_single_sand_break_happy_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill -2 59 -2 6 66 2 air")
    command(rcon, "fill -2 58 -2 6 58 2 stone")
    command(rcon, "setblock 2 59 0 sand")
    command(rcon, f"tp {BOT} 0 59 0 -90 0")
    command(rcon, f"item replace entity {BOT} weapon.mainhand with diamond_shovel")
    cells = {(x, 59, 0): GridCell() for x in range(0, 5)}
    cells[(2, 59, 0)] = GridCell(block_type="sand", walkable=False)
    policy = GovernancePolicy(natural_regions=[Region("nav_sand_break", (0, 0, 0), (4, 100, 0))])
    runtime = navigation_runtime(body, GridWorld(cells), policy)

    result = runtime.navigate_to(
        (4, 59, 0),
        break_context=BreakContext.TRAVEL,
        config=NavigationRunConfig(max_segments=5, segment_timeout_s=35.0, min_partial_progress=1),
    )
    final = body.get_state()
    block_after = body.perceive("blockAt", {"x": 2, "y": 59, "z": 0})
    dist = distance(final.pos, (4, 59, 0))
    payload = result.to_payload()
    if not result.success or result.reason != "arrived":
        raise AssertionError(f"single-sand-break navigation did not arrive: result={payload} final={final} block={block_after.data}")
    if block_after.data.get("state") != "CLEAR":
        raise AssertionError(f"single-sand-break navigation did not clear obstacle: result={payload} block={block_after.data}")
    if dist > 1.35:
        raise AssertionError(f"single-sand-break final position too far: final={final.pos} dist={dist:.3f} result={payload}")
    segments = (result.metrics or {}).get("segments", [])
    if not segments or "break" not in segments[0]["diagnostics"]["segment"].get("path_moves", []):
        raise AssertionError(f"single-sand-break path did not expose break movement: result={payload}")
    return {
        "reason": result.reason,
        "final": final.pos,
        "dist": round(dist, 3),
        "block_after": block_after.data,
        "segment_terminal_reasons": [item.get("terminal_reason") for item in segments],
        "metrics": payload,
    }


def run_single_gravel_break_happy_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill -2 59 -2 6 66 2 air")
    command(rcon, "fill -2 58 -2 6 58 2 stone")
    command(rcon, "setblock 2 59 0 gravel")
    command(rcon, f"tp {BOT} 0 59 0 -90 0")
    command(rcon, f"item replace entity {BOT} weapon.mainhand with diamond_shovel")
    cells = {(x, 59, 0): GridCell() for x in range(0, 5)}
    cells[(2, 59, 0)] = GridCell(block_type="gravel", walkable=False)
    policy = GovernancePolicy(natural_regions=[Region("nav_gravel_break", (0, 0, 0), (4, 100, 0))])
    runtime = navigation_runtime(body, GridWorld(cells), policy)

    result = runtime.navigate_to(
        (4, 59, 0),
        break_context=BreakContext.TRAVEL,
        config=NavigationRunConfig(max_segments=5, segment_timeout_s=35.0, min_partial_progress=1),
    )
    final = body.get_state()
    block_after = body.perceive("blockAt", {"x": 2, "y": 59, "z": 0})
    dist = distance(final.pos, (4, 59, 0))
    payload = result.to_payload()
    if not result.success or result.reason != "arrived":
        raise AssertionError(f"single-gravel-break navigation did not arrive: result={payload} final={final} block={block_after.data}")
    if block_after.data.get("state") != "CLEAR":
        raise AssertionError(f"single-gravel-break navigation did not clear obstacle: result={payload} block={block_after.data}")
    if dist > 1.35:
        raise AssertionError(f"single-gravel-break final position too far: final={final.pos} dist={dist:.3f} result={payload}")
    segments = (result.metrics or {}).get("segments", [])
    if not segments or "break" not in segments[0]["diagnostics"]["segment"].get("path_moves", []):
        raise AssertionError(f"single-gravel-break path did not expose break movement: result={payload}")
    return {
        "reason": result.reason,
        "final": final.pos,
        "dist": round(dist, 3),
        "block_after": block_after.data,
        "segment_terminal_reasons": [item.get("terminal_reason") for item in segments],
        "metrics": payload,
    }


def run_gravity_stack_break_failure(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill -2 59 -2 6 66 2 air")
    command(rcon, "fill -2 58 -2 6 58 2 stone")
    command(rcon, "setblock 2 59 0 sand")
    command(rcon, "setblock 2 60 0 sand")
    command(rcon, f"tp {BOT} 0 59 0 -90 0")
    command(rcon, f"item replace entity {BOT} weapon.mainhand with diamond_shovel")
    cells = {(x, 59, 0): GridCell() for x in range(0, 5)}
    cells[(2, 59, 0)] = GridCell(block_type="sand", walkable=False)
    cells[(2, 60, 0)] = GridCell(block_type="sand", walkable=False)
    policy = GovernancePolicy(natural_regions=[Region("nav_sand_stack", (0, 0, 0), (4, 100, 0))])
    runtime = navigation_runtime(body, GridWorld(cells), policy)

    result = runtime.navigate_to(
        (4, 59, 0),
        break_context=BreakContext.TRAVEL,
        config=NavigationRunConfig(max_segments=1, segment_timeout_s=10.0, min_partial_progress=2),
    )
    final = body.get_state()
    lower = body.perceive("blockAt", {"x": 2, "y": 59, "z": 0})
    upper = body.perceive("blockAt", {"x": 2, "y": 60, "z": 0})
    payload = result.to_payload()
    if result.success:
        raise AssertionError(f"gravity-stack break unexpectedly succeeded: result={payload} final={final}")
    if result.reason != "navigation_blocked:no_path":
        raise AssertionError(f"gravity-stack break returned wrong reason: result={payload} final={final}")
    if distance(final.pos, (0, 59, 0)) > 0.9:
        raise AssertionError(f"gravity-stack break moved the bot before denial: final={final.pos} result={payload}")
    path_update = (result.metrics or {}).get("path_update", {})
    blocked_reasons = path_update.get("blocked_reasons", {})
    if blocked_reasons.get("break_denied:gravity_stack", 0) < 1:
        raise AssertionError(f"gravity-stack break did not expose gravity-stack denial: result={payload}")
    segments = (result.metrics or {}).get("segments", [])
    if segments and segments[0].get("action_id") is not None:
        raise AssertionError(f"gravity-stack break dispatched a body action: result={payload}")
    if lower.data.get("type") not in {"sand", "minecraft:sand"} or lower.data.get("state") != "SOLID":
        raise AssertionError(f"gravity-stack break mutated lower sand block: block={lower.data} result={payload}")
    if upper.data.get("type") not in {"sand", "minecraft:sand"} or upper.data.get("state") != "SOLID":
        raise AssertionError(f"gravity-stack break mutated stacked sand block: block={upper.data} result={payload}")
    return {
        "reason": result.reason,
        "final": final.pos,
        "blocked_reasons": blocked_reasons,
        "lower_block": lower.data,
        "upper_block": upper.data,
        "metrics": payload,
    }


def run_gravity_liquid_adjacent_break_failure(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill -2 59 -2 6 66 2 air")
    command(rcon, "fill -2 58 -2 6 58 2 stone")
    command(rcon, "setblock 2 59 0 sand")
    command(rcon, "setblock 2 59 1 water")
    command(rcon, f"tp {BOT} 0 59 0 -90 0")
    command(rcon, f"item replace entity {BOT} weapon.mainhand with diamond_shovel")
    cells = {(x, 59, 0): GridCell() for x in range(0, 5)}
    cells[(2, 59, 0)] = GridCell(block_type="sand", walkable=False)
    cells[(2, 59, 1)] = GridCell(block_type="water", walkable=False, liquid=True)
    policy = GovernancePolicy(natural_regions=[Region("nav_sand_liquid", (0, 0, 0), (4, 100, 0))])
    runtime = navigation_runtime(body, GridWorld(cells), policy)

    result = runtime.navigate_to(
        (4, 59, 0),
        break_context=BreakContext.TRAVEL,
        config=NavigationRunConfig(max_segments=1, segment_timeout_s=10.0, min_partial_progress=2),
    )
    final = body.get_state()
    sand = body.perceive("blockAt", {"x": 2, "y": 59, "z": 0})
    water = body.perceive("blockAt", {"x": 2, "y": 59, "z": 1})
    payload = result.to_payload()
    if result.success:
        raise AssertionError(f"gravity-liquid-adjacent break unexpectedly succeeded: result={payload} final={final}")
    if result.reason != "navigation_blocked:no_path":
        raise AssertionError(f"gravity-liquid-adjacent break returned wrong reason: result={payload} final={final}")
    if distance(final.pos, (0, 59, 0)) > 0.9:
        raise AssertionError(f"gravity-liquid-adjacent break moved the bot before denial: final={final.pos} result={payload}")
    path_update = (result.metrics or {}).get("path_update", {})
    blocked_reasons = path_update.get("blocked_reasons", {})
    if blocked_reasons.get("break_denied:gravity_liquid_adjacent", 0) < 1:
        raise AssertionError(f"gravity-liquid-adjacent break did not expose liquid-adjacent denial: result={payload}")
    segments = (result.metrics or {}).get("segments", [])
    if segments and segments[0].get("action_id") is not None:
        raise AssertionError(f"gravity-liquid-adjacent break dispatched a body action: result={payload}")
    if sand.data.get("type") not in {"sand", "minecraft:sand"} or sand.data.get("state") != "SOLID":
        raise AssertionError(f"gravity-liquid-adjacent break mutated sand block: block={sand.data} result={payload}")
    if water.data.get("type") not in {"water", "minecraft:water"}:
        raise AssertionError(f"gravity-liquid-adjacent break lost adjacent water: block={water.data} result={payload}")
    return {
        "reason": result.reason,
        "final": final.pos,
        "blocked_reasons": blocked_reasons,
        "sand_block": sand.data,
        "water_block": water.data,
        "metrics": payload,
    }


def run_recheck_gravity_vertical_liquid_break_blocks_dispatch(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill -2 59 -2 6 66 2 air")
    command(rcon, "fill -2 58 -2 6 58 2 stone")
    command(rcon, "setblock 2 59 0 sand")
    command(rcon, "setblock 2 60 0 water")
    command(rcon, f"tp {BOT} 0 59 0 -90 0")
    command(rcon, f"item replace entity {BOT} weapon.mainhand with diamond_shovel")
    planned_cells = {(x, 59, 0): GridCell() for x in range(0, 5)}
    planned_cells[(2, 59, 0)] = GridCell(block_type="sand", walkable=False)
    recheck_cells = dict(planned_cells)
    recheck_cells[(2, 60, 0)] = GridCell(block_type="water", walkable=False, liquid=True)
    policy = GovernancePolicy(natural_regions=[Region("nav_sand_vertical_liquid", (0, 0, 0), (4, 100, 0))])
    runtime = NavigationTransactions(
        body,
        SegmentedNavigator(GridWorld(planned_cells), NavigationCostModel(policy)),
    )

    result = runtime.navigate_to(
        (4, 59, 0),
        break_context=BreakContext.TRAVEL,
        config=NavigationRunConfig(
            max_segments=1,
            segment_timeout_s=10.0,
            min_partial_progress=2,
            recheck_world=GridWorld(recheck_cells),
        ),
    )
    final = body.get_state()
    sand = body.perceive("blockAt", {"x": 2, "y": 59, "z": 0})
    water = body.perceive("blockAt", {"x": 2, "y": 60, "z": 0})
    payload = result.to_payload()
    if result.success:
        raise AssertionError(f"recheck gravity-vertical-liquid unexpectedly succeeded: result={payload} final={final}")
    if result.reason != "navigation_replan_required:break_denied:gravity_liquid_adjacent" or not result.can_retry:
        raise AssertionError(f"recheck gravity-vertical-liquid returned wrong reason: result={payload} final={final}")
    if distance(final.pos, (0, 59, 0)) > 0.9:
        raise AssertionError(f"recheck gravity-vertical-liquid moved the bot before dispatch denial: final={final.pos} result={payload}")
    segments = (result.metrics or {}).get("segments", [])
    if len(segments) != 1:
        raise AssertionError(f"recheck gravity-vertical-liquid should report one planned segment: result={payload}")
    if segments[0].get("action_id") is not None or segments[0].get("terminal_reason") is not None:
        raise AssertionError(f"recheck gravity-vertical-liquid dispatched a body action: result={payload}")
    segment = segments[0]["diagnostics"]["segment"]
    if segment.get("plan_reason") != "arrived" or segment.get("recheck_reason") != "break_denied:gravity_liquid_adjacent":
        raise AssertionError(f"recheck gravity-vertical-liquid did not expose plan/recheck contrast: result={payload}")
    if "break" not in segment.get("path_moves", []):
        raise AssertionError(f"recheck gravity-vertical-liquid did not plan a break step: result={payload}")
    unsafe_steps = segment.get("movement_cancel", {}).get("unsafe_steps", [])
    if not any(step.get("pos") == [2, 59, 0] and step.get("move") == "break" for step in unsafe_steps):
        raise AssertionError(
            f"recheck gravity-vertical-liquid did not expose the planned gravity-break candidate: result={payload}"
        )
    path_update = (result.metrics or {}).get("path_update", {})
    if path_update.get("source") != "recheck" or path_update.get("category") != "goal_changed_or_world_changed":
        raise AssertionError(f"recheck gravity-vertical-liquid path_update classification wrong: result={payload}")
    if sand.data.get("type") not in {"sand", "minecraft:sand"} or sand.data.get("state") != "SOLID":
        raise AssertionError(f"recheck gravity-vertical-liquid mutated sand block: block={sand.data} result={payload}")
    if water.data.get("type") not in {"water", "minecraft:water"}:
        raise AssertionError(f"recheck gravity-vertical-liquid lost vertical water: block={water.data} result={payload}")
    return {
        "reason": result.reason,
        "final": final.pos,
        "path_update": path_update,
        "segment": segment,
        "sand_block": sand.data,
        "water_block": water.data,
        "metrics": payload,
    }


def run_recheck_gravel_stack_break_blocks_dispatch(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill -2 59 -2 6 66 2 air")
    command(rcon, "fill -2 58 -2 6 58 2 stone")
    command(rcon, "setblock 2 59 0 gravel")
    command(rcon, "setblock 2 60 0 gravel")
    command(rcon, f"tp {BOT} 0 59 0 -90 0")
    command(rcon, f"item replace entity {BOT} weapon.mainhand with diamond_shovel")
    planned_cells = {(x, 59, 0): GridCell() for x in range(0, 5)}
    planned_cells[(2, 59, 0)] = GridCell(block_type="gravel", walkable=False)
    recheck_cells = dict(planned_cells)
    recheck_cells[(2, 60, 0)] = GridCell(block_type="gravel", walkable=False)
    policy = GovernancePolicy(natural_regions=[Region("nav_gravel_stack_recheck", (0, 0, 0), (4, 100, 0))])
    runtime = NavigationTransactions(
        body,
        SegmentedNavigator(GridWorld(planned_cells), NavigationCostModel(policy)),
    )

    result = runtime.navigate_to(
        (4, 59, 0),
        break_context=BreakContext.TRAVEL,
        config=NavigationRunConfig(
            max_segments=1,
            segment_timeout_s=10.0,
            min_partial_progress=2,
            recheck_world=GridWorld(recheck_cells),
        ),
    )
    final = body.get_state()
    lower = body.perceive("blockAt", {"x": 2, "y": 59, "z": 0})
    upper = body.perceive("blockAt", {"x": 2, "y": 60, "z": 0})
    payload = result.to_payload()
    if result.success:
        raise AssertionError(f"recheck gravel-stack unexpectedly succeeded: result={payload} final={final}")
    if result.reason != "navigation_replan_required:break_denied:gravity_stack" or not result.can_retry:
        raise AssertionError(f"recheck gravel-stack returned wrong reason: result={payload} final={final}")
    if distance(final.pos, (0, 59, 0)) > 0.9:
        raise AssertionError(f"recheck gravel-stack moved the bot before dispatch denial: final={final.pos} result={payload}")
    segments = (result.metrics or {}).get("segments", [])
    if len(segments) != 1:
        raise AssertionError(f"recheck gravel-stack should report one planned segment: result={payload}")
    if segments[0].get("action_id") is not None or segments[0].get("terminal_reason") is not None:
        raise AssertionError(f"recheck gravel-stack dispatched a body action: result={payload}")
    segment = segments[0]["diagnostics"]["segment"]
    if segment.get("plan_reason") != "arrived" or segment.get("recheck_reason") != "break_denied:gravity_stack":
        raise AssertionError(f"recheck gravel-stack did not expose plan/recheck contrast: result={payload}")
    if "break" not in segment.get("path_moves", []):
        raise AssertionError(f"recheck gravel-stack did not plan a break step: result={payload}")
    unsafe_steps = segment.get("movement_cancel", {}).get("unsafe_steps", [])
    if not any(step.get("pos") == [2, 59, 0] and step.get("move") == "break" for step in unsafe_steps):
        raise AssertionError(f"recheck gravel-stack did not expose the planned gravel-break candidate: result={payload}")
    path_update = (result.metrics or {}).get("path_update", {})
    if path_update.get("source") != "recheck" or path_update.get("category") != "goal_changed_or_world_changed":
        raise AssertionError(f"recheck gravel-stack path_update classification wrong: result={payload}")
    if lower.data.get("type") not in {"gravel", "minecraft:gravel"} or lower.data.get("state") != "SOLID":
        raise AssertionError(f"recheck gravel-stack mutated lower gravel block: block={lower.data} result={payload}")
    if upper.data.get("type") not in {"gravel", "minecraft:gravel"} or upper.data.get("state") != "SOLID":
        raise AssertionError(f"recheck gravel-stack mutated stacked gravel block: block={upper.data} result={payload}")
    return {
        "reason": result.reason,
        "final": final.pos,
        "path_update": path_update,
        "segment": segment,
        "lower_block": lower.data,
        "upper_block": upper.data,
        "metrics": payload,
    }


def run_headroom_break_happy_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill -2 59 -2 6 66 2 air")
    command(rcon, "fill -2 58 -2 6 58 2 stone")
    command(rcon, "setblock 1 60 0 stone")
    command(rcon, f"tp {BOT} 0 59 0 -90 0")
    command(rcon, f"item replace entity {BOT} weapon.mainhand with diamond_pickaxe")
    cells = {
        (0, 59, 0): GridCell(),
        (1, 59, 0): GridCell(headroom_block="stone"),
        (1, 60, 0): GridCell(block_type="stone", walkable=False),
        (2, 59, 0): GridCell(),
        (3, 59, 0): GridCell(),
    }
    policy = GovernancePolicy(natural_regions=[Region("nav_headroom_break", (-2, 0, -2), (6, 100, 2))])
    runtime = navigation_runtime(body, GridWorld(cells), policy)

    result = runtime.navigate_to(
        (3, 59, 0),
        break_context=BreakContext.TRAVEL,
        config=NavigationRunConfig(max_segments=4, segment_timeout_s=20.0, min_partial_progress=1),
    )
    final = body.get_state()
    ceiling = body.perceive("blockAt", {"x": 1, "y": 60, "z": 0})
    payload = result.to_payload()
    if not result.success or result.reason != "arrived":
        raise AssertionError(f"headroom-break navigation did not arrive: result={payload} final={final} block={ceiling.data}")
    if ceiling.data.get("state") != "CLEAR":
        raise AssertionError(f"headroom-break navigation did not clear ceiling obstacle: result={payload} block={ceiling.data}")
    if distance(final.pos, (3, 59, 0)) > 1.35:
        raise AssertionError(f"headroom-break final position too far: final={final.pos} result={payload}")
    segments = (result.metrics or {}).get("segments", [])
    if len(segments) < 2:
        raise AssertionError(f"headroom-break should expose terrain break plus movement continuation: result={payload}")
    first = segments[0]
    if first.get("status") != "terrain_break":
        raise AssertionError(f"headroom-break first segment should be terrain_break: result={payload}")
    terrain = first.get("diagnostics", {}).get("terrain_step", {})
    if terrain.get("pos") != [1, 60, 0] or terrain.get("move") != "break":
        raise AssertionError(f"headroom-break did not expose headroom break step facts: result={payload}")
    second = segments[1].get("diagnostics", {}).get("segment", {})
    second_moves = second.get("path_moves")
    if not second_moves or not all(move == "walk" for move in second_moves):
        raise AssertionError(f"headroom-break continuation did not replan into walk moves: result={payload}")
    return {
        "reason": result.reason,
        "final": final.pos,
        "ceiling_after": ceiling.data,
        "terrain_step": terrain,
        "continuation_moves": second_moves,
        "metrics": payload,
    }


def run_place_support_happy_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill -2 59 -2 5 66 2 air")
    command(rcon, "fill -2 58 -2 5 58 2 stone")
    command(rcon, "setblock 2 58 0 air")
    command(rcon, f"tp {BOT} 0 59 0 -90 0")
    command(rcon, f"item replace entity {BOT} weapon.mainhand with cobblestone 8")
    cells = flat_world(-1, 4, -1, 1).cells
    cells[(2, 58, 0)] = GridCell(block_type="air", walkable=True)
    cells[(2, 59, 0)] = GridCell(requires_support=True)
    policy = GovernancePolicy(natural_regions=[Region("nav_place", (-1, 0, -1), (4, 100, 1))])
    world = GridWorld(cells)
    runtime = navigation_runtime(body, world, policy)

    result = runtime.navigate_to(
        (4, 59, 0),
        config=NavigationRunConfig(max_segments=5, segment_timeout_s=20.0, min_partial_progress=1),
    )
    final = body.get_state()
    block_after = body.perceive("blockAt", {"x": 2, "y": 58, "z": 0})
    dist = distance(final.pos, (4, 59, 0))
    payload = result.to_payload()
    if not result.success or result.reason != "arrived":
        raise AssertionError(f"place-support navigation did not arrive: result={payload} final={final} block={block_after.data}")
    if block_after.data.get("type") not in {"cobblestone", "minecraft:cobblestone"}:
        raise AssertionError(f"place-support navigation did not place support: result={payload} block={block_after.data}")
    if dist > 1.25:
        raise AssertionError(f"place-support final position too far: final={final.pos} dist={dist:.3f} result={payload}")
    updated = world.cells[(2, 58, 0)]
    if updated.block_type not in {"cobblestone", "minecraft:cobblestone"} or updated.walkable:
        raise AssertionError(f"place-support local grid was not updated after authoritative placement: {updated}")
    cleanup = policy.can_break((2, 58, 0), "minecraft:cobblestone", BreakContext.BOT_CLEANUP)
    if not cleanup.allowed:
        raise AssertionError(f"place-support placement was not recorded as bot-owned cleanup: {cleanup}")
    return {
        "reason": result.reason,
        "final": final.pos,
        "dist": round(dist, 3),
        "block_after": block_after.data,
        "local_cell": {
            "block_type": updated.block_type,
            "walkable": updated.walkable,
        },
        "cleanup": cleanup.reason,
        "metrics": payload,
    }


def run_recheck_support_missing_blocks_dispatch(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill -2 59 -2 5 66 2 air")
    command(rcon, "fill -2 58 -2 5 58 2 stone")
    command(rcon, "setblock 2 58 0 stone")
    command(rcon, f"tp {BOT} 0 59 0 -90 0")
    planned_cells = flat_world(-1, 4, -1, 1).cells
    planned_cells[(2, 58, 0)] = GridCell(block_type="stone", walkable=False)
    planned_cells[(2, 59, 0)] = GridCell(requires_support=True)
    recheck_cells = dict(planned_cells)
    recheck_cells[(2, 58, 0)] = GridCell(block_type="air", walkable=True)
    policy = GovernancePolicy(natural_regions=[Region("nav_place", (-1, 0, -1), (4, 100, 1))])
    runtime = NavigationTransactions(
        body,
        SegmentedNavigator(GridWorld(planned_cells), NavigationCostModel(policy)),
    )

    result = runtime.navigate_to(
        (4, 59, 0),
        config=NavigationRunConfig(
            max_segments=1,
            segment_timeout_s=10.0,
            min_partial_progress=1,
            recheck_world=GridWorld(recheck_cells),
        ),
    )
    final = body.get_state()
    support = body.perceive("blockAt", {"x": 2, "y": 58, "z": 0})
    payload = result.to_payload()
    if result.success:
        raise AssertionError(f"recheck support-missing unexpectedly succeeded: result={payload} final={final}")
    if result.reason != "navigation_replan_required:support_missing" or not result.can_retry:
        raise AssertionError(f"recheck support-missing returned wrong reason: result={payload} final={final}")
    if distance(final.pos, (0, 59, 0)) > 0.9:
        raise AssertionError(f"recheck support-missing moved the bot before dispatch denial: final={final.pos} result={payload}")
    segments = (result.metrics or {}).get("segments", [])
    if len(segments) != 1:
        raise AssertionError(f"recheck support-missing should report one planned segment: result={payload}")
    if segments[0].get("action_id") is not None or segments[0].get("terminal_reason") is not None:
        raise AssertionError(f"recheck support-missing dispatched a body action: result={payload}")
    segment = segments[0]["diagnostics"]["segment"]
    if segment.get("plan_reason") != "arrived" or segment.get("recheck_reason") != "support_missing":
        raise AssertionError(f"recheck support-missing did not expose plan/recheck contrast: result={payload}")
    if segment.get("path_moves") != ["walk", "walk", "walk", "walk"]:
        raise AssertionError(f"recheck support-missing planned path shape changed unexpectedly: result={payload}")
    path_update = (result.metrics or {}).get("path_update", {})
    if path_update.get("source") != "recheck" or path_update.get("category") != "goal_changed_or_world_changed":
        raise AssertionError(f"recheck support-missing path_update classification wrong: result={payload}")
    if support.data.get("type") not in {"stone", "minecraft:stone"} or support.data.get("state") != "SOLID":
        raise AssertionError(f"recheck support-missing mutated support block unexpectedly: block={support.data} result={payload}")
    return {
        "reason": result.reason,
        "final": final.pos,
        "path_update": path_update,
        "segment": segment,
        "support_block": support.data,
        "metrics": payload,
    }


def run_vertical_ascend_happy_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill -2 59 -2 4 66 2 air")
    command(rcon, "fill -2 58 -2 4 58 2 stone")
    command(rcon, "setblock 1 59 0 stone")
    command(rcon, "setblock 2 60 0 stone")
    command(rcon, f"tp {BOT} 0 59 0 -90 0")
    policy = GovernancePolicy(natural_regions=[Region("nav_vertical", (-2, 0, -2), (4, 100, 2))])
    world = GridWorld({
        (0, 59, 0): GridCell(),
        (1, 60, 0): GridCell(),
        (2, 61, 0): GridCell(),
    })
    runtime = navigation_runtime(body, world, policy)

    result = runtime.navigate_to(
        (2, 61, 0),
        config=NavigationRunConfig(max_segments=2, segment_timeout_s=20.0, min_partial_progress=1),
    )
    final = body.get_state()
    payload = result.to_payload()
    if not result.success or result.reason != "arrived":
        raise AssertionError(f"vertical ascend navigation did not arrive: result={payload} final={final}")
    if final.pos[1] < 60.75:
        raise AssertionError(f"vertical ascend did not gain Y-level: final={final.pos} result={payload}")
    if distance(final.pos, (2, 61, 0)) > 1.25:
        raise AssertionError(f"vertical ascend final position too far: final={final.pos} result={payload}")
    segments = (result.metrics or {}).get("segments", [])
    first_segment = segments[0]["diagnostics"]["segment"] if segments else {}
    moves = first_segment.get("path_moves")
    cancel = first_segment.get("movement_cancel", {})
    if not moves or not all(move == "ascend" for move in moves):
        raise AssertionError(f"vertical ascend path did not expose ascend moves: result={payload}")
    if cancel.get("safe_to_cancel") is not False or cancel.get("unsafe_count", 0) < 1:
        raise AssertionError(f"vertical ascend did not expose unsafe cancel facts: result={payload}")
    policies = cancel.get("policies", [])
    if "settle_on_support" not in policies:
        raise AssertionError(f"vertical ascend cancel policy missing settle_on_support: result={payload}")
    return {
        "reason": result.reason,
        "final": final.pos,
        "path_moves": moves,
        "movement_cancel": cancel,
        "metrics": payload,
    }


def run_swim_happy_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill -2 59 -2 7 64 2 air")
    command(rcon, "fill -2 58 -2 7 58 2 stone")
    command(rcon, "fill 1 59 -1 4 61 1 water")
    command(rcon, f"tp {BOT} 0 59 0 -90 0")
    cells = {
        (0, 59, 0): GridCell(),
        (1, 59, 0): GridCell(block_type="water", liquid=True),
        (2, 59, 0): GridCell(block_type="water", liquid=True),
        (3, 59, 0): GridCell(block_type="water", liquid=True),
        (4, 59, 0): GridCell(block_type="water", liquid=True),
        (5, 59, 0): GridCell(),
    }
    policy = GovernancePolicy(natural_regions=[Region("nav_swim", (-2, 0, -2), (7, 100, 2))])
    runtime = navigation_runtime(body, GridWorld(cells), policy)

    result = runtime.navigate_to(
        (5, 59, 0),
        config=NavigationRunConfig(max_segments=2, segment_timeout_s=25.0, min_partial_progress=1),
    )
    final = body.get_state()
    payload = result.to_payload()
    if not result.success or result.reason != "arrived":
        raise AssertionError(f"swim navigation did not arrive: result={payload} final={final}")
    if distance(final.pos, (5, 59, 0)) > 1.35:
        raise AssertionError(f"swim final position too far: final={final.pos} result={payload}")
    segments = (result.metrics or {}).get("segments", [])
    first_segment = segments[0]["diagnostics"]["segment"] if segments else {}
    moves = first_segment.get("path_moves")
    cancel = first_segment.get("movement_cancel", {})
    if not moves or "swim" not in moves:
        raise AssertionError(f"swim path did not expose swim moves: result={payload}")
    if cancel.get("safe_to_cancel") is not False or cancel.get("unsafe_count", 0) < 1:
        raise AssertionError(f"swim path did not expose unsafe cancel facts: result={payload}")
    policies = cancel.get("policies", [])
    if "surface_or_stable_water" not in policies:
        raise AssertionError(f"swim cancel policy missing surface_or_stable_water: result={payload}")
    return {
        "reason": result.reason,
        "final": final.pos,
        "path_moves": moves,
        "movement_cancel": cancel,
        "metrics": payload,
    }


def run_step_surface_happy_paths(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    cases = [
        ("bottom_slab", "smooth_stone_slab[type=bottom]", {"type": "smooth_stone_slab", "type_prop": "bottom"}),
        ("top_slab", "smooth_stone_slab[type=top]", {"type": "smooth_stone_slab", "type_prop": "top"}),
        ("bottom_stair", "oak_stairs[facing=east,half=bottom,shape=straight]", {"type": "oak_stairs", "half_prop": "bottom"}),
        ("top_stair", "oak_stairs[facing=east,half=top,shape=straight]", {"type": "oak_stairs", "half_prop": "top"}),
    ]
    results: dict[str, object] = {}

    for label, block_state, expected in cases:
        command(rcon, "script in minebot run minebot_reset()")
        command(rcon, "fill -2 58 -2 4 64 2 air")
        command(rcon, "fill -2 57 -2 4 57 2 stone")
        command(rcon, f"setblock 2 58 0 {block_state}")
        command(rcon, f"tp {BOT} 0 58 0 -90 0")
        cells = {
            (0, 58, 0): GridCell(),
            (1, 59, 0): GridCell(),
            (2, 58, 0): GridCell(block_type=expected["type"], walkable=False),
            (2, 59, 0): GridCell(),
        }
        policy = GovernancePolicy(natural_regions=[Region(f"nav_{label}", (-2, 0, -2), (4, 100, 2))])
        runtime = navigation_runtime(body, GridWorld(cells), policy)

        result = runtime.navigate_to(
            (2, 59, 0),
            config=NavigationRunConfig(max_segments=2, segment_timeout_s=12.0, min_partial_progress=1),
        )
        final = body.get_state()
        payload = result.to_payload()
        if not result.success or result.reason != "arrived":
            raise AssertionError(f"{label} step-surface navigation did not arrive: result={payload} final={final}")
        if final.pos[1] < 58.9:
            raise AssertionError(f"{label} step-surface navigation did not settle onto the raised support: final={final.pos} result={payload}")
        if distance(final.pos, (2, 59, 0)) > 1.0:
            raise AssertionError(f"{label} step-surface final position too far: final={final.pos} result={payload}")
        segments = (result.metrics or {}).get("segments", [])
        first_segment = segments[0]["diagnostics"]["segment"] if segments else {}
        moves = first_segment.get("path_moves")
        cancel = first_segment.get("movement_cancel", {})
        if moves != ["ascend", "walk"]:
            raise AssertionError(f"{label} step-surface path shape changed: result={payload}")
        if cancel.get("policies") != ["settle_on_support", "immediate"]:
            raise AssertionError(f"{label} step-surface cancel profile changed: result={payload}")
        block = body.perceive("blockAt", {"x": 2, "y": 58, "z": 0})
        block_type = str(block.data.get("type"))
        if block_type not in {expected["type"], f"minecraft:{expected['type']}"}:
            raise AssertionError(f"{label} support block type mismatch: block={block.data} result={payload}")
        if expected.get("type_prop") is not None and block.data.get("properties", {}).get("type") != expected["type_prop"]:
            raise AssertionError(f"{label} slab properties were not preserved: block={block.data} result={payload}")
        if expected.get("half_prop") is not None and block.data.get("properties", {}).get("half") != expected["half_prop"]:
            raise AssertionError(f"{label} stair properties were not preserved: block={block.data} result={payload}")
        results[label] = {
            "reason": result.reason,
            "final": final.pos,
            "path_moves": moves,
            "movement_cancel": cancel,
            "support_block": block.data,
            "metrics": payload,
        }

    return results


def run_descend_happy_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill -2 58 -2 4 64 2 air")
    command(rcon, "fill -2 57 -2 4 57 2 stone")
    command(rcon, "setblock 0 59 0 stone")
    command(rcon, "setblock 1 58 0 stone")
    command(rcon, f"tp {BOT} 0 60 0 -90 0")
    cells = {
        (0, 60, 0): GridCell(),
        (1, 59, 0): GridCell(),
        (2, 58, 0): GridCell(),
    }
    policy = GovernancePolicy(natural_regions=[Region("nav_descend", (-2, 0, -2), (4, 100, 2))])
    runtime = navigation_runtime(body, GridWorld(cells), policy)

    result = runtime.navigate_to(
        (2, 58, 0),
        config=NavigationRunConfig(max_segments=2, segment_timeout_s=20.0, min_partial_progress=1),
    )
    final = body.get_state()
    payload = result.to_payload()
    if not result.success or result.reason != "arrived":
        raise AssertionError(f"descend navigation did not arrive: result={payload} final={final}")
    if final.pos[1] > 58.75:
        raise AssertionError(f"descend navigation did not lose Y-level: final={final.pos} result={payload}")
    if distance(final.pos, (2, 58, 0)) > 1.25:
        raise AssertionError(f"descend final position too far: final={final.pos} result={payload}")
    segments = (result.metrics or {}).get("segments", [])
    first_segment = segments[0]["diagnostics"]["segment"] if segments else {}
    moves = first_segment.get("path_moves")
    cancel = first_segment.get("movement_cancel", {})
    if not moves or not all(move == "descend" for move in moves):
        raise AssertionError(f"descend path did not expose descend moves: result={payload}")
    policies = cancel.get("policies", [])
    if policies != ["after_step", "after_step"]:
        raise AssertionError(f"descend cancel policy missing after_step: result={payload}")
    if cancel.get("safe_to_cancel") is not True or cancel.get("unsafe_count") != 0:
        raise AssertionError(f"descend cancel safety shape changed unexpectedly: result={payload}")
    return {
        "reason": result.reason,
        "final": final.pos,
        "path_moves": moves,
        "movement_cancel": cancel,
        "metrics": payload,
    }


def run_safe_fall_happy_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill -2 57 -2 5 66 2 air")
    command(rcon, "fill -2 56 -2 5 56 2 stone")
    command(rcon, "setblock 0 59 0 stone")
    command(rcon, f"tp {BOT} 0 60 0 -90 0")
    cells = {
        (0, 60, 0): GridCell(),
        (1, 59, 0): GridCell(fall_depth=3),
        (2, 57, 0): GridCell(),
    }
    policy = GovernancePolicy(natural_regions=[Region("nav_safe_fall", (-2, 0, -2), (5, 100, 2))])
    runtime = navigation_runtime(body, GridWorld(cells), policy)

    result = runtime.navigate_to(
        (2, 57, 0),
        config=NavigationRunConfig(max_segments=2, segment_timeout_s=20.0, min_partial_progress=1),
    )
    final = body.get_state()
    payload = result.to_payload()
    if not result.success or result.reason != "arrived":
        raise AssertionError(f"safe fall navigation did not arrive: result={payload} final={final}")
    if final.pos[1] > 57.75:
        raise AssertionError(f"safe fall did not land on the lower level: final={final.pos} result={payload}")
    if distance(final.pos, (2, 57, 0)) > 1.35:
        raise AssertionError(f"safe fall final position too far: final={final.pos} result={payload}")
    segments = (result.metrics or {}).get("segments", [])
    first_segment = segments[0]["diagnostics"]["segment"] if segments else {}
    moves = first_segment.get("path_moves")
    fall_depths = first_segment.get("path_fall_depths")
    waypoints = first_segment.get("movement_waypoints")
    cancel = first_segment.get("movement_cancel", {})
    if not moves or "fall" not in moves:
        raise AssertionError(f"safe fall path did not expose fall move: result={payload}")
    if not fall_depths or max(fall_depths) != 3:
        raise AssertionError(f"safe fall path did not preserve fall depth: result={payload}")
    if not waypoints or [1, 57, 0] not in waypoints:
        raise AssertionError(f"safe fall did not send the landing waypoint: result={payload}")
    if cancel.get("safe_to_cancel") is not False or cancel.get("unsafe_count", 0) < 1:
        raise AssertionError(f"safe fall did not expose unsafe cancel facts: result={payload}")
    policies = cancel.get("policies", [])
    if "land_first" not in policies:
        raise AssertionError(f"safe fall cancel policy missing land_first: result={payload}")
    return {
        "reason": result.reason,
        "final": final.pos,
        "path_moves": moves,
        "path_fall_depths": fall_depths,
        "movement_waypoints": waypoints,
        "movement_cancel": cancel,
        "metrics": payload,
    }


def run_recheck_fall_becomes_unsafe_blocks_dispatch(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill -2 57 -2 5 66 2 air")
    command(rcon, "fill -2 56 -2 5 56 2 stone")
    command(rcon, "setblock 0 59 0 stone")
    command(rcon, f"tp {BOT} 0 60 0 -90 0")
    planned_cells = {
        (0, 60, 0): GridCell(),
        (1, 59, 0): GridCell(fall_depth=3),
        (2, 57, 0): GridCell(),
    }
    recheck_cells = dict(planned_cells)
    recheck_cells[(1, 59, 0)] = GridCell(fall_depth=6)
    policy = GovernancePolicy(natural_regions=[Region("nav_safe_fall_recheck", (-2, 0, -2), (5, 100, 2))])
    runtime = NavigationTransactions(
        body,
        SegmentedNavigator(GridWorld(planned_cells), NavigationCostModel(policy)),
    )

    result = runtime.navigate_to(
        (2, 57, 0),
        config=NavigationRunConfig(
            max_segments=1,
            segment_timeout_s=10.0,
            min_partial_progress=1,
            recheck_world=GridWorld(recheck_cells),
        ),
    )
    final = body.get_state()
    payload = result.to_payload()
    if result.success:
        raise AssertionError(f"recheck fall-unsafe unexpectedly succeeded: result={payload} final={final}")
    if result.reason != "navigation_replan_required:fall_denied:unsafe_depth" or not result.can_retry:
        raise AssertionError(f"recheck fall-unsafe returned wrong reason: result={payload} final={final}")
    if final.pos[1] < 59.5:
        raise AssertionError(f"recheck fall-unsafe moved the bot before dispatch denial: final={final.pos} result={payload}")
    segments = (result.metrics or {}).get("segments", [])
    if len(segments) != 1:
        raise AssertionError(f"recheck fall-unsafe should report one planned segment: result={payload}")
    if segments[0].get("action_id") is not None or segments[0].get("terminal_reason") is not None:
        raise AssertionError(f"recheck fall-unsafe dispatched a body action: result={payload}")
    segment = segments[0]["diagnostics"]["segment"]
    if segment.get("plan_reason") not in {"arrived", "partial"} or segment.get("recheck_reason") != "fall_denied:unsafe_depth":
        raise AssertionError(f"recheck fall-unsafe did not expose plan/recheck contrast: result={payload}")
    if segment.get("path_moves") != ["fall"]:
        raise AssertionError(f"recheck fall-unsafe did not preserve planned fall path shape: result={payload}")
    fall_depths = segment.get("path_fall_depths")
    if not fall_depths or max(fall_depths) != 3:
        raise AssertionError(f"recheck fall-unsafe did not preserve planned safe fall depth: result={payload}")
    path_update = (result.metrics or {}).get("path_update", {})
    if path_update.get("source") != "recheck" or path_update.get("category") != "goal_changed_or_world_changed":
        raise AssertionError(f"recheck fall-unsafe path_update classification wrong: result={payload}")
    return {
        "reason": result.reason,
        "final": final.pos,
        "path_update": path_update,
        "segment": segment,
        "metrics": payload,
    }


def run_fall_then_walk_happy_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill -2 57 -2 6 66 2 air")
    command(rcon, "fill -2 56 -2 6 56 2 stone")
    command(rcon, "setblock 0 59 0 stone")
    command(rcon, f"tp {BOT} 0 60 0 -90 0")
    cells = {
        (0, 60, 0): GridCell(),
        (1, 59, 0): GridCell(fall_depth=3),
        (1, 57, 0): GridCell(),
        (2, 57, 0): GridCell(),
        (3, 57, 0): GridCell(),
    }
    policy = GovernancePolicy(natural_regions=[Region("nav_fall_walk", (-2, 0, -2), (6, 100, 2))])
    runtime = navigation_runtime(body, GridWorld(cells), policy)

    result = runtime.navigate_to(
        (3, 57, 0),
        config=NavigationRunConfig(max_segments=3, segment_timeout_s=20.0, min_partial_progress=1),
    )
    final = body.get_state()
    payload = result.to_payload()
    if not result.success or result.reason != "arrived":
        raise AssertionError(f"fall-then-walk navigation did not arrive: result={payload} final={final}")
    if final.pos[1] > 57.75:
        raise AssertionError(f"fall-then-walk did not stay on the lower level: final={final.pos} result={payload}")
    if distance(final.pos, (3, 57, 0)) > 1.35:
        raise AssertionError(f"fall-then-walk final position too far: final={final.pos} result={payload}")
    segments = (result.metrics or {}).get("segments", [])
    if len(segments) < 2:
        raise AssertionError(f"fall-then-walk should require landing continuation: result={payload}")
    first_segment = segments[0]["diagnostics"]["segment"]
    first_moves = first_segment.get("path_moves")
    first_waypoints = first_segment.get("movement_waypoints")
    first_cancel = first_segment.get("movement_cancel", {})
    if "fall" not in (first_moves or []):
        raise AssertionError(f"fall-then-walk first segment did not expose fall move: result={payload}")
    if not first_waypoints or [1, 57, 0] not in first_waypoints:
        raise AssertionError(f"fall-then-walk first segment did not preserve landing waypoint: result={payload}")
    if "land_first" not in first_cancel.get("policies", []):
        raise AssertionError(f"fall-then-walk first segment missing land_first cancel facts: result={payload}")
    second_segment = segments[1]["diagnostics"]["segment"]
    second_moves = second_segment.get("path_moves")
    second_cancel = second_segment.get("movement_cancel", {})
    if not second_moves or not all(move == "walk" for move in second_moves):
        raise AssertionError(f"fall-then-walk continuation did not replan into lower-level walk moves: result={payload}")
    if second_cancel.get("safe_to_cancel") is not True:
        raise AssertionError(f"fall-then-walk continuation did not return to immediate-cancel walk semantics: result={payload}")
    return {
        "reason": result.reason,
        "final": final.pos,
        "first_segment": {
            "status": segments[0]["status"],
            "target": segments[0]["target"],
            "path_moves": first_moves,
            "movement_waypoints": first_waypoints,
            "movement_cancel": first_cancel,
        },
        "second_segment": {
            "status": segments[1]["status"],
            "target": segments[1]["target"],
            "path_moves": second_moves,
            "movement_cancel": second_cancel,
        },
        "metrics": payload,
    }


def run_pillar_happy_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill -2 59 -2 2 68 2 air")
    command(rcon, "fill -2 58 -2 2 58 2 stone")
    command(rcon, f"tp {BOT} 0 59 0 0 0")
    command(rcon, f"item replace entity {BOT} hotbar.0 with cobblestone 8")
    cells = {
        (0, 59, 0): GridCell(),
        (0, 60, 0): GridCell(),
    }
    policy = GovernancePolicy(natural_regions=[Region("nav_pillar", (-2, 0, -2), (2, 100, 2))])
    runtime = navigation_runtime(body, GridWorld(cells), policy)

    result = runtime.navigate_to(
        GoalYLevel(60),
        config=NavigationRunConfig(max_segments=2, segment_timeout_s=30.0, min_partial_progress=1),
    )
    final = body.get_state()
    payload = result.to_payload()
    pillar_block = body.perceive("blockAt", {"x": 0, "y": 59, "z": 0})
    if not result.success or result.reason != "arrived":
        raise AssertionError(f"pillar navigation did not arrive: result={payload} final={final}")
    if final.pos[1] < 60.0:
        raise AssertionError(f"pillar navigation did not gain Y: final={final.pos} result={payload}")
    if pillar_block.data.get("state") != "SOLID":
        raise AssertionError(f"pillar block was not placed underfoot: block={pillar_block.data} result={payload}")
    segments = (result.metrics or {}).get("segments", [])
    first_segment = segments[0]["diagnostics"]["segment"] if segments else {}
    moves = first_segment.get("path_moves")
    waypoints = first_segment.get("movement_waypoints")
    cancel = first_segment.get("movement_cancel", {})
    if moves != ["pillar"]:
        raise AssertionError(f"pillar path did not expose pillar move: result={payload}")
    if waypoints != []:
        raise AssertionError(f"pillar move leaked into moveTo waypoints: result={payload}")
    if cancel.get("safe_to_cancel") is not False or cancel.get("policies") != ["finish_or_abort_controller"]:
        raise AssertionError(f"pillar cancel policy missing controller boundary: result={payload}")
    return {
        "reason": result.reason,
        "final": final.pos,
        "path_moves": moves,
        "movement_waypoints": waypoints,
        "movement_cancel": cancel,
        "pillar_block": pillar_block.data,
        "metrics": payload,
    }


def run_pillar_no_scaffold_failure(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill -2 59 -2 2 68 2 air")
    command(rcon, "fill -2 58 -2 2 58 2 stone")
    command(rcon, f"tp {BOT} 0 59 0 0 0")
    command(rcon, f"clear {BOT}")
    cells = {
        (0, 59, 0): GridCell(),
        (0, 60, 0): GridCell(),
    }
    policy = GovernancePolicy(natural_regions=[Region("nav_pillar", (-2, 0, -2), (2, 100, 2))])
    runtime = navigation_runtime(body, GridWorld(cells), policy)

    result = runtime.navigate_to(
        GoalYLevel(60),
        config=NavigationRunConfig(max_segments=1, segment_timeout_s=10.0, min_partial_progress=1),
    )
    final = body.get_state()
    payload = result.to_payload()
    pillar_block = body.perceive("blockAt", {"x": 0, "y": 59, "z": 0})
    if result.success:
        raise AssertionError(f"pillar unexpectedly succeeded without scaffold: result={payload} final={final}")
    if result.reason != "dig_up_no_scaffold_available":
        raise AssertionError(f"pillar no-scaffold returned wrong reason: result={payload} final={final}")
    if final.pos[1] >= 60.0:
        raise AssertionError(f"pillar no-scaffold moved upward despite failure: final={final.pos} result={payload}")
    if pillar_block.data.get("type") not in {"air", "minecraft:air"}:
        raise AssertionError(f"pillar no-scaffold mutated world: block={pillar_block.data} result={payload}")
    segments = (result.metrics or {}).get("segments", [])
    segment = segments[0]["diagnostics"]["segment"] if segments else {}
    if segment.get("path_moves") != ["pillar"] or segment.get("movement_waypoints") != []:
        raise AssertionError(f"pillar no-scaffold did not preserve controller facts: result={payload}")
    return {
        "reason": result.reason,
        "final": final.pos,
        "path_moves": segment.get("path_moves"),
        "movement_waypoints": segment.get("movement_waypoints"),
        "pillar_block": pillar_block.data,
        "metrics": payload,
    }


def run_downward_happy_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill -2 60 -2 2 66 2 air")
    command(rcon, "fill -2 57 -2 2 59 2 stone")
    command(rcon, f"tp {BOT} 0 60 0 0 0")
    command(rcon, f"item replace entity {BOT} weapon.mainhand with diamond_pickaxe")
    cells = {
        (0, 60, 0): GridCell(),
        (0, 59, 0): GridCell(block_type="stone", walkable=False),
    }
    policy = GovernancePolicy(natural_regions=[Region("nav_downward", (-2, 0, -2), (2, 100, 2))])
    runtime = navigation_runtime(body, GridWorld(cells), policy)

    result = runtime.navigate_to(
        (0, 59, 0),
        config=NavigationRunConfig(max_segments=2, segment_timeout_s=25.0, min_partial_progress=1),
    )
    final = body.get_state()
    payload = result.to_payload()
    opened = body.perceive("blockAt", {"x": 0, "y": 59, "z": 0})
    if not result.success or result.reason != "arrived":
        raise AssertionError(f"downward navigation did not arrive: result={payload} final={final}")
    if final.pos[1] >= 60.0:
        raise AssertionError(f"downward navigation did not descend: final={final.pos} result={payload}")
    if opened.data.get("state") != "CLEAR":
        raise AssertionError(f"downward floor block was not opened: block={opened.data} result={payload}")
    segments = (result.metrics or {}).get("segments", [])
    first_segment = segments[0]["diagnostics"]["segment"] if segments else {}
    moves = first_segment.get("path_moves")
    waypoints = first_segment.get("movement_waypoints")
    cancel = first_segment.get("movement_cancel", {})
    if moves != ["downward"]:
        raise AssertionError(f"downward path did not expose downward move: result={payload}")
    if waypoints != []:
        raise AssertionError(f"downward move leaked into moveTo waypoints: result={payload}")
    if cancel.get("safe_to_cancel") is not False or cancel.get("policies") != ["finish_or_abort_controller"]:
        raise AssertionError(f"downward cancel policy missing controller boundary: result={payload}")
    return {
        "reason": result.reason,
        "final": final.pos,
        "path_moves": moves,
        "movement_waypoints": waypoints,
        "movement_cancel": cancel,
        "opened": opened.data,
        "metrics": payload,
    }


def run_downward_protected_floor_failure(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill -2 60 -2 2 66 2 air")
    command(rcon, "fill -2 57 -2 2 59 2 stone")
    command(rcon, f"tp {BOT} 0 60 0 0 0")
    command(rcon, f"item replace entity {BOT} weapon.mainhand with diamond_pickaxe")
    cells = {
        (0, 60, 0): GridCell(),
        (0, 59, 0): GridCell(block_type="stone", walkable=False),
    }
    policy = GovernancePolicy(
        natural_regions=[Region("nav_downward", (-2, 0, -2), (2, 100, 2))],
        protected_regions=[Region("protected_floor", (0, 59, 0), (0, 59, 0))],
    )
    runtime = navigation_runtime(body, GridWorld(cells), policy)

    result = runtime.navigate_to(
        (0, 59, 0),
        config=NavigationRunConfig(max_segments=1, segment_timeout_s=10.0, min_partial_progress=1),
    )
    final = body.get_state()
    payload = result.to_payload()
    floor = body.perceive("blockAt", {"x": 0, "y": 59, "z": 0})
    if result.success:
        raise AssertionError(f"downward unexpectedly succeeded through protected floor: result={payload} final={final}")
    if result.reason != "navigation_blocked:no_path":
        raise AssertionError(f"downward protected floor returned wrong reason: result={payload} final={final}")
    path_update = (result.metrics or {}).get("path_update", {})
    blocked_reasons = path_update.get("blocked_reasons", {})
    if blocked_reasons.get("break_denied:protected_region") != 1:
        raise AssertionError(f"downward protected floor did not expose governance denial: result={payload}")
    segments = (result.metrics or {}).get("segments", [])
    if segments and segments[0].get("action_id") is not None:
        raise AssertionError(f"downward protected floor dispatched an action: result={payload}")
    if final.pos[1] < 60.0:
        raise AssertionError(f"downward protected floor moved the bot despite denial: final={final.pos} result={payload}")
    if floor.data.get("state") != "SOLID":
        raise AssertionError(f"downward protected floor mutated protected block: floor={floor.data} result={payload}")
    return {
        "reason": result.reason,
        "final": final.pos,
        "blocked_reasons": blocked_reasons,
        "floor": floor.data,
        "metrics": payload,
    }


def run_unsafe_fall_failure(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill -1 54 -1 2 66 1 air")
    command(rcon, "setblock 1 58 0 stone")
    command(rcon, f"tp {BOT} 0 64 0 -90 0")
    cells = {
        (0, 64, 0): GridCell(),
        (1, 63, 0): GridCell(fall_depth=6),
    }
    policy = GovernancePolicy(natural_regions=[Region("nav_fall", (-1, 0, -1), (2, 100, 1))])
    runtime = navigation_runtime(body, GridWorld(cells), policy)

    result = runtime.navigate_to(
        (1, 63, 0),
        config=NavigationRunConfig(max_segments=1, segment_timeout_s=5.0, min_partial_progress=1),
    )
    final = body.get_state()
    payload = result.to_payload()
    if result.success:
        raise AssertionError(f"unsafe fall unexpectedly succeeded: result={payload} final={final}")
    if result.reason != "navigation_blocked:no_path":
        raise AssertionError(f"unsafe fall returned wrong reason: result={payload} final={final}")
    path_update = (result.metrics or {}).get("path_update", {})
    blocked_reasons = path_update.get("blocked_reasons", {})
    if blocked_reasons.get("fall_denied:unsafe_depth") != 1:
        raise AssertionError(f"unsafe fall did not expose fall denial: result={payload}")
    segments = (result.metrics or {}).get("segments", [])
    if segments and segments[0].get("action_id") is not None:
        raise AssertionError(f"unsafe fall dispatched an action despite planner denial: result={payload}")
    if final.pos[1] < 63.5:
        raise AssertionError(f"unsafe fall moved the bot despite planner denial: final={final.pos} result={payload}")
    return {
        "reason": result.reason,
        "final": final.pos,
        "blocked_reasons": blocked_reasons,
        "metrics": payload,
    }


def run_survival_reflex_preempts_navigation(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill -2 60 -2 16 66 3 air")
    command(rcon, "fill -2 59 -2 16 59 3 stone")
    command(rcon, "setblock 5 59 0 lava")
    command(rcon, f"tp {BOT} 0 60 0 -90 0")
    policy = GovernancePolicy(natural_regions=[Region("nav_reflex", (-2, 0, -2), (16, 100, 3))])
    runtime = navigation_runtime(body, flat_world(-1, 14, -1, 1, y=60), policy)

    result = runtime.navigate_to(
        (14, 60, 0),
        config=NavigationRunConfig(max_segments=2, segment_timeout_s=12.0, min_partial_progress=1),
    )
    payload = result.to_payload()
    if not result.success or result.reason != "preempted" or not result.can_retry:
        raise AssertionError(f"navigation was not neutrally preempted by survival reflex: {payload}")
    if not result.metrics or result.metrics.get("paused") is not True:
        raise AssertionError(f"preempted navigation did not expose paused sentinel: {payload}")
    segments = result.metrics.get("segments", [])
    if len(segments) != 1:
        raise AssertionError(f"preempted navigation continued after reflex: {payload}")
    if segments[0].get("terminal_reason") != "preempted":
        raise AssertionError(f"preempted navigation terminal reason was not preserved: {payload}")
    terminal = (segments[0].get("diagnostics", {}).get("terminal", {}) if segments else {})
    if terminal.get("stopped_reason") != "preempted":
        raise AssertionError(f"terminal moveDone did not report preempted: {payload}")

    triggered = next((event for event in body.event_log if event.name == "reflexTriggered"), None)
    if triggered is None:
        triggered = wait_for_named_event(body, "reflexTriggered", timeout_s=3.0)
    completed = next((event for event in body.event_log if event.name == "reflexCompleted"), None)
    if completed is None:
        completed = wait_for_named_event(body, "reflexCompleted", timeout_s=8.0)
    if completed.data.get("escaped_lava") is not True:
        raise AssertionError(f"survival reflex did not report lava escape: triggered={triggered} completed={completed} result={payload}")
    final = body.get_state()
    if distance(final.pos, tuple(completed.data.get("final_pos", final.pos))) > 1.5:
        raise AssertionError(f"body state diverged from reflex completion: final={final} completed={completed}")
    return {
        "reason": result.reason,
        "can_retry": result.can_retry,
        "paused": result.metrics.get("paused") if result.metrics else None,
        "terminal": terminal,
        "reflex_triggered": triggered.data,
        "reflex_completed": completed.data,
        "final": final.pos,
        "metrics": payload,
    }


def run_recovery_detour_distance_ladder(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill -2 59 -2 8 66 3 air")
    command(rcon, "fill -2 58 -2 8 58 3 stone")
    command(rcon, "setblock 0 59 1 stone")
    command(rcon, f"tp {BOT} 0 59 0 -90 0")
    policy = GovernancePolicy(natural_regions=[Region("nav_recovery", (-2, 0, -2), (8, 100, 3))])
    runtime = NavigationTransactions(
        body,
        FakeNavigator(
            [
                fake_segment(
                    "advanced",
                    (0, 59, 1),
                    success=False,
                    reason="partial",
                    path=(PathStep(pos=(0, 59, 1), move=MoveKind.WALK, cost=1.0, reason="walk"),),
                ),
                fake_segment(
                    "arrived",
                    (4, 59, 0),
                    success=True,
                    reason="arrived",
                    path=(
                        PathStep(pos=(3, 59, 0), move=MoveKind.WALK, cost=1.0, reason="walk"),
                        PathStep(pos=(4, 59, 0), move=MoveKind.WALK, cost=1.0, reason="walk"),
                    ),
                ),
            ]
        ),
    )

    result = runtime.navigate_to(
        (4, 59, 0),
        config=NavigationRunConfig(
            max_segments=3,
            min_partial_progress=1,
            recovery_attempts=1,
            recovery_detour_distances=(1, 2),
            recovery_detour_offsets=((1, 0),),
            recovery_detour_max_attempts=2,
            recovery_min_displacement=1.5,
            recovery_detour_timeout_s=4.0,
        ),
    )
    final = body.get_state()
    payload = result.to_payload()
    if not result.success or result.reason != "arrived":
        raise AssertionError(f"recovery detour ladder did not arrive: result={payload} final={final}")
    if distance(final.pos, (4, 59, 0)) > 1.35:
        raise AssertionError(f"recovery detour ladder final position too far: final={final.pos} result={payload}")
    segments = (result.metrics or {}).get("segments", [])
    statuses = [segment.get("status") for segment in segments]
    if statuses != ["advanced", "recovery_detour", "recovery_detour", "arrived"]:
        raise AssertionError(f"recovery detour ladder did not expose expected segment sequence: {payload}")
    first_detour = segments[1]
    second_detour = segments[2]
    if first_detour.get("terminal_reason") != "no_displacement" or first_detour.get("success") is not False:
        raise AssertionError(f"recovery detour ladder first rung was not an honest no-displacement failure: {payload}")
    attempts = second_detour.get("diagnostics", {}).get("attempts", [])
    if len(attempts) != 2:
        raise AssertionError(f"recovery detour ladder did not record both detour attempts: {payload}")
    if attempts[0].get("origin") != [0, 59, 1]:
        raise AssertionError(f"recovery detour ladder first attempt origin drifted unexpectedly: {payload}")
    if attempts[0].get("target") != [1, 59, 1] or attempts[0].get("detour_distance") != 1:
        raise AssertionError(f"recovery detour ladder first attempt metadata wrong: {payload}")
    if attempts[0].get("displaced") is not False:
        raise AssertionError(f"recovery detour ladder first attempt should fail displacement gate: {payload}")
    if attempts[1].get("target") != [2, 59, 1] or attempts[1].get("detour_distance") != 2:
        raise AssertionError(f"recovery detour ladder second attempt metadata wrong: {payload}")
    if attempts[1].get("displaced") is not True:
        raise AssertionError(f"recovery detour ladder second attempt did not produce sufficient displacement: {payload}")
    if second_detour.get("success") is not True:
        raise AssertionError(f"recovery detour ladder did not mark the successful farther rung: {payload}")
    blocked = body.perceive("blockAt", {"x": 0, "y": 59, "z": 1})
    if blocked.data.get("state") != "SOLID":
        raise AssertionError(f"recovery detour ladder mutated the blocked north cell unexpectedly: {blocked.data} result={payload}")
    return {
        "reason": result.reason,
        "final": final.pos,
        "statuses": statuses,
        "first_detour": first_detour,
        "second_detour": second_detour,
        "blocked": blocked.data,
        "metrics": payload,
    }


def run_recovery_support_step_ladder(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill -2 59 -2 8 67 3 air")
    command(rcon, "fill -2 58 -2 8 58 3 stone")
    command(rcon, "setblock 0 59 1 stone")
    command(rcon, "setblock 1 59 1 stone")
    command(rcon, f"tp {BOT} 0 59 0 -90 0")
    policy = GovernancePolicy(natural_regions=[Region("nav_recovery_step", (-2, 0, -2), (8, 100, 3))])
    world = GridWorld(
        {
            (0, 59, 0): GridCell(),
            (0, 59, 1): GridCell(),
            (1, 59, 1): GridCell(block_type="stone", walkable=False),
            (1, 60, 1): GridCell(),
            (2, 60, 1): GridCell(),
            (4, 59, 0): GridCell(),
        }
    )
    runtime = NavigationTransactions(
        body,
        FakeNavigator(
            [
                fake_segment(
                    "advanced",
                    (0, 59, 1),
                    success=False,
                    reason="partial",
                    path=(PathStep(pos=(0, 59, 1), move=MoveKind.WALK, cost=1.0, reason="walk"),),
                ),
                fake_segment(
                    "arrived",
                    (4, 59, 0),
                    success=True,
                    reason="arrived",
                    path=(
                        PathStep(pos=(3, 59, 0), move=MoveKind.WALK, cost=1.0, reason="walk"),
                        PathStep(pos=(4, 59, 0), move=MoveKind.WALK, cost=1.0, reason="walk"),
                    ),
                ),
            ],
            world=world,
            costs=NavigationCostModel(policy),
        ),
    )

    result = runtime.navigate_to(
        (4, 59, 0),
        config=NavigationRunConfig(
            max_segments=3,
            min_partial_progress=1,
            recovery_attempts=1,
            recovery_detour_distances=(1, 2),
            recovery_detour_offsets=((1, 0),),
            recovery_detour_y_offsets=(0, 1, -1),
            recovery_detour_max_attempts=2,
            recovery_min_displacement=1.1,
            recovery_detour_timeout_s=4.0,
        ),
    )
    final = body.get_state()
    payload = result.to_payload()
    if not result.success or result.reason != "arrived":
        raise AssertionError(f"recovery support-step ladder did not arrive: result={payload} final={final}")
    if distance(final.pos, (4, 59, 0)) > 1.35:
        raise AssertionError(f"recovery support-step ladder final position too far: final={final.pos} result={payload}")
    segments = (result.metrics or {}).get("segments", [])
    if [segment.get("status") for segment in segments] != ["advanced", "recovery_detour", "arrived"]:
        raise AssertionError(f"recovery support-step ladder did not expose expected segment sequence: {payload}")
    attempt = segments[1].get("diagnostics", {}).get("attempts", [None])[0]
    if not attempt:
        raise AssertionError(f"recovery support-step ladder missing detour attempt facts: {payload}")
    if attempt.get("target") != [1, 60, 1]:
        raise AssertionError(f"recovery support-step ladder picked the wrong support target: {payload}")
    if attempt.get("target_y_offset") != 1 or attempt.get("target_kind") != "support_step_up":
        raise AssertionError(f"recovery support-step ladder did not classify support-step-up target: {payload}")
    if attempt.get("displaced") is not True:
        raise AssertionError(f"recovery support-step ladder did not achieve real displacement: {payload}")
    blocked = body.perceive("blockAt", {"x": 0, "y": 59, "z": 1})
    if blocked.data.get("state") != "SOLID":
        raise AssertionError(f"recovery support-step ladder mutated the blocked north cell unexpectedly: {blocked.data} result={payload}")
    return {
        "reason": result.reason,
        "final": final.pos,
        "attempt": attempt,
        "blocked": blocked.data,
        "metrics": payload,
    }


def run_recovery_support_step_down_ladder(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill -2 57 -2 8 67 3 air")
    command(rcon, "fill -2 58 -2 8 58 3 stone")
    command(rcon, "setblock 0 59 1 stone")
    command(rcon, "setblock 1 58 1 air")
    command(rcon, "setblock 1 57 1 stone")
    command(rcon, f"tp {BOT} 0 59 0 -90 0")
    policy = GovernancePolicy(natural_regions=[Region("nav_recovery_step_down", (-2, 0, -2), (8, 100, 3))])
    world = GridWorld(
        {
            (0, 59, 0): GridCell(),
            (0, 59, 1): GridCell(),
            (1, 59, 1): GridCell(),
            (1, 58, 1): GridCell(),
            (1, 57, 1): GridCell(block_type="stone", walkable=False),
            (3, 59, 0): GridCell(),
            (4, 59, 0): GridCell(),
        }
    )
    runtime = NavigationTransactions(
        body,
        FakeNavigator(
            [
                fake_segment(
                    "advanced",
                    (0, 59, 1),
                    success=False,
                    reason="partial",
                    path=(PathStep(pos=(0, 59, 1), move=MoveKind.WALK, cost=1.0, reason="walk"),),
                ),
                fake_segment(
                    "arrived",
                    (4, 59, 0),
                    success=True,
                    reason="arrived",
                    path=(
                        PathStep(pos=(3, 59, 0), move=MoveKind.WALK, cost=1.0, reason="walk"),
                        PathStep(pos=(4, 59, 0), move=MoveKind.WALK, cost=1.0, reason="walk"),
                    ),
                ),
            ],
            world=world,
            costs=NavigationCostModel(policy),
        ),
    )

    result = runtime.navigate_to(
        (4, 59, 0),
        config=NavigationRunConfig(
            max_segments=3,
            min_partial_progress=1,
            recovery_attempts=1,
            recovery_detour_distances=(1,),
            recovery_detour_offsets=((1, 0),),
            recovery_detour_y_offsets=(0, -1, 1),
            recovery_detour_max_attempts=1,
            recovery_min_displacement=1.1,
            recovery_detour_timeout_s=5.0,
        ),
    )
    final = body.get_state()
    payload = result.to_payload()
    if not result.success or result.reason != "arrived":
        raise AssertionError(f"recovery support-step-down ladder did not arrive: result={payload} final={final}")
    if distance(final.pos, (4, 59, 0)) > 1.35:
        raise AssertionError(f"recovery support-step-down final position too far: final={final.pos} result={payload}")
    segments = (result.metrics or {}).get("segments", [])
    if [segment.get("status") for segment in segments] != ["advanced", "recovery_detour", "arrived"]:
        raise AssertionError(f"recovery support-step-down did not expose expected segment sequence: {payload}")
    attempt = segments[1].get("diagnostics", {}).get("attempts", [None])[0]
    if not attempt:
        raise AssertionError(f"recovery support-step-down missing detour attempt facts: {payload}")
    if attempt.get("target") != [1, 58, 1]:
        raise AssertionError(f"recovery support-step-down picked the wrong target: {payload}")
    if attempt.get("target_y_offset") != -1 or attempt.get("target_kind") != "support_step_down":
        raise AssertionError(f"recovery support-step-down did not classify support-step-down target: {payload}")
    if attempt.get("pulse_kind") != "single_waypoint_move" or attempt.get("path_moves") != ["walk"]:
        raise AssertionError(f"recovery support-step-down did not expose short-pulse movement facts: {payload}")
    if attempt.get("displaced") is not True:
        raise AssertionError(f"recovery support-step-down did not achieve real displacement: {payload}")
    target = attempt.get("target")
    support = body.perceive("blockAt", {"x": target[0], "y": target[1] - 1, "z": target[2]})
    if support.data.get("state") != "SOLID":
        raise AssertionError(f"recovery support-step-down support block missing: {support.data} result={payload}")
    blocked = body.perceive("blockAt", {"x": 0, "y": 59, "z": 1})
    if blocked.data.get("state") != "SOLID":
        raise AssertionError(f"recovery support-step-down mutated the blocked north cell unexpectedly: {blocked.data} result={payload}")
    return {
        "reason": result.reason,
        "final": final.pos,
        "attempt": attempt,
        "support": support.data,
        "blocked": blocked.data,
        "metrics": payload,
    }


def run_recovery_clearance_ladder(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill -2 59 -2 8 66 3 air")
    command(rcon, "fill -2 58 -2 8 58 3 stone")
    command(rcon, "setblock 0 59 1 stone")
    command(rcon, "setblock 1 59 1 dirt")
    command(rcon, f"tp {BOT} 0 59 0 -90 0")
    policy = GovernancePolicy(natural_regions=[Region("nav_recovery_clearance", (-2, 0, -2), (8, 100, 3))])
    world = GridWorld(
        {
            (0, 59, 0): GridCell(),
            (0, 59, 1): GridCell(),
            (1, 59, 1): GridCell(block_type="dirt", walkable=False),
            (2, 59, 1): GridCell(),
            (3, 59, 0): GridCell(),
            (4, 59, 0): GridCell(),
        }
    )
    runtime = NavigationTransactions(
        body,
        FakeNavigator(
            [
                fake_segment(
                    "advanced",
                    (0, 59, 1),
                    success=False,
                    reason="partial",
                    path=(PathStep(pos=(0, 59, 1), move=MoveKind.WALK, cost=1.0, reason="walk"),),
                ),
                fake_segment(
                    "arrived",
                    (4, 59, 0),
                    success=True,
                    reason="arrived",
                    path=(
                        PathStep(pos=(3, 59, 0), move=MoveKind.WALK, cost=1.0, reason="walk"),
                        PathStep(pos=(4, 59, 0), move=MoveKind.WALK, cost=1.0, reason="walk"),
                    ),
                ),
            ],
            world=world,
            costs=NavigationCostModel(policy),
        ),
    )

    result = runtime.navigate_to(
        (4, 59, 0),
        config=NavigationRunConfig(
            max_segments=3,
            min_partial_progress=1,
            recovery_attempts=1,
            recovery_detour_distances=(1,),
            recovery_detour_offsets=((1, 0),),
            recovery_detour_max_attempts=1,
            recovery_min_displacement=1.1,
            recovery_detour_timeout_s=4.0,
        ),
    )
    final = body.get_state()
    payload = result.to_payload()
    if not result.success or result.reason != "arrived":
        raise AssertionError(f"recovery clearance ladder did not arrive: result={payload} final={final}")
    if distance(final.pos, (4, 59, 0)) > 1.35:
        raise AssertionError(f"recovery clearance ladder final position too far: final={final.pos} result={payload}")
    segments = (result.metrics or {}).get("segments", [])
    statuses = [segment.get("status") for segment in segments]
    if statuses != ["advanced", "recovery_detour", "arrived"]:
        raise AssertionError(f"recovery clearance ladder did not expose expected segment sequence: {payload}")
    detour = segments[1]
    if detour.get("terminal_reason") != "no_displacement" or detour.get("success") is not False:
        raise AssertionError(f"recovery clearance ladder should keep the detour rung honest on low displacement: {payload}")
    attempts = detour.get("diagnostics", {}).get("attempts", [])
    if len(attempts) != 1:
        raise AssertionError(f"recovery clearance ladder should record exactly one detour attempt: {payload}")
    attempt = attempts[0]
    if attempt.get("target") != [1, 59, 1] or attempt.get("detour_distance") != 1:
        raise AssertionError(f"recovery clearance ladder detour target metadata wrong: {payload}")
    clearance = attempt.get("clearance")
    if not clearance or clearance.get("attempted") is not True:
        raise AssertionError(f"recovery clearance ladder did not expose clearance facts: {payload}")
    if clearance.get("target") != [1, 59, 1]:
        raise AssertionError(f"recovery clearance ladder cleared the wrong block: {payload}")
    if clearance.get("result", {}).get("reason") != "completed":
        raise AssertionError(f"recovery clearance ladder mine step did not complete: {payload}")
    if clearance.get("retry", {}).get("reason") != "arrived":
        raise AssertionError(f"recovery clearance ladder retry step did not arrive: {payload}")
    if clearance.get("success") is not False or clearance.get("reason") != "no_displacement":
        raise AssertionError(f"recovery clearance ladder should keep clearance retry honest on low displacement: {payload}")
    if attempt.get("displaced") is not False or float(attempt.get("displacement", 0.0)) >= 1.1:
        raise AssertionError(f"recovery clearance ladder did not preserve the displacement gate: {payload}")
    uncleared = body.perceive("blockAt", {"x": 0, "y": 59, "z": 1})
    cleared = body.perceive("blockAt", {"x": 1, "y": 59, "z": 1})
    if uncleared.data.get("state") != "SOLID":
        raise AssertionError(
            f"recovery clearance ladder mutated the original blocked north cell unexpectedly: {uncleared.data} result={payload}"
        )
    if cleared.data.get("state") != "CLEAR":
        raise AssertionError(f"recovery clearance ladder did not clear the detour target block: {cleared.data} result={payload}")
    return {
        "reason": result.reason,
        "final": final.pos,
        "statuses": statuses,
        "detour": detour,
        "attempt": attempt,
        "uncleared_block": uncleared.data,
        "cleared_block": cleared.data,
        "metrics": payload,
    }


def run_recovery_gravel_clearance_ladder(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill -2 59 -2 8 66 3 air")
    command(rcon, "fill -2 58 -2 8 58 3 stone")
    command(rcon, "setblock 0 59 1 stone")
    command(rcon, "setblock 1 59 1 gravel")
    command(rcon, f"tp {BOT} 0 59 0 -90 0")
    policy = GovernancePolicy(natural_regions=[Region("nav_recovery_gravel_clearance", (-2, 0, -2), (8, 100, 3))])
    world = GridWorld(
        {
            (0, 59, 0): GridCell(),
            (0, 59, 1): GridCell(),
            (1, 59, 1): GridCell(block_type="gravel", walkable=False),
            (2, 59, 1): GridCell(),
            (3, 59, 0): GridCell(),
            (4, 59, 0): GridCell(),
        }
    )
    runtime = NavigationTransactions(
        body,
        FakeNavigator(
            [
                fake_segment(
                    "advanced",
                    (0, 59, 1),
                    success=False,
                    reason="partial",
                    path=(PathStep(pos=(0, 59, 1), move=MoveKind.WALK, cost=1.0, reason="walk"),),
                ),
                fake_segment(
                    "arrived",
                    (4, 59, 0),
                    success=True,
                    reason="arrived",
                    path=(
                        PathStep(pos=(3, 59, 0), move=MoveKind.WALK, cost=1.0, reason="walk"),
                        PathStep(pos=(4, 59, 0), move=MoveKind.WALK, cost=1.0, reason="walk"),
                    ),
                ),
            ],
            world=world,
            costs=NavigationCostModel(policy),
        ),
    )

    result = runtime.navigate_to(
        (4, 59, 0),
        config=NavigationRunConfig(
            max_segments=3,
            min_partial_progress=1,
            recovery_attempts=1,
            recovery_detour_distances=(1,),
            recovery_detour_offsets=((1, 0),),
            recovery_detour_max_attempts=1,
            recovery_min_displacement=1.1,
            recovery_detour_timeout_s=4.0,
        ),
    )
    final = body.get_state()
    payload = result.to_payload()
    if not result.success or result.reason != "arrived":
        raise AssertionError(f"recovery gravel-clearance ladder did not arrive: result={payload} final={final}")
    if distance(final.pos, (4, 59, 0)) > 1.35:
        raise AssertionError(f"recovery gravel-clearance final position too far: final={final.pos} result={payload}")
    segments = (result.metrics or {}).get("segments", [])
    statuses = [segment.get("status") for segment in segments]
    if statuses != ["advanced", "recovery_detour", "arrived"]:
        raise AssertionError(f"recovery gravel-clearance did not expose expected segment sequence: {payload}")
    detour = segments[1]
    attempts = detour.get("diagnostics", {}).get("attempts", [])
    if len(attempts) != 1:
        raise AssertionError(f"recovery gravel-clearance should record exactly one detour attempt: {payload}")
    attempt = attempts[0]
    if attempt.get("target") != [1, 59, 1] or attempt.get("target_kind") != "fallback_raw":
        raise AssertionError(f"recovery gravel-clearance target metadata wrong: {payload}")
    if attempt.get("pulse_kind") != "single_waypoint_move":
        raise AssertionError(f"recovery gravel-clearance did not expose short-pulse facts: {payload}")
    clearance = attempt.get("clearance")
    if not clearance or clearance.get("attempted") is not True:
        raise AssertionError(f"recovery gravel-clearance did not expose clearance facts: {payload}")
    if clearance.get("result", {}).get("metrics", {}).get("block_type") != "gravel":
        raise AssertionError(f"recovery gravel-clearance did not mine gravel: {payload}")
    if clearance.get("result", {}).get("metrics", {}).get("legality", {}).get("reason") != "allowed_natural":
        raise AssertionError(f"recovery gravel-clearance did not use governed natural legality: {payload}")
    if clearance.get("result", {}).get("reason") != "completed":
        raise AssertionError(f"recovery gravel-clearance mine step did not complete: {payload}")
    cleared = body.perceive("blockAt", {"x": 1, "y": 59, "z": 1})
    if cleared.data.get("state") != "CLEAR":
        raise AssertionError(f"recovery gravel-clearance did not clear the detour target block: {cleared.data} result={payload}")
    return {
        "reason": result.reason,
        "final": final.pos,
        "statuses": statuses,
        "detour": detour,
        "attempt": attempt,
        "cleared_block": cleared.data,
        "metrics": payload,
    }


def run_recovery_exhausted_honesty(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill -2 59 -2 8 66 3 air")
    command(rcon, "fill -2 58 -2 8 58 3 stone")
    command(rcon, "setblock 0 59 1 stone")
    command(rcon, "setblock 1 59 1 chest")
    command(rcon, f"tp {BOT} 0 59 0 -90 0")
    policy = GovernancePolicy(natural_regions=[Region("nav_recovery_exhausted", (-2, 0, -2), (8, 100, 3))])
    world = GridWorld(
        {
            (0, 59, 0): GridCell(),
            (0, 59, 1): GridCell(),
            (1, 59, 1): GridCell(block_type="chest", walkable=False),
            (3, 59, 0): GridCell(),
            (4, 59, 0): GridCell(),
        }
    )
    runtime = NavigationTransactions(
        body,
        FakeNavigator(
            [
                fake_segment(
                    "advanced",
                    (0, 59, 1),
                    success=False,
                    reason="partial",
                    path=(PathStep(pos=(0, 59, 1), move=MoveKind.WALK, cost=1.0, reason="walk"),),
                )
            ],
            world=world,
            costs=NavigationCostModel(policy),
        ),
    )

    result = runtime.navigate_to(
        (4, 59, 0),
        config=NavigationRunConfig(
            max_segments=3,
            min_partial_progress=1,
            recovery_attempts=1,
            recovery_detour_distances=(1,),
            recovery_detour_offsets=((1, 0),),
            recovery_detour_y_offsets=(0,),
            recovery_detour_max_attempts=1,
            recovery_min_displacement=1.1,
            recovery_detour_timeout_s=4.0,
        ),
    )
    final = body.get_state()
    payload = result.to_payload()
    if result.success:
        raise AssertionError(f"recovery exhausted unexpectedly succeeded: result={payload} final={final}")
    if result.reason != "stuck" or result.can_retry is not True:
        raise AssertionError(f"recovery exhausted returned wrong terminal contract: result={payload} final={final}")
    if not (result.metrics or {}).get("recovery_exhausted"):
        raise AssertionError(f"recovery exhausted did not expose recovery_exhausted=true: {payload}")
    segments = (result.metrics or {}).get("segments", [])
    statuses = [segment.get("status") for segment in segments]
    if statuses != ["advanced", "recovery_detour", "advanced", "recovery_detour"]:
        raise AssertionError(f"recovery exhausted did not expose expected bounded retry sequence: {payload}")
    detours = [segment for segment in segments if segment.get("status") == "recovery_detour"]
    if len(detours) != 2:
        raise AssertionError(f"recovery exhausted should expose two bounded detour attempts before exhausting: {payload}")
    for detour in detours:
        if detour.get("terminal_reason") != "no_displacement" or detour.get("success") is not False:
            raise AssertionError(f"recovery exhausted detour should stay honest: {payload}")
    attempts = [
        attempt
        for detour in detours
        for attempt in detour.get("diagnostics", {}).get("attempts", [])
    ]
    if len(attempts) != 2:
        raise AssertionError(f"recovery exhausted should record both detour attempts: {payload}")
    attempt = attempts[0]
    if attempt.get("pulse_kind") != "single_waypoint_move":
        raise AssertionError(f"recovery exhausted did not expose pulse facts: {payload}")
    protected_clearance = attempt.get("clearance")
    if not protected_clearance or protected_clearance.get("attempted") is not True:
        raise AssertionError(f"recovery exhausted did not try governed clearance: {payload}")
    if protected_clearance.get("success") is not False:
        raise AssertionError(f"recovery exhausted protected clearance should fail: {payload}")
    if protected_clearance.get("result", {}).get("reason") != "break_denied:protected_type":
        raise AssertionError(f"recovery exhausted clearance did not preserve protected-block failure: {payload}")
    if not any(item.get("clearance", {}).get("success") is False for item in attempts):
        raise AssertionError(f"recovery exhausted should expose failed clearance facts: {payload}")
    chest = body.perceive("blockAt", {"x": 1, "y": 59, "z": 1})
    if chest.data.get("state") != "SOLID" or chest.data.get("type") not in {"chest", "minecraft:chest"}:
        raise AssertionError(f"recovery exhausted mutated protected chest: block={chest.data} result={payload}")
    if distance(final.pos, (4, 59, 0)) <= 1.35:
        raise AssertionError(f"recovery exhausted should not arrive at the original goal: final={final.pos} result={payload}")
    return {
        "reason": result.reason,
        "can_retry": result.can_retry,
        "final": final.pos,
        "statuses": statuses,
        "attempt": attempt,
        "chest": chest.data,
        "metrics": payload,
    }


def run_recovery_clearance_success_ladder(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill -2 59 -2 8 66 3 air")
    command(rcon, "fill -2 58 -2 8 58 3 stone")
    command(rcon, "setblock 0 59 1 stone")
    command(rcon, "setblock 2 59 1 dirt")
    command(rcon, f"tp {BOT} 0 59 0 -90 0")
    policy = GovernancePolicy(natural_regions=[Region("nav_recovery_clearance_success", (-2, 0, -2), (8, 100, 3))])
    world = GridWorld(
        {
            (0, 59, 0): GridCell(),
            (0, 59, 1): GridCell(),
            (1, 59, 1): GridCell(),
            (2, 59, 1): GridCell(block_type="dirt", walkable=False),
            (3, 59, 0): GridCell(),
            (4, 59, 0): GridCell(),
        }
    )
    runtime = NavigationTransactions(
        body,
        FakeNavigator(
            [
                fake_segment(
                    "advanced",
                    (0, 59, 1),
                    success=False,
                    reason="partial",
                    path=(PathStep(pos=(0, 59, 1), move=MoveKind.WALK, cost=1.0, reason="walk"),),
                ),
                fake_segment(
                    "arrived",
                    (4, 59, 0),
                    success=True,
                    reason="arrived",
                    path=(
                        PathStep(pos=(3, 59, 0), move=MoveKind.WALK, cost=1.0, reason="walk"),
                        PathStep(pos=(4, 59, 0), move=MoveKind.WALK, cost=1.0, reason="walk"),
                    ),
                ),
            ],
            world=world,
            costs=NavigationCostModel(policy),
        ),
    )

    result = runtime.navigate_to(
        (4, 59, 0),
        config=NavigationRunConfig(
            max_segments=3,
            min_partial_progress=1,
            recovery_attempts=1,
            recovery_detour_distances=(2,),
            recovery_detour_offsets=((1, 0),),
            recovery_detour_max_attempts=1,
            recovery_min_displacement=1.5,
            recovery_detour_timeout_s=4.0,
        ),
    )
    final = body.get_state()
    payload = result.to_payload()
    if not result.success or result.reason != "arrived":
        raise AssertionError(f"recovery clearance-success ladder did not arrive: result={payload} final={final}")
    if distance(final.pos, (4, 59, 0)) > 1.35:
        raise AssertionError(f"recovery clearance-success final position too far: final={final.pos} result={payload}")
    segments = (result.metrics or {}).get("segments", [])
    statuses = [segment.get("status") for segment in segments]
    if statuses != ["advanced", "recovery_detour", "arrived"]:
        raise AssertionError(f"recovery clearance-success did not expose expected segment sequence: {payload}")
    detour = segments[1]
    if detour.get("terminal_reason") != "arrived" or detour.get("success") is not True:
        raise AssertionError(f"recovery clearance-success should mark the detour rung successful after retry: {payload}")
    attempts = detour.get("diagnostics", {}).get("attempts", [])
    if len(attempts) != 1:
        raise AssertionError(f"recovery clearance-success should record exactly one detour attempt: {payload}")
    attempt = attempts[0]
    if attempt.get("target") != [2, 59, 1] or attempt.get("detour_distance") != 2:
        raise AssertionError(f"recovery clearance-success target metadata wrong: {payload}")
    clearance = attempt.get("clearance")
    if not clearance or clearance.get("attempted") is not True or clearance.get("success") is not True:
        raise AssertionError(f"recovery clearance-success did not expose successful clearance facts: {payload}")
    if clearance.get("target") != [2, 59, 1]:
        raise AssertionError(f"recovery clearance-success cleared the wrong block: {payload}")
    if clearance.get("result", {}).get("reason") != "completed":
        raise AssertionError(f"recovery clearance-success mine step did not complete: {payload}")
    if clearance.get("retry", {}).get("reason") != "arrived":
        raise AssertionError(f"recovery clearance-success retry step did not arrive: {payload}")
    if attempt.get("displaced") is not True or float(attempt.get("displacement", 0.0)) < 1.5:
        raise AssertionError(f"recovery clearance-success did not satisfy the displacement gate: {payload}")
    original_block = body.perceive("blockAt", {"x": 0, "y": 59, "z": 1})
    cleared = body.perceive("blockAt", {"x": 2, "y": 59, "z": 1})
    if original_block.data.get("state") != "SOLID":
        raise AssertionError(
            f"recovery clearance-success mutated the original blocked north cell unexpectedly: {original_block.data} result={payload}"
        )
    if cleared.data.get("state") != "CLEAR":
        raise AssertionError(f"recovery clearance-success did not clear the detour target block: {cleared.data} result={payload}")
    return {
        "reason": result.reason,
        "final": final.pos,
        "statuses": statuses,
        "detour": detour,
        "attempt": attempt,
        "original_block": original_block.data,
        "cleared_block": cleared.data,
        "metrics": payload,
    }


def run_recovery_water_prep_ladder(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill -2 59 -2 8 66 3 air")
    command(rcon, "fill -2 58 -2 8 58 3 stone")
    command(rcon, "setblock 0 59 1 stone")
    command(rcon, "setblock 1 59 1 water")
    command(rcon, "setblock 1 59 2 water")
    command(rcon, f"tp {BOT} 0 59 0 -90 0")
    policy = GovernancePolicy(natural_regions=[Region("nav_recovery_water_prep", (-2, 0, -2), (8, 100, 3))])
    world = GridWorld(
        {
            (0, 59, 0): GridCell(),
            (0, 59, 1): GridCell(),
            (1, 59, 1): GridCell(block_type="water", liquid=True),
            (1, 60, 1): GridCell(),
            (1, 59, 2): GridCell(block_type="water", liquid=True),
            (1, 60, 2): GridCell(),
            (3, 59, 0): GridCell(),
            (4, 59, 0): GridCell(),
        }
    )
    runtime = NavigationTransactions(
        body,
        FakeNavigator(
            [
                fake_segment(
                    "advanced",
                    (0, 59, 1),
                    success=False,
                    reason="partial",
                    path=(PathStep(pos=(0, 59, 1), move=MoveKind.WALK, cost=1.0, reason="walk"),),
                ),
                fake_segment(
                    "arrived",
                    (4, 59, 0),
                    success=True,
                    reason="arrived",
                    path=(
                        PathStep(pos=(3, 59, 0), move=MoveKind.WALK, cost=1.0, reason="walk"),
                        PathStep(pos=(4, 59, 0), move=MoveKind.WALK, cost=1.0, reason="walk"),
                    ),
                ),
            ],
            world=world,
            costs=NavigationCostModel(policy),
        ),
    )

    result = runtime.navigate_to(
        (4, 59, 0),
        config=NavigationRunConfig(
            max_segments=3,
            min_partial_progress=1,
            recovery_attempts=1,
            recovery_detour_distances=(1,),
            recovery_detour_offsets=((1, 0),),
            recovery_detour_y_offsets=(0,),
            recovery_detour_max_attempts=1,
            recovery_min_displacement=0.75,
            recovery_detour_timeout_s=5.0,
        ),
    )
    final = body.get_state()
    payload = result.to_payload()
    if not result.success or result.reason != "arrived":
        raise AssertionError(f"recovery water-prep ladder did not arrive: result={payload} final={final}")
    if distance(final.pos, (4, 59, 0)) > 1.35:
        raise AssertionError(f"recovery water-prep final position too far: final={final.pos} result={payload}")
    segments = (result.metrics or {}).get("segments", [])
    statuses = [segment.get("status") for segment in segments]
    if statuses != ["advanced", "recovery_detour", "arrived"]:
        raise AssertionError(f"recovery water-prep did not expose expected segment sequence: {payload}")
    detour = segments[1]
    if detour.get("terminal_reason") != "arrived" or detour.get("success") is not True:
        raise AssertionError(f"recovery water-prep should mark the detour rung successful: {payload}")
    attempts = detour.get("diagnostics", {}).get("attempts", [])
    if len(attempts) != 1:
        raise AssertionError(f"recovery water-prep should record exactly one detour attempt: {payload}")
    attempt = attempts[0]
    if attempt.get("target") not in ([1, 59, 1], [1, 59, 2]) or attempt.get("target_kind") != "water_prep":
        raise AssertionError(f"recovery water-prep target metadata wrong: {payload}")
    if attempt.get("target_y_offset") != 0:
        raise AssertionError(f"recovery water-prep y-offset metadata wrong: {payload}")
    if attempt.get("path_moves") != ["swim"]:
        raise AssertionError(f"recovery water-prep should expose swim movement facts: {payload}")
    if attempt.get("displaced") is not True:
        raise AssertionError(f"recovery water-prep did not satisfy the displacement gate: {payload}")
    if "clearance" in attempt:
        raise AssertionError(f"recovery water-prep should not attempt block clearance for liquid target: {payload}")
    target = attempt.get("target")
    water = body.perceive("blockAt", {"x": target[0], "y": target[1], "z": target[2]})
    if water.data.get("state") != "LIQUID":
        raise AssertionError(f"recovery water-prep mutated the water target unexpectedly: {water.data} result={payload}")
    return {
        "reason": result.reason,
        "final": final.pos,
        "statuses": statuses,
        "detour": detour,
        "attempt": attempt,
        "water_block": water.data,
        "metrics": payload,
    }


def run_recovery_water_prep_no_displacement(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill -2 59 -2 8 66 3 air")
    command(rcon, "fill -2 58 -2 8 58 3 stone")
    command(rcon, "setblock 0 59 1 stone")
    command(rcon, "setblock 1 59 1 water")
    command(rcon, "setblock 1 59 2 water")
    command(rcon, f"tp {BOT} 0 59 0 -90 0")
    policy = GovernancePolicy(natural_regions=[Region("nav_recovery_water_prep_inverse", (-2, 0, -2), (8, 100, 3))])
    world = GridWorld(
        {
            (0, 59, 0): GridCell(),
            (0, 59, 1): GridCell(),
            (1, 59, 1): GridCell(block_type="water", liquid=True),
            (1, 60, 1): GridCell(),
            (1, 59, 2): GridCell(block_type="water", liquid=True),
            (1, 60, 2): GridCell(),
            (3, 59, 0): GridCell(),
            (4, 59, 0): GridCell(),
        }
    )
    runtime = NavigationTransactions(
        body,
        FakeNavigator(
            [
                fake_segment(
                    "advanced",
                    (0, 59, 1),
                    success=False,
                    reason="partial",
                    path=(PathStep(pos=(0, 59, 1), move=MoveKind.WALK, cost=1.0, reason="walk"),),
                ),
                fake_segment(
                    "arrived",
                    (4, 59, 0),
                    success=True,
                    reason="arrived",
                    path=(
                        PathStep(pos=(3, 59, 0), move=MoveKind.WALK, cost=1.0, reason="walk"),
                        PathStep(pos=(4, 59, 0), move=MoveKind.WALK, cost=1.0, reason="walk"),
                    ),
                ),
            ],
            world=world,
            costs=NavigationCostModel(policy),
        ),
    )

    result = runtime.navigate_to(
        (4, 59, 0),
        config=NavigationRunConfig(
            max_segments=3,
            min_partial_progress=1,
            recovery_attempts=1,
            recovery_detour_distances=(1,),
            recovery_detour_offsets=((1, 0),),
            recovery_detour_y_offsets=(0,),
            recovery_detour_max_attempts=1,
            recovery_min_displacement=2.0,
            recovery_detour_timeout_s=5.0,
        ),
    )
    final = body.get_state()
    payload = result.to_payload()
    if not result.success or result.reason != "arrived":
        raise AssertionError(f"recovery water-prep inverse outer navigation did not arrive: result={payload} final={final}")
    segments = (result.metrics or {}).get("segments", [])
    statuses = [segment.get("status") for segment in segments]
    if statuses != ["advanced", "recovery_detour", "arrived"]:
        raise AssertionError(f"recovery water-prep inverse did not expose expected segment sequence: {payload}")
    detour = segments[1]
    if detour.get("terminal_reason") != "no_displacement" or detour.get("success") is not False:
        raise AssertionError(f"recovery water-prep inverse should keep the detour rung honest: {payload}")
    attempts = detour.get("diagnostics", {}).get("attempts", [])
    if len(attempts) != 1:
        raise AssertionError(f"recovery water-prep inverse should record exactly one detour attempt: {payload}")
    attempt = attempts[0]
    if attempt.get("target_kind") != "water_prep" or attempt.get("target") not in ([1, 59, 1], [1, 59, 2]):
        raise AssertionError(f"recovery water-prep inverse target metadata wrong: {payload}")
    if attempt.get("path_moves") != ["swim"]:
        raise AssertionError(f"recovery water-prep inverse should expose swim movement facts: {payload}")
    if "clearance" in attempt:
        raise AssertionError(f"recovery water-prep inverse should not attempt block clearance for liquid target: {payload}")
    if attempt.get("displaced") is not False or float(attempt.get("displacement", 0.0)) >= 2.0:
        raise AssertionError(f"recovery water-prep inverse did not preserve the displacement gate: {payload}")
    target = attempt.get("target")
    water = body.perceive("blockAt", {"x": target[0], "y": target[1], "z": target[2]})
    if water.data.get("state") != "LIQUID":
        raise AssertionError(f"recovery water-prep inverse mutated the water target unexpectedly: {water.data} result={payload}")
    return {
        "reason": result.reason,
        "final": final.pos,
        "statuses": statuses,
        "detour": detour,
        "attempt": attempt,
        "water_block": water.data,
        "metrics": payload,
    }


def run_unloaded_boundary_partial_honesty(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill -2 59 -2 16 66 2 air")
    command(rcon, "fill -2 58 -2 16 58 2 stone")
    command(rcon, f"tp {BOT} 0 59 0 -90 0")
    policy = GovernancePolicy(natural_regions=[Region("nav_unloaded", (-2, 0, -2), (16, 100, 2))])
    runtime = navigation_runtime(body, flat_world(0, 4, 0, 0), policy)

    result = runtime.navigate_to(
        (14, 59, 0),
        config=NavigationRunConfig(
            max_segments=1,
            segment_timeout_s=12.0,
            min_partial_progress=2,
            unloaded_boundary_limit=40,
            partial_tail_trim=1,
        ),
    )
    final = body.get_state()
    payload = result.to_payload()
    if result.success:
        raise AssertionError(f"unloaded-boundary partial navigation unexpectedly succeeded: {payload} final={final}")
    if result.reason != "segment_budget_exhausted" or not result.can_retry:
        raise AssertionError(f"unloaded-boundary partial returned wrong reason: {payload} final={final}")
    path_update = (result.metrics or {}).get("path_update", {})
    if path_update.get("category") != "unloaded_boundary":
        raise AssertionError(f"unloaded-boundary partial did not preserve path_update category: {payload}")
    if path_update.get("unloaded_boundary_count", 0) < 1:
        raise AssertionError(f"unloaded-boundary partial did not expose boundary count: {payload}")
    segments = (result.metrics or {}).get("segments", [])
    if len(segments) != 1:
        raise AssertionError(f"unloaded-boundary partial should execute one segment: {payload}")
    segment = segments[0]["diagnostics"]["segment"] if segments else {}
    diagnostics = segment.get("plan_diagnostics", {})
    if segment.get("plan_reason") != "partial":
        raise AssertionError(f"unloaded-boundary segment was not a partial plan: {payload}")
    if diagnostics.get("stop_reason") != "unloaded_boundary":
        raise AssertionError(f"unloaded-boundary stop reason missing: {payload}")
    if diagnostics.get("tail_trimmed_steps") != 1:
        raise AssertionError(f"unloaded-boundary tail trim missing: {payload}")
    if diagnostics.get("original_partial_target") != [3, 59, 0]:
        raise AssertionError(f"unloaded-boundary original partial target was unexpected: {payload}")
    if diagnostics.get("partial_target") != [2, 59, 0] or segment.get("target") != [2, 59, 0]:
        raise AssertionError(f"unloaded-boundary did not trim to expected safe partial target: {payload}")
    if distance(final.pos, (2, 59, 0)) > 1.35:
        raise AssertionError(f"unloaded-boundary final position not near partial target: final={final.pos} result={payload}")
    if final.pos[0] > 5.0:
        raise AssertionError(f"unloaded-boundary navigation crossed beyond known grid: final={final.pos} result={payload}")
    return {
        "reason": result.reason,
        "final": final.pos,
        "path_update": path_update,
        "segment": segment,
        "metrics": payload,
    }


def run_multi_segment_world_refresh(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill -2 59 -2 28 66 2 air")
    command(rcon, "fill -2 58 -2 28 58 2 stone")
    command(rcon, f"tp {BOT} 0 59 0 -90 0")
    policy = GovernancePolicy(natural_regions=[Region("nav_refresh", (-2, 0, -2), (28, 100, 2))])
    world = corridor_world(0, 4)
    runtime = navigation_runtime(body, world, policy)
    result = runtime.navigate_to(
        (14, 59, 0),
        config=NavigationRunConfig(
            max_segments=4,
            segment_timeout_s=14.0,
            min_partial_progress=2,
            unloaded_boundary_limit=40,
            partial_tail_trim=1,
            world_update=make_block_at_prism_world_update(
                body,
                lateral_margin=1,
                y_offsets=(-1, 0, 1),
                max_cells=96,
                forward_axis_limit=4,
            ),
        ),
    )
    final = body.get_state()
    payload = result.to_payload()
    if not result.success or result.reason != "arrived":
        raise AssertionError(f"multi-segment world-refresh navigation did not arrive: {payload} final={final}")
    if distance(final.pos, (14, 59, 0)) > 1.35:
        raise AssertionError(f"multi-segment world-refresh final position too far: final={final.pos} result={payload}")
    segments = (result.metrics or {}).get("segments", [])
    if len(segments) < 3:
        raise AssertionError(f"multi-segment world-refresh should execute multiple chained segments: {payload}")
    partial_segments = segments[:-1]
    if any(item["diagnostics"]["segment"].get("plan_reason") != "partial" for item in partial_segments):
        raise AssertionError(f"multi-segment world-refresh did not preserve partial continuation segments: {payload}")
    if partial_segments[0]["diagnostics"]["segment"].get("plan_diagnostics", {}).get("stop_reason") != "unloaded_boundary":
        raise AssertionError(f"multi-segment world-refresh did not start from unloaded-boundary partial: {payload}")
    updates = [item["diagnostics"].get("world_update") for item in partial_segments]
    if any(not update or update.get("source") != "authoritative_block_at_prism_refresh" for update in updates):
        raise AssertionError(f"multi-segment world-refresh did not expose refresh facts on each partial segment: {payload}")
    if any(update.get("complete") is not True for update in updates):
        raise AssertionError(f"multi-segment world-refresh lost completeness truth on a refresh step: {payload}")
    refresh_goals = [update.get("refresh_goal") for update in updates]
    refresh_goal_xs = [
        goal[0] for goal in refresh_goals if isinstance(goal, list) and len(goal) == 3
    ]
    if refresh_goal_xs != sorted(refresh_goal_xs) or refresh_goal_xs[-1] != 14:
        raise AssertionError(f"multi-segment world-refresh bounded refresh goals were not monotonic to goal: {payload}")
    if any(update.get("forward_axis_limit") != 4 for update in updates):
        raise AssertionError(f"multi-segment world-refresh lost forward-axis bound: {payload}")
    if any(update.get("max_cells") != 96 or update.get("max_tiles") != 16 for update in updates):
        raise AssertionError(f"multi-segment world-refresh lost configured paging budget facts: {payload}")
    if any(update.get("tile_width") != 4 or update.get("tile_depth") != 4 for update in updates):
        raise AssertionError(f"multi-segment world-refresh lost tile geometry facts: {payload}")
    if any(update.get("refreshed_cells", 0) > 95 for update in updates):
        raise AssertionError(f"multi-segment world-refresh exceeded bounded refresh window: {payload}")
    if any(update.get("tile_count", 0) < 4 for update in updates):
        raise AssertionError(f"multi-segment world-refresh did not emit tile diagnostics per continuation step: {payload}")
    if any(not isinstance(update.get("elapsed_ms"), (int, float)) or update.get("elapsed_ms", -1) < 0 for update in updates):
        raise AssertionError(f"multi-segment world-refresh lost elapsed timing diagnostics: {payload}")
    if any(
        not all(
            isinstance(tile.get("elapsed_ms"), (int, float))
            and tile.get("elapsed_ms", -1) >= 0
            and isinstance(tile.get("bounds"), dict)
            for tile in (update.get("tiles") or [])
        )
        for update in updates
    ):
        raise AssertionError(f"multi-segment world-refresh lost per-tile timing/bounds diagnostics: {payload}")
    final_segment = segments[-1]["diagnostics"]["segment"]
    if final_segment.get("plan_reason") != "arrived":
        raise AssertionError(f"multi-segment world-refresh final segment did not replan to arrival: {payload}")
    return {
        "reason": result.reason,
        "final": final.pos,
        "segments": [
            {
                "status": item["status"],
                "target": item["target"],
                "terminal_reason": item["terminal_reason"],
                "plan_reason": item["diagnostics"]["segment"]["plan_reason"],
                "world_update": item["diagnostics"].get("world_update"),
            }
            for item in segments
        ],
        "metrics": payload,
    }


def run_sequential_path_quality_adaptation(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    """Prove the adapted sequential segment policy stays forward and bounded."""
    summary = run_multi_segment_world_refresh(rcon, body)
    payload = summary["metrics"]
    metrics = payload["metrics"]
    segments = metrics.get("segments", [])
    if len(segments) < 3:
        raise AssertionError(f"path-quality adaptation should use a multi-segment chain: {payload}")

    targets = [item.get("target") for item in segments]
    target_xs = [target[0] for target in targets if isinstance(target, list) and len(target) == 3]
    if target_xs != sorted(target_xs) or len(set(target_xs)) != len(target_xs):
        raise AssertionError(f"path-quality adaptation segment targets did not move monotonically forward: {payload}")
    if target_xs[-1] != 14 or target_xs[0] <= 0:
        raise AssertionError(f"path-quality adaptation target anchors did not reach the goal from a forward partial: {payload}")

    partials = segments[:-1]
    for index, item in enumerate(partials):
        segment = item["diagnostics"]["segment"]
        diagnostics = segment.get("plan_diagnostics", {})
        update = item["diagnostics"].get("world_update", {})
        if segment.get("plan_reason") != "partial":
            raise AssertionError(f"path-quality adaptation lost partial segment {index}: {payload}")
        if diagnostics.get("stop_reason") != "unloaded_boundary":
            raise AssertionError(f"path-quality adaptation partial {index} lost unloaded-boundary truth: {payload}")
        if diagnostics.get("tail_trimmed_steps") != 1:
            raise AssertionError(f"path-quality adaptation partial {index} did not trim the unreliable tail: {payload}")
        original = diagnostics.get("original_partial_target")
        trimmed = diagnostics.get("partial_target")
        if not (
            isinstance(original, list)
            and isinstance(trimmed, list)
            and len(original) == 3
            and len(trimmed) == 3
            and original[0] > trimmed[0]
            and original[0] - trimmed[0] <= 2
        ):
            raise AssertionError(f"path-quality adaptation partial {index} lost tail-trim provenance: {payload}")
        if update.get("source") != "authoritative_block_at_prism_refresh" or update.get("complete") is not True:
            raise AssertionError(f"path-quality adaptation partial {index} lost authoritative refresh truth: {payload}")
        if update.get("forward_axis_limit") != 4 or update.get("refreshed_cells", 0) > 95:
            raise AssertionError(f"path-quality adaptation partial {index} exceeded bounded refresh policy: {payload}")

    statuses = [item.get("status") for item in segments]
    if any(status == "recovery_detour" for status in statuses):
        raise AssertionError(f"path-quality adaptation used recovery instead of stable sequential continuation: {payload}")
    final_segment = segments[-1]["diagnostics"]["segment"]
    if final_segment.get("plan_reason") != "arrived":
        raise AssertionError(f"path-quality adaptation did not finish with a fresh arrived plan: {payload}")

    return {
        "reason": summary["reason"],
        "final": summary["final"],
        "target_xs": target_xs,
        "tail_trimmed_steps": [
            item["diagnostics"]["segment"].get("plan_diagnostics", {}).get("tail_trimmed_steps")
            for item in partials
        ],
        "refresh_goals": [item["diagnostics"].get("world_update", {}).get("refresh_goal") for item in partials],
        "statuses": statuses,
        "metrics": payload,
    }


def run_recheck_world_change_blocks_dispatch(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill -2 59 -2 6 66 2 air")
    command(rcon, "fill -2 58 -2 6 58 2 stone")
    command(rcon, "setblock 1 60 0 stone")
    command(rcon, f"tp {BOT} 0 59 0 -90 0")
    policy = GovernancePolicy(natural_regions=[Region("nav_recheck", (-2, 0, -2), (6, 100, 2))])
    planned_world = flat_world(0, 4, 0, 0)
    recheck_cells = flat_world(0, 4, 0, 0).cells
    recheck_cells[(1, 60, 0)] = GridCell(block_type="stone", walkable=False)
    runtime = NavigationTransactions(
        body,
        SegmentedNavigator(planned_world, NavigationCostModel(policy)),
    )

    result = runtime.navigate_to(
        (4, 59, 0),
        config=NavigationRunConfig(
            max_segments=1,
            segment_timeout_s=8.0,
            min_partial_progress=1,
            recheck_world=GridWorld(recheck_cells),
        ),
    )
    final = body.get_state()
    payload = result.to_payload()
    if result.success:
        raise AssertionError(f"recheck world-change unexpectedly succeeded: {payload} final={final}")
    if result.reason != "navigation_replan_required:headroom_blocked" or not result.can_retry:
        raise AssertionError(f"recheck world-change returned wrong reason: {payload} final={final}")
    if distance(final.pos, (0, 59, 0)) > 0.9:
        raise AssertionError(f"recheck world-change moved the bot before dispatch denial: final={final.pos} result={payload}")
    segments = (result.metrics or {}).get("segments", [])
    if len(segments) != 1:
        raise AssertionError(f"recheck world-change should report one planned segment: {payload}")
    if segments[0].get("action_id") is not None or segments[0].get("terminal_reason") is not None:
        raise AssertionError(f"recheck world-change dispatched a body action: {payload}")
    segment = segments[0]["diagnostics"]["segment"]
    if segment.get("plan_reason") != "arrived" or segment.get("recheck_reason") != "headroom_blocked":
        raise AssertionError(f"recheck world-change did not expose plan/recheck contrast: {payload}")
    path_update = (result.metrics or {}).get("path_update", {})
    if path_update.get("source") != "recheck" or path_update.get("category") != "goal_changed_or_world_changed":
        raise AssertionError(f"recheck world-change path_update classification wrong: {payload}")
    block = body.perceive("blockAt", {"x": 1, "y": 60, "z": 0})
    if block.data.get("state") != "SOLID":
        raise AssertionError(f"recheck world-change server obstacle missing: block={block.data} result={payload}")
    return {
        "reason": result.reason,
        "final": final.pos,
        "path_update": path_update,
        "segment": segment,
        "server_block": block.data,
        "metrics": payload,
    }


def run_protected_headroom_collision_failure(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill -2 59 -2 5 66 2 air")
    command(rcon, "fill -2 58 -2 5 58 2 stone")
    command(rcon, "setblock 1 60 0 stone")
    command(rcon, f"tp {BOT} 0 59 0 -90 0")
    cells = {
        (0, 59, 0): GridCell(),
        (1, 59, 0): GridCell(headroom_block="stone"),
        (1, 60, 0): GridCell(block_type="stone", walkable=False),
        (2, 59, 0): GridCell(),
    }
    policy = GovernancePolicy(
        natural_regions=[Region("nav_headroom", (-2, 0, -2), (5, 100, 2))],
        protected_regions=[Region("protected_ceiling", (1, 60, 0), (1, 60, 0))],
    )
    runtime = NavigationTransactions(
        body,
        SegmentedNavigator(GridWorld(cells), NavigationCostModel(policy)),
    )

    result = runtime.navigate_to(
        (2, 59, 0),
        break_context=BreakContext.TRAVEL,
        config=NavigationRunConfig(max_segments=1, segment_timeout_s=8.0, min_partial_progress=1),
    )
    final = body.get_state()
    payload = result.to_payload()
    if result.success:
        raise AssertionError(f"protected headroom collision unexpectedly succeeded: {payload} final={final}")
    if result.reason != "navigation_blocked:no_path" or result.can_retry:
        raise AssertionError(f"protected headroom collision returned wrong reason: {payload} final={final}")
    if distance(final.pos, (0, 59, 0)) > 0.9:
        raise AssertionError(f"protected headroom collision moved the bot before dispatch denial: final={final.pos} result={payload}")
    segments = (result.metrics or {}).get("segments", [])
    if len(segments) != 1:
        raise AssertionError(f"protected headroom collision should report one planned segment: {payload}")
    if segments[0].get("action_id") is not None or segments[0].get("terminal_reason") is not None:
        raise AssertionError(f"protected headroom collision dispatched a body action: {payload}")
    path_update = (result.metrics or {}).get("path_update", {})
    blocked_reasons = path_update.get("blocked_reasons", {})
    if blocked_reasons.get("break_denied:protected_region", 0) < 1:
        raise AssertionError(f"protected headroom collision did not expose protected ceiling denial: {payload}")
    if path_update.get("category") != "protected_or_denied":
        raise AssertionError(f"protected headroom collision path_update classification wrong: {payload}")
    segment = segments[0]["diagnostics"]["segment"]
    if segment.get("path_steps") != 0 or segment.get("plan_reason") != "no_path":
        raise AssertionError(f"protected headroom collision should not produce a movement path: {payload}")
    block = body.perceive("blockAt", {"x": 1, "y": 60, "z": 0})
    if block.data.get("type") not in {"stone", "minecraft:stone"} or block.data.get("state") != "SOLID":
        raise AssertionError(f"protected headroom collision mutated protected ceiling: block={block.data} result={payload}")
    return {
        "reason": result.reason,
        "final": final.pos,
        "path_update": path_update,
        "segment": segment,
        "server_block": block.data,
        "metrics": payload,
    }


def run_recheck_unloaded_blocks_dispatch(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill -2 59 -2 6 66 2 air")
    command(rcon, "fill -2 58 -2 6 58 2 stone")
    command(rcon, f"tp {BOT} 0 59 0 -90 0")
    policy = GovernancePolicy(natural_regions=[Region("nav_recheck_unloaded", (-2, 0, -2), (6, 100, 2))])
    planned_world = flat_world(0, 4, 0, 0)
    recheck_cells = flat_world(0, 4, 0, 0).cells
    del recheck_cells[(1, 59, 0)]
    runtime = NavigationTransactions(
        body,
        SegmentedNavigator(planned_world, NavigationCostModel(policy)),
    )

    result = runtime.navigate_to(
        (4, 59, 0),
        config=NavigationRunConfig(
            max_segments=1,
            segment_timeout_s=8.0,
            min_partial_progress=1,
            recheck_world=GridWorld(recheck_cells),
        ),
    )
    final = body.get_state()
    payload = result.to_payload()
    if result.success:
        raise AssertionError(f"recheck unloaded unexpectedly succeeded: {payload} final={final}")
    if result.reason != "navigation_replan_required:unloaded" or not result.can_retry:
        raise AssertionError(f"recheck unloaded returned wrong reason: {payload} final={final}")
    if distance(final.pos, (0, 59, 0)) > 0.9:
        raise AssertionError(f"recheck unloaded moved the bot before dispatch denial: final={final.pos} result={payload}")
    segments = (result.metrics or {}).get("segments", [])
    if len(segments) != 1:
        raise AssertionError(f"recheck unloaded should report one planned segment: {payload}")
    if segments[0].get("action_id") is not None or segments[0].get("terminal_reason") is not None:
        raise AssertionError(f"recheck unloaded dispatched a body action: {payload}")
    segment = segments[0]["diagnostics"]["segment"]
    if segment.get("plan_reason") != "arrived" or segment.get("recheck_reason") != "unloaded":
        raise AssertionError(f"recheck unloaded did not expose plan/recheck contrast: {payload}")
    path_update = (result.metrics or {}).get("path_update", {})
    if path_update.get("recheck_checked") != 1:
        raise AssertionError(f"recheck unloaded did not fail on the first lookahead step: {payload}")
    if path_update.get("source") != "recheck" or path_update.get("category") != "goal_changed_or_world_changed":
        raise AssertionError(f"recheck unloaded path_update classification wrong: {payload}")
    block = body.perceive("blockAt", {"x": 1, "y": 59, "z": 0})
    if block.data.get("state") != "CLEAR":
        raise AssertionError(f"recheck unloaded server corridor should remain clear: block={block.data} result={payload}")
    return {
        "reason": result.reason,
        "final": final.pos,
        "path_update": path_update,
        "segment": segment,
        "server_block": block.data,
        "metrics": payload,
    }


def run_recheck_protected_break_blocks_dispatch(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill -2 59 -2 5 66 2 air")
    command(rcon, "fill -2 58 -2 5 58 2 stone")
    command(rcon, "setblock 1 59 0 stone")
    command(rcon, f"tp {BOT} 0 59 0 -90 0")
    command(rcon, f"item replace entity {BOT} weapon.mainhand with diamond_pickaxe")
    cells = {
        (0, 59, 0): GridCell(),
        (1, 59, 0): GridCell(block_type="stone", walkable=False),
        (2, 59, 0): GridCell(),
    }
    planned_policy = GovernancePolicy(natural_regions=[Region("nav_recheck_break", (-2, 0, -2), (5, 100, 2))])
    protected_policy = GovernancePolicy(
        natural_regions=[Region("nav_recheck_break", (-2, 0, -2), (5, 100, 2))],
        protected_regions=[Region("new_build", (1, 0, 0), (1, 100, 0))],
    )
    runtime = NavigationTransactions(
        body,
        SegmentedNavigator(GridWorld(cells), NavigationCostModel(planned_policy)),
    )

    result = runtime.navigate_to(
        (2, 59, 0),
        break_context=BreakContext.TRAVEL,
        config=NavigationRunConfig(
            max_segments=1,
            segment_timeout_s=8.0,
            min_partial_progress=1,
            recheck_costs=NavigationCostModel(protected_policy),
        ),
    )
    final = body.get_state()
    payload = result.to_payload()
    if result.success:
        raise AssertionError(f"recheck protected-break unexpectedly succeeded: {payload} final={final}")
    if result.reason != "navigation_replan_required:break_denied:protected_region" or not result.can_retry:
        raise AssertionError(f"recheck protected-break returned wrong reason: {payload} final={final}")
    if distance(final.pos, (0, 59, 0)) > 0.9:
        raise AssertionError(f"recheck protected-break moved the bot before dispatch denial: final={final.pos} result={payload}")
    segments = (result.metrics or {}).get("segments", [])
    if len(segments) != 1:
        raise AssertionError(f"recheck protected-break should report one planned segment: {payload}")
    if segments[0].get("action_id") is not None or segments[0].get("terminal_reason") is not None:
        raise AssertionError(f"recheck protected-break dispatched a body action: {payload}")
    segment = segments[0]["diagnostics"]["segment"]
    if segment.get("plan_reason") != "arrived" or segment.get("recheck_reason") != "break_denied:protected_region":
        raise AssertionError(f"recheck protected-break did not expose plan/recheck contrast: {payload}")
    if segment.get("path_moves")[:2] != ["break", "walk"]:
        raise AssertionError(f"recheck protected-break did not plan through the break candidate: {payload}")
    path_update = (result.metrics or {}).get("path_update", {})
    if path_update.get("source") != "recheck" or path_update.get("category") != "goal_changed_or_world_changed":
        raise AssertionError(f"recheck protected-break path_update classification wrong: {payload}")
    block = body.perceive("blockAt", {"x": 1, "y": 59, "z": 0})
    if block.data.get("type") not in {"stone", "minecraft:stone"} or block.data.get("state") != "SOLID":
        raise AssertionError(f"recheck protected-break mutated protected obstacle: block={block.data} result={payload}")
    return {
        "reason": result.reason,
        "final": final.pos,
        "path_update": path_update,
        "segment": segment,
        "server_block": block.data,
        "metrics": payload,
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
        command(rcon, "script in minebot run minebot_reset()")
        cases = {
            "happy": lambda: run_typed_goal_happy_path(rcon, body),
            "diagonal": lambda: run_diagonal_happy_path(rcon, body),
            "diagonal_protected_corner": lambda: run_diagonal_protected_corner_failure(rcon, body),
            "diagonal_corner_headroom": lambda: run_recheck_diagonal_corner_headroom_blocks_dispatch(rcon, body),
            "break_wall": lambda: run_break_wall_happy_path(rcon, body),
            "open_gate_path": lambda: run_open_gate_path_happy_path(rcon, body),
            "single_sand_break": lambda: run_single_sand_break_happy_path(rcon, body),
            "single_gravel_break": lambda: run_single_gravel_break_happy_path(rcon, body),
            "gravity_stack_break": lambda: run_gravity_stack_break_failure(rcon, body),
            "gravity_liquid_break": lambda: run_gravity_liquid_adjacent_break_failure(rcon, body),
            "recheck_gravity_vertical_liquid_break": lambda: run_recheck_gravity_vertical_liquid_break_blocks_dispatch(rcon, body),
            "recheck_gravel_stack_break": lambda: run_recheck_gravel_stack_break_blocks_dispatch(rcon, body),
            "headroom_break": lambda: run_headroom_break_happy_path(rcon, body),
            "place_support": lambda: run_place_support_happy_path(rcon, body),
            "recheck_support_missing": lambda: run_recheck_support_missing_blocks_dispatch(rcon, body),
            "vertical_ascend": lambda: run_vertical_ascend_happy_path(rcon, body),
            "step_surfaces": lambda: run_step_surface_happy_paths(rcon, body),
            "swim": lambda: run_swim_happy_path(rcon, body),
            "descend": lambda: run_descend_happy_path(rcon, body),
            "safe_fall": lambda: run_safe_fall_happy_path(rcon, body),
            "recheck_fall_unsafe": lambda: run_recheck_fall_becomes_unsafe_blocks_dispatch(rcon, body),
            "fall_then_walk": lambda: run_fall_then_walk_happy_path(rcon, body),
            "pillar": lambda: run_pillar_happy_path(rcon, body),
            "pillar_no_scaffold": lambda: run_pillar_no_scaffold_failure(rcon, body),
            "downward": lambda: run_downward_happy_path(rcon, body),
            "downward_protected_floor": lambda: run_downward_protected_floor_failure(rcon, body),
            "reflex_preempt": lambda: run_survival_reflex_preempts_navigation(rcon, body),
            "recovery_ladder": lambda: run_recovery_detour_distance_ladder(rcon, body),
            "recovery_support_step": lambda: run_recovery_support_step_ladder(rcon, body),
            "recovery_support_step_down": lambda: run_recovery_support_step_down_ladder(rcon, body),
            "recovery_clearance": lambda: run_recovery_clearance_ladder(rcon, body),
            "recovery_clearance_success": lambda: run_recovery_clearance_success_ladder(rcon, body),
            "recovery_gravel_clearance": lambda: run_recovery_gravel_clearance_ladder(rcon, body),
            "recovery_exhausted": lambda: run_recovery_exhausted_honesty(rcon, body),
            "recovery_water_prep": lambda: run_recovery_water_prep_ladder(rcon, body),
            "recovery_water_prep_inverse": lambda: run_recovery_water_prep_no_displacement(rcon, body),
            "unloaded_boundary": lambda: run_unloaded_boundary_partial_honesty(rcon, body),
            "multi_segment_refresh": lambda: run_multi_segment_world_refresh(rcon, body),
            "sequential_path_quality": lambda: run_sequential_path_quality_adaptation(rcon, body),
            "recheck_world_change": lambda: run_recheck_world_change_blocks_dispatch(rcon, body),
            "protected_headroom": lambda: run_protected_headroom_collision_failure(rcon, body),
            "recheck_unloaded": lambda: run_recheck_unloaded_blocks_dispatch(rcon, body),
            "recheck_protected_break": lambda: run_recheck_protected_break_blocks_dispatch(rcon, body),
            "unsafe_fall": lambda: run_unsafe_fall_failure(rcon, body),
            "failure": lambda: run_protected_wall_failure(rcon, body),
        }
        selected_raw = os.environ.get("MINEBOT_NAV_CASES")
        selected = [name.strip() for name in selected_raw.split(",") if name.strip()] if selected_raw else list(cases.keys())
        unknown = [name for name in selected if name not in cases]
        if unknown:
            raise AssertionError(f"unknown navigation e2e cases: {unknown}")
        results = {name: cases[name]() for name in selected}
        print(results)


if __name__ == "__main__":
    main()
