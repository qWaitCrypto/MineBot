"""Body transaction furnace workflows."""

from __future__ import annotations

from dataclasses import asdict
from dataclasses import dataclass
import time
from math import dist

from minebot.body.block_work import BlockWork
from minebot.body.interaction_support import (
    InteractionNavigator,
    find_nearby_block_targets,
    ensure_interaction_range,
    merge_context,
    normalize_block_type,
    perception_failure as shared_perception_failure,
)
from minebot.contract import Body, InteractionContext
from minebot.contract import terminal_event_to_tool_result
from minebot.contract import Action, BreakContext, InventorySlot, PerceptionResult, PlaceContext, Position, Result, ToolResult, perception_next_cursor
from minebot.game.governance import GovernancePolicy


DEFAULT_FURNACE_TYPES = ("furnace", "blast_furnace", "smoker")
DEFAULT_SMELT_SECONDS_PER_ITEM = 10.0
FUEL_BURN_SECONDS = {
    "lava_bucket": 1000.0,
    "coal_block": 800.0,
    "dried_kelp_block": 200.0,
    "blaze_rod": 120.0,
    "coal": 80.0,
    "charcoal": 80.0,
    "stick": 5.0,
    "bamboo": 2.5,
}

FURNACE_SLOTS = {"input": 0, "fuel": 1, "output": 2}
CLEAR_ORDER = ("output", "input", "fuel")


@dataclass(frozen=True)
class FurnaceClearPlan:
    pos: Position
    moves: tuple[dict[str, object], ...]
    occupied_furnace_slots: int


@dataclass(frozen=True)
class SmeltPlan:
    pos: Position
    input_item: str
    input_count: int
    input_slot: int
    fuel_item: str
    fuel_count: int
    fuel_slot: int
    fuel_auto: bool
    fuel_seconds_available: float
    fuel_seconds_required: float
    output_item: str
    output_count: int
    output_slot: int


