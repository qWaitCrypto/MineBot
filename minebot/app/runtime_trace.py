"""Structured runtime trace sink (harness block #5).

Extracted verbatim from ``app/runner.py``. Every record is sanitized before
it reaches a sink, which is what keeps the no-secrets-in-logs constraint a
property of the trace rather than of each call site.
"""

from __future__ import annotations

import bisect
import threading
import time
from dataclasses import dataclass, field

from minebot.app.observability import ObservationSink, sanitize_observation


@dataclass
class RuntimeTrace:
    """In-memory trace sink for Phase-1 turn/tool observability.

    The sink receives every record. A long-lived session sets
    ``retain_limit`` so memory keeps only the most recent events, plus the
    low-rate ``durable_events`` that callers look up across a whole turn.
    Unbounded retention made a 5-hour session hold 1.7 GB of records, and
    collecting that heap stopped the event loop for 4-5 seconds.
    """

    session_id: str = "default"
    sink: ObservationSink | None = None
    events: list[dict[str, object]] = field(default_factory=list)
    retain_limit: int | None = None
    durable_events: frozenset[str] = frozenset()
    _seq: int = 0
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def emit(self, event: str, **fields: object) -> None:
        self._emit(event, fields, retain=True)

    def emit_transient(self, event: str, **fields: object) -> None:
        """Sink-only record for high-rate observer telemetry; never kept in ``events``."""
        self._emit(event, fields, retain=False)

    def _emit(self, event: str, fields: dict[str, object], *, retain: bool) -> None:
        with self._lock:
            self._seq += 1
            record = sanitize_observation(
                {
                    "seq": self._seq,
                    "ts": time.time(),
                    "session_id": self.session_id,
                    "event": event,
                    **fields,
                }
            )
            if retain:
                self.events.append(record)
                if self.retain_limit is not None and len(self.events) > 2 * self.retain_limit:
                    self._trim()
            if self.sink is not None:
                self.sink.write(record)

    def _trim(self) -> None:
        # Trim in halves so appends stay amortized O(1). Durable events keep
        # their own cap of the same size and stay ahead of the recent tail,
        # so ``events`` remains ordered by seq.
        limit = self.retain_limit or 0
        head, tail = self.events[:-limit], self.events[-limit:]
        durable = [event for event in head if event.get("event") in self.durable_events]
        self.events = durable[-limit:] + tail

    def snapshot(self) -> list[dict[str, object]]:
        with self._lock:
            return [dict(event) for event in self.events]

    def events_since(self, after_seq: int) -> list[dict[str, object]]:
        """Copies of the retained events whose seq is greater than ``after_seq``."""
        with self._lock:
            start = bisect.bisect_right(self.events, after_seq, key=lambda event: int(event.get("seq") or 0))
            return [dict(event) for event in self.events[start:]]

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._seq

    def close(self) -> None:
        with self._lock:
            if self.sink is not None:
                self.sink.close()


__all__ = [
    "RuntimeTrace",
]
