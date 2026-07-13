"""Long-lived Agent session supervisor for Phase 1.

This is an app-layer object: it owns the outer autonomous/session loop around
the SDK inner `Runner.run` turns. It may see the runtime, Body, lifecycle, and
future conversation entrypoints; it must not live in `brain/`.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from uuid import uuid4

from minebot.app.wiring import AgentRuntimeParts
from minebot.app.runner import RecoveryOutcome
from minebot.app.runtime_state import CheckpointDisposition, CompletionAuthority, TaskStatus
from minebot.app.tasks import TaskWorkspace
from minebot.app.work_queue import (
    MemoryWorkIntentQueue,
    WorkIntent,
    WorkIntentKind,
    WorkIntentQueue,
    WorkIntentState,
    superseded_kinds_for,
)
from minebot.brain.lifecycle import LifecycleError, LifecycleState
from minebot.brain.modes import AgentSignal, signalize_events
from minebot.contract import Event

DEFAULT_RUNAWAY_STEP_LIMIT = 100_000
DEFAULT_TASK_CONTINUATION_LIMIT = 64


class SessionCommandKind(Enum):
    START = "start"
    PAUSE = "pause"
    CONTINUE = "continue"
    CANCEL = "cancel"
    REPLACE_GOAL = "replace_goal"
    MESSAGE = "message"
    QUIT = "quit"


@dataclass(frozen=True)
class SessionCommand:
    kind: SessionCommandKind
    text: str = ""
    reason: str = ""
    sender: str = ""

    @classmethod
    def start(cls, goal: str, reason: str = "goal_started", *, sender: str = "") -> "SessionCommand":
        return cls(SessionCommandKind.START, text=goal, reason=reason, sender=sender)

    @classmethod
    def pause(cls, reason: str = "user_pause", *, sender: str = "") -> "SessionCommand":
        return cls(SessionCommandKind.PAUSE, reason=reason, sender=sender)

    @classmethod
    def continue_(cls, text: str = "", *, sender: str = "") -> "SessionCommand":
        return cls(SessionCommandKind.CONTINUE, text=text, reason="user_continue", sender=sender)

    @classmethod
    def cancel(cls, reason: str = "user_cancel", *, sender: str = "") -> "SessionCommand":
        return cls(SessionCommandKind.CANCEL, reason=reason, sender=sender)

    @classmethod
    def replace_goal(cls, goal: str, reason: str = "goal_replaced", *, sender: str = "") -> "SessionCommand":
        return cls(SessionCommandKind.REPLACE_GOAL, text=goal, reason=reason, sender=sender)

    @classmethod
    def message(cls, text: str, reason: str = "user_message", *, sender: str = "") -> "SessionCommand":
        return cls(SessionCommandKind.MESSAGE, text=text, reason=reason, sender=sender)

    @classmethod
    def quit(cls, reason: str = "user_quit", *, sender: str = "") -> "SessionCommand":
        return cls(SessionCommandKind.QUIT, reason=reason, sender=sender)


@dataclass(frozen=True)
class SessionStep:
    status: str
    lifecycle: LifecycleState
    message: str | None = None


PartsFactory = Callable[[str], AgentRuntimeParts]
ShouldStop = Callable[[SessionStep], bool]
_WORK_PREEMPTED = object()


@dataclass
class AgentSession:
    """Persistent outer session around one AgentRuntimeParts instance."""

    parts_factory: PartsFactory
    task_workspace: TaskWorkspace | None = None
    work_queue: WorkIntentQueue | None = None
    parts: AgentRuntimeParts | None = None
    max_recovery_attempts: int = 3
    max_task_continuations: int = DEFAULT_TASK_CONTINUATION_LIMIT
    _recovery_attempts: int = 0
    _goal_active: bool = False
    _turn_pending: bool = False
    _active_turn_request: tuple[str, str] | None = None
    _suspended_turn_request: tuple[str, str] | None = None
    _work_in_flight: bool = False
    _execution_quarantined: bool = False
    _scheduler_id: str = field(default_factory=lambda: f"session-{uuid4().hex}")

    def __post_init__(self) -> None:
        if self.work_queue is None:
            self.work_queue = MemoryWorkIntentQueue()

    def submit(
        self,
        command: SessionCommand,
        *,
        dedupe_key: str | None = None,
    ) -> WorkIntent:
        always_interrupt = command.kind in {
            SessionCommandKind.PAUSE,
            SessionCommandKind.CANCEL,
            SessionCommandKind.REPLACE_GOAL,
            SessionCommandKind.QUIT,
        }
        assert self.work_queue is not None
        if dedupe_key is not None:
            existing = self.work_queue.get_by_dedupe(dedupe_key)
            if existing is not None:
                return existing
        intent_kind = WorkIntentKind(command.kind.value)
        if self.parts is not None and (self._work_in_flight or always_interrupt):
            self.parts.authority.invalidate_generation(f"session_command:{command.kind.value}")
            self._body_interrupt(command.reason)
        superseded = superseded_kinds_for(intent_kind)
        if superseded:
            self.work_queue.supersede(
                superseded,
                reason=f"superseded_by:{command.kind.value}",
            )
        task = self.task_workspace.current_task if self.task_workspace is not None else None
        generation = (
            None
            if self.parts is None
            else self.parts.authority.current_generation()
        )
        return self.work_queue.enqueue(
            intent_kind,
            source=command.reason or command.kind.value,
            payload={
                "text": command.text,
                "reason": command.reason,
                "sender": command.sender,
            },
            dedupe_key=dedupe_key,
            task_id=None if task is None else task.task_id,
            generation=generation,
        )

    @property
    def lifecycle_state(self) -> LifecycleState | None:
        if self.parts is None:
            return None
        return self.parts.lifecycle.state

    @property
    def current_goal(self) -> str | None:
        if self.task_workspace is not None:
            task = self.task_workspace.current_task
            if task is not None:
                return task.goal_text
        if self.parts is None or not self._goal_active:
            return None
        return self.parts.context.goal_text

    @property
    def has_active_goal(self) -> bool:
        if self.task_workspace is not None:
            return self.task_workspace.current_task is not None
        return self.parts is not None and self._goal_active

    @property
    def has_pending_work(self) -> bool:
        assert self.work_queue is not None
        if self.work_queue.available.is_set():
            return True
        return self.parts is not None and self.parts.lifecycle.state is LifecycleState.RECOVERING

    async def step(self) -> SessionStep:
        """Admit one queued intent, then advance at most one SDK run."""
        assert self.work_queue is not None
        if self.parts is not None:
            quarantine = await self._guard_execution_quarantine()
            if quarantine is not None:
                return quarantine
        self._queue_recovery_if_required()
        intent = self.work_queue.lease_next()
        try:
            if intent is None:
                if self.parts is None:
                    return SessionStep("idle", LifecycleState.IDLE, "no pending intent")
                status = "waiting" if self.has_active_goal else "idle"
                return SessionStep(status, self.parts.lifecycle.state, "no pending intent")
            admission_version = self.work_queue.notification_version
            self._work_in_flight = True
            try:
                result = await self._execute_intent(
                    intent,
                    admission_version=admission_version,
                )
            finally:
                self._work_in_flight = False
        except Exception as exc:
            self.work_queue.fail(
                intent,
                {"error_type": type(exc).__name__, "message": str(exc)},
            )
            raise
        if result.status == "preempted":
            self.work_queue.supersede_active(intent, reason=result.message or "work_preempted")
        elif result.status == "failed":
            self.work_queue.fail(intent, {"reason": result.message or "runtime_failed"})
        else:
            self.work_queue.complete(intent)
        return result

    async def _execute_intent(
        self,
        intent: WorkIntent,
        *,
        admission_version: int,
    ) -> SessionStep:
        checkpoint_before = self._latest_checkpoint_id()
        command = _command_from_intent(intent) if _is_command_intent(intent) else None
        signals = (
            await self._apply_command(command)
            if command is not None
            else await self._apply_runtime_intent(intent)
        )
        runtime_terminal = self._runtime_intent_terminal_step(intent)
        if runtime_terminal is not None:
            return runtime_terminal
        if command is not None and command.kind is SessionCommandKind.QUIT:
            lifecycle = self.parts.lifecycle.state if self.parts is not None else LifecycleState.IDLE
            return SessionStep("quit", lifecycle, "user_quit")
        if self.parts is None:
            return SessionStep("idle", LifecycleState.IDLE, "no active work")

        self._sync_task_context()
        start_fingerprint = self._current_body_fingerprint()
        if command is not None and command.kind is SessionCommandKind.CANCEL:
            return SessionStep("waiting", self.parts.lifecycle.state)
        if not self._turn_pending and self.parts.lifecycle.state is not LifecycleState.RECOVERING:
            status = "waiting" if self.has_active_goal else "idle"
            return SessionStep(status, self.parts.lifecycle.state, "intent handled without model turn")

        if self.parts.lifecycle.state is LifecycleState.RECOVERING:
            recovered = await self._run_supervised(
                self._drive_recovery(),
                work_kind="recovery",
                admission_version=admission_version,
            )
            if recovered is _WORK_PREEMPTED:
                return SessionStep("preempted", self.parts.lifecycle.state, "superseded_during_recovery")
            return self._finish_step(
                recovered,
                intent=intent,
                checkpoint_before=checkpoint_before,
                start_fingerprint=start_fingerprint,
            )

        runnable_states = {
            LifecycleState.INIT,
            LifecycleState.IDLE,
            LifecycleState.ACTIVE,
            LifecycleState.RESUMING,
        }
        if self.parts.lifecycle.state not in runnable_states:
            return SessionStep("waiting", self.parts.lifecycle.state)

        try:
            outcome = await self._run_supervised(
                self.parts.runtime.run_turn(
                    extra_signals=signals,
                    body_actions_allowed=intent.kind is not WorkIntentKind.MAINTENANCE,
                ),
                work_kind="agent_turn",
                admission_version=admission_version,
            )
            if outcome is _WORK_PREEMPTED:
                return SessionStep("preempted", self.parts.lifecycle.state, "superseded_during_agent_turn")
        except Exception as exc:
            self._trace(
                "session_step_failed",
                error_type=type(exc).__name__,
                error_message=str(exc),
                cause_type=type(exc.__cause__).__name__ if exc.__cause__ is not None else None,
                cause_message=str(exc.__cause__) if exc.__cause__ is not None else None,
                lifecycle=self.parts.lifecycle.state.value,
            )
            self._stand_down()
            self._turn_pending = False
            return SessionStep("failed", self.parts.lifecycle.state, f"runtime_error:{type(exc).__name__}")
        return self._finish_step(
            SessionStep(outcome.status, outcome.lifecycle, outcome.message),
            intent=intent,
            checkpoint_before=checkpoint_before,
            start_fingerprint=start_fingerprint,
        )

    async def _drive_recovery(self) -> SessionStep:
        assert self.parts is not None
        handler = self.parts.runtime.recovery_handler
        if handler is None:
            self._trace("session_recovery_missing_driver", lifecycle=self.parts.lifecycle.state.value)
            return self._yield_recovery_failure("recovery_driver_missing", {})
        self._recovery_attempts += 1
        try:
            if inspect.iscoroutinefunction(handler):
                outcome = await handler(self.parts.runtime)
            else:
                outcome = await self.parts.runtime.run_sync(handler, self.parts.runtime)
                if inspect.isawaitable(outcome):
                    outcome = await outcome
        except Exception as exc:
            self._trace(
                "session_recovery_failed",
                error_type=type(exc).__name__,
                error_message=str(exc),
                lifecycle=self.parts.lifecycle.state.value,
                attempt=self._recovery_attempts,
            )
            return self._yield_recovery_failure("recovery_driver_error", {"error_type": type(exc).__name__, "message": str(exc)})
        if not isinstance(outcome, RecoveryOutcome):
            self._trace(
                "session_recovery_invalid_result",
                result_type=type(outcome).__name__,
                lifecycle=self.parts.lifecycle.state.value,
                attempt=self._recovery_attempts,
            )
            return self._yield_recovery_failure("recovery_driver_invalid_result", {"result_type": type(outcome).__name__})

        self._trace(
            "session_recovery_result",
            success=outcome.success,
            reason=outcome.reason,
            can_retry=outcome.can_retry,
            attempt=self._recovery_attempts,
            max_attempts=self.max_recovery_attempts,
            facts=outcome.facts,
        )
        if not outcome.success:
            if outcome.can_retry and self._recovery_attempts < self.max_recovery_attempts:
                return SessionStep("recovery_retry", self.parts.lifecycle.state, f"recovery_retry:{outcome.reason}")
            return self._yield_recovery_failure(outcome.reason, outcome.facts)

        self._recovery_attempts = 0
        signals = [AgentSignal.recovery_completed(outcome.reason, **outcome.facts)]
        recovered = await self.parts.runtime.run_turn(extra_signals=signals)
        return SessionStep(recovered.status, recovered.lifecycle, recovered.message)

    def _yield_recovery_failure(self, reason: str, facts: dict[str, object]) -> SessionStep:
        assert self.parts is not None
        self._trace(
            "session_recovery_gave_up",
            reason=reason,
            attempts=self._recovery_attempts,
            max_attempts=self.max_recovery_attempts,
            facts=facts,
        )
        self._recovery_attempts = 0
        try:
            if self.parts.lifecycle.state is LifecycleState.RECOVERING:
                self.parts.lifecycle.stand_down()
        except LifecycleError:
            self._trace("session_lifecycle_error", action="recovery_stand_down", state=self.parts.lifecycle.state.value)
        return SessionStep("yielded", self.parts.lifecycle.state, f"recovery_failed:{reason}")

    async def run_until_waiting(
        self,
        *,
        max_steps: int | None = None,
        should_stop: ShouldStop | None = None,
    ) -> SessionStep:
        """Run active work until lifecycle/yield/terminal truth stops it.

        ``max_steps`` is a runaway guard only. It is not a continuation or stop
        mechanism for normal autonomous work.
        """
        last = await self.step()
        if should_stop is not None and should_stop(last):
            return last
        remaining = None if max_steps is None else max(0, max_steps - 1)
        while remaining is None or remaining > 0:
            if not self.has_pending_work:
                return last
            if last.lifecycle not in {LifecycleState.ACTIVE, LifecycleState.RECOVERING, LifecycleState.RESUMING}:
                return last
            last = await self.step()
            if should_stop is not None and should_stop(last):
                return last
            if remaining is not None:
                remaining -= 1
        return last

    def complete_current_goal(
        self,
        reason: str = "goal_completed",
        *,
        authority: CompletionAuthority = CompletionAuthority.BODY_TRUTH,
    ) -> SessionStep:
        """Stand down after authoritative terminal truth has completed a goal."""
        if self.parts is None:
            return SessionStep("idle", LifecycleState.IDLE, "no active goal")
        completed_goal = self.parts.context.goal_text
        completed_task = (
            None if self.task_workspace is None else self.task_workspace.current_task
        )
        completed_artifact = (
            None if self.task_workspace is None else self.task_workspace.payload()
        )
        self._trace("session_goal_completed", goal=completed_goal, reason=reason)
        if self.task_workspace is not None:
            self.task_workspace.complete(authority=authority)
        self.parts.context.discard_pending_turn_input()
        self.parts.context.observe_system_message(
            f"Goal completed: {completed_goal}. Terminal reason: {reason}."
        )
        self._goal_active = False
        self._turn_pending = False
        self._active_turn_request = None
        self._suspended_turn_request = None
        self._stand_down()
        self.parts.context.set_goal("")
        self.parts.runtime.weld_context.goal_text = ""
        self._sync_task_context()
        if "write_memory" in self.parts.runtime.registry:
            self._enqueue_reflection(
                trigger="task_completed",
                task_id=None if completed_task is None else completed_task.task_id,
                facts={
                    "goal": completed_goal,
                    "terminal_reason": reason,
                    "completion_authority": authority.value,
                    "task_artifact": completed_artifact,
                },
            )
        return SessionStep("completed", self.parts.lifecycle.state, reason)

    async def _apply_command(self, command: SessionCommand) -> list[AgentSignal]:
        if command.kind is SessionCommandKind.START:
            had_parts = self.parts is not None
            if self.parts is None:
                self.parts = self.parts_factory(command.text)
            else:
                self.parts.context.set_goal(command.text)
                self.parts.runtime.weld_context.goal_text = command.text
                self.parts.authority.invalidate_generation("goal_started")
                self._resume_if_waiting()
            if self.task_workspace is not None:
                task = self.task_workspace.start(
                    command.text,
                    source=command.reason or "user_start",
                    requested_by=command.sender,
                )
                self.parts.context.set_goal(task.goal_text)
                self.parts.runtime.weld_context.goal_text = task.goal_text
            self._goal_active = True
            self._turn_pending = True
            self._active_turn_request = None
            self._suspended_turn_request = None
            self.parts.context.observe_user_message(command.text, sender=command.sender or None)
            if not had_parts and command.reason == "chat_goal_promoted":
                self._trace(
                    "chat_message",
                    sender=command.sender,
                    command=command.kind.value,
                    content=command.text,
                    reason=command.reason,
                )
            self._trace("user_message", command="start", content=command.text, sender=command.sender)
            if command.reason == "chat_goal_promoted":
                self._trace("chat_goal_promoted", goal=command.text)
            self._sync_task_context()
            return [AgentSignal.goal_started(command.text)]

        if command.kind is SessionCommandKind.QUIT:
            if self.parts is not None:
                self.parts.context.observe_user_message(command.reason, sender=command.sender or None)
                self._trace("user_message", command="quit", reason=command.reason, sender=command.sender)
                self._goal_active = False
                self._turn_pending = False
                self._active_turn_request = None
                self._suspended_turn_request = None
                self._stand_down()
            return []

        if command.kind is SessionCommandKind.MESSAGE:
            had_parts = self.parts is not None
            if self.parts is None:
                task = self.task_workspace.current_task if self.task_workspace is not None else None
                if task is None:
                    self.parts = self.parts_factory("")
                else:
                    self._ensure_parts_for_runtime_intent()
                    self._goal_active = True
            if not self.has_active_goal:
                self.parts.context.set_goal("")
                self.parts.runtime.weld_context.goal_text = ""
                self._resume_if_waiting()
            self._turn_pending = True
            self._active_turn_request = (command.text, command.sender)
            self._suspended_turn_request = None
            self.parts.context.observe_user_message(command.text, sender=command.sender or None)
            if not had_parts and command.reason == "chat_session_started":
                self._trace(
                    "chat_message",
                    sender=command.sender,
                    command=command.kind.value,
                    content=command.text,
                    reason=command.reason,
                )
            self._trace("user_message", command="message", content=command.text, sender=command.sender)
            return []

        if command.kind is SessionCommandKind.REPLACE_GOAL:
            had_parts = self.parts is not None
            if self.parts is None:
                self.parts = self.parts_factory(command.text)
            else:
                self.parts.context.set_goal(command.text)
                self.parts.runtime.weld_context.goal_text = command.text
                self.parts.authority.invalidate_generation("goal_replaced")
                self._resume_if_waiting()
            if self.task_workspace is not None:
                task = self.task_workspace.replace(
                    command.text,
                    source=command.reason or "user_replace",
                    requested_by=command.sender,
                )
                self.parts.context.set_goal(task.goal_text)
                self.parts.runtime.weld_context.goal_text = task.goal_text
            self.parts.context.observe_user_message(command.text, sender=command.sender or None)
            self._goal_active = True
            self._turn_pending = True
            self._active_turn_request = None
            self._suspended_turn_request = None
            self._trace("user_message", command="replace_goal", content=command.text, sender=command.sender)
            if command.reason == "chat_goal_promoted":
                if not had_parts:
                    self._trace(
                        "chat_message",
                        sender=command.sender,
                        command=command.kind.value,
                        content=command.text,
                        reason=command.reason,
                    )
                self._trace("chat_goal_promoted", goal=command.text)
            self._sync_task_context()
            return [AgentSignal.goal_started(command.text)]

        if self.parts is None:
            if command.kind is SessionCommandKind.PAUSE:
                if self.task_workspace is not None:
                    self.task_workspace.pause()
                self._turn_pending = False
                return []
            if command.kind is SessionCommandKind.CANCEL:
                if self.task_workspace is not None:
                    self.task_workspace.cancel()
                self._goal_active = False
                self._turn_pending = False
                return []
            if (
                command.kind is SessionCommandKind.CONTINUE
                and self.task_workspace is not None
                and self.task_workspace.current_task is not None
            ):
                self._ensure_parts_for_runtime_intent()
            else:
                return []

        if command.kind is SessionCommandKind.PAUSE:
            if not self._goal_active and self._active_turn_request is not None:
                self._suspended_turn_request = self._active_turn_request
            self.parts.context.observe_user_message(command.reason, sender=command.sender or None)
            self._trace("user_message", command="pause", reason=command.reason, sender=command.sender)
            self._turn_pending = True
            if self.task_workspace is not None:
                self.task_workspace.pause()
                self._sync_task_context()
            return [AgentSignal.user_interrupt(command.reason)]

        if command.kind is SessionCommandKind.CONTINUE:
            if command.text:
                self.parts.context.observe_user_message(command.text, sender=command.sender or None)
            self._trace("user_message", command="continue", content=command.text, sender=command.sender)
            if self.task_workspace is not None and self.task_workspace.current_task is not None:
                self.task_workspace.resume()
                self._sync_task_context()
            if self._suspended_turn_request is not None:
                request_text, request_sender = self._suspended_turn_request
                request_payload = json.dumps(
                    {
                        "sender": request_sender or None,
                        "message": request_text,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                self.parts.context.observe_system_message(
                    f"Resume the interrupted user request represented by this JSON: {request_payload}"
                )
                self._turn_pending = True
                self._resume_if_waiting()
                return []
            if self._goal_active:
                self._turn_pending = True
                self._resume_if_waiting()
                return []
            if command.text:
                self._active_turn_request = (command.text, command.sender)
                self._turn_pending = True
                self._resume_if_waiting()
                return []
            self._turn_pending = False
            self._resume_if_waiting()
            return []

        if command.kind is SessionCommandKind.CANCEL:
            self.parts.context.observe_user_message(command.reason, sender=command.sender or None)
            self._trace("user_message", command="cancel", reason=command.reason, sender=command.sender)
            self._goal_active = False
            self._turn_pending = False
            self._active_turn_request = None
            self._suspended_turn_request = None
            if self.task_workspace is not None:
                self.task_workspace.cancel()
                self._sync_task_context()
            self._stand_down()
            return []

        return []

    async def _apply_runtime_intent(self, intent: WorkIntent) -> list[AgentSignal]:
        if intent.kind is WorkIntentKind.BODY_EVENT:
            task = self.task_workspace.current_task if self.task_workspace is not None else None
            if intent.task_id is not None and (
                task is None or task.task_id != intent.task_id
            ):
                self._turn_pending = False
                self._trace(
                    "body_event_intent_dropped",
                    reason="task_generation_changed",
                    intent_task_id=intent.task_id,
                    current_task_id=None if task is None else task.task_id,
                )
                return []
            if (
                intent.generation is not None
                and self.parts is not None
                and not self.parts.authority.generation_current(intent.generation)
            ):
                self._turn_pending = False
                self._trace(
                    "body_event_intent_dropped",
                    reason="runtime_generation_changed",
                    intent_generation=intent.generation,
                    current_generation=self.parts.authority.current_generation(),
                )
                return []
            raw = intent.payload.get("event")
            if not isinstance(raw, dict):
                raise ValueError("body_event intent is missing event payload")
            data = raw.get("data")
            if not isinstance(data, dict):
                data = {}
            event = Event(
                seq=int(raw.get("seq") or 0),
                tick=int(raw.get("tick") or 0),
                bot=str(raw.get("bot") or ""),
                name=str(raw.get("name") or ""),
                data=dict(data),
            )
            if not event.name:
                raise ValueError("body_event intent has no event name")
            self._ensure_parts_for_runtime_intent()
            assert self.parts is not None
            payload = json.dumps(
                {
                    "seq": event.seq,
                    "tick": event.tick,
                    "bot": event.bot,
                    "name": event.name,
                    "data": event.data,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            self.parts.context.observe_system_message(f"BODY_EVENT: {payload}")
            self._turn_pending = True
            self._resume_if_waiting()
            self._trace(
                "body_event_wake",
                name=event.name,
                seq=event.seq,
                tick=event.tick,
                task_id=intent.task_id,
            )
            return signalize_events([event])

        if intent.kind in {
            WorkIntentKind.TASK_CONTINUE,
            WorkIntentKind.RECOVERY_RECONCILE,
        }:
            self._ensure_parts_for_runtime_intent()
            assert self.parts is not None
            task = self.task_workspace.current_task if self.task_workspace is not None else None
            if intent.kind is WorkIntentKind.TASK_CONTINUE and task is None:
                self._turn_pending = False
                self._trace("task_continue_dropped", reason="no_active_task")
                return []
            if intent.kind is WorkIntentKind.TASK_CONTINUE:
                if intent.task_id != task.task_id or task.status is not TaskStatus.RUNNING:
                    self._turn_pending = False
                    self._trace(
                        "task_continue_dropped",
                        reason="task_changed",
                        intent_task_id=intent.task_id,
                        current_task_id=task.task_id,
                        task_status=task.status.value,
                    )
                    return []
                if intent.source != "recovery_completed":
                    checkpoint = self.task_workspace.store.get_latest_checkpoint(task.task_id)
                    expected_checkpoint = str(intent.payload.get("checkpoint_id") or "")
                    if (
                        checkpoint is None
                        or checkpoint.checkpoint_id != expected_checkpoint
                        or checkpoint.disposition is not CheckpointDisposition.CONTINUE
                    ):
                        self._turn_pending = False
                        self._trace(
                            "task_continue_dropped",
                            reason="checkpoint_changed",
                            expected_checkpoint=expected_checkpoint,
                            current_checkpoint=None if checkpoint is None else checkpoint.checkpoint_id,
                        )
                        return []
            if task is not None:
                self._goal_active = True
                self.parts.context.set_goal(task.goal_text)
                self.parts.runtime.weld_context.goal_text = task.goal_text
            frame = json.dumps(intent.payload, ensure_ascii=False, sort_keys=True)
            self.parts.context.observe_system_message(
                f"{intent.kind.value.upper()}: {frame}"
            )
            decision = str(intent.payload.get("decision") or "resume")
            if (
                intent.kind is WorkIntentKind.RECOVERY_RECONCILE
                and decision == "resume"
                and task is not None
                and task.status is TaskStatus.WAITING_EVENT
            ):
                task = self.task_workspace.resume()
                self._sync_task_context()
            self._turn_pending = not (
                intent.kind is WorkIntentKind.RECOVERY_RECONCILE
                and decision in {"idle", "park", "complete"}
            )
            if self._turn_pending:
                self._resume_if_waiting()
            self._trace(
                "runtime_intent_wake",
                kind=intent.kind.value,
                task_id=None if task is None else task.task_id,
                decision=decision,
            )
            return []

        if intent.kind is WorkIntentKind.MAINTENANCE:
            action = str(intent.payload.get("action") or "")
            if action != "reflection":
                self._trace("maintenance_intent_deferred", payload=intent.payload)
                return []
            self._ensure_parts_for_runtime_intent()
            assert self.parts is not None
            frame = json.dumps(intent.payload, ensure_ascii=False, sort_keys=True)
            self.parts.context.observe_system_message(
                "REFLECTION_MAINTENANCE: Review the bounded completed-task facts "
                "below and, only when useful, query recent archives or existing "
                "memory. Decide agentically whether to write, update, consolidate, "
                "or delete durable memory. Keep stable facts and reusable experience; "
                "do not copy routine logs, do not perform Body actions, and do not "
                "start or continue a gameplay objective. Finish after the reflection "
                f"decision. FACTS: {frame}"
            )
            self._turn_pending = True
            self._resume_if_waiting()
            self._trace(
                "reflection_maintenance_started",
                trigger=intent.payload.get("trigger"),
                task_id=intent.task_id,
            )
            return []
        raise ValueError(f"unsupported runtime intent: {intent.kind.value}")

    def _runtime_intent_terminal_step(self, intent: WorkIntent) -> SessionStep | None:
        if intent.kind is not WorkIntentKind.RECOVERY_RECONCILE:
            return None
        decision = str(intent.payload.get("decision") or "resume")
        if decision == "complete":
            return self.complete_current_goal(
                "startup_terminal_truth_satisfied",
                authority=CompletionAuthority.BODY_TRUTH,
            )
        if decision in {"idle", "park"}:
            assert self.parts is not None
            self._turn_pending = False
            self._stand_down()
            status = "idle" if decision == "idle" else "waiting"
            return SessionStep(status, self.parts.lifecycle.state, f"startup_{decision}")
        return None

    def _ensure_parts_for_runtime_intent(self) -> None:
        if self.parts is not None:
            return
        task = self.task_workspace.current_task if self.task_workspace is not None else None
        goal = "" if task is None else task.goal_text
        self.parts = self.parts_factory(goal)
        if task is not None:
            self._goal_active = True
            self.parts.context.set_goal(task.goal_text)
            self.parts.runtime.weld_context.goal_text = task.goal_text
        self._sync_task_context()

    def _queue_recovery_if_required(self) -> None:
        if self.parts is None or self.parts.lifecycle.state is not LifecycleState.RECOVERING:
            return
        assert self.work_queue is not None
        task = self.task_workspace.current_task if self.task_workspace is not None else None
        intent = self.work_queue.enqueue(
            WorkIntentKind.RECOVERY_RECONCILE,
            source="lifecycle_recovery",
            payload={
                "reason": "lifecycle_recovering",
                "attempt": self._recovery_attempts + 1,
            },
            dedupe_key=(
                f"{self._scheduler_id}:recovery:"
                f"{self.parts.authority.current_generation()}:"
                f"{self._recovery_attempts}"
            ),
            task_id=None if task is None else task.task_id,
            generation=self.parts.authority.current_generation(),
        )
        self._trace(
            "recovery_intent_queued",
            intent_id=intent.intent_id,
            task_id=None if task is None else task.task_id,
            attempt=self._recovery_attempts + 1,
        )

    def _enqueue_reflection(
        self,
        *,
        trigger: str,
        task_id: str | None,
        facts: dict[str, object],
    ) -> WorkIntent:
        assert self.work_queue is not None
        dedupe_subject = task_id or hashlib.sha256(
            json.dumps(facts, ensure_ascii=True, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:16]
        intent = self.work_queue.enqueue(
            WorkIntentKind.MAINTENANCE,
            source="reflection_trigger",
            payload={"action": "reflection", "trigger": trigger, "facts": facts},
            dedupe_key=f"reflection:{trigger}:{dedupe_subject}",
            task_id=task_id,
            generation=(
                None
                if self.parts is None
                else self.parts.authority.current_generation()
            ),
        )
        self._trace(
            "reflection_intent_queued",
            intent_id=intent.intent_id,
            trigger=trigger,
            task_id=task_id,
        )
        return intent

    def _resume_if_waiting(self) -> None:
        assert self.parts is not None
        state = self.parts.lifecycle.state
        if state is LifecycleState.RECOVERING:
            self._trace("session_continue_deferred_during_recovery", lifecycle=state.value)
            return
        if state in {LifecycleState.YIELDED, LifecycleState.INTERRUPTED}:
            if self._goal_active or self._turn_pending:
                self.parts.lifecycle.resume()
            else:
                self.parts.lifecycle.stand_down()

    def _stand_down(self) -> None:
        assert self.parts is not None
        state = self.parts.lifecycle.state
        try:
            if state is LifecycleState.INIT:
                self.parts.lifecycle.ready()
            elif state in {
                LifecycleState.ACTIVE,
                LifecycleState.YIELDED,
                LifecycleState.INTERRUPTED,
                LifecycleState.RECOVERING,
                LifecycleState.RESUMING,
            }:
                self.parts.lifecycle.stand_down()
        except LifecycleError:
            self._trace("session_lifecycle_error", action="stand_down", state=self.parts.lifecycle.state.value)

    def _trace(self, event: str, **fields: object) -> None:
        if self.parts is not None:
            self.parts.runtime.trace.emit(event, **fields)

    def _sync_task_context(self) -> None:
        if self.parts is None:
            return
        if self.task_workspace is not None:
            self.task_workspace.sync_context(self.parts.context)
        summary_payload = getattr(
            self.parts.runtime.conversation_session,
            "summary_payload",
            None,
        )
        if callable(summary_payload):
            self.parts.context.observe_conversation_summary(summary_payload())

    def _finish_step(
        self,
        step: SessionStep,
        *,
        intent: WorkIntent,
        checkpoint_before: str | None,
        start_fingerprint: dict[str, object] | None,
    ) -> SessionStep:
        if self.parts is None:
            return step
        if step.status == "completed_turn":
            self._turn_pending = False
            self._active_turn_request = None
            self._suspended_turn_request = None
            self._trace("session_turn_completed", reason="assistant_final_output")
            self._apply_task_checkpoint_lifecycle(
                intent=intent,
                checkpoint_before=checkpoint_before,
                start_fingerprint=start_fingerprint,
            )
            task = self.task_workspace.current_task if self.task_workspace is not None else None
            if task is not None and task.status in {
                TaskStatus.WAITING_EVENT,
                TaskStatus.PAUSED,
            }:
                self._stand_down()
            elif not self._goal_active:
                self._stand_down()
            self._sync_task_context()
            return SessionStep(step.status, self.parts.lifecycle.state, step.message)
        self._turn_pending = False
        if step.lifecycle is LifecycleState.RESUMING:
            self._queue_resume_successor_if_required()
        self._sync_task_context()
        return step

    def _queue_resume_successor_if_required(self) -> None:
        assert self.parts is not None
        assert self.work_queue is not None
        if self.work_queue.pending_count() > 0:
            return
        task = self.task_workspace.current_task if self.task_workspace is not None else None
        if task is not None:
            if task.status is not TaskStatus.RUNNING:
                self._stand_down()
                return
            kind = WorkIntentKind.TASK_CONTINUE
            task_id = task.task_id
        elif self._goal_active:
            kind = WorkIntentKind.CONTINUE
            task_id = None
        else:
            self._stand_down()
            return
        intent = self.work_queue.enqueue(
            kind,
            source="recovery_completed",
            payload={"reason": "recovery_completed", "text": "", "sender": ""},
            dedupe_key=(
                f"{self._scheduler_id}:resume:"
                f"{self.parts.authority.current_generation()}:"
                f"{self._recovery_attempts}"
            ),
            task_id=task_id,
            generation=self.parts.authority.current_generation(),
        )
        self._trace(
            "recovery_successor_queued",
            intent_id=intent.intent_id,
            kind=kind.value,
            task_id=task_id,
        )

    def _apply_task_checkpoint_lifecycle(
        self,
        *,
        intent: WorkIntent,
        checkpoint_before: str | None,
        start_fingerprint: dict[str, object] | None,
    ) -> None:
        if self.parts is None or self.task_workspace is None:
            return
        task = self.task_workspace.current_task
        if task is None:
            return
        checkpoint = self.task_workspace.store.get_latest_checkpoint(task.task_id)
        if checkpoint is None or checkpoint.checkpoint_id == checkpoint_before:
            if intent.kind in {
                WorkIntentKind.START,
                WorkIntentKind.REPLACE_GOAL,
                WorkIntentKind.CONTINUE,
                WorkIntentKind.BODY_EVENT,
                WorkIntentKind.RECOVERY_RECONCILE,
                WorkIntentKind.TASK_CONTINUE,
            }:
                final_fingerprint = self._current_body_fingerprint()
                parked = self.task_workspace.park_without_continuation(
                    body_fingerprint=final_fingerprint,
                )
                if parked is not None:
                    self._trace(
                        "task_parked_without_continuation",
                        task_id=parked[0].task_id,
                        checkpoint_id=parked[1].checkpoint_id,
                    )
                    self._stand_down()
            return
        if checkpoint.disposition is CheckpointDisposition.CONTINUE:
            final_fingerprint = self._current_body_fingerprint()
            start_value = None if start_fingerprint is None else start_fingerprint.get("fingerprint")
            final_value = None if final_fingerprint is None else final_fingerprint.get("fingerprint")
            continuation_count = self.work_queue.count_for_task(
                WorkIntentKind.TASK_CONTINUE,
                task.task_id,
            )
            rejection_reason = None
            if not start_value or not final_value or start_value == final_value:
                rejection_reason = "task_continue_without_world_progress"
            elif continuation_count >= self.max_task_continuations:
                rejection_reason = "task_continuation_budget_exhausted"
            if rejection_reason is not None:
                rejected = self.task_workspace.reject_continuation(
                    reason=rejection_reason,
                    evidence=(
                        f"checkpoint={checkpoint.checkpoint_id}",
                        f"continuations={continuation_count}",
                    ),
                    body_fingerprint=final_fingerprint,
                )
                self._trace(
                    "task_continue_rejected",
                    reason=rejection_reason,
                    task_id=task.task_id,
                    checkpoint_id=checkpoint.checkpoint_id,
                    continuation_count=continuation_count,
                )
                if rejected is not None and self.parts.lifecycle.state is LifecycleState.ACTIVE:
                    self.parts.lifecycle.yield_()
                else:
                    self._stand_down()
                return
            successor = self.work_queue.enqueue(
                WorkIntentKind.TASK_CONTINUE,
                source="task_checkpoint_continue",
                payload={
                    "checkpoint_id": checkpoint.checkpoint_id,
                    "checkpoint_revision": checkpoint.revision,
                    "next_step": checkpoint.next_step,
                    "summary": checkpoint.summary,
                },
                dedupe_key=f"task_continue:{checkpoint.checkpoint_id}",
                task_id=task.task_id,
                generation=self.parts.authority.current_generation(),
            )
            self._trace(
                "task_continue_queued",
                intent_id=successor.intent_id,
                task_id=task.task_id,
                checkpoint_id=checkpoint.checkpoint_id,
                continuation_count=continuation_count + 1,
                queued=successor.state is WorkIntentState.QUEUED,
            )
            if successor.state is not WorkIntentState.QUEUED:
                self.task_workspace.reject_continuation(
                    reason="task_continue_duplicate_checkpoint",
                    evidence=(f"checkpoint={checkpoint.checkpoint_id}",),
                    body_fingerprint=final_fingerprint,
                )
                if self.parts.lifecycle.state is LifecycleState.ACTIVE:
                    self.parts.lifecycle.yield_()
                else:
                    self._stand_down()
            return
        if checkpoint.disposition is CheckpointDisposition.YIELD:
            if self.parts.lifecycle.state is LifecycleState.ACTIVE:
                self.parts.lifecycle.yield_()
            return
        if checkpoint.disposition in {
            CheckpointDisposition.WAIT_EVENT,
            CheckpointDisposition.COMPLETE,
        }:
            self._stand_down()

    def _latest_checkpoint_id(self) -> str | None:
        if self.task_workspace is None:
            return None
        task = self.task_workspace.current_task
        if task is None:
            return None
        checkpoint = self.task_workspace.store.get_latest_checkpoint(task.task_id)
        return None if checkpoint is None else checkpoint.checkpoint_id

    def _current_body_fingerprint(self) -> dict[str, object] | None:
        if self.parts is None:
            return None
        try:
            state = self.parts.runtime.body.get_state()
            fingerprint = self.parts.authority.fingerprint(state)
        except Exception as exc:
            self._trace(
                "task_fingerprint_failed",
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            return None
        return {
            "fingerprint": fingerprint,
            "pos": list(state.pos),
            "health": state.health,
            "food": state.food,
            "oxygen": state.oxygen,
            "inventory_hash": state.inventory_hash,
            "dimension": state.dimension,
            "missing": state.missing,
        }

    def _body_interrupt(self, reason: str) -> None:
        assert self.parts is not None
        try:
            self.parts.runtime.body.interrupt(reason)
        except Exception as exc:  # pragma: no cover - interruption must not hide command receipt
            self.parts.runtime.trace.emit("body_interrupt_failed", reason=reason, error_type=type(exc).__name__)

    def close(self) -> None:
        if self.parts is not None:
            self.parts.runtime.close()
        if self.work_queue is not None:
            self.work_queue.close()

    async def _run_supervised(
        self,
        work,
        *,
        work_kind: str,
        admission_version: int,
    ):
        task = asyncio.create_task(work)
        assert self.work_queue is not None
        try:
            while not task.done():
                if self.work_queue.notification_version != admission_version:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                    idle = True
                    if self.parts is not None:
                        idle = await self.parts.runtime.wait_for_execution_idle()
                        self._execution_quarantined = not idle
                        self._trace(
                            "session_work_preempted",
                            work_kind=work_kind,
                            execution_idle=idle,
                            pending_count=self.work_queue.pending_count(),
                        )
                    return _WORK_PREEMPTED
                await asyncio.sleep(0.01)
            return task.result()
        except asyncio.CancelledError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            raise

    async def _guard_execution_quarantine(self) -> SessionStep | None:
        if not self._execution_quarantined or self.parts is None:
            return None
        idle = await self.parts.runtime.wait_for_execution_idle(timeout_s=0.0)
        if idle:
            self._execution_quarantined = False
            self._trace("session_execution_quarantine_cleared")
            return None
        self._turn_pending = False
        self._trace(
            "session_execution_quarantined",
            reason="execution_not_idle_after_preempt",
            active_count=self.parts.runtime.execution_lane.active_count,
        )
        if self.parts.lifecycle.state is LifecycleState.ACTIVE:
            self.parts.lifecycle.yield_()
        else:
            self._stand_down()
        return SessionStep(
            "yielded",
            self.parts.lifecycle.state,
            "execution_not_idle_after_preempt",
        )


def _command_from_intent(intent: WorkIntent) -> SessionCommand:
    payload = intent.payload
    try:
        kind = SessionCommandKind(intent.kind.value)
    except ValueError as exc:
        raise ValueError(f"work intent is not a session command: {intent.kind.value}") from exc
    return SessionCommand(
        kind=kind,
        text=str(payload.get("text") or ""),
        reason=str(payload.get("reason") or intent.source),
        sender=str(payload.get("sender") or ""),
    )


def _is_command_intent(intent: WorkIntent) -> bool:
    return intent.kind.value in {kind.value for kind in SessionCommandKind}

__all__ = [
    "AgentSession",
    "SessionCommand",
    "SessionCommandKind",
    "SessionStep",
]