class FurnaceTransactions:
    """Furnace workflows above named-slot transfer primitives."""

    def __init__(
        self,
        body: Body,
        *,
        navigator: InteractionNavigator | None = None,
        governance: GovernancePolicy | None = None,
        work: BlockWork | None = None,
    ):
        self.body = body
        self.navigator = navigator
        self.governance = governance
        self.work = work

    def clear_furnace(
        self,
        pos: Position,
        *,
        timeout_s: float = 2.0,
    ) -> ToolResult:
        target_type = _read_target_type(self.body, pos)
        if isinstance(target_type, ToolResult):
            return target_type
        if target_type not in DEFAULT_FURNACE_TYPES:
            return ToolResult(
                success=False,
                reason="furnace_wrong_type",
                can_retry=False,
                metrics={"furnace_pos": list(pos), "furnace_type": target_type},
            )
        denied = _guard_furnace_target(self.governance, pos, target_type)
        if denied is not None:
            return denied

        furnace = _read_furnace(self.body, pos)
        failed = _perception_failure(furnace)
        if failed is not None:
            return failed

        inventory = _read_inventory(self.body)
        failed = _perception_failure(inventory)
        if failed is not None:
            return failed

        plan = _plan_clear(pos, _slots(furnace), _slots(inventory))
        if not plan.moves:
            reason = "already_empty" if plan.occupied_furnace_slots == 0 else "bot_inventory_full"
            return ToolResult(
                success=plan.occupied_furnace_slots == 0,
                reason=reason,
                can_retry=plan.occupied_furnace_slots > 0,
                next_suggestion="free bot inventory slots before clearing the furnace"
                if plan.occupied_furnace_slots > 0
                else None,
                metrics=_plan_metrics(plan),
            )

        executed: list[dict[str, object]] = []
        moved_total = 0
        for move in plan.moves:
            result = self.transfer_slot(
                pos,
                direction="furnace_to_bot",
                furnace_slot=move["furnace_slot"],
                bot_slot=int(move["bot_slot"]),
                timeout_s=timeout_s,
            )
            moved = int((result.metrics or {}).get("count") or 0)
            moved_total += moved
            executed.append(
                {
                    "action_id": (result.metrics or {}).get("action_id"),
                    "furnace_slot": move["furnace_slot"],
                    "bot_slot": move["bot_slot"],
                    "item": move["item"],
                    "expected_count": move["count"],
                    "moved_count": moved,
                    "success": result.success,
                    "reason": result.reason,
                }
            )
            if not result.success:
                metrics = _plan_metrics(plan)
                metrics["executed"] = executed
                metrics["moved_count"] = moved_total
                return ToolResult(
                    success=False,
                    reason=f"furnace_clear_failed:{result.reason}",
                    can_retry=result.can_retry,
                    next_suggestion=result.next_suggestion,
                    metrics=metrics,
                )

        return ToolResult(
            success=True,
            reason="completed",
            can_retry=False,
            metrics={**_plan_metrics(plan), "executed": executed, "moved_count": moved_total},
        )

    def transfer_slot(
        self,
        pos: Position,
        *,
        direction: str,
        furnace_slot: str = "output",
        bot_slot: int = 0,
        count: int | None = None,
        max_stack: int = 64,
        timeout_s: float = 2.0,
    ) -> ToolResult:
        if direction not in {"furnace_to_bot", "bot_to_furnace"}:
            return ToolResult(
                success=False,
                reason="invalid_direction",
                can_retry=False,
                metrics={
                    "furnace_pos": list(pos),
                    "direction": direction,
                    "furnace_slot": furnace_slot,
                    "bot_slot": bot_slot,
                    "count": count,
                },
            )
        if furnace_slot not in FURNACE_SLOTS:
            return ToolResult(
                success=False,
                reason="invalid_furnace_slot",
                can_retry=False,
                metrics={
                    "furnace_pos": list(pos),
                    "direction": direction,
                    "furnace_slot": furnace_slot,
                    "bot_slot": bot_slot,
                    "count": count,
                },
            )
        if bot_slot < 0 or bot_slot > 45:
            return ToolResult(
                success=False,
                reason="invalid_bot_slot",
                can_retry=False,
                metrics={
                    "furnace_pos": list(pos),
                    "direction": direction,
                    "furnace_slot": furnace_slot,
                    "bot_slot": bot_slot,
                    "count": count,
                },
            )

        target_type = _read_target_type(self.body, pos)
        if isinstance(target_type, ToolResult):
            return target_type
        if target_type not in DEFAULT_FURNACE_TYPES:
            return ToolResult(
                success=False,
                reason="furnace_wrong_type",
                can_retry=False,
                metrics={"furnace_pos": list(pos), "furnace_type": target_type},
            )
        denied = _guard_furnace_target(self.governance, pos, target_type)
        if denied is not None:
            return denied

        furnace_before = _read_furnace(self.body, pos)
        failed = _perception_failure(furnace_before)
        if failed is not None:
            return failed
        inventory_before = _read_inventory(self.body)
        failed = _perception_failure(inventory_before)
        if failed is not None:
            return failed

        furnace_before_slots = _slots(furnace_before)
        inventory_before_slots = _slots(inventory_before)
        before_furnace_slot = _slot_by_index(furnace_before_slots, FURNACE_SLOTS[furnace_slot])
        before_bot_slot = _slot_by_index(inventory_before_slots, bot_slot)

        params: dict[str, object] = {
            "pos": list(pos),
            "direction": direction,
            "furnace_slot": furnace_slot,
            "bot_slot": bot_slot,
            "max_stack": max_stack,
        }
        if count is not None:
            params["count"] = count
        action = Action.create("furnaceTransfer", params)
        accepted = self.body.execute(action)
        rejected = _acceptance_failure(
            accepted,
            FurnaceClearPlan(pos=pos, moves=tuple(), occupied_furnace_slots=0),
            [],
        )
        if rejected is not None:
            return ToolResult(
                success=rejected.success,
                reason=rejected.reason,
                can_retry=rejected.can_retry,
                next_suggestion=rejected.next_suggestion,
                metrics={
                    **dict(rejected.metrics or {}),
                    "furnace_pos": list(pos),
                    "direction": direction,
                    "furnace_slot": furnace_slot,
                    "bot_slot": bot_slot,
                    "count": count,
                },
            )

        terminal = self.body.await_action_terminal(action.id, timeout_s=timeout_s)
        result = terminal_event_to_tool_result(terminal)
        moved_count = int((result.metrics or {}).get("count") or 0)
        metrics = {
            "action_id": action.id,
            "furnace_pos": list(pos),
            "furnace_type": target_type,
            "direction": direction,
            "furnace_slot": furnace_slot,
            "bot_slot": bot_slot,
            "requested_count": count,
            "count": moved_count,
            "furnace_before": _slot_metrics(before_furnace_slot),
            "bot_before": _slot_metrics(before_bot_slot),
            "primitive": dict(result.metrics or {}),
        }
        if not result.success:
            return ToolResult(
                success=False,
                reason=result.reason,
                can_retry=result.can_retry,
                next_suggestion=result.next_suggestion,
                metrics=metrics,
            )

        furnace_after = _read_furnace(self.body, pos)
        failed = _perception_failure(furnace_after)
        if failed is not None:
            return ToolResult(
                success=False,
                reason=failed.reason,
                can_retry=failed.can_retry,
                next_suggestion=failed.next_suggestion,
                metrics={**metrics, "after_read": dict(failed.metrics or {})},
            )
        inventory_after = _read_inventory(self.body)
        failed = _perception_failure(inventory_after)
        if failed is not None:
            return ToolResult(
                success=False,
                reason=failed.reason,
                can_retry=failed.can_retry,
                next_suggestion=failed.next_suggestion,
                metrics={**metrics, "after_read": dict(failed.metrics or {})},
            )

        furnace_after_slots = _slots(furnace_after)
        inventory_after_slots = _slots(inventory_after)
        after_furnace_slot = _slot_by_index(furnace_after_slots, FURNACE_SLOTS[furnace_slot])
        after_bot_slot = _slot_by_index(inventory_after_slots, bot_slot)
        metrics["furnace_after"] = _slot_metrics(after_furnace_slot)
        metrics["bot_after"] = _slot_metrics(after_bot_slot)
        if not _verify_transfer(
            direction=direction,
            furnace_slot=furnace_slot,
            moved_count=moved_count,
            before_furnace_slot=before_furnace_slot,
            after_furnace_slot=after_furnace_slot,
            before_bot_slot=before_bot_slot,
            after_bot_slot=after_bot_slot,
        ):
            return ToolResult(
                success=False,
                reason="furnace_transfer_unverified",
                can_retry=True,
                next_suggestion="re-read furnace and bot inventory slots and verify the transfer did not leave an inconsistent source/destination delta",
                metrics=metrics,
            )

        return ToolResult(
            success=True,
            reason="completed",
            can_retry=False,
            metrics=metrics,
        )

    def clear_nearest_furnace(
        self,
        *,
        search_radius: int = 8,
        furnace_types: tuple[str, ...] = DEFAULT_FURNACE_TYPES,
        timeout_s: float = 2.0,
        approach_timeout_s: float = 15.0,
    ) -> ToolResult:
        targets = find_nearby_block_targets(
            self.body,
            furnace_types,
            search_radius,
            not_found_reason="furnace_not_found",
            limit=64,
        )
        if isinstance(targets, ToolResult):
            return targets

        attempted: list[dict[str, object]] = []
        last_failure: ToolResult | None = None
        for target in targets:
            denied = _guard_furnace_target(self.governance, target.pos, target.block_type)
            if denied is not None:
                return merge_context(
                    denied,
                    {
                        "search_radius": search_radius,
                        "furnace_types": list(furnace_types),
                        "furnace_target": list(target.pos),
                        "furnace_type": target.block_type,
                        "attempted_targets": attempted,
                    },
                )
            approach = ensure_interaction_range(
                self.body,
                self.navigator,
                target.pos,
                timeout_s=approach_timeout_s,
                missing_reason="furnace_navigation_missing",
                failure_prefix="furnace_navigation_failed",
                no_stand_reason="furnace_no_stand_point",
            )
            if isinstance(approach, ToolResult):
                attempted.append(
                    {
                        "furnace_target": list(target.pos),
                        "furnace_type": target.block_type,
                        "approach_result": approach.to_payload(),
                    }
                )
                last_failure = approach
                if approach.reason == "furnace_navigation_missing":
                    return merge_context(
                        approach,
                        {
                            "search_radius": search_radius,
                            "furnace_types": list(furnace_types),
                            "attempted_targets": attempted,
                        },
                    )
                continue

            result = self.clear_furnace(target.pos, timeout_s=timeout_s)
            return merge_context(
                result,
                {
                    "furnace_target": list(target.pos),
                    "furnace_type": target.block_type,
                    "search_radius": search_radius,
                    "furnace_types": list(furnace_types),
                    "approach": approach,
                    "attempted_targets": attempted,
                },
            )

        if last_failure is None:
            last_failure = ToolResult(
                success=False,
                reason="furnace_not_found",
                can_retry=True,
                metrics={"search_radius": search_radius, "furnace_types": list(furnace_types)},
            )
        return merge_context(
            last_failure,
            {
                "search_radius": search_radius,
                "furnace_types": list(furnace_types),
                "attempted_targets": attempted,
            },
        )

    def smelt_once(
        self,
        pos: Position,
        *,
        input_item: str,
        input_count: int,
        fuel_item: str,
        fuel_count: int | None = None,
        output_item: str,
        output_count: int,
        output_slot: int | None = None,
        poll_interval_s: float = 0.5,
        smelt_timeout_s: float = 15.0,
        transfer_timeout_s: float = 2.0,
    ) -> ToolResult:
        if input_count <= 0 or (fuel_count is not None and fuel_count <= 0) or output_count <= 0:
            return ToolResult(
                success=False,
                reason="invalid_smelt_request",
                can_retry=False,
                metrics={
                    "furnace_pos": list(pos),
                    "input_item": input_item,
                    "input_count": input_count,
                    "fuel_item": fuel_item,
                    "fuel_count": fuel_count,
                    "output_item": output_item,
                    "output_count": output_count,
                },
            )

        preflight = self._smelt_preflight(
            pos,
            input_item=input_item,
            input_count=input_count,
            fuel_item=fuel_item,
            fuel_count=fuel_count,
            output_item=output_item,
            output_count=output_count,
            output_slot=output_slot,
        )
        if isinstance(preflight, ToolResult):
            return preflight
        plan = preflight

        executed: list[dict[str, object]] = []
        input_move = self.transfer_slot(
            pos,
            direction="bot_to_furnace",
            furnace_slot="input",
            bot_slot=plan.input_slot,
            count=plan.input_count,
            timeout_s=transfer_timeout_s,
        )
        executed.append({"kind": "deposit_input", "result": input_move.to_payload()})
        if not input_move.success:
            return _smelt_failure("smelt_input_deposit_failed", input_move, plan, executed)

        fuel_move = self.transfer_slot(
            pos,
            direction="bot_to_furnace",
            furnace_slot="fuel",
            bot_slot=plan.fuel_slot,
            count=plan.fuel_count,
            timeout_s=transfer_timeout_s,
        )
        executed.append({"kind": "deposit_fuel", "result": fuel_move.to_payload()})
        if not fuel_move.success:
            reclaim = self._reclaim_smelt_slots(pos, plan, timeout_s=transfer_timeout_s)
            return _smelt_failure("smelt_fuel_deposit_failed", fuel_move, plan, executed, reclaim=reclaim)

        deadline = time.monotonic() + smelt_timeout_s
        polls: list[dict[str, object]] = []
        output_ready = False
        while time.monotonic() <= deadline:
            furnace = _read_furnace(self.body, pos)
            failed = _perception_failure(furnace)
            if failed is not None:
                reclaim = self._reclaim_smelt_slots(pos, plan, timeout_s=transfer_timeout_s)
                return _smelt_failure("smelt_poll_failed", failed, plan, executed, polls=polls, reclaim=reclaim)
            output = _slot_by_index(_slots(furnace), FURNACE_SLOTS["output"])
            poll = {"output": _slot_metrics(output)}
            polls.append(poll)
            if output is not None and not output.empty and _same_item(output.item, output_item) and output.count >= output_count:
                output_ready = True
                break
            time.sleep(poll_interval_s)

        if not output_ready:
            partial_output = self._collect_partial_output(pos, plan, timeout_s=transfer_timeout_s)
            reclaim = self._reclaim_smelt_slots(pos, plan, timeout_s=transfer_timeout_s)
            return ToolResult(
                success=False,
                reason="smelt_partial_timeout" if partial_output else "smelt_timeout",
                can_retry=True,
                next_suggestion="retry smelting with more time or verify the furnace recipe/fuel",
                metrics={
                    **_smelt_plan_metrics(plan),
                    "executed": executed,
                    "polls": polls,
                    "partial_output": partial_output,
                    "reclaim": reclaim,
                },
            )

        output_move = self.transfer_slot(
            pos,
            direction="furnace_to_bot",
            furnace_slot="output",
            bot_slot=plan.output_slot,
            count=plan.output_count,
            timeout_s=transfer_timeout_s,
        )
        executed.append({"kind": "collect_output", "result": output_move.to_payload()})
        if not output_move.success:
            return _smelt_failure("smelt_output_collect_failed", output_move, plan, executed, polls=polls)

        return ToolResult(
            success=True,
            reason="completed",
            can_retry=False,
            metrics={**_smelt_plan_metrics(plan), "executed": executed, "polls": polls},
        )

    def smelt_nearest_furnace(
        self,
        *,
        input_item: str,
        input_count: int,
        fuel_item: str,
        fuel_count: int | None = None,
        output_item: str,
        output_count: int,
        output_slot: int | None = None,
        search_radius: int = 8,
        furnace_types: tuple[str, ...] = DEFAULT_FURNACE_TYPES,
        poll_interval_s: float = 0.5,
        smelt_timeout_s: float = 15.0,
        transfer_timeout_s: float = 2.0,
        approach_timeout_s: float = 15.0,
    ) -> ToolResult:
        targets = find_nearby_block_targets(
            self.body,
            furnace_types,
            search_radius,
            not_found_reason="furnace_not_found",
            limit=64,
        )
        if isinstance(targets, ToolResult):
            return targets

        attempted: list[dict[str, object]] = []
        last_failure: ToolResult | None = None
        for target in targets:
            denied = _guard_furnace_target(self.governance, target.pos, target.block_type)
            if denied is not None:
                return merge_context(
                    denied,
                    {
                        "search_radius": search_radius,
                        "furnace_types": list(furnace_types),
                        "furnace_target": list(target.pos),
                        "furnace_type": target.block_type,
                        "attempted_targets": attempted,
                    },
                )
            approach = ensure_interaction_range(
                self.body,
                self.navigator,
                target.pos,
                timeout_s=approach_timeout_s,
                missing_reason="furnace_navigation_missing",
                failure_prefix="furnace_navigation_failed",
                no_stand_reason="furnace_no_stand_point",
            )
            if isinstance(approach, ToolResult):
                attempted.append(
                    {
                        "furnace_target": list(target.pos),
                        "furnace_type": target.block_type,
                        "approach_result": approach.to_payload(),
                    }
                )
                last_failure = approach
                if approach.reason == "furnace_navigation_missing":
                    return merge_context(
                        approach,
                        {
                            "search_radius": search_radius,
                            "furnace_types": list(furnace_types),
                            "attempted_targets": attempted,
                        },
                    )
                continue

            result = self.smelt_once(
                target.pos,
                input_item=input_item,
                input_count=input_count,
                fuel_item=fuel_item,
                fuel_count=fuel_count,
                output_item=output_item,
                output_count=output_count,
                output_slot=output_slot,
                poll_interval_s=poll_interval_s,
                smelt_timeout_s=smelt_timeout_s,
                transfer_timeout_s=transfer_timeout_s,
            )
            return merge_context(
                result,
                {
                    "furnace_target": list(target.pos),
                    "furnace_type": target.block_type,
                    "search_radius": search_radius,
                    "furnace_types": list(furnace_types),
                    "approach": approach,
                    "attempted_targets": attempted,
                },
            )

        if last_failure is None:
            last_failure = ToolResult(
                success=False,
                reason="furnace_not_found",
                can_retry=True,
                metrics={"search_radius": search_radius, "furnace_types": list(furnace_types)},
            )
        return merge_context(
            last_failure,
            {
                "search_radius": search_radius,
                "furnace_types": list(furnace_types),
                "attempted_targets": attempted,
            },
        )

    def smelt_with_temporary_furnace(
        self,
        furnace_pos: Position,
        *,
        input_item: str,
        input_count: int,
        fuel_item: str,
        fuel_count: int | None = None,
        output_item: str,
        output_count: int,
        output_slot: int | None = None,
        furnace_item: str = "minecraft:furnace",
        place_face: str | None = "up",
        place_context: PlaceContext | str = PlaceContext.DIRECT,
        place_purpose: str = "temporary_furnace",
        poll_interval_s: float = 0.5,
        smelt_timeout_s: float = 15.0,
        transfer_timeout_s: float = 2.0,
        place_timeout_s: float = 10.0,
        reclaim_timeout_s: float = 10.0,
        reclaim_tool: str | None = None,
    ) -> ToolResult:
        if self.work is None:
            return ToolResult(
                success=False,
                reason="furnace_work_runtime_missing",
                can_retry=True,
                next_suggestion="attach BlockWork to place and reclaim a temporary furnace",
                metrics={"furnace_pos": list(furnace_pos), "furnace_item": furnace_item},
            )

        select = _dispatch(self.body, "selectItem", {"item": furnace_item}, timeout_s=transfer_timeout_s)
        if not select.success:
            return ToolResult(
                success=False,
                reason=f"temporary_furnace_select_failed:{select.reason}",
                can_retry=select.can_retry,
                next_suggestion=select.next_suggestion,
                metrics={"furnace_pos": list(furnace_pos), "furnace_item": furnace_item, "select": select.to_payload()},
            )

        place = self.work.place_block(
            furnace_pos,
            furnace_item,
            face=place_face,
            context=place_context,
            purpose=place_purpose,
            timeout_s=place_timeout_s,
        )
        if not place.success:
            return ToolResult(
                success=False,
                reason=f"temporary_furnace_place_failed:{place.reason}",
                can_retry=place.can_retry,
                next_suggestion=place.next_suggestion,
                metrics={
                    "furnace_pos": list(furnace_pos),
                    "furnace_item": furnace_item,
                    "select": select.to_payload(),
                    "place": place.to_payload(),
                },
            )

        smelt = self.smelt_once(
            furnace_pos,
            input_item=input_item,
            input_count=input_count,
            fuel_item=fuel_item,
            fuel_count=fuel_count,
            output_item=output_item,
            output_count=output_count,
            output_slot=output_slot,
            poll_interval_s=poll_interval_s,
            smelt_timeout_s=smelt_timeout_s,
            transfer_timeout_s=transfer_timeout_s,
        )
        reclaim_select = None
        if reclaim_tool is not None:
            reclaim_select = _dispatch(self.body, "selectItem", {"item": reclaim_tool}, timeout_s=transfer_timeout_s)
            if not reclaim_select.success:
                metrics = {
                    "furnace_pos": list(furnace_pos),
                    "furnace_item": furnace_item,
                    "select": select.to_payload(),
                    "place": place.to_payload(),
                    "smelt": smelt.to_payload(),
                    "reclaim_tool": reclaim_tool,
                    "reclaim_select": reclaim_select.to_payload(),
                }
                return ToolResult(
                    success=False,
                    reason=f"temporary_furnace_reclaim_tool_failed:{reclaim_select.reason}",
                    can_retry=reclaim_select.can_retry,
                    next_suggestion=reclaim_select.next_suggestion,
                    metrics=metrics,
                )

        reclaim = self.work.mine_block(
            furnace_pos,
            context=BreakContext.BOT_CLEANUP,
            timeout_s=reclaim_timeout_s,
        )
        metrics = {
            "furnace_pos": list(furnace_pos),
            "furnace_item": furnace_item,
            "select": select.to_payload(),
            "place": place.to_payload(),
            "smelt": smelt.to_payload(),
            "reclaim": reclaim.to_payload(),
        }
        if reclaim_select is not None:
            metrics["reclaim_tool"] = reclaim_tool
            metrics["reclaim_select"] = reclaim_select.to_payload()
        if not smelt.success:
            return ToolResult(
                success=False,
                reason=f"temporary_furnace_smelt_failed:{smelt.reason}",
                can_retry=smelt.can_retry or reclaim.can_retry,
                next_suggestion=smelt.next_suggestion,
                metrics=metrics,
            )
        if not reclaim.success:
            return ToolResult(
                success=False,
                reason=f"temporary_furnace_reclaim_failed:{reclaim.reason}",
                can_retry=reclaim.can_retry,
                next_suggestion=reclaim.next_suggestion,
                metrics=metrics,
            )
        return ToolResult(success=True, reason="completed", can_retry=False, metrics=metrics)

    def smelt_with_nearby_temporary_furnace(
        self,
        *,
        input_item: str,
        input_count: int,
        fuel_item: str,
        fuel_count: int | None = None,
        output_item: str,
        output_count: int,
        output_slot: int | None = None,
        furnace_item: str = "minecraft:furnace",
        radius: int = 2,
        place_context: PlaceContext | str = PlaceContext.DIRECT,
        poll_interval_s: float = 0.5,
        smelt_timeout_s: float = 15.0,
        transfer_timeout_s: float = 2.0,
        place_timeout_s: float = 10.0,
        reclaim_timeout_s: float = 10.0,
        reclaim_tool: str | None = None,
    ) -> ToolResult:
        """Place a temporary furnace at a nearby supported clear site, then smelt.

        This is a conservative furnace-site planning slice. It does not mine,
        replace, or recover terrain; it only chooses a same-Y clear target with
        solid support and delegates placement/smelt/reclaim to the proven
        temporary-furnace transaction.
        """

        if radius < 1:
            raise ValueError("radius must be >= 1")
        if self.work is None:
            return ToolResult(
                success=False,
                reason="furnace_work_runtime_missing",
                can_retry=True,
                next_suggestion="attach BlockWork to place and reclaim a temporary furnace",
                metrics={"furnace_item": furnace_item, "radius": radius},
            )

        origin = _state_block_pos(self.body.get_state().pos)
        scan = _scan_temporary_furnace_sites(self.body, origin, radius)
        if isinstance(scan, ToolResult):
            return scan
        candidates = [candidate for candidate in scan if candidate["candidate"]]
        if not candidates:
            return ToolResult(
                success=False,
                reason="temporary_furnace_no_supported_site",
                can_retry=True,
                next_suggestion="move to a nearby clear supported area or pass an explicit temporary furnace position",
                metrics={
                    "origin": list(origin),
                    "radius": radius,
                    "furnace_item": furnace_item,
                    "candidates": scan,
                },
            )

        chosen = tuple(candidates[0]["target"])
        result = self.smelt_with_temporary_furnace(
            chosen,
            input_item=input_item,
            input_count=input_count,
            fuel_item=fuel_item,
            fuel_count=fuel_count,
            output_item=output_item,
            output_count=output_count,
            output_slot=output_slot,
            furnace_item=furnace_item,
            place_context=place_context,
            place_purpose="temporary_furnace_auto_site",
            poll_interval_s=poll_interval_s,
            smelt_timeout_s=smelt_timeout_s,
            transfer_timeout_s=transfer_timeout_s,
            place_timeout_s=place_timeout_s,
            reclaim_timeout_s=reclaim_timeout_s,
            reclaim_tool=reclaim_tool,
        )
        return merge_context(
            result,
            {
                "temporary_furnace_site": list(chosen),
                "origin": list(origin),
                "radius": radius,
                "site_scan": scan,
            },
        )

    def _smelt_preflight(
        self,
        pos: Position,
        *,
        input_item: str,
        input_count: int,
        fuel_item: str,
        fuel_count: int | None,
        output_item: str,
        output_count: int,
        output_slot: int | None,
    ) -> SmeltPlan | ToolResult:
        target_type = _read_target_type(self.body, pos)
        if isinstance(target_type, ToolResult):
            return target_type
        if target_type not in DEFAULT_FURNACE_TYPES:
            return ToolResult(
                success=False,
                reason="furnace_wrong_type",
                can_retry=False,
                metrics={"furnace_pos": list(pos), "furnace_type": target_type},
            )
        denied = _guard_furnace_target(self.governance, pos, target_type)
        if denied is not None:
            return denied

        furnace = _read_furnace(self.body, pos)
        failed = _perception_failure(furnace)
        if failed is not None:
            return failed
        furnace_slots = _slots(furnace)
        for slot_name in ("input", "fuel", "output"):
            current = _slot_by_index(furnace_slots, FURNACE_SLOTS[slot_name])
            if current is not None and not current.empty:
                return ToolResult(
                    success=False,
                    reason="smelt_furnace_not_empty",
                    can_retry=True,
                    next_suggestion="clear the furnace before starting a new smelt lifecycle",
                    metrics={"furnace_pos": list(pos), "occupied_slot": slot_name, "slot": _slot_metrics(current)},
                )

        inventory = _read_inventory(self.body)
        failed = _perception_failure(inventory)
        if failed is not None:
            return failed
        slots = _slots(inventory)
        input_slot = _find_item_slot(slots, input_item, min_count=input_count)
        fuel_budget = _plan_fuel_budget(
            fuel_item=fuel_item,
            fuel_count=fuel_count,
            input_count=input_count,
            output_count=output_count,
        )
        if isinstance(fuel_budget, ToolResult):
            return merge_context(fuel_budget, {"furnace_pos": list(pos), "fuel_item": fuel_item})
        planned_fuel_count, fuel_auto, fuel_seconds_available, fuel_seconds_required = fuel_budget
        fuel_slot = _find_item_slot(
            slots,
            fuel_item,
            min_count=planned_fuel_count,
            exclude={input_slot} if input_slot is not None else set(),
        )
        if input_slot is None:
            return ToolResult(
                success=False,
                reason="smelt_input_not_available",
                can_retry=False,
                metrics={"furnace_pos": list(pos), "input_item": input_item, "input_count": input_count},
            )
        if fuel_slot is None:
            available = _available_item_count(slots, fuel_item, exclude={input_slot} if input_slot is not None else set())
            return ToolResult(
                success=False,
                reason="smelt_fuel_not_available",
                can_retry=False,
                metrics={
                    "furnace_pos": list(pos),
                    "fuel_item": fuel_item,
                    "fuel_count": planned_fuel_count,
                    "requested_fuel_count": fuel_count,
                    "available_count": available,
                    "fuel_auto": fuel_auto,
                    "fuel_seconds_available": fuel_seconds_available,
                    "fuel_seconds_required": fuel_seconds_required,
                },
            )
        if output_slot is None:
            output_slot = _find_output_slot(slots, output_item, output_count, exclude={input_slot, fuel_slot})
        else:
            candidate = _slot_by_index(slots, output_slot)
            if candidate is not None and not candidate.empty and not _same_item(candidate.item, output_item):
                output_slot = None
        if output_slot is None:
            return ToolResult(
                success=False,
                reason="smelt_output_no_space",
                can_retry=True,
                next_suggestion="free an empty inventory slot or merge compatible output stacks",
                metrics={"furnace_pos": list(pos), "output_item": output_item, "output_count": output_count},
            )
        return SmeltPlan(
            pos=pos,
            input_item=input_item,
            input_count=input_count,
            input_slot=input_slot,
            fuel_item=fuel_item,
            fuel_count=planned_fuel_count,
            fuel_slot=fuel_slot,
            fuel_auto=fuel_auto,
            fuel_seconds_available=fuel_seconds_available,
            fuel_seconds_required=fuel_seconds_required,
            output_item=output_item,
            output_count=output_count,
            output_slot=output_slot,
        )

    def _reclaim_smelt_slots(self, pos: Position, plan: SmeltPlan, *, timeout_s: float) -> list[dict[str, object]]:
        reclaim: list[dict[str, object]] = []
        for furnace_slot in ("input", "fuel"):
            furnace = _read_furnace(self.body, pos)
            failed = _perception_failure(furnace)
            if failed is not None:
                reclaim.append({"furnace_slot": furnace_slot, "success": False, "reason": "furnace_read_failed"})
                continue
            source = _slot_by_index(_slots(furnace), FURNACE_SLOTS[furnace_slot])
            if source is None or source.empty:
                reclaim.append({"furnace_slot": furnace_slot, "success": True, "reason": "already_empty"})
                continue
            inventory = _read_inventory(self.body)
            if _perception_failure(inventory) is not None:
                reclaim.append({"furnace_slot": furnace_slot, "success": False, "reason": "inventory_read_failed"})
                continue
            destination = _first_empty_slot(_slots(inventory), exclude={plan.output_slot})
            if destination is None:
                reclaim.append({"furnace_slot": furnace_slot, "success": False, "reason": "no_empty_slot"})
                continue
            result = self.transfer_slot(
                pos,
                direction="furnace_to_bot",
                furnace_slot=furnace_slot,
                bot_slot=destination,
                timeout_s=timeout_s,
            )
            reclaim.append({"furnace_slot": furnace_slot, "bot_slot": destination, "result": result.to_payload()})
        return reclaim

    def _collect_partial_output(self, pos: Position, plan: SmeltPlan, *, timeout_s: float) -> dict[str, object] | None:
        furnace = _read_furnace(self.body, pos)
        if _perception_failure(furnace) is not None:
            return None
        output = _slot_by_index(_slots(furnace), FURNACE_SLOTS["output"])
        if output is None or output.empty or not _same_item(output.item, plan.output_item):
            return None
        inventory = _read_inventory(self.body)
        if _perception_failure(inventory) is not None:
            return {"success": False, "reason": "inventory_read_failed", "output": _slot_metrics(output)}
        destination = plan.output_slot
        candidate = _slot_by_index(_slots(inventory), destination)
        if candidate is not None and not candidate.empty and not _same_item(candidate.item, plan.output_item):
            replacement = _find_output_slot(_slots(inventory), plan.output_item, output.count, exclude={plan.input_slot, plan.fuel_slot})
            if replacement is None:
                return {"success": False, "reason": "no_output_space", "output": _slot_metrics(output)}
            destination = replacement
        result = self.transfer_slot(
            pos,
            direction="furnace_to_bot",
            furnace_slot="output",
            bot_slot=destination,
            count=output.count,
            timeout_s=timeout_s,
        )
        return {"bot_slot": destination, "output": _slot_metrics(output), "result": result.to_payload()}


