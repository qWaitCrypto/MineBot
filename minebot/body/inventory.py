"""Body transaction inventory workflows."""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil

from minebot.body.block_work import BlockWork
from minebot.body.interaction_support import (
    InteractionNavigator,
    ensure_interaction_range,
    find_nearby_block_targets,
    merge_context,
)

from minebot.contract import Body
from minebot.contract import terminal_event_to_tool_result
from minebot.contract import Action, BreakContext, InteractionContext, InventorySlot, PerceptionResult, PlaceContext, Result, ToolResult, perception_next_cursor
from minebot.game.governance import GovernancePolicy


@dataclass(frozen=True)
class DiscardPlan:
    item: str
    requested_count: int
    available: int
    planned_count: int
    moves: tuple[dict[str, object], ...]


@dataclass(frozen=True)
class EquipPlan:
    item: str
    target: str
    target_slot: int
    source_slot: int | None
    stage_slot: int | None
    move_count: int | None
    source_item: str | None
    source_count: int
    target_before_item: str | None
    target_before_count: int


@dataclass(frozen=True)
class CraftRecipeVariant:
    output_item: str
    output_count: int
    recipe_kind: str
    width: int
    height: int
    ingredient_groups: tuple[tuple[str, ...] | None, ...]
    requires_table: bool


@dataclass(frozen=True)
class CraftPlan:
    item: str
    requested_count: int
    crafted_count: int
    output_slot: int
    variant_index: int
    variant: CraftRecipeVariant
    inputs: tuple[dict[str, object], ...]
    remainders: tuple[dict[str, object], ...]


EQUIP_TARGET_SLOTS = {
    "feet": 36,
    "legs": 37,
    "chest": 38,
    "head": 39,
    "offhand": 40,
}

EQUIP_TARGET_ALIASES = {
    "mainhand": "mainhand",
    "main_hand": "mainhand",
    "hand": "mainhand",
    "offhand": "offhand",
    "off_hand": "offhand",
    "shield": "offhand",
    "head": "head",
    "helmet": "head",
    "chest": "chest",
    "chestplate": "chest",
    "torso": "chest",
    "legs": "legs",
    "leggings": "legs",
    "feet": "feet",
    "boots": "feet",
}

OFFHAND_AUTO_ITEMS = {
    "shield",
    "totem_of_undying",
    "arrow",
    "spectral_arrow",
    "firework_rocket",
}


