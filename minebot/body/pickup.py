"""Body-owned dropped-item pickup process."""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from math import floor
from typing import TYPE_CHECKING, Protocol

from minebot.body.inventory_read import read_inventory_counts
from minebot.contract import Body, BreakContext, Position, ToolResult
from minebot.game.navigation import GoalComposite, GoalNear

if TYPE_CHECKING:
    from minebot.body.navigation import NavigationRunConfig


class PickupNavigator(Protocol):
    def navigate_to(self, goal, **kwargs) -> ToolResult: ...


@dataclass(frozen=True)
class PickupConfig:
    radius: int = 8
    entity_limit: int = 16
    max_goals: int = 16
    max_scan_rounds: int = 2
    candidate_budget: int = 5
    max_wall_s: float = 20.0
    poll_timeout_s: float = 1.5
    segment_timeout_s: float = 6.0
    max_segments: int = 3
    arrival_radius: float = 0.25
    # Resource collection may supply its provider-local, load-limited profile
    # so the post-mutation pickup route does not reopen an unbounded A* path.
    navigation_config: "NavigationRunConfig | None" = None


@dataclass(frozen=True)
class _PickupCandidate:
    key: str
    pos: Position
    entity_id: str | None


class PickupTransactions:
    """Discover, route to, and verify dropped-item pickup as one Body process."""

    def __init__(
        self,
        body: Body,
        navigator: PickupNavigator | None,
        *,
        settle=None,
    ) -> None:
        self.body = body
        self.navigator = navigator
        self._settle = settle

    def pickup_items(
        self,
        *,
        expected_items: tuple[str, ...] = (),
        minimum_count: int = 1,
        config: PickupConfig | None = None,
    ) -> ToolResult:
        cfg = config or PickupConfig()
        invalid = _validate_config(cfg, minimum_count=minimum_count)
        if invalid is not None:
            return invalid
        before = read_inventory_counts(self.body)
        if isinstance(before, ToolResult):
            return before
        outcome = self._collect_inventory_delta(
            before=before,
            expected=expected_items,
            minimum_count=minimum_count,
            fallback_positions=(),
            config=cfg,
        )
        failed = outcome.get("failed")
        if isinstance(failed, ToolResult):
            return failed
        collected_total = int(outcome["collected_total"])
        success = collected_total >= minimum_count
        return ToolResult(
            success=success,
            reason="pickup_collected" if success else str(outcome.get("reason") or "pickup_candidate_domain_exhausted"),
            can_retry=not success,
            next_suggestion=None if success else "rescan after dropped items move or choose another nearby item domain",
            metrics={
                "expected_items": list(_normalize_items(expected_items)),
                "minimum_count": minimum_count,
                "before": before,
                "after": outcome["after"],
                "deltas": outcome["deltas"],
                "collected_total": collected_total,
                "pickup_process": outcome["assist"],
            },
        )

    def _collect_inventory_delta(
        self,
        *,
        before: dict[str, int],
        expected: tuple[str, ...] | list[str],
        minimum_count: int,
        fallback_positions: tuple[Position, ...],
        config: PickupConfig,
    ) -> dict[str, object]:
        expected_items = _normalize_items(expected)
        assist: dict[str, object] = {
            "waited": False,
            "moved": False,
            "scan_rounds": 0,
            "candidate_attempts": 0,
            "candidate_blacklist": [],
            "plans": [],
        }
        started = time.monotonic()

        after, deltas, collected_total = self._poll_inventory_delta(
            before,
            expected_items,
            config.poll_timeout_s,
            assist,
        )
        if isinstance(after, ToolResult):
            return {"failed": after, "assist": assist}
        if collected_total >= minimum_count:
            return _pickup_outcome(after, deltas, collected_total, assist, "pickup_collected")
        if self.navigator is None:
            return _pickup_outcome(after, deltas, collected_total, assist, "pickup_navigation_unavailable")

        blacklist: set[str] = set()
        # A dropped entity can drift between perception and arrival.  Keep the
        # no-delta blacklist tied to the position that was actually attempted;
        # the same entity at a new position is a new reach candidate.
        blacklist_positions: dict[str, Position] = {}
        fallback = list(
            _PickupCandidate(key=f"fallback:{pos[0]}:{pos[1]}:{pos[2]}", pos=pos, entity_id=None)
            for pos in _unique_positions(fallback_positions)
        )
        # Keep neighborhoods derived from an observed entity separate from
        # the caller's origin fallbacks. Once an entity was actually found,
        # its local stand domain is stronger evidence than returning to the
        # original block position after a failed route.
        dynamic_fallback: list[_PickupCandidate] = []
        for scan_round in range(config.max_scan_rounds):
            if time.monotonic() - started >= config.max_wall_s:
                return _pickup_outcome(after, deltas, collected_total, assist, "pickup_budget_exhausted")
            assist["scan_rounds"] = scan_round + 1
            scanned, scan_metrics = self._scan_candidates(config, expected_items)
            assist.setdefault("scans", []).append(scan_metrics)
            # The entity's cell is not necessarily a legal feet cell: item
            # drops commonly settle on top of a solid block or straddle a
            # block boundary.  Keep the entity as the primary candidate on
            # the first pass; after any rejected candidate, expose the
            # bounded local stand neighborhoods in the next combined domain.
            if blacklist:
                _append_fallback_neighbors(dynamic_fallback, scanned)
            if not scanned and not fallback and scan_metrics["ok"] is not True:
                failure = ToolResult(
                    success=False,
                    reason="pickup_perception_failed",
                    can_retry=True,
                    next_suggestion="retry the dropped-item scan before selecting a pickup route",
                    metrics={"pickup_process": assist, "perception": scan_metrics},
                )
                return {"failed": failure, "assist": assist}
            if not scanned and not fallback and scan_metrics["complete"] is not True:
                return _pickup_outcome(
                    after,
                    deltas,
                    collected_total,
                    assist,
                    "pickup_candidate_domain_incomplete",
                )
            scanned_candidates = [
                candidate
                for candidate in scanned
                if candidate.key not in blacklist
                or blacklist_positions.get(candidate.key) != candidate.pos
            ]
            fallback_candidates = [
                candidate
                for candidate in fallback
                if candidate.key not in blacklist
                or blacklist_positions.get(candidate.key) != candidate.pos
            ]
            dynamic_candidates = [
                candidate
                for candidate in dynamic_fallback
                if candidate.key not in blacklist
                or blacklist_positions.get(candidate.key) != candidate.pos
            ]
            if blacklist and (scanned_candidates or dynamic_candidates):
                candidates = scanned_candidates + dynamic_candidates
            else:
                candidates = scanned_candidates if scanned_candidates else fallback_candidates
            candidates = _unique_candidates(candidates)[: config.max_goals]
            if not candidates:
                reason = "pickup_partial_candidate_exhausted" if collected_total > 0 else "pickup_candidate_domain_exhausted"
                return _pickup_outcome(after, deltas, collected_total, assist, reason)

            goals = tuple(candidate.pos for candidate in candidates)
            from minebot.body.navigation import NavigationRunConfig, pure_movement_navigation_config

            base_navigation = config.navigation_config or NavigationRunConfig()
            nav_config = pure_movement_navigation_config(
                replace(
                    base_navigation,
                    max_segments=config.max_segments,
                    max_partial_segments=config.max_segments,
                    segment_timeout_s=config.segment_timeout_s,
                )
            )
            goal = GoalComposite(tuple(GoalNear(pos, radius=0) for pos in goals))
            navigation = self.navigator.navigate_to(
                goal,
                break_context=BreakContext.TRAVEL,
                config=nav_config,
                arrival_radius=config.arrival_radius,
            )
            selected = _selected_goal(navigation, goals)
            selected_candidates = [candidate for candidate in candidates if candidate.pos == selected]
            plan = {
                "goal_set": [list(pos) for pos in goals],
                "selected_goal": list(selected),
                "selected_keys": [candidate.key for candidate in selected_candidates],
                "navigation": navigation.to_payload(),
            }
            assist["plans"].append(plan)
            assist["moved"] = True
            assist["candidate_attempts"] = int(assist["candidate_attempts"]) + 1

            if not navigation.success:
                if navigation.reason in {"preempted", "body_missing", "death", "respawned", "progress_yielded"}:
                    failure = ToolResult(
                        success=False,
                        reason=f"pickup_navigation_{navigation.reason}",
                        can_retry=True,
                        metrics={"pickup_process": assist, "navigation": navigation.to_payload()},
                    )
                    return {"failed": failure, "assist": assist}
                if not selected_candidates or int(assist["candidate_attempts"]) >= config.candidate_budget:
                    return _pickup_outcome(after, deltas, collected_total, assist, f"pickup_navigation_{navigation.reason}")
                blacklist.update(candidate.key for candidate in selected_candidates)
                blacklist_positions.update(
                    {candidate.key: candidate.pos for candidate in selected_candidates}
                )
                _append_fallback_neighbors(dynamic_fallback, selected_candidates)
                assist["candidate_blacklist"] = sorted(blacklist)
                continue

            after, deltas, collected_total = self._poll_inventory_delta(
                before,
                expected_items,
                config.poll_timeout_s,
                assist,
            )
            if isinstance(after, ToolResult):
                return {"failed": after, "assist": assist}
            if collected_total >= minimum_count:
                return _pickup_outcome(after, deltas, collected_total, assist, "pickup_collected")
            blacklist.update(candidate.key for candidate in selected_candidates)
            blacklist_positions.update(
                {candidate.key: candidate.pos for candidate in selected_candidates}
            )
            _append_fallback_neighbors(dynamic_fallback, selected_candidates)
            assist["candidate_blacklist"] = sorted(blacklist)
            if int(assist["candidate_attempts"]) >= config.candidate_budget:
                return _pickup_outcome(after, deltas, collected_total, assist, "pickup_budget_exhausted")

        reason = "pickup_partial_candidate_exhausted" if collected_total > 0 else "pickup_candidate_domain_exhausted"
        return _pickup_outcome(after, deltas, collected_total, assist, reason)

    def _scan_candidates(
        self,
        config: PickupConfig,
        expected_items: tuple[str, ...] = (),
    ) -> tuple[tuple[_PickupCandidate, ...], dict[str, object]]:
        nearby = self.body.perceive(
            "nearbyEntities",
            {"radius": config.radius, "limit": config.entity_limit, "types": ["item"]},
        )
        metrics: dict[str, object] = {
            "ok": nearby.ok,
            "complete": nearby.complete,
            "error": nearby.error,
            "uncertainty": nearby.uncertainty,
        }
        if not nearby.ok:
            return (), metrics
        candidates: list[_PickupCandidate] = []
        filtered_count = 0
        for entity in nearby.data.get("entities") or []:
            if str(entity.get("type") or "") not in {"item", "minecraft:item"}:
                continue
            if expected_items and not _entity_matches_expected(entity, expected_items):
                filtered_count += 1
                continue
            raw = entity.get("pos") or []
            if len(raw) != 3:
                continue
            # Item entities can drift across a block boundary after the drop
            # event.  Route to the nearest cell so a coordinate such as
            # z=-0.029 does not select the obstructed cell below it.
            pos = _nearest_block_cell(raw)
            entity_id = str(entity.get("id") or "") or None
            key = f"entity:{entity_id}" if entity_id is not None else f"pos:{pos[0]}:{pos[1]}:{pos[2]}"
            candidates.append(_PickupCandidate(key=key, pos=pos, entity_id=entity_id))
        metrics["candidate_count"] = len(candidates)
        metrics["filtered_count"] = filtered_count
        metrics["candidates"] = [
            {"key": candidate.key, "entity_id": candidate.entity_id, "pos": list(candidate.pos)}
            for candidate in candidates
        ]
        return tuple(candidates), metrics

    def _poll_inventory_delta(
        self,
        before: dict[str, int],
        expected: tuple[str, ...],
        window_s: float,
        assist: dict[str, object],
    ) -> tuple[dict[str, int] | ToolResult, dict[str, int] | None, int]:
        after, deltas, total = _read_delta(self.body, before, expected)
        if isinstance(after, ToolResult) or total > 0 or window_s <= 0:
            return after, deltas, total
        assist["waited"] = True
        deadline = time.monotonic() + window_s
        while time.monotonic() < deadline:
            self._pause(min(0.10, max(0.0, deadline - time.monotonic())))
            after, deltas, total = _read_delta(self.body, before, expected)
            if isinstance(after, ToolResult) or total > 0:
                return after, deltas, total
        return after, deltas, total

    def _pause(self, seconds: float) -> None:
        if seconds <= 0:
            return
        if self._settle is not None:
            self._settle(seconds)
            return
        time.sleep(seconds)


