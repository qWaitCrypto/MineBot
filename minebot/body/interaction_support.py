"""Shared helper logic for block-anchored interaction transactions."""

from __future__ import annotations

from dataclasses import dataclass
from math import fabs
from math import floor
from math import dist
from typing import Protocol

from minebot.body.navigation import (
    SERVER_GOAL_SET_LIMIT,
    NavigationRunConfig,
    pure_movement_navigation_config,
)
from minebot.contract import Action, Body, PerceptionResult, Position, ToolResult, perception_next_cursor
from minebot.contract import terminal_event_to_tool_result
from minebot.body.world_read import read_block_facts
from minebot.game.navigation import GoalComposite, GoalLike, GoalNear


INTERACTION_RANGE = 4.5
STAND_OFFSETS = ((1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1))
ENTITY_HANDOFF_OFFSET = 0.4
ENTITY_STAND_ARRIVAL_RADIUS = 0.25


class InteractionNavigator(Protocol):
    def navigate_to(self, goal: GoalLike, **kwargs) -> ToolResult: ...


def _navigation_goal_for_stands(stands: list[Position]) -> GoalComposite:
    unique = tuple(dict.fromkeys(stands))
    if not unique:
        raise ValueError("stand goal set requires at least one position")
    if len(unique) > SERVER_GOAL_SET_LIMIT:
        raise ValueError(f"stand goal set exceeds server limit {SERVER_GOAL_SET_LIMIT}")
    return GoalComposite(tuple(GoalNear(stand, radius=0) for stand in unique))


def _selected_navigation_stand(result: ToolResult, stands: list[Position]) -> Position:
    candidates = tuple(dict.fromkeys(stands))
    metrics = dict(result.metrics or {})
    raw = metrics.get("selected_goal", metrics.get("goal"))
    if isinstance(raw, (list, tuple)) and len(raw) >= 3:
        selected = (int(raw[0]), int(raw[1]), int(raw[2]))
        if selected in candidates:
            return selected
    return candidates[0]


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


@dataclass(frozen=True)
class NearbyBlockSearch:
    targets: list[NearbyBlockTarget]
    truncated: bool
    uncertainty: list[object]
    errors: list[str]
    pages_read: int
    total_matches: int


def _read_find_blocks_pages(
    body: Body,
    *,
    wanted_type: str,
    radius: int,
    y_radius: int,
    limit: int,
    max_pages: int = 32,
) -> dict[str, object] | ToolResult:
    start: int | None = 0
    pages_read = 0
    blocks: list[dict[str, object]] = []
    uncertainty: list[object] = []
    complete = False
    total_matches = 0
    while start is not None:
        found = body.perceive(
            "findBlocks",
            {
                "type": wanted_type,
                "radius": radius,
                "y_radius": y_radius,
                "limit": limit,
                "start": start,
            },
        )
        pages_read += 1
        if not found.ok:
            failed = perception_failure(found)
            if failed is not None:
                return failed
            return ToolResult(False, "perception_failed", True, metrics={"scope": "findBlocks", "error": found.error})
        page_blocks = [dict(item) for item in found.data.get("blocks") or [] if isinstance(item, dict)]
        blocks.extend(page_blocks)
        uncertainty.extend(list(found.uncertainty or ()))
        total_matches = max(total_matches, int(found.data.get("totalMatches") or len(blocks)))
        next_start = perception_next_cursor(found)
        if found.complete or next_start is None:
            complete = found.complete
            break
        if pages_read >= max_pages:
            uncertainty.append({"reason": "page_limit", "max_pages": max_pages})
            break
        start = int(next_start)
    return {
        "blocks": blocks,
        "complete": complete,
        "uncertainty": uncertainty,
        "pages_read": pages_read,
        "total_matches": total_matches,
    }


def find_nearby_block_targets(
    body: Body,
    block_types: tuple[str, ...],
    radius: int,
    *,
    not_found_reason: str,
    limit: int = 64,
) -> list[NearbyBlockTarget] | ToolResult:
    search = find_nearby_block_search(body, block_types, radius, not_found_reason=not_found_reason, limit=limit)
    if isinstance(search, ToolResult):
        return search
    return search.targets