class InventoryTransactions:
    """Inventory workflows above raw hotbar/slot primitives."""

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

    def discard_item(
        self,
        *,
        item: str,
        count: int,
        timeout_s: float = 2.0,
    ) -> ToolResult:
        if count <= 0:
            return ToolResult(
                success=False,
                reason="invalid_count",
                can_retry=False,
                metrics={"item": item, "requested_count": count},
            )

        inventory = _read_inventory(self.body)
        failed = _perception_failure(inventory)
        if failed is not None:
            return failed

        slots = [InventorySlot.from_payload(slot) for slot in inventory.data.get("slots") or []]
        plan = _plan_discard(item, count, slots)
        if plan.planned_count <= 0:
            reason = "item_not_available" if plan.available <= 0 else "hotbar_full"
            return ToolResult(
                success=False,
                reason=reason,
                can_retry=reason == "hotbar_full",
                next_suggestion="free an empty hotbar slot before discarding non-hotbar items"
                if reason == "hotbar_full"
                else "choose an item currently present in inventory",
                metrics=_plan_metrics(plan),
            )

        executed: list[dict[str, object]] = []
        dropped_total = 0
        for move in plan.moves:
            if move["kind"] == "stage":
                action = Action.create(
                    "moveItem",
                    {
                        "from_slot": move["from_slot"],
                        "to_slot": move["to_slot"],
                        "count": move["count"],
                    },
                )
                accepted = self.body.execute(action)
                rejected = _acceptance_failure(accepted, plan, executed)
                if rejected is not None:
                    return rejected
                terminal = self.body.await_action_terminal(action.id, timeout_s=timeout_s)
                result = terminal_event_to_tool_result(terminal)
                executed.append(
                    {
                        "kind": "stage",
                        "action_id": action.id,
                        "from_slot": move["from_slot"],
                        "to_slot": move["to_slot"],
                        "count": move["count"],
                        "success": result.success,
                        "reason": result.reason,
                    }
                )
                if not result.success:
                    return _terminal_failure("discard_stage_failed", result, plan, executed, dropped_total)

            mode = str(move.get("mode") or "all")
            action = Action.create("dropItem", {"slot": move["drop_slot"], "mode": mode})
            accepted = self.body.execute(action)
            rejected = _acceptance_failure(accepted, plan, executed)
            if rejected is not None:
                return rejected
            terminal = self.body.await_action_terminal(action.id, timeout_s=timeout_s)
            result = terminal_event_to_tool_result(terminal)
            before = int((result.metrics or {}).get("count_before") or 0)
            after = int((result.metrics or {}).get("count_after") or 0)
            dropped = max(0, before - after)
            dropped_total += dropped
            executed.append(
                {
                    "kind": "drop",
                    "action_id": action.id,
                    "slot": move["drop_slot"],
                    "requested_count": move["count"],
                    "dropped_count": dropped,
                    "success": result.success,
                    "reason": result.reason,
                }
            )
            if not result.success:
                return _terminal_failure("discard_drop_failed", result, plan, executed, dropped_total)

        return ToolResult(
            success=dropped_total >= count,
            reason="completed" if dropped_total >= count else "partial",
            can_retry=dropped_total < count,
            metrics={**_plan_metrics(plan), "executed": executed, "dropped_count": dropped_total},
        )

    def equip_item(
        self,
        *,
        item: str,
        target: str = "auto",
        timeout_s: float = 2.0,
    ) -> ToolResult:
        inventory = _read_inventory(self.body)
        failed = _perception_failure(inventory)
        if failed is not None:
            return failed

        slots = [InventorySlot.from_payload(slot) for slot in inventory.data.get("slots") or []]
        normalized_target = _normalize_equip_target(target, item)
        if normalized_target is None:
            return ToolResult(
                success=False,
                reason="invalid_target",
                can_retry=False,
                next_suggestion="specify mainhand/offhand/head/chest/legs/feet explicitly for ambiguous items",
                metrics={"item": item, "target": target},
            )

        if normalized_target == "mainhand":
            result = _dispatch_select_item(self.body, item, timeout_s=timeout_s)
            reason = result.reason
            if not result.success and reason == "not_in_inventory":
                reason = "item_not_available"
            metrics = {"item": item, "target": normalized_target, "select": dict(result.metrics or {})}
            return ToolResult(
                success=result.success,
                reason="completed" if result.success else reason,
                can_retry=result.can_retry,
                next_suggestion=result.next_suggestion,
                metrics=metrics,
            )

        plan = _plan_equip(item, normalized_target, slots)
        if plan is None:
            return ToolResult(
                success=False,
                reason="item_not_available",
                can_retry=False,
                next_suggestion="choose an item currently present in inventory",
                metrics={"item": item, "target": normalized_target},
            )
        if plan.source_slot is None:
            return ToolResult(
                success=True,
                reason="already_equipped",
                can_retry=False,
                metrics=_equip_plan_metrics(plan),
            )
        if plan.stage_slot == -1:
            return ToolResult(
                success=False,
                reason="no_swap_space",
                can_retry=True,
                next_suggestion="free an empty carry slot before swapping equipped items",
                metrics=_equip_plan_metrics(plan),
            )

        executed: list[dict[str, object]] = []
        if plan.stage_slot is not None:
            staged = _execute_move_item(
                self.body,
                from_slot=plan.target_slot,
                to_slot=plan.stage_slot,
                count=None,
                timeout_s=timeout_s,
            )
            executed.append(
                {
                    "kind": "stage_existing",
                    "from_slot": plan.target_slot,
                    "to_slot": plan.stage_slot,
                    "success": staged.success,
                    "reason": staged.reason,
                    "metrics": dict(staged.metrics or {}),
                }
            )
            if not staged.success:
                return _equip_terminal_failure("equip_stage_failed", staged, plan, executed)

        moved = _execute_move_item(
            self.body,
            from_slot=plan.source_slot,
            to_slot=plan.target_slot,
            count=plan.move_count,
            timeout_s=timeout_s,
        )
        executed.append(
            {
                "kind": "equip_move",
                "from_slot": plan.source_slot,
                "to_slot": plan.target_slot,
                "count": plan.move_count,
                "success": moved.success,
                "reason": moved.reason,
                "metrics": dict(moved.metrics or {}),
            }
        )
        if not moved.success:
            return _equip_terminal_failure("equip_move_failed", moved, plan, executed)

        after_inventory = _read_inventory(self.body)
        failed = _perception_failure(after_inventory)
        if failed is not None:
            return failed
        after_slots = [InventorySlot.from_payload(slot) for slot in after_inventory.data.get("slots") or []]
        after_target = _slot_by_index(after_slots, plan.target_slot)
        if after_target is None or after_target.empty or not _same_item(after_target.item, item):
            return ToolResult(
                success=False,
                reason="equip_unverified",
                can_retry=True,
                next_suggestion="re-read inventory and verify the target equipment slot",
                metrics={
                    **_equip_plan_metrics(plan),
                    "executed": executed,
                    "target_after": _slot_metrics(after_target),
                },
            )

        return ToolResult(
            success=True,
            reason="completed",
            can_retry=False,
            metrics={
                **_equip_plan_metrics(plan),
                "executed": executed,
                "target_after": _slot_metrics(after_target),
            },
        )

    def craft_exact(
        self,
        *,
        inputs: list[dict[str, object]],
        output: dict[str, object],
        remainders: list[dict[str, object]] | None = None,
        max_stack: int = 64,
        timeout_s: float = 2.0,
    ) -> ToolResult:
        normalized_inputs = _normalize_craft_inputs(inputs)
        normalized_output = _normalize_craft_output(output)
        normalized_remainders = _normalize_craft_remainders(remainders or [])
        if not normalized_inputs or normalized_output is None or normalized_remainders is None:
            return ToolResult(
                success=False,
                reason="invalid_craft_request",
                can_retry=False,
                next_suggestion="provide at least one explicit input slot plus one explicit output slot/item/count",
                metrics={"inputs": inputs, "output": output, "remainders": remainders, "max_stack": max_stack},
            )

        before_inventory = _read_inventory(self.body)
        failed = _perception_failure(before_inventory)
        if failed is not None:
            return failed
        before_slots = [InventorySlot.from_payload(slot) for slot in before_inventory.data.get("slots") or []]

        dispatch = _dispatch(
            self.body,
            "craftItem",
            {
                "inputs": [dict(entry) for entry in normalized_inputs],
                "output": dict(normalized_output),
                "remainders": [dict(entry) for entry in normalized_remainders],
                "max_stack": max_stack,
            },
            timeout_s=timeout_s,
        )
        metrics = {
            "inputs": [dict(entry) for entry in normalized_inputs],
            "output": dict(normalized_output),
            "remainders": [dict(entry) for entry in normalized_remainders],
            "max_stack": max_stack,
            "input_before": [
                _slot_metrics(_slot_by_index(before_slots, int(entry["slot"])))
                for entry in normalized_inputs
            ],
            "output_before": _slot_metrics(_slot_by_index(before_slots, int(normalized_output["slot"]))),
        }
        if not dispatch.success:
            return ToolResult(
                success=False,
                reason=dispatch.reason,
                can_retry=dispatch.can_retry,
                next_suggestion=dispatch.next_suggestion,
                metrics={**metrics, "craft": dict(dispatch.metrics or {})},
            )

        after_inventory = _read_inventory(self.body)
        failed = _perception_failure(after_inventory)
        if failed is not None:
            return ToolResult(
                success=False,
                reason=failed.reason,
                can_retry=failed.can_retry,
                next_suggestion=failed.next_suggestion,
                metrics={**metrics, "craft": dict(dispatch.metrics or {}), "after_read": dict(failed.metrics or {})},
            )
        after_slots = [InventorySlot.from_payload(slot) for slot in after_inventory.data.get("slots") or []]
        metrics["input_after"] = [
            _slot_metrics(_slot_by_index(after_slots, int(entry["slot"])))
            for entry in normalized_inputs
        ]
        metrics["output_after"] = _slot_metrics(_slot_by_index(after_slots, int(normalized_output["slot"])))
        metrics["craft"] = dict(dispatch.metrics or {})

        if not _craft_matches_expectation(
            before_slots,
            after_slots,
            normalized_inputs,
            normalized_output,
            normalized_remainders,
        ):
            return ToolResult(
                success=False,
                reason="craft_unverified",
                can_retry=True,
                next_suggestion="re-read the involved inventory slots and verify the requested craft did not leave an unexpected residue",
                metrics=metrics,
            )

        return ToolResult(
            success=True,
            reason="completed",
            can_retry=False,
            metrics=metrics,
        )

    def cleanup_crafting_residue(
        self,
        *,
        residue_slots: tuple[int, ...] = (41, 42, 43, 44),
        destination_slots: tuple[int, ...] = tuple(range(0, 36)),
        max_stack: int = 64,
        timeout_s: float = 2.0,
    ) -> ToolResult:
        residue_set = set(residue_slots)
        destination_order = tuple(slot for slot in destination_slots if slot not in residue_set)
        if not residue_slots or not destination_order:
            return ToolResult(
                success=False,
                reason="invalid_residue_cleanup_request",
                can_retry=False,
                metrics={"residue_slots": list(residue_slots), "destination_slots": list(destination_order)},
            )

        executed: list[dict[str, object]] = []
        while True:
            inventory = _read_inventory(self.body)
            failed = _perception_failure(inventory)
            if failed is not None:
                return failed
            slots = [InventorySlot.from_payload(slot) for slot in inventory.data.get("slots") or []]
            residue = _first_residue(slots, residue_slots)
            if residue is None:
                return ToolResult(
                    success=True,
                    reason="completed" if executed else "already_clean",
                    can_retry=False,
                    metrics={
                        "residue_slots": list(residue_slots),
                        "destination_slots": list(destination_order),
                        "executed": executed,
                    },
                )
            destination = _choose_residue_destination(residue, slots, destination_order, max_stack=max_stack)
            if destination is None:
                return ToolResult(
                    success=False,
                    reason="crafting_residue_no_space",
                    can_retry=True,
                    next_suggestion="free an empty inventory slot or merge compatible stacks before crafting again",
                    metrics={
                        "residue_slots": list(residue_slots),
                        "destination_slots": list(destination_order),
                        "remaining_residue": _slot_metrics(residue),
                        "executed": executed,
                    },
                )

            move_count = min(residue.count, destination["room"])
            moved = _execute_move_item(
                self.body,
                from_slot=residue.slot,
                to_slot=destination["slot"],
                count=move_count,
                timeout_s=timeout_s,
            )
            executed.append(
                {
                    "from_slot": residue.slot,
                    "to_slot": destination["slot"],
                    "item": residue.item,
                    "requested_count": move_count,
                    "success": moved.success,
                    "reason": moved.reason,
                    "metrics": dict(moved.metrics or {}),
                }
            )
            if not moved.success:
                return ToolResult(
                    success=False,
                    reason=f"crafting_residue_move_failed:{moved.reason}",
                    can_retry=moved.can_retry,
                    next_suggestion=moved.next_suggestion,
                    metrics={
                        "residue_slots": list(residue_slots),
                        "destination_slots": list(destination_order),
                        "remaining_residue": _slot_metrics(residue),
                        "executed": executed,
                    },
                )

    def craft_recipe(
        self,
        *,
        item: str,
        count: int = 1,
        output_slot: int | None = None,
        search_radius: int = 8,
        residue_slots: tuple[int, ...] = (41, 42, 43, 44),
        destination_slots: tuple[int, ...] = tuple(range(0, 36)),
        crafting_table_item: str = "minecraft:crafting_table",
        temporary_table_radius: int = 2,
        temporary_table_context: PlaceContext | str = PlaceContext.DIRECT,
        auto_equip: bool = False,
        residue_timeout_s: float = 4.0,
        craft_timeout_s: float = 4.0,
        approach_timeout_s: float = 12.0,
        place_timeout_s: float = 12.0,
        reclaim_timeout_s: float = 12.0,
        keep_temporary_table: bool = False,
        cleanup_existing_bot_table: bool = False,
    ) -> ToolResult:
        if count <= 0:
            return ToolResult(
                success=False,
                reason="invalid_craft_count",
                can_retry=False,
                metrics={"item": item, "requested_count": count},
            )

        cleanup_before = self.cleanup_crafting_residue(
            residue_slots=residue_slots,
            destination_slots=destination_slots,
            timeout_s=residue_timeout_s,
        )
        if not cleanup_before.success and cleanup_before.reason != "already_clean":
            return ToolResult(
                success=False,
                reason=f"craft_residue_preflight_failed:{cleanup_before.reason}",
                can_retry=cleanup_before.can_retry,
                next_suggestion=cleanup_before.next_suggestion,
                metrics={
                    "item": item,
                    "requested_count": count,
                    "cleanup_before": cleanup_before.to_payload(),
                },
            )

        inventory = _read_inventory(self.body)
        failed = _perception_failure(inventory)
        if failed is not None:
            return failed
        slots = [InventorySlot.from_payload(slot) for slot in inventory.data.get("slots") or []]

        recipe_perception = _read_recipe_data(self.body, item)
        failed = _perception_failure(recipe_perception)
        if failed is not None:
            return ToolResult(
                success=False,
                reason=f"recipe_data_failed:{failed.reason}",
                can_retry=failed.can_retry,
                next_suggestion=failed.next_suggestion,
                metrics={"item": item, "requested_count": count, "recipe_data": dict(recipe_perception.data or {})},
            )
        variants = _parse_recipe_variants(item, recipe_perception)
        if isinstance(variants, ToolResult):
            return variants

        plan = _choose_craft_plan(slots, item=item, count=count, output_slot=output_slot, variants=variants)
        if isinstance(plan, ToolResult):
            return plan

        workspace = None
        if plan.variant.requires_table:
            workspace = self._resolve_crafting_workspace(
                item=item,
                crafting_table_item=crafting_table_item,
                search_radius=search_radius,
                temporary_table_radius=temporary_table_radius,
                temporary_table_context=temporary_table_context,
                approach_timeout_s=approach_timeout_s,
                place_timeout_s=place_timeout_s,
            )
            if isinstance(workspace, ToolResult):
                return merge_context(
                    workspace,
                    {
                        "item": item,
                        "requested_count": count,
                        "craft_plan": _craft_plan_metrics(plan),
                        "cleanup_before": cleanup_before.to_payload(),
                    },
                )

        craft = self.craft_exact(
            inputs=[dict(entry) for entry in plan.inputs],
            output={"slot": plan.output_slot, "item": plan.item, "count": plan.crafted_count},
            remainders=[dict(entry) for entry in plan.remainders],
            timeout_s=craft_timeout_s,
        )

        cleanup_after = self.cleanup_crafting_residue(
            residue_slots=residue_slots,
            destination_slots=destination_slots,
            timeout_s=residue_timeout_s,
        )
        reclaim = None
        if isinstance(workspace, dict) and workspace.get("mode") == "temporary_table":
            if keep_temporary_table:
                workspace["retained"] = True
            else:
                reclaim = self.work.mine_block(
                    tuple(workspace["table_pos"]),
                    context=BreakContext.BOT_CLEANUP,
                    timeout_s=reclaim_timeout_s,
                )
        elif isinstance(workspace, dict) and workspace.get("mode") == "existing_table" and cleanup_existing_bot_table:
            reclaim = self.work.mine_block(
                tuple(workspace["table_pos"]),
                context=BreakContext.BOT_CLEANUP,
                timeout_s=reclaim_timeout_s,
            )

        equip = None
        if craft.success and auto_equip and _infer_equip_target(plan.item) != "mainhand":
            equip = self.equip_item(item=plan.item, target="auto", timeout_s=craft_timeout_s)

        metrics = {
            "item": item,
            "requested_count": count,
            "cleanup_before": cleanup_before.to_payload(),
            "craft_plan": _craft_plan_metrics(plan),
            "craft": craft.to_payload(),
            "cleanup_after": cleanup_after.to_payload(),
        }
        if workspace is not None:
            metrics["workspace"] = workspace if isinstance(workspace, dict) else workspace.to_payload()
        if reclaim is not None:
            metrics["reclaim"] = reclaim.to_payload()
        if equip is not None:
            metrics["equip"] = equip.to_payload()

        if not craft.success:
            reason = f"craft_recipe_failed:{craft.reason}"
            if reclaim is not None and not reclaim.success:
                reason = f"{reason}:reclaim_failed:{reclaim.reason}"
            return ToolResult(
                success=False,
                reason=reason,
                can_retry=craft.can_retry or bool(reclaim is not None and reclaim.can_retry),
                next_suggestion=craft.next_suggestion,
                metrics=metrics,
            )
        if not cleanup_after.success and cleanup_after.reason != "already_clean":
            return ToolResult(
                success=False,
                reason=f"craft_residue_post_failed:{cleanup_after.reason}",
                can_retry=cleanup_after.can_retry,
                next_suggestion=cleanup_after.next_suggestion,
                metrics=metrics,
            )
        if equip is not None and not equip.success:
            return ToolResult(
                success=False,
                reason=f"craft_auto_equip_failed:{equip.reason}",
                can_retry=equip.can_retry,
                next_suggestion=equip.next_suggestion,
                metrics=metrics,
            )
        return ToolResult(success=True, reason="completed", can_retry=False, metrics=metrics)

    def _resolve_crafting_workspace(
        self,
        *,
        item: str,
        crafting_table_item: str,
        search_radius: int,
        temporary_table_radius: int,
        temporary_table_context: PlaceContext | str,
        approach_timeout_s: float,
        place_timeout_s: float,
    ) -> dict[str, object] | ToolResult:
        attempted: list[dict[str, object]] = []
        if self.governance is not None:
            targets = find_nearby_block_targets(
                self.body,
                ("crafting_table",),
                search_radius,
                not_found_reason="crafting_table_not_found",
            )
            if not isinstance(targets, ToolResult):
                for target in targets:
                    decision = self.governance.can_interact(
                        target.pos,
                        target.block_type,
                        InteractionContext.ACTIVATE,
                    )
                    if not decision.allowed:
                        attempted.append(
                            {
                                "table_pos": list(target.pos),
                                "table_type": target.block_type,
                                "interaction_denied": decision.reason,
                            }
                        )
                        continue
                    approach = ensure_interaction_range(
                        self.body,
                        self.navigator,
                        target.pos,
                        timeout_s=approach_timeout_s,
                        missing_reason="crafting_table_navigation_missing",
                        failure_prefix="crafting_table_navigation_failed",
                        no_stand_reason="crafting_table_no_stand_point",
                    )
                    if isinstance(approach, ToolResult):
                        attempted.append(
                            {
                                "table_pos": list(target.pos),
                                "table_type": target.block_type,
                                "approach_result": approach.to_payload(),
                            }
                        )
                        continue
                    return {
                        "mode": "existing_table",
                        "table_pos": list(target.pos),
                        "table_type": target.block_type,
                        "approach": approach,
                        "attempted_targets": attempted,
                    }

        if self.work is None:
            return ToolResult(
                success=False,
                reason="crafting_table_not_available",
                can_retry=True,
                next_suggestion="move near an allowed crafting table or carry one before retrying the table recipe",
                metrics={"item": item, "crafting_table_item": crafting_table_item, "attempted_targets": attempted},
            )

        select = _dispatch_select_item(self.body, crafting_table_item, timeout_s=place_timeout_s)
        if not select.success:
            return ToolResult(
                success=False,
                reason=f"crafting_table_select_failed:{select.reason}",
                can_retry=select.can_retry,
                next_suggestion=select.next_suggestion,
                metrics={"item": item, "crafting_table_item": crafting_table_item, "attempted_targets": attempted},
            )
        place = self.work.place_here(
            crafting_table_item,
            radius=temporary_table_radius,
            context=temporary_table_context,
            purpose="workstation",
            timeout_s=place_timeout_s,
        )
        if not place.success:
            return ToolResult(
                success=False,
                reason=f"crafting_table_place_failed:{place.reason}",
                can_retry=place.can_retry,
                next_suggestion=place.next_suggestion,
                metrics={
                    "item": item,
                    "crafting_table_item": crafting_table_item,
                    "attempted_targets": attempted,
                    "select": select.to_payload(),
                    "place": place.to_payload(),
                },
            )
        place_metrics = (place.metrics or {}).get("place_here") or {}
        target = place_metrics.get("chosen_target")
        if not target:
            return ToolResult(
                success=False,
                reason="crafting_table_place_unverified",
                can_retry=True,
                next_suggestion="re-read the placed workstation target before crafting",
                metrics={
                    "item": item,
                    "crafting_table_item": crafting_table_item,
                    "attempted_targets": attempted,
                    "select": select.to_payload(),
                    "place": place.to_payload(),
                },
            )
        return {
            "mode": "temporary_table",
            "table_pos": list(target),
            "table_type": "crafting_table",
            "select": select.to_payload(),
            "place": place.to_payload(),
            "attempted_targets": attempted,
        }


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


