"""Event-loop stall watchdog.

The Body loop, the Decision channel and the IPC readers share one asyncio
loop with the Agent's bookkeeping. When synchronous work holds the loop, every
real-time layer stops at once: in the 2026-10-16 villa run the whole trace was
silent for 2974 s in 369 stretches of 2.5-15 s, and a Decision request bounded
by ``wait_for(2.0)`` came back after 15 s (living-continuity-20261017 §1.2).
Nothing recorded who held the loop.

A heartbeat task marks the loop alive. A daemon thread notices when the mark
is late and samples the loop thread's stack while it is still blocked; the
next heartbeat reports the stall with those samples and the garbage-collection
time spent inside it. The watchdog only observes; it never interrupts the loop.
"""

from __future__ import annotations

import asyncio
import gc
import sys
import threading
import time
import traceback
from typing import Any

STALL_THRESHOLD_S = 0.5
HEARTBEAT_INTERVAL_S = 0.1
# Later samples show whether one call held the loop throughout or several did.
SAMPLE_AT_S = (STALL_THRESHOLD_S, 2.0, 5.0)
STACK_FRAMES = 24


class LoopWatchdog:
    def __init__(self, trace: Any, *, threshold_s: float = STALL_THRESHOLD_S,
                 interval_s: float = HEARTBEAT_INTERVAL_S,
                 sample_at_s: tuple[float, ...] = SAMPLE_AT_S) -> None:
        self.trace = trace
        self.threshold_s = threshold_s
        self.interval_s = interval_s
        self.sample_at_s = tuple(sorted(sample_at_s))
        # Reentrant: a collection can start while the loop thread holds it.
        self._lock = threading.RLock()
        self._beat = time.monotonic()
        self._samples: list[dict[str, object]] = []
        self._gc_s = 0.0
        self._gc_started: float | None = None
        self._loop_thread: int | None = None
        self._task: asyncio.Task[None] | None = None
        self._thread: threading.Thread | None = None
        self._stopped = threading.Event()

    def start(self) -> None:
        if self._task is not None:
            return
        self._loop_thread = threading.get_ident()
        self._beat = time.monotonic()
        self._stopped.clear()
        gc.callbacks.append(self._on_gc)
        self._task = asyncio.get_running_loop().create_task(self._heartbeat())
        self._thread = threading.Thread(target=self._watch, name="loop-watchdog", daemon=True)
        self._thread.start()

    async def stop(self) -> None:
        self._stopped.set()
        if self._on_gc in gc.callbacks:
            gc.callbacks.remove(self._on_gc)
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=1.0)

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(self.interval_s)
            now = time.monotonic()
            with self._lock:
                late = now - self._beat - self.interval_s
                samples, self._samples = self._samples, []
                gc_s, self._gc_s = self._gc_s, 0.0
                self._beat = now
            if late >= self.threshold_s:
                self.trace.emit(
                    "event_loop_stalled", stalled_s=round(late, 3), gc_s=round(gc_s, 3),
                    samples=samples,
                )

    def _watch(self) -> None:
        sampled_beat = None
        taken = 0
        while not self._stopped.wait(self.interval_s / 2):
            with self._lock:
                beat = self._beat
            if beat != sampled_beat:
                sampled_beat, taken = beat, 0
            behind = time.monotonic() - beat - self.interval_s
            if taken < len(self.sample_at_s) and behind >= self.sample_at_s[taken]:
                sample = {"at_s": round(behind, 3), "stack": self._loop_stack()}
                with self._lock:
                    if self._beat == beat:
                        self._samples.append(sample)
                taken += 1

    def _loop_stack(self) -> list[str]:
        frame = sys._current_frames().get(self._loop_thread or -1)
        if frame is None:
            return []
        return [line.rstrip() for line in traceback.format_stack(frame)[-STACK_FRAMES:]]

    def _on_gc(self, phase: str, info: dict[str, Any]) -> None:
        # Only collections on the loop thread stall it; threads collect too.
        if threading.get_ident() != self._loop_thread:
            return
        if phase == "start":
            self._gc_started = time.monotonic()
        elif self._gc_started is not None:
            elapsed = time.monotonic() - self._gc_started
            self._gc_started = None
            with self._lock:
                self._gc_s += elapsed


__all__ = ["LoopWatchdog"]
