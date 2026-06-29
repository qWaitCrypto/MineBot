"""Formal Agent Phase 1 runtime wiring.

This app-layer composition root owns the formal real-server tool surface. Narrow
helpers such as ``resource_runtime`` may delegate here, but the real harness must
not silently expose only a resource-only registry.
"""

from __future__ import annotations

from dataclasses import dataclass

from minebot.app.model_provider import ModelProviderRegistry
from minebot.app.runner import RuntimeTrace
from minebot.app.runner import sdk_tool_for
from minebot.app.wiring import AgentRuntimeParts, build_agent_runtime
from minebot.body import BlockWork, NavigationRunConfig, NavigationTransactions
from minebot.brain.composition import (
    CompositionBudget,
    CompositionContext,
    register_collect_resource_tool,
    register_inventory_tools,
)
from minebot.brain.registry import RegisteredTool, ToolRegistry, ToolSidecar
from minebot.contract import Body, BreakContext, Position, Region, ToolResult
from minebot.game import GovernancePolicy, GridWorld, NavigationCostModel, ScarpetBody
from minebot.game.navigation import GoalNear, SegmentedNavigator


@dataclass(frozen=True)
class Phase1RuntimeConfig:
    natural_region: Region
    budget: CompositionBudget = CompositionBudget(max_candidates=96, max_mutating_calls=96, max_wall_s=900.0)


@dataclass(frozen=True)
class Phase1ToolManifestEntry:
    name: str
    source: str
    tool_type: str
    permission: str
    mutating: bool
    body_scope: tuple[str, ...]


def build_phase1_agent_runtime(
    *,
    body: ScarpetBody,
    goal_text: str,
    model_provider: ModelProviderRegistry | None,
    config: Phase1RuntimeConfig,
    agent_name: str = "MineBot",
    language: str = "English",
    trace: RuntimeTrace | None = None,
) -> AgentRuntimeParts:
    registry = build_phase1_registry(body, config)
    parts = build_agent_runtime(
        body=body,
        registry=registry,
        goal_text=goal_text,
        model_provider=model_provider,
        agent_name=agent_name,
        language=language,
        trace=trace,
    )
    context = CompositionContext(
        registry=registry,
        weld_context=parts.runtime.weld_context,
        runtime_profile=parts.modes.profile_for(parts.lifecycle.state),
        budget=config.budget,
        trace=lambda event, payload: parts.runtime.trace.emit(event, **payload),
    )
    register_collect_resource_tool(registry, context)
    parts.runtime.registry = registry
    parts.runtime.agent = parts.runtime.agent.clone(tools=[sdk_tool_for(registry.get(name)) for name in registry.names()])
    parts.runtime.trace.emit("tool_manifest", tools=tool_manifest(registry))
    return parts


def build_phase1_registry(body: ScarpetBody, config: Phase1RuntimeConfig) -> ToolRegistry:
    policy = GovernancePolicy(natural_regions=[config.natural_region])
    # SegmentedNavigator still carries governance policy for fallback/legacy
    # transaction seams, but production move_to delegates primary pathfinding to
    # Scarpet navigateTo instead of populating a Python-local terrain grid.
    world = GridWorld({})
    navigator = NavigationTransactions(
        body,
        SegmentedNavigator(world, NavigationCostModel(policy)),
    )
    work = BlockWork(body, policy, navigator=navigator)
    registry = ToolRegistry()
    registry.register(_read_state_tool(body))
    register_inventory_tools(registry, body)
    registry.register(_move_to_tool(navigator))
    registry.register(_search_tool(work))
    registry.register(_mine_collect_tool(work))
    return registry


def tool_manifest(registry: ToolRegistry) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for name in registry.names():
        tool = registry.get(name)
        sidecar = tool.sidecar
        rows.append(
            {
                "name": tool.name,
                "source": sidecar.source,
                "tool_type": sidecar.tool_type,
                "permission": sidecar.permission,
                "mutating": sidecar.mutating,
                "body_scope": list(sidecar.body_scope),
                "terminal_truth": list(sidecar.terminal_truth),
            }
        )
    return rows


