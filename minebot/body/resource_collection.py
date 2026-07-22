"""Body-owned resource-domain collection process."""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Callable

from minebot.body.block_approach import (
    blacklist_candidate_clusters,
    select_spatial_candidate_clusters,
)
from minebot.body.block_work import (
    BlockWork,
    _is_clear_perception,
    _is_solid_support_perception,
    _mining_approach_stand_candidates,
    _mining_reach_distance,
    _mining_stand_sort_key,
)
from minebot.body.interaction_support import (
    NearbyBlockSearch,
    NearbyBlockTarget,
    find_nearby_block_search,
)
from minebot.body.navigation import (
    SERVER_GOAL_SET_LIMIT,
    NavigationRunConfig,
    NavigationTransactions,
    dry_land_navigation_config,
)
from minebot.body.world_read import read_block_facts
from minebot.contract import Body, BreakContext, Position, ToolResult, is_candidate_skip
from minebot.game.navigation import GoalComposite, GoalNear


@dataclass(frozen=True)
class ResourceCollectionConfig:
    search_radius: int = 16
    candidate_budget: int = 8
    mutation_budget: int = 8
    max_wall_s: float = 60.0
    find_limit: int = 12
    max_pages: int = 1
    max_goals: int = SERVER_GOAL_SET_LIMIT
    segment_timeout_s: float = 15.0
    tree_domain_search_radius: int = 6
    tree_domain_target_limit: int = 24
    tree_domain_max_retargets: int = 4


@dataclass(frozen=True)
class _StandDomain:
    goals: tuple[Position, ...]
    targets_by_goal: dict[Position, tuple[NearbyBlockTarget, ...]]
    targets: tuple[NearbyBlockTarget, ...]
    diagnostics: dict[str, object]


