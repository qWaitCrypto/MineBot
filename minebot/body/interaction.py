"""Body transactions for entity-centered interactions."""

from __future__ import annotations

import time
from dataclasses import asdict
from dataclasses import dataclass
from math import dist, floor

from minebot.body.interaction_support import (
    DirectInteractionNavigator,
    INTERACTION_RANGE,
    InteractionNavigator,
    NearbyEntityTarget,
    _move_to_stand_center,
    ensure_interaction_range,
    find_block_target,
    ensure_entity_range,
    find_entity_target,
    find_named_entity_target,
    interaction_stand_points,
    merge_context,
    normalize_block_type,
    perception_failure,
    refresh_entity_target,
)
from minebot.body.block_work import BlockWork
from minebot.body.inventory import InventoryTransactions
from minebot.body.use import UseTransactions
from minebot.contract import (
    Action,
    Body,
    BreakContext,
    InteractionContext,
    Position,
    Result,
    ToolResult,
    terminal_event_to_tool_result,
)
from minebot.game.governance import GovernancePolicy


HANDOFF_MIN_DISTANCE = 1.25
HANDOFF_MAX_DISTANCE = 3.0


@dataclass(frozen=True)
class HandoffReceipt:
    player: str
    item: str | None
    count: int
    seq: int
    tick: int


