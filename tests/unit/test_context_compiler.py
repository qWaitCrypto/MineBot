"""F2 context compiler: golden byte-compatibility + profiles + budget drops.

brain-cognitive-framework.md §5. The golden literals below were captured
from the pre-compiler ``AgentContext.turn_preamble`` implementation on
2026-07-26; the ``full`` profile must reproduce them byte-for-byte forever
(or the change is a deliberate, test-updating decision — never drift).
"""

from __future__ import annotations

import unittest
from dataclasses import replace

import pytest

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
    ctx.observe_life({"revision": 2, "routine": "Home before dark."})
    ctx.observe_state(_state())
    ctx.observe_profile(_profile())
    ctx.observe_resume(
        {"reason": "mobility_blocked", "goal": "collect 64 logs", "last_progress": {"pos": [1, 2, 3]}}
    )
    ctx.begin_turn()
    return ctx


GOLDEN_CONTRACT = (
    "TASK_RUNTIME_CONTRACT: The task goes on by itself when your turn ends: "
    "stop calling tools and say in a line or two what you did and what comes "
    "next; you rest until something wakes you. Call checkpoint_task only to wait for a named material event "
    "(wait_event), to put one grounded question to the player (yield), or to "
    "claim the goal complete with authoritative evidence (complete)."
)
GOLDEN_TASK = 'TASK_ARTIFACT: {"active": true, "task": {"revision": 2, "status": "running", "task_id": "t1"}}'
GOLDEN_SUMMARY = 'CONVERSATION_SUMMARY: {"archive_revision": 7, "compacted_turns": 3, "complete": true}'
GOLDEN_STATE = (
    "STATE: pos=(12.5, 70.0, -8.2) health=17.0 food=18 dim=overworld\n"
    "Clock: time 0, day; sunset at 12000, in about 600 seconds."
)
GOLDEN_PROFILE = (
    "PROFILE: relationship=autonomous.user_request situational=mobility "
    "lifecycle=active focus=navigation,recovery model=primary effort=standard "
    "policy=mobility frame=Mobility terminal context"
)
GOLDEN_LIFE = 'LIFE: {"revision": 2, "routine": "Home before dark."}'
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
        GOLDEN_LIFE,
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
        self.assertEqual(set(compiled.excluded),
                         {"session_messages", "conversation_summary", "life", "ladder", "home"})

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
        context = _full_context()
        context.observe_ladder("LADDER (your plan, in order; states are the Body's own record):")
        context.observe_home("HOME: You have no home in this world yet.")
        facts = context.snapshot_facts()
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


def test_state_line_shows_what_the_body_carries():
    import dataclasses
    from types import SimpleNamespace

    from minebot.brain.context_compiler import _render_body_state

    carrying = dataclasses.replace(_state(), inventory_counts={"oak_log": 46, "cobblestone": 48, "stick": 0})
    line = _render_body_state(SimpleNamespace(body_state=carrying))
    assert "dim=overworld inventory=[cobblestone 48, oak_log 46]\n" in line
    empty = dataclasses.replace(_state(), inventory_counts={})
    assert "inventory=[empty]\n" in _render_body_state(SimpleNamespace(body_state=empty))
    unknown = _render_body_state(SimpleNamespace(body_state=_state()))
    assert unknown == "STATE: pos=(12.5, 70.0, -8.2) health=17.0 food=18 dim=overworld\nClock: time 0, day; sunset at 12000, in about 600 seconds."


def test_state_line_says_when_the_agent_keeps_walks_looking_down():
    import dataclasses
    from types import SimpleNamespace

    from minebot.brain.context_compiler import _render_body_state

    on = dataclasses.replace(_state(), walk_look_down=True)
    assert "dim=overworld\nWalking: looking down (your setting)" in _render_body_state(SimpleNamespace(body_state=on))
    # False: a death turned the setting off and the Agent has not set it since.
    reset = dataclasses.replace(_state(), walk_look_down=False)
    assert "\nWalking: level (the last death turned your look-down setting off)" in _render_body_state(SimpleNamespace(body_state=reset))
    # None: never set, or the Agent turned it off itself.
    unset = dataclasses.replace(_state(), walk_look_down=None)
    assert "Walking" not in _render_body_state(SimpleNamespace(body_state=unset))


def test_state_says_the_leg_mode_and_a_death_that_ended_it():
    import dataclasses
    from types import SimpleNamespace
    from minebot.brain.context_compiler import _render_body_state

    moving = dataclasses.replace(_state(), movement={"mode": "keep_moving", "center": [0.5, 64.0, 30.2], "radius": 14.0})
    assert "\nMovement: keeps moving around [0, 64, 30] within 14 blocks (your setting)" in _render_body_state(SimpleNamespace(body_state=moving))
    reset = dataclasses.replace(_state(), movement={"mode": "stand", "reset_by_death": True})
    assert "\nMovement: standing (the last death ended your keep_moving setting)" in _render_body_state(SimpleNamespace(body_state=reset))
    standing = dataclasses.replace(_state(), movement={"mode": "stand"})
    assert "Movement" not in _render_body_state(SimpleNamespace(body_state=standing))


