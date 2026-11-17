"""Per-turn reasoning effort for the Agent, chosen from facts the turn already has.

User decisions (agent-effort-routing-20261108.md, 2026-11-09): only effort changes,
never the model; the Agent may ask for deep thought; a deep turn is not interrupted
when danger comes (Decision and the reflex layer handle danger); xhigh at most.

Only facts the Agent itself sees decide it: the last hurt and its age, the nearest
hostile mob and its distance, whether the Body is working, and what woke the turn.
No verdict about the situation is derived.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# A hit this recent, or a hostile this close, wants a quick answer.
URGENT_HURT_S = 30.0
URGENT_MOB_BLOCKS = 8.0
# Calm enough to think longer: no hit for this long, no hostile this close.
CALM_HURT_S = 60.0
CALM_MOB_BLOCKS = 16.0

# What woke the turn, when it usually asks for a new or changed plan.
PLANNING_WAKES = frozenset({"ladder_low", "decision_handback", "user_message", "heartbeat", "life", "deep_thought"})
PLANNING_EVENTS = frozenset({"dawn"})
URGENT_WAKES = frozenset({"death", "respawned"})

LOW, HIGH, XHIGH = "low", "high", "xhigh"


@dataclass(frozen=True)
class EffortChoice:
    effort: str
    why: str


def _hurt_age(state: Any) -> float | None:
    source = getattr(state, "hurt_source", None)
    if not isinstance(source, Mapping):
        return None
    age = source.get("seconds_since_hurt")
    return float(age) if isinstance(age, (int, float)) and not isinstance(age, bool) else None


def _nearest_hostile(state: Any, entities: Sequence[Mapping[str, Any]]) -> tuple[float, str] | None:
    pos = getattr(state, "pos", None)
    best: tuple[float, str] | None = None
    for entity in entities:
        if not (entity.get("hostile") is True or entity.get("aggressive") is True):
            continue
        distance = entity.get("nearest_dist")
        if not isinstance(distance, (int, float)) or isinstance(distance, bool):
            dist2 = entity.get("dist2")
            if isinstance(dist2, (int, float)) and not isinstance(dist2, bool):
                distance = math.sqrt(max(0.0, float(dist2)))
            elif pos is not None and isinstance(entity.get("pos"), (list, tuple)) and len(entity["pos"]) == 3:
                distance = math.dist(tuple(float(v) for v in pos), tuple(float(v) for v in entity["pos"]))
            else:
                continue
        kind = str(entity.get("type") or entity.get("name") or "mob").removeprefix("minecraft:")
        if best is None or float(distance) < best[0]:
            best = (float(distance), kind)
    return best


def wake_names(wake: Mapping[str, Any] | None) -> tuple[str, str]:
    """The wake's reason (without a ':detail' suffix) and its body event name."""
    if not isinstance(wake, Mapping):
        return "", ""
    reason = str(wake.get("reason") or "").split(":", 1)[0]
    detail = wake.get("detail") if isinstance(wake.get("detail"), Mapping) else {}
    return reason, str(detail.get("name") or "")


def choose_effort(*, wake: Mapping[str, Any] | None, state: Any, entities: Sequence[Mapping[str, Any]] = (),
                  body_busy: bool = False, deep_requested: bool = False) -> EffortChoice:
    reason, event = wake_names(wake)
    age = _hurt_age(state)
    nearest = _nearest_hostile(state, entities)
    if age is not None and age <= URGENT_HURT_S:
        return EffortChoice(LOW, f"hurt {round(age)} s ago")
    if nearest is not None and nearest[0] <= URGENT_MOB_BLOCKS:
        return EffortChoice(LOW, f"{nearest[1]} at {round(nearest[0])} blocks")
    if reason in URGENT_WAKES:
        return EffortChoice(LOW, f"woken by {reason}")
    if deep_requested:
        return EffortChoice(XHIGH, "the Agent asked to think deeply")
    calm = (age is None or age >= CALM_HURT_S) and (nearest is None or nearest[0] > CALM_MOB_BLOCKS)
    if calm and body_busy:
        return EffortChoice(HIGH, "the Body is working and no danger is near")
    if calm and (reason in PLANNING_WAKES or event in PLANNING_EVENTS):
        return EffortChoice(HIGH, f"woken by {event or reason} and no danger is near")
    return EffortChoice(LOW, "default")


__all__ = ["EffortChoice", "choose_effort", "wake_names", "LOW", "HIGH", "XHIGH"]