def _default_find_blocks_y_radius(radius: int) -> int:
    return min(max(4, radius // 2), 16)


def find_nearby_block_search(
    body: Body,
    block_types: tuple[str, ...],
    radius: int,
    *,
    not_found_reason: str,
    limit: int = 64,
    max_pages: int = 1,
) -> NearbyBlockSearch | ToolResult:
    state = body.get_state()
    wanted = sorted({normalize_block_type(block_type) for block_type in block_types})
    candidates: list[NearbyBlockTarget] = []
    seen: set[Position] = set()
    uncertainty: list[object] = []
    errors: list[str] = []
    truncated = False
    pages_read = 0
    total_matches = 0
    y_radius = _default_find_blocks_y_radius(radius)
    for wanted_type in wanted:
        pages = _read_find_blocks_pages(
            body,
            wanted_type=wanted_type,
            radius=radius,
            y_radius=y_radius,
            limit=limit,
            max_pages=max_pages,
        )
        if isinstance(pages, ToolResult):
            return pages
        blocks = pages["blocks"]
        uncertainty.extend(pages["uncertainty"])
        pages_read += int(pages["pages_read"])
        total_matches += int(pages["total_matches"])
        if not pages["complete"]:
            truncated = True
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
            metrics={"search_radius": radius, "block_types": list(block_types), "limit": limit, "uncertainty": uncertainty},
        )
    return NearbyBlockSearch(
        targets=sorted(candidates, key=lambda candidate: (candidate.distance, candidate.pos)),
        truncated=truncated,
        uncertainty=uncertainty,
        errors=errors,
        pages_read=pages_read,
        total_matches=total_matches,
    )


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
    nearby = body.perceive(
        "nearbyEntities",
        {"radius": radius, "limit": 64, "types": list(wanted_types), "name": entity_name},
    )
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
    query: dict[str, object] = {"radius": radius, "limit": 64}
    if wanted_types:
        query["types"] = list(wanted_types)
    if entity_name is not None:
        query["name"] = entity_name
    nearby = body.perceive("nearbyEntities", query)
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

    nav_kwargs: dict[str, object] = {"timeout_s": timeout_s}
    if navigation_arrival_radius is not None:
        nav_kwargs["arrival_radius"] = navigation_arrival_radius
    nav_result = navigator.navigate_to(_navigation_goal_for_stands(stand_candidates), **nav_kwargs)
    selected_stand = _selected_navigation_stand(nav_result, stand_candidates)
    attempt: dict[str, object] = {
        "goals": [list(stand) for stand in stand_candidates],
        "selected_goal": list(selected_stand),
        "result": nav_result.to_payload(),
    }
    attempts = [attempt]
    if not nav_result.success:
        last_failure = nav_result
    else:
        last_failure = None
        if navigation_arrival_radius is not None and center_after_navigation:
            center_result = move_to_block_center(
                body,
                selected_stand,
                arrival_radius=navigation_arrival_radius,
                timeout_s=timeout_s,
            )
            attempt["center_result"] = center_result.to_payload()
            if not center_result.success:
                last_failure = center_result
        if last_failure is None:
            final_state = body.get_state()
            final_distance = dist(final_state.pos, (target[0] + 0.5, target[1] + 0.5, target[2] + 0.5))
            if final_distance <= interaction_radius:
                return {
                    "navigated": True,
                    "stand_target": list(selected_stand),
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
                    "stand_target": list(selected_stand),
                    "initial_distance": initial_distance,
                    "final_distance": final_distance,
                },
            )

    return ToolResult(
        success=False,
        reason=f"{failure_prefix}:{last_failure.reason}",
        can_retry=last_failure.can_retry,
        next_suggestion=last_failure.next_suggestion,
        metrics={**dict(last_failure.metrics or {}), "target": list(target), "attempts": attempts},
    )


def move_to_block_center(
    body: Body,
    stand: Position,
    *,
    arrival_radius: float,
    timeout_s: float,
    stabilize: bool = True,
) -> ToolResult:
    """Execute one bounded sub-block centering pulse inside ``stand``.

    This is not route planning: the caller already owns the destination block
    and uses this primitive only when exact horizontal placement matters.
    """

    center = (stand[0] + 0.5, stand[1], stand[2] + 0.5)
    precise_radius = min(arrival_radius, 0.1)
    action = Action.create(
        "moveTo",
        {
            "target": list(center),
            "waypoints": [list(center)],
            "arrival_radius": precise_radius,
            "timeout_ticks": min(80, max(20, int(timeout_s * 20))),
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
    stop_result = (
        _stop_body_controls(body, timeout_s=min(timeout_s, 2.0))
        if result.success and stabilize
        else None
    )
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
    navigation_config: NavigationRunConfig | None = None,
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

    navigation_kwargs: dict[str, object] = {
        "timeout_s": timeout_s,
        "arrival_radius": ENTITY_STAND_ARRIVAL_RADIUS,
        "config": pure_movement_navigation_config(navigation_config),
    }
    nav_result = navigator.navigate_to(_navigation_goal_for_stands(stand_points), **navigation_kwargs)
    selected_stand = _selected_navigation_stand(nav_result, stand_points)
    attempts = [
        {
            "goals": [list(stand) for stand in stand_points],
            "selected_goal": list(selected_stand),
            "result": nav_result.to_payload(),
        }
    ]
    if not nav_result.success:
        last_failure = nav_result
    else:
        final_state = body.get_state()
        final_distance = dist(final_state.pos, target)
        final_vertical = fabs(final_state.pos[1] - target[1])
        if final_distance <= max_distance and final_distance >= min_distance and final_vertical <= vertical_tolerance:
            return {
                "navigated": True,
                "stand_target": list(selected_stand),
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
                "stand_target": list(selected_stand),
                "initial_distance": initial_distance,
                "final_distance": final_distance,
                "initial_vertical_delta": initial_vertical,
                "final_vertical_delta": final_vertical,
            },
        )

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
    expand_vertical: bool = False,
    interaction_radius: float = INTERACTION_RANGE,
) -> list[Position] | ToolResult:
    same_level = _interaction_stand_points_at_y(body, target, target[1], include_target=include_target)
    if isinstance(same_level, ToolResult) or same_level:
        return same_level
    one_below = _interaction_stand_points_at_y(body, target, target[1] - 1, include_target=include_target)
    if isinstance(one_below, ToolResult) or one_below or not expand_vertical:
        return one_below

    current_y = body.get_state().pos[1]
    extra_levels = _interaction_vertical_reach_levels(
        target,
        interaction_radius=interaction_radius,
        current_y=current_y,
    )
    for stand_y in extra_levels:
        stands = _interaction_stand_points_at_y(body, target, stand_y, include_target=include_target)
        if isinstance(stands, ToolResult) or stands:
            return stands
    return []


