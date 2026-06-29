"""Shared helper logic for block-anchored interaction transactions."""

from __future__ import annotations

from dataclasses import dataclass
from math import fabs
from math import floor
from math import dist
from typing import Protocol

from minebot.contract import Action, Body, PerceptionResult, Position, ToolResult
from minebot.contract import terminal_event_to_tool_result


INTERACTION_RANGE = 4.5
STAND_OFFSETS = ((1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1))
ENTITY_HANDOFF_OFFSET = 0.4
class InteractionNavigator(Protocol):
    def navigate_to(self, goal: Position, **kwargs) -> ToolResult: ...


class DirectInteractionNavigator:
    """Bounded local fallback for already-vetted adjacent interaction stands."""

    def __init__(
        self,
        body: Body,
        *,
        arrival_radius: float = 0.85,
        timeout_ticks: int = 120,
        no_progress_ticks: int = 40,
        max_deviation: float = 3.0,
    ):
        self.body = body
        self.arrival_radius = arrival_radius
        self.timeout_ticks = timeout_ticks
        self.no_progress_ticks = no_progress_ticks
        self.max_deviation = max_deviation

    def navigate_to(self, goal: Position, **kwargs) -> ToolResult:
        timeout_s = float(kwargs.get("timeout_s") or 8.0)
        action = Action.create(
            "moveTo",
            {
                "target": list(goal),
                "waypoints": [list(goal)],
                "arrival_radius": self.arrival_radius,
                "timeout_ticks": self.timeout_ticks,
                "no_progress_ticks": self.no_progress_ticks,
                "max_deviation": self.max_deviation,
            },
        )
        accepted = self.body.execute(action)
        if not (accepted.ok and accepted.accepted):
            return ToolResult(
                success=False,
                reason="body_rejected",
                can_retry=True,
                metrics={
                    "action_id": action.id,
                    "goal": list(goal),
                    "accepted": {
                        "ok": accepted.ok,
                        "accepted": accepted.accepted,
                        "error": accepted.error,
                        "data": accepted.data,
                    },
                },
            )
        terminal = self.body.await_action_terminal(action.id, timeout_s=timeout_s)
        result = terminal_event_to_tool_result(terminal)
        if result.success:
            return ToolResult(
                success=True,
                reason=result.reason,
                can_retry=False,
                metrics={"action_id": action.id, "goal": list(goal), **dict(result.metrics or {})},
            )
        return ToolResult(
            success=False,
            reason=result.reason,
            can_retry=result.can_retry,
            next_suggestion=result.next_suggestion,
            metrics={"action_id": action.id, "goal": list(goal), **dict(result.metrics or {})},
        )


@dataclass(frozen=True)
class NearbyBlockTarget:
    pos: Position
    block_type: str
    distance: float


@dataclass(frozen=True)
class NearbyEntityTarget:
    entity_id: str | None
    name: str | None
    entity_type: str
    pos: tuple[float, float, float]
    health: float | None
    distance: float


def find_nearby_block_targets(
    body: Body,
    block_types: tuple[str, ...],
    radius: int,
    *,
    not_found_reason: str,
    limit: int = 64,
) -> list[NearbyBlockTarget] | ToolResult:
    state = body.get_state()
    wanted = sorted({normalize_block_type(block_type) for block_type in block_types})
    candidates: list[NearbyBlockTarget] = []
    seen: set[Position] = set()
    for wanted_type in wanted:
        found = body.perceive(
            "findBlocks",
            {
                "type": wanted_type,
                "radius": radius,
                "limit": limit,
            },
        )
        if not found.ok:
            failed = perception_failure(found)
            if failed is not None:
                return failed
        blocks = found.data.get("blocks") or []
        if not found.complete and not blocks:
            failed = perception_failure(found)
            if failed is not None:
                return failed
        for item in blocks:
            block_type = normalize_block_type(str(item.get("type") or ""))
            if not block_type_matches_wanted(block_type, set(wanted)):
                continue
            pos = (int(item["x"]), int(item["y"]), int(item["z"]))
            if pos in seen:
                continue
            seen.add(pos)
            candidates.append(
                NearbyBlockTarget(
                    pos=pos,
                    block_type=block_type,
                    distance=dist(state.pos, (pos[0] + 0.5, pos[1] + 0.5, pos[2] + 0.5)),
                )
            )

    if not candidates:
        return ToolResult(
            success=False,
            reason=not_found_reason,
            can_retry=True,
            next_suggestion="move closer or expand the search radius before retrying",
            metrics={"search_radius": radius, "block_types": list(block_types), "limit": limit},
        )
    return sorted(candidates, key=lambda candidate: (candidate.distance, candidate.pos))


