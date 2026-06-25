import unittest

from minebot.brain.lifecycle import LifecycleState
from minebot.brain.modes import AgentSignal, ModeRuntime, signalize_body_state, signalize_events
from minebot.contract import BodyState, Event, ProgressFacts


def state(**overrides):
    data = dict(
        bot="Bot",
        pos=(0.0, 64.0, 0.0),
        yaw=None,
        pitch=None,
        health=20.0,
        food=20,
        oxygen=300,
        inventory_raw="[]",
        inventory_hash="empty",
        effects=None,
        time=1000,
        weather=None,
        dimension="overworld",
        complete=True,
    )
    data.update(overrides)
    return BodyState(**data)


class ModeRuntimeTests(unittest.TestCase):
    def test_default_profile_is_autonomous_normal(self):
        modes = ModeRuntime()

        reduction = modes.reduce([], LifecycleState.ACTIVE, goal_text="collect 64 dirt")

        self.assertEqual(reduction.profile.relationship, "autonomous.user_request")
        self.assertEqual(reduction.profile.situational, "normal")
        self.assertEqual(reduction.profile.goal_lock, "mutable")
        self.assertIn("resource", reduction.profile.tool_focus)
        self.assertEqual(reduction.profile.model_route, "primary")
        self.assertIsNone(reduction.requested_lifecycle)

    def test_progress_abort_requests_yield_and_keeps_facts(self):
        modes = ModeRuntime()
        facts = ProgressFacts(
            goal="collect 64 dirt",
            last_action=("navigate.segment",),
            stagnant_steps=3,
            stalled_steps=8,
            failure_steps=0,
            last_fingerprint="a",
            current_fingerprint="a",
            recent_events=[],
        )

        reduction = modes.reduce(
            [AgentSignal.progress_abort(facts)],
            LifecycleState.ACTIVE,
            goal_text="collect 64 dirt",
        )

        self.assertEqual(reduction.requested_lifecycle, LifecycleState.YIELDED)
        self.assertEqual(reduction.profile.situational, "mobility")
        self.assertIsNotNone(modes.suspend_slot)
        self.assertEqual(modes.suspend_slot.goal_text, "collect 64 dirt")
        consumed = modes.consume_suspend_slot()
        self.assertIsNotNone(consumed)
        self.assertEqual(consumed.goal_text, "collect 64 dirt")
        self.assertIsNone(modes.suspend_slot)

    def test_survival_reflex_changes_situational_without_lifecycle_request(self):
        modes = ModeRuntime()

        reduction = modes.reduce(
            [AgentSignal.body_reflex_started("lava")],
            LifecycleState.ACTIVE,
            goal_text="collect 64 dirt",
        )

        self.assertEqual(reduction.profile.situational, "survival")
        self.assertIsNone(reduction.requested_lifecycle)
        self.assertIn("survival", reduction.profile.tool_focus)

        recovered = modes.reduce(
            [AgentSignal.body_reflex_completed("lava")],
            LifecycleState.ACTIVE,
            goal_text="collect 64 dirt",
        )
        self.assertEqual(recovered.profile.situational, "normal")

    def test_death_requests_recovery_then_recovery_completed_requests_resume(self):
        modes = ModeRuntime()

        death = modes.reduce(
            [AgentSignal.death_detected()],
            LifecycleState.ACTIVE,
            goal_text="collect 64 dirt",
        )
        self.assertEqual(death.profile.situational, "death")
        self.assertEqual(death.requested_lifecycle, LifecycleState.RECOVERING)

        recovered = modes.reduce(
            [AgentSignal.recovery_completed()],
            LifecycleState.RECOVERING,
            goal_text="collect 64 dirt",
        )
        self.assertEqual(recovered.profile.situational, "normal")
        self.assertEqual(recovered.requested_lifecycle, LifecycleState.RESUMING)

    def test_signalize_body_state_and_events(self):
        self.assertEqual(signalize_body_state(state(health=4.0))[0].kind, "survival_metric_red")
        self.assertEqual(signalize_body_state(state(health=0.0))[0].kind, "death_detected")

        events = [
            Event(seq=1, tick=10, bot="Bot", name="reflexTriggered", data={"kind": "water"}),
            Event(seq=2, tick=11, bot="Bot", name="navigationBlocked", data={"reason": "blocked"}),
        ]
        kinds = [signal.kind for signal in signalize_events(events)]
        self.assertEqual(kinds, ["body_reflex_started", "mobility_blocked"])


if __name__ == "__main__":
    unittest.main()
