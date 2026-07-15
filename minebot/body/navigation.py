"""Body transaction navigation runtime."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from math import ceil, dist, floor
from time import monotonic, sleep
from typing import Callable

from minebot.body.block_work import BlockWork
from minebot.body.interaction import _openable_look_target
from minebot.body.world_read import read_block_cells_tiled, refresh_grid_world_around
from minebot.contract import (
    Action,
    Body,
    BodyState,
    BreakContext,
    Event,
    InventorySlot,
    LocalProgressController,
    PlaceContext,
    Position,
    ProgressAbort,
    ProgressController,
    PerceptionResult,
    Result,
    ToolResult,
    terminal_event_to_tool_result,
    perception_next_cursor,
)
from minebot.game.navigation import (
    GoalAvoid,
    GoalBlock,
    GoalComposite,
    GoalLike,
    GoalNear,
    GridWorld,
    MoveKind,
    NavigationCostModel,
    NavigationSegment,
    PathStep,
    SegmentedNavigator,
    normalize_goal,
)


WAYPOINT_MOVES = frozenset({MoveKind.WALK, MoveKind.DIAGONAL, MoveKind.ASCEND, MoveKind.DESCEND, MoveKind.SWIM, MoveKind.FALL})
TERRAIN_ACTION_MOVES = frozenset({MoveKind.BREAK, MoveKind.PLACE, MoveKind.PILLAR, MoveKind.DOWNWARD})
SCARPET_FALLBACK_REASONS = frozenset({"no_path", "stuck", "timeout", "deviated", "segment_budget_exhausted", "partial_segment_budget_exhausted"})
TERRAIN_FALLBACK_H_RADIUS = 5
TERRAIN_FALLBACK_Y_BELOW = 3
TERRAIN_FALLBACK_Y_ABOVE = 8
TERRAIN_FALLBACK_MAX_TILES = 64
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
    allow_break: bool = True
    max_break_steps: int = 8
    allow_place: bool = True
    max_place_steps: int = 8
    allow_pillar: bool = True
    max_pillar_steps: int = 8
    allow_downward: bool = True
    max_downward_steps: int = 8
    scaffold_blocks: tuple[str, ...] = (
        "cobblestone",
        "cobbled_deepslate",
        "deepslate",
        "stone",
        "dirt",
        "netherrack",
    )
    recovery_attempts: int = 2
    backtrack_cost_factor: float = 0.5
    unloaded_boundary_limit: int | None = None
    partial_tail_trim: int = 1
    guard_target: Position | None = None
    max_worse_distance: float | None = None
    recovery_detour_distances: tuple[int, ...] = (1,)
    recovery_detour_offsets: tuple[tuple[int, int], ...] = ((1, 0), (-1, 0), (0, 1), (0, -1))
    recovery_detour_y_offsets: tuple[int, ...] = (0, 1, -1)
    recovery_detour_max_attempts: int = 1
    recovery_min_displacement: float = 0.75
    recovery_detour_timeout_s: float = 3.0
    recovery_clearance_enabled: bool = True
    recheck_world: GridWorld | None = None
    recheck_costs: NavigationCostModel | None = None
    world_update: Callable[[object, NavigationSegment], dict[str, object] | None] | None = None
    movement_arrival_radius: float | None = None
    allow_local_terrain_fallback: bool = False
    progress_neutral_failures: bool = False


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
    Python remains the Body transaction glue: it starts the action, waits for
    terminal truth, records progress, and preserves migration seams for legacy
    terrain-aware helpers that are not on the Phase 1 hot path.
    """

    def __init__(
        self,
        body: Body,
        navigator: SegmentedNavigator,
        *,
        progress: ProgressController | None = None,
        work: BlockWork | None = None,
        governance=None,
    ):
        self.body = body
        self.navigator = navigator
        self.progress = progress or LocalProgressController()
        self.work = work or _default_work_runtime(body, navigator)
        self.governance = governance or getattr(getattr(navigator, "costs", None), "governance", None)

    @classmethod
    def server_side(
        cls,
        body: Body,
        governance,
        *,
        progress: ProgressController | None = None,
        work: BlockWork | None = None,
    ) -> "NavigationTransactions":
        """Build the production server-side navigation transaction runtime.

        Primary pathfinding is owned by Scarpet `navigateTo`; the small
        `SegmentedNavigator` object is retained only as a compatibility carrier
        for governance-aware helper code such as `move_away` and Body
        transactions that still accept a navigator-shaped dependency.
        """

        return cls(
            body,
            SegmentedNavigator(GridWorld({}), NavigationCostModel(governance)),
            progress=progress,
            work=work,
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
    ) -> ToolResult:
        cfg = config or NavigationRunConfig()
        if cfg.recheck_lookahead < 0:
            raise ValueError("recheck_lookahead must be >= 0")
        if cfg.max_safe_fall_depth < 0 or cfg.max_safe_fall_depth > 3:
            raise ValueError("max_safe_fall_depth must be between 0 and 3")
        if cfg.max_break_steps < 0:
            raise ValueError("max_break_steps must be >= 0")
        if cfg.max_place_steps < 0:
            raise ValueError("max_place_steps must be >= 0")
        if cfg.max_pillar_steps < 0:
            raise ValueError("max_pillar_steps must be >= 0")
        if cfg.max_downward_steps < 0:
            raise ValueError("max_downward_steps must be >= 0")
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
        denied_mutations: set[Position] = set()
        broken_steps = 0
        placed_steps = 0
        pillar_steps = 0
        downward_steps = 0

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
                    "allow_diagonal": cfg.allow_diagonal,
                    "allow_ascend": cfg.allow_ascend,
                    "allow_descend": cfg.allow_descend,
                    "allow_swim": cfg.allow_swim,
                    "max_fall_depth": cfg.max_safe_fall_depth,
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
                        "death",
                        "respawned",
                        "ownerPreempted",
                    },
                    intermediate_events={"navigateMutationProposed", "navigateMutationDone"},
                )
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
                    "mutation_events": mutation_events,
                    "event_data": dict(td),
                    "selected_goal": list(selected_goal),
                    "goal_count": len(server_goals),
                    "goal_set_preserved": goal_set_preserved,
                },
            ))

            neutral_step = nav_reason in {"partial", "preempted", "world_changed"} or (
                cfg.progress_neutral_failures and nav_reason in SCARPET_FALLBACK_REASONS
            )
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
                    if reflex.data.get("escaped_hazard") is False:
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

            if _scarpet_failure_can_fallback(nav_reason, break_context, cfg):
                fallback = self._run_local_terrain_fallback(
                    start=self.body.get_state(),
                    nav_goal=nav_goal,
                    goal=goal_anchor,
                    goal_payload=nav_goal.payload(),
                    break_context=break_context,
                    cfg=cfg,
                    executed=executed,
                    first_segment_index=segment_index + 1,
                    original_reason=nav_reason,
                )
                if fallback is not None:
                    return fallback

            return _result(False, nav_reason, nav_reason in ("stuck", "timeout"), goal_anchor, executed, {
                "navigation_goal": nav_goal.payload(), "goal_dist": goal_dist,
            })

        return _result(False, "segment_budget_exhausted", True, goal_anchor, executed, {"navigation_goal": nav_goal.payload()})

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

    def _run_local_terrain_fallback(
        self,
        *,
        start: BodyState,
        nav_goal,
        goal: Position,
        goal_payload: dict[str, object],
        break_context: BreakContext | str,
        cfg: NavigationRunConfig,
        executed: list[ExecutedSegment],
        first_segment_index: int,
        original_reason: str,
    ) -> ToolResult | None:
        world = getattr(self.navigator, "world", None)
        if not isinstance(world, GridWorld):
            return None

        origin = _block_pos(start)
        try:
            refresh = refresh_grid_world_around(
                self.body,
                world,
                origin,
                h_radius=TERRAIN_FALLBACK_H_RADIUS,
                y_below=TERRAIN_FALLBACK_Y_BELOW,
                y_above=TERRAIN_FALLBACK_Y_ABOVE,
                max_tiles=TERRAIN_FALLBACK_MAX_TILES,
                failure_label="navigation_fallback",
            )
        except Exception as exc:
            return _result(
                False,
                "terrain_fallback_world_read_failed",
                True,
                goal,
                executed,
                {
                    "navigation_goal": goal_payload,
                    "original_reason": original_reason,
                    "error": str(exc),
                },
            )

        if executed:
            executed[-1].diagnostics["terrain_fallback"] = {
                "trigger": original_reason,
                "world_refresh": refresh,
            }

        break_steps_used = 0
        previous_segment: tuple[Position, ...] = ()
        for offset in range(max(0, cfg.max_segments - first_segment_index)):
            segment_index = first_segment_index + offset
            current = _block_pos(self.body.get_state())
            if nav_goal.is_satisfied(current):
                return _with_fallback_origin(
                    _result(
                        True,
                        "arrived",
                        False,
                        goal,
                        executed,
                        {"navigation_goal": goal_payload, "original_reason": original_reason},
                    ),
                    original_reason,
                )
            try:
                self.progress.require_can_continue(f"navigate_terrain_fallback:{goal_payload}")
            except ProgressAbort as exc:
                return _with_fallback_origin(
                    _result(
                        False,
                        "progress_yielded",
                        True,
                        goal,
                        executed,
                        {"error": str(exc), "navigation_goal": goal_payload, "original_reason": original_reason},
                    ),
                    original_reason,
                )

            try:
                refresh = refresh_grid_world_around(
                    self.body,
                    world,
                    current,
                    h_radius=TERRAIN_FALLBACK_H_RADIUS,
                    y_below=TERRAIN_FALLBACK_Y_BELOW,
                    y_above=TERRAIN_FALLBACK_Y_ABOVE,
                    max_tiles=TERRAIN_FALLBACK_MAX_TILES,
                    failure_label="navigation_fallback",
                )
            except Exception as exc:
                return _with_fallback_origin(
                    _result(
                        False,
                        "terrain_fallback_world_read_failed",
                        True,
                        goal,
                        executed,
                        {
                            "navigation_goal": goal_payload,
                            "original_reason": original_reason,
                            "error": str(exc),
                        },
                    ),
                    original_reason,
                )

            segment = self.navigator.next_segment(
                current,
                goal,
                break_context=break_context,
                min_partial_progress=max(1, cfg.min_partial_progress),
                recheck_lookahead=cfg.recheck_lookahead,
                recheck_world=cfg.recheck_world,
                recheck_costs=cfg.recheck_costs,
                previous_segment=previous_segment,
                backtrack_cost_factor=cfg.backtrack_cost_factor,
                unloaded_boundary_limit=cfg.unloaded_boundary_limit,
                partial_tail_trim=cfg.partial_tail_trim,
            )
            if segment.target is None:
                executed.append(
                    ExecutedSegment(
                        index=segment_index,
                        status=f"terrain_fallback_{segment.status}",
                        target=None,
                        terminal_reason=segment.plan.reason,
                        success=False,
                        diagnostics={
                            "original_reason": original_reason,
                            "world_refresh": refresh,
                            "segment": _segment_payload(segment),
                            "planned_segment": segment,
                        },
                    )
                )
                return _with_fallback_origin(
                    _result(
                        False,
                        f"terrain_fallback:{segment.plan.reason}",
                        True,
                        goal,
                        executed,
                        {"navigation_goal": goal_payload, "original_reason": original_reason},
                    ),
                    original_reason,
                )

            if segment.status == "replan_required":
                executed.append(
                    ExecutedSegment(
                        index=segment_index,
                        status="terrain_fallback_replan_required",
                        target=segment.target,
                        terminal_reason=segment.recheck.reason if segment.recheck is not None else segment.plan.reason,
                        success=False,
                        diagnostics={
                            "original_reason": original_reason,
                            "world_refresh": refresh,
                            "segment": _segment_payload(segment),
                            "planned_segment": segment,
                        },
                    )
                )
                return _with_fallback_origin(
                    _result(
                        False,
                        "terrain_fallback:replan_required",
                        True,
                        goal,
                        executed,
                        {"navigation_goal": goal_payload, "original_reason": original_reason},
                    ),
                    original_reason,
                )

            segment_path = segment.plan.path
            action_step = _first_action_step(segment_path)
            if action_step is not None:
                prefix = _prefix_before_step(segment_path, action_step)
                if prefix:
                    prefix_segment = _segment_for_path_prefix(segment, prefix)
                    moved = self._execute_move(
                        segment_index,
                        prefix_segment,
                        prefix_segment.target,
                        prefix,
                        goal,
                        goal_payload,
                        break_context,
                        cfg,
                        executed,
                    )
                    if moved is not None:
                        if moved.success:
                            return _with_fallback_origin(moved, original_reason)
                        if moved.reason == "recoverable_move_failure":
                            retry = self._attempt_recovery_detour(
                                segment_index,
                                goal,
                                goal_payload,
                                cfg,
                                executed,
                                str((moved.metrics or {}).get("original_reason") or moved.reason),
                            )
                            if retry is not None:
                                return _with_fallback_origin(retry, original_reason)
                            previous_segment = _walk_positions(prefix)
                            continue
                        return _with_fallback_origin(moved, original_reason)
                    previous_segment = _walk_positions(prefix)
                    continue

                if action_step.move == MoveKind.OPEN:
                    opened_result = self._execute_open_step(
                        action_step,
                        cfg,
                        goal,
                        executed,
                        segment_index=segment_index,
                        segment=segment,
                    )
                    if opened_result is not None:
                        return _with_fallback_origin(opened_result, original_reason)
                    _apply_executed_terrain_effect(self.navigator, action_step)
                    previous_segment = _walk_positions(prefix)
                    continue

                terrain_result = self._execute_terrain_step(
                    action_step,
                    break_context,
                    cfg,
                    goal,
                    executed,
                    break_steps_used=break_steps_used,
                    segment_index=segment_index,
                    segment=segment,
                )
                if terrain_result is not None:
                    return _with_fallback_origin(terrain_result, original_reason)
                if action_step.move in {MoveKind.BREAK, MoveKind.DOWNWARD}:
                    break_steps_used += 1
                _apply_executed_terrain_effect(self.navigator, action_step)
                previous_segment = _walk_positions(prefix)
                continue

            moved = self._execute_move(
                segment_index,
                segment,
                segment.target,
                segment_path,
                goal,
                goal_payload,
                break_context,
                cfg,
                executed,
            )
            if moved is not None:
                if moved.success:
                    return _with_fallback_origin(moved, original_reason)
                if moved.reason == "recoverable_move_failure":
                    retry = self._attempt_recovery_detour(
                        segment_index,
                        goal,
                        goal_payload,
                        cfg,
                        executed,
                        str((moved.metrics or {}).get("original_reason") or moved.reason),
                    )
                    if retry is not None:
                        return _with_fallback_origin(retry, original_reason)
                    previous_segment = _walk_positions(segment_path)
                    continue
                return _with_fallback_origin(moved, original_reason)
            previous_segment = _walk_positions(segment_path)

        return _with_fallback_origin(
            _result(
                False,
                "terrain_fallback_segment_budget_exhausted",
                True,
                goal,
                executed,
                {"navigation_goal": goal_payload, "original_reason": original_reason},
            ),
            original_reason,
        )

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
                "grid_radius": 32,
                "max_expand": 200,
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

        cfg = config or NavigationRunConfig()
        static_danger = _xyz_pos(danger)
        danger_provider = danger_refresh or (lambda: static_danger)
        desired_distance = max(min_distance, target_distance or min_distance)
        required_distance = desired_distance + hazard_radius
        attempts: list[dict[str, object]] = []
        moved = False
        origin = _block_pos(self.body.get_state())
        legacy_candidates = list(candidate_radii)
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
                        "candidate_radii_legacy": legacy_candidates,
                        "attempts": attempts,
                    },
                )

            chosen_goal = witness_candidates[0]
            goal = GoalAvoid(
                _danger_block_pos(danger_xyz),
                min_distance=max(1, int(ceil(required_distance))),
                fallback=GoalBlock(chosen_goal),
            )
            final_goal_payload = goal.payload()
            result = self.navigate_to(goal, break_context=BreakContext.TRAVEL, config=cfg)
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
                    "candidate_radii_legacy": legacy_candidates,
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
                        "candidate_radii_legacy": legacy_candidates,
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
                    "candidate_radii_legacy": legacy_candidates,
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
                "candidate_radii_legacy": legacy_candidates,
                "attempts": attempts,
            },
        )

    def _execute_move(
        self,
        segment_index: int,
        segment: NavigationSegment,
        target: Position,
        path: tuple[PathStep, ...],
        goal: Position,
        goal_payload: dict[str, object],
        break_context: BreakContext | str,
        cfg: NavigationRunConfig,
        executed: list[ExecutedSegment],
    ) -> ToolResult | None:
        walk_path = tuple(step for step in path if step.move in WAYPOINT_MOVES)
        execution_target = _movement_waypoint(walk_path[-1]) if walk_path else target
        cancel_profile = _cancel_profile_payload(path)
        action = Action.create(
            "moveTo",
            {
                "target": list(execution_target),
                "waypoints": [list(_movement_waypoint(step)) for step in walk_path],
                "planned_target": list(target),
                "final_goal": list(goal),
                "navigation_goal": goal_payload,
                "segment_status": segment.status,
                "break_context": BreakContext(break_context).value,
                "path_steps": len(path),
                "path_moves": [step.move.value for step in path],
                "path_fall_depths": [step.fall_depth for step in path],
                "movement_cancel": cancel_profile,
                **({"arrival_radius": cfg.movement_arrival_radius} if cfg.movement_arrival_radius is not None else {}),
            },
        )
        accepted = self.body.execute(action)
        rejected = _acceptance_failure(accepted, "moveTo", target)
        if rejected is not None:
            executed.append(
                ExecutedSegment(
                    index=segment_index,
                    status=segment.status,
                    target=target,
                    terminal_reason="body_rejected",
                    success=False,
                    action_id=action.id,
                    diagnostics={"segment": _segment_payload(segment), "accepted": accepted.data},
                )
            )
            return _merge_metrics(rejected, goal, executed)

        terminal = self.body.await_action_terminal(action.id, timeout_s=cfg.segment_timeout_s)
        segment_result = terminal_event_to_tool_result(terminal)
        executed.append(
            ExecutedSegment(
                index=segment_index,
                status=segment.status,
                target=target,
                terminal_reason=segment_result.reason,
                success=segment_result.success,
                action_id=action.id,
                diagnostics={
                    "segment": _segment_payload(segment),
                    "planned_segment": segment,
                    "movement_cancel": cancel_profile,
                    "terminal": dict(segment_result.metrics or {}),
                },
            )
        )
        if segment_result.reason == "preempted":
            return _merge_metrics(segment_result, goal, executed)
        if not segment_result.success:
            if _recoverable_terminal_reason(segment_result.reason):
                self.progress.note_step(
                    ("navigate.recover", segment_result.reason, goal_payload),
                    success=False,
                    fingerprint=self.progress.fingerprint(self.body.get_state()),
                    neutral=True,
                )
                try:
                    self.progress.require_can_continue(f"navigate_recover:{goal_payload}")
                except ProgressAbort as exc:
                    return _result(
                        False,
                        "progress_yielded",
                        True,
                        goal,
                        executed,
                        {"error": str(exc), "navigation_goal": goal_payload},
                    )
                return ToolResult(
                    success=False,
                    reason="recoverable_move_failure",
                    can_retry=True,
                    metrics={
                        "original_reason": segment_result.reason,
                        "path_update": _path_update_probe("terminal", "recoverable_move_failure", segment_result.reason),
                        **dict(segment_result.metrics or {}),
                    },
                )
            return _merge_metrics(segment_result, goal, executed)

        if segment.status == "arrived" and target == segment.target:
            return _result(True, "arrived", False, goal, executed, {"navigation_goal": goal_payload})
        return None

    def _attempt_recovery_detour(
        self,
        segment_index: int,
        goal: Position,
        goal_payload: dict[str, object],
        cfg: NavigationRunConfig,
        executed: list[ExecutedSegment],
        original_reason: str,
    ) -> ToolResult | None:
        if not cfg.recovery_detour_offsets or cfg.recovery_detour_max_attempts == 0:
            return None

        before = self.body.get_state()
        origin = _block_pos(before)
        attempts: list[dict[str, object]] = []
        candidates = _recovery_detour_candidates(
            origin,
            goal,
            distances=cfg.recovery_detour_distances,
            offsets=cfg.recovery_detour_offsets,
            y_offsets=cfg.recovery_detour_y_offsets,
            max_attempts=cfg.recovery_detour_max_attempts,
            world=getattr(self.navigator, "world", None),
        )
        for target, detour_distance, detour_direction, target_y_offset, target_kind in candidates:
            move_kind = MoveKind.SWIM if target_kind == "water_prep" else MoveKind.WALK
            pulse_kind = "single_waypoint_move"
            action = Action.create(
                "moveTo",
                {
                    "target": list(target),
                    "waypoints": [list(target)],
                    "final_goal": list(goal),
                    "navigation_goal": goal_payload,
                    "segment_status": "recovery_detour",
                    "break_context": BreakContext.RECOVERY.value,
                    "path_steps": 1,
                    "path_moves": [move_kind.value],
                    "recovery_reason": original_reason,
                    "recovery_pulse": {
                        "kind": pulse_kind,
                        "timeout_s": cfg.recovery_detour_timeout_s,
                        "min_displacement": cfg.recovery_min_displacement,
                    },
                },
            )
            accepted = self.body.execute(action)
            rejected = _acceptance_failure(accepted, "moveTo", target)
            if rejected is not None:
                attempt = {
                    "target": list(target),
                    "detour_distance": detour_distance,
                    "detour_direction": list(detour_direction),
                    "target_y_offset": target_y_offset,
                    "target_kind": target_kind,
                    "path_moves": [move_kind.value],
                    "pulse_kind": pulse_kind,
                    "pulse_timeout_s": cfg.recovery_detour_timeout_s,
                    "accepted": False,
                    "reason": rejected.reason,
                    "displacement": 0.0,
                }
                attempts.append(attempt)
                executed.append(
                    ExecutedSegment(
                        index=segment_index,
                        status="recovery_detour",
                        target=target,
                        terminal_reason=rejected.reason,
                        success=False,
                        action_id=action.id,
                        diagnostics={"original_reason": original_reason, "attempts": list(attempts)},
                    )
                )
                continue

            terminal = self.body.await_action_terminal(action.id, timeout_s=cfg.recovery_detour_timeout_s)
            detour_result = terminal_event_to_tool_result(terminal)
            after = self.body.get_state()
            displacement = dist(before.pos, after.pos)
            displaced = displacement >= cfg.recovery_min_displacement
            success = detour_result.success and displaced
            segment_terminal_reason = detour_result.reason if displaced else "no_displacement"
            attempt = {
                "target": list(target),
                "detour_distance": detour_distance,
                "detour_direction": list(detour_direction),
                "target_y_offset": target_y_offset,
                "target_kind": target_kind,
                "path_moves": [move_kind.value],
                "pulse_kind": pulse_kind,
                "pulse_timeout_s": cfg.recovery_detour_timeout_s,
                "accepted": True,
                "terminal_reason": detour_result.reason,
                "terminal_success": detour_result.success,
                "origin": list(origin),
                "final": list(_block_pos(after)),
                "displacement": displacement,
                "min_displacement": cfg.recovery_min_displacement,
                "displaced": displaced,
            }
            if (
                not displaced
                and cfg.recovery_clearance_enabled
                and target_kind != "water_prep"
                and (detour_result.success or _recoverable_terminal_reason(detour_result.reason))
            ):
                clearance = self._attempt_recovery_clearance(
                    target,
                    goal,
                    goal_payload,
                    cfg,
                    original_reason,
                    before,
                )
                attempt["clearance"] = clearance
                if clearance.get("success"):
                    displacement = float(clearance.get("displacement", 0.0))
                    displaced = displacement >= cfg.recovery_min_displacement
                    success = displaced
                    if "final" in clearance:
                        attempt["final"] = clearance["final"]
                    attempt["displacement"] = displacement
                    attempt["displaced"] = displaced
                    retry = clearance.get("retry")
                    if isinstance(retry, dict):
                        segment_terminal_reason = str(retry.get("reason") or detour_result.reason)
            attempts.append(attempt)
            executed.append(
                ExecutedSegment(
                    index=segment_index,
                    status="recovery_detour",
                    target=target,
                    terminal_reason=segment_terminal_reason if displaced else "no_displacement",
                    success=success,
                    action_id=action.id,
                    diagnostics={"original_reason": original_reason, "attempts": list(attempts)},
                )
            )
            self.progress.note_step(
                ("navigate.recovery_detour", original_reason, target, goal_payload),
                success=success,
                fingerprint=self.progress.fingerprint(after),
                neutral=not success,
            )
            try:
                self.progress.require_can_continue(f"navigate_recovery_detour:{goal_payload}")
            except ProgressAbort as exc:
                return _result(
                    False,
                    "progress_yielded",
                    True,
                    goal,
                    executed,
                    {"error": str(exc), "navigation_goal": goal_payload},
                )
            if success:
                return None
        return None

    def _execute_open_step(
        self,
        step: PathStep,
        cfg: NavigationRunConfig,
        goal: Position,
        executed: list[ExecutedSegment],
        *,
        segment_index: int,
        segment: NavigationSegment,
    ) -> ToolResult | None:
        target_pos = step.interaction_target or step.pos
        block = self.body.perceive("blockAt", {"x": target_pos[0], "y": target_pos[1], "z": target_pos[2]})
        if not (block.ok and block.complete):
            result = ToolResult(
                success=False,
                reason="openable_perception_failed",
                can_retry=True,
                metrics={"target": list(target_pos), "step": {"pos": list(step.pos), "move": step.move.value}},
            )
            executed.append(_terrain_executed(segment_index, segment, step, result.reason, False, result.metrics))
            return _merge_metrics(result, goal, executed)
        props = dict(block.data.get("properties") or {})
        look = _look_at_openable(
            self.body,
            _openable_look_target(target_pos, str(block.data.get("type") or step.block_type), props),
            timeout_s=min(cfg.segment_timeout_s, 2.0),
        )
        if not look.success:
            executed.append(_terrain_executed(segment_index, segment, step, look.reason, False, look.metrics))
            return _merge_metrics(look, goal, executed)
        used = _use_item_once(self.body, timeout_s=cfg.segment_timeout_s)
        if not used.success:
            executed.append(_terrain_executed(segment_index, segment, step, used.reason, False, used.metrics))
            return _merge_metrics(used, goal, executed)
        after = self.body.perceive("blockAt", {"x": target_pos[0], "y": target_pos[1], "z": target_pos[2]})
        if not (after.ok and after.complete):
            result = ToolResult(
                success=False,
                reason="openable_perception_failed",
                can_retry=True,
                metrics={"target": list(target_pos), "step": {"pos": list(step.pos), "move": step.move.value}},
            )
            executed.append(_terrain_executed(segment_index, segment, step, result.reason, False, result.metrics))
            return _merge_metrics(result, goal, executed)
        expected = step.open_expected_properties or {"open": "true"}
        after_props = {str(key): str(value).lower() for key, value in dict(after.data.get("properties") or {}).items()}
        if any(after_props.get(key) != str(value).lower() for key, value in expected.items()):
            result = ToolResult(
                success=False,
                reason="openable_no_effect",
                can_retry=True,
                metrics={
                    "target": list(target_pos),
                    "expected_properties": dict(expected),
                    "observed_properties": after_props,
                    "step": {"pos": list(step.pos), "move": step.move.value},
                },
            )
            executed.append(_terrain_executed(segment_index, segment, step, result.reason, False, result.metrics))
            return _merge_metrics(result, goal, executed)
        result = ToolResult(
            success=True,
            reason="opened",
            can_retry=False,
            metrics={
                "target": list(target_pos),
                "expected_properties": dict(expected),
                "observed_properties": after_props,
                "look": look.to_payload(),
                "use": used.to_payload(),
            },
        )
        executed.append(_terrain_executed(segment_index, segment, step, result.reason, True, result.metrics))
        return None

    def _attempt_recovery_clearance(
        self,
        target: Position,
        goal: Position,
        goal_payload: dict[str, object],
        cfg: NavigationRunConfig,
        original_reason: str,
        before: BodyState,
    ) -> dict[str, object]:
        if self.work is None:
            return {"attempted": False, "success": False, "reason": "recovery_clearance_runtime_missing"}

        mined = self.work.mine_block(
            target,
            context=BreakContext.RECOVERY,
            timeout_s=cfg.recovery_detour_timeout_s,
        )
        clearance: dict[str, object] = {
            "attempted": True,
            "target": list(target),
            "result": mined.to_payload(),
            "success": False,
        }
        if not mined.success:
            clearance["reason"] = mined.reason
            return clearance

        retry = Action.create(
            "moveTo",
            {
                "target": list(target),
                "waypoints": [list(target)],
                "final_goal": list(goal),
                "navigation_goal": goal_payload,
                "segment_status": "recovery_detour_clearance",
                "break_context": BreakContext.RECOVERY.value,
                "path_steps": 1,
                "path_moves": [MoveKind.WALK.value],
                "recovery_reason": original_reason,
            },
        )
        accepted = self.body.execute(retry)
        rejected = _acceptance_failure(accepted, "moveTo", target)
        if rejected is not None:
            clearance["retry"] = rejected.to_payload()
            clearance["reason"] = rejected.reason
            return clearance

        terminal = self.body.await_action_terminal(retry.id, timeout_s=cfg.recovery_detour_timeout_s)
        retry_result = terminal_event_to_tool_result(terminal)
        after = self.body.get_state()
        displacement = dist(before.pos, after.pos)
        displaced = displacement >= cfg.recovery_min_displacement
        clearance["retry"] = retry_result.to_payload()
        clearance["final"] = list(_block_pos(after))
        clearance["displacement"] = displacement
        clearance["min_displacement"] = cfg.recovery_min_displacement
        clearance["displaced"] = displaced
        clearance["success"] = retry_result.success and displaced
        if not clearance["success"]:
            clearance["reason"] = "no_displacement" if retry_result.success else retry_result.reason
        return clearance

    def _execute_terrain_step(
        self,
        step: PathStep,
        break_context: BreakContext | str,
        cfg: NavigationRunConfig,
        goal: Position,
        executed: list[ExecutedSegment],
        *,
        break_steps_used: int,
        segment_index: int,
        segment: NavigationSegment,
    ) -> ToolResult | None:
        if step.move == MoveKind.BREAK:
            if cfg.max_break_steps is not None and break_steps_used >= cfg.max_break_steps:
                executed.append(_planned_only(segment_index, segment))
                return _result(
                    False,
                    "navigation_break_budget_exhausted",
                    False,
                    goal,
                    executed,
                    {
                        "break_steps_used": break_steps_used,
                        "max_break_steps": cfg.max_break_steps,
                        "attempted_break": list(step.pos),
                    },
                )
            if self.work is None:
                return _result(False, "terrain_break_runtime_missing", True, goal, executed)
            result = self.work.mine_block(
                step.pos,
                context=break_context,
                timeout_s=cfg.segment_timeout_s,
            )
            if not result.success:
                executed.append(_terrain_executed(segment_index, segment, step, result.reason, False, result.metrics))
                return _merge_metrics(result, goal, executed)
            executed.append(_terrain_executed(segment_index, segment, step, result.reason, True, result.metrics))
            return None

        if step.move == MoveKind.PLACE:
            if self.work is None:
                return _result(False, "terrain_place_runtime_missing", True, goal, executed)
            result = self.work.place_block(
                step.pos,
                _place_block_type(step),
                face=step.place_face,
                context=PlaceContext.TRAVEL,
                purpose="scaffold",
                timeout_s=cfg.segment_timeout_s,
            )
            if not result.success:
                executed.append(_terrain_executed(segment_index, segment, step, result.reason, False, result.metrics))
                return _merge_metrics(result, goal, executed)
            executed.append(_terrain_executed(segment_index, segment, step, result.reason, True, result.metrics))
            return None

        if step.move == MoveKind.PILLAR:
            if self.work is None:
                return _result(False, "terrain_pillar_runtime_missing", True, goal, executed)
            result = self.work.dig_up_one(
                current_pos=(step.pos[0], step.pos[1] - 1, step.pos[2]),
                context=break_context,
                timeout_s=cfg.segment_timeout_s,
            )
            if not result.success:
                executed.append(_terrain_executed(segment_index, segment, step, result.reason, False, result.metrics))
                return _merge_metrics(result, goal, executed)
            executed.append(_terrain_executed(segment_index, segment, step, result.reason, True, result.metrics))
            return None

        if step.move == MoveKind.DOWNWARD:
            if self.work is None:
                return _result(False, "terrain_downward_runtime_missing", True, goal, executed)
            result = self.work.dig_down_to_y(
                step.pos[1],
                current_pos=(step.pos[0], step.pos[1] + 1, step.pos[2]),
                context=break_context,
                max_steps=1,
                dig_timeout_s=cfg.segment_timeout_s,
                move_timeout_s=cfg.segment_timeout_s,
            )
            if not result.success:
                executed.append(_terrain_executed(segment_index, segment, step, result.reason, False, result.metrics))
                return _merge_metrics(result, goal, executed)
            executed.append(_terrain_executed(segment_index, segment, step, result.reason, True, result.metrics))
            return None

        return None


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


