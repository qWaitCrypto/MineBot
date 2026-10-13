"""The frame question wording, checked against the sentences settled offline."""

import json
from pathlib import Path

import pytest

from minebot.app import reflex_question as rq

GOAL = "make a stone pickaxe"
CALL = dict(tool="collect_resource", params={"count": 3, "item": "raw_iron", "timeout_s": 60, "intent": "x"})


def _running(facts, **extra):
    return rq.frame_state(goal=GOAL, intent="get 3 raw iron", facts=facts, running=True, **CALL, **extra)


def test_running_frame_says_each_fact_once_as_a_sentence():
    state = _running(
        {"health": 20, "food": 20, "oxygen": 300, "inventory": {"stone_pickaxe": 1, "dirt": 2},
         "inventory_delta": {"raw_iron": 1, "dirt": -2},
         "start_position": [60.6, 64, -51.5], "position": [49.7, 62.4, -61.3],
         "entities": [{"hostile": True, "name": "Creeper", "dist": 21.4}, {"hostile": False, "name": "Cow", "dist": 2}]},
        phase="mining", elapsed_s=3.71, progress_signal="world_edit", since_progress_s=0.0,
    )
    assert state == {
        "task": GOAL,
        "agent_intent": "The Agent wants the following result: “get 3 raw iron”.",
        "inventory": "stone_pickaxe 1, dirt 2",
        "body": "Health is 20 of 20. Hunger is 20 of 20. Air supply is full.",
        "threats": "Nearest hostile mob: Creeper, 21 blocks away.",
        "current_work": 'collect_resource(count=3, item="raw_iron") is running. elapsed 3.7s. Phase: mining.',
        "progress": "last progress event: world_edit, 0.0s ago. inventory changed: raw_iron +1, dirt -2.",
        "position": "started at [60.6, 64.0, -51.5]; now at [49.7, 62.4, -61.3].",
    }


def test_changing_air_and_injury_are_written_as_change():
    facts = {"health": 3.0, "food": 16, "oxygen": 139, "health_before": 6.0, "oxygen_before": 243, "vital_age_s": 5.2}
    assert _running(facts)["body"] == (
        "Health is 3 of 20. Hunger is 16 of 20. The head is under water and the Body cannot breathe: "
        "air supply was 243 5 seconds ago and is 139 of 300 now. "
        "The Body was just hurt: health fell from 6 to 3 in the last 5 seconds."
    )
    assert "drowning" in _running({"oxygen": -12})["body"]
    assert "hurt" not in _running({"health": 20.0, "health_before": 20.2})["body"]
    assert _running({"health": 20})["progress"] == "no real progress has been observed yet."
    assert "inventory" not in _running({"health": 20})
    assert _running({"inventory": {}})["inventory"] == "empty"


def test_equipment_intent_sees_current_main_hand_once_in_all_frames():
    # Live seq1825: wooden pickaxe was in the bag, but the hand still held a sapling.
    facts = {"inventory_counts": {"wooden_pickaxe": 1}, "selected_item": "oak_sapling",
             "health": 20, "food": 20, "oxygen": 300}
    for running in (True, False):
        state = rq.frame_state(goal="make a pickaxe", intent="craft and equip a wooden pickaxe",
            tool="craft_item", params={"item": "wooden_pickaxe", "auto_equip": True},
            facts=facts, running=running, result=None if running else {"success": True})
        assert state["inventory"] == "wooden_pickaxe 1"
        assert state["body"].endswith("The main hand holds oak_sapling.")
        assert state["body"].count("main hand") == 1
    assert rq.idle_question(facts, rq.idle_options(facts))["state"]["body"].endswith(
        "The main hand holds oak_sapling.")


@pytest.mark.parametrize("facts,expected", [
    ({"selected_item": ""}, "The main hand holds nothing."),
    ({"selected_item": None, "selected_item_known": True}, ""),
    ({"selected_item": "oak_sapling", "selected_item_known": False}, ""),
    ({}, ""),
])
def test_unknown_main_hand_is_not_reported_as_empty(facts, expected):
    assert rq._body_sentence(facts) == expected