def _plan_discard(item: str, count: int, slots: list[InventorySlot]) -> DiscardPlan:
    matching = [slot for slot in slots if _same_item(slot.item, item) and not slot.empty]
    available = sum(slot.count for slot in matching)
    remaining = count
    moves: list[dict[str, object]] = []
    hotbar_slots = {slot.slot for slot in slots if 0 <= slot.slot <= 8}
    empty_hotbar = [slot.slot for slot in slots if 0 <= slot.slot <= 8 and slot.empty]
    for index in range(9):
        if index not in hotbar_slots:
            empty_hotbar.append(index)

    for source in matching:
        if remaining <= 0:
            break
        move_count = min(source.count, remaining)
        if 0 <= source.slot <= 8:
            if move_count >= source.count:
                moves.append({"kind": "drop", "drop_slot": source.slot, "count": move_count, "mode": "all"})
            else:
                loop_count = 0
                while loop_count < move_count:
                    moves.append({"kind": "drop", "drop_slot": source.slot, "count": 1, "mode": "one"})
                    loop_count += 1
            remaining -= move_count
            continue
        if not empty_hotbar:
            break
        hotbar_slot = empty_hotbar.pop(0)
        moves.append(
            {
                "kind": "stage",
                "from_slot": source.slot,
                "to_slot": hotbar_slot,
                "drop_slot": hotbar_slot,
                "count": move_count,
            }
        )
        remaining -= move_count

    return DiscardPlan(
        item=item,
        requested_count=count,
        available=available,
        planned_count=sum(int(move["count"]) for move in moves),
        moves=tuple(moves),
    )


