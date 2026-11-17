"""Persistent task workspace and governed task-artifact tools."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, replace

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
from minebot.app.projection import bounded_summary_value
from minebot.brain.context import AgentContext
from minebot.brain.registry import RegisteredTool, ToolRegistry, ToolSidecar
from minebot.contract import JsonObject, ToolResult

# Evidence on a yield the program wrote for the Agent (an unclosed turn, a
# refused continuation): it sets the goal aside and asks the player nothing.
SET_ASIDE_BY_PROGRAM = "set_aside_by_program"
# A turn that ended without a disposition: the task goes on, recorded here.
CONTINUED_BY_PROGRAM = "continued_by_program"
_QUESTION_CHARS = 300


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

    def yield_without_continuation(
        self,
        *,
        body_fingerprint: dict[str, object] | None,
    ) -> tuple[TaskRecord, TaskCheckpointRecord] | None:
        task = self.current_task
        if task is None or task.status is not TaskStatus.RUNNING:
            return None
        return self.checkpoint(
            expected_task_revision=task.revision,
            disposition=CheckpointDisposition.YIELD,
            summary="model_final_without_continuation_after_boundary_retry",
            evidence=("bounded_task_boundary_retry_exhausted", SET_ASIDE_BY_PROGRAM),
            body_fingerprint=body_fingerprint,
        )

    def continue_by_program(
        self,
        *,
        summary: str,
        body_fingerprint: dict[str, object] | None,
        evidence_cursor: int,
        generation: int,
    ) -> tuple[TaskRecord, TaskCheckpointRecord] | None:
        """A turn ended without a disposition: the unfinished task goes on.

        The Agent used to spend one model request a turn on checkpoint_task
        with disposition=continue: 53 of 179 requests in easy run-19, 17% of
        the wall clock, while the Body waited (project-review-20261110.md
        §2.1). Going on is what an unfinished task does; only waiting for an
        event, asking the player and claiming completion need the Agent's own
        word. The last continuation the Agent wrote is carried on; without one
        the goal itself is the objective.
        """
        task = self.current_task
        if task is None or task.status is not TaskStatus.RUNNING:
            return None
        previous = self.store.get_latest_checkpoint(task.task_id)
        carried = None if previous is None else previous.continuation
        if carried is not None:
            continuation = replace(carried, evidence_cursor=max(0, int(evidence_cursor)),
                                   generation=max(0, int(generation)))
        else:
            continuation = _continuation_contract(
                self, disposition=CheckpointDisposition.CONTINUE,
                raw={
                    "objective": (task.goal_text or "").strip()[:2000] or "go on with the task",
                    "operation_class": "mixed",
                    "target_descriptor": {"kind": "state", "identifier": "the goal of the task"},
                    "expected_evidence": ["progress toward the goal"],
                    "bounded_epoch_budget": 4,
                },
                evidence_cursor=evidence_cursor, generation=generation,
            )
        return self.checkpoint(
            expected_task_revision=task.revision,
            disposition=CheckpointDisposition.CONTINUE,
            summary=" ".join((summary or "").split())[:600] or "The turn ended without a note.",
            evidence=(CONTINUED_BY_PROGRAM,),
            body_fingerprint=body_fingerprint,
            continuation=continuation,
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
            evidence=(*evidence, SET_ASIDE_BY_PROGRAM),
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
        if disposition is CheckpointDisposition.COMPLETE and not any(
            str(item).strip() for item in evidence
        ):
            raise ValueError("complete checkpoints require bounded completion evidence")
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
        checkpoint = self.store.get_latest_checkpoint(task.task_id)
        skills = self.store.list_skill_activations(
            self.scope,
            task_id=task.task_id,
            include_scope_activations=True,
        )
        payload = {
            "active": True,
            "task": _task_payload(task),
            "checkpoint": None if checkpoint is None else _checkpoint_payload(checkpoint),
            "skills": [skill_activation_payload(record) for record in skills],
        }
        waiting = self.waiting_for_player()
        if waiting is not None:
            payload["waiting_for_player"] = waiting
        return payload

    def sync_context(self, context: AgentContext) -> None:
        context.observe_task(self.payload())

    def waiting_for_player(self) -> dict[str, object] | None:
        """The question the Agent's own yield put to the player, until they speak.

        A yield hands one decision to the player, not the Agent's day
        (s7-later-20261029.md §8 D1). Asking again before anyone answered
        keeps the first time asked: a reply moves the task on, so asks with a
        reply between them are not consecutive revisions.
        """
        task = self.current_task
        if task is None or task.status is not TaskStatus.YIELDED:
            return None
        checkpoints = self.store.list_task_checkpoints(task.task_id)
        if not checkpoints or not _asks_player(checkpoints[-1]):
            return None
        first = checkpoints[-1]
        for earlier in reversed(checkpoints[:-1]):
            if not _asks_player(earlier) or earlier.revision != first.revision - 1:
                break
            first = earlier
        question = checkpoints[-1].summary
        if len(question) > _QUESTION_CHARS:
            question = question[: _QUESTION_CHARS - 1] + "… (whole in checkpoint.summary)"
        return {"since": first.created_at, "question": question}

    @property
    def completion_requested(self) -> bool:
        task = self.current_task
        if task is None or task.status is not TaskStatus.WAITING_EVENT:
            return False
        checkpoint = self.store.get_latest_checkpoint(task.task_id)
        return (
            checkpoint is not None
            and checkpoint.disposition is CheckpointDisposition.COMPLETE
        )

    def completion_review_budget_owner(self) -> str | None:
        """Return the live continuation a pending completion request interrupted.

        A completion request is not terminal authority.  When its independent
        verifier rejects it, only the immediately preceding continuation may
        fund the correction turn; looking further back could revive a strategy
        that a later checkpoint had already closed.
        """
        task = self.current_task
        if task is None or task.status is not TaskStatus.WAITING_EVENT:
            return None
        checkpoints = self.store.list_task_checkpoints(task.task_id)
        if len(checkpoints) < 2:
            return None
        pending, owner = checkpoints[-1], checkpoints[-2]
        if (
            pending.disposition is not CheckpointDisposition.COMPLETE
            or owner.disposition is not CheckpointDisposition.CONTINUE
            or owner.continuation is None
        ):
            return None
        return owner.checkpoint_id

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
    ended_activity: Callable[[str], dict[str, object] | None] | None = None,
) -> None:
    registry.register(_read_task_tool(workspace))
    # update_plan is gone: the plan is the ladder in the life profile
    # (plan-ladder-20261028.md §7.1); TASK_ARTIFACT keeps goal and checkpoint.
    registry.register(
        _checkpoint_task_tool(
            workspace,
            body_fingerprint=body_fingerprint,
            evidence_cursor=evidence_cursor,
            generation=generation,
            ended_activity=ended_activity,
        )
    )


def task_artifact_projector(_reason: str, metrics: dict[str, object]) -> JsonObject:
    """Model-visible projection for read_task / checkpoint_task."""
    return {"task_artifact": _task_artifact_summary(metrics)}


def _task_artifact_summary(metrics: dict[str, object]) -> JsonObject:
    current = metrics.get("current")
    source = current if isinstance(current, dict) else metrics
    summary: JsonObject = {}
    if "active" in source:
        summary["active"] = bool(source.get("active"))
    if source.get("scope_key") is not None:
        summary["scope_key"] = str(source.get("scope_key"))

    task = metrics.get("task")
    if not isinstance(task, dict):
        task = source.get("task")
    if isinstance(task, dict):
        summary["task"] = {
            key: bounded_summary_value(task[key])
            for key in (
                "task_id",
                "revision",
                "goal",
                "status",
                "completion_authority",
                "active_plan_id",
                "latest_checkpoint_id",
            )
            if key in task
        }


    checkpoint = metrics.get("checkpoint")
    if not isinstance(checkpoint, dict):
        checkpoint = source.get("checkpoint")
    if isinstance(checkpoint, dict):
        summary["checkpoint"] = {
            key: bounded_summary_value(checkpoint[key])
            for key in (
                "checkpoint_id",
                "revision",
                "disposition",
                "summary",
                "next_step",
                "wait_for",
            )
            if key in checkpoint
        }
    waiting = source.get("waiting_for_player")
    if isinstance(waiting, dict):
        summary["waiting_for_player"] = {
            key: bounded_summary_value(waiting[key]) for key in ("since", "question") if key in waiting
        }
    if metrics.get("error") is not None:
        summary["error"] = bounded_summary_value(metrics["error"])
    return summary


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
        projector=task_artifact_projector,
    )


def _checkpoint_task_tool(
    workspace: TaskWorkspace,
    *,
    body_fingerprint: Callable[[], dict[str, object]] | None,
    evidence_cursor: Callable[[], int] | None,
    generation: Callable[[], int] | None,
    ended_activity: Callable[[str], dict[str, object] | None] | None = None,
) -> RegisteredTool:
    def checkpoint(params: dict[str, object]) -> ToolResult:
        try:
            disposition = CheckpointDisposition(str(params.get("disposition") or ""))
            # Waiting on a plan that has already ended waits forever: its one
            # result fact has been delivered (house run 2026-10-18, 23 minutes
            # asleep). Say so instead of recording the wait.
            if disposition is CheckpointDisposition.WAIT_EVENT and ended_activity is not None:
                for condition in params.get("wait_for") or []:
                    ended = ended_activity(str(condition).removeprefix("action:").strip())
                    if ended is not None:
                        return ToolResult(
                            success=False,
                            reason="task_checkpoint_rejected",
                            can_retry=True,
                            metrics={"error": f"{condition}: that plan is not running; it ended "
                                              f"{ended.get('status')}, so nothing will raise this event",
                                     "plan": ended, "current": workspace.payload()},
                        )
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
        "End the turn otherwise than by going on: wait_event, yield, or complete. A turn that simply ends continues the task by itself and you rest until a result, a hand-back, a Body event, a ladder fact or the heartbeat wakes you, so do not call this to continue. Complete requires bounded evidence and is only a completion request; Body truth or a human remains the completion authority. Yield puts one decision to the player: summary is your question, kept on the task as waiting_for_player until they speak. Your ladder, your home and the Body go on meanwhile, and Body events, ladder facts and the heartbeat still wake you.",
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
                    "description": "Material wake conditions. Use event:<eventName>, action:<action_id>, or entity:<uuid-or-name>. action:<activity_id> of a running plan wakes you when that plan ends. Free text is retained as evidence but does not automatically wake the model.",
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
        projector=task_artifact_projector,
    )


def _asks_player(checkpoint: TaskCheckpointRecord) -> bool:
    return (
        checkpoint.disposition is CheckpointDisposition.YIELD
        and SET_ASIDE_BY_PROGRAM not in checkpoint.evidence
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
    # The budget is the Agent's own estimate, kept as a fact. Earlier stops or
    # spent rounds never refuse the Agent's next checkpoint (the Agent stays live).
    effective_budget = requested_budget
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
