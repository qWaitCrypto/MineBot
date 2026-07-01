"""Body transactions for item use / consume workflows."""

from __future__ import annotations

from dataclasses import dataclass
from math import dist
import time

from minebot.body.interaction_support import (
    INTERACTION_RANGE,
    InteractionNavigator,
    NearbyEntityTarget,
    ensure_entity_range,
    ensure_interaction_range,
    find_entity_target,
    interaction_stand_points,
    merge_context,
    normalize_block_type,
    normalize_entity_type,
    perception_failure as shared_perception_failure,
    refresh_entity_target,
)
from minebot.body.inventory import InventoryTransactions
from minebot.contract import Action, Body, InventorySlot, PerceptionResult, Result, ToolResult, perception_next_cursor
from minebot.contract import terminal_event_to_tool_result


FOOD_ITEMS = {
    "apple",
    "baked_potato",
    "beetroot",
    "beetroot_soup",
    "bread",
    "cake",
    "carrot",
    "chicken",
    "chorus_fruit",
    "cod",
    "cooked_beef",
    "cooked_chicken",
    "cooked_cod",
    "cooked_mutton",
    "cooked_porkchop",
    "cooked_rabbit",
    "cooked_salmon",
    "cookie",
    "dried_kelp",
    "enchanted_golden_apple",
    "glow_berries",
    "golden_apple",
    "golden_carrot",
    "honey_bottle",
    "melon_slice",
    "mushroom_stew",
    "mutton",
    "poisonous_potato",
    "porkchop",
    "potato",
    "pufferfish",
    "pumpkin_pie",
    "rabbit",
    "rabbit_stew",
    "rotten_flesh",
    "salmon",
    "spider_eye",
    "suspicious_stew",
    "sweet_berries",
    "tropical_fish",
}

POST_MOVE_SETTLE_S = 0.10


Position = tuple[int, int, int]


@dataclass(frozen=True)
class BlockUseExpectation:
    expected_block_types: tuple[str, ...]
    expected_properties: dict[str, str]
    allow_unchanged: bool


@dataclass(frozen=True)
class BlockUseAttempt:
    look: ToolResult
    use: ToolResult
    target_after: PerceptionResult
    observe_after: PerceptionResult


