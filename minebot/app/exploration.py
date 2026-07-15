"""Persistent adapter for Body exploration coverage events."""

from __future__ import annotations

from minebot.app.runtime_state import RuntimeScope, RuntimeStateStore
from minebot.body.exploration import (
    CoverageRegion,
    CoverageStatus,
    ExplorationCoverageStore,
    fold_coverage_events,
)
from minebot.contract import JsonObject, Position


class PersistentExplorationCoverageStore(ExplorationCoverageStore):
    def __init__(self, store: RuntimeStateStore, scope: RuntimeScope) -> None:
        self.store = store
        self.scope = scope

    def list_regions(self, dimension: str, query_signature: str) -> tuple[CoverageRegion, ...]:
        events = self.store.list_exploration_coverage(
            self.scope,
            dimension=dimension,
            query_signature=query_signature,
        )
        return fold_coverage_events(events)

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
        self.store.append_exploration_coverage(
            self.scope,
            dimension=dimension,
            query_signature=query_signature,
            region_x=region[0],
            region_z=region[1],
            status=status.value,
            center=center,
            reason=reason,
            observations=observations,
            negative_evidence=negative_evidence,
            uncertainty=uncertainty,
        )
        regions = {item.key: item for item in self.list_regions(dimension, query_signature)}
        return regions[region]


__all__ = ["PersistentExplorationCoverageStore"]
