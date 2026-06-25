"""LifecycleController — owned seam ④: one ACTIVE entry, symmetric resume/yield.

The prior agent's root defect was **five back doors into ACTIVE** plus a
**recovery asymmetry** (a stop side with no resume side, so a NaN/death recovery
silently failed to continue the task). This controller pins exactly one entry
into ACTIVE and makes yield/resume symmetric.

```
INIT → IDLE ↔ ACTIVE → YIELDED
                ↕          ↓
            INTERRUPTED → RESUMING
                ↕
            RECOVERING  (NaN / death / disconnect)
```

This is the **thin** version (``agent-layer-architecture.md`` §10): the FSM enum,
the single-entry guard, and the resume/yield symmetry exist and are
unit-testable. ``RECOVERING`` exposes the return-of-control hook; the strategic
"should we continue?" call is the LLM's (yield-to-human, ``agent-loop.md``), not
the controller's.

Framework-agnostic: imports only the stdlib.
"""

from __future__ import annotations

from enum import Enum


class LifecycleState(Enum):
    INIT = "init"
    IDLE = "idle"
    ACTIVE = "active"
    YIELDED = "yielded"
    INTERRUPTED = "interrupted"
    RESUMING = "resuming"
    RECOVERING = "recovering"


class LifecycleError(RuntimeError):
    """An illegal lifecycle transition was attempted."""


# The ONLY legal transitions. ACTIVE has exactly one inbound edge from a normal
# start (IDLE→ACTIVE) and one from a resume (RESUMING→ACTIVE); there is no other
# door in. Every stop edge has a matching return edge (symmetry).
_TRANSITIONS: dict[LifecycleState, frozenset[LifecycleState]] = {
    LifecycleState.INIT: frozenset({LifecycleState.IDLE}),
    LifecycleState.IDLE: frozenset({LifecycleState.ACTIVE}),
    LifecycleState.ACTIVE: frozenset({
        LifecycleState.YIELDED,       # progress authority / completion yield
        LifecycleState.INTERRUPTED,   # external interrupt
        LifecycleState.RECOVERING,    # NaN / death / disconnect
        LifecycleState.IDLE,          # clean stop, nothing pending
    }),
    LifecycleState.YIELDED: frozenset({
        LifecycleState.RESUMING,      # human/strategic decision to continue
        LifecycleState.IDLE,          # goal abandoned
    }),
    LifecycleState.INTERRUPTED: frozenset({
        LifecycleState.RESUMING,      # interrupt cleared, continue
        LifecycleState.RECOVERING,    # interrupt escalated to recovery
        LifecycleState.IDLE,          # abandoned
    }),
    LifecycleState.RECOVERING: frozenset({
        LifecycleState.RESUMING,      # body recovered -> return of control
        LifecycleState.IDLE,          # unrecoverable -> stand down
    }),
    LifecycleState.RESUMING: frozenset({
        LifecycleState.ACTIVE,        # the second (and only other) door into ACTIVE
    }),
}


class LifecycleController:
    """Single-entry ACTIVE guard with symmetric yield/resume."""

    def __init__(self) -> None:
        self._state = LifecycleState.INIT
        self._history: list[LifecycleState] = [LifecycleState.INIT]

    @property
    def state(self) -> LifecycleState:
        return self._state

    @property
    def is_active(self) -> bool:
        return self._state is LifecycleState.ACTIVE

    @property
    def history(self) -> list[LifecycleState]:
        return list(self._history)

    def can_transition(self, target: LifecycleState) -> bool:
        return target in _TRANSITIONS.get(self._state, frozenset())

    def transition(self, target: LifecycleState) -> LifecycleState:
        if not self.can_transition(target):
            allowed = ", ".join(sorted(s.value for s in _TRANSITIONS.get(self._state, frozenset())))
            raise LifecycleError(
                f"illegal transition {self._state.value} -> {target.value}; "
                f"allowed: {allowed or '<none>'}"
            )
        self._state = target
        self._history.append(target)
        return target

    # -- named intents (the only sanctioned ways to move) ---------------------

    def ready(self) -> None:
        self.transition(LifecycleState.IDLE)

    def start(self) -> None:
        """The single normal entry into ACTIVE (IDLE→ACTIVE)."""
        self.transition(LifecycleState.ACTIVE)

    def yield_(self, *, completed: bool = False) -> None:
        """ACTIVE→YIELDED (progress authority trip or completion). The strategic
        decision to resume is made elsewhere; the controller only records the
        stop and keeps the matching resume edge open."""
        self.transition(LifecycleState.YIELDED)

    def interrupt(self) -> None:
        self.transition(LifecycleState.INTERRUPTED)

    def enter_recovery(self) -> None:
        """ACTIVE/INTERRUPTED→RECOVERING (NaN / death / disconnect)."""
        self.transition(LifecycleState.RECOVERING)

    def resume(self) -> None:
        """The symmetric return: any stopped state → RESUMING. Pairs with every
        yield/interrupt/recovery so a stopped task is never stranded."""
        self.transition(LifecycleState.RESUMING)

    def reenter_active(self) -> None:
        """RESUMING→ACTIVE: the only other door into ACTIVE, and it is reachable
        only after a resume. This is what makes resume symmetric with yield."""
        self.transition(LifecycleState.ACTIVE)

    def stand_down(self) -> None:
        """Any non-active stopped state → IDLE (goal abandoned / unrecoverable)."""
        self.transition(LifecycleState.IDLE)


__all__ = ["LifecycleController", "LifecycleState", "LifecycleError"]
