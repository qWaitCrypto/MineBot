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
from minebot.app.runner import AgentRuntime, sdk_tool_for  # noqa: E402
from minebot.brain.context import AgentContext  # noqa: E402
from minebot.brain.lifecycle import LifecycleController, LifecycleState  # noqa: E402
from minebot.brain.modes import AgentSignal, ModeRuntime  # noqa: E402
from minebot.brain.progress import ProgressAuthority  # noqa: E402
from minebot.brain.registry import RegisteredTool, ToolRegistry, ToolSidecar, WeldContext, execute_tool  # noqa: E402
from minebot.contract import BreakContext, ToolResult  # noqa: E402
from minebot.game import GovernancePolicy, RconClient, Region, ScarpetBody  # noqa: E402
from minebot.game.errors import RconError  # noqa: E402
from minebot.game.rcon import RconConfig  # noqa: E402
from tests.e2e_support import SKIP_EXIT_CODE, spawn_or_fail  # noqa: E402


BOT = "E2EAgentCollect"
REGION = Region("agent-collect", (-10, 0, -10), (16, 100, 10))
STALE_TEST_PLAYERS = (
    "E2EAgentBot",
    "E2EAgentCollect",
    "E2EAgentRealModel",
    "E2EAgentToolBot",
    "E2EOakPickupProbe",
    "E2EPickupProbe",
    "TestBot",
    "NavProbe",
    "NavAlphaProbe",
    "NavRefreshProbe",
    "AlphaProbeBot",
    "CollectProbe",
    "DigThroughProbe",
    "StandBatchProbe",
)


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
        "fill -10 70 -10 16 78 10 air",
        "fill -10 69 -10 16 69 10 stone",
    ]:
        command(rcon, cmd)
    cleanup_stale_test_players(rcon)


def cleanup_stale_test_players(rcon: RconClient) -> None:
    for name in STALE_TEST_PLAYERS:
        command(rcon, f"player {name} kill", delay=0.0)
    time.sleep(0.2)


def reset_subject(rcon: RconClient, *, item: str = "dirt", blocks: list[tuple[int, int, int, str]] | None = None) -> None:
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
    if blocks is None:
        blocks = [(x, 70, 0, item) for x in range(3, 9)]
    for x, y, z, block_type in blocks:
        command(rcon, f"setblock {x} {y} {z} {block_type}", delay=0.0)


def make_registry(
    body: ScarpetBody,
    *,
    protected: bool = False,
    weld_context: WeldContext | None = None,
    mode_runtime: ModeRuntime | None = None,
) -> tuple[ToolRegistry, CompositionContext]:
    policy = GovernancePolicy(
        natural_regions=[REGION],
        protected_regions=[Region("protected-target", (3, 70, 0), (8, 70, 0))] if protected else [],
    )
    navigator = NavigationTransactions.server_side(body, policy)
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
                pickup_timeout_s=2.0,
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
    weld_context = weld_context or WeldContext(body=body, authority=ProgressAuthority(), goal_text="collect_resource dirt 3")
    mode_runtime = mode_runtime or ModeRuntime()
    context = CompositionContext(
        registry=registry,
        weld_context=weld_context,
        runtime_profile=mode_runtime.profile_for(LifecycleState.ACTIVE),
        budget=CompositionBudget(max_candidates=6, max_mutating_calls=6, max_wall_s=45.0),
    )
    register_collect_resource_tool(registry, context)
    return registry, context


def make_runtime(body: ScarpetBody) -> tuple[AgentRuntime, ToolRegistry, CompositionContext]:
    async def runner(agent, input_text, *, context=None, **kwargs):
        tool = next(tool for tool in agent.tools if tool.name == "collect_resource")

        class Wrapper:
            def __init__(self, context):
                self.context = context

        import json

        return await tool.on_invoke_tool(
            Wrapper(context),
            json.dumps({"item": "dirt", "count": 3, "constraints": {"radius": 12, "max_candidates": 6}}),
        )

    mode_runtime = ModeRuntime()
    lifecycle = LifecycleController()
    authority = ProgressAuthority()
    placeholder_registry = ToolRegistry()
    runtime = AgentRuntime(
        body=body,
        registry=placeholder_registry,
        agent_context=AgentContext(
            system_prompt="You are MineBot. Continue collection from fresh facts after interruptions.",
            goal_text="collect_resource dirt 3",
        ),
        lifecycle=lifecycle,
        mode_runtime=mode_runtime,
        authority=authority,
        runner_run=runner,
        max_turns=2,
    )
    registry, ctx = make_registry(
        body,
        weld_context=runtime.weld_context,
        mode_runtime=mode_runtime,
    )
    runtime.registry = registry
    runtime.agent = runtime.agent.clone(tools=[sdk_tool_for(registry.get(name)) for name in registry.names()])
    return runtime, registry, ctx