class UseTransactions:
    """Use/consume workflows above raw `selectItem` + `useItem` primitives."""

    def __init__(
        self,
        body: Body,
        *,
        navigator: InteractionNavigator | None = None,
        inventory: InventoryTransactions | None = None,
    ):
        self.body = body
        self.navigator = navigator
        self.inventory = inventory or InventoryTransactions(body)

    def consume_item(
        self,
        *,
        item: str,
        use_ticks: int = 80,
        timeout_s: float = 8.0,
    ) -> ToolResult:
        before_state = self.body.get_state()
        before_inventory = _read_inventory(self.body)
        failed = _perception_failure(before_inventory)
        if failed is not None:
            return failed

        select = _dispatch(self.body, "selectItem", {"item": item}, timeout_s=timeout_s)
        if not select.success:
            reason = select.reason
            if reason == "not_in_inventory":
                reason = "item_not_available"
            return ToolResult(
                success=False,
                reason=reason if reason == "hotbar_full" else reason,
                can_retry=select.can_retry,
                next_suggestion=select.next_suggestion,
                metrics={"item": item, "select": dict(select.metrics or {})},
            )

        use = _dispatch(
            self.body,
            "useItem",
            {"mode": "continuous", "ticks": use_ticks, "item": item},
            timeout_s=timeout_s,
        )
        after_state = self.body.get_state()
        after_inventory = _read_inventory(self.body)
        failed = _perception_failure(after_inventory)
        if failed is not None:
            return failed

        before_count = _count_item(before_inventory, item)
        after_count = _count_item(after_inventory, item)
        item_delta = before_count - after_count
        food_delta = int(after_state.food) - int(before_state.food)
        effect_metrics = _effect_delta_metrics(before_state.effects, after_state.effects)

        metrics = {
            "item": item,
            "before_count": before_count,
            "after_count": after_count,
            "item_delta": item_delta,
            "food_before": before_state.food,
            "food_after": after_state.food,
            "food_delta": food_delta,
            **effect_metrics,
            "select": dict(select.metrics or {}),
            "use": dict(use.metrics or {}),
        }

        if item_delta > 0 or food_delta > 0 or int(effect_metrics["effect_delta"]) > 0:
            return ToolResult(success=True, reason="completed", can_retry=False, metrics=metrics)

        if _is_food_item(item) and before_state.food >= 20 and after_state.food >= before_state.food:
            return ToolResult(success=True, reason="already_full", can_retry=False, metrics=metrics)

        if use.reason == "no_effect":
            return ToolResult(
                success=False,
                reason="consume_no_effect",
                can_retry=True,
                next_suggestion="verify the item is consumable in the current state or inspect effect-specific truth",
                metrics=metrics,
            )

        if not use.success:
            return ToolResult(
                success=False,
                reason=f"consume_failed:{use.reason}",
                can_retry=use.can_retry,
                next_suggestion=use.next_suggestion,
                metrics=metrics,
            )
        return ToolResult(
            success=False,
            reason="consume_no_effect",
            can_retry=True,
            next_suggestion="verify the item is consumable in the current state or inspect effect-specific truth",
            metrics=metrics,
        )

    def use_item(
        self,
        *,
        item: str | None,
        look_target: tuple[float, float, float] | None = None,
        use_mode: str = "once",
        use_ticks: int = 1,
        watched_items: tuple[str, ...] | list[str] = (),
        required_watched_item_deltas: dict[str, int] | None = None,
        min_effect_delta: int = 0,
        min_position_delta: float = 0.0,
        look_timeout_s: float = 2.0,
        timeout_s: float = 8.0,
    ) -> ToolResult:
        use_item = _normalize_use_item(item)
        before_state = self.body.get_state()
        before_inventory = _read_inventory(self.body)
        failed = _perception_failure(before_inventory)
        if failed is not None:
            return failed

        equip = ToolResult(success=True, reason="empty_hand", can_retry=False, metrics={"empty_hand": True})
        if use_item is not None:
            equip = self.inventory.equip_item(item=use_item, target="mainhand", timeout_s=timeout_s)
            if not equip.success:
                return equip

        look = None
        if look_target is not None:
            look = _look_at(self.body, look_target, timeout_s=look_timeout_s)
            if not look.success:
                return merge_context(
                    look,
                    {
                        "item": use_item,
                        "empty_hand": use_item is None,
                        "equip": equip.to_payload(),
                        "look_target": list(look_target),
                    },
                )

        use = _dispatch(
            self.body,
            "useItem",
            {"mode": use_mode, "ticks": use_ticks, **({"item": use_item} if use_item is not None else {})},
            timeout_s=timeout_s,
        )
        after_state = self.body.get_state()
        if min_position_delta > 0 and use.success:
            after_state = _await_position_delta(
                self.body,
                origin=before_state.pos,
                current=after_state,
                min_position_delta=float(min_position_delta),
            )
        after_inventory = _read_inventory(self.body)
        failed = _perception_failure(after_inventory)
        if failed is not None:
            return failed

        primary_before = _count_item(before_inventory, use_item) if use_item is not None else 0
        primary_after = _count_item(after_inventory, use_item) if use_item is not None else 0
        primary_item_delta = primary_before - primary_after
        position_delta = dist(before_state.pos, after_state.pos)
        effect_metrics = _effect_delta_metrics(before_state.effects, after_state.effects)
        watched_metrics = _watched_item_metrics(before_inventory, after_inventory, watched_items)
        watched_ok = _meets_watched_item_delta_requirements(
            watched_metrics["watched_item_deltas"],
            required_watched_item_deltas,
        )
        effect_ok = int(effect_metrics["effect_delta"]) >= int(min_effect_delta)
        position_ok = position_delta >= float(min_position_delta)

        metrics = {
            "item": use_item,
            "empty_hand": use_item is None,
            "primary_item_before": primary_before,
            "primary_item_after": primary_after,
            "primary_item_delta": primary_item_delta,
            "pos_before": list(before_state.pos),
            "pos_after": list(after_state.pos),
            "position_delta": position_delta,
            **effect_metrics,
            **watched_metrics,
            "required_watched_item_deltas": _normalize_required_item_deltas(required_watched_item_deltas),
            "min_effect_delta": int(min_effect_delta),
            "min_position_delta": float(min_position_delta),
            "equip": equip.to_payload(),
            "use": use.to_payload(),
        }
        if look_target is not None:
            metrics["look_target"] = list(look_target)
            metrics["look"] = look.to_payload() if look is not None else None

        observed_delta = primary_item_delta > 0 or int(effect_metrics["effect_delta"]) > 0 or position_delta > 0.01
        if (watched_ok and effect_ok and position_ok and observed_delta) or (
            required_watched_item_deltas is None and min_effect_delta <= 0 and min_position_delta <= 0 and observed_delta
        ):
            return ToolResult(success=True, reason="completed", can_retry=False, metrics=metrics)

        if use.reason == "no_effect":
            return ToolResult(
                success=False,
                reason="use_no_effect",
                can_retry=True,
                next_suggestion="verify targeting, line of sight, and expected observable effect before retrying the item use",
                metrics=metrics,
            )

        if not use.success:
            return ToolResult(
                success=False,
                reason=f"use_failed:{use.reason}",
                can_retry=use.can_retry,
                next_suggestion=use.next_suggestion,
                metrics=metrics,
            )

        return ToolResult(
            success=False,
            reason="use_unverified",
            can_retry=True,
            next_suggestion="provide stronger expected post-use truth or inspect whether the attempted item use had any real effect",
            metrics=metrics,
        )

    def use_on_entity(
        self,
        *,
        item: str | None,
        entity_types: tuple[str, ...] | list[str] = (),
        entity_name: str | None = None,
        search_radius: int = 24,
        min_distance: float = 0.0,
        max_distance: float = 4.5,
        vertical_tolerance: float = 1.5,
        watched_items: tuple[str, ...] | list[str] = (),
        required_watched_item_deltas: dict[str, int] | None = None,
        min_effect_delta: int = 0,
        min_position_delta: float = 0.0,
        use_mode: str = "once",
        use_ticks: int = 1,
        look_timeout_s: float = 2.0,
        timeout_s: float = 8.0,
    ) -> ToolResult:
        if entity_name is None and not entity_types:
            return ToolResult(
                success=False,
                reason="use_entity_filter_missing",
                can_retry=False,
                metrics={"search_radius": search_radius},
            )

        target = find_entity_target(
            self.body,
            radius=search_radius,
            not_found_reason="use_entity_not_found",
            wanted_types=tuple(entity_types),
            entity_name=entity_name,
        )
        if isinstance(target, ToolResult):
            return target

        approach = ensure_entity_range(
            self.body,
            self.navigator,
            target.pos,
            min_distance=min_distance,
            max_distance=max_distance,
            vertical_tolerance=vertical_tolerance,
            timeout_s=timeout_s,
            missing_reason="use_entity_navigation_missing",
            failure_prefix="use_entity_navigation_failed",
            no_stand_reason="use_entity_no_stand_point",
        )
        context = {
            "item": _normalize_use_item(item),
            "entity_name": entity_name,
            "entity_types": [normalize_entity_type(entity_type) for entity_type in entity_types],
            "search_radius": search_radius,
            "distance_band": {
                "min_distance": min_distance,
                "max_distance": max_distance,
                "vertical_tolerance": vertical_tolerance,
            },
            "target": _entity_metrics(target),
        }
        if isinstance(approach, ToolResult):
            return merge_context(approach, context)

        refreshed = refresh_entity_target(
            self.body,
            target,
            radius=search_radius,
            not_found_reason="use_entity_target_lost",
            wanted_types=tuple(entity_types),
            entity_name=entity_name,
        )
        if isinstance(refreshed, ToolResult):
            return merge_context(refreshed, {**context, "approach": approach})

        target = refreshed
        result = self.use_item(
            item=item,
            look_target=_entity_look_target(target),
            use_mode=use_mode,
            use_ticks=use_ticks,
            watched_items=watched_items,
            required_watched_item_deltas=required_watched_item_deltas,
            min_effect_delta=min_effect_delta,
            min_position_delta=min_position_delta,
            look_timeout_s=look_timeout_s,
            timeout_s=timeout_s,
        )
        return merge_context(
            result,
            {
                **context,
                "target": _entity_metrics(target),
                "approach": approach,
            },
        )

    def use_on_block(
        self,
        *,
        pos: Position,
        item: str | None,
        observe_pos: Position | None = None,
        expected_block_types: tuple[str, ...] | list[str] | None = None,
        expected_properties: dict[str, str] | None = None,
        allow_unchanged: bool = False,
        look_target: tuple[float, float, float] | None = None,
        use_mode: str = "once",
        use_ticks: int = 1,
        approach_timeout_s: float = 15.0,
        navigation_arrival_radius: float | None = None,
        center_after_navigation: bool = True,
        stand_points: list[Position] | None = None,
        look_timeout_s: float = 2.0,
        timeout_s: float = 8.0,
        line_of_sight_retries: int = 1,
        watched_items: tuple[str, ...] | list[str] = (),
        required_watched_item_deltas: dict[str, int] | None = None,
        min_effect_delta: int = 0,
    ) -> ToolResult:
        use_item = _normalize_use_item(item)
        observed_pos = observe_pos or pos
        target_before = self.body.perceive("blockAt", _block_params(pos))
        failed = _perception_failure(target_before)
        if failed is not None:
            return failed
        observed_before = target_before
        if observed_pos != pos:
            observed_before = self.body.perceive("blockAt", _block_params(observed_pos))
            failed = _perception_failure(observed_before)
            if failed is not None:
                return failed

        expectation = _normalize_expectation(expected_block_types, expected_properties, allow_unchanged)
        target_before_type = normalize_block_type(str(target_before.data.get("type") or "unknown"))
        target_before_state = str(target_before.data.get("state") or "UNKNOWN")
        target_before_properties = _normalize_block_properties(target_before.data.get("properties"))
        before_type = normalize_block_type(str(observed_before.data.get("type") or "unknown"))
        before_state = str(observed_before.data.get("state") or "UNKNOWN")
        before_properties = _normalize_block_properties(observed_before.data.get("properties"))

        if expectation is not None and _matches_expectation(
            before_type,
            before_properties,
            expectation,
        ):
            return ToolResult(
                success=True,
                reason="already_in_expected_state",
                can_retry=False,
                metrics={
                    "item": use_item,
                    "empty_hand": use_item is None,
                    "target": list(pos),
                    "observe_pos": list(observed_pos),
                    "target_before": {
                        "type": target_before_type,
                        "state": target_before_state,
                        "properties": target_before_properties,
                    },
                    "observed_before": {
                        "type": before_type,
                        "state": before_state,
                        "properties": before_properties,
                    },
                    "expected_block_types": list(expectation.expected_block_types),
                    "expected_properties": dict(expectation.expected_properties),
                },
            )

        equip = ToolResult(
            success=True,
            reason="empty_hand",
            can_retry=False,
            metrics={"empty_hand": True},
        )
        if use_item is not None:
            equip = self.inventory.equip_item(item=use_item, target="mainhand", timeout_s=timeout_s)
            equip = merge_context(
                equip,
                {
                    "item": use_item,
                    "target": list(pos),
                    "observe_pos": list(observed_pos),
                    "target_before": {
                        "type": target_before_type,
                        "state": target_before_state,
                        "properties": target_before_properties,
                    },
                    "observed_before": {
                        "type": before_type,
                        "state": before_state,
                        "properties": before_properties,
                    },
                },
            )
            if not equip.success:
                return equip

        approach = ensure_interaction_range(
            self.body,
            self.navigator,
            pos,
            interaction_radius=INTERACTION_RANGE,
            timeout_s=approach_timeout_s,
            missing_reason="use_navigation_missing",
            failure_prefix="use_navigation_failed",
            no_stand_reason="use_no_stand_point",
            navigation_arrival_radius=navigation_arrival_radius,
            center_after_navigation=center_after_navigation,
            stand_points=stand_points,
        )
        if isinstance(approach, ToolResult):
            return merge_context(
                approach,
                {
                    "item": use_item,
                    "empty_hand": use_item is None,
                    "target": list(pos),
                    "observe_pos": list(observed_pos),
                    "target_before": {
                        "type": target_before_type,
                        "state": target_before_state,
                        "properties": target_before_properties,
                    },
                    "observed_before": {
                        "type": before_type,
                        "state": before_state,
                        "properties": before_properties,
                    },
                    "equip": equip.to_payload(),
                },
            )

        use_fire_primitive = _should_use_fire_primitive(
            item=use_item,
            pos=pos,
            observed_pos=observed_pos,
            expectation=expectation,
        )

        body_state_before = self.body.get_state() if min_effect_delta > 0 else None
        before_inventory: PerceptionResult | None = None
        if watched_items:
            before_inventory = _read_inventory(self.body)
            failed = _perception_failure(before_inventory)
            if failed is not None:
                return failed

        attempt = _attempt_fire_ignite(
            self.body,
            pos=pos,
            observe_pos=observed_pos,
            item=use_item,
            allow_server_substitute=True,
            timeout_s=timeout_s,
        ) if use_fire_primitive else _attempt_block_use(
            self.body,
            pos=pos,
            observe_pos=observed_pos,
            look_target=look_target or _block_center_target(pos),
            item=use_item,
            use_mode=use_mode,
            use_ticks=use_ticks,
            look_timeout_s=look_timeout_s,
            timeout_s=timeout_s,
        )
        if isinstance(attempt, ToolResult):
            return merge_context(
                attempt,
                {
                    "item": use_item,
                    "empty_hand": use_item is None,
                    "target": list(pos),
                    "observe_pos": list(observed_pos),
                    "target_before": {
                        "type": target_before_type,
                        "state": target_before_state,
                        "properties": target_before_properties,
                    },
                    "observed_before": {
                        "type": before_type,
                        "state": before_state,
                        "properties": before_properties,
                    },
                    "equip": equip.to_payload(),
                    "approach": approach,
                },
            )

        recovery: dict[str, object] | None = None
        if _should_retry_line_of_sight(attempt, before_type=before_type, before_state=before_state):
            recovery = _recover_line_of_sight(
                self.body,
                self.navigator,
                pos=pos,
                timeout_s=approach_timeout_s,
                max_retries=line_of_sight_retries,
                arrival_radius=navigation_arrival_radius,
                center_after_navigation=center_after_navigation,
                stand_points=stand_points,
            )
            if isinstance(recovery, ToolResult):
                return merge_context(
                    recovery,
                    {
                        "item": use_item,
                        "empty_hand": use_item is None,
                        "target": list(pos),
                        "observe_pos": list(observed_pos),
                        "target_before": {
                            "type": target_before_type,
                            "state": target_before_state,
                            "properties": target_before_properties,
                        },
                        "observed_before": {
                            "type": before_type,
                            "state": before_state,
                            "properties": before_properties,
                        },
                        "equip": equip.to_payload(),
                        "approach": approach,
                        "look": attempt.look.to_payload(),
                        "use": attempt.use.to_payload(),
                    },
                )
            if recovery is not None and recovery.get("repositioned"):
                retried = _attempt_block_use(
                    self.body,
                    pos=pos,
                    observe_pos=observed_pos,
                    look_target=look_target or _block_center_target(pos),
                    item=use_item,
                    use_mode=use_mode,
                    use_ticks=use_ticks,
                    look_timeout_s=look_timeout_s,
                    timeout_s=timeout_s,
                )
                if use_fire_primitive:
                    retried = _attempt_fire_ignite(
                        self.body,
                        pos=pos,
                        observe_pos=observed_pos,
                        item=use_item,
                        allow_server_substitute=True,
                        timeout_s=timeout_s,
                    )
                if isinstance(retried, ToolResult):
                    return merge_context(
                    retried,
                        {
                            "item": use_item,
                            "empty_hand": use_item is None,
                            "target": list(pos),
                            "observe_pos": list(observed_pos),
                            "target_before": {
                                "type": target_before_type,
                                "state": target_before_state,
                                "properties": target_before_properties,
                            },
                            "observed_before": {
                                "type": before_type,
                                "state": before_state,
                                "properties": before_properties,
                            },
                            "equip": equip.to_payload(),
                            "approach": approach,
                            "line_of_sight_recovery": recovery,
                        },
                    )
                attempt = retried

        target_after_type = normalize_block_type(str(attempt.target_after.data.get("type") or "unknown"))
        target_after_state = str(attempt.target_after.data.get("state") or "UNKNOWN")
        target_after_properties = _normalize_block_properties(attempt.target_after.data.get("properties"))
        after_type = normalize_block_type(str(attempt.observe_after.data.get("type") or "unknown"))
        after_state = str(attempt.observe_after.data.get("state") or "UNKNOWN")
        after_properties = _normalize_block_properties(attempt.observe_after.data.get("properties"))
        body_state_after = self.body.get_state() if min_effect_delta > 0 else None
        effect_metrics = {
            "effects_before": [],
            "effects_after": [],
            "effects_added": [],
            "effects_removed": [],
            "effects_refreshed": [],
            "effect_delta": 0,
        }
        if body_state_before is not None and body_state_after is not None:
            effect_metrics = _effect_delta_metrics(body_state_before.effects, body_state_after.effects)
        watched_metrics = {
            "watched_items": [],
            "watched_item_counts_before": {},
            "watched_item_counts_after": {},
            "watched_item_deltas": {},
        }
        if before_inventory is not None:
            after_inventory = _read_inventory(self.body)
            failed = _perception_failure(after_inventory)
            if failed is not None:
                return failed
            watched_metrics = _watched_item_metrics(before_inventory, after_inventory, watched_items)
        watched_ok = _meets_watched_item_delta_requirements(
            watched_metrics["watched_item_deltas"],
            required_watched_item_deltas,
        )
        effect_ok = int(effect_metrics["effect_delta"]) >= int(min_effect_delta)
        metrics = {
            "item": use_item,
            "empty_hand": use_item is None,
            "target": list(pos),
            "observe_pos": list(observed_pos),
            "target_before": {
                "type": target_before_type,
                "state": target_before_state,
                "properties": target_before_properties,
            },
            "target_after": {
                "type": target_after_type,
                "state": target_after_state,
                "properties": target_after_properties,
            },
            "observed_before": {"type": before_type, "state": before_state, "properties": before_properties},
            "observed_after": {"type": after_type, "state": after_state, "properties": after_properties},
            **effect_metrics,
            **watched_metrics,
            "required_watched_item_deltas": _normalize_required_item_deltas(required_watched_item_deltas),
            "min_effect_delta": int(min_effect_delta),
            "equip": equip.to_payload(),
            "approach": approach,
            "look": attempt.look.to_payload(),
            "use": attempt.use.to_payload(),
        }
        if body_state_before is not None:
            metrics["body_pos_before_use"] = list(body_state_before.pos)
        if body_state_after is not None:
            metrics["body_pos_after_use"] = list(body_state_after.pos)
        if expectation is not None:
            metrics["expected_block_types"] = list(expectation.expected_block_types)
            metrics["expected_properties"] = dict(expectation.expected_properties)
        if recovery is not None:
            metrics["line_of_sight_recovery"] = recovery

        matched_expectation = expectation is not None and _matches_expectation(
            after_type,
            after_properties,
            expectation,
        )
        if matched_expectation and watched_ok and effect_ok:
            return ToolResult(success=True, reason="completed", can_retry=False, metrics=metrics)

        changed = (
            after_type != before_type
            or after_state != before_state
            or after_properties != before_properties
        )
        if changed and (expectation is None or expectation.allow_unchanged) and watched_ok and effect_ok:
            return ToolResult(success=True, reason="completed", can_retry=False, metrics=metrics)

        if attempt.use.reason == "no_effect":
            return ToolResult(
                success=False,
                reason="targeted_use_no_effect",
                can_retry=True,
                next_suggestion="verify line of sight, interaction range, and target-state expectations before retrying",
                metrics=metrics,
            )

        if not attempt.use.success:
            return ToolResult(
                success=False,
                reason=f"targeted_use_failed:{attempt.use.reason}",
                can_retry=attempt.use.can_retry,
                next_suggestion=attempt.use.next_suggestion,
                metrics=metrics,
            )

        return ToolResult(
            success=False,
            reason="targeted_use_unverified",
            can_retry=True,
            next_suggestion="provide expected target-state truth or inspect whether the target block should have changed",
            metrics=metrics,
        )

    def _prepare_use_item(self, item: str | None, *, timeout_s: float = 8.0) -> ToolResult:
        use_item = _normalize_use_item(item)
        if use_item is None:
            return ToolResult(success=True, reason="empty_hand", can_retry=False, metrics={"empty_hand": True})
        return self.inventory.equip_item(item=use_item, target="mainhand", timeout_s=timeout_s)

    def _sow_crop_on_farmland(
        self,
        *,
        pos: Position,
        observe_pos: Position,
        seed_item: str,
        crop_block: str,
        timeout_s: float = 8.0,
    ) -> ToolResult:
        target_before = self.body.perceive("blockAt", _block_params(pos))
        failed = _perception_failure(target_before)
        if failed is not None:
            return failed
        observed_before = self.body.perceive("blockAt", _block_params(observe_pos))
        failed = _perception_failure(observed_before)
        if failed is not None:
            return failed

        before_inventory = _read_inventory(self.body)
        failed = _perception_failure(before_inventory)
        if failed is not None:
            return failed
        before_seed_count = _count_item(before_inventory, seed_item)

        attempt = _attempt_crop_sow(
            self.body,
            pos=pos,
            observe_pos=observe_pos,
            crop_block=crop_block,
            seed_item=seed_item,
            allow_server_substitute=True,
            timeout_s=timeout_s,
        )
        if isinstance(attempt, ToolResult):
            return attempt

        after_inventory = _read_inventory(self.body)
        failed = _perception_failure(after_inventory)
        if failed is not None:
            return failed
        after_seed_count = _count_item(after_inventory, seed_item)

        target_after_type = normalize_block_type(str(attempt.target_after.data.get("type") or "unknown"))
        target_after_state = str(attempt.target_after.data.get("state") or "UNKNOWN")
        target_after_properties = _normalize_block_properties(attempt.target_after.data.get("properties"))
        after_type = normalize_block_type(str(attempt.observe_after.data.get("type") or "unknown"))
        after_state = str(attempt.observe_after.data.get("state") or "UNKNOWN")
        after_properties = _normalize_block_properties(attempt.observe_after.data.get("properties"))
        before_type = normalize_block_type(str(observed_before.data.get("type") or "unknown"))
        before_state = str(observed_before.data.get("state") or "UNKNOWN")
        before_properties = _normalize_block_properties(observed_before.data.get("properties"))
        target_before_type = normalize_block_type(str(target_before.data.get("type") or "unknown"))
        target_before_state = str(target_before.data.get("state") or "UNKNOWN")
        target_before_properties = _normalize_block_properties(target_before.data.get("properties"))

        seed_delta = before_seed_count - after_seed_count
        metrics = {
            "item": seed_item,
            "target": list(pos),
            "observe_pos": list(observe_pos),
            "crop_block": crop_block,
            "target_before": {
                "type": target_before_type,
                "state": target_before_state,
                "properties": target_before_properties,
            },
            "target_after": {
                "type": target_after_type,
                "state": target_after_state,
                "properties": target_after_properties,
            },
            "observed_before": {"type": before_type, "state": before_state, "properties": before_properties},
            "observed_after": {"type": after_type, "state": after_state, "properties": after_properties},
            "before_seed_count": before_seed_count,
            "after_seed_count": after_seed_count,
            "seed_delta": seed_delta,
            "look": attempt.look.to_payload(),
            "use": attempt.use.to_payload(),
        }
        if after_type == crop_block and seed_delta == 1:
            return ToolResult(success=True, reason="completed", can_retry=False, metrics=metrics)
        if after_type == crop_block and seed_delta == 0:
            return ToolResult(
                success=False,
                reason="sow_seed_not_consumed",
                can_retry=False,
                next_suggestion="inspect live inventory mutation before accepting sow completion",
                metrics=metrics,
            )
        if after_type != crop_block and seed_delta == 1:
            return ToolResult(
                success=False,
                reason="sow_crop_not_observed",
                can_retry=True,
                next_suggestion="inspect the observed crop block truth and retry if the environment changed",
                metrics=metrics,
            )
        if not attempt.use.success:
            return ToolResult(
                success=False,
                reason=f"targeted_use_failed:{attempt.use.reason}",
                can_retry=attempt.use.can_retry,
                next_suggestion=attempt.use.next_suggestion,
                metrics=metrics,
            )
        return ToolResult(
            success=False,
            reason="targeted_use_no_effect",
            can_retry=True,
            next_suggestion="verify the selected seed, farmland target, and crop truth before retrying",
            metrics=metrics,
        )


