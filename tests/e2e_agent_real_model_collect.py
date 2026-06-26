#!/usr/bin/env python3
"""Real-model e2e for Agent Phase-1 collect_resource.

This is the long-test entry that replaces deterministic smoke tests once an API
key is available. It runs openai-agents Runner.run with a real ModelProvider and
expects the model to call the registered collect_resource tool.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.app.config import AppConfigError, provider_registry_from_env  # noqa: E402
from minebot.app.wiring import build_agent_runtime  # noqa: E402
from minebot.body import BlockWork, NavigationTransactions  # noqa: E402
from minebot.brain.composition import (  # noqa: E402
    CompositionBudget,
    CompositionContext,
    register_collect_resource_tool,
    register_inventory_tools,
)
from minebot.brain.progress import ProgressAuthority  # noqa: E402
from minebot.brain.registry import RegisteredTool, ToolRegistry, ToolSidecar, WeldContext  # noqa: E402
from minebot.contract import BreakContext  # noqa: E402
from minebot.game import GovernancePolicy, GridCell, GridWorld, NavigationCostModel, RconClient, Region, ScarpetBody  # noqa: E402
from minebot.game.errors import RconError  # noqa: E402
from minebot.game.navigation import SegmentedNavigator  # noqa: E402
from minebot.game.rcon import RconConfig  # noqa: E402
from tests.e2e_support import SKIP_EXIT_CODE, spawn_or_fail  # noqa: E402


BOT = "E2EAgentRealModel"
REGION = Region("agent-real-model", (-10, 0, -10), (16, 100, 10))


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


def reset_subject(rcon: RconClient, *, item: str = "dirt", count: int = 4) -> None:
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
    for offset in range(count):
        command(rcon, f"setblock {3 + offset} 70 0 {item}", delay=0.0)


def flat_world() -> GridWorld:
    return GridWorld({(x, 70, z): GridCell() for x in range(-10, 17) for z in range(-10, 11)})


def make_registry(body: ScarpetBody) -> ToolRegistry:
    policy = GovernancePolicy(natural_regions=[REGION])
    navigator = NavigationTransactions(body, SegmentedNavigator(flat_world(), NavigationCostModel(policy)))
    work = BlockWork(body, policy, navigator=navigator)
    registry = ToolRegistry()
    register_inventory_tools(registry, body)
    registry.register(
        RegisteredTool(
            "search_for_block",
            "Search for nearby natural resource blocks.",
            {
                "type": "object",
                "properties": {
                    "block_types": {"type": "array", "items": {"type": "string"}},
                    "search_radius": {"type": "integer"},
                    "find_limit": {"type": "integer"},
                },
                "required": ["block_types"],
                "additionalProperties": True,
            },
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
            "Mine one target block and verify pickup by authoritative inventory delta.",
            {
                "type": "object",
                "properties": {
                    "pos": {"type": "array", "items": {"type": "integer"}, "minItems": 3, "maxItems": 3},
                    "expected_drops": {"type": "array", "items": {"type": "string"}},
                    "dry": {"type": "boolean"},
                },
                "required": ["pos"],
                "additionalProperties": True,
            },
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
                timeout_s=12.0,
            ),
        )
    )
    return registry


def inventory_count(body: ScarpetBody, item: str) -> int:
    wanted = item.removeprefix("minecraft:")
    total = 0
    for slot in body.get_inventory():
        if slot.item is not None and slot.item.removeprefix("minecraft:") == wanted:
            total += slot.count
    return total


async def run_real_model_collect(body: ScarpetBody) -> dict[str, object]:
    model_provider = provider_registry_from_env()
    registry = make_registry(body)
    parts = build_agent_runtime(
        body=body,
        registry=registry,
        goal_text="Collect exactly 3 dirt blocks using the collect_resource tool. Stop after the tool reports success.",
        model_provider=model_provider,
        agent_name="MineBotRealModelE2E",
    )
    context = CompositionContext(
        registry=registry,
        weld_context=parts.runtime.weld_context,
        runtime_profile=parts.modes.profile_for(parts.lifecycle.state),
        budget=CompositionBudget(max_candidates=6, max_mutating_calls=6, max_wall_s=60.0),
    )
    register_collect_resource_tool(registry, context)
    parts.runtime.registry = registry
    from minebot.app.runner import sdk_tool_for

    parts.runtime.agent = parts.runtime.agent.clone(tools=[sdk_tool_for(registry.get(name)) for name in registry.names()])
    outcome = await parts.runtime.run_turn()
    after = inventory_count(body, "dirt")
    trace = parts.runtime.trace.snapshot()
    collect_results = [
        event for event in trace
        if event.get("event") == "tool_result" and event.get("tool") == "collect_resource"
    ]
    if outcome.status not in {"completed_turn", "yielded"}:
        raise AssertionError(f"unexpected model turn status: {outcome}")
    if after < 3:
        raise AssertionError(f"model did not complete collect_resource: after={after} outcome={outcome} trace={trace}")
    if not any(event.get("success") is True for event in collect_results):
        raise AssertionError(f"model did not call collect_resource successfully: trace={trace}")
    await model_provider.aclose()
    return {
        "status": outcome.status,
        "after_count": after,
        "tool_results": collect_results,
        "trace_events": [event.get("event") for event in trace],
    }


def main() -> None:
    try:
        provider = provider_registry_from_env()
        provider.resolve("primary")
    except AppConfigError as exc:
        if os.environ.get("MINEBOT_E2E_REQUIRED") == "1":
            raise
        print(f"SKIP: real model provider not configured: {exc}")
        raise SystemExit(SKIP_EXIT_CODE)
    else:
        asyncio.run(provider.aclose())

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
        reset_subject(rcon, item="dirt", count=4)
        result = asyncio.run(run_real_model_collect(body))
        print(result)


if __name__ == "__main__":
    main()
