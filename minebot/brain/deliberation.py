"""F1 Deliberation Economy — deterministic decision-context classification.

brain-cognitive-framework.md §4. This is the P0 *skeleton*: pure types, the
closed ``DecisionContext`` classifier, and the identity default routing
policy. There is no spine wiring here — the default policy maps every class
to today's exact behavior (``primary`` provider, provider-default effort,
``full`` context profile), so nothing changes until an operator opts in per
class at the composition root.

Hard constraints (§4.3):

- Classification derives ONLY from enumerated deterministic facts (intent
  kind, lifecycle state, mode reduction, settled epoch facts). Never from
  model output prose, never from a model's own request.
- A route changes cost/depth/context-profile only. It never alters the tool
  pool, tool schemas, or tool visibility, and never injects strategy.
- Resolution fails closed to the primary route: a missing or misconfigured
  class must not silently degrade to a weaker model.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Mapping

from minebot.brain.lifecycle import LifecycleState
from minebot.brain.modes import ModeReduction


class DecisionContext(str, Enum):
    """Closed set of turn classes; precedence is the declaration order."""

    BOUNDARY = "boundary"
    RECOVERY = "recovery"
    MOBILITY = "mobility"
    MAINTENANCE = "maintenance"
    SOCIAL = "social"
    NORMAL = "normal"


@dataclass(frozen=True)
class RouteSpec:
    """How one decision context is deliberated.

    ``model`` is a logical provider name (``"primary"`` / ``"fast"`` /
    ``"judge"`` ...), resolved by ``ModelProviderRegistry`` at the binding
    ring. ``effort`` overrides reasoning effort (``None`` = provider
    default). ``context_profile`` names the F2 compilation profile.
    """

    model: str
    effort: str | None
    context_profile: str


RoutingPolicy = Mapping[DecisionContext, RouteSpec]

# Identity route: exactly today's behavior. The P0 default policy maps every
# class to it, which is what makes landing this module behavior-preserving.
DEFAULT_ROUTE = RouteSpec(model="primary", effort=None, context_profile="full")
DEFAULT_ROUTING_POLICY: RoutingPolicy = MappingProxyType(
    {context: DEFAULT_ROUTE for context in DecisionContext}
)

# WorkIntent kinds (string values of app-layer WorkIntentKind; brain/ imports
# only contract + siblings, so the coupling is by value, guarded by tests).
_BOUNDARY_INTENTS = frozenset({"task_boundary"})
_RECOVERY_INTENTS = frozenset({"recovery_reconcile"})
_MAINTENANCE_INTENTS = frozenset({"maintenance"})
_SOCIAL_INTENTS = frozenset({"message"})

# Typed fact key an epoch settlement may carry to force the recovery class.
BODY_RECOVERY_FACT_KEY = "body_recovery_required"


def decision_context(
    *,
    intent_kind: str | None,
    lifecycle_state: LifecycleState,
    mode_facts: ModeReduction | None = None,
    last_epoch_facts: Mapping[str, object] | None = None,
    has_durable_goal: bool = False,
) -> DecisionContext:
    """Classify one turn from deterministic facts (first match wins).

    Precedence (brain-cognitive-framework.md §4.1):

    1. ``boundary`` — the turn exists to author a checkpoint/continuation
       disposition (``task_boundary`` intent).
    2. ``recovery`` — lifecycle is RECOVERING, a recovery-reconcile intent,
       the mode reduction reports the death stance, or the previous epoch
       settled with the typed body-recovery fact.
    3. ``mobility`` — the mode reduction currently reports the typed
       mobility terminal window (owned by ``brain/modes.py``; r49/r50b).
    4. ``maintenance`` — reflection/distillation maintenance intents.
    5. ``social`` — an ordinary conversational turn with no durable goal.
    6. ``normal`` — default active-goal turn.
    """

    kind = str(intent_kind or "").strip().lower()
    situational = mode_facts.profile.situational if mode_facts is not None else None

    if kind in _BOUNDARY_INTENTS:
        return DecisionContext.BOUNDARY
    if (
        lifecycle_state is LifecycleState.RECOVERING
        or kind in _RECOVERY_INTENTS
        or situational == "death"
        or _epoch_requires_body_recovery(last_epoch_facts)
    ):
        return DecisionContext.RECOVERY
    if situational == "mobility":
        return DecisionContext.MOBILITY
    if kind in _MAINTENANCE_INTENTS:
        return DecisionContext.MAINTENANCE
    if kind in _SOCIAL_INTENTS and not has_durable_goal:
        return DecisionContext.SOCIAL
    return DecisionContext.NORMAL


def resolve_route(policy: RoutingPolicy, context: DecisionContext) -> RouteSpec:
    """Resolve a context's route, failing closed to the primary default.

    A missing class never degrades silently to a weaker model: absence maps
    to ``DEFAULT_ROUTE`` (primary / provider-default effort / full context).
    The caller is responsible for emitting the fail-closed trace event when
    it observes the fallback (§4.3), which is wired in P1, not here.
    """

    return policy.get(context, DEFAULT_ROUTE)


def _epoch_requires_body_recovery(facts: Mapping[str, object] | None) -> bool:
    return bool(facts) and facts.get(BODY_RECOVERY_FACT_KEY) is True


__all__ = [
    "BODY_RECOVERY_FACT_KEY",
    "DEFAULT_ROUTE",
    "DEFAULT_ROUTING_POLICY",
    "DecisionContext",
    "RouteSpec",
    "RoutingPolicy",
    "decision_context",
    "resolve_route",
]