def _dispatch(body: Body, name: str, params: dict[str, object], *, timeout_s: float) -> ToolResult:
    action = Action.create(name, params)
    accepted = body.execute(action)
    rejected = _acceptance_failure(accepted)
    if rejected is not None:
        return rejected
    terminal = body.await_action_terminal(action.id, timeout_s=timeout_s)
    return terminal_event_to_tool_result(terminal)


def _await_position_delta(
    body: Body,
    *,
    origin: tuple[float, float, float],
    current,
    min_position_delta: float,
    timeout_s: float = 1.5,
    poll_interval_s: float = 0.1,
):
    latest = current
    if dist(origin, latest.pos) >= min_position_delta:
        return latest
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(poll_interval_s)
        latest = body.get_state()
        if dist(origin, latest.pos) >= min_position_delta:
            return latest
    return latest


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
        type=last.type,
        ok=last.ok,
        complete=last.complete,
        data=data,
        uncertainty=last.uncertainty,
        next=last.next,
        error=last.error,
    )


def _perception_failure(perception: PerceptionResult) -> ToolResult | None:
    shared = shared_perception_failure(perception)
    if shared is not None:
        return shared
    if perception.ok and perception.complete:
        return None
    return ToolResult(
        success=False,
        reason="perception_failed",
        can_retry=True,
        next_suggestion="refresh inventory facts before attempting consumption",
        metrics={
            "scope": perception.scope,
            "ok": perception.ok,
            "complete": perception.complete,
            "error": perception.error,
            "uncertainty": perception.uncertainty,
        },
    )


