"""Body transaction container workflows."""

from __future__ import annotations

from dataclasses import asdict
from dataclasses import dataclass

from minebot.body.interaction_support import (
    InteractionNavigator,
    ensure_interaction_range,
    find_nearby_block_targets,
    merge_context,
    normalize_block_type,
    perception_failure as shared_perception_failure,
)
from minebot.contract import Action, Body, InteractionContext, InventorySlot, PerceptionResult, Position, Result, ToolResult, perception_next_cursor
from minebot.contract import terminal_event_to_tool_result
from minebot.game.governance import GovernancePolicy


DEFAULT_CONTAINER_TYPES = ("chest", "trapped_chest", "barrel")


@dataclass(frozen=True)
class ContainerTransferPlan:
    direction: str
    item: str
    requested_count: int
    moves: tuple[tuple[int, int, int], ...]
    available: int
    planned_count: int


class ContainerTransactions:
    """Container workflows above raw slot primitives."""

    def __init__(
        self,
        body: Body,
        *,
        navigator: InteractionNavigator | None = None,
        governance: GovernancePolicy | None = None,
    ):
        self.body = body
        self.navigator = navigator
        self.governance = governance

    def transfer_item(
        self,
        pos: Position,
        *,
        item: str,
        count: int,
        direction: str,
        total_slots: int = 27,
        page_size: int = 27,
        timeout_s: float = 2.0,
    ) -> ToolResult:
        if count <= 0:
            return ToolResult(
                success=False,
                reason="invalid_count",
                can_retry=False,
                metrics={"item": item, "requested_count": count, "direction": direction},
            )
        if direction not in {"container_to_bot", "bot_to_container"}:
            return ToolResult(
                success=False,
                reason="invalid_direction",
                can_retry=False,
                metrics={"item": item, "requested_count": count, "direction": direction},
            )
        target_type = _read_target_type(self.body, pos)
        if isinstance(target_type, ToolResult):
            return target_type
        if target_type not in DEFAULT_CONTAINER_TYPES:
            return ToolResult(
                success=False,
                reason="container_wrong_type",
                can_retry=False,
                metrics={"container_pos": list(pos), "container_type": target_type},
            )
        denied = _guard_container_target(self.governance, pos, target_type)
        if denied is not None:
            return denied

        container = _read_paged(
            self.body,
            "container",
            {"pos": list(pos), "total_slots": total_slots},
            page_size=page_size,
        )
        failed = _perception_failure(container)
        if failed is not None:
            return failed

        inventory = _read_paged(self.body, "inventory", {}, page_size=46)
        failed = _perception_failure(inventory)
        if failed is not None:
            return failed

        container_slots = _slots(container)
        inventory_slots = _slots(inventory)
        plan = _plan_transfer(
            direction=direction,
            item=item,
            count=count,
            container_slots=container_slots,
            inventory_slots=inventory_slots,
        )
        if plan.planned_count <= 0:
            return ToolResult(
                success=False,
                reason="item_not_available" if plan.available <= 0 else "destination_full",
                can_retry=plan.available > 0,
                next_suggestion="free destination slots or lower the requested count"
                if plan.available > 0
                else "choose a present item or refresh container/inventory facts",
                metrics=_plan_metrics(pos, plan),
            )

        executed: list[dict[str, object]] = []
        moved_total = 0
        for source_slot, dest_slot, move_count in plan.moves:
            action = Action.create(
                "containerTransfer",
                {
                    "pos": list(pos),
                    "direction": direction,
                    "container_slot": source_slot if direction == "container_to_bot" else dest_slot,
                    "bot_slot": dest_slot if direction == "container_to_bot" else source_slot,
                    "count": move_count,
                },
            )
            accepted = self.body.execute(action)
            rejected = _acceptance_failure(accepted, pos, plan, executed)
            if rejected is not None:
                return rejected

            terminal = self.body.await_action_terminal(action.id, timeout_s=timeout_s)
            result = terminal_event_to_tool_result(terminal)
            moved = int((result.metrics or {}).get("count") or 0)
            moved_total += moved
            executed.append(
                {
                    "action_id": action.id,
                    "source_slot": source_slot,
                    "dest_slot": dest_slot,
                    "requested_count": move_count,
                    "moved_count": moved,
                    "reason": result.reason,
                    "success": result.success,
                }
            )
            if not result.success:
                metrics = _plan_metrics(pos, plan)
                metrics["executed"] = executed
                metrics["moved_count"] = moved_total
                return ToolResult(
                    success=False,
                    reason=f"container_transfer_failed:{result.reason}",
                    can_retry=result.can_retry,
                    next_suggestion=result.next_suggestion,
                    metrics=metrics,
                )

        return ToolResult(
            success=moved_total >= count,
            reason="completed" if moved_total >= count else "partial",
            can_retry=moved_total < count,
            metrics={**_plan_metrics(pos, plan), "executed": executed, "moved_count": moved_total},
        )

    def transfer_nearest_container(
        self,
        *,
        item: str,
        count: int,
        direction: str,
        search_radius: int = 8,
        container_types: tuple[str, ...] = DEFAULT_CONTAINER_TYPES,
        total_slots: int = 27,
        page_size: int = 27,
        timeout_s: float = 2.0,
        approach_timeout_s: float = 15.0,
    ) -> ToolResult:
        targets = find_nearby_block_targets(
            self.body,
            container_types,
            search_radius,
            not_found_reason="container_not_found",
            limit=64,
        )
        if isinstance(targets, ToolResult):
            return targets

        attempted: list[dict[str, object]] = []
        last_failure: ToolResult | None = None
        for target in targets:
            denied = _guard_container_target(self.governance, target.pos, target.block_type)
            if denied is not None:
                return merge_context(
                    denied,
                    {
                        "search_radius": search_radius,
                        "container_types": list(container_types),
                        "container_target": list(target.pos),
                        "container_type": target.block_type,
                        "attempted_targets": attempted,
                    },
                )
            approach = ensure_interaction_range(
                self.body,
                self.navigator,
                target.pos,
                timeout_s=approach_timeout_s,
                missing_reason="container_navigation_missing",
                failure_prefix="container_navigation_failed",
                no_stand_reason="container_no_stand_point",
            )
            if isinstance(approach, ToolResult):
                attempted.append(
                    {
                        "container_target": list(target.pos),
                        "container_type": target.block_type,
                        "approach_result": approach.to_payload(),
                    }
                )
                last_failure = approach
                if approach.reason == "container_navigation_missing":
                    return merge_context(
                        approach,
                        {
                            "search_radius": search_radius,
                            "container_types": list(container_types),
                            "attempted_targets": attempted,
                        },
                    )
                continue

            result = self.transfer_item(
                target.pos,
                item=item,
                count=count,
                direction=direction,
                total_slots=total_slots,
                page_size=page_size,
                timeout_s=timeout_s,
            )
            return merge_context(
                result,
                {
                    "container_target": list(target.pos),
                    "container_type": target.block_type,
                    "search_radius": search_radius,
                    "container_types": list(container_types),
                    "approach": approach,
                    "attempted_targets": attempted,
                },
            )

        if last_failure is None:
            last_failure = ToolResult(
                success=False,
                reason="container_not_found",
                can_retry=True,
                metrics={"search_radius": search_radius, "container_types": list(container_types)},
            )
        return merge_context(
            last_failure,
            {
                "search_radius": search_radius,
                "container_types": list(container_types),
                "attempted_targets": attempted,
            },
        )


