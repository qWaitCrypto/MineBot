"""Single progress authority for agent/body tool execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from minebot.contract import BodyState, FAILURE_STORM_LIMIT, ProgressAbort, STAGNATION_LIMIT, STALL_LIMIT


@dataclass(frozen=True)
class ProgressFacts:
    goal: str
    last_action: tuple[Any, ...] | None
    stagnant_steps: int
    stalled_steps: int
    failure_steps: int
    last_fingerprint: str
    current_fingerprint: str
    recent_events: list[str]


@dataclass
class ProgressAuthority:
    stagnant_steps: int = 0
    stalled_steps: int = 0
    failure_steps: int = 0
    last_action: tuple[Any, ...] | None = None
    last_fingerprint: str = ""
    current_fingerprint: str = ""
    recent_events: list[str] = field(default_factory=list)
    _generation: int = 0

    def next_generation(self) -> int:
        self._generation += 1
        return self._generation

    def generation_current(self, generation: int) -> bool:
        return generation == self._generation

    def invalidate_generation(self, reason: str) -> None:
        self._generation += 1
        self.recent_events.append(f"generation_invalidated:{reason}")

    def fingerprint(self, state: BodyState) -> str:
        if state.food is None:
            raise ValueError("BodyState.food is required for progress fingerprint")
        pos = ",".join(f"{value:.1f}" for value in state.pos)
        health = f"{state.health:.1f}"
        time_bucket = state.time // 1000
        return "|".join([pos, health, str(int(state.food)), str(time_bucket), state.inventory_hash])

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

    def should_yield(self) -> bool:
        return (
            self.stagnant_steps >= STAGNATION_LIMIT
            or self.stalled_steps >= STALL_LIMIT
            or self.failure_steps >= FAILURE_STORM_LIMIT
        )

    def require_can_continue(self, goal_text: str) -> None:
        if self.should_yield():
            facts = self.facts(goal_text)
            raise ProgressAbort(
                "progress authority yielded: "
                f"goal={facts.goal!r} stagnant={facts.stagnant_steps} "
                f"stalled={facts.stalled_steps} failures={facts.failure_steps}"
            )

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
