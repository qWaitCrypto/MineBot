from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class SequenceGapError(RuntimeError):
    pass


class SequenceTracker:
    """Reject missing, duplicate, malformed, and out-of-order bridge messages."""

    def __init__(self, initial_seq: int = 0) -> None:
        if isinstance(initial_seq, bool) or not isinstance(initial_seq, int) or initial_seq < 0:
            raise ValueError("initial_seq must be a non-negative integer")
        self.last_seq = initial_seq

    def check(self, message: Mapping[str, Any]) -> None:
        raw_seq = message.get("seq")
        expected = self.last_seq + 1
        if isinstance(raw_seq, bool):
            raise SequenceGapError(f"observer-control sequence gap: got {raw_seq!r}, expected {expected}")
        try:
            seq = int(raw_seq)
        except (TypeError, ValueError) as exc:
            raise SequenceGapError(
                f"observer-control sequence gap: got {raw_seq!r}, expected {expected}"
            ) from exc
        if seq != expected:
            raise SequenceGapError(f"observer-control sequence gap: got {seq}, expected {expected}")
        self.last_seq = seq
