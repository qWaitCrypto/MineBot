"""Formal Agent Phase 1 runtime wiring.

This app-layer composition root owns the formal real-server tool surface. Narrow
helpers such as ``resource_runtime`` may delegate here, but the real harness must
not silently expose only a resource-only registry.
"""

from __future__ import annotations

from collections.abc import Callable
import time
from dataclasses import dataclass, replace

from agents import Session

from minebot.app.body_capability_tools import register_body_capability_tools
from minebot.app.model_provider import ModelProviderRegistry
from minebot.app.conversation_tools import register_conversation_archive_tools
from minebot.app.memory import MemoryWorkspace, register_memory_tools
from minebot.app.skills import SkillWorkspace, register_skill_tools
from minebot.app.wiki import WikiKnowledge, register_wiki_tools
from minebot.app.runner import AgentRuntime, RecoveryOutcome, RuntimeTrace
from minebot.app.runner import sdk_tool_for
from minebot.app.observation_artifacts import (
    ToolObservationArchive,
    register_tool_observation_tools,
)
from minebot.app.progress_epochs import ProgressEpochArchive
from minebot.app.tasks import TaskWorkspace, register_task_tools
from minebot.app.wiring import AgentRuntimeParts, build_agent_runtime
from minebot.body import (
    BlockApproachTransactions,
    BlockWork,
    ContainerTransactions,
    ExplorationCoverageStore,
    ExplorationTransactions,
    FurnaceTransactions,
    InteractionTransactions,
    InventoryTransactions,
    LifecycleTransactions,
    NavigationRunConfig,
    NavigationTransactions,
    MemoryExplorationCoverageStore,
    PickupTransactions,
    ResourceCollectionTransactions,
    UseTransactions,
    VoxelStructureRiskAssessor,
)
from minebot.body.combat import CombatTransactions, find_hostiles
from minebot.body.furnace import DEFAULT_SMELT_SECONDS_PER_ITEM, resolve_smelt_output, select_fuel
from minebot.body.inventory import _parse_recipe_variants
from minebot.body.world_read import read_block_facts
from minebot.brain.acquisition import RecipeVariant
from minebot.brain.composition import (
    CompositionBudget,
    CompositionContext,
    register_collect_resource_tool,
    register_ensure_tool_for_tool,
    register_inventory_tools,
)
from minebot.brain.registry import RegisteredTool, ToolRegistry, ToolSidecar
from minebot.brain.progress import ProgressAuthority
from minebot.contract import Body, BreakContext, InventorySlot, PerceptionResult, Position, Region, ToolResult, perception_next_cursor
from minebot.game import GovernancePolicy, ScarpetBody
from minebot.game.navigation import GoalNear