def _acceptance_failure(result: Result) -> ToolResult | None:
    if result.ok and result.accepted:
        return None
    return ToolResult(
        success=False,
        reason="body_rejected",
        can_retry=True,
        metrics={
            "accepted": {
                "ok": result.ok,
                "accepted": result.accepted,
                "error": result.error,
                "data": result.data,
            }
        },
    )


def _count_item(perception: PerceptionResult, item: str) -> int:
    total = 0
    for slot in perception.data.get("slots") or []:
        inv_slot = InventorySlot.from_payload(slot)
        if _same_item(inv_slot.item, item):
            total += inv_slot.count
    return total


def _same_item(actual: str | None, wanted: str) -> bool:
    if actual is None:
        return False
    return actual == wanted or actual == f"minecraft:{wanted}" or f"minecraft:{actual}" == wanted


def _is_food_item(item: str) -> bool:
    plain = item.removeprefix("minecraft:")
    return plain in FOOD_ITEMS


def _normalize_use_item(item: str | None) -> str | None:
    if item is None:
        return None
    plain = item.removeprefix("minecraft:")
    if plain == "air":
        return None
    return item


def _watched_item_metrics(
    before_inventory: PerceptionResult,
    after_inventory: PerceptionResult,
    watched_items: tuple[str, ...] | list[str],
) -> dict[str, object]:
    normalized: list[str] = []
    seen: set[str] = set()
    for item in watched_items:
        key = str(item).removeprefix("minecraft:")
        if key in seen:
            continue
        seen.add(key)
        normalized.append(key)

    before_counts = {item: _count_item(before_inventory, item) for item in normalized}
    after_counts = {item: _count_item(after_inventory, item) for item in normalized}
    deltas = {item: after_counts[item] - before_counts[item] for item in normalized}
    return {
        "watched_items": normalized,
        "watched_item_counts_before": before_counts,
        "watched_item_counts_after": after_counts,
        "watched_item_deltas": deltas,
    }