class ResourceCollectionTransactions:
    """Own physical candidate selection for one bounded resource objective."""

    def __init__(
        self,
        body: Body,
        navigator: NavigationTransactions,
        work: BlockWork,
        *,
        mobility_egress: Callable[[float], ToolResult] | None = None,
    ) -> None:
        self.body = body
        self.navigator = navigator
        self.work = work
        self.mobility_egress = mobility_egress

    def collect_block_domain(
        self,
        *,
        block_types: tuple[str, ...],
        expected_drops: tuple[str, ...],
        remaining_count: int,
        dry: bool = False,
        config: ResourceCollectionConfig | None = None,
    ) -> ToolResult:
        cfg = config or ResourceCollectionConfig()
        invalid = _validate_request(block_types, expected_drops, remaining_count, cfg)
        if invalid is not None:
            return invalid

        normalized_blocks = tuple(dict.fromkeys(_normalize_item(item) for item in block_types))
        normalized_drops = tuple(dict.fromkeys(_normalize_item(item) for item in expected_drops))
        started = time.monotonic()
        collected = 0
        candidate_attempts = 0
        mutation_attempts = 0
        candidate_blacklist: set[Position] = set()
        patch_blacklist: list[Position] = []
        attempts: list[dict[str, object]] = []
        searches: list[dict[str, object]] = []
        navigation_failures: list[str] = []
        mobility_egress_attempted = False
        pending_targets: tuple[NearbyBlockTarget, ...] = ()
        tree_pending_targets: tuple[NearbyBlockTarget, ...] = ()
        tree_retargets_discovered = 0

        dry_egress = self.work.egress_to_dry(timeout_s=cfg.segment_timeout_s)
        if not dry_egress.success:
            return self._terminal(
                success=False,
                reason=f"resource_{dry_egress.reason}",
                can_retry=dry_egress.can_retry,
                block_types=normalized_blocks,
                expected_drops=normalized_drops,
                remaining_count=remaining_count,
                collected=collected,
                candidate_blacklist=candidate_blacklist,
                patch_blacklist=patch_blacklist,
                attempts=attempts,
                searches=searches,
                config=cfg,
                started=started,
                last_failure=dry_egress.to_payload(),
            )

        while collected < remaining_count:
            if (
                mutation_attempts >= cfg.mutation_budget
                or time.monotonic() - started >= cfg.max_wall_s
            ):
                return self._terminal(
                    success=False,
                    reason="resource_domain_budget_exhausted",
                    can_retry=True,
                    block_types=normalized_blocks,
                    expected_drops=normalized_drops,
                    remaining_count=remaining_count,
                    collected=collected,
                    candidate_blacklist=candidate_blacklist,
                    patch_blacklist=patch_blacklist,
                    attempts=attempts,
                    searches=searches,
                    config=cfg,
                    started=started,
                    navigation_failures=navigation_failures,
                )

            candidate_budget_hit = candidate_attempts >= cfg.candidate_budget
            search = find_nearby_block_search(
                self.body,
                normalized_blocks,
                cfg.search_radius,
                not_found_reason="resource_candidates_not_found",
                limit=cfg.find_limit,
                max_pages=cfg.max_pages,
            )
            if isinstance(search, ToolResult):
                if (
                    (not pending_targets and not tree_pending_targets)
                    or search.reason != "resource_candidates_not_found"
                ):
                    reason = "resource_domain_partial_exhausted" if collected > 0 else search.reason
                    return self._terminal(
                        success=False,
                        reason=reason,
                        can_retry=search.can_retry,
                        block_types=normalized_blocks,
                        expected_drops=normalized_drops,
                        remaining_count=remaining_count,
                        collected=collected,
                        candidate_blacklist=candidate_blacklist,
                        patch_blacklist=patch_blacklist,
                        attempts=attempts,
                        searches=searches,
                        config=cfg,
                        started=started,
                        last_failure=search.to_payload(),
                    )
                search = NearbyBlockSearch(
                    targets=[],
                    truncated=True,
                    uncertainty=[{"reason": "pending_candidate_reuse", "search_reason": search.reason}],
                    errors=[search.reason],
                    pages_read=int((search.metrics or {}).get("pages_read") or 0),
                    total_matches=0,
                )

            active = _active_targets(
                search,
                candidate_blacklist=candidate_blacklist,
                patch_blacklist=patch_blacklist,
                limit=cfg.find_limit if candidate_budget_hit else max(1, cfg.candidate_budget - candidate_attempts),
                pending_targets=pending_targets,
                priority_targets=tree_pending_targets,
            )
            searches.append(_search_metrics(search, active))
            if candidate_budget_hit and not tree_pending_targets:
                exhausted = not active
                if exhausted:
                    terminal_reason = (
                        "resource_domain_partial_exhausted"
                        if collected > 0
                        else "resource_candidate_domain_exhausted"
                    )
                else:
                    terminal_reason = "resource_domain_budget_exhausted"
                terminal_reason = _candidate_exhaustion_terminal_reason(
                    terminal_reason,
                    navigation_failures=navigation_failures,
                    mutation_attempts=mutation_attempts,
                )
                return self._terminal(
                    success=False,
                    reason=terminal_reason,
                    can_retry=True,
                    block_types=normalized_blocks,
                    expected_drops=normalized_drops,
                    remaining_count=remaining_count,
                    collected=collected,
                    candidate_blacklist=candidate_blacklist,
                    patch_blacklist=patch_blacklist,
                    attempts=attempts,
                    searches=searches,
                    config=cfg,
                    started=started,
                    navigation_failures=navigation_failures,
                )
            if not active:
                terminal_reason = _candidate_exhaustion_terminal_reason(
                    "resource_domain_partial_exhausted" if collected > 0 else "resource_candidate_domain_exhausted",
                    navigation_failures=navigation_failures,
                    mutation_attempts=mutation_attempts,
                )
                return self._terminal(
                    success=False,
                    reason=terminal_reason,
                    can_retry=True,
                    block_types=normalized_blocks,
                    expected_drops=normalized_drops,
                    remaining_count=remaining_count,
                    collected=collected,
                    candidate_blacklist=candidate_blacklist,
                    patch_blacklist=patch_blacklist,
                    attempts=attempts,
                    searches=searches,
                    config=cfg,
                    started=started,
                    navigation_failures=navigation_failures,
                )

            domain = _build_stand_domain(self.body, active, max_goals=cfg.max_goals)
            if isinstance(domain, ToolResult):
                return self._terminal(
                    success=False,
                    reason=domain.reason,
                    can_retry=domain.can_retry,
                    block_types=normalized_blocks,
                    expected_drops=normalized_drops,
                    remaining_count=remaining_count,
                    collected=collected,
                    candidate_blacklist=candidate_blacklist,
                    patch_blacklist=patch_blacklist,
                    attempts=attempts,
                    searches=searches,
                    config=cfg,
                    started=started,
                    last_failure=domain.to_payload(),
                )

            goal = GoalComposite(tuple(GoalNear(pos, radius=0) for pos in domain.goals))
            nav_config = dry_land_navigation_config(
                replace(
                    NavigationRunConfig(),
                    segment_timeout_s=cfg.segment_timeout_s,
                    max_break_steps=self.work.MINE_APPROACH_MAX_BREAK_STEPS,
                )
            )
            navigation = self.navigator.navigate_to(
                goal,
                break_context=BreakContext.COLLECT_APPROACH,
                config=nav_config,
            )
            selected_goal = _selected_goal(navigation, domain.goals)
            selected_targets = domain.targets_by_goal.get(selected_goal, ())
            attempt: dict[str, object] = {
                "goal_count": len(domain.goals),
                "candidate_count": len(domain.targets),
                "selected_goal": list(selected_goal),
                "selected_targets": [list(target.pos) for target in selected_targets],
                "goal_set": [list(pos) for pos in domain.goals],
                "domain": domain.diagnostics,
                "navigation": navigation.to_payload(),
            }

            if navigation.reason in {"preempted", "body_missing", "death", "respawned", "progress_yielded"}:
                attempts.append(attempt)
                return self._terminal(
                    success=False,
                    reason=f"resource_navigation_{navigation.reason}",
                    can_retry=True,
                    block_types=normalized_blocks,
                    expected_drops=normalized_drops,
                    remaining_count=remaining_count,
                    collected=collected,
                    candidate_blacklist=candidate_blacklist,
                    patch_blacklist=patch_blacklist,
                    attempts=attempts,
                    searches=searches,
                    config=cfg,
                    started=started,
                    last_failure=navigation.to_payload(),
                )

            if not navigation.success:
                attempts.append(attempt)
                navigation_failures.append(navigation.reason)
                pending_targets = tuple(active)
                egress_succeeded = False
                selected_positions = {target.pos for target in selected_targets}
                tree_pending_targets = tuple(
                    target for target in tree_pending_targets if target.pos not in selected_positions
                )
                if (
                    navigation.reason == "no_path"
                    and not mobility_egress_attempted
                    and self.mobility_egress is not None
                ):
                    before_egress = self.body.get_state()
                    mobility_egress_attempted = True
                    egress = self.mobility_egress(30.0)
                    after_egress = self.body.get_state()
                    egress_payload = {
                        "success": egress.success,
                        "reason": egress.reason,
                        "can_retry": egress.can_retry,
                        "final_pos": list(after_egress.pos),
                        "distance": _distance_between(before_egress.pos, after_egress.pos),
                        "result": egress.to_payload(),
                    }
                    attempt["mobility_egress"] = egress_payload
                    egress_succeeded = egress.success
                    if egress_succeeded:
                        untried_targets = tuple(
                            target for target in active if target.pos not in selected_positions
                        )
                        pending_targets = untried_targets or tuple(selected_targets)
                rejected_targets = selected_targets or domain.targets
                if egress_succeeded and pending_targets == tuple(selected_targets):
                    rejected_targets = ()
                blacklist_size = len(candidate_blacklist)
                blacklist_candidate_clusters(
                    candidate_blacklist,
                    (target.pos for target in rejected_targets),
                )
                for target in rejected_targets:
                    if _is_patch_resource(target.block_type) and _is_patch_blocker(navigation.reason):
                        _add_patch_blacklist(patch_blacklist, target.pos)
                candidate_attempts += len(candidate_blacklist) - blacklist_size
                tree_pending_targets = _remove_tree_pending_clusters(
                    tree_pending_targets,
                    tuple(target.pos for target in rejected_targets),
                )

                tree_candidates: tuple[NearbyBlockTarget, ...] = ()
                if (
                    not tree_pending_targets
                    and tree_retargets_discovered < cfg.tree_domain_max_retargets
                    and _has_tree_resource(normalized_blocks)
                    and _is_tree_navigation_failure(navigation.reason)
                ):
                    excluded = set(selected_positions)
                    discovered = _probe_tree_domain_targets(
                        self.body,
                        selected_targets or domain.targets,
                        _tree_log_types(normalized_blocks),
                        cfg.tree_domain_search_radius,
                        excluded=excluded | selected_positions,
                        limit=cfg.tree_domain_target_limit,
                    )
                    tree_diagnostics: dict[str, object] = {
                        "original_target": list(selected_targets[0].pos if selected_targets else selected_goal),
                        "original_failure": navigation.to_payload(),
                        "search_radius": cfg.tree_domain_search_radius,
                        "target_limit": cfg.tree_domain_target_limit,
                        "block_types": list(_tree_log_types(normalized_blocks)),
                    }
                    if isinstance(discovered, ToolResult):
                        tree_diagnostics["search_result"] = discovered.to_payload()
                    else:
                        current = self.body.get_state().pos
                        tree_candidates = tuple(
                            sorted(
                                (
                                    target
                                    for target in discovered
                                    if target.pos not in excluded
                                    and target.pos not in selected_positions
                                ),
                                key=lambda target: _tree_domain_target_sort_key(
                                    current,
                                    target.pos,
                                    target.distance,
                                ),
                            )[: max(0, cfg.tree_domain_max_retargets - tree_retargets_discovered)]
                        )
                        tree_diagnostics["candidate_count"] = len(tree_candidates)
                        tree_retargets_discovered += len(tree_candidates)
                    tree_diagnostics["candidates"] = [list(target.pos) for target in tree_candidates]
                    attempt["tree_domain_retarget"] = tree_diagnostics
                    tree_pending_targets = tree_candidates
                continue

            target = _selected_target(self.body, selected_targets)
            if target is None:
                attempts.append(attempt)
                return self._terminal(
                    success=False,
                    reason="resource_selected_goal_unmapped",
                    can_retry=False,
                    block_types=normalized_blocks,
                    expected_drops=normalized_drops,
                    remaining_count=remaining_count,
                    collected=collected,
                    candidate_blacklist=candidate_blacklist,
                    patch_blacklist=patch_blacklist,
                    attempts=attempts,
                    searches=searches,
                    config=cfg,
                    started=started,
                    last_failure=navigation.to_payload(),
                )

            candidate_attempts += 1
            mutation_attempts += 1
            tree_pending_targets = tuple(candidate for candidate in tree_pending_targets if candidate.pos != target.pos)
            pending_targets = tuple(candidate for candidate in pending_targets if candidate.pos != target.pos)
            mined = self.work.mine_block_collect(
                target.pos,
                context=BreakContext.COLLECT,
                dry=dry,
                expected_drops=normalized_drops,
                target_block_types=normalized_blocks,
                timeout_s=cfg.segment_timeout_s,
                prepositioned=True,
            )
            attempt["target"] = list(target.pos)
            attempt["block_type"] = target.block_type
            attempt["mine"] = mined.to_payload()
            attempts.append(attempt)

            if mined.success:
                delta = max(0, int((mined.metrics or {}).get("collected_total") or 0))
                collected += delta
                candidate_blacklist.discard(target.pos)
                _remove_patch_blacklist(patch_blacklist, target.pos)
                if delta <= 0:
                    blacklist_candidate_clusters(candidate_blacklist, (target.pos,))
                continue

            if mined.reason == "missing_required_tool" or mined.reason.startswith("tool_equip_failed:"):
                return self._terminal(
                    success=False,
                    reason=mined.reason,
                    can_retry=mined.can_retry,
                    block_types=normalized_blocks,
                    expected_drops=normalized_drops,
                    remaining_count=remaining_count,
                    collected=collected,
                    candidate_blacklist=candidate_blacklist,
                    patch_blacklist=patch_blacklist,
                    attempts=attempts,
                    searches=searches,
                    config=cfg,
                    started=started,
                    last_failure=mined.to_payload(),
                )

            if is_candidate_skip(mined.reason) or mined.reason == "collect_no_inventory_delta":
                blacklist_candidate_clusters(candidate_blacklist, (target.pos,))
                tree_pending_targets = _remove_tree_pending_clusters(tree_pending_targets, (target.pos,))
                if _is_patch_resource(target.block_type) and _is_patch_blocker(mined.reason):
                    _add_patch_blacklist(patch_blacklist, target.pos)
                continue

            return self._terminal(
                success=False,
                reason=f"resource_collect_failed:{mined.reason}",
                can_retry=mined.can_retry,
                block_types=normalized_blocks,
                expected_drops=normalized_drops,
                remaining_count=remaining_count,
                collected=collected,
                candidate_blacklist=candidate_blacklist,
                patch_blacklist=patch_blacklist,
                attempts=attempts,
                searches=searches,
                config=cfg,
                started=started,
                last_failure=mined.to_payload(),
            )

        return self._terminal(
            success=True,
            reason="resource_domain_collected",
            can_retry=False,
            block_types=normalized_blocks,
            expected_drops=normalized_drops,
            remaining_count=remaining_count,
            collected=collected,
            candidate_blacklist=candidate_blacklist,
            patch_blacklist=patch_blacklist,
            attempts=attempts,
            searches=searches,
            config=cfg,
            started=started,
        )

    def _terminal(
        self,
        *,
        success: bool,
        reason: str,
        can_retry: bool,
        block_types: tuple[str, ...],
        expected_drops: tuple[str, ...],
        remaining_count: int,
        collected: int,
        candidate_blacklist: set[Position],
        patch_blacklist: list[Position],
        attempts: list[dict[str, object]],
        searches: list[dict[str, object]],
        config: ResourceCollectionConfig,
        started: float,
        last_failure: dict[str, object] | None = None,
        navigation_failures: list[str] | None = None,
    ) -> ToolResult:
        metrics: dict[str, object] = {
            "block_types": list(block_types),
            "expected_drops": list(expected_drops),
            "requested_delta": remaining_count,
            "collected_total": collected,
            "remaining_delta": max(0, remaining_count - collected),
            "complete": collected >= remaining_count,
            "candidate_blacklist": [list(pos) for pos in sorted(candidate_blacklist)],
            "patch_blacklist": [list(pos) for pos in patch_blacklist],
            "attempts": attempts,
            "searches": searches,
            "elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
            "budget": {
                "candidate_budget": config.candidate_budget,
                "mutation_budget": config.mutation_budget,
                "max_wall_s": config.max_wall_s,
                "find_limit": config.find_limit,
                "max_pages": config.max_pages,
                "max_goals": config.max_goals,
                "tree_domain_search_radius": config.tree_domain_search_radius,
                "tree_domain_target_limit": config.tree_domain_target_limit,
                "tree_domain_max_retargets": config.tree_domain_max_retargets,
            },
        }
        if last_failure is not None:
            metrics["last_failure"] = last_failure
        if navigation_failures:
            metrics["navigation_failure_reasons"] = list(navigation_failures)
        return ToolResult(success, reason, can_retry, metrics=metrics)