def run_happy(
    rcon: RconClient,
    body: ScarpetBody,
    *,
    item: str,
    blocks: list[tuple[int, int, int, str]] | None = None,
    count: int = 3,
    tool: str | None = None,
) -> dict[str, object]:
    reset_subject(rcon, item=item, blocks=blocks)
    if tool is not None:
        command(rcon, f"item replace entity {BOT} weapon.mainhand with {tool}")
        command(rcon, "script in minebot run minebot_reset()")
    registry, ctx = make_registry(body)
    result = execute_tool(
        registry.get("collect_resource"),
        {"item": item, "count": count, "constraints": {"radius": 12, "max_candidates": 6}},
        ctx.weld_context,
    )
    if not result.get("success") or result.get("reason") != "collected":
        raise AssertionError(f"collect {item} happy failed: {result}")
    metrics = result["metrics"]
    if metrics["after_count"] < count or metrics["candidates_tried"] < count:
        raise AssertionError(f"collect {item} did not prove inventory/candidate truth: {result}")
    if ctx.weld_context.authority.last_action is None or ctx.weld_context.authority.last_action[0] != "mine_block_collect":
        raise AssertionError(f"collect {item} did not route leaf mutation through progress weld: {result}")
    return {
        "item": item,
        "inventory_item": metrics["item"],
        "block_types": metrics["block_types"],
        "expected_drops": metrics["expected_drops"],
        "reason": result["reason"],
        "after_count": metrics["after_count"],
        "candidates_tried": metrics["candidates_tried"],
        "last_action": ctx.weld_context.authority.last_action,
    }