def _normalize_required_item_deltas(required: dict[str, int] | None) -> dict[str, int] | None:
    if required is None:
        return None
    return {str(item).removeprefix("minecraft:"): int(delta) for item, delta in required.items()}


def _meets_watched_item_delta_requirements(
    deltas: object,
    required: dict[str, int] | None,
) -> bool:
    if required is None:
        return True
    if not isinstance(deltas, dict):
        return False
    normalized_required = _normalize_required_item_deltas(required)
    if normalized_required is None:
        return True
    for item, delta in normalized_required.items():
        if int(deltas.get(item) or 0) < int(delta):
            return False
    return True


def _effect_delta_metrics(before: object, after: object) -> dict[str, object]:
    before_effects = _normalize_effects(before)
    after_effects = _normalize_effects(after)
    before_by_id = {str(effect["id"]): effect for effect in before_effects}
    after_by_id = {str(effect["id"]): effect for effect in after_effects}

    added = [dict(after_by_id[effect_id]) for effect_id in sorted(set(after_by_id) - set(before_by_id))]
    removed = [dict(before_by_id[effect_id]) for effect_id in sorted(set(before_by_id) - set(after_by_id))]

    refreshed: list[dict[str, object]] = []
    for effect_id in sorted(set(before_by_id) & set(after_by_id)):
        before_effect = before_by_id[effect_id]
        after_effect = after_by_id[effect_id]
        before_amplifier = int(before_effect["amplifier"])
        after_amplifier = int(after_effect["amplifier"])
        before_duration = int(before_effect["duration"])
        after_duration = int(after_effect["duration"])
        if after_amplifier != before_amplifier or after_duration > before_duration + 1:
            refreshed.append(
                {
                    "id": effect_id,
                    "before": dict(before_effect),
                    "after": dict(after_effect),
                }
            )

    return {
        "effects_before": before_effects,
        "effects_after": after_effects,
        "effects_added": added,
        "effects_removed": removed,
        "effects_refreshed": refreshed,
        "effect_delta": len(added) + len(removed) + len(refreshed),
    }


