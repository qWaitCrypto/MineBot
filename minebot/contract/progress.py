"""Progress authority protocol visible at the Body boundary."""

from __future__ import annotations

from typing import Any, Protocol

from .messages import BodyState

STAGNATION_LIMIT = 3
STALL_LIMIT = 8
FAILURE_STORM_LIMIT = 5


class ProgressAbort(Exception):
    """Raised when a progress authority decides a loop must yield."""


class ProgressController(Protocol):
    def next_generation(self) -> int: ...
    def generation_current(self, generation: int) -> bool: ...
    def fingerprint(self, state: BodyState) -> str: ...
    def note_step(
        self,
        action_key: tuple[Any, ...],
        success: bool,
        fingerprint: str,
        *,
        neutral: bool = False,
    ) -> None: ...
    def require_can_continue(self, goal_text: str) -> None: ...


class LocalProgressController:
    """Minimal bounded progress guard for Body transaction unit tests/defaults."""

    stagnant_steps: int = 0
    stalled_steps: int = 0
    failure_steps: int = 0

    def __init__(self) -> None:
        self._generation = 0
        self.last_action: tuple[Any, ...] | None = None
        self.last_fingerprint = ""
        self.current_fingerprint = ""

    def next_generation(self) -> int:
        self._generation += 1
        return self._generation

    def generation_current(self, generation: int) -> bool:
        return generation == self._generation

    def fingerprint(self, state: BodyState) -> str:
        pos = ",".join(f"{value:.1f}" for value in state.pos)
        return "|".join([pos, f"{state.health:.1f}", str(int(state.food)), str(state.time // 1000), state.inventory_hash])

    def note_step(
        self,
        action_key: tuple[Any, ...],
        success: bool,
        fingerprint: str,
        *,
        neutral: bool = False,
    ) -> None:
        self.current_fingerprint = fingerprint
        progressed = bool(self.last_fingerprint) and fingerprint != self.last_fingerprint
        if not self.last_fingerprint or progressed:
            self.stagnant_steps = 0
            self.stalled_steps = 0
        elif action_key == self.last_action:
            self.stagnant_steps += 1
            self.stalled_steps += 1
        else:
            self.stalled_steps += 1

        if neutral:
            pass
        elif success:
            self.failure_steps = 0
        else:
            self.failure_steps += 1

        self.last_action = action_key
        self.last_fingerprint = fingerprint

    def require_can_continue(self, goal_text: str) -> None:
        if (
            self.stagnant_steps >= STAGNATION_LIMIT
            or self.stalled_steps >= STALL_LIMIT
            or self.failure_steps >= FAILURE_STORM_LIMIT
        ):
            raise ProgressAbort(
                "progress authority yielded: "
                f"goal={goal_text!r} stagnant={self.stagnant_steps} "
                f"stalled={self.stalled_steps} failures={self.failure_steps}"
            )
