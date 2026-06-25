"""AgentContext — owned seam ③: goal single-ownership + state injection.

The SDK's Sessions manage conversation history; they do not own the goal, inject
per-turn Body state, or re-inject the goal as context scrolls. Per
``agent-loop.md`` §5 the goal must have exactly one textual owner, re-injected on
a fixed cadence. ``AgentContext`` is that owner.

This is the **thin** version (``agent-layer-architecture.md`` §10): the slot
physically exists and is unit-testable. Sliding-window/summary depth and the
exact re-injection cadence are deferred to the first long-running e2e (§12); the
single-ownership contract is what must exist now so the stance-FSM, Skills, and
memory/RAG slots are additive later (§8).

Framework-agnostic: imports only ``minebot.contract``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from minebot.contract import BodyState

# Re-inject the goal every N model turns so it never scrolls out of the window.
DEFAULT_GOAL_REINJECT_EVERY = 5


@dataclass
class AgentContext:
    """Single textual owner of the goal + per-turn state injection point.

    This is the shared feed for three future slots: the stance FSM swaps a
    "context profile" here, agent Skills inject methodology here, and memory/RAG
    retrieval feeds here. Designing it as the one context owner now is what makes
    all three additive.
    """

    system_prompt: str
    goal_text: str
    goal_reinject_every: int = DEFAULT_GOAL_REINJECT_EVERY
    _turn: int = 0
    _last_state: BodyState | None = field(default=None, repr=False)

    # -- goal ownership -------------------------------------------------------

    def set_goal(self, goal_text: str) -> None:
        """Replace the goal. Re-injection cadence restarts so the new goal is
        guaranteed to appear on the very next turn."""
        self.goal_text = goal_text
        self._turn = 0

    def observe_state(self, state: BodyState) -> None:
        """Record the latest authoritative Body state for per-turn injection."""
        self._last_state = state

    # -- per-turn assembly ----------------------------------------------------

    def begin_turn(self) -> int:
        self._turn += 1
        return self._turn

    def should_reinject_goal(self) -> bool:
        """True on the first turn and every Nth turn thereafter."""
        return self._turn <= 1 or (self._turn - 1) % self.goal_reinject_every == 0

    def turn_preamble(self) -> str:
        """The text prepended to a model turn: goal (on cadence) + live state.

        The goal line is the single source of goal truth; no other component
        emits it. State is injected every turn so the model always reasons over
        current Body facts, not a stale snapshot.
        """
        parts: list[str] = []
        if self.should_reinject_goal():
            parts.append(f"GOAL: {self.goal_text}")
        if self._last_state is not None:
            parts.append(self._state_line(self._last_state))
        return "\n".join(parts)

    @staticmethod
    def _state_line(state: BodyState) -> str:
        pos = ", ".join(f"{value:.1f}" for value in state.pos)
        return (
            f"STATE: pos=({pos}) health={state.health:.1f} food={state.food} "
            f"dim={state.dimension or 'overworld'}"
        )


__all__ = ["AgentContext", "DEFAULT_GOAL_REINJECT_EVERY"]
