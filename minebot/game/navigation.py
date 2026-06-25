"""Navigation cost primitives tied to governance.

This is not the full A* implementation. It is the shared cost boundary that
future path search consumes so protected terrain modification is impossible in
the planner, not merely rejected later by runtime guards.
"""

from __future__ import annotations

import heapq
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from math import inf

from minebot.contract import BreakContext, PlaceContext, Position
from minebot.game.governance import GovernancePolicy


class MoveKind(StrEnum):
    WALK = "walk"
    DIAGONAL = "diagonal"
    ASCEND = "ascend"
    PILLAR = "pillar"
    DESCEND = "descend"
    DOWNWARD = "downward"
    SWIM = "swim"
    FALL = "fall"
    BREAK = "break"
    PLACE = "place"
    OPEN = "open"
    INVALID = "invalid"


class NavigationGoalKind(StrEnum):
    BLOCK = "block"
    NEAR = "near"
    XZ = "xz"
    Y_LEVEL = "y_level"
    COMPOSITE = "composite"
    AVOID = "avoid"


class NavigationGoal:
    kind: NavigationGoalKind

    def is_satisfied(self, pos: Position) -> bool:
        raise NotImplementedError

    def representative(self, start: Position) -> Position:
        raise NotImplementedError

    def heuristic(self, pos: Position) -> float:
        return AStarPlanner._heuristic(pos, self.representative(pos))

    def payload(self) -> dict[str, object]:
        return {"kind": self.kind.value}


@dataclass(frozen=True)
class GoalBlock(NavigationGoal):
    pos: Position
    kind: NavigationGoalKind = NavigationGoalKind.BLOCK

    def is_satisfied(self, pos: Position) -> bool:
        return pos == self.pos

    def representative(self, start: Position) -> Position:
        return self.pos

    def payload(self) -> dict[str, object]:
        return {"kind": self.kind.value, "pos": list(self.pos)}


@dataclass(frozen=True)
class GoalNear(NavigationGoal):
    pos: Position
    radius: int = 1
    kind: NavigationGoalKind = NavigationGoalKind.NEAR

    def __post_init__(self) -> None:
        if self.radius < 0:
            raise ValueError("radius must be >= 0")

    def is_satisfied(self, pos: Position) -> bool:
        return AStarPlanner._heuristic(pos, self.pos) <= self.radius

    def representative(self, start: Position) -> Position:
        return self.pos

    def heuristic(self, pos: Position) -> float:
        return max(0.0, AStarPlanner._heuristic(pos, self.pos) - self.radius)

    def payload(self) -> dict[str, object]:
        return {"kind": self.kind.value, "pos": list(self.pos), "radius": self.radius}


@dataclass(frozen=True)
class GoalXZ(NavigationGoal):
    x: int
    z: int
    kind: NavigationGoalKind = NavigationGoalKind.XZ

    def is_satisfied(self, pos: Position) -> bool:
        return pos[0] == self.x and pos[2] == self.z

    def representative(self, start: Position) -> Position:
        return (self.x, start[1], self.z)

    def heuristic(self, pos: Position) -> float:
        return abs(pos[0] - self.x) + abs(pos[2] - self.z)

    def payload(self) -> dict[str, object]:
        return {"kind": self.kind.value, "x": self.x, "z": self.z}


@dataclass(frozen=True)
class GoalYLevel(NavigationGoal):
    y: int
    kind: NavigationGoalKind = NavigationGoalKind.Y_LEVEL

    def is_satisfied(self, pos: Position) -> bool:
        return pos[1] == self.y

    def representative(self, start: Position) -> Position:
        return (start[0], self.y, start[2])

    def heuristic(self, pos: Position) -> float:
        return abs(pos[1] - self.y)

    def payload(self) -> dict[str, object]:
        return {"kind": self.kind.value, "y": self.y}


@dataclass(frozen=True)
class GoalComposite(NavigationGoal):
    goals: tuple[NavigationGoal, ...]
    mode: str = "any"
    kind: NavigationGoalKind = NavigationGoalKind.COMPOSITE

    def __post_init__(self) -> None:
        if not self.goals:
            raise ValueError("composite goal requires at least one child goal")
        if self.mode not in {"any", "all"}:
            raise ValueError("composite mode must be 'any' or 'all'")

    def is_satisfied(self, pos: Position) -> bool:
        if self.mode == "any":
            return any(goal.is_satisfied(pos) for goal in self.goals)
        return all(goal.is_satisfied(pos) for goal in self.goals)

    def representative(self, start: Position) -> Position:
        return min(
            (goal.representative(start) for goal in self.goals),
            key=lambda pos: AStarPlanner._heuristic(start, pos),
        )

    def heuristic(self, pos: Position) -> float:
        values = [goal.heuristic(pos) for goal in self.goals]
        if self.mode == "any":
            return min(values)
        return sum(values)

    def payload(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "mode": self.mode,
            "goals": [goal.payload() for goal in self.goals],
        }


@dataclass(frozen=True)
class GoalAvoid(NavigationGoal):
    pos: Position
    min_distance: int
    fallback: NavigationGoal | None = None
    kind: NavigationGoalKind = NavigationGoalKind.AVOID

    def __post_init__(self) -> None:
        if self.min_distance < 1:
            raise ValueError("min_distance must be >= 1")

    def is_satisfied(self, pos: Position) -> bool:
        return AStarPlanner._heuristic(pos, self.pos) >= self.min_distance and (
            self.fallback is None or self.fallback.is_satisfied(pos)
        )

    def representative(self, start: Position) -> Position:
        if self.fallback is not None:
            return self.fallback.representative(start)
        dx = _sign(start[0] - self.pos[0])
        dz = _sign(start[2] - self.pos[2])
        if dx == 0 and dz == 0:
            dx = 1
        return (self.pos[0] + dx * self.min_distance, start[1], self.pos[2] + dz * self.min_distance)

    def heuristic(self, pos: Position) -> float:
        avoid_distance = AStarPlanner._heuristic(pos, self.pos)
        avoid_need = max(0.0, self.min_distance - avoid_distance)
        if self.fallback is None:
            return avoid_need
        return avoid_need + self.fallback.heuristic(pos)

    def payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "kind": self.kind.value,
            "pos": list(self.pos),
            "min_distance": self.min_distance,
        }
        if self.fallback is not None:
            payload["fallback"] = self.fallback.payload()
        return payload


GoalLike = NavigationGoal | Position


def normalize_goal(goal: GoalLike) -> NavigationGoal:
    if isinstance(goal, NavigationGoal):
        return goal
    return GoalBlock(goal)