def _interaction_vertical_reach_levels(
    target: Position,
    *,
    interaction_radius: float,
    current_y: float,
) -> tuple[int, ...]:
    if interaction_radius <= 0:
        return ()
    vertical_limit = int(interaction_radius)
    levels = []
    for offset in range(-vertical_limit, vertical_limit + 2):
        stand_y = target[1] + offset
        if stand_y in {target[1], target[1] - 1}:
            continue
        stand_center = (target[0] + 1.5, float(stand_y), target[2] + 0.5)
        target_center = (target[0] + 0.5, target[1] + 0.5, target[2] + 0.5)
        if dist(stand_center, target_center) <= interaction_radius:
            levels.append(stand_y)
    levels.sort(key=lambda stand_y: (abs(float(stand_y) - current_y), abs(stand_y - target[1]), stand_y))
    return tuple(levels)


def _interaction_stand_points_at_y(
    body: Body,
    target: Position,
    stand_y: int,
    *,
    include_target: bool,
) -> list[Position] | ToolResult:
    state = body.get_state()
    offsets = STAND_OFFSETS + ((0, 0, 0),) if include_target else STAND_OFFSETS
    feet_positions: list[Position] = []
    seen: set[Position] = set()
    for dx, dy, dz in offsets:
        pos = (target[0] + dx, stand_y + dy, target[2] + dz)
        if pos in seen:
            continue
        seen.add(pos)
        feet_positions.append(pos)
    wanted: list[Position] = []
    for pos in feet_positions:
        wanted.append(pos)
        wanted.append((pos[0], pos[1] + 1, pos[2]))
        wanted.append((pos[0], pos[1] - 1, pos[2]))
    try:
        facts = read_block_facts(body, tuple(wanted), failure_label="interaction_stand")
    except ValueError as exc:
        return ToolResult(
            success=False,
            reason="perception_failed",
            can_retry=True,
            next_suggestion="refresh world and inventory facts before attempting the interaction",
            metrics={"scope": "blockCells", "ok": False, "complete": False, "error": str(exc), "uncertainty": None},
        )
    candidates: list[tuple[float, Position]] = []
    for pos in feet_positions:
        stand = facts.get(pos)
        head = facts.get((pos[0], pos[1] + 1, pos[2]))
        below = facts.get((pos[0], pos[1] - 1, pos[2]))
        if stand is None or head is None or below is None:
            continue
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
    max_points: int = SERVER_GOAL_SET_LIMIT,
) -> list[tuple[float, float, float]] | ToolResult:
    if max_points < 1 or max_points > SERVER_GOAL_SET_LIMIT:
        raise ValueError(f"max_points must be between 1 and {SERVER_GOAL_SET_LIMIT}")
    block_pos = (floor(target[0]), floor(target[1]), floor(target[2]))
    stand_points = _entity_distance_band_stand_points(
        body,
        target,
        min_distance=min_distance,
        max_distance=max_distance,
        max_points=max_points,
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
        if handoff in stand_points:
            return stand_points
        return [*stand_points[: max_points - 1], handoff]
    return stand_points


def _entity_distance_band_stand_points(
    body: Body,
    target: tuple[float, float, float],
    *,
    min_distance: float,
    max_distance: float | None,
    max_points: int,
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
    return [pos for _distance, pos in candidates[:max_points]]


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
