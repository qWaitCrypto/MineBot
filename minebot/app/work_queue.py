"""The single work-admission queue for MineBot's outer agent runtime."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from filelock import FileLock, Timeout

from minebot.app.runtime_state import RuntimeScope, RuntimeStateConflict, RuntimeStateStore


class WorkIntentKind(str, Enum):
    START = "start"
    PAUSE = "pause"
    CONTINUE = "continue"
    CANCEL = "cancel"
    REPLACE_GOAL = "replace_goal"
    MESSAGE = "message"
    QUIT = "quit"
    BODY_EVENT = "body_event"
    RECOVERY_RECONCILE = "recovery_reconcile"
    TASK_BOUNDARY = "task_boundary"
    TASK_CONTINUE = "task_continue"
    MAINTENANCE = "maintenance"


class WorkIntentState(str, Enum):
    QUEUED = "queued"
    LEASED = "leased"
    COMPLETED = "completed"
    SUPERSEDED = "superseded"
    FAILED = "failed"


WORK_INTENT_PRIORITY: dict[WorkIntentKind, int] = {
    WorkIntentKind.QUIT: 100,
    WorkIntentKind.CANCEL: 95,
    WorkIntentKind.REPLACE_GOAL: 90,
    WorkIntentKind.PAUSE: 85,
    WorkIntentKind.RECOVERY_RECONCILE: 80,
    WorkIntentKind.BODY_EVENT: 75,
    WorkIntentKind.CONTINUE: 70,
    WorkIntentKind.START: 65,
    WorkIntentKind.MESSAGE: 60,
    WorkIntentKind.TASK_BOUNDARY: 55,
    WorkIntentKind.TASK_CONTINUE: 50,
    WorkIntentKind.MAINTENANCE: 10,
}

CONTROL_INTENT_KINDS = {
    WorkIntentKind.PAUSE,
    WorkIntentKind.CANCEL,
    WorkIntentKind.REPLACE_GOAL,
    WorkIntentKind.QUIT,
}


def superseded_kinds_for(kind: WorkIntentKind) -> set[WorkIntentKind]:
    """Return queued work invalidated by a newly submitted control intent."""
    if kind not in CONTROL_INTENT_KINDS:
        return set()
    priority = WORK_INTENT_PRIORITY[kind]
    return {
        candidate
        for candidate, candidate_priority in WORK_INTENT_PRIORITY.items()
        if candidate is not kind and candidate_priority < priority
    }


@dataclass(frozen=True)
class WorkIntent:
    intent_id: str
    revision: int
    kind: WorkIntentKind
    source: str
    priority: int
    payload: dict[str, object]
    state: WorkIntentState
    dedupe_key: str | None = None
    task_id: str | None = None
    generation: int | None = None
    attempt_count: int = 0


class WorkIntentQueue(Protocol):
    @property
    def available(self) -> threading.Event: ...

    @property
    def notification_version(self) -> int: ...

    def enqueue(
        self,
        kind: WorkIntentKind,
        *,
        source: str,
        payload: dict[str, object],
        dedupe_key: str | None = None,
        task_id: str | None = None,
        generation: int | None = None,
    ) -> WorkIntent: ...

    def lease_next(self) -> WorkIntent | None: ...

    def complete(self, intent: WorkIntent) -> WorkIntent: ...

    def fail(self, intent: WorkIntent, error: dict[str, object]) -> WorkIntent: ...

    def supersede_active(self, intent: WorkIntent, *, reason: str) -> WorkIntent: ...

    def supersede(self, kinds: set[WorkIntentKind], *, reason: str) -> int: ...

    def pending_count(self) -> int: ...

    def count_for_task(self, kind: WorkIntentKind, task_id: str) -> int: ...

    def queued_intents(self, kind: WorkIntentKind | None = None) -> list[WorkIntent]: ...

    def get_by_dedupe(self, dedupe_key: str) -> WorkIntent | None: ...

    def issue_task_continuation(
        self,
        *,
        checkpoint_id: str,
        checkpoint_revision: int,
        task_id: str,
        payload: dict[str, object],
        dedupe_key: str,
        generation: int,
    ) -> WorkIntent | None: ...

    def close(self) -> None: ...


class MemoryWorkIntentQueue:
    """Process-local queue implementing the same semantics as the SQLite queue."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._available = threading.Event()
        self._records: dict[str, WorkIntent] = {}
        self._order: list[str] = []
        self._notification_version = 0

    @property
    def available(self) -> threading.Event:
        return self._available

    @property
    def notification_version(self) -> int:
        with self._lock:
            return self._notification_version

    def enqueue(
        self,
        kind: WorkIntentKind,
        *,
        source: str,
        payload: dict[str, object],
        dedupe_key: str | None = None,
        task_id: str | None = None,
        generation: int | None = None,
    ) -> WorkIntent:
        with self._lock:
            if dedupe_key is not None:
                for record in self._records.values():
                    if record.dedupe_key == dedupe_key:
                        return record
            intent = WorkIntent(
                intent_id=f"intent-{uuid4().hex}",
                revision=1,
                kind=kind,
                source=source,
                priority=WORK_INTENT_PRIORITY[kind],
                payload=dict(payload),
                state=WorkIntentState.QUEUED,
                dedupe_key=dedupe_key,
                task_id=task_id,
                generation=generation,
            )
            self._records[intent.intent_id] = intent
            self._order.append(intent.intent_id)
            self._notification_version += 1
            self._available.set()
            return intent

    def lease_next(self) -> WorkIntent | None:
        with self._lock:
            queued = [
                self._records[intent_id]
                for intent_id in self._order
                if self._records[intent_id].state is WorkIntentState.QUEUED
            ]
            if not queued:
                self._available.clear()
                return None
            selected = min(queued, key=lambda item: (-item.priority, self._order.index(item.intent_id)))
            leased = WorkIntent(
                **{
                    **selected.__dict__,
                    "revision": selected.revision + 1,
                    "state": WorkIntentState.LEASED,
                    "attempt_count": selected.attempt_count + 1,
                }
            )
            self._records[selected.intent_id] = leased
            self._refresh_available()
            return leased

    def complete(self, intent: WorkIntent) -> WorkIntent:
        return self._finish(intent, WorkIntentState.COMPLETED, None)

    def fail(self, intent: WorkIntent, error: dict[str, object]) -> WorkIntent:
        return self._finish(intent, WorkIntentState.FAILED, error)

    def supersede_active(self, intent: WorkIntent, *, reason: str) -> WorkIntent:
        return self._finish(
            intent,
            WorkIntentState.SUPERSEDED,
            {"reason": reason},
        )

    def supersede(self, kinds: set[WorkIntentKind], *, reason: str) -> int:
        del reason
        changed = 0
        with self._lock:
            for intent_id, record in list(self._records.items()):
                if record.state is WorkIntentState.QUEUED and record.kind in kinds:
                    self._records[intent_id] = WorkIntent(
                        **{
                            **record.__dict__,
                            "revision": record.revision + 1,
                            "state": WorkIntentState.SUPERSEDED,
                        }
                    )
                    changed += 1
            if changed:
                self._notification_version += 1
            self._refresh_available()
        return changed

    def pending_count(self) -> int:
        with self._lock:
            return sum(
                record.state is WorkIntentState.QUEUED
                for record in self._records.values()
            )

    def count_for_task(self, kind: WorkIntentKind, task_id: str) -> int:
        with self._lock:
            return sum(
                record.kind is kind and record.task_id == task_id
                for record in self._records.values()
            )

    def queued_intents(self, kind: WorkIntentKind | None = None) -> list[WorkIntent]:
        with self._lock:
            return [
                self._records[intent_id]
                for intent_id in self._order
                if self._records[intent_id].state is WorkIntentState.QUEUED
                and (kind is None or self._records[intent_id].kind is kind)
            ]

    def get_by_dedupe(self, dedupe_key: str) -> WorkIntent | None:
        with self._lock:
            for intent_id in self._order:
                record = self._records[intent_id]
                if record.dedupe_key == dedupe_key:
                    return record
        return None

    def issue_task_continuation(
        self,
        *,
        checkpoint_id: str,
        checkpoint_revision: int,
        task_id: str,
        payload: dict[str, object],
        dedupe_key: str,
        generation: int,
    ) -> WorkIntent | None:
        del checkpoint_id, checkpoint_revision
        return self.enqueue(
            WorkIntentKind.TASK_CONTINUE,
            source="task_checkpoint_continue",
            payload=payload,
            dedupe_key=dedupe_key,
            task_id=task_id,
            generation=generation,
        )

    def close(self) -> None:
        return None

    def _finish(
        self,
        intent: WorkIntent,
        state: WorkIntentState,
        error: dict[str, object] | None,
    ) -> WorkIntent:
        del error
        with self._lock:
            current = self._records[intent.intent_id]
            if current.revision != intent.revision or current.state is not WorkIntentState.LEASED:
                raise RuntimeError(f"work intent revision conflict: {intent.intent_id}")
            finished = WorkIntent(
                **{
                    **current.__dict__,
                    "revision": current.revision + 1,
                    "state": state,
                }
            )
            self._records[intent.intent_id] = finished
            self._refresh_available()
            return finished

    def _refresh_available(self) -> None:
        if any(record.state is WorkIntentState.QUEUED for record in self._records.values()):
            self._available.set()
        else:
            self._available.clear()