def make_block_at_prism_world_update(
    body: Body,
    *,
    lateral_margin: int = 1,
    y_offsets: tuple[int, ...] = (-1, 0, 1),
    max_cells: int = 192,
    tile_width: int = 4,
    tile_depth: int = 4,
    max_tiles: int = 16,
    forward_axis_limit: int | None = None,
) -> Callable[[object, NavigationSegment], dict[str, object] | None]:
    """Build a bounded authoritative local-world refresh from `blockAt`.

    This is the first shared Body-side continuation adapter for navigation:
    after a safe partial segment, re-read a small prism between the reached
    target and the original goal from authoritative `blockAt` facts, then
    overwrite the local planner grid with those facts. It is intentionally
    bounded and additive; broader chunk paging remains separate debt.
    """

    if lateral_margin < 0:
        raise ValueError("lateral_margin must be >= 0")
    if not y_offsets:
        raise ValueError("y_offsets must not be empty")
    if len(set(y_offsets)) != len(y_offsets):
        raise ValueError("y_offsets must not contain duplicates")
    if max_cells < 1:
        raise ValueError("max_cells must be >= 1")
    if tile_width < 1:
        raise ValueError("tile_width must be >= 1")
    if tile_depth < 1:
        raise ValueError("tile_depth must be >= 1")
    if max_tiles < 1:
        raise ValueError("max_tiles must be >= 1")
    if forward_axis_limit is not None and forward_axis_limit < 1:
        raise ValueError("forward_axis_limit must be >= 1")

    def refresh(navigator: object, segment: NavigationSegment) -> dict[str, object] | None:
        if segment.target is None:
            return {
                "source": "authoritative_block_at_prism_refresh",
                "refreshed_cells": 0,
                "reason": "no_target",
            }
        world = getattr(navigator, "world", None)
        cells = getattr(world, "cells", None)
        if not isinstance(cells, dict):
            raise ValueError("navigator world does not expose mutable cells")

        goal = _segment_original_goal(segment) or segment.target
        refresh_goal = _bounded_refresh_goal(
            segment.target,
            goal,
            forward_axis_limit=forward_axis_limit,
        )
        positions = _block_refresh_positions(
            segment.target,
            refresh_goal,
            lateral_margin=lateral_margin,
            y_offsets=y_offsets,
        )
        if len(positions) > max_cells:
            raise ValueError(
                f"authoritative blockAt prism refresh exceeds max_cells: {len(positions)} > {max_cells}"
            )
        read = read_block_cells_tiled(
            body,
            positions,
            tile_width=tile_width,
            tile_depth=tile_depth,
            max_tiles=max_tiles,
            failure_label="refresh",
        )
        added_positions: list[Position] = []
        tile_added_counts: dict[int, int] = {}
        for pos, cell in read.cells.items():
            previous = cells.get(pos)
            cells[pos] = cell
            if previous is None:
                added_positions.append(pos)
                tile_index = _tile_index_for_pos(
                    pos,
                    tile_width=tile_width,
                    tile_depth=tile_depth,
                    tiles=read.diagnostics.get("tiles") or [],
                )
                if tile_index is not None:
                    tile_added_counts[tile_index] = tile_added_counts.get(tile_index, 0) + 1

        tile_summaries: list[dict[str, object]] = []
        for tile in read.diagnostics.get("tiles") or []:
            enriched = dict(tile)
            enriched["added_cells"] = tile_added_counts.get(int(tile.get("index", -1)), 0)
            tile_summaries.append(enriched)

        return {
            "source": "authoritative_block_at_prism_refresh",
            "segment_target": list(segment.target),
            "goal": list(goal),
            "refresh_goal": list(refresh_goal),
            "refreshed_cells": read.diagnostics["refreshed_cells"],
            "added_cells": len(added_positions),
            "clear_cells": read.diagnostics["clear_cells"],
            "solid_cells": read.diagnostics["solid_cells"],
            "liquid_cells": read.diagnostics["liquid_cells"],
            "complete": read.diagnostics["complete"],
            "lateral_margin": lateral_margin,
            "y_offsets": list(y_offsets),
            "max_cells": max_cells,
            "tile_width": read.diagnostics["tile_width"],
            "tile_depth": read.diagnostics["tile_depth"],
            "max_tiles": read.diagnostics["max_tiles"],
            "tile_count": read.diagnostics["tile_count"],
            "forward_axis_limit": forward_axis_limit,
            "tiles": tile_summaries,
            "elapsed_ms": read.diagnostics["elapsed_ms"],
        }

    return refresh