def _read_furnace(body: Body, pos: Position) -> PerceptionResult:
    return body.perceive("container", {"pos": list(pos), "start": 0, "limit": 3, "total_slots": 3})


def _dispatch(body: Body, action_name: str, params: dict[str, object], *, timeout_s: float) -> ToolResult:
    action = Action.create(action_name, params)
    accepted = body.execute(action)
    if not (accepted.ok and accepted.accepted):
        return ToolResult(
            success=False,
            reason="body_rejected",
            can_retry=True,
            metrics={
                "action": action_name,
                "action_id": action.id,
                "accepted": {
                    "ok": accepted.ok,
                    "accepted": accepted.accepted,
                    "error": accepted.error,
                    "data": accepted.data,
                },
            },
        )
    terminal = body.await_action_terminal(action.id, timeout_s=timeout_s)
    result = terminal_event_to_tool_result(terminal)
    return ToolResult(
        success=result.success,
        reason=result.reason,
        can_retry=result.can_retry,
        next_suggestion=result.next_suggestion,
        metrics={"action": action_name, "action_id": action.id, **dict(result.metrics or {})},
    )


def _read_inventory(body: Body, page_size: int = 12) -> PerceptionResult:
    start: int | None = 0
    slots: list[dict[str, object]] = []
    last: PerceptionResult | None = None
    while start is not None:
        last = body.perceive("inventory", {"start": start, "limit": page_size})
        if not last.ok:
            return last
        slots.extend(dict(item) for item in last.data.get("slots") or [])
        start = perception_next_cursor(last)
        if start is not None:
            start = int(start)
    if last is None:
        return PerceptionResult(
            bot=body.bot_name,
            scope="inventory",
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


def _state_block_pos(pos: tuple[float, float, float]) -> Position:
    return (int(pos[0] // 1), int(pos[1] // 1), int(pos[2] // 1))


def _scan_temporary_furnace_sites(
    body: Body,
    origin: Position,
    radius: int,
) -> list[dict[str, object]] | ToolResult:
    scanned: list[dict[str, object]] = []
    for target in _temporary_furnace_site_targets(origin, radius):
        target_block = body.perceive("blockAt", {"x": target[0], "y": target[1], "z": target[2]})
        failed = _perception_failure(target_block)
        if failed is not None:
            return failed
        support = (target[0], target[1] - 1, target[2])
        support_block = body.perceive("blockAt", {"x": support[0], "y": support[1], "z": support[2]})
        failed = _perception_failure(support_block)
        if failed is not None:
            return failed

        target_state = str(target_block.data.get("state") or "UNKNOWN")
        support_state = str(support_block.data.get("state") or "UNKNOWN")
        target_clear = target_state == "CLEAR"
        support_solid = support_state == "SOLID"
        scanned.append(
            {
                "target": list(target),
                "support": list(support),
                "target_block": normalize_block_type(str(target_block.data.get("type") or "unknown")),
                "target_state": target_state,
                "support_block": normalize_block_type(str(support_block.data.get("type") or "unknown")),
                "support_state": support_state,
                "candidate": target_clear and support_solid,
            }
        )
    return scanned


def _temporary_furnace_site_targets(origin: Position, radius: int) -> tuple[Position, ...]:
    candidates: list[tuple[int, float, Position]] = []
    for dz in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            if dx == 0 and dz == 0:
                continue
            target = (origin[0] + dx, origin[1], origin[2] + dz)
            manhattan = abs(dx) + abs(dz)
            distance = dist(
                (float(origin[0]), float(origin[1]), float(origin[2])),
                (float(target[0]), float(target[1]), float(target[2])),
            )
            candidates.append((manhattan, distance, target))
    candidates.sort(key=lambda item: (item[0], item[1], item[2][2], item[2][0]))
    return tuple(target for _manhattan, _distance, target in candidates)


def _slots(perception: PerceptionResult) -> list[InventorySlot]:
    return [InventorySlot.from_payload(slot) for slot in perception.data.get("slots") or []]


def _read_target_type(body: Body, pos: Position) -> str | ToolResult:
    block = body.perceive("blockAt", {"x": pos[0], "y": pos[1], "z": pos[2]})
    failed = _perception_failure(block)
    if failed is not None:
        return failed
    return normalize_block_type(str(block.data.get("type") or "unknown"))


def _plan_clear(pos: Position, furnace_slots: list[InventorySlot], inventory_slots: list[InventorySlot]) -> FurnaceClearPlan:
    by_slot = {slot.slot: slot for slot in furnace_slots}
    empty_bot_slots = [slot.slot for slot in inventory_slots if slot.empty]
    occupied = [
        (name, by_slot.get(index))
        for name, index in ((slot_name, FURNACE_SLOTS[slot_name]) for slot_name in CLEAR_ORDER)
        if by_slot.get(index) is not None and not by_slot[index].empty
    ]
    moves: list[dict[str, object]] = []
    for furnace_slot_name, furnace_slot in occupied:
        if not empty_bot_slots:
            break
        moves.append(
            {
                "furnace_slot": furnace_slot_name,
                "furnace_slot_index": furnace_slot.slot,
                "bot_slot": empty_bot_slots.pop(0),
                "item": furnace_slot.item,
                "count": furnace_slot.count,
            }
        )
    return FurnaceClearPlan(pos=pos, moves=tuple(moves), occupied_furnace_slots=len(occupied))


def _perception_failure(perception: PerceptionResult) -> ToolResult | None:
    failed = shared_perception_failure(perception)
    if failed is None:
        return None
    if failed.next_suggestion == "refresh world and inventory facts before attempting the interaction":
        return ToolResult(
            success=False,
            reason=failed.reason,
            can_retry=failed.can_retry,
            next_suggestion="refresh furnace and inventory facts before moving furnace contents",
            metrics=failed.metrics,
        )
    return failed


def _acceptance_failure(
    result: Result,
    plan: FurnaceClearPlan,
    executed: list[dict[str, object]],
) -> ToolResult | None:
    if result.ok and result.accepted:
        return None
    metrics = _plan_metrics(plan)
    metrics["executed"] = executed
    metrics["accepted"] = {
        "ok": result.ok,
        "accepted": result.accepted,
        "error": result.error,
        "data": result.data,
    }
    return ToolResult(success=False, reason="body_rejected", can_retry=True, metrics=metrics)


def _plan_metrics(plan: FurnaceClearPlan) -> dict[str, object]:
    return {
        "furnace_pos": list(plan.pos),
        "occupied_furnace_slots": plan.occupied_furnace_slots,
        "moves": list(plan.moves),
    }


def _slot_by_index(slots: list[InventorySlot], index: int) -> InventorySlot | None:
    for slot in slots:
        if slot.slot == index:
            return slot
    return None


def _slot_metrics(slot: InventorySlot | None) -> dict[str, object] | None:
    if slot is None:
        return None
    return {"slot": slot.slot, "item": slot.item, "count": slot.count, "empty": slot.empty}


def _same_item(actual: str | None, wanted: str) -> bool:
    if actual is None:
        return False
    return actual == wanted or actual == f"minecraft:{wanted}" or f"minecraft:{actual}" == wanted


def _find_item_slot(slots: list[InventorySlot], item: str, *, min_count: int, exclude: set[int] | None = None) -> int | None:
    excluded = exclude or set()
    for slot in slots:
        if slot.slot in excluded or slot.empty:
            continue
        if _same_item(slot.item, item) and slot.count >= min_count:
            return slot.slot
    return None


def _available_item_count(slots: list[InventorySlot], item: str, *, exclude: set[int] | None = None) -> int:
    excluded = exclude or set()
    return sum(
        slot.count
        for slot in slots
        if slot.slot not in excluded and not slot.empty and _same_item(slot.item, item)
    )


def _fuel_seconds_per_item(item: str) -> float | None:
    normalized = item.removeprefix("minecraft:")
    return FUEL_BURN_SECONDS.get(normalized)


def _plan_fuel_budget(
    *,
    fuel_item: str,
    fuel_count: int | None,
    input_count: int,
    output_count: int,
) -> tuple[int, bool, float, float] | ToolResult:
    smelt_count = max(input_count, output_count)
    required_seconds = smelt_count * DEFAULT_SMELT_SECONDS_PER_ITEM
    seconds_per_fuel = _fuel_seconds_per_item(fuel_item)
    if fuel_count is None:
        if seconds_per_fuel is None:
            return ToolResult(
                success=False,
                reason="smelt_unknown_fuel_value",
                can_retry=False,
                next_suggestion="pass an explicit fuel_count for unknown furnace fuel items",
                metrics={
                    "fuel_item": fuel_item,
                    "input_count": input_count,
                    "output_count": output_count,
                    "fuel_seconds_required": required_seconds,
                },
            )
        planned_count = max(1, int((required_seconds + seconds_per_fuel - 0.000001) // seconds_per_fuel))
        return (planned_count, True, planned_count * seconds_per_fuel, required_seconds)
    available_seconds = fuel_count * seconds_per_fuel if seconds_per_fuel is not None else 0.0
    return (fuel_count, False, available_seconds, required_seconds)


def _find_output_slot(slots: list[InventorySlot], item: str, count: int, *, exclude: set[int]) -> int | None:
    empty: int | None = None
    for slot_index in range(0, 36):
        if slot_index in exclude:
            continue
        slot = _slot_by_index(slots, slot_index)
        if slot is None or slot.empty:
            if empty is None:
                empty = slot_index
            continue
        if _same_item(slot.item, item) and slot.count + count <= 64:
            return slot_index
    return empty


def _first_empty_slot(slots: list[InventorySlot], *, exclude: set[int] | None = None) -> int | None:
    excluded = exclude or set()
    for slot_index in range(0, 36):
        if slot_index in excluded:
            continue
        slot = _slot_by_index(slots, slot_index)
        if slot is None or slot.empty:
            return slot_index
    return None


def _smelt_plan_metrics(plan: SmeltPlan) -> dict[str, object]:
    return {
        "furnace_pos": list(plan.pos),
        "input": {"item": plan.input_item, "count": plan.input_count, "bot_slot": plan.input_slot},
        "fuel": {
            "item": plan.fuel_item,
            "count": plan.fuel_count,
            "bot_slot": plan.fuel_slot,
            "auto": plan.fuel_auto,
            "seconds_available": plan.fuel_seconds_available,
            "seconds_required": plan.fuel_seconds_required,
        },
        "output": {"item": plan.output_item, "count": plan.output_count, "bot_slot": plan.output_slot},
    }


def _smelt_failure(
    prefix: str,
    result: ToolResult,
    plan: SmeltPlan,
    executed: list[dict[str, object]],
    *,
    polls: list[dict[str, object]] | None = None,
    reclaim: list[dict[str, object]] | None = None,
) -> ToolResult:
    metrics = {**_smelt_plan_metrics(plan), "executed": executed}
    if polls is not None:
        metrics["polls"] = polls
    if reclaim is not None:
        metrics["reclaim"] = reclaim
    return ToolResult(
        success=False,
        reason=f"{prefix}:{result.reason}",
        can_retry=result.can_retry,
        next_suggestion=result.next_suggestion,
        metrics=metrics,
    )


def _verify_transfer(
    *,
    direction: str,
    furnace_slot: str,
    moved_count: int,
    before_furnace_slot: InventorySlot | None,
    after_furnace_slot: InventorySlot | None,
    before_bot_slot: InventorySlot | None,
    after_bot_slot: InventorySlot | None,
) -> bool:
    if moved_count <= 0:
        return False

    before_furnace_count = 0 if before_furnace_slot is None or before_furnace_slot.empty else before_furnace_slot.count
    after_furnace_count = 0 if after_furnace_slot is None or after_furnace_slot.empty else after_furnace_slot.count
    before_bot_count = 0 if before_bot_slot is None or before_bot_slot.empty else before_bot_slot.count
    after_bot_count = 0 if after_bot_slot is None or after_bot_slot.empty else after_bot_slot.count

    if direction == "furnace_to_bot":
        if before_furnace_slot is None or before_furnace_slot.empty:
            return False
        item = before_furnace_slot.item
        if after_furnace_count != before_furnace_count - moved_count:
            return False
        if after_furnace_count > 0 and (after_furnace_slot is None or after_furnace_slot.empty or after_furnace_slot.item != item):
            return False
        if after_bot_count != before_bot_count + moved_count:
            return False
        if after_bot_slot is None or after_bot_slot.empty or after_bot_slot.item != item:
            return False
        if before_bot_count > 0 and (before_bot_slot is None or before_bot_slot.empty or before_bot_slot.item != item):
            return False
        return True

    if before_bot_slot is None or before_bot_slot.empty:
        return False
    item = before_bot_slot.item
    if after_bot_count != before_bot_count - moved_count:
        return False
    if after_bot_count > 0 and (after_bot_slot is None or after_bot_slot.empty or after_bot_slot.item != item):
        return False
    if before_furnace_count > 0 and (before_furnace_slot is None or before_furnace_slot.empty or before_furnace_slot.item != item):
        return False
    if furnace_slot == "fuel" and after_furnace_count <= before_furnace_count + moved_count:
        if after_furnace_count > 0 and (after_furnace_slot is None or after_furnace_slot.empty or after_furnace_slot.item != item):
            return False
        return True
    if after_furnace_count != before_furnace_count + moved_count:
        return False
    if after_furnace_slot is None or after_furnace_slot.empty or after_furnace_slot.item != item:
        return False
    return True


def _guard_furnace_target(
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
        reason="furnace_denied",
        can_retry=False,
        next_suggestion="choose a furnace inside an allowed natural work region instead of touching protected or unknown-provenance workstation blocks",
        metrics={
            "furnace_pos": list(pos),
            "furnace_type": block_type,
            "legality": _decision_payload(decision),
        },
    )


def _decision_payload(decision) -> dict[str, object]:
    payload = asdict(decision)
    payload["allowed"] = bool(decision.allowed)
    return payload