@dataclass(frozen=True)
class Phase1RuntimeConfig:
    natural_region: Region
    budget: CompositionBudget = CompositionBudget(max_candidates=96, max_mutating_calls=96, max_wall_s=900.0)
    recovery_respawn_pos: Position | None = None
    recovery_gamemode: str | None = None
    speech_sink: Callable[[str], None] | None = None
    conversation_session: Session | None = None
    task_workspace: TaskWorkspace | None = None
    observation_archive: ToolObservationArchive | None = None
    progress_epoch_archive: ProgressEpochArchive | None = None
    memory_workspace: MemoryWorkspace | None = None
    skill_workspace: SkillWorkspace | None = None
    wiki_knowledge: WikiKnowledge | None = None
    exploration_coverage_store: ExplorationCoverageStore | None = None


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
        speech_sink=config.speech_sink,
        conversation_session=config.conversation_session,
        observation_archive=config.observation_archive,
        progress_epoch_archive=config.progress_epoch_archive,
    )
    context = CompositionContext(
        registry=registry,
        weld_context=parts.runtime.weld_context,
        runtime_profile=parts.modes.profile_for(parts.lifecycle.state),
        budget=config.budget,
        recipe_lookup=_recipe_lookup(body),
        trace=lambda event, payload: parts.runtime.trace.emit(event, **payload),
    )
    register_collect_resource_tool(registry, context)
    register_ensure_tool_for_tool(registry, context, _recipe_lookup(body))
    if config.task_workspace is not None:
        register_task_tools(
            registry,
            config.task_workspace,
            body_fingerprint=lambda: _task_body_fingerprint(body, authority),
            evidence_cursor=parts.runtime.current_run_evidence_cursor,
            generation=authority.current_generation,
        )
    if (
        config.conversation_session is not None
        and callable(getattr(config.conversation_session, "query_archive", None))
        and callable(getattr(config.conversation_session, "read_archive_turn", None))
    ):
        register_conversation_archive_tools(registry, config.conversation_session)
    if config.observation_archive is not None:
        register_tool_observation_tools(registry, config.observation_archive)
    if config.memory_workspace is not None:
        register_memory_tools(registry, config.memory_workspace)
    if config.skill_workspace is not None:
        register_skill_tools(registry, config.skill_workspace)
    if config.wiki_knowledge is not None:
        register_wiki_tools(registry, config.wiki_knowledge)
    if config.skill_workspace is not None:
        config.skill_workspace.bind_registry(registry)
        config.skill_workspace.sync_context(parts.context)
        parts.runtime.add_context_refresher(config.skill_workspace.sync_context)
    parts.runtime.registry = registry
    parts.runtime.agent = parts.runtime.agent.clone(tools=[sdk_tool_for(registry.get(name)) for name in registry.names()])
    parts.runtime.trace.emit("tool_manifest", tools=tool_manifest(registry))
    return replace(parts, skill_workspace=config.skill_workspace)