def _normalize_effects(raw: object) -> list[dict[str, object]]:
    if not isinstance(raw, list):
        return []

    normalized: list[dict[str, object]] = []
    for effect in raw:
        if not isinstance(effect, dict):
            continue
        effect_id = _normalize_effect_id(effect.get("id") or effect.get("effect") or effect.get("name"))
        if effect_id is None:
            continue
        amplifier = _coerce_int(effect.get("amplifier"), default=0)
        duration = _coerce_int(effect.get("duration"), default=0)
        normalized.append(
            {
                "id": effect_id,
                "amplifier": amplifier,
                "duration": duration,
            }
        )
    normalized.sort(key=lambda effect: (str(effect["id"]), int(effect["amplifier"]), int(effect["duration"])))
    return normalized


def _normalize_effect_id(value: object) -> str | None:
    if value is None:
        return None
    effect_id = str(value).strip()
    if not effect_id:
        return None
    return effect_id.removeprefix("minecraft:")


def _coerce_int(value: object, *, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(str(value)))
        except (TypeError, ValueError):
            return default


def _entity_metrics(target: NearbyEntityTarget) -> dict[str, object]:
    return {
        "id": target.entity_id,
        "name": target.name,
        "type": target.entity_type,
        "pos": list(target.pos),
        "health": target.health,
        "distance": target.distance,
    }


