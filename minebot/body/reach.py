"""Provider-local reach-domain primitives for block objectives.

This module owns the geometry shared by FakePlayer block interactions.  It does
not navigate, mutate the world, or make governance decisions; callers still
own those responsibilities and may attach their own movement/mutation
profiles to the intent for diagnostics.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from math import ceil, dist, floor
from typing import TypeVar

from minebot.body.world_read import read_block_facts
from minebot.contract import Body, PerceptionResult, Position, ToolResult


CARDINAL_OFFSETS: tuple[Position, ...] = (
    (1, 0, 0),
    (-1, 0, 0),
    (0, 0, 1),
    (0, 0, -1),
)


_TargetT = TypeVar("_TargetT")


@dataclass(frozen=True)
class ReachIntent:
    """A bounded physical objective inside the FakePlayer provider.

    ``vertical_offsets`` are feet-cell bands relative to the target block.  A
    caller that wants a broader search can leave them unset; the range-derived
    bands are then filtered by the same interaction geometry.  The profile
    fields are deliberately descriptive: navigation and mutation remain owned
    by their existing transactions.
    """

    target: Position
    interaction_radius: float = 4.5
    vertical_offsets: tuple[int, ...] | None = None
    include_target: bool = False
    require_support: bool = True
    allow_liquid: bool = False
    # Some callers only need a movement stand domain and do not have a target
    # ray in their bounded perception.  Mutating block consumers opt in so
    # that an occluded target is rejected before navigation/mine execution.
    require_line_of_sight: bool = False
    max_candidates: int | None = None
    movement_profile: str = "pure_movement"
    mutation_profile: str = "none"
    # ``governed`` keeps a candidate whose feet/head body cell is currently
    # blocked by an observed SOLID voxel.  The returned candidate is a request
    # for the navigation layer to clear that cell through its normal mutation
    # proposal and exact governance recheck; it is not an authorization to
    # mutate.  LOS remains a hard interaction precondition: a route must not
    # arrive at a stand that still cannot physically address its target.
    clearance_profile: str = "none"
    terminal_predicate: str = "within_interaction_range"


@dataclass(frozen=True)
class ReachDomain:
    """Authoritative candidate domain plus rejection evidence."""

    candidates: tuple[Position, ...]
    geometric_candidates: tuple[Position, ...]
    rejected: tuple[dict[str, object], ...]
    clearance: tuple[dict[str, object], ...]
    diagnostics: dict[str, object]


def block_reach_domain(body: Body, intent: ReachIntent) -> ReachDomain | ToolResult:
    """Resolve a block target into one bounded, evidence-backed stand domain."""

    domains = block_reach_domains(body, (intent,))
    if isinstance(domains, ToolResult):
        return domains
    return domains[0]


def block_reach_domains(
    body: Body,
    intents: tuple[ReachIntent, ...],
) -> tuple[ReachDomain, ...] | ToolResult:
    """Resolve several block intents from one authoritative cell batch."""

    if not intents:
        return ()
    for intent in intents:
        if intent.interaction_radius <= 0:
            return ToolResult(False, "invalid_interaction_radius", False)
        if intent.max_candidates is not None and intent.max_candidates < 1:
            return ToolResult(False, "invalid_reach_candidate_budget", False)

    geometries = tuple(_geometric_candidates(intent) for intent in intents)
    wanted: list[Position] = []
    for intent, geometric in zip(intents, geometries):
        for candidate in geometric:
            wanted.extend(
                (
                    candidate,
                    (candidate[0], candidate[1] + 1, candidate[2]),
                    (candidate[0], candidate[1] - 1, candidate[2]),
                )
            )
            if intent.require_line_of_sight:
                wanted.extend(_line_of_sight_cells(candidate, intent.target))
    try:
        facts = read_block_facts(
            body,
            tuple(dict.fromkeys(wanted)),
            failure_label="reach:domain_batch",
        )
    except ValueError as exc:
        return ToolResult(
            False,
            "perception_failed",
            True,
            next_suggestion="refresh authoritative block facts before selecting a reach candidate",
            metrics={
                "scope": "blockCells",
                "failure_label": "reach:domain_batch",
                "error": str(exc),
                "targets": [list(intent.target) for intent in intents],
            },
        )

    state = body.get_state()
    return tuple(
        _resolve_domain(intent, geometric, facts, state.pos)
        for intent, geometric in zip(intents, geometries)
    )


def block_reach_points(body: Body, intent: ReachIntent) -> list[Position] | ToolResult:
    """Compatibility adapter for consumers that only need ordered stand cells."""

    domain = block_reach_domain(body, intent)
    if isinstance(domain, ToolResult):
        return domain
    return list(domain.candidates)


def block_reach_geometry(intent: ReachIntent) -> tuple[Position, ...]:
    """Return the bounded geometric domain before world-fact filtering."""

    if intent.interaction_radius <= 0:
        return ()
    return _geometric_candidates(intent)


def round_robin_reach_goals(
    targets: tuple[_TargetT, ...],
    candidates_by_target: dict[Position, tuple[Position, ...] | list[Position]],
    *,
    max_goals: int,
    target_position: Callable[[_TargetT], Position],
) -> tuple[tuple[Position, ...], dict[Position, tuple[_TargetT, ...]]]:
    """Merge per-target stand candidates into one bounded goal set.

    Consumers may rank or expand their own candidate domains, but the server
    goal set has one provider-local assembly rule: take one candidate from each
    target in round-robin order, then move to the next depth.  This keeps a
    large target domain from starving the other targets while preserving the
    target-to-goal mapping needed for terminal truth.
    """

    if max_goals < 1:
        raise ValueError("max_goals must be >= 1")
    goals: list[Position] = []
    targets_by_goal: dict[Position, list[_TargetT]] = {}
    depth = 0
    pending = True
    while pending and len(goals) < max_goals:
        pending = False
        for target in targets:
            candidates = candidates_by_target.get(target_position(target), ())
            if depth >= len(candidates):
                continue
            pending = True
            stand = candidates[depth]
            if stand not in goals:
                goals.append(stand)
            linked = targets_by_goal.setdefault(stand, [])
            if target not in linked:
                linked.append(target)
            if len(goals) >= max_goals:
                break
        depth += 1
    return tuple(goals), {goal: tuple(linked) for goal, linked in targets_by_goal.items()}


def _resolve_domain(
    intent: ReachIntent,
    geometric: tuple[Position, ...],
    facts: dict[Position, PerceptionResult],
    state_pos: tuple[float, float, float],
) -> ReachDomain:
    candidates: list[tuple[float, float, Position]] = []
    rejected: list[dict[str, object]] = []
    clearance: list[dict[str, object]] = []
    for candidate in geometric:
        feet = facts.get(candidate)
        head_pos = (candidate[0], candidate[1] + 1, candidate[2])
        support_pos = (candidate[0], candidate[1] - 1, candidate[2])
        deferred_clearance: list[dict[str, object]] = []
        rejection = _candidate_rejection(
            intent,
            candidate=candidate,
            feet=feet,
            head=facts.get(head_pos),
            support=facts.get(support_pos),
            head_pos=head_pos,
        )
        if rejection is not None:
            if _can_defer_clearance(intent, rejection):
                deferred_clearance.append(rejection)
            else:
                rejected.append(rejection)
                continue
        rejection = _line_of_sight_rejection(intent, candidate, facts)
        if rejection is not None:
            if _can_defer_clearance(intent, rejection):
                deferred_clearance.append(rejection)
            else:
                rejected.append(rejection)
                continue
        candidates.append(
            (
                dist(state_pos, (candidate[0] + 0.5, candidate[1], candidate[2] + 0.5)),
                abs(float(candidate[1]) - state_pos[1]),
                candidate,
            )
        )
        if deferred_clearance:
            clearance.append(
                {
                    "candidate": list(candidate),
                    "profile": intent.clearance_profile,
                    "requirements": deferred_clearance,
                }
            )

    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    ordered = tuple(item[2] for item in candidates)
    if intent.max_candidates is not None:
        ordered = ordered[: intent.max_candidates]
    return ReachDomain(
        candidates=ordered,
        geometric_candidates=geometric,
        rejected=tuple(rejected),
        diagnostics={
            "target": list(intent.target),
            "interaction_radius": intent.interaction_radius,
            "vertical_offsets": list(_vertical_offsets(intent)),
            "candidate_count": len(ordered),
            "geometric_candidate_count": len(geometric),
            "rejected_count": len(rejected),
            "movement_profile": intent.movement_profile,
            "mutation_profile": intent.mutation_profile,
            "clearance_profile": intent.clearance_profile,
            "terminal_predicate": intent.terminal_predicate,
            "line_of_sight_required": intent.require_line_of_sight,
            "clearance_candidate_count": len(clearance),
        },
        clearance=tuple(clearance),
    )


def cell_is_clear(perception: PerceptionResult | None) -> bool:
    return perception is not None and str(perception.data.get("state") or "UNKNOWN") == "CLEAR"


def cell_is_liquid(perception: PerceptionResult | None) -> bool:
    return perception is not None and str(perception.data.get("state") or "UNKNOWN") == "LIQUID"


def cell_is_solid_support(perception: PerceptionResult | None) -> bool:
    if perception is None or str(perception.data.get("state") or "UNKNOWN") != "SOLID":
        return False
    block_type = str(perception.data.get("type") or "unknown").removeprefix("minecraft:")
    properties = {
        str(key): str(value).lower()
        for key, value in dict(perception.data.get("properties") or {}).items()
    }
    if block_type.endswith("_slab"):
        return properties.get("type") == "bottom"
    if block_type.endswith("_stairs"):
        return properties.get("half", "bottom") == "bottom"
    return True


def _geometric_candidates(intent: ReachIntent) -> tuple[Position, ...]:
    tx, ty, tz = intent.target
    offsets = _vertical_offsets(intent)
    horizontal = list(CARDINAL_OFFSETS)
    if intent.include_target:
        horizontal.append((0, 0, 0))
    target_center = (tx + 0.5, ty + 0.5, tz + 0.5)
    candidates: list[Position] = []
    for vertical in offsets:
        for dx, _dy, dz in horizontal:
            candidate = (tx + dx, ty + vertical, tz + dz)
            if candidate in candidates:
                continue
            candidate_center = (candidate[0] + 0.5, candidate[1], candidate[2] + 0.5)
            if dist(candidate_center, target_center) > intent.interaction_radius + 1e-6:
                continue
            candidates.append(candidate)
    return tuple(candidates)


def _vertical_offsets(intent: ReachIntent) -> tuple[int, ...]:
    if intent.vertical_offsets is not None:
        return tuple(dict.fromkeys(int(offset) for offset in intent.vertical_offsets))
    radius = max(1, int(floor(intent.interaction_radius)) + 1)
    return tuple(range(-radius, radius + 1))


def _line_of_sight_cells(candidate: Position, target: Position) -> tuple[Position, ...]:
    """Return the bounded voxel ray checked before interacting with a target.

    The source and target cells are excluded, matching the Scarpet executor's
    LOS predicate: the stand cell is already validated separately and the
    target block is allowed to be solid.  Intermediate solid cells are
    evidence that the target is currently occluded, not a reason to retry the
    same candidate with an unverified geometric stand point.
    """

    source = (candidate[0] + 0.5, candidate[1] + 1.0, candidate[2] + 0.5)
    destination = (target[0] + 0.5, target[1] + 0.5, target[2] + 0.5)
    delta = tuple(destination[index] - source[index] for index in range(3))
    steps = max(abs(value) for value in delta)
    if steps < 1.0:
        return ()
    sample_count = max(1, int(ceil(steps)))
    source_cell = (floor(source[0]), floor(source[1]), floor(source[2]))
    target_cell = target
    cells: list[Position] = []
    for index in range(1, sample_count + 1):
        fraction = index / sample_count
        cell = (
            floor(source[0] + delta[0] * fraction),
            floor(source[1] + delta[1] * fraction),
            floor(source[2] + delta[2] * fraction),
        )
        if cell in {source_cell, target_cell} or cell in cells:
            continue
        cells.append(cell)
    return tuple(cells)


def _line_of_sight_rejection(
    intent: ReachIntent,
    candidate: Position,
    facts: dict[Position, PerceptionResult],
) -> dict[str, object] | None:
    if not intent.require_line_of_sight:
        return None
    for cell in _line_of_sight_cells(candidate, intent.target):
        perception = facts.get(cell)
        if perception is None:
            return {
                "candidate": list(candidate),
                "reason": "line_of_sight_unknown",
                "cell": list(cell),
            }
        state = str(perception.data.get("state") or "UNKNOWN").upper()
        if state == "UNKNOWN":
            return {
                "candidate": list(candidate),
                "reason": "line_of_sight_unknown",
                "cell": list(cell),
            }
        if state == "SOLID":
            return {
                "candidate": list(candidate),
                "reason": "target_occluded",
                "cell": list(cell),
                "block_type": str(perception.data.get("type") or "unknown"),
                "state": state,
            }
    return None


def _candidate_rejection(
    intent: ReachIntent,
    *,
    candidate: Position,
    feet: PerceptionResult | None,
    head: PerceptionResult | None,
    support: PerceptionResult | None,
    head_pos: Position,
) -> dict[str, object] | None:
    if feet is None or head is None or support is None:
        return {"candidate": list(candidate), "reason": "missing_fact"}
    if not intent.allow_liquid and (cell_is_liquid(feet) or cell_is_liquid(head) or cell_is_liquid(support)):
        return {"candidate": list(candidate), "reason": "liquid_contact"}
    if not (cell_is_clear(feet) or (intent.allow_liquid and cell_is_liquid(feet))):
        return {
            "candidate": list(candidate),
            "reason": "feet_blocked",
            "state": str(feet.data.get("state") or "UNKNOWN").upper(),
            "block_type": str(feet.data.get("type") or "unknown"),
        }
    if not (
        cell_is_clear(head)
        or (intent.allow_liquid and cell_is_liquid(head))
        or head_pos == intent.target
    ):
        return {
            "candidate": list(candidate),
            "reason": "head_blocked",
            "cell": list(head_pos),
            "state": str(head.data.get("state") or "UNKNOWN").upper(),
            "block_type": str(head.data.get("type") or "unknown"),
        }
    if intent.require_support and not cell_is_solid_support(support):
        return {"candidate": list(candidate), "reason": "support_unstable"}
    return None


def _can_defer_clearance(intent: ReachIntent, rejection: dict[str, object]) -> bool:
    """Return whether a solid obstruction may be handed to governed navigation."""

    if intent.clearance_profile != "governed":
        return False
    if rejection.get("reason") not in {"feet_blocked", "head_blocked"}:
        return False
    # A missing/unknown fact is never a clearance candidate.  Only an
    # authoritative SOLID voxel can be proposed to the mutation arbiter.
    return str(rejection.get("state") or "UNKNOWN").upper() == "SOLID"