def _validate_request(
    block_types: tuple[str, ...],
    expected_drops: tuple[str, ...],
    remaining_count: int,
    config: ResourceCollectionConfig,
) -> ToolResult | None:
    if not block_types:
        return ToolResult(False, "resource_block_types_missing", False)
    if not expected_drops:
        return ToolResult(False, "resource_expected_drops_missing", False)
    if remaining_count <= 0:
        return ToolResult(False, "resource_remaining_count_invalid", False, metrics={"remaining_count": remaining_count})
    if config.search_radius <= 0:
        return ToolResult(False, "resource_search_radius_invalid", False)
    if config.candidate_budget <= 0 or config.mutation_budget <= 0:
        return ToolResult(False, "resource_budget_invalid", False)
    if config.max_wall_s <= 0 or config.segment_timeout_s <= 0:
        return ToolResult(False, "resource_timeout_invalid", False)
    if config.find_limit <= 0 or config.max_pages <= 0:
        return ToolResult(False, "resource_search_budget_invalid", False)
    if (
        config.tree_domain_search_radius <= 0
        or config.tree_domain_target_limit <= 0
        or config.tree_domain_max_retargets <= 0
    ):
        return ToolResult(False, "resource_tree_domain_budget_invalid", False)
    if config.max_goals <= 0 or config.max_goals > SERVER_GOAL_SET_LIMIT:
        return ToolResult(
            False,
            "resource_goal_budget_invalid",
            False,
            metrics={"max_goals": config.max_goals, "server_goal_set_limit": SERVER_GOAL_SET_LIMIT},
        )
    return None


