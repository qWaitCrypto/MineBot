"""Body transaction block work wrappers with governance guards."""

from __future__ import annotations

from dataclasses import asdict, replace
from math import ceil, dist, floor
import time
from typing import Callable, Protocol

from minebot.body.interaction_support import (
    find_nearby_block_search,
    find_block_target,
    interaction_stand_points,
    merge_context,
    move_to_block_center,
)
from minebot.body.inventory_read import (
    inventory_counts as _inventory_counts,
    read_inventory_counts as _inventory_counts_from_body,
    read_inventory_slots as _read_inventory_slots,
)
from minebot.body.pickup import PickupConfig, PickupTransactions
from minebot.body.world_read import read_block_facts, read_surface_columns
from minebot.contract import (
    Action,
    Body,
    BreakContext,
    InventorySlot,
    PerceptionResult,
    PlaceContext,
    Position,
    Result,
    ToolResult,
    PICKAXE_BY_TIER,
    best_owned_pickaxe,
    required_pickaxe_tier,
    terminal_event_to_tool_result,
    tier_satisfies,
)
from minebot.game.governance import GovernancePolicy
from minebot.game.navigation import GoalComposite, GoalLike, GoalNear


class SealFaceNavigator(Protocol):
    def navigate_to(self, goal: GoalLike, **kwargs) -> ToolResult: ...


