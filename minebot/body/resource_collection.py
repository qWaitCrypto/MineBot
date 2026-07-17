"""Body-owned resource-domain collection process."""

from __future__ import annotations

import time
from dataclasses import dataclass, replace

from minebot.body.block_work import (
    BlockWork,
    _is_clear_perception,
    _is_solid_support_perception,
    _mining_approach_stand_candidates,
    _mining_reach_distance,
    _mining_stand_sort_key,
)
from minebot.body.interaction_support import NearbyBlockSearch, NearbyBlockTarget, find_nearby_block_search
from minebot.body.navigation import NavigationRunConfig, NavigationTransactions, SERVER_GOAL_SET_LIMIT
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
    ) -> None:
        self.body = body
        self.navigator = navigator
        self.work = work

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

            active = _active_targets(
                search,
                candidate_blacklist=candidate_blacklist,
                patch_blacklist=patch_blacklist,
                limit=cfg.find_limit if candidate_budget_hit else max(1, cfg.candidate_budget - candidate_attempts),
            )
            searches.append(_search_metrics(search, active))
            if candidate_budget_hit:
                exhausted = not active
                if exhausted:
                    terminal_reason = (
                        "resource_domain_partial_exhausted"
                        if collected > 0
                        else "resource_candidate_domain_exhausted"
                    )
                else:
                    terminal_reason = "resource_domain_budget_exhausted"
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
                )
            if not active:
                return self._terminal(
                    success=False,
                    reason="resource_domain_partial_exhausted" if collected > 0 else "resource_candidate_domain_exhausted",
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
            nav_config = replace(
                NavigationRunConfig(),
                segment_timeout_s=cfg.segment_timeout_s,
                max_break_steps=self.work.MINE_APPROACH_MAX_BREAK_STEPS,
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
                rejected_targets = selected_targets or domain.targets
                for target in rejected_targets:
                    candidate_blacklist.add(target.pos)
                    if _is_patch_resource(target.block_type) and _is_patch_blocker(navigation.reason):
                        _add_patch_blacklist(patch_blacklist, target.pos)
                candidate_attempts += len(rejected_targets)
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
                    candidate_blacklist.add(target.pos)
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
                candidate_blacklist.add(target.pos)
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
            },
        }
        if last_failure is not None:
            metrics["last_failure"] = last_failure
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
) -> tuple[NearbyBlockTarget, ...]:
    out: list[NearbyBlockTarget] = []
    for target in search.targets:
        if target.pos in candidate_blacklist:
            continue
        if _is_patch_resource(target.block_type) and _in_patch_blacklist(target.pos, patch_blacklist):
            continue
        out.append(target)
        if len(out) >= limit:
            break
    return tuple(out)


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