def _sign(value: int) -> int:
    if value < 0:
        return -1
    if value > 0:
        return 1
    return 0


def _is_horizontal_diagonal(origin: Position, target: Position) -> bool:
    return target[1] == origin[1] and abs(target[0] - origin[0]) == 1 and abs(target[2] - origin[2]) == 1


def _horizontal_face_from(origin: Position, target: Position) -> str | None:
    dx = target[0] - origin[0]
    dz = target[2] - origin[2]
    if abs(dx) >= abs(dz) and dx > 0:
        return "west"
    if abs(dx) >= abs(dz) and dx < 0:
        return "east"
    if dz > 0:
        return "north"
    if dz < 0:
        return "south"
    return None


def movement_cancel_profile(kind: MoveKind) -> tuple[bool, str]:
    """Return whether a movement can be interrupted immediately without extra stabilization."""
    if kind in {MoveKind.WALK, MoveKind.DIAGONAL}:
        return True, "immediate"
    if kind == MoveKind.DESCEND:
        return True, "after_step"
    if kind == MoveKind.DOWNWARD:
        return False, "finish_or_abort_controller"
    if kind == MoveKind.ASCEND:
        return False, "settle_on_support"
    if kind == MoveKind.PILLAR:
        return False, "finish_or_abort_controller"
    if kind == MoveKind.SWIM:
        return False, "surface_or_stable_water"
    if kind == MoveKind.FALL:
        return False, "land_first"
    if kind in {MoveKind.BREAK, MoveKind.PLACE, MoveKind.OPEN}:
        return False, "finish_or_abort_controller"
    if kind == MoveKind.INVALID:
        return False, "invalid"
    return False, "unknown"


@dataclass(frozen=True)
class MovementCandidate:
    kind: MoveKind
    pos: Position
    block_type: str = "air"
    context: BreakContext | PlaceContext | str = BreakContext.PATH
    place_type: str | None = None
    place_face: str | None = None
    purpose: str = "scaffold"
    fall_depth: int = 0
    interaction_target: Position | None = None


@dataclass(frozen=True)
class CostDecision:
    passable: bool
    cost: float
    reason: str
    kind: MoveKind
    pos: Position
    safe_to_cancel: bool
    cancel_policy: str
    diagnostics: dict[str, object] = field(default_factory=dict)
    place_face: str | None = None
    fall_depth: int = 0
    interaction_target: Position | None = None


@dataclass(frozen=True)
class GridCell:
    block_type: str = "air"
    walkable: bool = True
    liquid: bool = False
    fall_depth: int = 0
    requires_support: bool = False
    headroom_block: str | None = None


FALLING_BLOCK_TYPES = frozenset({"sand", "gravel"})


@dataclass(frozen=True)
class VirtualBlockOverlay:
    """Planned break/place effects used while evaluating one path search."""

    blocks: tuple[tuple[Position, GridCell], ...] = ()

    def cell_at(self, world: "GridWorld", pos: Position) -> GridCell | None:
        for overlay_pos, cell in reversed(self.blocks):
            if overlay_pos == pos:
                return cell
        return world.cell_at(pos)

    def set_cell(self, pos: Position, cell: GridCell) -> "VirtualBlockOverlay":
        return VirtualBlockOverlay((*self.blocks, (pos, cell)))

    def after_step(self, step: "PathStep") -> "VirtualBlockOverlay":
        if step.move == MoveKind.BREAK:
            return self.set_cell(step.pos, GridCell(block_type="air", walkable=True))
        if step.move == MoveKind.PLACE:
            return self.set_cell(step.pos, GridCell(block_type=step.block_type, walkable=False))
        if step.move == MoveKind.OPEN:
            overlay = self.set_cell(
                step.pos,
                GridCell(
                    block_type=step.block_type,
                    walkable=True,
                    liquid=False,
                    headroom_block=step.open_headroom_block,
                ),
            )
            if _is_door_type(step.block_type):
                lower = step.interaction_target or step.pos
                upper = (lower[0], lower[1] + 1, lower[2])
                overlay = overlay.set_cell(lower, GridCell(block_type=step.block_type, walkable=True, liquid=False, headroom_block=step.block_type))
                overlay = overlay.set_cell(upper, GridCell(block_type=step.block_type, walkable=True, liquid=False))
            return overlay
        return self

    def payload(self) -> list[dict[str, object]]:
        return [
            {
                "pos": list(pos),
                "block_type": cell.block_type,
                "walkable": cell.walkable,
                "liquid": cell.liquid,
                "requires_support": cell.requires_support,
                "headroom_block": cell.headroom_block,
            }
            for pos, cell in self.blocks
        ]


@dataclass(frozen=True)
class _SearchState:
    pos: Position
    overlay: VirtualBlockOverlay = field(default_factory=VirtualBlockOverlay)


@dataclass(frozen=True)
class PathStep:
    pos: Position
    move: MoveKind
    cost: float
    reason: str
    block_type: str = "air"
    safe_to_cancel: bool = True
    cancel_policy: str = "immediate"
    virtual_effect: str | None = None
    place_face: str | None = None
    fall_depth: int = 0
    interaction_target: Position | None = None
    open_expected_properties: dict[str, str] | None = None
    open_headroom_block: str | None = None


@dataclass(frozen=True)
class PathResult:
    success: bool
    reason: str
    path: tuple[PathStep, ...] = ()
    cost: float = inf
    expanded: int = 0
    diagnostics: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class RecheckResult:
    ok: bool
    reason: str
    checked: int
    failing_step: PathStep | None = None
    diagnostics: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class NavigationSegment:
    status: str
    target: Position | None
    plan: PathResult
    recheck: RecheckResult | None = None
    diagnostics: dict[str, object] = field(default_factory=dict)


