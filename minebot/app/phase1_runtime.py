"""Formal Agent Phase 1 runtime wiring.

This app-layer composition root owns the formal real-server tool surface. Narrow
helpers such as ``resource_runtime`` may delegate here, but the real harness must
not silently expose only a resource-only registry.
"""

from __future__ import annotations

from dataclasses import dataclass

from minebot.app.model_provider import ModelProviderRegistry
from minebot.app.runner import AgentRuntime, RecoveryOutcome, RuntimeTrace
from minebot.app.runner import sdk_tool_for
from minebot.app.wiring import AgentRuntimeParts, build_agent_runtime
from minebot.body import BlockWork, LifecycleTransactions, NavigationRunConfig, NavigationTransactions
from minebot.body.combat import CombatTransactions, find_hostiles
from minebot.brain.composition import (
    CompositionBudget,
    CompositionContext,
    register_collect_resource_tool,
    register_inventory_tools,
)
from minebot.brain.registry import RegisteredTool, ToolRegistry, ToolSidecar
from minebot.brain.progress import ProgressAuthority
from minebot.contract import Body, BreakContext, InventorySlot, Position, Region, ToolResult, perception_next_cursor
from minebot.game import GovernancePolicy, ScarpetBody
from minebot.game.navigation import GoalNear


@dataclass(frozen=True)
class Phase1RuntimeConfig:
    natural_region: Region
    budget: CompositionBudget = CompositionBudget(max_candidates=96, max_mutating_calls=96, max_wall_s=900.0)
    recovery_respawn_pos: Position | None = None
    recovery_gamemode: str | None = None


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
    authority = ProgressAuthority()
    registry = build_phase1_registry(body, config, authority=authority)
    parts = build_agent_runtime(
        body=body,
        registry=registry,
        goal_text=goal_text,
        model_provider=model_provider,
        agent_name=agent_name,
        language=language,
        trace=trace,
        recovery_handler=_phase1_recovery_handler(body, config),
        authority=authority,
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


def _phase1_recovery_handler(body: ScarpetBody, config: Phase1RuntimeConfig):
    lifecycle = LifecycleTransactions(body)

    def recover(runtime: AgentRuntime) -> RecoveryOutcome:
        pre_recovery_inventory = _pre_recovery_inventory_facts(runtime, body)
        before_state = body.get_state()
        respawn_pos = _recovery_respawn_pos(runtime, config)
        runtime.trace.emit(
            "recovery_driver_start",
            respawn_pos=None if respawn_pos is None else list(respawn_pos),
            state_before_missing=before_state.missing,
            state_before_pos=list(before_state.pos),
            inventory_before_recovery=pre_recovery_inventory,
            last_known_body_state=runtime.last_known_body_state,
        )
        result = lifecycle.recover_after_death(
            respawn_pos=respawn_pos,
            yaw=_maybe_float_from_state(runtime.last_known_body_state, "yaw"),
            pitch=_maybe_float_from_state(runtime.last_known_body_state, "pitch"),
            dimension=_maybe_str_from_state(runtime.last_known_body_state, "dimension"),
            gamemode=config.recovery_gamemode,
        )
        facts: dict[str, object] = {
            "respawn_pos": None if respawn_pos is None else list(respawn_pos),
            "state_before_missing": before_state.missing,
            "state_before_pos": list(before_state.pos),
            "recovery_reason": result.reason,
            "inventory_before_recovery": pre_recovery_inventory,
        }
        if runtime.last_known_body_state is not None:
            facts["last_known_body_state"] = dict(runtime.last_known_body_state)
        if isinstance(result.metrics, dict):
            facts["recovery_metrics"] = dict(result.metrics)
        if not result.success:
            return RecoveryOutcome(False, result.reason, facts=facts, can_retry=result.can_retry)
        after_state = body.get_state()
        runtime._remember_body_state(after_state)
        post_recovery_inventory = _safe_inventory_counts(body)
        facts["inventory_after_recovery"] = post_recovery_inventory
        facts["inventory_recovery_delta"] = _inventory_delta(pre_recovery_inventory, post_recovery_inventory)
        facts.update(
            {
                "state_after_pos": list(after_state.pos),
                "state_after_missing": after_state.missing,
                "state_after_inventory_hash": after_state.inventory_hash,
            }
        )
        return RecoveryOutcome(True, "respawned", facts=facts, can_retry=False)

    return recover


def _pre_recovery_inventory_facts(runtime: AgentRuntime, body: Body) -> dict[str, object]:
    slot = runtime.mode_runtime.suspend_slot
    progress = slot.last_progress if slot is not None else {}
    event_counts = progress.get("inventory_counts_before")
    if isinstance(event_counts, dict):
        return {"ok": True, "source": "death_event", "counts": _normalized_counts(event_counts)}
    return _safe_inventory_counts(body, source="body_recount")


def _recovery_respawn_pos(runtime: AgentRuntime, config: Phase1RuntimeConfig) -> Position | None:
    if config.recovery_respawn_pos is not None:
        return tuple(int(value) for value in config.recovery_respawn_pos)
    state = runtime.last_known_body_state or {}
    pos = state.get("pos")
    if isinstance(pos, list) and len(pos) == 3:
        return (round(float(pos[0])), round(float(pos[1])), round(float(pos[2])))
    return None


def _maybe_float_from_state(state: dict[str, object] | None, key: str) -> float | None:
    if not state:
        return None
    value = state.get(key)
    return None if value is None else float(value)


def _maybe_str_from_state(state: dict[str, object] | None, key: str) -> str | None:
    if not state:
        return None
    value = state.get(key)
    return None if value is None else str(value)


def _safe_inventory_counts(body: Body, *, source: str = "body_recount") -> dict[str, object]:
    try:
        return {"ok": True, "source": source, "counts": _inventory_counts_snapshot(body)}
    except Exception as exc:
        return {"ok": False, "source": source, "error": str(exc), "error_type": type(exc).__name__}


def _inventory_counts_snapshot(body: Body, *, page_size: int = 12) -> dict[str, int]:
    counts: dict[str, int] = {}
    start: int | None = 0
    saw_page = False
    while start is not None:
        perception = body.perceive("inventory", {"start": start, "limit": page_size})
        saw_page = True
        if not perception.ok or not perception.complete:
            raise ValueError(
                "inventory perception failed during recovery recount: "
                f"ok={perception.ok} complete={perception.complete} error={perception.error}"
            )
        for payload in perception.data.get("slots") or []:
            if not isinstance(payload, dict):
                continue
            slot = InventorySlot.from_payload(payload)
            if slot.empty or not slot.item:
                continue
            item = str(slot.item).removeprefix("minecraft:")
            counts[item] = counts.get(item, 0) + slot.count
        next_start = _next_start(perception)
        start = int(next_start) if next_start is not None else None
    if not saw_page:
        raise ValueError("inventory perception failed during recovery recount: no pages read")
    return counts


def _inventory_delta(before: dict[str, object], after: dict[str, object]) -> dict[str, object]:
    if before.get("ok") is not True or after.get("ok") is not True:
        return {"ok": False, "before_ok": before.get("ok"), "after_ok": after.get("ok")}
    before_counts = before.get("counts")
    after_counts = after.get("counts")
    if not isinstance(before_counts, dict) or not isinstance(after_counts, dict):
        return {"ok": False, "reason": "counts_missing"}
    deltas: dict[str, int] = {}
    for item in sorted(set(before_counts) | set(after_counts)):
        delta = int(after_counts.get(item, 0) or 0) - int(before_counts.get(item, 0) or 0)
        if delta:
            deltas[item] = delta
    lost = {item: -delta for item, delta in deltas.items() if delta < 0}
    gained = {item: delta for item, delta in deltas.items() if delta > 0}
    return {"ok": True, "deltas": deltas, "lost": lost, "gained": gained}


def _normalized_counts(counts: dict[object, object]) -> dict[str, int]:
    normalized: dict[str, int] = {}
    for key, value in counts.items():
        item = str(key).removeprefix("minecraft:")
        normalized[item] = int(value or 0)
    return normalized


def _next_start(perception) -> object | None:
    return perception_next_cursor(perception)


def build_phase1_registry(
    body: ScarpetBody,
    config: Phase1RuntimeConfig,
    *,
    authority: ProgressAuthority | None = None,
) -> ToolRegistry:
    policy = GovernancePolicy(natural_regions=[config.natural_region])
    progress = authority or ProgressAuthority()
    navigator = NavigationTransactions.server_side(body, policy, progress=progress)
    work = BlockWork(body, policy, navigator=navigator)
    registry = ToolRegistry()
    registry.register(_read_state_tool(body))
    register_inventory_tools(registry, body)
    registry.register(_move_to_tool(navigator))
    registry.register(_follow_tool(navigator))
    combat = CombatTransactions(body, progress=progress)
    registry.register(_engage_tool(combat))
    registry.register(_find_hostiles_tool(body))
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
        next_start = _next_start(perception)
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


def _follow_tool(navigator: NavigationTransactions) -> RegisteredTool:
    return RegisteredTool(
        "follow_entity",
        "Follow a moving player or named entity, keeping a distance. The Body re-plans the path server-side as the target moves.",
        {
            "type": "object",
            "properties": {
                "target": {"type": "string", "minLength": 1},
                "keep_distance": {"type": "number", "minimum": 0},
                "timeout_s": {"type": "number", "exclusiveMinimum": 0},
            },
            "required": ["target"],
            "additionalProperties": False,
        },
        lambda params: navigator.follow_entity(
            str(params["target"]),
            keep_distance=float(params.get("keep_distance") or 3.0),
            timeout_s=float(params.get("timeout_s") or 30.0),
        ),
        ToolSidecar(
            "follow_entity",
            mutating=True,
            source="body.navigation",
            tool_type="navigation",
            permission="move",
            body_scope=("navigation",),
            terminal_truth=("followDone", "position"),
            timeout_s=120.0,
        ),
    )


def _engage_tool(combat: CombatTransactions) -> RegisteredTool:
    return RegisteredTool(
        "engage_entity",
        "Engage and fight a hostile target (by name/type/uuid, or 'nearest_hostile'). The Body approaches via server-side pathfinding, swings on cooldown when in range with line-of-sight, disengages on low health, and kill-verifies. Melee; ranged mobs use cover-aware approach.",
        {
            "type": "object",
            "properties": {
                "target": {"type": "string", "minLength": 1},
                "attack_range": {"type": "number", "minimum": 1.2, "maximum": 3.0},
                "cooldown_ticks": {"type": "integer", "minimum": 1},
                "timeout_s": {"type": "number", "exclusiveMinimum": 0},
                "disengage_health": {"type": "number", "minimum": 0},
            },
            "required": ["target"],
            "additionalProperties": False,
        },
        lambda params: combat.engage_entity(
            str(params["target"]),
            attack_range=float(params.get("attack_range") or 2.0),
            cooldown_ticks=int(params.get("cooldown_ticks") or 10),
            timeout_s=float(params.get("timeout_s") or 20.0),
            disengage_health=float(params.get("disengage_health") or 6.0),
        ),
        ToolSidecar(
            "engage_entity",
            mutating=True,
            source="body.combat",
            tool_type="combat",
            permission="combat",
            body_scope=("combat", "nearby_entities"),
            terminal_truth=("engageDone", "position"),
            timeout_s=120.0,
        ),
    )


def _find_hostiles_tool(body: Body) -> RegisteredTool:
    return RegisteredTool(
        "find_hostiles",
        "Find nearby hostile mobs via the nearbyHostiles perception, sorted nearest-first. Returns type/name/pos/health for each.",
        {
            "type": "object",
            "properties": {
                "radius": {"type": "integer", "minimum": 1, "maximum": 32},
                "limit": {"type": "integer", "minimum": 1, "maximum": 128},
            },
            "additionalProperties": False,
        },
        lambda params: find_hostiles(
            body,
            radius=int(params.get("radius") or 16),
            limit=int(params.get("limit") or 16),
        ),
        ToolSidecar(
            "find_hostiles",
            mutating=False,
            source="body.perception",
            tool_type="perception",
            permission="read_world",
            body_scope=("nearby_entities",),
            timeout_s=15.0,
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
            search_radius=min(max(1, int(params.get("search_radius") or 16)), 64),
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
            pickup_timeout_s=2.0,
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