def _phase1_recovery_handler(body: ScarpetBody, config: Phase1RuntimeConfig):
    lifecycle = LifecycleTransactions(body)

    def recover(runtime: AgentRuntime) -> RecoveryOutcome:
        pre_recovery_inventory = _pre_recovery_inventory_facts(runtime, body)
        before_state, before_state_errors = _body_state_with_transport_retry(body)
        respawn_pos = _recovery_respawn_pos(runtime, config)
        runtime.trace.emit(
            "recovery_driver_start",
            respawn_pos=None if respawn_pos is None else list(respawn_pos),
            state_before_missing=before_state.missing,
            state_before_pos=list(before_state.pos),
            inventory_before_recovery=pre_recovery_inventory,
            last_known_body_state=runtime.last_known_body_state,
            state_before_recheck_errors=before_state_errors,
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
        if before_state_errors:
            facts["state_before_recheck_errors"] = before_state_errors
        if runtime.last_known_body_state is not None:
            facts["last_known_body_state"] = dict(runtime.last_known_body_state)
        if isinstance(result.metrics, dict):
            facts["recovery_metrics"] = dict(result.metrics)
        reconciled_existing_body = (
            not result.success
            and result.reason == "body_not_missing"
            and isinstance(result.metrics, dict)
            and result.metrics.get("state_before_missing") is False
        )
        if reconciled_existing_body:
            facts["recovery_reconciliation"] = "body_already_present"
        elif not result.success:
            return RecoveryOutcome(False, result.reason, facts=facts, can_retry=result.can_retry)
        after_state, after_state_errors = _body_state_with_transport_retry(body)
        if after_state_errors:
            facts["state_after_recheck_errors"] = after_state_errors
        if reconciled_existing_body and after_state.missing:
            facts["state_after_pos"] = list(after_state.pos)
            facts["state_after_missing"] = True
            return RecoveryOutcome(
                False,
                "body_missing_during_reconciliation",
                facts=facts,
                can_retry=True,
            )
        safe_respawn = _ensure_safe_recovery_stand(body, lifecycle, after_state, config)
        if safe_respawn is not None:
            facts["safe_respawn"] = safe_respawn.to_payload()
            if not safe_respawn.success:
                return RecoveryOutcome(False, safe_respawn.reason, facts=facts, can_retry=safe_respawn.can_retry)
            after_state, safe_after_errors = _body_state_with_transport_retry(body)
            if safe_after_errors:
                facts["safe_respawn_state_recheck_errors"] = safe_after_errors
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
        reason = "body_reconciled" if reconciled_existing_body else "respawned"
        return RecoveryOutcome(True, reason, facts=facts, can_retry=False)

    return recover


def _body_state_with_transport_retry(body: Body, *, attempts: int = 5, delay_s: float = 0.2):
    errors: list[dict[str, object]] = []
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return body.get_state(), errors
        except Exception as exc:
            if not _is_recheckable_recovery_transport_error(exc):
                raise
            last_exc = exc
            errors.append({"attempt": attempt, "error_type": type(exc).__name__, "message": str(exc)})
            if attempt < attempts:
                time.sleep(delay_s)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("body state recheck failed")


def _is_recheckable_recovery_transport_error(exc: Exception) -> bool:
    if isinstance(exc, (OSError, TimeoutError)):
        return True
    error_type = type(exc).__name__
    if error_type in {"BodyProtocolError", "EnvelopeError", "RconError", "TruncatedPayloadError", "IncompletePayloadError"}:
        return True
    return "RCON" in str(exc)


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
    if _last_known_state_is_hazardous_for_respawn(state):
        return None
    pos = state.get("pos")
    if isinstance(pos, list) and len(pos) == 3:
        return (round(float(pos[0])), round(float(pos[1])), round(float(pos[2])))
    return None


def _last_known_state_is_hazardous_for_respawn(state: dict[str, object]) -> bool:
    oxygen = state.get("oxygen")
    if oxygen is None:
        return False
    try:
        return float(oxygen) <= 80.0
    except (TypeError, ValueError):
        return False


def _ensure_safe_recovery_stand(
    body: ScarpetBody,
    lifecycle: LifecycleTransactions,
    state,
    config: Phase1RuntimeConfig,
) -> ToolResult | None:
    if not _state_requires_safe_respawn(state):
        return None
    safe_pos = _find_recovery_dry_stand(body, state.pos)
    if safe_pos is None:
        return ToolResult(
            False,
            "respawn_unsafe:no_dry_stand",
            True,
            metrics={"state_after": _recovery_state_metrics(state)},
        )
    despawn = body.despawn()
    if not (despawn.ok and despawn.accepted):
        return ToolResult(
            False,
            f"respawn_unsafe:despawn_failed:{despawn.error or 'despawn_failed'}",
            True,
            metrics={
                "state_after": _recovery_state_metrics(state),
                "safe_respawn_pos": list(safe_pos),
                "despawn": {
                    "ok": despawn.ok,
                    "accepted": despawn.accepted,
                    "complete": despawn.complete,
                    "error": despawn.error,
                    "data": despawn.data,
                },
            },
        )
    result = lifecycle.recover_after_death(
        respawn_pos=safe_pos,
        yaw=getattr(state, "yaw", None),
        pitch=getattr(state, "pitch", None),
        dimension=getattr(state, "dimension", None),
        gamemode=config.recovery_gamemode,
    )
    metrics = dict(result.metrics or {})
    metrics["safe_respawn_pos"] = list(safe_pos)
    metrics["unsafe_state_after_default_spawn"] = _recovery_state_metrics(state)
    return ToolResult(result.success, f"safe_{result.reason}", result.can_retry, result.next_suggestion, metrics)


def _state_requires_safe_respawn(state) -> bool:
    if getattr(state, "missing", False):
        return True
    oxygen = getattr(state, "oxygen", None)
    if oxygen is not None:
        try:
            if float(oxygen) <= 80.0:
                return True
        except (TypeError, ValueError):
            pass
    health = getattr(state, "health", None)
    if health is not None:
        try:
            if float(health) <= 0.0:
                return True
        except (TypeError, ValueError):
            pass
    return False


def _find_recovery_dry_stand(body: Body, pos: tuple[float, float, float]) -> Position | None:
    bx, by, bz = round(float(pos[0])), round(float(pos[1])), round(float(pos[2]))
    origins: list[Position] = []
    for radius in range(0, 7):
        for dx in range(-radius, radius + 1):
            for dz in range(-radius, radius + 1):
                if max(abs(dx), abs(dz)) != radius:
                    continue
                for dy in range(4, -7, -1):
                    origins.append((bx + dx, by + dy, bz + dz))
    wanted: list[Position] = []
    for x, y, z in origins:
        wanted.extend(((x, y, z), (x, y + 1, z), (x, y - 1, z)))
    try:
        facts = read_block_facts(body, tuple(wanted), page_size=96, failure_label="recovery_safe_stand")
    except ValueError:
        return None
    for stand in origins:
        feet = facts.get(stand)
        head = facts.get((stand[0], stand[1] + 1, stand[2]))
        support = facts.get((stand[0], stand[1] - 1, stand[2]))
        if feet is None or head is None or support is None:
            continue
        if _is_recovery_clear(feet) and _is_recovery_clear(head) and _is_recovery_support(support):
            return stand
    return None


def _is_recovery_clear(perception: PerceptionResult) -> bool:
    block_type = _normalize_block_type(str(perception.data.get("type") or "unknown"))
    block_state = str(perception.data.get("state") or "UNKNOWN")
    return block_type == "air" or block_state == "CLEAR"


def _is_recovery_support(perception: PerceptionResult) -> bool:
    block_type = _normalize_block_type(str(perception.data.get("type") or "unknown"))
    block_state = str(perception.data.get("state") or "UNKNOWN")
    return block_type not in {"air", "water", "lava"} and block_state not in {"CLEAR", "LIQUID"}


def _normalize_block_type(value: str) -> str:
    return value.removeprefix("minecraft:")


def _recovery_state_metrics(state) -> dict[str, object]:
    return {
        "pos": list(getattr(state, "pos", ())),
        "health": getattr(state, "health", None),
        "food": getattr(state, "food", None),
        "oxygen": getattr(state, "oxygen", None),
        "missing": getattr(state, "missing", None),
        "dimension": getattr(state, "dimension", None),
    }


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


def _task_body_fingerprint(body: Body, authority: ProgressAuthority) -> dict[str, object]:
    try:
        state = body.get_state()
    except Exception as exc:
        raise ValueError(
            f"task checkpoint body fingerprint failed: {type(exc).__name__}: {exc}"
        ) from exc
    return {
        "fingerprint": authority.fingerprint(state),
        "generation": authority.current_generation(),
        "pos": list(state.pos),
        "health": state.health,
        "food": state.food,
        "oxygen": state.oxygen,
        "inventory_hash": state.inventory_hash,
        "dimension": state.dimension,
        "missing": state.missing,
    }


def _inventory_counts_snapshot(body: Body, *, page_size: int = 12) -> dict[str, int]:
    counts: dict[str, int] = {}
    start: int | None = 0
    saw_page = False
    seen_starts: set[int] = set()
    while start is not None:
        if start in seen_starts:
            raise ValueError(
                f"inventory perception failed during recovery recount: repeated cursor {start}"
            )
        seen_starts.add(start)
        perception = body.perceive("inventory", {"start": start, "limit": page_size})
        saw_page = True
        next_start = _next_start(perception)
        if not perception.ok or (not perception.complete and next_start is None):
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
    policy = GovernancePolicy(
        natural_regions=[config.natural_region],
        structure_risk_assessor=VoxelStructureRiskAssessor(body),
        require_structure_assessment=True,
    )
    progress = authority or ProgressAuthority()
    navigator = NavigationTransactions.server_side(body, policy, progress=progress)
    exploration = ExplorationTransactions(
        body,
        navigator,
        config.exploration_coverage_store or MemoryExplorationCoverageStore(),
    )
    pickup = PickupTransactions(body, navigator)
    work = BlockWork(body, policy, navigator=navigator, pickup=pickup)
    block_approach = BlockApproachTransactions(body, navigator)
    resource_collection = ResourceCollectionTransactions(body, navigator, work)
    inventory_txn = InventoryTransactions(body, navigator=navigator, governance=policy, work=work)
    furnace_txn = FurnaceTransactions(body, navigator=navigator, governance=policy, work=work)
    use_txn = UseTransactions(body, navigator=navigator, inventory=inventory_txn)
    interaction_txn = InteractionTransactions(
        body,
        navigator=navigator,
        inventory=inventory_txn,
        use=use_txn,
        work=work,
        governance=policy,
    )
    container_txn = ContainerTransactions(body, navigator=navigator, governance=policy)
    registry = ToolRegistry()
    registry.register(_read_state_tool(body))
    register_inventory_tools(registry, body)
    registry.register(_move_to_tool(navigator))
    registry.register(_explore_for_tool(exploration))
    registry.register(_go_to_surface_tool(work))
    registry.register(_follow_tool(navigator))
    combat = CombatTransactions(body, progress=progress)
    registry.register(_engage_tool(combat))
    registry.register(_find_hostiles_tool(body))
    registry.register(_search_tool(work))
    registry.register(_mine_collect_tool(work))
    registry.register(_craft_tool(inventory_txn))
    registry.register(_equip_tool(inventory_txn))
    registry.register(_smelt_tool(body, furnace_txn))
    register_body_capability_tools(
        registry,
        body=body,
        block_approach=block_approach,
        navigator=navigator,
        work=work,
        inventory=inventory_txn,
        furnace=furnace_txn,
        container=container_txn,
        interaction=interaction_txn,
        pickup=pickup,
        resource_collection=resource_collection,
        use=use_txn,
    )
    return registry


def _recipe_lookup(body: Body):
    def lookup(item: str) -> list[RecipeVariant] | None:
        perception = body.perceive("recipeData", {"item": item})
        if not perception.ok:
            return None
        parsed = _parse_recipe_variants(item, perception)
        if isinstance(parsed, ToolResult):
            return None
        return [
            RecipeVariant(
                output_item=_phase1_recipe_item(variant.output_item),
                output_count=variant.output_count,
                ingredient_groups=tuple(tuple(_phase1_recipe_item(item) for item in group) for group in variant.ingredient_groups if group),
                requires_table=variant.requires_table,
                recipe_kind=variant.recipe_kind,
            )
            for variant in parsed
        ]

    return lookup


def _phase1_recipe_item(item: object) -> str:
    return str(item).removeprefix("minecraft:").strip().lower()


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
                "body_mutating": sidecar.can_mutate_body,
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
            config=NavigationRunConfig(max_segments=32, segment_timeout_s=float(params.get("timeout_s") or 12.0)),
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


def _explore_for_tool(exploration: ExplorationTransactions) -> RegisteredTool:
    return RegisteredTool(
        "explore_for",
        "Explore safe new world frontiers for one or more block or entity target classes. "
        "Choose WHAT to find; the Body owns frontier selection, navigation, coverage, and "
        "terminal verification. Resumable results include a typed continuation whose target "
        "descriptor must remain unchanged.",
        {
            "type": "object",
            "properties": {
                "block_targets": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1, "maxLength": 128},
                    "maxItems": 32,
                },
                "entity_targets": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1, "maxLength": 128},
                    "maxItems": 32,
                },
                "max_distance": {"type": "integer", "minimum": 16, "maximum": 512},
                "max_regions": {"type": "integer", "minimum": 1, "maximum": 32},
                "return_policy": {
                    "type": "string",
                    "enum": ["first_match", "region_budget"],
                },
                "scan_radius": {"type": "integer", "minimum": 4, "maximum": 32},
                "resume_cursor": {
                    "type": ["object", "null"],
                    "description": (
                        "Opaque cursor from a resumable explore_for result. Pass it unchanged "
                        "only with the exact target descriptor returned by that result."
                    ),
                    "properties": {
                        "query_signature": {"type": "string"},
                        "dimension": {"type": "string"},
                        "coverage_revision": {"type": "integer", "minimum": 0},
                    },
                    "required": ["query_signature", "dimension", "coverage_revision"],
                    "additionalProperties": False,
                },
            },
            "additionalProperties": False,
        },
        lambda params: exploration.explore_for(
            block_targets=tuple(str(item) for item in params.get("block_targets", [])),
            entity_targets=tuple(str(item) for item in params.get("entity_targets", [])),
            max_distance=int(params.get("max_distance") or 192),
            max_regions=int(params.get("max_regions") or 12),
            return_policy=str(params.get("return_policy") or "first_match"),
            scan_radius=int(params.get("scan_radius") or 12),
            resume_cursor=(
                dict(params["resume_cursor"])
                if isinstance(params.get("resume_cursor"), dict)
                else None
            ),
        ),
        ToolSidecar(
            "explore_for",
            mutating=True,
            source="body.exploration",
            tool_type="exploration",
            permission="explore_world",
            body_scope=("navigation", "blocks", "entities", "state"),
            terminal_truth=("ToolResult", "position", "exploration_coverage"),
            timeout_s=900.0,
            body_mutating=True,
        ),
    )