def _entity_look_target(target: NearbyEntityTarget) -> tuple[float, float, float]:
    return (float(target.pos[0]), float(target.pos[1] + 1.0), float(target.pos[2]))


def _should_use_fire_primitive(
    *,
    item: str | None,
    pos: Position,
    observed_pos: Position,
    expectation: BlockUseExpectation | None,
) -> bool:
    if item is None or not _same_item(item, "minecraft:flint_and_steel"):
        return False
    if observed_pos != pos:
        return False
    if expectation is None:
        return False
    if expectation.expected_block_types != ("fire",):
        return False
    if expectation.expected_properties:
        return False
    return pos[1] >= 70


def _attempt_block_use(
    body: Body,
    *,
    pos: Position,
    observe_pos: Position,
    look_target: tuple[float, float, float],
    item: str | None,
    use_mode: str,
    use_ticks: int,
    look_timeout_s: float,
    timeout_s: float,
) -> BlockUseAttempt | ToolResult:
    stabilized = _stop_body_controls(body, timeout_s=min(timeout_s, 1.5))
    if not stabilized.success:
        return ToolResult(
            success=False,
            reason=f"stabilize_failed:{stabilized.reason}",
            can_retry=stabilized.can_retry,
            next_suggestion=stabilized.next_suggestion,
            metrics={"stabilize": stabilized.to_payload()},
        )
    looked = _look_at(body, look_target, timeout_s=look_timeout_s)
    if not looked.success:
        return looked
    params: dict[str, object] = {"mode": use_mode, "ticks": use_ticks}
    if item is not None:
        params["item"] = item
    used = _dispatch(
        body,
        "useItem",
        params,
        timeout_s=timeout_s,
    )
    target_after = body.perceive("blockAt", _block_params(pos))
    failed = _perception_failure(target_after)
    if failed is not None:
        return failed
    observe_after = target_after
    if observe_pos != pos:
        observe_after = body.perceive("blockAt", _block_params(observe_pos))
        failed = _perception_failure(observe_after)
        if failed is not None:
            return failed
    return BlockUseAttempt(look=looked, use=used, target_after=target_after, observe_after=observe_after)


def _attempt_fire_ignite(
    body: Body,
    *,
    pos: Position,
    observe_pos: Position,
    item: str | None,
    allow_server_substitute: bool,
    timeout_s: float,
) -> BlockUseAttempt | ToolResult:
    terminal = terminal_event_to_tool_result(
        body.ignite_block(
            pos,
            item=item,
            allow_server_substitute=allow_server_substitute,
            timeout_s=timeout_s,
        )
    )
    target_after = body.perceive("blockAt", _block_params(pos))
    failed = _perception_failure(target_after)
    if failed is not None:
        return failed
    observe_after = target_after
    if observe_pos != pos:
        observe_after = body.perceive("blockAt", _block_params(observe_pos))
        failed = _perception_failure(observe_after)
        if failed is not None:
            return failed
    look_metrics = {"target": list(pos), "via": "igniteBlock"}
    return BlockUseAttempt(
        look=ToolResult(success=True, reason="completed", can_retry=False, metrics=look_metrics),
        use=terminal,
        target_after=target_after,
        observe_after=observe_after,
    )


def _attempt_crop_sow(
    body: Body,
    *,
    pos: Position,
    observe_pos: Position,
    crop_block: str,
    seed_item: str | None,
    allow_server_substitute: bool,
    timeout_s: float,
) -> BlockUseAttempt | ToolResult:
    terminal = terminal_event_to_tool_result(
        body.sow_crop(
            pos,
            crop_block=crop_block,
            seed_item=seed_item,
            allow_server_substitute=allow_server_substitute,
            timeout_s=timeout_s,
        )
    )
    target_after = body.perceive("blockAt", _block_params(pos))
    failed = _perception_failure(target_after)
    if failed is not None:
        return failed
    observe_after = target_after
    if observe_pos != pos:
        observe_after = body.perceive("blockAt", _block_params(observe_pos))
        failed = _perception_failure(observe_after)
        if failed is not None:
            return failed
    look_metrics = {"target": list(pos), "observe_pos": list(observe_pos), "via": "sowCrop"}
    return BlockUseAttempt(
        look=ToolResult(success=True, reason="completed", can_retry=False, metrics=look_metrics),
        use=terminal,
        target_after=target_after,
        observe_after=observe_after,
    )


def _should_retry_line_of_sight(
    attempt: BlockUseAttempt,
    *,
    before_type: str,
    before_state: str,
) -> bool:
    after_type = normalize_block_type(str(attempt.observe_after.data.get("type") or "unknown"))
    after_state = str(attempt.observe_after.data.get("state") or "UNKNOWN")
    unchanged = after_type == before_type and after_state == before_state
    return unchanged and attempt.use.reason in {"no_effect", "completed"}