def _plan_equip(item: str, target: str, slots: list[InventorySlot]) -> EquipPlan | None:
    target_slot = EQUIP_TARGET_SLOTS[target]
    target_before = _slot_by_index(slots, target_slot)
    if target_before is None:
        target_before = InventorySlot(slot=target_slot, item=None, count=0, empty=True)
    if not target_before.empty and _same_item(target_before.item, item):
        return EquipPlan(
            item=item,
            target=target,
            target_slot=target_slot,
            source_slot=None,
            stage_slot=None,
            move_count=None,
            source_item=target_before.item,
            source_count=target_before.count,
            target_before_item=target_before.item,
            target_before_count=target_before.count,
        )

    source = _choose_equip_source(item, slots, exclude={target_slot})
    if source is None:
        return None

    needs_stage = not target_before.empty and not _same_item(target_before.item, item)
    stage_slot = None
    if needs_stage:
        stage_slot = _find_empty_carry_slot(slots, exclude={target_slot, source.slot})
        if stage_slot is None:
            stage_slot = -1

    move_count = None if target == "offhand" else 1
    return EquipPlan(
        item=item,
        target=target,
        target_slot=target_slot,
        source_slot=source.slot,
        stage_slot=stage_slot,
        move_count=move_count,
        source_item=source.item,
        source_count=source.count,
        target_before_item=target_before.item,
        target_before_count=target_before.count,
    )


