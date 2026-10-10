"""Trace sources: follow a growing trace.jsonl, or load a finished one."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

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
    records = []
    with open(path, "rb") as handle:
        for line in handle:
            record = parse_line(line)
            if record is not None:
                records.append(record)
    return records


class TraceFollower:
    """Incremental reader that only yields complete lines and survives truncation."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.offset = 0
        self.pending = b""
        self.identity: tuple[int, int] | None = None
        self.reset_count = 0

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
        records = [record for line in lines if line.strip() and (record := parse_line(line)) is not None]
        return records, reset


__all__ = ["TraceFollower", "latest_trace", "load_records", "parse_line", "resolve_trace"]
