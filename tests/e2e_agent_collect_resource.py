#!/usr/bin/env python3
"""Live proof for Agent Phase-1 collect_resource composition."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.body import BlockWork, NavigationTransactions  # noqa: E402
from minebot.brain.composition import (  # noqa: E402
    CompositionBudget,
    CompositionContext,
    register_collect_resource_tool,
    register_inventory_tools,
)
from minebot.brain.lifecycle import LifecycleState  # noqa: E402
from minebot.brain.modes import ModeRuntime  # noqa: E402
from minebot.brain.progress import ProgressAuthority  # noqa: E402
from minebot.brain.registry import RegisteredTool, ToolRegistry, ToolSidecar, WeldContext, execute_tool  # noqa: E402
from minebot.contract import BreakContext, ToolResult  # noqa: E402
from minebot.game import GovernancePolicy, GridCell, GridWorld, NavigationCostModel, RconClient, Region, ScarpetBody  # noqa: E402
from minebot.game.errors import RconError  # noqa: E402
from minebot.game.navigation import SegmentedNavigator  # noqa: E402
from minebot.game.rcon import RconConfig  # noqa: E402
from tests.e2e_support import SKIP_EXIT_CODE, spawn_or_fail  # noqa: E402


BOT = "E2EAgentCollect"
REGION = Region("agent-collect", (-10, 0, -10), (16, 100, 10))


def command(rcon: RconClient, command_text: str, delay: float = 0.05) -> str:
    out = rcon.command(command_text)
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
        "fill -10 70 -10 16 78 10 air",
        "fill -10 69 -10 16 69 10 stone",
    ]:
        command(rcon, cmd)


def reset_subject(rcon: RconClient, *, item: str = "dirt") -> None:
    for cmd in [
        "script in minebot run minebot_reset()",
        "kill @e[type=!player]",
        "fill -10 70 -10 16 78 10 air",
        "fill -10 69 -10 16 69 10 stone",
        f"clear {BOT}",
        f"tp {BOT} 0 70 0 -90 0",
        f"gamemode survival {BOT}",
        f"effect clear {BOT}",
    ]:
        command(rcon, cmd)
    for x in range(3, 9):
        command(rcon, f"setblock {x} 70 0 {item}", delay=0.0)


def flat_world() -> GridWorld:
    return GridWorld({(x, 70, z): GridCell() for x in range(-10, 17) for z in range(-10, 11)})


def make_registry(body: ScarpetBody, *, protected: bool = False) -> tuple[ToolRegistry, CompositionContext]:
    policy = GovernancePolicy(
        natural_regions=[REGION],
        protected_regions=[Region("protected-target", (3, 70, 0), (8, 70, 0))] if protected else [],
    )
    navigator = NavigationTransactions(body, SegmentedNavigator(flat_world(), NavigationCostModel(policy)))
    work = BlockWork(body, policy, navigator=navigator)
    registry = ToolRegistry()
    register_inventory_tools(registry, body)
    registry.register(
        RegisteredTool(
            "search_for_block",
            "Search for a nearby resource block.",
            {"type": "object"},
            lambda params: work.search_for_block(
                block_types=tuple(str(item) for item in params.get("block_types", [])),
                search_radius=int(params.get("search_radius") or 16),
                find_limit=int(params.get("find_limit") or 8),
                timeout_s=12.0,
            ),
            ToolSidecar("search_for_block", mutating=False, permission="read_world", body_scope=("blocks",)),
        )
    )
    registry.register(
        RegisteredTool(
            "mine_block_collect",
            "Mine one target block and verify pickup.",
            {"type": "object"},
            lambda params: work.mine_block_collect(
                tuple(int(v) for v in params["pos"]),
                context=BreakContext.COLLECT,
                expected_drops=tuple(str(item) for item in params.get("expected_drops", [])),
                dry=bool(params.get("dry", False)),
                settle_s=0.1,
                pickup_timeout_s=1.0,
                timeout_s=10.0,
            ),
            ToolSidecar(
                "mine_block_collect",
                mutating=True,
                permission="break_collect",
                body_scope=("mine",),
                terminal_truth=("mineDone", "inventory"),
            ),
        )
    )
    context = CompositionContext(
        registry=registry,
        weld_context=WeldContext(body=body, authority=ProgressAuthority(), goal_text="collect_resource dirt 3"),
        runtime_profile=ModeRuntime().profile_for(LifecycleState.ACTIVE),
        budget=CompositionBudget(max_candidates=6, max_mutating_calls=6, max_wall_s=45.0),
    )
    register_collect_resource_tool(registry, context)
    return registry, context


def run_happy(rcon: RconClient, body: ScarpetBody, *, item: str) -> dict[str, object]:
    reset_subject(rcon, item=item)
    registry, ctx = make_registry(body)
    result = execute_tool(
        registry.get("collect_resource"),
        {"item": item, "count": 3, "constraints": {"radius": 12, "max_candidates": 6}},
        ctx.weld_context,
    )
    if not result.get("success") or result.get("reason") != "collected":
        raise AssertionError(f"collect {item} happy failed: {result}")
    metrics = result["metrics"]
    if metrics["after_count"] < 3 or metrics["candidates_tried"] < 3:
        raise AssertionError(f"collect {item} did not prove inventory/candidate truth: {result}")
    if ctx.weld_context.authority.last_action is None or ctx.weld_context.authority.last_action[0] != "mine_block_collect":
        raise AssertionError(f"collect {item} did not route leaf mutation through progress weld: {result}")
    return {
        "item": item,
        "reason": result["reason"],
        "after_count": metrics["after_count"],
        "candidates_tried": metrics["candidates_tried"],
        "last_action": ctx.weld_context.authority.last_action,
    }


def run_not_found(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_subject(rcon, item="dirt")
    command(rcon, "fill -10 70 -10 16 78 10 air")
    command(rcon, "fill -10 69 -10 16 69 10 stone")
    registry, ctx = make_registry(body)
    result = execute_tool(
        registry.get("collect_resource"),
        {"item": "gravel", "count": 1, "constraints": {"radius": 6, "max_candidates": 2}},
        ctx.weld_context,
    )
    if result.get("success") or result.get("reason") != "target_not_found" or not result.get("canRetry"):
        raise AssertionError(f"not-found inverse returned wrong truth: {result}")
    if result["metrics"]["after_count"] != 0:
        raise AssertionError(f"not-found inverse invented inventory progress: {result}")
    return {"reason": result["reason"], "can_retry": result["canRetry"], "after_count": result["metrics"]["after_count"]}


def run_illegal(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_subject(rcon, item="dirt")
    registry, ctx = make_registry(body, protected=True)
    result = execute_tool(
        registry.get("collect_resource"),
        {"item": "dirt", "count": 1, "constraints": {"radius": 12, "max_candidates": 2}},
        ctx.weld_context,
    )
    if result.get("success") or result.get("reason") != "protected_or_illegal_target" or not result.get("canRetry"):
        raise AssertionError(f"illegal inverse returned wrong truth: {result}")
    if result["metrics"]["after_count"] != 0:
        raise AssertionError(f"illegal inverse counted protected mutation: {result}")
    return {
        "reason": result["reason"],
        "skipped": result["metrics"]["skipped"],
        "last_failure": result["metrics"].get("last_failure"),
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
        spawn_or_fail(body, (0, 70, 0))
        dirt = run_happy(rcon, body, item="dirt")
        sand = run_happy(rcon, body, item="sand")
        gravel = run_happy(rcon, body, item="gravel")
        missing = run_not_found(rcon, body)
        illegal = run_illegal(rcon, body)
        print({"dirt": dirt, "sand": sand, "gravel": gravel, "missing": missing, "illegal": illegal})


if __name__ == "__main__":
    main()