def _read_paged(
    body: Body,
    scope: str,
    base_params: dict[str, object],
    *,
    page_size: int,
) -> PerceptionResult:
    start: int | None = 0
    slots: list[dict[str, object]] = []
    last: PerceptionResult | None = None
    while start is not None:
        params = {**base_params, "start": start, "limit": page_size}
        last = body.perceive(scope, params)
        if not last.ok:
            return last
        slots.extend(dict(item) for item in last.data.get("slots") or [])
        start = perception_next_cursor(last)
        if start is not None:
            start = int(start)
    if last is None:
        return PerceptionResult(
            bot=body.bot_name,
            scope=scope,
            type="perception",
            ok=False,
            complete=True,
            error="no pages read",
        )
    data = dict(last.data)
    data["slots"] = slots
    return PerceptionResult(
        bot=last.bot,
        scope=last.scope,
        type="perception",
        ok=last.ok,
        complete=last.complete,
        data=data,
        uncertainty=last.uncertainty,
        next=last.next,
        error=last.error,
    )


def _slots(perception: PerceptionResult) -> list[InventorySlot]:
    return [InventorySlot.from_payload(item) for item in perception.data.get("slots") or []]


def _read_target_type(body: Body, pos: Position) -> str | ToolResult:
    block = body.perceive("blockAt", {"x": pos[0], "y": pos[1], "z": pos[2]})
    failed = _perception_failure(block)
    if failed is not None:
        return failed
    return normalize_block_type(str(block.data.get("type") or "unknown"))