def test_vital_window_gives_values_from_a_few_seconds_ago():
    window = rq.VitalWindow()
    assert window.add(0.0, {"health": 20, "oxygen": 300}) == {}
    assert window.add(1.0, {"health": 20, "oxygen": 280}) == {"oxygen_before": 300.0, "oxygen_age_s": 1.0}
    before = window.add(5.0, {"health": 14, "oxygen": 200})
    assert before == {"health_before": 20.0, "oxygen_before": 300.0, "vital_age_s": 5.0, "oxygen_age_s": 5.0}
    assert window.add(30.0, {"health": 14}) == {}


def test_recorded_air_turnaround_uses_current_direction_and_its_own_age():
    recorded = json.loads((Path(__file__).parents[1] / "fixtures" / "reflex_step2_oxygen_direction.json").read_text())
    expected = {
        2197: (122, 1, 211, True), 2286: (122, 2, 295, True),
        2287: (295, 1, 291, False), 2288: (295, 2, 271, False),
        2289: (295, 3, 250, False), 2290: (295, 4, 229, False),
        2291: (295, 5, 208, False), 2292: (295, 6, 188, False),
    }
    window = rq.VitalWindow()
    seen = []
    for sample in recorded["samples"]:
        facts = {**sample, **window.add(sample["ts"], sample)}
        if sample["seq"] not in expected:
            continue
        baseline, age, air, rising = expected[sample["seq"]]
        prefix = "The Body is breathing again:" if rising else "The head is under water and the Body cannot breathe:"
        unit = "second" if age == 1 else "seconds"
        assert rq._body_sentence(facts) == (
            f"Health is 20 of 20. Hunger is 20 of 20. {prefix} air supply was "
            f"{baseline} {age} {unit} ago and is {air} of 300 now."
        )
        seen.append(sample["seq"])
    assert seen == recorded["targets"]


@pytest.mark.parametrize("samples,baseline,age", [
    ([(0, 300), (1, 270), (2, 240), (3, 270), (4, 290)], 240, 2),
    ([(0, 270), (1, 270), (2, 270), (3, 250), (4, 250)], 270, 4),
    ([(0, 270)], None, None),
])
def test_air_direction_with_recovery_plateaus_and_one_sample(samples, baseline, age):
    window = rq.VitalWindow()
    for stamp, air in samples:
        facts = window.add(stamp, {"oxygen": air})
    assert facts.get("oxygen_before") == baseline
    assert facts.get("oxygen_age_s") == age


def test_idle_and_work_use_the_same_protective_eligibility_and_inventory_words():
    facts = {"health": 5, "health_before": 10, "vital_age_s": 5,
             "oxygen": 77, "food": 15, "food_item": "apple", "inventory": {"apple": 2},
             "safe_standing_position": [1, 64, 1],
             "entities": [{"hostile": True, "name": "hostile mob", "dist": 30}]}
    idle = rq.idle_options(facts)
    work = rq.frame_options(tool="move_to", params={}, intent="reach home", facts=facts, running=True)
    assert set(idle) - {"stay"} == set(work) - {"keep_going", "next_step", "call_agent"}
    assert "move_away" in idle
    idle_state = rq.idle_question(facts, idle)["state"]
    assert idle_state["inventory"] == _running(facts)["inventory"] == "apple 2"


def test_running_options_are_concrete_and_protective_ones_follow_the_facts():
    calm = rq.frame_options(facts={"oxygen": 300}, intent="get 3 raw iron", running=True,
                            next_call='smelt_item(count=3, input_item="raw_iron")', **CALL)
    assert calm == {
        "keep_going": 'Keep doing the current work, collect_resource(count=3, item="raw_iron"), for: “get 3 raw iron”',
        "next_step": 'Stop the current action and start the next step of the plan: smelt_item(count=3, input_item="raw_iron").',
        "call_agent": "Stop the current action and hand control back to the Agent for a new plan.",
    }
    danger = rq.frame_options(
        facts={"oxygen": 77, "health": 3, "health_before": 6, "last_safe_position": [1, 64, 1],
               "entities": [{"hostile": True, "name": "Skeleton", "dist": 9, "pos": [5, 64, 5]}]},
        intent="", running=True, **CALL)
    assert danger["surface"] == "Stop the current action and swim up to air now."
    assert danger["move_away"] == "Stop the current action and move away from the Skeleton."
    assert danger["retreat_safe"].startswith("Stop the current action and go back")
    assert danger["next_step"] == "Stop the current action and report to the Agent that this work is finished."
    far = rq.frame_options(facts={"entities": [{"hostile": True, "name": "Skeleton", "dist": 34}]},
                           intent="", running=True, **CALL)
    assert "move_away" not in far and "surface" not in far and "eat" not in far