def _first_action_step(path: tuple[PathStep, ...]) -> PathStep | None:
    for step in path:
        if step.move in TERRAIN_ACTION_MOVES or step.move == MoveKind.OPEN:
            return step
    return None


def _walk_positions(path: tuple[PathStep, ...]) -> tuple[Position, ...]:
    return tuple(step.pos for step in path if step.move in WAYPOINT_MOVES)


def _recovery_detour_candidates(
    origin: Position,
    goal: Position,
    *,
    distances: tuple[int, ...],
    offsets: tuple[tuple[int, int], ...],
    y_offsets: tuple[int, ...],
    max_attempts: int,
    world: GridWorld | None = None,
) -> tuple[tuple[Position, int, tuple[int, int], int, str], ...]:
    if max_attempts <= 0 or not offsets or not distances:
        return ()

    goal_x, _, goal_z = goal
    nearest_distance = distances[0]
    ordered_directions = sorted(
        offsets,
        key=lambda direction: (
            abs(goal_x - (origin[0] + (direction[0] * nearest_distance)))
            + abs(goal_z - (origin[2] + (direction[1] * nearest_distance))),
            direction[0],
            direction[1],
        ),
    )

    candidates: list[tuple[Position, int, tuple[int, int], int, str]] = []
    seen: set[Position] = set()
    for direction in ordered_directions:
        dx, dz = direction
        for detour_distance in distances:
            target, target_y_offset, target_kind = _recovery_detour_target(
                origin,
                direction,
                detour_distance,
                y_offsets=y_offsets,
                world=world,
            )
            if target == origin or target in seen:
                continue
            candidates.append((target, detour_distance, direction, target_y_offset, target_kind))
            seen.add(target)
            if len(candidates) >= max_attempts:
                return tuple(candidates)

    return tuple(candidates)