class InteractionTransactions:
    """Single-objective entity interaction workflows."""

    BED_BLOCK_TYPES = (
        "bed",
        "white_bed",
        "orange_bed",
        "magenta_bed",
        "light_blue_bed",
        "yellow_bed",
        "lime_bed",
        "pink_bed",
        "gray_bed",
        "light_gray_bed",
        "cyan_bed",
        "purple_bed",
        "blue_bed",
        "brown_bed",
        "green_bed",
        "red_bed",
        "black_bed",
    )
    BED_SLEEP_START_TICK = 12542
    BED_SLEEP_END_TICK = 23459
    TILLABLE_BLOCK_TYPES = ("dirt", "grass_block", "grass_path", "dirt_path", "coarse_dirt", "rooted_dirt")
    SEED_TO_CROP_BLOCK = {
        "wheat_seeds": "wheat",
        "beetroot_seeds": "beetroots",
        "carrot": "carrots",
        "potato": "potatoes",
    }
    CROP_TO_SEED_ITEM = {
        "wheat": "wheat_seeds",
        "beetroots": "beetroot_seeds",
        "carrots": "carrot",
        "potatoes": "potato",
    }
    CROP_TO_HARVEST_DROPS = {
        "wheat": ("wheat", "wheat_seeds"),
        "beetroots": ("beetroot", "beetroot_seeds"),
        "carrots": ("carrot",),
        "potatoes": ("potato",),
    }
    CROP_MAX_AGE = {
        "wheat": 7,
        "beetroots": 3,
        "carrots": 7,
        "potatoes": 7,
    }
    SWITCHABLE_BLOCK_TYPES = (
        "lever",
        "stone_button",
        "polished_blackstone_button",
        "oak_button",
        "spruce_button",
        "birch_button",
        "jungle_button",
        "acacia_button",
        "dark_oak_button",
        "mangrove_button",
        "cherry_button",
        "crimson_button",
        "warped_button",
        "bamboo_button",
        "pale_oak_button",
    )
    OPENABLE_BLOCK_TYPES = (
        "oak_door",
        "spruce_door",
        "birch_door",
        "jungle_door",
        "acacia_door",
        "dark_oak_door",
        "mangrove_door",
        "cherry_door",
        "crimson_door",
        "warped_door",
        "bamboo_door",
        "pale_oak_door",
        "iron_door",
        "oak_trapdoor",
        "spruce_trapdoor",
        "birch_trapdoor",
        "jungle_trapdoor",
        "acacia_trapdoor",
        "dark_oak_trapdoor",
        "mangrove_trapdoor",
        "cherry_trapdoor",
        "crimson_trapdoor",
        "warped_trapdoor",
        "bamboo_trapdoor",
        "iron_trapdoor",
        "oak_fence_gate",
        "spruce_fence_gate",
        "birch_fence_gate",
        "jungle_fence_gate",
        "acacia_fence_gate",
        "dark_oak_fence_gate",
        "mangrove_fence_gate",
        "cherry_fence_gate",
        "crimson_fence_gate",
        "warped_fence_gate",
        "bamboo_fence_gate",
        "pale_oak_fence_gate",
    )
    REDSTONE_ONLY_OPENABLE_BLOCK_TYPES = ("iron_door", "iron_trapdoor")

    def __init__(
        self,
        body: Body,
        *,
        navigator: InteractionNavigator | None = None,
        inventory: InventoryTransactions | None = None,
        use: UseTransactions | None = None,
        work: BlockWork | None = None,
        governance: GovernancePolicy | None = None,
    ):
        self.body = body
        self.navigator = navigator
        self.inventory = inventory or InventoryTransactions(body)
        self.use = use or UseTransactions(body, navigator=navigator, inventory=self.inventory)
        self.work = work
        self.governance = governance

    def give_player(
        self,
        *,
        receiver_name: str,
        item: str,
        count: int,
        search_radius: int = 12,
        approach_timeout_s: float = 15.0,
        look_timeout_s: float = 2.0,
        pickup_timeout_s: float = 3.0,
    ) -> ToolResult:
        if receiver_name == self.body.bot_name:
            return ToolResult(
                success=False,
                reason="receiver_is_self",
                can_retry=False,
                metrics={"receiver_name": receiver_name, "item": item, "requested_count": count},
            )
        if count <= 0:
            return ToolResult(
                success=False,
                reason="invalid_count",
                can_retry=False,
                metrics={"receiver_name": receiver_name, "item": item, "requested_count": count},
            )

        target = find_named_entity_target(
            self.body,
            receiver_name,
            radius=search_radius,
            not_found_reason="receiver_not_found",
        )
        if isinstance(target, ToolResult):
            return target

        approach_navigator = self.navigator
        if approach_navigator is None and target.distance <= INTERACTION_RANGE:
            approach_navigator = DirectInteractionNavigator(self.body, arrival_radius=0.25)

        approach = ensure_entity_range(
            self.body,
            approach_navigator,
            target.pos,
            min_distance=HANDOFF_MIN_DISTANCE,
            max_distance=HANDOFF_MAX_DISTANCE,
            vertical_tolerance=1.5,
            timeout_s=approach_timeout_s,
            missing_reason="receiver_navigation_missing",
            failure_prefix="receiver_navigation_failed",
            no_stand_reason="receiver_no_stand_point",
            include_entity_block=True,
        )
        if isinstance(approach, ToolResult):
            return merge_context(
                approach,
                {
                    "receiver_name": receiver_name,
                    "item": item,
                    "requested_count": count,
                    "search_radius": search_radius,
                    "receiver_target": _target_metrics(target),
                },
            )

        refreshed = find_named_entity_target(
            self.body,
            receiver_name,
            radius=search_radius,
            not_found_reason="receiver_not_found_after_navigation",
        )
        if isinstance(refreshed, ToolResult):
            return merge_context(
                refreshed,
                {
                    "receiver_name": receiver_name,
                    "item": item,
                    "requested_count": count,
                    "search_radius": search_radius,
                    "receiver_target": _target_metrics(target),
                    "approach": approach,
                },
            )
        target = refreshed
        refreshed_distance = dist(self.body.get_state().pos, target.pos)
        if refreshed_distance > HANDOFF_MAX_DISTANCE:
            adjustment = ensure_entity_range(
                self.body,
                approach_navigator,
                target.pos,
                min_distance=HANDOFF_MIN_DISTANCE,
                max_distance=HANDOFF_MAX_DISTANCE,
                vertical_tolerance=1.5,
                timeout_s=approach_timeout_s,
                missing_reason="receiver_navigation_missing",
                failure_prefix="receiver_navigation_failed",
                no_stand_reason="receiver_no_stand_point",
                include_entity_block=True,
            )
            if isinstance(adjustment, ToolResult):
                return merge_context(
                    adjustment,
                    {
                        "receiver_name": receiver_name,
                        "item": item,
                        "requested_count": count,
                        "search_radius": search_radius,
                        "receiver_target": _target_metrics(target),
                        "approach": approach,
                    },
                )
            approach = {
                **approach,
                "refreshed_distance_before_adjustment": refreshed_distance,
                "refresh_adjustment": adjustment,
            }
            refreshed = find_named_entity_target(
                self.body,
                receiver_name,
                radius=search_radius,
                not_found_reason="receiver_not_found_after_adjustment",
            )
            if isinstance(refreshed, ToolResult):
                return merge_context(
                    refreshed,
                    {
                        "receiver_name": receiver_name,
                        "item": item,
                        "requested_count": count,
                        "search_radius": search_radius,
                        "receiver_target": _target_metrics(target),
                        "approach": approach,
                    },
                )
            target = refreshed

        look = self._look_at(target, timeout_s=look_timeout_s)
        if not look.success:
            return merge_context(
                look,
                {
                    "receiver_name": receiver_name,
                    "item": item,
                    "requested_count": count,
                    "search_radius": search_radius,
                    "receiver_target": _target_metrics(target),
                    "approach": approach,
                },
            )

        # Avoid classifying stale pickup events as receipt for this drop.
        self.body.poll_events()

        handoff = self._handoff_item(
            receiver_name=receiver_name,
            item=item,
            count=count,
            timeout_s=look_timeout_s,
        )
        spawned = int((handoff.metrics or {}).get("spawned_count") or 0)
        handoff = merge_context(
            handoff,
            {
                "receiver_name": receiver_name,
                "item": item,
                "requested_count": count,
                "search_radius": search_radius,
                "receiver_target": _target_metrics(target),
                "approach": approach,
                "look": look.metrics,
            },
        )
        if not handoff.success:
            return handoff

        receipt = self._await_pickup(receiver_name, item, timeout_s=pickup_timeout_s)
        if receipt is None:
            return merge_context(
                ToolResult(
                    success=False,
                    reason="receiver_pickup_unconfirmed",
                    can_retry=True,
                    next_suggestion="move closer to the receiver or wait for a visible pickup receipt before retrying",
                    metrics={"spawned_count": spawned},
                ),
                dict(handoff.metrics or {}),
            )

        return merge_context(
            ToolResult(
                success=True,
                reason="completed",
                can_retry=False,
                metrics={
                    "spawned_count": spawned,
                    "pickup_receipt": {
                        "player": receipt.player,
                        "item": receipt.item,
                        "count": receipt.count,
                        "seq": receipt.seq,
                        "tick": receipt.tick,
                    },
                },
            ),
            dict(handoff.metrics or {}),
        )

    def follow_player(
        self,
        *,
        player_name: str,
        search_radius: int = 24,
        min_distance: float = 2.0,
        max_distance: float = 4.5,
        vertical_tolerance: float = 1.5,
        timeout_s: float = 15.0,
        maintenance_checks: int = 1,
        maintenance_interval_s: float = 0.0,
    ) -> ToolResult:
        """Enter and verify a bounded follow distance band around one named player."""
        return self._enter_player_distance_band(
            player_name=player_name,
            search_radius=search_radius,
            min_distance=min_distance,
            max_distance=max_distance,
            vertical_tolerance=vertical_tolerance,
            timeout_s=timeout_s,
            maintenance_checks=maintenance_checks,
            maintenance_interval_s=maintenance_interval_s,
            not_found_reason="follow_target_not_found",
            missing_reason="follow_navigation_missing",
            failure_prefix="follow_navigation_failed",
            no_stand_reason="follow_target_no_stand_point",
            lost_reason="follow_target_lost",
            out_of_band_reason="follow_target_out_of_band",
            success_reason="distance_band_reached",
            retry_suggestion="retry from a clearer route or reacquire the player before continuing the follow task",
        )

    def go_to_player(
        self,
        *,
        player_name: str,
        search_radius: int = 24,
        min_distance: float = 1.0,
        max_distance: float = 4.5,
        vertical_tolerance: float = 1.5,
        timeout_s: float = 15.0,
        maintenance_checks: int = 1,
        maintenance_interval_s: float = 0.0,
    ) -> ToolResult:
        """Reach and verify an interaction-range band around one named player."""
        return self._enter_player_distance_band(
            player_name=player_name,
            search_radius=search_radius,
            min_distance=min_distance,
            max_distance=max_distance,
            vertical_tolerance=vertical_tolerance,
            timeout_s=timeout_s,
            maintenance_checks=maintenance_checks,
            maintenance_interval_s=maintenance_interval_s,
            not_found_reason="goto_player_target_not_found",
            missing_reason="goto_player_navigation_missing",
            failure_prefix="goto_player_navigation_failed",
            no_stand_reason="goto_player_target_no_stand_point",
            lost_reason="goto_player_target_lost",
            out_of_band_reason="goto_player_out_of_band",
            success_reason="player_reached",
            retry_suggestion="retry from a clearer route or reacquire the player before continuing the approach",
        )

    def search_for_entity(
        self,
        *,
        entity_types: tuple[str, ...] = (),
        entity_name: str | None = None,
        search_radius: int = 24,
        min_distance: float = 0.0,
        max_distance: float = 4.5,
        vertical_tolerance: float = 1.5,
        timeout_s: float = 15.0,
    ) -> ToolResult:
        """Find one entity target and, if needed, approach into usable range."""
        if entity_name is None and not entity_types:
            return ToolResult(
                success=False,
                reason="search_entity_filter_missing",
                can_retry=False,
                metrics={"search_radius": search_radius},
            )
        if min_distance < 0 or max_distance <= 0 or min_distance > max_distance:
            return ToolResult(
                success=False,
                reason="invalid_distance_band",
                can_retry=False,
                metrics={
                    "entity_name": entity_name,
                    "entity_types": list(entity_types),
                    "min_distance": min_distance,
                    "max_distance": max_distance,
                    "vertical_tolerance": vertical_tolerance,
                },
            )

        target = find_entity_target(
            self.body,
            radius=search_radius,
            not_found_reason="search_entity_not_found",
            wanted_types=entity_types,
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
            missing_reason="search_entity_navigation_missing",
            failure_prefix="search_entity_navigation_failed",
            no_stand_reason="search_entity_no_stand_point",
        )
        context = {
            "entity_name": entity_name,
            "entity_types": list(entity_types),
            "search_radius": search_radius,
            "distance_band": {
                "min_distance": min_distance,
                "max_distance": max_distance,
                "vertical_tolerance": vertical_tolerance,
            },
            "target": _target_metrics(target),
        }
        if isinstance(approach, ToolResult):
            return merge_context(approach, context)

        refreshed = refresh_entity_target(
            self.body,
            target,
            radius=search_radius,
            not_found_reason="search_entity_target_lost",
            wanted_types=entity_types,
            entity_name=entity_name,
        )
        if isinstance(refreshed, ToolResult):
            return merge_context(refreshed, {**context, "approach": approach})

        target = refreshed
        state = self.body.get_state()
        final_distance = _distance_to_entity(state.pos, target.pos)
        final_vertical = abs(state.pos[1] - target.pos[1])
        metrics = {
            **context,
            "target": _target_metrics(target),
            "approach": approach,
            "final_distance": final_distance,
            "final_vertical_delta": final_vertical,
            "body_pos": list(state.pos),
        }
        if final_distance < min_distance or final_distance > max_distance or final_vertical > vertical_tolerance:
            return ToolResult(
                success=False,
                reason="search_entity_out_of_band",
                can_retry=True,
                next_suggestion="retry from a clearer route or reacquire the target before continuing the approach",
                metrics=metrics,
            )

        return ToolResult(
            success=True,
            reason="entity_in_range",
            can_retry=False,
            metrics=metrics,
        )

    def go_to_bed(
        self,
        *,
        search_radius: int = 24,
        approach_timeout_s: float = 15.0,
        look_timeout_s: float = 2.0,
        use_timeout_s: float = 8.0,
    ) -> ToolResult:
        """Find a bed, approach it, use it, and verify actual sleep state."""
        before = self.body.get_state()
        if before.sleeping is True:
            return ToolResult(
                success=True,
                reason="already_sleeping",
                can_retry=False,
                metrics={"body_pos": list(before.pos), "sleeping_before": True},
            )

        target = find_block_target(
            self.body,
            block_types=self.BED_BLOCK_TYPES,
            radius=search_radius,
            limit=32,
            not_found_reason="bed_not_found",
        )
        if isinstance(target, ToolResult):
            return target
        target_block = self.body.perceive("blockAt", {"x": target.pos[0], "y": target.pos[1], "z": target.pos[2]})
        failed = perception_failure(target_block)
        if failed is not None:
            return merge_context(failed, {"target": {"pos": list(target.pos), "type": target.block_type}})
        target_props = dict(target_block.data.get("properties") or {})
        interaction_pos = _bed_interaction_target(target.pos, target_props)

        context = {
            "search_radius": search_radius,
            "target": {
                "pos": list(target.pos),
                "type": target.block_type,
                "distance": target.distance,
                "properties": target_props,
            },
            "interaction_target": list(interaction_pos),
        }
        approach = _approach_bed_target(
            self.body,
            self.navigator,
            target.pos,
            timeout_s=approach_timeout_s,
        )
        if isinstance(approach, ToolResult):
            return merge_context(approach, context)

        refreshed = find_block_target(
            self.body,
            block_types=self.BED_BLOCK_TYPES,
            radius=search_radius,
            limit=32,
            not_found_reason="bed_target_lost",
        )
        if isinstance(refreshed, ToolResult):
            return merge_context(refreshed, {**context, "approach": approach})
        target = refreshed
        denied = self._guard_interaction_target(
            target.pos,
            target.block_type,
            InteractionContext.SLEEP,
            denied_reason="bed_denied",
        )
        if denied is not None:
            return merge_context(denied, {**context, "approach": approach})

        look = self._look_at_pos(_block_center_target(interaction_pos), timeout_s=look_timeout_s)
        if not look.success:
            return merge_context(look, {**context, "approach": approach})

        used = self._use_empty_hand(timeout_s=use_timeout_s)
        after = self.body.get_state()
        metrics = {
            **context,
            "target": {
                "pos": list(target.pos),
                "type": target.block_type,
                "distance": target.distance,
            },
            "approach": approach,
            "look": look.to_payload(),
            "use": used.to_payload(),
            "body_pos": list(after.pos),
            "sleeping_before": before.sleeping,
            "sleeping_after": after.sleeping,
        }
        if after.sleeping is True:
            return ToolResult(success=True, reason="sleeping", can_retry=False, metrics=metrics)

        if not used.success:
            return ToolResult(
                success=False,
                reason=f"bed_use_failed:{used.reason}",
                can_retry=used.can_retry,
                next_suggestion=used.next_suggestion,
                metrics=metrics,
            )

        if not _is_bedtime(after.time):
            return ToolResult(
                success=False,
                reason="bed_not_night",
                can_retry=True,
                next_suggestion="retry during the sleep window instead of attempting bed entry in daytime",
                metrics=metrics,
            )

        occupied = self.body.perceive("blockAt", {"x": interaction_pos[0], "y": interaction_pos[1], "z": interaction_pos[2]})
        failed = perception_failure(occupied)
        if failed is not None:
            return merge_context(failed, metrics)
        occupied_props = {str(key): str(value).lower() for key, value in dict(occupied.data.get("properties") or {}).items()}
        metrics["target_after"] = {
            "pos": list(interaction_pos),
            "type": normalize_block_type(str(occupied.data.get("type") or target.block_type)),
            "state": str(occupied.data.get("state") or "UNKNOWN"),
            "properties": dict(occupied.data.get("properties") or {}),
        }
        if occupied_props.get("occupied") == "true":
            return ToolResult(
                success=False,
                reason="bed_occupied",
                can_retry=True,
                next_suggestion="retry with a different unoccupied bed or wait until the current occupant leaves",
                metrics=metrics,
            )

        return ToolResult(
            success=False,
            reason="bed_not_entered",
            can_retry=True,
            next_suggestion="retry at night with a clear reachable bed and verify whether the bed is occupied or blocked",
            metrics=metrics,
        )

    def set_openable_state(
        self,
        *,
        desired_open: bool,
        pos: Position | None = None,
        search_radius: int = 12,
        block_types: tuple[str, ...] | None = None,
        approach_timeout_s: float = 15.0,
        look_timeout_s: float = 2.0,
        use_timeout_s: float = 8.0,
    ) -> ToolResult:
        """Reach one openable block and verify its final `open` property."""
        allowed = tuple(block_types or self.OPENABLE_BLOCK_TYPES)
        context: dict[str, object] = {
            "desired_open": desired_open,
            "search_radius": search_radius,
            "block_types": list(allowed),
        }

        if pos is None:
            target = find_block_target(
                self.body,
                block_types=allowed,
                radius=search_radius,
                limit=32,
                not_found_reason="openable_not_found",
            )
            if isinstance(target, ToolResult):
                return merge_context(target, context)
            target_pos = target.pos
            target_type = target.block_type
            context["target"] = {
                "pos": list(target.pos),
                "type": target.block_type,
                "distance": target.distance,
            }
        else:
            target_pos = (int(pos[0]), int(pos[1]), int(pos[2]))
            target_before = self.body.perceive(
                "blockAt",
                {"x": target_pos[0], "y": target_pos[1], "z": target_pos[2]},
            )
            failed = perception_failure(target_before)
            if failed is not None:
                return merge_context(failed, {**context, "target": {"pos": list(target_pos)}})
            target_type = normalize_block_type(str(target_before.data.get("type") or "unknown"))
            context["target"] = {
                "pos": list(target_pos),
                "type": target_type,
                "state": str(target_before.data.get("state") or "UNKNOWN"),
                "properties": dict(target_before.data.get("properties") or {}),
            }
            if target_type not in {normalize_block_type(block_type) for block_type in allowed}:
                return ToolResult(
                    success=False,
                    reason="openable_wrong_type",
                    can_retry=False,
                    metrics=context,
                )

        target_properties = dict((context.get("target") or {}).get("properties") or {})  # type: ignore[union-attr]
        if not target_properties:
            target_fact = self.body.perceive("blockAt", {"x": target_pos[0], "y": target_pos[1], "z": target_pos[2]})
            failed = perception_failure(target_fact)
            if failed is not None:
                return merge_context(failed, context)
            target_properties = dict(target_fact.data.get("properties") or {})
            if isinstance(context.get("target"), dict):
                context["target"]["properties"] = target_properties  # type: ignore[index]

        if target_type in self.REDSTONE_ONLY_OPENABLE_BLOCK_TYPES:
            return ToolResult(
                success=False,
                reason="openable_requires_redstone",
                can_retry=False,
                next_suggestion="use a redstone-capable mechanism instead of empty-hand activation",
                metrics=context,
            )
        current_open = str(target_properties.get("open") or "false").lower() == "true"
        if current_open == desired_open:
            return ToolResult(
                success=True,
                reason="already_open" if desired_open else "already_closed",
                can_retry=False,
                metrics=context,
            )
        denied = self._guard_interaction_target(
            target_pos,
            target_type,
            InteractionContext.ACTIVATE,
            denied_reason="openable_denied",
        )
        if denied is not None:
            return merge_context(denied, context)
        approach = _approach_openable_target(
            self.body,
            self.navigator,
            target_pos,
            target_type,
            target_properties,
            timeout_s=approach_timeout_s,
        )
        if isinstance(approach, ToolResult):
            return merge_context(
                approach,
                context,
            )

        result = self.use.use_on_block(
            pos=target_pos,
            item=None,
            expected_block_types=(target_type,),
            expected_properties={"open": "true" if desired_open else "false"},
            look_target=_openable_look_target(target_pos, target_type, target_properties),
            look_timeout_s=look_timeout_s,
            approach_timeout_s=approach_timeout_s,
            navigation_arrival_radius=None,
            line_of_sight_retries=0,
            timeout_s=use_timeout_s,
        )
        result = merge_context(result, {**context, "approach": approach})
        if not result.success:
            return result

        if result.reason == "already_in_expected_state":
            return ToolResult(
                success=True,
                reason="already_open" if desired_open else "already_closed",
                can_retry=False,
                metrics=result.metrics,
            )
        return ToolResult(
            success=True,
            reason="opened" if desired_open else "closed",
            can_retry=False,
            metrics=result.metrics,
        )

    def open_openable(
        self,
        *,
        pos: Position | None = None,
        search_radius: int = 12,
        block_types: tuple[str, ...] | None = None,
        approach_timeout_s: float = 15.0,
        look_timeout_s: float = 2.0,
        use_timeout_s: float = 8.0,
    ) -> ToolResult:
        """Open one nearby door/gate/trapdoor and verify `open=true`."""
        return self.set_openable_state(
            desired_open=True,
            pos=pos,
            search_radius=search_radius,
            block_types=block_types,
            approach_timeout_s=approach_timeout_s,
            look_timeout_s=look_timeout_s,
            use_timeout_s=use_timeout_s,
        )

    def close_openable(
        self,
        *,
        pos: Position | None = None,
        search_radius: int = 12,
        block_types: tuple[str, ...] | None = None,
        approach_timeout_s: float = 15.0,
        look_timeout_s: float = 2.0,
        use_timeout_s: float = 8.0,
    ) -> ToolResult:
        """Close one nearby door/gate/trapdoor and verify `open=false`."""
        return self.set_openable_state(
            desired_open=False,
            pos=pos,
            search_radius=search_radius,
            block_types=block_types,
            approach_timeout_s=approach_timeout_s,
            look_timeout_s=look_timeout_s,
            use_timeout_s=use_timeout_s,
        )

    def till_farmland(
        self,
        *,
        hoe_item: str,
        pos: Position | None = None,
        search_radius: int = 8,
        approach_timeout_s: float = 15.0,
        look_timeout_s: float = 2.0,
        use_timeout_s: float = 8.0,
    ) -> ToolResult:
        """Till one dirt-like block into farmland and verify the final block truth."""
        context: dict[str, object] = {
            "hoe_item": hoe_item,
            "search_radius": search_radius,
            "block_types": list(self.TILLABLE_BLOCK_TYPES),
        }
        if pos is not None:
            target_pos = (int(pos[0]), int(pos[1]), int(pos[2]))
            target_before = self.body.perceive("blockAt", {"x": target_pos[0], "y": target_pos[1], "z": target_pos[2]})
            failed = perception_failure(target_before)
            if failed is not None:
                return merge_context(failed, {"hoe_item": hoe_item, "target": {"pos": list(target_pos)}})
            target_type = normalize_block_type(str(target_before.data.get("type") or "unknown"))
            target_context = {
                "pos": list(target_pos),
                "type": target_type,
                "state": str(target_before.data.get("state") or "UNKNOWN"),
                "properties": dict(target_before.data.get("properties") or {}),
            }
            context["target"] = target_context
            if target_type == "farmland":
                return ToolResult(
                    success=True,
                    reason="already_tilled",
                    can_retry=False,
                    metrics=context,
                )
        target_pos, target_type, target_context_or_error = self._resolve_block_target(
            pos=pos,
            allowed_block_types=self.TILLABLE_BLOCK_TYPES,
            search_radius=search_radius,
            not_found_reason="till_target_not_found",
            wrong_type_reason="till_target_wrong_type",
        )
        if target_pos is None:
            return merge_context(target_context_or_error, context)  # type: ignore[arg-type]
        context["target"] = target_context_or_error
        denied = self._guard_interaction_target(
            target_pos,
            target_type,
            InteractionContext.FARM,
            denied_reason="till_denied",
        )
        if denied is not None:
            return merge_context(denied, context)

        result = self.use.use_on_block(
            pos=target_pos,
            item=hoe_item,
            expected_block_types=("farmland",),
            look_timeout_s=look_timeout_s,
            approach_timeout_s=approach_timeout_s,
            navigation_arrival_radius=0.25,
            timeout_s=use_timeout_s,
        )
        result = merge_context(result, context)
        if not result.success:
            return result
        if result.reason == "already_in_expected_state":
            return ToolResult(success=True, reason="already_tilled", can_retry=False, metrics=result.metrics)
        return ToolResult(success=True, reason="tilled", can_retry=False, metrics=result.metrics)

    def sow_crop(
        self,
        *,
        seed_item: str,
        farmland_pos: Position,
        expected_crop_block: str | None = None,
        stand_points: list[Position] | None = None,
        approach_timeout_s: float = 15.0,
        look_timeout_s: float = 2.0,
        use_timeout_s: float = 8.0,
    ) -> ToolResult:
        """Use seeds on farmland and verify that the crop block appears above it."""
        normalized_seed = normalize_block_type(seed_item)
        crop_block = normalize_block_type(expected_crop_block or self.SEED_TO_CROP_BLOCK.get(normalized_seed, ""))
        if not crop_block:
            return ToolResult(
                success=False,
                reason="unsupported_seed_item",
                can_retry=False,
                metrics={"seed_item": seed_item, "farmland_pos": list(farmland_pos)},
            )

        target_pos = (int(farmland_pos[0]), int(farmland_pos[1]), int(farmland_pos[2]))
        above_pos = (target_pos[0], target_pos[1] + 1, target_pos[2])
        context: dict[str, object] = {
            "seed_item": seed_item,
            "expected_crop_block": crop_block,
            "target": {"pos": list(target_pos)},
            "observe_pos": list(above_pos),
        }
        farmland = self.body.perceive("blockAt", {"x": target_pos[0], "y": target_pos[1], "z": target_pos[2]})
        failed = perception_failure(farmland)
        if failed is not None:
            return merge_context(failed, context)
        farmland_type = normalize_block_type(str(farmland.data.get("type") or "unknown"))
        context["target"]["type"] = farmland_type  # type: ignore[index]
        context["target"]["properties"] = dict(farmland.data.get("properties") or {})  # type: ignore[index]
        if farmland_type != "farmland":
            return ToolResult(
                success=False,
                reason="sow_target_not_farmland",
                can_retry=False,
                metrics=context,
            )
        denied = self._guard_interaction_target(
            target_pos,
            farmland_type,
            InteractionContext.FARM,
            denied_reason="sow_denied",
        )
        if denied is not None:
            return merge_context(denied, context)
        equip = self.use._prepare_use_item(seed_item, timeout_s=use_timeout_s)
        if not equip.success:
            return merge_context(
                ToolResult(
                    success=False,
                    reason=f"sow_equip_failed:{equip.reason}",
                    can_retry=equip.can_retry,
                    next_suggestion=equip.next_suggestion,
                    metrics={"equip": equip.to_payload()},
                ),
                context,
            )

        resolved_stand_points = stand_points
        if not resolved_stand_points:
            resolved_stand_points = interaction_stand_points(self.body, target_pos)
            if isinstance(resolved_stand_points, ToolResult):
                return merge_context(resolved_stand_points, {**context, "equip": equip.to_payload()})
        state = self.body.get_state()
        initial_distance = dist(state.pos, (target_pos[0] + 0.5, target_pos[1] + 0.5, target_pos[2] + 0.5))
        if resolved_stand_points:
            approach = ensure_interaction_range(
                self.body,
                self.navigator,
                target_pos,
                interaction_radius=INTERACTION_RANGE,
                timeout_s=approach_timeout_s,
                missing_reason="use_navigation_missing",
                failure_prefix="use_navigation_failed",
                no_stand_reason="use_no_stand_point",
                navigation_arrival_radius=0.25,
                center_after_navigation=True,
                stand_points=resolved_stand_points,
            )
            if isinstance(approach, ToolResult):
                return merge_context(
                    approach,
                    {
                        **context,
                        "item": seed_item,
                        "equip": equip.to_payload(),
                    },
                )
        elif initial_distance <= INTERACTION_RANGE:
            approach = {
                "navigated": False,
                "initial_distance": initial_distance,
                "final_distance": initial_distance,
                "standless_in_range": True,
            }
        else:
            return merge_context(
                ToolResult(
                    success=False,
                    reason="use_no_stand_point",
                    can_retry=False,
                    next_suggestion="clear a standable adjacent block before retrying the interaction",
                    metrics={"target": list(target_pos), "initial_distance": initial_distance},
                ),
                {
                    **context,
                    "item": seed_item,
                    "equip": equip.to_payload(),
                },
            )

        result = self.use._sow_crop_on_farmland(
            pos=target_pos,
            observe_pos=above_pos,
            seed_item=seed_item,
            crop_block=crop_block,
            timeout_s=use_timeout_s,
        )
        result = merge_context(
            result,
            {
                **context,
                "item": seed_item,
                "equip": equip.to_payload(),
                "approach": approach,
            },
        )
        if not result.success:
            return result
        if result.reason == "already_in_expected_state":
            return ToolResult(success=True, reason="already_sown", can_retry=False, metrics=result.metrics)
        return ToolResult(success=True, reason="sown", can_retry=False, metrics=result.metrics)

    def harvest_and_resow(
        self,
        *,
        farmland_pos: Position,
        crop_block: str | None = None,
        seed_item: str | None = None,
        break_context: BreakContext | str = BreakContext.FARM,
        timeout_s: float = 15.0,
        settle_s: float = 0.2,
        approach_timeout_s: float = 15.0,
        look_timeout_s: float = 2.0,
        use_timeout_s: float = 8.0,
    ) -> ToolResult:
        """Harvest one mature crop above farmland and re-sow the same spot."""
        if self.work is None:
            return ToolResult(
                success=False,
                reason="harvest_runtime_missing",
                can_retry=True,
                next_suggestion="provide BlockWork before attempting crop harvest",
                metrics={"farmland_pos": list(farmland_pos)},
            )

        farmland_target = (int(farmland_pos[0]), int(farmland_pos[1]), int(farmland_pos[2]))
        crop_pos = (farmland_target[0], farmland_target[1] + 1, farmland_target[2])
        context: dict[str, object] = {
            "farmland_pos": list(farmland_target),
            "crop_pos": list(crop_pos),
            "break_context": BreakContext(break_context).value,
        }

        farmland = self.body.perceive("blockAt", {"x": farmland_target[0], "y": farmland_target[1], "z": farmland_target[2]})
        failed = perception_failure(farmland)
        if failed is not None:
            return merge_context(failed, context)
        farmland_type = normalize_block_type(str(farmland.data.get("type") or "unknown"))
        context["farmland"] = {
            "type": farmland_type,
            "state": str(farmland.data.get("state") or "UNKNOWN"),
            "properties": dict(farmland.data.get("properties") or {}),
        }
        if farmland_type != "farmland":
            return ToolResult(
                success=False,
                reason="harvest_target_not_farmland",
                can_retry=False,
                metrics=context,
            )
        denied = self._guard_interaction_target(
            farmland_target,
            farmland_type,
            InteractionContext.FARM,
            denied_reason="harvest_denied",
        )
        if denied is not None:
            return merge_context(denied, context)

        crop = self.body.perceive("blockAt", {"x": crop_pos[0], "y": crop_pos[1], "z": crop_pos[2]})
        failed = perception_failure(crop)
        if failed is not None:
            return merge_context(failed, context)
        actual_crop_block = normalize_block_type(str(crop.data.get("type") or "unknown"))
        context["crop_before"] = {
            "type": actual_crop_block,
            "state": str(crop.data.get("state") or "UNKNOWN"),
            "properties": dict(crop.data.get("properties") or {}),
        }

        expected_crop_block = normalize_block_type(crop_block) if crop_block is not None else actual_crop_block
        if actual_crop_block == "air":
            return ToolResult(
                success=False,
                reason="harvest_crop_missing",
                can_retry=True,
                next_suggestion="wait for the crop to grow before retrying harvest",
                metrics={**context, "expected_crop_block": expected_crop_block},
            )
        if crop_block is not None and actual_crop_block != expected_crop_block:
            return ToolResult(
                success=False,
                reason="harvest_crop_wrong_type",
                can_retry=False,
                metrics={**context, "expected_crop_block": expected_crop_block},
            )

        resolved_seed_item = normalize_block_type(seed_item or self.CROP_TO_SEED_ITEM.get(actual_crop_block, ""))
        if not resolved_seed_item:
            return ToolResult(
                success=False,
                reason="unsupported_crop_block",
                can_retry=False,
                metrics={**context, "crop_block": actual_crop_block},
            )

        max_age = self.CROP_MAX_AGE.get(actual_crop_block)
        age = _parse_int((crop.data.get("properties") or {}).get("age"))
        if max_age is not None and age is not None and age < max_age:
            return ToolResult(
                success=False,
                reason="harvest_crop_not_mature",
                can_retry=True,
                next_suggestion="wait until the crop reaches full age before harvesting",
                metrics={
                    **context,
                    "crop_block": actual_crop_block,
                    "seed_item": resolved_seed_item,
                    "age": age,
                    "required_age": max_age,
                },
            )

        expected_drops = self.CROP_TO_HARVEST_DROPS.get(actual_crop_block, (resolved_seed_item,))
        context.update(
            {
                "crop_block": actual_crop_block,
                "seed_item": resolved_seed_item,
                "expected_drops": list(expected_drops),
                "maturity": {"age": age, "required_age": max_age},
            }
        )

        harvested = self.work.mine_block_collect(
            crop_pos,
            context=break_context,
            expected_drops=expected_drops,
            settle_s=settle_s,
            timeout_s=timeout_s,
        )
        if not harvested.success:
            return merge_context(
                ToolResult(
                    success=False,
                    reason=f"harvest_failed:{harvested.reason}",
                    can_retry=harvested.can_retry,
                    next_suggestion=harvested.next_suggestion,
                    metrics={"harvest": harvested.to_payload()},
                ),
                context,
            )

        resolved_stands: list[Position] | None = None
        if self.navigator is not None:
            stands = interaction_stand_points(self.body, farmland_target)
            if isinstance(stands, ToolResult):
                return merge_context(
                    ToolResult(
                        success=False,
                        reason=f"resow_failed:{stands.reason}",
                        can_retry=stands.can_retry,
                        next_suggestion=stands.next_suggestion,
                        metrics={"harvest": harvested.to_payload()},
                    ),
                    context,
                )
            resolved_stands = stands
            state_after_harvest = self.body.get_state()
            current_feet = (floor(state_after_harvest.pos[0]), floor(state_after_harvest.pos[1]), floor(state_after_harvest.pos[2]))
            if current_feet not in stands and stands:
                nav = self.navigator.navigate_to(
                    stands[0],
                    timeout_s=approach_timeout_s,
                    arrival_radius=0.25,
                )
                if not nav.success:
                    return merge_context(
                        ToolResult(
                            success=False,
                            reason=f"resow_failed:use_navigation_failed:{nav.reason}",
                            can_retry=nav.can_retry,
                            next_suggestion=nav.next_suggestion,
                            metrics={"harvest": harvested.to_payload(), "resow_navigation": nav.to_payload()},
                        ),
                        context,
                    )

        resown = self.sow_crop(
            seed_item=resolved_seed_item,
            farmland_pos=farmland_target,
            expected_crop_block=actual_crop_block,
            stand_points=resolved_stands,
            approach_timeout_s=approach_timeout_s,
            look_timeout_s=look_timeout_s,
            use_timeout_s=use_timeout_s,
        )
        if not resown.success:
            return merge_context(
                ToolResult(
                    success=False,
                    reason=f"resow_failed:{resown.reason}",
                    can_retry=resown.can_retry,
                    next_suggestion=resown.next_suggestion,
                    metrics={"harvest": harvested.to_payload(), "resow": resown.to_payload()},
                ),
                context,
            )

        return merge_context(
            ToolResult(
                success=True,
                reason="harvested_and_resown",
                can_retry=False,
                metrics={"harvest": harvested.to_payload(), "resow": resown.to_payload()},
            ),
            context,
        )

    def activate_switch(
        self,
        *,
        pos: Position | None = None,
        search_radius: int = 8,
        block_types: tuple[str, ...] | None = None,
        approach_timeout_s: float = 15.0,
        look_timeout_s: float = 2.0,
        use_timeout_s: float = 8.0,
    ) -> ToolResult:
        """Activate one lever/button and verify `powered=true`."""
        return self.set_switch_state(
            desired_powered=True,
            pos=pos,
            search_radius=search_radius,
            block_types=block_types,
            approach_timeout_s=approach_timeout_s,
            look_timeout_s=look_timeout_s,
            use_timeout_s=use_timeout_s,
        )

    def deactivate_switch(
        self,
        *,
        pos: Position | None = None,
        search_radius: int = 8,
        block_types: tuple[str, ...] | None = None,
        approach_timeout_s: float = 15.0,
        look_timeout_s: float = 2.0,
        use_timeout_s: float = 8.0,
        release_timeout_s: float = 2.0,
        release_poll_s: float = 0.05,
    ) -> ToolResult:
        """Drive a switch to `powered=false` and verify the final truth."""
        return self.set_switch_state(
            desired_powered=False,
            pos=pos,
            search_radius=search_radius,
            block_types=block_types,
            approach_timeout_s=approach_timeout_s,
            look_timeout_s=look_timeout_s,
            use_timeout_s=use_timeout_s,
            release_timeout_s=release_timeout_s,
            release_poll_s=release_poll_s,
        )

    def set_switch_state(
        self,
        *,
        desired_powered: bool,
        pos: Position | None = None,
        search_radius: int = 8,
        block_types: tuple[str, ...] | None = None,
        approach_timeout_s: float = 15.0,
        look_timeout_s: float = 2.0,
        use_timeout_s: float = 8.0,
        release_timeout_s: float = 2.0,
        release_poll_s: float = 0.05,
    ) -> ToolResult:
        """Set one switchable target to `powered=true/false` with honest truth."""
        allowed_types = tuple(block_types or self.SWITCHABLE_BLOCK_TYPES)
        context: dict[str, object] = {
            "search_radius": search_radius,
            "block_types": list(allowed_types),
            "desired_powered": desired_powered,
        }
        target_pos, target_type, target_context_or_error = self._resolve_block_target(
            pos=pos,
            allowed_block_types=allowed_types,
            search_radius=search_radius,
            not_found_reason="switch_not_found",
            wrong_type_reason="switch_wrong_type",
        )
        if target_pos is None:
            return merge_context(target_context_or_error, context)  # type: ignore[arg-type]
        context["target"] = target_context_or_error
        denied = self._guard_interaction_target(
            target_pos,
            target_type,
            InteractionContext.ACTIVATE,
            denied_reason="switch_denied",
        )
        if denied is not None:
            return merge_context(denied, context)

        target_props = dict(target_context_or_error.get("properties") or {})
        current_powered = str(target_props.get("powered") or "false").lower() == "true"
        if current_powered == desired_powered:
            return ToolResult(
                success=True,
                reason="already_powered" if desired_powered else "already_unpowered",
                can_retry=False,
                metrics=context,
            )

        if not desired_powered and _is_button_type(target_type):
            released = self._wait_for_switch_state(
                target_pos,
                target_type,
                desired_powered=False,
                timeout_s=release_timeout_s,
                poll_s=release_poll_s,
            )
            released = merge_context(released, context)
            if not released.success:
                return released
            return ToolResult(
                success=True,
                reason="released",
                can_retry=False,
                metrics=released.metrics,
            )

        result = self.use.use_on_block(
            pos=target_pos,
            item=None,
            expected_block_types=(target_type,),
            expected_properties={"powered": "true" if desired_powered else "false"},
            look_target=_switch_look_target(target_pos, target_type, target_props),
            look_timeout_s=look_timeout_s,
            approach_timeout_s=approach_timeout_s,
            navigation_arrival_radius=0.25,
            timeout_s=use_timeout_s,
        )
        result = merge_context(result, context)
        if not result.success:
            return result
        if result.reason == "already_in_expected_state":
            return ToolResult(
                success=True,
                reason="already_powered" if desired_powered else "already_unpowered",
                can_retry=False,
                metrics=result.metrics,
            )
        return ToolResult(
            success=True,
            reason="powered" if desired_powered else "unpowered",
            can_retry=False,
            metrics=result.metrics,
        )

    def _resolve_block_target(
        self,
        *,
        pos: Position | None,
        allowed_block_types: tuple[str, ...],
        search_radius: int,
        not_found_reason: str,
        wrong_type_reason: str,
    ) -> tuple[Position | None, str | None, dict[str, object] | ToolResult]:
        allowed = {normalize_block_type(block_type) for block_type in allowed_block_types}
        if pos is None:
            target = find_block_target(
                self.body,
                block_types=allowed_block_types,
                radius=search_radius,
                limit=32,
                not_found_reason=not_found_reason,
            )
            if isinstance(target, ToolResult):
                return None, None, target
            return (
                target.pos,
                target.block_type,
                {"pos": list(target.pos), "type": target.block_type, "distance": target.distance},
            )

        target_pos = (int(pos[0]), int(pos[1]), int(pos[2]))
        target_before = self.body.perceive("blockAt", {"x": target_pos[0], "y": target_pos[1], "z": target_pos[2]})
        failed = perception_failure(target_before)
        if failed is not None:
            return None, None, merge_context(failed, {"target": {"pos": list(target_pos)}})
        target_type = normalize_block_type(str(target_before.data.get("type") or "unknown"))
        target_context = {
            "pos": list(target_pos),
            "type": target_type,
            "state": str(target_before.data.get("state") or "UNKNOWN"),
            "properties": dict(target_before.data.get("properties") or {}),
        }
        if target_type not in allowed:
            return None, None, ToolResult(success=False, reason=wrong_type_reason, can_retry=False, metrics={"target": target_context})
        return target_pos, target_type, target_context

    def _enter_player_distance_band(
        self,
        *,
        player_name: str,
        search_radius: int,
        min_distance: float,
        max_distance: float,
        vertical_tolerance: float,
        timeout_s: float,
        maintenance_checks: int,
        maintenance_interval_s: float,
        not_found_reason: str,
        missing_reason: str,
        failure_prefix: str,
        no_stand_reason: str,
        lost_reason: str,
        out_of_band_reason: str,
        success_reason: str,
        retry_suggestion: str,
    ) -> ToolResult:
        if not player_name:
            return ToolResult(
                success=False,
                reason="invalid_player_name",
                can_retry=False,
                metrics={"player_name": player_name},
            )
        if min_distance < 0 or max_distance <= 0 or min_distance > max_distance:
            return ToolResult(
                success=False,
                reason="invalid_distance_band",
                can_retry=False,
                metrics={
                    "player_name": player_name,
                    "min_distance": min_distance,
                    "max_distance": max_distance,
                    "vertical_tolerance": vertical_tolerance,
                },
            )
        if maintenance_checks < 1:
            return ToolResult(
                success=False,
                reason="invalid_maintenance_checks",
                can_retry=False,
                metrics={"player_name": player_name, "maintenance_checks": maintenance_checks},
            )
        if maintenance_interval_s < 0:
            return ToolResult(
                success=False,
                reason="invalid_maintenance_interval",
                can_retry=False,
                metrics={"player_name": player_name, "maintenance_interval_s": maintenance_interval_s},
            )

        context = {
            "player_name": player_name,
            "search_radius": search_radius,
            "distance_band": {
                "min_distance": min_distance,
                "max_distance": max_distance,
                "vertical_tolerance": vertical_tolerance,
            },
            "maintenance": {
                "requested_checks": maintenance_checks,
                "interval_s": maintenance_interval_s,
            },
        }
        attempts: list[dict[str, object]] = []
        last_metrics: dict[str, object] = dict(context)
        not_found = not_found_reason
        for check_index in range(maintenance_checks):
            target = find_named_entity_target(
                self.body,
                player_name,
                radius=search_radius,
                not_found_reason=not_found,
            )
            if isinstance(target, ToolResult):
                return merge_context(
                    target,
                    {
                        **context,
                        "maintenance": {
                            **context["maintenance"],  # type: ignore[index]
                            "completed_checks": check_index,
                            "attempts": attempts,
                        },
                    },
                )

            approach = ensure_entity_range(
                self.body,
                self.navigator,
                target.pos,
                min_distance=min_distance,
                max_distance=max_distance,
                vertical_tolerance=vertical_tolerance,
                timeout_s=timeout_s,
                missing_reason=missing_reason,
                failure_prefix=failure_prefix,
                no_stand_reason=no_stand_reason,
            )
            if isinstance(approach, ToolResult):
                return merge_context(approach, {**context, "target": _target_metrics(target), "maintenance_attempts": attempts})

            refreshed = find_named_entity_target(
                self.body,
                player_name,
                radius=search_radius,
                not_found_reason=lost_reason,
            )
            if isinstance(refreshed, ToolResult):
                lost_attempt = {
                    "check": check_index + 1,
                    "target_before": _target_metrics(target),
                    "approach": approach,
                    "lost_after_navigation": True,
                }
                return merge_context(
                    refreshed,
                    {
                        **context,
                        "target": _target_metrics(target),
                        "approach": approach,
                        "maintenance_attempts": [*attempts, lost_attempt],
                    },
                )

            state = self.body.get_state()
            final_distance = _distance_to_entity(state.pos, refreshed.pos)
            final_vertical = abs(state.pos[1] - refreshed.pos[1])
            attempt = {
                "check": check_index + 1,
                "target_before": _target_metrics(target),
                "target_after": _target_metrics(refreshed),
                "approach": approach,
                "final_distance": final_distance,
                "final_vertical_delta": final_vertical,
                "body_pos": list(state.pos),
            }
            attempts.append(attempt)
            last_metrics = {
                **context,
                "target": _target_metrics(refreshed),
                "approach": approach,
                "maintenance_attempts": attempts,
                "final_distance": final_distance,
                "final_vertical_delta": final_vertical,
                "body_pos": list(state.pos),
            }
            if final_distance < min_distance or final_distance > max_distance or final_vertical > vertical_tolerance:
                not_found = lost_reason
                continue
            if check_index + 1 < maintenance_checks and maintenance_interval_s > 0:
                time.sleep(maintenance_interval_s)
            not_found = lost_reason

        if (
            float(last_metrics.get("final_distance", max_distance + 1)) < min_distance
            or float(last_metrics.get("final_distance", max_distance + 1)) > max_distance
            or float(last_metrics.get("final_vertical_delta", vertical_tolerance + 1)) > vertical_tolerance
        ):
            return ToolResult(
                success=False,
                reason=out_of_band_reason,
                can_retry=True,
                next_suggestion=retry_suggestion,
                metrics=last_metrics,
            )

        return ToolResult(success=True, reason=success_reason, can_retry=False, metrics=last_metrics)

    def _guard_interaction_target(
        self,
        pos: Position,
        block_type: str,
        context: InteractionContext,
        *,
        denied_reason: str,
    ) -> ToolResult | None:
        if self.governance is None:
            return None
        decision = self.governance.can_interact(pos, block_type, context)
        if decision.allowed:
            return None
        return ToolResult(
            success=False,
            reason=denied_reason,
            can_retry=False,
            next_suggestion="choose a target inside an allowed natural work region instead of interacting with a protected or unknown-provenance block",
            metrics={
                "target": list(pos),
                "block_type": block_type,
                "legality": _decision_payload(decision),
            },
        )

    def _wait_for_switch_state(
        self,
        pos: Position,
        block_type: str,
        *,
        desired_powered: bool,
        timeout_s: float,
        poll_s: float,
    ) -> ToolResult:
        deadline = time.monotonic() + timeout_s
        observed: list[dict[str, object]] = []
        desired = "true" if desired_powered else "false"
        while time.monotonic() < deadline:
            block = self.body.perceive("blockAt", {"x": pos[0], "y": pos[1], "z": pos[2]})
            failed = perception_failure(block)
            if failed is not None:
                return failed
            block_type_now = normalize_block_type(str(block.data.get("type") or "unknown"))
            props = dict(block.data.get("properties") or {})
            powered = str(props.get("powered") or "false").lower()
            observed.append(
                {
                    "type": block_type_now,
                    "state": str(block.data.get("state") or "UNKNOWN"),
                    "properties": props,
                }
            )
            if block_type_now == block_type and powered == desired:
                return ToolResult(
                    success=True,
                    reason="completed",
                    can_retry=False,
                    metrics={
                        "target": list(pos),
                        "block_type": block_type,
                        "target_after": {
                            "type": block_type_now,
                            "state": str(block.data.get("state") or "UNKNOWN"),
                            "properties": props,
                        },
                        "waited_for_release": True,
                        "poll_samples": observed,
                    },
                )
            time.sleep(max(0.0, poll_s))
        return ToolResult(
            success=False,
            reason="switch_release_timeout",
            can_retry=True,
            next_suggestion="wait longer for the button to release or re-check whether the target is really a momentary button",
            metrics={
                "target": list(pos),
                "block_type": block_type,
                "desired_powered": desired_powered,
                "waited_for_release": True,
                "poll_samples": observed,
            },
        )

    def _look_at(self, target: NearbyEntityTarget, *, timeout_s: float) -> ToolResult:
        result = self._look_at_pos(target.pos, timeout_s=timeout_s)
        return merge_context(
            result,
            {
                "receiver_name": target.name,
                "receiver_type": target.entity_type,
            },
        )

    def _look_at_pos(self, target: tuple[float, float, float], *, timeout_s: float) -> ToolResult:
        action = Action.create("lookAt", {"target": list(target)})
        accepted = self.body.execute(action)
        rejected = _acceptance_failure(accepted)
        if rejected is not None:
            return rejected
        terminal = self.body.await_action_terminal(action.id, timeout_s=timeout_s)
        result = terminal_event_to_tool_result(terminal)
        if result.success:
            return ToolResult(
                success=True,
                reason=result.reason,
                can_retry=False,
                metrics={
                    "action_id": action.id,
                    "target": list(target),
                    **dict(result.metrics or {}),
                },
            )
        return ToolResult(
            success=False,
            reason=f"look_failed:{result.reason}",
            can_retry=result.can_retry,
            next_suggestion=result.next_suggestion,
            metrics={
                "action_id": action.id,
                "target": list(target),
                **dict(result.metrics or {}),
            },
        )

    def _use_empty_hand(self, *, timeout_s: float) -> ToolResult:
        action = Action.create("useItem", {"mode": "once", "ticks": 1})
        accepted = self.body.execute(action)
        rejected = _acceptance_failure(accepted)
        if rejected is not None:
            return rejected
        terminal = self.body.await_action_terminal(action.id, timeout_s=timeout_s)
        return terminal_event_to_tool_result(terminal)

    def _handoff_item(
        self,
        *,
        receiver_name: str,
        item: str,
        count: int,
        timeout_s: float,
    ) -> ToolResult:
        action = Action.create("handoffItem", {"receiver": receiver_name, "item": item, "count": count})
        accepted = self.body.execute(action)
        rejected = _acceptance_failure(accepted)
        if rejected is not None:
            return rejected
        terminal = self.body.await_action_terminal(action.id, timeout_s=timeout_s)
        return terminal_event_to_tool_result(terminal)

    def _await_pickup(self, receiver_name: str, item: str, *, timeout_s: float) -> HandoffReceipt | None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            for event in self.body.poll_events():
                receipt = _pickup_receipt(event, receiver_name, item)
                if receipt is not None:
                    return receipt
            time.sleep(0.10)
        return None


