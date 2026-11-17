"""Durable-seam stores for F8 task transfers and team facts.

brain-cognitive-framework.md §11. Independent domain module; the persistent
implementation lands with P4 activation, when the cross-bot storage boundary
(shared database vs per-bot databases plus an exchange) is decided.

Two rules are enforced here rather than left to callers, because both are
correctness properties rather than conveniences:

- an ``idempotency_key`` maps to exactly one transfer, so a retried offer
  can never create a second copy of the same work;
- a team-fact key has exactly one writer, so shared facts stay
  append-mediated with provenance instead of becoming shared mutable state.
"""

from __future__ import annotations

from typing import Iterable, Protocol

from minebot.brain.collective import (
    CollectiveError,
    TaskTransferRecord,
    TeamFact,
    TransferStatus,
    validate_team_fact,
    validate_transfer,
)


class TaskTransferStore(Protocol):
    def offer(self, record: TaskTransferRecord) -> TaskTransferRecord: ...

    def get(self, transfer_id: str) -> TaskTransferRecord | None: ...

    def inbox(self, bot: str) -> tuple[TaskTransferRecord, ...]: ...

    def resolve(
        self,
        transfer_id: str,
        status: TransferStatus,
        *,
        resolved_at: str = "",
    ) -> TaskTransferRecord: ...


class TeamFactStore(Protocol):
    def put(self, fact: TeamFact) -> TeamFact: ...

    def get(self, key: str) -> TeamFact | None: ...

    def all(self) -> tuple[TeamFact, ...]: ...


class InMemoryTaskTransferStore:
    def __init__(self, records: Iterable[TaskTransferRecord] = ()) -> None:
        self._records: dict[str, TaskTransferRecord] = {}
        self._by_idempotency: dict[str, str] = {}
        for record in records:
            self.offer(record)

    def offer(self, record: TaskTransferRecord) -> TaskTransferRecord:
        """Idempotent: a repeated key returns the original, never a duplicate."""
        validated = validate_transfer(record)
        existing_id = self._by_idempotency.get(validated.idempotency_key)
        if existing_id is not None:
            return self._records[existing_id]
        self._records[validated.transfer_id] = validated
        self._by_idempotency[validated.idempotency_key] = validated.transfer_id
        return validated

    def get(self, transfer_id: str) -> TaskTransferRecord | None:
        return self._records.get(transfer_id)

    def inbox(self, bot: str) -> tuple[TaskTransferRecord, ...]:
        return tuple(
            self._records[key]
            for key in sorted(self._records)
            if self._records[key].to_bot == bot
            and self._records[key].status is TransferStatus.OFFERED
        )

    def resolve(
        self,
        transfer_id: str,
        status: TransferStatus,
        *,
        resolved_at: str = "",
    ) -> TaskTransferRecord:
        record = self._records.get(transfer_id)
        if record is None:
            raise CollectiveError(f"unknown transfer: {transfer_id}")
        resolved = record.resolve(status, resolved_at=resolved_at)
        self._records[transfer_id] = resolved
        return resolved


class InMemoryTeamFactStore:
    def __init__(self, facts: Iterable[TeamFact] = ()) -> None:
        self._facts: dict[str, TeamFact] = {}
        for fact in facts:
            self.put(fact)

    def put(self, fact: TeamFact) -> TeamFact:
        validated = validate_team_fact(fact)
        existing = self._facts.get(validated.key)
        if existing is not None and existing.writer_bot != validated.writer_bot:
            raise CollectiveError(
                f"team fact {validated.key!r} is owned by {existing.writer_bot!r}; "
                f"{validated.writer_bot!r} may not overwrite it"
            )
        seq = 0 if existing is None else existing.seq + 1
        stored = TeamFact(
            key=validated.key,
            value=validated.value,
            writer_bot=validated.writer_bot,
            seq=seq,
            updated_at=validated.updated_at,
        )
        self._facts[stored.key] = stored
        return stored

    def get(self, key: str) -> TeamFact | None:
        return self._facts.get(key)

    def all(self) -> tuple[TeamFact, ...]:
        return tuple(self._facts[key] for key in sorted(self._facts))


__all__ = [
    "InMemoryTaskTransferStore",
    "InMemoryTeamFactStore",
    "TaskTransferStore",
    "TeamFactStore",
]