def _recovery_detour_target(
    origin: Position,
    direction: tuple[int, int],
    detour_distance: int,
    *,
    y_offsets: tuple[int, ...],
    world: GridWorld | None,
) -> tuple[Position, int, str]:
    dx, dz = direction
    base_x = origin[0] + (dx * detour_distance)
    base_z = origin[2] + (dz * detour_distance)
    if world is not None:
        for y_offset in y_offsets:
            target = (base_x, origin[1] + y_offset, base_z)
            if _recovery_target_is_standable(world, target):
                kind = "same_level" if y_offset == 0 else ("support_step_up" if y_offset > 0 else "support_step_down")
                return target, y_offset, kind
            if _recovery_target_is_swimmable(world, target):
                return target, y_offset, "water_prep"
    return (base_x, origin[1], base_z), 0, "fallback_raw"


def _recovery_target_is_standable(world: GridWorld, pos: Position) -> bool:
    cell = world.cell_at(pos)
    if cell is None or not cell.walkable or cell.liquid:
        return False
    support = world.cell_at((pos[0], pos[1] - 1, pos[2]))
    if support is None or support.walkable or support.liquid:
        return False
    headroom = world.cell_at((pos[0], pos[1] + 1, pos[2]))
    if headroom is not None and (not headroom.walkable or headroom.liquid):
        return False
    if headroom is None and cell.headroom_block:
        return False
    return True