def _target_metrics(target: NearbyEntityTarget) -> dict[str, object]:
    return {
        "id": target.entity_id,
        "name": target.name,
        "type": target.entity_type,
        "pos": list(target.pos),
        "health": target.health,
        "distance": target.distance,
    }


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


def _pickup_receipt(event: Event, receiver_name: str, item: str) -> HandoffReceipt | None:
    if event.name != "itemPickup":
        return None
    if event.data.get("player") != receiver_name:
        return None
    found_item = event.data.get("item")
    if not _same_item(found_item, item):
        return None
    return HandoffReceipt(
        player=receiver_name,
        item=str(found_item) if found_item is not None else None,
        count=int(event.data.get("count") or 0),
        seq=event.seq,
        tick=event.tick,
    )


def _same_item(actual: object, wanted: str) -> bool:
    if actual is None:
        return False
    actual_s = str(actual)
    return actual_s == wanted or actual_s == f"minecraft:{wanted}" or f"minecraft:{actual_s}" == wanted


def _decision_payload(decision) -> dict[str, object]:
    payload = asdict(decision)
    payload["allowed"] = bool(decision.allowed)
    return payload


def _distance_to_entity(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    dx = a[0] - b[0]
    dy = a[1] - b[1]
    dz = a[2] - b[2]
    return (dx * dx + dy * dy + dz * dz) ** 0.5


def _parse_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _block_center_target(pos: tuple[int, int, int]) -> tuple[float, float, float]:
    return (pos[0] + 0.5, pos[1] + 0.5, pos[2] + 0.5)


def _openable_look_target(pos: Position, block_type: str, properties: dict[str, object] | None = None) -> tuple[float, float, float]:
    normalized_type = normalize_block_type(block_type)
    props = {str(key): str(value).lower() for key, value in dict(properties or {}).items()}
    if normalized_type.endswith("_fence_gate"):
        if props.get("open") == "true":
            facing = props.get("facing")
            if facing in {"east", "west"}:
                return (pos[0] + 0.5, pos[1] + 0.2, pos[2] + 0.9)
            if facing in {"north", "south"}:
                return (pos[0] + 0.9, pos[1] + 0.2, pos[2] + 0.5)
        return (pos[0] + 0.5, pos[1] + 0.2, pos[2] + 0.5)
    if normalized_type.endswith("_trapdoor"):
        half = props.get("half")
        y_offset = 0.8 if half == "top" else 0.2
        return (pos[0] + 0.5, pos[1] + y_offset, pos[2] + 0.5)
    if normalized_type.endswith("_door"):
        if props.get("open") == "true":
            facing = props.get("facing")
            hinge = props.get("hinge")
            if facing == "east":
                return (pos[0] + 0.5, pos[1] + 0.5, pos[2] + (0.1 if hinge == "left" else 0.9))
            if facing == "west":
                return (pos[0] + 0.5, pos[1] + 0.5, pos[2] + (0.9 if hinge == "left" else 0.1))
            if facing == "north":
                return (pos[0] + (0.9 if hinge == "left" else 0.1), pos[1] + 0.5, pos[2] + 0.5)
            if facing == "south":
                return (pos[0] + (0.1 if hinge == "left" else 0.9), pos[1] + 0.5, pos[2] + 0.5)
        return (pos[0] + 0.5, pos[1] + 0.5, pos[2] + 0.5)
    return _block_center_target(pos)


def _openable_stand_points(
    pos: Position,
    block_type: str,
    properties: dict[str, object] | None = None,
) -> list[Position]:
    normalized_type = normalize_block_type(block_type)
    props = {str(key): str(value).lower() for key, value in dict(properties or {}).items()}
    x, y, z = int(pos[0]), int(pos[1]), int(pos[2])
    ordered: list[Position] = []

    def add(offsets: list[tuple[int, int, int]]) -> None:
        for dx, dy, dz in offsets:
            stand = (x + dx, y + dy, z + dz)
            if stand not in ordered:
                ordered.append(stand)

    if normalized_type.endswith("_fence_gate"):
        facing = props.get("facing")
        if facing in {"east", "west"}:
            add([(0, 0, 1), (0, 0, -1), (-1, 0, 0), (1, 0, 0)])
        elif facing in {"north", "south"}:
            add([(1, 0, 0), (-1, 0, 0), (0, 0, -1), (0, 0, 1)])
    elif normalized_type.endswith("_door"):
        facing = props.get("facing")
        is_open = props.get("open") == "true"
        if facing == "east":
            add([(1, 0, 0), (0, 0, -1), (0, 0, 1), (-1, 0, 0)] if is_open else [(-1, 0, 0), (0, 0, -1), (0, 0, 1), (1, 0, 0)])
        elif facing == "west":
            add([(-1, 0, 0), (0, 0, 1), (0, 0, -1), (1, 0, 0)] if is_open else [(1, 0, 0), (0, 0, 1), (0, 0, -1), (-1, 0, 0)])
        elif facing == "north":
            add([(0, 0, -1), (1, 0, 0), (-1, 0, 0), (0, 0, 1)] if is_open else [(0, 0, 1), (1, 0, 0), (-1, 0, 0), (0, 0, -1)])
        elif facing == "south":
            add([(0, 0, 1), (-1, 0, 0), (1, 0, 0), (0, 0, -1)] if is_open else [(0, 0, -1), (-1, 0, 0), (1, 0, 0), (0, 0, 1)])

    add([(-1, 0, 0), (1, 0, 0), (0, 0, 1), (0, 0, -1)])
    return ordered


def _approach_openable_target(
    body: Body,
    navigator: InteractionNavigator | None,
    pos: Position,
    block_type: str,
    properties: dict[str, object] | None,
    *,
    timeout_s: float,
) -> dict[str, object] | ToolResult:
    state = body.get_state()
    initial_distance = dist(state.pos, (pos[0] + 0.5, pos[1] + 0.5, pos[2] + 0.5))
    if initial_distance <= INTERACTION_RANGE and navigator is None:
        return {"navigated": False, "initial_distance": initial_distance, "final_distance": initial_distance}
    if navigator is None:
        return ToolResult(
            success=False,
            reason="use_navigation_missing",
            can_retry=True,
            next_suggestion="provide a navigation transaction before attempting distant block interaction",
            metrics={"target": list(pos), "initial_distance": initial_distance},
        )

    valid_stands = interaction_stand_points(body, pos)
    if isinstance(valid_stands, ToolResult):
        return valid_stands
    if not valid_stands:
        return ToolResult(
            success=False,
            reason="use_no_stand_point",
            can_retry=False,
            next_suggestion="clear a standable adjacent block before retrying the interaction",
            metrics={"target": list(pos), "initial_distance": initial_distance},
        )

    preferred = _openable_stand_points(pos, block_type, properties)
    ordered: list[Position] = [stand for stand in preferred if stand in valid_stands]
    ordered.extend(stand for stand in valid_stands if stand not in ordered)
    attempts: list[dict[str, object]] = []
    last_failure: ToolResult | None = None

    for stand in ordered:
        nav_result = navigator.navigate_to(stand, timeout_s=timeout_s, arrival_radius=0.25)
        attempt: dict[str, object] = {"goal": list(stand), "result": nav_result.to_payload()}
        if not nav_result.success:
            attempts.append(attempt)
            last_failure = nav_result
            continue
        center_result = _move_to_bed_use_stance(body, stand, arrival_radius=0.25, timeout_s=timeout_s)
        attempt["center_result"] = center_result.to_payload()
        attempts.append(attempt)
        if not center_result.success:
            last_failure = center_result
            continue
        final_state = body.get_state()
        final_distance = dist(final_state.pos, (pos[0] + 0.5, pos[1] + 0.5, pos[2] + 0.5))
        if final_distance <= INTERACTION_RANGE:
            return {
                "navigated": True,
                "stand_target": list(stand),
                "initial_distance": initial_distance,
                "final_distance": final_distance,
                "attempts": attempts,
            }
        last_failure = ToolResult(
            success=False,
            reason="target_out_of_range_after_navigation",
            can_retry=True,
            metrics={
                "target": list(pos),
                "stand_target": list(stand),
                "initial_distance": initial_distance,
                "final_distance": final_distance,
            },
        )

    if last_failure is None:
        last_failure = ToolResult(success=False, reason="use_navigation_failed", can_retry=True, metrics={"target": list(pos)})
    return ToolResult(
        success=False,
        reason=f"use_navigation_failed:{last_failure.reason}",
        can_retry=last_failure.can_retry,
        next_suggestion=last_failure.next_suggestion,
        metrics={**dict(last_failure.metrics or {}), "target": list(pos), "attempts": attempts},
    )


def _approach_bed_target(
    body: Body,
    navigator: InteractionNavigator | None,
    pos: Position,
    *,
    timeout_s: float,
) -> dict[str, object] | ToolResult:
    state = body.get_state()
    initial_distance = dist(state.pos, (pos[0] + 0.5, pos[1] + 0.5, pos[2] + 0.5))
    if initial_distance <= INTERACTION_RANGE and navigator is None:
        return {"navigated": False, "initial_distance": initial_distance, "final_distance": initial_distance}
    if navigator is None:
        return ToolResult(
            success=False,
            reason="bed_navigation_missing",
            can_retry=True,
            next_suggestion="provide a navigation transaction before attempting distant bed interaction",
            metrics={"target": list(pos), "initial_distance": initial_distance},
        )

    valid_stands = interaction_stand_points(body, pos)
    if isinstance(valid_stands, ToolResult):
        return valid_stands
    if not valid_stands:
        return ToolResult(
            success=False,
            reason="bed_no_stand_point",
            can_retry=False,
            next_suggestion="clear a standable adjacent block before retrying bed interaction",
            metrics={"target": list(pos), "initial_distance": initial_distance},
        )

    preferred_stands = _bed_preferred_stands(pos)
    ordered_stands = [stand for stand in preferred_stands if stand in valid_stands]
    ordered_stands.extend(stand for stand in valid_stands if stand not in ordered_stands)

    attempts: list[dict[str, object]] = []
    last_failure: ToolResult | None = None
    for stand in ordered_stands:
        nav_result = navigator.navigate_to(stand, timeout_s=timeout_s, arrival_radius=0.25)
        attempt: dict[str, object] = {"goal": list(stand), "result": nav_result.to_payload()}
        if not nav_result.success:
            attempts.append(attempt)
            last_failure = nav_result
            continue
        center_result = _move_to_bed_use_stance(body, stand, arrival_radius=0.25, timeout_s=timeout_s)
        attempt["center_result"] = center_result.to_payload()
        attempts.append(attempt)
        if not center_result.success:
            last_failure = center_result
            continue
        final_state = body.get_state()
        final_distance = dist(final_state.pos, (pos[0] + 0.5, pos[1] + 0.5, pos[2] + 0.5))
        return {
            "navigated": True,
            "stand_target": list(stand),
            "initial_distance": initial_distance,
            "final_distance": final_distance,
            "attempts": attempts,
        }

    if last_failure is None:
        last_failure = ToolResult(success=False, reason="bed_navigation_failed", can_retry=True, metrics={"target": list(pos)})
    return ToolResult(
        success=False,
        reason=f"bed_navigation_failed:{last_failure.reason}",
        can_retry=last_failure.can_retry,
        next_suggestion=last_failure.next_suggestion,
        metrics={**dict(last_failure.metrics or {}), "target": list(pos), "attempts": attempts},
    )


def _switch_look_target(pos: Position, block_type: str, properties: dict[str, object] | None = None) -> tuple[float, float, float]:
    props = {str(key): str(value).lower() for key, value in dict(properties or {}).items()}
    face = props.get("face")
    facing = props.get("facing")
    if face == "floor":
        return (pos[0] + 0.5, pos[1] + 0.15, pos[2] + 0.5)
    if face == "ceiling":
        return (pos[0] + 0.5, pos[1] + 0.85, pos[2] + 0.5)
    if face == "wall":
        if facing == "east":
            return (pos[0] + 0.9, pos[1] + 0.5, pos[2] + 0.5)
        if facing == "west":
            return (pos[0] + 0.1, pos[1] + 0.5, pos[2] + 0.5)
        if facing == "south":
            return (pos[0] + 0.5, pos[1] + 0.5, pos[2] + 0.9)
        if facing == "north":
            return (pos[0] + 0.5, pos[1] + 0.5, pos[2] + 0.1)
    return _block_center_target(pos)


def _is_button_type(block_type: str) -> bool:
    return normalize_block_type(block_type).endswith("_button")


def _bed_interaction_target(pos: Position, properties: dict[str, object] | None = None) -> Position:
    props = {str(key): str(value).lower() for key, value in dict(properties or {}).items()}
    if props.get("part") == "head":
        return pos
    facing = props.get("facing")
    if facing == "east":
        return (pos[0] + 1, pos[1], pos[2])
    if facing == "west":
        return (pos[0] - 1, pos[1], pos[2])
    if facing == "south":
        return (pos[0], pos[1], pos[2] + 1)
    if facing == "north":
        return (pos[0], pos[1], pos[2] - 1)
    return pos


def _bed_preferred_stands(pos: Position) -> list[Position]:
    # Keep the west-side stand first for the current best-effort physical probe
    # order. Real bed sleep is still mechanism-deferred on the live server.
    return [
        (pos[0] - 1, pos[1], pos[2]),
        (pos[0], pos[1], pos[2] - 1),
        (pos[0], pos[1], pos[2] + 1),
        (pos[0] + 1, pos[1], pos[2]),
    ]


def _move_to_bed_use_stance(
    body: Body,
    stand: Position,
    *,
    arrival_radius: float,
    timeout_s: float,
) -> ToolResult:
    # Current best-effort stance bias, retained for honest probing even though
    # real bed sleep is still mechanism-deferred on the live server.
    target = (stand[0] + 0.6, stand[1], stand[2] + 0.3)
    precise_radius = min(arrival_radius, 0.1)
    action = Action.create(
        "moveTo",
        {
            "target": list(target),
            "waypoints": [list(target)],
            "arrival_radius": precise_radius,
            "timeout_ticks": 80,
            "no_progress_ticks": 25,
            "max_deviation": 1.5,
        },
    )
    accepted = body.execute(action)
    if not (accepted.ok and accepted.accepted):
        return ToolResult(
            success=False,
            reason="body_rejected",
            can_retry=True,
            metrics={
                "action_id": action.id,
                "stand": list(stand),
                "center": list(target),
                "center_radius": precise_radius,
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
    metrics = {
        "action_id": action.id,
        "stand": list(stand),
        "center": list(target),
        "center_radius": precise_radius,
        **dict(result.metrics or {}),
    }
    if not result.success:
        return ToolResult(
            success=False,
            reason=f"center_failed:{result.reason}",
            can_retry=result.can_retry,
            next_suggestion=result.next_suggestion,
            metrics=metrics,
        )
    return ToolResult(success=True, reason=result.reason, can_retry=False, metrics=metrics)


def _is_bedtime(world_time: int) -> bool:
    time_of_day = int(world_time) % 24000
    return (
        InteractionTransactions.BED_SLEEP_START_TICK
        <= time_of_day
        < InteractionTransactions.BED_SLEEP_END_TICK
    )
