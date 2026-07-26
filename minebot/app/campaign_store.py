"""Durable-seam store for F4 campaigns and stance grants.

brain-cognitive-framework.md §7. An independent domain module by
construction: it does not extend the `runtime_state.py` monolith, which is
how H2's target shape advances without a rewrite.

The skeleton ships in-memory implementations behind protocols. A persistent
implementation lands with activation, when it is known whether campaigns
share the runtime SQLite connection or get their own file.
"""

from __future__ import annotations

from typing import Iterable, Protocol

from minebot.brain.volition import CampaignRecord, StancePolicy, validate_campaign


class CampaignStore(Protocol):
    def get(self, campaign_id: str) -> CampaignRecord | None: ...

    def put(self, record: CampaignRecord) -> CampaignRecord: ...

    def all(self) -> tuple[CampaignRecord, ...]: ...

    def delete(self, campaign_id: str) -> bool: ...


class StanceStore(Protocol):
    def get(self) -> StancePolicy: ...

    def put(self, policy: StancePolicy) -> StancePolicy: ...


class InMemoryCampaignStore:
    """Validates on write so an invalid DAG can never be persisted."""

    def __init__(self, records: Iterable[CampaignRecord] = ()) -> None:
        self._records: dict[str, CampaignRecord] = {}
        for record in records:
            self.put(record)

    def get(self, campaign_id: str) -> CampaignRecord | None:
        return self._records.get(campaign_id)

    def put(self, record: CampaignRecord) -> CampaignRecord:
        validated = validate_campaign(record)
        self._records[validated.campaign_id] = validated
        return validated

    def all(self) -> tuple[CampaignRecord, ...]:
        return tuple(self._records[key] for key in sorted(self._records))

    def delete(self, campaign_id: str) -> bool:
        return self._records.pop(campaign_id, None) is not None


class InMemoryStanceStore:
    """Holds the single active stance grant for one bot.

    One grant at a time by construction: stances are a stance, not a stack,
    and revoking is `put(StancePolicy.none())`.
    """

    def __init__(self, policy: StancePolicy | None = None) -> None:
        self._policy = policy or StancePolicy.none()

    def get(self) -> StancePolicy:
        return self._policy

    def put(self, policy: StancePolicy) -> StancePolicy:
        self._policy = policy
        return self._policy


__all__ = [
    "CampaignStore",
    "InMemoryCampaignStore",
    "InMemoryStanceStore",
    "StanceStore",
]
