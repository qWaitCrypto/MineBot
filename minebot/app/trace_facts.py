"""Lossless, ordered facts storage for persisted runtime traces.

Only raw supervision inputs and death facts are encoded. RuntimeTrace and
Decision keep their original objects. Read from the start of a trace with
``read_trace`` (or keep one TraceFactsReader while following it).
"""

from __future__ import annotations

import base64
import hashlib
import json
import zlib
from collections.abc import Iterable, Iterator
from typing import Any


_FACT_FIELDS = {"call_supervision_request": ("frame_input", "compiler_input"),
                "reflex_death": ("facts",)}


def encode_line(record: dict[str, Any]) -> str:
    return json.dumps(record, ensure_ascii=True, default=str,
                      sort_keys=record.get("event") not in ("call_supervision_request", "frame_facts")) + "\n"


class TraceFactsWriter:
    """Intern ordered input bundles in bounded, independently readable blocks."""

    def __init__(self, *, min_bytes: int = 512, block_bytes: int = 4 << 20) -> None:
        self.min_bytes = min_bytes
        self.block_bytes = block_bytes
        self._seen: set[str] = set()
        self._bytes = 0
        self._base_id: str | None = None
        self._dictionary = b""

    def encode(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        facts = {field: record[field] for field in _FACT_FIELDS.get(record.get("event"), ())
                 if field in record}
        if not facts:
            return [record]
        # Keep ordered inputs together: zlib also removes copies of facts
        # within one frame, even when a small change prevents hash reuse.
        raw = json.dumps(facts, ensure_ascii=True, separators=(",", ":"), default=str).encode("utf-8")
        if len(raw) < self.min_bytes:
            return [record]
        if self._bytes >= self.block_bytes:
            self._seen.clear()
            self._bytes = 0
            self._base_id = None
            self._dictionary = b""
        facts_id = hashlib.sha256(raw).hexdigest()
        definitions = []
        if facts_id not in self._seen:
            packed = base64.b64encode(zlib.compress(raw)).decode("ascii")
            definition = {"event": "frame_facts", "facts_id": facts_id}
            if self._base_id is not None:
                compressor = zlib.compressobj(zdict=self._dictionary)
                delta = base64.b64encode(compressor.compress(raw) + compressor.flush()).decode("ascii")
                if len(delta) + 80 < len(packed):
                    packed = delta
                    definition["base_id"] = self._base_id
            if len(packed) < len(raw):
                definition["facts_zlib"] = packed
            else:
                definition["facts"] = facts
            if not self._seen:
                definition["reset"] = True
            self._seen.add(facts_id)
            # Bound decoded reader storage, not just compressed disk bytes.
            self._bytes += len(raw)
            self._base_id = facts_id
            self._dictionary = raw[-32768:]
            definitions.append(definition)
        compact = dict(record)
        for field in facts:
            compact[field] = None
        compact["facts_id"] = facts_id
        return [*definitions, compact]


class TraceFactsReader:
    """Restore source fields, retaining one bounded block of ordered JSON bytes."""

    def __init__(self) -> None:
        self._facts: dict[str, bytes] = {}

    def decode(self, record: dict[str, Any]) -> dict[str, Any] | None:
        if record.get("event") == "frame_facts":
            if record.get("reset"):
                self._facts.clear()
            if "facts_zlib" in record:
                packed = base64.b64decode(record["facts_zlib"])
                if "base_id" in record:
                    inflater = zlib.decompressobj(zdict=self._get(record["base_id"])[-32768:])
                    raw = inflater.decompress(packed) + inflater.flush()
                else:
                    raw = zlib.decompress(packed)
            else:
                raw = json.dumps(record["facts"], ensure_ascii=True, separators=(",", ":")).encode("utf-8")
            self._facts[record["facts_id"]] = raw
            return None
        if record.get("event") not in _FACT_FIELDS or "facts_id" not in record:
            return record
        restored = dict(record)
        facts_id = restored.pop("facts_id")
        restored.update(json.loads(self._get(facts_id)))
        return restored

    def _get(self, facts_id: str) -> bytes:
        try:
            return self._facts[facts_id]
        except KeyError:
            raise ValueError(f"missing frame_facts {facts_id}; read the trace from its start") from None


def read_trace(lines: Iterable[str | bytes]) -> Iterator[dict[str, Any]]:
    """Yield old-shape events from old or encoded JSONL; ignore malformed lines."""
    reader = TraceFactsReader()
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            restored = reader.decode(record)
            if restored is not None:
                yield restored


__all__ = ["TraceFactsReader", "TraceFactsWriter", "encode_line", "read_trace"]