def _recovery_target_is_swimmable(world: GridWorld, pos: Position) -> bool:
    cell = world.cell_at(pos)
    if cell is None or not cell.liquid:
        return False
    headroom = world.cell_at((pos[0], pos[1] + 1, pos[2]))
    if headroom is not None and (not headroom.walkable or headroom.liquid):
        return False
    return True


def _movement_waypoint(step: PathStep) -> Position:
    if step.move == MoveKind.FALL and step.fall_depth > 1:
        x, y, z = step.pos
        return (x, y - (step.fall_depth - 1), z)
    return step.pos


def _prefix_before_step(path: tuple[PathStep, ...], target_step: PathStep) -> tuple[PathStep, ...]:
    out: list[PathStep] = []
    for step in path:
        if step is target_step:
            break
        out.append(step)
    return tuple(out)


def _suffix_after_step(path: tuple[PathStep, ...], target_step: PathStep) -> tuple[PathStep, ...]:
    out: list[PathStep] = []
    seen = False
    for step in path:
        if seen:
            out.append(step)
        elif step is target_step:
            seen = True
    return tuple(out)


def _segment_for_path_prefix(segment: NavigationSegment, prefix: tuple[PathStep, ...]) -> NavigationSegment:
    if not prefix:
        raise ValueError("prefix must not be empty")
    prefix_target = prefix[-1].pos
    prefix_plan = replace(
        segment.plan,
        path=prefix,
        success=False,
        reason="prefix_before_action",
        cost=sum(step.cost for step in prefix),
    )
    diagnostics = dict(segment.diagnostics)
    diagnostics["prefix_before_action"] = True
    diagnostics["action_target"] = list(segment.plan.path[len(prefix)].pos)
    return replace(
        segment,
        status="advanced",
        target=prefix_target,
        plan=prefix_plan,
        recheck=None,
        diagnostics=diagnostics,
    )