class BlockWork:
    """Guarded block work entrypoints used by Body transaction callers.

    This is the first runtime weld between governance and Body actions.  It does
    not implement physical mining/placing itself; it refuses illegal work before
    any Body mutation is sent and turns Body terminal events into ToolResult.
    """

    LIQUID_TYPES = frozenset({"water", "lava"})
    LIQUID_STATES = frozenset({"LIQUID"})
    DEFAULT_SEAL_BLOCKS = (
        "cobblestone",
        "cobbled_deepslate",
        "deepslate",
        "stone",
        "dirt",
        "netherrack",
    )
    DEFAULT_PILLAR_BLOCKS = DEFAULT_SEAL_BLOCKS
    DIRECT_MINE_REACH = 2.25
    MINE_INTERACTION_RANGE = 4.5
    MAX_SEAL_FACES = 2
    LIQUID_CONTACT_OFFSETS = (
        (0, 0, 0),
        (0, 1, 0),
        (0, -1, 0),
        (1, 0, 0),
        (-1, 0, 0),
        (0, 0, 1),
        (0, 0, -1),
    )
    SEAL_FACE_OFFSETS = (
        (1, 0, 0),
        (-1, 0, 0),
        (0, 1, 0),
        (0, -1, 0),
        (0, 0, 1),
        (0, 0, -1),
    )
    DEFAULT_DROP_MAP = {
        "coal_ore": ("coal",),
        "deepslate_coal_ore": ("coal",),
        "copper_ore": ("raw_copper",),
        "deepslate_copper_ore": ("raw_copper",),
        "diamond_ore": ("diamond",),
        "deepslate_diamond_ore": ("diamond",),
        "emerald_ore": ("emerald",),
        "deepslate_emerald_ore": ("emerald",),
        "iron_ore": ("raw_iron",),
        "deepslate_iron_ore": ("raw_iron",),
        "gold_ore": ("raw_gold",),
        "deepslate_gold_ore": ("raw_gold",),
        "lapis_ore": ("lapis_lazuli",),
        "deepslate_lapis_ore": ("lapis_lazuli",),
        "redstone_ore": ("redstone",),
        "deepslate_redstone_ore": ("redstone",),
        "nether_quartz_ore": ("quartz",),
        "nether_gold_ore": ("gold_nugget",),
        "grass_block": ("dirt",),
        "coarse_dirt": ("dirt",),
        "rooted_dirt": ("dirt",),
    }
    MINE_APPROACH_MAX_BREAK_STEPS = 8
    SURFACE_GOAL_LIMIT = 32
    SURFACE_EGRESS_GOAL_LIMIT = 32
    SURFACE_EGRESS_RADIUS = 12
    SURFACE_EGRESS_Y_BELOW = 2
    SURFACE_EGRESS_Y_ABOVE = 10
    SURFACE_LATERAL_RING_SPECS = ((16, 8), (24, 16), (32, 64))
    SURFACE_LATERAL_GOAL_LIMIT = 16
    PLACE_HERE_VERTICAL_RADIUS = 4
    PLACE_HERE_COLUMN_LIMIT = 32
    PLACE_HERE_CANDIDATE_LIMIT = 32
    SHAFT_ALIGNMENT_RADIUS = 0.1
    SHAFT_ALIGNMENT_TIMEOUT_S = 5.0

    def __init__(
        self,
        body: Body,
        governance: GovernancePolicy,
        *,
        navigator: SealFaceNavigator | None = None,
        pickup: PickupTransactions | None = None,
        settle: Callable[[float], None] | None = None,
        mine_approach_settle_s: float = 0.3,
    ):
        self.body = body
        self.governance = governance
        self.navigator = navigator
        self.pickup = pickup or PickupTransactions(body, navigator, settle=settle)
        self._settle = settle
        self._mine_approach_settle_s = mine_approach_settle_s

    def _pause(self, seconds: float) -> None:
        if seconds <= 0:
            return
        if self._settle is not None:
            self._settle(seconds)
            return
        time.sleep(seconds)

    def mine_block(
        self,
        pos: Position,
        *,
        context: BreakContext | str = BreakContext.DIRECT,
        timeout_s: float = 30.0,
        approach: bool = True,
    ) -> ToolResult:
        block = self.body.perceive("blockAt", {"x": pos[0], "y": pos[1], "z": pos[2]})
        failed = _perception_failure(block)
        if failed is not None:
            return failed

        block_type = str(block.data.get("type") or "unknown")
        decision = self.governance.can_break(pos, block_type, context)
        if not decision.allowed:
            return _denied_result("break_denied", pos, block_type, decision)

        approach_metrics = None
        if approach:
            approach_failed, approach_metrics = self._approach_mining_target(
                pos,
                context=context,
                target_block_type=block_type,
                timeout_s=timeout_s,
            )
            if approach_failed is not None:
                return approach_failed
        else:
            state = self.body.get_state()
            reach = _mining_reach_distance(state.pos, pos)
            if reach > self._mine_reach_limit(context) and not _same_column(state.pos, pos):
                return ToolResult(
                    success=False,
                    reason="mine_approach_out_of_range",
                    can_retry=True,
                    next_suggestion="replan the resource stand domain from fresh body and world facts",
                    metrics={
                        "target": list(pos),
                        "block_type": block_type,
                        "reach_distance": reach,
                        "prepositioned": True,
                    },
                )

        action = Action.create(
            "mineBlock",
            {
                "target": list(pos),
                "block_type": block_type,
                "context": BreakContext(context).value,
                "timeout_ticks": _seconds_to_ticks(timeout_s),
                "legality": _decision_payload(decision),
            },
        )
        accepted = self.body.execute(action)
        rejected = _acceptance_failure(accepted, "mineBlock", pos)
        if rejected is not None:
            return rejected

        terminal = self.body.await_action_terminal(action.id, timeout_s=_server_terminal_timeout(timeout_s))
        result = terminal_event_to_tool_result(terminal)
        metrics = dict(result.metrics or {})
        metrics.setdefault("target", list(pos))
        metrics.setdefault("block_type", block_type)
        metrics["legality"] = _decision_payload(decision)
        if approach_metrics is not None:
            metrics["mine_approach"] = approach_metrics
        return ToolResult(
            success=result.success,
            reason=result.reason,
            can_retry=result.can_retry,
            next_suggestion=result.next_suggestion,
            metrics=metrics,
        )

    def _approach_mining_target(
        self,
        pos: Position,
        *,
        context: BreakContext | str,
        target_block_type: str,
        timeout_s: float,
    ) -> tuple[ToolResult | None, dict[str, object] | None]:
        state = self.body.get_state()
        reach_limit = self._mine_reach_limit(context)
        if _mining_reach_distance(state.pos, pos) <= reach_limit or _same_column(state.pos, pos):
            return None, None

        if self.navigator is None:
            return ToolResult(
                success=False,
                reason="mine_approach_navigation_missing",
                can_retry=True,
                next_suggestion="attach the shared navigation process before mining a distant target",
                metrics={"target": list(pos)},
            ), None

        stand_candidates = _ranked_mining_stand_candidates(self.body, pos, state.pos)
        if isinstance(stand_candidates, ToolResult):
            return stand_candidates, None
        if not stand_candidates:
            return ToolResult(
                success=False,
                reason="mine_approach_failed:no_stand_candidate",
                can_retry=True,
                next_suggestion="choose another nearby target; no mining stand candidate was available",
                metrics={"target": list(pos)},
            ), None
        from minebot.body.navigation import NavigationRunConfig  # local import: navigation.py imports BlockWork at module top

        navigation_config = NavigationRunConfig(
            max_break_steps=self.MINE_APPROACH_MAX_BREAK_STEPS,
        )
        if timeout_s is not None:
            navigation_config = replace(navigation_config, segment_timeout_s=timeout_s)

        goal = GoalComposite(tuple(GoalNear(candidate, radius=0) for candidate in stand_candidates))
        nav = self.navigator.navigate_to(
            goal,
            break_context=BreakContext.COLLECT_APPROACH,
            config=navigation_config,
        )
        selected_stand = _selected_mining_stand(nav, stand_candidates)
        navigation_metrics = {
            "target": list(pos),
            "stand_candidates": [list(candidate) for candidate in stand_candidates],
            "selected_goal": list(selected_stand),
            "navigation_goal": goal.payload(),
            "state_before": list(_state_block_pos(state.pos)),
            "navigation_result": nav.to_payload(),
            "break_context": BreakContext.COLLECT_APPROACH.value,
            "max_break_steps": self.MINE_APPROACH_MAX_BREAK_STEPS,
            "target_block_type": target_block_type,
        }
        if not nav.success:
            return ToolResult(
                success=False,
                reason=f"mine_approach_failed:{nav.reason}",
                can_retry=nav.can_retry,
                next_suggestion=nav.next_suggestion or "choose another target if this stand domain is exhausted",
                metrics=navigation_metrics,
            ), None

        self._pause(self._mine_approach_settle_s)
        after = self.body.get_state()
        reach = _mining_reach_distance(after.pos, pos)
        navigation_metrics.update(
            {
                "state_after": list(_state_block_pos(after.pos)),
                "reach_distance": reach,
                "approach_settle_s": self._mine_approach_settle_s,
            }
        )
        if reach > reach_limit:
            return ToolResult(
                success=False,
                reason="mine_approach_out_of_range",
                can_retry=True,
                next_suggestion="rescan mining stands from the current world state before retrying",
                metrics=navigation_metrics,
            ), None
        return None, navigation_metrics

    def _mine_reach_limit(self, context: BreakContext | str) -> float:
        if BreakContext(context) is BreakContext.COLLECT:
            return self.MINE_INTERACTION_RANGE
        return self.DIRECT_MINE_REACH

    def mine_block_dry(
        self,
        pos: Position,
        *,
        context: BreakContext | str = BreakContext.COLLECT,
        seal_blocks: tuple[str, ...] | list[str] = DEFAULT_SEAL_BLOCKS,
        require_seal_inventory: bool = True,
        approach_seal_faces: bool = False,
        seal_approach_range: int = 4,
        max_seal_faces: int = MAX_SEAL_FACES,
        settle_s: float = 0.2,
        timeout_s: float = 30.0,
        prepositioned: bool = False,
    ) -> ToolResult:
        """Mine one target after preserving the old dry-mining liquid guard.

        The transaction owns only this target and its immediate liquid faces.
        It reads inventory facts only to choose an available seal material. It
        does not choose the next ore or loop across candidates; those are
        agent-layer concerns. Scarpet/placeBlock remains the physical authority
        for whether a requested seal block can actually be placed.
        """

        target = self.body.perceive("blockAt", _block_params(pos))
        failed = _perception_failure(target)
        if failed is not None:
            return failed

        block_type = str(target.data.get("type") or "unknown")
        block_state = str(target.data.get("state") or "UNKNOWN")
        if not _is_ore_block(block_type):
            result = self.mine_block(pos, context=context, timeout_s=timeout_s, approach=not prepositioned)
            return _with_metric(result, "dry_mining", {"required": False, "block_state": block_state})

        liquid_touch = self._liquid_contact_positions(pos)
        if liquid_touch.failed is not None:
            return liquid_touch.failed

        if not liquid_touch.positions:
            result = self.mine_block(pos, context=context, timeout_s=timeout_s, approach=not prepositioned)
            return _with_metric(
                result,
                "dry_mining",
                {
                    "required": True,
                    "initial_liquid_faces": 0,
                    "sealed_faces": [],
                    "block_state": block_state,
                },
            )

        liquid_faces = self._liquid_face_positions(pos)
        if liquid_faces.failed is not None:
            return liquid_faces.failed

        if len(liquid_faces.positions) > max_seal_faces:
            return ToolResult(
                success=False,
                reason="dry_mining_too_many_liquid_faces",
                can_retry=False,
                next_suggestion="choose a safer ore target or drain the liquid pocket before mining",
                metrics={
                    "target": list(pos),
                    "block_type": block_type,
                    "liquid_faces": [list(face) for face in liquid_faces.positions],
                    "max_seal_faces": max_seal_faces,
                },
            )

        if not seal_blocks:
            return ToolResult(
                success=False,
                reason="dry_mining_no_seal_blocks",
                can_retry=False,
                next_suggestion="provide a safe temporary seal block before mining this liquid-adjacent ore",
                metrics={
                    "target": list(pos),
                    "block_type": block_type,
                    "liquid_faces": [list(face) for face in liquid_faces.positions],
                },
            )

        seal_candidates = tuple(seal_blocks)
        inventory_counts: dict[str, int] | None = None
        if require_seal_inventory:
            inventory = _read_inventory_slots(self.body)
            failed = _perception_failure(inventory)
            if failed is not None:
                return _with_metric(
                    failed,
                    "dry_mining",
                    {
                        "target": list(pos),
                        "block_type": block_type,
                        "liquid_faces": [list(face) for face in liquid_faces.positions],
                    },
                )
            slots = [InventorySlot.from_payload(slot) for slot in inventory.data.get("slots") or []]
            inventory_counts = _inventory_counts(slots)
            seal_candidates = tuple(
                block for block in seal_candidates if inventory_counts.get(_normalize_item(block), 0) > 0
            )
            if not seal_candidates:
                return ToolResult(
                    success=False,
                    reason="dry_mining_no_seal_blocks_available",
                    can_retry=False,
                    next_suggestion="put a temporary seal block in inventory before mining this liquid-adjacent ore",
                    metrics={
                        "target": list(pos),
                        "block_type": block_type,
                        "liquid_faces": [list(face) for face in liquid_faces.positions],
                        "requested_seal_blocks": [str(block) for block in seal_blocks],
                        "inventory_counts": inventory_counts,
                    },
                )

        sealed: list[dict[str, object]] = []
        for face_pos in liquid_faces.positions:
            if approach_seal_faces:
                approached = self._approach_seal_face(face_pos, timeout_s=timeout_s)
                if approached is not None:
                    return _with_metric(
                        approached,
                        "dry_mining",
                        {
                            "target": list(pos),
                            "block_type": block_type,
                            "failed_face": list(face_pos),
                            "sealed_faces": sealed,
                            "seal_approach_range": seal_approach_range,
                        },
                    )
            placed = self._place_first_available_seal_block(
                face_pos,
                seal_blocks=seal_candidates,
                timeout_s=timeout_s,
            )
            if not placed.success:
                return _with_metric(
                    placed,
                    "dry_mining",
                    {
                        "target": list(pos),
                        "block_type": block_type,
                        "failed_face": list(face_pos),
                        "sealed_faces": sealed,
                    },
                )
            sealed.append(
                {
                    "pos": list(face_pos),
                    "block_type": placed.metrics.get("block_type") if placed.metrics else None,
                }
            )
            self._pause(settle_s)

        recheck = self._liquid_contact_positions(pos)
        if recheck.failed is not None:
            return recheck.failed
        if recheck.positions:
            return ToolResult(
                success=False,
                reason="dry_mining_still_liquid_adjacent",
                can_retry=True,
                next_suggestion="re-scan the pocket or choose a different target; the seal did not make the ore dry",
                metrics={
                    "target": list(pos),
                    "block_type": block_type,
                    "sealed_faces": sealed,
                    "remaining_liquid_contact": [list(face) for face in recheck.positions],
                },
            )

        mined = self.mine_block(pos, context=context, timeout_s=timeout_s, approach=not prepositioned)
        return _with_metric(
            mined,
            "dry_mining",
            {
                "required": True,
                "initial_liquid_faces": len(liquid_faces.positions),
                "sealed_faces": sealed,
                "seal_candidates": [str(block) for block in seal_candidates],
                "seal_inventory_counts": inventory_counts,
                "settle_s": settle_s,
            },
        )

    def mine_block_collect(
        self,
        pos: Position,
        *,
        context: BreakContext | str = BreakContext.COLLECT,
        dry: bool = False,
        expected_drops: tuple[str, ...] | list[str] | None = None,
        target_block_types: tuple[str, ...] | list[str] | None = None,
        drop_map: dict[str, tuple[str, ...]] | None = None,
        settle_s: float = 0.2,
        pickup_timeout_s: float = 1.5,
        timeout_s: float = 30.0,
        prepositioned: bool = False,
    ) -> ToolResult:
        """Mine one target and verify collection by inventory delta.

        This is not agent-side collect-N orchestration. It owns one physical target and returns
        whether expected drops appeared in inventory after the mining action.
        """

        target = self.body.perceive("blockAt", _block_params(pos))
        failed = _perception_failure(target)
        if failed is not None:
            return failed
        block_type = _normalize_item(str(target.data.get("type") or "unknown"))
        allowed_targets = tuple(_normalize_item(item) for item in (target_block_types or ()))
        if allowed_targets and block_type not in allowed_targets:
            return ToolResult(
                success=False,
                reason="break_denied:collect_target_required",
                can_retry=False,
                next_suggestion="retry with a candidate whose observed block type matches the collect plan",
                metrics={
                    "target": list(pos),
                    "block_type": block_type,
                    "target_block_types": list(allowed_targets),
                },
            )
        break_context = BreakContext.COLLECT_APPROACH if allowed_targets else context

        decision = self.governance.can_break(pos, block_type, break_context)
        if not decision.allowed:
            return _denied_result("break_denied", pos, block_type, decision)

        tool_gate = self._ensure_collect_pickaxe(pos, block_type, timeout_s=timeout_s)
        if not tool_gate.success:
            return tool_gate
        tool_gate_metrics = dict(tool_gate.metrics or {})

        expected = tuple(_normalize_item(item) for item in (expected_drops or ()))
        if not expected:
            expected = _expected_drops_for_block(block_type, drop_map or self.DEFAULT_DROP_MAP)

        before = _inventory_counts_from_body(self.body)
        if isinstance(before, ToolResult):
            return _with_metric(before, "collect", {"target": list(pos), "block_type": block_type, "phase": "before"})

        if dry:
            mined = self.mine_block_dry(
                pos,
                context=break_context,
                settle_s=settle_s,
                timeout_s=timeout_s,
                prepositioned=prepositioned,
            )
        else:
            mined = self.mine_block(pos, context=break_context, timeout_s=timeout_s, approach=not prepositioned)
        if not mined.success:
            collect_metrics = {
                "target": list(pos),
                "block_type": block_type,
                "target_block_types": list(allowed_targets),
                "expected_drops": list(expected),
                "before": before,
                "tool_gate": tool_gate_metrics,
            }
            return _with_metric(
                mined,
                "collect",
                collect_metrics,
            )

        self._pause(settle_s)
        pickup = self._collect_inventory_delta(
            pos=pos,
            before=before,
            expected=expected,
            pickup_timeout_s=pickup_timeout_s,
        )
        failed_after = pickup.get("failed")
        if isinstance(failed_after, ToolResult):
            return _with_metric(
                failed_after,
                "collect",
                {
                    "target": list(pos),
                    "block_type": block_type,
                    "phase": "after",
                    "pickup_assist": pickup.get("assist"),
                },
            )
        after = pickup["after"]
        deltas = pickup["deltas"]
        collected_total = int(pickup["collected_total"])
        metrics = {
            "target": list(pos),
            "block_type": block_type,
            "target_block_types": list(allowed_targets),
            "expected_drops": list(expected),
            "before": before,
            "after": after,
            "deltas": deltas,
            "collected_total": collected_total,
            "mine_result": mined.to_payload(),
            "pickup_assist": pickup["assist"],
            "tool_gate": tool_gate_metrics,
        }
        if collected_total <= 0:
            return ToolResult(
                success=False,
                reason="collect_no_inventory_delta",
                can_retry=True,
                next_suggestion="wait for pickup, move to the drop, or verify the expected drop mapping before counting this target complete",
                metrics=metrics,
            )
        return ToolResult(success=True, reason="collected", can_retry=False, metrics=metrics)

    def _ensure_collect_pickaxe(self, pos: Position, block_type: str, *, timeout_s: float) -> ToolResult:
        required = required_pickaxe_tier(block_type)
        if required is None:
            return ToolResult(
                success=True,
                reason="no_required_tool",
                can_retry=False,
                metrics={"target": list(pos), "block_type": block_type, "required_tier": None},
            )

        counts = _inventory_counts_from_body(self.body)
        if isinstance(counts, ToolResult):
            return _with_metric(
                counts,
                "tool_gate",
                {"target": list(pos), "block_type": block_type, "required_tier": required, "phase": "inventory"},
            )

        best = best_owned_pickaxe(counts)
        best_owned = None if best is None else {"item": best[0], "tier": best[1]}
        metrics: dict[str, object] = {
            "target": list(pos),
            "block_type": block_type,
            "required_tier": required,
            "best_owned": best_owned,
        }
        if best is None or not tier_satisfies(best[1], required):
            return ToolResult(
                success=False,
                reason="missing_required_tool",
                can_retry=False,
                next_suggestion=f"craft and equip a {PICKAXE_BY_TIER[required]} or better first",
                metrics=metrics,
            )

        item, tier = best
        selected = _dispatch_select_item(self.body, item, timeout_s=min(timeout_s, 5.0))
        metrics["selected_item"] = item
        metrics["selected_tier"] = tier
        metrics["select_result"] = selected.to_payload()
        if not selected.success:
            return ToolResult(
                success=False,
                reason=f"tool_equip_failed:{selected.reason}",
                can_retry=selected.can_retry,
                next_suggestion=selected.next_suggestion or f"make {item} available in the hotbar before mining {block_type}",
                metrics=metrics,
            )
        return ToolResult(success=True, reason="tool_ready", can_retry=False, metrics=metrics)


    def _collect_inventory_delta(
        self,
        *,
        pos: Position,
        before: dict[str, int],
        expected: tuple[str, ...],
        pickup_timeout_s: float,
    ) -> dict[str, object]:
        return self.pickup._collect_inventory_delta(
            before=before,
            expected=expected,
            minimum_count=1,
            fallback_positions=_pickup_fallback_positions(pos),
            config=PickupConfig(poll_timeout_s=pickup_timeout_s),
        )

    def dig_down_one(
        self,
        *,
        current_pos: Position | None = None,
        context: BreakContext | str = BreakContext.DIRECT,
        max_clear_fall: int = 2,
        timeout_s: float = 30.0,
    ) -> ToolResult:
        """Safely open one block below the bot for a downward shaft.

        This is the opt-in, single-step Body transaction under a future
        cross-depth digDown wrapper. It refuses liquid starts, liquid landings,
        excessive fall depth, and governance-denied floor blocks before sending
        any mutation.
        """

        if max_clear_fall < 1:
            raise ValueError("max_clear_fall must be at least 1")

        origin = current_pos or _state_block_pos(self.body.get_state().pos)
        start_scan = self._scan_start_liquid(origin)
        if start_scan is not None:
            return start_scan

        target_pos = (origin[0], origin[1] - 1, origin[2])
        target = self.body.perceive("blockAt", _block_params(target_pos))
        failed = _perception_failure(target)
        if failed is not None:
            return _with_metric(failed, "dig_down", {"origin": list(origin), "target": list(target_pos)})

        target_type = str(target.data.get("type") or "unknown")
        target_state = str(target.data.get("state") or "UNKNOWN")
        if self._is_liquid_perception(target):
            return ToolResult(
                success=False,
                reason="dig_down_target_liquid",
                can_retry=False,
                next_suggestion="choose a different descent column or drain/seal the liquid before digging down",
                metrics={
                    "origin": list(origin),
                    "target": list(target_pos),
                    "block_type": _normalize_item(target_type),
                    "target_state": target_state,
                },
            )

        fall_probe = self._fall_probe_after_opening(target_pos, max_clear_fall=max_clear_fall)
        if fall_probe.failed is not None:
            return _with_metric(
                fall_probe.failed,
                "dig_down",
                {
                    "origin": list(origin),
                    "target": list(target_pos),
                    "block_type": _normalize_item(target_type),
                },
            )

        if fall_probe.liquid_landing is not None:
            return ToolResult(
                success=False,
                reason="dig_down_landing_liquid",
                can_retry=False,
                next_suggestion="choose a different descent column or seal the liquid below before digging down",
                metrics={
                    "origin": list(origin),
                    "target": list(target_pos),
                    "liquid_pos": list(fall_probe.liquid_landing),
                    "fall_clearance": fall_probe.clear_depth,
                    "max_clear_fall": max_clear_fall,
                },
            )

        if fall_probe.clear_depth > max_clear_fall:
            return ToolResult(
                success=False,
                reason="dig_down_fall_risk",
                can_retry=False,
                next_suggestion="use a staircase, pillar, or scaffold descent instead of opening this shaft",
                metrics={
                    "origin": list(origin),
                    "target": list(target_pos),
                    "fall_clearance": fall_probe.clear_depth,
                    "max_clear_fall": max_clear_fall,
                    "first_support": list(fall_probe.support_pos) if fall_probe.support_pos else None,
                },
            )

        dig_metrics = {
            "origin": list(origin),
            "target": list(target_pos),
            "block_type": _normalize_item(target_type),
            "target_state": target_state,
            "fall_clearance": fall_probe.clear_depth,
            "first_support": list(fall_probe.support_pos) if fall_probe.support_pos else None,
            "first_support_type": fall_probe.support_type,
            "safe_to_continue": fall_probe.clear_depth <= max_clear_fall,
        }

        if _is_clear_perception(target):
            return ToolResult(
                success=True,
                reason="dig_down_already_open",
                can_retry=False,
                metrics={"dig_down": dig_metrics},
            )

        decision = self.governance.can_break(target_pos, target_type, context)
        if not decision.allowed:
            return _denied_result("dig_down_denied", target_pos, target_type, decision)

        mined = self.mine_block(target_pos, context=context, timeout_s=timeout_s)
        return _with_metric(mined, "dig_down", {**dig_metrics, "legality": _decision_payload(decision)})

    def dig_down_to_y(
        self,
        target_y: int,
        *,
        current_pos: Position | None = None,
        context: BreakContext | str = BreakContext.DIRECT,
        max_clear_fall: int = 2,
        move_timeout_s: float = 15.0,
        dig_timeout_s: float = 30.0,
        max_steps: int | None = None,
    ) -> ToolResult:
        """Descend a vertical shaft to a target Y using safe single-step opens.

        This remains a single-objective Body transaction: one descent column,
        repeated guarded floor openings, and honest stop facts when descent
        cannot continue.
        """

        origin = current_pos or _state_block_pos(self.body.get_state().pos)
        if origin[1] <= target_y:
            return ToolResult(
                success=True,
                reason="dig_down_target_reached",
                can_retry=False,
                metrics={
                    "origin": list(origin),
                    "target_y": target_y,
                    "final_pos": list(origin),
                    "steps": [],
                    "steps_completed": 0,
                },
            )

        if max_steps is not None and max_steps < 1:
            raise ValueError("max_steps must be at least 1 when provided")

        step_budget = max_steps if max_steps is not None else origin[1] - target_y
        current = origin
        steps: list[dict[str, object]] = []
        descents_completed = 0

        while current[1] > target_y and descents_completed < step_budget:
            open_result = self.dig_down_one(
                current_pos=current,
                context=context,
                max_clear_fall=max_clear_fall,
                timeout_s=dig_timeout_s,
            )
            open_metrics = dict(open_result.metrics or {})
            steps.append(
                {
                    "kind": "open",
                    "origin": list(current),
                    "success": open_result.success,
                    "reason": open_result.reason,
                    "metrics": open_metrics,
                }
            )
            if not open_result.success:
                return ToolResult(
                    success=False,
                    reason=open_result.reason,
                    can_retry=open_result.can_retry,
                    next_suggestion=open_result.next_suggestion,
                    metrics={
                        "origin": list(origin),
                        "target_y": target_y,
                        "final_pos": list(current),
                        "steps": steps,
                        "steps_completed": descents_completed,
                    },
                )

            alignment = self._align_for_descent(
                current,
                timeout_s=min(move_timeout_s, self.SHAFT_ALIGNMENT_TIMEOUT_S),
            )
            if bool((alignment.metrics or {}).get("attempted")) or not alignment.success:
                steps.append(
                    {
                        "kind": "alignment",
                        "origin": list(current),
                        "success": alignment.success,
                        "reason": alignment.reason,
                        "metrics": dict(alignment.metrics or {}),
                    }
                )
            if not alignment.success:
                return ToolResult(
                    success=False,
                    reason=alignment.reason,
                    can_retry=alignment.can_retry,
                    next_suggestion=alignment.next_suggestion,
                    metrics={
                        "origin": list(origin),
                        "target_y": target_y,
                        "final_pos": list(_state_block_pos(self.body.get_state().pos)),
                        "steps": steps,
                        "steps_completed": descents_completed,
                    },
                )

            descend_to = (current[0], current[1] - 1, current[2])
            descent = self._wait_for_descent(descend_to, timeout_s=move_timeout_s)
            steps.append(
                {
                    "kind": "descent",
                    "origin": list(current),
                    "target": list(descend_to),
                    "success": descent.success,
                    "reason": descent.reason,
                    "metrics": dict(descent.metrics or {}),
                }
            )
            if not descent.success:
                return ToolResult(
                    success=False,
                    reason=descent.reason,
                    can_retry=descent.can_retry,
                    next_suggestion=descent.next_suggestion,
                    metrics={
                        "origin": list(origin),
                        "target_y": target_y,
                        "final_pos": list(current),
                        "steps": steps,
                        "steps_completed": descents_completed,
                    },
                )

            observed = tuple(descent.metrics["observed_pos"])
            steps[-1]["observed_pos"] = list(observed)
            if observed[1] > current[1] - 1:
                return ToolResult(
                    success=False,
                    reason="dig_down_descent_incomplete",
                    can_retry=True,
                    next_suggestion="re-sync the body position before continuing the shaft descent",
                    metrics={
                        "origin": list(origin),
                        "target_y": target_y,
                        "expected_pos": list(descend_to),
                        "final_pos": list(observed),
                        "steps": steps,
                        "steps_completed": descents_completed,
                    },
                )
            descents_completed += 1
            current = observed

        if current[1] <= target_y:
            return ToolResult(
                success=True,
                reason="dig_down_target_reached",
                can_retry=False,
                metrics={
                    "origin": list(origin),
                    "target_y": target_y,
                    "final_pos": list(current),
                    "steps": steps,
                    "steps_completed": descents_completed,
                },
            )

        return ToolResult(
            success=False,
            reason="dig_down_step_budget_exhausted",
            can_retry=True,
            next_suggestion="raise the step budget or resume the shaft descent from the current position",
            metrics={
                "origin": list(origin),
                "target_y": target_y,
                "final_pos": list(current),
                "step_budget": step_budget,
                "steps": steps,
                "steps_completed": descents_completed,
            },
        )

    def _align_for_descent(self, shaft: Position, *, timeout_s: float) -> ToolResult:
        state = self.body.get_state()
        observed = _state_block_pos(state.pos)
        base_metrics: dict[str, object] = {
            "attempted": False,
            "shaft": list(shaft),
            "position_before": [round(value, 3) for value in state.pos],
            "block_pos_before": list(observed),
        }
        if observed[1] <= shaft[1] - 1:
            return ToolResult(
                success=True,
                reason="dig_down_alignment_already_descended",
                can_retry=False,
                metrics={**base_metrics, "already_descended": True},
            )
        if _body_fits_shaft(state.pos, shaft):
            return ToolResult(
                success=True,
                reason="dig_down_alignment_not_required",
                can_retry=False,
                metrics={**base_metrics, "already_aligned": True},
            )

        try:
            centered = move_to_block_center(
                self.body,
                shaft,
                arrival_radius=self.SHAFT_ALIGNMENT_RADIUS,
                timeout_s=timeout_s,
                stabilize=False,
            )
        except TimeoutError as exc:
            interrupted = self.body.interrupt("dig_down_alignment_timeout")
            return ToolResult(
                success=False,
                reason="dig_down_alignment_failed:timeout",
                can_retry=True,
                next_suggestion="re-sync position and retry the shaft from a stable centered stance",
                metrics={
                    **base_metrics,
                    "attempted": True,
                    "timeout_s": timeout_s,
                    "timeout_diagnostics": dict(getattr(exc, "diagnostics", {}) or {}),
                    "interrupt": {
                        "ok": interrupted.ok,
                        "accepted": interrupted.accepted,
                        "error": interrupted.error,
                        "data": interrupted.data,
                    },
                },
            )

        movement = centered.to_payload()
        after = self.body.get_state()
        after_block = _state_block_pos(after.pos)
        descended = after_block[1] <= shaft[1] - 1
        aligned = _body_fits_shaft(after.pos, shaft)
        metrics = {
            **base_metrics,
            "attempted": True,
            "movement": movement,
            "position_after": [round(value, 3) for value in after.pos],
            "block_pos_after": list(after_block),
            "aligned": aligned,
            "already_descended": descended,
        }
        if descended:
            return ToolResult(
                success=True,
                reason="dig_down_aligned",
                can_retry=False,
                metrics=metrics,
            )
        if not centered.success or centered.reason != "arrived":
            return ToolResult(
                success=False,
                reason=f"dig_down_alignment_failed:{centered.reason}",
                can_retry=centered.can_retry,
                next_suggestion=centered.next_suggestion or "re-sync position and retry from a stable shaft edge",
                metrics=metrics,
            )
        if not descended and not aligned:
            return ToolResult(
                success=False,
                reason="dig_down_alignment_failed:terminal_position_mismatch",
                can_retry=True,
                next_suggestion="re-sync position before continuing the shaft descent",
                metrics=metrics,
            )
        return ToolResult(
            success=True,
            reason="dig_down_aligned",
            can_retry=False,
            metrics=metrics,
        )

    def _wait_for_descent(self, target: Position, *, timeout_s: float, poll_s: float = 0.05) -> ToolResult:
        deadline = time.monotonic() + timeout_s
        samples: list[list[float]] = []
        observed = _state_block_pos(self.body.get_state().pos)
        while time.monotonic() <= deadline:
            state = self.body.get_state()
            observed = _state_block_pos(state.pos)
            samples.append([round(state.pos[0], 3), round(state.pos[1], 3), round(state.pos[2], 3)])
            if observed[1] <= target[1]:
                return ToolResult(
                    success=True,
                    reason="descended",
                    can_retry=False,
                    metrics={
                        "target": list(target),
                        "observed_pos": list(observed),
                        "samples": samples[-5:],
                    },
                )
            self._pause(poll_s)
        return ToolResult(
            success=False,
            reason="dig_down_descent_timeout",
            can_retry=True,
            next_suggestion="wait for the body to finish falling or re-sync position before continuing shaft descent",
            metrics={
                "target": list(target),
                "observed_pos": list(observed),
                "timeout_s": timeout_s,
                "samples": samples[-5:],
            },
        )

    def dig_up_one(
        self,
        *,
        current_pos: Position | None = None,
        context: BreakContext | str = BreakContext.DIRECT,
        scaffold_blocks: tuple[str, ...] | list[str] = DEFAULT_PILLAR_BLOCKS,
        timeout_s: float = 30.0,
    ) -> ToolResult:
        """Raise the bot by one block via guarded shaft-clear + jump + pillar.

        This is the single-step upward Body transaction beneath a future
        multi-step `digUp`/surface escape wrapper.
        """

        origin = current_pos or _state_block_pos(self.body.get_state().pos)
        head_pos = (origin[0], origin[1] + 1, origin[2])
        cap_pos = (origin[0], origin[1] + 2, origin[2])

        scaffold_counts = _inventory_counts_from_body(self.body)
        if isinstance(scaffold_counts, ToolResult):
            return _with_metric(scaffold_counts, "dig_up", {"origin": list(origin), "phase": "inventory"})

        candidates = tuple(
            _normalize_item(block)
            for block in scaffold_blocks
            if scaffold_counts.get(_normalize_item(block), 0) > 0
        )
        if not candidates:
            return ToolResult(
                success=False,
                reason="dig_up_no_scaffold_available",
                can_retry=False,
                next_suggestion="put a safe scaffold block in inventory before attempting a pillar ascent",
                metrics={
                    "origin": list(origin),
                    "requested_scaffold_blocks": [_normalize_item(block) for block in scaffold_blocks],
                    "inventory_counts": scaffold_counts,
                },
            )

        cleared: list[dict[str, object]] = []
        for label, pos in (("head", head_pos), ("cap", cap_pos)):
            perception = self.body.perceive("blockAt", _block_params(pos))
            failed = _perception_failure(perception)
            if failed is not None:
                return _with_metric(failed, "dig_up", {"origin": list(origin), "phase": label, "target": list(pos)})
            if self._is_liquid_perception(perception):
                return ToolResult(
                    success=False,
                    reason="dig_up_liquid_above",
                    can_retry=False,
                    next_suggestion="choose another ascent column or seal the liquid above before pillar-up",
                    metrics={
                        "origin": list(origin),
                        "phase": label,
                        "target": list(pos),
                        "block_type": _normalize_item(str(perception.data.get("type") or "unknown")),
                    },
                )
            if _is_clear_perception(perception):
                continue
            mined = self.mine_block(pos, context=context, timeout_s=timeout_s)
            cleared.append({"phase": label, "target": list(pos), "reason": mined.reason, "success": mined.success})
            if not mined.success:
                return _with_metric(
                    mined,
                    "dig_up",
                    {
                        "origin": list(origin),
                        "cleared": cleared,
                        "scaffold_candidates": list(candidates),
                    },
                )

        before = self.body.get_state()
        jump_action = Action.create("jump", {})
        accepted = self.body.execute(jump_action)
        rejected = _acceptance_failure(accepted, "jump", origin)
        if rejected is not None:
            return _with_metric(
                rejected,
                "dig_up",
                {"origin": list(origin), "cleared": cleared, "scaffold_candidates": list(candidates)},
            )

        jump_terminal = self.body.await_action_terminal(jump_action.id, timeout_s=timeout_s)
        jump_result = terminal_event_to_tool_result(jump_terminal)
        if not jump_result.success:
            return _with_metric(
                jump_result,
                "dig_up",
                {"origin": list(origin), "cleared": cleared, "scaffold_candidates": list(candidates)},
            )

        scaffold_block = candidates[0]
        placed = self.place_block(
            origin,
            scaffold_block,
            context=PlaceContext.WORK,
            purpose="pillar",
            timeout_s=timeout_s,
        )
        if not placed.success:
            return _with_metric(
                placed,
                "dig_up",
                {
                    "origin": list(origin),
                    "cleared": cleared,
                    "jump_result": jump_result.to_payload(),
                    "scaffold_candidates": list(candidates),
                },
            )

        after = self.body.get_state()
        gained_y = after.pos[1] - before.pos[1]
        metrics = {
            "origin": list(origin),
            "final_pos": list(_state_block_pos(after.pos)),
            "gained_y": gained_y,
            "cleared": cleared,
            "jump_result": jump_result.to_payload(),
            "place_result": placed.to_payload(),
            "scaffold_block": scaffold_block,
            "scaffold_candidates": list(candidates),
        }
        if gained_y <= 0:
            return ToolResult(
                success=False,
                reason="dig_up_no_height_gain",
                can_retry=True,
                next_suggestion="re-sync the body position or use a dedicated vertical controller before retrying pillar-up",
                metrics=metrics,
            )
        return ToolResult(success=True, reason="dig_up_step_completed", can_retry=False, metrics=metrics)

    def dig_up_to_y(
        self,
        target_y: int,
        *,
        current_pos: Position | None = None,
        context: BreakContext | str = BreakContext.DIRECT,
        scaffold_blocks: tuple[str, ...] | list[str] = DEFAULT_PILLAR_BLOCKS,
        timeout_s: float = 30.0,
        max_steps: int | None = None,
    ) -> ToolResult:
        """Ascend a vertical shaft to a target Y using guarded pillar-up steps."""

        origin = current_pos or _state_block_pos(self.body.get_state().pos)
        if origin[1] >= target_y:
            return ToolResult(
                success=True,
                reason="dig_up_target_reached",
                can_retry=False,
                metrics={
                    "origin": list(origin),
                    "target_y": target_y,
                    "final_pos": list(origin),
                    "steps": [],
                    "steps_completed": 0,
                },
            )

        if max_steps is not None and max_steps < 1:
            raise ValueError("max_steps must be at least 1 when provided")

        step_budget = max_steps if max_steps is not None else target_y - origin[1]
        current = origin
        steps: list[dict[str, object]] = []
        ascents_completed = 0

        while current[1] < target_y and ascents_completed < step_budget:
            step_result = self.dig_up_one(
                current_pos=current,
                context=context,
                scaffold_blocks=scaffold_blocks,
                timeout_s=timeout_s,
            )
            step_metrics = dict(step_result.metrics or {})
            steps.append(
                {
                    "kind": "ascend",
                    "origin": list(current),
                    "success": step_result.success,
                    "reason": step_result.reason,
                    "metrics": step_metrics,
                }
            )
            if not step_result.success:
                return ToolResult(
                    success=False,
                    reason=step_result.reason,
                    can_retry=step_result.can_retry,
                    next_suggestion=step_result.next_suggestion,
                    metrics={
                        "origin": list(origin),
                        "target_y": target_y,
                        "final_pos": list(current),
                        "steps": steps,
                        "steps_completed": ascents_completed,
                    },
                )

            observed = _state_block_pos(self.body.get_state().pos)
            steps[-1]["observed_pos"] = list(observed)
            if observed[1] <= current[1]:
                return ToolResult(
                    success=False,
                    reason="dig_up_ascent_incomplete",
                    can_retry=True,
                    next_suggestion="re-sync the body position before continuing the shaft ascent",
                    metrics={
                        "origin": list(origin),
                        "target_y": target_y,
                        "expected_min_y": current[1] + 1,
                        "final_pos": list(observed),
                        "steps": steps,
                        "steps_completed": ascents_completed,
                    },
                )
            current = observed
            ascents_completed += 1

        if current[1] >= target_y:
            return ToolResult(
                success=True,
                reason="dig_up_target_reached",
                can_retry=False,
                metrics={
                    "origin": list(origin),
                    "target_y": target_y,
                    "final_pos": list(current),
                    "steps": steps,
                    "steps_completed": ascents_completed,
                },
            )

        return ToolResult(
            success=False,
            reason="dig_up_step_budget_exhausted",
            can_retry=True,
            next_suggestion="raise the step budget or resume the shaft ascent from the current position",
            metrics={
                "origin": list(origin),
                "target_y": target_y,
                "final_pos": list(current),
                "step_budget": step_budget,
                "steps": steps,
                "steps_completed": ascents_completed,
            },
        )

    def go_to_surface(
        self,
        *,
        current_pos: Position | None = None,
        context: BreakContext | str = BreakContext.DIRECT,
        scaffold_blocks: tuple[str, ...] | list[str] = DEFAULT_PILLAR_BLOCKS,
        timeout_s: float = 30.0,
        max_steps: int | None = None,
        surface_scan_height: int = 96,
        surface_scan_radius: int = 1,
        world_top_y: int = 320,
    ) -> ToolResult:
        """Reach one bounded, verified surface domain through shared navigation."""

        if surface_scan_height < 0:
            raise ValueError("surface_scan_height must be >= 0")
        if surface_scan_radius < 0:
            raise ValueError("surface_scan_radius must be >= 0")
        if world_top_y < -64:
            raise ValueError("world_top_y must be realistic")

        requested_origin = current_pos or _state_block_pos(self.body.get_state().pos)
        origin = requested_origin
        surface_egress: dict[str, object] | None = None
        initial = self._surface_candidate_at(origin, world_top_y=world_top_y)
        if isinstance(initial, ToolResult):
            return _with_metric(initial, "go_to_surface", {"origin": list(origin)})
        if _surface_terminal_verified(initial):
            return ToolResult(
                success=True,
                reason="surface_reached",
                can_retry=False,
                metrics={
                    "origin": list(origin),
                    "target_surface": list(origin),
                    "selected_goal": list(origin),
                    "final_pos": list(origin),
                    "terminal_surface": initial,
                    "terminal_surface_verified": True,
                    "navigation": None,
                },
            )

        from minebot.body.navigation import NavigationRunConfig, pure_movement_navigation_config

        if _surface_requires_lateral_egress(initial):
            egress_domain = self._find_surface_egress_domain(
                origin,
                radius=self.SURFACE_EGRESS_RADIUS,
                y_below=self.SURFACE_EGRESS_Y_BELOW,
                y_above=self.SURFACE_EGRESS_Y_ABOVE,
                max_candidates=self.SURFACE_EGRESS_GOAL_LIMIT,
            )
            if isinstance(egress_domain, ToolResult):
                return _with_metric(
                    egress_domain,
                    "go_to_surface",
                    {"origin": list(requested_origin), "phase": "lateral_egress"},
                )
            egress_candidates = tuple(tuple(entry["feet_pos"]) for entry in egress_domain["candidates"])
            surface_egress = {
                "required": True,
                "origin": list(origin),
                "domain": egress_domain,
                "navigation": None,
            }
            if egress_candidates:
                if self.navigator is None:
                    return ToolResult(
                        success=False,
                        reason="surface_navigation_missing",
                        can_retry=True,
                        next_suggestion="attach the shared navigation process before requesting a covered-water exit",
                        metrics={"origin": list(requested_origin), "surface_egress": surface_egress},
                    )
                egress_goal = GoalComposite(tuple(GoalNear(candidate, radius=0) for candidate in egress_candidates))
                egress_navigation = self.navigator.navigate_to(
                    egress_goal,
                    break_context=BreakContext.TRAVEL,
                    config=pure_movement_navigation_config(
                        NavigationRunConfig(segment_timeout_s=timeout_s)
                    ),
                    arrival_radius=0.25,
                )
                selected_egress = _selected_surface_goal(egress_navigation, egress_candidates)
                surface_egress.update(
                    {
                        "selected_goal": list(selected_egress),
                        "navigation_goal": egress_goal.payload(),
                        "navigation": egress_navigation.to_payload(),
                    }
                )
                if not egress_navigation.success:
                    return ToolResult(
                        success=False,
                        reason=f"surface_egress_failed:{egress_navigation.reason}",
                        can_retry=egress_navigation.can_retry,
                        next_suggestion=egress_navigation.next_suggestion,
                        metrics={"origin": list(requested_origin), "surface_egress": surface_egress},
                    )
                origin = _state_block_pos(self.body.get_state().pos)
                dry_stand = self._standable_feet_at(origin)
                surface_egress["final_pos"] = list(origin)
                surface_egress["terminal_stand"] = (
                    dry_stand.to_payload() if isinstance(dry_stand, ToolResult) else dry_stand
                )
                if isinstance(dry_stand, ToolResult) or not dry_stand["standable"]:
                    return ToolResult(
                        success=False,
                        reason="surface_egress_verification_failed",
                        can_retry=True,
                        next_suggestion="rescan a dry shore domain from authoritative world facts",
                        metrics={"origin": list(requested_origin), "surface_egress": surface_egress},
                    )
                initial = self._surface_candidate_at(origin, world_top_y=world_top_y)
                if isinstance(initial, ToolResult):
                    return _with_metric(
                        initial,
                        "go_to_surface",
                        {"origin": list(requested_origin), "surface_egress": surface_egress},
                    )
                if _surface_terminal_verified(initial):
                    return ToolResult(
                        success=True,
                        reason="surface_reached",
                        can_retry=False,
                        metrics={
                            "origin": list(requested_origin),
                            "surface_origin": list(origin),
                            "target_surface": list(origin),
                            "selected_goal": list(origin),
                            "final_pos": list(origin),
                            "terminal_surface": initial,
                            "terminal_surface_verified": True,
                            "surface_egress": surface_egress,
                            "navigation": None,
                        },
                    )

        scaffold_counts = _inventory_counts_from_body(self.body)
        if isinstance(scaffold_counts, ToolResult):
            return _with_metric(
                scaffold_counts,
                "go_to_surface",
                {
                    "origin": list(requested_origin),
                    "surface_origin": list(origin),
                    "surface_egress": surface_egress,
                    "phase": "capability_snapshot",
                },
            )
        normalized_scaffolds = tuple(_normalize_item(block) for block in scaffold_blocks)
        available_scaffolds = tuple(block for block in normalized_scaffolds if scaffold_counts.get(block, 0) > 0)
        surface_capability = {
            "constructible_pillar": bool(available_scaffolds),
            "available_scaffolds": list(available_scaffolds),
        }

        domain = self._find_surface_domain(
            origin,
            max_scan_height=surface_scan_height,
            scan_radius=surface_scan_radius,
            world_top_y=world_top_y,
            max_candidates=self.SURFACE_GOAL_LIMIT,
            allow_constructible=bool(available_scaffolds),
        )
        if isinstance(domain, ToolResult):
            return _with_metric(
                domain,
                "go_to_surface",
                {
                    "origin": list(requested_origin),
                    "surface_origin": list(origin),
                    "surface_egress": surface_egress,
                    "surface_capability": surface_capability,
                },
            )
        lateral_domain: dict[str, object] | None = None
        if surface_egress is not None:
            lateral = self._find_lateral_surface_domain(
                origin,
                ring_specs=self.SURFACE_LATERAL_RING_SPECS,
                max_candidates=self.SURFACE_LATERAL_GOAL_LIMIT,
            )
            if isinstance(lateral, ToolResult):
                return _with_metric(
                    lateral,
                    "go_to_surface",
                    {
                        "origin": list(requested_origin),
                        "surface_origin": list(origin),
                        "surface_egress": surface_egress,
                    },
                )
            lateral_domain = lateral
            if lateral_domain["candidates"]:
                domain = {
                    **domain,
                    "local_candidates": domain["candidates"],
                    "candidates": lateral_domain["candidates"],
                    "selection": "covered_water_lateral_surface",
                }
        candidates = tuple(tuple(entry["feet_pos"]) for entry in domain["candidates"])
        if not candidates:
            return ToolResult(
                success=False,
                reason="surface_not_found_in_column",
                can_retry=True,
                next_suggestion="try another column or fall back to a staircase/surface-search transaction",
                metrics={
                    "origin": list(requested_origin),
                    "surface_origin": list(origin),
                    "surface_scan_height": surface_scan_height,
                    "surface_scan_radius": surface_scan_radius,
                    "world_top_y": world_top_y,
                    "surface_domain": domain,
                    "surface_lateral_domain": lateral_domain,
                    "surface_egress": surface_egress,
                    "surface_capability": surface_capability,
                },
            )
        if self.navigator is None:
            return ToolResult(
                success=False,
                reason="surface_navigation_missing",
                can_retry=True,
                next_suggestion="attach the shared navigation process before requesting a surface exit",
                metrics={
                    "origin": list(requested_origin),
                    "surface_origin": list(origin),
                    "surface_domain": domain,
                    "surface_lateral_domain": lateral_domain,
                    "surface_egress": surface_egress,
                    "surface_capability": surface_capability,
                },
            )

        mutation_budget = max_steps if max_steps is not None else max(1, surface_scan_height)
        goal = GoalComposite(tuple(GoalNear(candidate, radius=0) for candidate in candidates))
        navigation = self.navigator.navigate_to(
            goal,
            break_context=BreakContext(context),
            config=NavigationRunConfig(
                segment_timeout_s=timeout_s,
                allow_break=True,
                max_break_steps=mutation_budget,
                allow_place=True,
                max_place_steps=mutation_budget,
                allow_pillar=True,
                max_pillar_steps=mutation_budget,
                allow_downward=False,
                max_downward_steps=0,
                scaffold_blocks=tuple(scaffold_blocks),
            ),
            arrival_radius=0.25,
        )
        selected_goal = _selected_surface_goal(navigation, candidates)
        if not navigation.success:
            return ToolResult(
                success=False,
                reason=f"surface_navigation_failed:{navigation.reason}",
                can_retry=navigation.can_retry,
                next_suggestion=navigation.next_suggestion,
                metrics={
                    "origin": list(requested_origin),
                    "surface_origin": list(origin),
                    "target_surface": list(selected_goal),
                    "selected_goal": list(selected_goal),
                    "surface_domain": domain,
                    "surface_lateral_domain": lateral_domain,
                    "navigation_goal": goal.payload(),
                    "navigation": navigation.to_payload(),
                    "surface_egress": surface_egress,
                    "surface_capability": surface_capability,
                },
            )

        final_pos = _state_block_pos(self.body.get_state().pos)
        terminal = self._surface_candidate_at(final_pos, world_top_y=world_top_y)
        if isinstance(terminal, ToolResult):
            return _with_metric(
                terminal,
                "go_to_surface",
                {
                    "origin": list(requested_origin),
                    "surface_origin": list(origin),
                    "target_surface": list(selected_goal),
                    "selected_goal": list(selected_goal),
                    "surface_domain": domain,
                    "surface_lateral_domain": lateral_domain,
                    "navigation": navigation.to_payload(),
                    "surface_egress": surface_egress,
                    "surface_capability": surface_capability,
                },
            )
        terminal_verified = final_pos in candidates and _surface_terminal_verified(terminal)
        if not terminal_verified:
            return ToolResult(
                success=False,
                reason="surface_verification_failed",
                can_retry=True,
                next_suggestion="re-sync the body state or continue with a broader surface-search fallback",
                metrics={
                    "origin": list(requested_origin),
                    "surface_origin": list(origin),
                    "target_surface": list(selected_goal),
                    "selected_goal": list(selected_goal),
                    "final_pos": list(final_pos),
                    "surface_domain": domain,
                    "surface_lateral_domain": lateral_domain,
                    "terminal_surface": terminal,
                    "navigation_goal": goal.payload(),
                    "navigation": navigation.to_payload(),
                    "surface_egress": surface_egress,
                    "surface_capability": surface_capability,
                },
            )

        return ToolResult(
            success=True,
            reason="surface_reached",
            can_retry=False,
            metrics={
                "origin": list(requested_origin),
                "surface_origin": list(origin),
                "target_surface": list(selected_goal),
                "selected_goal": list(selected_goal),
                "final_pos": list(final_pos),
                "surface_domain": domain,
                "surface_lateral_domain": lateral_domain,
                "terminal_surface": terminal,
                "terminal_surface_verified": True,
                "navigation_goal": goal.payload(),
                "navigation": navigation.to_payload(),
                "surface_egress": surface_egress,
                "surface_capability": surface_capability,
            },
        )

    def search_for_block(
        self,
        *,
        block_types: tuple[str, ...],
        search_radius: int = 16,
        interaction_radius: float = 4.5,
        timeout_s: float = 15.0,
        find_limit: int = 32,
        max_pages: int = 1,
    ) -> ToolResult:
        """Find block candidates without moving the body.

        This tool is exposed as read_world/perception. It must not invoke
        navigation or mutate position; approach belongs to mine_block_collect or
        an explicit navigation tool.
        """

        if not block_types:
            return ToolResult(
                success=False,
                reason="search_block_filter_missing",
                can_retry=False,
                metrics={"search_radius": search_radius},
            )
        if interaction_radius <= 0:
            return ToolResult(
                success=False,
                reason="invalid_interaction_radius",
                can_retry=False,
                metrics={
                    "block_types": list(block_types),
                    "search_radius": search_radius,
                    "interaction_radius": interaction_radius,
                },
            )

        search = find_nearby_block_search(
            self.body,
            block_types=block_types,
            radius=search_radius,
            limit=find_limit,
            max_pages=max(1, int(max_pages)),
            not_found_reason="search_block_not_found",
        )
        if isinstance(search, ToolResult):
            return search
        targets = search.targets
        target = targets[0]

        target_center = (target.pos[0] + 0.5, target.pos[1] + 0.5, target.pos[2] + 0.5)
        state = self.body.get_state()
        initial_distance = dist(state.pos, target_center)
        context = {
            "block_types": list(block_types),
            "search_radius": search_radius,
            "interaction_radius": interaction_radius,
            "find_limit": find_limit,
            "max_pages": max(1, int(max_pages)),
            "target": {
                "pos": list(target.pos),
                "type": target.block_type,
                "distance": target.distance,
            },
            "candidates": [
                {"pos": list(candidate.pos), "type": candidate.block_type, "distance": candidate.distance}
                for candidate in targets
            ],
            "truncated": search.truncated,
            "uncertainty": list(search.uncertainty),
            "pages_read": search.pages_read,
            "total_matches": search.total_matches,
            "line_of_sight_verified": False,
            "interaction_readiness": "unknown",
        }
        if search.errors:
            context["perception_errors"] = list(search.errors)

        if initial_distance <= interaction_radius:
            return ToolResult(
                success=True,
                reason="block_in_range",
                can_retry=False,
                next_suggestion=(
                    "Distance alone is not interaction truth. Use collect_resource for count-based acquisition "
                    "or get_to_block for one block-approach objective before choosing an exact-target action."
                ),
                metrics={
                    **context,
                    "range_verified": True,
                    "initial_distance": initial_distance,
                    "final_distance": initial_distance,
                },
            )

        return ToolResult(
            success=True,
            reason="block_candidates_found",
            can_retry=False,
            next_suggestion=(
                "A search hit is not an approach result. Use collect_resource for count-based acquisition or "
                "get_to_block for one block-approach objective; generic move_to does not prove access."
            ),
            metrics={
                **context,
                "range_verified": False,
                "initial_distance": initial_distance,
                "final_distance": initial_distance,
            },
        )

    def _find_surface_domain(
        self,
        origin: Position,
        *,
        max_scan_height: int,
        scan_radius: int = 1,
        world_top_y: int,
        max_candidates: int,
        allow_constructible: bool = True,
    ) -> dict[str, object] | ToolResult:
        top = min(origin[1] + max_scan_height, world_top_y - 1)
        candidates: list[dict[str, object]] = []
        seen: set[Position] = set()
        scanned = 0
        for y in range(origin[1], top + 1):
            for feet_pos in _surface_scan_targets(origin, y, radius=scan_radius):
                if feet_pos in seen:
                    continue
                seen.add(feet_pos)
                scanned += 1
                candidate = self._surface_candidate_at(feet_pos, world_top_y=world_top_y)
                if isinstance(candidate, ToolResult):
                    return candidate
                if candidate["candidate"]:
                    entry = dict(candidate)
                    entry["support_mode"] = "natural"
                    candidates.append(entry)
                elif allow_constructible and feet_pos[1] > origin[1]:
                    ascent_origin = (feet_pos[0], origin[1], feet_pos[2])
                    constructible = self._constructible_surface_column_at(
                        ascent_origin,
                        target_y=feet_pos[1],
                        world_top_y=world_top_y,
                    )
                    if isinstance(constructible, ToolResult):
                        return constructible
                    if constructible["constructible"]:
                        entry = dict(candidate)
                        entry["candidate"] = True
                        entry["support_mode"] = "constructible_pillar"
                        entry["ascent_origin"] = list(ascent_origin)
                        entry["column_plan"] = constructible
                        candidates.append(entry)
                if len(candidates) >= max_candidates:
                    return {
                        "candidates": candidates,
                        "scanned": scanned,
                        "complete": False,
                        "exhaustion_reason": "surface_goal_limit",
                        "max_candidates": max_candidates,
                        "constructible_allowed": allow_constructible,
                    }
        return {
            "candidates": candidates,
            "scanned": scanned,
            "complete": True,
            "exhaustion_reason": None,
            "max_candidates": max_candidates,
            "constructible_allowed": allow_constructible,
        }

    def _find_surface_egress_domain(
        self,
        origin: Position,
        *,
        radius: int,
        y_below: int,
        y_above: int,
        max_candidates: int,
    ) -> dict[str, object] | ToolResult:
        y_min = origin[1] - y_below
        y_max = origin[1] + y_above
        columns = tuple(
            (origin[0] + dx, origin[2] + dz)
            for dx in range(-radius, radius + 1)
            for dz in range(-radius, radius + 1)
            if dx * dx + dz * dz <= radius * radius
        )
        scan_positions = tuple(
            (x, y, z)
            for x, z in columns
            for y in range(y_min - 1, y_max + 2)
        )
        try:
            facts = read_block_facts(self.body, scan_positions, failure_label="surface_egress")
        except ValueError as exc:
            return ToolResult(
                success=False,
                reason="surface_egress_world_read_failed",
                can_retry=True,
                next_suggestion="retry the bounded shore-domain read before surface navigation",
                metrics={"origin": list(origin), "error": str(exc)},
            )

        def fact_at(pos: Position) -> PerceptionResult | None:
            return facts.get(pos)

        candidates: list[dict[str, object]] = []
        for x, z in columns:
            for y in range(y_min, y_max + 1):
                feet_pos = (x, y, z)
                head_pos = (x, y + 1, z)
                support_pos = (x, y - 1, z)
                feet = fact_at(feet_pos)
                head = fact_at(head_pos)
                support = fact_at(support_pos)
                if feet is None or head is None or support is None:
                    continue
                if not (
                    _is_clear_perception(feet)
                    and _is_clear_perception(head)
                    and _is_solid_support_perception(support)
                ):
                    continue
                water_adjacent = any(
                    self._is_liquid_perception(fact)
                    for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1))
                    for fact in (
                        fact_at((x + dx, y, z + dz)),
                        fact_at((x + dx, y - 1, z + dz)),
                    )
                    if fact is not None
                )
                if not water_adjacent:
                    continue
                candidates.append(
                    {
                        "feet_pos": list(feet_pos),
                        "head_pos": list(head_pos),
                        "support_pos": list(support_pos),
                        "support_block": _normalize_item(str(support.data.get("type") or "unknown")),
                        "distance": dist(origin, feet_pos),
                    }
                )

        candidates.sort(
            key=lambda entry: (
                float(entry["distance"]),
                abs(int(entry["feet_pos"][1]) - origin[1]),
                tuple(entry["feet_pos"]),
            )
        )
        complete = len(candidates) <= max_candidates
        return {
            "candidates": candidates[:max_candidates],
            "candidate_count": len(candidates),
            "complete": complete,
            "exhaustion_reason": None if complete else "surface_egress_goal_limit",
            "radius": radius,
            "y_min": y_min,
            "y_max": y_max,
            "scanned_cells": len(scan_positions),
            "max_candidates": max_candidates,
        }

    def _find_lateral_surface_domain(
        self,
        origin: Position,
        *,
        ring_specs: tuple[tuple[int, int], ...],
        max_candidates: int,
    ) -> dict[str, object] | ToolResult:
        sampled = _surface_lateral_columns(origin, ring_specs)
        columns = tuple((entry["x"], entry["z"]) for entry in sampled)
        try:
            facts = read_surface_columns(self.body, columns, failure_label="surface_lateral")
        except ValueError as exc:
            return ToolResult(
                success=False,
                reason="surface_lateral_world_read_failed",
                can_retry=True,
                next_suggestion="retry the bounded lateral surface-domain read",
                metrics={"origin": list(origin), "error": str(exc), "column_count": len(columns)},
            )

        raw_candidates: list[dict[str, object]] = []
        rejection_counts = {
            "feet_not_clear": 0,
            "head_not_clear": 0,
            "support_not_solid": 0,
            "support_governance": 0,
        }
        for sample in sampled:
            fact = facts[(sample["x"], sample["z"])]
            if fact.feet_state != "CLEAR":
                rejection_counts["feet_not_clear"] += 1
                continue
            if fact.head_state != "CLEAR":
                rejection_counts["head_not_clear"] += 1
                continue
            if fact.support_state != "SOLID":
                rejection_counts["support_not_solid"] += 1
                continue
            raw_candidates.append(
                {
                    "feet_pos": list(fact.feet_pos),
                    "head_pos": list(fact.head_pos),
                    "support_pos": list(fact.support_pos),
                    "support_block": _normalize_item(fact.support_type),
                    "feet_state": fact.feet_state,
                    "head_state": fact.head_state,
                    "support_state": fact.support_state,
                    "distance": round(dist(origin, fact.feet_pos), 3),
                    "vertical_delta": fact.feet_y - origin[1],
                    "ring_radius": sample["ring_radius"],
                    "ring_index": sample["ring_index"],
                    "sample_index": sample["sample_index"],
                }
            )
        raw_candidates.sort(
            key=lambda entry: (
                abs(int(entry["vertical_delta"])),
                float(entry["distance"]),
                int(entry["ring_index"]),
                int(entry["sample_index"]),
            )
        )

        candidates: list[dict[str, object]] = []
        evaluated = 0
        selected_vertical_effort: int | None = None
        selected_tier_count = 0
        selected_tier_evaluated = 0
        vertical_efforts = sorted({abs(int(entry["vertical_delta"])) for entry in raw_candidates})
        for vertical_effort in vertical_efforts:
            tier = [entry for entry in raw_candidates if abs(int(entry["vertical_delta"])) == vertical_effort]
            tier_candidates: list[dict[str, object]] = []
            tier_evaluated = 0
            for entry in tier:
                evaluated += 1
                tier_evaluated += 1
                support_pos = tuple(entry["support_pos"])
                decision = self.governance.can_stand(support_pos, str(entry["support_block"]))
                entry["support_legality"] = _decision_payload(decision)
                entry["sky_exposure"] = {"exposed": True, "source": "surface_heightmap"}
                entry["support_mode"] = "natural"
                entry["candidate"] = decision.allowed
                if not entry["candidate"]:
                    rejection_counts["support_governance"] += 1
                    continue
                tier_candidates.append(entry)
                if len(tier_candidates) >= max_candidates:
                    break
            if tier_candidates:
                candidates = tier_candidates
                selected_vertical_effort = vertical_effort
                selected_tier_count = len(tier)
                selected_tier_evaluated = tier_evaluated
                break
        complete = selected_vertical_effort is None or selected_tier_evaluated >= selected_tier_count
        return {
            "candidates": candidates,
            "candidate_count": len(candidates),
            "raw_candidate_count": len(raw_candidates),
            "complete": complete,
            "exhaustion_reason": None if complete else "surface_lateral_goal_limit",
            "selection_strategy": "minimum_vertical_effort_tier",
            "selected_vertical_effort": selected_vertical_effort,
            "selected_tier_count": selected_tier_count,
            "deferred_candidate_count": max(0, len(raw_candidates) - evaluated),
            "column_count": len(columns),
            "ring_specs": [list(spec) for spec in ring_specs],
            "max_candidates": max_candidates,
            "rejection_counts": rejection_counts,
            "source": "surfaceColumns",
        }

    def _standable_feet_at(self, feet_pos: Position) -> dict[str, object] | ToolResult:
        head_pos = (feet_pos[0], feet_pos[1] + 1, feet_pos[2])
        support_pos = (feet_pos[0], feet_pos[1] - 1, feet_pos[2])
        try:
            facts = read_block_facts(self.body, (feet_pos, head_pos, support_pos), failure_label="surface_stand")
        except ValueError:
            return self._standable_feet_at_fallback(feet_pos)
        feet = facts.get(feet_pos)
        head = facts.get(head_pos)
        support = facts.get(support_pos)
        if feet is None or head is None or support is None:
            return self._standable_feet_at_fallback(feet_pos)
        failed = _perception_failure(feet)
        if failed is not None:
            return failed
        failed = _perception_failure(head)
        if failed is not None:
            return failed
        failed = _perception_failure(support)
        if failed is not None:
            return failed
        return self._standable_feet_payload(feet_pos, head_pos, support_pos, feet, head, support)

    def _standable_feet_at_fallback(self, feet_pos: Position) -> dict[str, object] | ToolResult:
        feet = self.body.perceive("blockAt", _block_params(feet_pos))
        failed = _perception_failure(feet)
        if failed is not None:
            return failed
        head_pos = (feet_pos[0], feet_pos[1] + 1, feet_pos[2])
        head = self.body.perceive("blockAt", _block_params(head_pos))
        failed = _perception_failure(head)
        if failed is not None:
            return failed
        support_pos = (feet_pos[0], feet_pos[1] - 1, feet_pos[2])
        support = self.body.perceive("blockAt", _block_params(support_pos))
        failed = _perception_failure(support)
        if failed is not None:
            return failed
        return self._standable_feet_payload(feet_pos, head_pos, support_pos, feet, head, support)

    def _standable_feet_payload(
        self,
        feet_pos: Position,
        head_pos: Position,
        support_pos: Position,
        feet: PerceptionResult,
        head: PerceptionResult,
        support: PerceptionResult,
    ) -> dict[str, object]:
        return {
            "standable": _is_clear_perception(feet)
            and _is_clear_perception(head)
            and _is_solid_support_perception(support),
            "feet_pos": list(feet_pos),
            "head_pos": list(head_pos),
            "support_pos": list(support_pos),
            "feet_state": str(feet.data.get("state") or "UNKNOWN"),
            "head_state": str(head.data.get("state") or "UNKNOWN"),
            "support_state": str(support.data.get("state") or "UNKNOWN"),
            "support_block": _normalize_item(str(support.data.get("type") or "unknown")),
        }

    def _constructible_surface_column_at(
        self,
        ascent_origin: Position,
        *,
        target_y: int,
        world_top_y: int,
    ) -> dict[str, object] | ToolResult:
        stand = self._standable_feet_at(ascent_origin)
        if isinstance(stand, ToolResult):
            return stand
        if not stand["standable"]:
            return {"constructible": False, "reason": "ascent_origin_not_standable", "stand": stand}

        scan_positions = tuple((ascent_origin[0], y, ascent_origin[2]) for y in range(ascent_origin[1] + 1, target_y + 2))
        try:
            facts = read_block_facts(self.body, scan_positions, failure_label="surface_column")
        except ValueError:
            return self._constructible_surface_column_at_fallback(
                ascent_origin,
                target_y=target_y,
                world_top_y=world_top_y,
                stand=stand,
            )

        checks: list[dict[str, object]] = []
        for pos in scan_positions:
            perception = facts.get(pos)
            if perception is None:
                return self._constructible_surface_column_at_fallback(
                    ascent_origin,
                    target_y=target_y,
                    world_top_y=world_top_y,
                    stand=stand,
                )
            failed = _perception_failure(perception)
            if failed is not None:
                return failed
            block_type = _normalize_item(str(perception.data.get("type") or "unknown"))
            state = str(perception.data.get("state") or "UNKNOWN")
            check = {"pos": list(pos), "block_type": block_type, "state": state}
            if self._is_liquid_perception(perception):
                check["clearable"] = False
                check["reason"] = "liquid"
                checks.append(check)
                return {"constructible": False, "reason": "liquid_in_column", "stand": stand, "checks": checks}
            if _is_clear_perception(perception):
                check["clearable"] = True
                check["reason"] = "already_clear"
                checks.append(check)
                continue
            decision = self.governance.can_break(pos, block_type, BreakContext.DIRECT)
            check["legality"] = _decision_payload(decision)
            check["clearable"] = decision.allowed
            checks.append(check)
            if not decision.allowed:
                return {
                    "constructible": False,
                    "reason": f"blocked:{decision.reason}",
                    "stand": stand,
                    "checks": checks,
                }

        sky = self.sky_exposed((ascent_origin[0], target_y, ascent_origin[2]), world_top_y=world_top_y)
        if isinstance(sky, ToolResult):
            return sky
        return {
            "constructible": bool(sky["exposed"]),
            "reason": "constructible" if sky["exposed"] else "sky_blocked_after_target",
            "stand": stand,
            "checks": checks,
            "sky_exposure": sky,
        }

    def _constructible_surface_column_at_fallback(
        self,
        ascent_origin: Position,
        *,
        target_y: int,
        world_top_y: int,
        stand: dict[str, object],
    ) -> dict[str, object] | ToolResult:
        checks: list[dict[str, object]] = []
        for y in range(ascent_origin[1] + 1, target_y + 2):
            pos = (ascent_origin[0], y, ascent_origin[2])
            perception = self.body.perceive("blockAt", _block_params(pos))
            failed = _perception_failure(perception)
            if failed is not None:
                return failed
            block_type = _normalize_item(str(perception.data.get("type") or "unknown"))
            state = str(perception.data.get("state") or "UNKNOWN")
            check = {"pos": list(pos), "block_type": block_type, "state": state}
            if self._is_liquid_perception(perception):
                check["clearable"] = False
                check["reason"] = "liquid"
                checks.append(check)
                return {"constructible": False, "reason": "liquid_in_column", "stand": stand, "checks": checks}
            if _is_clear_perception(perception):
                check["clearable"] = True
                check["reason"] = "already_clear"
                checks.append(check)
                continue
            decision = self.governance.can_break(pos, block_type, BreakContext.DIRECT)
            check["legality"] = _decision_payload(decision)
            check["clearable"] = decision.allowed
            checks.append(check)
            if not decision.allowed:
                return {
                    "constructible": False,
                    "reason": f"blocked:{decision.reason}",
                    "stand": stand,
                    "checks": checks,
                }

        sky = self.sky_exposed((ascent_origin[0], target_y, ascent_origin[2]), world_top_y=world_top_y)
        if isinstance(sky, ToolResult):
            return sky
        return {
            "constructible": bool(sky["exposed"]),
            "reason": "constructible" if sky["exposed"] else "sky_blocked_after_target",
            "stand": stand,
            "checks": checks,
            "sky_exposure": sky,
        }

    def _surface_candidate_at(
        self,
        feet_pos: Position,
        *,
        world_top_y: int,
    ) -> dict[str, object] | ToolResult:
        head_pos = (feet_pos[0], feet_pos[1] + 1, feet_pos[2])
        below_pos = (feet_pos[0], feet_pos[1] - 1, feet_pos[2])
        try:
            facts = read_block_facts(self.body, (feet_pos, head_pos, below_pos), failure_label="surface_candidate")
        except ValueError:
            return self._surface_candidate_at_fallback(feet_pos, world_top_y=world_top_y)
        feet = facts.get(feet_pos)
        head = facts.get(head_pos)
        below = facts.get(below_pos)
        if feet is None or head is None or below is None:
            return self._surface_candidate_at_fallback(feet_pos, world_top_y=world_top_y)
        failed = _perception_failure(feet)
        if failed is not None:
            return failed
        failed = _perception_failure(head)
        if failed is not None:
            return failed
        failed = _perception_failure(below)
        if failed is not None:
            return failed

        support_type = str(below.data.get("type") or "unknown")
        support_legality = self.governance.can_stand(below_pos, support_type)
        sky = self.sky_exposed(feet_pos, world_top_y=world_top_y)
        if isinstance(sky, ToolResult):
            return sky

        return self._surface_candidate_payload(feet_pos, head_pos, below_pos, feet, head, below, support_legality, sky)

    def _surface_candidate_at_fallback(
        self,
        feet_pos: Position,
        *,
        world_top_y: int,
    ) -> dict[str, object] | ToolResult:
        feet = self.body.perceive("blockAt", _block_params(feet_pos))
        failed = _perception_failure(feet)
        if failed is not None:
            return failed
        head_pos = (feet_pos[0], feet_pos[1] + 1, feet_pos[2])
        head = self.body.perceive("blockAt", _block_params(head_pos))
        failed = _perception_failure(head)
        if failed is not None:
            return failed
        below_pos = (feet_pos[0], feet_pos[1] - 1, feet_pos[2])
        below = self.body.perceive("blockAt", _block_params(below_pos))
        failed = _perception_failure(below)
        if failed is not None:
            return failed

        support_type = str(below.data.get("type") or "unknown")
        support_legality = self.governance.can_stand(below_pos, support_type)
        sky = self.sky_exposed(feet_pos, world_top_y=world_top_y)
        if isinstance(sky, ToolResult):
            return sky

        return self._surface_candidate_payload(feet_pos, head_pos, below_pos, feet, head, below, support_legality, sky)

    def _surface_candidate_payload(
        self,
        feet_pos: Position,
        head_pos: Position,
        below_pos: Position,
        feet: PerceptionResult,
        head: PerceptionResult,
        below: PerceptionResult,
        support_legality,
        sky: dict[str, object],
    ) -> dict[str, object]:
        support_type = str(below.data.get("type") or "unknown")
        return {
            "candidate": (
                _is_clear_perception(feet)
                and _is_clear_perception(head)
                and _is_solid_support_perception(below)
                and support_legality.allowed
                and sky["exposed"]
            ),
            "feet_pos": list(feet_pos),
            "head_pos": list(head_pos),
            "support_pos": list(below_pos),
            "support_block": _normalize_item(support_type),
            "feet_state": str(feet.data.get("state") or "UNKNOWN"),
            "head_state": str(head.data.get("state") or "UNKNOWN"),
            "support_state": str(below.data.get("state") or "UNKNOWN"),
            "support_legality": _decision_payload(support_legality),
            "sky_exposure": sky,
        }

    def sky_exposed(self, feet_pos: Position, *, world_top_y: int) -> dict[str, object] | ToolResult:
        """Return whether the column above ``feet_pos`` is truly sky exposed.

        This is the public P1.3 primitive: one bounded authoritative batch read
        replaces the old per-cell upward walk. The returned payload is reused by
        `go_to_surface` and other surface-search helpers.
        """

        return self._sky_exposure_above(feet_pos, world_top_y=world_top_y)

    def _sky_exposure_above(self, feet_pos: Position, *, world_top_y: int) -> dict[str, object] | ToolResult:
        scan_positions = tuple((feet_pos[0], y, feet_pos[2]) for y in range(feet_pos[1] + 2, world_top_y + 1))
        try:
            facts = read_block_facts(self.body, scan_positions, failure_label="sky_exposure")
        except ValueError:
            return self._sky_exposure_above_fallback(feet_pos, world_top_y=world_top_y)

        first_blocker: dict[str, object] | None = None
        for pos in scan_positions:
            perception = facts.get(pos)
            if perception is None:
                return self._sky_exposure_above_fallback(feet_pos, world_top_y=world_top_y)
            failed = _perception_failure(perception)
            if failed is not None:
                return failed
            if not _is_clear_perception(perception):
                first_blocker = {
                    "pos": list(pos),
                    "block_type": _normalize_item(str(perception.data.get("type") or "unknown")),
                    "block_state": str(perception.data.get("state") or "UNKNOWN"),
                }
                break
        return {
            "exposed": first_blocker is None,
            "world_top_y": world_top_y,
            "first_blocker": first_blocker,
        }

    def _sky_exposure_above_fallback(self, feet_pos: Position, *, world_top_y: int) -> dict[str, object] | ToolResult:
        first_blocker: dict[str, object] | None = None
        for y in range(feet_pos[1] + 2, world_top_y + 1):
            pos = (feet_pos[0], y, feet_pos[2])
            perception = self.body.perceive("blockAt", _block_params(pos))
            failed = _perception_failure(perception)
            if failed is not None:
                return failed
            if not _is_clear_perception(perception):
                first_blocker = {
                    "pos": list(pos),
                    "block_type": _normalize_item(str(perception.data.get("type") or "unknown")),
                    "block_state": str(perception.data.get("state") or "UNKNOWN"),
                }
                break
        return {
            "exposed": first_blocker is None,
            "world_top_y": world_top_y,
            "first_blocker": first_blocker,
        }

    def _approach_seal_face(self, pos: Position, *, timeout_s: float) -> ToolResult | None:
        if self.navigator is None:
            return ToolResult(
                success=False,
                reason="dry_mining_seal_approach_runtime_missing",
                can_retry=True,
                next_suggestion="attach a Body navigation transaction before sealing a liquid face",
                metrics={"target": list(pos)},
            )
        result = self.navigator.navigate_to(
            pos,
            break_context=BreakContext.TRAVEL,
        )
        if result.success:
            return None
        return ToolResult(
            success=False,
            reason="dry_mining_seal_approach_failed",
            can_retry=result.can_retry,
            next_suggestion=result.next_suggestion,
            metrics={"target": list(pos), "navigation": result.to_payload()},
        )

    def place_block(
        self,
        pos: Position,
        block_type: str,
        *,
        face: str | None = None,
        context: PlaceContext | str = PlaceContext.WORK,
        purpose: str = "scaffold",
        allow_replace_liquid: bool = False,
        timeout_s: float = 30.0,
    ) -> ToolResult:
        target_block = self.body.perceive("blockAt", {"x": pos[0], "y": pos[1], "z": pos[2]})
        failed = _perception_failure(target_block)
        if failed is not None:
            return failed

        target_state = str(target_block.data.get("state") or "UNKNOWN")
        target_type = str(target_block.data.get("type") or "unknown")
        replacing_liquid = allow_replace_liquid and self._is_liquid_perception(target_block)
        if target_state != "CLEAR" and not replacing_liquid:
            return ToolResult(
                success=False,
                reason="place_denied:target_occupied",
                can_retry=False,
                next_suggestion="choose an empty target position or mine the target first if governance allows it",
                metrics={
                    "target": list(pos),
                    "block_type": block_type,
                    "block_at_target": target_type,
                    "target_state": target_state,
                },
            )

        collision = _placement_collision(self.body.get_state().pos, pos, purpose=purpose)
        if collision is not None:
            return collision

        decision = self.governance.can_place(pos, block_type, context, self.body.bot_name)
        if not decision.allowed:
            return _denied_result("place_denied", pos, block_type, decision)

        action = Action.create(
            "placeBlock",
            {
                "target": list(pos),
                "block_type": block_type,
                "face": face,
                "context": PlaceContext(context).value,
                "purpose": purpose,
                "replace_liquid": replacing_liquid,
                "timeout_ticks": _seconds_to_ticks(timeout_s),
                "legality": _decision_payload(decision),
            },
        )
        accepted = self.body.execute(action)
        rejected = _acceptance_failure(accepted, "placeBlock", pos)
        if rejected is not None:
            return rejected

        terminal = self.body.await_action_terminal(action.id, timeout_s=_server_terminal_timeout(timeout_s))
        result = terminal_event_to_tool_result(terminal)
        if result.success:
            self.governance.record_bot_placement(pos, block_type, purpose, self.body.bot_name)
        metrics = dict(result.metrics or {})
        metrics.setdefault("target", list(pos))
        metrics.setdefault("block_type", block_type)
        metrics.setdefault("block_before", target_type)
        metrics["legality"] = _decision_payload(decision)
        return ToolResult(
            success=result.success,
            reason=result.reason,
            can_retry=result.can_retry,
            next_suggestion=result.next_suggestion,
            metrics=metrics,
        )

    def place_here(
        self,
        block_type: str,
        *,
        radius: int = 1,
        context: PlaceContext | str = PlaceContext.WORK,
        purpose: str = "scaffold",
        timeout_s: float = 30.0,
    ) -> ToolResult:
        """Place one block at a nearby supported top-face candidate.

        This is a bounded Body transaction for the old `!placeHere` family:
        scan a small local neighborhood around the bot, find a clear target with
        solid support directly below, and try candidates in order without asking
        the Brain to micro-pick exact coordinates. Same-level surfaces remain the
        preferred domain; when that level has no supported target, a bounded
        vertical band discovers nearby natural banks and ledges.

        It now includes two narrow work-position recoveries before giving up:
        clear one recoverable adjacent head block, or carve one recoverable
        adjacent stand block. It intentionally does not yet claim broader side
        pockets, richer face recovery, or general work-position planning.
        """

        state = self.body.get_state()
        origin = _state_block_pos(state.pos)
        if radius < 1:
            raise ValueError("radius must be >= 1")

        scan_result = _scan_place_here_candidates(
            self.body,
            origin,
            radius,
            vertical_radius=self.PLACE_HERE_VERTICAL_RADIUS,
            column_limit=self.PLACE_HERE_COLUMN_LIMIT,
            candidate_limit=self.PLACE_HERE_CANDIDATE_LIMIT,
        )
        if isinstance(scan_result, ToolResult):
            return scan_result
        scan = scan_result.candidates
        scan_diagnostics = scan_result.diagnostics

        supported = [candidate for candidate in scan if candidate["candidate"]]
        attempts: list[dict[str, object]] = []
        if not supported:
            return ToolResult(
                success=False,
                reason="place_here_no_supported_spot",
                can_retry=True,
                next_suggestion="move to a clearer nearby area or extend the local search radius before retrying placement",
                metrics={
                    "origin": list(origin),
                    "radius": radius,
                    "block_type": block_type,
                    "purpose": purpose,
                    "candidates": scan,
                    "scan": scan_diagnostics,
                },
            )

        stand_position_recovery: dict[str, object] | None = None
        headroom_recovery: dict[str, object] | None = None
        standable = [candidate for candidate in supported if candidate["has_stand_point"]]
        if not standable:
            recovery = self._recover_place_here_stand_position(supported, timeout_s=timeout_s)
            if isinstance(recovery, ToolResult):
                return recovery
            stand_position_recovery = recovery
            if recovery.get("recovered"):
                scan_result = _scan_place_here_candidates(
                    self.body,
                    origin,
                    radius,
                    vertical_radius=self.PLACE_HERE_VERTICAL_RADIUS,
                    column_limit=self.PLACE_HERE_COLUMN_LIMIT,
                    candidate_limit=self.PLACE_HERE_CANDIDATE_LIMIT,
                )
                if isinstance(scan_result, ToolResult):
                    return scan_result
                scan = scan_result.candidates
                scan_diagnostics = scan_result.diagnostics
                supported = [candidate for candidate in scan if candidate["candidate"]]
                standable = [candidate for candidate in supported if candidate["has_stand_point"]]
        if not standable:
            recovery = self._recover_place_here_headroom(supported, timeout_s=timeout_s)
            if isinstance(recovery, ToolResult):
                return recovery
            headroom_recovery = recovery
            if recovery.get("recovered"):
                scan_result = _scan_place_here_candidates(
                    self.body,
                    origin,
                    radius,
                    vertical_radius=self.PLACE_HERE_VERTICAL_RADIUS,
                    column_limit=self.PLACE_HERE_COLUMN_LIMIT,
                    candidate_limit=self.PLACE_HERE_CANDIDATE_LIMIT,
                )
                if isinstance(scan_result, ToolResult):
                    return scan_result
                scan = scan_result.candidates
                scan_diagnostics = scan_result.diagnostics
                supported = [candidate for candidate in scan if candidate["candidate"]]
                standable = [candidate for candidate in supported if candidate["has_stand_point"]]
        if not standable:
            return ToolResult(
                success=False,
                reason="place_here_no_stand_point",
                can_retry=True,
                next_suggestion="clear headroom or move to a better local stance before retrying placement",
                metrics={
                    "origin": list(origin),
                    "radius": radius,
                    "block_type": block_type,
                    "purpose": purpose,
                    "candidates": scan,
                    "scan": scan_diagnostics,
                    "stand_position_recovery": stand_position_recovery,
                    "headroom_recovery": headroom_recovery,
                },
            )

        navigation_needed = False
        navigation_missing = False
        navigation_failures: list[dict[str, object]] = []
        for candidate in standable:
            pos = tuple(candidate["target"])
            stand_points = [tuple(point) for point in candidate["stand_points"]]
            current_feet = _state_block_pos(self.body.get_state().pos)
            approach: dict[str, object] | None = None
            if current_feet not in stand_points:
                navigation_needed = True
                if self.navigator is None:
                    navigation_missing = True
                    attempts.append(
                        {
                            "target": list(pos),
                            "stand_points": [list(point) for point in stand_points],
                            "result": ToolResult(
                                success=False,
                                reason="place_here_navigation_missing",
                                can_retry=True,
                                next_suggestion="attach a navigation transaction before using distant placement stand points",
                            ).to_payload(),
                        }
                    )
                    continue
                approach_result = self._approach_place_candidate(stand_points, pos, timeout_s=timeout_s)
                if isinstance(approach_result, ToolResult):
                    navigation_failures.append(
                        {
                            "target": list(pos),
                            "result": approach_result.to_payload(),
                        }
                    )
                    attempts.append(
                        {
                            "target": list(pos),
                            "stand_points": [list(point) for point in stand_points],
                            "result": approach_result.to_payload(),
                        }
                    )
                    continue
                approach = approach_result

            result = self.place_block(
                pos,
                block_type,
                face="up",
                context=context,
                purpose=purpose,
                timeout_s=timeout_s,
            )
            attempts.append(
                {
                    "target": list(pos),
                    "stand_points": [list(point) for point in stand_points],
                    "approach": approach,
                    "result": result.to_payload(),
                }
            )
            if result.success:
                return _with_metric(
                    result,
                    "place_here",
                    {
                        "origin": list(origin),
                        "radius": radius,
                        "chosen_target": list(pos),
                        "candidates": scan,
                        "scan": scan_diagnostics,
                        "attempts": attempts,
                        "stand_position_recovery": stand_position_recovery,
                        "headroom_recovery": headroom_recovery,
                    },
                )

            if not _place_here_retryable_reason(result.reason):
                return _with_metric(
                    result,
                    "place_here",
                    {
                        "origin": list(origin),
                        "radius": radius,
                        "candidates": scan,
                        "scan": scan_diagnostics,
                        "attempts": attempts,
                        "stand_position_recovery": stand_position_recovery,
                        "headroom_recovery": headroom_recovery,
                    },
                )

        if navigation_missing:
            return ToolResult(
                success=False,
                reason="place_here_navigation_missing",
                can_retry=True,
                next_suggestion="attach a navigation transaction or move to a valid stand point before retrying placement",
                metrics={
                    "origin": list(origin),
                    "radius": radius,
                    "block_type": block_type,
                    "purpose": purpose,
                    "candidates": scan,
                    "scan": scan_diagnostics,
                    "attempts": attempts,
                    "stand_position_recovery": stand_position_recovery,
                    "headroom_recovery": headroom_recovery,
                },
            )

        if navigation_needed and navigation_failures and len(navigation_failures) == len(standable):
            last = navigation_failures[-1]["result"]["reason"]
            return ToolResult(
                success=False,
                reason=f"place_here_navigation_failed:{last}",
                can_retry=True,
                next_suggestion="retry from a better local position or verify the stand point remains reachable",
                metrics={
                    "origin": list(origin),
                    "radius": radius,
                    "block_type": block_type,
                    "purpose": purpose,
                    "candidates": scan,
                    "scan": scan_diagnostics,
                    "attempts": attempts,
                    "stand_position_recovery": stand_position_recovery,
                    "headroom_recovery": headroom_recovery,
                },
            )

        return ToolResult(
            success=False,
            reason="place_here_no_placeable_spot",
            can_retry=True,
            next_suggestion="move slightly, change the search radius, or choose a more specific placement target before retrying",
            metrics={
                "origin": list(origin),
                "radius": radius,
                "block_type": block_type,
                "purpose": purpose,
                "candidates": scan,
                "scan": scan_diagnostics,
                "attempts": attempts,
                "stand_position_recovery": stand_position_recovery,
                "headroom_recovery": headroom_recovery,
            },
        )

    def _liquid_contact_positions(self, pos: Position) -> "_LiquidScan":
        return self._scan_liquid_offsets(pos, self.LIQUID_CONTACT_OFFSETS)

    def _liquid_face_positions(self, pos: Position) -> "_LiquidScan":
        return self._scan_liquid_offsets(pos, self.SEAL_FACE_OFFSETS)

    def _scan_liquid_offsets(self, pos: Position, offsets: tuple[Position, ...]) -> "_LiquidScan":
        scan_positions = tuple(_offset_pos(pos, offset) for offset in offsets)
        try:
            facts = read_block_facts(self.body, scan_positions, failure_label="liquid_scan")
        except ValueError as exc:
            return _LiquidScan(
                positions=[],
                failed=ToolResult(
                    success=False,
                    reason="perception_failed",
                    can_retry=True,
                    next_suggestion="re-perceive the target block before mutating the world",
                    metrics={"scope": "blockCells", "ok": False, "complete": False, "error": str(exc), "uncertainty": None},
                ),
            )
        liquid: list[Position] = []
        for scan_pos in scan_positions:
            perception = facts.get(scan_pos)
            if perception is not None and self._is_liquid_perception(perception):
                liquid.append(scan_pos)
        return _LiquidScan(liquid, None)

    def _is_liquid_perception(self, perception: PerceptionResult) -> bool:
        block_type = str(perception.data.get("type") or "").removeprefix("minecraft:")
        block_state = str(perception.data.get("state") or "")
        return block_type in self.LIQUID_TYPES or block_state in self.LIQUID_STATES

    def _approach_place_candidate(
        self,
        stand_points: list[Position] | tuple[Position, ...],
        target: Position,
        *,
        timeout_s: float,
    ) -> dict[str, object] | ToolResult:
        if self.navigator is None:
            return ToolResult(
                success=False,
                reason="place_here_navigation_missing",
                can_retry=True,
                next_suggestion="attach a navigation transaction before approaching a placement stand point",
                metrics={"target": list(target)},
            )

        attempts: list[dict[str, object]] = []
        for stand in stand_points:
            nav_result = self.navigator.navigate_to(
                stand,
                timeout_s=timeout_s,
                break_context=BreakContext.TRAVEL,
                arrival_radius=0.25,
            )
            if not nav_result.success:
                attempts.append({"goal": list(stand), "result": nav_result.to_payload()})
                continue
            final_pos = _state_block_pos(self.body.get_state().pos)
            if final_pos == stand:
                attempts.append({"goal": list(stand), "result": nav_result.to_payload(), "final_feet": list(final_pos)})
                return {
                    "navigated": True,
                    "stand_target": list(stand),
                    "attempts": attempts,
                }
            attempts.append(
                {
                    "goal": list(stand),
                    "result": nav_result.to_payload(),
                    "final_feet": list(final_pos),
                    "stand_verified": False,
                    "reason": "stand_point_missed",
                }
            )
        return ToolResult(
            success=False,
            reason="stand_point_unreachable",
            can_retry=True,
            next_suggestion="retry from a clearer area or choose another supported local spot",
            metrics={
                "target": list(target),
                "stand_points": [list(point) for point in stand_points],
                "attempts": attempts,
            },
        )

    def _recover_place_here_headroom(
        self,
        supported_candidates: list[dict[str, object]],
        *,
        timeout_s: float,
    ) -> dict[str, object] | ToolResult:
        attempts: list[dict[str, object]] = []
        for candidate in supported_candidates:
            target = tuple(candidate["target"])
            recoverable = self._recoverable_place_stands(target)
            if isinstance(recoverable, ToolResult):
                return recoverable
            for option in recoverable:
                head_pos = tuple(option["head_pos"])
                clear = self.mine_block(head_pos, context=BreakContext.DIRECT, timeout_s=timeout_s)
                attempts.append(
                    {
                        "target": list(target),
                        "stand_pos": list(option["stand_pos"]),
                        "head_pos": list(head_pos),
                        "head_block": option["head_block"],
                        "clear_result": clear.to_payload(),
                    }
                )
                if clear.success:
                    return {
                        "recovered": True,
                        "target": list(target),
                        "stand_pos": list(option["stand_pos"]),
                        "head_pos": list(head_pos),
                        "attempts": attempts,
                    }
        return {"recovered": False, "attempts": attempts}

    def _recover_place_here_stand_position(
        self,
        supported_candidates: list[dict[str, object]],
        *,
        timeout_s: float,
    ) -> dict[str, object] | ToolResult:
        attempts: list[dict[str, object]] = []
        for candidate in supported_candidates:
            target = tuple(candidate["target"])
            recoverable = self._recoverable_place_side_pockets(target)
            if isinstance(recoverable, ToolResult):
                return recoverable
            for option in recoverable:
                stand_pos = tuple(option["stand_pos"])
                clear = self.mine_block(stand_pos, context=BreakContext.DIRECT, timeout_s=timeout_s)
                attempts.append(
                    {
                        "target": list(target),
                        "stand_pos": list(stand_pos),
                        "stand_block": option["stand_block"],
                        "head_pos": list(option["head_pos"]),
                        "head_block": option["head_block"],
                        "clear_result": clear.to_payload(),
                    }
                )
                if clear.success:
                    return {
                        "recovered": True,
                        "target": list(target),
                        "stand_pos": list(stand_pos),
                        "head_pos": list(option["head_pos"]),
                        "attempts": attempts,
                    }
        return {"recovered": False, "attempts": attempts}

    def _recoverable_place_stands(self, target: Position) -> list[dict[str, object]] | ToolResult:
        state = self.body.get_state()
        candidates: list[tuple[float, dict[str, object]]] = []
        for stand_pos in _mining_stand_candidates(target):
            stand = self.body.perceive("blockAt", _block_params(stand_pos))
            failed = _perception_failure(stand)
            if failed is not None:
                return failed
            below_pos = (stand_pos[0], stand_pos[1] - 1, stand_pos[2])
            below = self.body.perceive("blockAt", _block_params(below_pos))
            failed = _perception_failure(below)
            if failed is not None:
                return failed
            head_pos = (stand_pos[0], stand_pos[1] + 1, stand_pos[2])
            head = self.body.perceive("blockAt", _block_params(head_pos))
            failed = _perception_failure(head)
            if failed is not None:
                return failed

            if not _is_clear_perception(stand):
                continue
            if not _is_solid_support_perception(below):
                continue
            if _is_clear_perception(head):
                continue

            head_type = str(head.data.get("type") or "unknown")
            decision = self.governance.can_break(head_pos, head_type, BreakContext.DIRECT)
            if not decision.allowed:
                continue
            payload = {
                "stand_pos": list(stand_pos),
                "head_pos": list(head_pos),
                "head_block": _normalize_item(head_type),
                "legality": _decision_payload(decision),
            }
            candidates.append((_distance_to_block_center(state.pos, stand_pos), payload))
        candidates.sort(key=lambda item: (item[0], item[1]["stand_pos"]))
        return [payload for _distance, payload in candidates]

    def _recoverable_place_side_pockets(self, target: Position) -> list[dict[str, object]] | ToolResult:
        state = self.body.get_state()
        candidates: list[tuple[float, dict[str, object]]] = []
        for stand_pos in _mining_stand_candidates(target):
            stand = self.body.perceive("blockAt", _block_params(stand_pos))
            failed = _perception_failure(stand)
            if failed is not None:
                return failed
            below_pos = (stand_pos[0], stand_pos[1] - 1, stand_pos[2])
            below = self.body.perceive("blockAt", _block_params(below_pos))
            failed = _perception_failure(below)
            if failed is not None:
                return failed
            head_pos = (stand_pos[0], stand_pos[1] + 1, stand_pos[2])
            head = self.body.perceive("blockAt", _block_params(head_pos))
            failed = _perception_failure(head)
            if failed is not None:
                return failed

            if _is_clear_perception(stand):
                continue
            if not _is_solid_support_perception(below):
                continue

            head_type = str(head.data.get("type") or "unknown")
            if not _is_clear_perception(head):
                head_decision = self.governance.can_break(head_pos, head_type, BreakContext.DIRECT)
                if not head_decision.allowed:
                    continue

            stand_type = str(stand.data.get("type") or "unknown")
            decision = self.governance.can_break(stand_pos, stand_type, BreakContext.DIRECT)
            if not decision.allowed:
                continue

            payload = {
                "stand_pos": list(stand_pos),
                "stand_block": _normalize_item(stand_type),
                "head_pos": list(head_pos),
                "head_block": _normalize_item(head_type),
                "legality": _decision_payload(decision),
            }
            candidates.append((_distance_to_block_center(state.pos, stand_pos), payload))
        candidates.sort(key=lambda item: (item[0], item[1]["stand_pos"]))
        return [payload for _distance, payload in candidates]

    def _scan_start_liquid(self, origin: Position) -> ToolResult | None:
        for label, pos in (("feet", origin), ("head", (origin[0], origin[1] + 1, origin[2]))):
            perception = self.body.perceive("blockAt", _block_params(pos))
            failed = _perception_failure(perception)
            if failed is not None:
                return _with_metric(failed, "dig_down", {"origin": list(origin), "phase": f"start_{label}"})
            if self._is_liquid_perception(perception):
                return ToolResult(
                    success=False,
                    reason="dig_down_start_liquid",
                    can_retry=False,
                    next_suggestion="leave or seal the liquid before starting a downward shaft",
                    metrics={
                        "origin": list(origin),
                        "liquid_pos": list(pos),
                        "liquid_part": label,
                        "block_type": _normalize_item(str(perception.data.get("type") or "unknown")),
                    },
                )
        return None

    def _fall_probe_after_opening(self, target_pos: Position, *, max_clear_fall: int) -> "_FallProbe":
        scan_positions = tuple(
            (target_pos[0], target_pos[1] - 1 - offset, target_pos[2])
            for offset in range(max_clear_fall)
        )
        try:
            facts = read_block_facts(self.body, scan_positions, failure_label="fall_probe")
        except ValueError:
            return self._fall_probe_after_opening_fallback(target_pos, max_clear_fall=max_clear_fall)

        clear_depth = 1
        for pos in scan_positions:
            perception = facts.get(pos)
            if perception is None:
                return self._fall_probe_after_opening_fallback(target_pos, max_clear_fall=max_clear_fall)
            failed = _perception_failure(perception)
            if failed is not None:
                return _FallProbe(clear_depth, None, None, None, failed)
            if self._is_liquid_perception(perception):
                return _FallProbe(clear_depth, None, None, pos, None)
            if _is_clear_perception(perception):
                clear_depth += 1
                if clear_depth > max_clear_fall:
                    return _FallProbe(clear_depth, None, None, None, None)
                continue
            support_type = _normalize_item(str(perception.data.get("type") or "unknown"))
            return _FallProbe(clear_depth, pos, support_type, None, None)

        return _FallProbe(clear_depth, None, None, None, None)

    def _fall_probe_after_opening_fallback(self, target_pos: Position, *, max_clear_fall: int) -> "_FallProbe":
        clear_depth = 1
        scan_y = target_pos[1] - 1
        while True:
            pos = (target_pos[0], scan_y, target_pos[2])
            perception = self.body.perceive("blockAt", _block_params(pos))
            failed = _perception_failure(perception)
            if failed is not None:
                return _FallProbe(clear_depth, None, None, None, failed)
            if self._is_liquid_perception(perception):
                return _FallProbe(clear_depth, None, None, pos, None)
            if _is_clear_perception(perception):
                clear_depth += 1
                if clear_depth > max_clear_fall:
                    return _FallProbe(clear_depth, None, None, None, None)
                scan_y -= 1
                continue
            support_type = _normalize_item(str(perception.data.get("type") or "unknown"))
            return _FallProbe(clear_depth, pos, support_type, None, None)

    def _place_first_available_seal_block(
        self,
        pos: Position,
        *,
        seal_blocks: tuple[str, ...],
        timeout_s: float,
    ) -> ToolResult:
        attempts: list[dict[str, object]] = []
        for block_type in seal_blocks:
            result = self.place_block(
                pos,
                block_type,
                context=PlaceContext.WORK,
                purpose="seal",
                allow_replace_liquid=True,
                timeout_s=timeout_s,
            )
            attempts.append(
                {
                    "block_type": block_type,
                    "success": result.success,
                    "reason": result.reason,
                    "can_retry": result.can_retry,
                }
            )
            if result.success:
                return result
            if not result.can_retry and not result.reason.startswith("body_rejected"):
                return _with_metric(result, "seal_attempts", attempts)
        return ToolResult(
            success=False,
            reason="dry_mining_seal_failed",
            can_retry=True,
            next_suggestion="verify the bot has a usable seal block and can place into the liquid face",
            metrics={"target": list(pos), "seal_attempts": attempts},
        )


