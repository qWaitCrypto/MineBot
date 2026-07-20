"""Body transaction navigation runtime."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from math import dist, floor
from time import monotonic, sleep
from typing import Callable

from minebot.contract import (
    Action,
    Body,
    BodyState,
    BreakContext,
    Event,
    InteractionContext,
    InventorySlot,
    LocalProgressController,
    PlaceContext,
    Position,
    ProgressAbort,
    ProgressController,
    ToolResult,
    perception_next_cursor,
)
from minebot.game.navigation import (
    GoalBlock,
    GoalComposite,
    GoalLike,
    GoalNear,
    normalize_goal,
)


SERVER_GOAL_SET_LIMIT = 32


@dataclass(frozen=True)
class NavigationRunConfig:
    max_segments: int = 32
    max_partial_segments: int | None = None
    segment_timeout_s: float = 15.0
    server_grid_radius: int = 64
    server_max_expand: int = 2500
    server_no_progress_ticks: int = 120
    recheck_lookahead: int = 5
    min_partial_progress: int = 5
    allow_diagonal: bool = True
    allow_ascend: bool = True
    allow_descend: bool = True
    allow_swim: bool = True
    max_safe_fall_depth: int = 3
    max_water_drop_depth: int = 32
    allow_break: bool = True
    max_break_steps: int = 8
    allow_place: bool = True
    max_place_steps: int = 8
    allow_pillar: bool = True
    max_pillar_steps: int = 8
    allow_downward: bool = True
    max_downward_steps: int = 8
    allow_open: bool = True
    max_open_steps: int = 8
    scaffold_blocks: tuple[str, ...] = (
        "cobblestone",
        "cobbled_deepslate",
        "deepslate",
        "stone",
        "dirt",
        "netherrack",
    )
    recovery_attempts: int = 2
    recovery_detour_distances: tuple[int, ...] = (1,)
    recovery_detour_offsets: tuple[tuple[int, int], ...] = ((1, 0), (-1, 0), (0, 1), (0, -1))
    recovery_detour_y_offsets: tuple[int, ...] = (0, 1, -1)
    movement_arrival_radius: float | None = None


def pure_movement_navigation_config(
    config: NavigationRunConfig | None = None,
) -> NavigationRunConfig:
    """Return a movement profile that cannot alter terrain geometry."""

    return replace(
        config or NavigationRunConfig(),
        allow_break=False,
        max_break_steps=0,
        allow_place=False,
        max_place_steps=0,
        allow_pillar=False,
        max_pillar_steps=0,
        allow_downward=False,
        max_downward_steps=0,
    )


@dataclass(frozen=True)
class ExecutedSegment:
    index: int
    status: str
    target: Position | None
    terminal_reason: str | None
    success: bool
    action_id: str | None = None
    diagnostics: dict[str, object] = field(default_factory=dict)


class NavigationTransactions:
    """Executes navigation objectives through the Body navigation controller.

    The production ``navigate_to`` path delegates primary pathfinding and
    movement to Scarpet ``navigateTo`` so terrain reads stay server-side.
    Python remains the Body transaction glue: it starts the action, authorizes
    mutations, waits for terminal truth, and records progress.
    """

    def __init__(
        self,
        body: Body,
        conformance_model: object | None = None,
        *,
        progress: ProgressController | None = None,
        governance=None,
    ):
        self.body = body
        self.progress = progress or LocalProgressController()
        self.governance = governance or getattr(getattr(conformance_model, "costs", None), "governance", None)

    @classmethod
    def server_side(
        cls,
        body: Body,
        governance,
        *,
        progress: ProgressController | None = None,
    ) -> "NavigationTransactions":
        """Build the production runtime without constructing a Python planner."""

        return cls(
            body,
            progress=progress,
            governance=governance,
        )

    def navigate_to(
        self,
        goal: GoalLike,
        *,
        break_context: BreakContext | str = BreakContext.TRAVEL,
        config: NavigationRunConfig | None = None,
        timeout_s: float | None = None,
        arrival_radius: float | None = None,
        mutation_blacklist: set[Position] | None = None,
    ) -> ToolResult:
        cfg = config or NavigationRunConfig()
        if cfg.recheck_lookahead < 0:
            raise ValueError("recheck_lookahead must be >= 0")
        if cfg.max_safe_fall_depth < 0 or cfg.max_safe_fall_depth > 3:
            raise ValueError("max_safe_fall_depth must be between 0 and 3")
        if cfg.max_water_drop_depth < 1 or cfg.max_water_drop_depth > 64:
            raise ValueError("max_water_drop_depth must be between 1 and 64")
        if cfg.max_break_steps < 0:
            raise ValueError("max_break_steps must be >= 0")
        if cfg.max_place_steps < 0:
            raise ValueError("max_place_steps must be >= 0")
        if cfg.max_pillar_steps < 0:
            raise ValueError("max_pillar_steps must be >= 0")
        if cfg.max_downward_steps < 0:
            raise ValueError("max_downward_steps must be >= 0")
        if cfg.max_open_steps < 0:
            raise ValueError("max_open_steps must be >= 0")
        if cfg.recovery_attempts < 0:
            raise ValueError("recovery_attempts must be >= 0")
        if timeout_s is not None:
            if timeout_s <= 0:
                raise ValueError("timeout_s must be > 0")
            per_segment = max(cfg.segment_timeout_s, timeout_s / max(1, cfg.max_segments))
            cfg = replace(cfg, segment_timeout_s=per_segment)
        if arrival_radius is not None:
            if arrival_radius <= 0:
                raise ValueError("arrival_radius must be > 0")
            cfg = replace(cfg, movement_arrival_radius=arrival_radius)

        generation = self.progress.current_generation()
        executed: list[ExecutedSegment] = []
        nav_goal = normalize_goal(goal)
        state = self.body.get_state()
        start = _block_pos(state)
        break_policy_context = BreakContext(break_context)
        goal_anchor = nav_goal.representative(start)
        server_goals, goal_set_preserved = _server_goal_set(nav_goal, start)
        gx, gy, gz = int(goal_anchor[0]), int(goal_anchor[1]), int(goal_anchor[2])
        ar = cfg.movement_arrival_radius or 0.75
        goal_radius = int(getattr(nav_goal, "radius", 0) or 0)
        capability_snapshot = _navigation_capability_snapshot(self.body, cfg)
        denied_mutations = mutation_blacklist if mutation_blacklist is not None else set()
        broken_steps = 0
        placed_steps = 0
        pillar_steps = 0
        downward_steps = 0
        open_steps = 0
        recovery_attempts: list[dict[str, object]] = []

        segment_index = 0
        partial_segments = 0
        max_partial_segments = cfg.max_partial_segments if cfg.max_partial_segments is not None else cfg.max_segments
        while segment_index < cfg.max_segments:
            if not self.progress.generation_current(generation):
                return _result(False, "preempted", True, goal_anchor, executed, {"generation_current": False})

            try:
                self.progress.require_can_continue(f"navigate_to:{nav_goal.payload()}")
            except ProgressAbort as exc:
                return _result(False, "progress_yielded", True, goal_anchor, executed, {"error": str(exc)})

            action = Action.create(
                "navigateTo",
                {
                    "target": [gx, gy, gz],
                    "goals": [list(candidate) for candidate in server_goals],
                    "grid_radius": cfg.server_grid_radius,
                    "max_expand": cfg.server_max_expand,
                    "y_below": 8,
                    "y_above": 8,
                    "arrival_radius": ar,
                    "goal_radius": goal_radius,
                    "timeout_ticks": max(20, int(cfg.segment_timeout_s * 20)),
                    "no_progress_ticks": cfg.server_no_progress_ticks,
                    "min_partial_progress": max(1, cfg.min_partial_progress),
                    "partial_replans": max(0, max_partial_segments - partial_segments - 1),
                    "segment_index": segment_index,
                    "allow_diagonal": cfg.allow_diagonal,
                    "allow_ascend": cfg.allow_ascend,
                    "allow_descend": cfg.allow_descend,
                    "allow_swim": cfg.allow_swim,
                    "max_fall_depth": cfg.max_safe_fall_depth,
                    "max_water_drop_depth": cfg.max_water_drop_depth,
                    "recheck_lookahead": cfg.recheck_lookahead,
                    "allow_break": bool(cfg.allow_break and cfg.max_break_steps > broken_steps),
                    "break_budget": max(0, cfg.max_break_steps - broken_steps),
                    "break_timeout_ticks": max(20, int(cfg.segment_timeout_s * 20)),
                    "break_pickaxe": capability_snapshot["break_pickaxe"],
                    "break_axe": capability_snapshot["break_axe"],
                    "break_shovel": capability_snapshot["break_shovel"],
                    "allow_place": bool(capability_snapshot["allow_place"]),
                    "scaffold_item": capability_snapshot["scaffold_item"],
                    "scaffold_count": max(
                        0,
                        int(capability_snapshot["scaffold_count"]) - placed_steps - pillar_steps,
                    ),
                    "place_budget": max(0, cfg.max_place_steps - placed_steps),
                    "allow_pillar": bool(
                        cfg.allow_pillar
                        and capability_snapshot["has_scaffold"]
                        and cfg.max_pillar_steps > pillar_steps
                    ),
                    "pillar_budget": max(0, cfg.max_pillar_steps - pillar_steps),
                    "allow_downward": bool(
                        cfg.allow_downward and cfg.max_downward_steps > downward_steps
                    ),
                    "downward_budget": max(0, cfg.max_downward_steps - downward_steps),
                    "allow_open": bool(cfg.allow_open and cfg.max_open_steps > open_steps),
                    "open_budget": max(0, cfg.max_open_steps - open_steps),
                    "denied_mutations": [list(pos) for pos in sorted(denied_mutations)],
                },
            )
            result = self.body.execute(action)
            if not (result.ok and result.accepted):
                executed.append(ExecutedSegment(
                    index=segment_index, status="rejected", target=goal_anchor,
                    terminal_reason="body_rejected", success=False, action_id=action.id,
                    diagnostics={"error": result.error, "data": result.data},
                ))
                return _result(False, "body_rejected", True, goal_anchor, executed, {"error": result.error})

            navigation_events: list[dict[str, object]] = []
            mutation_events: list[dict[str, object]] = []
            last_proposal_pos: Position | None = None
            while True:
                terminal = self.body.await_action_terminal(
                    action.id,
                    timeout_s=cfg.segment_timeout_s + 5.0,
                    terminal_events={
                        "navigateDone",
                        "navigateMutationProposed",
                        "navigateMutationDone",
                        "navigateStartTrace",
                        "moveDone",
                        "death",
                        "respawned",
                        "ownerPreempted",
                    },
                    intermediate_events={
                        "navigateMutationProposed",
                        "navigateMutationDone",
                        "navigateStartTrace",
                        "moveDone",
                    },
                )
                if terminal.name in {"navigateStartTrace", "moveDone"}:
                    navigation_events.append({"event": terminal.name, "data": dict(terminal.data)})
                    continue
                if terminal.name == "navigateMutationProposed":
                    proposal = dict(terminal.data)
                    mutation_events.append({"event": terminal.name, "data": proposal})
                    last_proposal_pos = _event_position(proposal.get("pos"))
                    self._answer_navigation_mutation(action.id, proposal, break_context=break_policy_context)
                    continue
                if terminal.name == "navigateMutationDone":
                    mutation = dict(terminal.data)
                    mutation_events.append({"event": terminal.name, "data": mutation})
                    mutation_pos = _event_position(mutation.get("pos"))
                    if bool(mutation.get("success")):
                        if mutation.get("kind") == "break":
                            broken_steps += 1
                        if mutation.get("kind") == "downward":
                            downward_steps += 1
                        if mutation.get("kind") == "open":
                            open_steps += 1
                        if mutation.get("kind") in {"place", "pillar"} and mutation_pos is not None:
                            mutation_kind = str(mutation.get("kind"))
                            purpose = "pillar" if mutation_kind == "pillar" else "bridge"
                            if mutation_kind == "pillar":
                                pillar_steps += 1
                            else:
                                placed_steps += 1
                            block_type = str(mutation.get("block_type") or capability_snapshot["scaffold_item"] or "unknown")
                            if self.governance is not None:
                                self.governance.record_bot_placement(
                                    mutation_pos,
                                    block_type,
                                    purpose,
                                    self.body.bot_name,
                                )
                    continue
                break
            if not self.progress.generation_current(generation):
                return _result(False, "preempted", True, goal_anchor, executed, {"generation_current": False})

            td = terminal.data
            raw_nav_reason = td.get("reason") or td.get("nav_reason") or td.get("stopped_reason") or terminal.name
            nav_reason = "preempted" if terminal.name == "ownerPreempted" else raw_nav_reason
            nav_arrived = bool(td.get("arrived", False)) or nav_reason == "arrived"
            goal_dist = td.get("goal_dist", td.get("dist_to_target", 9999.0))
            selected_goal = _event_position(td.get("selected_goal")) or goal_anchor

            executed.append(ExecutedSegment(
                index=segment_index, status=nav_reason, target=selected_goal,
                terminal_reason=nav_reason, success=nav_arrived, action_id=action.id,
                diagnostics={
                    "expanded": td.get("expanded", 0),
                    "waypoints": td.get("waypoints", 0),
                    "goal_dist": goal_dist,
                    "event": terminal.name,
                    "raw_reason": raw_nav_reason,
                    "move_ticks": td.get("move_ticks"),
                    "move_min_dist": td.get("move_min_dist"),
                    "move_stuck_ticks": td.get("move_stuck_ticks"),
                    "move_deviation": td.get("move_deviation"),
                    "move_waypoint_index": td.get("move_waypoint_index"),
                    "move_waypoint_count": td.get("move_waypoint_count"),
                    "move_current_waypoint": td.get("move_current_waypoint"),
                    "movement_counts": td.get("movement_counts"),
                    "capability_snapshot": td.get("capability_snapshot"),
                    "partial_coefficient": td.get("partial_coefficient"),
                    "partial_distance": td.get("partial_distance"),
                    "recheck_reason": td.get("recheck_reason"),
                    "navigation_events": navigation_events,
                    "mutation_events": mutation_events,
                    "event_data": dict(td),
                    "selected_goal": list(selected_goal),
                    "goal_count": len(server_goals),
                    "goal_set_preserved": goal_set_preserved,
                },
            ))

            neutral_step = nav_reason in {"partial", "preempted", "world_changed"}
            self.progress.note_step(
                ("navigate.segment", start, nav_goal.payload(), nav_reason, goal_anchor),
                success=nav_arrived or nav_reason == "partial",
                fingerprint=self.progress.fingerprint(self.body.get_state()),
                neutral=neutral_step,
            )

            if nav_arrived:
                return _result(
                    True,
                    "arrived",
                    False,
                    selected_goal,
                    executed,
                    {
                        "navigation_goal": nav_goal.payload(),
                        "selected_goal": list(selected_goal),
                        "goal_count": len(server_goals),
                        "goal_set_preserved": goal_set_preserved,
                        "movement_counts": td.get("movement_counts"),
                        "capability_snapshot": td.get("capability_snapshot"),
                    },
                )

            if nav_reason == "preempted":
                reflex = _wait_for_reflex_completion(
                    self.body,
                    timeout_s=min(5.0, max(1.0, cfg.segment_timeout_s / 3.0)),
                )
                if reflex is not None and reflex.name == "reflexCompleted":
                    if executed:
                        executed[-1].diagnostics["reflex_handoff"] = {
                            "event": reflex.name,
                            "seq": reflex.seq,
                            "data": dict(reflex.data),
                        }
                    water_egress_confirmed = (
                        reflex.data.get("kind") == "water"
                        and reflex.data.get("escaped_hazard") is True
                        and reflex.data.get("final_is_dry_stand") is True
                    )
                    if reflex.data.get("escaped_hazard") is False or (
                        reflex.data.get("kind") == "water" and not water_egress_confirmed
                    ):
                        return _result(
                            False,
                            "water_egress_failed" if reflex.data.get("kind") == "water" else "reflex_failed",
                            True,
                            goal_anchor,
                            executed,
                            {
                                "navigation_goal": nav_goal.payload(),
                                "paused": False,
                                "reflex_handoff": "reflex_failed",
                                "reflex": dict(reflex.data),
                            },
                        )
                    state = self.body.get_state()
                    start = _block_pos(state)
                    segment_index += 1
                    continue
                return _result(
                    True,
                    "preempted",
                    True,
                    goal_anchor,
                    executed,
                    {
                        "navigation_goal": nav_goal.payload(),
                        "paused": True,
                        "reflex_handoff": "timeout" if reflex is None else reflex.name,
                    },
                )

            if nav_reason == "partial":
                state = self.body.get_state()
                start = _block_pos(state)
                partial_segments += 1
                if partial_segments >= max_partial_segments:
                    return _result(
                        False,
                        "partial_segment_budget_exhausted",
                        True,
                        goal_anchor,
                        executed,
                        {
                            "navigation_goal": nav_goal.payload(),
                            "goal_dist": goal_dist,
                            "partial_segments": partial_segments,
                            "max_partial_segments": max_partial_segments,
                        },
                    )
                segment_index += 1
                continue

            if nav_reason == "world_changed":
                state = self.body.get_state()
                start = _block_pos(state)
                segment_index += 1
                continue

            if nav_reason == "mutation_denied":
                if last_proposal_pos is not None:
                    denied_mutations.add(last_proposal_pos)
                state = self.body.get_state()
                start = _block_pos(state)
                segment_index += 1
                continue

            if nav_reason in {"stuck", "deviated", "no_path"} and len(recovery_attempts) < cfg.recovery_attempts:
                recovery = self._run_navigation_recovery(
                    original_goal=goal_anchor,
                    original_reason=str(nav_reason),
                    cfg=cfg,
                )
                recovery_attempts.append(recovery.to_payload())
                executed[-1].diagnostics["recovery"] = recovery.to_payload()
                if recovery.success and recovery.reason == "arrived":
                    state = self.body.get_state()
                    start = _block_pos(state)
                    segment_index += 1
                    continue
                return _result(
                    False,
                    f"recovery_exhausted:{nav_reason}",
                    True,
                    goal_anchor,
                    executed,
                    {
                        "navigation_goal": nav_goal.payload(),
                        "goal_dist": goal_dist,
                        "recovery_attempts": recovery_attempts,
                    },
                )

            return _result(False, nav_reason, nav_reason in ("stuck", "timeout", "partial_segment_budget_exhausted"), goal_anchor, executed, {
                "navigation_goal": nav_goal.payload(), "goal_dist": goal_dist,
            })

        if executed and all(segment.status == "mutation_denied" for segment in executed):
            return _result(
                False,
                "protected_or_denied",
                True,
                goal_anchor,
                executed,
                {
                    "navigation_goal": nav_goal.payload(),
                    **_denied_navigation_summary(executed),
                },
            )
        return _result(False, "segment_budget_exhausted", True, goal_anchor, executed, {"navigation_goal": nav_goal.payload()})

    def _run_navigation_recovery(
        self,
        *,
        original_goal: Position,
        original_reason: str,
        cfg: NavigationRunConfig,
    ) -> ToolResult:
        origin = _block_pos(self.body.get_state())
        candidates = _recovery_goal_candidates(
            origin,
            original_goal,
            distances=cfg.recovery_detour_distances,
            offsets=cfg.recovery_detour_offsets,
            y_offsets=cfg.recovery_detour_y_offsets,
        )
        if not candidates:
            return ToolResult(
                success=False,
                reason="recovery_candidate_exhausted",
                can_retry=True,
                metrics={"origin": list(origin), "original_reason": original_reason},
            )
        recovery_config = replace(
            cfg,
            max_segments=max(1, min(4, cfg.max_segments)),
            max_partial_segments=max(1, min(4, cfg.max_partial_segments or cfg.max_segments)),
            recovery_attempts=0,
        )
        goal = GoalComposite(tuple(GoalNear(candidate, radius=0) for candidate in candidates))
        result = self.navigate_to(
            goal,
            break_context=BreakContext.RECOVERY,
            config=recovery_config,
        )
        metrics = dict(result.metrics or {})
        metrics.update(
            {
                "origin": list(origin),
                "original_goal": list(original_goal),
                "original_reason": original_reason,
                "recovery_goal": goal.payload(),
                "recovery_candidates": [list(candidate) for candidate in candidates],
            }
        )
        return ToolResult(
            success=result.success,
            reason=result.reason,
            can_retry=result.can_retry,
            next_suggestion=result.next_suggestion,
            metrics=metrics,
        )

    def _answer_navigation_mutation(
        self,
        navigation_action_id: str,
        proposal: dict[str, object],
        *,
        break_context: BreakContext,
    ) -> None:
        mutation_kind = str(proposal.get("kind") or "unknown")
        pos = _event_position(proposal.get("pos"))
        block_type = str(proposal.get("block_type") or "unknown")
        allowed = False
        reason = "unsupported_mutation"
        if mutation_kind in {"break", "downward"} and pos is not None and self.governance is not None:
            try:
                fact = self.body.perceive("blockAt", {"x": pos[0], "y": pos[1], "z": pos[2]})
            except Exception as exc:
                fact = None
                reason = f"world_read_failed:{type(exc).__name__}"
            observed_type = (
                "unknown"
                if fact is None
                else str(fact.data.get("type") or "unknown").removeprefix("minecraft:")
            )
            proposed_type = block_type.removeprefix("minecraft:")
            if fact is None:
                pass
            elif not fact.ok or not fact.complete:
                reason = "world_read_failed"
            elif observed_type != proposed_type:
                reason = "world_changed"
            else:
                decision = self.governance.can_break(pos, observed_type, break_context)
                allowed = decision.allowed
                reason = decision.reason
        elif mutation_kind in {"place", "pillar"} and pos is not None and self.governance is not None:
            decision = self.governance.can_place(
                pos,
                block_type,
                PlaceContext.WORK,
                self.body.bot_name,
            )
            allowed = decision.allowed
            reason = decision.reason
        elif mutation_kind == "open" and pos is not None and self.governance is not None:
            try:
                fact = self.body.perceive("blockAt", {"x": pos[0], "y": pos[1], "z": pos[2]})
            except Exception as exc:
                fact = None
                reason = f"world_read_failed:{type(exc).__name__}"
            observed_type = (
                "unknown"
                if fact is None
                else str(fact.data.get("type") or "unknown").removeprefix("minecraft:")
            )
            proposed_type = block_type.removeprefix("minecraft:")
            if fact is None:
                pass
            elif not fact.ok or not fact.complete:
                reason = "world_read_failed"
            elif observed_type != proposed_type:
                reason = "world_changed"
            elif str((fact.data.get("properties") or {}).get("open") or "false").lower() == "true":
                reason = "world_changed"
            else:
                decision = self.governance.can_interact(pos, observed_type, InteractionContext.ACTIVATE)
                allowed = decision.allowed
                reason = decision.reason
        decision_action = Action.create(
            "navigationMutationDecision",
            {
                "navigation_action_id": navigation_action_id,
                "proposal_id": proposal.get("proposal_id"),
                "authorized": allowed,
                "reason": reason,
                "pos": list(pos) if pos is not None else None,
                "kind": mutation_kind,
                "block_type": block_type,
            },
        )
        accepted = self.body.execute(decision_action)
        if not (accepted.ok and accepted.accepted):
            raise RuntimeError(f"navigation mutation decision rejected: {accepted.error or accepted.data}")


    def follow_entity(
        self,
        target_spec: str,
        *,
        keep_distance: float = 3.0,
        timeout_s: float = 30.0,
        config: NavigationRunConfig | None = None,
    ) -> ToolResult:
        """Follow a moving entity (player or named mob) by name.

        The Body owns the physical pursuit: Scarpet ``followEntity`` re-plans a
        server-side path to the target's live position on a cadence (move done
        or target drifted beyond ``replan_distance``) and ``run_move_tick``
        walks it with jump/obstacle handling. The brain emits one intent
        ("follow X") and waits for ``followDone``. This is ``navigate_to`` with
        a moving goal — the same body-owns-execution, brain-stays-thin shape.
        """
        cfg = config or NavigationRunConfig()
        if not target_spec:
            raise ValueError("target_spec must be a non-empty name/uuid")
        if timeout_s <= 0:
            raise ValueError("timeout_s must be > 0")
        if keep_distance < 0:
            raise ValueError("keep_distance must be >= 0")

        generation = self.progress.current_generation()
        if not self.progress.generation_current(generation):
            return _result(False, "preempted", True, _block_pos(self.body.get_state()), [], {"generation_current": False})
        try:
            self.progress.require_can_continue(f"follow_entity:{target_spec}")
        except ProgressAbort as exc:
            return _result(False, "progress_yielded", True, _block_pos(self.body.get_state()), [], {"error": str(exc), "target_spec": target_spec})

        action = Action.create(
            "followEntity",
            {
                "target_spec": target_spec,
                "keep_radius": keep_distance,
                "replan_distance": 2.0,
                "acquire_radius": 32,
                "grid_radius": cfg.server_grid_radius,
                "max_expand": cfg.server_max_expand,
                "no_progress_ticks": cfg.server_no_progress_ticks,
                "min_partial_progress": max(1, cfg.min_partial_progress),
                "allow_diagonal": cfg.allow_diagonal,
                "allow_ascend": cfg.allow_ascend,
                "allow_descend": cfg.allow_descend,
                "allow_swim": cfg.allow_swim,
                "max_fall_depth": cfg.max_safe_fall_depth,
                "max_water_drop_depth": cfg.max_water_drop_depth,
                "timeout_ticks": max(20, int(timeout_s * 20)),
            },
        )
        result = self.body.execute(action)
        start = _block_pos(self.body.get_state())
        if not (result.ok and result.accepted):
            return _result(False, "body_rejected", True, start, [], {"error": result.error, "target_spec": target_spec})

        terminal = self.body.await_action_terminal(
            action.id,
            timeout_s=timeout_s + 5.0,
            terminal_events={"followDone", "death", "respawned"},
        )
        if not self.progress.generation_current(generation):
            return _result(False, "preempted", True, start, [], {"generation_current": False, "target_spec": target_spec})

        td = terminal.data
        arrived = bool(td.get("arrived", False))
        reason = str(td.get("reason") or "unknown")
        self.progress.note_step(
            ("follow.tick", start, target_spec, reason),
            success=arrived,
            fingerprint=self.progress.fingerprint(self.body.get_state()),
            neutral=reason in ("timeout", "target_lost"),
        )
        return _result(
            arrived,
            reason,
            reason in ("timeout", "target_lost", "stuck"),
            start,
            [],
            {"target_spec": target_spec, "keep_distance": keep_distance, "event": terminal.name},
        )

    def move_away(
        self,
        danger: Position | tuple[float, float, float],
        *,
        min_distance: float = 6.0,
        target_distance: float | None = None,
        hazard_radius: float = 0.0,
        maintenance_checks: int = 1,
        maintenance_interval_s: float = 0.0,
        danger_refresh: Callable[[], Position | tuple[float, float, float]] | None = None,
        candidate_radii: tuple[int, ...] = (2, 4, 6, 8),
        max_candidates: int = 12,
        config: NavigationRunConfig | None = None,
    ) -> ToolResult:
        """Increase distance from one hazard through shared avoid-goal navigation.

        This Body transaction routes through the shared navigation planner's
        inverse-goal shape instead of sampling bespoke local targets. It can
        also re-check a refreshed hazard anchor a bounded number of times so a
        moving hazard cannot silently invalidate the first escape segment.
        """

        if min_distance <= 0:
            raise ValueError("min_distance must be > 0")
        if hazard_radius < 0:
            raise ValueError("hazard_radius must be >= 0")
        if maintenance_checks < 1:
            raise ValueError("maintenance_checks must be >= 1")
        if maintenance_interval_s < 0:
            raise ValueError("maintenance_interval_s must be >= 0")
        if max_candidates < 1:
            raise ValueError("max_candidates must be >= 1")
        if max_candidates > SERVER_GOAL_SET_LIMIT:
            raise ValueError(f"max_candidates must be <= {SERVER_GOAL_SET_LIMIT}")

        cfg = pure_movement_navigation_config(config)
        static_danger = _xyz_pos(danger)
        danger_provider = danger_refresh or (lambda: static_danger)
        desired_distance = max(min_distance, target_distance or min_distance)
        required_distance = desired_distance + hazard_radius
        attempts: list[dict[str, object]] = []
        moved = False
        origin = _block_pos(self.body.get_state())
        last_failure: ToolResult | None = None
        first_initial_distance: float | None = None
        last_initial_distance: float | None = None

        final_goal_payload: dict[str, object] | None = None
        for check_index in range(maintenance_checks):
            state = self.body.get_state()
            current = _block_pos(state)
            danger_xyz = _xyz_pos(danger_provider())
            initial_distance = dist(state.pos, danger_xyz)
            if first_initial_distance is None:
                first_initial_distance = initial_distance
            last_initial_distance = initial_distance
            attempt_context = {
                "check": check_index + 1,
                "danger": list(danger_xyz),
                "danger_block": list(_danger_block_pos(danger_xyz)),
                "origin": list(current),
                "initial_distance": initial_distance,
                "required_distance": required_distance,
            }

            if initial_distance >= required_distance:
                if maintenance_checks == 1 and not moved:
                    return ToolResult(
                        success=True,
                        reason="already_safe",
                        can_retry=False,
                        metrics={
                            "danger": list(danger_xyz),
                            "origin": list(current),
                            "initial_distance": initial_distance,
                            "desired_distance": desired_distance,
                            "hazard_radius": hazard_radius,
                            "required_distance": required_distance,
                            "final_distance": initial_distance,
                            "attempts": [],
                        },
                    )
                attempts.append({**attempt_context, "result": {"success": True, "reason": "already_safe"}})
                if check_index + 1 < maintenance_checks and maintenance_interval_s > 0:
                    sleep(maintenance_interval_s)
                continue

            witness_candidates = _move_away_candidates(
                current,
                danger_xyz,
                candidate_radii=candidate_radii,
                min_distance=required_distance,
                max_candidates=max_candidates,
            )
            if not witness_candidates:
                return ToolResult(
                    success=False,
                    reason="move_away_no_candidate",
                    can_retry=True,
                    next_suggestion="expand the move-away radius or fall back to a broader navigation/reflex escape path",
                    metrics={
                        "danger": list(danger_xyz),
                        "origin": list(current),
                        "initial_distance": initial_distance,
                        "desired_distance": desired_distance,
                        "hazard_radius": hazard_radius,
                        "required_distance": required_distance,
                            "candidate_radii": list(candidate_radii),
                        "attempts": attempts,
                    },
                )

            goal = GoalComposite(tuple(GoalNear(candidate, radius=0) for candidate in witness_candidates))
            final_goal_payload = goal.payload()
            result = self.navigate_to(goal, break_context=BreakContext.TRAVEL, config=cfg)
            chosen_goal = _selected_candidate(result, witness_candidates)
            final_state = self.body.get_state()
            refreshed_danger = _xyz_pos(danger_provider())
            final_distance = dist(final_state.pos, refreshed_danger)
            attempt = {
                **attempt_context,
                "goal": goal.payload(),
                "chosen_goal": list(chosen_goal),
                "result": result.to_payload(),
                "danger_after": list(refreshed_danger),
                "final_distance": final_distance,
            }
            attempts.append(attempt)
            if final_distance >= required_distance and final_distance > initial_distance:
                moved = True
                last_failure = None
                if check_index + 1 < maintenance_checks and maintenance_interval_s > 0:
                    sleep(maintenance_interval_s)
                continue

            if result.success:
                last_failure = ToolResult(
                    success=False,
                    reason="move_away_out_of_band",
                    can_retry=True,
                    next_suggestion="retry from a broader inverse-goal distance or refresh the hazard anchor before continuing",
                    metrics=attempt,
                )
            else:
                last_failure = result

        if last_failure is None:
            final_state = self.body.get_state()
            final_danger = _xyz_pos(danger_provider())
            final_distance = dist(final_state.pos, final_danger)
            return ToolResult(
                success=final_distance >= required_distance,
                reason="moved_away" if moved else "already_safe",
                can_retry=False if final_distance >= required_distance else True,
                next_suggestion=None if final_distance >= required_distance else "retry from a broader inverse-goal distance",
                metrics={
                    "danger": list(final_danger),
                    "origin": list(origin),
                    "initial_distance": first_initial_distance if first_initial_distance is not None else final_distance,
                    "last_initial_distance": last_initial_distance if last_initial_distance is not None else final_distance,
                    "desired_distance": desired_distance,
                    "hazard_radius": hazard_radius,
                    "required_distance": required_distance,
                    "final_distance": final_distance,
                    "maintenance_checks": maintenance_checks,
                    "chosen_goal": attempts[-1].get("chosen_goal"),
                    "navigation_goal": final_goal_payload,
                    "candidate_radii": list(candidate_radii),
                    "attempts": attempts,
                },
            )

        if last_failure is not None:
            if (
                last_failure.reason == "move_away_out_of_band"
                and not any((attempt.get("result") or {}).get("success") is False for attempt in attempts)
            ):
                return ToolResult(
                    success=False,
                    reason="move_away_no_candidate",
                    can_retry=True,
                    next_suggestion="expand the move-away radius or fall back to a broader navigation/reflex escape path",
                    metrics={
                        "danger": list(_xyz_pos(danger_provider())),
                        "origin": list(origin),
                        "initial_distance": first_initial_distance if first_initial_distance is not None else required_distance,
                        "last_initial_distance": last_initial_distance if last_initial_distance is not None else required_distance,
                        "desired_distance": desired_distance,
                        "hazard_radius": hazard_radius,
                        "required_distance": required_distance,
                        "maintenance_checks": maintenance_checks,
                        "navigation_goal": final_goal_payload,
                        "candidate_radii": list(candidate_radii),
                        "attempts": attempts,
                    },
                )
            return ToolResult(
                success=False,
                reason=f"move_away_failed:{last_failure.reason}",
                can_retry=True,
                next_suggestion=last_failure.next_suggestion or "retry with a wider escape radius or different hazard anchor",
                metrics={
                    "danger": list(_xyz_pos(danger_provider())),
                    "origin": list(origin),
                    "initial_distance": first_initial_distance if first_initial_distance is not None else required_distance,
                    "last_initial_distance": last_initial_distance if last_initial_distance is not None else required_distance,
                    "desired_distance": desired_distance,
                    "hazard_radius": hazard_radius,
                    "required_distance": required_distance,
                    "maintenance_checks": maintenance_checks,
                    "navigation_goal": final_goal_payload,
                    "candidate_radii": list(candidate_radii),
                    "attempts": attempts,
                },
            )
        return ToolResult(
            success=False,
            reason="move_away_failed",
            can_retry=True,
            next_suggestion="retry with a wider escape radius or different hazard anchor",
            metrics={
                "danger": list(_xyz_pos(danger_provider())),
                "origin": list(origin),
                "initial_distance": first_initial_distance if first_initial_distance is not None else required_distance,
                "last_initial_distance": last_initial_distance if last_initial_distance is not None else required_distance,
                "desired_distance": desired_distance,
                "hazard_radius": hazard_radius,
                "required_distance": required_distance,
                "maintenance_checks": maintenance_checks,
                "navigation_goal": final_goal_payload,
                "candidate_radii": list(candidate_radii),
                "attempts": attempts,
            },
        )


def _block_pos(state: BodyState) -> Position:
    return (round(state.pos[0]), round(state.pos[1]), round(state.pos[2]))


def _server_goal_set(goal, start: Position) -> tuple[tuple[tuple[int, int, int, int], ...], bool]:
    if isinstance(goal, GoalBlock):
        return ((int(goal.pos[0]), int(goal.pos[1]), int(goal.pos[2]), 0),), True
    if isinstance(goal, GoalNear):
        return ((int(goal.pos[0]), int(goal.pos[1]), int(goal.pos[2]), int(goal.radius)),), True
    if isinstance(goal, GoalComposite) and goal.mode == "any":
        candidates: list[tuple[int, int, int, int]] = []
        fully_preserved = True
        for child in goal.goals:
            child_goals, preserved = _server_goal_set(child, start)
            fully_preserved = fully_preserved and preserved
            for candidate in child_goals:
                if candidate in candidates:
                    continue
                if len(candidates) >= SERVER_GOAL_SET_LIMIT:
                    return tuple(candidates), False
                candidates.append(candidate)
        if candidates:
            return tuple(candidates), fully_preserved
    anchor = goal.representative(start)
    radius = max(0, int(getattr(goal, "radius", 0) or 0))
    return ((int(anchor[0]), int(anchor[1]), int(anchor[2]), radius),), False


def _event_position(value: object) -> Position | None:
    if not isinstance(value, (list, tuple)) or len(value) < 3:
        return None
    try:
        return (int(value[0]), int(value[1]), int(value[2]))
    except (TypeError, ValueError):
        return None



def _result(
    success: bool,
    reason: str,
    can_retry: bool,
    goal: Position,
    executed: list[ExecutedSegment],
    extra: dict[str, object] | None = None,
) -> ToolResult:
    metrics: dict[str, object] = {
        "goal": list(goal),
        "segments": [_executed_payload(segment) for segment in executed],
        "segment_count": len(executed),
    }
    if extra:
        metrics.update(extra)
    return ToolResult(success=success, reason=reason, can_retry=can_retry, metrics=metrics)


def _denied_navigation_summary(executed: list[ExecutedSegment]) -> dict[str, object]:
    governance_blockers: dict[str, int] = {}
    mutation_blockers: dict[str, int] = {}
    movement_counts: dict[str, int] = {}
    final_pos: object | None = None
    selected_goal: object | None = None
    capability_snapshot: object | None = None
    denial_count = 0

    for segment in executed:
        diagnostics = segment.diagnostics
        event_data = diagnostics.get("event_data")
        if isinstance(event_data, dict) and event_data.get("final_pos") is not None:
            final_pos = event_data["final_pos"]
        if diagnostics.get("selected_goal") is not None:
            selected_goal = diagnostics["selected_goal"]
        if diagnostics.get("capability_snapshot") is not None:
            capability_snapshot = diagnostics["capability_snapshot"]

        segment_counts = diagnostics.get("movement_counts")
        if isinstance(segment_counts, dict):
            for movement, value in segment_counts.items():
                if isinstance(value, (int, float)) and int(value) > 0:
                    key = str(movement)
                    movement_counts[key] = movement_counts.get(key, 0) + int(value)

        mutation_events = diagnostics.get("mutation_events")
        if not isinstance(mutation_events, list):
            continue
        for event in mutation_events:
            if not isinstance(event, dict) or event.get("event") != "navigateMutationDone":
                continue
            data = event.get("data")
            if not isinstance(data, dict) or data.get("success") is not False:
                continue
            denial_count += 1
            decision_reason = str(data.get("decision_reason") or data.get("reason") or "mutation_denied")
            governance_blockers[decision_reason] = governance_blockers.get(decision_reason, 0) + 1
            mutation_key = ":".join(
                (
                    str(data.get("kind") or "unknown"),
                    str(data.get("block_type") or "unknown"),
                    decision_reason,
                )
            )
            mutation_blockers[mutation_key] = mutation_blockers.get(mutation_key, 0) + 1

    summary: dict[str, object] = {
        "denied_mutation_count": denial_count,
        "governance_blockers": governance_blockers,
        "mutation_blockers": mutation_blockers,
    }
    if movement_counts:
        summary["movement_counts"] = movement_counts
    if final_pos is not None:
        summary["final_pos"] = final_pos
    if selected_goal is not None:
        summary["selected_goal"] = selected_goal
    if capability_snapshot is not None:
        summary["capability_snapshot"] = capability_snapshot
    return summary


def _navigation_capability_snapshot(body: Body, cfg: NavigationRunConfig) -> dict[str, object]:
    disabled = {
        "break_pickaxe": None,
        "break_axe": None,
        "break_shovel": None,
        "has_scaffold": False,
        "allow_place": False,
        "scaffold_item": None,
        "scaffold_count": 0,
        "inventory_complete": False,
    }
    needs_break_inventory = cfg.allow_break and cfg.max_break_steps > 0
    needs_place_inventory = cfg.allow_place and cfg.max_place_steps > 0
    needs_pillar_inventory = cfg.allow_pillar and cfg.max_pillar_steps > 0
    needs_downward_inventory = cfg.allow_downward and cfg.max_downward_steps > 0
    if not needs_break_inventory and not needs_place_inventory and not needs_pillar_inventory and not needs_downward_inventory:
        return disabled

    start: int | None = 0
    slots: list[InventorySlot] = []
    try:
        while start is not None:
            page = body.perceive("inventory", {"start": start, "limit": 12})
            if not page.ok:
                return disabled
            slots.extend(InventorySlot.from_payload(dict(raw)) for raw in page.data.get("slots") or [])
            cursor = perception_next_cursor(page)
            if not page.complete and cursor is None:
                return disabled
            start = None if cursor is None else int(cursor)
    except (AttributeError, KeyError, TypeError, ValueError, RuntimeError, AssertionError):
        return disabled

    counts: dict[str, int] = {}
    for slot in slots:
        if slot.empty or not slot.item:
            continue
        item = str(slot.item).removeprefix("minecraft:")
        counts[item] = counts.get(item, 0) + int(slot.count)
    scaffold_item = next(
        (str(item).removeprefix("minecraft:") for item in cfg.scaffold_blocks if counts.get(str(item).removeprefix("minecraft:"), 0) > 0),
        None,
    )
    break_tools = {
        "break_pickaxe": _best_navigation_tool(counts, "pickaxe"),
        "break_axe": _best_navigation_tool(counts, "axe"),
        "break_shovel": _best_navigation_tool(counts, "shovel"),
    }
    if scaffold_item is None:
        return {**disabled, **break_tools, "inventory_complete": True}
    return {
        **break_tools,
        "has_scaffold": True,
        "allow_place": needs_place_inventory,
        "scaffold_item": scaffold_item,
        "scaffold_count": counts[scaffold_item],
        "inventory_complete": True,
    }


def _best_navigation_tool(counts: dict[str, int], tool: str) -> str | None:
    return next(
        (
            f"{material}_{tool}"
            for material in ("netherite", "diamond", "iron", "stone", "golden", "wooden")
            if counts.get(f"{material}_{tool}", 0) > 0
        ),
        None,
    )


def _executed_payload(segment: ExecutedSegment) -> dict[str, object]:
    diagnostics = {key: value for key, value in segment.diagnostics.items() if key != "planned_segment"}
    return {
        "index": segment.index,
        "status": segment.status,
        "target": list(segment.target) if segment.target is not None else None,
        "terminal_reason": segment.terminal_reason,
        "success": segment.success,
        "action_id": segment.action_id,
        "diagnostics": diagnostics,
    }



def _wait_for_reflex_completion(body: Body, *, timeout_s: float) -> Event | None:
    deadline = monotonic() + timeout_s
    while monotonic() < deadline:
        for event in body.poll_events():
            if event.name == "reflexCompleted":
                return event
            if event.name in {"death", "bodyMissing", "respawned"}:
                return event
        sleep(0.05)
    return None



def _xyz_pos(pos: Position | tuple[float, float, float]) -> tuple[float, float, float]:
    return (float(pos[0]), float(pos[1]), float(pos[2]))


def _danger_block_pos(pos: tuple[float, float, float]) -> Position:
    return (int(floor(pos[0])), int(floor(pos[1])), int(floor(pos[2])))


def _move_away_candidates(
    current: Position,
    danger: tuple[float, float, float],
    *,
    candidate_radii: tuple[int, ...],
    min_distance: float,
    max_candidates: int,
) -> tuple[Position, ...]:
    candidates: list[tuple[float, float, Position]] = []
    offsets = (
        (1, 0),
        (-1, 0),
        (0, 1),
        (0, -1),
        (1, 1),
        (1, -1),
        (-1, 1),
        (-1, -1),
    )
    for radius in candidate_radii:
        for dx, dz in offsets:
            target = (current[0] + dx * radius, current[1], current[2] + dz * radius)
            center = (target[0] + 0.5, float(target[1]), target[2] + 0.5)
            hazard_distance = dist(center, danger)
            if hazard_distance < min_distance:
                continue
            travel_distance = dist((current[0] + 0.5, float(current[1]), current[2] + 0.5), center)
            candidates.append((-hazard_distance, travel_distance, target))
    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    seen: list[Position] = []
    ordered: list[Position] = []
    for _neg_hazard_distance, _travel_distance, target in candidates:
        if target in seen:
            continue
        seen.append(target)
        ordered.append(target)
        if len(ordered) >= max_candidates:
            break
    return tuple(ordered)


def _recovery_goal_candidates(
    origin: Position,
    original_goal: Position,
    *,
    distances: tuple[int, ...],
    offsets: tuple[tuple[int, int], ...],
    y_offsets: tuple[int, ...],
) -> tuple[Position, ...]:
    if not distances or not offsets or not y_offsets:
        return ()
    ranked: list[tuple[int, int, int, Position]] = []
    for distance_value in distances:
        if distance_value < 1:
            continue
        for dx, dz in offsets:
            if dx == 0 and dz == 0:
                continue
            for y_offset in y_offsets:
                candidate = (
                    origin[0] + dx * distance_value,
                    origin[1] + y_offset,
                    origin[2] + dz * distance_value,
                )
                goal_distance = abs(candidate[0] - original_goal[0]) + abs(candidate[2] - original_goal[2])
                ranked.append((goal_distance, distance_value, abs(y_offset), candidate))
    ranked.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
    ordered: list[Position] = []
    for _goal_distance, _distance_value, _y_offset, candidate in ranked:
        if candidate in ordered:
            continue
        ordered.append(candidate)
        if len(ordered) >= SERVER_GOAL_SET_LIMIT:
            break
    return tuple(ordered)


def _selected_candidate(result: ToolResult, candidates: tuple[Position, ...]) -> Position:
    metrics = dict(result.metrics or {})
    raw = metrics.get("selected_goal", metrics.get("goal"))
    if isinstance(raw, (list, tuple)) and len(raw) >= 3:
        selected = (int(raw[0]), int(raw[1]), int(raw[2]))
        if selected in candidates:
            return selected
    return candidates[0]