def _same_item(actual: str | None, wanted: str) -> bool:
    if actual is None:
        return False
    return actual == wanted or actual == f"minecraft:{wanted}" or f"minecraft:{actual}" == wanted


def _plain_item_name(item: str) -> str:
    return item.removeprefix("minecraft:")


def _normalize_equip_target(target: str, item: str) -> str | None:
    lowered = (target or "auto").strip().lower()
    if lowered == "auto":
        return _infer_equip_target(item)
    return EQUIP_TARGET_ALIASES.get(lowered)


def _infer_equip_target(item: str) -> str:
    plain = _plain_item_name(item)
    if plain in OFFHAND_AUTO_ITEMS:
        return "offhand"
    if plain == "elytra" or plain.endswith("_chestplate"):
        return "chest"
    if plain == "turtle_helmet" or plain.endswith("_helmet"):
        return "head"
    if plain.endswith("_leggings"):
        return "legs"
    if plain.endswith("_boots"):
        return "feet"
    return "mainhand"


def _choose_equip_source(
    item: str,
    slots: list[InventorySlot],
    *,
    exclude: set[int],
) -> InventorySlot | None:
    slot_map = {slot.slot: slot for slot in slots}
    ordered = list(range(0, 36)) + [40, 36, 37, 38, 39]
    for slot_index in ordered:
        if slot_index in exclude:
            continue
        slot = slot_map.get(slot_index)
        if slot is None or slot.empty:
            continue
        if _same_item(slot.item, item):
            return slot
    return None


def _find_empty_carry_slot(slots: list[InventorySlot], *, exclude: set[int]) -> int | None:
    slot_map = {slot.slot: slot for slot in slots}
    for slot_index in range(0, 36):
        if slot_index in exclude:
            continue
        slot = slot_map.get(slot_index)
        if slot is None or slot.empty:
            return slot_index
    return None


def _slot_by_index(slots: list[InventorySlot], index: int) -> InventorySlot | None:
    for slot in slots:
        if slot.slot == index:
            return slot
    return None


def _slot_metrics(slot: InventorySlot | None) -> dict[str, object] | None:
    if slot is None:
        return None
    return {"slot": slot.slot, "item": slot.item, "count": slot.count, "empty": slot.empty}


def _first_residue(slots: list[InventorySlot], residue_slots: tuple[int, ...]) -> InventorySlot | None:
    slot_map = {slot.slot: slot for slot in slots}
    for slot_index in residue_slots:
        slot = slot_map.get(slot_index)
        if slot is not None and not slot.empty:
            return slot
    return None


def _choose_residue_destination(
    residue: InventorySlot,
    slots: list[InventorySlot],
    destination_slots: tuple[int, ...],
    *,
    max_stack: int,
) -> dict[str, int] | None:
    slot_map = {slot.slot: slot for slot in slots}
    empty_slot: int | None = None
    for slot_index in destination_slots:
        slot = slot_map.get(slot_index)
        if slot is None or slot.empty:
            if empty_slot is None:
                empty_slot = slot_index
            continue
        if _same_item(slot.item, str(residue.item)) and slot.count < max_stack:
            return {"slot": slot_index, "room": max_stack - slot.count}
    if empty_slot is not None:
        return {"slot": empty_slot, "room": max_stack}
    return None


def _normalize_craft_inputs(inputs: list[dict[str, object]]) -> list[dict[str, object]]:
    normalized: list[dict[str, object]] = []
    for raw in inputs:
        try:
            slot = int(raw["slot"])
            item = str(raw["item"])
            count = int(raw["count"])
        except (KeyError, TypeError, ValueError):
            return []
        if slot < 0 or slot > 45 or count <= 0 or not item:
            return []
        normalized.append({"slot": slot, "item": item, "count": count})
    return normalized


def _normalize_craft_output(output: dict[str, object]) -> dict[str, object] | None:
    try:
        slot = int(output["slot"])
        item = str(output["item"])
        count = int(output["count"])
    except (KeyError, TypeError, ValueError):
        return None
    if slot < 0 or slot > 45 or count <= 0 or not item:
        return None
    return {"slot": slot, "item": item, "count": count}


def _normalize_craft_remainders(remainders: list[dict[str, object]]) -> list[dict[str, object]] | None:
    normalized: list[dict[str, object]] = []
    seen_slots: set[int] = set()
    for raw in remainders:
        try:
            slot = int(raw["slot"])
            item = str(raw["item"])
            count = int(raw["count"])
        except (KeyError, TypeError, ValueError):
            return None
        if slot < 0 or slot > 45 or count <= 0 or not item or slot in seen_slots:
            return None
        seen_slots.add(slot)
        normalized.append({"slot": slot, "item": item, "count": count})
    return normalized