class _LiquidScan:
    def __init__(self, positions: list[Position], failed: ToolResult | None) -> None:
        self.positions = positions
        self.failed = failed


class _FallProbe:
    def __init__(
        self,
        clear_depth: int,
        support_pos: Position | None,
        support_type: str | None,
        liquid_landing: Position | None,
        failed: ToolResult | None,
    ) -> None:
        self.clear_depth = clear_depth
        self.support_pos = support_pos
        self.support_type = support_type
        self.liquid_landing = liquid_landing
        self.failed = failed


class _PlaceHereScan:
    def __init__(self, candidates: list[dict[str, object]], diagnostics: dict[str, object]) -> None:
        self.candidates = candidates
        self.diagnostics = diagnostics


def _perception_failure(perception: PerceptionResult) -> ToolResult | None:
    if perception.ok and perception.complete:
        return None
    return ToolResult(
        success=False,
        reason="perception_failed",
        can_retry=True,
        next_suggestion="re-perceive the target block before mutating the world",
        metrics={
            "scope": perception.scope,
            "ok": perception.ok,
            "complete": perception.complete,
            "error": perception.error,
            "uncertainty": perception.uncertainty,
        },
    )


def _acceptance_failure(result: Result, action_name: str, pos: Position) -> ToolResult | None:
    if result.ok and result.accepted:
        return None
    return ToolResult(
        success=False,
        reason="body_rejected",
        can_retry=True,
        metrics={
            "action": action_name,
            "target": list(pos),
            "ok": result.ok,
            "accepted": result.accepted,
            "error": result.error,
            "data": result.data,
        },
    )