def _segment_original_goal(segment: NavigationSegment) -> Position | None:
    raw = segment.plan.diagnostics.get("original_goal")
    if isinstance(raw, (list, tuple)) and len(raw) == 3:
        return (int(raw[0]), int(raw[1]), int(raw[2]))
    return None


def _block_refresh_positions(
    start: Position,
    goal: Position,
    *,
    lateral_margin: int,
    y_offsets: tuple[int, ...],
) -> tuple[Position, ...]:
    x_min = min(start[0], goal[0]) - lateral_margin
    x_max = max(start[0], goal[0]) + lateral_margin
    z_min = min(start[2], goal[2]) - lateral_margin
    z_max = max(start[2], goal[2]) + lateral_margin
    y_min = min(start[1], goal[1])
    y_max = max(start[1], goal[1])
    out: list[Position] = []
    seen: set[Position] = set()
    for x in range(x_min, x_max + 1):
        for y_base in range(y_min, y_max + 1):
            for y_offset in y_offsets:
                y = y_base + y_offset
                for z in range(z_min, z_max + 1):
                    pos = (x, y, z)
                    if pos in seen:
                        continue
                    out.append(pos)
                    seen.add(pos)
    return tuple(out)


def _bounded_refresh_goal(
    start: Position,
    goal: Position,
    *,
    forward_axis_limit: int | None,
) -> Position:
    if forward_axis_limit is None:
        return goal
    return (
        _clamp_axis_toward(start[0], goal[0], forward_axis_limit),
        _clamp_axis_toward(start[1], goal[1], forward_axis_limit),
        _clamp_axis_toward(start[2], goal[2], forward_axis_limit),
    )