def _read_delta(
    body: Body,
    before: dict[str, int],
    expected: tuple[str, ...],
) -> tuple[dict[str, int] | ToolResult, dict[str, int] | None, int]:
    after = read_inventory_counts(body)
    if isinstance(after, ToolResult):
        return after, None, 0
    items = expected or tuple(sorted(set(before) | set(after)))
    deltas = {item: after.get(item, 0) - before.get(item, 0) for item in items}
    return after, deltas, sum(max(0, value) for value in deltas.values())


def _normalize_items(items) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(item).removeprefix("minecraft:") for item in items if str(item)))


def _entity_matches_expected(entity: dict[str, object], expected_items: tuple[str, ...]) -> bool:
    """Keep unknown item names, but skip known entities from other item domains."""

    raw_name = str(entity.get("name") or "").strip().lower()
    if not raw_name:
        return True
    name = raw_name.removeprefix("minecraft:").replace("_", " ")
    aliases = tuple(item.replace("_", " ").lower() for item in expected_items)
    return any(name == alias or alias in name for alias in aliases)


def _nearest_block_cell(raw) -> Position:
    """Map an entity's floating-point position to its nearest block cell.

    ``round`` is deliberately avoided because its tie handling is
    implementation-defined for this use (and uses bankers rounding in
    Python).  Flooring after adding half a block gives deterministic nearest
    cell semantics for both positive and negative coordinates.
    """

    return tuple(floor(float(value) + 0.5) for value in raw)  # type: ignore[return-value]