def _denied_result(prefix: str, pos: Position, block_type: str, decision) -> ToolResult:
    return ToolResult(
        success=False,
        reason=f"{prefix}:{decision.reason}",
        can_retry=False,
        next_suggestion="choose another target or register a safe work region/provenance before mutating this block",
        metrics={
            "target": list(pos),
            "block_type": block_type,
            "legality": _decision_payload(decision),
        },
    )


def _decision_payload(decision) -> dict[str, object]:
    payload = asdict(decision)
    payload["allowed"] = bool(decision.allowed)
    return payload


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
    return ToolResult(
        success=False,
        reason="body_rejected",
        can_retry=True,
        metrics={
            "action": "selectItem",
            "item": item,
            "ok": accepted.ok,
            "accepted": accepted.accepted,
            "error": accepted.error,
            "data": accepted.data,
        },
    )


def _normalize_item(item: str) -> str:
    return item.removeprefix("minecraft:")


def _body_fits_shaft(
    pos: tuple[float, float, float],
    shaft: Position,
    *,
    half_width: float = 0.3,
    edge_margin: float = 0.02,
) -> bool:
    inset = half_width + edge_margin
    return (
        shaft[0] + inset <= pos[0] <= shaft[0] + 1.0 - inset
        and shaft[2] + inset <= pos[2] <= shaft[2] + 1.0 - inset
    )




