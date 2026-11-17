"""F6 Expression — narration cadence, register, and honesty as policy data.

brain-cognitive-framework.md §9. Companionship quality lives mostly in *when
to speak, how much, in what register, and with what honesty phrasing* — today
those are implicit in the persona prompt and therefore unmanaged. This module
makes them data and control, never control flow: the policy decides whether a
already-produced message is rendered; it never decides what to do in the game.

Three concrete parts:

1. **Cadence** — a real, testable gate (minimum interval, per-minute cap,
   duplicate suppression) enforced at the chat egress.
2. **Register** — a trust-keyed style directive, the F5 → F6 join.
3. **Honesty** — rules carried as data so activation can inject them into the
   F2 identity section from one owner instead of prose scattered in a prompt.
   These restate C5 for speech; they may never be weakened below it.

Inert by default: :meth:`ExpressionPolicy.default` reproduces today's egress
behavior exactly (no throttle, consecutive-duplicate suppression only), which
is also the documented rollback.

Framework-agnostic: imports only stdlib.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType


@dataclass(frozen=True)
class NarrationRules:
    """When a produced message is allowed to reach the world.

    ``min_interval_s = 0`` and ``max_per_minute = None`` disable throttling,
    which is today's behavior. ``suppress_consecutive_duplicates`` matches the
    existing interactive sink, which already drops an immediate repeat.
    """

    min_interval_s: float = 0.0
    max_per_minute: int | None = None
    suppress_consecutive_duplicates: bool = True


@dataclass(frozen=True)
class HonestyRules:
    """Speech-side restatement of terminal-truth discipline (C5).

    ``forbid_unverified_success`` is a hard floor: it may be read, never
    lowered. The phrasing fields are directives injected into the identity
    context at activation, so failure/uncertainty language has one owner.
    """

    forbid_unverified_success: bool = True
    failure_directive: str = (
        "Report a failure plainly, name what actually happened, and never "
        "describe unfinished work as done."
    )
    uncertainty_directive: str = (
        "When the outcome is unverified, say so and name the fact you are "
        "still missing."
    )


# Trust tier -> style directive. Keys are TrustTier values (F5); this module
# stays import-free of app/ so the join is by value, guarded by tests.
DEFAULT_REGISTERS: Mapping[str, str] = MappingProxyType(
    {
        "owner": "Speak plainly and concisely; skip pleasantries and lead with the fact.",
        "friend": "Speak warmly and conversationally; give a little more context.",
        "stranger": "Speak politely and briefly; be friendly without volunteering plans or state.",
    }
)

DEFAULT_REGISTER = "Speak warmly and concisely."


@dataclass(frozen=True)
class ExpressionPolicy:
    narration: NarrationRules = field(default_factory=NarrationRules)
    registers: Mapping[str, str] = DEFAULT_REGISTERS
    honesty: HonestyRules = field(default_factory=HonestyRules)

    @classmethod
    def default(cls) -> "ExpressionPolicy":
        """Today's behavior: no throttle, duplicate suppression only."""
        return cls()

    @classmethod
    def companion(
        cls,
        *,
        min_interval_s: float = 8.0,
        max_per_minute: int = 6,
    ) -> "ExpressionPolicy":
        """A paced profile for always-on play; opt-in, never the default."""
        return cls(
            narration=NarrationRules(
                min_interval_s=min_interval_s,
                max_per_minute=max_per_minute,
            )
        )

    def register_directive(self, trust: str | None) -> str:
        return self.registers.get(str(trust or ""), DEFAULT_REGISTER)

    def honesty_directives(self) -> tuple[str, ...]:
        return (self.honesty.failure_directive, self.honesty.uncertainty_directive)


@dataclass(frozen=True)
class NarrationDecision:
    emit: bool
    reason: str


class NarrationGate:
    """Stateful cadence gate; pure with respect to an injected clock.

    The gate only ever *suppresses rendering*. It never rewrites a message,
    because silently editing what the model said would break the honesty
    contract it exists to serve.
    """

    def __init__(self, policy: ExpressionPolicy | None = None) -> None:
        self.policy = policy or ExpressionPolicy.default()
        self._emitted_at: deque[float] = deque()
        self._last_text: str | None = None

    def decide(self, text: str, *, now: float) -> NarrationDecision:
        rules = self.policy.narration
        if not text.strip():
            return NarrationDecision(False, "empty")
        if rules.suppress_consecutive_duplicates and text == self._last_text:
            return NarrationDecision(False, "duplicate")
        if rules.min_interval_s > 0 and self._emitted_at:
            elapsed = now - self._emitted_at[-1]
            if elapsed < rules.min_interval_s:
                return NarrationDecision(False, "min_interval")
        if rules.max_per_minute is not None:
            self._evict(now)
            if len(self._emitted_at) >= rules.max_per_minute:
                return NarrationDecision(False, "rate_limited")
        return NarrationDecision(True, "allowed")

    def record(self, text: str, *, now: float) -> None:
        """Commit an emission; call only when the message actually went out."""
        self._last_text = text
        self._emitted_at.append(now)
        self._evict(now)

    def _evict(self, now: float) -> None:
        while self._emitted_at and now - self._emitted_at[0] >= 60.0:
            self._emitted_at.popleft()


__all__ = [
    "DEFAULT_REGISTER",
    "DEFAULT_REGISTERS",
    "ExpressionPolicy",
    "HonestyRules",
    "NarrationDecision",
    "NarrationGate",
    "NarrationRules",
]