def test_result_options_never_skip_a_failed_step():
    ok = rq.frame_options(facts={}, intent="", running=False, result={"success": True}, **CALL)
    assert list(ok) == ["repeat", "next_step", "call_agent"]
    assert ok["repeat"] == 'Run the same call again: collect_resource(count=3, item="raw_iron").'
    assert ok["next_step"] == "Do not repeat it and report to the Agent that this work is finished."
    failed = rq.frame_options(facts={}, intent="", running=False, result={"success": False, "reason": "x"}, **CALL)
    assert list(failed) == ["repeat", "call_agent"]
    hopeless = rq.frame_options(facts={}, intent="", running=False,
                                result={"success": False, "reason": "x", "canRetry": False}, **CALL)
    assert list(hopeless) == ["call_agent"]
    assert rq.only_option(rq.frame_questions(hopeless)) == "call_agent"


def test_result_frame_reports_how_the_call_ended_and_earlier_attempts():
    body = {"position": [1, 64, 1], "inventory": {"stick": 2}}
    fail = {"success": False, "reason": "stand_point_unreachable"}
    history = [{"result": fail, "body": body}] * 3
    lines = rq.attempt_lines(history, earlier=35)
    assert lines == [
        "There were 35 earlier attempts before these.",
        "Attempt 36: failure: stand_point_unreachable; inventory and position unchanged.",
        "Attempt 37: failure: stand_point_unreachable; inventory and position unchanged.",
    ]
    assert rq.attempt_lines(history[:1]) == []
    state = rq.frame_state(goal=GOAL, intent="i", facts={}, running=False, result=fail, earlier_attempts=lines, **CALL)
    assert state["finished_call"] == (
        'collect_resource(count=3, item="raw_iron") has ended. It reported failure: stand_point_unreachable.'
    )
    assert state["earlier_attempts_of_this_call"] == lines and "current_work" not in state
    done = rq.frame_state(goal=GOAL, intent="i", facts={}, running=False, result={"success": True, "reason": "collected"}, **CALL)
    assert done["finished_call"] == 'collect_resource(count=3, item="raw_iron") has ended normally.'


def test_danger_is_asked_only_when_a_protective_action_is_on_offer():
    calm = rq.frame_questions({"keep_going": "k", "call_agent": "a"})
    assert list(calm) == ["q"] and calm["q"]["instructions"] == "Choose what the Body does next."
    wet = rq.frame_questions({"keep_going": "k", "surface": "s", "call_agent": "a"})
    assert list(wet["q"]["criteria"]) == ["keep_going", "call_agent"]
    assert list(wet["d"]["criteria"]) == ["safe", "in_danger"]
    assert list(wet["p"]["criteria"]) == ["surface", "call_agent"]

    def raw(**answers):
        return {"answers": {name: {"choice": choice} for name, choice in answers.items()}}

    assert rq.frame_choice(raw(q="keep_going", d="in_danger", p="surface"), wet) == "surface"
    assert rq.frame_choice(raw(q="keep_going", d="safe", p="surface"), wet) == "keep_going"
    assert rq.frame_choice(raw(q="keep_going"), calm) == "keep_going"
    assert rq.frame_choice(raw(q="made_up"), calm) == "unknown"
    assert rq.frame_choice(None, calm) == "unknown"


