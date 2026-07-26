"""Durable, scope-isolated control-plane storage for MineBot runtimes.

This module owns the SQLite side: schema, migrations, transactions, and the
``RuntimeStateStore`` facade. The persisted record vocabulary lives in
``runtime_records`` and the pure row/payload/validation layer in
``runtime_state_mapping`` (framework §12 H2); both are re-exported here so
every existing consumer keeps importing one control-plane surface.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from uuid import uuid4

from minebot.app.runtime_records import (
    _MEMORY_SOURCE_RANK,
    CheckpointDisposition,
    CompletionAuthority,
    ContinuationContract,
    ContinuationOperationClass,
    MemoryKind,
    MemoryRecord,
    MemorySource,
    MemoryStateConflict,
    PlanStepRecord,
    PlanStepStatus,
    RuntimeScope,
    RuntimeStateConflict,
    RuntimeStateError,
    SkillActivationRecord,
    SkillHeadRecord,
    SkillVersionRecord,
    TaskCheckpointRecord,
    TaskPlanRecord,
    TaskRecord,
    TaskStatus,
    WikiCacheRecord,
)
from minebot.app.runtime_state_mapping import (
    _bounded_text,
    _bounded_text_list,
    _checkpoint_from_row,
    _continuation_from_json,
    _continuation_payload,
    _exploration_coverage_from_row,
    _fuse_memory_lanes,
    _json_dump,
    _json_load,
    _memory_filter_sql,
    _memory_from_row,
    _memory_like_query,
    _memory_payload,
    _memory_terms_query,
    _memory_trigram_query,
    _normalize_plan_steps,
    _normalized_evidence_handles,
    _plan_from_rows,
    _point_columns,
    _progress_epoch_from_row,
    _region_columns,
    _required_text,
    _skill_activation_from_row,
    _skill_head_from_row,
    _skill_version_from_row,
    _strict_optional_text,
    _strict_required_text,
    _task_from_row,
    _tool_observation_from_row,
    _utc_after,
    _utc_now,
    _validated_json_objects,
    _validated_memory_search_geometry,
    _validated_memory_values,
    _validated_string_tuple,
    _wiki_cache_from_row,
    _work_intent_from_row,
    memory_record_payload,
    skill_activation_payload,
)


RUNTIME_SCHEMA_VERSION = 16
DEFAULT_RUNTIME_STATE_DB = Path("var/minebot/agent-state.sqlite3")



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
        continuation: ContinuationContract | None = None,
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
                    body_fingerprint_json, continuation_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    None if continuation is None else _json_dump(_continuation_payload(continuation)),
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

    def list_task_checkpoints(self, task_id: str) -> list[TaskCheckpointRecord]:
        with self._lock:
            self._require_open()
            rows = self._connection.execute(
                """
                SELECT * FROM task_checkpoints
                WHERE task_id = ?
                ORDER BY revision ASC, created_at ASC
                """,
                (task_id,),
            ).fetchall()
        return [_checkpoint_from_row(row) for row in rows]

    def continuation_approach_remaining(
        self,
        scope: RuntimeScope,
        *,
        task_id: str,
        approach_key: str,
        requested_budget: int,
    ) -> int:
        with self._lock:
            self._require_open()
            row = self._connection.execute(
                """
                SELECT budget_limit, consumed_epochs
                FROM continuation_approaches
                WHERE scope_key = ? AND task_id = ? AND approach_key = ?
                """,
                (scope.key, task_id, approach_key),
            ).fetchone()
        if row is None:
            return max(0, int(requested_budget))
        remaining = max(0, int(row["budget_limit"]) - int(row["consumed_epochs"]))
        return min(max(0, int(requested_budget)), remaining)

    def settle_continuation_approach(
        self,
        scope: RuntimeScope,
        *,
        checkpoint_id: str,
        task_id: str,
        approach_key: str,
        budget_limit: int,
        consumed_epochs: int,
    ) -> dict[str, int] | None:
        now = _utc_now()
        consumed = max(0, int(consumed_epochs))
        with self._lock, self._connection:
            self._require_open()
            checkpoint = self._connection.execute(
                """
                SELECT c.continuation_json, t.latest_checkpoint_id
                FROM task_checkpoints c
                JOIN tasks t ON t.task_id = c.task_id
                WHERE c.checkpoint_id = ? AND c.task_id = ? AND t.scope_key = ?
                """,
                (checkpoint_id, task_id, scope.key),
            ).fetchone()
            if checkpoint is None or str(checkpoint["latest_checkpoint_id"] or "") != checkpoint_id:
                return None
            contract = _continuation_from_json(checkpoint["continuation_json"])
            if contract is None or contract.approach_key != approach_key:
                return None
            self._connection.execute(
                """
                INSERT OR IGNORE INTO continuation_approaches (
                    scope_key, task_id, approach_key, budget_limit,
                    consumed_epochs, updated_at
                ) VALUES (?, ?, ?, ?, 0, ?)
                """,
                (
                    scope.key,
                    task_id,
                    _required_text("approach_key", approach_key, max_length=256),
                    max(1, int(budget_limit)),
                    now,
                ),
            )
            settlement = self._connection.execute(
                """
                INSERT OR IGNORE INTO continuation_settlements (
                    checkpoint_id, scope_key, task_id, approach_key,
                    consumed_epochs, settled_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (checkpoint_id, scope.key, task_id, approach_key, consumed, now),
            )
            if settlement.rowcount == 1:
                self._connection.execute(
                    """
                    UPDATE continuation_approaches
                    SET consumed_epochs = consumed_epochs + ?, updated_at = ?
                    WHERE scope_key = ? AND task_id = ? AND approach_key = ?
                    """,
                    (consumed, now, scope.key, task_id, approach_key),
                )
            row = self._connection.execute(
                """
                SELECT budget_limit, consumed_epochs
                FROM continuation_approaches
                WHERE scope_key = ? AND task_id = ? AND approach_key = ?
                """,
                (scope.key, task_id, approach_key),
            ).fetchone()
            assert row is not None
            limit = int(row["budget_limit"])
            total = int(row["consumed_epochs"])
            return {
                "budget_limit": limit,
                "consumed_epochs": total,
                "remaining_epochs": max(0, limit - total),
            }

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

    def issue_checkpoint_continuation(
        self,
        scope: RuntimeScope,
        *,
        checkpoint_id: str,
        checkpoint_revision: int,
        task_id: str,
        generation: int,
        kind: str,
        source: str,
        priority: int,
        payload: dict[str, object],
        dedupe_key: str,
    ) -> dict[str, object] | None:
        self.register_scope(scope)
        normalized_dedupe = _required_text("dedupe_key", dedupe_key, max_length=500)
        intent_id = f"intent-{uuid4().hex}"
        now = _utc_now()
        with self._lock, self._connection:
            self._require_open()
            row = self._connection.execute(
                """
                SELECT c.*, t.status AS task_status, t.latest_checkpoint_id, t.scope_key
                FROM task_checkpoints c
                JOIN tasks t ON t.task_id = c.task_id
                WHERE c.checkpoint_id = ? AND c.task_id = ? AND t.scope_key = ?
                """,
                (checkpoint_id, task_id, scope.key),
            ).fetchone()
            if (
                row is None
                or int(row["revision"]) != int(checkpoint_revision)
                or str(row["latest_checkpoint_id"] or "") != checkpoint_id
                or str(row["disposition"]) != CheckpointDisposition.CONTINUE.value
                or str(row["task_status"]) != TaskStatus.RUNNING.value
            ):
                return None
            contract = _continuation_from_json(row["continuation_json"])
            if contract is None or contract.generation != int(generation):
                return None
            settlement = self._connection.execute(
                """
                SELECT consumed_epochs FROM continuation_settlements
                WHERE checkpoint_id = ? AND scope_key = ? AND task_id = ?
                  AND approach_key = ?
                """,
                (checkpoint_id, scope.key, task_id, contract.approach_key),
            ).fetchone()
            if (
                settlement is None
                or int(settlement["consumed_epochs"]) >= contract.bounded_epoch_budget
            ):
                return None
            approach = self._connection.execute(
                """
                SELECT budget_limit, consumed_epochs
                FROM continuation_approaches
                WHERE scope_key = ? AND task_id = ? AND approach_key = ?
                """,
                (scope.key, task_id, contract.approach_key),
            ).fetchone()
            if (
                approach is None
                or int(approach["consumed_epochs"]) >= int(approach["budget_limit"])
            ):
                return None
            existing = self._connection.execute(
                """
                SELECT * FROM work_intents
                WHERE scope_key = ? AND dedupe_key = ?
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (scope.key, normalized_dedupe),
            ).fetchone()
            if existing is not None:
                return _work_intent_from_row(existing)
            try:
                self._connection.execute(
                    """
                    INSERT INTO work_intents (
                        intent_id, scope_key, revision, kind, source, priority,
                        payload_json, dedupe_key, task_id, generation, state,
                        available_at, created_at, lease_owner, lease_expires_at,
                        attempt_count, leased_at, completed_at, error_json
                    ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, NULL, NULL, 0, NULL, NULL, NULL)
                    """,
                    (
                        intent_id,
                        scope.key,
                        _required_text("kind", kind, max_length=128),
                        _required_text("source", source, max_length=128),
                        int(priority),
                        _json_dump(payload),
                        normalized_dedupe,
                        task_id,
                        int(generation),
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                existing = self._connection.execute(
                    "SELECT * FROM work_intents WHERE scope_key = ? AND dedupe_key = ?",
                    (scope.key, normalized_dedupe),
                ).fetchone()
                if existing is None:
                    raise
                return _work_intent_from_row(existing)
            created = self._connection.execute(
                "SELECT * FROM work_intents WHERE intent_id = ?",
                (intent_id,),
            ).fetchone()
            assert created is not None
            return _work_intent_from_row(created)

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

    def create_progress_epoch(
        self,
        scope: RuntimeScope,
        *,
        record: dict[str, object],
    ) -> dict[str, object]:
        self.register_scope(scope)
        epoch_id = _required_text("epoch_id", record.get("epoch_id"), max_length=256)
        run_id = _required_text("run_id", record.get("run_id"), max_length=256)
        model_turn = int(record.get("model_turn") or 0)
        if model_turn < 1:
            raise ValueError("model_turn must be at least 1")
        raw_members = record.get("members")
        if not isinstance(raw_members, list) or len(raw_members) > 128:
            raise ValueError("progress epoch members must be a list of at most 128 items")
        members = [dict(member) for member in raw_members if isinstance(member, dict)]
        if len(members) != len(raw_members):
            raise ValueError("progress epoch members must contain only objects")
        evidence_refs = _bounded_text_list(
            record.get("evidence_refs") or (),
            max_items=128,
            max_length=256,
        )
        epistemic_keys = _bounded_text_list(
            record.get("epistemic_keys") or (),
            max_items=256,
            max_length=512,
        )
        now = _utc_now()
        values = (
            epoch_id,
            scope.key,
            run_id,
            model_turn,
            _json_dump(members),
            _bounded_text(record.get("pre_body_fingerprint") or "", max_length=2000) or None,
            _bounded_text(record.get("post_body_fingerprint") or "", max_length=2000) or None,
            _json_dump(evidence_refs),
            _json_dump(epistemic_keys),
            _json_dump([]),
            int(bool(record.get("material_changed"))),
            int(bool(record.get("progress_aborted"))),
            now,
        )
        try:
            with self._lock, self._connection:
                self._require_open()
                insert = self._connection.execute(
                    """
                    INSERT INTO progress_epochs (
                        epoch_id, scope_key, run_id, model_turn, members_json,
                        pre_body_fingerprint, post_body_fingerprint,
                        evidence_refs_json, epistemic_keys_json, novel_epistemic_keys_json,
                        material_changed, progress_aborted, settled_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
                epoch_cursor = int(insert.lastrowid)
                novel_keys: list[str] = []
                for evidence_key in epistemic_keys:
                    evidence_insert = self._connection.execute(
                        """
                        INSERT OR IGNORE INTO progress_evidence (
                            scope_key, evidence_key, first_epoch_cursor,
                            last_epoch_cursor, seen_count, last_observation_handle
                        ) VALUES (?, ?, ?, ?, 1, ?)
                        """,
                        (
                            scope.key,
                            evidence_key,
                            epoch_cursor,
                            epoch_cursor,
                            evidence_refs[-1] if evidence_refs else None,
                        ),
                    )
                    if evidence_insert.rowcount == 1:
                        novel_keys.append(evidence_key)
                        continue
                    self._connection.execute(
                        """
                        UPDATE progress_evidence
                        SET last_epoch_cursor = ?, seen_count = seen_count + 1,
                            last_observation_handle = COALESCE(?, last_observation_handle)
                        WHERE scope_key = ? AND evidence_key = ?
                        """,
                        (
                            epoch_cursor,
                            evidence_refs[-1] if evidence_refs else None,
                            scope.key,
                            evidence_key,
                        ),
                    )
                self._connection.execute(
                    """
                    UPDATE progress_epochs
                    SET novel_epistemic_keys_json = ?
                    WHERE cursor = ?
                    """,
                    (_json_dump(novel_keys), epoch_cursor),
                )
        except sqlite3.IntegrityError as exc:
            existing = self.get_progress_epoch(scope, epoch_id)
            if existing is None:
                raise RuntimeStateConflict(f"progress epoch insert failed: {exc}") from exc
            return existing
        created = self.get_progress_epoch(scope, epoch_id)
        assert created is not None
        return created

    def get_progress_epoch(
        self,
        scope: RuntimeScope,
        epoch_id: str,
    ) -> dict[str, object] | None:
        with self._lock:
            self._require_open()
            row = self._connection.execute(
                """
                SELECT * FROM progress_epochs
                WHERE scope_key = ? AND epoch_id = ?
                """,
                (scope.key, str(epoch_id)),
            ).fetchone()
        return None if row is None else _progress_epoch_from_row(row)

    def list_progress_epochs_after(
        self,
        scope: RuntimeScope,
        *,
        cursor: int,
        limit: int = 100,
    ) -> list[dict[str, object]]:
        bounded_limit = min(max(int(limit), 1), 500)
        with self._lock:
            self._require_open()
            rows = self._connection.execute(
                """
                SELECT * FROM progress_epochs
                WHERE scope_key = ? AND cursor > ?
                ORDER BY cursor ASC
                LIMIT ?
                """,
                (scope.key, max(int(cursor), 0), bounded_limit),
            ).fetchall()
        return [_progress_epoch_from_row(row) for row in rows]

    def latest_progress_epoch_cursor(self, scope: RuntimeScope) -> int:
        with self._lock:
            self._require_open()
            row = self._connection.execute(
                "SELECT COALESCE(MAX(cursor), 0) AS cursor FROM progress_epochs WHERE scope_key = ?",
                (scope.key,),
            ).fetchone()
        return 0 if row is None else int(row["cursor"])

    def mark_progress_epoch_aborted(
        self,
        scope: RuntimeScope,
        epoch_id: str,
    ) -> None:
        with self._lock, self._connection:
            self._require_open()
            cursor = self._connection.execute(
                """
                UPDATE progress_epochs
                SET progress_aborted = 1
                WHERE scope_key = ? AND epoch_id = ?
                """,
                (scope.key, str(epoch_id)),
            )
            if cursor.rowcount != 1:
                raise RuntimeStateConflict(
                    f"progress epoch not found: scope={scope.key} epoch_id={epoch_id}"
                )

    def progress_epoch_count_after(self, scope: RuntimeScope, *, cursor: int) -> int:
        with self._lock:
            self._require_open()
            row = self._connection.execute(
                "SELECT COUNT(*) AS count FROM progress_epochs WHERE scope_key = ? AND cursor > ?",
                (scope.key, max(int(cursor), 0)),
            ).fetchone()
        return 0 if row is None else int(row["count"])

    def append_exploration_coverage(
        self,
        scope: RuntimeScope,
        *,
        dimension: str,
        query_signature: str,
        region_x: int,
        region_z: int,
        status: str,
        center: tuple[int, int, int],
        reason: str,
        observations: tuple[dict[str, object], ...] = (),
        negative_evidence: tuple[str, ...] = (),
        uncertainty: tuple[dict[str, object], ...] = (),
    ) -> dict[str, object]:
        dimension_value = _required_text("dimension", dimension, max_length=128)
        signature_value = _required_text("query_signature", query_signature, max_length=128)
        status_value = str(status or "").strip()
        if status_value not in {
            "covered",
            "found",
            "mobility_blocked",
            "unloaded_boundary",
        }:
            raise ValueError(f"invalid exploration coverage status: {status_value!r}")
        if not isinstance(center, (list, tuple)) or len(center) != 3:
            raise ValueError("exploration coverage center must contain three coordinates")
        center_value = [int(value) for value in center]
        reason_value = _required_text("reason", reason, max_length=1000)
        observation_values = _validated_json_objects(
            "observations",
            observations,
            max_items=128,
        )
        uncertainty_values = _validated_json_objects(
            "uncertainty",
            uncertainty,
            max_items=128,
        )
        negative_values = _bounded_text_list(
            negative_evidence,
            max_items=64,
            max_length=256,
        )
        self.register_scope(scope)
        with self._lock, self._connection:
            self._require_open()
            inserted = self._connection.execute(
                """
                INSERT INTO exploration_coverage (
                    scope_key, dimension, query_signature, region_x, region_z,
                    status, center_json, reason, observations_json,
                    negative_evidence_json, uncertainty_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scope.key,
                    dimension_value,
                    signature_value,
                    int(region_x),
                    int(region_z),
                    status_value,
                    _json_dump(center_value),
                    reason_value,
                    _json_dump(observation_values),
                    _json_dump(negative_values),
                    _json_dump(uncertainty_values),
                    _utc_now(),
                ),
            )
            cursor = int(inserted.lastrowid)
            row = self._connection.execute(
                "SELECT * FROM exploration_coverage WHERE cursor = ?",
                (cursor,),
            ).fetchone()
        if row is None:
            raise RuntimeStateError("exploration coverage insert was not readable")
        return _exploration_coverage_from_row(row)

    def list_exploration_coverage(
        self,
        scope: RuntimeScope,
        *,
        dimension: str,
        query_signature: str,
    ) -> list[dict[str, object]]:
        dimension_value = _required_text("dimension", dimension, max_length=128)
        signature_value = _required_text("query_signature", query_signature, max_length=128)
        with self._lock:
            self._require_open()
            rows = self._connection.execute(
                """
                SELECT * FROM exploration_coverage
                WHERE scope_key = ? AND dimension = ? AND query_signature = ?
                ORDER BY cursor ASC
                """,
                (scope.key, dimension_value, signature_value),
            ).fetchall()
        return [_exploration_coverage_from_row(row) for row in rows]

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

    def create_memory(
        self,
        scope: RuntimeScope,
        *,
        kind: MemoryKind,
        source: MemorySource,
        title: str,
        content: str,
        subject_key: str = "",
        evidence_ref: str = "",
        evidence_handles: tuple[str, ...] | list[str] = (),
        dimension: str | None = None,
        point: tuple[float, float, float] | None = None,
        region: tuple[float, float, float, float, float, float] | None = None,
    ) -> MemoryRecord:
        handles = _normalized_evidence_handles(evidence_handles)
        values = _validated_memory_values(
            kind=kind,
            source=source,
            title=title,
            content=content,
            subject_key=subject_key,
            evidence_ref=evidence_ref,
            dimension=dimension,
            point=point,
            region=region,
        )
        memory_id = f"memory-{uuid4().hex}"
        now = _utc_now()
        self.register_scope(scope)
        try:
            with self._lock, self._connection:
                self._require_open()
                self._connection.execute(
                    """
                    INSERT INTO memory_entries (
                        memory_id, scope_key, revision, kind, source,
                        subject_key, title, content, evidence_ref, dimension,
                        x, y, z, min_x, min_y, min_z, max_x, max_y, max_z,
                        created_at, updated_at, evidence_handles_json
                    ) VALUES (
                        ?, ?, 1, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        memory_id,
                        scope.key,
                        values["kind"].value,
                        values["source"].value,
                        values["subject_key"],
                        values["title"],
                        values["content"],
                        values["evidence_ref"],
                        values["dimension"],
                        *(_point_columns(values["point"])),
                        *(_region_columns(values["region"])),
                        now,
                        now,
                        json.dumps(list(handles), ensure_ascii=True),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise MemoryStateConflict(
                f"memory subject already exists in scope: {values['subject_key']}"
            ) from exc
        record = self.get_memory(scope, memory_id)
        assert record is not None
        return record

    def get_memory(self, scope: RuntimeScope, memory_id: str) -> MemoryRecord | None:
        with self._lock:
            self._require_open()
            row = self._connection.execute(
                "SELECT * FROM memory_entries WHERE scope_key = ? AND memory_id = ?",
                (scope.key, str(memory_id)),
            ).fetchone()
        return None if row is None else _memory_from_row(row)

    def update_memory(
        self,
        scope: RuntimeScope,
        memory_id: str,
        *,
        expected_revision: int,
        kind: MemoryKind,
        source: MemorySource,
        title: str,
        content: str,
        subject_key: str = "",
        evidence_ref: str = "",
        dimension: str | None = None,
        point: tuple[float, float, float] | None = None,
        region: tuple[float, float, float, float, float, float] | None = None,
    ) -> MemoryRecord:
        current = self.get_memory(scope, memory_id)
        if current is None:
            raise MemoryStateConflict(f"memory not found in scope: {memory_id}")
        if current.revision != int(expected_revision):
            raise MemoryStateConflict(
                f"memory revision conflict: memory_id={memory_id} "
                f"expected={expected_revision} actual={current.revision}"
            )
        values = _validated_memory_values(
            kind=kind,
            source=source,
            title=title,
            content=content,
            subject_key=subject_key,
            evidence_ref=evidence_ref,
            dimension=dimension,
            point=point,
            region=region,
        )
        if _MEMORY_SOURCE_RANK[values["source"]] < _MEMORY_SOURCE_RANK[current.source]:
            raise MemoryStateConflict(
                "lower-trust memory source cannot overwrite a higher-trust entry: "
                f"{current.source.value} -> {values['source'].value}"
            )
        now = _utc_now()
        try:
            with self._lock, self._connection:
                self._require_open()
                cursor = self._connection.execute(
                    """
                    UPDATE memory_entries
                    SET revision = revision + 1, kind = ?, source = ?,
                        subject_key = ?, title = ?, content = ?, evidence_ref = ?,
                        dimension = ?, x = ?, y = ?, z = ?,
                        min_x = ?, min_y = ?, min_z = ?,
                        max_x = ?, max_y = ?, max_z = ?, updated_at = ?
                    WHERE scope_key = ? AND memory_id = ? AND revision = ?
                    """,
                    (
                        values["kind"].value,
                        values["source"].value,
                        values["subject_key"],
                        values["title"],
                        values["content"],
                        values["evidence_ref"],
                        values["dimension"],
                        *(_point_columns(values["point"])),
                        *(_region_columns(values["region"])),
                        now,
                        scope.key,
                        memory_id,
                        expected_revision,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise MemoryStateConflict(
                f"memory subject already exists in scope: {values['subject_key']}"
            ) from exc
        if cursor.rowcount != 1:
            raise MemoryStateConflict(f"memory revision conflict: {memory_id}")
        record = self.get_memory(scope, memory_id)
        assert record is not None
        return record

    def delete_memory(
        self,
        scope: RuntimeScope,
        memory_id: str,
        *,
        expected_revision: int,
    ) -> None:
        with self._lock, self._connection:
            self._require_open()
            cursor = self._connection.execute(
                """
                DELETE FROM memory_entries
                WHERE scope_key = ? AND memory_id = ? AND revision = ?
                """,
                (scope.key, str(memory_id), int(expected_revision)),
            )
        if cursor.rowcount != 1:
            raise MemoryStateConflict(f"memory revision conflict or missing: {memory_id}")

    def supersede_memory(
        self,
        scope: RuntimeScope,
        memory_id: str,
        *,
        superseded_by: str,
        expected_revision: int,
    ) -> MemoryRecord:
        """Retire a memory by pointing at its replacement (F3 §6.2).

        Forgetting is a supersession chain, not a silent delete: the retired
        fact stays resolvable so anything that once cited it can still be
        traced. ``delete_memory`` remains available for genuine removal.
        """

        successor = str(superseded_by).strip()
        if not successor:
            raise MemoryStateConflict("superseded_by must name a replacement memory")
        if successor == str(memory_id):
            raise MemoryStateConflict("a memory cannot supersede itself")
        if self.get_memory(scope, successor) is None:
            raise MemoryStateConflict(f"replacement memory not found: {successor}")
        now = _utc_now()
        with self._lock, self._connection:
            self._require_open()
            cursor = self._connection.execute(
                """
                UPDATE memory_entries
                SET superseded_by = ?, revision = revision + 1, updated_at = ?
                WHERE scope_key = ? AND memory_id = ? AND revision = ?
                """,
                (successor, now, scope.key, str(memory_id), int(expected_revision)),
            )
        if cursor.rowcount != 1:
            raise MemoryStateConflict(f"memory revision conflict or missing: {memory_id}")
        record = self.get_memory(scope, memory_id)
        assert record is not None
        return record

    def search_memories(
        self,
        scope: RuntimeScope,
        *,
        query: str = "",
        kinds: tuple[MemoryKind, ...] = (),
        sources: tuple[MemorySource, ...] = (),
        subject_key: str = "",
        dimension: str | None = None,
        center: tuple[float, float, float] | None = None,
        radius: float | None = None,
        region: tuple[float, float, float, float, float, float] | None = None,
        start: int = 0,
        limit: int = 10,
    ) -> dict[str, object]:
        query_text = _bounded_text(query, max_length=500).strip()
        subject = _bounded_text(subject_key, max_length=256).strip()
        clean_dimension = None if dimension is None else _bounded_text(dimension, max_length=128).strip()
        clean_kinds = tuple(MemoryKind(value) for value in kinds)
        clean_sources = tuple(MemorySource(value) for value in sources)
        clean_center, clean_radius, clean_region = _validated_memory_search_geometry(
            center=center,
            radius=radius,
            region=region,
        )
        start = max(0, int(start))
        limit = max(1, min(50, int(limit)))
        candidate_limit = min(500, max(100, start + limit * 8))
        clauses, params = _memory_filter_sql(
            scope_key=scope.key,
            kinds=clean_kinds,
            sources=clean_sources,
            subject_key=subject,
            dimension=clean_dimension,
            center=clean_center,
            radius=clean_radius,
            region=clean_region,
            alias="e",
        )
        where = " AND ".join(clauses)
        lanes: dict[str, list[MemoryRecord]] = {}
        lane_truncated: dict[str, bool] = {}
        with self._lock:
            self._require_open()
            if query_text:
                terms_query = _memory_terms_query(query_text)
                if terms_query:
                    rows = self._connection.execute(
                        f"""
                        SELECT e.*, bm25(memory_fts_terms, 2.0, 1.0, 3.0) AS rank_score
                        FROM memory_fts_terms
                        JOIN memory_entries e ON e.rowid = memory_fts_terms.rowid
                        WHERE memory_fts_terms MATCH ? AND {where}
                        ORDER BY rank_score, e.memory_id
                        LIMIT ?
                        """,
                        [terms_query, *params, candidate_limit + 1],
                    ).fetchall()
                    lanes["terms"] = [_memory_from_row(row) for row in rows[:candidate_limit]]
                    lane_truncated["terms"] = len(rows) > candidate_limit
                trigram_query = _memory_trigram_query(query_text)
                if trigram_query:
                    rows = self._connection.execute(
                        f"""
                        SELECT e.*, bm25(memory_fts_trigrams, 2.0, 1.0, 3.0) AS rank_score
                        FROM memory_fts_trigrams
                        JOIN memory_entries e ON e.rowid = memory_fts_trigrams.rowid
                        WHERE memory_fts_trigrams MATCH ? AND {where}
                        ORDER BY rank_score, e.memory_id
                        LIMIT ?
                        """,
                        [trigram_query, *params, candidate_limit + 1],
                    ).fetchall()
                    lanes["trigram"] = [_memory_from_row(row) for row in rows[:candidate_limit]]
                    lane_truncated["trigram"] = len(rows) > candidate_limit
                like_clauses, like_params = _memory_like_query(query_text)
                if like_clauses:
                    rows = self._connection.execute(
                        f"""
                        SELECT e.* FROM memory_entries e
                        WHERE {where} AND ({like_clauses})
                        ORDER BY e.memory_id
                        LIMIT ?
                        """,
                        [*params, *like_params, candidate_limit + 1],
                    ).fetchall()
                    lanes["substring"] = [_memory_from_row(row) for row in rows[:candidate_limit]]
                    lane_truncated["substring"] = len(rows) > candidate_limit
            else:
                rows = self._connection.execute(
                    f"""
                    SELECT e.* FROM memory_entries e
                    WHERE {where}
                    ORDER BY e.memory_id
                    LIMIT ?
                    """,
                    [*params, candidate_limit + 1],
                ).fetchall()
                lanes["structured"] = [_memory_from_row(row) for row in rows[:candidate_limit]]
                lane_truncated["structured"] = len(rows) > candidate_limit

        fused = _fuse_memory_lanes(
            lanes,
            center=clean_center,
            radius=clean_radius,
            region=clean_region,
        )
        page = fused[start : start + limit]
        next_start = start + len(page) if start + len(page) < len(fused) else None
        truncated = any(lane_truncated.values())
        return {
            "query": query_text,
            "filters": {
                "kinds": [item.value for item in clean_kinds],
                "sources": [item.value for item in clean_sources],
                "subject_key": subject or None,
                "dimension": clean_dimension,
                "center": None if clean_center is None else list(clean_center),
                "radius": clean_radius,
                "region": None if clean_region is None else list(clean_region),
            },
            "start": start,
            "limit": limit,
            "candidate_count": len(fused),
            "results": [item for item in page],
            "next_start": next_start,
            "complete": next_start is None and not truncated,
            "candidate_truncated": truncated,
            "lanes": {name: len(records) for name, records in lanes.items()},
        }

    def create_skill_head(
        self,
        scope: RuntimeScope,
        *,
        name: str,
        version_digest: str,
        description: str,
        tools: tuple[str, ...],
        body: str,
        evidence_refs: tuple[str, ...],
        change_reason: str,
        derived_from: str = "",
    ) -> tuple[SkillHeadRecord, SkillVersionRecord]:
        clean_name = _strict_required_text("skill_name", name, max_length=64)
        clean_version = _strict_required_text("version_digest", version_digest, max_length=128)
        clean_description = _strict_required_text("description", description, max_length=320)
        clean_body = _strict_required_text("body", body, max_length=8_000)
        clean_reason = _strict_required_text("change_reason", change_reason, max_length=1_000)
        clean_derived = _strict_optional_text("derived_from", derived_from, max_length=256)
        clean_tools = _validated_string_tuple("tools", tools, max_items=64, max_length=128)
        clean_evidence = _validated_string_tuple(
            "evidence_refs", evidence_refs, max_items=32, max_length=1_000
        )
        self.register_scope(scope)
        skill_id = f"skill-{uuid4().hex}"
        now = _utc_now()
        try:
            with self._lock, self._connection:
                self._require_open()
                self._connection.execute(
                    """
                    INSERT INTO skill_heads (
                        skill_id, server_id, bot_id, name, head_revision,
                        head_version, status, origin, derived_from,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 1, ?, 'active', 'learned', ?, ?, ?)
                    """,
                    (
                        skill_id,
                        scope.server_id,
                        scope.bot_id,
                        clean_name,
                        clean_version,
                        clean_derived,
                        now,
                        now,
                    ),
                )
                self._connection.execute(
                    """
                    INSERT INTO skill_versions (
                        skill_id, revision, version_digest, description,
                        tools_json, body, evidence_refs_json, change_reason,
                        created_at
                    ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        skill_id,
                        clean_version,
                        clean_description,
                        _json_dump(list(clean_tools)),
                        clean_body,
                        _json_dump(list(clean_evidence)),
                        clean_reason,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise RuntimeStateConflict(f"Skill name or version already exists: {clean_name}") from exc
        head = self.get_skill_head(scope, clean_name, include_retired=True)
        version = self.get_skill_version(skill_id, version_digest=clean_version)
        if head is None or version is None:
            raise RuntimeStateError("Skill create did not produce a complete record")
        return head, version

    def update_skill_head(
        self,
        scope: RuntimeScope,
        *,
        name: str,
        expected_revision: int,
        version_digest: str,
        description: str,
        tools: tuple[str, ...],
        body: str,
        evidence_refs: tuple[str, ...],
        change_reason: str,
    ) -> tuple[SkillHeadRecord, SkillVersionRecord]:
        head = self.get_skill_head(scope, name, include_retired=True)
        if head is None:
            raise RuntimeStateConflict(f"Skill does not exist: {name}")
        if head.status != "active":
            raise RuntimeStateConflict(f"Skill is retired: {name}")
        if head.head_revision != int(expected_revision):
            raise RuntimeStateConflict(
                f"Skill revision conflict: name={name} expected={expected_revision} "
                f"actual={head.head_revision}"
            )
        clean_version = _strict_required_text("version_digest", version_digest, max_length=128)
        clean_description = _strict_required_text("description", description, max_length=320)
        clean_body = _strict_required_text("body", body, max_length=8_000)
        clean_reason = _strict_required_text("change_reason", change_reason, max_length=1_000)
        clean_tools = _validated_string_tuple("tools", tools, max_items=64, max_length=128)
        clean_evidence = _validated_string_tuple(
            "evidence_refs", evidence_refs, max_items=32, max_length=1_000
        )
        revision = head.head_revision + 1
        now = _utc_now()
        try:
            with self._lock, self._connection:
                self._require_open()
                cursor = self._connection.execute(
                    """
                    UPDATE skill_heads
                    SET head_revision = ?, head_version = ?, updated_at = ?
                    WHERE skill_id = ? AND status = 'active' AND head_revision = ?
                    """,
                    (revision, clean_version, now, head.skill_id, int(expected_revision)),
                )
                if cursor.rowcount != 1:
                    raise RuntimeStateConflict(f"Skill revision changed concurrently: {name}")
                self._connection.execute(
                    """
                    INSERT INTO skill_versions (
                        skill_id, revision, version_digest, description,
                        tools_json, body, evidence_refs_json, change_reason,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        head.skill_id,
                        revision,
                        clean_version,
                        clean_description,
                        _json_dump(list(clean_tools)),
                        clean_body,
                        _json_dump(list(clean_evidence)),
                        clean_reason,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise RuntimeStateConflict(f"Skill version already exists: {clean_version}") from exc
        updated = self.get_skill_head(scope, name, include_retired=True)
        version = self.get_skill_version(head.skill_id, revision=revision)
        if updated is None or version is None:
            raise RuntimeStateError("Skill update did not produce a complete record")
        return updated, version

    def retire_skill_head(
        self,
        scope: RuntimeScope,
        *,
        name: str,
        expected_revision: int,
        evidence_refs: tuple[str, ...],
        reason: str,
    ) -> SkillHeadRecord:
        now = _utc_now()
        clean_evidence = _validated_string_tuple(
            "evidence_refs", evidence_refs, max_items=32, max_length=1_000
        )
        clean_reason = _strict_required_text("reason", reason, max_length=1_000)
        with self._lock, self._connection:
            self._require_open()
            cursor = self._connection.execute(
                """
                UPDATE skill_heads
                SET status = 'retired', retired_at = ?,
                    retirement_evidence_refs_json = ?, retirement_reason = ?,
                    updated_at = ?
                WHERE server_id = ? AND bot_id = ? AND name = ?
                  AND status = 'active' AND head_revision = ?
                """,
                (
                    now,
                    _json_dump(list(clean_evidence)),
                    clean_reason,
                    now,
                    scope.server_id,
                    scope.bot_id,
                    name,
                    int(expected_revision),
                ),
            )
            if cursor.rowcount != 1:
                current = self._connection.execute(
                    """
                    SELECT status, head_revision FROM skill_heads
                    WHERE server_id = ? AND bot_id = ? AND name = ?
                    """,
                    (scope.server_id, scope.bot_id, name),
                ).fetchone()
                if current is None:
                    raise RuntimeStateConflict(f"Skill does not exist: {name}")
                raise RuntimeStateConflict(
                    f"Skill retire conflict: name={name} expected={expected_revision} "
                    f"actual={current['head_revision']} status={current['status']}"
                )
        record = self.get_skill_head(scope, name, include_retired=True)
        if record is None:
            raise RuntimeStateError("Skill retire lost the head record")
        return record

    def get_skill_head(
        self,
        scope: RuntimeScope,
        name: str,
        *,
        include_retired: bool = False,
    ) -> SkillHeadRecord | None:
        clauses = ["server_id = ?", "bot_id = ?", "name = ?"]
        params: list[object] = [scope.server_id, scope.bot_id, str(name)]
        if not include_retired:
            clauses.append("status = 'active'")
        with self._lock:
            self._require_open()
            row = self._connection.execute(
                f"SELECT * FROM skill_heads WHERE {' AND '.join(clauses)}",
                params,
            ).fetchone()
        return None if row is None else _skill_head_from_row(row)

    def list_skill_heads(
        self,
        scope: RuntimeScope,
        *,
        include_retired: bool = False,
    ) -> tuple[SkillHeadRecord, ...]:
        clauses = ["server_id = ?", "bot_id = ?"]
        params: list[object] = [scope.server_id, scope.bot_id]
        if not include_retired:
            clauses.append("status = 'active'")
        with self._lock:
            self._require_open()
            rows = self._connection.execute(
                f"""
                SELECT * FROM skill_heads
                WHERE {' AND '.join(clauses)}
                ORDER BY name, skill_id
                """,
                params,
            ).fetchall()
        return tuple(_skill_head_from_row(row) for row in rows)

    def get_skill_version(
        self,
        skill_id: str,
        *,
        revision: int | None = None,
        version_digest: str | None = None,
    ) -> SkillVersionRecord | None:
        clauses = ["skill_id = ?"]
        params: list[object] = [str(skill_id)]
        if revision is not None:
            clauses.append("revision = ?")
            params.append(int(revision))
        if version_digest is not None:
            clauses.append("version_digest = ?")
            params.append(str(version_digest))
        with self._lock:
            self._require_open()
            row = self._connection.execute(
                f"SELECT * FROM skill_versions WHERE {' AND '.join(clauses)} ORDER BY revision DESC LIMIT 1",
                params,
            ).fetchone()
        return None if row is None else _skill_version_from_row(row)

    def record_skill_activation(
        self,
        scope: RuntimeScope,
        *,
        skill_name: str,
        skill_version: str,
        skill_id: str | None = None,
        owner_kind: str | None = None,
        owner_id: str | None = None,
        task_id: str | None = None,
    ) -> SkillActivationRecord:
        clean_name = _required_text("skill_name", skill_name, max_length=128)
        clean_version = _required_text("skill_version", skill_version, max_length=128)
        clean_task_id = None if task_id is None else _required_text("task_id", task_id, max_length=128)
        clean_kind = str(owner_kind or ("task" if clean_task_id else "turn"))
        if clean_kind not in {"turn", "task", "maintenance", "legacy_scope"}:
            raise ValueError(f"invalid Skill activation owner kind: {clean_kind}")
        clean_owner = _required_text(
            "owner_id",
            owner_id or clean_task_id or f"turn-{uuid4().hex}",
            max_length=256,
        )
        clean_skill_id = _required_text(
            "skill_id", skill_id or f"builtin:{clean_name}", max_length=256
        )
        self.register_scope(scope)
        if clean_task_id is not None:
            with self._lock:
                task = self._connection.execute(
                    "SELECT scope_key FROM tasks WHERE task_id = ?",
                    (clean_task_id,),
                ).fetchone()
            if task is None or str(task["scope_key"]) != scope.key:
                raise RuntimeStateConflict(
                    f"skill activation task is not in runtime scope: {clean_task_id}"
                )
        activation_id = f"skill-activation-{uuid4().hex}"
        now = _utc_now()
        with self._lock, self._connection:
            self._require_open()
            self._connection.execute(
                """
                INSERT OR IGNORE INTO skill_activations (
                    activation_id, scope_key, task_id, owner_kind, owner_id,
                    skill_id, skill_name, skill_version, activated_at, ended_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    activation_id,
                    scope.key,
                    clean_task_id,
                    clean_kind,
                    clean_owner,
                    clean_skill_id,
                    clean_name,
                    clean_version,
                    now,
                ),
            )
            row = self._connection.execute(
                """
                SELECT * FROM skill_activations
                WHERE scope_key = ? AND owner_kind = ? AND owner_id = ?
                  AND skill_name = ? AND skill_version = ? AND ended_at IS NULL
                """,
                (scope.key, clean_kind, clean_owner, clean_name, clean_version),
            ).fetchone()
        if row is None:
            raise RuntimeStateError("skill activation insert did not produce a record")
        return _skill_activation_from_row(row)

    def list_skill_activations(
        self,
        scope: RuntimeScope,
        *,
        task_id: str | None = None,
        include_scope_activations: bool = True,
        include_ended: bool = False,
        owner_kind: str | None = None,
        owner_id: str | None = None,
    ) -> tuple[SkillActivationRecord, ...]:
        clauses = ["scope_key = ?"]
        params: list[object] = [scope.key]
        if not include_ended:
            clauses.append("ended_at IS NULL")
        if task_id is not None:
            if include_scope_activations:
                clauses.append("(task_id = ? OR owner_kind = 'legacy_scope')")
            else:
                clauses.append("task_id = ?")
            params.append(str(task_id))
        elif not include_scope_activations:
            clauses.append("task_id IS NOT NULL")
        if owner_kind is not None:
            clauses.append("owner_kind = ?")
            params.append(str(owner_kind))
        if owner_id is not None:
            clauses.append("owner_id = ?")
            params.append(str(owner_id))
        with self._lock:
            self._require_open()
            rows = self._connection.execute(
                f"""
                SELECT * FROM skill_activations
                WHERE {' AND '.join(clauses)}
                ORDER BY activated_at, activation_id
                """,
                params,
            ).fetchall()
        return tuple(_skill_activation_from_row(row) for row in rows)

    def end_skill_activation_owner(
        self,
        scope: RuntimeScope,
        *,
        owner_kind: str,
        owner_id: str,
    ) -> int:
        now = _utc_now()
        with self._lock, self._connection:
            self._require_open()
            cursor = self._connection.execute(
                """
                UPDATE skill_activations SET ended_at = ?
                WHERE scope_key = ? AND owner_kind = ? AND owner_id = ?
                  AND ended_at IS NULL
                """,
                (now, scope.key, str(owner_kind), str(owner_id)),
            )
        return int(cursor.rowcount)

    def end_skill_activations_for_name(
        self,
        scope: RuntimeScope,
        *,
        owner_kind: str,
        owner_id: str,
        skill_name: str,
        except_version: str | None = None,
    ) -> int:
        clauses = [
            "scope_key = ?",
            "owner_kind = ?",
            "owner_id = ?",
            "skill_name = ?",
            "ended_at IS NULL",
        ]
        params: list[object] = [
            scope.key,
            str(owner_kind),
            str(owner_id),
            str(skill_name),
        ]
        if except_version is not None:
            clauses.append("skill_version != ?")
            params.append(str(except_version))
        now = _utc_now()
        with self._lock, self._connection:
            self._require_open()
            cursor = self._connection.execute(
                f"UPDATE skill_activations SET ended_at = ? WHERE {' AND '.join(clauses)}",
                [now, *params],
            )
        return int(cursor.rowcount)

    def end_task_skill_activations(self, scope: RuntimeScope, task_id: str) -> int:
        return self.end_skill_activation_owner(
            scope,
            owner_kind="task",
            owner_id=str(task_id),
        )

    def end_transient_skill_activations(self, scope: RuntimeScope) -> int:
        now = _utc_now()
        with self._lock, self._connection:
            self._require_open()
            cursor = self._connection.execute(
                """
                UPDATE skill_activations SET ended_at = ?
                WHERE scope_key = ? AND owner_kind IN ('turn', 'maintenance')
                  AND ended_at IS NULL
                """,
                (now, scope.key),
            )
        return int(cursor.rowcount)

    def end_terminal_task_skill_activations(self, scope: RuntimeScope) -> int:
        now = _utc_now()
        with self._lock, self._connection:
            self._require_open()
            cursor = self._connection.execute(
                """
                UPDATE skill_activations SET ended_at = ?
                WHERE scope_key = ? AND owner_kind = 'task' AND ended_at IS NULL
                  AND task_id IN (
                      SELECT task_id FROM tasks
                      WHERE status IN ('completed', 'cancelled', 'failed')
                  )
                """,
                (now, scope.key),
            )
        return int(cursor.rowcount)

    def get_wiki_cache(self, cache_key: str) -> WikiCacheRecord | None:
        with self._lock:
            self._require_open()
            row = self._connection.execute(
                "SELECT * FROM wiki_cache WHERE cache_key = ?",
                (str(cache_key),),
            ).fetchone()
        return None if row is None else _wiki_cache_from_row(row)

    def put_wiki_cache(
        self,
        *,
        cache_key: str,
        endpoint: str,
        kind: str,
        request_key: str,
        payload: dict[str, object],
        fetched_at: str,
        expires_at: str,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> WikiCacheRecord:
        clean_key = _required_text("cache_key", cache_key, max_length=128)
        clean_endpoint = _required_text("endpoint", endpoint, max_length=1000)
        clean_kind = _required_text("kind", kind, max_length=32)
        clean_request = _required_text("request_key", request_key, max_length=1000)
        clean_fetched = _required_text("fetched_at", fetched_at, max_length=64)
        clean_expires = _required_text("expires_at", expires_at, max_length=64)
        payload_json = _json_dump(payload)
        with self._lock, self._connection:
            self._require_open()
            self._connection.execute(
                """
                INSERT INTO wiki_cache (
                    cache_key, endpoint, kind, request_key, payload_json,
                    etag, last_modified, fetched_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    endpoint = excluded.endpoint,
                    kind = excluded.kind,
                    request_key = excluded.request_key,
                    payload_json = excluded.payload_json,
                    etag = excluded.etag,
                    last_modified = excluded.last_modified,
                    fetched_at = excluded.fetched_at,
                    expires_at = excluded.expires_at
                """,
                (
                    clean_key,
                    clean_endpoint,
                    clean_kind,
                    clean_request,
                    payload_json,
                    None if etag is None else _bounded_text(etag, max_length=1000),
                    (
                        None
                        if last_modified is None
                        else _bounded_text(last_modified, max_length=1000)
                    ),
                    clean_fetched,
                    clean_expires,
                ),
            )
        record = self.get_wiki_cache(clean_key)
        assert record is not None
        return record

    def refresh_wiki_cache_expiry(
        self,
        cache_key: str,
        *,
        fetched_at: str,
        expires_at: str,
    ) -> WikiCacheRecord | None:
        with self._lock, self._connection:
            self._require_open()
            self._connection.execute(
                """
                UPDATE wiki_cache SET fetched_at = ?, expires_at = ?
                WHERE cache_key = ?
                """,
                (str(fetched_at), str(expires_at), str(cache_key)),
            )
        return self.get_wiki_cache(cache_key)

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
                    continuation_json TEXT,
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

                CREATE TABLE IF NOT EXISTS continuation_approaches (
                    scope_key TEXT NOT NULL REFERENCES runtime_scopes(scope_key) ON DELETE CASCADE,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                    approach_key TEXT NOT NULL,
                    budget_limit INTEGER NOT NULL,
                    consumed_epochs INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(scope_key, task_id, approach_key)
                );

                CREATE TABLE IF NOT EXISTS continuation_settlements (
                    checkpoint_id TEXT PRIMARY KEY REFERENCES task_checkpoints(checkpoint_id) ON DELETE CASCADE,
                    scope_key TEXT NOT NULL REFERENCES runtime_scopes(scope_key) ON DELETE CASCADE,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                    approach_key TEXT NOT NULL,
                    consumed_epochs INTEGER NOT NULL,
                    settled_at TEXT NOT NULL
                );

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

                CREATE TABLE IF NOT EXISTS progress_epochs (
                    cursor INTEGER PRIMARY KEY AUTOINCREMENT,
                    epoch_id TEXT NOT NULL UNIQUE,
                    scope_key TEXT NOT NULL REFERENCES runtime_scopes(scope_key) ON DELETE CASCADE,
                    run_id TEXT NOT NULL,
                    model_turn INTEGER NOT NULL,
                    members_json TEXT NOT NULL,
                    pre_body_fingerprint TEXT,
                    post_body_fingerprint TEXT,
                    evidence_refs_json TEXT NOT NULL,
                    epistemic_keys_json TEXT NOT NULL,
                    novel_epistemic_keys_json TEXT NOT NULL,
                    material_changed INTEGER NOT NULL,
                    progress_aborted INTEGER NOT NULL,
                    settled_at TEXT NOT NULL,
                    UNIQUE(scope_key, run_id, model_turn)
                );

                CREATE INDEX IF NOT EXISTS idx_progress_epochs_scope_cursor
                ON progress_epochs(scope_key, cursor);

                CREATE TABLE IF NOT EXISTS progress_evidence (
                    scope_key TEXT NOT NULL REFERENCES runtime_scopes(scope_key) ON DELETE CASCADE,
                    evidence_key TEXT NOT NULL,
                    first_epoch_cursor INTEGER NOT NULL REFERENCES progress_epochs(cursor) ON DELETE CASCADE,
                    last_epoch_cursor INTEGER NOT NULL REFERENCES progress_epochs(cursor) ON DELETE CASCADE,
                    seen_count INTEGER NOT NULL,
                    last_observation_handle TEXT,
                    PRIMARY KEY(scope_key, evidence_key)
                );

                CREATE TABLE IF NOT EXISTS exploration_coverage (
                    cursor INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope_key TEXT NOT NULL REFERENCES runtime_scopes(scope_key) ON DELETE CASCADE,
                    dimension TEXT NOT NULL,
                    query_signature TEXT NOT NULL,
                    region_x INTEGER NOT NULL,
                    region_z INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN (
                        'covered', 'found', 'mobility_blocked', 'unloaded_boundary'
                    )),
                    center_json TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    observations_json TEXT NOT NULL,
                    negative_evidence_json TEXT NOT NULL,
                    uncertainty_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_exploration_coverage_scope_query
                ON exploration_coverage(
                    scope_key, dimension, query_signature, cursor
                );

                CREATE TABLE IF NOT EXISTS memory_entries (
                    memory_id TEXT PRIMARY KEY,
                    scope_key TEXT NOT NULL REFERENCES runtime_scopes(scope_key) ON DELETE CASCADE,
                    revision INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    source TEXT NOT NULL,
                    subject_key TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    evidence_ref TEXT NOT NULL,
                    dimension TEXT,
                    x REAL,
                    y REAL,
                    z REAL,
                    min_x REAL,
                    min_y REAL,
                    min_z REAL,
                    max_x REAL,
                    max_y REAL,
                    max_z REAL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    evidence_handles_json TEXT NOT NULL DEFAULT '[]',
                    superseded_by TEXT
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_subject
                ON memory_entries(scope_key, subject_key)
                WHERE subject_key <> '';

                CREATE INDEX IF NOT EXISTS idx_memory_scope_kind_source
                ON memory_entries(scope_key, kind, source, memory_id);

                CREATE INDEX IF NOT EXISTS idx_memory_scope_dimension_point
                ON memory_entries(scope_key, dimension, x, y, z);

                CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts_terms USING fts5(
                    title,
                    content,
                    subject_key,
                    content='memory_entries',
                    content_rowid='rowid',
                    tokenize='porter unicode61 remove_diacritics 2'
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts_trigrams USING fts5(
                    title,
                    content,
                    subject_key,
                    content='memory_entries',
                    content_rowid='rowid',
                    tokenize='trigram'
                );

                CREATE TRIGGER IF NOT EXISTS memory_entries_ai AFTER INSERT ON memory_entries BEGIN
                    INSERT INTO memory_fts_terms(rowid, title, content, subject_key)
                    VALUES (new.rowid, new.title, new.content, new.subject_key);
                    INSERT INTO memory_fts_trigrams(rowid, title, content, subject_key)
                    VALUES (new.rowid, new.title, new.content, new.subject_key);
                END;

                CREATE TRIGGER IF NOT EXISTS memory_entries_ad AFTER DELETE ON memory_entries BEGIN
                    INSERT INTO memory_fts_terms(memory_fts_terms, rowid, title, content, subject_key)
                    VALUES ('delete', old.rowid, old.title, old.content, old.subject_key);
                    INSERT INTO memory_fts_trigrams(memory_fts_trigrams, rowid, title, content, subject_key)
                    VALUES ('delete', old.rowid, old.title, old.content, old.subject_key);
                END;

                CREATE TRIGGER IF NOT EXISTS memory_entries_au AFTER UPDATE ON memory_entries BEGIN
                    INSERT INTO memory_fts_terms(memory_fts_terms, rowid, title, content, subject_key)
                    VALUES ('delete', old.rowid, old.title, old.content, old.subject_key);
                    INSERT INTO memory_fts_terms(rowid, title, content, subject_key)
                    VALUES (new.rowid, new.title, new.content, new.subject_key);
                    INSERT INTO memory_fts_trigrams(memory_fts_trigrams, rowid, title, content, subject_key)
                    VALUES ('delete', old.rowid, old.title, old.content, old.subject_key);
                    INSERT INTO memory_fts_trigrams(rowid, title, content, subject_key)
                    VALUES (new.rowid, new.title, new.content, new.subject_key);
                END;

                CREATE TABLE IF NOT EXISTS skill_heads (
                    skill_id TEXT PRIMARY KEY,
                    server_id TEXT NOT NULL,
                    bot_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    head_revision INTEGER NOT NULL,
                    head_version TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('active', 'retired')),
                    origin TEXT NOT NULL CHECK(origin = 'learned'),
                    derived_from TEXT NOT NULL DEFAULT '',
                    retired_at TEXT,
                    retirement_evidence_refs_json TEXT NOT NULL DEFAULT '[]',
                    retirement_reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(server_id, bot_id, name)
                );

                CREATE INDEX IF NOT EXISTS idx_skill_heads_owner_status
                ON skill_heads(server_id, bot_id, status, name);

                CREATE TABLE IF NOT EXISTS skill_versions (
                    skill_id TEXT NOT NULL REFERENCES skill_heads(skill_id) ON DELETE RESTRICT,
                    revision INTEGER NOT NULL,
                    version_digest TEXT NOT NULL,
                    description TEXT NOT NULL,
                    tools_json TEXT NOT NULL,
                    body TEXT NOT NULL,
                    evidence_refs_json TEXT NOT NULL,
                    change_reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(skill_id, revision),
                    UNIQUE(skill_id, version_digest)
                );

                CREATE TABLE IF NOT EXISTS skill_activations (
                    activation_id TEXT PRIMARY KEY,
                    scope_key TEXT NOT NULL REFERENCES runtime_scopes(scope_key) ON DELETE CASCADE,
                    task_id TEXT REFERENCES tasks(task_id) ON DELETE SET NULL,
                    owner_kind TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    skill_id TEXT NOT NULL,
                    skill_name TEXT NOT NULL,
                    skill_version TEXT NOT NULL,
                    activated_at TEXT NOT NULL,
                    ended_at TEXT
                );

                CREATE TABLE IF NOT EXISTS wiki_cache (
                    cache_key TEXT PRIMARY KEY,
                    endpoint TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    request_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    etag TEXT,
                    last_modified TEXT,
                    fetched_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_wiki_cache_endpoint_kind
                ON wiki_cache(endpoint, kind, request_key);
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
            self._ensure_current_skill_indexes()

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
            elif current == 7:
                current = 8
            elif current == 8:
                current = 9
            elif current == 9:
                current = 10
            elif current == 10:
                columns = {
                    str(row["name"])
                    for row in self._connection.execute(
                        "PRAGMA table_info(skill_activations)"
                    ).fetchall()
                }
                for name, declaration in (
                    ("owner_kind", "TEXT"),
                    ("owner_id", "TEXT"),
                    ("skill_id", "TEXT"),
                    ("ended_at", "TEXT"),
                ):
                    if name not in columns:
                        self._connection.execute(
                            f"ALTER TABLE skill_activations ADD COLUMN {name} {declaration}"
                        )
                self._connection.execute(
                    """
                    UPDATE skill_activations
                    SET owner_kind = CASE WHEN task_id IS NULL THEN 'legacy_scope' ELSE 'task' END,
                        owner_id = CASE WHEN task_id IS NULL THEN scope_key ELSE task_id END,
                        skill_id = 'legacy:' || skill_name
                    WHERE owner_kind IS NULL OR owner_id IS NULL OR skill_id IS NULL
                    """
                )
                self._connection.execute(
                    """
                    UPDATE skill_activations
                    SET ended_at = activated_at
                    WHERE ended_at IS NULL AND owner_kind = 'legacy_scope'
                    """
                )
                self._connection.execute(
                    """
                    UPDATE skill_activations
                    SET ended_at = activated_at
                    WHERE ended_at IS NULL AND owner_kind = 'task'
                      AND task_id IN (
                          SELECT task_id FROM tasks
                          WHERE status IN ('completed', 'cancelled', 'failed')
                      )
                    """
                )
                current = 11
            elif current == 11:
                self._connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS progress_epochs (
                        cursor INTEGER PRIMARY KEY AUTOINCREMENT,
                        epoch_id TEXT NOT NULL UNIQUE,
                        scope_key TEXT NOT NULL REFERENCES runtime_scopes(scope_key) ON DELETE CASCADE,
                        run_id TEXT NOT NULL,
                        model_turn INTEGER NOT NULL,
                        members_json TEXT NOT NULL,
                        pre_body_fingerprint TEXT,
                        post_body_fingerprint TEXT,
                        evidence_refs_json TEXT NOT NULL,
                        epistemic_keys_json TEXT NOT NULL,
                        novel_epistemic_keys_json TEXT NOT NULL,
                        material_changed INTEGER NOT NULL,
                        progress_aborted INTEGER NOT NULL,
                        settled_at TEXT NOT NULL,
                        UNIQUE(scope_key, run_id, model_turn)
                    );
                    CREATE INDEX IF NOT EXISTS idx_progress_epochs_scope_cursor
                    ON progress_epochs(scope_key, cursor);
                    CREATE TABLE IF NOT EXISTS progress_evidence (
                        scope_key TEXT NOT NULL REFERENCES runtime_scopes(scope_key) ON DELETE CASCADE,
                        evidence_key TEXT NOT NULL,
                        first_epoch_cursor INTEGER NOT NULL REFERENCES progress_epochs(cursor) ON DELETE CASCADE,
                        last_epoch_cursor INTEGER NOT NULL REFERENCES progress_epochs(cursor) ON DELETE CASCADE,
                        seen_count INTEGER NOT NULL,
                        last_observation_handle TEXT,
                        PRIMARY KEY(scope_key, evidence_key)
                    );
                    """
                )
                current = 12
            elif current == 12:
                columns = {
                    str(row["name"])
                    for row in self._connection.execute(
                        "PRAGMA table_info(task_checkpoints)"
                    ).fetchall()
                }
                if "continuation_json" not in columns:
                    self._connection.execute(
                        "ALTER TABLE task_checkpoints ADD COLUMN continuation_json TEXT"
                    )
                current = 13
            elif current == 13:
                self._connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS continuation_approaches (
                        scope_key TEXT NOT NULL REFERENCES runtime_scopes(scope_key) ON DELETE CASCADE,
                        task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                        approach_key TEXT NOT NULL,
                        budget_limit INTEGER NOT NULL,
                        consumed_epochs INTEGER NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY(scope_key, task_id, approach_key)
                    );
                    CREATE TABLE IF NOT EXISTS continuation_settlements (
                        checkpoint_id TEXT PRIMARY KEY REFERENCES task_checkpoints(checkpoint_id) ON DELETE CASCADE,
                        scope_key TEXT NOT NULL REFERENCES runtime_scopes(scope_key) ON DELETE CASCADE,
                        task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                        approach_key TEXT NOT NULL,
                        consumed_epochs INTEGER NOT NULL,
                        settled_at TEXT NOT NULL
                    );
                    """
                )
                current = 14
            elif current == 14:
                self._connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS exploration_coverage (
                        cursor INTEGER PRIMARY KEY AUTOINCREMENT,
                        scope_key TEXT NOT NULL REFERENCES runtime_scopes(scope_key) ON DELETE CASCADE,
                        dimension TEXT NOT NULL,
                        query_signature TEXT NOT NULL,
                        region_x INTEGER NOT NULL,
                        region_z INTEGER NOT NULL,
                        status TEXT NOT NULL CHECK(status IN (
                            'covered', 'found', 'mobility_blocked', 'unloaded_boundary'
                        )),
                        center_json TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        observations_json TEXT NOT NULL,
                        negative_evidence_json TEXT NOT NULL,
                        uncertainty_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_exploration_coverage_scope_query
                    ON exploration_coverage(
                        scope_key, dimension, query_signature, cursor
                    );
                    """
                )
                current = 15
            elif current == 15:
                # F3 §6.2: plural evidence handles + supersession chain.
                columns = {
                    str(row["name"])
                    for row in self._connection.execute(
                        "PRAGMA table_info(memory_entries)"
                    ).fetchall()
                }
                if "evidence_handles_json" not in columns:
                    self._connection.execute(
                        "ALTER TABLE memory_entries "
                        "ADD COLUMN evidence_handles_json TEXT NOT NULL DEFAULT '[]'"
                    )
                if "superseded_by" not in columns:
                    self._connection.execute(
                        "ALTER TABLE memory_entries ADD COLUMN superseded_by TEXT"
                    )
                current = 16
            else:
                raise RuntimeStateError(
                    f"no runtime schema migration from version {current}"
                )
            self._connection.execute(
                "UPDATE minebot_schema SET version = ? WHERE singleton = 1",
                (current,),
            )

    def _ensure_current_skill_indexes(self) -> None:
        self._connection.execute("DROP INDEX IF EXISTS idx_skill_activation_version")
        self._connection.execute("DROP INDEX IF EXISTS idx_skill_activations_task")
        self._connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_skill_activation_owner_version
            ON skill_activations(
                scope_key, owner_kind, owner_id, skill_name, skill_version
            ) WHERE ended_at IS NULL
            """
        )
        self._connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_skill_activations_task_active
            ON skill_activations(scope_key, task_id, ended_at, activated_at)
            """
        )

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeStateError("runtime state store is closed")


__all__ = [
    "DEFAULT_RUNTIME_STATE_DB",
    "RUNTIME_SCHEMA_VERSION",
    "CheckpointDisposition",
    "CompletionAuthority",
    "MemoryKind",
    "MemoryRecord",
    "MemorySource",
    "MemoryStateConflict",
    "PlanStepRecord",
    "PlanStepStatus",
    "RuntimeScope",
    "RuntimeStateConflict",
    "RuntimeStateError",
    "RuntimeStateStore",
    "SkillActivationRecord",
    "SkillHeadRecord",
    "SkillVersionRecord",
    "TaskCheckpointRecord",
    "TaskPlanRecord",
    "TaskRecord",
    "TaskStatus",
    "WikiCacheRecord",
    "memory_record_payload",
    "skill_activation_payload",
]