def _go_to_surface_tool(work: BlockWork) -> RegisteredTool:
    return RegisteredTool(
        "go_to_surface",
        "Move from a pit, water pocket, or below-grade position to a verified nearby natural surface using Body-owned navigation/ascent logic.",
        {
            "type": "object",
            "properties": {
                "timeout_s": {"type": "number", "exclusiveMinimum": 0},
                "surface_scan_height": {"type": "integer", "minimum": 0},
                "surface_scan_radius": {"type": "integer", "minimum": 0},
                "max_steps": {"type": "integer", "minimum": 1},
                "world_top_y": {"type": "integer"},
            },
            "additionalProperties": False,
        },
        lambda params: work.go_to_surface(
            context=BreakContext.COLLECT_APPROACH,
            timeout_s=float(params.get("timeout_s") or 30.0),
            surface_scan_height=int(params.get("surface_scan_height") or 32),
            surface_scan_radius=int(params.get("surface_scan_radius") or 2),
            max_steps=(int(params["max_steps"]) if params.get("max_steps") is not None else None),
            world_top_y=int(params.get("world_top_y") or 320),
        ),
        ToolSidecar(
            "go_to_surface",
            mutating=True,
            source="body.block_work",
            tool_type="navigation",
            permission="move",
            body_scope=("navigation", "surface"),
            terminal_truth=("position", "ToolResult"),
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


def _craft_tool(inventory_txn: InventoryTransactions) -> RegisteredTool:
    return RegisteredTool(
        "craft_item",
        "Craft an item from materials already in inventory. Handles recipe lookup, nearby/temporary crafting table lifecycle, and residue cleanup. Fails honestly with missing materials if ingredients are absent; it does not gather them.",
        {
            "type": "object",
            "properties": {
                "item": {
                    "type": "string",
                    "description": "Output item to craft, e.g. 'stone_pickaxe'.",
                },
                "count": {"type": "integer", "minimum": 1, "default": 1},
                "auto_equip": {"type": "boolean", "default": False},
            },
            "required": ["item"],
            "additionalProperties": False,
        },
        lambda params: inventory_txn.craft_recipe(
            item=str(params["item"]),
            count=int(params.get("count") or 1),
            auto_equip=bool(params.get("auto_equip", False)),
            search_radius=min(max(1, int(params.get("search_radius") or 8)), 64),
            keep_temporary_table=bool(params.get("keep_temporary_table", False)),
            cleanup_existing_bot_table=bool(params.get("cleanup_existing_bot_table", False)),
        ),
        ToolSidecar(
            "craft_item",
            mutating=True,
            source="body.inventory",
            tool_type="inventory",
            permission="craft",
            body_scope=("inventory", "blocks"),
            terminal_truth=("inventory", "ToolResult"),
            timeout_s=45.0,
        ),
    )


def _equip_tool(inventory_txn: InventoryTransactions) -> RegisteredTool:
    return RegisteredTool(
        "equip_item",
        "Equip an owned item into the appropriate equipment slot or the requested slot, then verify the inventory/equipment delta. It fails honestly if the item is not in inventory.",
        {
            "type": "object",
            "properties": {
                "item": {"type": "string"},
                "target": {
                    "type": "string",
                    "enum": ["auto", "mainhand", "offhand", "head", "chest", "legs", "feet"],
                    "default": "auto",
                },
            },
            "required": ["item"],
            "additionalProperties": False,
        },
        lambda params: inventory_txn.equip_item(
            item=str(params["item"]),
            target=str(params.get("target") or "auto"),
        ),
        ToolSidecar(
            "equip_item",
            mutating=True,
            source="body.inventory",
            tool_type="inventory",
            permission="equip",
            body_scope=("inventory",),
            terminal_truth=("inventory", "ToolResult"),
            timeout_s=10.0,
        ),
    )


def _smelt_tool(body: Body, furnace_txn: FurnaceTransactions) -> RegisteredTool:
    return RegisteredTool(
        "smelt_item",
        "Smelt items already in inventory using a nearby furnace, or a carried furnace placed temporarily and reclaimed. It auto-selects fuel from inventory unless fuel_item is provided. It fails honestly if the input has no smelting recipe, no fuel is owned, or no furnace is available or carried; it does not gather materials.",
        {
            "type": "object",
            "properties": {
                "input_item": {
                    "type": "string",
                    "description": "Input item to smelt, e.g. 'raw_iron'.",
                },
                "count": {"type": "integer", "minimum": 1, "maximum": 16},
                "fuel_item": {"type": "string"},
            },
            "required": ["input_item", "count"],
            "additionalProperties": False,
        },
        lambda params: _run_smelt_tool(body, furnace_txn, params),
        ToolSidecar(
            "smelt_item",
            mutating=True,
            source="body.furnace",
            tool_type="inventory",
            permission="smelt",
            body_scope=("inventory", "blocks"),
            terminal_truth=("inventory", "furnace", "ToolResult"),
            timeout_s=190.0,
        ),
    )


def _run_smelt_tool(body: Body, furnace_txn: FurnaceTransactions, params: dict[str, object]) -> ToolResult:
    input_item = str(params["input_item"])
    count = int(params.get("count") or 1)
    if count <= 0 or count > 16:
        return ToolResult(
            success=False,
            reason="invalid_smelt_count",
            can_retry=False,
            metrics={"input_item": input_item, "count": count},
        )

    output = resolve_smelt_output(input_item, lambda item: body.perceive("recipeData", {"item": item, "type": "smelting"}))
    if output is None:
        return ToolResult(
            success=False,
            reason="smelt_recipe_not_found",
            can_retry=False,
            next_suggestion="choose an input item with a furnace smelting recipe",
            metrics={"input_item": input_item, "count": count},
        )
    output_item, output_count_per_input = output
    output_count = output_count_per_input * count

    counts_result = _tool_inventory_counts(body)
    if isinstance(counts_result, ToolResult):
        return counts_result
    counts = counts_result

    fuel_item = str(params.get("fuel_item") or "")
    fuel_count: int | None = None
    if fuel_item:
        normalized_fuel = fuel_item.removeprefix("minecraft:")
        if counts.get(normalized_fuel, 0) <= 0:
            return ToolResult(
                success=False,
                reason="fuel_not_found",
                can_retry=False,
                next_suggestion="choose a fuel item currently present in inventory",
                metrics={"fuel_item": fuel_item, "counts": counts},
            )
    else:
        seconds_needed = max(count, output_count) * DEFAULT_SMELT_SECONDS_PER_ITEM
        selected = select_fuel(counts, seconds_needed)
        if selected is None:
            return ToolResult(
                success=False,
                reason="fuel_not_found",
                can_retry=False,
                next_suggestion="collect or craft a valid furnace fuel before smelting",
                metrics={
                    "input_item": input_item,
                    "count": count,
                    "seconds_needed": seconds_needed,
                    "usable_fuels": {
                        item: amount
                        for item, amount in counts.items()
                        if select_fuel({item: amount}, seconds_needed) is not None
                    },
                },
            )
        fuel_item, fuel_count = selected

    smelt_timeout_s = count * DEFAULT_SMELT_SECONDS_PER_ITEM + 10.0
    result = furnace_txn.smelt_nearest_furnace(
        input_item=input_item,
        input_count=count,
        fuel_item=fuel_item,
        fuel_count=fuel_count,
        output_item=output_item,
        output_count=output_count,
        smelt_timeout_s=smelt_timeout_s,
        transfer_timeout_s=6.0,
        approach_timeout_s=15.0,
    )
    if result.success or not result.can_retry or counts.get("furnace", 0) <= 0:
        return result
    temporary = furnace_txn.smelt_with_nearby_temporary_furnace(
        input_item=input_item,
        input_count=count,
        fuel_item=fuel_item,
        fuel_count=fuel_count,
        output_item=output_item,
        output_count=output_count,
        smelt_timeout_s=smelt_timeout_s,
        transfer_timeout_s=6.0,
        place_timeout_s=8.0,
        reclaim_timeout_s=8.0,
    )
    metrics = dict(temporary.metrics or {})
    metrics["nearest_furnace_result"] = result.to_payload()
    return ToolResult(
        temporary.success,
        temporary.reason,
        temporary.can_retry,
        temporary.next_suggestion,
        metrics=metrics,
    )


def _tool_inventory_counts(body: Body, *, page_size: int = 12) -> dict[str, int] | ToolResult:
    counts: dict[str, int] = {}
    start: int | None = 0
    saw_page = False
    while start is not None:
        perception = body.perceive("inventory", {"start": start, "limit": page_size})
        saw_page = True
        if not perception.ok:
            return ToolResult(
                success=False,
                reason="perception_failed",
                can_retry=True,
                metrics={"scope": "inventory", "error": perception.error, "uncertainty": list(perception.uncertainty)},
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
        if start is None and not perception.complete:
            return ToolResult(
                success=False,
                reason="perception_failed",
                can_retry=True,
                metrics={"scope": "inventory", "error": perception.error, "uncertainty": list(perception.uncertainty)},
            )
    if not saw_page:
        return ToolResult(False, "perception_failed", True, metrics={"scope": "inventory", "error": "no pages read"})
    return counts


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
                "max_pages": {"type": "integer"},
            },
            "required": ["block_types"],
            "additionalProperties": True,
        },
        lambda params: work.search_for_block(
            block_types=tuple(str(item) for item in params.get("block_types", [])),
            search_radius=min(max(1, int(params.get("search_radius") or 16)), 64),
            find_limit=int(params.get("find_limit") or 6),
            max_pages=min(max(1, int(params.get("max_pages") or 1)), 8),
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
            body_mutating=False,
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
                "target_block_types": {"type": "array", "items": {"type": "string"}},
                "dry": {"type": "boolean"},
            },
            "required": ["pos"],
            "additionalProperties": True,
        },
        lambda params: work.mine_block_collect(
            tuple(int(v) for v in params["pos"]),
            context=BreakContext.COLLECT,
            expected_drops=tuple(str(item) for item in params.get("expected_drops", [])),
            target_block_types=tuple(str(item) for item in params.get("target_block_types", [])),
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