def _is_clear_perception(perception: PerceptionResult) -> bool:
    block_type = _normalize_item(str(perception.data.get("type") or "unknown"))
    block_state = str(perception.data.get("state") or "UNKNOWN")
    return block_type == "air" or block_state == "CLEAR"


def _placement_collision(
    body_pos: tuple[float, float, float],
    target: Position,
    *,
    purpose: str,
) -> ToolResult | None:
    feet = _state_block_pos(body_pos)
    head = (feet[0], feet[1] + 1, feet[2])
    if target == head:
        return ToolResult(
            success=False,
            reason="place_denied:body_collision",
            can_retry=False,
            next_suggestion="step away or lower the target before placing a solid block into the bot's head space",
            metrics={
                "target": list(target),
                "body_feet": list(feet),
                "body_head": list(head),
                "collision_part": "head",
                "purpose": purpose,
            },
        )
    if target == feet and purpose != "pillar":
        return ToolResult(
            success=False,
            reason="place_denied:body_collision",
            can_retry=False,
            next_suggestion="step off the target block before placing into the bot's occupied foot space",
            metrics={
                "target": list(target),
                "body_feet": list(feet),
                "body_head": list(head),
                "collision_part": "feet",
                "purpose": purpose,
            },
        )
    return None


def _scan_place_here_candidates(
    body: Body,
    origin: Position,
    radius: int,
    *,
    vertical_radius: int,
    column_limit: int,
    candidate_limit: int,
) -> _PlaceHereScan | ToolResult:
    if vertical_radius < 0:
        raise ValueError("vertical_radius must be >= 0")
    if column_limit < 1:
        raise ValueError("column_limit must be >= 1")
    if candidate_limit < 1:
        raise ValueError("candidate_limit must be >= 1")

    all_columns = _place_here_columns(origin, radius)
    columns = all_columns[:column_limit]
    rejection_counts: dict[str, int] = {}
    rejection_samples: list[dict[str, object]] = []
    scanned_position_count = 0

    same_level_targets = tuple((x, origin[1], z) for x, z in columns)
    same_level = _read_place_here_surfaces(body, same_level_targets)
    if isinstance(same_level, ToolResult):
        return same_level
    scanned_position_count += same_level[1]
    supported = _place_here_supported_surfaces(
        same_level[0],
        rejection_counts=rejection_counts,
        rejection_samples=rejection_samples,
    )
    vertical_fallback = not supported and vertical_radius > 0

    if vertical_fallback:
        offsets = tuple(offset for delta in range(1, vertical_radius + 1) for offset in (delta, -delta))
        vertical_targets = tuple((x, origin[1] + offset, z) for offset in offsets for x, z in columns)
        vertical = _read_place_here_surfaces(body, vertical_targets)
        if isinstance(vertical, ToolResult):
            return vertical
        scanned_position_count += vertical[1]
        supported = _place_here_supported_surfaces(
            vertical[0],
            rejection_counts=rejection_counts,
            rejection_samples=rejection_samples,
        )

    supported.sort(key=lambda item: _place_here_surface_sort_key(origin, item[0]))
    supported_total = len(supported)
    supported = supported[:candidate_limit]
    candidates: list[dict[str, object]] = []
    for target, target_block, below_block in supported:
        below = (target[0], target[1] - 1, target[2])
        stand_points_result = interaction_stand_points(body, target)
        if isinstance(stand_points_result, ToolResult):
            return stand_points_result
        candidates.append(
            {
                "target": list(target),
                "support": list(below),
                "target_block": _normalize_item(str(target_block.data.get("type") or "unknown")),
                "target_state": str(target_block.data.get("state") or "UNKNOWN"),
                "support_block": _normalize_item(str(below_block.data.get("type") or "unknown")),
                "support_state": str(below_block.data.get("state") or "UNKNOWN"),
                "vertical_delta": target[1] - origin[1],
                "candidate": True,
                "stand_points": [list(point) for point in stand_points_result],
                "has_stand_point": bool(stand_points_result),
            }
        )

    return _PlaceHereScan(
        candidates,
        {
            "radius": radius,
            "vertical_radius": vertical_radius,
            "vertical_fallback": vertical_fallback,
            "columns_total": len(all_columns),
            "columns_scanned": len(columns),
            "columns_complete": len(columns) == len(all_columns),
            "column_limit": column_limit,
            "scanned_position_count": scanned_position_count,
            "supported_total": supported_total,
            "candidate_count": len(candidates),
            "candidate_limit": candidate_limit,
            "candidates_complete": supported_total <= candidate_limit,
            "rejection_counts": rejection_counts,
            "rejection_samples": rejection_samples,
        },
    )