def _craft_matches_expectation(
    before_slots: list[InventorySlot],
    after_slots: list[InventorySlot],
    inputs: list[dict[str, object]],
    output: dict[str, object],
    remainders: list[dict[str, object]],
) -> bool:
    before_map = {slot.slot: slot for slot in before_slots}
    after_map = {slot.slot: slot for slot in after_slots}
    involved_slots = {int(entry["slot"]) for entry in inputs}
    remainder_map = {int(entry["slot"]): entry for entry in remainders}

    for entry in inputs:
        slot_index = int(entry["slot"])
        item = str(entry["item"])
        count = int(entry["count"])
        before = before_map.get(slot_index)
        after = after_map.get(slot_index)
        if before is None or before.empty or not _same_item(before.item, item) or before.count < count:
            return False
        expected_after_count = before.count - count
        remainder = remainder_map.get(slot_index)
        if remainder is not None:
            if expected_after_count != 0:
                return False
            if after is None or after.empty or not _same_item(after.item, str(remainder["item"])):
                return False
            if after.count != int(remainder["count"]):
                return False
        else:
            actual_after_count = 0 if after is None or after.empty else after.count
            if actual_after_count != expected_after_count:
                return False
            if expected_after_count > 0 and (after is None or after.empty or not _same_item(after.item, item)):
                return False

    output_slot = int(output["slot"])
    output_item = str(output["item"])
    output_count = int(output["count"])
    involved_slots.add(output_slot)
    before_output = before_map.get(output_slot)
    after_output = after_map.get(output_slot)
    before_output_count = 0 if before_output is None or before_output.empty else before_output.count
    if before_output is not None and not before_output.empty and not _same_item(before_output.item, output_item):
        return False
    if after_output is None or after_output.empty or not _same_item(after_output.item, output_item):
        return False
    if after_output.count != before_output_count + output_count:
        return False

    # The current non-GUI craft path is explicit-slot only: it must not mutate
    # unrelated inventory slots or leave hidden residue outside the named inputs/output.
    all_slots = set(before_map) | set(after_map)
    for slot_index in all_slots:
        if slot_index in involved_slots:
            continue
        before = before_map.get(slot_index)
        after = after_map.get(slot_index)
        if _slot_signature(before) != _slot_signature(after):
            return False
    return True


def _slot_signature(slot: InventorySlot | None) -> tuple[str | None, int, bool]:
    if slot is None:
        return (None, 0, True)
    return (slot.item, slot.count, slot.empty)


CRAFT_REMAINDER_MAP = {
    "minecraft:milk_bucket": "minecraft:bucket",
    "minecraft:water_bucket": "minecraft:bucket",
    "minecraft:lava_bucket": "minecraft:bucket",
    "minecraft:powder_snow_bucket": "minecraft:bucket",
    "minecraft:honey_bottle": "minecraft:glass_bottle",
}


def _read_recipe_data(body: Body, item: str) -> PerceptionResult:
    return body.perceive("recipeData", {"item": item})


def _parse_recipe_variants(item: str, perception: PerceptionResult) -> list[CraftRecipeVariant] | ToolResult:
    recipe_raw = str(perception.data.get("recipe_raw") or "")
    if not recipe_raw:
        return ToolResult(
            success=False,
            reason="recipe_not_found",
            can_retry=False,
            metrics={"item": item, "recipe_data": dict(perception.data or {})},
        )
    try:
        parsed = _ScarpetValueParser(recipe_raw).parse()
    except ValueError as exc:
        return ToolResult(
            success=False,
            reason="recipe_parse_failed",
            can_retry=True,
            next_suggestion="retry the recipe query or inspect the Scarpet recipe_data payload shape",
            metrics={"item": item, "recipe_raw": recipe_raw, "error": str(exc)},
        )
    if not isinstance(parsed, list):
        return ToolResult(
            success=False,
            reason="recipe_parse_failed",
            can_retry=True,
            metrics={"item": item, "recipe_raw": recipe_raw, "error": "recipe_data did not decode to a list"},
        )
    if (
        len(parsed) >= 3
        and isinstance(parsed[0], list)
        and isinstance(parsed[1], list)
        and isinstance(parsed[2], list)
        and (not parsed[0] or not isinstance(parsed[0][0], list) or len(parsed) == 3)
    ):
        parsed = [parsed]

    variants: list[CraftRecipeVariant] = []
    normalized_item = _normalize_recipe_item(item)
    for raw_index, raw in enumerate(parsed):
        if not isinstance(raw, list) or len(raw) < 3:
            continue
        outputs_raw, groups_raw, meta_raw = raw[0], raw[1], raw[2]
        if not isinstance(outputs_raw, list) or not outputs_raw:
            continue
        output = outputs_raw[0]
        if not isinstance(output, list) or len(output) < 2:
            continue
        output_item = _normalize_recipe_item(output[0])
        output_count = int(output[1])
        if output_item != normalized_item or output_count <= 0:
            continue
        recipe_kind = "shapeless"
        width = 0
        height = 0
        if isinstance(meta_raw, list) and meta_raw:
            recipe_kind = str(meta_raw[0])
            if len(meta_raw) >= 3:
                width = int(meta_raw[1])
                height = int(meta_raw[2])
        groups: list[tuple[str, ...] | None] = []
        if isinstance(groups_raw, list):
            for group in groups_raw:
                if group is None:
                    groups.append(None)
                    continue
                if isinstance(group, list):
                    groups.append(tuple(_normalize_recipe_item(entry) for entry in group if entry is not None))
                else:
                    groups.append((_normalize_recipe_item(group),))
        ingredient_count = sum(1 for group in groups if group)
        requires_table = (recipe_kind == "shaped" and (width > 2 or height > 2)) or (
            recipe_kind != "shaped" and ingredient_count > 4
        )
        variants.append(
            CraftRecipeVariant(
                output_item=output_item,
                output_count=output_count,
                recipe_kind=recipe_kind,
                width=width,
                height=height,
                ingredient_groups=tuple(groups),
                requires_table=requires_table,
            )
        )

    if not variants:
        return ToolResult(
            success=False,
            reason="recipe_not_found",
            can_retry=False,
            metrics={"item": item, "recipe_raw": recipe_raw, "variant_count": len(parsed)},
        )
    return variants


def _choose_craft_plan(
    slots: list[InventorySlot],
    *,
    item: str,
    count: int,
    output_slot: int | None,
    variants: list[CraftRecipeVariant],
) -> CraftPlan | ToolResult:
    item = _normalize_recipe_item(item)
    failures: list[dict[str, object]] = []
    for index, variant in enumerate(sorted(variants, key=lambda candidate: (candidate.requires_table, candidate.width * candidate.height))):
        if count % variant.output_count != 0:
            failures.append(
                {
                    "variant_index": index,
                    "reason": "requested_count_not_multiple",
                    "output_count": variant.output_count,
                    "requires_table": variant.requires_table,
                }
            )
            continue
        crafts = count // variant.output_count
        inputs, chosen_items = _allocate_recipe_inputs(slots, variant, crafts)
        if inputs is None:
            failures.append(
                {
                    "variant_index": index,
                    "reason": "inputs_not_available",
                    "requires_table": variant.requires_table,
                    "output_count": variant.output_count,
                }
            )
            continue
        remainders = _plan_craft_remainders(slots, inputs, chosen_items)
        if isinstance(remainders, ToolResult):
            failures.append(
                {
                    "variant_index": index,
                    "reason": remainders.reason,
                    "requires_table": variant.requires_table,
                }
            )
            continue
        selected_output_slot = _find_craft_output_slot(
            slots,
            item,
            count,
            preferred=output_slot,
            exclude={int(entry["slot"]) for entry in inputs},
        )
        if selected_output_slot is None:
            failures.append(
                {
                    "variant_index": index,
                    "reason": "output_no_space",
                    "requires_table": variant.requires_table,
                }
            )
            continue
        return CraftPlan(
            item=item,
            requested_count=count,
            crafted_count=count,
            output_slot=selected_output_slot,
            variant_index=index,
            variant=variant,
            inputs=tuple(inputs),
            remainders=tuple(remainders),
        )
    return ToolResult(
        success=False,
        reason="craft_plan_not_available",
        can_retry=True,
        next_suggestion="free inventory space, gather the missing ingredients, or adjust the requested craft count",
        metrics={"item": item, "requested_count": count, "variant_failures": failures},
    )


