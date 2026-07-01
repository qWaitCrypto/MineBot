"""Long-lived Agent session supervisor for Phase 1.

This is an app-layer object: it owns the outer autonomous/session loop around
the SDK inner `Runner.run` turns. It may see the runtime, Body, lifecycle, and
future conversation entrypoints; it must not live in `brain/`.
"""

from __future__ import annotations

import inspect
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


@dataclass(frozen=True)
class SessionCommand:
    kind: SessionCommandKind
    text: str = ""
    reason: str = ""

    @classmethod
    def start(cls, goal: str) -> "SessionCommand":
        return cls(SessionCommandKind.START, text=goal, reason="goal_started")

    @classmethod
    def pause(cls, reason: str = "user_pause") -> "SessionCommand":
        return cls(SessionCommandKind.PAUSE, reason=reason)

    @classmethod
    def continue_(cls, text: str = "") -> "SessionCommand":
        return cls(SessionCommandKind.CONTINUE, text=text, reason="user_continue")

    @classmethod
    def cancel(cls, reason: str = "user_cancel") -> "SessionCommand":
        return cls(SessionCommandKind.CANCEL, reason=reason)

    @classmethod
    def replace_goal(cls, goal: str) -> "SessionCommand":
        return cls(SessionCommandKind.REPLACE_GOAL, text=goal, reason="goal_replaced")

    @classmethod
    def message(cls, text: str) -> "SessionCommand":
        return cls(SessionCommandKind.MESSAGE, text=text, reason="user_message")


@dataclass(frozen=True)
class SessionStep:
    status: str
    lifecycle: LifecycleState
    message: str | None = None


PartsFactory = Callable[[str], AgentRuntimeParts]
ShouldStop = Callable[[SessionStep], bool]


@dataclass
class AgentSession:
    """Persistent outer session around one AgentRuntimeParts instance."""

    parts_factory: PartsFactory
    parts: AgentRuntimeParts | None = None
    pending: deque[SessionCommand] = field(default_factory=deque)
    max_recovery_attempts: int = 3
    _recovery_attempts: int = 0

    def submit(self, command: SessionCommand) -> None:
        self.pending.append(command)
        if self.parts is not None and command.kind in {SessionCommandKind.PAUSE, SessionCommandKind.CANCEL}:
            self._body_interrupt(command.reason)

    @property
    def lifecycle_state(self) -> LifecycleState | None:
        if self.parts is None:
            return None
        return self.parts.lifecycle.state

    @property
    def current_goal(self) -> str | None:
        if self.parts is None:
            return None
        return self.parts.context.goal_text

    async def step(self) -> SessionStep:
        """Drain queued user commands, then advance active work by one SDK run."""
        signals: list[AgentSignal] = []
        suppress_run = False
        while self.pending:
            command = self.pending.popleft()
            signals.extend(await self._apply_command(command))
            if command.kind is SessionCommandKind.CANCEL:
                suppress_run = True

        if self.parts is None:
            return SessionStep("idle", LifecycleState.IDLE, "no active goal")

        if suppress_run:
            return SessionStep("waiting", self.parts.lifecycle.state)

        if self.parts.lifecycle.state is LifecycleState.RECOVERING:
            return await self._drive_recovery()

        runnable_states = {LifecycleState.INIT, LifecycleState.IDLE, LifecycleState.ACTIVE, LifecycleState.RESUMING}
        if self.parts.lifecycle.state not in runnable_states:
            return SessionStep("waiting", self.parts.lifecycle.state)

        try:
            outcome = await self.parts.runtime.run_turn(extra_signals=signals)
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
            return SessionStep("failed", self.parts.lifecycle.state, f"runtime_error:{type(exc).__name__}")
        return SessionStep(outcome.status, outcome.lifecycle, outcome.message)

    async def _drive_recovery(self) -> SessionStep:
        assert self.parts is not None
        handler = self.parts.runtime.recovery_handler
        if handler is None:
            self._trace("session_recovery_missing_driver", lifecycle=self.parts.lifecycle.state.value)
            return self._yield_recovery_failure("recovery_driver_missing", {})
        self._recovery_attempts += 1
        try:
            outcome = handler(self.parts.runtime)
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
        recovered = await self.parts.runtime.run_turn(
            extra_signals=[AgentSignal.recovery_completed(outcome.reason, **outcome.facts)]
        )
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
        self._trace("session_goal_completed", goal=self.parts.context.goal_text, reason=reason)
        self._stand_down()
        return SessionStep("completed", self.parts.lifecycle.state, reason)

    async def _apply_command(self, command: SessionCommand) -> list[AgentSignal]:
        if command.kind is SessionCommandKind.START:
            self.parts = self.parts_factory(command.text)
            self.parts.context.observe_user_message(command.text)
            self._trace("user_message", command="start", content=command.text)
            return [AgentSignal.goal_started(command.text)]

        if self.parts is None:
            return []

        if command.kind is SessionCommandKind.MESSAGE:
            self.parts.context.observe_user_message(command.text)
            self._trace("user_message", command="message", content=command.text)
            return []

        if command.kind is SessionCommandKind.REPLACE_GOAL:
            self.parts.context.set_goal(command.text)
            self.parts.context.observe_user_message(command.text)
            self.parts.runtime.weld_context.goal_text = command.text
            self.parts.authority.invalidate_generation("goal_replaced")
            self._trace("user_message", command="replace_goal", content=command.text)
            return [AgentSignal.goal_started(command.text)]

        if command.kind is SessionCommandKind.PAUSE:
            self.parts.context.observe_user_message(command.reason)
            self._trace("user_message", command="pause", reason=command.reason)
            return [AgentSignal.user_interrupt(command.reason)]

        if command.kind is SessionCommandKind.CONTINUE:
            if command.text:
                self.parts.context.observe_user_message(command.text)
            self._trace("user_message", command="continue", content=command.text)
            self._resume_if_waiting()
            if command.text:
                self.parts.context.set_goal(command.text)
                self.parts.runtime.weld_context.goal_text = command.text
                return [AgentSignal.goal_started(command.text)]
            return []

        if command.kind is SessionCommandKind.CANCEL:
            self.parts.context.observe_user_message(command.reason)
            self._trace("user_message", command="cancel", reason=command.reason)
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
            self.parts.lifecycle.resume()

    def _stand_down(self) -> None:
        assert self.parts is not None
        state = self.parts.lifecycle.state
        try:
            if state is LifecycleState.INIT:
                self.parts.lifecycle.ready()
            if self.parts.lifecycle.state is LifecycleState.ACTIVE:
                self.parts.lifecycle.yield_()
            if self.parts.lifecycle.state in {LifecycleState.YIELDED, LifecycleState.INTERRUPTED, LifecycleState.RECOVERING}:
                self.parts.lifecycle.stand_down()
        except LifecycleError:
            self._trace("session_lifecycle_error", action="stand_down", state=self.parts.lifecycle.state.value)

    def _trace(self, event: str, **fields: object) -> None:
        if self.parts is not None:
            self.parts.runtime.trace.emit(event, **fields)

    def _body_interrupt(self, reason: str) -> None:
        assert self.parts is not None
        try:
            self.parts.runtime.body.interrupt(reason)
        except Exception as exc:  # pragma: no cover - interruption must not hide command receipt
            self.parts.runtime.trace.emit("body_interrupt_failed", reason=reason, error_type=type(exc).__name__)


__all__ = [
    "AgentSession",
    "SessionCommand",
    "SessionCommandKind",
    "SessionStep",
]