class NavigationCostModel:
    WALK_COST = 1.0
    DIAGONAL_COST = 1.4
    ASCEND_COST = 2.0
    PILLAR_COST = 8.0
    DESCEND_COST = 1.2
    DOWNWARD_COST = 7.0
    SWIM_COST = 3.0
    FALL_COST = 4.0
    NATURAL_BREAK_COST = 6.0
    BOT_CLEANUP_BREAK_COST = 2.0
    PLACE_COST = 3.0
    MAX_SAFE_FALL = 3

    def __init__(self, governance: GovernancePolicy):
        self.governance = governance

    def evaluate(self, candidate: MovementCandidate) -> CostDecision:
        kind = MoveKind(candidate.kind)
        if kind == MoveKind.INVALID:
            safe_to_cancel, cancel_policy = movement_cancel_profile(kind)
            return CostDecision(
                passable=False,
                cost=inf,
                reason=str(candidate.context),
                kind=kind,
                pos=candidate.pos,
                safe_to_cancel=safe_to_cancel,
                cancel_policy=cancel_policy,
            )
        if kind == MoveKind.WALK:
            return self._simple_move(candidate, self.WALK_COST, "walk")
        if kind == MoveKind.DIAGONAL:
            return self._simple_move(candidate, self.DIAGONAL_COST, "diagonal")
        if kind == MoveKind.ASCEND:
            return self._simple_move(candidate, self.ASCEND_COST, "ascend")
        if kind == MoveKind.PILLAR:
            return self._simple_move(candidate, self.PILLAR_COST, "pillar")
        if kind == MoveKind.DESCEND:
            return self._simple_move(candidate, self.DESCEND_COST, "descend")
        if kind == MoveKind.DOWNWARD:
            return self._simple_move(candidate, self.DOWNWARD_COST, "downward")
        if kind == MoveKind.SWIM:
            return self._simple_move(candidate, self.SWIM_COST, "swim")
        if kind == MoveKind.FALL:
            return self._evaluate_fall(candidate)
        if kind == MoveKind.BREAK:
            return self._evaluate_break(candidate)
        if kind == MoveKind.PLACE:
            return self._evaluate_place(candidate)
        if kind == MoveKind.OPEN:
            return self._evaluate_open(candidate)
        raise ValueError(f"unknown movement kind: {candidate.kind}")

    @staticmethod
    def _simple_move(candidate: MovementCandidate, cost: float, reason: str) -> CostDecision:
        safe_to_cancel, cancel_policy = movement_cancel_profile(MoveKind(candidate.kind))
        return CostDecision(
            passable=True,
            cost=cost,
            reason=reason,
            kind=MoveKind(candidate.kind),
            pos=candidate.pos,
            safe_to_cancel=safe_to_cancel,
            cancel_policy=cancel_policy,
        )

    def _evaluate_fall(self, candidate: MovementCandidate) -> CostDecision:
        fall_depth = max(1, candidate.fall_depth)
        if fall_depth > self.MAX_SAFE_FALL:
            return CostDecision(
                passable=False,
                cost=inf,
                reason="fall_denied:unsafe_depth",
                kind=MoveKind.FALL,
                pos=candidate.pos,
                safe_to_cancel=False,
                cancel_policy="land_first",
                diagnostics={"fall_depth": fall_depth, "max_safe_fall": self.MAX_SAFE_FALL},
            )
        return CostDecision(
            passable=True,
            cost=self.FALL_COST + fall_depth,
            reason="fall",
            kind=MoveKind.FALL,
            pos=candidate.pos,
            safe_to_cancel=False,
            cancel_policy="land_first",
            diagnostics={"fall_depth": fall_depth, "max_safe_fall": self.MAX_SAFE_FALL},
            fall_depth=fall_depth,
        )

    def _evaluate_break(self, candidate: MovementCandidate) -> CostDecision:
        context = BreakContext(candidate.context)
        decision = self.governance.can_break(candidate.pos, candidate.block_type, context)
        if not decision.allowed:
            return CostDecision(
                passable=False,
                cost=inf,
                reason=f"break_denied:{decision.reason}",
                kind=MoveKind.BREAK,
                pos=candidate.pos,
                safe_to_cancel=False,
                cancel_policy="finish_or_abort_controller",
                diagnostics={"legality": asdict(decision)},
            )
        cost = self.BOT_CLEANUP_BREAK_COST if decision.bot_owned else self.NATURAL_BREAK_COST
        return CostDecision(
            passable=True,
            cost=cost,
            reason=f"break_allowed:{decision.reason}",
            kind=MoveKind.BREAK,
            pos=candidate.pos,
            safe_to_cancel=False,
            cancel_policy="finish_or_abort_controller",
            diagnostics={"legality": asdict(decision), "context": context.value},
        )

    def _evaluate_place(self, candidate: MovementCandidate) -> CostDecision:
        context = PlaceContext(candidate.context)
        block_type = candidate.place_type or candidate.block_type
        decision = self.governance.can_place(candidate.pos, block_type, context, bot="planner")
        if not decision.allowed:
            return CostDecision(
                passable=False,
                cost=inf,
                reason=f"place_denied:{decision.reason}",
                kind=MoveKind.PLACE,
                pos=candidate.pos,
                safe_to_cancel=False,
                cancel_policy="finish_or_abort_controller",
                diagnostics={"legality": asdict(decision)},
            )
        return CostDecision(
            passable=True,
            cost=self.PLACE_COST,
            reason=f"place_allowed:{decision.reason}",
            kind=MoveKind.PLACE,
            pos=candidate.pos,
            safe_to_cancel=False,
            cancel_policy="finish_or_abort_controller",
            diagnostics={
                "legality": asdict(decision),
                "context": context.value,
                "block_type": block_type,
                "purpose": candidate.purpose,
            },
            place_face=candidate.place_face,
        )

    def _evaluate_open(self, candidate: MovementCandidate) -> CostDecision:
        safe_to_cancel, cancel_policy = movement_cancel_profile(MoveKind.OPEN)
        return CostDecision(
            passable=True,
            cost=self.WALK_COST,
            reason="open_allowed",
            kind=MoveKind.OPEN,
            pos=candidate.pos,
            safe_to_cancel=safe_to_cancel,
            cancel_policy=cancel_policy,
            diagnostics={"block_type": candidate.block_type},
            interaction_target=candidate.interaction_target,
        )


def _with_cost_diagnostics(
    decision: CostDecision,
    cost: float,
    *,
    backtrack_favored: bool,
    backtrack_cost_factor: float,
) -> CostDecision:
    diagnostics = dict(decision.diagnostics)
    if backtrack_favored:
        diagnostics["backtrack_favored"] = True
        diagnostics["base_cost"] = decision.cost
        diagnostics["backtrack_cost_factor"] = backtrack_cost_factor
    return replace(decision, cost=cost, diagnostics=diagnostics)


class GridWorld:
    """Small loaded-local grid for path computation tests and early planners."""

    def __init__(self, cells: dict[Position, GridCell]):
        self.cells = dict(cells)

    def cell_at(self, pos: Position) -> GridCell | None:
        return self.cells.get(pos)


