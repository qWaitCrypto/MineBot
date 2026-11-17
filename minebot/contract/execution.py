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
    "execution_receipt_scope",
    "current_execution_receipt",
]


# Captured when a Body action is admitted, before its IPC worker starts.
# This scope labels observations; it does not own or consume terminals.
_BODY_CLAIM_GENERATION: ContextVar[int | None] = ContextVar("body_claim_generation", default=None)


@contextmanager
def body_claim_scope(generation: int) -> Iterator[None]:
    token = _BODY_CLAIM_GENERATION.set(generation)
    try:
        yield
    finally:
        _BODY_CLAIM_GENERATION.reset(token)


def current_claim_generation() -> int | None:
    return _BODY_CLAIM_GENERATION.get()


_TOOL_OBSERVATION: ContextVar[tuple[str, Callable[[JsonObject], None]] | None] = ContextVar(
    "minebot_tool_observation", default=None,
)

# Internal receipt owned by the execution channel. Body tools must not add
# delivery fields to their public ToolResult; the serialized lane records the
# fact after the tool has returned a successful terminal.
_EXECUTION_RECEIPT: ContextVar[dict[str, object] | None] = ContextVar(
    "minebot_execution_receipt", default=None,
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


# A walking tool called with look_down (user 2026-11-05: 默认平视，传参之后就低头):
# every Body action it starts walks looking at the ground. Only the caller's
# argument sets it; nothing turns it on by itself.
# A walking tool called without the argument opens the scope with None: the
# Agent's standing walk gaze (set_walk_gaze; user 2026-11-05: 死亡之后刷新)
# then decides for every action it starts.
_WALK_LOOK_DOWN: ContextVar[bool | None] = ContextVar("minebot_walk_look_down", default=None)
_IN_WALKING_TOOL: ContextVar[bool] = ContextVar("minebot_in_walking_tool", default=False)


@contextmanager
def walk_look_down_scope(look_down: bool | None) -> Iterator[None]:
    token = _WALK_LOOK_DOWN.set(None if look_down is None else bool(look_down))
    walking = _IN_WALKING_TOOL.set(True)
    try:
        yield
    finally:
        _IN_WALKING_TOOL.reset(walking)
        _WALK_LOOK_DOWN.reset(token)


def current_walk_look_down(standing: bool = False, *, walking_action: bool = True) -> bool:
    """Whether a Body action walks looking at the ground.

    The caller's explicit argument wins (true or false). Without one, the
    Agent's standing setting applies to every action inside a walking tool
    and, outside one (Decision's own moves, plan internals), to walking
    actions only.
    """
    explicit = _WALK_LOOK_DOWN.get()
    if explicit is not None:
        return explicit
    return bool(standing) and (walking_action or _IN_WALKING_TOOL.get())


@contextmanager
def execution_receipt_scope(receipt: dict[str, object]) -> Iterator[None]:
    token = _EXECUTION_RECEIPT.set(receipt)
    try:
        yield
    finally:
        _EXECUTION_RECEIPT.reset(token)


def current_execution_receipt() -> dict[str, object] | None:
    return _EXECUTION_RECEIPT.get()