def _allocate_recipe_inputs(
    slots: list[InventorySlot],
    variant: CraftRecipeVariant,
    crafts: int,
) -> tuple[list[dict[str, object]], dict[int, str]] | tuple[None, None]:
    requirements: list[tuple[str, ...]] = []
    for group in variant.ingredient_groups:
        if not group:
            continue
        loop_count = 0
        while loop_count < crafts:
            requirements.append(group)
            loop_count += 1
    requirements.sort(key=len)
    slot_map = {slot.slot: slot for slot in slots}
    remaining = {slot.slot: slot.count for slot in slots if not slot.empty}
    allocated: dict[int, int] = {}
    chosen_items: dict[int, str] = {}
    for group in requirements:
        candidates = [
            slot
            for slot in slots
            if not slot.empty
            and remaining.get(slot.slot, 0) > 0
            and any(_same_item(slot.item, option) for option in group)
        ]
        if not candidates:
            return None, None
        chosen = sorted(candidates, key=lambda slot: (-remaining[slot.slot], slot.slot))[0]
        actual_item = _normalize_recipe_item(chosen.item or "")
        allocated[chosen.slot] = allocated.get(chosen.slot, 0) + 1
        remaining[chosen.slot] = remaining.get(chosen.slot, 0) - 1
        chosen_items[chosen.slot] = actual_item
    inputs = [
        {"slot": slot_index, "item": chosen_items[slot_index], "count": count}
        for slot_index, count in sorted(allocated.items())
    ]
    for entry in inputs:
        source = slot_map[int(entry["slot"])]
        if source.count < int(entry["count"]):
            return None, None
    return inputs, chosen_items


def _plan_craft_remainders(
    slots: list[InventorySlot],
    inputs: list[dict[str, object]],
    chosen_items: dict[int, str],
) -> list[dict[str, object]] | ToolResult:
    slot_map = {slot.slot: slot for slot in slots}
    remainders: list[dict[str, object]] = []
    for entry in inputs:
        slot_index = int(entry["slot"])
        item = chosen_items.get(slot_index, _normalize_recipe_item(str(entry["item"])))
        remainder_item = CRAFT_REMAINDER_MAP.get(item)
        if remainder_item is None:
            continue
        source = slot_map.get(slot_index)
        if source is None:
            continue
        if source.count != int(entry["count"]):
            return ToolResult(
                success=False,
                reason="craft_remainder_slot_conflict",
                can_retry=True,
                next_suggestion="split remainder-yielding inputs into their own slots before crafting",
                metrics={"slot": slot_index, "item": item, "count": entry["count"], "source_count": source.count},
            )
        remainders.append({"slot": slot_index, "item": remainder_item, "count": int(entry["count"])})
    return remainders


def _find_craft_output_slot(
    slots: list[InventorySlot],
    item: str,
    count: int,
    *,
    preferred: int | None,
    exclude: set[int],
    max_stack: int = 64,
) -> int | None:
    if preferred is not None:
        slot = _slot_by_index(slots, preferred)
        if slot is None or slot.empty:
            return preferred
        if slot.slot not in exclude and _same_item(slot.item, item) and slot.count + count <= max_stack:
            return preferred
        return None
    for slot in slots:
        if slot.slot in exclude or slot.empty:
            continue
        if _same_item(slot.item, item) and slot.count + count <= max_stack:
            return slot.slot
    for slot_index in range(0, 36):
        if slot_index in exclude:
            continue
        slot = _slot_by_index(slots, slot_index)
        if slot is None or slot.empty:
            return slot_index
    return None


def _craft_plan_metrics(plan: CraftPlan) -> dict[str, object]:
    return {
        "item": plan.item,
        "requested_count": plan.requested_count,
        "crafted_count": plan.crafted_count,
        "output_slot": plan.output_slot,
        "variant_index": plan.variant_index,
        "recipe": {
            "kind": plan.variant.recipe_kind,
            "width": plan.variant.width,
            "height": plan.variant.height,
            "requires_table": plan.variant.requires_table,
            "ingredient_groups": [list(group) if group is not None else None for group in plan.variant.ingredient_groups],
        },
        "inputs": [dict(entry) for entry in plan.inputs],
        "remainders": [dict(entry) for entry in plan.remainders],
    }


def _normalize_recipe_item(value: object) -> str:
    text = str(value)
    if not text:
        return text
    return text if text.startswith("minecraft:") else f"minecraft:{text}"