def _active_targets(
    search: NearbyBlockSearch,
    *,
    candidate_blacklist: set[Position],
    patch_blacklist: list[Position],
    limit: int,
    pending_targets: tuple[NearbyBlockTarget, ...] = (),
    priority_targets: tuple[NearbyBlockTarget, ...] = (),
) -> tuple[NearbyBlockTarget, ...]:
    priority: list[NearbyBlockTarget] = []
    seen_priority: set[Position] = set()
    for target in priority_targets:
        if target.pos in seen_priority:
            continue
        if _is_patch_resource(target.block_type) and _in_patch_blacklist(target.pos, patch_blacklist):
            continue
        seen_priority.add(target.pos)
        priority.append(target)
        if len(priority) >= limit:
            break
    if priority:
        return tuple(priority)

    by_position = {target.pos: target for target in pending_targets}
    by_position.update({target.pos: target for target in search.targets})
    eligible = [
        target
        for target in by_position.values()
        if not (
            _is_patch_resource(target.block_type)
            and _in_patch_blacklist(target.pos, patch_blacklist)
        )
    ]
    return select_spatial_candidate_clusters(
        eligible,
        blacklist=candidate_blacklist,
        limit=limit,
    )


def _has_tree_resource(block_types: tuple[str, ...]) -> bool:
    return bool(_tree_log_types(block_types))


