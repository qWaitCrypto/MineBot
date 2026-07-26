"""Serialized Body execution lane and its typed failures.

Extracted verbatim from ``app/runner.py`` (brain-cognitive-framework.md §12
H1/B): the lane is spine machinery with no capability knowledge, so it reads
and tests better beside the runner than inside it. Behavior is unchanged.

One physical writer means one lane: every mutating tool call runs on this
single worker thread, and cancellation is cooperative so a Body request can
settle before the lane reports idle.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from concurrent.futures import CancelledError as FutureCancelledError
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from minebot.contract import (
    ExecutionCancellation,
    ExecutionCancelled,
    execution_cancellation_scope,
)

EXECUTION_LANE_POLL_S = 0.01
EXECUTION_LANE_CANCEL_TIMEOUT_S = 30.0


class SerialExecutionLane:
    """Run synchronous Body work off-loop and serialize it per runtime."""

    def __init__(self, *, thread_name: str = "minebot-body") -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=thread_name)
        self._futures: dict[Future[Any], ExecutionCancellation] = {}
        self._lock = threading.Lock()
        self._closed = False

    async def run(
        self,
        callback: Callable[..., Any],
        *args: object,
        timeout_s: float | None = None,
    ) -> Any:
        if timeout_s is not None and timeout_s <= 0:
            raise ValueError("execution timeout_s must be > 0")
        submitted_at = time.monotonic()
        started = threading.Event()
        started_at: list[float] = []
        cancellation = ExecutionCancellation()

        def invoke() -> Any:
            started_at.append(time.monotonic())
            started.set()
            with execution_cancellation_scope(cancellation):
                return callback(*args)

        with self._lock:
            if self._closed:
                raise RuntimeError("execution lane is closed")
            future = self._executor.submit(invoke)
            self._futures[future] = cancellation
        future.add_done_callback(self._discard)
        try:
            while not future.done():
                if timeout_s is not None and started.is_set():
                    elapsed_s = time.monotonic() - started_at[0]
                    if elapsed_s >= timeout_s:
                        cancellation.cancel("execution_timeout")
                        future.cancel()
                        raise ToolExecutionTimeout(
                            timeout_s=timeout_s,
                            execution_elapsed_s=elapsed_s,
                            queue_wait_s=started_at[0] - submitted_at,
                        )
                await asyncio.sleep(EXECUTION_LANE_POLL_S)
            if future.cancelled():
                raise asyncio.CancelledError
            try:
                return future.result()
            except (FutureCancelledError, ExecutionCancelled) as exc:
                raise asyncio.CancelledError from exc
        except asyncio.CancelledError:
            cancellation.cancel("asyncio_cancelled")
            future.cancel()
            raise

    def request_cancel(self, reason: str) -> int:
        """Signal every running or queued callback without violating serialization."""

        with self._lock:
            pending = list(self._futures.items())
        cancellation_scope_count = sum(
            not future.done() for future, _cancellation in pending
        )
        for future, cancellation in pending:
            cancellation.cancel(reason)
            future.cancel()
        return cancellation_scope_count

    async def wait_idle(self, *, timeout_s: float = EXECUTION_LANE_CANCEL_TIMEOUT_S) -> bool:
        deadline = time.monotonic() + max(0.0, timeout_s)
        while self.active_count:
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(EXECUTION_LANE_POLL_S)
        return True

    @property
    def active_count(self) -> int:
        with self._lock:
            return sum(not future.done() for future in self._futures)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.request_cancel("execution_lane_closed")
        self._executor.shutdown(wait=True, cancel_futures=True)

    def _discard(self, future: Future[Any]) -> None:
        with self._lock:
            self._futures.pop(future, None)


class ToolExecutionTimeout(TimeoutError):
    """A tool exceeded its budget after entering the serialized execution lane."""

    def __init__(
        self,
        *,
        timeout_s: float,
        execution_elapsed_s: float,
        queue_wait_s: float,
    ) -> None:
        super().__init__(f"tool execution exceeded {timeout_s:.3f}s")
        self.diagnostics = {
            "timeout_s": timeout_s,
            "execution_elapsed_s": execution_elapsed_s,
            "queue_wait_s": queue_wait_s,
        }


class BodyRecoveryRequired(RuntimeError):
    """Raised when a Body-critical fact must preempt the model turn."""

    def __init__(self, reason: str, *, facts: dict[str, object] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.facts = dict(facts or {})


__all__ = [
    "SerialExecutionLane",
    "ToolExecutionTimeout",
    "BodyRecoveryRequired",
]
