"""Durable, scope-isolated control-plane storage for MineBot runtimes."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from uuid import uuid4


RUNTIME_SCHEMA_VERSION = 7
DEFAULT_RUNTIME_STATE_DB = Path("var/minebot/agent-state.sqlite3")
_MAX_SCOPE_COMPONENT_LENGTH = 256


class RuntimeStateError(RuntimeError):
    """Persistent runtime state is invalid or incompatible."""


class RuntimeStateConflict(RuntimeStateError):
    """A revision or single-foreground-task invariant was violated."""


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_EVENT = "waiting_event"
    PAUSED = "paused"
    YIELDED = "yielded"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class CompletionAuthority(str, Enum):
    NONE = "none"
    BODY_TRUTH = "body_truth"
    MODEL = "model"
    HUMAN = "human"


class PlanStepStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


class CheckpointDisposition(str, Enum):
    CONTINUE = "continue"
    WAIT_EVENT = "wait_event"
    YIELD = "yield"
    COMPLETE = "complete"


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    scope_key: str
    revision: int
    goal_text: str
    source: str
    requested_by: str
    status: TaskStatus
    completion_authority: CompletionAuthority
    active_plan_id: str | None
    latest_checkpoint_id: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class PlanStepRecord:
    step_id: str
    ordinal: int
    title: str
    status: PlanStepStatus
    evidence: tuple[str, ...]
    blocker: str | None
    updated_at: str


@dataclass(frozen=True)
class TaskPlanRecord:
    plan_id: str
    task_id: str
    revision: int
    summary: str
    steps: tuple[PlanStepRecord, ...]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class TaskCheckpointRecord:
    checkpoint_id: str
    task_id: str
    revision: int
    disposition: CheckpointDisposition
    summary: str
    next_step: str
    evidence: tuple[str, ...]
    wait_for: tuple[str, ...]
    body_fingerprint: dict[str, object] | None
    created_at: str


@dataclass(frozen=True)
class RuntimeScope:
    """Stable identity boundary for all durable state owned by one bot."""

    server_id: str
    world_id: str
    bot_id: str

    def __post_init__(self) -> None:
        for field_name in ("server_id", "world_id", "bot_id"):
            value = _validated_scope_component(field_name, getattr(self, field_name))
            object.__setattr__(self, field_name, value)

    @property
    def key(self) -> str:
        encoded = json.dumps(
            self.to_payload(),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @property
    def conversation_session_id(self) -> str:
        return f"minebot:{self.key}:conversation"

    def to_payload(self) -> dict[str, str]:
        return {
            "server_id": self.server_id,
            "world_id": self.world_id,
            "bot_id": self.bot_id,
        }


class RuntimeStateStore:
    """SQLite owner for MineBot state outside the SDK conversation tables."""

    def __init__(self, db_path: str | Path = DEFAULT_RUNTIME_STATE_DB) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        self._closed = False
        if str(db_path) != ":memory:":
            Path(db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(db_path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA busy_timeout=5000")
        try:
            self._initialize_schema()
        except Exception as exc:
            self._closed = True
            self._connection.close()
            if isinstance(exc, RuntimeStateError):
                raise
            if isinstance(exc, sqlite3.DatabaseError):
                raise RuntimeStateError(f"runtime state database error: {exc}") from exc
            raise

    @property
    def schema_version(self) -> int:
        with self._lock:
            self._require_open()
            row = self._connection.execute(
                "SELECT version FROM minebot_schema WHERE singleton = 1"
            ).fetchone()
        if row is None:
            raise RuntimeStateError("runtime schema version row is missing")
        return int(row["version"])

    def register_scope(self, scope: RuntimeScope) -> None:
        with self._lock, self._connection:
            self._require_open()
            self._connection.execute(
                """
                INSERT INTO runtime_scopes (
                    scope_key, server_id, world_id, bot_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(scope_key) DO UPDATE SET
                    updated_at = CURRENT_TIMESTAMP
                """,
                (scope.key, scope.server_id, scope.world_id, scope.bot_id),
            )
            row = self._connection.execute(
                """
                SELECT server_id, world_id, bot_id
                FROM runtime_scopes
                WHERE scope_key = ?
                """,
                (scope.key,),
            ).fetchone()
        if row is None or dict(row) != scope.to_payload():
            raise RuntimeStateError("runtime scope hash collision or corrupt scope row")

    def has_scope(self, scope: RuntimeScope) -> bool:
        with self._lock:
            self._require_open()
            row = self._connection.execute(
                "SELECT 1 FROM runtime_scopes WHERE scope_key = ?",
                (scope.key,),
            ).fetchone()
        return row is not None

    def create_task(
        self,
        scope: RuntimeScope,
        *,
        goal_text: str,
        source: str,
        requested_by: str = "",
        status: TaskStatus = TaskStatus.RUNNING,
    ) -> TaskRecord:
        goal = _required_text("goal_text", goal_text, max_length=4000)
        task_id = f"task-{uuid4().hex}"
        now = _utc_now()
        self.register_scope(scope)
        try:
            with self._lock, self._connection:
                self._require_open()
                self._connection.execute(
                    """
                    INSERT INTO tasks (
                        task_id, scope_key, revision, goal_text, source,
                        requested_by, status, completion_authority,
                        active_plan_id, latest_checkpoint_id, created_at, updated_at
                    ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
                    """,
                    (
                        task_id,
                        scope.key,
                        goal,
                        _required_text("source", source, max_length=128),
                        _bounded_text(requested_by, max_length=128),
                        status.value,
                        CompletionAuthority.NONE.value,
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise RuntimeStateConflict(
                f"scope {scope.key} already has foreground work"
            ) from exc
        task = self.get_task(task_id)
        assert task is not None
        return task

    def replace_foreground_task(
        self,
        scope: RuntimeScope,
        *,
        goal_text: str,
        source: str,
        requested_by: str = "",
    ) -> TaskRecord:
        goal = _required_text("goal_text", goal_text, max_length=4000)
        now = _utc_now()
        task_id = f"task-{uuid4().hex}"
        self.register_scope(scope)
        with self._lock, self._connection:
            self._require_open()
            self._connection.execute(
                """
                UPDATE tasks
                SET status = ?, completion_authority = ?, revision = revision + 1,
                    updated_at = ?
                WHERE scope_key = ? AND status IN ('running', 'waiting_event', 'paused', 'yielded')
                """,
                (
                    TaskStatus.CANCELLED.value,
                    CompletionAuthority.HUMAN.value,
                    now,
                    scope.key,
                ),
            )
            self._connection.execute(
                """
                INSERT INTO tasks (
                    task_id, scope_key, revision, goal_text, source,
                    requested_by, status, completion_authority,
                    active_plan_id, latest_checkpoint_id, created_at, updated_at
                ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
                """,
                (
                    task_id,
                    scope.key,
                    goal,
                    _required_text("source", source, max_length=128),
                    _bounded_text(requested_by, max_length=128),
                    TaskStatus.RUNNING.value,
                    CompletionAuthority.NONE.value,
                    now,
                    now,
                ),
            )
        task = self.get_task(task_id)
        assert task is not None
        return task

    def get_task(self, task_id: str) -> TaskRecord | None:
        with self._lock:
            self._require_open()
            row = self._connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return None if row is None else _task_from_row(row)

    def get_foreground_task(self, scope: RuntimeScope) -> TaskRecord | None:
        with self._lock:
            self._require_open()
            row = self._connection.execute(
                """
                SELECT * FROM tasks
                WHERE scope_key = ?
                  AND status IN ('running', 'waiting_event', 'paused', 'yielded')
                ORDER BY updated_at DESC, task_id DESC
                LIMIT 1
                """,
                (scope.key,),
            ).fetchone()
        return None if row is None else _task_from_row(row)

    def transition_task(
        self,
        task_id: str,
        *,
        expected_revision: int,
        status: TaskStatus,
        completion_authority: CompletionAuthority | None = None,
    ) -> TaskRecord:
        now = _utc_now()
        with self._lock, self._connection:
            self._require_open()
            cursor = self._connection.execute(
                """
                UPDATE tasks
                SET status = ?,
                    completion_authority = COALESCE(?, completion_authority),
                    revision = revision + 1,
                    updated_at = ?
                WHERE task_id = ? AND revision = ?
                """,
                (
                    status.value,
                    None if completion_authority is None else completion_authority.value,
                    now,
                    task_id,
                    int(expected_revision),
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeStateConflict(
                    f"task revision conflict: task_id={task_id} expected={expected_revision}"
                )
        task = self.get_task(task_id)
        assert task is not None
        return task

    def update_plan(
        self,
        task_id: str,
        *,
        expected_revision: int,
        summary: str,
        steps: list[dict[str, object]],
    ) -> TaskPlanRecord:
        normalized_steps = _normalize_plan_steps(steps)
        summary_text = _bounded_text(summary, max_length=4000)
        now = _utc_now()
        with self._lock, self._connection:
            self._require_open()
            task_row = self._connection.execute(
                "SELECT active_plan_id FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if task_row is None:
                raise RuntimeStateConflict(f"task not found: {task_id}")
            plan_id = task_row["active_plan_id"]
            if plan_id is None:
                if expected_revision != 0:
                    raise RuntimeStateConflict(
                        f"plan revision conflict: task_id={task_id} expected={expected_revision} actual=0"
                    )
                plan_id = f"plan-{uuid4().hex}"
                revision = 1
                self._connection.execute(
                    """
                    INSERT INTO task_plans (
                        plan_id, task_id, revision, summary, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (plan_id, task_id, revision, summary_text, now, now),
                )
                self._connection.execute(
                    """
                    UPDATE tasks
                    SET active_plan_id = ?, revision = revision + 1, updated_at = ?
                    WHERE task_id = ?
                    """,
                    (plan_id, now, task_id),
                )
            else:
                plan_row = self._connection.execute(
                    "SELECT revision FROM task_plans WHERE plan_id = ?",
                    (plan_id,),
                ).fetchone()
                if plan_row is None:
                    raise RuntimeStateError(
                        f"task {task_id} references missing plan {plan_id}"
                    )
                actual_revision = int(plan_row["revision"])
                if actual_revision != expected_revision:
                    raise RuntimeStateConflict(
                        f"plan revision conflict: plan_id={plan_id} "
                        f"expected={expected_revision} actual={actual_revision}"
                    )
                revision = actual_revision + 1
                self._connection.execute(
                    """
                    UPDATE task_plans
                    SET revision = ?, summary = ?, updated_at = ?
                    WHERE plan_id = ?
                    """,
                    (revision, summary_text, now, plan_id),
                )
                self._connection.execute(
                    "DELETE FROM task_plan_steps WHERE plan_id = ?",
                    (plan_id,),
                )
            self._connection.executemany(
                """
                INSERT INTO task_plan_steps (
                    step_id, plan_id, ordinal, title, status,
                    evidence_json, blocker, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        f"step-{uuid4().hex}",
                        plan_id,
                        index,
                        step["title"],
                        step["status"],
                        _json_dump(step["evidence"]),
                        step["blocker"],
                        now,
                    )
                    for index, step in enumerate(normalized_steps)
                ],
            )
        plan = self.get_plan(task_id)
        assert plan is not None
        return plan

    def get_plan(self, task_id: str) -> TaskPlanRecord | None:
        with self._lock:
            self._require_open()
            plan_row = self._connection.execute(
                "SELECT * FROM task_plans WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if plan_row is None:
                return None
            step_rows = self._connection.execute(
                """
                SELECT * FROM task_plan_steps
                WHERE plan_id = ?
                ORDER BY ordinal ASC
                """,
                (plan_row["plan_id"],),
            ).fetchall()
        return _plan_from_rows(plan_row, step_rows)

    def create_checkpoint(
        self,
        task_id: str,
        *,
        expected_task_revision: int,
        disposition: CheckpointDisposition,
        summary: str,
        next_step: str = "",
        evidence: list[str] | tuple[str, ...] = (),
        wait_for: list[str] | tuple[str, ...] = (),
        body_fingerprint: dict[str, object] | None = None,
    ) -> tuple[TaskRecord, TaskCheckpointRecord]:
        checkpoint_id = f"checkpoint-{uuid4().hex}"
        now = _utc_now()
        status = {
            CheckpointDisposition.CONTINUE: TaskStatus.RUNNING,
            CheckpointDisposition.WAIT_EVENT: TaskStatus.WAITING_EVENT,
            CheckpointDisposition.YIELD: TaskStatus.YIELDED,
            CheckpointDisposition.COMPLETE: TaskStatus.WAITING_EVENT,
        }[disposition]
        evidence_items = _bounded_text_list(evidence, max_items=32, max_length=1000)
        wait_items = _bounded_text_list(wait_for, max_items=16, max_length=500)
        with self._lock, self._connection:
            self._require_open()
            cursor = self._connection.execute(
                """
                UPDATE tasks
                SET latest_checkpoint_id = ?, status = ?, revision = revision + 1,
                    updated_at = ?
                WHERE task_id = ? AND revision = ?
                """,
                (
                    checkpoint_id,
                    status.value,
                    now,
                    task_id,
                    int(expected_task_revision),
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeStateConflict(
                    f"task revision conflict: task_id={task_id} expected={expected_task_revision}"
                )
            task_revision = expected_task_revision + 1
            self._connection.execute(
                """
                INSERT INTO task_checkpoints (
                    checkpoint_id, task_id, revision, disposition, summary,
                    next_step, evidence_json, wait_for_json,
                    body_fingerprint_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint_id,
                    task_id,
                    task_revision,
                    disposition.value,
                    _required_text("summary", summary, max_length=4000),
                    _bounded_text(next_step, max_length=2000),
                    _json_dump(evidence_items),
                    _json_dump(wait_items),
                    None if body_fingerprint is None else _json_dump(body_fingerprint),
                    now,
                ),
            )
        task = self.get_task(task_id)
        checkpoint = self.get_checkpoint(checkpoint_id)
        assert task is not None and checkpoint is not None
        return task, checkpoint

    def get_checkpoint(self, checkpoint_id: str) -> TaskCheckpointRecord | None:
        with self._lock:
            self._require_open()
            row = self._connection.execute(
                "SELECT * FROM task_checkpoints WHERE checkpoint_id = ?",
                (checkpoint_id,),
            ).fetchone()
        return None if row is None else _checkpoint_from_row(row)

    def get_latest_checkpoint(self, task_id: str) -> TaskCheckpointRecord | None:
        with self._lock:
            self._require_open()
            row = self._connection.execute(
                """
                SELECT c.* FROM task_checkpoints c
                JOIN tasks t ON t.latest_checkpoint_id = c.checkpoint_id
                WHERE t.task_id = ?
                """,
                (task_id,),
            ).fetchone()
        return None if row is None else _checkpoint_from_row(row)

    def enqueue_work_intent(
        self,
        scope: RuntimeScope,
        *,
        kind: str,
        source: str,
        priority: int,
        payload: dict[str, object],
        dedupe_key: str | None = None,
        task_id: str | None = None,
        generation: int | None = None,
        available_at: str | None = None,
    ) -> dict[str, object]:
        self.register_scope(scope)
        intent_id = f"intent-{uuid4().hex}"
        now = _utc_now()
        normalized_dedupe = _bounded_text(dedupe_key or "", max_length=500) or None
        values = (
            intent_id,
            scope.key,
            _required_text("kind", kind, max_length=128),
            _required_text("source", source, max_length=128),
            int(priority),
            _json_dump(payload),
            normalized_dedupe,
            task_id,
            generation,
            "queued",
            available_at or now,
            now,
        )
        try:
            with self._lock, self._connection:
                self._require_open()
                self._connection.execute(
                    """
                    INSERT INTO work_intents (
                        intent_id, scope_key, revision, kind, source, priority,
                        payload_json, dedupe_key, task_id, generation, state,
                        available_at, created_at, lease_owner, lease_expires_at,
                        attempt_count, leased_at, completed_at, error_json
                    ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 0, NULL, NULL, NULL)
                    """,
                    values,
                )
        except sqlite3.IntegrityError as exc:
            if normalized_dedupe is None:
                raise RuntimeStateConflict(f"work intent insert failed: {exc}") from exc
            existing = self.get_work_intent_by_dedupe(scope, normalized_dedupe)
            if existing is None:
                raise RuntimeStateConflict(f"work intent insert failed: {exc}") from exc
            return existing
        record = self.get_work_intent(intent_id)
        assert record is not None
        return record

    def get_work_intent(self, intent_id: str) -> dict[str, object] | None:
        with self._lock:
            self._require_open()
            row = self._connection.execute(
                "SELECT * FROM work_intents WHERE intent_id = ?",
                (intent_id,),
            ).fetchone()
        return None if row is None else _work_intent_from_row(row)

    def get_live_work_intent(
        self,
        scope: RuntimeScope,
        dedupe_key: str,
    ) -> dict[str, object] | None:
        with self._lock:
            self._require_open()
            row = self._connection.execute(
                """
                SELECT * FROM work_intents
                WHERE scope_key = ? AND dedupe_key = ?
                  AND state IN ('queued', 'leased')
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (scope.key, dedupe_key),
            ).fetchone()
        return None if row is None else _work_intent_from_row(row)

    def get_work_intent_by_dedupe(
        self,
        scope: RuntimeScope,
        dedupe_key: str,
    ) -> dict[str, object] | None:
        with self._lock:
            self._require_open()
            row = self._connection.execute(
                """
                SELECT * FROM work_intents
                WHERE scope_key = ? AND dedupe_key = ?
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (scope.key, dedupe_key),
            ).fetchone()
        return None if row is None else _work_intent_from_row(row)

    def list_queued_work_intents(
        self,
        scope: RuntimeScope,
        *,
        kind: str | None = None,
    ) -> list[dict[str, object]]:
        with self._lock:
            self._require_open()
            if kind is None:
                rows = self._connection.execute(
                    """
                    SELECT * FROM work_intents
                    WHERE scope_key = ? AND state = 'queued'
                    ORDER BY priority DESC, created_at ASC, intent_id ASC
                    """,
                    (scope.key,),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    """
                    SELECT * FROM work_intents
                    WHERE scope_key = ? AND state = 'queued' AND kind = ?
                    ORDER BY priority DESC, created_at ASC, intent_id ASC
                    """,
                    (scope.key, kind),
                ).fetchall()
        return [_work_intent_from_row(row) for row in rows]

    def lease_next_work_intent(
        self,
        scope: RuntimeScope,
        *,
        lease_owner: str,
        lease_seconds: float = 60.0,
    ) -> dict[str, object] | None:
        now = _utc_now()
        expires_at = _utc_after(lease_seconds)
        with self._lock:
            self._require_open()
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute(
                    """
                    UPDATE work_intents
                    SET state = 'queued', lease_owner = NULL,
                        lease_expires_at = NULL, leased_at = NULL,
                        revision = revision + 1
                    WHERE scope_key = ? AND state = 'leased'
                      AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?
                    """,
                    (scope.key, now),
                )
                row = self._connection.execute(
                    """
                    SELECT * FROM work_intents
                    WHERE scope_key = ? AND state = 'queued' AND available_at <= ?
                    ORDER BY priority DESC, created_at ASC, intent_id ASC
                    LIMIT 1
                    """,
                    (scope.key, now),
                ).fetchone()
                if row is None:
                    self._connection.commit()
                    return None
                cursor = self._connection.execute(
                    """
                    UPDATE work_intents
                    SET state = 'leased', revision = revision + 1,
                        lease_owner = ?, lease_expires_at = ?, leased_at = ?,
                        attempt_count = attempt_count + 1
                    WHERE intent_id = ? AND revision = ? AND state = 'queued'
                    """,
                    (
                        _required_text("lease_owner", lease_owner, max_length=256),
                        expires_at,
                        now,
                        row["intent_id"],
                        row["revision"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeStateConflict("work intent lease race")
                leased = self._connection.execute(
                    "SELECT * FROM work_intents WHERE intent_id = ?",
                    (row["intent_id"],),
                ).fetchone()
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
        assert leased is not None
        return _work_intent_from_row(leased)

    def finish_work_intent(
        self,
        intent_id: str,
        *,
        expected_revision: int,
        state: str,
        error: dict[str, object] | None = None,
    ) -> dict[str, object]:
        if state not in {"completed", "superseded", "failed"}:
            raise ValueError(f"invalid terminal work intent state: {state}")
        now = _utc_now()
        with self._lock, self._connection:
            self._require_open()
            cursor = self._connection.execute(
                """
                UPDATE work_intents
                SET state = ?, revision = revision + 1, completed_at = ?,
                    lease_owner = NULL, lease_expires_at = NULL,
                    error_json = ?
                WHERE intent_id = ? AND revision = ? AND state = 'leased'
                """,
                (
                    state,
                    now,
                    None if error is None else _json_dump(error),
                    intent_id,
                    int(expected_revision),
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeStateConflict(
                    f"work intent revision conflict: intent_id={intent_id} expected={expected_revision}"
                )
        record = self.get_work_intent(intent_id)
        assert record is not None
        return record

    def supersede_queued_work_intents(
        self,
        scope: RuntimeScope,
        *,
        kinds: set[str],
        reason: str,
    ) -> int:
        if not kinds:
            return 0
        placeholders = ",".join("?" for _ in kinds)
        now = _utc_now()
        ordered_kinds = sorted(kinds)
        with self._lock, self._connection:
            self._require_open()
            cursor = self._connection.execute(
                f"""
                UPDATE work_intents
                SET state = 'superseded', revision = revision + 1,
                    completed_at = ?, error_json = ?
                WHERE scope_key = ? AND state = 'queued'
                  AND kind IN ({placeholders})
                """,
                (
                    now,
                    _json_dump({"reason": reason}),
                    scope.key,
                    *ordered_kinds,
                ),
            )
        return int(cursor.rowcount)

    def queued_work_intent_count(self, scope: RuntimeScope) -> int:
        with self._lock:
            self._require_open()
            row = self._connection.execute(
                """
                SELECT COUNT(*) AS count FROM work_intents
                WHERE scope_key = ? AND state = 'queued'
                """,
                (scope.key,),
            ).fetchone()
        return int(row["count"] if row is not None else 0)

    def work_intent_count(
        self,
        scope: RuntimeScope,
        *,
        kind: str,
        task_id: str,
    ) -> int:
        with self._lock:
            self._require_open()
            row = self._connection.execute(
                """
                SELECT COUNT(*) AS count FROM work_intents
                WHERE scope_key = ? AND kind = ? AND task_id = ?
                """,
                (scope.key, kind, task_id),
            ).fetchone()
        return int(row["count"] if row is not None else 0)

    def abandon_leased_work_intents(
        self,
        scope: RuntimeScope,
        *,
        reason: str = "process_restart_unknown_outcome",
    ) -> list[dict[str, object]]:
        now = _utc_now()
        with self._lock, self._connection:
            self._require_open()
            rows = self._connection.execute(
                """
                SELECT * FROM work_intents
                WHERE scope_key = ? AND state = 'leased'
                ORDER BY leased_at ASC, created_at ASC
                """,
                (scope.key,),
            ).fetchall()
            if not rows:
                return []
            cursor = self._connection.execute(
                """
                UPDATE work_intents
                SET state = 'failed', revision = revision + 1,
                    lease_owner = NULL, lease_expires_at = NULL,
                    completed_at = ?, error_json = ?
                WHERE scope_key = ? AND state = 'leased'
                """,
                (
                    now,
                    _json_dump({"reason": reason}),
                    scope.key,
                ),
            )
            if cursor.rowcount != len(rows):
                raise RuntimeStateConflict("leased work intent abandonment race")
        return [_work_intent_from_row(row) for row in rows]

    def get_event_cursor(self, scope: RuntimeScope) -> tuple[int, int, str | None] | None:
        with self._lock:
            self._require_open()
            row = self._connection.execute(
                "SELECT last_seq, last_chat_seq, event_epoch FROM event_cursors WHERE scope_key = ?",
                (scope.key,),
            ).fetchone()
        if row is None:
            return None
        return int(row["last_seq"]), int(row["last_chat_seq"]), (
            None if row["event_epoch"] is None else str(row["event_epoch"])
        )

    def set_event_cursor(
        self,
        scope: RuntimeScope,
        *,
        last_seq: int,
        last_chat_seq: int,
        event_epoch: str | None,
    ) -> None:
        self.register_scope(scope)
        now = _utc_now()
        with self._lock, self._connection:
            self._require_open()
            self._connection.execute(
                """
                INSERT INTO event_cursors (
                    scope_key, last_seq, last_chat_seq, event_epoch, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(scope_key) DO UPDATE SET
                    last_seq = excluded.last_seq,
                    last_chat_seq = excluded.last_chat_seq,
                    event_epoch = excluded.event_epoch,
                    updated_at = excluded.updated_at
                """,
                (
                    scope.key,
                    max(0, int(last_seq)),
                    max(0, int(last_chat_seq)),
                    event_epoch,
                    now,
                ),
            )

    def replace_conversation_archive(
        self,
        scope: RuntimeScope,
        *,
        items: list[object],
        summary: dict[str, object],
    ) -> int:
        self.register_scope(scope)
        now = _utc_now()
        with self._lock, self._connection:
            self._require_open()
            row = self._connection.execute(
                "SELECT revision FROM conversation_archives WHERE scope_key = ?",
                (scope.key,),
            ).fetchone()
            revision = 1 if row is None else int(row["revision"]) + 1
            self._connection.execute(
                """
                INSERT INTO conversation_archives (
                    scope_key, revision, item_count, items_json, summary_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope_key) DO UPDATE SET
                    revision = excluded.revision,
                    item_count = excluded.item_count,
                    items_json = excluded.items_json,
                    summary_json = excluded.summary_json,
                    updated_at = excluded.updated_at
                """,
                (
                    scope.key,
                    revision,
                    len(items),
                    _json_dump(items),
                    _json_dump(summary),
                    now,
                ),
            )
        return revision

    def get_conversation_archive(self, scope: RuntimeScope) -> dict[str, object] | None:
        with self._lock:
            self._require_open()
            row = self._connection.execute(
                "SELECT * FROM conversation_archives WHERE scope_key = ?",
                (scope.key,),
            ).fetchone()
        if row is None:
            return None
        items = _json_load(row["items_json"], default=[])
        summary = _json_load(row["summary_json"], default={})
        if not isinstance(items, list) or not isinstance(summary, dict):
            raise RuntimeStateError("conversation archive payload is corrupt")
        return {
            "scope_key": scope.key,
            "revision": int(row["revision"]),
            "item_count": int(row["item_count"]),
            "items": items,
            "summary": summary,
            "updated_at": str(row["updated_at"]),
        }

    def clear_conversation_archive(self, scope: RuntimeScope) -> None:
        with self._lock, self._connection:
            self._require_open()
            self._connection.execute(
                "DELETE FROM conversation_archives WHERE scope_key = ?",
                (scope.key,),
            )

    def create_tool_observation(
        self,
        scope: RuntimeScope,
        *,
        tool_name: str,
        tool_call_id: str,
        result: dict[str, object],
        complete: bool | None,
    ) -> dict[str, object]:
        self.register_scope(scope)
        observation_id = f"observation-{uuid4().hex}"
        handle = f"observation:{observation_id}"
        now = _utc_now()
        encoded = _json_dump(result)
        success = bool(result.get("success"))
        reason = _bounded_text(result.get("reason") or "", max_length=512)
        with self._lock, self._connection:
            self._require_open()
            self._connection.execute(
                """
                INSERT INTO tool_observations (
                    observation_id, scope_key, handle, tool_name, tool_call_id,
                    success, reason, complete, payload_bytes, result_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation_id,
                    scope.key,
                    handle,
                    _required_text("tool_name", tool_name, max_length=128),
                    _required_text("tool_call_id", tool_call_id, max_length=256),
                    int(success),
                    reason,
                    None if complete is None else int(complete),
                    len(encoded.encode("utf-8")),
                    encoded,
                    now,
                ),
            )
        record = self.get_tool_observation(scope, handle)
        assert record is not None
        return record

    def get_tool_observation(
        self,
        scope: RuntimeScope,
        handle: str,
    ) -> dict[str, object] | None:
        with self._lock:
            self._require_open()
            row = self._connection.execute(
                """
                SELECT * FROM tool_observations
                WHERE scope_key = ? AND handle = ?
                """,
                (scope.key, str(handle)),
            ).fetchone()
        return None if row is None else _tool_observation_from_row(row)

    def query_tool_observations(
        self,
        scope: RuntimeScope,
        *,
        query: str = "",
        tool_name: str = "",
        reason: str = "",
        start: int = 0,
        limit: int = 10,
    ) -> dict[str, object]:
        query_text = _bounded_text(query, max_length=500)
        tool_text = _bounded_text(tool_name, max_length=128)
        reason_text = _bounded_text(reason, max_length=512)
        start = max(0, int(start))
        limit = max(1, min(50, int(limit)))
        clauses = ["scope_key = ?"]
        params: list[object] = [scope.key]
        if tool_text:
            clauses.append("tool_name = ?")
            params.append(tool_text)
        if reason_text:
            clauses.append("reason = ?")
            params.append(reason_text)
        if query_text:
            clauses.append("(tool_name LIKE ? OR reason LIKE ? OR result_json LIKE ?)")
            needle = f"%{query_text}%"
            params.extend((needle, needle, needle))
        where = " AND ".join(clauses)
        with self._lock:
            self._require_open()
            total_row = self._connection.execute(
                f"SELECT COUNT(*) AS count FROM tool_observations WHERE {where}",
                params,
            ).fetchone()
            rows = self._connection.execute(
                f"""
                SELECT * FROM tool_observations
                WHERE {where}
                ORDER BY created_at DESC, observation_id DESC
                LIMIT ? OFFSET ?
                """,
                [*params, limit, start],
            ).fetchall()
        total = 0 if total_row is None else int(total_row["count"])
        records = [_tool_observation_from_row(row, include_result=False) for row in rows]
        next_start = start + len(records) if start + len(records) < total else None
        return {
            "query": query_text,
            "tool": tool_text or None,
            "reason": reason_text or None,
            "start": start,
            "limit": limit,
            "total_matches": total,
            "results": records,
            "next_start": next_start,
            "complete": next_start is None,
        }

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._connection.close()

    def _initialize_schema(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS minebot_schema (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    version INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runtime_scopes (
                    scope_key TEXT PRIMARY KEY,
                    server_id TEXT NOT NULL,
                    world_id TEXT NOT NULL,
                    bot_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(server_id, world_id, bot_id)
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    scope_key TEXT NOT NULL REFERENCES runtime_scopes(scope_key) ON DELETE CASCADE,
                    revision INTEGER NOT NULL,
                    goal_text TEXT NOT NULL,
                    source TEXT NOT NULL,
                    requested_by TEXT NOT NULL,
                    status TEXT NOT NULL,
                    completion_authority TEXT NOT NULL,
                    active_plan_id TEXT,
                    latest_checkpoint_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_tasks_scope_status
                ON tasks(scope_key, status, updated_at);

                CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_one_foreground
                ON tasks(scope_key)
                WHERE status IN ('running', 'waiting_event', 'paused', 'yielded');

                CREATE TABLE IF NOT EXISTS task_plans (
                    plan_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                    revision INTEGER NOT NULL,
                    summary TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS task_plan_steps (
                    step_id TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL REFERENCES task_plans(plan_id) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    blocker TEXT,
                    updated_at TEXT NOT NULL,
                    UNIQUE(plan_id, ordinal)
                );

                CREATE TABLE IF NOT EXISTS task_checkpoints (
                    checkpoint_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                    revision INTEGER NOT NULL,
                    disposition TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    next_step TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    wait_for_json TEXT NOT NULL,
                    body_fingerprint_json TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS work_intents (
                    intent_id TEXT PRIMARY KEY,
                    scope_key TEXT NOT NULL REFERENCES runtime_scopes(scope_key) ON DELETE CASCADE,
                    revision INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    source TEXT NOT NULL,
                    priority INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    dedupe_key TEXT,
                    task_id TEXT REFERENCES tasks(task_id) ON DELETE CASCADE,
                    generation INTEGER,
                    state TEXT NOT NULL,
                    available_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    leased_at TEXT,
                    completed_at TEXT,
                    error_json TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_work_intents_ready
                ON work_intents(scope_key, state, priority DESC, available_at, created_at);

                CREATE UNIQUE INDEX IF NOT EXISTS idx_work_intents_live_dedupe
                ON work_intents(scope_key, dedupe_key)
                WHERE dedupe_key IS NOT NULL;

                CREATE TABLE IF NOT EXISTS event_cursors (
                    scope_key TEXT PRIMARY KEY REFERENCES runtime_scopes(scope_key) ON DELETE CASCADE,
                    last_seq INTEGER NOT NULL,
                    last_chat_seq INTEGER NOT NULL DEFAULT 0,
                    event_epoch TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS conversation_archives (
                    scope_key TEXT PRIMARY KEY REFERENCES runtime_scopes(scope_key) ON DELETE CASCADE,
                    revision INTEGER NOT NULL,
                    item_count INTEGER NOT NULL,
                    items_json TEXT NOT NULL,
                    summary_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tool_observations (
                    observation_id TEXT PRIMARY KEY,
                    scope_key TEXT NOT NULL REFERENCES runtime_scopes(scope_key) ON DELETE CASCADE,
                    handle TEXT NOT NULL UNIQUE,
                    tool_name TEXT NOT NULL,
                    tool_call_id TEXT NOT NULL,
                    success INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    complete INTEGER,
                    payload_bytes INTEGER NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_tool_observations_scope_created
                ON tool_observations(scope_key, created_at DESC, observation_id DESC);
                """
            )
            row = self._connection.execute(
                "SELECT version FROM minebot_schema WHERE singleton = 1"
            ).fetchone()
            if row is None:
                self._connection.execute(
                    "INSERT INTO minebot_schema(singleton, version) VALUES (1, ?)",
                    (RUNTIME_SCHEMA_VERSION,),
                )
            elif int(row["version"]) > RUNTIME_SCHEMA_VERSION:
                raise RuntimeStateError(
                    "unsupported runtime schema version "
                    f"{row['version']}; expected {RUNTIME_SCHEMA_VERSION}"
                )
            elif int(row["version"]) < RUNTIME_SCHEMA_VERSION:
                self._migrate_schema(int(row["version"]))

    def _migrate_schema(self, version: int) -> None:
        current = version
        while current < RUNTIME_SCHEMA_VERSION:
            if current == 1:
                # The v2 foreground-task index is created idempotently by the
                # main schema script before this version bump.
                current = 2
            elif current == 2:
                columns = {
                    str(row["name"])
                    for row in self._connection.execute(
                        "PRAGMA table_info(work_intents)"
                    ).fetchall()
                }
                if "lease_owner" not in columns:
                    self._connection.execute(
                        "ALTER TABLE work_intents ADD COLUMN lease_owner TEXT"
                    )
                if "lease_expires_at" not in columns:
                    self._connection.execute(
                        "ALTER TABLE work_intents ADD COLUMN lease_expires_at TEXT"
                    )
                if "attempt_count" not in columns:
                    self._connection.execute(
                        "ALTER TABLE work_intents ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0"
                    )
                current = 3
            elif current == 3:
                self._connection.execute("DROP INDEX IF EXISTS idx_work_intents_live_dedupe")
                self._connection.execute(
                    """
                    CREATE UNIQUE INDEX idx_work_intents_live_dedupe
                    ON work_intents(scope_key, dedupe_key)
                    WHERE dedupe_key IS NOT NULL
                    """
                )
                current = 4
            elif current == 4:
                columns = {
                    str(row["name"])
                    for row in self._connection.execute(
                        "PRAGMA table_info(event_cursors)"
                    ).fetchall()
                }
                if "last_chat_seq" not in columns:
                    self._connection.execute(
                        "ALTER TABLE event_cursors ADD COLUMN last_chat_seq INTEGER NOT NULL DEFAULT 0"
                    )
                current = 5
            elif current == 5:
                current = 6
            elif current == 6:
                current = 7
            else:
                raise RuntimeStateError(
                    f"no runtime schema migration from version {current}"
                )
            self._connection.execute(
                "UPDATE minebot_schema SET version = ? WHERE singleton = 1",
                (current,),
            )

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeStateError("runtime state store is closed")


def _validated_scope_component(field_name: str, value: object) -> str:
    clean = str(value or "").strip()
    if not clean:
        raise ValueError(f"{field_name} must not be empty")
    if len(clean) > _MAX_SCOPE_COMPONENT_LENGTH:
        raise ValueError(f"{field_name} exceeds {_MAX_SCOPE_COMPONENT_LENGTH} characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in clean):
        raise ValueError(f"{field_name} contains control characters")
    return clean


def _task_from_row(row: sqlite3.Row) -> TaskRecord:
    return TaskRecord(
        task_id=str(row["task_id"]),
        scope_key=str(row["scope_key"]),
        revision=int(row["revision"]),
        goal_text=str(row["goal_text"]),
        source=str(row["source"]),
        requested_by=str(row["requested_by"]),
        status=TaskStatus(str(row["status"])),
        completion_authority=CompletionAuthority(str(row["completion_authority"])),
        active_plan_id=None if row["active_plan_id"] is None else str(row["active_plan_id"]),
        latest_checkpoint_id=(
            None if row["latest_checkpoint_id"] is None else str(row["latest_checkpoint_id"])
        ),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _plan_from_rows(plan_row: sqlite3.Row, step_rows: list[sqlite3.Row]) -> TaskPlanRecord:
    return TaskPlanRecord(
        plan_id=str(plan_row["plan_id"]),
        task_id=str(plan_row["task_id"]),
        revision=int(plan_row["revision"]),
        summary=str(plan_row["summary"]),
        steps=tuple(
            PlanStepRecord(
                step_id=str(row["step_id"]),
                ordinal=int(row["ordinal"]),
                title=str(row["title"]),
                status=PlanStepStatus(str(row["status"])),
                evidence=tuple(_json_string_list(row["evidence_json"])),
                blocker=None if row["blocker"] is None else str(row["blocker"]),
                updated_at=str(row["updated_at"]),
            )
            for row in step_rows
        ),
        created_at=str(plan_row["created_at"]),
        updated_at=str(plan_row["updated_at"]),
    )


def _checkpoint_from_row(row: sqlite3.Row) -> TaskCheckpointRecord:
    raw_fingerprint = row["body_fingerprint_json"]
    fingerprint: dict[str, object] | None = None
    if raw_fingerprint is not None:
        try:
            decoded = json.loads(str(raw_fingerprint))
        except json.JSONDecodeError as exc:
            raise RuntimeStateError("stored body fingerprint JSON is corrupt") from exc
        if isinstance(decoded, dict):
            fingerprint = decoded
        else:
            raise RuntimeStateError("stored body fingerprint is not an object")
    return TaskCheckpointRecord(
        checkpoint_id=str(row["checkpoint_id"]),
        task_id=str(row["task_id"]),
        revision=int(row["revision"]),
        disposition=CheckpointDisposition(str(row["disposition"])),
        summary=str(row["summary"]),
        next_step=str(row["next_step"]),
        evidence=tuple(_json_string_list(row["evidence_json"])),
        wait_for=tuple(_json_string_list(row["wait_for_json"])),
        body_fingerprint=fingerprint,
        created_at=str(row["created_at"]),
    )


def _work_intent_from_row(row: sqlite3.Row) -> dict[str, object]:
    try:
        payload = json.loads(str(row["payload_json"]))
        error = None if row["error_json"] is None else json.loads(str(row["error_json"]))
    except json.JSONDecodeError as exc:
        raise RuntimeStateError("stored work intent JSON is corrupt") from exc
    if not isinstance(payload, dict):
        raise RuntimeStateError("stored work intent payload is not an object")
    if error is not None and not isinstance(error, dict):
        raise RuntimeStateError("stored work intent error is not an object")
    return {
        "intent_id": str(row["intent_id"]),
        "scope_key": str(row["scope_key"]),
        "revision": int(row["revision"]),
        "kind": str(row["kind"]),
        "source": str(row["source"]),
        "priority": int(row["priority"]),
        "payload": payload,
        "dedupe_key": None if row["dedupe_key"] is None else str(row["dedupe_key"]),
        "task_id": None if row["task_id"] is None else str(row["task_id"]),
        "generation": None if row["generation"] is None else int(row["generation"]),
        "state": str(row["state"]),
        "available_at": str(row["available_at"]),
        "created_at": str(row["created_at"]),
        "lease_owner": None if row["lease_owner"] is None else str(row["lease_owner"]),
        "lease_expires_at": (
            None if row["lease_expires_at"] is None else str(row["lease_expires_at"])
        ),
        "attempt_count": int(row["attempt_count"]),
        "leased_at": None if row["leased_at"] is None else str(row["leased_at"]),
        "completed_at": None if row["completed_at"] is None else str(row["completed_at"]),
        "error": error,
    }


def _tool_observation_from_row(
    row: sqlite3.Row,
    *,
    include_result: bool = True,
) -> dict[str, object]:
    result: object | None = None
    if include_result:
        result = _json_load(row["result_json"], default={})
        if not isinstance(result, dict):
            raise RuntimeStateError("stored tool observation result is not an object")
    complete = row["complete"]
    record: dict[str, object] = {
        "observation_id": str(row["observation_id"]),
        "scope_key": str(row["scope_key"]),
        "handle": str(row["handle"]),
        "tool": str(row["tool_name"]),
        "tool_call_id": str(row["tool_call_id"]),
        "success": bool(row["success"]),
        "reason": str(row["reason"]),
        "complete": None if complete is None else bool(complete),
        "payload_bytes": int(row["payload_bytes"]),
        "created_at": str(row["created_at"]),
    }
    if include_result:
        record["result"] = result
    return record


def _normalize_plan_steps(steps: list[dict[str, object]]) -> list[dict[str, object]]:
    if not isinstance(steps, list):
        raise ValueError("steps must be a list")
    if len(steps) > 64:
        raise ValueError("plan exceeds 64 steps")
    normalized: list[dict[str, object]] = []
    in_progress = 0
    for index, raw in enumerate(steps):
        if not isinstance(raw, dict):
            raise ValueError(f"plan step {index} must be an object")
        title = _required_text(f"steps[{index}].title", raw.get("title"), max_length=500)
        try:
            status = PlanStepStatus(str(raw.get("status") or PlanStepStatus.PENDING.value))
        except ValueError as exc:
            raise ValueError(f"invalid plan step status at index {index}") from exc
        if status is PlanStepStatus.IN_PROGRESS:
            in_progress += 1
        evidence = _bounded_text_list(
            raw.get("evidence") or (),
            max_items=16,
            max_length=1000,
        )
        blocker = _bounded_text(raw.get("blocker") or "", max_length=1000) or None
        normalized.append(
            {
                "title": title,
                "status": status.value,
                "evidence": evidence,
                "blocker": blocker,
            }
        )
    if in_progress > 1:
        raise ValueError("at most one plan step may be in_progress")
    return normalized


def _required_text(field_name: str, value: object, *, max_length: int) -> str:
    clean = _bounded_text(value, max_length=max_length)
    if not clean:
        raise ValueError(f"{field_name} must not be empty")
    return clean


def _bounded_text(value: object, *, max_length: int) -> str:
    return " ".join(str(value or "").strip().split())[:max_length]


def _bounded_text_list(
    values: object,
    *,
    max_items: int,
    max_length: int,
) -> list[str]:
    if not isinstance(values, (list, tuple)):
        raise ValueError("expected a list of strings")
    items = [
        clean
        for value in values[:max_items]
        if (clean := _bounded_text(value, max_length=max_length))
    ]
    return items


def _json_dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _json_load(value: object, *, default: object) -> object:
    if value is None:
        return default
    try:
        return json.loads(str(value))
    except json.JSONDecodeError as exc:
        raise RuntimeStateError("stored JSON value is corrupt") from exc


def _json_string_list(value: object) -> list[str]:
    try:
        decoded = json.loads(str(value))
    except json.JSONDecodeError as exc:
        raise RuntimeStateError("stored JSON list is corrupt") from exc
    if not isinstance(decoded, list) or not all(isinstance(item, str) for item in decoded):
        raise RuntimeStateError("stored JSON value is not a string list")
    return decoded


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _utc_after(seconds: float) -> str:
    from datetime import timedelta

    return (datetime.now(UTC) + timedelta(seconds=max(0.0, float(seconds)))).isoformat(
        timespec="milliseconds"
    )


__all__ = [
    "DEFAULT_RUNTIME_STATE_DB",
    "RUNTIME_SCHEMA_VERSION",
    "CheckpointDisposition",
    "CompletionAuthority",
    "PlanStepRecord",
    "PlanStepStatus",
    "RuntimeScope",
    "RuntimeStateConflict",
    "RuntimeStateError",
    "RuntimeStateStore",
    "TaskCheckpointRecord",
    "TaskPlanRecord",
    "TaskRecord",
    "TaskStatus",
]
