"""F1 deliberation-economy skeleton: classifier fixtures + routing identity.

brain-cognitive-framework.md §4. The classifier must be a pure function of
enumerated deterministic facts, cover every precedence row, and the default
routing policy must be a behavior-preserving identity.
"""

from __future__ import annotations

import unittest

from minebot.brain.deliberation import (
    BODY_RECOVERY_FACT_KEY,
    DEFAULT_ROUTE,
    DEFAULT_ROUTING_POLICY,
    DecisionContext,
    RouteSpec,
    decision_context,
    resolve_route,
)
from minebot.brain.lifecycle import LifecycleState
from minebot.brain.modes import ModeReduction, RuntimeProfile


def _mode_facts(situational: str) -> ModeReduction:
    return ModeReduction(
        profile=RuntimeProfile(
            relationship="autonomous.user_request",
            situational=situational,  # type: ignore[arg-type]
            lifecycle="active",
            goal_lock="mutable",
            context_frame=f"{situational}-frame",
            tool_focus=(),
            model_route="primary",
            effort="standard",
            policy_tags=(),
        )
    )


class DecisionContextClassifierTests(unittest.TestCase):
    def classify(self, **overrides: object) -> DecisionContext:
        params: dict[str, object] = {
            "intent_kind": "start",
            "lifecycle_state": LifecycleState.ACTIVE,
            "mode_facts": _mode_facts("normal"),
            "last_epoch_facts": None,
            "has_durable_goal": True,
        }
        params.update(overrides)
        return decision_context(**params)  # type: ignore[arg-type]

    # -- one fixture per precedence row ------------------------------------

    def test_task_boundary_intent_is_boundary(self) -> None:
        self.assertIs(self.classify(intent_kind="task_boundary"), DecisionContext.BOUNDARY)

    def test_recovering_lifecycle_is_recovery(self) -> None:
        self.assertIs(
            self.classify(lifecycle_state=LifecycleState.RECOVERING),
            DecisionContext.RECOVERY,
        )

    def test_recovery_reconcile_intent_is_recovery(self) -> None:
        self.assertIs(self.classify(intent_kind="recovery_reconcile"), DecisionContext.RECOVERY)

    def test_death_stance_is_recovery(self) -> None:
        self.assertIs(self.classify(mode_facts=_mode_facts("death")), DecisionContext.RECOVERY)

    def test_settled_body_recovery_fact_is_recovery(self) -> None:
        self.assertIs(
            self.classify(last_epoch_facts={BODY_RECOVERY_FACT_KEY: True}),
            DecisionContext.RECOVERY,
        )

    def test_mobility_window_is_mobility(self) -> None:
        self.assertIs(self.classify(mode_facts=_mode_facts("mobility")), DecisionContext.MOBILITY)

    def test_maintenance_intent_is_maintenance(self) -> None:
        self.assertIs(self.classify(intent_kind="maintenance"), DecisionContext.MAINTENANCE)

    def test_goalless_message_is_social(self) -> None:
        self.assertIs(
            self.classify(intent_kind="message", has_durable_goal=False),
            DecisionContext.SOCIAL,
        )

    def test_active_goal_turn_is_normal(self) -> None:
        self.assertIs(self.classify(), DecisionContext.NORMAL)

    # -- adversarial precedence combinations -------------------------------

    def test_boundary_wins_over_recovery_and_mobility(self) -> None:
        self.assertIs(
            self.classify(
                intent_kind="task_boundary",
                lifecycle_state=LifecycleState.RECOVERING,
                mode_facts=_mode_facts("mobility"),
            ),
            DecisionContext.BOUNDARY,
        )

    def test_recovery_wins_over_mobility_window(self) -> None:
        self.assertIs(
            self.classify(
                lifecycle_state=LifecycleState.RECOVERING,
                mode_facts=_mode_facts("mobility"),
            ),
            DecisionContext.RECOVERY,
        )

    def test_mobility_wins_over_maintenance_intent(self) -> None:
        self.assertIs(
            self.classify(intent_kind="maintenance", mode_facts=_mode_facts("mobility")),
            DecisionContext.MOBILITY,
        )

    def test_message_with_durable_goal_is_not_social(self) -> None:
        self.assertIs(
            self.classify(intent_kind="message", has_durable_goal=True),
            DecisionContext.NORMAL,
        )

    def test_engage_and_survival_stances_stay_normal_class(self) -> None:
        # Combat/survival context framing is the mode system's job; routing
        # keeps them on the default class unless an operator says otherwise.
        for situational in ("engage", "survival"):
            self.assertIs(self.classify(mode_facts=_mode_facts(situational)), DecisionContext.NORMAL)

    def test_missing_mode_facts_and_intent_default_to_normal(self) -> None:
        self.assertIs(
            self.classify(intent_kind=None, mode_facts=None),
            DecisionContext.NORMAL,
        )


class RoutingPolicyTests(unittest.TestCase):
    def test_default_policy_is_identity_for_every_class(self) -> None:
        for context in DecisionContext:
            self.assertEqual(resolve_route(DEFAULT_ROUTING_POLICY, context), DEFAULT_ROUTE)
        self.assertEqual(DEFAULT_ROUTE, RouteSpec(model="primary", effort=None, context_profile="full"))

    def test_missing_class_fails_closed_to_primary_default(self) -> None:
        partial = {DecisionContext.SOCIAL: RouteSpec(model="fast", effort="low", context_profile="social")}
        self.assertEqual(resolve_route(partial, DecisionContext.BOUNDARY), DEFAULT_ROUTE)
        self.assertEqual(
            resolve_route(partial, DecisionContext.SOCIAL).model,
            "fast",
        )

    def test_default_policy_covers_every_declared_class(self) -> None:
        self.assertEqual(set(DEFAULT_ROUTING_POLICY), set(DecisionContext))


if __name__ == "__main__":
    unittest.main()
