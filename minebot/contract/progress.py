"""Progress authority protocol visible at the Body boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from .messages import BodyState

STAGNATION_LIMIT = 3
STALL_LIMIT = 8
FAILURE_STORM_LIMIT = 5


@dataclass(frozen=True)
class ProgressFacts:
    goal: str
    last_action: tuple[Any, ...] | None
    stagnant_steps: int
    stalled_steps: int
    failure_steps: int
    last_fingerprint: str
    current_fingerprint: str
    recent_events: list[str] = field(default_factory=list)


class ProgressAbort(Exception):
    """Raised when a progress authority decides a loop must yield."""

    def __init__(self, message: str = "", *, facts: ProgressFacts | None = None) -> None:
        super().__init__(message)
        self.facts = facts


class ProgressController(Protocol):
    def current_generation(self) -> int: ...
    def next_generation(self) -> int: ...
    def generation_current(self, generation: int) -> bool: ...
    def fingerprint(self, state: BodyState) -> str: ...
    def observe_step(self, action_key: tuple[Any, ...], fingerprint: str) -> None: ...
    def note_step(
        self,
        action_key: tuple[Any, ...],
        success: bool,
        fingerprint: str,
        *,
        neutral: bool = False,
    ) -> None: ...
    def facts(self, goal_text: str) -> ProgressFacts: ...
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
        self.recent_events: list[str] = []

    def next_generation(self) -> int:
        self._generation += 1
        return self._generation

    def current_generation(self) -> int:
        return self._generation

    def generation_current(self, generation: int) -> bool:
        return generation == self._generation

    def fingerprint(self, state: BodyState) -> str:
        pos = ",".join(f"{value:.1f}" for value in state.pos)
        return "|".join([pos, f"{state.health:.1f}", str(int(state.food)), str(state.time // 1000), state.inventory_hash])

    def observe_step(self, action_key: tuple[Any, ...], fingerprint: str) -> None:
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
        self.last_action = action_key
        self.last_fingerprint = fingerprint

    def note_step(
        self,
        action_key: tuple[Any, ...],
        success: bool,
        fingerprint: str,
        *,
        neutral: bool = False,
    ) -> None:
        if neutral:
            self.current_fingerprint = fingerprint
            return
        if success:
            self.current_fingerprint = fingerprint
            self.stagnant_steps = 0
            self.stalled_steps = 0
            self.failure_steps = 0
            self.last_action = action_key
            self.last_fingerprint = fingerprint
            return
        self.observe_step(action_key, fingerprint)

        self.failure_steps += 1

        self.last_action = action_key
        self.last_fingerprint = fingerprint

    def facts(self, goal_text: str) -> ProgressFacts:
        return ProgressFacts(
            goal=goal_text,
            last_action=self.last_action,
            stagnant_steps=self.stagnant_steps,
            stalled_steps=self.stalled_steps,
            failure_steps=self.failure_steps,
            last_fingerprint=self.last_fingerprint,
            current_fingerprint=self.current_fingerprint,
            recent_events=list(self.recent_events),
        )

    def require_can_continue(self, goal_text: str) -> None:
        if (
            self.stagnant_steps >= STAGNATION_LIMIT
            or self.stalled_steps >= STALL_LIMIT
            or self.failure_steps >= FAILURE_STORM_LIMIT
        ):
            facts = self.facts(goal_text)
            raise ProgressAbort(
                "progress authority yielded: "
                f"goal={facts.goal!r} stagnant={facts.stagnant_steps} "
                f"stalled={facts.stalled_steps} failures={facts.failure_steps}",
                facts=facts,
            )
