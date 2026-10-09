"""Cooperative cancellation for synchronous Body transaction execution."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator, Callable
from contextlib import contextmanager
from contextvars import ContextVar

from .messages import Action, JsonObject


class ExecutionCancelled(BaseException):
    """The active synchronous execution scope was cancelled by its supervisor."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ExecutionCancellation:
    """Thread-safe cancellation state shared with one execution-lane callback."""

    def __init__(self, *, deadline: float | None = None) -> None:
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._reason = "execution_cancelled"
        self.deadline = deadline

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    @property
    def reason(self) -> str:
        with self._lock:
            return self._reason

    def cancel(self, reason: str) -> bool:
        clean_reason = str(reason).strip() or "execution_cancelled"
        with self._lock:
            if self._cancelled.is_set():
                return False
            self._reason = clean_reason
            self._cancelled.set()
            return True

    def checkpoint(self) -> None:
        if self._cancelled.is_set():
            raise ExecutionCancelled(self.reason)


_CURRENT_EXECUTION_CANCELLATION: ContextVar[ExecutionCancellation | None] = ContextVar(
    "minebot_execution_cancellation",
    default=None,
)


@contextmanager
def execution_cancellation_scope(cancellation: ExecutionCancellation) -> Iterator[None]:
    """Bind one cancellation token to the current synchronous execution scope."""

    binding = _CURRENT_EXECUTION_CANCELLATION.set(cancellation)
    try:
        cancellation.checkpoint()
        yield
    finally:
        _CURRENT_EXECUTION_CANCELLATION.reset(binding)


def execution_checkpoint() -> None:
    """Stop cooperative synchronous work when its owning scope was cancelled."""

    cancellation = _CURRENT_EXECUTION_CANCELLATION.get()
    if cancellation is not None:
        cancellation.checkpoint()


def action_with_timeout(action: Action, timeout_s: float) -> Action:
    """Give the executor and terminal waiter the same finite action budget.

    A transaction's child action cannot outlive its owning tool invocation.
    The extra wait allowance is for receipt delivery and stopping controls,
    not additional physical work.
    """
    execution_checkpoint()
    params = dict(action.params)
    if "timeout_ms" in params:
        budget = float(params["timeout_ms"]) / 1000
    elif "timeout_ticks" in params:
        budget = float(params["timeout_ticks"]) / 20
    elif "timeout_s" in params:
        budget = float(params["timeout_s"])
    else:
        budget = float(timeout_s)
    budget = min(budget, float(timeout_s), 300.0)
    cancellation = _CURRENT_EXECUTION_CANCELLATION.get()
    if cancellation is not None and cancellation.deadline is not None:
        budget = min(budget, cancellation.deadline - time.monotonic())
    if budget <= 0:
        raise ExecutionCancelled("execution_timeout")
    params.update(timeout_ms=max(1, int(budget * 1000)), timeout_ticks=max(1, int(budget * 20)))
    return Action(id=action.id, name=action.name, params=params)


def action_wait_timeout_s(action: Action) -> float:
    return float(action.params["timeout_ms"]) / 1000 + 5.0


__all__ = [
    "ExecutionCancellation",
    "ExecutionCancelled",
    "execution_cancellation_scope",
    "execution_checkpoint",
    "action_with_timeout",
    "action_wait_timeout_s",
]


# Captured when a Body action is admitted, before its IPC worker starts.
# This scope labels observations; it does not own or consume terminals.
_TOOL_OBSERVATION: ContextVar[tuple[str, Callable[[JsonObject], None]] | None] = ContextVar(
    "minebot_tool_observation", default=None,
)


@contextmanager
def tool_observation_scope(call_id: str, notify: Callable[[JsonObject], None]) -> Iterator[None]:
    token = _TOOL_OBSERVATION.set((call_id, notify))
    try:
        yield
    finally:
        _TOOL_OBSERVATION.reset(token)


def current_tool_observation() -> tuple[str, Callable[[JsonObject], None]] | None:
    return _TOOL_OBSERVATION.get()