def _tree_log_types(block_types: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            _normalize_item(block_type)
            for block_type in block_types
            if _is_log_block_type(block_type)
        )
    )


def _is_log_block_type(block_type: str) -> bool:
    return _normalize_item(block_type).endswith("_log")


def _is_tree_navigation_failure(reason: str) -> bool:
    return reason in {"no_path", "stuck", "deviated", "budget_exceeded"}


def _tree_domain_target_sort_key(
    current: tuple[float, float, float],
    target: Position,
    distance: float,
) -> tuple[float, int, float, float, int, int]:
    return (
        abs(float(target[1]) - current[1]),
        target[1],
        _mining_reach_distance(current, target),
        distance,
        target[0],
        target[2],
    )


def _probe_tree_domain_targets(
    body: Body,
    anchors: tuple[NearbyBlockTarget, ...],
    block_types: tuple[str, ...],
    radius: int,
    *,
    excluded: set[Position],
    limit: int,
) -> list[NearbyBlockTarget] | ToolResult:
    """Read a bounded vertical tree neighborhood around failed candidates."""

    horizontal_radius = min(2, radius)
    positions = tuple(
        dict.fromkeys(
            (anchor.pos[0] + dx, anchor.pos[1] + dy, anchor.pos[2] + dz)
            for anchor in anchors
            for dx in range(-horizontal_radius, horizontal_radius + 1)
            for dz in range(-horizontal_radius, horizontal_radius + 1)
            for dy in range(-radius, radius + 1)
        )
    )
    try:
        facts = read_block_facts(body, positions, failure_label="resource_tree_domain")
    except ValueError as exc:
        return ToolResult(
            False,
            "tree_domain_probe_failed",
            True,
            metrics={
                "anchor_targets": [list(anchor.pos) for anchor in anchors],
                "search_radius": radius,
                "horizontal_radius": horizontal_radius,
                "requested_cells": len(positions),
                "error": str(exc),
            },
        )

    wanted = set(block_types)
    current = body.get_state().pos
    candidates: list[NearbyBlockTarget] = []
    for pos, perception in facts.items():
        block_type = _normalize_item(str(perception.data.get("type") or ""))
        if block_type not in wanted or pos in excluded:
            continue
        candidates.append(
            NearbyBlockTarget(
                pos=pos,
                block_type=block_type,
                distance=_distance_between(
                    current,
                    (pos[0] + 0.5, pos[1] + 0.5, pos[2] + 0.5),
                ),
            )
        )
    if not candidates:
        return ToolResult(
            False,
            "tree_domain_log_not_found",
            True,
            next_suggestion="change the tree candidate or use bounded exploration before retrying",
            metrics={
                "anchor_targets": [list(anchor.pos) for anchor in anchors],
                "search_radius": radius,
                "horizontal_radius": horizontal_radius,
                "requested_cells": len(positions),
            },
        )
    return sorted(
        candidates,
        key=lambda target: _tree_domain_target_sort_key(current, target.pos, target.distance),
    )[:limit]


