"""Agent-layer composition tools for Phase 1.

These tools compose registered leaf tools through the registry/weld path. They
do not import Body transactions or game transport.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from minebot.brain.modes import RuntimeProfile
from minebot.brain.registry import RegisteredTool, ToolRegistry, ToolSidecar, WeldContext, execute_tool
from minebot.contract import Body, InventorySlot, JsonObject, ToolResult


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
    radius = int(constraints.get("radius") or 16)
    allow_dry = bool(constraints.get("allow_dry", False))
    started = time.monotonic()

    before_result = _read_count(context, item)
    if not before_result.success:
        return before_result
    before_count = int((before_result.metrics or {}).get("count") or 0)
    current_count = before_count
    if before_count >= count:
        return _collect_result(
            True,
            "already_satisfied",
            False,
            item,
            count,
            before_count,
            current_count,
            [],
            [],
            "complete",
        )

    attempts: list[dict[str, object]] = []
    skipped: list[dict[str, object]] = []
    mutating_calls = 0
    last_failure: dict[str, object] | None = None

    while len(attempts) < budget.max_candidates and mutating_calls < budget.max_mutating_calls:
        if time.monotonic() - started > budget.max_wall_s:
            return _collect_result(
                False,
                "partial_budget_exhausted",
                True,
                item,
                count,
                before_count,
                current_count,
                attempts,
                skipped,
                "reselect_candidates",
                last_failure=last_failure,
                budget=budget,
            )

        search = execute_tool(
            context.registry.get("search_for_block"),
            {"block_types": [item], "search_radius": radius, "find_limit": max(32, budget.max_candidates * 4)},
            context.weld_context,
        )
        target = _target_from_search(search)
        if not search.get("success") or target is None:
            reason = str(search.get("reason") or "search_failed")
            last_failure = {"phase": "search", "reason": reason, "result": search}
            return _collect_result(
                False,
                "target_not_found" if reason == "search_block_not_found" else f"search_failed:{reason}",
                True,
                item,
                count,
                before_count,
                current_count,
                attempts,
                skipped,
                "reselect_candidates",
                last_failure=last_failure,
                budget=budget,
            )

        mutating_calls += 1
        mined = execute_tool(
            context.registry.get("mine_block_collect"),
            {"pos": target, "expected_drops": [item], "dry": allow_dry},
            context.weld_context,
        )
        attempt = {"target": target, "search": search, "mine": mined}
        attempts.append(attempt)

        after_result = _read_count(context, item)
        if not after_result.success:
            return after_result
        current_count = int((after_result.metrics or {}).get("count") or 0)
        if current_count >= count:
            return _collect_result(
                True,
                "collected",
                False,
                item,
                count,
                before_count,
                current_count,
                attempts,
                skipped,
                "complete",
                budget=budget,
            )

        if not mined.get("success"):
            skipped.append({"pos": target, "reason": str(mined.get("reason") or "mine_failed")})
            last_failure = {"phase": "mine", "target": target, "reason": mined.get("reason"), "result": mined}
            if str(mined.get("reason") or "").startswith("break_denied"):
                return _collect_result(
                    False,
                    "protected_or_illegal_target",
                    True,
                    item,
                    count,
                    before_count,
                    current_count,
                    attempts,
                    skipped,
                    "reselect_candidates",
                    last_failure=last_failure,
                    budget=budget,
                )

    return _collect_result(
        False,
        "partial_budget_exhausted",
        True,
        item,
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


def _read_count(context: CompositionContext, item: str) -> ToolResult:
    payload = execute_tool(context.registry.get("read_inventory"), {}, context.weld_context)
    if not payload.get("success"):
        return ToolResult(
            False,
            f"inventory_read_failed:{payload.get('reason')}",
            True,
            metrics={"item": item, "inventory_result": payload},
        )
    counts = payload.get("metrics", {}).get("counts") if isinstance(payload.get("metrics"), dict) else {}
    return ToolResult(
        True,
        "inventory_counted",
        False,
        metrics={"item": item, "count": int((counts or {}).get(item, 0)), "counts": counts or {}},
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
        next_start = perception.data.get("nextStart")
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


def _target_from_search(payload: JsonObject) -> list[int] | None:
    metrics = payload.get("metrics")
    if not isinstance(metrics, dict):
        return None
    target = metrics.get("target")
    if not isinstance(target, dict):
        return None
    pos = target.get("pos")
    if not isinstance(pos, list) or len(pos) != 3:
        return None
    return [int(pos[0]), int(pos[1]), int(pos[2])]


def _collect_result(
    success: bool,
    reason: str,
    can_retry: bool,
    item: str,
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
        "item": item,
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


__all__ = [
    "CompositionBudget",
    "CompositionContext",
    "collect_resource",
    "register_collect_resource_tool",
    "register_inventory_tools",
]
