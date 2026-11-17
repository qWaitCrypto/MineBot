"""Trace sources: follow a growing trace.jsonl, or load a finished one."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from minebot.app.trace_facts import TraceFactsReader, read_trace
TRACE_NAME = "trace.jsonl"


def resolve_trace(target: str | os.PathLike[str]) -> Path:
    path = Path(target).expanduser()
    if path.is_dir():
        direct = path / TRACE_NAME
        if direct.exists():
            return direct
        found = latest_trace(path)
        if found is None:
            raise FileNotFoundError(f"no {TRACE_NAME} under {path}")
        return found
    if not path.exists():
        raise FileNotFoundError(str(path))
    return path


SEARCH_DEPTH = 4


def latest_trace(root: str | os.PathLike[str], depth: int = SEARCH_DEPTH) -> Path | None:
    """The most recently modified trace.jsonl at most ``depth`` directories below ``root``."""
    best: tuple[float, Path] | None = None
    base = Path(root)
    candidates = (path for level in range(depth + 1) for path in base.glob("/".join(["*"] * level + [TRACE_NAME])))
    for candidate in candidates:
        try:
            mtime = candidate.stat().st_mtime
        except OSError:
            continue
        if best is None or mtime > best[0]:
            best = (mtime, candidate)
    return best[1] if best else None


def parse_line(line: bytes) -> dict[str, Any] | None:
    try:
        record = json.loads(line)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def load_records(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    with open(path, "rb") as handle:
        return list(read_trace(handle))


SESSION_START = "provider_manifest"
SESSION_END = "session_terminal"


def trace_finished(path: str | os.PathLike[str]) -> bool:
    """Whether the last session in the trace has ended. A restarted session appends to the same
    trace, so an earlier session's terminal record does not make the run finished. Streams the
    file and parses only the lines that can mark a session boundary."""
    finished = False
    with open(path, "rb") as handle:
        for line in handle:
            if SESSION_START.encode() not in line and SESSION_END.encode() not in line:
                continue
            record = parse_line(line)
            event = record.get("event") if record is not None else None
            if event == SESSION_START:
                finished = False
            elif event == SESSION_END:
                finished = True
    return finished


class TraceFollower:
    """Incremental reader that only yields complete lines and survives truncation."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.offset = 0
        self.pending = b""
        self.identity: tuple[int, int] | None = None
        self.reset_count = 0
        self._facts = TraceFactsReader()

    def read(self, limit_bytes: int = 4 << 20) -> tuple[list[dict[str, Any]], bool]:
        """New records since the last call, and whether the file was replaced or truncated."""
        try:
            stat = self.path.stat()
        except OSError:
            return [], False
        identity = (stat.st_dev, stat.st_ino)
        reset = False
        if self.identity is not None and (identity != self.identity or stat.st_size < self.offset):
            self.offset, self.pending, reset = 0, b"", True
            self.reset_count += 1
            self._facts = TraceFactsReader()
        self.identity = identity
        if stat.st_size == self.offset:
            return [], reset
        with open(self.path, "rb") as handle:
            handle.seek(self.offset)
            chunk = handle.read(limit_bytes)
        self.offset += len(chunk)
        data = self.pending + chunk
        lines = data.split(b"\n")
        self.pending = lines.pop()
        records = []
        for line in lines:
            record = parse_line(line)
            if record is not None:
                restored = self._facts.decode(record)
                if restored is not None:
                    records.append(restored)
        return records, reset


__all__ = ["TraceFollower", "latest_trace", "load_records", "parse_line", "resolve_trace", "trace_finished"]