def _remove_tree_pending_clusters(
    pending: tuple[NearbyBlockTarget, ...],
    centers: tuple[Position, ...],
) -> tuple[NearbyBlockTarget, ...]:
    return tuple(
        target
        for target in pending
        if not any(_same_candidate_cluster(target.pos, center) for center in centers)
    )


def _same_candidate_cluster(left: Position, right: Position) -> bool:
    return (
        abs(left[0] - right[0]) <= 2
        and abs(left[1] - right[1]) <= 6
        and abs(left[2] - right[2]) <= 2
    )


def _candidate_exhaustion_terminal_reason(
    fallback_reason: str,
    *,
    navigation_failures: list[str],
    mutation_attempts: int,
) -> str:
    """Preserve a route-only resource failure without relabeling other budgets."""

    if (
        mutation_attempts == 0
        and navigation_failures
        and all(_is_no_path_route_failure(reason) for reason in navigation_failures)
    ):
        return "resource_navigation_no_path"
    return fallback_reason


def _is_no_path_route_failure(reason: str) -> bool:
    return reason == "no_path"


def _build_stand_domain(
    body: Body,
    targets: tuple[NearbyBlockTarget, ...],
    *,
    max_goals: int,
) -> _StandDomain | ToolResult:
    current = body.get_state().pos
    approaches: dict[Position, tuple[Position, ...]] = {}
    wanted: list[Position] = []
    for target in targets:
        target_approaches = _mining_approach_stand_candidates(target.pos)
        approaches[target.pos] = target_approaches
        for stand in target_approaches:
            wanted.extend((stand, (stand[0], stand[1] + 1, stand[2]), (stand[0], stand[1] - 1, stand[2])))
    try:
        facts = read_block_facts(body, tuple(dict.fromkeys(wanted)), failure_label="resource_stand_domain")
    except ValueError as exc:
        return ToolResult(
            False,
            "perception_failed",
            True,
            metrics={"scope": "blockCells", "failure_label": "resource_stand_domain", "error": str(exc)},
        )

    stands_by_target: dict[Position, list[Position]] = {}
    for target in targets:
        standable: list[Position] = []
        for stand in approaches[target.pos]:
            feet = facts.get(stand)
            head = facts.get((stand[0], stand[1] + 1, stand[2]))
            support = facts.get((stand[0], stand[1] - 1, stand[2]))
            if feet is None or head is None or support is None:
                continue
            if _is_clear_perception(feet) and _is_clear_perception(head) and _is_solid_support_perception(support):
                standable.append(stand)
        candidates = standable or list(approaches[target.pos])
        candidates.sort(key=lambda stand: _mining_stand_sort_key(current, target.pos, stand))
        stands_by_target[target.pos] = list(dict.fromkeys(candidates))

    goals: list[Position] = []
    targets_by_goal: dict[Position, list[NearbyBlockTarget]] = {}
    depth = 0
    pending = True
    while pending and len(goals) < max_goals:
        pending = False
        for target in targets:
            candidates = stands_by_target[target.pos]
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

    if not goals:
        return ToolResult(
            False,
            "resource_candidate_domain_exhausted",
            True,
            metrics={"candidate_targets": [list(target.pos) for target in targets], "reason": "no_stand_goals"},
        )
    return _StandDomain(
        goals=tuple(goals),
        targets_by_goal={stand: tuple(linked) for stand, linked in targets_by_goal.items()},
        targets=targets,
        diagnostics={
            "candidate_targets": [
                {
                    "pos": list(target.pos),
                    "block_type": target.block_type,
                    "stand_count": len(stands_by_target[target.pos]),
                }
                for target in targets
            ],
            "goal_count": len(goals),
            "max_goals": max_goals,
            "batched_stand_cells": len(facts),
        },
    )


