"""Agent-layer composition tools for Phase 1.

These tools compose registered leaf tools through the registry/weld path. They
do not import Body transactions or game transport.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from minebot.brain.modes import RuntimeProfile
from minebot.brain.progress import ProgressAbort
from minebot.brain.registry import RegisteredTool, ToolRegistry, ToolSidecar, WeldContext, execute_tool
from minebot.contract import Body, InventorySlot, JsonObject, ToolResult, is_candidate_skip, perception_next_cursor


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
    trace: Callable[[str, dict[str, object]], None] | None = None


@dataclass(frozen=True)
class ResourcePlan:
    requested_item: str
    inventory_item: str
    inventory_items: tuple[str, ...]
    block_types: tuple[str, ...]
    expected_drops: tuple[str, ...]


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
    started = time.monotonic()
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

    attempts: list[dict[str, object]] = []
    skipped: list[dict[str, object]] = []
    tried_positions: set[tuple[int, int, int]] = set()
    mutating_calls = 0
    last_failure: dict[str, object] | None = None
    targets: list[list[int]] = []
    search: JsonObject | None = None
    search_ok = False
    search_reason = "search_not_run"
    all_skips = True

    while len(attempts) < budget.max_candidates and mutating_calls < budget.max_mutating_calls:
        if time.monotonic() - started > budget.max_wall_s:
            _emit_collect_summary(
                context,
                reason="partial_budget_exhausted",
                success=False,
                plan=plan,
                target_count=count,
                before_count=before_count,
                current_count=current_count,
                attempts=attempts,
                skipped=skipped,
                last_failure=last_failure,
            )
            return _collect_result(
                False,
                "partial_budget_exhausted",
                True,
                plan,
                count,
                before_count,
                current_count,
                attempts,
                skipped,
                "reselect_candidates",
                last_failure=last_failure,
                budget=budget,
            )

        target = _first_untried_target(targets, tried_positions)
        if target is None:
            find_limit = min(12, max(6, min(budget.max_candidates, radius // 2 if radius > 1 else 1)))
            max_pages = _search_max_pages_for_budget(budget, find_limit)
            search = _execute_composition_phase(
                context,
                "search",
                context.registry.get("search_for_block"),
                {
                    "block_types": list(plan.block_types),
                    "search_radius": radius,
                    "find_limit": find_limit,
                    "max_pages": max_pages,
                },
            )
            _emit_trace(
                context,
                "composition_search",
                {
                    "item": plan.requested_item,
                    "radius": radius,
                    "find_limit": find_limit,
                    "max_pages": max_pages,
                    "success": bool(search.get("success")),
                    "reason": str(search.get("reason") or ""),
                },
            )
            targets = _targets_from_search(search, plan=plan)
            target = _first_untried_target(targets, tried_positions)
            search_ok = bool(search.get("success"))
            search_reason = str(search.get("reason") or "search_failed")
        if search is None:
            search = {}
        target = _first_untried_target(targets, tried_positions)
        # A search that navigated to its own nearest pick and could not stand there
        # (no_stand_point / out_of_range / target_lost / navigation_blocked) is a
        # CANDIDATE skip, not a search failure: the candidate list it returned is
        # still real (Scarpet sorts by dist2), so fall through and try the next
        # untried candidate via mine's own approach. Only abort when there is no
        # untried candidate left, or the search failed for a non-skip reason
        # (perception_failed, owner_busy, transport) that means the candidate list
        # itself is untrustworthy.
        search_is_candidate_skip = is_candidate_skip(search_reason)
        if target is None or (not search_ok and not search_is_candidate_skip):
            failure_reason = "candidate_targets_exhausted" if target is None and targets else search_reason
            last_failure = {"phase": "search", "reason": failure_reason, "result": search}
            result_reason = _search_failure_reason(
                failure_reason,
                bool(targets),
                before_count=before_count,
                current_count=current_count,
            )
            _emit_collect_summary(
                context,
                reason=result_reason,
                success=False,
                plan=plan,
                target_count=count,
                before_count=before_count,
                current_count=current_count,
                attempts=attempts,
                skipped=skipped,
                last_failure=last_failure,
            )
            return _collect_result(
                False,
                result_reason,
                True,
                plan,
                count,
                before_count,
                current_count,
                attempts,
                skipped,
                "reselect_candidates",
                last_failure=last_failure,
                budget=budget,
            )
        if not search_ok:
            # Candidate-skip search outcome: record it, keep the candidate list, and
            # try the next untried target instead of treating it as a task failure.
            skipped.append({"pos": list(target), "reason": search_reason, "skip": True, "phase": "search"})
            _emit_trace(
                context,
                "composition_search_skip",
                {"item": plan.requested_item, "reason": search_reason, "next_target": list(target)},
            )

        mutating_calls += 1
        tried_positions.add(tuple(target))
        mined = _execute_candidate_probe_tool(
            context,
            context.registry.get("mine_block_collect"),
            {"pos": target, "expected_drops": list(plan.expected_drops), "dry": allow_dry},
        )
        _emit_trace(
            context,
            "composition_mine_attempt",
            {
                "item": plan.requested_item,
                "target": list(target),
                "success": bool(mined.get("success")),
                "reason": str(mined.get("reason") or ""),
                "diagnostics": _mine_attempt_diagnostics(mined),
            },
        )
        attempt = {"target": target, "search": search, "mine": mined}
        attempts.append(attempt)

        after_result = _read_count(context, plan.inventory_items)
        if not after_result.success:
            return after_result
        current_count = int((after_result.metrics or {}).get("count") or 0)
        if current_count >= count:
            _emit_collect_summary(
                context,
                reason="collected",
                success=True,
                plan=plan,
                target_count=count,
                before_count=before_count,
                current_count=current_count,
                attempts=attempts,
                skipped=skipped,
                last_failure=last_failure,
            )
            return _collect_result(
                True,
                "collected",
                False,
                plan,
                count,
                before_count,
                current_count,
                attempts,
                skipped,
                "complete",
                budget=budget,
            )

        if not mined.get("success"):
            reason = str(mined.get("reason") or "mine_failed")
            skip = _is_collect_candidate_rejection(reason, mined)
            skipped.append({"pos": target, "reason": reason, "skip": skip})
            last_failure = {"phase": "mine", "target": target, "reason": mined.get("reason"), "result": mined}
            if _is_collect_control_yield(reason, mined):
                _emit_collect_summary(
                    context,
                    reason=reason,
                    success=False,
                    plan=plan,
                    target_count=count,
                    before_count=before_count,
                    current_count=current_count,
                    attempts=attempts,
                    skipped=skipped,
                    last_failure=last_failure,
                )
                return _collect_result(
                    False,
                    reason,
                    True,
                    plan,
                    count,
                    before_count,
                    current_count,
                    attempts,
                    skipped,
                    "resume_after_body_control",
                    last_failure=last_failure,
                    budget=budget,
                )
            if not skip:
                all_skips = False
            # A candidate-skip (unreachable / protected / no observed pickup) is not
            # a task failure: exclude this target and try the next one. The shared
            # progress authority still trips the failure storm on genuine repeated
            # failures (those are NOT neutral in the weld), and budget/tried_positions
            # bound the loop, so we deliberately do not stop here on a bad candidate.
        else:
            all_skips = False

    candidate_budget_hit = len(attempts) >= budget.max_candidates
    if attempts and candidate_budget_hit and all_skips:
        reason = "candidate_targets_exhausted"
    else:
        reason = "partial_budget_exhausted"
    _emit_collect_summary(
        context,
        reason=reason,
        success=False,
        plan=plan,
        target_count=count,
        before_count=before_count,
        current_count=current_count,
        attempts=attempts,
        skipped=skipped,
        last_failure=last_failure,
    )
    return _collect_result(
        False,
        reason,
        True,
        plan,
        count,
        before_count,
        current_count,
        attempts,
        skipped,
        "reselect_candidates",
        last_failure=last_failure,
        budget=budget,
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
    try:
        return execute_tool(tool, tool_input, context.weld_context)
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


def _execute_candidate_probe_tool(context: CompositionContext, tool: RegisteredTool, tool_input: JsonObject) -> JsonObject:
    authority = context.weld_context.authority
    progress_snapshot = _progress_authority_snapshot(authority)
    before_failures = authority.failure_steps
    try:
        result = _execute_composition_phase(context, "mine", tool, tool_input)
    except ProgressAbort as exc:
        _restore_progress_authority(authority, progress_snapshot)
        return ToolResult(
            False,
            "mine_progress_yielded",
            True,
            metrics={
                "target": tool_input.get("pos"),
                "progress_facts": _progress_facts_payload(exc),
            },
        ).to_payload()
    if not result.get("success") and _is_collect_candidate_rejection(str(result.get("reason") or ""), result):
        authority.failure_steps = before_failures
    return result


def _is_collect_candidate_rejection(reason: str, result: JsonObject | None = None) -> bool:
    if is_candidate_skip(reason):
        return True
    if reason == "body_rejected" and _is_mining_stand_body_rejection(result):
        return True
    return reason == "mine_progress_yielded"


def _is_collect_control_yield(reason: str, result: JsonObject | None = None) -> bool:
    if reason == "body_rejected" and _is_mining_stand_body_rejection(result):
        return False
    return reason in {"body_rejected", "mine_progress_yielded"} or reason.endswith(":preempted")


def _is_mining_stand_body_rejection(result: JsonObject | None) -> bool:
    if not isinstance(result, dict):
        return False
    metrics = result.get("metrics")
    if not isinstance(metrics, dict):
        return False
    failures = metrics.get("stand_candidate_failures")
    if not isinstance(failures, list) or not failures:
        return False
    if not isinstance(metrics.get("mine_approach"), dict):
        return False
    for failure in failures:
        if not isinstance(failure, dict):
            return False
        if str(failure.get("reason") or "") != "body_rejected":
            return False
        nested = failure.get("result")
        if not isinstance(nested, dict):
            return False
        nested_metrics = nested.get("metrics")
        if not isinstance(nested_metrics, dict):
            return False
        if str(nested_metrics.get("action") or "") != "moveTo":
            return False
        if not isinstance(nested_metrics.get("mine_approach"), dict):
            return False
    return True


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


def _progress_authority_snapshot(authority) -> dict[str, object]:
    return {
        "stagnant_steps": authority.stagnant_steps,
        "stalled_steps": authority.stalled_steps,
        "failure_steps": authority.failure_steps,
        "last_action": authority.last_action,
        "last_fingerprint": authority.last_fingerprint,
        "current_fingerprint": authority.current_fingerprint,
    }


def _restore_progress_authority(authority, snapshot: dict[str, object]) -> None:
    authority.stagnant_steps = int(snapshot["stagnant_steps"])
    authority.stalled_steps = int(snapshot["stalled_steps"])
    authority.failure_steps = int(snapshot["failure_steps"])
    authority.last_action = snapshot["last_action"]
    authority.last_fingerprint = str(snapshot["last_fingerprint"])
    authority.current_fingerprint = str(snapshot["current_fingerprint"])


def _progress_facts_payload(exc: ProgressAbort) -> dict[str, object]:
    return {
        "stagnant_steps": exc.facts.stagnant_steps,
        "stalled_steps": exc.facts.stalled_steps,
        "failure_steps": exc.facts.failure_steps,
        "last_action": list(exc.facts.last_action) if exc.facts.last_action is not None else None,
    }


def _candidate_distance_for_target(payload: JsonObject, target: list[int]) -> float | None:
    metrics = payload.get("metrics")
    if not isinstance(metrics, dict):
        return None
    for candidate in _iter_search_candidates(metrics):
        pos = candidate.get("pos") if isinstance(candidate, dict) else None
        parsed = _parse_pos(pos)
        if parsed == target:
            return _candidate_distance(candidate)
    return None


def _resolve_search_radius(plan: ResourcePlan, constraints: dict[str, object]) -> int:
    requested = constraints.get("radius")
    radius = int(requested) if requested is not None else _default_search_radius(plan)
    return min(max(1, radius), _max_search_radius(plan))


def _read_count(context: CompositionContext, items: tuple[str, ...]) -> ToolResult:
    payload = _execute_composition_phase(context, "inventory", context.registry.get("read_inventory"), {})
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


def _mine_attempt_diagnostics(result: dict[str, Any]) -> dict[str, object]:
    metrics = result.get("metrics")
    if not isinstance(metrics, dict):
        return {}
    diagnostics: dict[str, object] = {}
    for key in ("target", "stand_block", "move_target", "state_after", "reach_distance", "error", "accepted", "data"):
        if key in metrics:
            diagnostics[key] = metrics[key]
    clearance = metrics.get("clearance")
    if isinstance(clearance, dict):
        diagnostics["clearance"] = _clearance_diagnostics(clearance)
    dig = metrics.get("dig_through_result")
    if isinstance(dig, dict):
        diagnostics["dig_through_result"] = _tool_result_diagnostics(dig)
    return diagnostics


def _clearance_diagnostics(clearance: dict[str, Any]) -> dict[str, object]:
    out = _tool_result_diagnostics(clearance)
    metrics = clearance.get("metrics")
    if isinstance(metrics, dict):
        for key in ("stand_block", "target", "block_type"):
            if key in metrics:
                out[key] = metrics[key]
        legality = metrics.get("legality")
        if isinstance(legality, dict):
            out["legality"] = {
                key: legality[key]
                for key in ("reason", "allowed", "block_type", "pos", "context")
                if key in legality
            }
        cleared = metrics.get("cleared")
        if isinstance(cleared, list):
            out["cleared"] = [
                _cleared_block_diagnostics(item)
                for item in cleared[:4]
                if isinstance(item, dict)
            ]
            out["cleared_count"] = len(cleared)
    return out


def _cleared_block_diagnostics(item: dict[str, Any]) -> dict[str, object]:
    out: dict[str, object] = {}
    for key in ("pos", "block_type"):
        if key in item:
            out[key] = item[key]
    result = item.get("result")
    if isinstance(result, dict):
        out["result"] = _tool_result_diagnostics(result)
    return out


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


def _targets_from_search(payload: JsonObject, *, plan: ResourcePlan | None = None) -> list[list[int]]:
    metrics = payload.get("metrics")
    if not isinstance(metrics, dict):
        return []
    targets: list[tuple[list[int], float | None]] = []
    seen_targets: set[tuple[int, int, int]] = set()
    for candidate in _iter_search_candidates(metrics):
        pos = candidate.get("pos") if isinstance(candidate, dict) else None
        parsed = _parse_pos(pos)
        if parsed is not None and tuple(parsed) not in seen_targets:
            targets.append((parsed, _candidate_distance(candidate)))
            seen_targets.add(tuple(parsed))
    if plan is not None and _is_log_plan(plan):
        return _sort_log_targets(targets)
    return _diversify_targets([target for target, _distance in targets])


def _iter_search_candidates(metrics: dict[str, object]) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    target = metrics.get("target")
    if isinstance(target, dict):
        out.append(target)
    candidates = metrics.get("candidates")
    if isinstance(candidates, list):
        out.extend(candidate for candidate in candidates if isinstance(candidate, dict))
    return out


def _is_log_plan(plan: ResourcePlan) -> bool:
    return plan.requested_item in {"log", "logs"} or any(
        _normalize_item(block_type).endswith("_log") for block_type in plan.block_types
    )


def _candidate_distance(candidate: object) -> float | None:
    if not isinstance(candidate, dict):
        return None
    value = candidate.get("distance")
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _sort_log_targets(targets: list[tuple[list[int], float | None]]) -> list[list[int]]:
    if len(targets) < 3:
        return [target for target, _distance in sorted(targets, key=_log_target_sort_key)]

    columns: list[list[tuple[list[int], float | None]]] = []
    for item in sorted(targets, key=_log_target_sort_key):
        column = _find_log_column(columns, item[0])
        if column is None:
            columns.append([item])
        else:
            column.append(item)

    diversified: list[list[int]] = []
    pending = True
    while pending:
        pending = False
        for column in columns:
            if not column:
                continue
            diversified.append(column.pop(0)[0])
            pending = True
    return diversified


def _log_target_sort_key(item: tuple[list[int], float | None]) -> tuple[int, float, int, int]:
    target, distance = item
    return (
        target[1],
        distance if distance is not None else 1_000_000.0,
        target[0],
        target[2],
    )


def _find_log_column(
    columns: list[list[tuple[list[int], float | None]]],
    target: list[int],
) -> list[tuple[list[int], float | None]] | None:
    for column in columns:
        if column and _targets_share_log_column(column[0][0], target):
            return column
    return None


def _targets_share_log_column(left: list[int], right: list[int]) -> bool:
    return _targets_share_patch(left, right)


def _diversify_targets(targets: list[list[int]]) -> list[list[int]]:
    if len(targets) < 3:
        return targets
    clusters: list[list[list[int]]] = []
    for target in targets:
        cluster = _find_target_cluster(clusters, target)
        if cluster is None:
            clusters.append([target])
        else:
            cluster.append(target)

    diversified: list[list[int]] = []
    pending = True
    while pending:
        pending = False
        for cluster in clusters:
            if not cluster:
                continue
            diversified.append(cluster.pop(0))
            pending = True
    return diversified


def _find_target_cluster(clusters: list[list[list[int]]], target: list[int]) -> list[list[int]] | None:
    for cluster in clusters:
        if any(_targets_share_patch(candidate, target) for candidate in cluster):
            return cluster
    return None


def _targets_share_patch(left: list[int], right: list[int]) -> bool:
    return abs(left[0] - right[0]) <= 2 and abs(left[2] - right[2]) <= 2 and abs(left[1] - right[1]) <= 6


def _parse_pos(value: object) -> list[int] | None:
    if not isinstance(value, list) or len(value) != 3:
        return None
    return [int(value[0]), int(value[1]), int(value[2])]


def _first_untried_target(targets: list[list[int]], tried_positions: set[tuple[int, int, int]]) -> list[int] | None:
    for target in targets:
        if tuple(target) not in tried_positions:
            return target
    return None


def _search_failure_reason(reason: str, had_candidates: bool, *, before_count: int, current_count: int) -> str:
    if current_count > before_count:
        return "partial_candidate_targets_exhausted"
    if had_candidates:
        return "candidate_targets_exhausted"
    return "target_not_found" if reason == "search_block_not_found" else f"search_failed:{reason}"


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
    return ToolResult(success, reason, can_retry, metrics=metrics)


def _normalize_item(item: str) -> str:
    return item.removeprefix("minecraft:")


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
    "register_collect_resource_tool",
    "register_inventory_tools",
    "resource_plan_for",
]