def _read_place_here_surfaces(
    body: Body,
    targets: tuple[Position, ...],
) -> tuple[list[tuple[Position, PerceptionResult, PerceptionResult]], int] | ToolResult:
    wanted = tuple(dict.fromkeys((*targets, *((target[0], target[1] - 1, target[2]) for target in targets))))
    try:
        facts = read_block_facts(body, wanted, failure_label="place_here_surface")
    except ValueError as exc:
        return ToolResult(
            success=False,
            reason="perception_failed",
            can_retry=True,
            next_suggestion="refresh nearby terrain facts before retrying placement",
            metrics={
                "scope": "blockCells",
                "ok": False,
                "complete": False,
                "error": str(exc),
                "uncertainty": None,
            },
        )
    return [
        (target, facts[target], facts[(target[0], target[1] - 1, target[2])])
        for target in targets
    ], len(wanted)


def _place_here_supported_surfaces(
    surfaces: list[tuple[Position, PerceptionResult, PerceptionResult]],
    *,
    rejection_counts: dict[str, int],
    rejection_samples: list[dict[str, object]],
) -> list[tuple[Position, PerceptionResult, PerceptionResult]]:
    supported: list[tuple[Position, PerceptionResult, PerceptionResult]] = []
    for target, target_block, below_block in surfaces:
        target_clear = _is_clear_perception(target_block)
        support_solid = _is_solid_support_perception(below_block)
        if target_clear and support_solid:
            supported.append((target, target_block, below_block))
            continue
        if not target_clear:
            reason = "target_not_clear"
        elif str(below_block.data.get("state") or "UNKNOWN") == "LIQUID":
            reason = "support_liquid"
        else:
            reason = "support_not_solid"
        rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
        if len(rejection_samples) < 8:
            rejection_samples.append(
                {
                    "target": list(target),
                    "target_block": _normalize_item(str(target_block.data.get("type") or "unknown")),
                    "target_state": str(target_block.data.get("state") or "UNKNOWN"),
                    "support_block": _normalize_item(str(below_block.data.get("type") or "unknown")),
                    "support_state": str(below_block.data.get("state") or "UNKNOWN"),
                    "reason": reason,
                }
            )
    return supported


