"""Resource-collection runtime wiring for local agent runs.

This is an app-layer composition root: it is allowed to join Brain registry
tools with Body transactions and the Scarpet/RCON game client. The Brain package
stays free of Body/Game imports.
"""

from __future__ import annotations

from dataclasses import dataclass

from minebot.app.model_provider import ModelProviderRegistry
from minebot.app.runner import sdk_tool_for
from minebot.app.wiring import AgentRuntimeParts, build_agent_runtime
from minebot.body import BlockWork, NavigationTransactions
from minebot.brain.composition import (
    CompositionBudget,
    CompositionContext,
    register_collect_resource_tool,
    register_inventory_tools,
)
from minebot.brain.registry import RegisteredTool, ToolRegistry, ToolSidecar
from minebot.contract import Body, BreakContext, Region
from minebot.game import GovernancePolicy, GridCell, GridWorld, NavigationCostModel, ScarpetBody
from minebot.game.navigation import SegmentedNavigator


@dataclass(frozen=True)
class ResourceRuntimeConfig:
    natural_region: Region
    budget: CompositionBudget = CompositionBudget(max_candidates=8, max_mutating_calls=8, max_wall_s=90.0)


def build_resource_agent_runtime(
    *,
    body: ScarpetBody,
    goal_text: str,
    model_provider: ModelProviderRegistry | None,
    config: ResourceRuntimeConfig,
    agent_name: str = "MineBot",
    language: str = "English",
) -> AgentRuntimeParts:
    registry = build_resource_registry(body, config)
    parts = build_agent_runtime(
        body=body,
        registry=registry,
        goal_text=goal_text,
        model_provider=model_provider,
        agent_name=agent_name,
        language=language,
    )
    context = CompositionContext(
        registry=registry,
        weld_context=parts.runtime.weld_context,
        runtime_profile=parts.modes.profile_for(parts.lifecycle.state),
        budget=config.budget,
    )
    register_collect_resource_tool(registry, context)
    parts.runtime.registry = registry
    parts.runtime.agent = parts.runtime.agent.clone(tools=[sdk_tool_for(registry.get(name)) for name in registry.names()])
    return parts


def build_resource_registry(body: ScarpetBody, config: ResourceRuntimeConfig) -> ToolRegistry:
    policy = GovernancePolicy(natural_regions=[config.natural_region])
    navigator = NavigationTransactions(body, SegmentedNavigator(_flat_world(config.natural_region), NavigationCostModel(policy)))
    work = BlockWork(body, policy, navigator=navigator)
    registry = ToolRegistry()
    register_inventory_tools(registry, body)
    registry.register(_search_tool(work))
    registry.register(_mine_collect_tool(work))
    return registry


def inventory_count(body: Body, item: str) -> int:
    wanted = item.removeprefix("minecraft:")
    total = 0
    for slot in body.get_inventory():
        if slot.item is not None and slot.item.removeprefix("minecraft:") == wanted:
            total += slot.count
    return total


def _flat_world(region: Region) -> GridWorld:
    cells: dict[tuple[int, int, int], GridCell] = {}
    for x in range(region.min_pos[0], region.max_pos[0] + 1):
        for z in range(region.min_pos[2], region.max_pos[2] + 1):
            cells[(x, 70, z)] = GridCell()
    return GridWorld(cells)


def _search_tool(work: BlockWork) -> RegisteredTool:
    return RegisteredTool(
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


def _mine_collect_tool(work: BlockWork) -> RegisteredTool:
    return RegisteredTool(
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


__all__ = [
    "ResourceRuntimeConfig",
    "build_resource_agent_runtime",
    "build_resource_registry",
    "inventory_count",
]
