"""Bounded Body-owned frontier exploration over authoritative world facts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from math import dist, floor, hypot
from typing import Callable, Protocol

from minebot.body.navigation import (
    NavigationRunConfig,
    NavigationTransactions,
    dry_land_navigation_config,
    governed_mobility_navigation_config,
    navigation_governed_mobility_upgrade_allowed,
    navigation_zero_progress,
    pure_movement_navigation_config,
)
from minebot.body.world_read import read_block_facts
from minebot.contract import (
    Body,
    BreakContext,
    JsonObject,
    PerceptionResult,
    Position,
    ToolResult,
    perception_next_cursor,
    perception_resume_params,
)
from minebot.game.navigation import GoalComposite, GoalNear


REGION_SIZE = 16
DEFAULT_SCAN_RADIUS = 12
DEFAULT_VERTICAL_RADIUS = 20
MAX_REQUESTED_TARGETS = 32
MAX_EXPANDED_TARGETS = 64
FRONTIER_FAILURE_LIMIT = 2
MAX_GOVERNED_MOBILITY_FALLBACKS = 4
# Sample the region interior, not just its center cross.  Golden-world frontier
# failures repeatedly occur on uneven water/lava/vertical boundaries where a
# region center can be unsafe while an interior corner/edge column is a valid
# Body stand.  This remains a provider-local generic stand-domain expansion:
# no seed, coordinate, target, or terrain-specific route knowledge is encoded.
FRONTIER_COLUMN_OFFSETS = (
    (0, 0),
    (-4, 0),
    (4, 0),
    (0, -4),
    (0, 4),
    (-6, -6),
    (-6, 6),
    (6, -6),
    (6, 6),
)
FIND_BLOCK_PAGE_SIZE = 32
FIND_BLOCK_MAX_PAGES = 4

BLOCK_TARGET_GROUPS: dict[str, tuple[str, ...]] = {
    "#logs": (
        "oak_log",
        "spruce_log",
        "birch_log",
        "jungle_log",
        "acacia_log",
        "dark_oak_log",
        "mangrove_log",
        "cherry_log",
        "pale_oak_log",
        "crimson_stem",
        "warped_stem",
    ),
    "#flowers": (
        "dandelion",
        "poppy",
        "blue_orchid",
        "allium",
        "azure_bluet",
        "red_tulip",
        "orange_tulip",
        "white_tulip",
        "pink_tulip",
        "oxeye_daisy",
        "cornflower",
        "lily_of_the_valley",
        "wither_rose",
        "sunflower",
        "lilac",
        "rose_bush",
        "peony",
        "torchflower",
        "pitcher_plant",
        "open_eyeblossom",
        "closed_eyeblossom",
    ),
}

ENTITY_TARGET_GROUPS: dict[str, tuple[str, ...]] = {
    "#farm_animals": ("pig", "cow", "sheep", "chicken"),
    "#passive_animals": (
        "pig",
        "cow",
        "sheep",
        "chicken",
        "rabbit",
        "horse",
        "donkey",
        "llama",
        "goat",
    ),
}


class CoverageStatus(StrEnum):
    COVERED = "covered"
    FOUND = "found"
    MOBILITY_BLOCKED = "mobility_blocked"
    UNLOADED_BOUNDARY = "unloaded_boundary"


@dataclass(frozen=True)
class ExplorationTargets:
    requested_blocks: tuple[str, ...]
    requested_entities: tuple[str, ...]
    blocks: tuple[str, ...]
    entities: tuple[str, ...]
    query_signature: str

    @classmethod
    def create(
        cls,
        *,
        blocks: tuple[str, ...],
        entities: tuple[str, ...],
    ) -> "ExplorationTargets":
        requested_blocks = _normalize_requested_targets(blocks)
        requested_entities = _normalize_requested_targets(entities)
        if not requested_blocks and not requested_entities:
            raise ValueError("exploration requires at least one block or entity target")
        if len(requested_blocks) + len(requested_entities) > MAX_REQUESTED_TARGETS:
            raise ValueError(f"exploration accepts at most {MAX_REQUESTED_TARGETS} requested targets")
        expanded_blocks = _expand_targets(requested_blocks, BLOCK_TARGET_GROUPS, kind="block")
        expanded_entities = _expand_targets(requested_entities, ENTITY_TARGET_GROUPS, kind="entity")
        if len(expanded_blocks) + len(expanded_entities) > MAX_EXPANDED_TARGETS:
            raise ValueError(f"exploration expands to at most {MAX_EXPANDED_TARGETS} targets")
        signature_payload = {
            "blocks": sorted(expanded_blocks),
            "entities": sorted(expanded_entities),
        }
        query_signature = hashlib.sha256(
            json.dumps(
                signature_payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return cls(
            requested_blocks=requested_blocks,
            requested_entities=requested_entities,
            blocks=expanded_blocks,
            entities=expanded_entities,
            query_signature=query_signature,
        )

    def payload(self) -> JsonObject:
        return {
            "requested": {
                "blocks": list(self.requested_blocks),
                "entities": list(self.requested_entities),
            },
            "expanded": {
                "blocks": list(self.blocks),
                "entities": list(self.entities),
            },
            "query_signature": self.query_signature,
        }


@dataclass(frozen=True)
class CoverageRegion:
    dimension: str
    query_signature: str
    region_x: int
    region_z: int
    status: CoverageStatus
    center: Position
    visit_count: int
    failure_count: int
    revision: int
    reason: str
    observations: tuple[JsonObject, ...] = ()
    negative_evidence: tuple[str, ...] = ()
    uncertainty: tuple[JsonObject, ...] = ()

    @property
    def key(self) -> tuple[int, int]:
        return (self.region_x, self.region_z)

    @property
    def settled(self) -> bool:
        return self.status in {CoverageStatus.COVERED, CoverageStatus.FOUND}


class ExplorationCoverageStore(Protocol):
    def list_regions(self, dimension: str, query_signature: str) -> tuple[CoverageRegion, ...]: ...

    def record_region(
        self,
        *,
        dimension: str,
        query_signature: str,
        region: tuple[int, int],
        status: CoverageStatus,
        center: Position,
        reason: str,
        observations: tuple[JsonObject, ...] = (),
        negative_evidence: tuple[str, ...] = (),
        uncertainty: tuple[JsonObject, ...] = (),
    ) -> CoverageRegion: ...


class MemoryExplorationCoverageStore:
    """In-process coverage store for tests and non-persistent runtimes."""

    def __init__(self) -> None:
        self._events: list[dict[str, object]] = []

    def list_regions(self, dimension: str, query_signature: str) -> tuple[CoverageRegion, ...]:
        matching = [
            event
            for event in self._events
            if event["dimension"] == dimension and event["query_signature"] == query_signature
        ]
        return fold_coverage_events(matching)

    def record_region(
        self,
        *,
        dimension: str,
        query_signature: str,
        region: tuple[int, int],
        status: CoverageStatus,
        center: Position,
        reason: str,
        observations: tuple[JsonObject, ...] = (),
        negative_evidence: tuple[str, ...] = (),
        uncertainty: tuple[JsonObject, ...] = (),
    ) -> CoverageRegion:
        self._events.append(
            {
                "cursor": len(self._events) + 1,
                "dimension": dimension,
                "query_signature": query_signature,
                "region_x": region[0],
                "region_z": region[1],
                "status": status.value,
                "center": list(center),
                "reason": reason,
                "observations": [dict(item) for item in observations],
                "negative_evidence": list(negative_evidence),
                "uncertainty": [dict(item) for item in uncertainty],
            }
        )
        return {item.key: item for item in self.list_regions(dimension, query_signature)}[region]


@dataclass(frozen=True)
class _ScanResult:
    blocks: tuple[JsonObject, ...]
    entities: tuple[JsonObject, ...]
    uncertainty: tuple[JsonObject, ...]
    terminal_reason: str | None = None

    @property
    def found(self) -> bool:
        return bool(self.blocks or self.entities)


class ExplorationTransactions:
    """Execute a model-selected target search through deterministic safe frontiers."""

    def __init__(
        self,
        body: Body,
        navigator: NavigationTransactions,
        coverage: ExplorationCoverageStore,
        *,
        mobility_egress: Callable[[float], ToolResult] | None = None,
    ) -> None:
        self.body = body
        self.navigator = navigator
        self.coverage = coverage
        self.mobility_egress = mobility_egress

    def explore_for(
        self,
        *,
        block_targets: tuple[str, ...] = (),
        entity_targets: tuple[str, ...] = (),
        max_distance: int = 192,
        max_regions: int = 12,
        return_policy: str = "first_match",
        scan_radius: int = DEFAULT_SCAN_RADIUS,
        resume_cursor: JsonObject | None = None,
    ) -> ToolResult:
        try:
            targets = ExplorationTargets.create(blocks=block_targets, entities=entity_targets)
        except ValueError as exc:
            return ToolResult(False, "exploration_target_invalid", False, metrics={"error": str(exc)})
        if max_distance < REGION_SIZE or max_distance > 512:
            return ToolResult(False, "exploration_budget_invalid", False, metrics={"max_distance": max_distance})
        if max_regions < 1 or max_regions > 32:
            return ToolResult(False, "exploration_budget_invalid", False, metrics={"max_regions": max_regions})
        if scan_radius < 4 or scan_radius > 32:
            return ToolResult(False, "exploration_budget_invalid", False, metrics={"scan_radius": scan_radius})
        if return_policy not in {"first_match", "region_budget"}:
            return ToolResult(False, "exploration_return_policy_invalid", False, metrics={"return_policy": return_policy})

        state = self.body.get_state()
        lifecycle = _body_lifecycle_terminal(state)
        if lifecycle is not None:
            return lifecycle
        dimension = str(state.dimension or "unknown")
        origin = _block_pos(state.pos)
        prior = {item.key: item for item in self.coverage.list_regions(dimension, targets.query_signature)}
        cursor_failure = _validate_resume_cursor(
            resume_cursor,
            dimension=dimension,
            query_signature=targets.query_signature,
            latest_revision=max((item.revision for item in prior.values()), default=0),
        )
        if cursor_failure is not None:
            return cursor_failure

        settled_before = {key for key, item in prior.items() if item.settled}
        current_region = _region_key(origin)
        covered = set(settled_before)
        covered.add(current_region)
        regions_consumed = 0
        distance_consumed = 0.0
        candidate_attempts = 0
        max_candidate_attempts = max(4, max_regions * 4)
        attempted_this_call: set[tuple[int, int]] = set()
        failures: list[JsonObject] = []
        mutation_blacklist: set[Position] = set()
        governed_mobility_fallback_regions: set[tuple[int, int]] = set()
        governed_mobility_fallbacks_used = 0
        mobility_egress_attempted = False
        evidence_keys: list[str] = []
        covered_this_call: list[list[int]] = []
        all_blocks: list[JsonObject] = []
        all_entities: list[JsonObject] = []

        current_scan = self._scan_targets(
            targets,
            scan_radius=scan_radius,
            stop_on_match=return_policy == "first_match",
        )
        terminal = self._scan_terminal(current_scan, dimension, targets, current_region, origin, prior)
        if terminal is not None:
            return _merge_result_context(
                terminal,
                targets=targets,
                dimension=dimension,
                origin=origin,
                max_regions=max_regions,
                max_distance=max_distance,
                regions_consumed=regions_consumed,
                distance_consumed=distance_consumed,
                covered_this_call=covered_this_call,
                blocks=[*all_blocks, *current_scan.blocks],
                entities=[*all_entities, *current_scan.entities],
                failures=failures,
                evidence_keys=[*evidence_keys, *_target_evidence_keys(dimension, current_scan)],
                coverage=prior,
            )
        current_record, current_keys = self._record_scan(
            dimension=dimension,
            targets=targets,
            region=current_region,
            center=origin,
            scan=current_scan,
        )
        prior[current_region] = current_record
        evidence_keys.extend(current_keys)
        all_blocks.extend(current_scan.blocks)
        all_entities.extend(current_scan.entities)
        if current_region not in settled_before:
            regions_consumed += 1
            covered_this_call.append([current_region[0], current_region[1]])
        if current_scan.found and return_policy == "first_match":
            return self._result(
                "found",
                targets=targets,
                dimension=dimension,
                origin=origin,
                regions_consumed=regions_consumed,
                max_regions=max_regions,
                max_distance=max_distance,
                distance_consumed=distance_consumed,
                covered_this_call=covered_this_call,
                blocks=all_blocks,
                entities=all_entities,
                failures=failures,
                evidence_keys=evidence_keys,
                coverage=prior,
                success=True,
                can_retry=False,
            )

        while regions_consumed < max_regions and candidate_attempts < max_candidate_attempts:
            if distance_consumed >= max_distance:
                return self._result(
                    "budget_exhausted",
                    targets=targets,
                    dimension=dimension,
                    origin=origin,
                    regions_consumed=regions_consumed,
                    max_regions=max_regions,
                    max_distance=max_distance,
                    distance_consumed=distance_consumed,
                    covered_this_call=covered_this_call,
                    blocks=all_blocks,
                    entities=all_entities,
                    failures=failures,
                    evidence_keys=evidence_keys,
                    coverage=prior,
                    success=True,
                    can_retry=True,
                )
            state = self.body.get_state()
            lifecycle = _body_lifecycle_terminal(state)
            if lifecycle is not None:
                return _merge_result_context(
                    lifecycle,
                    targets=targets,
                    dimension=dimension,
                    origin=origin,
                    max_regions=max_regions,
                    max_distance=max_distance,
                    regions_consumed=regions_consumed,
                    distance_consumed=distance_consumed,
                    covered_this_call=covered_this_call,
                    blocks=all_blocks,
                    entities=all_entities,
                    failures=failures,
                    evidence_keys=evidence_keys,
                    coverage=prior,
                )
            current = _block_pos(state.pos)
            candidates = _frontier_regions(
                origin=origin,
                current=current,
                max_distance=max_distance,
                covered=covered,
                coverage=prior,
                excluded=attempted_this_call,
            )
            if not candidates:
                reason = "found" if all_blocks or all_entities else (
                    "mobility_blocked" if failures else "frontier_exhausted"
                )
                return self._result(
                    reason,
                    targets=targets,
                    dimension=dimension,
                    origin=origin,
                    regions_consumed=regions_consumed,
                    max_regions=max_regions,
                    max_distance=max_distance,
                    distance_consumed=distance_consumed,
                    covered_this_call=covered_this_call,
                    blocks=all_blocks,
                    entities=all_entities,
                    failures=failures,
                    evidence_keys=evidence_keys,
                    coverage=prior,
                    success=reason in {"found", "frontier_exhausted"},
                    can_retry=reason == "mobility_blocked",
                )

            region = candidates[0]
            attempted_this_call.add(region)
            candidate_attempts += 1
            stands, stand_error = self._safe_frontier_stands(region, current_y=current[1])
            if stand_error is not None or not stands:
                mobility_egress: JsonObject | None = None
                if (
                    stand_error is None
                    and not stands
                    and not mobility_egress_attempted
                    and self.mobility_egress is not None
                ):
                    egress_before = self.body.get_state()
                    mobility_egress_attempted = True
                    egress_result = self.mobility_egress(30.0)
                    egress_after = self.body.get_state()
                    distance_consumed += dist(egress_before.pos, egress_after.pos)
                    mobility_egress = _terminal_result_summary(egress_result)
                    if egress_result.success:
                        # The region was uninspectable from the old position,
                        # not permanently unreachable. Re-evaluate it from the
                        # authoritative post-egress position.
                        failures.append(
                            {
                                "region": [region[0], region[1]],
                                "reason": "frontier_stands_unavailable",
                                "candidate_stands": 0,
                                "mobility_egress": mobility_egress,
                            }
                        )
                        attempted_this_call.discard(region)
                        continue
                status = (
                    CoverageStatus.UNLOADED_BOUNDARY
                    if stand_error is not None
                    else CoverageStatus.MOBILITY_BLOCKED
                )
                reason = status.value if stand_error is None else f"unloaded_boundary:{stand_error}"
                record = self.coverage.record_region(
                    dimension=dimension,
                    query_signature=targets.query_signature,
                    region=region,
                    status=status,
                    center=_region_center(region, current[1]),
                    reason=reason,
                    uncertainty=({"reason": stand_error},) if stand_error else (),
                )
                prior[region] = record
                failure: JsonObject = {
                    "region": [region[0], region[1]],
                    "reason": reason,
                    "candidate_stands": len(stands),
                }
                if mobility_egress is not None:
                    failure["mobility_egress"] = mobility_egress
                failures.append(failure)
                continue

            navigation_failures: list[JsonObject] = []
            navigation_uncertainty: tuple[JsonObject, ...] = ()
            fallback_failure_recorded = False
            fallback_attempted = False
            candidate_stands = stands[:3]
            before = self.body.get_state()
            nav = self.navigator.navigate_to(
                GoalComposite(tuple(GoalNear(stand, radius=1) for stand in candidate_stands)),
                break_context=BreakContext.TRAVEL,
                config=dry_land_navigation_config(
                    pure_movement_navigation_config(
                        NavigationRunConfig(max_segments=16, segment_timeout_s=12.0)
                    )
                ),
                mutation_blacklist=mutation_blacklist,
            )
            after = self.body.get_state()
            distance_consumed += dist(before.pos, after.pos)
            navigation_reason = str(nav.reason or "mobility_blocked")
            navigation_failures.append(
                {
                    "stands": [list(stand) for stand in candidate_stands],
                    "selected_goal": (nav.metrics or {}).get("selected_goal"),
                    "reason": navigation_reason,
                    "success": nav.success,
                    "can_retry": nav.can_retry,
                    "mutation_blacklist_size": len(mutation_blacklist),
                }
            )
            lifecycle = _body_lifecycle_terminal(after)
            if lifecycle is not None:
                return _merge_result_context(
                    lifecycle,
                    targets=targets,
                    dimension=dimension,
                    origin=origin,
                    max_regions=max_regions,
                    max_distance=max_distance,
                    regions_consumed=regions_consumed,
                    distance_consumed=distance_consumed,
                    covered_this_call=covered_this_call,
                    blocks=all_blocks,
                    entities=all_entities,
                    failures=[*failures, *navigation_failures],
                    evidence_keys=evidence_keys,
                    coverage=prior,
                )
            if navigation_reason in {"preempted", "owner_preempted"}:
                return self._result(
                    "preempted",
                    targets=targets,
                    dimension=dimension,
                    origin=origin,
                    regions_consumed=regions_consumed,
                    max_regions=max_regions,
                    max_distance=max_distance,
                    distance_consumed=distance_consumed,
                    covered_this_call=covered_this_call,
                    blocks=all_blocks,
                    entities=all_entities,
                    failures=[*failures, *navigation_failures],
                    evidence_keys=evidence_keys,
                    coverage=prior,
                    success=True,
                    can_retry=True,
            )
            reached = nav.success and _region_key(_block_pos(after.pos)) == region
            dry_after = after.pos
            trigger_zero_progress = navigation_zero_progress(nav, before.pos, dry_after)
            if (
                not reached
                and navigation_governed_mobility_upgrade_allowed(nav, before.pos, after.pos)
                and region not in governed_mobility_fallback_regions
                and governed_mobility_fallbacks_used < min(MAX_GOVERNED_MOBILITY_FALLBACKS, max_regions)
            ):
                fallback_attempted = True
                governed_mobility_fallback_regions.add(region)
                governed_mobility_fallbacks_used += 1
                if not mobility_egress_attempted and self.mobility_egress is not None:
                    egress_before = self.body.get_state()
                    egress_result = self.mobility_egress(30.0)
                    egress_after = self.body.get_state()
                    mobility_egress_attempted = True
                    distance_consumed += dist(egress_before.pos, egress_after.pos)
                    navigation_failures[0]["mobility_egress"] = _terminal_result_summary(egress_result)
                    egress_lifecycle = _body_lifecycle_terminal(egress_after)
                    if egress_lifecycle is not None:
                        return _merge_result_context(
                            egress_lifecycle,
                            targets=targets,
                            dimension=dimension,
                            origin=origin,
                            max_regions=max_regions,
                            max_distance=max_distance,
                            regions_consumed=regions_consumed,
                            distance_consumed=distance_consumed,
                            covered_this_call=covered_this_call,
                            blocks=all_blocks,
                            entities=all_entities,
                            failures=[*failures, *navigation_failures],
                            evidence_keys=evidence_keys,
                            coverage=prior,
                        )
                    after = egress_after

                aquatic_before = self.body.get_state()
                aquatic = self.navigator.navigate_to(
                    GoalComposite(tuple(GoalNear(stand, radius=1) for stand in candidate_stands)),
                    break_context=BreakContext.TRAVEL,
                    config=governed_mobility_navigation_config(
                        NavigationRunConfig(max_segments=16, segment_timeout_s=12.0)
                    ),
                    mutation_blacklist=mutation_blacklist,
                )
                aquatic_after = self.body.get_state()
                distance_consumed += dist(aquatic_before.pos, aquatic_after.pos)
                navigation_failures.append(
                    {
                        "mode": "governed_mobility",
                        "stands": [list(stand) for stand in candidate_stands],
                        "selected_goal": (aquatic.metrics or {}).get("selected_goal"),
                        "reason": aquatic.reason,
                        "success": aquatic.success,
                        "can_retry": aquatic.can_retry,
                        "trigger": navigation_reason,
                        "trigger_zero_progress": trigger_zero_progress,
                        "trigger_partial_progress": not trigger_zero_progress,
                        "fallback_budget": {
                            "used": governed_mobility_fallbacks_used,
                            "limit": min(MAX_GOVERNED_MOBILITY_FALLBACKS, max_regions),
                        },
                        "mutation_blacklist_size": len(mutation_blacklist),
                    }
                )
                aquatic_lifecycle = _body_lifecycle_terminal(aquatic_after)
                if aquatic_lifecycle is not None:
                    return _merge_result_context(
                        aquatic_lifecycle,
                        targets=targets,
                        dimension=dimension,
                        origin=origin,
                        max_regions=max_regions,
                        max_distance=max_distance,
                        regions_consumed=regions_consumed,
                        distance_consumed=distance_consumed,
                        covered_this_call=covered_this_call,
                        blocks=all_blocks,
                        entities=all_entities,
                        failures=[*failures, *navigation_failures],
                        evidence_keys=evidence_keys,
                        coverage=prior,
                    )
                if aquatic.reason in {"preempted", "owner_preempted"}:
                    return self._result(
                        "preempted",
                        targets=targets,
                        dimension=dimension,
                        origin=origin,
                        regions_consumed=regions_consumed,
                        max_regions=max_regions,
                        max_distance=max_distance,
                        distance_consumed=distance_consumed,
                        covered_this_call=covered_this_call,
                        blocks=all_blocks,
                        entities=all_entities,
                        failures=[*failures, *navigation_failures],
                        evidence_keys=evidence_keys,
                        coverage=prior,
                        success=True,
                        can_retry=True,
                    )
                # The fallback is the latest authoritative attempt even when it
                # ends in a typed failure.  Keep its final position so recovery
                # cannot erase useful partial progress from the widened profile.
                nav = aquatic
                after = aquatic_after
                navigation_reason = str(aquatic.reason or "mobility_blocked")
                reached = aquatic.success and _region_key(_block_pos(after.pos)) == region
                if reached and fallback_attempted:
                    navigation_uncertainty = tuple(navigation_failures)
                    failures.append(
                        {
                            "region": [region[0], region[1]],
                            "reason": navigation_reason,
                            "navigation_attempts": navigation_failures,
                        }
                    )
                    fallback_failure_recorded = True
            if not reached:
                recovery: JsonObject | None = None
                recovered = after
                verified_goal = GoalNear(_block_pos(before.pos), radius=1)
                # Only retreat to the pre-attempt position when the attempt made
                # no useful forward progress. A governed-mobility fallback that
                # successfully changed the stand domain and left the body
                # meaningfully closer to the target region must not be dragged
                # back: pure-movement recovery might not re-cross that choke.
                # Drift from a failed fallback is not a verified exploration
                # anchor and still retreats.
                region_center = _region_center(region, _block_pos(before.pos)[1])
                before_gap = hypot(
                    region_center[0] - before.pos[0], region_center[2] - before.pos[2]
                )
                after_gap = hypot(
                    region_center[0] - after.pos[0], region_center[2] - after.pos[2]
                )
                governed_advance = (
                    fallback_attempted
                    and nav.success
                    and before_gap - after_gap > 0.25
                )
                if (
                    navigation_reason != "progress_yielded"
                    and not governed_advance
                    and not verified_goal.is_satisfied(_block_pos(after.pos))
                ):
                    recovery_result = self.navigator.navigate_to(
                        verified_goal,
                        break_context=BreakContext.TRAVEL,
                        config=pure_movement_navigation_config(
                            NavigationRunConfig(
                                max_segments=8,
                                segment_timeout_s=12.0,
                                recovery_attempts=0,
                            )
                        ),
                    )
                    recovered = self.body.get_state()
                    distance_consumed += dist(after.pos, recovered.pos)
                    recovery = {
                        "attempted": True,
                        "target": list(_block_pos(before.pos)),
                        "success": recovery_result.success,
                        "reason": recovery_result.reason,
                        "final_pos": list(recovered.pos),
                    }
                    navigation_failures[0]["recovery"] = recovery
                    recovery_lifecycle = _body_lifecycle_terminal(recovered)
                    if recovery_lifecycle is not None:
                        return _merge_result_context(
                            recovery_lifecycle,
                            targets=targets,
                            dimension=dimension,
                            origin=origin,
                            max_regions=max_regions,
                            max_distance=max_distance,
                            regions_consumed=regions_consumed,
                            distance_consumed=distance_consumed,
                            covered_this_call=covered_this_call,
                            blocks=all_blocks,
                            entities=all_entities,
                            failures=[*failures, *navigation_failures],
                            evidence_keys=evidence_keys,
                            coverage=prior,
                        )
                status = (
                    CoverageStatus.UNLOADED_BOUNDARY
                    if _is_unloaded_reason(navigation_reason)
                    else CoverageStatus.MOBILITY_BLOCKED
                )
                record = self.coverage.record_region(
                    dimension=dimension,
                    query_signature=targets.query_signature,
                    region=region,
                    status=status,
                    center=stands[0],
                    reason=navigation_reason,
                    uncertainty=tuple(navigation_failures),
                )
                prior[region] = record
                if not fallback_failure_recorded:
                    failures.append(
                        {
                            "region": [region[0], region[1]],
                            "reason": navigation_reason,
                            "navigation_attempts": navigation_failures,
                        }
                    )
                if recovery is not None and recovery["success"] is not True:
                    # A failed return-to-last-verified-position must not erase
                    # target facts already collected earlier in this bounded
                    # call.  The caller can act on those exact coordinates;
                    # only a call with no target facts remains mobility-blocked.
                    found = bool(all_blocks or all_entities)
                    return self._result(
                        "found" if found else "mobility_blocked",
                        targets=targets,
                        dimension=dimension,
                        origin=origin,
                        regions_consumed=regions_consumed,
                        max_regions=max_regions,
                        max_distance=max_distance,
                        distance_consumed=distance_consumed,
                        covered_this_call=covered_this_call,
                        blocks=all_blocks,
                        entities=all_entities,
                        failures=failures,
                        evidence_keys=evidence_keys,
                        coverage=prior,
                        success=found,
                        can_retry=not found,
                    )
                if navigation_reason == "progress_yielded":
                    return self._result(
                        "budget_exhausted",
                        targets=targets,
                        dimension=dimension,
                        origin=origin,
                        regions_consumed=regions_consumed,
                        max_regions=max_regions,
                        max_distance=max_distance,
                        distance_consumed=distance_consumed,
                        covered_this_call=covered_this_call,
                        blocks=all_blocks,
                        entities=all_entities,
                        failures=failures,
                        evidence_keys=evidence_keys,
                        coverage=prior,
                        success=True,
                        can_retry=True,
                    )
                if distance_consumed >= max_distance:
                    return self._result(
                        "budget_exhausted",
                        targets=targets,
                        dimension=dimension,
                        origin=origin,
                        regions_consumed=regions_consumed,
                        max_regions=max_regions,
                        max_distance=max_distance,
                        distance_consumed=distance_consumed,
                        covered_this_call=covered_this_call,
                        blocks=all_blocks,
                        entities=all_entities,
                        failures=failures,
                        evidence_keys=evidence_keys,
                        coverage=prior,
                        success=True,
                        can_retry=True,
                    )
                continue

            final_state = self.body.get_state()
            final_pos = _block_pos(final_state.pos)
            reached_region = _region_key(final_pos)
            scan = self._scan_targets(
                targets,
                scan_radius=scan_radius,
                stop_on_match=return_policy == "first_match",
            )
            terminal = self._scan_terminal(scan, dimension, targets, reached_region, final_pos, prior)
            if terminal is not None:
                return _merge_result_context(
                    terminal,
                    targets=targets,
                    dimension=dimension,
                    origin=origin,
                    max_regions=max_regions,
                    max_distance=max_distance,
                    regions_consumed=regions_consumed,
                    distance_consumed=distance_consumed,
                    covered_this_call=covered_this_call,
                    blocks=[*all_blocks, *scan.blocks],
                    entities=[*all_entities, *scan.entities],
                    failures=failures,
                    evidence_keys=[*evidence_keys, *_target_evidence_keys(dimension, scan)],
                    coverage=prior,
                )
            record, scan_keys = self._record_scan(
                dimension=dimension,
                targets=targets,
                region=reached_region,
                center=final_pos,
                scan=scan,
                additional_uncertainty=navigation_uncertainty,
            )
            prior[reached_region] = record
            covered.add(reached_region)
            evidence_keys.extend(key for key in scan_keys if key not in evidence_keys)
            all_blocks.extend(item for item in scan.blocks if item not in all_blocks)
            all_entities.extend(item for item in scan.entities if item not in all_entities)
            if reached_region not in settled_before and [reached_region[0], reached_region[1]] not in covered_this_call:
                regions_consumed += 1
                covered_this_call.append([reached_region[0], reached_region[1]])
            if scan.found and return_policy == "first_match":
                return self._result(
                    "found",
                    targets=targets,
                    dimension=dimension,
                    origin=origin,
                    regions_consumed=regions_consumed,
                    max_regions=max_regions,
                    max_distance=max_distance,
                    distance_consumed=distance_consumed,
                    covered_this_call=covered_this_call,
                    blocks=all_blocks,
                    entities=all_entities,
                    failures=failures,
                    evidence_keys=evidence_keys,
                    coverage=prior,
                    success=True,
                    can_retry=False,
                )
            if distance_consumed >= max_distance:
                return self._result(
                    "budget_exhausted",
                    targets=targets,
                    dimension=dimension,
                    origin=origin,
                    regions_consumed=regions_consumed,
                    max_regions=max_regions,
                    max_distance=max_distance,
                    distance_consumed=distance_consumed,
                    covered_this_call=covered_this_call,
                    blocks=all_blocks,
                    entities=all_entities,
                    failures=failures,
                    evidence_keys=evidence_keys,
                    coverage=prior,
                    success=True,
                    can_retry=True,
                )

        if all_blocks or all_entities:
            reason = "found"
        elif regions_consumed >= max_regions:
            reason = "budget_exhausted"
        elif failures and all(_is_unloaded_reason(str(item.get("reason") or "")) for item in failures):
            reason = "unloaded_boundary"
        else:
            reason = "mobility_blocked"
        return self._result(
            reason,
            targets=targets,
            dimension=dimension,
            origin=origin,
            regions_consumed=regions_consumed,
            max_regions=max_regions,
            max_distance=max_distance,
            distance_consumed=distance_consumed,
            covered_this_call=covered_this_call,
            blocks=all_blocks,
            entities=all_entities,
            failures=failures,
            evidence_keys=evidence_keys,
            coverage=prior,
            success=reason in {"found", "budget_exhausted"},
            can_retry=reason in {"budget_exhausted", "mobility_blocked", "unloaded_boundary"},
        )

    def _scan_targets(
        self,
        targets: ExplorationTargets,
        *,
        scan_radius: int,
        stop_on_match: bool,
    ) -> _ScanResult:
        blocks: list[JsonObject] = []
        entities: list[JsonObject] = []
        uncertainty: list[JsonObject] = []
        block_page_limited = False
        if targets.blocks:
            cursor: object = 0
            seen_cursors: set[str] = set()
            for page_index in range(FIND_BLOCK_MAX_PAGES):
                perception = self.body.perceive(
                    "findBlocks",
                    {
                        "types": list(targets.blocks),
                        "radius": scan_radius,
                        "y_radius": min(DEFAULT_VERTICAL_RADIUS, scan_radius * 2),
                        "limit": FIND_BLOCK_PAGE_SIZE,
                        **perception_resume_params(cursor),
                    },
                )
                terminal = _perception_terminal(perception, allow_partial=True)
                if terminal is not None:
                    return _ScanResult(
                        tuple(blocks),
                        (),
                        tuple([*uncertainty, *_uncertainty(perception)]),
                        terminal,
                    )
                for item in perception.data.get("blocks") or []:
                    if not isinstance(item, dict):
                        continue
                    block_type = _normalize_identifier(item.get("type"))
                    if block_type in targets.blocks:
                        blocks.append(_normalized_block_match(item))
                uncertainty.extend(_uncertainty(perception))
                if blocks and stop_on_match:
                    return _ScanResult(tuple(blocks), (), tuple(uncertainty))
                if perception.complete:
                    uncertainty = [
                        item
                        for item in uncertainty
                        if str(item.get("reason") or "").casefold() != "page_limit"
                    ]
                    break
                next_cursor = perception_next_cursor(perception, "nextStart", "next")
                if next_cursor is None:
                    return _ScanResult(
                        tuple(blocks),
                        (),
                        tuple(uncertainty),
                        "scan_cursor_missing",
                    )
                cursor_key = str(next_cursor)
                if cursor_key in seen_cursors or cursor_key == str(cursor):
                    return _ScanResult(
                        tuple(blocks),
                        (),
                        tuple(uncertainty),
                        "scan_cursor_invalid",
                    )
                if page_index + 1 >= FIND_BLOCK_MAX_PAGES:
                    uncertainty.append(
                        {
                            "reason": "scan_page_limit",
                            "pages": FIND_BLOCK_MAX_PAGES,
                            "next_start": next_cursor,
                        }
                    )
                    # A dense block domain must not starve a second target
                    # domain (for example, nearby entities) or frontier
                    # traversal.  Keep the uncertainty and partial matches,
                    # then let the caller decide whether the positive facts
                    # are sufficient to continue.  Single-domain block
                    # searches retain the historical typed terminal below.
                    block_page_limited = True
                    break
                seen_cursors.add(str(cursor))
                cursor = next_cursor
        if targets.entities:
            perception = self.body.perceive(
                "nearbyEntities",
                {
                    "radius": min(32, max(scan_radius, 16)),
                    "limit": 128,
                    "types": list(targets.entities),
                },
            )
            terminal = _perception_terminal(perception, allow_partial=True)
            if terminal is not None:
                return _ScanResult(tuple(blocks), (), tuple([*uncertainty, *_uncertainty(perception)]), terminal)
            for item in perception.data.get("entities") or []:
                if not isinstance(item, dict):
                    continue
                entity_type = _normalize_identifier(item.get("type"))
                if entity_type in targets.entities:
                    entities.append(_normalized_entity_match(item))
            uncertainty.extend(_uncertainty(perception))
        terminal_reason = None
        if block_page_limited and (not targets.entities or not (blocks or entities)):
            terminal_reason = "scan_page_limit"
        return _ScanResult(tuple(blocks), tuple(entities), tuple(uncertainty), terminal_reason)

    def _scan_terminal(
        self,
        scan: _ScanResult,
        dimension: str,
        targets: ExplorationTargets,
        region: tuple[int, int],
        center: Position,
        coverage: dict[tuple[int, int], CoverageRegion],
    ) -> ToolResult | None:
        if scan.terminal_reason is None:
            return None
        reason = scan.terminal_reason
        if reason == "unloaded_boundary":
            record = self.coverage.record_region(
                dimension=dimension,
                query_signature=targets.query_signature,
                region=region,
                status=CoverageStatus.UNLOADED_BOUNDARY,
                center=center,
                reason=scan.terminal_reason,
                uncertainty=scan.uncertainty,
            )
            coverage[region] = record
        return ToolResult(
            False,
            reason,
            reason not in {"death", "body_missing"},
            metrics={
                "targets": targets.payload(),
                "dimension": dimension,
                "region": [region[0], region[1]],
                "final_pos": list(center),
                "uncertainty": list(scan.uncertainty),
                "source_reason": scan.terminal_reason,
            },
        )

    def _record_scan(
        self,
        *,
        dimension: str,
        targets: ExplorationTargets,
        region: tuple[int, int],
        center: Position,
        scan: _ScanResult,
        additional_uncertainty: tuple[JsonObject, ...] = (),
    ) -> tuple[CoverageRegion, tuple[str, ...]]:
        observations = tuple([*scan.blocks, *scan.entities])
        negative: list[str] = []
        if targets.blocks and not scan.blocks:
            negative.append("no_matching_blocks")
        if targets.entities and not scan.entities:
            negative.append("no_matching_entities")
        status = CoverageStatus.FOUND if scan.found else CoverageStatus.COVERED
        record = self.coverage.record_region(
            dimension=dimension,
            query_signature=targets.query_signature,
            region=region,
            status=status,
            center=center,
            reason="target_found" if scan.found else "authoritative_negative_scan",
            observations=observations,
            negative_evidence=tuple(negative),
            uncertainty=tuple([*scan.uncertainty, *additional_uncertainty]),
        )
        keys = [_coverage_evidence_key(record)]
        keys.extend(_target_evidence_keys(dimension, scan))
        return record, tuple(dict.fromkeys(keys))

    def _safe_frontier_stands(
        self,
        region: tuple[int, int],
        *,
        current_y: int,
    ) -> tuple[tuple[Position, ...], str | None]:
        center = _region_center(region, current_y)
        columns = tuple((center[0] + dx, center[2] + dz) for dx, dz in FRONTIER_COLUMN_OFFSETS)
        y_values = tuple(range(current_y - 12, current_y + 13))
        positions: list[Position] = []
        for x, z in columns:
            for y in y_values:
                positions.extend(((x, y - 1, z), (x, y, z), (x, y + 1, z)))
        try:
            facts = read_block_facts(
                self.body,
                tuple(dict.fromkeys(positions)),
                page_size=64,
                failure_label="exploration_frontier",
            )
        except ValueError as exc:
            return (), str(exc)
        stands: list[Position] = []
        for x, z in columns:
            candidates = sorted(y_values, key=lambda y: (abs(y - current_y), -y))
            for y in candidates:
                support = facts.get((x, y - 1, z))
                feet = facts.get((x, y, z))
                head = facts.get((x, y + 1, z))
                if support is None or feet is None or head is None:
                    continue
                if _safe_stand(support, feet, head):
                    stands.append((x, y, z))
                    break
        stands.sort(
            key=lambda pos: (
                abs(pos[1] - current_y),
                hypot(pos[0] - center[0], pos[2] - center[2]),
                pos[0],
                pos[2],
            )
        )
        return tuple(stands), None

    def _result(
        self,
        reason: str,
        *,
        targets: ExplorationTargets,
        dimension: str,
        origin: Position,
        regions_consumed: int,
        max_regions: int,
        max_distance: int,
        distance_consumed: float,
        covered_this_call: list[list[int]],
        blocks: list[JsonObject],
        entities: list[JsonObject],
        failures: list[JsonObject],
        evidence_keys: list[str],
        coverage: dict[tuple[int, int], CoverageRegion],
        success: bool,
        can_retry: bool,
    ) -> ToolResult:
        final_state = self.body.get_state()
        latest_revision = max((item.revision for item in coverage.values()), default=0)
        uncertainty: list[JsonObject] = []
        for record in coverage.values():
            for item in record.uncertainty:
                if item not in uncertainty:
                    uncertainty.append(dict(item))
        resumable = reason in {"budget_exhausted", "mobility_blocked", "unloaded_boundary", "preempted"}
        resume_cursor = (
            {
                "query_signature": targets.query_signature,
                "dimension": dimension,
                "coverage_revision": latest_revision,
            }
            if resumable
            else None
        )
        continuation = _exploration_continuation(targets, resume_cursor)
        return ToolResult(
            success,
            reason,
            can_retry,
            metrics={
                "targets": targets.payload(),
                "dimension": dimension,
                "origin": list(origin),
                "final_pos": list(final_state.pos),
                "budget": {
                    "max_distance": max_distance,
                    "max_regions": max_regions,
                    "regions_consumed": regions_consumed,
                    "distance_consumed": round(distance_consumed, 3),
                },
                "covered_regions": covered_this_call,
                "coverage_revision": latest_revision,
                "blocks": blocks,
                "entities": entities,
                "candidate_failures": failures,
                "evidence_keys": list(dict.fromkeys(evidence_keys)),
                "uncertainty": uncertainty,
                "resume_cursor": resume_cursor,
                "continuation": continuation,
                "complete": reason in {"found", "frontier_exhausted"},
            },
        )


def fold_coverage_events(events: list[dict[str, object]]) -> tuple[CoverageRegion, ...]:
    grouped: dict[tuple[int, int], list[dict[str, object]]] = {}
    for event in events:
        key = (int(event["region_x"]), int(event["region_z"]))
        grouped.setdefault(key, []).append(event)
    regions: list[CoverageRegion] = []
    for key, region_events in grouped.items():
        latest = max(region_events, key=lambda item: int(item["cursor"]))
        statuses = [CoverageStatus(str(item["status"])) for item in region_events]
        observations = latest.get("observations") or []
        negative = latest.get("negative_evidence") or []
        uncertainty = latest.get("uncertainty") or []
        center = latest.get("center") or [0, 0, 0]
        regions.append(
            CoverageRegion(
                dimension=str(latest["dimension"]),
                query_signature=str(latest["query_signature"]),
                region_x=key[0],
                region_z=key[1],
                status=CoverageStatus(str(latest["status"])),
                center=(int(center[0]), int(center[1]), int(center[2])),
                visit_count=sum(status in {CoverageStatus.COVERED, CoverageStatus.FOUND} for status in statuses),
                failure_count=sum(status in {CoverageStatus.MOBILITY_BLOCKED, CoverageStatus.UNLOADED_BOUNDARY} for status in statuses),
                revision=int(latest["cursor"]),
                reason=str(latest.get("reason") or ""),
                observations=tuple(dict(item) for item in observations if isinstance(item, dict)),
                negative_evidence=tuple(str(item) for item in negative),
                uncertainty=tuple(dict(item) for item in uncertainty if isinstance(item, dict)),
            )
        )
    return tuple(sorted(regions, key=lambda item: (item.region_x, item.region_z)))


def _frontier_regions(
    *,
    origin: Position,
    current: Position,
    max_distance: int,
    covered: set[tuple[int, int]],
    coverage: dict[tuple[int, int], CoverageRegion],
    excluded: set[tuple[int, int]],
) -> list[tuple[int, int]]:
    frontiers: set[tuple[int, int]] = set()
    for region_x, region_z in covered:
        for dx in (-1, 0, 1):
            for dz in (-1, 0, 1):
                if dx == 0 and dz == 0:
                    continue
                candidate = (region_x + dx, region_z + dz)
                if candidate in covered or candidate in excluded:
                    continue
                prior = coverage.get(candidate)
                if prior is not None and prior.failure_count >= FRONTIER_FAILURE_LIMIT:
                    continue
                center = _region_center(candidate, current[1])
                if hypot(center[0] - origin[0], center[2] - origin[2]) > max_distance:
                    continue
                frontiers.add(candidate)

    origin_region = _region_key(origin)

    def score(region: tuple[int, int]) -> tuple[float, float, float, float, int, int]:
        center = _region_center(region, current[1])
        gain = sum(
            (region[0] + dx, region[1] + dz) not in covered
            for dx in (-1, 0, 1)
            for dz in (-1, 0, 1)
            if dx != 0 or dz != 0
        )
        failures = 0 if coverage.get(region) is None else coverage[region].failure_count
        direction = _frontier_direction(region, origin_region)
        direction_load = sum(
            _frontier_direction(item, origin_region) == direction for item in covered
        )
        return (
            float(failures),
            float(direction_load),
            -float(gain),
            hypot(center[0] - current[0], center[2] - current[2]),
            region[0],
            region[1],
        )

    return sorted(frontiers, key=score)


def _normalize_requested_targets(values: tuple[str, ...]) -> tuple[str, ...]:
    normalized = tuple(dict.fromkeys(_normalize_identifier(value) for value in values if str(value).strip()))
    if any(len(value) > 128 for value in normalized):
        raise ValueError("exploration target identifiers must be at most 128 characters")
    return normalized


def _expand_targets(
    requested: tuple[str, ...],
    groups: dict[str, tuple[str, ...]],
    *,
    kind: str,
) -> tuple[str, ...]:
    expanded: list[str] = []
    for target in requested:
        if target.startswith("#") and target not in groups:
            raise ValueError(f"unknown {kind} target group: {target}")
        values = groups.get(target, (target,))
        for value in values:
            normalized = _normalize_identifier(value)
            if normalized not in expanded:
                expanded.append(normalized)
    return tuple(expanded)


def _normalize_identifier(value: object) -> str:
    return str(value or "").strip().casefold().removeprefix("minecraft:")


def _block_pos(pos: tuple[float, float, float]) -> Position:
    return (floor(pos[0]), floor(pos[1]), floor(pos[2]))


def _region_key(pos: Position) -> tuple[int, int]:
    return (pos[0] // REGION_SIZE, pos[2] // REGION_SIZE)


def _region_center(region: tuple[int, int], y: int) -> Position:
    return (region[0] * REGION_SIZE + REGION_SIZE // 2, y, region[1] * REGION_SIZE + REGION_SIZE // 2)


def _frontier_direction(
    region: tuple[int, int], origin_region: tuple[int, int]
) -> tuple[int, int]:
    return (
        (region[0] > origin_region[0]) - (region[0] < origin_region[0]),
        (region[1] > origin_region[1]) - (region[1] < origin_region[1]),
    )


def _perception_terminal(perception: PerceptionResult, *, allow_partial: bool = False) -> str | None:
    if perception.ok and (perception.complete or allow_partial):
        return None
    error = str(perception.error or "").casefold()
    if "missing" in error or "body" in error and "missing" in error:
        return "body_missing"
    if _is_unloaded_reason(error):
        return "unloaded_boundary"
    return error or "perception_incomplete"


def _uncertainty(perception: PerceptionResult) -> list[JsonObject]:
    return [dict(item) for item in (perception.uncertainty or ()) if isinstance(item, dict)]


def _normalized_block_match(item: dict[str, object]) -> JsonObject:
    return {
        "kind": "block",
        "type": _normalize_identifier(item.get("type")),
        "pos": [int(item.get("x") or 0), int(item.get("y") or 0), int(item.get("z") or 0)],
        "state": str(item.get("state") or ""),
        "dist2": float(item.get("dist2") or 0.0),
    }


def _normalized_entity_match(item: dict[str, object]) -> JsonObject:
    pos = item.get("pos") if isinstance(item.get("pos"), list) else []
    return {
        "kind": "entity",
        "id": str(item.get("id") or ""),
        "type": _normalize_identifier(item.get("type")),
        "name": item.get("name"),
        "pos": [float(value) for value in pos[:3]],
        "health": item.get("health"),
        "dist2": float(item.get("dist2") or 0.0),
    }


def _body_lifecycle_terminal(state: object) -> ToolResult | None:
    if bool(getattr(state, "missing", False)):
        return ToolResult(False, "body_missing", True, metrics={"final_pos": list(getattr(state, "pos", ()))})
    if float(getattr(state, "health", 0.0) or 0.0) <= 0.0:
        return ToolResult(False, "death", True, metrics={"final_pos": list(getattr(state, "pos", ()))})
    return None


def _terminal_result_summary(result: ToolResult) -> JsonObject:
    """Keep durable coverage evidence at terminal-truth granularity.

    Full tool results belong in the observation archive. Exploration coverage only
    needs enough evidence to explain whether its one internal egress attempt
    changed mobility, so it must not embed a potentially large surface-domain
    or navigation trace as a single uncertainty item.
    """

    metrics = result.metrics or {}
    summary: JsonObject = {
        "success": result.success,
        "reason": result.reason,
        "can_retry": result.can_retry,
    }
    for key in (
        "origin",
        "surface_origin",
        "target_surface",
        "selected_goal",
        "final_pos",
    ):
        value = metrics.get(key)
        if isinstance(value, (list, tuple)) and len(value) == 3:
            summary[key] = list(value)
    if "terminal_surface_verified" in metrics:
        summary["terminal_surface_verified"] = bool(metrics["terminal_surface_verified"])
    surface_domain = metrics.get("surface_domain")
    if isinstance(surface_domain, dict):
        domain_summary: JsonObject = {}
        for key in (
            "selection",
            "selection_strategy",
            "candidate_count",
            "raw_candidate_count",
            "complete",
            "exhaustion_reason",
            "selected_vertical_effort",
            "column_count",
        ):
            if key in surface_domain:
                domain_summary[key] = surface_domain[key]
        if domain_summary:
            summary["surface_domain"] = domain_summary
    return summary


def _safe_stand(
    support: PerceptionResult,
    feet: PerceptionResult,
    head: PerceptionResult,
) -> bool:
    support_type = _normalize_identifier(support.data.get("type"))
    feet_type = _normalize_identifier(feet.data.get("type"))
    head_type = _normalize_identifier(head.data.get("type"))
    if support_type in {"air", "cave_air", "void_air", "water", "lava", "magma_block", "cactus"}:
        return False
    if str(support.data.get("state") or "").upper() != "SOLID":
        return False
    for perception, block_type in ((feet, feet_type), (head, head_type)):
        if block_type in {"water", "lava", "fire", "soul_fire", "powder_snow"}:
            return False
        state = str(perception.data.get("state") or "").upper()
        if state != "CLEAR" and block_type not in {"air", "cave_air", "void_air"}:
            return False
    return True


def _coverage_evidence_key(record: CoverageRegion) -> str:
    suffix = "positive" if record.status is CoverageStatus.FOUND else "negative"
    return (
        f"exploration:{record.dimension}:{record.query_signature}:"
        f"region:{record.region_x},{record.region_z}:{suffix}"
    )


def _target_evidence_keys(dimension: str, scan: _ScanResult) -> list[str]:
    keys: list[str] = []
    for block in scan.blocks:
        pos = block.get("pos") or []
        keys.append(
            f"block:{dimension}:{block.get('type')}:{','.join(str(value) for value in pos)}"
        )
    for entity in scan.entities:
        identity = str(entity.get("id") or "")
        if not identity:
            pos = entity.get("pos") or []
            identity = f"{entity.get('type')}@{','.join(str(round(float(value), 1)) for value in pos)}"
        keys.append(f"entity:{dimension}:{identity}")
    return keys


def _exploration_continuation(
    targets: ExplorationTargets,
    resume_cursor: JsonObject | None,
) -> JsonObject | None:
    if resume_cursor is None:
        return None
    return {
        "kind": "resume_operation",
        "tool": "explore_for",
        "target_descriptor": {
            "block_targets": list(targets.requested_blocks),
            "entity_targets": list(targets.requested_entities),
        },
        "resume_cursor": dict(resume_cursor),
        "target_descriptor_must_match": True,
    }


def _validate_resume_cursor(
    cursor: JsonObject | None,
    *,
    dimension: str,
    query_signature: str,
    latest_revision: int,
) -> ToolResult | None:
    if cursor is None:
        return None
    if (
        str(cursor.get("query_signature") or "") != query_signature
        or str(cursor.get("dimension") or "") != dimension
    ):
        return ToolResult(
            False,
            "exploration_resume_cursor_mismatch",
            False,
            metrics={"cursor": dict(cursor), "query_signature": query_signature, "dimension": dimension},
        )
    try:
        revision = int(cursor.get("coverage_revision") or 0)
    except (TypeError, ValueError):
        revision = -1
    if revision < 0 or revision > latest_revision:
        return ToolResult(
            False,
            "exploration_resume_cursor_invalid",
            False,
            metrics={"cursor": dict(cursor), "latest_coverage_revision": latest_revision},
        )
    return None


def _is_unloaded_reason(reason: str) -> bool:
    normalized = str(reason or "").casefold()
    return "unloaded" in normalized or "chunk" in normalized or "boundary" in normalized


def _merge_result_context(
    result: ToolResult,
    *,
    targets: ExplorationTargets,
    dimension: str,
    origin: Position,
    max_regions: int,
    max_distance: int,
    regions_consumed: int,
    distance_consumed: float,
    covered_this_call: list[list[int]],
    blocks: list[JsonObject],
    entities: list[JsonObject],
    failures: list[JsonObject],
    evidence_keys: list[str],
    coverage: dict[tuple[int, int], CoverageRegion],
) -> ToolResult:
    metrics = dict(result.metrics or {})
    latest_revision = max((item.revision for item in coverage.values()), default=0)
    resumable = result.reason in {"mobility_blocked", "unloaded_boundary", "preempted"}
    resume_cursor = (
        {
            "query_signature": targets.query_signature,
            "dimension": dimension,
            "coverage_revision": latest_revision,
        }
        if resumable
        else None
    )
    metrics.update(
        {
            "targets": targets.payload(),
            "dimension": dimension,
            "origin": list(origin),
            "budget": {
                "max_distance": max_distance,
                "max_regions": max_regions,
                "regions_consumed": regions_consumed,
                "distance_consumed": round(distance_consumed, 3),
            },
            "covered_regions": covered_this_call,
            "coverage_revision": latest_revision,
            "blocks": blocks,
            "entities": entities,
            "candidate_failures": failures,
            "evidence_keys": list(dict.fromkeys(evidence_keys)),
            "resume_cursor": resume_cursor,
            "continuation": _exploration_continuation(targets, resume_cursor),
            "complete": False,
        }
    )
    return ToolResult(result.success, result.reason, result.can_retry, result.next_suggestion, metrics)


__all__ = [
    "BLOCK_TARGET_GROUPS",
    "CoverageRegion",
    "CoverageStatus",
    "ENTITY_TARGET_GROUPS",
    "ExplorationCoverageStore",
    "ExplorationTargets",
    "ExplorationTransactions",
    "MemoryExplorationCoverageStore",
    "REGION_SIZE",
    "fold_coverage_events",
]
