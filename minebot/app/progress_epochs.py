"""Durable progress-epoch summaries over full-fidelity tool observations."""

from __future__ import annotations

from typing import Protocol

from minebot.app.runtime_state import RuntimeScope, RuntimeStateStore


class ProgressEpochArchive(Protocol):
    def store(self, record: dict[str, object]) -> dict[str, object]: ...

    def list_after(self, cursor: int, *, limit: int = 100) -> list[dict[str, object]]: ...

    def latest_cursor(self) -> int: ...


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


__all__ = ["PersistentProgressEpochArchive", "ProgressEpochArchive"]
