"""Long-lived Agent session supervisor for Phase 1.

This is an app-layer object: it owns the outer autonomous/session loop around
the SDK inner `Runner.run` turns. It may see the runtime, Body, lifecycle, and
future conversation entrypoints; it must not live in `brain/`.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from minebot.app.wiring import AgentRuntimeParts
from minebot.brain.lifecycle import LifecycleError, LifecycleState
from minebot.brain.modes import AgentSignal


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
    max_auto_turns_per_step: int = 1

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
        """Drain queued user commands, then advance active work by bounded turns."""
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

        runnable_states = {LifecycleState.INIT, LifecycleState.IDLE, LifecycleState.ACTIVE, LifecycleState.RESUMING}
        if self.parts.lifecycle.state not in runnable_states:
            return SessionStep("waiting", self.parts.lifecycle.state)

        outcome = None
        for index in range(max(1, self.max_auto_turns_per_step)):
            extra = signals if index == 0 else None
            try:
                outcome = await self.parts.runtime.run_turn(extra_signals=extra)
            except Exception as exc:
                self._trace(
                    "session_step_failed",
                    error_type=type(exc).__name__,
                    lifecycle=self.parts.lifecycle.state.value,
                )
                self._stand_down()
                return SessionStep("failed", self.parts.lifecycle.state, f"runtime_error:{type(exc).__name__}")
            if outcome.lifecycle is not LifecycleState.ACTIVE or outcome.status == "yielded":
                break
        if outcome is None:
            return SessionStep("waiting", self.parts.lifecycle.state)
        return SessionStep(outcome.status, outcome.lifecycle, outcome.message)

    async def run_until_waiting(self, *, max_steps: int = 100, should_stop: ShouldStop | None = None) -> SessionStep:
        """Run bounded session steps until no automatic ACTIVE work remains."""
        last = await self.step()
        if should_stop is not None and should_stop(last):
            return last
        for _ in range(max(0, max_steps - 1)):
            if last.lifecycle is not LifecycleState.ACTIVE:
                return last
            if self.pending:
                last = await self.step()
            else:
                last = await self.step()
            if should_stop is not None and should_stop(last):
                return last
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
        if state in {LifecycleState.YIELDED, LifecycleState.INTERRUPTED, LifecycleState.RECOVERING}:
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