def _recover_line_of_sight(
    body: Body,
    navigator: InteractionNavigator | None,
    *,
    pos: Position,
    timeout_s: float,
    max_retries: int,
    arrival_radius: float | None,
    center_after_navigation: bool = True,
    stand_points: list[Position] | None = None,
) -> dict[str, object] | ToolResult | None:
    if navigator is None or max_retries <= 0:
        return None

    state = body.get_state()
    current = state.pos
    candidates_source = stand_points
    if candidates_source is None:
        candidates_source = interaction_stand_points(body, pos)
    if isinstance(candidates_source, ToolResult):
        return candidates_source

    candidates = [
        stand
        for stand in candidates_source
        if dist(current, (stand[0] + 0.5, stand[1], stand[2] + 0.5)) > 0.75
    ]
    if not candidates:
        return {
            "attempted": True,
            "repositioned": False,
            "retries_used": 0,
            "reason": "no_alternate_stand_point",
            "attempts": [],
        }

    attempts: list[dict[str, object]] = []
    for stand in candidates[:max_retries]:
        nav_kwargs: dict[str, object] = {"timeout_s": timeout_s}
        if arrival_radius is not None:
            nav_kwargs["arrival_radius"] = arrival_radius
        nav_result = navigator.navigate_to(stand, **nav_kwargs)
        attempt: dict[str, object] = {"goal": list(stand), "result": nav_result.to_payload()}
        if nav_result.success and arrival_radius is not None and center_after_navigation:
            center_result = _move_to_stand_center(
                body,
                stand,
                arrival_radius=arrival_radius,
                timeout_s=timeout_s,
            )
            attempt["center_result"] = center_result.to_payload()
            if not center_result.success:
                attempts.append(attempt)
                continue
        attempts.append(attempt)
        if nav_result.success:
            return {
                "attempted": True,
                "repositioned": True,
                "retries_used": len(attempts),
                "stand_target": list(stand),
                "attempts": attempts,
            }

    return {
        "attempted": True,
        "repositioned": False,
        "retries_used": len(attempts),
        "reason": "reposition_failed",
        "attempts": attempts,
    }


def _look_at(body: Body, target: tuple[float, float, float], *, timeout_s: float) -> ToolResult:
    action = Action.create("lookAt", {"target": list(target)})
    accepted = body.execute(action)
    rejected = _acceptance_failure(accepted)
    if rejected is not None:
        return rejected
    terminal = body.await_action_terminal(action.id, timeout_s=timeout_s)
    result = terminal_event_to_tool_result(terminal)
    if result.success:
        return ToolResult(
            success=True,
            reason=result.reason,
            can_retry=False,
            metrics={"action_id": action.id, "target": list(target), **dict(result.metrics or {})},
        )
    return ToolResult(
        success=False,
        reason=f"look_failed:{result.reason}",
        can_retry=result.can_retry,
        next_suggestion=result.next_suggestion,
        metrics={"action_id": action.id, "target": list(target), **dict(result.metrics or {})},
    )


def _move_to_stand_center(
    body: Body,
    stand: Position,
    *,
    arrival_radius: float,
    timeout_s: float,
) -> ToolResult:
    center = (stand[0] + 0.5, stand[1], stand[2] + 0.5)
    action = Action.create(
        "moveTo",
        {
            "target": list(center),
            "waypoints": [list(center)],
            "arrival_radius": arrival_radius,
            "timeout_ticks": 80,
            "no_progress_ticks": 25,
            "max_deviation": 1.5,
        },
    )
    accepted = body.execute(action)
    rejected = _acceptance_failure(accepted)
    if rejected is not None:
        return rejected
    terminal = body.await_action_terminal(action.id, timeout_s=timeout_s)
    result = terminal_event_to_tool_result(terminal)
    if result.success:
        stop_result = _stop_body_controls(body, timeout_s=min(timeout_s, 2.0))
        if stop_result.success:
            time.sleep(POST_MOVE_SETTLE_S)
            return ToolResult(
                success=True,
                reason=result.reason,
                can_retry=False,
                metrics={
                    "action_id": action.id,
                    "stand": list(stand),
                    "center": list(center),
                    **dict(result.metrics or {}),
                    "stabilize": stop_result.to_payload(),
                },
            )
        return ToolResult(
            success=False,
            reason=f"stabilize_failed:{stop_result.reason}",
            can_retry=stop_result.can_retry,
            next_suggestion=stop_result.next_suggestion,
            metrics={
                "action_id": action.id,
                "stand": list(stand),
                "center": list(center),
                **dict(result.metrics or {}),
                "stabilize": stop_result.to_payload(),
            },
        )
    return ToolResult(
        success=False,
        reason=result.reason,
        can_retry=result.can_retry,
        next_suggestion=result.next_suggestion,
        metrics={"action_id": action.id, "stand": list(stand), "center": list(center), **dict(result.metrics or {})},
    )


def _stop_body_controls(body: Body, *, timeout_s: float) -> ToolResult:
    action = Action.create("stop", {})
    accepted = body.execute(action)
    rejected = _acceptance_failure(accepted)
    if rejected is not None:
        return rejected
    terminal = body.await_action_terminal(action.id, timeout_s=timeout_s)
    return terminal_event_to_tool_result(terminal)


def _block_params(pos: Position) -> dict[str, int]:
    return {"x": int(pos[0]), "y": int(pos[1]), "z": int(pos[2])}


def _block_center_target(pos: Position) -> tuple[float, float, float]:
    return (pos[0] + 0.5, pos[1] + 0.5, pos[2] + 0.5)


def _normalize_expectation(
    expected_block_types: tuple[str, ...] | list[str] | None,
    expected_properties: dict[str, str] | None,
    allow_unchanged: bool,
) -> BlockUseExpectation | None:
    if not expected_block_types and not expected_properties:
        return None
    normalized_types = tuple(normalize_block_type(str(block_type)) for block_type in (expected_block_types or ()))
    normalized_properties = _normalize_block_properties(expected_properties)
    return BlockUseExpectation(
        expected_block_types=normalized_types,
        expected_properties=normalized_properties,
        allow_unchanged=allow_unchanged,
    )


def _normalize_block_properties(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, str] = {}
    for key, value in raw.items():
        normalized[str(key)] = str(value).lower()
    return normalized


def _matches_expectation(
    block_type: str,
    block_properties: dict[str, str],
    expectation: BlockUseExpectation,
) -> bool:
    if expectation.expected_block_types and block_type not in expectation.expected_block_types:
        return False
    for key, expected_value in expectation.expected_properties.items():
        if block_properties.get(key) != expected_value:
            return False
    return True
