"""Single-owner state for the physical Body.

The object is deliberately small.  It records who may own the Body and the
native stop boundary; it does not decide priorities for future life or threat
owners.  A generation changes whenever a claim is revoked or a new claim is
admitted, so late progress/results can be recognized without guessing from a
worker's idle state.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

from minebot.brain.progress import ProgressAuthority

BodyHolder = Literal["none", "agent_call", "agent_plan", "unknown", "life", "threat"]
BodyStopState = Literal["running", "stopping", "settled", "unknown"]


@dataclass(frozen=True)
class BodyControlSnapshot:
    holder: BodyHolder
    claim_id: str | None
    generation: int
    stop_state: BodyStopState
    since: float

    def to_payload(self) -> dict[str, object]:
        return {
            "holder": self.holder,
            "claim_id": self.claim_id,
            "generation": self.generation,
            "stop_state": self.stop_state,
            "since_monotonic": self.since,
        }


class BodyControl:
    """Thread-safe single Body claim and native settlement record."""

    def __init__(self, authority: ProgressAuthority | None = None) -> None:
        import threading

        self._lock = threading.RLock()
        self.authority = authority or ProgressAuthority()
        self._holder: BodyHolder = "none"
        self._claim_id: str | None = None
        self._generation = self.authority.current_generation()
        self.last_stop: dict[str, object] = {}
        self._stop_state: BodyStopState = "settled"
        self._since = time.monotonic()

    def snapshot(self) -> BodyControlSnapshot:
        with self._lock:
            return BodyControlSnapshot(
                holder=self._holder,
                claim_id=self._claim_id,
                generation=self._generation,
                stop_state=self._stop_state,
                since=self._since,
            )

    @property
    def unknown(self) -> bool:
        return self.snapshot().stop_state == "unknown"

    def claim(self, claim_id: str, *, holder: BodyHolder = "agent_call") -> BodyControlSnapshot:
        if not claim_id:
            raise ValueError("Body claim_id must be non-empty")
        with self._lock:
            if self._stop_state in {"unknown", "stopping"}:
                raise RuntimeError("body_control_unknown")
            if self._holder not in {"none", "unknown"} and self._claim_id != claim_id:
                raise RuntimeError(f"body_busy:{self._claim_id or self._holder}")
            if self._claim_id != claim_id or self._holder == "none":
                self._generation = self.authority.next_generation()
            self._holder = holder
            self._claim_id = claim_id
            self._stop_state = "running"
            self._since = time.monotonic()
            return self.snapshot()

    def mark_stopping(self, claim_id: str | None = None) -> BodyControlSnapshot:
        with self._lock:
            if claim_id is not None and self._claim_id not in {None, claim_id}:
                return self.snapshot()
            self._stop_state = "stopping"
            # Releasing the claim invalidates callbacks from the old owner.
            self._generation = self.authority.next_generation()
            self._since = time.monotonic()
            return self.snapshot()

    def settle(self, *, claim_id: str | None = None) -> BodyControlSnapshot:
        with self._lock:
            if claim_id is not None and self._claim_id not in {None, claim_id}:
                return self.snapshot()
            self._holder = "none"
            self._claim_id = None
            self._stop_state = "settled"
            self._generation = self.authority.current_generation()
            self._since = time.monotonic()
            return self.snapshot()

    def mark_unknown(self, *, claim_id: str | None = None) -> BodyControlSnapshot:
        with self._lock:
            if claim_id is not None and self._claim_id not in {None, claim_id}:
                return self.snapshot()
            self._holder = "unknown"
            self._stop_state = "unknown"
            self._since = time.monotonic()
            return self.snapshot()

    def confirm_native_stopped(self, native_stopped: bool) -> BodyControlSnapshot:
        if native_stopped:
            return self.settle()
        return self.mark_unknown()

    def owns(self, claim_id: str, generation: int | None = None) -> bool:
        with self._lock:
            return (
                self._claim_id == claim_id
                and self._holder not in {"none", "unknown"}
                and self._stop_state == "running"
                and self.authority.generation_current(self._generation)
                and (generation is None or self._generation == generation)
            )


__all__ = ["BodyControl", "BodyControlSnapshot", "BodyHolder", "BodyStopState"]