def find_blocks_metadata(
    body: Body,
    *,
    block_types: tuple[str, ...],
    radius: int,
    limit: int,
) -> dict[str, object]:
    wanted = sorted({normalize_block_type(block_type) for block_type in block_types})
    truncated = False
    uncertainty: list[object] = []
    errors: list[str] = []
    for wanted_type in wanted:
        found = body.perceive(
            "findBlocks",
            {
                "type": wanted_type,
                "radius": radius,
                "limit": limit,
            },
        )
        if not found.ok:
            errors.append(str(found.error or "perception_failed"))
            continue
        if not found.complete:
            truncated = True
            uncertainty.extend(list(found.uncertainty or ()))
    return {
        "truncated": truncated,
        "uncertainty": uncertainty,
        "errors": errors,
    }


def find_block_target(
    body: Body,
    *,
    block_types: tuple[str, ...],
    radius: int,
    limit: int = 32,
    not_found_reason: str,
) -> NearbyBlockTarget | ToolResult:
    if not block_types:
        return ToolResult(
            success=False,
            reason="search_block_filter_missing",
            can_retry=False,
            metrics={"search_radius": radius},
        )

    targets = find_nearby_block_targets(body, block_types, radius, not_found_reason=not_found_reason, limit=limit)
    if isinstance(targets, ToolResult):
        return targets
    return targets[0]


def find_named_entity_target(
    body: Body,
    entity_name: str,
    *,
    radius: int,
    not_found_reason: str,
    wanted_types: tuple[str, ...] = ("player",),
) -> NearbyEntityTarget | ToolResult:
    nearby = body.perceive("nearbyEntities", {"radius": radius, "limit": 64})
    failed = perception_failure(nearby)
    if failed is not None:
        return failed

    state = body.get_state()
    wanted = {normalize_entity_type(entity_type) for entity_type in wanted_types}
    candidates: list[NearbyEntityTarget] = []
    for item in nearby.data.get("entities") or []:
        found_name = item.get("name")
        if found_name != entity_name:
            continue
        entity_type = normalize_entity_type(str(item.get("type") or ""))
        if wanted and entity_type not in wanted:
            continue
        pos_raw = item.get("pos") or [0, 0, 0]
        pos = (float(pos_raw[0]), float(pos_raw[1]), float(pos_raw[2]))
        candidates.append(
            NearbyEntityTarget(
                entity_id=str(item["id"]) if item.get("id") is not None else None,
                name=str(found_name) if found_name is not None else None,
                entity_type=entity_type,
                pos=pos,
                health=float(item["health"]) if item.get("health") is not None else None,
                distance=dist(state.pos, pos),
            )
        )

    if not candidates:
        return ToolResult(
            success=False,
            reason=not_found_reason,
            can_retry=True,
            next_suggestion="move closer to the receiver or expand the entity search radius before retrying",
            metrics={"search_radius": radius, "entity_name": entity_name, "entity_types": list(wanted_types)},
        )
    return sorted(candidates, key=lambda candidate: (candidate.distance, candidate.pos))[0]