def _selected_goal(result: ToolResult, goals: tuple[Position, ...]) -> Position:
    raw = (result.metrics or {}).get("selected_goal", (result.metrics or {}).get("goal"))
    if isinstance(raw, (list, tuple)) and len(raw) >= 3:
        selected = (int(raw[0]), int(raw[1]), int(raw[2]))
        if selected in goals:
            return selected
    return goals[0]


def _selected_target(body: Body, targets: tuple[NearbyBlockTarget, ...]) -> NearbyBlockTarget | None:
    if not targets:
        return None
    current = body.get_state().pos
    reachable = [
        target
        for target in targets
        if _mining_reach_distance(current, target.pos) <= BlockWork.MINE_INTERACTION_RANGE
    ]
    if not reachable:
        return None
    return min(reachable, key=lambda target: (_mining_reach_distance(current, target.pos), target.pos))


def _search_metrics(search: NearbyBlockSearch, active: tuple[NearbyBlockTarget, ...]) -> dict[str, object]:
    return {
        "total_matches": search.total_matches,
        "pages_read": search.pages_read,
        "truncated": search.truncated,
        "uncertainty": list(search.uncertainty),
        "returned_candidates": len(search.targets),
        "active_candidates": [list(target.pos) for target in active],
    }


def _is_patch_resource(block_type: str) -> bool:
    normalized = _normalize_item(block_type)
    return normalized.endswith("_log") or normalized.endswith("_stem") or normalized in {"log", "logs"}


def _is_patch_blocker(reason: str) -> bool:
    return "not_natural_breakable" in reason or reason in {"no_path", "stuck", "deviated"}


def _same_patch(left: Position, right: Position) -> bool:
    return abs(left[0] - right[0]) <= 2 and abs(left[2] - right[2]) <= 2 and abs(left[1] - right[1]) <= 6


def _in_patch_blacklist(target: Position, blocked: list[Position]) -> bool:
    return any(_same_patch(target, center) for center in blocked)


def _add_patch_blacklist(blocked: list[Position], target: Position) -> None:
    if not _in_patch_blacklist(target, blocked):
        blocked.append(target)


def _remove_patch_blacklist(blocked: list[Position], target: Position) -> None:
    blocked[:] = [center for center in blocked if not _same_patch(target, center)]


def _normalize_item(item: str) -> str:
    return item.removeprefix("minecraft:").strip().lower()


def _distance_between(left: tuple[float, float, float], right: tuple[float, float, float]) -> float:
    return ((left[0] - right[0]) ** 2 + (left[1] - right[1]) ** 2 + (left[2] - right[2]) ** 2) ** 0.5