def _selected_goal(result: ToolResult, goals: tuple[Position, ...]) -> Position:
    raw = (result.metrics or {}).get("selected_goal", (result.metrics or {}).get("goal"))
    if isinstance(raw, (list, tuple)) and len(raw) >= 3:
        selected = (int(raw[0]), int(raw[1]), int(raw[2]))
        if selected in goals:
            return selected
    return goals[0]


def _unique_positions(positions: tuple[Position, ...]) -> tuple[Position, ...]:
    return tuple(dict.fromkeys((int(pos[0]), int(pos[1]), int(pos[2])) for pos in positions))


def _pickup_neighbor_positions(pos: Position) -> tuple[Position, ...]:
    """Return bounded alternative feet cells around a dropped-item cell."""

    x, y, z = pos
    return (
        (x, y + 1, z),
        (x - 1, y, z - 1),
        (x + 1, y, z - 1),
        (x - 1, y, z + 1),
        (x + 1, y, z + 1),
        (x - 1, y, z),
        (x + 1, y, z),
        (x, y, z - 1),
        (x, y, z + 1),
        (x, y - 1, z),
    )


def _append_fallback_neighbors(
    fallback: list[_PickupCandidate],
    candidates: tuple[_PickupCandidate, ...] | list[_PickupCandidate],
) -> None:
    for candidate in candidates:
        for pos in _pickup_neighbor_positions(candidate.pos):
            key = f"fallback:{pos[0]}:{pos[1]}:{pos[2]}"
            if all(existing.key != key for existing in fallback):
                fallback.append(_PickupCandidate(key=key, pos=pos, entity_id=None))


