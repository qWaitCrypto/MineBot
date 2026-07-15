"""Single progress authority for agent/body tool execution."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

from minebot.contract import (
    BodyState,
    FAILURE_STORM_LIMIT,
    ProgressAbort,
    ProgressFacts,
    STAGNATION_LIMIT,
    STALL_LIMIT,
)


@dataclass(frozen=True)
class ProgressStep:
    kind: Literal["observe", "note"]
    action_key: tuple[Any, ...]
    fingerprint: str
    success: bool | None = None
    neutral: bool = False


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
    _captures: threading.local = field(
        default_factory=threading.local,
        init=False,
        repr=False,
        compare=False,
    )

    def current_generation(self) -> int:
        return self._generation

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

    def observe_step(self, action_key: tuple[Any, ...], fingerprint: str) -> None:
        capture = self._active_capture()
        if capture is not None:
            capture.append(ProgressStep("observe", action_key, fingerprint))
            return
        self._apply_observation(action_key, fingerprint)

    def _apply_observation(self, action_key: tuple[Any, ...], fingerprint: str) -> None:
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
        capture = self._active_capture()
        if capture is not None:
            capture.append(
                ProgressStep(
                    "note",
                    action_key,
                    fingerprint,
                    success=success,
                    neutral=neutral,
                )
            )
            return
        self._apply_note(action_key, success, fingerprint, neutral=neutral)

    def _apply_note(
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
        self._apply_observation(action_key, fingerprint)

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
        if self._active_capture() is not None:
            return
        if self.should_yield():
            facts = self.facts(goal_text)
            raise ProgressAbort(
                "progress authority yielded: "
                f"goal={facts.goal!r} stagnant={facts.stagnant_steps} "
                f"stalled={facts.stalled_steps} failures={facts.failure_steps}",
                facts=facts,
            )

    @contextmanager
    def capture_steps(self) -> Iterator[list[ProgressStep]]:
        stack = self._capture_stack()
        captured: list[ProgressStep] = []
        stack.append(captured)
        try:
            yield captured
        finally:
            popped = stack.pop()
            assert popped is captured
            if stack:
                stack[-1].extend(captured)

    def commit_steps(self, steps: list[ProgressStep] | tuple[ProgressStep, ...], goal_text: str) -> None:
        if self._active_capture() is not None:
            raise RuntimeError("cannot commit progress steps inside an active capture")
        for step in steps:
            if step.kind == "observe":
                self._apply_observation(step.action_key, step.fingerprint)
                continue
            assert step.success is not None
            self._apply_note(
                step.action_key,
                step.success,
                step.fingerprint,
                neutral=step.neutral,
            )
        self.require_can_continue(goal_text)

    def _capture_stack(self) -> list[list[ProgressStep]]:
        stack = getattr(self._captures, "stack", None)
        if stack is None:
            stack = []
            self._captures.stack = stack
        return stack

    def _active_capture(self) -> list[ProgressStep] | None:
        stack = self._capture_stack()
        return stack[-1] if stack else None

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