def find_entity_target(
    body: Body,
    *,
    radius: int,
    not_found_reason: str,
    wanted_types: tuple[str, ...] = (),
    entity_name: str | None = None,
) -> NearbyEntityTarget | ToolResult:
    nearby = body.perceive("nearbyEntities", {"radius": radius, "limit": 64})
    failed = perception_failure(nearby)
    if failed is not None:
        return failed

    state = body.get_state()
    wanted = {normalize_entity_type(entity_type) for entity_type in wanted_types}
    candidates: list[NearbyEntityTarget] = []
    for item in nearby.data.get("entities") or []:
        found_name = item.get("name")
        if entity_name is not None and found_name != entity_name:
            continue
        entity_type = normalize_entity_type(str(item.get("type") or ""))
        if wanted and entity_type not in wanted:
            continue
        pos_raw = item.get("pos") or [0, 0, 0]
        pos = (float(pos_raw[0]), float(pos_raw[1]), float(pos_raw[2]))
        candidates.append(
            NearbyEntityTarget(
                entity_id=str(item["id"]) if item.get("id") is not None else None,
                name=str(found_name) if found_name is not None else None,
                entity_type=entity_type,
                pos=pos,
                health=float(item["health"]) if item.get("health") is not None else None,
                distance=dist(state.pos, pos),
            )
        )

    if not candidates:
        metrics = {"search_radius": radius, "entity_types": list(wanted_types)}
        if entity_name is not None:
            metrics["entity_name"] = entity_name
        return ToolResult(
            success=False,
            reason=not_found_reason,
            can_retry=True,
            next_suggestion="move closer to the target area or expand the entity search radius before retrying",
            metrics=metrics,
        )
    return sorted(candidates, key=lambda candidate: (candidate.distance, candidate.pos))[0]


def refresh_entity_target(
    body: Body,
    original: NearbyEntityTarget,
    *,
    radius: int,
    not_found_reason: str,
    wanted_types: tuple[str, ...] = (),
    entity_name: str | None = None,
) -> NearbyEntityTarget | ToolResult:
    refreshed = find_entity_target(
        body,
        radius=radius,
        not_found_reason=not_found_reason,
        wanted_types=wanted_types,
        entity_name=entity_name,
    )
    if isinstance(refreshed, ToolResult):
        return refreshed
    if original.entity_id is not None and refreshed.entity_id != original.entity_id:
        return ToolResult(
            success=False,
            reason=not_found_reason,
            can_retry=True,
            next_suggestion="reacquire the entity target instead of continuing against a different matching entity",
            metrics={
                "search_radius": radius,
                "entity_types": list(wanted_types),
                "entity_name": entity_name,
                "original_entity_id": original.entity_id,
                "refreshed_entity_id": refreshed.entity_id,
                "refreshed_target": _entity_metrics(refreshed),
            },
        )
    return refreshed