class _ScarpetValueParser:
    def __init__(self, text: str):
        self.text = text.strip()
        self.index = 0

    def parse(self) -> object:
        value = self._parse_value()
        self._skip_ws()
        if self.index != len(self.text):
            raise ValueError(f"unexpected trailing content at {self.index}")
        return value

    def _parse_value(self) -> object:
        self._skip_ws()
        if self.index >= len(self.text):
            raise ValueError("unexpected end of input")
        ch = self.text[self.index]
        if ch == "[":
            return self._parse_list()
        if ch == "{":
            return self._parse_map()
        if ch == '"':
            return self._parse_string()
        if ch == "-" or ch.isdigit():
            return self._parse_number()
        return self._parse_identifier()

    def _parse_list(self) -> list[object]:
        self.index += 1
        items: list[object] = []
        while True:
            self._skip_ws()
            if self.index >= len(self.text):
                raise ValueError("unterminated list")
            if self.text[self.index] == "]":
                self.index += 1
                return items
            items.append(self._parse_value())
            self._skip_ws()
            if self.index < len(self.text) and self.text[self.index] == ",":
                self.index += 1
                continue
            if self.index < len(self.text) and self.text[self.index] == "]":
                self.index += 1
                return items
            raise ValueError(f"expected ',' or ']' at {self.index}")

    def _parse_map(self) -> dict[str, object]:
        self.index += 1
        out: dict[str, object] = {}
        while True:
            self._skip_ws()
            if self.index >= len(self.text):
                raise ValueError("unterminated map")
            if self.text[self.index] == "}":
                self.index += 1
                return out
            key = self._parse_string() if self.text[self.index] == '"' else str(self._parse_identifier())
            self._skip_ws()
            if self.index >= len(self.text) or self.text[self.index] != ":":
                raise ValueError(f"expected ':' at {self.index}")
            self.index += 1
            out[key] = self._parse_value()
            self._skip_ws()
            if self.index < len(self.text) and self.text[self.index] == ",":
                self.index += 1
                continue
            if self.index < len(self.text) and self.text[self.index] == "}":
                self.index += 1
                return out
            raise ValueError(f"expected ',' or '}}' at {self.index}")

    def _parse_string(self) -> str:
        self.index += 1
        start = self.index
        out: list[str] = []
        while self.index < len(self.text):
            ch = self.text[self.index]
            if ch == '"':
                out.append(self.text[start:self.index])
                self.index += 1
                return "".join(out)
            if ch == "\\":
                out.append(self.text[start:self.index])
                self.index += 1
                if self.index >= len(self.text):
                    raise ValueError("unterminated escape")
                out.append(self.text[self.index])
                self.index += 1
                start = self.index
                continue
            self.index += 1
        raise ValueError("unterminated string")

    def _parse_number(self) -> int | float:
        start = self.index
        if self.text[self.index] == "-":
            self.index += 1
        while self.index < len(self.text) and self.text[self.index].isdigit():
            self.index += 1
        if self.index < len(self.text) and self.text[self.index] == ".":
            self.index += 1
            while self.index < len(self.text) and self.text[self.index].isdigit():
                self.index += 1
            return float(self.text[start:self.index])
        return int(self.text[start:self.index])

    def _parse_identifier(self) -> object:
        start = self.index
        while self.index < len(self.text) and self.text[self.index] not in ",]}: \t\r\n":
            self.index += 1
        token = self.text[start:self.index]
        if token == "null":
            return None
        return token

    def _skip_ws(self) -> None:
        while self.index < len(self.text) and self.text[self.index].isspace():
            self.index += 1


def _execute_move_item(
    body: Body,
    *,
    from_slot: int,
    to_slot: int,
    count: int | None,
    timeout_s: float,
) -> ToolResult:
    params: dict[str, object] = {"from_slot": from_slot, "to_slot": to_slot}
    if count is not None:
        params["count"] = count
    return _dispatch(body, "moveItem", params, timeout_s=timeout_s)


def _perception_failure(perception: PerceptionResult) -> ToolResult | None:
    if perception.ok and perception.complete:
        return None
    return ToolResult(
        success=False,
        reason="perception_failed",
        can_retry=True,
        next_suggestion="refresh inventory facts before moving or dropping items",
        metrics={
            "scope": perception.scope,
            "ok": perception.ok,
            "complete": perception.complete,
            "error": perception.error,
            "uncertainty": perception.uncertainty,
        },
    )


def _acceptance_failure(
    result: Result,
    plan: DiscardPlan,
    executed: list[dict[str, object]],
) -> ToolResult | None:
    if result.ok and result.accepted:
        return None
    return _body_rejected(
        result,
        {
            **_plan_metrics(plan),
            "executed": executed,
        },
    )


def _terminal_failure(
    prefix: str,
    result: ToolResult,
    plan: DiscardPlan,
    executed: list[dict[str, object]],
    dropped_total: int,
) -> ToolResult:
    metrics = _plan_metrics(plan)
    metrics["executed"] = executed
    metrics["dropped_count"] = dropped_total
    return ToolResult(
        success=False,
        reason=f"{prefix}:{result.reason}",
        can_retry=result.can_retry,
        next_suggestion=result.next_suggestion,
        metrics=metrics,
    )


def _plan_metrics(plan: DiscardPlan) -> dict[str, object]:
    return {
        "item": plan.item,
        "requested_count": plan.requested_count,
        "available_count": plan.available,
        "planned_count": plan.planned_count,
        "moves": list(plan.moves),
    }


def _equip_plan_metrics(plan: EquipPlan) -> dict[str, object]:
    return {
        "item": plan.item,
        "target": plan.target,
        "target_slot": plan.target_slot,
        "source_slot": plan.source_slot,
        "stage_slot": None if plan.stage_slot == -1 else plan.stage_slot,
        "move_count": plan.move_count,
        "source_item": plan.source_item,
        "source_count": plan.source_count,
        "target_before": {
            "slot": plan.target_slot,
            "item": plan.target_before_item,
            "count": plan.target_before_count,
            "empty": plan.target_before_count <= 0 or plan.target_before_item is None,
        },
    }


def _equip_terminal_failure(
    prefix: str,
    result: ToolResult,
    plan: EquipPlan,
    executed: list[dict[str, object]],
) -> ToolResult:
    metrics = _equip_plan_metrics(plan)
    metrics["executed"] = executed
    return ToolResult(
        success=False,
        reason=f"{prefix}:{result.reason}",
        can_retry=result.can_retry,
        next_suggestion=result.next_suggestion,
        metrics=metrics,
    )


def _dispatch(body: Body, name: str, params: dict[str, object], *, timeout_s: float) -> ToolResult:
    action = Action.create(name, params)
    accepted = body.execute(action)
    rejected = _body_rejected(accepted, {"action": name, "params": params})
    if rejected is not None:
        return rejected
    terminal = body.await_action_terminal(action.id, timeout_s=timeout_s)
    return terminal_event_to_tool_result(terminal)


def _dispatch_select_item(body: Body, item: str, *, timeout_s: float) -> ToolResult:
    action = Action.create("selectItem", {"item": item})
    accepted = body.execute(action)
    if accepted.ok and accepted.accepted:
        terminal = body.await_action_terminal(action.id, timeout_s=timeout_s)
        return terminal_event_to_tool_result(terminal)
    if accepted.ok and not accepted.accepted and (accepted.data or {}).get("action") == "selectItem":
        try:
            terminal = body.await_action_terminal(action.id, timeout_s=timeout_s)
        except TimeoutError:
            pass
        else:
            return terminal_event_to_tool_result(terminal)
    rejected = _body_rejected(accepted, {"action": "selectItem", "params": {"item": item}})
    if rejected is not None:
        return rejected
    return ToolResult(success=False, reason="body_rejected", can_retry=True, metrics={"action": "selectItem", "item": item})


def _body_rejected(result: Result, metrics: dict[str, object]) -> ToolResult | None:
    if result.ok and result.accepted:
        return None
    merged = dict(metrics)
    merged["accepted"] = {
        "ok": result.ok,
        "accepted": result.accepted,
        "error": result.error,
        "data": result.data,
    }
    return ToolResult(success=False, reason="body_rejected", can_retry=True, metrics=merged)
