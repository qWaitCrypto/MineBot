"""Single progress authority for agent/body tool execution."""

from __future__ import annotations

import math
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
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


def progress_snapshot(state: BodyState) -> BodyState | None:
    if state.missing or not state.complete:
        return None
    return replace(state, inventory_counts=(
        None if state.inventory_counts is None else dict(state.inventory_counts)
    ))


def material_progress_evidence(
    before: BodyState | None,
    after: BodyState | None,
    *,
    successful_body_action: bool,
) -> tuple[str, ...]:
    """Credit observed effects, never clock ticks, damage, or failed movement."""
    if (
        before is None or after is None
        or before.missing or after.missing
        or not before.complete or not after.complete
        or before.bot != after.bot or before.dimension != after.dimension
    ):
        return ()
    evidence: list[str] = []
    if before.inventory_counts is not None and after.inventory_counts is not None:
        if any(count > before.inventory_counts.get(item, 0) for item, count in after.inventory_counts.items()):
            evidence.append("inventory_gain")
        elif successful_body_action and before.inventory_counts != after.inventory_counts:
            evidence.append("completed_inventory_change")
    elif successful_body_action and before.inventory_hash != after.inventory_hash:
        evidence.append("inventory_change")
    if successful_body_action:
        if tuple(round(value, 1) for value in before.pos) != tuple(round(value, 1) for value in after.pos):
            evidence.append("completed_movement")
        if after.health > before.health or after.food > before.food:
            evidence.append("survival_improvement")
        if before.oxygen is not None and after.oxygen is not None and after.oxygen > before.oxygen:
            evidence.append("oxygen_recovered")
        if (before.selected_item, before.offhand_item) != (after.selected_item, after.offhand_item):
            evidence.append("equipment_changed")
    return tuple(evidence)


def combat_progress_evidence(result: dict[str, object]) -> tuple[str, ...]:
    """Use target-locked terminal observations, not swings or a success label."""
    metrics = result.get("metrics")
    if not isinstance(metrics, dict):
        return ()
    if (
        metrics.get("event") != "engageDone"
        or not isinstance(metrics.get("action_id"), str)
        or not metrics["action_id"]
        or not isinstance(metrics.get("target_uuid"), str)
        or not metrics["target_uuid"]
        or metrics.get("target_id") != metrics["target_uuid"]
        or metrics.get("persistent_target") is not True
        or metrics.get("damage_observed") is not True
    ):
        return ()
    initial, final = metrics.get("target_initial_health"), metrics.get("target_health")
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in (initial, final)):
        return ()
    if not initial > final >= 0:
        return ()
    if result.get("success") is True and result.get("reason") == "killed" and final == 0:
        return ("observed_target_defeat",)
    if result.get("success") is False and result.get("reason") in {
        "timeout", "disengaged_low_health", "no_path",
    }:
        return ("observed_target_damage",)
    return ()


@dataclass
class ProgressAuthority:
    stagnant_steps: int = 0
    stalled_steps: int = 0
    failure_steps: int = 0
    last_action: tuple[Any, ...] | None = None
    last_fingerprint: str = ""
    current_fingerprint: str = ""
    recent_events: list[str] = field(default_factory=list)
    epistemic_steps: int = 0
    last_epistemic_keys: tuple[str, ...] = ()
    _generation: int = 0
    _generation_listeners: list[Callable[[str, int], None]] = field(
        default_factory=list,
        init=False,
        repr=False,
        compare=False,
    )
    _captures: threading.local = field(
        default_factory=threading.local,
        init=False,
        repr=False,
        compare=False,
    )

    def current_generation(self) -> int:
        return self._generation

    def restore_facts(self, facts: ProgressFacts) -> None:
        """Restore task progress without reviving process-local execution tokens."""
        if self._active_capture() is not None:
            raise RuntimeError("cannot restore progress during tool execution")
        self.stagnant_steps = facts.stagnant_steps
        self.stalled_steps = facts.stalled_steps
        self.failure_steps = facts.failure_steps
        self.epistemic_steps = facts.epistemic_steps
        self.last_action = facts.last_action
        self.last_fingerprint = facts.last_fingerprint
        self.current_fingerprint = facts.current_fingerprint
        self.last_epistemic_keys = facts.last_epistemic_keys
        self.recent_events = list(facts.recent_events)

    def next_generation(self) -> int:
        self._generation += 1
        return self._generation

    def generation_current(self, generation: int) -> bool:
        return generation == self._generation

    def add_generation_listener(self, listener: Callable[[str, int], None]) -> None:
        """Register a local owner that must observe generation invalidation."""
        if listener not in self._generation_listeners:
            self._generation_listeners.append(listener)

    def invalidate_generation(self, reason: str) -> None:
        self._generation += 1
        self.recent_events.append(f"generation_invalidated:{reason}")
        for listener in tuple(self._generation_listeners):
            try:
                listener(reason, self._generation)
            except Exception:
                # A telemetry/owner observer must never hide the invalidation.
                continue

    def fingerprint(self, state: BodyState) -> str:
        if state.food is None:
            raise ValueError("BodyState.food is required for progress fingerprint")
        pos = ",".join(f"{value:.1f}" for value in state.pos)
        health = f"{state.health:.1f}"
        return "|".join([pos, health, str(int(state.food)), state.inventory_hash])

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
            or self.epistemic_steps >= STALL_LIMIT
        )

    def require_can_continue(self, goal_text: str) -> None:
        if self._active_capture() is not None:
            return
        if self.should_yield():
            facts = self.facts(goal_text)
            raise ProgressAbort(
                "progress authority yielded: "
                f"goal={facts.goal!r} stagnant={facts.stagnant_steps} "
                f"stalled={facts.stalled_steps} failures={facts.failure_steps} "
                f"epistemic={facts.epistemic_steps}",
                facts=facts,
                stop_reason=facts.stop_reason(),
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

    def commit_steps(
        self,
        steps: list[ProgressStep] | tuple[ProgressStep, ...],
        goal_text: str,
        *,
        novel_epistemic_keys: list[str] | tuple[str, ...] = (),
        material_changed: bool = False,
    ) -> None:
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
        epistemic_keys = tuple(dict.fromkeys(str(key) for key in novel_epistemic_keys if str(key)))
        if material_changed:
            self.epistemic_steps = 0
            self.last_epistemic_keys = ()
            self.failure_steps = 0
        elif epistemic_keys:
            self.epistemic_steps += 1
            self.last_epistemic_keys = epistemic_keys
            self.stagnant_steps = 0
            self.stalled_steps = 0
            self.recent_events.append(
                f"epistemic_progress:{self.epistemic_steps}:{len(epistemic_keys)}"
            )
            if self.epistemic_steps < STALL_LIMIT:
                return
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
            epistemic_steps=self.epistemic_steps,
            last_epistemic_keys=self.last_epistemic_keys,
        )
