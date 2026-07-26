"""F2 context compiler: golden byte-compatibility + profiles + budget drops.

brain-cognitive-framework.md §5. The golden literals below were captured
from the pre-compiler ``AgentContext.turn_preamble`` implementation on
2026-07-26; the ``full`` profile must reproduce them byte-for-byte forever
(or the change is a deliberate, test-updating decision — never drift).
"""

from __future__ import annotations

import unittest

from minebot.brain.context import AgentContext
from minebot.brain.context_compiler import (
    CONTEXT_PROFILES,
    ContextBudget,
    SECTION_REGISTRY,
    compile_context,
)
from minebot.brain.modes import RuntimeProfile
from minebot.contract import BodyState


def _state() -> BodyState:
    return BodyState(
        bot="Bot1",
        pos=(12.5, 70.0, -8.25),
        yaw=None,
        pitch=None,
        health=17.0,
        food=18,
        oxygen=300,
        inventory_raw="",
        inventory_hash="h1",
        effects=None,
        time=0,
        weather=None,
        dimension=None,
        complete=True,
    )


def _profile() -> RuntimeProfile:
    return RuntimeProfile(
        relationship="autonomous.user_request",
        situational="mobility",
        lifecycle="active",
        goal_lock="mutable",
        context_frame="Mobility terminal context",
        tool_focus=("navigation", "recovery"),
        model_route="primary",
        effort="standard",
        policy_tags=("mobility",),
    )


def _full_context() -> AgentContext:
    ctx = AgentContext(system_prompt="SP", goal_text="collect 64 logs", language="Chinese")
    ctx.observe_user_message("hello  there", sender="qWait")
    ctx.observe_assistant_message("on it")
    ctx.observe_system_message("restart happened")
    ctx.observe_task({"task": {"task_id": "t1", "status": "running", "revision": 2}, "active": True})
    ctx.observe_conversation_summary({"compacted_turns": 3, "complete": True, "archive_revision": 7})
    ctx.observe_state(_state())
    ctx.observe_profile(_profile())
    ctx.observe_resume(
        {"reason": "mobility_blocked", "goal": "collect 64 logs", "last_progress": {"pos": [1, 2, 3]}}
    )
    ctx.begin_turn()
    return ctx


GOLDEN_CONTRACT = (
    "TASK_RUNTIME_CONTRACT: A durable task spans finite SDK runs only "
    "through checkpoint_task. Before final output, record exactly one "
    "explicit disposition: continue with a structured continuation when "
    "the unfinished goal remains actionable; wait_event only for a named "
    "material wake condition; yield only for a grounded bounded blocker; "
    "complete only with authoritative evidence."
)
GOLDEN_TASK = 'TASK_ARTIFACT: {"active": true, "task": {"revision": 2, "status": "running", "task_id": "t1"}}'
GOLDEN_SUMMARY = 'CONVERSATION_SUMMARY: {"archive_revision": 7, "compacted_turns": 3, "complete": true}'
GOLDEN_STATE = "STATE: pos=(12.5, 70.0, -8.2) health=17.0 food=18 dim=overworld"
GOLDEN_PROFILE = (
    "PROFILE: relationship=autonomous.user_request situational=mobility "
    "lifecycle=active focus=navigation,recovery model=primary effort=standard "
    "policy=mobility frame=Mobility terminal context"
)
GOLDEN_RESUME = "RESUME: reason=mobility_blocked goal=collect 64 logs last_progress={'pos': [1, 2, 3]}"
GOLDEN_MESSAGES = (
    "SESSION_MESSAGES: user: qWait: hello there | assistant: on it | system: restart happened"
)
GOLDEN_FULL_WITH_MESSAGES = "\n".join(
    (
        "GOAL: collect 64 logs",
        "SESSION: turn=1 language=Chinese",
        GOLDEN_MESSAGES,
        GOLDEN_TASK,
        GOLDEN_CONTRACT,
        GOLDEN_SUMMARY,
        GOLDEN_STATE,
        GOLDEN_PROFILE,
        GOLDEN_RESUME,
    )
)


class GoldenByteCompatibilityTests(unittest.TestCase):
    def test_empty_context(self) -> None:
        ctx = AgentContext(system_prompt="SP", goal_text="")
        ctx.begin_turn()
        self.assertEqual(ctx.turn_preamble(), "SESSION: turn=1 language=English")

    def test_goal_only(self) -> None:
        ctx = AgentContext(system_prompt="SP", goal_text="collect 64 logs")
        ctx.begin_turn()
        self.assertEqual(
            ctx.turn_preamble(), "GOAL: collect 64 logs\nSESSION: turn=1 language=English"
        )

    def test_full_stack_with_messages(self) -> None:
        self.assertEqual(_full_context().turn_preamble(), GOLDEN_FULL_WITH_MESSAGES)

    def test_resume_consumed_after_render(self) -> None:
        ctx = _full_context()
        first = ctx.turn_preamble()
        second = ctx.turn_preamble()
        self.assertIn("RESUME:", first)
        self.assertEqual(second, GOLDEN_FULL_WITH_MESSAGES.replace("\n" + GOLDEN_RESUME, ""))

    def test_full_without_session_messages(self) -> None:
        expected = GOLDEN_FULL_WITH_MESSAGES.replace("\n" + GOLDEN_MESSAGES, "")
        self.assertEqual(
            _full_context().turn_preamble(include_session_messages=False), expected
        )

    def test_full_without_goal(self) -> None:
        expected = GOLDEN_FULL_WITH_MESSAGES.replace("GOAL: collect 64 logs\n", "")
        self.assertEqual(_full_context().turn_preamble(include_goal=False), expected)

    def test_pending_task_and_zero_compaction_render_nothing_extra(self) -> None:
        ctx = AgentContext(system_prompt="SP", goal_text="g")
        ctx.observe_task({"task": {"status": "pending"}})
        ctx.observe_conversation_summary({"compacted_turns": 0})
        ctx.begin_turn()
        self.assertEqual(
            ctx.turn_preamble(),
            'GOAL: g\nSESSION: turn=1 language=English\nTASK_ARTIFACT: {"task": {"status": "pending"}}',
        )

    def test_compile_full_equals_turn_preamble(self) -> None:
        ctx = _full_context()
        compiled = compile_context("full", ctx.snapshot_facts())
        self.assertEqual(compiled.text, GOLDEN_FULL_WITH_MESSAGES)
        self.assertEqual(compiled.dropped, ())
        self.assertEqual(compiled.excluded, ())
        self.assertFalse(compiled.truncated)


