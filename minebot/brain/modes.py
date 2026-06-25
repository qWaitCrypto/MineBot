"""Phase-1 relationship/situational runtime.

Framework-agnostic stance reducer for Agent Phase 1. It owns the relationship
and situational axes and emits a per-turn RuntimeProfile for context, policy,
and runner binding. Tools report facts; they do not mutate this state directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from minebot.brain.lifecycle import LifecycleState
from minebot.contract import BodyState, Event, ProgressFacts

RelationshipState = Literal["autonomous.user_request"]
SituationalState = Literal["normal", "survival", "mobility", "death"]
SignalKind = Literal[
    "goal_started",
    "progress_abort",
    "body_reflex_started",
    "body_reflex_completed",
    "survival_metric_red",
    "mobility_blocked",
    "death_detected",
    "recovery_completed",
    "tool_results",
    "user_interrupt",
]


@dataclass
class SuspendSlot:
    goal_text: str
    composition_id: str | None
    last_progress: dict[str, object]
    reason: str


@dataclass(frozen=True)
class RuntimeProfile:
    relationship: RelationshipState
    situational: SituationalState
    lifecycle: str
    goal_lock: Literal["mutable"]
    context_frame: str
    tool_focus: tuple[str, ...]
    model_route: Literal["primary", "fast"]
    effort: Literal["minimal", "standard", "deep"]
    policy_tags: tuple[str, ...]


@dataclass(frozen=True)
class ModeReduction:
    profile: RuntimeProfile
    requested_lifecycle: LifecycleState | None = None
    reason: str | None = None


@dataclass(frozen=True)
class AgentSignal:
    kind: SignalKind
    facts: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def goal_started(cls, goal_text: str) -> "AgentSignal":
        return cls("goal_started", {"goal": goal_text})

    @classmethod
    def progress_abort(cls, facts: ProgressFacts) -> "AgentSignal":
        return cls("progress_abort", {"progress": facts})

    @classmethod
    def body_reflex_started(cls, reason: str, **facts: Any) -> "AgentSignal":
        return cls("body_reflex_started", {"reason": reason, **facts})

    @classmethod
    def body_reflex_completed(cls, reason: str = "recovered", **facts: Any) -> "AgentSignal":
        return cls("body_reflex_completed", {"reason": reason, **facts})

    @classmethod
    def survival_metric_red(cls, reason: str, **facts: Any) -> "AgentSignal":
        return cls("survival_metric_red", {"reason": reason, **facts})

    @classmethod
    def mobility_blocked(cls, reason: str, **facts: Any) -> "AgentSignal":
        return cls("mobility_blocked", {"reason": reason, **facts})

    @classmethod
    def death_detected(cls, reason: str = "death_detected", **facts: Any) -> "AgentSignal":
        return cls("death_detected", {"reason": reason, **facts})

    @classmethod
    def recovery_completed(cls, reason: str = "recovered", **facts: Any) -> "AgentSignal":
        return cls("recovery_completed", {"reason": reason, **facts})

    @classmethod
    def tool_results(cls, results: list[dict[str, Any]]) -> "AgentSignal":
        return cls("tool_results", {"results": results})

    @classmethod
    def user_interrupt(cls, reason: str = "user_interrupt", **facts: Any) -> "AgentSignal":
        return cls("user_interrupt", {"reason": reason, **facts})


class ModeRuntime:
    """Reduce turn-boundary signals into the current runtime profile."""

    def __init__(self, *, relationship: RelationshipState = "autonomous.user_request") -> None:
        self.relationship: RelationshipState = relationship
        self.situational: SituationalState = "normal"
        self.suspend_slot: SuspendSlot | None = None
        self.last_reason: str | None = None

    def reduce(
        self,
        signals: list[AgentSignal] | tuple[AgentSignal, ...],
        lifecycle_state: LifecycleState,
        *,
        goal_text: str | None = None,
    ) -> ModeReduction:
        requested_candidates: list[tuple[LifecycleState, str, dict[str, Any]]] = []
        situational_candidates: list[tuple[SituationalState, str]] = []

        for signal in signals:
            if signal.kind == "goal_started":
                situational_candidates.append(("normal", str(signal.facts.get("goal") or "goal_started")))
                continue

            if signal.kind == "progress_abort":
                progress = signal.facts.get("progress")
                situational_candidates.append((_situational_from_progress(progress), "progress_abort"))
                requested_candidates.append((LifecycleState.YIELDED, "progress_abort", signal.facts))
                continue

            if signal.kind in {"body_reflex_started", "survival_metric_red"}:
                situational_candidates.append(("survival", str(signal.facts.get("reason") or signal.kind)))
                continue

            if signal.kind == "body_reflex_completed":
                situational_candidates.append(("normal", str(signal.facts.get("reason") or "body_reflex_completed")))
                continue

            if signal.kind == "mobility_blocked":
                situational_candidates.append(("mobility", str(signal.facts.get("reason") or "mobility_blocked")))
                continue

            if signal.kind == "death_detected":
                reason = str(signal.facts.get("reason") or "death_detected")
                situational_candidates.append(("death", reason))
                requested_candidates.append((LifecycleState.RECOVERING, reason, signal.facts))
                continue

            if signal.kind == "recovery_completed":
                reason = str(signal.facts.get("reason") or "recovery_completed")
                situational_candidates.append(("normal", reason))
                if lifecycle_state is LifecycleState.RECOVERING:
                    requested_candidates.append((LifecycleState.RESUMING, reason, signal.facts))
                continue

            if signal.kind == "user_interrupt":
                reason = str(signal.facts.get("reason") or "user_interrupt")
                requested_candidates.append((LifecycleState.YIELDED, reason, signal.facts))
                continue

            if signal.kind == "tool_results":
                result_reason = _first_blocking_tool_reason(signal.facts.get("results"))
                if result_reason:
                    situational_candidates.append(("mobility", result_reason))

        situational, situational_reason = _highest_situational(situational_candidates, self.situational)
        requested, requested_reason, suspend_facts = _highest_lifecycle_request(requested_candidates)
        self.situational = situational
        reason = requested_reason or situational_reason

        if reason is not None:
            self.last_reason = reason
        if requested in {LifecycleState.YIELDED, LifecycleState.RECOVERING} and suspend_facts is not None:
            self._write_suspend(goal_text, reason or requested.value, suspend_facts)
        return ModeReduction(
            profile=self.profile_for(lifecycle_state),
            requested_lifecycle=requested,
            reason=reason,
        )

    def profile_for(self, lifecycle_state: LifecycleState) -> RuntimeProfile:
        focus, route, effort, tags = _profile_axes(self.situational)
        return RuntimeProfile(
            relationship=self.relationship,
            situational=self.situational,
            lifecycle=lifecycle_state.value,
            goal_lock="mutable",
            context_frame=_context_frame(self.situational),
            tool_focus=focus,
            model_route=route,
            effort=effort,
            policy_tags=tags,
        )

    def consume_suspend_slot(self) -> SuspendSlot | None:
        slot = self.suspend_slot
        self.suspend_slot = None
        return slot

    def _write_suspend(
        self,
        goal_text: str | None,
        reason: str,
        facts: dict[str, Any],
    ) -> None:
        if goal_text is None:
            return
        self.suspend_slot = SuspendSlot(
            goal_text=goal_text,
            composition_id=_maybe_str(facts.get("composition_id")),
            last_progress=_jsonish(facts),
            reason=reason,
        )


def signalize_body_state(state: BodyState) -> list[AgentSignal]:
    signals: list[AgentSignal] = []
    if state.missing or state.health <= 0:
        signals.append(AgentSignal.death_detected(health=state.health, missing=state.missing))
    elif state.health <= 6:
        signals.append(AgentSignal.survival_metric_red("low_health", health=state.health))
    elif state.food <= 6:
        signals.append(AgentSignal.survival_metric_red("low_food", food=state.food))
    elif state.oxygen is not None and state.oxygen <= 80:
        signals.append(AgentSignal.survival_metric_red("low_oxygen", oxygen=state.oxygen))
    return signals


def signalize_events(events: list[Event] | tuple[Event, ...]) -> list[AgentSignal]:
    signals: list[AgentSignal] = []
    for event in events:
        name = event.name
        data = dict(event.data)
        if name in {"reflexTriggered", "reflexStarted", "ownerPreempted"}:
            signals.append(AgentSignal.body_reflex_started(str(data.get("kind") or name), event=name))
        elif name in {"reflexCompleted", "recoveryCompleted"}:
            signals.append(AgentSignal.body_reflex_completed(str(data.get("kind") or name), event=name))
        elif name in {"deathDetected", "botDied", "respawned"}:
            signals.append(AgentSignal.death_detected(str(data.get("reason") or name), event=name))
        elif name in {"stuck", "navigationBlocked", "lostPosition"}:
            signals.append(AgentSignal.mobility_blocked(str(data.get("reason") or name), event=name))
    return signals


def _situational_from_progress(progress: Any) -> SituationalState:
    if isinstance(progress, ProgressFacts):
        action = progress.last_action[0] if progress.last_action else ""
        if "navigate" in str(action) or "move" in str(action):
            return "mobility"
    return "normal"


_SITUATIONAL_SEVERITY: dict[SituationalState, int] = {
    "normal": 0,
    "mobility": 1,
    "survival": 2,
    "death": 3,
}

_LIFECYCLE_REQUEST_PRIORITY: dict[LifecycleState, int] = {
    LifecycleState.RESUMING: 0,
    LifecycleState.INTERRUPTED: 1,
    LifecycleState.YIELDED: 2,
    LifecycleState.RECOVERING: 3,
}


def _highest_situational(
    candidates: list[tuple[SituationalState, str]],
    current: SituationalState,
) -> tuple[SituationalState, str | None]:
    if not candidates:
        return current, None
    return max(candidates, key=lambda item: _SITUATIONAL_SEVERITY[item[0]])


def _highest_lifecycle_request(
    candidates: list[tuple[LifecycleState, str, dict[str, Any]]],
) -> tuple[LifecycleState | None, str | None, dict[str, Any] | None]:
    if not candidates:
        return None, None, None
    state, reason, facts = max(candidates, key=lambda item: _LIFECYCLE_REQUEST_PRIORITY.get(item[0], -1))
    return state, reason, facts


def _profile_axes(
    situational: SituationalState,
) -> tuple[tuple[str, ...], Literal["primary", "fast"], Literal["minimal", "standard", "deep"], tuple[str, ...]]:
    if situational == "survival":
        return ("survival", "recovery", "navigation"), "fast", "standard", ("survival",)
    if situational == "mobility":
        return ("navigation", "perception", "recovery"), "primary", "standard", ("mobility",)
    if situational == "death":
        return ("recovery", "inventory", "navigation"), "primary", "standard", ("death",)
    return ("resource", "navigation", "perception"), "primary", "standard", ("normal",)


def _context_frame(situational: SituationalState) -> str:
    if situational == "survival":
        return "Survival issue resolved or active; reason from current body facts before continuing."
    if situational == "mobility":
        return "Mobility/reachability issue; use fresh position, route, and candidate facts."
    if situational == "death":
        return "Death/recovery context; recount inventory and resume at the resource goal level."
    return "Normal autonomous resource collection."


def _first_blocking_tool_reason(results: Any) -> str | None:
    if not isinstance(results, list):
        return None
    for result in results:
        if not isinstance(result, dict):
            continue
        reason = str(result.get("reason") or "")
        if reason.startswith(("navigation_", "mobility_", "stuck", "lost_position")):
            return reason
    return None


def _maybe_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _jsonish(value: Any) -> dict[str, object]:
    out: dict[str, object] = {}
    for key, item in value.items():
        if isinstance(item, ProgressFacts):
            out[key] = {
                "goal": item.goal,
                "last_action": list(item.last_action) if item.last_action else None,
                "stagnant_steps": item.stagnant_steps,
                "stalled_steps": item.stalled_steps,
                "failure_steps": item.failure_steps,
                "last_fingerprint": item.last_fingerprint,
                "current_fingerprint": item.current_fingerprint,
                "recent_events": list(item.recent_events),
            }
        elif isinstance(item, (str, int, float, bool)) or item is None:
            out[key] = item
        else:
            out[key] = str(item)
    return out


__all__ = [
    "AgentSignal",
    "ModeReduction",
    "ModeRuntime",
    "RelationshipState",
    "RuntimeProfile",
    "SignalKind",
    "SituationalState",
    "SuspendSlot",
    "signalize_body_state",
    "signalize_events",
]