def _is_solid_support_perception(perception: PerceptionResult) -> bool:
    block_state = str(perception.data.get("state") or "UNKNOWN")
    return block_state == "SOLID"


def _place_here_columns(origin: Position, radius: int) -> tuple[tuple[int, int], ...]:
    candidates: list[tuple[int, float, int, int]] = []
    for dz in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            if dx == 0 and dz == 0:
                continue
            manhattan = abs(dx) + abs(dz)
            distance = (dx * dx + dz * dz) ** 0.5
            candidates.append((manhattan, distance, origin[0] + dx, origin[2] + dz))
    candidates.sort(key=lambda item: (item[0], item[1], item[3], item[2]))
    return tuple((x, z) for _manhattan, _distance, x, z in candidates)


def _place_here_surface_sort_key(origin: Position, target: Position) -> tuple[float, int, int, int, int]:
    horizontal = abs(target[0] - origin[0]) + abs(target[2] - origin[2])
    return (
        dist(
            (float(origin[0]), float(origin[1]), float(origin[2])),
            (float(target[0]), float(target[1]), float(target[2])),
        ),
        abs(target[1] - origin[1]),
        horizontal,
        target[2],
        target[0],
    )


def _surface_scan_targets(origin: Position, y: int, *, radius: int = 1) -> tuple[Position, ...]:
    if radius <= 0:
        return ((origin[0], y, origin[2]),)
    if radius == 1:
        return (
            (origin[0], y, origin[2]),
            (origin[0] + 1, y, origin[2]),
            (origin[0] - 1, y, origin[2]),
            (origin[0], y, origin[2] + 1),
            (origin[0], y, origin[2] - 1),
        )

    candidates: list[tuple[int, int, int, Position]] = []
    for dz in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            manhattan = abs(dx) + abs(dz)
            if manhattan > radius:
                continue
            pos = (origin[0] + dx, y, origin[2] + dz)
            candidates.append((manhattan, 0 if dx >= 0 else 1, abs(dz), pos))
    candidates.sort(key=lambda item: (item[0], item[1], item[2], item[3][2], item[3][0]))
    return tuple(pos for _manhattan, _x_bias, _z_abs, pos in candidates)