class AStarPlanner:
    """Minimal A* that consumes NavigationCostModel for terrain changes."""

    PARTIAL_BACKOFF_COEFFICIENTS = (1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 10.0)
    DEFAULT_MIN_PARTIAL_PROGRESS = 5
    HEURISTIC_WEIGHT = 3.563

    def __init__(self, world: GridWorld, costs: NavigationCostModel):
        self.world = world
        self.costs = costs

    def plan(
        self,
        start: Position,
        goal: GoalLike,
        *,
        break_context: BreakContext | str = BreakContext.TRAVEL,
        max_expansions: int = 4096,
        allow_partial: bool = True,
        min_partial_progress: int = DEFAULT_MIN_PARTIAL_PROGRESS,
        previous_segment: tuple[Position, ...] = (),
        backtrack_cost_factor: float = 0.5,
        unloaded_boundary_limit: int | None = None,
    ) -> PathResult:
        if not 0 < backtrack_cost_factor <= 1:
            raise ValueError("backtrack_cost_factor must be > 0 and <= 1")
        if unloaded_boundary_limit is not None and unloaded_boundary_limit < 1:
            raise ValueError("unloaded_boundary_limit must be >= 1")
        start_state = _SearchState(start, VirtualBlockOverlay())
        frontier: list[tuple[float, int, _SearchState]] = []
        heapq.heappush(frontier, (0.0, 0, start_state))
        came_from: dict[_SearchState, tuple[_SearchState, CostDecision, str, str | None]] = {}
        cost_so_far: dict[_SearchState, float] = {start_state: 0.0}
        blocked: list[dict[str, object]] = []
        unloaded_boundary_count = 0
        stopped_reason: str | None = None
        expanded = 0
        tie = 0
        nav_goal = normalize_goal(goal)
        representative_goal = nav_goal.representative(start)
        start_distance = nav_goal.heuristic(start)
        partial_candidates: dict[float, tuple[float, Position, float, float]] = {}
        backtrack_nodes = set(previous_segment)

        while frontier and expanded < max_expansions:
            _priority, _tie, current_state = heapq.heappop(frontier)
            current = current_state.pos
            expanded += 1
            current_distance = nav_goal.heuristic(current)
            progress = start_distance - current_distance
            if current != start and progress >= min_partial_progress:
                self._note_partial_candidates(
                    partial_candidates,
                    current,
                    cost_so_far[current_state],
                    current_distance,
                    progress,
                )
            if nav_goal.is_satisfied(current):
                return self._build_result(
                    start_state,
                    current_state,
                    came_from,
                    cost_so_far[current_state],
                    expanded,
                    success=True,
                    reason="arrived",
                    diagnostics={"partial": False, "goal": nav_goal.payload()},
                )

            for next_pos in self._neighbors(current):
                cell = current_state.overlay.cell_at(self.world, next_pos)
                if cell is None:
                    if _is_horizontal_diagonal(current, next_pos):
                        continue
                    unloaded_boundary_count += 1
                    blocked.append({"pos": list(next_pos), "reason": "unloaded"})
                    if (
                        unloaded_boundary_limit is not None
                        and unloaded_boundary_count >= unloaded_boundary_limit
                    ):
                        stopped_reason = "unloaded_boundary"
                        frontier.clear()
                        break
                    continue
                candidate = self._candidate_for(current_state, next_pos, cell, break_context)
                decision = self.costs.evaluate(candidate)
                if not decision.passable:
                    blocked.append({"pos": list(next_pos), "reason": decision.reason})
                    continue
                virtual_effect = self._virtual_effect_for(decision)
                next_overlay = self._overlay_after_decision(current_state.overlay, decision, cell)
                next_state = self._next_state_after_decision(current_state, next_pos, next_overlay, decision)
                step_cost = decision.cost
                backtrack_favored = next_pos in backtrack_nodes
                if backtrack_favored:
                    step_cost *= backtrack_cost_factor
                new_cost = cost_so_far[current_state] + step_cost
                if next_state not in cost_so_far or new_cost < cost_so_far[next_state]:
                    cost_so_far[next_state] = new_cost
                    tie += 1
                    priority = new_cost + self.HEURISTIC_WEIGHT * nav_goal.heuristic(next_pos)
                    heapq.heappush(frontier, (priority, tie, next_state))
                    came_from[next_state] = (
                        current_state,
                        _with_cost_diagnostics(
                            decision,
                            step_cost,
                            backtrack_favored=backtrack_favored,
                            backtrack_cost_factor=backtrack_cost_factor,
                        ),
                        self._step_block_type(current_state.overlay, decision, next_pos, cell),
                        virtual_effect,
                    )
            if stopped_reason is not None:
                break

        reason = stopped_reason or ("expansion_limit" if expanded >= max_expansions else "no_path")
        partial_choice = self._choose_partial_candidate(partial_candidates)
        if allow_partial and partial_choice is not None:
            best_pos, progress, selected_coefficient = partial_choice
            best_state = min(
                (state for state in cost_so_far if state.pos == best_pos),
                key=lambda state: cost_so_far[state],
            )
            return self._build_result(
                start_state,
                best_state,
                came_from,
                cost_so_far[best_state],
                expanded,
                success=False,
                reason="partial",
                diagnostics={
                    "partial": True,
                    "partial_target": list(best_pos),
                    "original_goal": list(representative_goal),
                    "goal": nav_goal.payload(),
                    "progress": progress,
                    "min_partial_progress": min_partial_progress,
                    "partial_backoff_coefficients": list(self.PARTIAL_BACKOFF_COEFFICIENTS),
                    "selected_coefficient": selected_coefficient,
                    "stop_reason": reason,
                    "blocked": blocked[:64],
                    "blocked_count": len(blocked),
                    "unloaded_boundary_count": unloaded_boundary_count,
                    "unloaded_boundary_limit": unloaded_boundary_limit,
                },
            )
        return PathResult(
            success=False,
            reason=reason,
            expanded=expanded,
            diagnostics={
                "blocked": blocked[:64],
                "blocked_count": len(blocked),
                "goal": nav_goal.payload(),
                "unloaded_boundary_count": unloaded_boundary_count,
                "unloaded_boundary_limit": unloaded_boundary_limit,
            },
        )

    def _note_partial_candidates(
        self,
        candidates: dict[float, tuple[float, Position, float, float]],
        pos: Position,
        path_cost: float,
        remaining_distance: float,
        progress: float,
    ) -> None:
        for coefficient in self.PARTIAL_BACKOFF_COEFFICIENTS:
            score = path_cost + coefficient * remaining_distance
            current = candidates.get(coefficient)
            if current is None or score < current[0] or (score == current[0] and progress > current[3]):
                candidates[coefficient] = (score, pos, remaining_distance, progress)

    @staticmethod
    def _choose_partial_candidate(
        candidates: dict[float, tuple[float, Position, float, float]]
    ) -> tuple[Position, float, float] | None:
        if not candidates:
            return None
        selected_coefficient, (_score, pos, _remaining_distance, progress) = min(
            candidates.items(),
            key=lambda item: (item[1][2], item[0], item[1][0]),
        )
        return pos, progress, selected_coefficient

    def _step_block_type(
        self,
        overlay: VirtualBlockOverlay,
        decision: CostDecision,
        next_pos: Position,
        cell: GridCell,
    ) -> str:
        if decision.pos == next_pos:
            return cell.block_type
        observed = overlay.cell_at(self.world, decision.pos)
        if observed is not None:
            return observed.block_type
        return cell.block_type

    def _build_result(
        self,
        start: "_SearchState",
        goal: "_SearchState",
        came_from: dict["_SearchState", tuple["_SearchState", CostDecision, str, str | None]],
        total_cost: float,
        expanded: int,
        success: bool,
        reason: str,
        diagnostics: dict[str, object] | None = None,
    ) -> PathResult:
        steps: list[PathStep] = []
        current = goal
        while current != start:
            prev, decision, block_type, virtual_effect = came_from[current]
            steps.append(
                PathStep(
                    pos=decision.pos,
                    move=decision.kind,
                    cost=decision.cost,
                    reason=decision.reason,
                    block_type=block_type,
                    safe_to_cancel=decision.safe_to_cancel,
                    cancel_policy=decision.cancel_policy,
                    virtual_effect=virtual_effect,
                    place_face=decision.place_face,
                    fall_depth=decision.fall_depth,
                    interaction_target=decision.interaction_target,
                    open_expected_properties=(
                        {"open": "true"} if decision.kind == MoveKind.OPEN else None
                    ),
                    open_headroom_block=(
                        None if decision.kind != MoveKind.OPEN else _open_headroom_block_type(block_type)
                    ),
                )
            )
            current = prev
        steps.reverse()
        return PathResult(
            success=success,
            reason=reason,
            path=tuple(steps),
            cost=total_cost,
            expanded=expanded,
            diagnostics={"path_start": list(start.pos), **(diagnostics or {})},
        )

    @staticmethod
    def _neighbors(pos: Position) -> tuple[Position, ...]:
        x, y, z = pos
        return (
            (x + 1, y, z),
            (x - 1, y, z),
            (x, y, z + 1),
            (x, y, z - 1),
            (x + 1, y, z + 1),
            (x + 1, y, z - 1),
            (x - 1, y, z + 1),
            (x - 1, y, z - 1),
            (x + 1, y + 1, z),
            (x - 1, y + 1, z),
            (x, y + 1, z + 1),
            (x, y + 1, z - 1),
            (x + 1, y - 1, z),
            (x - 1, y - 1, z),
            (x, y - 1, z + 1),
            (x, y - 1, z - 1),
            (x, y + 1, z),
            (x, y - 1, z),
        )

    def _candidate_for(
        self,
        current: _SearchState,
        pos: Position,
        cell: GridCell,
        break_context: BreakContext | str,
    ) -> MovementCandidate:
        if cell.requires_support and not self._has_support(current.overlay, pos):
            return MovementCandidate(
                kind=MoveKind.PLACE,
                pos=(pos[0], pos[1] - 1, pos[2]),
                block_type="cobblestone",
                context=PlaceContext.TRAVEL,
                place_face=_horizontal_face_from(current.pos, pos),
            )
        blocked_headroom = self._blocked_headroom(current.overlay, pos, cell)
        if blocked_headroom is not None:
            headroom_pos, block_type = blocked_headroom
            headroom_cell = current.overlay.cell_at(self.world, headroom_pos) or GridCell(
                block_type=block_type,
                walkable=False,
            )
            gravity_hazard = self._gravity_break_hazard(current.overlay, headroom_pos, headroom_cell)
            if gravity_hazard is not None:
                return gravity_hazard
            return MovementCandidate(kind=MoveKind.BREAK, pos=headroom_pos, block_type=block_type, context=break_context)
        if cell.walkable:
            dy = pos[1] - current.pos[1]
            dx = pos[0] - current.pos[0]
            dz = pos[2] - current.pos[2]
            if cell.liquid:
                return MovementCandidate(kind=MoveKind.SWIM, pos=pos, block_type=cell.block_type)
            if dy > 0:
                if dx == 0 and dz == 0:
                    return MovementCandidate(kind=MoveKind.PILLAR, pos=pos, block_type=cell.block_type)
                return MovementCandidate(kind=MoveKind.ASCEND, pos=pos, block_type=cell.block_type)
            if dy < 0:
                if cell.fall_depth > 1:
                    return MovementCandidate(
                        kind=MoveKind.FALL,
                        pos=pos,
                        block_type=cell.block_type,
                        fall_depth=cell.fall_depth,
                    )
                return MovementCandidate(kind=MoveKind.DESCEND, pos=pos, block_type=cell.block_type)
            if dx != 0 and dz != 0:
                diagonal_block = self._diagonal_blocker(current.overlay, current.pos, pos, break_context)
                if diagonal_block is not None:
                    return diagonal_block
                return MovementCandidate(kind=MoveKind.DIAGONAL, pos=pos, block_type=cell.block_type)
            return MovementCandidate(kind=MoveKind.WALK, pos=pos, block_type=cell.block_type)
        open_candidate = self._openable_pass_candidate(current.overlay, current.pos, pos, cell)
        if open_candidate is not None:
            return open_candidate
        if pos == (current.pos[0], current.pos[1] - 1, current.pos[2]):
            decision = self.costs.evaluate(
                MovementCandidate(kind=MoveKind.BREAK, pos=pos, block_type=cell.block_type, context=break_context)
            )
            if decision.passable:
                return MovementCandidate(kind=MoveKind.DOWNWARD, pos=pos, block_type=cell.block_type, context=break_context)
        gravity_hazard = self._gravity_break_hazard(current.overlay, pos, cell)
        if gravity_hazard is not None:
            return gravity_hazard
        return MovementCandidate(kind=MoveKind.BREAK, pos=pos, block_type=cell.block_type, context=break_context)

    def _openable_pass_candidate(
        self,
        overlay: VirtualBlockOverlay,
        current: Position,
        pos: Position,
        cell: GridCell,
    ) -> MovementCandidate | None:
        if not _is_pass_openable(cell.block_type):
            return None
        interaction_target = pos
        if _is_door_type(cell.block_type):
            current_half = overlay.cell_at(self.world, pos)
            above = overlay.cell_at(self.world, (pos[0], pos[1] + 1, pos[2]))
            below = overlay.cell_at(self.world, (pos[0], pos[1] - 1, pos[2]))
            if current_half is not None and not current_half.walkable and above is not None and _is_door_type(above.block_type):
                interaction_target = pos
            elif current_half is not None and not current_half.walkable and below is not None and _is_door_type(below.block_type):
                interaction_target = (pos[0], pos[1] - 1, pos[2])
        return MovementCandidate(
            kind=MoveKind.OPEN,
            pos=pos,
            block_type=cell.block_type,
            interaction_target=interaction_target,
        )

    def _has_support(self, overlay: VirtualBlockOverlay, pos: Position) -> bool:
        support_pos = (pos[0], pos[1] - 1, pos[2])
        support = overlay.cell_at(self.world, support_pos)
        return support is not None and not support.walkable and not support.liquid

    def _blocked_headroom(
        self,
        overlay: VirtualBlockOverlay,
        pos: Position,
        cell: GridCell,
    ) -> tuple[Position, str] | None:
        headroom_pos = (pos[0], pos[1] + 1, pos[2])
        headroom_cell = overlay.cell_at(self.world, headroom_pos)
        if headroom_cell is not None:
            if not headroom_cell.walkable or headroom_cell.liquid:
                return headroom_pos, headroom_cell.block_type
            return None
        if cell.headroom_block:
            return headroom_pos, cell.headroom_block
        return None

    def _diagonal_blocker(
        self,
        overlay: VirtualBlockOverlay,
        current: Position,
        pos: Position,
        break_context: BreakContext | str,
    ) -> MovementCandidate | None:
        dx = pos[0] - current[0]
        dz = pos[2] - current[2]
        if abs(dx) != 1 or abs(dz) != 1 or pos[1] != current[1]:
            return None
        for corner_pos in ((current[0] + dx, current[1], current[2]), (current[0], current[1], current[2] + dz)):
            corner = overlay.cell_at(self.world, corner_pos)
            if corner is None:
                return MovementCandidate(kind=MoveKind.INVALID, pos=corner_pos, context="diagonal_corner_unloaded")
            blocked_headroom = self._blocked_headroom(overlay, corner_pos, corner)
            if blocked_headroom is not None:
                headroom_pos, _block_type = blocked_headroom
                return MovementCandidate(kind=MoveKind.INVALID, pos=headroom_pos, context="diagonal_corner_headroom_blocked")
            if corner.walkable and not corner.liquid:
                continue
            gravity_hazard = self._gravity_break_hazard(overlay, corner_pos, corner)
            if gravity_hazard is not None:
                return gravity_hazard
            decision = self.costs.evaluate(
                MovementCandidate(kind=MoveKind.BREAK, pos=corner_pos, block_type=corner.block_type, context=break_context)
            )
            return MovementCandidate(kind=MoveKind.INVALID, pos=corner_pos, context=f"diagonal_corner_blocked:{decision.reason}")
        return None

    def _gravity_break_hazard(
        self,
        overlay: VirtualBlockOverlay,
        pos: Position,
        cell: GridCell,
    ) -> MovementCandidate | None:
        block_type = cell.block_type.removeprefix("minecraft:")
        if block_type not in FALLING_BLOCK_TYPES:
            return None
        for neighbor_pos in (
            (pos[0] + 1, pos[1], pos[2]),
            (pos[0] - 1, pos[1], pos[2]),
            (pos[0], pos[1], pos[2] + 1),
            (pos[0], pos[1], pos[2] - 1),
            (pos[0], pos[1] + 1, pos[2]),
            (pos[0], pos[1] - 1, pos[2]),
        ):
            neighbor = overlay.cell_at(self.world, neighbor_pos)
            if neighbor is not None and neighbor.liquid:
                return MovementCandidate(
                    kind=MoveKind.INVALID,
                    pos=pos,
                    block_type=cell.block_type,
                    context="break_denied:gravity_liquid_adjacent",
                )
        above_pos = (pos[0], pos[1] + 1, pos[2])
        above = overlay.cell_at(self.world, above_pos)
        below_pos = (pos[0], pos[1] - 1, pos[2])
        below = overlay.cell_at(self.world, below_pos)
        if above is not None:
            above_type = above.block_type.removeprefix("minecraft:")
            if above_type in FALLING_BLOCK_TYPES and not above.walkable and not above.liquid:
                return MovementCandidate(kind=MoveKind.INVALID, pos=pos, block_type=cell.block_type, context="break_denied:gravity_stack")
        if below is not None:
            below_type = below.block_type.removeprefix("minecraft:")
            if below_type in FALLING_BLOCK_TYPES and not below.walkable and not below.liquid:
                return MovementCandidate(kind=MoveKind.INVALID, pos=pos, block_type=cell.block_type, context="break_denied:gravity_stack")
        return None


    @staticmethod
    def _overlay_after_decision(
        overlay: VirtualBlockOverlay,
        decision: CostDecision,
        cell: GridCell,
    ) -> VirtualBlockOverlay:
        if decision.kind == MoveKind.BREAK:
            return overlay.set_cell(decision.pos, GridCell(block_type="air", walkable=True))
        if decision.kind == MoveKind.PLACE:
            block_type = str(decision.diagnostics.get("block_type") or cell.block_type or "cobblestone")
            return overlay.set_cell(decision.pos, GridCell(block_type=block_type, walkable=False))
        if decision.kind == MoveKind.OPEN:
            updated = overlay.set_cell(
                decision.pos,
                GridCell(
                    block_type=cell.block_type,
                    walkable=True,
                    liquid=False,
                    headroom_block=_open_headroom_block_type(cell.block_type),
                ),
            )
            if _is_door_type(cell.block_type):
                lower = decision.interaction_target or decision.pos
                upper = (lower[0], lower[1] + 1, lower[2])
                updated = updated.set_cell(
                    lower,
                    GridCell(block_type=cell.block_type, walkable=True, liquid=False, headroom_block=cell.block_type),
                )
                updated = updated.set_cell(
                    upper,
                    GridCell(block_type=cell.block_type, walkable=True, liquid=False),
                )
            return updated
        return overlay

    @staticmethod
    def _virtual_effect_for(decision: CostDecision) -> str | None:
        if decision.kind == MoveKind.BREAK:
            return "break_to_air"
        if decision.kind == MoveKind.PLACE:
            return "place_solid"
        if decision.kind == MoveKind.OPEN:
            return "open_passage"
        return None

    @staticmethod
    def _next_state_after_decision(
        current: _SearchState,
        next_pos: Position,
        overlay: VirtualBlockOverlay,
        decision: CostDecision,
    ) -> _SearchState:
        if decision.kind == MoveKind.PLACE:
            return _SearchState(current.pos, overlay)
        if decision.kind == MoveKind.BREAK and decision.pos != next_pos:
            return _SearchState(current.pos, overlay)
        if decision.kind == MoveKind.OPEN and decision.pos == next_pos:
            return _SearchState(current.pos, overlay)
        return _SearchState(next_pos, overlay)

    @staticmethod
    def _heuristic(a: Position, b: Position) -> float:
        return abs(a[0] - b[0]) + abs(a[1] - b[1]) + abs(a[2] - b[2])