def test_state_says_the_clock_in_the_overworld_only():
    import dataclasses
    from types import SimpleNamespace
    from minebot.brain.context_compiler import _render_body_state

    def said(**changes):
        return _render_body_state(SimpleNamespace(body_state=dataclasses.replace(_state(), **changes)))

    assert "\nClock: time 6000, day; sunset at 12000, in about 300 seconds." in said(time=6000)
    assert "\nClock: time 23500, day; sunset at 12000, in about 625 seconds." in said(time=23500)
    assert "\nClock: time 12500, dusk; night falls at 13000, in about 25 seconds." in said(time=12500)
    assert "\nClock: time 16179, night; day comes at about 23000, in about 341 seconds." in said(
        time=16179, dimension="minecraft:overworld")
    assert "Clock" not in said(time=16179, dimension="minecraft:the_nether")


def test_state_says_the_last_hurt_as_the_client_saw_it():
    import dataclasses
    from types import SimpleNamespace
    from minebot.brain.context_compiler import _render_body_state

    def said(source):
        return _render_body_state(SimpleNamespace(body_state=dataclasses.replace(_state(), hurt_source=source)))

    assert "\nLast hurt: 12 seconds ago by Creeper (player_explosion), 2 blocks away at that moment." in said(
        {"damage_type": "minecraft:player_explosion", "name": "Creeper", "type": "creeper",
         "distance": 1.9, "seconds_since_hurt": 12.3})
    assert "\nLast hurt: 40 seconds ago (fall)." in said({"damage_type": "minecraft:fall", "seconds_since_hurt": 40.0})
    assert "\nLast hurt: 1 second ago by Zombie (mob_attack), 1 block away at that moment." in said(
        {"damage_type": "mob_attack", "name": "Zombie", "distance": 1.2, "seconds_since_hurt": 0.8})
    assert "Last hurt" not in said(None)


def test_last_hurt_says_where_that_mob_is_now():
    """run-36: the distance at the hit was read as the skeleton's place now; it was 18 blocks off."""
    import dataclasses
    from types import SimpleNamespace
    from minebot.brain.context_compiler import _render_body_state

    arrow = {"damage_type": "minecraft:arrow", "name": "Skeleton", "type": "skeleton", "id": "sk-1",
             "distance": 2.6, "seconds_since_hurt": 7.0}

    def said(entities):
        return _render_body_state(SimpleNamespace(
            body_state=dataclasses.replace(_state(), hurt_source=arrow), nearby_entities=tuple(entities)))

    assert ("\nLast hurt: 7 seconds ago by Skeleton (arrow), 3 blocks away at that moment; "
            "that Skeleton is 18 blocks away now.") in said(
        [{"id": "sk-2", "type": "skeleton", "hostile": True, "nearest_dist": 12.0},
         {"id": "sk-1", "type": "skeleton", "hostile": True, "nearest_dist": 18.4}])
    assert "; that Skeleton is not among the entities the Body sees now." in said(
        [{"id": "zo-1", "type": "zombie", "hostile": True, "nearest_dist": 20.0}])
    assert "3 blocks away at that moment." in said([])


def test_notable_dragon_and_crystals_use_reported_facts_in_any_dimension():
    ctx = AgentContext(system_prompt="SP", goal_text="g")
    ctx.observe_state(_state(), nearby_entities=[
        {"type": "ender_dragon", "boss": True, "health": 200, "max_health": 200,
         "phase": "circling", "nearest_dist": 48.4, "pos": [41.8, 90.5, -24.2]},
        *[{"type": "end_crystal", "caged": index < 2, "dist2": (31 + index) ** 2}
          for index in range(7)],
    ])
    text = ctx.compile().text
    assert "dim=overworld" in text  # Classification does not depend on dimension.
    assert text.endswith(
        "NOTABLE (the Body's sample of nearest entities, not a full count): ender_dragon health 200/200 phase circling 48 blocks "
        "pos=(41, 90, -25); end_crystal ×7 (2 caged) nearest 31 blocks"
    )


@pytest.mark.parametrize("entities", [
    [],
    [{"type": "sheep", "health": 8, "max_health": 8, "hostile": False,
      "aggressive": False, "dist2": 4}],
    [{"type": "enderman", "hostile": False, "aggressive": False, "dist2": 4}],
])
def test_no_notable_creatures_means_no_line(entities):
    ctx = AgentContext(system_prompt="SP", goal_text="g")
    ctx.observe_state(_state(), nearby_entities=entities)
    assert "NOTABLE:" not in ctx.compile().text


