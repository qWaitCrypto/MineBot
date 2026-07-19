"""Cooperative cancellation for synchronous Body transaction execution."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar


class ExecutionCancelled(BaseException):
    """The active synchronous execution scope was cancelled by its supervisor."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ExecutionCancellation:
    """Thread-safe cancellation state shared with one execution-lane callback."""

    def __init__(self) -> None:
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._reason = "execution_cancelled"

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


__all__ = [
    "ExecutionCancellation",
    "ExecutionCancelled",
    "execution_cancellation_scope",
    "execution_checkpoint",
]