def _is_door_type(block_type: str) -> bool:
    normalized = block_type.removeprefix("minecraft:")
    return normalized.endswith("_door")


def _is_pass_openable(block_type: str) -> bool:
    normalized = block_type.removeprefix("minecraft:")
    return (
        normalized.endswith("_door")
        or normalized.endswith("_trapdoor")
        or normalized.endswith("_fence_gate")
    ) and normalized not in {"iron_door", "iron_trapdoor"}


def _open_headroom_block_type(block_type: str) -> str | None:
    if _is_door_type(block_type):
        return block_type
    return None


class PathRechecker:
    """Forward cost recheck for the next planned moves."""

    def __init__(self, world: GridWorld, costs: NavigationCostModel):
        self.world = world
        self.costs = costs
        self._planner_probe = AStarPlanner(world, costs)

    def recheck(
        self,
        path: PathResult,
        *,
        lookahead: int = 5,
        break_context: BreakContext | str = BreakContext.TRAVEL,
    ) -> RecheckResult:
        if not path.path:
            return RecheckResult(ok=True, reason="empty_path", checked=0)

        checked = 0
        overlay = VirtualBlockOverlay()
        current_pos = self._path_start(path)
        for step in path.path[:lookahead]:
            checked += 1
            cell = overlay.cell_at(self.world, step.pos)
            if cell is None:
                return RecheckResult(
                    ok=False,
                    reason="unloaded",
                    checked=checked,
                    failing_step=step,
                    diagnostics={"pos": list(step.pos), "current_pos": list(current_pos)},
                )
            candidate = self._candidate_for_step(step, cell, break_context, overlay, current_pos)
            decision = self.costs.evaluate(candidate)
            if not decision.passable:
                return RecheckResult(
                    ok=False,
                    reason=decision.reason,
                    checked=checked,
                    failing_step=step,
                    diagnostics={
                        "pos": list(step.pos),
                        "current_pos": list(current_pos),
                        "legality": decision.diagnostics.get("legality"),
                    },
                )
            if step.move == MoveKind.BREAK and cell.block_type != step.block_type:
                return RecheckResult(
                    ok=False,
                    reason="block_changed",
                    checked=checked,
                    failing_step=step,
                    diagnostics={
                        "pos": list(step.pos),
                        "planned_block_type": step.block_type,
                        "observed_block_type": cell.block_type,
                    },
                )
            overlay = overlay.after_step(step)
            if step.move not in {MoveKind.BREAK, MoveKind.PLACE, MoveKind.OPEN}:
                current_pos = step.pos

        diagnostics: dict[str, object] = {}
        if overlay.blocks:
            diagnostics["virtual_overlay"] = overlay.payload()
        return RecheckResult(ok=True, reason="valid", checked=checked, diagnostics=diagnostics)

    def _candidate_for_step(
        self,
        step: PathStep,
        cell: GridCell,
        break_context: BreakContext | str,
        overlay: VirtualBlockOverlay,
        current_pos: Position,
    ) -> MovementCandidate:
        if step.move == MoveKind.DIAGONAL:
            blocker = self._planner_probe._diagonal_blocker(overlay, current_pos, step.pos, break_context)
            if blocker is not None:
                return blocker
        if step.move in {MoveKind.WALK, MoveKind.DIAGONAL, MoveKind.ASCEND, MoveKind.PILLAR, MoveKind.DESCEND, MoveKind.DOWNWARD, MoveKind.FALL} and cell.requires_support:
            support_pos = (step.pos[0], step.pos[1] - 1, step.pos[2])
            support = overlay.cell_at(self.world, support_pos)
            if support is None or support.walkable or support.liquid:
                return MovementCandidate(
                    kind=MoveKind.INVALID,
                    pos=support_pos,
                    block_type="cobblestone",
                    context="support_missing",
                )
        if step.move in {MoveKind.WALK, MoveKind.DIAGONAL, MoveKind.ASCEND, MoveKind.PILLAR, MoveKind.DESCEND, MoveKind.DOWNWARD, MoveKind.FALL}:
            headroom_pos = (step.pos[0], step.pos[1] + 1, step.pos[2])
            headroom_cell = overlay.cell_at(self.world, headroom_pos)
            if headroom_cell is not None and (not headroom_cell.walkable or headroom_cell.liquid):
                return MovementCandidate(
                    kind=MoveKind.INVALID,
                    pos=headroom_pos,
                    block_type=headroom_cell.block_type,
                    context="headroom_blocked",
                )
            if headroom_cell is None and cell.headroom_block:
                return MovementCandidate(
                    kind=MoveKind.INVALID,
                    pos=headroom_pos,
                    block_type=cell.headroom_block,
                    context="headroom_blocked",
                )
        if step.move == MoveKind.WALK:
            return MovementCandidate(kind=MoveKind.WALK, pos=step.pos, block_type=cell.block_type)
        if step.move == MoveKind.DIAGONAL:
            return MovementCandidate(kind=MoveKind.DIAGONAL, pos=step.pos, block_type=cell.block_type)
        if step.move == MoveKind.ASCEND:
            return MovementCandidate(kind=MoveKind.ASCEND, pos=step.pos, block_type=cell.block_type)
        if step.move == MoveKind.PILLAR:
            return MovementCandidate(kind=MoveKind.PILLAR, pos=step.pos, block_type=cell.block_type)
        if step.move == MoveKind.DESCEND:
            return MovementCandidate(kind=MoveKind.DESCEND, pos=step.pos, block_type=cell.block_type)
        if step.move == MoveKind.DOWNWARD:
            return MovementCandidate(kind=MoveKind.DOWNWARD, pos=step.pos, block_type=cell.block_type)
        if step.move == MoveKind.SWIM:
            return MovementCandidate(kind=MoveKind.SWIM, pos=step.pos, block_type=cell.block_type)
        if step.move == MoveKind.FALL:
            return MovementCandidate(
                kind=MoveKind.FALL,
                pos=step.pos,
                block_type=cell.block_type,
                fall_depth=cell.fall_depth,
            )
        if step.move == MoveKind.BREAK:
            gravity_hazard = self._gravity_break_hazard(overlay, step.pos, cell)
            if gravity_hazard is not None:
                return gravity_hazard
            return MovementCandidate(kind=MoveKind.BREAK, pos=step.pos, block_type=cell.block_type, context=break_context)
        if step.move == MoveKind.PLACE:
            return MovementCandidate(kind=MoveKind.PLACE, pos=step.pos, block_type=cell.block_type, context=PlaceContext.TRAVEL)
        if step.move == MoveKind.OPEN:
            return MovementCandidate(
                kind=MoveKind.OPEN,
                pos=step.pos,
                block_type=cell.block_type,
                interaction_target=step.interaction_target or step.pos,
            )
        raise ValueError(f"unknown path step move: {step.move}")

    @staticmethod
    def _path_start(path: PathResult) -> Position:
        raw = path.diagnostics.get("path_start")
        if isinstance(raw, (list, tuple)) and len(raw) == 3:
            return (int(raw[0]), int(raw[1]), int(raw[2]))
        first = path.path[0]
        if first.move == MoveKind.DIAGONAL:
            raise ValueError("diagonal recheck requires path_start diagnostics")
        return first.pos

    def _gravity_break_hazard(
        self,
        overlay: VirtualBlockOverlay,
        pos: Position,
        cell: GridCell,
    ) -> MovementCandidate | None:
        block_type = cell.block_type.removeprefix("minecraft:")
        if block_type not in FALLING_BLOCK_TYPES:
            return None
        for neighbor_pos in (
            (pos[0] + 1, pos[1], pos[2]),
            (pos[0] - 1, pos[1], pos[2]),
            (pos[0], pos[1], pos[2] + 1),
            (pos[0], pos[1], pos[2] - 1),
            (pos[0], pos[1] + 1, pos[2]),
            (pos[0], pos[1] - 1, pos[2]),
        ):
            neighbor = overlay.cell_at(self.world, neighbor_pos)
            if neighbor is not None and neighbor.liquid:
                return MovementCandidate(
                    kind=MoveKind.INVALID,
                    pos=pos,
                    block_type=cell.block_type,
                    context="break_denied:gravity_liquid_adjacent",
                )
        above_pos = (pos[0], pos[1] + 1, pos[2])
        above = overlay.cell_at(self.world, above_pos)
        below_pos = (pos[0], pos[1] - 1, pos[2])
        below = overlay.cell_at(self.world, below_pos)
        if above is not None:
            above_type = above.block_type.removeprefix("minecraft:")
            if above_type in FALLING_BLOCK_TYPES and not above.walkable and not above.liquid:
                return MovementCandidate(kind=MoveKind.INVALID, pos=pos, block_type=cell.block_type, context="break_denied:gravity_stack")
        if below is not None:
            below_type = below.block_type.removeprefix("minecraft:")
            if below_type in FALLING_BLOCK_TYPES and not below.walkable and not below.liquid:
                return MovementCandidate(kind=MoveKind.INVALID, pos=pos, block_type=cell.block_type, context="break_denied:gravity_stack")
        return None