def ensure_interaction_range(
    body: Body,
    navigator: InteractionNavigator | None,
    target: Position,
    *,
    interaction_radius: float = INTERACTION_RANGE,
    timeout_s: float,
    missing_reason: str,
    failure_prefix: str,
    no_stand_reason: str,
    navigation_arrival_radius: float | None = None,
    center_after_navigation: bool = True,
    stand_points: list[Position] | None = None,
) -> dict[str, object] | ToolResult:
    state = body.get_state()
    initial_distance = dist(state.pos, (target[0] + 0.5, target[1] + 0.5, target[2] + 0.5))
    if initial_distance <= interaction_radius and navigation_arrival_radius is None:
        return {"navigated": False, "initial_distance": initial_distance, "final_distance": initial_distance}

    if navigator is None:
        if initial_distance <= interaction_radius:
            return {"navigated": False, "initial_distance": initial_distance, "final_distance": initial_distance}
        return ToolResult(
            success=False,
            reason=missing_reason,
            can_retry=True,
            next_suggestion="provide a navigation transaction before attempting distant block interaction",
            metrics={"target": list(target), "initial_distance": initial_distance},
        )

    stand_candidates = stand_points
    if stand_candidates is None:
        stand_candidates = interaction_stand_points(body, target)
    if isinstance(stand_candidates, ToolResult):
        return stand_candidates
    if not stand_candidates:
        return ToolResult(
            success=False,
            reason=no_stand_reason,
            can_retry=False,
            next_suggestion="clear a standable adjacent block before retrying the interaction",
            metrics={"target": list(target), "initial_distance": initial_distance},
        )

    current_feet = (floor(state.pos[0]), floor(state.pos[1]), floor(state.pos[2]))
    if initial_distance <= interaction_radius and current_feet in stand_candidates:
        return {
            "navigated": False,
            "stand_target": list(current_feet),
            "initial_distance": initial_distance,
            "final_distance": initial_distance,
            "already_on_stand": True,
        }

    attempts: list[dict[str, object]] = []
    last_failure: ToolResult | None = None
    for stand in stand_candidates:
        nav_kwargs: dict[str, object] = {"timeout_s": timeout_s}
        if navigation_arrival_radius is not None:
            nav_kwargs["arrival_radius"] = navigation_arrival_radius
        nav_result = navigator.navigate_to(stand, **nav_kwargs)
        attempt: dict[str, object] = {"goal": list(stand), "result": nav_result.to_payload()}
        if not nav_result.success:
            attempts.append(attempt)
            last_failure = nav_result
            continue
        if navigation_arrival_radius is not None and center_after_navigation:
            center_result = _move_to_stand_center(
                body,
                stand,
                arrival_radius=navigation_arrival_radius,
                timeout_s=timeout_s,
            )
            attempt["center_result"] = center_result.to_payload()
            if not center_result.success:
                attempts.append(attempt)
                last_failure = center_result
                continue
        attempts.append(attempt)
        final_state = body.get_state()
        final_distance = dist(final_state.pos, (target[0] + 0.5, target[1] + 0.5, target[2] + 0.5))
        if final_distance <= interaction_radius:
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
                "target": list(target),
                "stand_target": list(stand),
                "initial_distance": initial_distance,
                "final_distance": final_distance,
            },
        )

    if last_failure is None:
        last_failure = ToolResult(success=False, reason=failure_prefix, can_retry=True, metrics={"target": list(target)})
    return ToolResult(
        success=False,
        reason=f"{failure_prefix}:{last_failure.reason}",
        can_retry=last_failure.can_retry,
        next_suggestion=last_failure.next_suggestion,
        metrics={**dict(last_failure.metrics or {}), "target": list(target), "attempts": attempts},
    )


