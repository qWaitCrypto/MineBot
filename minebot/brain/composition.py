"""Agent-layer composition tools for Phase 1.

These tools compose registered leaf tools through the registry/weld path. They
do not import Body transactions or game transport.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from minebot.brain.acquisition import AcquisitionError, AcquisitionStep, RecipeLookup, RecipeVariant, resolve_acquisition
from minebot.brain.modes import RuntimeProfile
from minebot.brain.registry import RegisteredTool, ToolRegistry, ToolSidecar, WeldContext, execute_tool
from minebot.contract.results import RECIPE_SOURCE_UNLOCKED_BOOK
from minebot.contract import (
    Body,
    InventorySlot,
    JsonObject,
    ToolResult,
    governance_confirmation_from,
    perception_next_cursor,
)
from minebot.contract.harvest import PICKAXE_BY_TIER, best_owned_pickaxe, required_pickaxe_tier, tier_satisfies
from minebot.contract.tool_trace import canonical_args_hash, tool_tactic_signature


@dataclass(frozen=True)
class CompositionBudget:
    max_candidates: int = 8
    max_mutating_calls: int = 8
    max_wall_s: float = 60.0


@dataclass
class CompositionContext:
    registry: ToolRegistry
    weld_context: WeldContext
    runtime_profile: RuntimeProfile
    budget: CompositionBudget
    recipe_lookup: RecipeLookup | None = None
    trace: Callable[[str, dict[str, object]], None] | None = None


@dataclass(frozen=True)
class ResourcePlan:
    requested_item: str
    inventory_item: str
    inventory_items: tuple[str, ...]
    block_types: tuple[str, ...]
    expected_drops: tuple[str, ...]


COMPOSITION_WORKSTATION_SEARCH_RADIUS = 64
LOG_RESOURCE_ITEMS = (
    "oak_log",
    "spruce_log",
    "birch_log",
    "jungle_log",
    "acacia_log",
    "dark_oak_log",
    "mangrove_log",
    "cherry_log",
    "pale_oak_log",
    "crimson_stem",
    "warped_stem",
)


def register_inventory_tools(registry: ToolRegistry, body: Body) -> None:
    registry.register(inventory_tool(body))


def inventory_tool(body: Body) -> RegisteredTool:
    return RegisteredTool(
        name="read_inventory",
        description="Read authoritative bot inventory counts.",
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        callable=lambda _params: _read_inventory_counts(body),
        sidecar=ToolSidecar(
            progress_key="read_inventory",
            mutating=False,
            source="body.perception",
            tool_type="state",
            permission="read_state",
            body_scope=("inventory",),
            terminal_truth=("inventory",),
        ),
    )


def register_collect_resource_tool(registry: ToolRegistry, context: CompositionContext) -> None:
    registry.register(collect_resource_tool(context))


def collect_resource_observation_projector(_reason: str, metrics: dict[str, object]) -> JsonObject:
    """Decision-grade, bounded summary for the model-visible collect-N tool."""
    summary: JsonObject = {
        key: metrics[key]
        for key in (
            "item",
            "requested_item",
            "target_count",
            "before_count",
            "after_count",
            "collected_delta",
            "remaining_count",
            "candidates_tried",
            "resume_hint",
        )
        if key in metrics
    }
    budget = metrics.get("budget")
    if isinstance(budget, dict):
        summary["budget"] = {
            key: budget[key]
            for key in ("max_candidates", "max_mutating_calls", "max_wall_s")
            if key in budget
        }

    body_process = metrics.get("body_process")
    body_metrics = body_process.get("metrics") if isinstance(body_process, dict) else None
    if isinstance(body_metrics, dict):
        for key in (
            "collected_total",
            "remaining_delta",
            "candidate_attempts_consumed",
            "candidate_budget_remaining",
            "navigation_fallback_attempts",
            "elapsed_ms",
        ):
            if key in body_metrics:
                summary[key] = body_metrics[key]
        resource_budget = body_metrics.get("budget")
        if isinstance(resource_budget, dict):
            summary["resource_budget"] = {
                key: resource_budget[key]
                for key in (
                    "candidate_budget",
                    "mutation_budget",
                    "max_wall_s",
                    "max_no_output_s",
                    "find_limit",
                    "max_pages",
                    "max_goals",
                )
                if key in resource_budget
            }
        navigation_failures = body_metrics.get("navigation_failure_reasons")
        if isinstance(navigation_failures, list):
            summary["navigation_failure_reasons"] = [
                str(value)[:160] for value in navigation_failures[:8]
            ]
            summary["navigation_failure_count"] = len(navigation_failures)
        searches = body_metrics.get("searches")
        if isinstance(searches, list):
            summary["search_count"] = len(searches)

    last_failure = metrics.get("last_failure")
    if last_failure is None and isinstance(body_metrics, dict):
        last_failure = body_metrics.get("last_failure")
    failure_summary = _collect_failure_summary(last_failure)
    if failure_summary:
        summary["last_failure"] = failure_summary
    return summary


def _collect_failure_summary(value: object) -> JsonObject:
    if not isinstance(value, dict):
        return {}
    summary: JsonObject = {
        key: value[key]
        for key in ("phase", "reason", "success", "canRetry", "nextSuggestion")
        if key in value and not isinstance(value[key], (dict, list, tuple))
    }
    reason_chain: list[str] = []

    def visit(node: object, depth: int) -> None:
        if depth > 5 or len(reason_chain) >= 8 or not isinstance(node, dict):
            return
        reason = node.get("reason")
        if isinstance(reason, str) and reason and reason not in reason_chain:
            reason_chain.append(reason[:160])
        for key in ("result", "last_failure", "metrics"):
            visit(node.get(key), depth + 1)

    visit(value, 0)
    if reason_chain:
        summary["reason_chain"] = reason_chain
    return summary


def ensure_tool_observation_projector(_reason: str, metrics: dict[str, object]) -> JsonObject:
    """Bounded acquisition-plan state for the next strategy decision."""
    summary: JsonObject = {
        key: metrics[key]
        for key in (
            "item",
            "target_count",
            "current_count",
            "remaining_count",
            "resume_hint",
            "governance_confirmation",
        )
        if key in metrics
    }
    plan = metrics.get("plan")
    if isinstance(plan, list):
        summary["plan_step_count"] = len(plan)
    completed = metrics.get("completed_steps")
    if isinstance(completed, list):
        summary["completed_step_count"] = len(completed)
    failed_step = _ensure_failed_step_summary(metrics.get("failed_step"))
    if failed_step:
        summary["failed_step"] = failed_step
    return summary


def _ensure_failed_step_summary(value: object) -> JsonObject:
    """Expose the decision-grade part of an interrupted acquisition step."""
    if not isinstance(value, dict):
        return {}
    summary = _collect_failure_summary(value)
    step = value.get("step")
    if isinstance(step, dict):
        step_summary = {
            key: step[key]
            for key in ("kind", "item", "count")
            if key in step
        }
        if step_summary:
            summary["step"] = step_summary
    result = value.get("result")
    if not isinstance(result, dict):
        return summary
    terminal = {
        key: result[key]
        for key in ("success", "reason", "canRetry", "nextSuggestion")
        if key in result
    }
    if terminal:
        summary["result"] = terminal
    result_metrics = result.get("metrics")
    if isinstance(step, dict) and step.get("kind") == "collect" and isinstance(result_metrics, dict):
        resource = collect_resource_observation_projector(
            str(result.get("reason") or ""), result_metrics
        )
        if resource:
            summary["resource"] = resource
    return summary


def collect_resource_tool(context: CompositionContext) -> RegisteredTool:
    return RegisteredTool(
        name="collect_resource",
        description=(
            "Collect a resource batch to the requested inventory total. Existing inventory counts toward the total; "
            "logs are a minimum: collection may finish the current tree and exceed the requested count. "
            "Request the amount needed for the next stage together, rather than issuing one call per block. "
            "item and count describe this stage and are never overridden by global goal wording. "
            "The tool handles discovery, local collection and inventory verification. When a recipe or goal accepts any "
            "equivalent material family, request "
            "the family name, e.g. item='logs' for any log/stem; use an exact species such as "
            "item='oak_log' only when that exact species is part of the goal or known recipe output."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "item": {
                    "type": "string",
                    "description": (
                        "Resource item or family. Use 'logs' for any log/stem when species is interchangeable; "
                        "use exact names like 'oak_log' only for exact-species objectives."
                    ),
                },
                "count": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Desired inventory total for this stage, not the number of additional items to collect.",
                },
                "avoid": {
                    "type": "array", "maxItems": 64,
                    "items": {"type": "array", "items": {"type": "integer"}, "minItems": 3, "maxItems": 3},
                    "description": "Block positions [x, y, z] you do not want collected, e.g. a block you judged out of reach.",
                },
                "retry_unreachable": {
                    "type": "boolean",
                    "description": (
                        "Targets that no path reached are skipped for the rest of the task and listed as "
                        "avoided_targets. Set true to try them again after changing how the Body can reach them."
                    ),
                },
            },
            "required": ["item", "count"],
            "additionalProperties": False,
        },
        callable=lambda params: collect_resource(params, context),
        sidecar=ToolSidecar(
            progress_key="collect_resource",
            mutating=False,
            source="agent.composition",
            tool_type="resource",
            permission="compose_collect",
            body_scope=("composition",),
            terminal_truth=("inventory", "ToolResult"),
            timeout_s=context.budget.max_wall_s,
            body_mutating=True,
        ),
        projector=collect_resource_observation_projector,
    )


def register_ensure_tool_for_tool(registry: ToolRegistry, context: CompositionContext, recipe_lookup: RecipeLookup) -> None:
    registry.register(ensure_tool_for_tool(context, recipe_lookup))


def ensure_tool_for_tool(
    context: CompositionContext,
    recipe_lookup: RecipeLookup,
) -> RegisteredTool:
    return RegisteredTool(
        name="ensure_tool_for",
        description=(
            "Acquire and equip a harvesting tool in one bounded call. This is the default for routine "
            "tool progression: it reads live inventory and recipes, collects prerequisites, crafts, and "
            "equips through deterministic steps without asking the model to issue each step. Pass either "
            "the resource to harvest (for example 'diamond') or the desired pickaxe (for example "
            "'stone_pickaxe'). It stops honestly at the first typed failure so the model can choose a new strategy."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "resource": {
                    "type": "string",
                    "description": "Resource/block to harvest, e.g. 'diamond', or item/tool to obtain directly, e.g. 'iron_pickaxe'.",
                }
            },
            "required": ["resource"],
            "additionalProperties": False,
        },
        callable=lambda params: ensure_tool_for(params, context, recipe_lookup),
        sidecar=ToolSidecar(
            progress_key="ensure_tool_for",
            mutating=False,
            source="agent.composition",
            tool_type="resource",
            permission="compose_ensure",
            body_scope=("composition",),
            terminal_truth=("inventory", "ToolResult"),
            timeout_s=900.0,
            body_mutating=True,
        ),
        projector=ensure_tool_observation_projector,
    )


def collect_resource(params: JsonObject, context: CompositionContext) -> ToolResult:
    item = _normalize_item(str(params.get("item") or ""))
    count = int(params.get("count") or 0)
    if not item:
        return ToolResult(False, "invalid_item", False, metrics={"item": params.get("item")})
    if count <= 0:
        return ToolResult(False, "invalid_count", False, metrics={"item": item, "target_count": count})

    constraints = params.get("constraints")
    constraints = constraints if isinstance(constraints, dict) else {}
    budget = _budget_from_constraints(context.budget, constraints)
    allow_dry = bool(constraints.get("allow_dry", False))
    plan = _resource_plan(item)
    radius = _resolve_search_radius(plan, constraints)

    before_result = _read_count(context, plan.inventory_items)
    if not before_result.success:
        return before_result
    before_count = int((before_result.metrics or {}).get("count") or 0)
    current_count = before_count
    if before_count >= count:
        _emit_collect_summary(
            context,
            reason="already_satisfied",
            success=True,
            plan=plan,
            target_count=count,
            before_count=before_count,
            current_count=current_count,
            attempts=[],
            skipped=[],
            last_failure=None,
        )
        return _collect_result(
            True,
            "already_satisfied",
            False,
            plan,
            count,
            before_count,
            current_count,
            [],
            [],
            "complete",
        )

    auto_prerequisites = bool(constraints.get("auto_prerequisites", True))
    if auto_prerequisites:
        prerequisite = _collect_prerequisite_tool(plan, _inventory_counts_from_result(before_result))
        if prerequisite is not None:
            ensured = ensure_item(
                {"item": prerequisite, "count": 1, "resource": plan.requested_item},
                context,
                _recipe_lookup_from_context(context),
            )
            if not ensured.success:
                ensure_metrics = {
                    "item": plan.inventory_item,
                    "requested_item": plan.requested_item,
                    "required_tool": prerequisite,
                    "ensure_result": ensured.to_payload(),
                    "resume_hint": (ensured.metrics or {}).get("resume_hint", "reinvoke_ensure"),
                }
                confirmation = governance_confirmation_from(ensured.to_payload())
                if confirmation is not None:
                    ensure_metrics["governance_confirmation"] = confirmation
                return ToolResult(
                    False,
                    ensured.reason,
                    ensured.can_retry,
                    ensured.next_suggestion,
                    metrics=ensure_metrics,
                )
            before_result = _read_count(context, plan.inventory_items)
            if not before_result.success:
                return before_result
            before_count = int((before_result.metrics or {}).get("count") or 0)
            current_count = before_count
            if before_count >= count:
                return _collect_result(
                    True,
                    "already_satisfied",
                    False,
                    plan,
                    count,
                    before_count,
                    current_count,
                    [],
                    [],
                    "complete",
                )

    remaining_count = max(1, count - current_count)
    find_limit = min(
        12,
        max(6, remaining_count + 2, min(budget.max_candidates, radius // 2 if radius > 1 else 1)),
    )
    max_pages = _search_max_pages_for_budget(budget, find_limit)
    body_process = _execute_composition_phase(
        context,
        "collect_domain",
        context.registry.get("collect_block_domain"),
        {
            "block_types": list(plan.block_types),
            "expected_drops": list(plan.expected_drops),
            "remaining_count": remaining_count,
            "search_radius": radius,
            "candidate_budget": budget.max_candidates,
            "mutation_budget": budget.max_mutating_calls,
            "max_wall_s": budget.max_wall_s,
            "find_limit": find_limit,
            "max_pages": max_pages,
            "segment_timeout_s": float(constraints.get("segment_timeout_s") or 15.0),
            "dry": allow_dry,
            "avoid": [list(pos) for pos in params.get("avoid") or ()],
            "retry_unreachable": bool(params.get("retry_unreachable", False)),
        },
    )
    _emit_trace(
        context,
        "composition_collect_domain",
        {
            "item": plan.requested_item,
            "block_types": list(plan.block_types),
            "remaining_count": remaining_count,
            "success": bool(body_process.get("success")),
            "reason": str(body_process.get("reason") or ""),
        },
    )

    after_result = _read_count(context, plan.inventory_items)
    if not after_result.success:
        return after_result
    current_count = int((after_result.metrics or {}).get("count") or 0)
    body_metrics = body_process.get("metrics")
    body_metrics = body_metrics if isinstance(body_metrics, dict) else {}
    attempts = [
        dict(attempt)
        for attempt in body_metrics.get("attempts") or []
        if isinstance(attempt, dict)
    ]
    skipped = _resource_process_skips(body_metrics)
    body_reason = str(body_process.get("reason") or "resource_process_failed")
    complete = current_count >= count
    if body_metrics.get("collection_strategy") == "native_resource_collection":
        complete = complete and bool(body_process.get("success"))

    if complete:
        result_reason = "collected"
        result_success = True
        can_retry = False
        resume_hint = "complete"
        next_suggestion = None
        last_failure = None
    else:
        result_reason = _resource_process_reason(body_reason, before_count=before_count, current_count=current_count)
        # Inventory progress is useful resume evidence, but it does not satisfy
        # a bounded collect objective until the requested terminal count is met.
        # Keep the Body failure honest so callers cannot treat partial progress
        # as a completed capability and move on to dependent steps.
        result_success = False
        can_retry = bool(body_process.get("canRetry", True))
        resume_hint = _resource_resume_hint(body_reason)
        next_suggestion = _tool_result_next_suggestion(body_process)
        last_failure = {
            "phase": "collect_domain",
            "reason": body_reason,
            "result": body_process,
        }

    _emit_collect_summary(
        context,
        reason=result_reason,
        success=result_success,
        plan=plan,
        target_count=count,
        before_count=before_count,
        current_count=current_count,
        attempts=attempts,
        skipped=skipped,
        last_failure=last_failure,
    )
    result = _collect_result(
        result_success,
        result_reason,
        can_retry,
        plan,
        count,
        before_count,
        current_count,
        attempts,
        skipped,
        resume_hint,
        next_suggestion=next_suggestion,
        last_failure=last_failure,
        budget=budget,
    )
    metrics = dict(result.metrics or {})
    metrics["body_process"] = body_process
    confirmation = governance_confirmation_from(body_process)
    if confirmation is not None:
        metrics["governance_confirmation"] = confirmation
    # Why the Body stopped is small and decisive; keep it where the Agent reads
    # it instead of only inside the nested process record.
    process_metrics = body_process.get("metrics") if isinstance(body_process, dict) else None
    stalled = process_metrics.get("stalled") if isinstance(process_metrics, dict) else None
    process_suggestion = body_process.get("nextSuggestion") if isinstance(body_process, dict) else None
    if isinstance(stalled, dict):
        metrics["stalled"] = stalled
    if isinstance(process_metrics, dict):
        for key in ("avoided_targets", "avoided_target_count"):
            if key in process_metrics:
                metrics[key] = process_metrics[key]
    return ToolResult(
        result.success,
        result.reason,
        result.can_retry,
        result.next_suggestion or (process_suggestion if isinstance(stalled, dict) else None),
        metrics=metrics,
    )
def ensure_tool_for(params: JsonObject, context: CompositionContext, recipe_lookup: RecipeLookup) -> ToolResult:
    resource = _normalize_item(str(params.get("resource") or ""))
    if not resource:
        return ToolResult(False, "invalid_resource", False, metrics={"resource": params.get("resource")})
    target = _ensure_target_for(resource)
    return ensure_item(
        {"item": target, "count": 1, "resource": resource},
        context,
        recipe_lookup,
        equip_target=target in PICKAXE_BY_TIER.values(),
    )


def ensure_item(
    params: JsonObject,
    context: CompositionContext,
    recipe_lookup: RecipeLookup,
    *,
    equip_target: bool = False,
) -> ToolResult:
    item = _normalize_item(str(params.get("item") or params.get("resource") or ""))
    count = int(params.get("count") or 1)
    if not item:
        return ToolResult(False, "invalid_item", False, metrics={"item": params.get("item")})
    if count <= 0:
        return ToolResult(False, "invalid_count", False, metrics={"item": item, "target_count": count})

    started = time.monotonic()
    completed_steps: list[dict[str, object]] = []
    step_payloads: list[dict[str, object]] = []
    while True:
        if step_payloads and time.monotonic() - started > context.budget.max_wall_s:
            return _ensure_result(
                False,
                "partial_budget_exhausted",
                True,
                item,
                count,
                plan=step_payloads,
                completed_steps=completed_steps,
                failed_step={"index": len(step_payloads), "phase": "replan", "reason": "max_wall_s"},
                resume_hint="reinvoke_ensure",
            )
        inventory = _read_count(context, (item,))
        if not inventory.success:
            return inventory
        counts = _inventory_counts_from_result(inventory)
        plan = resolve_acquisition(item, count, counts, recipe_lookup, max_depth=18)
        if isinstance(plan, AcquisitionError):
            error = _acquisition_error_payload(plan)
            return ToolResult(
                False,
                plan.reason,
                False,
                next_suggestion=(
                    "The recipe for the missing item is not unlocked on this Body yet, so the chain cannot "
                    "be expanded from here. Look the recipe up in the wiki, obtain its ingredients (which "
                    "unlocks it), then retry."
                    if plan.detail.get("recipe_source") == RECIPE_SOURCE_UNLOCKED_BOOK
                    else "choose a reachable item/resource or add a recipe/acquisition route"
                ),
                metrics={
                    "item": item,
                    "target_count": count,
                    "error": error,
                    "plan": step_payloads,
                    "completed_steps": completed_steps,
                    "failed_step": {"index": len(step_payloads), "phase": "planning", "error": error},
                    "resume_hint": "reinvoke_ensure",
                },
            )

        replan_after = any(bool(step.detail.get("replan_after")) for step in plan)
        if equip_target and not replan_after:
            plan = [
                *plan,
                AcquisitionStep(
                    kind="equip",
                    item=item,
                    count=1,
                    detail={"target": "mainhand", "requested_by": "ensure_tool_for"},
                ),
            ]
        if not plan:
            reason = "ensured" if completed_steps else "already_satisfied"
            return _ensure_result(
                True,
                reason,
                False,
                item,
                count,
                plan=step_payloads,
                completed_steps=completed_steps,
                current_count=int((inventory.metrics or {}).get("count") or 0),
                resume_hint="complete",
            )

        plan_offset = len(step_payloads)
        step_payloads.extend(_acquisition_step_payload(step) for step in plan)
        last_table_craft_index = _last_table_craft_index(plan)
        should_replan = False
        for index, step in enumerate(plan):
            evidence_index = plan_offset + index
            if time.monotonic() - started > context.budget.max_wall_s:
                return _ensure_result(
                    False,
                    "partial_budget_exhausted",
                    True,
                    item,
                    count,
                    plan=step_payloads,
                    completed_steps=completed_steps,
                    failed_step={"index": evidence_index, "step": _acquisition_step_payload(step), "reason": "max_wall_s"},
                    resume_hint="reinvoke_ensure",
                )
            before_step_count = _acquisition_step_inventory_count(context, step)
            if before_step_count is None:
                failed_step = {
                    "index": evidence_index,
                    "step": _acquisition_step_payload(step),
                    "reason": "inventory_read_failed_before_step",
                }
                return _ensure_result(
                    False,
                    "ensure_step_failed:inventory_read_failed",
                    True,
                    item,
                    count,
                    plan=step_payloads,
                    completed_steps=completed_steps,
                    failed_step=failed_step,
                    resume_hint="reinvoke_ensure",
                )
            result = _execute_acquisition_step(
                context,
                step,
                before_count=before_step_count,
                keep_workstation=_should_keep_workstation(plan, index, last_table_craft_index),
                cleanup_workstation=_should_cleanup_workstation(plan, index, last_table_craft_index),
            )
            step_record = {"index": evidence_index, "step": _acquisition_step_payload(step), "result": result}
            if not result.get("success"):
                return _ensure_result(
                    False,
                    f"ensure_step_failed:{result.get('reason') or 'unknown'}",
                    bool(result.get("canRetry", True)),
                    item,
                    count,
                    plan=step_payloads,
                    completed_steps=completed_steps,
                    failed_step=step_record,
                    resume_hint="reinvoke_ensure",
                )
            step_count = _acquisition_step_inventory_count(context, step)
            if step_count is None:
                return _ensure_result(
                    False,
                    "ensure_step_failed:inventory_read_failed",
                    True,
                    item,
                    count,
                    plan=step_payloads,
                    completed_steps=completed_steps,
                    failed_step=step_record,
                    resume_hint="reinvoke_ensure",
                )
            required_step_count = before_step_count if step.kind == "equip" else before_step_count + step.count
            if step_count < required_step_count:
                step_record = dict(step_record)
                step_record["before_count"] = before_step_count
                step_record["current_count"] = step_count
                step_record["required_count"] = required_step_count
                step_record["remaining_count"] = max(0, required_step_count - step_count)
                return _ensure_result(
                    False,
                    "ensure_step_incomplete",
                    True,
                    item,
                    count,
                    plan=step_payloads,
                    completed_steps=completed_steps,
                    failed_step=step_record,
                    resume_hint="reinvoke_ensure",
                )
            completed_steps.append(step_record)
            if bool(step.detail.get("replan_after")):
                should_replan = True
                break

        if should_replan:
            continue

        inventory = _read_count(context, (item,))
        if not inventory.success:
            return inventory
        current = int((inventory.metrics or {}).get("count") or 0)
        return _ensure_result(
            current >= count,
            "ensured" if current >= count else "ensure_no_inventory_delta",
            current < count,
            item,
            count,
            plan=step_payloads,
            completed_steps=completed_steps,
            current_count=current,
            resume_hint="complete" if current >= count else "reinvoke_ensure",
        )


def _budget_from_constraints(default: CompositionBudget, constraints: dict[str, object]) -> CompositionBudget:
    return CompositionBudget(
        max_candidates=int(constraints.get("max_candidates") or default.max_candidates),
        max_mutating_calls=int(constraints.get("max_mutating_calls") or default.max_mutating_calls),
        max_wall_s=float(constraints.get("max_wall_s") or default.max_wall_s),
    )


def _search_max_pages_for_budget(budget: CompositionBudget, find_limit: int) -> int:
    # Each page is still a bounded RCON payload; composition can ask for a few
    # pages so "this candidate page is exhausted" does not masquerade as "all
    # resource targets are exhausted" in dense forests.
    per_page = max(1, find_limit)
    requested_pages = (max(1, budget.max_candidates) + per_page - 1) // per_page
    return min(8, max(1, requested_pages))


def _execute_composition_phase(
    context: CompositionContext,
    phase: str,
    tool: RegisteredTool,
    tool_input: JsonObject,
) -> JsonObject:
    _emit_tool_invoke(context, phase, tool, tool_input)
    try:
        result = execute_tool(tool, tool_input, context.weld_context)
    except Exception as exc:
        _emit_trace(
            context,
            "composition_phase_exception",
            {
                "phase": phase,
                "tool": tool.name,
                "error_type": type(exc).__name__,
                "message": str(exc),
                "recent_requests": _recent_body_requests(context.weld_context.body),
            },
        )
        raise
    summary = _summarize_composition_result(result)
    _emit_trace(
        context,
        "composition_tool_result",
        {
            "phase": phase,
            "tool": tool.name,
            "success": bool(result.get("success", False)),
            "reason": str(result.get("reason") or ""),
            "canRetry": bool(result.get("canRetry", False)),
            "summary": summary,
        },
    )
    return result


def _emit_tool_invoke(
    context: CompositionContext,
    phase: str,
    tool: RegisteredTool,
    tool_input: JsonObject,
) -> None:
    args_hash = canonical_args_hash(tool_input)
    tactic_signature = tool_tactic_signature(tool.name, tool_input)
    _emit_trace(
        context,
        "tool_invoke",
        {
            "tool_call_id": f"composition-{phase}-{tool.name}",
            "tool": tool.name,
            "source": tool.sidecar.source,
            "tool_type": tool.sidecar.tool_type,
            "mutating": tool.sidecar.mutating,
            "permission": tool.sidecar.permission,
            "body_scope": list(tool.sidecar.body_scope),
            "terminal_truth": list(tool.sidecar.terminal_truth),
            "situational": context.runtime_profile.situational,
            "lifecycle": context.runtime_profile.lifecycle,
            "args_hash": args_hash,
            "tactic_signature": tactic_signature,
            "driver": f"composition:{phase}",
            "arguments_summary": _summarize_composition_arguments(tool_input),
        },
    )


def _summarize_composition_arguments(tool_input: JsonObject) -> dict[str, object]:
    summary: dict[str, object] = {}
    for key in (
        "item",
        "count",
        "input_item",
        "resource",
        "target",
        "pos",
        "search_radius",
        "keep_temporary_table",
        "cleanup_existing_bot_table",
    ):
        if key in tool_input:
            summary[key] = tool_input[key]
    constraints = tool_input.get("constraints")
    if isinstance(constraints, dict):
        summary["constraints"] = {
            key: constraints[key]
            for key in ("auto_prerequisites", "max_candidates", "max_mutating_calls", "max_wall_s")
            if key in constraints
        }
    return summary


def _summarize_composition_result(result: JsonObject) -> dict[str, object]:
    summary: dict[str, object] = {}
    for key in ("success", "reason", "canRetry", "nextSuggestion"):
        if key in result:
            summary[key] = result[key]
    metrics = result.get("metrics")
    if isinstance(metrics, dict):
        selected = _summarize_composition_metrics(metrics)
        if selected:
            summary["metrics"] = selected
    return summary


def _summarize_composition_metrics(metrics: dict[str, object]) -> dict[str, object]:
    selected: dict[str, object] = {}
    for key in (
        "item",
        "input_item",
        "fuel_item",
        "output_item",
        "count",
        "target_count",
        "before_count",
        "after_count",
        "current_count",
        "remaining_count",
        "collected_delta",
        "resume_hint",
        "temporary_furnace_site",
        "furnace_pos",
        "seconds_needed",
        "governance_confirmation",
    ):
        if key in metrics:
            selected[key] = metrics[key]
    for key in ("input", "fuel", "output", "usable_fuels"):
        value = metrics.get(key)
        if isinstance(value, dict):
            selected[key] = value
    for key in ("nearest_furnace_result", "place", "smelt", "reclaim_select"):
        value = metrics.get(key)
        if isinstance(value, dict):
            selected[key] = _compact_tool_result_payload(value)
    reclaim = metrics.get("reclaim")
    if isinstance(reclaim, dict):
        selected["reclaim"] = _compact_tool_result_payload(reclaim)
    elif isinstance(reclaim, list):
        selected["reclaim"] = _compact_reclaim_entries(reclaim)
    return selected


def _compact_tool_result_payload(payload: dict[str, object]) -> dict[str, object]:
    out: dict[str, object] = {}
    for key in ("success", "reason", "canRetry", "nextSuggestion"):
        if key in payload:
            out[key] = payload[key]
    metrics = payload.get("metrics")
    if isinstance(metrics, dict):
        selected = _summarize_composition_metrics(metrics)
        if selected:
            out["metrics"] = selected
    return out


def _compact_reclaim_entries(entries: list[object]) -> dict[str, object]:
    tail: list[dict[str, object]] = []
    for entry in entries[-3:]:
        if not isinstance(entry, dict):
            continue
        compact = {
            key: entry[key]
            for key in ("furnace_slot", "success", "reason")
            if key in entry
        }
        result = entry.get("result")
        if isinstance(result, dict):
            compact["result"] = _compact_tool_result_payload(result)
        tail.append(compact)
    return {"count": len(entries), "tail": tail}


def _recent_body_requests(body: Body, *, limit: int = 6) -> list[dict[str, object]]:
    history = getattr(body, "request_history", None)
    if not isinstance(history, list):
        return []
    out: list[dict[str, object]] = []
    for entry in history[-limit:]:
        if not isinstance(entry, dict):
            continue
        out.append(
            {
                key: entry[key]
                for key in (
                    "kind",
                    "scope",
                    "ok",
                    "elapsed_ms",
                    "command_len",
                    "error_type",
                    "error",
                    "action_name",
                    "action_id",
                )
                if key in entry
            }
        )
    return out


def _resolve_search_radius(plan: ResourcePlan, constraints: dict[str, object]) -> int:
    requested = constraints.get("radius")
    radius = int(requested) if requested is not None else _default_search_radius(plan)
    return min(max(1, radius), _max_search_radius(plan))


def _read_count(context: CompositionContext, items: tuple[str, ...]) -> ToolResult:
    try:
        payload = context.registry.get("read_inventory").callable({}).to_payload()
    except Exception as exc:
        _emit_trace(
            context,
            "composition_phase_exception",
            {
                "phase": "inventory",
                "tool": "read_inventory",
                "error_type": type(exc).__name__,
                "message": str(exc),
                "recent_requests": _recent_body_requests(context.weld_context.body),
            },
        )
        raise
    if not payload.get("success"):
        return ToolResult(
            False,
            f"inventory_read_failed:{payload.get('reason')}",
            True,
            metrics={"items": list(items), "inventory_result": payload},
        )
    counts = payload.get("metrics", {}).get("counts") if isinstance(payload.get("metrics"), dict) else {}
    safe_counts = counts or {}
    total = sum(int(safe_counts.get(item, 0)) for item in items)
    return ToolResult(
        True,
        "inventory_counted",
        False,
        metrics={
            "item": items[0] if len(items) == 1 else "equivalent_items",
            "items": list(items),
            "count": total,
            "counts": safe_counts,
        },
    )


def _resource_plan(item: str) -> ResourcePlan:
    item = _normalize_item(item)
    aliases: dict[str, tuple[str, tuple[str, ...], tuple[str, ...]]] = {
        "log": (
            "oak_log",
            LOG_RESOURCE_ITEMS,
            LOG_RESOURCE_ITEMS,
        ),
        "logs": (
            "oak_log",
            LOG_RESOURCE_ITEMS,
            LOG_RESOURCE_ITEMS,
        ),
        "coal": ("coal", ("coal_ore", "deepslate_coal_ore"), ("coal",)),
        "iron": ("raw_iron", ("iron_ore", "deepslate_iron_ore"), ("raw_iron",)),
        "raw_iron": ("raw_iron", ("iron_ore", "deepslate_iron_ore"), ("raw_iron",)),
        "gold": ("raw_gold", ("gold_ore", "deepslate_gold_ore"), ("raw_gold",)),
        "raw_gold": ("raw_gold", ("gold_ore", "deepslate_gold_ore"), ("raw_gold",)),
        "stone": ("cobblestone", ("stone", "cobblestone"), ("cobblestone",)),
        "cobblestone": ("cobblestone", ("stone", "cobblestone"), ("cobblestone",)),
        "diamond": ("diamond", ("diamond_ore", "deepslate_diamond_ore"), ("diamond",)),
        "dirt": ("dirt", ("dirt", "grass_block", "coarse_dirt", "rooted_dirt"), ("dirt",)),
    }
    mapped = aliases.get(item)
    if mapped is not None:
        inventory_item, block_types, expected_drops = mapped
        return ResourcePlan(
            requested_item=item,
            inventory_item=inventory_item,
            inventory_items=tuple(_normalize_item(drop) for drop in expected_drops),
            block_types=block_types,
            expected_drops=expected_drops,
        )
    return ResourcePlan(requested_item=item, inventory_item=item, inventory_items=(item,), block_types=(item,), expected_drops=(item,))


def resource_plan_for(item: str) -> ResourcePlan:
    return _resource_plan(item)


def _default_search_radius(plan: ResourcePlan) -> int:
    if _is_tree_resource_plan(plan):
        return 48
    return 16


def _max_search_radius(plan: ResourcePlan) -> int:
    if _is_tree_resource_plan(plan):
        return 64
    return 48


def _is_tree_resource_plan(plan: ResourcePlan) -> bool:
    return plan.requested_item in {"log", "logs"} or any(
        _is_log_like_item(block_type) for block_type in plan.block_types
    )


def _read_inventory_counts(body: Body, *, page_size: int = 12) -> ToolResult:
    start: int | None = 0
    slots: list[dict[str, object]] = []
    perception = None
    while start is not None:
        perception = body.perceive("inventory", {"start": start, "limit": page_size})
        if not perception.ok:
            break
        slots.extend(dict(slot) for slot in perception.data.get("slots") or [])
        next_start = _next_start(perception)
        start = int(next_start) if next_start is not None else None
    if perception is None:
        return ToolResult(False, "perception_failed", True, metrics={"scope": "inventory", "error": "no pages read"})
    if not perception.ok or not perception.complete:
        return ToolResult(
            False,
            "perception_failed",
            True,
            metrics={
                "scope": "inventory",
                "error": perception.error,
                "uncertainty": list(perception.uncertainty or ()),
            },
        )
    counts: dict[str, int] = {}
    for payload in slots:
        slot = InventorySlot.from_payload(payload)
        if slot.empty or not slot.item:
            continue
        item = _normalize_item(slot.item)
        counts[item] = counts.get(item, 0) + slot.count
    return ToolResult(True, "inventory_counted", False, metrics={"counts": counts})


def _emit_collect_summary(
    context: CompositionContext,
    *,
    reason: str,
    success: bool,
    plan: ResourcePlan,
    target_count: int,
    before_count: int,
    current_count: int,
    attempts: list[dict[str, object]],
    skipped: list[dict[str, object]],
    last_failure: dict[str, object] | None,
) -> None:
    payload: dict[str, object] = {
        "item": plan.requested_item,
        "inventory_items": list(plan.inventory_items),
        "success": success,
        "reason": reason,
        "target_count": target_count,
        "before_count": before_count,
        "current_count": current_count,
        "collected_delta": max(0, current_count - before_count),
        "remaining_count": max(0, target_count - current_count),
        "attempt_count": len(attempts),
        "skip_count": len(skipped),
        "skip_reasons": _reason_counts(skipped),
        "pre_approach_result_reasons": _attempt_reason_counts(attempts, "pre_approach"),
        "mine_result_reasons": _attempt_reason_counts(attempts, "mine"),
        "search_result_reasons": _attempt_reason_counts(attempts, "search"),
        "blocked_clearance": _blocked_clearance_summary(attempts),
    }
    if last_failure is not None:
        payload["last_failure"] = _last_failure_summary(last_failure)
    _emit_trace(context, "composition_summary", payload)


def _reason_counts(items: list[dict[str, object]], *, limit: int = 8) -> list[dict[str, object]]:
    counts: dict[str, int] = {}
    for item in items:
        reason = str(item.get("reason") or "")
        if reason:
            counts[reason] = counts.get(reason, 0) + 1
    return [
        {"reason": reason, "count": count}
        for reason, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))[:limit]
    ]


def _attempt_reason_counts(
    attempts: list[dict[str, object]],
    key: str,
    *,
    limit: int = 8,
) -> list[dict[str, object]]:
    counts: dict[str, int] = {}
    for attempt in attempts:
        result = attempt.get(key)
        if not isinstance(result, dict):
            continue
        reason = str(result.get("reason") or "")
        if reason:
            counts[reason] = counts.get(reason, 0) + 1
    return [
        {"reason": reason, "count": count}
        for reason, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))[:limit]
    ]


def _blocked_clearance_summary(
    attempts: list[dict[str, object]],
    *,
    limit: int = 8,
) -> list[dict[str, object]]:
    counts: dict[tuple[str, str], dict[str, object]] = {}
    for attempt in attempts:
        result = attempt.get("mine")
        if not isinstance(result, dict):
            continue
        metrics = result.get("metrics")
        if not isinstance(metrics, dict):
            continue
        clearance = metrics.get("clearance")
        if not isinstance(clearance, dict):
            continue
        clearance_metrics = clearance.get("metrics")
        if not isinstance(clearance_metrics, dict):
            continue
        block_type = str(clearance_metrics.get("block_type") or "unknown")
        legality = clearance_metrics.get("legality")
        legality_reason = "unknown"
        if isinstance(legality, dict):
            legality_reason = str(legality.get("reason") or legality_reason)
        key = (block_type, legality_reason)
        row = counts.setdefault(
            key,
            {
                "block_type": block_type,
                "legality_reason": legality_reason,
                "count": 0,
                "sample_targets": [],
                "sample_stand_blocks": [],
            },
        )
        row["count"] = int(row["count"]) + 1
        target = clearance_metrics.get("target")
        if isinstance(target, list) and len(row["sample_targets"]) < 3:
            row["sample_targets"].append(target)
        approach = clearance_metrics.get("collect_approach_clearance")
        if isinstance(approach, dict):
            stand_block = approach.get("stand_block")
            if isinstance(stand_block, list) and len(row["sample_stand_blocks"]) < 3:
                row["sample_stand_blocks"].append(stand_block)
    return sorted(counts.values(), key=lambda item: (-int(item["count"]), str(item["block_type"])))[:limit]


def _last_failure_summary(last_failure: dict[str, object]) -> dict[str, object]:
    summary: dict[str, object] = {}
    for key in ("phase", "target", "reason"):
        if key in last_failure:
            summary[key] = last_failure[key]
    result = last_failure.get("result")
    if isinstance(result, dict):
        summary["result"] = _tool_result_diagnostics(result)
    return summary


def _tool_result_diagnostics(result: dict[str, Any]) -> dict[str, object]:
    out: dict[str, object] = {}
    for key in ("success", "reason", "canRetry", "nextSuggestion"):
        if key in result:
            out[key] = result[key]
    metrics = result.get("metrics")
    if isinstance(metrics, dict):
        selected: dict[str, object] = {}
        for key in (
            "target",
            "block_type",
            "goal",
            "goal_dist",
            "reflex_handoff",
            "state_after",
            "reach_distance",
            "terrain_fallback_original_reason",
            "pre_approach_reached",
            "pre_approach_raw_reason",
            "pre_approach_required_radius",
        ):
            if key in metrics:
                selected[key] = metrics[key]
        segments = metrics.get("segments")
        if isinstance(segments, list):
            selected["segments"] = [
                _segment_diagnostics(segment)
                for segment in segments[-3:]
                if isinstance(segment, dict)
            ]
            selected["segment_count"] = metrics.get("segment_count", len(segments))
        if selected:
            out["metrics"] = selected
    return out


def _tool_result_next_suggestion(result: dict[str, object], *, _depth: int = 0) -> str | None:
    """Keep the first actionable suggestion across composed result envelopes.

    Body processes commonly put the actionable failure one level below the
    outer result (``metrics.last_failure``).  A composition tool must not turn
    that useful contract field into ``null`` merely because it rewrites the
    terminal reason.  The depth bound keeps this projection defensive if a
    malformed result contains a recursive-looking payload.
    """

    if _depth > 3:
        return None
    direct = result.get("nextSuggestion")
    if isinstance(direct, str) and direct.strip():
        return direct
    metrics = result.get("metrics")
    if not isinstance(metrics, dict):
        return None
    for key in ("last_failure", "body_process", "result"):
        nested = metrics.get(key)
        if isinstance(nested, dict):
            suggestion = _tool_result_next_suggestion(nested, _depth=_depth + 1)
            if suggestion:
                return suggestion
    return None


def _segment_diagnostics(segment: dict[str, Any]) -> dict[str, object]:
    out: dict[str, object] = {}
    for key in ("index", "status", "terminal_reason", "success", "target"):
        if key in segment:
            out[key] = segment[key]
    diagnostics = segment.get("diagnostics")
    if isinstance(diagnostics, dict):
        selected = {
            key: diagnostics[key]
            for key in ("event", "raw_reason", "goal_dist", "reflex_handoff")
            if key in diagnostics
        }
        event_data = diagnostics.get("event_data")
        if isinstance(event_data, dict):
            selected["event_data"] = {
                key: event_data[key]
                for key in ("previous_owner", "new_owner", "reason", "nav_reason", "stopped_reason")
                if key in event_data
            }
        if selected:
            out["diagnostics"] = selected
    return out


def _is_log_like_item(item: str) -> bool:
    normalized = _normalize_item(item)
    return normalized.endswith("_log") or normalized.endswith("_stem")


def _parse_pos(value: object) -> list[int] | None:
    if not isinstance(value, list) or len(value) != 3:
        return None
    return [int(value[0]), int(value[1]), int(value[2])]


def _resource_process_reason(reason: str, *, before_count: int, current_count: int) -> str:
    if reason == "resource_navigation_no_path":
        return reason
    if reason == "resource_candidates_not_found":
        return "partial_candidate_targets_exhausted" if current_count > before_count else "target_not_found"
    if reason in {"resource_candidate_domain_exhausted", "resource_domain_partial_exhausted"}:
        return "partial_candidate_targets_exhausted" if current_count > before_count else "candidate_targets_exhausted"
    if reason == "resource_domain_budget_exhausted":
        return "partial_budget_exhausted"
    if reason == "resource_domain_collected":
        return "collect_inventory_target_not_met"
    return reason


def _resource_resume_hint(reason: str) -> str:
    if reason in {
        "resource_candidates_not_found",
        "resource_candidate_domain_exhausted",
        "resource_domain_partial_exhausted",
    }:
        return "reselect_candidates"
    if reason == "resource_output_window_exhausted":
        return "reconsider_or_resume_collect"
    if "budget" in reason:
        return "budget_exhausted"
    if "timeout" in reason:
        return "timeout"
    return "body_terminal"


def _resource_process_skips(metrics: dict[str, object]) -> list[dict[str, object]]:
    skipped: list[dict[str, object]] = []
    seen: set[tuple[int, int, int]] = set()
    attempts = metrics.get("attempts")
    if isinstance(attempts, list):
        for attempt in attempts:
            if not isinstance(attempt, dict):
                continue
            target = _parse_pos(attempt.get("target"))
            mined = attempt.get("mine")
            navigation = attempt.get("navigation")
            reason = None
            if isinstance(mined, dict) and mined.get("success") is not True:
                reason = str(mined.get("reason") or "resource_candidate_rejected")
            elif isinstance(navigation, dict) and navigation.get("success") is not True:
                reason = f"resource_navigation_{navigation.get('reason') or 'failed'}"
            if target is None or reason is None:
                continue
            key = tuple(target)
            if key in seen:
                continue
            seen.add(key)
            skipped.append({"pos": target, "reason": reason, "skip": True})

    blacklisted = metrics.get("candidate_blacklist")
    if isinstance(blacklisted, list):
        for raw in blacklisted:
            pos = _parse_pos(raw)
            if pos is None or tuple(pos) in seen:
                continue
            seen.add(tuple(pos))
            skipped.append({"pos": pos, "reason": "body_candidate_blacklist", "skip": True})
    return skipped


def _collect_result(
    success: bool,
    reason: str,
    can_retry: bool,
    plan: ResourcePlan,
    target_count: int,
    before_count: int,
    after_count: int,
    attempts: list[dict[str, object]],
    skipped: list[dict[str, object]],
    resume_hint: str,
    *,
    next_suggestion: str | None = None,
    last_failure: dict[str, object] | None = None,
    budget: CompositionBudget | None = None,
) -> ToolResult:
    metrics: dict[str, object] = {
        "item": plan.inventory_item,
        "requested_item": plan.requested_item,
        "block_types": list(plan.block_types),
        "expected_drops": list(plan.expected_drops),
        "target_count": target_count,
        "before_count": before_count,
        "after_count": after_count,
        "collected_delta": max(0, after_count - before_count),
        "remaining_count": max(0, target_count - after_count),
        "complete": after_count >= target_count,
        "candidates_tried": len(attempts),
        "attempts": attempts,
        "skipped": skipped,
        "resume_hint": resume_hint,
    }
    if last_failure is not None:
        metrics["last_failure"] = last_failure
    if budget is not None:
        metrics["budget"] = {
            "max_candidates": budget.max_candidates,
            "max_mutating_calls": budget.max_mutating_calls,
            "max_wall_s": budget.max_wall_s,
        }
    return ToolResult(success, reason, can_retry, next_suggestion=next_suggestion, metrics=metrics)


def _ensure_target_for(resource: str) -> str:
    plan = _resource_plan(resource)
    required_tier = None
    for block_type in plan.block_types:
        candidate = required_pickaxe_tier(block_type)
        if candidate is not None and (required_tier is None or not tier_satisfies(required_tier, candidate)):
            required_tier = candidate
    if required_tier is not None:
        return PICKAXE_BY_TIER[required_tier]
    return resource


def _collect_prerequisite_tool(plan: ResourcePlan, counts: dict[str, int]) -> str | None:
    required_tier = None
    for block_type in plan.block_types:
        candidate = required_pickaxe_tier(block_type)
        if candidate is not None and (required_tier is None or not tier_satisfies(required_tier, candidate)):
            required_tier = candidate
    if required_tier is None:
        return None
    best = best_owned_pickaxe(counts)
    if best is not None and tier_satisfies(best[1], required_tier):
        return None
    return PICKAXE_BY_TIER[required_tier]


def _recipe_lookup_from_context(context: CompositionContext) -> RecipeLookup:
    if context.recipe_lookup is None:
        def missing(_item: str) -> list[RecipeVariant] | None:
            return None

        return missing
    return context.recipe_lookup


def _execute_acquisition_step(
    context: CompositionContext,
    step: AcquisitionStep,
    *,
    before_count: int = 0,
    keep_workstation: bool = False,
    cleanup_workstation: bool = False,
) -> JsonObject:
    if step.kind == "collect":
        return _execute_composition_phase(
            context,
            "ensure_collect",
            context.registry.get("collect_resource"),
            {"item": step.item, "count": before_count + step.count, "constraints": {"auto_prerequisites": False}},
        )
    if step.kind == "craft":
        craft_input: JsonObject = {"item": step.item, "count": step.count}
        if bool(step.detail.get("requires_table")):
            craft_input["search_radius"] = COMPOSITION_WORKSTATION_SEARCH_RADIUS
            if keep_workstation:
                craft_input["keep_temporary_table"] = True
            if cleanup_workstation:
                craft_input["cleanup_existing_bot_table"] = True
        return _execute_composition_phase(
            context,
            "ensure_craft",
            context.registry.get("craft_item"),
            craft_input,
        )
    if step.kind == "smelt":
        smelt_input: JsonObject = {
            "input_item": step.detail.get("input_item") or step.item,
            "count": int(step.detail.get("input_count") or step.count),
        }
        result = _execute_composition_phase(
            context,
            "ensure_smelt",
            context.registry.get("smelt_item"),
            smelt_input,
        )
        if result.get("success") or result.get("reason") != "temporary_furnace_no_supported_site":
            return result
        try:
            surface_tool = context.registry.get("go_to_surface")
        except KeyError:
            return result
        recovery = _execute_composition_phase(
            context,
            "ensure_smelt_workspace_recovery",
            surface_tool,
            {"timeout_s": min(120.0, max(1.0, context.budget.max_wall_s))},
        )
        if not recovery.get("success"):
            return _with_workspace_recovery(result, recovery)
        retry = _execute_composition_phase(
            context,
            "ensure_smelt_after_workspace_recovery",
            context.registry.get("smelt_item"),
            smelt_input,
        )
        return _with_workspace_recovery(retry, recovery)
    if step.kind == "equip":
        return _execute_composition_phase(
            context,
            "ensure_equip",
            context.registry.get("equip_item"),
            {"item": step.item, "target": "mainhand"},
        )
    return ToolResult(False, "unknown_acquisition_step", False, metrics={"step": _acquisition_step_payload(step)}).to_payload()


def _with_workspace_recovery(result: JsonObject, recovery: JsonObject) -> JsonObject:
    enriched = dict(result)
    metrics = dict(enriched.get("metrics") or {})
    metrics["workspace_recovery"] = recovery
    enriched["metrics"] = metrics
    return enriched


def _acquisition_step_inventory_count(context: CompositionContext, step: AcquisitionStep) -> int | None:
    if step.kind == "equip":
        return step.count
    items = (step.item,)
    if step.kind == "collect":
        expected_drops = step.detail.get("expected_drops")
        if isinstance(expected_drops, (list, tuple)):
            items = tuple(_normalize_item(item) for item in expected_drops if item)
        if not items:
            items = (step.item,)
    inventory = _read_count(context, items)
    if not inventory.success:
        return None
    return int((inventory.metrics or {}).get("count") or 0)


def _last_table_craft_index(plan: list[AcquisitionStep]) -> int | None:
    last: int | None = None
    for index, step in enumerate(plan):
        if step.kind == "craft" and bool(step.detail.get("requires_table")):
            last = index
    return last


def _should_keep_workstation(
    plan: list[AcquisitionStep],
    index: int,
    last_table_craft_index: int | None,
) -> bool:
    step = plan[index]
    if (
        step.kind != "craft"
        or not bool(step.detail.get("requires_table"))
        or last_table_craft_index is None
        or index >= last_table_craft_index
    ):
        return False
    next_table_craft_index = next(
        candidate_index
        for candidate_index in range(index + 1, len(plan))
        if plan[candidate_index].kind == "craft"
        and bool(plan[candidate_index].detail.get("requires_table"))
    )
    return all(
        intervening.kind in {"craft", "equip"}
        for intervening in plan[index + 1:next_table_craft_index]
    )


def _should_cleanup_workstation(
    plan: list[AcquisitionStep],
    index: int,
    last_table_craft_index: int | None,
) -> bool:
    step = plan[index]
    return (
        step.kind == "craft"
        and bool(step.detail.get("requires_table"))
        and not _should_keep_workstation(plan, index, last_table_craft_index)
    )


def _inventory_counts_from_result(result: ToolResult) -> dict[str, int]:
    metrics = result.metrics or {}
    counts = metrics.get("counts")
    if not isinstance(counts, dict):
        return {}
    normalized: dict[str, int] = {}
    for raw_item, raw_count in counts.items():
        try:
            count = int(raw_count or 0)
        except (TypeError, ValueError):
            continue
        item = _normalize_item(str(raw_item))
        if item and count > 0:
            normalized[item] = normalized.get(item, 0) + count
    return normalized


def _ensure_result(
    success: bool,
    reason: str,
    can_retry: bool,
    item: str,
    target_count: int,
    *,
    plan: list[dict[str, object]],
    completed_steps: list[dict[str, object]],
    failed_step: dict[str, object] | None = None,
    current_count: int | None = None,
    resume_hint: str,
) -> ToolResult:
    metrics: dict[str, object] = {
        "item": item,
        "target_count": target_count,
        "plan": plan,
        "completed_steps": completed_steps,
        "resume_hint": resume_hint,
    }
    if failed_step is not None:
        metrics["failed_step"] = failed_step
        confirmation = governance_confirmation_from(failed_step)
        if confirmation is not None:
            metrics["governance_confirmation"] = confirmation
    if current_count is not None:
        metrics["current_count"] = current_count
        metrics["remaining_count"] = max(0, target_count - current_count)
    return ToolResult(success, reason, can_retry, metrics=metrics)


def _acquisition_step_payload(step: AcquisitionStep) -> dict[str, object]:
    return {"kind": step.kind, "item": step.item, "count": step.count, "detail": dict(step.detail)}


def _acquisition_error_payload(error: AcquisitionError) -> dict[str, object]:
    return {
        "reason": error.reason,
        "item": error.item,
        "count": error.count,
        "chain": list(error.chain),
        "detail": dict(error.detail),
    }


def _normalize_item(item: str) -> str:
    return item.removeprefix("minecraft:").strip().lower().replace(" ", "_")


def _next_start(perception) -> object | None:
    return perception_next_cursor(perception)


def _emit_trace(context: CompositionContext, event: str, payload: dict[str, object]) -> None:
    if context.trace is not None:
        context.trace(event, payload)


__all__ = [
    "CompositionBudget",
    "CompositionContext",
    "ResourcePlan",
    "collect_resource",
    "ensure_item",
    "ensure_tool_for",
    "register_collect_resource_tool",
    "register_ensure_tool_for_tool",
    "register_inventory_tools",
    "collect_resource_tool",
    "ensure_tool_for_tool",
    "inventory_tool",
    "resource_plan_for",
]
