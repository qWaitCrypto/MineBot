"""Structured runtime trace sink (harness block #5).

Extracted verbatim from ``app/runner.py``. Every record is sanitized before
it reaches a sink, which is what keeps the no-secrets-in-logs constraint a
property of the trace rather than of each call site.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from minebot.app.observability import ObservationSink, sanitize_observation


@dataclass
class RuntimeTrace:
    """In-memory trace sink for Phase-1 turn/tool observability."""

    session_id: str = "default"
    sink: ObservationSink | None = None
    events: list[dict[str, object]] = field(default_factory=list)
    _seq: int = 0
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def emit(self, event: str, **fields: object) -> None:
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
            self.events.append(record)
            if self.sink is not None:
                self.sink.write(record)

    def snapshot(self) -> list[dict[str, object]]:
        with self._lock:
            return [dict(event) for event in self.events]

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
