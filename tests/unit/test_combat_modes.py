"""Unit tests for combat FSM state/signals in modes.py (S5)."""

import unittest

from minebot.brain.lifecycle import LifecycleState
from minebot.brain.modes import AgentSignal, ModeRuntime, signalize_events
from minebot.contract import Event


class CombatModesTests(unittest.TestCase):
    def test_hostile_nearby_signal_engages(self):
        red = ModeRuntime().reduce([AgentSignal.hostile_nearby("zombie_nearby")], LifecycleState.ACTIVE)
        self.assertEqual(red.profile.situational, "engage")
        self.assertIn("combat", red.profile.tool_focus)
        self.assertEqual(red.profile.model_route, "fast")
        self.assertIn("combat", red.profile.policy_tags)

    def test_under_attack_signal_engages(self):
        red = ModeRuntime().reduce([AgentSignal.under_attack("hit_by_skeleton")], LifecycleState.ACTIVE)
        self.assertEqual(red.profile.situational, "engage")

    def test_survival_outranks_engage(self):
        red = ModeRuntime().reduce(
            [AgentSignal.hostile_nearby("z"), AgentSignal.survival_metric_red("low_health")],
            LifecycleState.ACTIVE,
        )
        self.assertEqual(red.profile.situational, "survival")

    def test_engage_outranks_mobility(self):
        red = ModeRuntime().reduce(
            [AgentSignal.mobility_blocked("stuck"), AgentSignal.hostile_nearby("z")],
            LifecycleState.ACTIVE,
        )
        self.assertEqual(red.profile.situational, "engage")

    def test_death_outranks_engage(self):
        red = ModeRuntime().reduce(
            [AgentSignal.hostile_nearby("z"), AgentSignal.death_detected("died")],
            LifecycleState.ACTIVE,
        )
        self.assertEqual(red.profile.situational, "death")

    def test_signalize_events_hostile_nearby(self):
        events = [Event(seq=1, tick=1, bot="B", name="hostileNearby", data={"reason": "zombie"})]
        signals = signalize_events(events)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].kind, "hostile_nearby")

    def test_signalize_events_under_attack(self):
        events = [Event(seq=1, tick=1, bot="B", name="underAttack", data={"reason": "skeleton"})]
        signals = signalize_events(events)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].kind, "under_attack")

    def test_engage_profile_axes(self):
        rt = ModeRuntime()
        rt.reduce([AgentSignal.hostile_nearby("z")], LifecycleState.ACTIVE)
        prof = rt.profile_for(LifecycleState.ACTIVE)
        self.assertEqual(prof.situational, "engage")
        self.assertEqual(prof.tool_focus, ("combat", "navigation", "perception"))
        self.assertEqual(prof.model_route, "fast")
        self.assertEqual(prof.policy_tags, ("combat",))


if __name__ == "__main__":
    unittest.main()