def _surface_lateral_columns(
    origin: Position,
    ring_specs: tuple[tuple[int, int], ...],
) -> tuple[dict[str, int], ...]:
    columns: list[dict[str, int]] = []
    seen: set[tuple[int, int]] = set()
    for ring_index, (radius, samples) in enumerate(ring_specs):
        if radius < 1 or samples < 1:
            continue
        perimeter = 8 * radius
        for sample_index in range(samples):
            distance = sample_index * perimeter // samples
            side, offset = divmod(distance, 2 * radius)
            if side == 0:
                dx, dz = -radius + offset, -radius
            elif side == 1:
                dx, dz = radius, -radius + offset
            elif side == 2:
                dx, dz = radius - offset, radius
            else:
                dx, dz = -radius, radius - offset
            column = (origin[0] + dx, origin[2] + dz)
            if column in seen:
                continue
            seen.add(column)
            columns.append(
                {
                    "x": column[0],
                    "z": column[1],
                    "ring_radius": radius,
                    "ring_index": ring_index,
                    "sample_index": sample_index,
                }
            )
    return tuple(columns)


def _place_here_retryable_reason(reason: str) -> bool:
    return reason == "timeout" or reason.startswith("place_denied:")


def _state_block_pos(pos: tuple[float, float, float]) -> Position:
    return (floor(pos[0]), floor(pos[1]), floor(pos[2]))


def _block_params(pos: Position) -> dict[str, int]:
    return {"x": pos[0], "y": pos[1], "z": pos[2]}


def _normalize_block_type(block_type: str) -> str:
    return block_type.removeprefix("minecraft:")


def _seconds_to_ticks(seconds: float) -> int:
    return max(1, ceil(seconds * 20.0))


def _server_terminal_timeout(seconds: float) -> float:
    return seconds + 2.0


def _offset_pos(pos: Position, offset: Position) -> Position:
    return (pos[0] + offset[0], pos[1] + offset[1], pos[2] + offset[2])


def _mining_stand_candidates(pos: Position) -> tuple[Position, ...]:
    return (
        (pos[0], pos[1], pos[2] - 1),
        (pos[0], pos[1], pos[2] + 1),
        (pos[0] - 1, pos[1], pos[2]),
        (pos[0] + 1, pos[1], pos[2]),
    )


def _mining_approach_stand_candidates(pos: Position) -> tuple[Position, ...]:
    # Stand candidates are *feet* positions.  For a target block at y=N, a
    # same-level adjacent floor block supports feet at y=N+1; passing y=N to
    # moveTo asks the body to walk inside the floor block.  Wall/head targets
    # can also be mined from feet at y=N or y=N-1, so all nearby vertical bands
    # are offered and the standability check below filters impossible cells.
    bands = (
        (pos[0], pos[1] + 1, pos[2]),
        pos,
        (pos[0], pos[1] - 1, pos[2]),
    )
    candidates: list[Position] = []
    for band in bands:
        for candidate in _mining_stand_candidates(band):
            if candidate not in candidates:
                candidates.append(candidate)
    return tuple(candidates)


def _ranked_mining_stand_candidates(body: Body, pos: Position, current: tuple[float, float, float]) -> list[Position] | ToolResult:
    approach = _mining_approach_stand_candidates(pos)
    wanted: list[Position] = []
    for candidate in approach:
        wanted.append(candidate)
        wanted.append((candidate[0], candidate[1] - 1, candidate[2]))
        wanted.append((candidate[0], candidate[1] + 1, candidate[2]))
    try:
        facts = read_block_facts(body, tuple(wanted), failure_label="mining_stand")
    except ValueError as exc:
        return ToolResult(
            success=False,
            reason="perception_failed",
            can_retry=True,
            next_suggestion="re-perceive the target block before mutating the world",
            metrics={"scope": "blockCells", "ok": False, "complete": False, "error": str(exc), "uncertainty": None},
        )
    standable: list[Position] = []
    for candidate in approach:
        stand = facts.get(candidate)
        head = facts.get((candidate[0], candidate[1] + 1, candidate[2]))
        below = facts.get((candidate[0], candidate[1] - 1, candidate[2]))
        if stand is None or head is None or below is None:
            continue
        if _is_clear_perception(stand) and _is_solid_support_perception(below) and _is_clear_perception(head):
            standable.append(candidate)
    candidates = standable or list(approach)
    return sorted(candidates, key=lambda candidate: _mining_stand_sort_key(current, pos, candidate))


def _best_mining_stand_candidate(body: Body, pos: Position, current: tuple[float, float, float]) -> Position | ToolResult:
    candidates = _ranked_mining_stand_candidates(body, pos, current)
    if isinstance(candidates, ToolResult):
        return candidates
    if not candidates:
        return ToolResult(
            success=False,
            reason="mine_approach_failed:no_stand_candidate",
            can_retry=True,
            next_suggestion="choose another nearby target; no mining stand candidate was available",
            metrics={"target": list(pos)},
        )
    return candidates[0]


def _selected_mining_stand(result: ToolResult, candidates: list[Position]) -> Position:
    metrics = dict(result.metrics or {})
    raw = metrics.get("selected_goal", metrics.get("goal"))
    if isinstance(raw, (list, tuple)) and len(raw) >= 3:
        selected = (int(raw[0]), int(raw[1]), int(raw[2]))
        if selected in candidates:
            return selected
    return candidates[0]


def _selected_surface_goal(result: ToolResult, candidates: tuple[Position, ...]) -> Position:
    metrics = dict(result.metrics or {})
    raw = metrics.get("selected_goal", metrics.get("goal"))
    if isinstance(raw, (list, tuple)) and len(raw) >= 3:
        selected = (int(raw[0]), int(raw[1]), int(raw[2]))
        if selected in candidates:
            return selected
    return candidates[0]


def _surface_terminal_verified(surface: dict[str, object]) -> bool:
    return bool(surface.get("candidate"))


def _surface_requires_lateral_egress(surface: dict[str, object]) -> bool:
    sky_exposure = surface.get("sky_exposure") or {}
    return (
        surface.get("feet_state") == "LIQUID" or surface.get("head_state") == "LIQUID"
    ) and isinstance(sky_exposure, dict) and not bool(sky_exposure.get("exposed"))


def _distance_to_block_center(pos: tuple[float, float, float], target: Position) -> float:
    return dist(pos, (target[0] + 0.5, float(target[1]), target[2] + 0.5))


def _mining_stand_sort_key(current: tuple[float, float, float], target: Position, candidate: Position) -> tuple[float, float, float]:
    return (
        abs(float(candidate[1]) - current[1]),
        _distance_to_block_center(current, candidate),
        _mining_reach_distance((candidate[0] + 0.5, float(candidate[1]), candidate[2] + 0.5), target),
    )


def _mining_reach_distance(pos: tuple[float, float, float], target: Position) -> float:
    return dist(pos, (target[0] + 0.5, target[1] + 0.5, target[2] + 0.5))


def _block_center_target(pos: Position) -> tuple[float, float, float]:
    return (pos[0] + 0.5, float(pos[1]), pos[2] + 0.5)


def _pickup_fallback_positions(pos: Position) -> tuple[Position, ...]:
    px, py, pz = pos
    return (
        pos,
        (px - 1, py, pz),
        (px + 1, py, pz),
        (px, py, pz - 1),
        (px, py, pz + 1),
    )


def _same_column(pos: tuple[float, float, float], target: Position) -> bool:
    return floor(pos[0]) == target[0] and floor(pos[2]) == target[2]


def _is_ore_block(block_type: str) -> bool:
    return "ore" in block_type.removeprefix("minecraft:").split("/")[-1]


def _expected_drops_for_block(block_type: str, drop_map: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    normalized = _normalize_item(block_type)
    return tuple(_normalize_item(item) for item in drop_map.get(normalized, (normalized,)))


def _with_metric(result: ToolResult, key: str, value: object) -> ToolResult:
    metrics = dict(result.metrics or {})
    metrics[key] = value
    return ToolResult(
        success=result.success,
        reason=result.reason,
        can_retry=result.can_retry,
        next_suggestion=result.next_suggestion,
        metrics=metrics,
    )