class SegmentedNavigator:
    """Plans one safe local segment toward a goal."""

    def __init__(self, world: GridWorld, costs: NavigationCostModel):
        self.world = world
        self.costs = costs

    def next_segment(
        self,
        start: Position,
        goal: Position,
        *,
        break_context: BreakContext | str = BreakContext.TRAVEL,
        max_expansions: int = 4096,
        min_partial_progress: int = AStarPlanner.DEFAULT_MIN_PARTIAL_PROGRESS,
        recheck_lookahead: int = 5,
        recheck_world: GridWorld | None = None,
        recheck_costs: NavigationCostModel | None = None,
        previous_segment: tuple[Position, ...] = (),
        backtrack_cost_factor: float = 0.5,
        unloaded_boundary_limit: int | None = None,
        partial_tail_trim: int = 1,
    ) -> NavigationSegment:
        plan = AStarPlanner(self.world, self.costs).plan(
            start,
            goal,
            break_context=break_context,
            max_expansions=max_expansions,
            allow_partial=True,
            min_partial_progress=min_partial_progress,
            previous_segment=previous_segment,
            backtrack_cost_factor=backtrack_cost_factor,
            unloaded_boundary_limit=unloaded_boundary_limit,
        )
        plan = _trim_unloaded_partial_tail(plan, partial_tail_trim)

        if not plan.path:
            return NavigationSegment(
                status="blocked",
                target=None,
                plan=plan,
                diagnostics={"reason": plan.reason},
            )

        recheck = PathRechecker(recheck_world or self.world, recheck_costs or self.costs).recheck(
            plan,
            lookahead=recheck_lookahead,
            break_context=break_context,
        )
        if not recheck.ok:
            return NavigationSegment(
                status="replan_required",
                target=None,
                plan=plan,
                recheck=recheck,
                diagnostics={"reason": recheck.reason},
            )

        target = plan.path[-1].pos
        status = "arrived" if plan.success else "advanced"
        return NavigationSegment(
            status=status,
            target=target,
            plan=plan,
            recheck=recheck,
            diagnostics={
                "path_steps": len(plan.path),
                "plan_reason": plan.reason,
                "plan_success": plan.success,
            },
        )


def _trim_unloaded_partial_tail(plan: PathResult, tail_trim: int) -> PathResult:
    if tail_trim < 0:
        raise ValueError("partial_tail_trim must be >= 0")
    if plan.success or plan.reason != "partial" or tail_trim == 0:
        return plan
    if plan.diagnostics.get("stop_reason") != "unloaded_boundary":
        return plan
    if len(plan.path) <= 1:
        return plan

    trim_count = min(tail_trim, len(plan.path) - 1)
    trimmed_path = plan.path[:-trim_count]
    target = trimmed_path[-1].pos
    diagnostics = dict(plan.diagnostics)
    diagnostics["original_partial_target"] = diagnostics.get("partial_target")
    diagnostics["partial_target"] = list(target)
    diagnostics["tail_trimmed_steps"] = trim_count
    diagnostics["tail_trim_reason"] = "unloaded_boundary"
    return replace(
        plan,
        path=trimmed_path,
        cost=sum(step.cost for step in trimmed_path),
        diagnostics=diagnostics,
    )
