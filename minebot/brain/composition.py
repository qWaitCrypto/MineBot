"""Agent-layer composition tools for Phase 1.

These tools compose registered leaf tools through the registry/weld path. They
do not import Body transactions or game transport.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from minebot.brain.acquisition import AcquisitionError, AcquisitionStep, RecipeLookup, RecipeVariant, resolve_acquisition
from minebot.brain.modes import RuntimeProfile
from minebot.brain.registry import RegisteredTool, ToolRegistry, ToolSidecar, WeldContext, execute_tool
from minebot.contract import Body, InventorySlot, JsonObject, ToolResult, perception_next_cursor
from minebot.contract.harvest import PICKAXE_BY_TIER, best_owned_pickaxe, required_pickaxe_tier, tier_satisfies


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


def register_inventory_tools(registry: ToolRegistry, body: Body) -> None:
    registry.register(
        RegisteredTool(
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
    )


def register_collect_resource_tool(registry: ToolRegistry, context: CompositionContext) -> None:
    registry.register(
        RegisteredTool(
            name="collect_resource",
            description="Collect a requested resource count by composing search, mine, and inventory tools.",
            input_schema={
                "type": "object",
                "properties": {
                    "item": {"type": "string"},
                    "count": {"type": "integer", "minimum": 1},
                    "constraints": {
                        "type": "object",
                        "properties": {
                            "radius": {"type": "integer", "minimum": 1},
                            "max_candidates": {"type": "integer", "minimum": 1},
                            "max_mutating_calls": {"type": "integer", "minimum": 1},
                            "max_wall_s": {"type": "number", "exclusiveMinimum": 0},
                            "allow_dry": {"type": "boolean"},
                            "auto_prerequisites": {"type": "boolean"},
                        },
                        "additionalProperties": True,
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
        )
    )


def register_ensure_tool_for_tool(registry: ToolRegistry, context: CompositionContext, recipe_lookup: RecipeLookup) -> None:
    registry.register(
        RegisteredTool(
            name="ensure_tool_for",
            description="Ensure the bot owns and equips the tool needed for harvesting a resource, or obtain a requested item through deterministic collect/craft/smelt/equip steps. Uses existing Body tools and fails honestly with the planned step that failed.",
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
        )
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
    requested_count = count
    requested_plan = _resource_plan(item)
    goal_target = _goal_collect_target(context.weld_context.goal_text)
    plan = _resolve_collect_plan(requested_plan, goal_target)
    goal_target_count: int | None = None
    if goal_target is not None and _goal_target_matches_plan(goal_target, plan):
        count = max(count, goal_target[1])
        goal_target_count = goal_target[1]
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
            requested_count=requested_count,
            goal_target_count=goal_target_count,
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
                return ToolResult(
                    False,
                    ensured.reason,
                    ensured.can_retry,
                    ensured.next_suggestion,
                    metrics={
                        "item": plan.inventory_item,
                        "requested_item": plan.requested_item,
                        "required_tool": prerequisite,
                        "ensure_result": ensured.to_payload(),
                        "resume_hint": (ensured.metrics or {}).get("resume_hint", "reinvoke_ensure"),
                    },
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
                    requested_count=requested_count,
                    goal_target_count=goal_target_count,
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

    if complete:
        result_reason = "collected"
        result_success = True
        can_retry = False
        resume_hint = "complete"
        last_failure = None
    else:
        result_reason = _resource_process_reason(body_reason, before_count=before_count, current_count=current_count)
        result_success = _collect_partial_success(before_count, current_count)
        can_retry = bool(body_process.get("canRetry", True))
        resume_hint = "reselect_candidates" if can_retry else "body_terminal"
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
        last_failure=last_failure,
        budget=budget,
        requested_count=requested_count,
        goal_target_count=goal_target_count,
    )
    metrics = dict(result.metrics or {})
    metrics["body_process"] = body_process
    return ToolResult(
        result.success,
        result.reason,
        result.can_retry,
        result.next_suggestion,
        metrics=metrics,
    )
def ensure_tool_for(params: JsonObject, context: CompositionContext, recipe_lookup: RecipeLookup) -> ToolResult:
    resource = _normalize_item(str(params.get("resource") or ""))
    if not resource:
        return ToolResult(False, "invalid_resource", False, metrics={"resource": params.get("resource")})
    target = _ensure_target_for(resource)
    return ensure_item({"item": target, "count": 1, "resource": resource}, context, recipe_lookup)


def ensure_item(params: JsonObject, context: CompositionContext, recipe_lookup: RecipeLookup) -> ToolResult:
    item = _normalize_item(str(params.get("item") or params.get("resource") or ""))
    count = int(params.get("count") or 1)
    if not item:
        return ToolResult(False, "invalid_item", False, metrics={"item": params.get("item")})
    if count <= 0:
        return ToolResult(False, "invalid_count", False, metrics={"item": item, "target_count": count})

    started = time.monotonic()
    inventory = _read_count(context, (item,))
    if not inventory.success:
        return inventory
    counts = _inventory_counts_from_result(inventory)
    plan = resolve_acquisition(item, count, counts, recipe_lookup, max_depth=18)
    if isinstance(plan, AcquisitionError):
        return ToolResult(
            False,
            plan.reason,
            False,
            next_suggestion="choose a reachable item/resource or add a recipe/acquisition route",
            metrics={"item": item, "target_count": count, "error": _acquisition_error_payload(plan)},
        )
    if not plan:
        return ToolResult(
            True,
            "already_satisfied",
            False,
            metrics={"item": item, "target_count": count, "plan": [], "completed_steps": []},
        )

    completed_steps: list[dict[str, object]] = []
    step_payloads = [_acquisition_step_payload(step) for step in plan]
    last_table_craft_index = _last_table_craft_index(plan)
    for index, step in enumerate(plan):
        if time.monotonic() - started > context.budget.max_wall_s:
            return _ensure_result(
                False,
                "partial_budget_exhausted",
                True,
                item,
                count,
                plan=step_payloads,
                completed_steps=completed_steps,
                failed_step={"index": index, "step": _acquisition_step_payload(step), "reason": "max_wall_s"},
                resume_hint="reinvoke_ensure",
            )
        before_step_count = _acquisition_step_inventory_count(context, step)
        if before_step_count is None:
            failed_step = {
                "index": index,
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
            keep_workstation=_should_keep_workstation(step, index, last_table_craft_index),
            cleanup_workstation=_should_cleanup_workstation(step, index, last_table_craft_index),
        )
        step_record = {"index": index, "step": _acquisition_step_payload(step), "result": result}
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
        inventory = _read_count(context, (item,))
        if not inventory.success:
            return inventory
        current = int((inventory.metrics or {}).get("count") or 0)
        if current >= count:
            return _ensure_result(
                True,
                "ensured",
                False,
                item,
                count,
                plan=step_payloads,
                completed_steps=completed_steps,
                current_count=current,
                resume_hint="complete",
            )

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


def _goal_collect_target(goal_text: str) -> tuple[str, int] | None:
    text = goal_text.strip().lower().replace("minecraft:", "")
    match = re.search(r"\b(?:collect|get|gather|mine)\s+(\d+)\s+([a-z_]+)\b", text)
    if match:
        return (_normalize_item(match.group(2)), int(match.group(1)))
    match = re.search(r"\b(?:collect|get|gather|mine)\s+([a-z_]+)\s+(\d+)\b", text)
    if match:
        return (_normalize_item(match.group(1)), int(match.group(2)))
    return None


def _goal_target_matches_plan(goal_target: tuple[str, int], plan: ResourcePlan) -> bool:
    goal_item = _normalize_item(goal_target[0])
    try:
        goal_plan = _resource_plan(goal_item)
    except ValueError:
        return False
    return bool(set(goal_plan.inventory_items) & set(plan.inventory_items))


def _resolve_collect_plan(requested_plan: ResourcePlan, goal_target: tuple[str, int] | None) -> ResourcePlan:
    if goal_target is None:
        return requested_plan
    goal_plan = _resource_plan(goal_target[0])
    if _is_log_plan(goal_plan) and _is_log_plan(requested_plan):
        return goal_plan
    return requested_plan


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
            ("oak_log", "spruce_log", "birch_log", "jungle_log", "acacia_log", "dark_oak_log"),
            ("oak_log", "spruce_log", "birch_log", "jungle_log", "acacia_log", "dark_oak_log"),
        ),
        "logs": (
            "oak_log",
            ("oak_log", "spruce_log", "birch_log", "jungle_log", "acacia_log", "dark_oak_log"),
            ("oak_log", "spruce_log", "birch_log", "jungle_log", "acacia_log", "dark_oak_log"),
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
    if plan.requested_item in {"log", "logs"}:
        return 48
    return 16


def _max_search_radius(plan: ResourcePlan) -> int:
    if plan.requested_item in {"log", "logs"}:
        return 64
    return 48


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
            metrics={"scope": "inventory", "error": perception.error, "uncertainty": list(perception.uncertainty)},
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


def _is_log_plan(plan: ResourcePlan) -> bool:
    return plan.requested_item in {"log", "logs"} or any(
        _normalize_item(block_type).endswith("_log") for block_type in plan.block_types
    )


def _parse_pos(value: object) -> list[int] | None:
    if not isinstance(value, list) or len(value) != 3:
        return None
    return [int(value[0]), int(value[1]), int(value[2])]


def _resource_process_reason(reason: str, *, before_count: int, current_count: int) -> str:
    if reason == "resource_candidates_not_found":
        return "partial_candidate_targets_exhausted" if current_count > before_count else "target_not_found"
    if reason in {"resource_candidate_domain_exhausted", "resource_domain_partial_exhausted"}:
        return "partial_candidate_targets_exhausted" if current_count > before_count else "candidate_targets_exhausted"
    if reason == "resource_domain_budget_exhausted":
        return "partial_budget_exhausted"
    if reason == "resource_domain_collected":
        return "collect_inventory_target_not_met"
    return reason


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


def _collect_partial_success(before_count: int, current_count: int) -> bool:
    return current_count > before_count


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
    last_failure: dict[str, object] | None = None,
    budget: CompositionBudget | None = None,
    requested_count: int | None = None,
    goal_target_count: int | None = None,
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
    if requested_count is not None and requested_count != target_count:
        metrics["requested_count"] = requested_count
    if goal_target_count is not None:
        metrics["goal_target_count"] = goal_target_count
    if last_failure is not None:
        metrics["last_failure"] = last_failure
    if budget is not None:
        metrics["budget"] = {
            "max_candidates": budget.max_candidates,
            "max_mutating_calls": budget.max_mutating_calls,
            "max_wall_s": budget.max_wall_s,
        }
    return ToolResult(success, reason, can_retry, metrics=metrics)


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
        return _execute_composition_phase(
            context,
            "ensure_smelt",
            context.registry.get("smelt_item"),
            {"input_item": step.detail.get("input_item") or step.item, "count": int(step.detail.get("input_count") or step.count)},
        )
    if step.kind == "equip":
        return _execute_composition_phase(
            context,
            "ensure_equip",
            context.registry.get("equip_item"),
            {"item": step.item, "target": "mainhand"},
        )
    return ToolResult(False, "unknown_acquisition_step", False, metrics={"step": _acquisition_step_payload(step)}).to_payload()


def _acquisition_step_inventory_count(context: CompositionContext, step: AcquisitionStep) -> int | None:
    if step.kind == "equip":
        return step.count
    inventory = _read_count(context, (step.item,))
    if not inventory.success:
        return None
    return int((inventory.metrics or {}).get("count") or 0)


def _last_table_craft_index(plan: list[AcquisitionStep]) -> int | None:
    last: int | None = None
    for index, step in enumerate(plan):
        if step.kind == "craft" and bool(step.detail.get("requires_table")):
            last = index
    return last


def _should_keep_workstation(step: AcquisitionStep, index: int, last_table_craft_index: int | None) -> bool:
    return (
        step.kind == "craft"
        and bool(step.detail.get("requires_table"))
        and last_table_craft_index is not None
        and index < last_table_craft_index
    )


def _should_cleanup_workstation(step: AcquisitionStep, index: int, last_table_craft_index: int | None) -> bool:
    return (
        step.kind == "craft"
        and bool(step.detail.get("requires_table"))
        and last_table_craft_index is not None
        and index == last_table_craft_index
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
    "resource_plan_for",
]