class PersistentWorkIntentQueue:
    """SQLite-backed queue for one runtime scope and one scheduler owner."""

    def __init__(
        self,
        store: RuntimeStateStore,
        scope: RuntimeScope,
        *,
        lease_owner: str | None = None,
    ) -> None:
        self.store = store
        self.scope = scope
        self.lease_owner = lease_owner or f"scheduler-{uuid4().hex}"
        self._available = threading.Event()
        self._lock = threading.RLock()
        self._notification_version = 0
        self._scope_lock: FileLock | None = None
        try:
            self._acquire_scope_lock()
            self.orphaned_intents = tuple(
                _intent_from_row(row)
                for row in self.store.abandon_leased_work_intents(scope)
            )
            self._refresh_available()
        except Exception:
            self.close()
            raise

    @property
    def available(self) -> threading.Event:
        return self._available

    @property
    def notification_version(self) -> int:
        with self._lock:
            return self._notification_version

    def enqueue(
        self,
        kind: WorkIntentKind,
        *,
        source: str,
        payload: dict[str, object],
        dedupe_key: str | None = None,
        task_id: str | None = None,
        generation: int | None = None,
    ) -> WorkIntent:
        row = self.store.enqueue_work_intent(
            self.scope,
            kind=kind.value,
            source=source,
            priority=WORK_INTENT_PRIORITY[kind],
            payload=payload,
            dedupe_key=dedupe_key,
            task_id=task_id,
            generation=generation,
        )
        intent = _intent_from_row(row)
        if intent.state is WorkIntentState.QUEUED:
            with self._lock:
                self._notification_version += 1
            self._available.set()
        return intent

    def lease_next(self) -> WorkIntent | None:
        row = self.store.lease_next_work_intent(
            self.scope,
            lease_owner=self.lease_owner,
        )
        self._refresh_available()
        return None if row is None else _intent_from_row(row)

    def complete(self, intent: WorkIntent) -> WorkIntent:
        row = self.store.finish_work_intent(
            intent.intent_id,
            expected_revision=intent.revision,
            state=WorkIntentState.COMPLETED.value,
        )
        self._refresh_available()
        return _intent_from_row(row)

    def fail(self, intent: WorkIntent, error: dict[str, object]) -> WorkIntent:
        row = self.store.finish_work_intent(
            intent.intent_id,
            expected_revision=intent.revision,
            state=WorkIntentState.FAILED.value,
            error=error,
        )
        self._refresh_available()
        return _intent_from_row(row)

    def supersede_active(self, intent: WorkIntent, *, reason: str) -> WorkIntent:
        row = self.store.finish_work_intent(
            intent.intent_id,
            expected_revision=intent.revision,
            state=WorkIntentState.SUPERSEDED.value,
            error={"reason": reason},
        )
        self._refresh_available()
        return _intent_from_row(row)

    def supersede(self, kinds: set[WorkIntentKind], *, reason: str) -> int:
        changed = self.store.supersede_queued_work_intents(
            self.scope,
            kinds={kind.value for kind in kinds},
            reason=reason,
        )
        if changed:
            with self._lock:
                self._notification_version += 1
        self._refresh_available()
        return changed

    def pending_count(self) -> int:
        return self.store.queued_work_intent_count(self.scope)

    def count_for_task(self, kind: WorkIntentKind, task_id: str) -> int:
        return self.store.work_intent_count(
            self.scope,
            kind=kind.value,
            task_id=task_id,
        )

    def queued_intents(self, kind: WorkIntentKind | None = None) -> list[WorkIntent]:
        return [
            _intent_from_row(row)
            for row in self.store.list_queued_work_intents(
                self.scope,
                kind=None if kind is None else kind.value,
            )
        ]

    def get_by_dedupe(self, dedupe_key: str) -> WorkIntent | None:
        row = self.store.get_work_intent_by_dedupe(self.scope, dedupe_key)
        return None if row is None else _intent_from_row(row)

    def issue_task_continuation(
        self,
        *,
        checkpoint_id: str,
        checkpoint_revision: int,
        task_id: str,
        payload: dict[str, object],
        dedupe_key: str,
        generation: int,
    ) -> WorkIntent | None:
        existing = self.store.get_work_intent_by_dedupe(self.scope, dedupe_key)
        row = self.store.issue_checkpoint_continuation(
            self.scope,
            checkpoint_id=checkpoint_id,
            checkpoint_revision=checkpoint_revision,
            task_id=task_id,
            generation=generation,
            kind=WorkIntentKind.TASK_CONTINUE.value,
            source="task_checkpoint_continue",
            priority=WORK_INTENT_PRIORITY[WorkIntentKind.TASK_CONTINUE],
            payload=payload,
            dedupe_key=dedupe_key,
        )
        if row is None:
            return None
        intent = _intent_from_row(row)
        if existing is None and intent.state is WorkIntentState.QUEUED:
            with self._lock:
                self._notification_version += 1
            self._available.set()
        return intent

    def close(self) -> None:
        scope_lock = self._scope_lock
        self._scope_lock = None
        if scope_lock is None:
            return
        scope_lock.release()

    def _acquire_scope_lock(self) -> None:
        if str(self.store.db_path) == ":memory:":
            self.orphaned_intents = ()
            return
        db_path = Path(self.store.db_path).expanduser()
        lock_path = db_path.with_name(
            f"{db_path.name}.{self.scope.key}.scheduler.lock"
        )
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        scope_lock = FileLock(lock_path)
        try:
            scope_lock.acquire(timeout=0)
        except Timeout as exc:
            raise RuntimeStateConflict(
                f"runtime scope already has an active scheduler: {self.scope.key}"
            ) from exc
        self._scope_lock = scope_lock

    def _refresh_available(self) -> None:
        if self.pending_count() > 0:
            self._available.set()
        else:
            self._available.clear()


def _intent_from_row(row: dict[str, object]) -> WorkIntent:
    return WorkIntent(
        intent_id=str(row["intent_id"]),
        revision=int(row["revision"]),
        kind=WorkIntentKind(str(row["kind"])),
        source=str(row["source"]),
        priority=int(row["priority"]),
        payload=dict(row["payload"]),
        state=WorkIntentState(str(row["state"])),
        dedupe_key=None if row.get("dedupe_key") is None else str(row["dedupe_key"]),
        task_id=None if row.get("task_id") is None else str(row["task_id"]),
        generation=None if row.get("generation") is None else int(row["generation"]),
        attempt_count=int(row.get("attempt_count") or 0),
    )


__all__ = [
    "MemoryWorkIntentQueue",
    "PersistentWorkIntentQueue",
    "CONTROL_INTENT_KINDS",
    "WORK_INTENT_PRIORITY",
    "WorkIntent",
    "WorkIntentKind",
    "WorkIntentQueue",
    "WorkIntentState",
    "superseded_kinds_for",
]