def run_resource_ladder(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    ladder = {
        "logs": run_happy(
            rcon,
            body,
            item="logs",
            blocks=[(3, 70, 1, "oak_log"), (4, 70, 1, "oak_log"), (5, 70, 1, "oak_log")],
            count=2,
        ),
        "coal": run_happy(
            rcon,
            body,
            item="coal",
            blocks=[(3, 70, 2, "coal_ore"), (4, 70, 2, "coal_ore"), (5, 70, 2, "coal_ore")],
            count=2,
            tool="iron_pickaxe",
        ),
        "iron": run_happy(
            rcon,
            body,
            item="iron",
            blocks=[(3, 70, 3, "iron_ore"), (4, 70, 3, "iron_ore"), (5, 70, 3, "iron_ore")],
            count=2,
            tool="iron_pickaxe",
        ),
        "diamond": run_happy(
            rcon,
            body,
            item="diamond",
            blocks=[(3, 70, 4, "diamond_ore"), (4, 70, 4, "diamond_ore"), (5, 70, 4, "diamond_ore")],
            count=2,
            tool="diamond_pickaxe",
        ),
    }
    missing_rare = run_not_found(
        rcon,
        body,
        item="diamond",
        target_absent_block="diamond_ore",
        tool="iron_pickaxe",
    )
    protected_log = run_illegal(
        rcon,
        body,
        item="logs",
        blocks=[(3, 70, 0, "oak_log"), (4, 70, 0, "oak_log")],
    )
    return {"happy": ladder, "missing_rare": missing_rare, "protected_log": protected_log}


def inventory_count(body: ScarpetBody, item: str) -> int:
    wanted = item.removeprefix("minecraft:")
    count = 0
    for slot in body.get_inventory():
        if slot.item is not None and slot.item.removeprefix("minecraft:") == wanted:
            count += slot.count
    return count


def run_interrupt_resume(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_subject(rcon, item="dirt")
    registry, ctx = make_registry(body)
    partial = execute_tool(
        registry.get("collect_resource"),
        {"item": "dirt", "count": 3, "constraints": {"radius": 12, "max_candidates": 1, "max_mutating_calls": 1}},
        ctx.weld_context,
    )
    if partial.get("success") is not True or partial.get("reason") != "partial_budget_exhausted":
        raise AssertionError(f"expected bounded partial before interruption: {partial}")
    if partial["metrics"]["after_count"] != 1:
        raise AssertionError(f"partial collection did not leave one authoritative dirt: {partial}")

    runtime, _runtime_registry, _runtime_ctx = make_runtime(body)

    async def run_sequence():
        first = await runtime.run_turn(extra_signals=[AgentSignal.death_detected("death", composition_id="collect_resource")])
        second = await runtime.run_turn(extra_signals=[AgentSignal.recovery_completed("respawned")])
        third = await runtime.run_turn()
        return first, second, third

    import asyncio

    first, second, third = asyncio.run(run_sequence())
    final_count = inventory_count(body, "dirt")
    trace = runtime.trace.snapshot()
    if first.lifecycle is not LifecycleState.RECOVERING or second.lifecycle is not LifecycleState.RESUMING:
        raise AssertionError(f"death/recovery lifecycle did not suspend/resume: first={first} second={second}")
    if third.status != "completed_turn" or runtime.lifecycle.state is not LifecycleState.ACTIVE:
        raise AssertionError(f"resume did not re-enter active runtime: third={third} state={runtime.lifecycle.state}")
    if final_count < 3:
        raise AssertionError(f"resume did not continue from fresh inventory count: final_count={final_count}")
    if not any(event.get("event") == "resume_context" and event.get("reason") == "death" for event in trace):
        raise AssertionError(f"resume trace missing death suspend facts: {trace}")
    return {
        "partial_after_count": partial["metrics"]["after_count"],
        "final_count": final_count,
        "lifecycle_history": [state.value for state in runtime.lifecycle.history],
        "resume_events": [event for event in trace if event.get("event") == "resume_context"],
    }


def run_not_found(
    rcon: RconClient,
    body: ScarpetBody,
    *,
    item: str = "gravel",
    target_absent_block: str = "gravel",
    tool: str | None = None,
) -> dict[str, object]:
    reset_subject(rcon, item="dirt")
    if item != target_absent_block:
        command(rcon, f"clear {BOT} {item}")
    command(rcon, "fill -10 70 -10 16 78 10 air")
    command(rcon, "fill -10 69 -10 16 69 10 stone")
    if tool is not None:
        command(rcon, f"item replace entity {BOT} weapon.mainhand with {tool}")
        command(rcon, "script in minebot run minebot_reset()")
    registry, ctx = make_registry(body)
    result = execute_tool(
        registry.get("collect_resource"),
        {"item": item, "count": 1, "constraints": {"radius": 6, "max_candidates": 2}},
        ctx.weld_context,
    )
    if result.get("success") or result.get("reason") != "target_not_found" or not result.get("canRetry"):
        raise AssertionError(f"not-found inverse returned wrong truth: {result}")
    if result["metrics"]["after_count"] != 0:
        raise AssertionError(f"not-found inverse invented inventory progress: {result}")
    return {
        "item": item,
        "reason": result["reason"],
        "can_retry": result["canRetry"],
        "after_count": result["metrics"]["after_count"],
        "block_types": result["metrics"].get("block_types"),
    }


def run_illegal(
    rcon: RconClient,
    body: ScarpetBody,
    *,
    item: str = "dirt",
    blocks: list[tuple[int, int, int, str]] | None = None,
) -> dict[str, object]:
    reset_subject(rcon, item=item, blocks=blocks)
    registry, ctx = make_registry(body, protected=True)
    result = execute_tool(
        registry.get("collect_resource"),
        {"item": item, "count": 1, "constraints": {"radius": 12, "max_candidates": 2}},
        ctx.weld_context,
    )
    # A protected candidate is now a skip; with no legal candidate the collect
    # honestly exhausts. The red-line guarantees are unchanged: no success, no
    # mutation counted, and the break_denied surfaced in skipped (no greenwashing).
    if result.get("success") or result.get("reason") != "candidate_targets_exhausted" or not result.get("canRetry"):
        raise AssertionError(f"illegal inverse returned wrong truth: {result}")
    if result["metrics"]["after_count"] != 0:
        raise AssertionError(f"illegal inverse counted protected mutation: {result}")
    if not any(str(entry.get("reason", "")).startswith("break_denied") for entry in result["metrics"]["skipped"]):
        raise AssertionError(f"illegal inverse must surface break_denied in skipped: {result}")
    return {
        "item": item,
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
        resumed = run_interrupt_resume(rcon, body)
        ladder = run_resource_ladder(rcon, body)
        missing = run_not_found(rcon, body)
        illegal = run_illegal(rcon, body)
        print(
            {
                "dirt": dirt,
                "sand": sand,
                "gravel": gravel,
                "resumed": resumed,
                "ladder": ladder,
                "missing": missing,
                "illegal": illegal,
            }
        )


if __name__ == "__main__":
    main()