def _move_to_stand_center(
    body: Body,
    stand: Position,
    *,
    arrival_radius: float,
    timeout_s: float,
) -> ToolResult:
    center = (stand[0] + 0.5, stand[1], stand[2] + 0.5)
    precise_radius = min(arrival_radius, 0.1)
    action = Action.create(
        "moveTo",
        {
            "target": list(center),
            "waypoints": [list(center)],
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
                "center": list(center),
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
    stop_result = _stop_body_controls(body, timeout_s=min(timeout_s, 2.0)) if result.success else None
    return ToolResult(
        success=result.success and (stop_result is None or stop_result.success),
        reason=result.reason if stop_result is None or stop_result.success else f"stabilize_failed:{stop_result.reason}",
        can_retry=result.can_retry or (stop_result.can_retry if stop_result is not None else False),
        next_suggestion=result.next_suggestion,
        metrics={
            "action_id": action.id,
            "stand": list(stand),
            "center": list(center),
            "center_radius": precise_radius,
            **dict(result.metrics or {}),
            **({"stabilize": stop_result.to_payload()} if stop_result is not None else {}),
        },
    )


def _stop_body_controls(body: Body, *, timeout_s: float) -> ToolResult:
    action = Action.create("stop", {})
    accepted = body.execute(action)
    if not (accepted.ok and accepted.accepted):
        return ToolResult(
            success=False,
            reason="body_rejected",
            can_retry=True,
            metrics={
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
        metrics={"action_id": action.id, **dict(result.metrics or {})},
    )


def ensure_entity_range(
    body: Body,
    navigator: InteractionNavigator | None,
    target: tuple[float, float, float],
    *,
    min_distance: float,
    max_distance: float,
    vertical_tolerance: float,
    timeout_s: float,
    missing_reason: str,
    failure_prefix: str,
    no_stand_reason: str,
    include_entity_block: bool = False,
) -> dict[str, object] | ToolResult:
    state = body.get_state()
    initial_distance = dist(state.pos, target)
    initial_vertical = fabs(state.pos[1] - target[1])
    if initial_distance <= max_distance and initial_distance >= min_distance and initial_vertical <= vertical_tolerance:
        return {
            "navigated": False,
            "initial_distance": initial_distance,
            "final_distance": initial_distance,
            "initial_vertical_delta": initial_vertical,
            "final_vertical_delta": initial_vertical,
        }

    if navigator is None:
        return ToolResult(
            success=False,
            reason=missing_reason,
            can_retry=True,
            next_suggestion="provide a navigation transaction before attempting distant entity interaction",
            metrics={
                "target": list(target),
                "initial_distance": initial_distance,
                "initial_vertical_delta": initial_vertical,
            },
        )

    stand_points = entity_stand_points(
        body,
        target,
        include_entity_block=include_entity_block,
        min_distance=min_distance,
        max_distance=max_distance,
    )
    if isinstance(stand_points, ToolResult):
        return stand_points
    if not stand_points:
        return ToolResult(
            success=False,
            reason=no_stand_reason,
            can_retry=False,
            next_suggestion="clear a standable adjacent block near the receiver before retrying",
            metrics={
                "target": list(target),
                "initial_distance": initial_distance,
                "initial_vertical_delta": initial_vertical,
            },
        )

    attempts: list[dict[str, object]] = []
    last_failure: ToolResult | None = None
    for stand in stand_points:
        nav_result = navigator.navigate_to(stand, timeout_s=timeout_s)
        attempts.append({"goal": list(stand), "result": nav_result.to_payload()})
        if not nav_result.success:
            last_failure = nav_result
            continue
        final_state = body.get_state()
        final_distance = dist(final_state.pos, target)
        final_vertical = fabs(final_state.pos[1] - target[1])
        if final_distance <= max_distance and final_distance >= min_distance and final_vertical <= vertical_tolerance:
            return {
                "navigated": True,
                "stand_target": list(stand),
                "initial_distance": initial_distance,
                "final_distance": final_distance,
                "initial_vertical_delta": initial_vertical,
                "final_vertical_delta": final_vertical,
                "attempts": attempts,
            }
        last_failure = ToolResult(
            success=False,
            reason="target_out_of_range_after_navigation",
            can_retry=True,
            metrics={
                "target": list(target),
                "stand_target": list(stand),
                "initial_distance": initial_distance,
                "final_distance": final_distance,
                "initial_vertical_delta": initial_vertical,
                "final_vertical_delta": final_vertical,
            },
        )

    if last_failure is None:
        last_failure = ToolResult(success=False, reason=failure_prefix, can_retry=True, metrics={"target": list(target)})
    return ToolResult(
        success=False,
        reason=f"{failure_prefix}:{last_failure.reason}",
        can_retry=last_failure.can_retry,
        next_suggestion=last_failure.next_suggestion,
        metrics={**dict(last_failure.metrics or {}), "target": list(target), "attempts": attempts},
    )


def interaction_stand_points(
    body: Body,
    target: Position,
    *,
    include_target: bool = False,
) -> list[Position] | ToolResult:
    same_level = _interaction_stand_points_at_y(body, target, target[1], include_target=include_target)
    if isinstance(same_level, ToolResult) or same_level:
        return same_level
    return _interaction_stand_points_at_y(body, target, target[1] - 1, include_target=include_target)


def _interaction_stand_points_at_y(
    body: Body,
    target: Position,
    stand_y: int,
    *,
    include_target: bool,
) -> list[Position] | ToolResult:
    state = body.get_state()
    candidates: list[tuple[float, Position]] = []
    seen: set[Position] = set()
    offsets = STAND_OFFSETS + ((0, 0, 0),) if include_target else STAND_OFFSETS
    for dx, dy, dz in offsets:
        pos = (target[0] + dx, stand_y + dy, target[2] + dz)
        if pos in seen:
            continue
        seen.add(pos)
        stand = body.perceive("blockAt", {"x": pos[0], "y": pos[1], "z": pos[2]})
        failed = perception_failure(stand)
        if failed is not None:
            return failed
        head = body.perceive("blockAt", {"x": pos[0], "y": pos[1] + 1, "z": pos[2]})
        failed = perception_failure(head)
        if failed is not None:
            return failed
        below = body.perceive("blockAt", {"x": pos[0], "y": pos[1] - 1, "z": pos[2]})
        failed = perception_failure(below)
        if failed is not None:
            return failed
        if not _interaction_feet_clear(stand):
            continue
        if not _interaction_head_clear(head, head_pos=(pos[0], pos[1] + 1, pos[2]), target=target):
            continue
        if not _interaction_support_standable(below):
            continue
        candidates.append((dist(state.pos, (pos[0] + 0.5, pos[1], pos[2] + 0.5)), pos))
    candidates.sort(key=lambda item: (item[0], item[1]))
    return [pos for _distance, pos in candidates]


def entity_stand_points(
    body: Body,
    target: tuple[float, float, float],
    *,
    include_entity_block: bool = False,
    min_distance: float = 0.0,
    max_distance: float | None = None,
) -> list[tuple[float, float, float]] | ToolResult:
    block_pos = (floor(target[0]), floor(target[1]), floor(target[2]))
    stand_points = _entity_distance_band_stand_points(
        body,
        target,
        min_distance=min_distance,
        max_distance=max_distance,
    )
    if isinstance(stand_points, ToolResult) or not include_entity_block:
        return stand_points

    state = body.get_state()
    dx = target[0] - state.pos[0]
    dz = target[2] - state.pos[2]
    horizontal = (dx * dx + dz * dz) ** 0.5
    if horizontal <= 0.0001:
        return stand_points

    handoff = (
        target[0] - dx / horizontal * ENTITY_HANDOFF_OFFSET,
        target[1],
        target[2] - dz / horizontal * ENTITY_HANDOFF_OFFSET,
    )
    handoff_block = (floor(handoff[0]), floor(handoff[1]), floor(handoff[2]))
    feet = body.perceive("blockAt", {"x": handoff_block[0], "y": handoff_block[1], "z": handoff_block[2]})
    failed = perception_failure(feet)
    if failed is not None:
        return failed
    head = body.perceive("blockAt", {"x": handoff_block[0], "y": handoff_block[1] + 1, "z": handoff_block[2]})
    failed = perception_failure(head)
    if failed is not None:
        return failed
    below = body.perceive("blockAt", {"x": handoff_block[0], "y": handoff_block[1] - 1, "z": handoff_block[2]})
    failed = perception_failure(below)
    if failed is not None:
        return failed
    if (
        _interaction_feet_clear(feet)
        and _interaction_head_clear(head, head_pos=(handoff_block[0], handoff_block[1] + 1, handoff_block[2]), target=block_pos)
        and _interaction_support_standable(below)
    ):
        return [*stand_points, handoff]
    return stand_points


def _entity_distance_band_stand_points(
    body: Body,
    target: tuple[float, float, float],
    *,
    min_distance: float,
    max_distance: float | None,
) -> list[Position] | ToolResult:
    block_pos = (floor(target[0]), floor(target[1]), floor(target[2]))
    radius = max(1, int(max_distance or INTERACTION_RANGE) + 1)
    state = body.get_state()
    candidates: list[tuple[float, Position]] = []
    seen: set[Position] = set()

    for dx in range(-radius, radius + 1):
        for dz in range(-radius, radius + 1):
            if dx == 0 and dz == 0:
                continue
            pos = (block_pos[0] + dx, block_pos[1], block_pos[2] + dz)
            if pos in seen:
                continue
            seen.add(pos)
            stand_distance = dist((pos[0] + 0.5, pos[1], pos[2] + 0.5), target)
            if stand_distance < min_distance:
                continue
            if max_distance is not None and stand_distance > max_distance:
                continue
            stand = body.perceive("blockAt", {"x": pos[0], "y": pos[1], "z": pos[2]})
            failed = perception_failure(stand)
            if failed is not None:
                return failed
            head = body.perceive("blockAt", {"x": pos[0], "y": pos[1] + 1, "z": pos[2]})
            failed = perception_failure(head)
            if failed is not None:
                return failed
            below = body.perceive("blockAt", {"x": pos[0], "y": pos[1] - 1, "z": pos[2]})
            failed = perception_failure(below)
            if failed is not None:
                return failed
            if not _interaction_feet_clear(stand):
                continue
            if not _interaction_head_clear(head, head_pos=(pos[0], pos[1] + 1, pos[2]), target=block_pos):
                continue
            if not _interaction_support_standable(below):
                continue
            candidates.append((dist(state.pos, (pos[0] + 0.5, pos[1], pos[2] + 0.5)), pos))

    candidates.sort(key=lambda item: (item[0], item[1]))
    return [pos for _distance, pos in candidates]


def perception_failure(perception: PerceptionResult) -> ToolResult | None:
    if perception.ok and perception.complete:
        return None
    return ToolResult(
        success=False,
        reason="perception_failed",
        can_retry=True,
        next_suggestion="refresh world and inventory facts before attempting the interaction",
        metrics={
            "scope": perception.scope,
            "ok": perception.ok,
            "complete": perception.complete,
            "error": perception.error,
            "uncertainty": perception.uncertainty,
        },
    )


def normalize_block_type(block_type: str) -> str:
    return block_type.removeprefix("minecraft:")


def normalize_entity_type(entity_type: str) -> str:
    return entity_type.removeprefix("minecraft:")


def block_type_matches_wanted(block_type: str, wanted: set[str]) -> bool:
    normalized = normalize_block_type(block_type)
    if normalized in wanted:
        return True
    # Some live server reads collapse colored beds to the base `bed` id.
    if "bed" in wanted and normalized.endswith("_bed"):
        return True
    if normalized == "bed" and any(candidate.endswith("_bed") for candidate in wanted):
        return True
    return False


def _interaction_feet_clear(perception: PerceptionResult) -> bool:
    return str(perception.data.get("state") or "UNKNOWN") == "CLEAR"


def _interaction_head_clear(
    perception: PerceptionResult,
    *,
    head_pos: Position,
    target: Position,
) -> bool:
    return str(perception.data.get("state") or "UNKNOWN") == "CLEAR" or head_pos == target


def _interaction_support_standable(perception: PerceptionResult) -> bool:
    if str(perception.data.get("state") or "UNKNOWN") != "SOLID":
        return False
    block_type = normalize_block_type(str(perception.data.get("type") or "unknown"))
    properties = {str(key): str(value).lower() for key, value in dict(perception.data.get("properties") or {}).items()}
    if block_type.endswith("_slab"):
        return properties.get("type") == "bottom"
    if block_type.endswith("_stairs"):
        return properties.get("half", "bottom") == "bottom"
    return True


def _entity_metrics(target: NearbyEntityTarget) -> dict[str, object]:
    return {
        "id": target.entity_id,
        "name": target.name,
        "type": target.entity_type,
        "pos": list(target.pos),
        "health": target.health,
        "distance": target.distance,
    }


def merge_context(result: ToolResult, extra: dict[str, object]) -> ToolResult:
    metrics = dict(result.metrics or {})
    metrics.update(extra)
    return ToolResult(
        success=result.success,
        reason=result.reason,
        can_retry=result.can_retry,
        next_suggestion=result.next_suggestion,
        metrics=metrics,
    )