def _unique_candidates(candidates: list[_PickupCandidate]) -> list[_PickupCandidate]:
    unique: list[_PickupCandidate] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate.key in seen:
            continue
        seen.add(candidate.key)
        unique.append(candidate)
    return unique


def _pickup_outcome(
    after: dict[str, int],
    deltas: dict[str, int] | None,
    collected_total: int,
    assist: dict[str, object],
    reason: str,
) -> dict[str, object]:
    return {
        "after": after,
        "deltas": deltas or {},
        "collected_total": collected_total,
        "assist": assist,
        "reason": reason,
    }


def _validate_config(config: PickupConfig, *, minimum_count: int) -> ToolResult | None:
    if minimum_count <= 0:
        return ToolResult(False, "invalid_pickup_count", False)
    if config.radius < 1 or config.radius > 32:
        return ToolResult(False, "invalid_pickup_radius", False)
    if config.entity_limit < 1 or config.entity_limit > 128:
        return ToolResult(False, "invalid_pickup_entity_limit", False)
    if config.max_goals < 1 or config.max_goals > 32:
        return ToolResult(False, "invalid_pickup_goal_limit", False)
    if config.max_scan_rounds < 1 or config.candidate_budget < 1 or config.max_segments < 1:
        return ToolResult(False, "invalid_pickup_budget", False)
    if config.max_wall_s <= 0 or config.poll_timeout_s < 0 or config.segment_timeout_s <= 0:
        return ToolResult(False, "invalid_pickup_timeout", False)
    if config.arrival_radius <= 0:
        return ToolResult(False, "invalid_pickup_arrival_radius", False)
    return None


__all__ = ["PickupConfig", "PickupTransactions"]
