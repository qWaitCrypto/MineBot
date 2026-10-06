"""Durable progress-epoch summaries over full-fidelity tool observations."""

from __future__ import annotations

from typing import Protocol
from dataclasses import dataclass

from minebot.app.runtime_state import RuntimeScope, RuntimeStateStore
from minebot.app.progress_recovery import replay_progress_epoch
from minebot.brain.progress import ProgressAuthority
from minebot.contract import ProgressAbort, ProgressStopReason


class ProgressEpochArchive(Protocol):
    def store(self, record: dict[str, object]) -> dict[str, object]: ...

    def list_after(self, cursor: int, *, limit: int = 100) -> list[dict[str, object]]: ...

    def latest_cursor(self) -> int: ...

    def restore_authority(self, authority: ProgressAuthority, *, task_id: str) -> ProgressAbort | None: ...

    def mark_progress_aborted(
        self, epoch_id: str, *, stop_reason: ProgressStopReason = ProgressStopReason.UNKNOWN,
    ) -> None: ...


@dataclass(frozen=True)
class ContinuationEpochBudget:
    store: RuntimeStateStore
    scope: RuntimeScope
    checkpoint_id: str

    def reserve(self, epoch_id: str) -> bool:
        return self.store.reserve_continuation_epoch(
            self.scope, checkpoint_id=self.checkpoint_id, epoch_id=epoch_id,
        )


class PersistentProgressEpochArchive:
    def __init__(self, store: RuntimeStateStore, scope: RuntimeScope) -> None:
        self._state_store = store
        self.scope = scope

    def store(self, record: dict[str, object]) -> dict[str, object]:
        return self._state_store.create_progress_epoch(self.scope, record=record)

    def list_after(self, cursor: int, *, limit: int = 100) -> list[dict[str, object]]:
        return self._state_store.list_progress_epochs_after(
            self.scope,
            cursor=cursor,
            limit=limit,
        )

    def latest_cursor(self) -> int:
        return self._state_store.latest_progress_epoch_cursor(self.scope)

    def restore_authority(self, authority: ProgressAuthority, *, task_id: str) -> ProgressAbort | None:
        record = self._state_store.latest_task_progress_epoch(self.scope, task_id=task_id)
        if record is None:
            authority.restore_facts(ProgressAuthority().facts(""))
            return None
        facts, abort = replay_progress_epoch(record)
        authority.restore_facts(facts)
        return abort

    def mark_progress_aborted(
        self, epoch_id: str, *, stop_reason: ProgressStopReason = ProgressStopReason.UNKNOWN,
    ) -> None:
        self._state_store.mark_progress_epoch_aborted(self.scope, epoch_id, stop_reason=stop_reason)


__all__ = ["ContinuationEpochBudget", "PersistentProgressEpochArchive", "ProgressEpochArchive"]
