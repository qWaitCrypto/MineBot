"""Durable observation sinks for Agent Phase 1."""

from __future__ import annotations

import queue
import re
import sys
import threading
from pathlib import Path
from typing import Any, Protocol

from minebot.app.trace_facts import TraceFactsWriter, encode_line

_RAW_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bAIza[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?P<label>\b(?:api[_ -]?key|password|secret|token|auth(?:orization)?)\b\s*"
    r"(?:[:=]\s*|is\s+))"
    r"(?P<quote>['\"]?)(?P<value>[^\s,;\)\]\}>\"']{8,})(?P=quote)",
    re.IGNORECASE,
)
_BEARER_TOKEN_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~-]{16,}\b", re.IGNORECASE)
_TOKEN_COUNTS = frozenset({"input_tokens", "output_tokens", "total_tokens", "cached_tokens",
                           "cache_write_tokens", "reasoning_tokens"})


class ObservationSink(Protocol):
    def write(self, record: dict[str, object]) -> None: ...

    def close(self) -> None: ...


_CLOSE = object()


class JsonlObservationSink:
    """Append one sanitized observation record per line.

    With ``background=True`` sanitizing, encoding and the disk write happen on
    a writer thread, in order. The session's event loop calls ``write``; on a
    busy disk each flush took 0.5-2 s and froze the loop (and every Decision
    frame waiting on it) for that long.
    """

    def __init__(self, path: str | Path, *, background: bool = False) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", encoding="utf-8")
        self._facts = TraceFactsWriter()
        self._queue: queue.SimpleQueue[object] | None = None
        self._writer: threading.Thread | None = None
        if background:
            self._queue = queue.SimpleQueue()
            self._writer = threading.Thread(target=self._drain, name="observation-writer", daemon=True)
            self._writer.start()

    def write(self, record: dict[str, object]) -> None:
        if self._queue is not None:
            self._queue.put(record)
            return
        self._write_line(record)

    def _drain(self) -> None:
        assert self._queue is not None
        while (record := self._queue.get()) is not _CLOSE:
            try:
                self._write_line(record)  # type: ignore[arg-type]
            except Exception as exc:  # keep writing later records
                print(f"observation write failed: {type(exc).__name__}: {exc}", file=sys.stderr)

    def _write_line(self, record: dict[str, object]) -> None:
        safe = sanitize_observation(record)
        try:
            self._fh.write("".join(encode_line(row) for row in self._facts.encode(safe)))
            self._fh.flush()
        except Exception:
            self._facts = TraceFactsWriter()
            raise

    def close(self) -> None:
        if self._writer is not None and self._queue is not None:
            self._queue.put(_CLOSE)
            self._writer.join()
            self._writer = None
        self._fh.close()


def sanitize_observation(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text in _TOKEN_COUNTS and type(item) is int and item >= 0:
                out[key_text] = item
            elif key_text in {"input_tokens_details", "output_tokens_details"} and isinstance(item, dict):
                out[key_text] = sanitize_observation(item)
            elif _secret_key(key_text):
                out[key_text] = "<redacted>"
            else:
                out[key_text] = sanitize_observation(item)
        return out
    if isinstance(value, list):
        return [sanitize_observation(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_observation(item) for item in value]
    if isinstance(value, str):
        return _sanitize_text(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _sanitize_text(str(value))


def _sanitize_text(value: str) -> str:
    safe = _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group('label')}<redacted>", value)
    safe = _BEARER_TOKEN_RE.sub("Bearer <redacted>", safe)
    for pattern in _RAW_SECRET_PATTERNS:
        safe = pattern.sub("<redacted>", safe)
    return safe


def _secret_key(key: str) -> bool:
    normalized = key.lower()
    if normalized.endswith("_env") or normalized.endswith("_env_name"):
        return False
    return any(marker in normalized for marker in ("api_key", "password", "secret", "token", "auth"))


__all__ = ["JsonlObservationSink", "ObservationSink", "sanitize_observation"]