def _clamp_axis_toward(start: int, goal: int, limit: int) -> int:
    delta = goal - start
    if abs(delta) <= limit:
        return goal
    return start + (limit if delta > 0 else -limit)


def _tile_index_for_pos(
    pos: Position,
    *,
    tile_width: int,
    tile_depth: int,
    tiles: list[dict[str, object]],
) -> int | None:
    x_bucket = pos[0] // tile_width
    z_bucket = pos[2] // tile_depth
    for tile in tiles:
        bounds = tile.get("bounds") or {}
        x_bounds = bounds.get("x")
        z_bounds = bounds.get("z")
        if (
            isinstance(x_bounds, list)
            and len(x_bounds) == 2
            and isinstance(z_bounds, list)
            and len(z_bounds) == 2
            and x_bounds[0] // tile_width == x_bucket
            and z_bounds[0] // tile_depth == z_bucket
        ):
            return int(tile.get("index", -1))
    return None


def _planned_only(index: int, segment: NavigationSegment) -> ExecutedSegment:
    return ExecutedSegment(
        index=index,
        status=segment.status,
        target=segment.target,
        terminal_reason=None,
        success=False,
        diagnostics={"segment": _segment_payload(segment), "planned_segment": segment},
    )


def _terrain_executed(
    index: int,
    segment: NavigationSegment,
    step: PathStep,
    terminal_reason: str,
    success: bool,
    metrics: dict[str, object] | None,
) -> ExecutedSegment:
    return ExecutedSegment(
        index=index,
        status=f"terrain_{step.move.value}",
        target=step.pos,
        terminal_reason=terminal_reason,
        success=success,
        action_id=None,
        diagnostics={
            "segment": _segment_payload(segment),
            "terrain_step": {
                "pos": list(step.pos),
                "move": step.move.value,
                "reason": step.reason,
                "cancel_policy": step.cancel_policy,
            },
            "terminal": dict(metrics or {}),
        },
    )


def _last_planned_segment(executed: list[ExecutedSegment]) -> NavigationSegment | None:
    for segment in reversed(executed):
        planned = segment.diagnostics.get("planned_segment")
        if isinstance(planned, NavigationSegment):
            return planned
    return None


def _segment_payload(segment: NavigationSegment) -> dict[str, object]:
    return {
        "status": segment.status,
        "target": list(segment.target) if segment.target is not None else None,
        "plan_reason": segment.plan.reason,
        "plan_success": segment.plan.success,
        "path_steps": len(segment.plan.path),
        "path_moves": [step.move.value for step in segment.plan.path],
        "path_fall_depths": [step.fall_depth for step in segment.plan.path],
        "movement_waypoints": [list(_movement_waypoint(step)) for step in segment.plan.path if step.move in WAYPOINT_MOVES],
        "path_place_faces": [step.place_face for step in segment.plan.path],
        "movement_cancel": _cancel_profile_payload(segment.plan.path),
        "recheck_ok": None if segment.recheck is None else segment.recheck.ok,
        "recheck_reason": None if segment.recheck is None else segment.recheck.reason,
        "plan_diagnostics": dict(segment.plan.diagnostics),
        "diagnostics": dict(segment.diagnostics),
    }


def _cancel_profile_payload(path: tuple[PathStep, ...]) -> dict[str, object]:
    unsafe_steps = [
        {
            "index": index,
            "pos": list(step.pos),
            "move": step.move.value,
            "policy": step.cancel_policy,
        }
        for index, step in enumerate(path)
        if not step.safe_to_cancel
    ]
    return {
        "safe_to_cancel": not unsafe_steps,
        "unsafe_count": len(unsafe_steps),
        "unsafe_steps": unsafe_steps,
        "policies": [step.cancel_policy for step in path],
    }


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


def _merge_metrics(result: ToolResult, goal: Position, executed: list[ExecutedSegment]) -> ToolResult:
    metrics = dict(result.metrics or {})
    metrics["goal"] = list(goal)
    metrics["segments"] = [_executed_payload(segment) for segment in executed]
    metrics["segment_count"] = len(executed)
    return ToolResult(
        success=result.success,
        reason=result.reason,
        can_retry=result.can_retry,
        next_suggestion=result.next_suggestion,
        metrics=metrics,
    )


def _look_at_openable(body: Body, target: tuple[float, float, float], *, timeout_s: float) -> ToolResult:
    action = Action.create("lookAt", {"target": list(target)})
    accepted = body.execute(action)
    rejected = _acceptance_failure(accepted, "lookAt", (round(target[0]), round(target[1]), round(target[2])))
    if rejected is not None:
        return rejected
    terminal = body.await_action_terminal(action.id, timeout_s=timeout_s)
    result = terminal_event_to_tool_result(terminal)
    return ToolResult(
        success=result.success,
        reason=result.reason if result.success else f"look_failed:{result.reason}",
        can_retry=result.can_retry,
        next_suggestion=result.next_suggestion,
        metrics={"action_id": action.id, "target": list(target), **dict(result.metrics or {})},
    )


def _use_item_once(body: Body, *, timeout_s: float) -> ToolResult:
    action = Action.create("useItem", {"mode": "once", "ticks": 1})
    accepted = body.execute(action)
    rejected = _acceptance_failure(accepted, "useItem", (0, 0, 0))
    if rejected is not None:
        return rejected
    terminal = body.await_action_terminal(action.id, timeout_s=timeout_s)
    result = terminal_event_to_tool_result(terminal)
    return ToolResult(
        success=result.success,
        reason=result.reason if result.success else f"use_failed:{result.reason}",
        can_retry=result.can_retry,
        next_suggestion=result.next_suggestion,
        metrics={"action_id": action.id, **dict(result.metrics or {})},
    )


