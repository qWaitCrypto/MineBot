"""Long-lived Agent session supervisor for Phase 1.

This is an app-layer object: it owns the outer autonomous/session loop around
the SDK inner `Runner.run` turns. It may see the runtime, Body, lifecycle, and
future conversation entrypoints; it must not live in `brain/`.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from minebot.app.wiring import AgentRuntimeParts
from minebot.app.runner import RecoveryOutcome
from minebot.brain.lifecycle import LifecycleError, LifecycleState
from minebot.brain.modes import AgentSignal

DEFAULT_RUNAWAY_STEP_LIMIT = 100_000


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
GoalDriver = Callable[[AgentRuntimeParts, list[AgentSignal]], SessionStep | None]
_WORK_PREEMPTED = object()


@dataclass
class AgentSession:
    """Persistent outer session around one AgentRuntimeParts instance."""

    parts_factory: PartsFactory
    goal_driver: GoalDriver | None = None
    parts: AgentRuntimeParts | None = None
    pending: deque[SessionCommand] = field(default_factory=deque)
    max_recovery_attempts: int = 3
    _recovery_attempts: int = 0
    _goal_driver_keys: set[str] = field(default_factory=set)
    _goal_active: bool = False
    _turn_pending: bool = False
    _active_turn_request: tuple[str, str] | None = None
    _suspended_turn_request: tuple[str, str] | None = None
    _work_in_flight: bool = False
    _execution_quarantined: bool = False
    _pending_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _command_available: threading.Event = field(default_factory=threading.Event, repr=False)

    def submit(self, command: SessionCommand) -> None:
        with self._pending_lock:
            self.pending.append(command)
            self._command_available.set()
        always_interrupt = command.kind in {
            SessionCommandKind.PAUSE,
            SessionCommandKind.CANCEL,
            SessionCommandKind.REPLACE_GOAL,
            SessionCommandKind.QUIT,
        }
        if self.parts is not None and (self._work_in_flight or always_interrupt):
            self.parts.authority.invalidate_generation(f"session_command:{command.kind.value}")
            self._body_interrupt(command.reason)

    @property
    def lifecycle_state(self) -> LifecycleState | None:
        if self.parts is None:
            return None
        return self.parts.lifecycle.state

    @property
    def current_goal(self) -> str | None:
        if self.parts is None or not self._goal_active:
            return None
        return self.parts.context.goal_text

    @property
    def has_active_goal(self) -> bool:
        return self.parts is not None and self._goal_active

    @property
    def has_pending_work(self) -> bool:
        if self._command_available.is_set() or self._turn_pending:
            return True
        return self.parts is not None and self.parts.lifecycle.state in {
            LifecycleState.RECOVERING,
            LifecycleState.RESUMING,
        }

    async def step(self) -> SessionStep:
        """Drain queued user commands, then advance active work by one SDK run."""
        signals: list[AgentSignal] = []
        suppress_run = False
        quit_requested = False
        for command in self._drain_commands():
            if command.kind is SessionCommandKind.QUIT:
                quit_requested = True
            signals.extend(await self._apply_command(command))
            if command.kind is SessionCommandKind.CANCEL:
                suppress_run = True

        if quit_requested:
            lifecycle = self.parts.lifecycle.state if self.parts is not None else LifecycleState.IDLE
            return SessionStep("quit", lifecycle, "user_quit")

        if self.parts is None:
            return SessionStep("idle", LifecycleState.IDLE, "no active goal")

        quarantine = await self._guard_execution_quarantine()
        if quarantine is not None:
            return quarantine

        if suppress_run:
            return SessionStep("waiting", self.parts.lifecycle.state)

        if not self._turn_pending and self.parts.lifecycle.state not in {
            LifecycleState.RECOVERING,
            LifecycleState.RESUMING,
        }:
            status = "waiting" if self._goal_active else "idle"
            return SessionStep(status, self.parts.lifecycle.state, "no pending turn")

        if self.parts.lifecycle.state is LifecycleState.RECOVERING:
            recovered = await self._run_supervised(self._drive_recovery(), work_kind="recovery")
            if recovered is _WORK_PREEMPTED:
                return SessionStep("preempted", self.parts.lifecycle.state, "user_input")
            return self._finish_step(recovered)

        runnable_states = {LifecycleState.INIT, LifecycleState.IDLE, LifecycleState.ACTIVE, LifecycleState.RESUMING}
        if self.parts.lifecycle.state not in runnable_states:
            return SessionStep("waiting", self.parts.lifecycle.state)

        driven = await self._run_supervised(
            self._drive_goal_once_if_available(signals),
            work_kind="goal_driver",
        )
        if driven is _WORK_PREEMPTED:
            return SessionStep("preempted", self.parts.lifecycle.state, "user_input")
        if driven is not None:
            return driven

        try:
            outcome = await self._run_supervised(
                self.parts.runtime.run_turn(extra_signals=signals),
                work_kind="agent_turn",
            )
            if outcome is _WORK_PREEMPTED:
                return SessionStep("preempted", self.parts.lifecycle.state, "user_input")
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
        return self._finish_step(SessionStep(outcome.status, outcome.lifecycle, outcome.message))

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
        driven = await self._drive_goal_once_if_available(signals)
        if driven is not None:
            return driven
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

    def complete_current_goal(self, reason: str = "goal_completed") -> SessionStep:
        """Stand down after authoritative terminal truth has completed a goal."""
        if self.parts is None:
            return SessionStep("idle", LifecycleState.IDLE, "no active goal")
        completed_goal = self.parts.context.goal_text
        self._trace("session_goal_completed", goal=completed_goal, reason=reason)
        self.parts.context.discard_pending_turn_input()
        self.parts.context.observe_system_message(
            f"Goal completed: {completed_goal}. Terminal reason: {reason}."
        )
        self._goal_active = False
        self._turn_pending = False
        self._active_turn_request = None
        self._suspended_turn_request = None
        self._goal_driver_keys.clear()
        self._stand_down()
        self.parts.context.set_goal("")
        self.parts.runtime.weld_context.goal_text = ""
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
            self._goal_driver_keys.clear()
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
                self.parts = self.parts_factory("")
            if not self._goal_active:
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
            self.parts.context.observe_user_message(command.text, sender=command.sender or None)
            self._goal_driver_keys.clear()
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
            return [AgentSignal.goal_started(command.text)]

        if self.parts is None:
            return []

        if command.kind is SessionCommandKind.PAUSE:
            if not self._goal_active and self._active_turn_request is not None:
                self._suspended_turn_request = self._active_turn_request
            self.parts.context.observe_user_message(command.reason, sender=command.sender or None)
            self._trace("user_message", command="pause", reason=command.reason, sender=command.sender)
            self._turn_pending = True
            return [AgentSignal.user_interrupt(command.reason)]

        if command.kind is SessionCommandKind.CONTINUE:
            if command.text:
                self.parts.context.observe_user_message(command.text, sender=command.sender or None)
            self._trace("user_message", command="continue", content=command.text, sender=command.sender)
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
            self._goal_driver_keys.clear()
            self._stand_down()
            return []

        return []

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

    async def _drive_goal_once_if_available(self, signals: list[AgentSignal]) -> SessionStep | None:
        if self.goal_driver is None or self.parts is None or not self._goal_active:
            return None
        goal = self.parts.context.goal_text
        if goal in self._goal_driver_keys:
            return None
        driven = await self.parts.runtime.run_sync(self.goal_driver, self.parts, signals)
        if driven is None:
            self._goal_driver_keys.add(goal)
            return None
        if driven.status != "stopped" and driven.lifecycle not in {LifecycleState.RECOVERING, LifecycleState.RESUMING}:
            self._goal_driver_keys.add(goal)
        return driven

    def _finish_step(self, step: SessionStep) -> SessionStep:
        if self.parts is None:
            return step
        if step.status == "completed_turn":
            self._turn_pending = False
            self._active_turn_request = None
            self._suspended_turn_request = None
            self._trace("session_turn_completed", reason="assistant_final_output")
            if not self._goal_active:
                self._stand_down()
            return SessionStep(step.status, self.parts.lifecycle.state, step.message)
        if step.lifecycle not in {LifecycleState.RECOVERING, LifecycleState.RESUMING}:
            self._turn_pending = False
        return step

    def _body_interrupt(self, reason: str) -> None:
        assert self.parts is not None
        try:
            self.parts.runtime.body.interrupt(reason)
        except Exception as exc:  # pragma: no cover - interruption must not hide command receipt
            self.parts.runtime.trace.emit("body_interrupt_failed", reason=reason, error_type=type(exc).__name__)

    def close(self) -> None:
        if self.parts is not None:
            self.parts.runtime.close()

    def _drain_commands(self) -> list[SessionCommand]:
        with self._pending_lock:
            commands = list(self.pending)
            self.pending.clear()
            self._command_available.clear()
        return commands

    async def _run_supervised(self, work, *, work_kind: str):
        task = asyncio.create_task(work)
        self._work_in_flight = True
        try:
            while not task.done():
                if self._command_available.is_set():
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
                            pending_count=len(self.pending),
                        )
                    return _WORK_PREEMPTED
                await asyncio.sleep(0.01)
            return task.result()
        except asyncio.CancelledError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            raise
        finally:
            self._work_in_flight = False

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


__all__ = [
    "AgentSession",
    "SessionCommand",
    "SessionCommandKind",
    "SessionStep",
]