def inventory_count(body: Body, item: str) -> int:
    wanted = item.removeprefix("minecraft:")
    total = 0
    start: int | None = 0
    while start is not None:
        perception = body.perceive("inventory", {"start": start, "limit": 12})
        if not perception.ok:
            raise ValueError(f"inventory perception failed: {perception.error}")
        for payload in perception.data.get("slots") or []:
            slot = payload if hasattr(payload, "get") else None
            if not isinstance(slot, dict):
                continue
            slot_item = slot.get("item")
            slot_count = slot.get("count")
            if slot_item is not None and str(slot_item).removeprefix("minecraft:") == wanted:
                total += int(slot_count or 0)
        next_start = perception.data.get("nextStart")
        start = int(next_start) if next_start is not None else None
    return total


def _read_state_tool(body: Body) -> RegisteredTool:
    return RegisteredTool(
        "read_state",
        "Read authoritative bot state: position, health, food, oxygen, dimension, and inventory hash.",
        {"type": "object", "properties": {}, "additionalProperties": False},
        lambda _params: _read_state(body),
        ToolSidecar(
            "read_state",
            mutating=False,
            source="body.perception",
            tool_type="state",
            permission="read_state",
            body_scope=("state",),
            terminal_truth=("BodyState",),
            timeout_s=5.0,
        ),
    )


def _move_to_tool(navigator: NavigationTransactions) -> RegisteredTool:
    return RegisteredTool(
        "move_to",
        "Navigate the bot to a target position or near a target position using the Body navigation transaction.",
        {
            "type": "object",
            "properties": {
                "pos": {"type": "array", "items": {"type": "integer"}, "minItems": 3, "maxItems": 3},
                "radius": {"type": "integer", "minimum": 0},
                "timeout_s": {"type": "number", "exclusiveMinimum": 0},
            },
            "required": ["pos"],
            "additionalProperties": False,
        },
        lambda params: navigator.navigate_to(
            _nav_goal(params),
            break_context=BreakContext.TRAVEL,
            config=NavigationRunConfig(max_segments=8, segment_timeout_s=float(params.get("timeout_s") or 12.0)),
        ),
        ToolSidecar(
            "move_to",
            mutating=True,
            source="body.navigation",
            tool_type="navigation",
            permission="move",
            body_scope=("navigation",),
            terminal_truth=("navigateDone", "position"),
            timeout_s=120.0,
        ),
    )


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
            find_limit=int(params.get("find_limit") or 6),
            timeout_s=12.0,
        ),
        ToolSidecar(
            "search_for_block",
            mutating=False,
            source="body.block_work",
            tool_type="perception",
            permission="read_world",
            body_scope=("blocks",),
            timeout_s=15.0,
        ),
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
            source="body.block_work",
            tool_type="work",
            permission="break_collect",
            body_scope=("mine",),
            terminal_truth=("mineDone", "inventory"),
            timeout_s=12.0,
        ),
    )


def _read_state(body: Body) -> ToolResult:
    state = body.get_state()
    return ToolResult(
        True,
        "state_read",
        False,
        metrics={
            "bot": state.bot,
            "pos": list(state.pos),
            "health": state.health,
            "food": state.food,
            "oxygen": state.oxygen,
            "dimension": state.dimension,
            "inventory_hash": state.inventory_hash,
            "complete": state.complete,
            "missing": state.missing,
        },
    )


def _nav_goal(params: dict[str, object]) -> Position | GoalNear:
    pos = tuple(int(value) for value in params["pos"])
    if len(pos) != 3:
        raise ValueError("pos must contain exactly three coordinates")
    radius = int(params.get("radius") or 0)
    if radius > 0:
        return GoalNear(pos, radius=radius)
    return pos


__all__ = [
    "Phase1RuntimeConfig",
    "Phase1ToolManifestEntry",
    "build_phase1_agent_runtime",
    "build_phase1_registry",
    "inventory_count",
    "tool_manifest",
]