def _plan_transfer(
    *,
    direction: str,
    item: str,
    count: int,
    container_slots: list[InventorySlot],
    inventory_slots: list[InventorySlot],
) -> ContainerTransferPlan:
    sources = container_slots if direction == "container_to_bot" else inventory_slots
    destinations = inventory_slots if direction == "container_to_bot" else container_slots
    matching_sources = [slot for slot in sources if _same_item(slot.item, item) and not slot.empty]
    available = sum(slot.count for slot in matching_sources)
    moves: list[tuple[int, int, int]] = []
    remaining = count

    dest_remaining: dict[int, int] = {}
    for slot in destinations:
        if slot.empty:
            dest_remaining[slot.slot] = 64
        elif _same_item(slot.item, item) and slot.count < 64:
            dest_remaining[slot.slot] = 64 - slot.count

    for source in matching_sources:
        source_left = source.count
        for dest_slot, capacity in list(dest_remaining.items()):
            if remaining <= 0 or source_left <= 0:
                break
            if capacity <= 0:
                continue
            move_count = min(source_left, remaining, capacity)
            moves.append((source.slot, dest_slot, move_count))
            remaining -= move_count
            source_left -= move_count
            dest_remaining[dest_slot] = capacity - move_count
        if remaining <= 0:
            break

    return ContainerTransferPlan(
        direction=direction,
        item=item,
        requested_count=count,
        moves=tuple(moves),
        available=available,
        planned_count=sum(move[2] for move in moves),
    )


def _same_item(actual: str | None, wanted: str) -> bool:
    if actual is None:
        return False
    return actual == wanted or actual == f"minecraft:{wanted}" or f"minecraft:{actual}" == wanted


def _perception_failure(perception: PerceptionResult) -> ToolResult | None:
    failed = shared_perception_failure(perception)
    if failed is None:
        return None
    if failed.next_suggestion == "refresh world and inventory facts before attempting the interaction":
        return ToolResult(
            success=False,
            reason=failed.reason,
            can_retry=failed.can_retry,
            next_suggestion="refresh container/inventory facts before moving items",
            metrics=failed.metrics,
        )
    return failed


def _acceptance_failure(
    result: Result,
    pos: Position,
    plan: ContainerTransferPlan,
    executed: list[dict[str, object]],
) -> ToolResult | None:
    if result.ok and result.accepted:
        return None
    metrics = _plan_metrics(pos, plan)
    metrics["executed"] = executed
    metrics["accepted"] = {
        "ok": result.ok,
        "accepted": result.accepted,
        "error": result.error,
        "data": result.data,
    }
    return ToolResult(success=False, reason="body_rejected", can_retry=True, metrics=metrics)


def _plan_metrics(pos: Position, plan: ContainerTransferPlan) -> dict[str, object]:
    return {
        "container_pos": list(pos),
        "direction": plan.direction,
        "item": plan.item,
        "requested_count": plan.requested_count,
        "available_count": plan.available,
        "planned_count": plan.planned_count,
        "moves": [
            {"source_slot": source, "dest_slot": dest, "count": count}
            for source, dest, count in plan.moves
        ],
    }


def _guard_container_target(
    governance: GovernancePolicy | None,
    pos: Position,
    block_type: str,
) -> ToolResult | None:
    if governance is None:
        return None
    decision = governance.can_interact(pos, block_type, InteractionContext.ACTIVATE)
    if decision.allowed:
        return None
    return ToolResult(
        success=False,
        reason="container_denied",
        can_retry=False,
        next_suggestion="choose a container inside an allowed natural work region instead of touching protected or unknown-provenance storage",
        metrics={
            "container_pos": list(pos),
            "container_type": block_type,
            "legality": _decision_payload(decision),
        },
    )


def _decision_payload(decision) -> dict[str, object]:
    payload = asdict(decision)
    payload["allowed"] = bool(decision.allowed)
    return payload