def test_notable_groups_hostiles_using_nearest_distance_and_observed_anger():
    ctx = AgentContext(system_prompt="SP", goal_text="g")
    ctx.observe_state(_state(), nearby_entities=[
        {"type": "enderman", "hostile": True, "aggressive": True, "nearest_dist": 16, "dist2": 400},
        {"type": "zombie", "hostile": True, "dist": 4},
        {"type": "zombie", "hostile": True, "pos": _state().pos},
        {"type": "skeleton", "hostile": True, "dist": 17},
        {"type": "creeper", "hostile": True},  # Unknown distance: counted, no nearest.
    ])
    assert ctx.compile().text.endswith(
        "NOTABLE (the Body's sample of nearest entities, not a full count): hostile mobs: creeper ×1, enderman ×1 (1 aggressive) nearest 16 blocks,"
        " skeleton ×1 nearest 17 blocks, zombie ×2 nearest 0 blocks"
    )


def test_notable_missing_fields_are_not_inferred_and_new_samples_clear_old_ones():
    ctx = AgentContext(system_prompt="SP", goal_text="g")
    entities = [{"type": "wither", "boss": True, "health": 100},
                {"type": "end_crystal", "caged": True}, {"type": "end_crystal"}]
    ctx.observe_state(_state(), nearby_entities=entities)
    entities[0]["health"] = 0
    snapshot = ctx.snapshot_facts()
    assert ctx.compile().text.endswith(
        "NOTABLE (the Body's sample of nearest entities, not a full count): wither health 100; end_crystal ×2 (1 caged, others unknown)"
    )
    ctx.observe_state(replace(_state(), dimension="the_nether"))
    assert "NOTABLE:" not in ctx.compile().text
    assert "wither health 100" in compile_context("terse", snapshot).text


def test_notable_length_cap_and_single_line_even_with_many_named_bosses():
    from minebot.brain.context_compiler import NOTABLE_MAX_CHARS

    ctx = AgentContext(system_prompt="SP", goal_text="g")
    ctx.observe_state(_state(), nearby_entities=[
        {"name": "long\nname " * 100, "boss": True, "health": 200, "max_health": 200,
         "phase": "phase\n" * 100, "pos": [index, 70.2, -2.1]}
        for index in range(50)
    ])
    notable = "NOTABLE " + ctx.compile().text.split("\nNOTABLE ", 1)[1]
    assert "\n" not in notable
    assert len(notable) <= NOTABLE_MAX_CHARS
    assert notable.endswith("…")


def test_state_says_what_the_body_wears_apart_from_what_it_carries():
    # run-13: full iron armour sat in the bag ~80 minutes; the inventory count
    # read the same worn or not (body-tool-boundaries-20261110 §6).
    import dataclasses
    from types import SimpleNamespace

    from minebot.brain.context_compiler import _render_body_state

    state = dataclasses.replace(_state(), inventory_counts={"iron_chestplate": 1, "iron_sword": 1},
                                armor={"head": "iron_helmet", "chest": "", "legs": "", "feet": "iron_boots"},
                                selected_item="iron_sword", offhand_item="shield")
    line = _render_body_state(SimpleNamespace(body_state=state))
    assert ("\nWearing: head iron_helmet, chest nothing, legs nothing, feet iron_boots; "
            "main hand iron_sword; off hand shield") in line
    assert "Wearing" not in _render_body_state(SimpleNamespace(body_state=_state()))


def test_state_says_how_many_uses_each_tool_has_left():
    # run-15 / run-18: the only iron pickaxe broke mid-dig unseen (body-tool-boundaries §13).
    import dataclasses
    from types import SimpleNamespace

    from minebot.brain.context_compiler import _render_body_state

    state = dataclasses.replace(_state(), tool_wear=[{"item": "iron_sword", "left": 240, "max": 250},
                                                     {"item": "iron_pickaxe", "left": 12, "max": 250}])
    line = _render_body_state(SimpleNamespace(body_state=state))
    assert "\nUses left: iron_pickaxe 12 of 250, iron_sword 240 of 250" in line
    assert "Uses left" not in _render_body_state(SimpleNamespace(body_state=_state()))


def test_state_says_how_bright_the_spot_is():
    # Four easy runs placed no torch while a quarter to a third of their time
    # was spent below y 60: the Agent had no way to see that a place was dark.
    import dataclasses
    from types import SimpleNamespace

    from minebot.brain.context_compiler import _render_body_state

    cave = dataclasses.replace(_state(), light={"block": 0, "sky": 0, "open_sky": False})
    assert ("\nLight where the Body stands: 0 of 15 from torches and other lit blocks, "
            "0 of 15 of the sky reaches it; no open sky above") in _render_body_state(SimpleNamespace(body_state=cave))
    field = dataclasses.replace(_state(), light={"block": 14, "sky": 15, "open_sky": True})
    assert "14 of 15 from torches and other lit blocks, 15 of 15 of the sky reaches it; open sky above" in (
        _render_body_state(SimpleNamespace(body_state=field)))
    assert "Light where" not in _render_body_state(SimpleNamespace(body_state=_state()))
