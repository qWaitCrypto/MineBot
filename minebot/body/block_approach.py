"""Body-owned candidate selection and approach for block objectives."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from math import dist

from minebot.body.interaction_support import (
    NearbyBlockSearch,
    NearbyBlockTarget,
    block_type_matches_wanted,
    find_nearby_block_search,
    interaction_stand_points,
    normalize_block_type,
    perception_failure,
)
from minebot.body.navigation import (
    SERVER_GOAL_SET_LIMIT,
    NavigationRunConfig,
    NavigationTransactions,
    pure_movement_navigation_config,
)
from minebot.contract import Body, BreakContext, Position, ToolResult
from minebot.game.navigation import GoalComposite, GoalNear


@dataclass(frozen=True)
class GetToBlockConfig:
    search_radius: int = 16
    interaction_radius: float = 4.5
    candidate_budget: int = 8
    candidate_batch_size: int = 8
    find_limit: int = 16
    max_pages: int = 1
    max_goals: int = SERVER_GOAL_SET_LIMIT
    max_segments: int = 5
    segment_timeout_s: float = 15.0


@dataclass(frozen=True)
class _BlockStandDomain:
    goals: tuple[Position, ...]
    targets_by_goal: dict[Position, tuple[NearbyBlockTarget, ...]]
    targets: tuple[NearbyBlockTarget, ...]
    targets_without_stands: tuple[NearbyBlockTarget, ...]
    diagnostics: dict[str, object]


class BlockApproachTransactions:
    """Approach one usable block without delegating candidate choice to Brain."""

    def __init__(
        self,
        body: Body,
        navigator: NavigationTransactions,
    ) -> None:
        self.body = body
        self.navigator = navigator

    def get_to_block(
        self,
        *,
        block_types: tuple[str, ...],
        config: GetToBlockConfig | None = None,
    ) -> ToolResult:
        cfg = config or GetToBlockConfig()
        invalid = _validate_request(block_types, cfg)
        if invalid is not None:
            return invalid

        wanted = tuple(dict.fromkeys(normalize_block_type(value) for value in block_types))
        wanted_set = set(wanted)
        origin = self.body.get_state().pos
        blacklist: set[Position] = set()
        attempts: list[dict[str, object]] = []
        searches: list[dict[str, object]] = []

        while len(blacklist) < cfg.candidate_budget:
            search = find_nearby_block_search(
                self.body,
                wanted,
                cfg.search_radius,
                not_found_reason="get_to_block_not_found",
                limit=cfg.find_limit,
                max_pages=cfg.max_pages,
            )
            if isinstance(search, ToolResult):
                return _terminal(
                    search,
                    wanted=wanted,
                    origin=origin,
                    blacklist=blacklist,
                    attempts=attempts,
                    searches=searches,
                    config=cfg,
                )

            active = _active_candidate_clusters(
                search.targets,
                blacklist=blacklist,
                limit=min(cfg.candidate_batch_size, cfg.candidate_budget - len(blacklist)),
            )
            searches.append(_search_payload(search, active))
            if not active:
                return _terminal(
                    ToolResult(False, "get_to_block_candidate_domain_exhausted", True),
                    wanted=wanted,
                    origin=origin,
                    blacklist=blacklist,
                    attempts=attempts,
                    searches=searches,
                    config=cfg,
                )

            in_range_failure = self._already_in_range(active, wanted_set, cfg.interaction_radius)
            if isinstance(in_range_failure, ToolResult):
                if in_range_failure.success:
                    return _terminal(
                        in_range_failure,
                        wanted=wanted,
                        origin=origin,
                        blacklist=blacklist,
                        attempts=attempts,
                        searches=searches,
                        config=cfg,
                    )
                if in_range_failure.reason == "perception_failed":
                    return _terminal(
                        in_range_failure,
                        wanted=wanted,
                        origin=origin,
                        blacklist=blacklist,
                        attempts=attempts,
                        searches=searches,
                        config=cfg,
                    )
                stale = _metric_position(in_range_failure, "target")
                if stale is not None:
                    blacklist.add(stale)
                    attempts.append({"phase": "precheck", "target": list(stale), "result": in_range_failure.to_payload()})
                    continue

            domain = _build_block_stand_domain(
                self.body,
                active,
                max_goals=cfg.max_goals,
                interaction_radius=cfg.interaction_radius,
            )
            if isinstance(domain, ToolResult):
                return _terminal(
                    domain,
                    wanted=wanted,
                    origin=origin,
                    blacklist=blacklist,
                    attempts=attempts,
                    searches=searches,
                    config=cfg,
                )
            no_stand = tuple(target.pos for target in domain.targets_without_stands)
            _blacklist_candidate_clusters(blacklist, no_stand)
            if not domain.goals:
                attempts.append(
                    {
                        "phase": "stand_domain",
                        "targets": [list(pos) for pos in sorted(no_stand)],
                        "result": {"success": False, "reason": "get_to_block_no_stand_domain"},
                        "domain": domain.diagnostics,
                    }
                )
                continue

            goal = GoalComposite(tuple(GoalNear(pos, radius=0) for pos in domain.goals))
            navigation = self.navigator.navigate_to(
                goal,
                break_context=BreakContext.PATH,
                config=pure_movement_navigation_config(
                    replace(
                        NavigationRunConfig(),
                        max_segments=cfg.max_segments,
                        segment_timeout_s=cfg.segment_timeout_s,
                    )
                ),
            )
            selected_goal = _selected_goal(navigation, domain.goals)
            selected_targets = domain.targets_by_goal.get(selected_goal, ())
            attempt: dict[str, object] = {
                "phase": "navigation",
                "goal_count": len(domain.goals),
                "goal_set": [list(pos) for pos in domain.goals],
                "selected_goal": list(selected_goal),
                "selected_targets": [list(target.pos) for target in selected_targets],
                "domain": domain.diagnostics,
                "navigation": navigation.to_payload(),
            }

            if not navigation.success or navigation.reason != "arrived":
                attempts.append(attempt)
                if navigation.reason in {
                    "preempted",
                    "body_missing",
                    "death",
                    "respawned",
                    "progress_yielded",
                    "interrupted",
                }:
                    return _terminal(
                        ToolResult(
                            False,
                            f"get_to_block_navigation_{navigation.reason}",
                            True,
                            metrics={"navigation": navigation.to_payload()},
                        ),
                        wanted=wanted,
                        origin=origin,
                        blacklist=blacklist,
                        attempts=attempts,
                        searches=searches,
                        config=cfg,
                    )
                if not selected_targets:
                    return _terminal(
                        ToolResult(False, "get_to_block_selected_goal_unmapped", False),
                        wanted=wanted,
                        origin=origin,
                        blacklist=blacklist,
                        attempts=attempts,
                        searches=searches,
                        config=cfg,
                    )
                _blacklist_candidate_clusters(blacklist, (target.pos for target in selected_targets))
                continue

            verified = self._verify_selected_targets(selected_targets, wanted_set, cfg.interaction_radius)
            attempt["verification"] = verified.to_payload()
            attempts.append(attempt)
            if verified.success:
                metrics = dict(verified.metrics or {})
                metrics.update(
                    {
                        "selected_goal": list(selected_goal),
                        "navigation": navigation.to_payload(),
                    }
                )
                return _terminal(
                    ToolResult(True, "block_reached", False, metrics=metrics),
                    wanted=wanted,
                    origin=origin,
                    blacklist=blacklist,
                    attempts=attempts,
                    searches=searches,
                    config=cfg,
                )
            if verified.reason == "perception_failed":
                return _terminal(
                    verified,
                    wanted=wanted,
                    origin=origin,
                    blacklist=blacklist,
                    attempts=attempts,
                    searches=searches,
                    config=cfg,
                )
            _blacklist_candidate_clusters(blacklist, (target.pos for target in selected_targets))

        return _terminal(
            ToolResult(False, "get_to_block_candidate_budget_exhausted", True),
            wanted=wanted,
            origin=origin,
            blacklist=blacklist,
            attempts=attempts,
            searches=searches,
            config=cfg,
        )

    def _already_in_range(
        self,
        targets: tuple[NearbyBlockTarget, ...],
        wanted: set[str],
        interaction_radius: float,
    ) -> ToolResult | None:
        current = self.body.get_state().pos
        for target in targets:
            target_distance = _distance_to_target(current, target.pos)
            if target_distance > interaction_radius:
                continue
            return self._verify_target(target, wanted, interaction_radius, already_in_range=True)
        return None

    def _verify_selected_targets(
        self,
        targets: tuple[NearbyBlockTarget, ...],
        wanted: set[str],
        interaction_radius: float,
    ) -> ToolResult:
        if not targets:
            return ToolResult(False, "get_to_block_selected_goal_unmapped", False)
        failures: list[dict[str, object]] = []
        for target in targets:
            result = self._verify_target(target, wanted, interaction_radius, already_in_range=False)
            if result.success or result.reason == "perception_failed":
                return result
            failures.append(result.to_payload())
        return ToolResult(
            False,
            "get_to_block_target_unusable",
            True,
            metrics={"targets": [list(target.pos) for target in targets], "failures": failures},
        )

    def _verify_target(
        self,
        target: NearbyBlockTarget,
        wanted: set[str],
        interaction_radius: float,
        *,
        already_in_range: bool,
    ) -> ToolResult:
        block = self.body.perceive(
            "blockAt",
            {"x": target.pos[0], "y": target.pos[1], "z": target.pos[2]},
        )
        failed = perception_failure(block)
        if failed is not None:
            return failed
        actual_type = normalize_block_type(str(block.data.get("type") or "unknown"))
        final_pos = self.body.get_state().pos
        final_distance = _distance_to_target(final_pos, target.pos)
        metrics = {
            "target": list(target.pos),
            "expected_type": target.block_type,
            "actual_type": actual_type,
            "final_pos": list(final_pos),
            "final_distance": final_distance,
            "interaction_radius": interaction_radius,
            "identity_verified": block_type_matches_wanted(actual_type, wanted),
            "range_verified": final_distance <= interaction_radius,
            "already_in_range": already_in_range,
        }
        if not metrics["identity_verified"]:
            return ToolResult(False, "get_to_block_target_changed", True, metrics=metrics)
        if not metrics["range_verified"]:
            return ToolResult(False, "get_to_block_target_out_of_range", True, metrics=metrics)
        return ToolResult(True, "block_reached", False, metrics=metrics)


def _validate_request(block_types: tuple[str, ...], cfg: GetToBlockConfig) -> ToolResult | None:
    if not block_types:
        return ToolResult(False, "get_to_block_filter_missing", False)
    if cfg.search_radius < 1:
        return ToolResult(False, "invalid_search_radius", False)
    if cfg.interaction_radius <= 0:
        return ToolResult(False, "invalid_interaction_radius", False)
    if cfg.candidate_budget < 1 or cfg.candidate_batch_size < 1 or cfg.find_limit < 1:
        return ToolResult(False, "invalid_candidate_budget", False)
    if cfg.max_pages < 1:
        return ToolResult(False, "invalid_page_budget", False)
    if cfg.max_goals < 1 or cfg.max_goals > SERVER_GOAL_SET_LIMIT:
        return ToolResult(False, "invalid_goal_budget", False)
    if cfg.max_segments < 1 or cfg.segment_timeout_s <= 0:
        return ToolResult(False, "invalid_navigation_budget", False)
    return None


def _build_block_stand_domain(
    body: Body,
    targets: tuple[NearbyBlockTarget, ...],
    *,
    max_goals: int,
    interaction_radius: float,
) -> _BlockStandDomain | ToolResult:
    stands_by_target: dict[Position, list[Position]] = {}
    for target in targets:
        stands = interaction_stand_points(
            body,
            target.pos,
            expand_vertical=True,
            interaction_radius=interaction_radius,
        )
        if isinstance(stands, ToolResult):
            return stands
        stands_by_target[target.pos] = list(dict.fromkeys(stands))

    goals: list[Position] = []
    targets_by_goal: dict[Position, list[NearbyBlockTarget]] = {}
    depth = 0
    pending = True
    while pending and len(goals) < max_goals:
        pending = False
        for target in targets:
            stands = stands_by_target[target.pos]
            if depth >= len(stands):
                continue
            pending = True
            stand = stands[depth]
            if stand not in goals:
                goals.append(stand)
            linked = targets_by_goal.setdefault(stand, [])
            if target not in linked:
                linked.append(target)
            if len(goals) >= max_goals:
                break
        depth += 1

    return _BlockStandDomain(
        goals=tuple(goals),
        targets_by_goal={goal: tuple(linked) for goal, linked in targets_by_goal.items()},
        targets=targets,
        targets_without_stands=tuple(target for target in targets if not stands_by_target[target.pos]),
        diagnostics={
            "candidate_targets": [
                {
                    "pos": list(target.pos),
                    "block_type": target.block_type,
                    "stand_count": len(stands_by_target[target.pos]),
                    "expanded_vertical": any(
                        stand[1] not in {target.pos[1], target.pos[1] - 1}
                        for stand in stands_by_target[target.pos]
                    ),
                }
                for target in targets
            ],
            "goal_count": len(goals),
            "max_goals": max_goals,
        },
    )


def _active_candidate_clusters(
    targets: list[NearbyBlockTarget],
    *,
    blacklist: set[Position],
    limit: int,
) -> tuple[NearbyBlockTarget, ...]:
    representatives: list[NearbyBlockTarget] = []
    for target in targets:
        if _in_candidate_cluster(target.pos, blacklist):
            continue
        if _in_candidate_cluster(target.pos, (candidate.pos for candidate in representatives)):
            continue
        representatives.append(target)
    if len(representatives) <= limit:
        return tuple(representatives)

    active = [representatives[0]]
    remaining = representatives[1:]
    while remaining and len(active) < limit:
        selected_index = max(
            range(len(remaining)),
            key=lambda index: (
                min(
                    _horizontal_distance_sq(remaining[index].pos, selected.pos)
                    for selected in active
                ),
                -index,
            ),
        )
        active.append(remaining.pop(selected_index))
    return tuple(active)


def _blacklist_candidate_clusters(blacklist: set[Position], positions: Iterable[Position]) -> None:
    for pos in positions:
        if not _in_candidate_cluster(pos, blacklist):
            blacklist.add(pos)


def _in_candidate_cluster(pos: Position, centers: Iterable[Position]) -> bool:
    return any(
        abs(pos[0] - center[0]) <= 2
        and abs(pos[1] - center[1]) <= 6
        and abs(pos[2] - center[2]) <= 2
        for center in centers
    )


def _horizontal_distance_sq(left: Position, right: Position) -> int:
    return (left[0] - right[0]) ** 2 + (left[2] - right[2]) ** 2


def _selected_goal(result: ToolResult, goals: tuple[Position, ...]) -> Position:
    raw = (result.metrics or {}).get("selected_goal", (result.metrics or {}).get("goal"))
    if isinstance(raw, (list, tuple)) and len(raw) >= 3:
        selected = (int(raw[0]), int(raw[1]), int(raw[2]))
        if selected in goals:
            return selected
    return goals[0]


def _metric_position(result: ToolResult, key: str) -> Position | None:
    raw = (result.metrics or {}).get(key)
    if not isinstance(raw, (list, tuple)) or len(raw) < 3:
        return None
    return (int(raw[0]), int(raw[1]), int(raw[2]))


def _distance_to_target(pos: tuple[float, float, float], target: Position) -> float:
    return dist(pos, (target[0] + 0.5, target[1] + 0.5, target[2] + 0.5))


def _search_payload(search: NearbyBlockSearch, active: tuple[NearbyBlockTarget, ...]) -> dict[str, object]:
    return {
        "candidates": [
            {"pos": list(target.pos), "block_type": target.block_type, "distance": target.distance}
            for target in search.targets
        ],
        "active": [list(target.pos) for target in active],
        "truncated": search.truncated,
        "uncertainty": list(search.uncertainty),
        "errors": list(search.errors),
        "pages_read": search.pages_read,
        "total_matches": search.total_matches,
    }


def _terminal(
    result: ToolResult,
    *,
    wanted: tuple[str, ...],
    origin: tuple[float, float, float],
    blacklist: set[Position],
    attempts: list[dict[str, object]],
    searches: list[dict[str, object]],
    config: GetToBlockConfig,
) -> ToolResult:
    metrics = dict(result.metrics or {})
    metrics.update(
        {
            "block_types": list(wanted),
            "origin": list(origin),
            "candidate_blacklist": [list(pos) for pos in sorted(blacklist)],
            "attempts": attempts,
            "searches": searches,
            "config": {
                "search_radius": config.search_radius,
                "interaction_radius": config.interaction_radius,
                "candidate_budget": config.candidate_budget,
                "candidate_batch_size": config.candidate_batch_size,
                "find_limit": config.find_limit,
                "max_pages": config.max_pages,
                "max_goals": config.max_goals,
                "max_segments": config.max_segments,
                "segment_timeout_s": config.segment_timeout_s,
            },
        }
    )
    return ToolResult(
        success=result.success,
        reason=result.reason,
        can_retry=result.can_retry,
        next_suggestion=result.next_suggestion,
        metrics=metrics,
    )


__all__ = ["BlockApproachTransactions", "GetToBlockConfig"]