def _recoverable_exhausted_result(result: ToolResult, goal: Position, executed: list[ExecutedSegment]) -> ToolResult:
    metrics = dict(result.metrics or {})
    original_reason = str(metrics.pop("original_reason", result.reason))
    metrics["recovery_exhausted"] = True
    metrics["goal"] = list(goal)
    metrics["segments"] = [_executed_payload(segment) for segment in executed]
    metrics["segment_count"] = len(executed)
    return ToolResult(success=False, reason=original_reason, can_retry=True, metrics=metrics)


def _acceptance_failure(result: Result, action_name: str, target: Position) -> ToolResult | None:
    if result.ok and result.accepted:
        return None
    return ToolResult(
        success=False,
        reason="body_rejected",
        can_retry=True,
        metrics={
            "action": action_name,
            "target": list(target),
            "ok": result.ok,
            "accepted": result.accepted,
            "error": result.error,
            "data": result.data,
        },
    )


def _recoverable_terminal_reason(reason: str) -> bool:
    return reason in {"stuck", "timeout", "deviated"}


def _scarpet_failure_can_fallback(
    reason: str,
    break_context: BreakContext | str,
    cfg: NavigationRunConfig,
) -> bool:
    if not cfg.allow_local_terrain_fallback:
        return False
    if BreakContext(break_context) is not BreakContext.COLLECT_APPROACH:
        return False
    return reason in SCARPET_FALLBACK_REASONS or reason.startswith("navigation_blocked:")


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


def _with_fallback_origin(result: ToolResult, original_reason: str) -> ToolResult:
    metrics = dict(result.metrics or {})
    metrics.setdefault("terrain_fallback_original_reason", original_reason)
    return ToolResult(
        success=result.success,
        reason=result.reason,
        can_retry=result.can_retry,
        next_suggestion=result.next_suggestion,
        metrics=metrics,
    )


def _path_update_probe(
    source: str,
    status: str,
    reason: str,
    *,
    segment: NavigationSegment | None = None,
) -> dict[str, object]:
    category = _path_update_category(source, reason, segment)
    payload: dict[str, object] = {
        "source": source,
        "status": status,
        "reason": reason,
        "category": category,
    }
    if segment is not None:
        payload["segment_status"] = segment.status
        payload["plan_reason"] = segment.plan.reason
        payload["plan_success"] = segment.plan.success
        payload["plan_expanded"] = segment.plan.expanded
        payload["plan_blocked_count"] = segment.plan.diagnostics.get("blocked_count", 0)
        if segment.plan.diagnostics.get("unloaded_boundary_count") is not None:
            payload["unloaded_boundary_count"] = segment.plan.diagnostics.get("unloaded_boundary_count")
        if segment.recheck is not None:
            payload["recheck_reason"] = segment.recheck.reason
            payload["recheck_checked"] = segment.recheck.checked
            payload["recheck_ok"] = segment.recheck.ok
        blocked = segment.plan.diagnostics.get("blocked")
        if isinstance(blocked, list):
            payload["blocked_reasons"] = _blocked_reason_summary(blocked)
    return payload


def _path_update_category(source: str, reason: str, segment: NavigationSegment | None) -> str:
    if segment is not None and segment.plan.diagnostics.get("stop_reason") == "unloaded_boundary":
        return "unloaded_boundary"
    if reason in {"timeout", "segment_budget_exhausted", "partial_segment_budget_exhausted", "expansion_limit"}:
        return "timeout"
    if reason in {"stuck", "deviated"}:
        return reason
    if reason == "unloaded_boundary":
        return "unloaded_boundary"
    if reason == "no_path":
        if segment is not None:
            blocked = segment.plan.diagnostics.get("blocked")
            if isinstance(blocked, list):
                reasons = {str(item.get("reason", "")) for item in blocked if isinstance(item, dict)}
                if reasons and all(item == "unloaded" for item in reasons):
                    return "unloaded_boundary"
                if any(item.startswith(("break_denied:", "place_denied:")) for item in reasons):
                    return "protected_or_denied"
        return "no_path"
    if reason.startswith(("break_denied:", "place_denied:")):
        return "goal_changed_or_world_changed" if source == "recheck" else "protected_or_denied"
    if reason in {"changed", "became_unloaded", "unloaded"}:
        return "goal_changed_or_world_changed"
    if source == "recheck":
        return "goal_changed_or_world_changed"
    return "other"


def _blocked_reason_summary(blocked: list[object]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for item in blocked:
        if not isinstance(item, dict):
            continue
        reason = str(item.get("reason", "unknown"))
        summary[reason] = summary.get(reason, 0) + 1
    return summary


def _place_block_type(step: PathStep) -> str:
    for prefix in ("place_allowed:", "place:"):
        if step.reason.startswith(prefix):
            candidate = step.reason.removeprefix(prefix)
            if candidate and candidate not in {"allowed_place", "unknown"}:
                return candidate
    return "minecraft:cobblestone"


def _apply_executed_terrain_effect(navigator: object, step: PathStep) -> None:
    """Update a local planner grid after authoritative terrain mutation success."""
    world = getattr(navigator, "world", None)
    cells = getattr(world, "cells", None)
    if not isinstance(cells, dict):
        return
    if step.move == MoveKind.BREAK:
        cells[step.pos] = _grid_cell(block_type="air", walkable=True)
    elif step.move == MoveKind.PLACE:
        cells[step.pos] = _grid_cell(block_type=_place_block_type(step), walkable=False)
    elif step.move == MoveKind.OPEN:
        cells[step.pos] = _grid_cell(
            block_type=step.block_type,
            walkable=True,
            headroom_block=step.open_headroom_block,
        )
        if step.block_type.removeprefix("minecraft:").endswith("_door"):
            lower = step.interaction_target or step.pos
            upper = (lower[0], lower[1] + 1, lower[2])
            cells[lower] = _grid_cell(block_type=step.block_type, walkable=True, headroom_block=step.block_type)
            cells[upper] = _grid_cell(block_type=step.block_type, walkable=True)


def _apply_world_update(
    navigator: object,
    segment: NavigationSegment,
    updater: Callable[[object, NavigationSegment], dict[str, object] | None] | None,
    executed: list[ExecutedSegment],
) -> ToolResult | None:
    if updater is None:
        return None
    try:
        facts = updater(navigator, segment)
    except Exception as exc:  # pragma: no cover - defensive boundary for external refresh adapters.
        return ToolResult(
            success=False,
            reason="world_update_failed",
            can_retry=True,
            metrics={"error": str(exc), "segment": _segment_payload(segment)},
        )
    if facts is not None and executed:
        executed[-1].diagnostics["world_update"] = facts
    return None


def _grid_cell(*, block_type: str, walkable: bool, liquid: bool = False, headroom_block: str | None = None):
    from minebot.game.navigation import GridCell

    return GridCell(block_type=block_type, walkable=walkable, liquid=liquid, headroom_block=headroom_block)


def _default_work_runtime(body: Body, navigator: object) -> BlockWork | None:
    costs = getattr(navigator, "costs", None)
    governance = getattr(costs, "governance", None)
    if governance is None:
        return None
    return BlockWork(body, governance)


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
