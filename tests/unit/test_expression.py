"""F6 expression: cadence gate, registers, honesty data, egress decoration.

brain-cognitive-framework.md §9. The default policy must reproduce today's
egress behavior exactly; the gate must only ever suppress rendering (never
rewrite a message); suppression must always be observable.
"""

from __future__ import annotations

import unittest

from minebot.app.expression_egress import expression_speech_sink
from minebot.brain.expression import (
    DEFAULT_REGISTER,
    ExpressionPolicy,
    HonestyRules,
    NarrationGate,
    NarrationRules,
)


class _Clock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class DefaultPolicyTests(unittest.TestCase):
    def test_default_is_inert_apart_from_duplicate_suppression(self) -> None:
        policy = ExpressionPolicy.default()
        self.assertEqual(policy.narration.min_interval_s, 0.0)
        self.assertIsNone(policy.narration.max_per_minute)
        self.assertTrue(policy.narration.suppress_consecutive_duplicates)

    def test_default_gate_emits_every_distinct_message_without_delay(self) -> None:
        gate = NarrationGate()
        for index, text in enumerate(["one", "two", "three"]):
            decision = gate.decide(text, now=float(index))
            self.assertTrue(decision.emit, text)
            gate.record(text, now=float(index))

    def test_consecutive_duplicate_is_suppressed_but_a_later_repeat_is_not(self) -> None:
        gate = NarrationGate()
        gate.record("same", now=0.0)
        self.assertFalse(gate.decide("same", now=1.0).emit)
        gate.record("other", now=2.0)
        self.assertTrue(gate.decide("same", now=3.0).emit)

    def test_empty_text_never_emits(self) -> None:
        self.assertFalse(NarrationGate().decide("   ", now=0.0).emit)


class CadenceTests(unittest.TestCase):
    def test_min_interval_suppresses_until_it_elapses(self) -> None:
        gate = NarrationGate(ExpressionPolicy(narration=NarrationRules(min_interval_s=10.0)))
        gate.record("first", now=100.0)

        early = gate.decide("second", now=105.0)
        self.assertFalse(early.emit)
        self.assertEqual(early.reason, "min_interval")

        self.assertTrue(gate.decide("second", now=110.0).emit)

    def test_per_minute_cap_limits_burst_and_recovers_after_the_window(self) -> None:
        gate = NarrationGate(ExpressionPolicy(narration=NarrationRules(max_per_minute=2)))
        gate.record("a", now=0.0)
        gate.record("b", now=1.0)

        blocked = gate.decide("c", now=2.0)
        self.assertFalse(blocked.emit)
        self.assertEqual(blocked.reason, "rate_limited")

        # The 60s window rolls forward and frees a slot.
        self.assertTrue(gate.decide("c", now=61.0).emit)

    def test_suppression_does_not_consume_a_rate_slot(self) -> None:
        gate = NarrationGate(ExpressionPolicy(narration=NarrationRules(max_per_minute=1)))
        gate.record("a", now=0.0)
        self.assertFalse(gate.decide("b", now=1.0).emit)
        # Still exactly one recorded emission, so the window frees on schedule.
        self.assertTrue(gate.decide("b", now=61.0).emit)

    def test_companion_profile_is_opt_in_and_paced(self) -> None:
        policy = ExpressionPolicy.companion()
        self.assertGreater(policy.narration.min_interval_s, 0.0)
        self.assertIsNotNone(policy.narration.max_per_minute)


class RegisterAndHonestyTests(unittest.TestCase):
    def test_registers_are_keyed_by_trust_tier_values(self) -> None:
        policy = ExpressionPolicy.default()
        owner = policy.register_directive("owner")
        stranger = policy.register_directive("stranger")
        self.assertNotEqual(owner, stranger)
        self.assertIn("concise", owner.lower())

    def test_unknown_or_missing_trust_falls_back_to_a_safe_register(self) -> None:
        policy = ExpressionPolicy.default()
        self.assertEqual(policy.register_directive(None), DEFAULT_REGISTER)
        self.assertEqual(policy.register_directive("nonexistent"), DEFAULT_REGISTER)

    def test_register_keys_match_the_f5_trust_tier_values(self) -> None:
        from minebot.app.principals import TrustTier

        policy = ExpressionPolicy.default()
        for tier in TrustTier:
            self.assertIn(tier.value, policy.registers)

    def test_honesty_floor_is_declared_and_directives_are_available(self) -> None:
        policy = ExpressionPolicy.default()
        self.assertTrue(policy.honesty.forbid_unverified_success)
        directives = policy.honesty_directives()
        self.assertEqual(len(directives), 2)
        self.assertTrue(all(text.strip() for text in directives))

    def test_honesty_rules_are_immutable_data(self) -> None:
        rules = HonestyRules()
        with self.assertRaises(Exception):
            rules.forbid_unverified_success = False  # type: ignore[misc]


class EgressDecorationTests(unittest.TestCase):
    def test_default_wrapper_passes_every_distinct_message_through(self) -> None:
        spoken: list[str] = []
        sink = expression_speech_sink(spoken.append, clock=_Clock())

        sink("first")
        sink("second")

        self.assertEqual(spoken, ["first", "second"])

    def test_wrapper_reproduces_the_existing_duplicate_suppression(self) -> None:
        spoken: list[str] = []
        sink = expression_speech_sink(spoken.append, clock=_Clock())

        sink("same")
        sink("same")

        self.assertEqual(spoken, ["same"])

    def test_paced_policy_throttles_and_traces_every_suppression(self) -> None:
        spoken: list[str] = []
        events: list[tuple[str, dict[str, object]]] = []
        clock = _Clock()

        def trace(event: str, **fields: object) -> None:
            events.append((event, fields))

        sink = expression_speech_sink(
            spoken.append,
            policy=ExpressionPolicy(narration=NarrationRules(min_interval_s=5.0)),
            trace=trace,
            clock=clock,
        )

        sink("first")
        clock.advance(1.0)
        sink("second")
        clock.advance(10.0)
        sink("third")

        self.assertEqual(spoken, ["first", "third"])
        self.assertEqual([event for event, _ in events], ["speech_suppressed"])
        self.assertEqual(events[0][1]["reason"], "min_interval")

    def test_wrapper_never_rewrites_message_text(self) -> None:
        spoken: list[str] = []
        sink = expression_speech_sink(spoken.append, policy=ExpressionPolicy.companion(), clock=_Clock())

        sink("  I found spruce at (12, 70, -8).  ")

        self.assertEqual(spoken, ["  I found spruce at (12, 70, -8).  "])

    def test_inner_sink_failure_is_not_swallowed_by_the_wrapper(self) -> None:
        def failing(_text: str) -> None:
            raise RuntimeError("chat down")

        sink = expression_speech_sink(failing, clock=_Clock())
        with self.assertRaises(RuntimeError):
            sink("hello")


if __name__ == "__main__":
    unittest.main()
