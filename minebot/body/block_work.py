"""Body transaction block work wrappers with governance guards."""

from __future__ import annotations

from dataclasses import asdict
from math import ceil, dist, floor
import time
from typing import Callable, Protocol

from minebot.body.interaction_support import (
    find_nearby_block_search,
    find_nearby_block_targets,
    find_block_target,
    interaction_stand_points,
    merge_context,
)
from minebot.body.world_read import read_block_facts
from minebot.contract import (
    Action,
    Body,
    BodyState,
    BreakContext,
    InventorySlot,
    PerceptionResult,
    PlaceContext,
    Position,
    Result,
    ToolResult,
    perception_next_cursor,
    terminal_event_to_tool_result,
)
from minebot.game.governance import GovernancePolicy


class SealFaceNavigator(Protocol):
    def navigate_to(self, goal: Position, **kwargs) -> ToolResult: ...


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
    # Bounded migration seam for COLLECT_APPROACH terrain clearing. The current
    # implementation clears the selected stand point before server-side
    # navigation; a future terrain-aware navigator can consume the same budget
    # when BREAK/DOWNWARD steps move fully into Scarpet.
    DIG_THROUGH_MAX_BREAK_STEPS = 8

    def __init__(
        self,
        body: Body,
        governance: GovernancePolicy,
        *,
        navigator: SealFaceNavigator | None = None,
        settle: Callable[[float], None] | None = None,
        mine_approach_settle_s: float = 0.3,
    ):
        self.body = body
        self.governance = governance
        self.navigator = navigator
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
    ) -> ToolResult:
        block = self.body.perceive("blockAt", {"x": pos[0], "y": pos[1], "z": pos[2]})
        failed = _perception_failure(block)
        if failed is not None:
            return failed

        block_type = str(block.data.get("type") or "unknown")
        decision = self.governance.can_break(pos, block_type, context)
        if not decision.allowed:
            return _denied_result("break_denied", pos, block_type, decision)

        approach_failed, approach_metrics = self._approach_mining_target(
            pos,
            context=context,
            target_block_type=block_type,
            timeout_s=timeout_s,
        )
        if approach_failed is not None:
            return approach_failed

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

        stand_candidates = _ranked_mining_stand_candidates(self.body, pos, state.pos)
        if isinstance(stand_candidates, ToolResult):
            return stand_candidates, None
        attempts: list[dict[str, object]] = []
        last_failure: ToolResult | None = None
        for index, stand_block in enumerate(stand_candidates):
            failure, metrics = self._approach_mining_target_from_stand(
                pos,
                stand_block,
                state=state,
                reach_limit=reach_limit,
                target_block_type=target_block_type,
                timeout_s=timeout_s,
            )
            if failure is None:
                if metrics is not None:
                    metrics["stand_candidate_index"] = index
                    metrics["stand_candidates_tried"] = len(attempts) + 1
                    if attempts:
                        metrics["stand_candidate_failures"] = attempts
                return None, metrics
            attempts.append(
                {
                    "stand_block": list(stand_block),
                    "reason": failure.reason,
                    "result": failure.to_payload(),
                }
            )
            last_failure = failure
            if _should_try_next_mining_stand(failure):
                continue
            break
        if last_failure is None:
            return ToolResult(
                success=False,
                reason="mine_approach_failed:no_stand_candidate",
                can_retry=True,
                next_suggestion="choose another nearby target; no mining stand candidate was available",
                metrics={"target": list(pos), "stand_candidate_failures": attempts},
            ), None
        return _with_metric(last_failure, "stand_candidate_failures", attempts), None

    def _approach_mining_target_from_stand(
        self,
        pos: Position,
        stand_block: Position,
        *,
        state: BodyState,
        reach_limit: float,
        target_block_type: str,
        timeout_s: float,
    ) -> tuple[ToolResult | None, dict[str, object] | None]:
        move_target = _block_center_target(stand_block)
        action = Action.create(
            "moveTo",
            {
                "target": list(move_target),
                "arrival_radius": 0.15,
                "timeout_ticks": 160,
                "no_progress_ticks": 60,
                "max_deviation": 2.0,
            },
        )
        accepted = self.body.execute(action)
        rejected = _acceptance_failure(accepted, "moveTo", stand_block)
        if rejected is not None:
            return _with_metric(
                rejected,
                "mine_approach",
                {"target": list(pos), "move_target": list(move_target), "stand_block": list(stand_block)},
            ), None

        terminal = self.body.await_action_terminal(action.id, timeout_s=_server_terminal_timeout(timeout_s))
        result = terminal_event_to_tool_result(terminal)
        if not result.success:
            # The lightweight bare moveTo could not reach a stand point next to the
            # target (stuck / timeout / deviated) — typically because the target is
            # buried and there is no pre-existing air pocket beside it. Escalate to
            # collect-approach clearance: clear the selected stand point under
            # COLLECT_APPROACH governance, then let the navigator move there.
            # Only if that fails is it an honest candidate-skip.
            dig = self._dig_through_approach(
                pos,
                stand_block,
                move_target,
                target_block_type=target_block_type,
                timeout_s=timeout_s,
            )
            if dig is not None:
                return dig
            return _with_metric(
                ToolResult(
                    success=False,
                    reason=f"mine_approach_failed:{result.reason}",
                    can_retry=True,
                    next_suggestion="choose another nearby target; this one has no reachable stand point",
                    metrics=dict(result.metrics or {}),
                ),
                "mine_approach",
                {"target": list(pos), "move_target": list(move_target), "stand_block": list(stand_block)},
            ), None

        self._pause(self._mine_approach_settle_s)
        after = self.body.get_state()
        reach = _mining_reach_distance(after.pos, pos)
        metrics = {
            "target": list(pos),
            "move_target": list(move_target),
            "stand_block": list(stand_block),
            "state_before": list(_state_block_pos(state.pos)),
            "state_after": list(_state_block_pos(after.pos)),
            "reach_distance": reach,
            "approach_settle_s": self._mine_approach_settle_s,
            "move_result": result.to_payload(),
        }
        if reach > reach_limit:
            # Arrived near the guessed stand cell but still out of mining reach —
            # try collect-approach clearance before giving up on the target.
            dig = self._dig_through_approach(
                pos,
                stand_block,
                move_target,
                target_block_type=target_block_type,
                timeout_s=timeout_s,
            )
            if dig is not None:
                return dig
            return ToolResult(
                success=False,
                reason="mine_approach_out_of_range",
                can_retry=True,
                next_suggestion="retry from a closer adjacent stand point before mining",
                metrics=metrics,
            ), None
        return None, metrics

    def _dig_through_approach(
        self,
        pos: Position,
        stand_block: Position,
        move_target: tuple[float, float, float],
        *,
        target_block_type: str,
        timeout_s: float,
    ) -> tuple[ToolResult | None, dict[str, object] | None] | None:
        """Escalate a failed lightweight approach to collect-approach clearance.

        Returns:
          - ``(None, metrics)`` when the navigator reached mining range (caller
            proceeds to mine);
          - ``(None, dig_metrics)`` when a navigator is wired and the dig-through
            RAN but did not bring the target into reach — the metrics carry the
            A* failure reason (``dig_through_result``) so the caller can surface
            *why* it failed, then fall back to its own honest candidate-skip;
          - ``None`` when no navigator is wired at all (caller skips directly).

        The server-side navigator currently walks/swims through passable cells
        only. Before delegating to it, this path clears the chosen stand point's
        feet/head cells under ``BreakContext.COLLECT_APPROACH`` so buried natural
        candidates can become reachable without letting the navigation controller
        break arbitrary terrain. The break budget is still passed through as the
        migration seam for a future Scarpet-side terrain-aware navigator.
        Governance refuses player/protected blocks before every mutation.
        """

        if self.navigator is None:
            return None
        # Bounded break budget so one buried target cannot consume the session.
        # Current navigate_to delegates primary pathfinding to Scarpet. The break
        # budget remains the migration seam for a future terrain-aware Body
        # navigator, but this Python path clears only the selected stand cell.
        from minebot.body.navigation import NavigationRunConfig  # local import: navigation.py imports BlockWork at module top
        from dataclasses import replace

        dig_config = NavigationRunConfig(
            max_break_steps=self.DIG_THROUGH_MAX_BREAK_STEPS,
            allow_local_terrain_fallback=not _is_log_block_type(target_block_type),
            progress_neutral_failures=True,
        )
        if timeout_s is not None:
            dig_config = replace(dig_config, segment_timeout_s=timeout_s)
        clearance_result = self._clear_collect_approach_stand(
            stand_block,
            target=pos,
            timeout_s=timeout_s,
        )
        if clearance_result is not None and not clearance_result.success:
            return ToolResult(
                success=False,
                reason=f"mine_approach_failed:dig_through:{clearance_result.reason}",
                can_retry=clearance_result.can_retry,
                next_suggestion=clearance_result.next_suggestion,
                metrics={
                    "target": list(pos),
                    "move_target": list(move_target),
                    "stand_block": list(stand_block),
                    "dig_through": True,
                    "dig_through_context": BreakContext.COLLECT_APPROACH.value,
                    "clearance": clearance_result.to_payload(),
                },
            ), None
        nav = self.navigator.navigate_to(
            stand_block,
            break_context=BreakContext.COLLECT_APPROACH,
            config=dig_config,
        )
        after = self.body.get_state()
        reach = _mining_reach_distance(after.pos, pos)
        metrics = {
            "target": list(pos),
            "move_target": list(move_target),
            "stand_block": list(stand_block),
            "state_after": list(_state_block_pos(after.pos)),
            "reach_distance": reach,
            "dig_through": True,
            "dig_through_context": BreakContext.COLLECT_APPROACH.value,
            "dig_through_max_break_steps": self.DIG_THROUGH_MAX_BREAK_STEPS,
            "dig_through_result": nav.to_payload(),
        }
        if clearance_result is not None:
            metrics["clearance"] = clearance_result.to_payload()
        if nav.success and reach <= self._mine_reach_limit(BreakContext.COLLECT):
            return None, metrics
        # Dig-through ran but still could not bring the target into reach. Return
        # an honest candidate-skip that CARRIES the A* failure diagnostics
        # (dig_through_result) — previously this returned bare None and the
        # reason was lost, leaving the orchestrator blind to *why* the
        # approach failed. The caller folds these metrics into its skip.
        return ToolResult(
            success=False,
            reason=f"mine_approach_failed:dig_through:{nav.reason}",
            can_retry=True,
            next_suggestion="choose another nearby target; dig-through could not reach this one",
            metrics=metrics,
        ), None

    def _mine_reach_limit(self, context: BreakContext | str) -> float:
        if BreakContext(context) is BreakContext.COLLECT:
            return self.MINE_INTERACTION_RANGE
        return self.DIRECT_MINE_REACH

    def _clear_collect_approach_stand(
        self,
        stand_block: Position,
        *,
        target: Position,
        timeout_s: float,
    ) -> ToolResult | None:
        """Clear natural blocks occupying the chosen mining stand.

        Server-side `navigateTo` currently handles executable WALK/SWIM nodes, but it
        deliberately does not break blocks. Keep the collect-approach break
        authority here, where governance can check each block before mutation.
        This is a narrow fallback for buried targets: clear the feet/head cells
        of the selected stand point, then let the normal navigator move there.
        """

        cleared: list[dict[str, object]] = []
        for candidate in (stand_block, (stand_block[0], stand_block[1] + 1, stand_block[2])):
            block = self.body.perceive("blockAt", _block_params(candidate))
            failed = _perception_failure(block)
            if failed is not None:
                return _with_metric(
                    failed,
                    "collect_approach_clearance",
                    {"stand_block": list(stand_block), "target": list(target), "cleared": cleared},
                )
            block_type = _normalize_item(str(block.data.get("type") or "unknown"))
            if _is_clear_perception(block):
                continue
            decision = self.governance.can_break(candidate, block_type, BreakContext.COLLECT_APPROACH)
            if not decision.allowed:
                return _with_metric(
                    _denied_result("break_denied", candidate, block_type, decision),
                    "collect_approach_clearance",
                    {"stand_block": list(stand_block), "target": list(target), "cleared": cleared},
                )
            action = Action.create(
                "mineBlock",
                {
                    "target": list(candidate),
                    "block_type": block_type,
                    "context": BreakContext.COLLECT_APPROACH.value,
                    "timeout_ticks": _seconds_to_ticks(timeout_s),
                    "legality": _decision_payload(decision),
                },
            )
            accepted = self.body.execute(action)
            rejected = _acceptance_failure(accepted, "mineBlock", candidate)
            if rejected is not None:
                mined = rejected
            else:
                terminal = self.body.await_action_terminal(action.id, timeout_s=_server_terminal_timeout(timeout_s))
                terminal_result = terminal_event_to_tool_result(terminal)
                mined = ToolResult(
                    success=terminal_result.success,
                    reason=terminal_result.reason,
                    can_retry=terminal_result.can_retry,
                    metrics={
                        **dict(terminal_result.metrics or {}),
                        "target": list(candidate),
                        "block_type": block_type,
                        "legality": _decision_payload(decision),
                    },
                )
            cleared.append({"pos": list(candidate), "block_type": block_type, "result": mined.to_payload()})
            if not mined.success:
                return _with_metric(
                    mined,
                    "collect_approach_clearance",
                    {"stand_block": list(stand_block), "target": list(target), "cleared": cleared},
                )
        if not cleared:
            return None
        return ToolResult(
            success=True,
            reason="collect_approach_cleared",
            can_retry=False,
            metrics={"stand_block": list(stand_block), "target": list(target), "cleared": cleared},
        )



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
            result = self.mine_block(pos, context=context, timeout_s=timeout_s)
            return _with_metric(result, "dry_mining", {"required": False, "block_state": block_state})

        liquid_touch = self._liquid_contact_positions(pos)
        if liquid_touch.failed is not None:
            return liquid_touch.failed

        if not liquid_touch.positions:
            result = self.mine_block(pos, context=context, timeout_s=timeout_s)
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

        mined = self.mine_block(pos, context=context, timeout_s=timeout_s)
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
        drop_map: dict[str, tuple[str, ...]] | None = None,
        settle_s: float = 0.2,
        pickup_timeout_s: float = 1.5,
        timeout_s: float = 30.0,
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
        expected = tuple(_normalize_item(item) for item in (expected_drops or ()))
        if not expected:
            expected = _expected_drops_for_block(block_type, drop_map or self.DEFAULT_DROP_MAP)

        before = _inventory_counts_from_body(self.body)
        if isinstance(before, ToolResult):
            return _with_metric(before, "collect", {"target": list(pos), "block_type": block_type, "phase": "before"})

        if dry:
            mined = self.mine_block_dry(pos, context=context, settle_s=settle_s, timeout_s=timeout_s)
        else:
            mined = self.mine_block(pos, context=context, timeout_s=timeout_s)
        if not mined.success:
            return _with_metric(
                mined,
                "collect",
                {
                    "target": list(pos),
                    "block_type": block_type,
                    "expected_drops": list(expected),
                    "before": before,
                },
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
            "expected_drops": list(expected),
            "before": before,
            "after": after,
            "deltas": deltas,
            "collected_total": collected_total,
            "mine_result": mined.to_payload(),
            "pickup_assist": pickup["assist"],
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

    def _collect_inventory_delta(
        self,
        *,
        pos: Position,
        before: dict[str, int],
        expected: tuple[str, ...],
        pickup_timeout_s: float,
    ) -> dict[str, object]:
        # Two real defects vs vanilla pickup mechanics shaped this method:
        #   1. A broken block's dropped item has a ~10-tick (0.5s) pickup delay
        #      during which NO entity (including the bot) may collect it. The
        #      wait window must outlast that delay, not race it.
        #   2. Vanilla auto-pickup range is only ~1 block, but the bot mines
        #      from up to its reach (~1.5 blocks). The drop rests in the now-air
        #      cell at `pos` (on top of pos.y-1); to collect, the bot must stand
        #      AT `pos` so the item sits in its feet-level pickup box. (The old
        #      walk target `pos.y-1` is the SOLID floor under the drop — the
        #      planner can never stand inside it, so the assist walk never fired.)
        # Hence: wait, then walk onto `pos`, then wait again. Inventory delta is
        # the single source of truth; the itemPickup event is telemetry only.
        assist: dict[str, object] = {"waited": False, "moved": False}

        def read() -> tuple[dict[str, int] | ToolResult, dict[str, int] | None, int]:
            counts = _inventory_counts_from_body(self.body)
            if isinstance(counts, ToolResult):
                return counts, None, 0
            d = {item: counts.get(item, 0) - before.get(item, 0) for item in expected}
            return counts, d, sum(max(0, v) for v in d.values())

        def poll(window_s: float) -> tuple[dict[str, int] | ToolResult, dict[str, int] | None, int]:
            counts, d, total = read()
            if isinstance(counts, ToolResult) or total > 0 or window_s <= 0:
                return counts, d, total
            assist["waited"] = True
            deadline = time.monotonic() + window_s
            while time.monotonic() < deadline:
                for event in self.body.poll_events():
                    if event.name == "itemPickup" and event.data.get("player") == self.body.bot_name:
                        if _normalize_item(str(event.data.get("item") or "unknown")) in expected:
                            assist["saw_pickup_event"] = True
                self._pause(0.10)
                counts, d, total = read()
                if isinstance(counts, ToolResult) or total > 0:
                    return counts, d, total
            return counts, d, total

        after, deltas, collected_total = poll(pickup_timeout_s)
        if isinstance(after, ToolResult):
            return {"failed": after, "assist": assist}
        if collected_total > 0:
            return {"after": after, "deltas": deltas, "collected_total": collected_total, "assist": assist}

        # pickup-B: walk to the actual drop ENTITY, not the guessed mined cell.
        # A log mined at trunk height drops an item that FALLS to the ground — it
        # is not at `pos` (the §8 walk-to-pos only works when the drop stays put,
        # e.g. surface dirt). Scarpet nearbyEntities gives each item entity's
        # exact pos; walk to it (TRAVEL — pure reposition, never dig the floor).
        # Try the nearest few drop entities in turn and only report no delta
        # after all are exhausted — do not abandon on the first unreachable one
        # (Mindcraft's pickupNearbyItems gives up if it can't reach the first
        # item; we keep trying). If no drop entity is visible yet, fall back to
        # the mined cell.
        if self.navigator is not None:
            assist["moved"] = True
            assist["pickup_arrival_radius"] = 0.25
            assist["scan_rounds"] = 0
            assist["move_attempts"] = []
            max_drop_targets_seen = 0
            for scan_round in range(2):
                assist["scan_rounds"] = scan_round + 1
                drop_positions = self._nearby_drop_positions(radius=8, limit=16)
                max_drop_targets_seen = max(max_drop_targets_seen, len(drop_positions))
                assist["drop_targets_seen"] = max_drop_targets_seen
                walk_targets = _pickup_walk_targets(pos, drop_positions)
                for target_pos in walk_targets[:5]:
                    nav = self.navigator.navigate_to(
                        target_pos,
                        break_context=BreakContext.TRAVEL,
                        arrival_radius=0.25,
                        timeout_s=4.0,
                    )
                    assist["move_result"] = nav.to_payload()
                    assist["move_attempts"].append({"target": list(target_pos), "result": nav.to_payload()})
                    if not nav.success:
                        continue
                    after, deltas, collected_total = poll(pickup_timeout_s)
                    if isinstance(after, ToolResult):
                        return {"failed": after, "assist": assist}
                    if collected_total > 0:
                        return {"after": after, "deltas": deltas, "collected_total": collected_total, "assist": assist}

        return {"after": after, "deltas": deltas, "collected_total": collected_total, "assist": assist}

    def _nearby_drop_positions(self, *, radius: int, limit: int) -> list[Position]:
        """Nearest-first positions of dropped-item entities near the bot.

        Powers the pickup assist: a mined block's drop may land away from the
        mined cell (logs fall), so we walk to where the item entity actually is.
        Returns [] on any perception error so the caller falls back to the mined
        cell — a pickup-assist read must never abort the collect.
        """
        nearby = self.body.perceive("nearbyEntities", {"radius": radius, "limit": limit})
        if not nearby.ok:
            return []
        positions: list[Position] = []
        for entity in nearby.data.get("entities") or []:
            if str(entity.get("type") or "") not in ("item", "minecraft:item"):
                continue
            pos_raw = entity.get("pos") or []
            if len(pos_raw) != 3:
                continue
            positions.append((float(pos_raw[0]), float(pos_raw[1]), float(pos_raw[2])))
        return positions

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
        allow_staircase_fallback: bool = False,
        world_top_y: int = 320,
    ) -> ToolResult:
        """Ascend toward the first verified nearby sky-exposed natural surface.

        This is the first honest `goToSurface` Body transaction layer:
        scan upward for the first nearby standable position whose support is
        natural/protected-safe and whose head has real sky exposure, use
        guarded pillar ascent to reach that Y layer, then step onto the natural
        surface stand point and re-verify the terminal truth.

        It does not yet implement the full multi-tier surface ladder from the
        canonical plan (broader scan geometry and hazard/recovery matrices).
        When explicitly enabled, the staircase fallback first asks shared
        navigation to route to a higher natural surface before falling back to
        guarded pillar ascent.
        """

        if surface_scan_height < 0:
            raise ValueError("surface_scan_height must be >= 0")
        if surface_scan_radius < 0:
            raise ValueError("surface_scan_radius must be >= 0")
        if world_top_y < -64:
            raise ValueError("world_top_y must be realistic")

        origin = current_pos or _state_block_pos(self.body.get_state().pos)
        scan = self._find_surface_in_column(
            origin,
            max_scan_height=surface_scan_height,
            scan_radius=surface_scan_radius,
            world_top_y=world_top_y,
        )
        if isinstance(scan, ToolResult):
            return _with_metric(scan, "go_to_surface", {"origin": list(origin)})
        if scan is None:
            return ToolResult(
                success=False,
                reason="surface_not_found_in_column",
                can_retry=True,
                next_suggestion="try another column or fall back to a staircase/surface-search transaction",
                metrics={
                    "origin": list(origin),
                    "surface_scan_height": surface_scan_height,
                    "surface_scan_radius": surface_scan_radius,
                    "world_top_y": world_top_y,
                },
            )

        target = tuple(scan["feet_pos"])
        ascent_origin = tuple(scan.get("ascent_origin") or origin)
        if origin == target:
            return ToolResult(
                success=True,
                reason="surface_reached",
                can_retry=False,
                metrics={
                    "origin": list(origin),
                    "target_surface": list(target),
                    "final_pos": list(origin),
                    "surface": scan,
                    "ascent": None,
                },
            )

        staircase_attempt: dict[str, object] | None = None
        if allow_staircase_fallback and target[1] > origin[1] and self.navigator is not None:
            route = self._approach_surface_candidate(
                target,
                origin=origin,
                timeout_s=timeout_s,
                world_top_y=world_top_y,
            )
            if isinstance(route, ToolResult):
                staircase_attempt = route.to_payload()
            else:
                final_pos = tuple(route["final_pos"])
                terminal = self._surface_candidate_at(final_pos, world_top_y=world_top_y)
                if isinstance(terminal, ToolResult):
                    return _with_metric(
                        terminal,
                        "go_to_surface",
                        {
                            "origin": list(origin),
                            "ascent_origin": list(origin),
                            "target_surface": list(target),
                            "surface": scan,
                            "staircase_fallback": route,
                        },
                    )
                if terminal["candidate"]:
                    return ToolResult(
                        success=True,
                        reason="surface_reached",
                        can_retry=False,
                        metrics={
                            "origin": list(origin),
                            "ascent_origin": list(origin),
                            "target_surface": list(target),
                            "final_pos": list(final_pos),
                            "surface": scan,
                            "terminal_surface": terminal,
                            "terminal_surface_verified": True,
                            "ascent": None,
                            "column_approach": None,
                            "approach": route,
                            "staircase_fallback": {
                                "attempted": True,
                                "success": True,
                                "result": route,
                            },
                        },
                    )
                staircase_attempt = {
                    "success": False,
                    "reason": "surface_verification_failed",
                    "result": route,
                    "terminal_surface": terminal,
                }

        column_approach: dict[str, object] | None = None
        ascent_start = origin
        if ascent_origin != origin and ascent_origin != target:
            column = self._approach_surface_column(
                ascent_origin,
                target_surface=target,
                origin=origin,
                timeout_s=timeout_s,
            )
            if isinstance(column, ToolResult):
                return ToolResult(
                    success=False,
                    reason=f"surface_column_failed:{column.reason}",
                    can_retry=column.can_retry,
                    next_suggestion=column.next_suggestion,
                    metrics={
                        "origin": list(origin),
                        "target_surface": list(target),
                        "ascent_origin": list(ascent_origin),
                        "surface": scan,
                        "staircase_fallback": staircase_attempt,
                        "column_approach": column.to_payload(),
                    },
                )
            column_approach = column
            ascent_start = tuple(column["final_pos"])

        ascent = self.dig_up_to_y(
            target[1],
            current_pos=ascent_start,
            context=context,
            scaffold_blocks=scaffold_blocks,
            timeout_s=timeout_s,
            max_steps=max_steps,
        )
        if not ascent.success:
            return _with_metric(
                ascent,
                "go_to_surface",
                {
                    "origin": list(origin),
                    "ascent_origin": list(ascent_origin),
                    "target_surface": list(target),
                    "surface": scan,
                    "staircase_fallback": staircase_attempt,
                    "column_approach": column_approach,
                },
            )

        final_pos = _state_block_pos(self.body.get_state().pos)
        approach: dict[str, object] | None = None
        if final_pos != target:
            if self.navigator is None:
                return ToolResult(
                    success=False,
                    reason="surface_navigation_missing",
                    can_retry=True,
                    next_suggestion="attach a navigation transaction before relying on an adjacent natural surface exit",
                    metrics={
                        "origin": list(origin),
                        "ascent_origin": list(ascent_origin),
                        "target_surface": list(target),
                        "final_pos": list(final_pos),
                        "surface": scan,
                        "staircase_fallback": staircase_attempt,
                        "column_approach": column_approach,
                        "ascent": ascent.to_payload(),
                    },
                )
            approach_result = self._approach_surface_candidate(
                target,
                origin=origin,
                timeout_s=timeout_s,
                world_top_y=world_top_y,
            )
            if isinstance(approach_result, ToolResult):
                return ToolResult(
                    success=False,
                    reason=f"surface_navigation_failed:{approach_result.reason}",
                    can_retry=approach_result.can_retry,
                    next_suggestion=approach_result.next_suggestion,
                    metrics={
                        "origin": list(origin),
                        "ascent_origin": list(ascent_origin),
                        "target_surface": list(target),
                        "final_pos": list(final_pos),
                        "surface": scan,
                        "staircase_fallback": staircase_attempt,
                        "column_approach": column_approach,
                        "ascent": ascent.to_payload(),
                        "surface_navigation": approach_result.to_payload(),
                    },
                )
            approach = approach_result
            final_pos = tuple(approach["final_pos"])

        terminal = self._surface_candidate_at(final_pos, world_top_y=world_top_y)
        if isinstance(terminal, ToolResult):
            return _with_metric(
                terminal,
                "go_to_surface",
                {
                    "origin": list(origin),
                    "ascent_origin": list(ascent_origin),
                    "target_surface": list(target),
                    "ascent": ascent.to_payload(),
                    "staircase_fallback": staircase_attempt,
                    "column_approach": column_approach,
                    "approach": approach,
                },
            )
        terminal_verified = bool(terminal["candidate"])
        if not terminal_verified and ascent_origin != origin and ascent_origin != target:
            support_legality = terminal.get("support_legality") or {}
            sky_exposure = terminal.get("sky_exposure") or {}
            terminal_verified = (
                terminal.get("feet_state") == "CLEAR"
                and terminal.get("head_state") == "CLEAR"
                and bool(sky_exposure.get("exposed"))
                and support_legality.get("reason") == "allowed_bot_owned"
                and terminal.get("support_pos") == [target[0], target[1] - 1, target[2]]
            )
        if not terminal_verified:
            return ToolResult(
                success=False,
                reason="surface_verification_failed",
                can_retry=True,
                next_suggestion="re-sync the body state or continue with a broader surface-search fallback",
                metrics={
                    "origin": list(origin),
                    "ascent_origin": list(ascent_origin),
                    "target_surface": list(target),
                    "final_pos": list(final_pos),
                    "surface": scan,
                    "terminal_surface": terminal,
                    "ascent": ascent.to_payload(),
                    "staircase_fallback": staircase_attempt,
                    "column_approach": column_approach,
                    "approach": approach,
                },
            )

        return ToolResult(
            success=True,
            reason="surface_reached",
            can_retry=False,
            metrics={
                "origin": list(origin),
                "ascent_origin": list(ascent_origin),
                "target_surface": list(target),
                "final_pos": list(final_pos),
                "surface": scan,
                "terminal_surface": terminal,
                "terminal_surface_verified": terminal_verified,
                "ascent": ascent.to_payload(),
                "staircase_fallback": staircase_attempt,
                "column_approach": column_approach,
                "approach": approach,
            },
        )

    def _approach_surface_column(
        self,
        ascent_origin: Position,
        *,
        target_surface: Position,
        origin: Position,
        timeout_s: float,
    ) -> dict[str, object] | ToolResult:
        if self.navigator is None:
            return ToolResult(
                success=False,
                reason="surface_navigation_missing",
                can_retry=True,
                next_suggestion="attach a navigation transaction before switching to an alternate surface ascent column",
                metrics={"ascent_origin": list(ascent_origin), "target_surface": list(target_surface)},
            )
        stand = self._standable_feet_at(ascent_origin)
        if isinstance(stand, ToolResult):
            return stand
        if not stand["standable"]:
            return ToolResult(
                success=False,
                reason="surface_column_not_standable",
                can_retry=True,
                next_suggestion="try another ascent column or a staircase fallback",
                metrics={
                    "origin": list(origin),
                    "ascent_origin": list(ascent_origin),
                    "target_surface": list(target_surface),
                    "stand": stand,
                },
            )
        nav_result = self.navigator.navigate_to(
            ascent_origin,
            timeout_s=timeout_s,
            break_context=BreakContext.TRAVEL,
            arrival_radius=0.25,
        )
        if not nav_result.success:
            return _with_metric(
                nav_result,
                "surface_column",
                {"origin": list(origin), "ascent_origin": list(ascent_origin), "target_surface": list(target_surface)},
            )
        final_pos = _state_block_pos(self.body.get_state().pos)
        if final_pos != ascent_origin:
            return ToolResult(
                success=False,
                reason="surface_column_missed",
                can_retry=True,
                next_suggestion="retry with a stricter or different ascent column approach",
                metrics={
                    "origin": list(origin),
                    "ascent_origin": list(ascent_origin),
                    "target_surface": list(target_surface),
                    "final_pos": list(final_pos),
                    "result": nav_result.to_payload(),
                    "stand": stand,
                },
            )
        return {
            "navigated": True,
            "ascent_origin": list(ascent_origin),
            "target_surface": list(target_surface),
            "final_pos": list(final_pos),
            "result": nav_result.to_payload(),
            "stand": stand,
        }

    def _approach_surface_candidate(
        self,
        target: Position,
        *,
        origin: Position,
        timeout_s: float,
        world_top_y: int,
    ) -> dict[str, object] | ToolResult:
        if self.navigator is None:
            return ToolResult(
                success=False,
                reason="surface_navigation_missing",
                can_retry=True,
                next_suggestion="attach a navigation transaction before relying on an adjacent natural surface exit",
                metrics={"target_surface": list(target)},
            )

        attempts: list[dict[str, object]] = []
        last_failure: ToolResult | None = None
        candidates = _surface_scan_targets((target[0], origin[1], target[2]), target[1])
        for candidate in candidates:
            nav_result = self.navigator.navigate_to(
                candidate,
                timeout_s=timeout_s,
                break_context=BreakContext.TRAVEL,
                arrival_radius=0.25,
            )
            if not nav_result.success:
                attempts.append({"goal": list(candidate), "result": nav_result.to_payload()})
                last_failure = nav_result
                continue
            final_pos = _state_block_pos(self.body.get_state().pos)
            terminal = self._surface_candidate_at(final_pos, world_top_y=world_top_y)
            if isinstance(terminal, ToolResult):
                return terminal
            attempt = {
                "goal": list(candidate),
                "result": nav_result.to_payload(),
                "final_pos": list(final_pos),
                "terminal_surface": terminal,
            }
            if terminal["candidate"]:
                attempts.append(attempt)
                return {
                    "navigated": True,
                    "target_surface": list(final_pos),
                    "requested_goal": list(candidate),
                    "requested_surface": list(target),
                    "result": nav_result.to_payload(),
                    "final_pos": list(final_pos),
                    "attempts": attempts,
                }
            attempt["surface_verified"] = False
            attempt["reason"] = "surface_point_missed" if final_pos != candidate else "surface_no_longer_valid"
            attempts.append(attempt)
            last_failure = ToolResult(
                success=False,
                reason=str(attempt["reason"]),
                can_retry=True,
                metrics=attempt,
            )

        reason = last_failure.reason if last_failure is not None else "surface_no_candidate"
        return ToolResult(
            success=False,
            reason=reason,
            can_retry=True,
            next_suggestion="retry with a broader surface-search fallback or stricter movement arrival",
            metrics={
                "target_surface": list(target),
                "candidate_surfaces": [list(candidate) for candidate in candidates],
                "attempts": attempts,
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
        }
        if search.errors:
            context["perception_errors"] = list(search.errors)

        if initial_distance <= interaction_radius:
            return ToolResult(
                success=True,
                reason="block_in_range",
                can_retry=False,
                metrics={**context, "initial_distance": initial_distance, "final_distance": initial_distance},
            )

        return ToolResult(
            success=True,
            reason="block_candidates_found",
            can_retry=False,
            metrics={**context, "initial_distance": initial_distance, "final_distance": initial_distance},
        )

    def _find_surface_in_column(
        self,
        origin: Position,
        *,
        max_scan_height: int,
        scan_radius: int = 1,
        world_top_y: int,
    ) -> dict[str, object] | ToolResult | None:
        top = min(origin[1] + max_scan_height, world_top_y - 1)
        for y in range(origin[1], top + 1):
            for feet_pos in _surface_scan_targets(origin, y, radius=scan_radius):
                if y > origin[1] and feet_pos[0] == origin[0] and feet_pos[2] == origin[2]:
                    continue
                candidate = self._surface_candidate_at(feet_pos, world_top_y=world_top_y)
                if isinstance(candidate, ToolResult):
                    return candidate
                if candidate["candidate"]:
                    candidate = dict(candidate)
                    if feet_pos[0] == origin[0] and feet_pos[2] == origin[2]:
                        candidate["ascent_origin"] = list(origin)
                    elif feet_pos[1] > origin[1]:
                        ascent_origin = (feet_pos[0], origin[1], feet_pos[2])
                        stand = self._standable_feet_at(ascent_origin)
                        if isinstance(stand, ToolResult):
                            return stand
                        candidate["ascent_origin"] = list(ascent_origin) if stand["standable"] else list(origin)
                        candidate["ascent_column_stand"] = stand
                    else:
                        candidate["ascent_origin"] = [feet_pos[0], origin[1], feet_pos[2]]
                    return candidate
                if feet_pos[1] > origin[1] and (feet_pos[0], feet_pos[2]) != (origin[0], origin[2]):
                    ascent_origin = (feet_pos[0], origin[1], feet_pos[2])
                    constructible = self._constructible_surface_column_at(
                        ascent_origin,
                        target_y=feet_pos[1],
                        world_top_y=world_top_y,
                    )
                    if isinstance(constructible, ToolResult):
                        return constructible
                    if constructible["constructible"]:
                        candidate = dict(candidate)
                        candidate["candidate"] = True
                        candidate["support_mode"] = "constructible_pillar"
                        candidate["ascent_origin"] = list(ascent_origin)
                        candidate["ascent_column_stand"] = constructible["stand"]
                        candidate["column_plan"] = constructible
                        return candidate
        return None

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
        support_legality = self.governance.can_break(below_pos, support_type, BreakContext.DIRECT)
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
        support_legality = self.governance.can_break(below_pos, support_type, BreakContext.DIRECT)
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
                and support_legality.reason == "allowed_natural"
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
        the Brain to micro-pick exact coordinates.

        It now includes two narrow work-position recoveries before giving up:
        clear one recoverable adjacent head block, or carve one recoverable
        adjacent stand block. It intentionally does not yet claim broader side
        pockets, richer face recovery, or general work-position planning.
        """

        state = self.body.get_state()
        origin = _state_block_pos(state.pos)
        if radius < 1:
            raise ValueError("radius must be >= 1")

        scan = _scan_place_here_candidates(self.body, origin, radius)
        if isinstance(scan, ToolResult):
            return scan

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
                scan = _scan_place_here_candidates(self.body, origin, radius)
                if isinstance(scan, ToolResult):
                    return scan
                supported = [candidate for candidate in scan if candidate["candidate"]]
                standable = [candidate for candidate in supported if candidate["has_stand_point"]]
        if not standable:
            recovery = self._recover_place_here_headroom(supported, timeout_s=timeout_s)
            if isinstance(recovery, ToolResult):
                return recovery
            headroom_recovery = recovery
            if recovery.get("recovered"):
                scan = _scan_place_here_candidates(self.body, origin, radius)
                if isinstance(scan, ToolResult):
                    return scan
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


def _read_inventory_slots(body: Body, page_size: int = 12) -> PerceptionResult:
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


def _inventory_counts(slots: list[InventorySlot]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for slot in slots:
        if slot.empty or not slot.item:
            continue
        item = _normalize_item(slot.item)
        counts[item] = counts.get(item, 0) + slot.count
    return counts


def _inventory_counts_from_body(body: Body) -> dict[str, int] | ToolResult:
    inventory = _read_inventory_slots(body)
    failed = _perception_failure(inventory)
    if failed is not None:
        return failed
    return _inventory_counts([InventorySlot.from_payload(slot) for slot in inventory.data.get("slots") or []])


def _normalize_item(item: str) -> str:
    return item.removeprefix("minecraft:")


def _is_log_block_type(block_type: str) -> bool:
    return _normalize_item(block_type).endswith("_log")


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
) -> list[dict[str, object]] | ToolResult:
    scanned: list[dict[str, object]] = []
    for target in _place_here_targets(origin, radius):
        target_block = body.perceive("blockAt", _block_params(target))
        failed = _perception_failure(target_block)
        if failed is not None:
            return failed
        below = (target[0], target[1] - 1, target[2])
        below_block = body.perceive("blockAt", _block_params(below))
        failed = _perception_failure(below_block)
        if failed is not None:
            return failed

        target_clear = _is_clear_perception(target_block)
        support_solid = _is_solid_support_perception(below_block)
        stand_points: list[Position] = []
        if target_clear and support_solid:
            stand_points_result = interaction_stand_points(body, target)
            if isinstance(stand_points_result, ToolResult):
                return stand_points_result
            stand_points = stand_points_result
        scanned.append(
            {
                "target": list(target),
                "support": list(below),
                "target_block": _normalize_item(str(target_block.data.get("type") or "unknown")),
                "target_state": str(target_block.data.get("state") or "UNKNOWN"),
                "support_block": _normalize_item(str(below_block.data.get("type") or "unknown")),
                "support_state": str(below_block.data.get("state") or "UNKNOWN"),
                "candidate": target_clear and support_solid,
                "stand_points": [list(point) for point in stand_points],
                "has_stand_point": bool(stand_points),
            }
        )
    return scanned


def _is_solid_support_perception(perception: PerceptionResult) -> bool:
    block_state = str(perception.data.get("state") or "UNKNOWN")
    return block_state == "SOLID"


def _place_here_targets(origin: Position, radius: int) -> tuple[Position, ...]:
    candidates: list[tuple[int, float, Position]] = []
    for dz in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            if dx == 0 and dz == 0:
                continue
            target = (origin[0] + dx, origin[1], origin[2] + dz)
            manhattan = abs(dx) + abs(dz)
            distance = dist((float(origin[0]), float(origin[1]), float(origin[2])), (float(target[0]), float(target[1]), float(target[2])))
            candidates.append((manhattan, distance, target))
    candidates.sort(key=lambda item: (item[0], item[1], item[2][2], item[2][0]))
    return tuple(target for _manhattan, _distance, target in candidates)


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


def _place_here_retryable_reason(reason: str) -> bool:
    return reason.startswith("place_denied:")


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
    candidates = standable or list(_mining_stand_candidates((pos[0], pos[1] + 1, pos[2])))
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


def _should_try_next_mining_stand(result: ToolResult) -> bool:
    reason = str(result.reason or "")
    if reason.startswith("mine_approach_failed:dig_through:break_denied:"):
        clearance = (result.metrics or {}).get("clearance")
        if isinstance(clearance, dict):
            metrics = clearance.get("metrics")
            if isinstance(metrics, dict):
                legality = metrics.get("legality")
                if isinstance(legality, dict) and str(legality.get("reason") or "") == "not_natural_breakable":
                    return True
        return "not_natural_breakable" in reason
    return False


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


def _pickup_walk_targets(pos: Position, drop_positions: list[Position]) -> list[Position]:
    targets: list[Position] = [*drop_positions]
    px, py, pz = pos
    targets.extend(
        [
            (px + 0.5, py, pz + 0.5),
            (px, py, pz),
            (px - 0.25, py, pz + 0.5),
            (px + 1.25, py, pz + 0.5),
            (px + 0.5, py, pz - 0.25),
            (px + 0.5, py, pz + 1.25),
        ]
    )
    unique: list[Position] = []
    seen: set[tuple[float, float, float]] = set()
    for target in targets:
        key = (round(float(target[0]), 3), round(float(target[1]), 3), round(float(target[2]), 3))
        if key in seen:
            continue
        seen.add(key)
        unique.append(target)
    return unique


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
