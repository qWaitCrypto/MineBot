"""Persistent task workspace and governed task-artifact tools."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass

from minebot.app.runtime_state import (
    CheckpointDisposition,
    CompletionAuthority,
    ContinuationContract,
    ContinuationOperationClass,
    RuntimeScope,
    RuntimeStateConflict,
    RuntimeStateStore,
    TaskCheckpointRecord,
    TaskPlanRecord,
    TaskRecord,
    TaskStatus,
    skill_activation_payload,
)
from minebot.brain.context import AgentContext
from minebot.brain.registry import RegisteredTool, ToolRegistry, ToolSidecar
from minebot.contract import ToolResult


@dataclass
class TaskWorkspace:
    """Scoped facade over durable task, plan, and checkpoint state."""

    store: RuntimeStateStore
    scope: RuntimeScope

    @property
    def current_task(self) -> TaskRecord | None:
        return self.store.get_foreground_task(self.scope)

    def start(self, goal_text: str, *, source: str, requested_by: str = "") -> TaskRecord:
        return self.store.create_task(
            self.scope,
            goal_text=goal_text,
            source=source,
            requested_by=requested_by,
        )

    def replace(self, goal_text: str, *, source: str, requested_by: str = "") -> TaskRecord:
        return self.store.replace_foreground_task(
            self.scope,
            goal_text=goal_text,
            source=source,
            requested_by=requested_by,
        )

    def pause(self) -> TaskRecord | None:
        task = self.current_task
        if task is None:
            return None
        return self.store.transition_task(
            task.task_id,
            expected_revision=task.revision,
            status=TaskStatus.PAUSED,
        )

    def resume(self) -> TaskRecord | None:
        task = self.current_task
        if task is None:
            return None
        return self.store.transition_task(
            task.task_id,
            expected_revision=task.revision,
            status=TaskStatus.RUNNING,
        )

    def cancel(self) -> TaskRecord | None:
        task = self.current_task
        if task is None:
            return None
        return self.store.transition_task(
            task.task_id,
            expected_revision=task.revision,
            status=TaskStatus.CANCELLED,
            completion_authority=CompletionAuthority.HUMAN,
        )

    def complete(self, *, authority: CompletionAuthority) -> TaskRecord | None:
        task = self.current_task
        if task is None:
            return None
        return self.store.transition_task(
            task.task_id,
            expected_revision=task.revision,
            status=TaskStatus.COMPLETED,
            completion_authority=authority,
        )

    def park_without_continuation(
        self,
        *,
        body_fingerprint: dict[str, object] | None,
    ) -> tuple[TaskRecord, TaskCheckpointRecord] | None:
        task = self.current_task
        if task is None or task.status is not TaskStatus.RUNNING:
            return None
        return self.checkpoint(
            expected_task_revision=task.revision,
            disposition=CheckpointDisposition.WAIT_EVENT,
            summary="model_final_without_continuation",
            wait_for=("user_input_or_material_body_event",),
            body_fingerprint=body_fingerprint,
        )

    def reject_continuation(
        self,
        *,
        reason: str,
        evidence: list[str] | tuple[str, ...],
        body_fingerprint: dict[str, object] | None,
    ) -> tuple[TaskRecord, TaskCheckpointRecord] | None:
        task = self.current_task
        if task is None:
            return None
        return self.checkpoint(
            expected_task_revision=task.revision,
            disposition=CheckpointDisposition.YIELD,
            summary=reason,
            evidence=evidence,
            body_fingerprint=body_fingerprint,
        )

    def update_plan(
        self,
        *,
        expected_revision: int,
        summary: str,
        steps: list[dict[str, object]],
    ) -> TaskPlanRecord:
        task = self._require_task()
        return self.store.update_plan(
            task.task_id,
            expected_revision=expected_revision,
            summary=summary,
            steps=steps,
        )

    def checkpoint(
        self,
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
        task = self._require_task()
        if (disposition is CheckpointDisposition.CONTINUE) != (continuation is not None):
            raise ValueError("continue checkpoints require exactly one continuation contract")
        if task.revision != expected_task_revision:
            raise RuntimeStateConflict(
                f"task revision conflict: task_id={task.task_id} "
                f"expected={expected_task_revision} actual={task.revision}"
            )
        return self.store.create_checkpoint(
            task.task_id,
            expected_task_revision=expected_task_revision,
            disposition=disposition,
            summary=summary,
            next_step=next_step,
            evidence=evidence,
            wait_for=wait_for,
            body_fingerprint=body_fingerprint,
            continuation=continuation,
        )

    def payload(self) -> dict[str, object]:
        task = self.current_task
        if task is None:
            return {"active": False, "scope_key": self.scope.key}
        plan = self.store.get_plan(task.task_id)
        checkpoint = self.store.get_latest_checkpoint(task.task_id)
        skills = self.store.list_skill_activations(
            self.scope,
            task_id=task.task_id,
            include_scope_activations=True,
        )
        return {
            "active": True,
            "task": _task_payload(task),
            "plan": None if plan is None else _plan_payload(plan),
            "checkpoint": None if checkpoint is None else _checkpoint_payload(checkpoint),
            "skills": [skill_activation_payload(record) for record in skills],
        }

    def sync_context(self, context: AgentContext) -> None:
        context.observe_task(self.payload())

    @property
    def completion_requested(self) -> bool:
        task = self.current_task
        if task is None:
            return False
        checkpoint = self.store.get_latest_checkpoint(task.task_id)
        return (
            checkpoint is not None
            and checkpoint.disposition is CheckpointDisposition.COMPLETE
        )

    def _require_task(self) -> TaskRecord:
        task = self.current_task
        if task is None:
            raise RuntimeStateConflict("no active task")
        return task


def register_task_tools(
    registry: ToolRegistry,
    workspace: TaskWorkspace,
    *,
    body_fingerprint: Callable[[], dict[str, object]] | None = None,
    evidence_cursor: Callable[[], int] | None = None,
    generation: Callable[[], int] | None = None,
) -> None:
    registry.register(_read_task_tool(workspace))
    registry.register(_update_plan_tool(workspace))
    registry.register(
        _checkpoint_task_tool(
            workspace,
            body_fingerprint=body_fingerprint,
            evidence_cursor=evidence_cursor,
            generation=generation,
        )
    )


def _read_task_tool(workspace: TaskWorkspace) -> RegisteredTool:
    return RegisteredTool(
        "read_task",
        "Read the current durable task, versioned plan, and latest checkpoint. Ordinary chat has no task.",
        {"type": "object", "properties": {}, "additionalProperties": False},
        lambda _params: ToolResult(
            success=True,
            reason="task_read",
            can_retry=False,
            metrics=workspace.payload(),
        ),
        ToolSidecar(
            "read_task",
            mutating=False,
            source="agent.task",
            tool_type="task_state",
            permission="read_task",
            body_scope=(),
            terminal_truth=("TaskRecord", "TaskPlanRecord", "TaskCheckpointRecord"),
        ),
    )


def _update_plan_tool(workspace: TaskWorkspace) -> RegisteredTool:
    def update(params: dict[str, object]) -> ToolResult:
        try:
            plan = workspace.update_plan(
                expected_revision=int(params.get("expected_revision") or 0),
                summary=str(params.get("summary") or ""),
                steps=list(params.get("steps") or []),
            )
        except (RuntimeStateConflict, ValueError) as exc:
            return ToolResult(
                success=False,
                reason="task_plan_update_rejected",
                can_retry=True,
                metrics={"error": str(exc), "current": workspace.payload()},
            )
        return ToolResult(
            success=True,
            reason="task_plan_updated",
            can_retry=False,
            metrics={"plan": _plan_payload(plan), "current": workspace.payload()},
        )

    return RegisteredTool(
        "update_plan",
        "Create or revise the inspectable plan for the current durable task. This records steps, not hidden reasoning and not a second execution graph.",
        {
            "type": "object",
            "properties": {
                "expected_revision": {"type": "integer", "minimum": 0},
                "summary": {"type": "string", "maxLength": 4000},
                "steps": {
                    "type": "array",
                    "maxItems": 64,
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "maxLength": 500},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed", "blocked", "skipped"],
                            },
                            "evidence": {
                                "type": "array",
                                "maxItems": 16,
                                "items": {"type": "string", "maxLength": 1000},
                            },
                            "blocker": {"type": "string", "maxLength": 1000},
                        },
                        "required": ["title", "status"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["expected_revision", "summary", "steps"],
            "additionalProperties": False,
        },
        update,
        ToolSidecar(
            "update_plan",
            mutating=False,
            source="agent.task",
            tool_type="task_control",
            permission="update_task_plan",
            body_scope=(),
            terminal_truth=("TaskPlanRecord.revision",),
        ),
    )


def _checkpoint_task_tool(
    workspace: TaskWorkspace,
    *,
    body_fingerprint: Callable[[], dict[str, object]] | None,
    evidence_cursor: Callable[[], int] | None,
    generation: Callable[[], int] | None,
) -> RegisteredTool:
    def checkpoint(params: dict[str, object]) -> ToolResult:
        try:
            disposition = CheckpointDisposition(str(params.get("disposition") or ""))
            fingerprint = None if body_fingerprint is None else body_fingerprint()
            continuation = _continuation_contract(
                workspace,
                disposition=disposition,
                raw=params.get("continuation"),
                evidence_cursor=0 if evidence_cursor is None else evidence_cursor(),
                generation=0 if generation is None else generation(),
            )
            task, record = workspace.checkpoint(
                expected_task_revision=int(params.get("expected_task_revision") or 0),
                disposition=disposition,
                summary=str(params.get("summary") or ""),
                next_step=str(params.get("next_step") or ""),
                evidence=list(params.get("evidence") or []),
                wait_for=list(params.get("wait_for") or []),
                body_fingerprint=fingerprint,
                continuation=continuation,
            )
        except (RuntimeStateConflict, ValueError) as exc:
            return ToolResult(
                success=False,
                reason="task_checkpoint_rejected",
                can_retry=True,
                metrics={"error": str(exc), "current": workspace.payload()},
            )
        return ToolResult(
            success=True,
            reason="task_checkpoint_recorded",
            can_retry=False,
            metrics={
                "task": _task_payload(task),
                "checkpoint": _checkpoint_payload(record),
            },
        )

    return RegisteredTool(
        "checkpoint_task",
        "Checkpoint durable work with an explicit disposition: continue, wait_event, yield, or complete. Complete is still verified against authoritative terminal truth when available.",
        {
            "type": "object",
            "properties": {
                "expected_task_revision": {"type": "integer", "minimum": 1},
                "disposition": {
                    "type": "string",
                    "enum": ["continue", "wait_event", "yield", "complete"],
                },
                "summary": {"type": "string", "maxLength": 4000},
                "next_step": {"type": "string", "maxLength": 2000},
                "evidence": {
                    "type": "array",
                    "maxItems": 32,
                    "items": {"type": "string", "maxLength": 1000},
                },
                "wait_for": {
                    "type": "array",
                    "maxItems": 16,
                    "description": "Material wake conditions. Use event:<eventName>, action:<action_id>, or entity:<uuid-or-name>. Free text is retained as evidence but does not automatically wake the model.",
                    "items": {"type": "string", "maxLength": 500},
                },
                "continuation": {
                    "type": "object",
                    "description": "Required only for disposition=continue. Describes the next WHAT, never a route or coordinates.",
                    "properties": {
                        "objective": {"type": "string", "maxLength": 2000},
                        "operation_class": {
                            "type": "string",
                            "enum": ["epistemic", "material", "mixed"],
                        },
                        "target_descriptor": {
                            "type": "object",
                            "properties": {
                                "kind": {
                                    "type": "string",
                                    "enum": ["resource", "entity", "place", "state"],
                                },
                                "identifier": {"type": "string", "maxLength": 256},
                                "traits": {
                                    "type": "array",
                                    "maxItems": 16,
                                    "items": {"type": "string", "maxLength": 128},
                                },
                            },
                            "required": ["kind", "identifier"],
                            "additionalProperties": False,
                        },
                        "expected_evidence": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 16,
                            "items": {"type": "string", "maxLength": 256},
                        },
                        "bounded_epoch_budget": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 32,
                        },
                    },
                    "required": [
                        "objective",
                        "operation_class",
                        "target_descriptor",
                        "expected_evidence",
                        "bounded_epoch_budget",
                    ],
                    "additionalProperties": False,
                },
            },
            "required": ["expected_task_revision", "disposition", "summary"],
            "additionalProperties": False,
        },
        checkpoint,
        ToolSidecar(
            "checkpoint_task",
            mutating=False,
            source="agent.task",
            tool_type="task_control",
            permission="checkpoint_task",
            body_scope=(),
            terminal_truth=("TaskCheckpointRecord.revision",),
        ),
    )


def _task_payload(task: TaskRecord) -> dict[str, object]:
    return {
        "task_id": task.task_id,
        "revision": task.revision,
        "goal": task.goal_text,
        "source": task.source,
        "requested_by": task.requested_by,
        "status": task.status.value,
        "completion_authority": task.completion_authority.value,
        "active_plan_id": task.active_plan_id,
        "latest_checkpoint_id": task.latest_checkpoint_id,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }


def _plan_payload(plan: TaskPlanRecord) -> dict[str, object]:
    return {
        "plan_id": plan.plan_id,
        "revision": plan.revision,
        "summary": plan.summary,
        "steps": [
            {
                "step_id": step.step_id,
                "ordinal": step.ordinal,
                "title": step.title,
                "status": step.status.value,
                "evidence": list(step.evidence),
                "blocker": step.blocker,
            }
            for step in plan.steps
        ],
    }


def _checkpoint_payload(checkpoint: TaskCheckpointRecord) -> dict[str, object]:
    return {
        "checkpoint_id": checkpoint.checkpoint_id,
        "revision": checkpoint.revision,
        "disposition": checkpoint.disposition.value,
        "summary": checkpoint.summary,
        "next_step": checkpoint.next_step,
        "evidence": list(checkpoint.evidence),
        "wait_for": list(checkpoint.wait_for),
        "body_fingerprint": checkpoint.body_fingerprint,
        "continuation": (
            None
            if checkpoint.continuation is None
            else {
                "objective": checkpoint.continuation.objective,
                "operation_class": checkpoint.continuation.operation_class.value,
                "target_descriptor": dict(checkpoint.continuation.target_descriptor),
                "expected_evidence": list(checkpoint.continuation.expected_evidence),
                "bounded_epoch_budget": checkpoint.continuation.bounded_epoch_budget,
                "approach_key": checkpoint.continuation.approach_key,
                "evidence_cursor": checkpoint.continuation.evidence_cursor,
                "generation": checkpoint.continuation.generation,
            }
        ),
        "created_at": checkpoint.created_at,
    }


def _continuation_contract(
    workspace: TaskWorkspace,
    *,
    disposition: CheckpointDisposition,
    raw: object,
    evidence_cursor: int,
    generation: int,
) -> ContinuationContract | None:
    if disposition is not CheckpointDisposition.CONTINUE:
        if raw is not None:
            raise ValueError("continuation is allowed only when disposition=continue")
        return None
    if not isinstance(raw, dict):
        raise ValueError("disposition=continue requires a continuation contract")
    objective = str(raw.get("objective") or "").strip()
    if not objective or len(objective) > 2000:
        raise ValueError("continuation objective must be 1..2000 characters")
    operation_class = ContinuationOperationClass(str(raw.get("operation_class") or ""))
    descriptor = _normalize_target_descriptor(raw.get("target_descriptor"))
    raw_expected_evidence = raw.get("expected_evidence")
    if not isinstance(raw_expected_evidence, list):
        raise ValueError("continuation expected_evidence must be a list")
    expected_evidence = tuple(
        dict.fromkeys(
            str(item).strip()
            for item in raw_expected_evidence
            if str(item).strip()
        )
    )
    if not expected_evidence or len(expected_evidence) > 16 or any(
        len(item) > 256 for item in expected_evidence
    ):
        raise ValueError("continuation expected_evidence must contain 1..16 bounded facts")
    requested_budget = int(raw.get("bounded_epoch_budget") or 0)
    if requested_budget < 1 or requested_budget > 32:
        raise ValueError("continuation bounded_epoch_budget must be between 1 and 32")
    approach_payload = {
        "operation_class": operation_class.value,
        "target_descriptor": descriptor,
    }
    approach_key = "approach:" + hashlib.sha256(
        json.dumps(
            approach_payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    task = workspace._require_task()
    effective_budget = workspace.store.continuation_approach_remaining(
        workspace.scope,
        task_id=task.task_id,
        approach_key=approach_key,
        requested_budget=requested_budget,
    )
    if effective_budget < 1:
        raise ValueError(f"continuation approach budget exhausted: {approach_key}")
    return ContinuationContract(
        objective=objective,
        operation_class=operation_class,
        target_descriptor=descriptor,
        expected_evidence=expected_evidence,
        bounded_epoch_budget=effective_budget,
        approach_key=approach_key,
        evidence_cursor=max(0, int(evidence_cursor)),
        generation=max(0, int(generation)),
    )


def _normalize_target_descriptor(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise ValueError("continuation target_descriptor must be an object")
    extra = set(raw) - {"kind", "identifier", "traits"}
    if extra:
        raise ValueError("continuation target_descriptor cannot contain routes or coordinates")
    kind = str(raw.get("kind") or "").strip().casefold()
    if kind not in {"resource", "entity", "place", "state"}:
        raise ValueError("continuation target_descriptor kind is invalid")
    identifier = " ".join(str(raw.get("identifier") or "").split()).casefold()
    if not identifier or len(identifier) > 256:
        raise ValueError("continuation target identifier must be 1..256 characters")
    raw_traits = raw.get("traits") or []
    if not isinstance(raw_traits, list):
        raise ValueError("continuation target traits must be a list")
    traits = sorted(
        dict.fromkeys(
            " ".join(str(item).split()).casefold()
            for item in raw_traits
            if str(item).strip()
        )
    )
    if len(traits) > 16 or any(len(item) > 128 for item in traits):
        raise ValueError("continuation target traits exceed their bounded schema")
    return {"kind": kind, "identifier": identifier, "traits": traits}


__all__ = ["TaskWorkspace", "register_task_tools"]