@pytest.mark.parametrize("danger_answer", [
    None, {}, {"choice": ""}, {"choice": "unknown"},
    {"choice": "not_a_danger_option"}, {"choice": ["safe"]},
    {"choice": {"safe": True}}, {"choice": 1},
])
def test_unknown_danger_cannot_authorize_normal_or_protective_action(danger_answer):
    questions = rq.frame_questions({
        "keep_going": "Continue the work.", "next_step": "Stop and start the next step.",
        "surface": "Swim up to air.", "call_agent": "Stop and call the Agent.",
    })
    answers = {"q": {"choice": "next_step"}, "p": {"choice": "surface"}}
    if danger_answer is not None:
        answers["d"] = danger_answer
    assert rq.frame_choice({"answers": answers}, questions) == "unknown"


def test_after_a_protective_action_the_body_may_go_back_to_its_work():
    options = rq.after_default_options(
        original_call='collect_resource(count=3, item="raw_iron")', intent="get 3 raw iron",
        default_call="go_to_surface()", result={"success": True},
    )
    assert options == {
        "resume": 'Go back to the earlier work, collect_resource(count=3, item="raw_iron"), for: “get 3 raw iron”',
        "repeat": "Run the same call again: go_to_surface().",
        "call_agent": "Do not go back and hand control back to the Agent for a new plan.",
    }
    stuck = rq.after_default_options(original_call="x()", intent="", default_call="go_to_surface()",
                                     result={"success": False, "reason": "blocked", "canRetry": False})
    assert list(stuck) == ["resume", "call_agent"]


def test_a_step_without_its_own_intent_is_said_to_be_one_step_of_the_plan():
    # Live run reflex-stage-20261012d: read as the step's own purpose, the
    # plan's "make a stone pickaxe" made gathering stop after four seconds.
    common = dict(tool="collect_resource", params={"item": "cobblestone", "count": 3}, facts={},
                  running=True, next_call='craft_item(item="stone_pickaxe")')
    own = rq.frame_options(intent="have 3 cobblestone", **common)
    inherited = rq.frame_options(intent="make and equip a stone pickaxe", intent_of_plan=True, **common)
    assert own["keep_going"] == (
        'Keep doing the current work, collect_resource(item="cobblestone", count=3), for: “have 3 cobblestone”')
    assert inherited["keep_going"] == (
        'Keep doing the current step, collect_resource(item="cobblestone", count=3). '
        'It is one step of the plan: “make and equip a stone pickaxe”')
    assert list(inherited) == list(own)


def test_a_dead_body_is_handed_to_the_agent_without_asking():
    # Live run reflex-stage-20261012f: with health 0 the question still
    # offered the surface action again, and Decision took it.
    facts = {"health": 0, "food": 17, "oxygen": 0}
    running = rq.frame_options(tool="collect_resource", params={"item": "raw_iron"}, intent="get iron",
                               facts=facts, running=True, next_call="craft_item()")
    after = rq.after_default_options(original_call="collect_resource()", intent="get iron",
                                     default_call="go_to_surface()", result={"success": False}, facts=facts)
    assert list(running) == list(after) == ["call_agent"]
    assert rq.only_option(rq.frame_questions(running)) == "call_agent"
    state = rq.frame_state(goal="g", intent="get iron", tool="collect_resource", params={}, facts=facts, running=True)
    assert state["body"] == "The Body has died."
    alive = rq.after_default_options(original_call="c()", intent="i", default_call="d()", result={"success": True},
                                     facts={"health": 3})
    assert "resume" in alive


def test_rising_air_is_said_as_breathing_again_not_as_under_water():
    # Live run reflex-stage-20261012i: after surfacing, with air climbing back,
    # the frame still said the head was under water.
    rising = rq.frame_state(goal="g", intent="i", tool="t", params={}, running=True,
                            facts={"health": 20, "food": 20, "oxygen": 237, "oxygen_before": 150, "vital_age_s": 5})
    assert rising["body"].endswith("The Body is breathing again: air supply was 150 5 seconds ago and is 237 of 300 now.")
    falling = rq.frame_state(goal="g", intent="i", tool="t", params={}, running=True,
                             facts={"health": 20, "food": 20, "oxygen": 150, "oxygen_before": 237, "vital_age_s": 5})
    assert "The head is under water and the Body cannot breathe" in falling["body"]