class ProfileCompositionTests(unittest.TestCase):
    def test_registry_ids_are_unique_and_profiles_reference_real_sections(self) -> None:
        ids = [spec.section_id for spec in SECTION_REGISTRY]
        self.assertEqual(len(ids), len(set(ids)))
        for name, members in CONTEXT_PROFILES.items():
            self.assertTrue(members <= set(ids), f"profile {name} references unknown sections")

    def test_terse_is_exactly_the_priority_band(self) -> None:
        expected = {spec.section_id for spec in SECTION_REGISTRY if spec.priority <= 1}
        self.assertEqual(CONTEXT_PROFILES["terse"], expected)

    def test_terse_keeps_load_bearing_lines_and_drops_dialogue(self) -> None:
        compiled = compile_context("terse", _full_context().snapshot_facts())
        self.assertIn("GOAL:", compiled.text)
        self.assertIn("STATE:", compiled.text)
        self.assertIn("TASK_ARTIFACT:", compiled.text)
        self.assertIn("RESUME:", compiled.text)  # one-shot facts survive terse
        self.assertNotIn("SESSION_MESSAGES:", compiled.text)
        self.assertNotIn("CONVERSATION_SUMMARY:", compiled.text)
        self.assertEqual(set(compiled.excluded), {"session_messages", "conversation_summary"})

    def test_social_and_maintenance_compose_as_declared(self) -> None:
        facts = _full_context().snapshot_facts()
        social = compile_context("social", facts)
        self.assertIn("SESSION_MESSAGES:", social.text)
        self.assertNotIn("TASK_ARTIFACT:", social.text)
        self.assertNotIn("PROFILE:", social.text)
        maintenance = compile_context("maintenance", facts)
        self.assertIn("TASK_ARTIFACT:", maintenance.text)
        self.assertNotIn("SESSION_MESSAGES:", maintenance.text)

    def test_unknown_profile_is_a_loud_error(self) -> None:
        with self.assertRaises(ValueError):
            compile_context("nonexistent", _full_context().snapshot_facts())


class BudgetDropTests(unittest.TestCase):
    def test_drops_whole_sections_highest_priority_later_position_first(self) -> None:
        facts = _full_context().snapshot_facts()
        unlimited = compile_context("full", facts)
        # Force exactly one drop: the later of the two priority-3 sections
        # (conversation_summary) goes first.
        budget = ContextBudget(max_chars=unlimited.chars - 1)
        compiled = compile_context("full", facts, budget)
        self.assertEqual(compiled.dropped[0], "conversation_summary")
        self.assertNotIn("CONVERSATION_SUMMARY:", compiled.text)
        self.assertLessEqual(compiled.chars, budget.max_chars)
        self.assertFalse(compiled.truncated)

    def test_priority_zero_sections_are_never_dropped(self) -> None:
        facts = _full_context().snapshot_facts()
        compiled = compile_context("full", facts, ContextBudget(max_chars=10))
        self.assertTrue(compiled.truncated)
        self.assertEqual(
            set(compiled.sections), {"goal", "session_header", "body_state"}
        )
        droppable = {spec.section_id for spec in SECTION_REGISTRY if spec.priority > 0}
        self.assertEqual(set(compiled.dropped), droppable - set(compiled.excluded))
        # No mid-fact truncation: every surviving line is complete.
        self.assertIn("GOAL: collect 64 logs", compiled.text.splitlines())

    def test_budget_respecting_compile_is_deterministic(self) -> None:
        facts = _full_context().snapshot_facts()
        budget = ContextBudget(max_chars=400)
        first = compile_context("full", facts, budget)
        second = compile_context("full", facts, budget)
        self.assertEqual(first, second)

    def test_facade_does_not_consume_resume_when_profile_excludes_it(self) -> None:
        ctx = _full_context()
        social = ctx.compile("social")
        self.assertNotIn("RESUME:", social.text)
        # The one-shot fact must survive for the next full compile.
        self.assertIn("RESUME:", ctx.compile("full").text)
        # ...and only then be consumed.
        self.assertNotIn("RESUME:", ctx.compile("full").text)


if __name__ == "__main__":
    unittest.main()
