"""The frame question wording, checked against the sentences settled offline."""
from types import SimpleNamespace

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


@pytest.mark.parametrize("food", [0, 6])
def test_low_food_reports_that_the_body_cannot_sprint(food):
    sentence = _running({"food": food})["body"]
    assert sentence == f"Hunger is {food} of 20. Food is {food}; the Body cannot sprint."


@pytest.mark.parametrize("facts", [{"health": 20}, {"health": 20, "food": 7}, {"health": 20, "food": 20}])
def test_cannot_sprint_fact_is_absent_when_food_is_unknown_or_above_six(facts):
    assert "cannot sprint" not in _running(facts)["body"]


def test_equipment_intent_sees_current_main_hand_once_in_all_frames():
    # Live seq1825: wooden pickaxe was in the bag, but the hand still held a sapling.
    facts = {"inventory_counts": {"wooden_pickaxe": 1}, "selected_item": "oak_sapling",
             "health": 20, "food": 20, "oxygen": 300}
    for running in (True, False):
        state = rq.frame_state(goal="make a pickaxe", intent="craft and equip a wooden pickaxe",
            tool="craft_item", params={"item": "wooden_pickaxe", "auto_equip": True},
            facts=facts, running=running, result=None if running else {"success": True})
        # Said beside the inventory: inside the body sentence it was missed
        # when an intent named both the item and holding it (live seq1978).
        assert list(state)[list(state).index("inventory") + 1] == "main_hand"
        assert state["main_hand"] == "oak_sapling"
        assert "main hand" not in state["body"]
    idle = rq.idle_question(facts, rq.idle_options(facts))["state"]
    assert idle["main_hand"] == "oak_sapling"
    assert list(idle)[list(idle).index("inventory") + 1] == "main_hand"


@pytest.mark.parametrize("facts,expected", [
    ({"selected_item": ""}, "nothing"),
    ({"selected_item": None, "selected_item_known": True}, None),
    ({"selected_item": "oak_sapling", "selected_item_known": False}, None),
    ({}, None),
])
def test_unknown_main_hand_is_not_reported_as_empty(facts, expected):
    assert rq._main_hand(facts) == expected
    assert "main hand" not in rq._body_sentence(facts)


def test_vital_window_gives_values_from_a_few_seconds_ago():
    window = rq.VitalWindow()
    assert window.add(0.0, {"health": 20, "oxygen": 300}) == {}
    assert window.add(1.0, {"health": 20, "oxygen": 280}) == {"oxygen_before": 300.0, "oxygen_age_s": 1.0}
    before = window.add(5.0, {"health": 14, "oxygen": 200})
    assert before == {"health_before": 20.0, "oxygen_before": 300.0, "vital_age_s": 5.0, "oxygen_age_s": 5.0}
    assert window.add(30.0, {"health": 14}) == {}


def _zombie(dist, mob_id="z1"):
    return {"entities": [{"hostile": True, "id": mob_id, "name": "Zombie", "dist": dist}]}


def test_approaching_hostile_is_said_as_a_change():
    # Villa run 2026-10-15: "Zombie, 11 blocks away" as a still distance was
    # judged safe all the way in to 3 blocks; said as coming closer, the
    # recorded frames were in danger 72/72.
    window = rq.VitalWindow()
    window.add(0.0, {"health": 20, **_zombie(11.2)})
    facts = {"health": 20, **_zombie(6.1)}
    facts.update(window.add(3.0, facts))
    assert rq._threat_sentence(facts) == (
        "Nearest hostile mob: Zombie, 6 blocks away and coming closer: it was 11 blocks away 3 seconds ago.")


@pytest.mark.parametrize("before,now,mob_id", [(6.0, 6.3, "z1"), (6.0, 10.0, "z1"), (11.0, 6.0, "other")])
def test_still_retreating_or_other_hostile_keeps_the_plain_sentence(before, now, mob_id):
    # "moving away" was not added: a just-hurt, knocked-back frame went from
    # in danger to safe with it.
    window = rq.VitalWindow()
    window.add(0.0, {"health": 20, **_zombie(before)})
    facts = {"health": 20, **_zombie(now, mob_id)}
    facts.update(window.add(4.0, facts))
    assert rq._threat_sentence(facts) == f"Nearest hostile mob: Zombie, {now:.0f} blocks away."


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
             "entities": [{"hostile": True, "name": "hostile mob", "dist": 8}]}
    idle = rq.idle_options(facts)
    work = rq.frame_options(tool="move_to", params={}, intent="reach home", facts=facts, running=True)
    assert set(idle) - {"stay"} == set(work) - {"keep_going", "next_step", "call_agent"}
    assert "move_away" in idle
    idle_state = rq.idle_question(facts, idle)["state"]
    assert idle_state["inventory"] == _running(facts)["inventory"] == "apple 2"


def test_end_bed_option_says_explosion_and_requires_owned_bed_in_reach():
    facts = {
        "dimension": "minecraft:the_end",
        "position": [0.5, 64.0, 0.5],
        "entities": [{"hostile": True, "type": "ender_dragon", "name": "Ender Dragon",
                      "dist": 5.0, "health": 200, "head_pos": [1.5, 65.5, 2.5], "phase_id": 6}],
        "own_beds": [{"pos": [1, 64, 0], "block": "minecraft:white_bed"}],
    }
    options = rq.protective_options(facts)
    assert options["use_bed_1_64_0"] == (
        "Use the bed at [1, 64, 0] now. Using a bed in the End explodes it."
    )
    assert "The dragon's head is 2.2 blocks from the bed at [1, 64, 0]." in rq._body_sentence(facts)
    assert "use_bed_1_64_0" not in rq.protective_options({**facts, "own_beds": []})
    assert "use_bed_1_64_0" not in rq.protective_options({**facts, "own_beds": [], "placed_beds": [[1, 64, 0]]})
    assert "use_bed_1_64_0" not in rq.protective_options({**facts, "position": [20.5, 64.0, 0.5]})
    assert not any(key.startswith("use_bed_") for key in rq.protective_options({**facts, "entities": []}))


def test_running_options_are_concrete_and_protective_ones_follow_the_facts():
    calm = rq.frame_options(facts={"oxygen": 300}, intent="get 3 raw iron", running=True,
                            next_call='smelt_item(count=3, input_item="raw_iron")', **CALL)
    # With an intent, "next step" is not an option beside "keep going":
    # whether the facts show the intent is its own question.
    assert calm == {
        "keep_going": 'Keep doing the current work, collect_resource(count=3, item="raw_iron"), for: “get 3 raw iron”',
        "call_agent": "Stop the current action and hand control back to the Agent because the plan itself"
                      " cannot work as it is; the Agent makes a new plan.",
    }
    danger = rq.frame_options(
        facts={"oxygen": 77, "health": 3, "health_before": 6,
               "entities": [{"hostile": True, "name": "Skeleton", "dist": 9, "pos": [5, 64, 5]}]},
        intent="", running=True, **CALL)
    assert danger["surface"] == "Stop the current action and swim up to air now."
    assert danger["move_away"] == ("Stop the current action and run from the Skeleton (9 blocks away)"
                                   " until it is 23 blocks away.")
    assert "retreat_safe" not in danger  # leaving the danger is the safe move (end run-3)
    assert danger["next_step"] == "Stop the current action and report to the Agent that this work is finished."
    far = rq.frame_options(facts={"entities": [{"hostile": True, "name": "Skeleton", "dist": 34}]},
                           intent="", running=True, **CALL)
    # Already farther than the run would take it: nothing to run from
    # (body-tool-boundaries-20261110; run-13: 81 of 95 dodges from 16+ blocks moved nothing).
    assert "move_away" not in far
    assert "surface" not in far and "eat" not in far


@pytest.mark.parametrize("distance", [4, 40])
def test_fight_options_offer_one_visible_target_at_any_distance(distance):
    facts = {"health": 20, "health_before": 20,
             "entities": [{"id": "dragon", "name": "Ender Dragon", "boss": True, "dist": distance}],
             "inventory": {"iron_sword": 1, "bow": 1, "arrow": 32},
             "selected_item": "iron_sword"}
    assert rq.protective_options(facts) == {
        "engage": f"Fight back against the Ender Dragon ({distance} blocks away) in melee with the iron_sword in the main hand.",
        "ranged": f"Shoot the Ender Dragon ({distance} blocks away) with the bow from the inventory. Arrows left: 32.",
        "move_away": f"Move away from the Ender Dragon ({distance} blocks away).",
    }


def test_far_calm_hostile_adds_danger_and_protection_to_an_idle_frame():
    facts = {"health": 20, "health_before": 20, "food": 20, "oxygen": 300,
             "entities": [{"id": "dragon", "name": "Ender Dragon", "boss": True, "dist": 40}],
             "inventory": {"bow": 1, "arrow": 32}}
    request = rq.idle_question(facts, rq.idle_options(facts))
    assert request["state"]["body"] == "Health is 20 of 20. Hunger is 20 of 20. Air supply is full."
    assert request["state"]["threats"] == "Nearest hostile mob: Ender Dragon, 40 blocks away."
    questions = request["questions"]
    assert list(questions) == ["q", "d", "p"]
    assert questions["q"]["criteria"] == {"stay": "Stay where the Body is and wait for the next step."}
    assert questions["d"]["instructions"] == "Is the Body in danger?"
    assert set(questions["p"]["criteria"]) == {"engage", "ranged", "move_away"}
    # Deciding that the distant mob is safe still chooses the idle action.
    assert rq.frame_choice({"answers": {"q": {"choice": "stay"}, "d": {"choice": "safe"},
                                       "p": {"choice": "ranged"}}}, questions) == "stay"
    empty = {**facts, "entities": []}
    assert list(rq.idle_question(empty, rq.idle_options(empty))["questions"]) == ["q"]


def test_fight_options_keep_the_hurt_target_and_one_option_set_among_many_hostiles():
    facts = {"health": 18, "health_before": 20,
             "hurt_source": {"id": "dragon", "name": "Ender Dragon", "distance": 40, "seconds_since_hurt": 1},
             "entities": [{"id": f"enderman-{i}", "name": "Enderman", "hostile": True, "dist": i + 2}
                          for i in range(8)] + [{"id": "dragon", "name": "Ender Dragon", "boss": True, "dist": 40}],
             "inventory": {"bow": 1, "arrow": 32}}
    options = rq.protective_options(facts)
    assert set(options) == {"engage", "ranged", "move_away"}
    assert all("Ender Dragon (40 blocks away)" in text for text in options.values())
    assert options["engage"].endswith("The Ender Dragon at 40 blocks hit the Body 1 second ago.")


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


def test_result_report_is_a_concrete_thing_to_do_not_a_verdict():
    # Agreed rule (frame-question doc section 1): no "has been achieved"
    # options. The last step reports that the work is finished.
    args = dict(facts={}, intent="到达[220,121,220]", running=False, result={"success": True}, **CALL)
    terminal = rq.frame_options(**args)
    assert terminal["next_step"] == "Do not repeat it and report to the Agent that this work is finished."
    internal = rq.frame_options(next_call="equip_item(item=stone_sword)", **args)
    assert internal["next_step"] == "Do not repeat it and start the next step of the plan: equip_item(item=stone_sword)."
    assert set(terminal) == set(internal)


@pytest.mark.parametrize("next_call", ["", "equip_item(item=stone_sword)"])
def test_running_work_does_not_offer_next_step_beside_keep_going(next_call):
    # Live seq499/1978/3913: asked beside "keep going", the next step was
    # chosen at 3 of 5 logs, 3 of 12 coal and an unheld pickaxe.
    options = rq.frame_options(facts={}, intent="x", running=True, next_call=next_call, **CALL)
    assert "next_step" not in options
    assert list(rq.frame_questions(options, acting_for_intent=True)) == ["q"]


def test_intent_question_carries_only_the_facts_it_can_be_checked_against():
    # Probes on live frames: the progress line ("inventory changed: oak_log
    # +3"), the task and the call text made the 4B scorer answer "shown" at
    # 3 of 4 logs; with only these facts it compared the counts correctly.
    state = {"task": "t", "agent_intent": "i", "inventory": "oak_log 3", "main_hand": "oak_log",
             "body": "b", "threats": "x", "current_work": "collect_resource(count=4) is running.",
             "progress": "inventory changed: oak_log +3.", "position": "now at [1.0, 2.0, 3.0]."}
    intent = "at least 4 oak_log in the inventory"
    asked = rq.done_question(state, intent)
    assert asked["state"] == {
        "agent_intent": f"The Agent wants the following result: “{intent}”.",
        "inventory": "oak_log 3", "main_hand": "oak_log", "position": "now at [1.0, 2.0, 3.0].",
    }
    assert list(asked["questions"]) == ["a"]
    assert set(asked["questions"]["a"]["criteria"]) == {"shown", "not_shown"}
    assert rq.done_question({"inventory": "empty"}, intent)["state"] == {
        "agent_intent": f"The Agent wants the following result: “{intent}”.", "inventory": "empty"}


def test_intent_answer_and_danger_branch_are_read_strictly():
    assert rq.done_choice({"answers": {"a": {"choice": "shown"}}}) == "shown"
    assert rq.done_choice({"answers": {"a": {"choice": "not_shown"}}}) == "not_shown"
    assert rq.done_choice({"answers": {"a": {"choice": "maybe"}}}) is None
    assert rq.done_choice({"answers": {}}) is None and rq.done_choice(None) is None
    questions = rq.frame_questions({"keep_going": "k", "call_agent": "c", "surface": "s"})
    assert rq.in_danger({"answers": {"d": {"choice": "in_danger"}}}, questions)
    assert not rq.in_danger({"answers": {"d": {"choice": "safe"}}}, questions)
    assert not rq.in_danger({"answers": {"d": {"choice": "in_danger"}}}, {"q": {}})


def test_failure_observations_survive_the_agent_projector_without_new_result_fields():
    from minebot.app.runner import _model_tool_payload
    native = {"success": False, "reason": "target_not_found", "canRetry": True,
              "metrics": {"requested_target": "absent-uuid", "scope": "nearbyEntities",
                          "search_radius": 32, "entities_observed": 0}}
    projected = _model_tool_payload("engage_entity", native, trace_ref="regression")
    expected = "engage_entity() has ended. It reported failure: target_not_found. The tool observed 0 entities within 32 blocks."
    assert rq.result_sentence("engage_entity()", native) == expected
    assert rq.result_sentence("engage_entity()", projected) == expected
    assert native["canRetry"] is True


@pytest.mark.parametrize("details", [{}, {"search_radius": 32}, {"entities_observed": 0},
                                     {"entities_observed": False, "search_radius": 32}])
def test_failure_does_not_invent_missing_observation_facts(details):
    assert rq.result_sentence("call()", {"success": False, "reason": "x", "metrics": details}) == (
        "call() has ended. It reported failure: x.")


def test_success_still_only_says_ended_normally_even_with_observation_metrics():
    assert rq.result_sentence("call()", {"success": True, "metrics": {
        "entities_observed": 1, "search_radius": 32}}) == "call() has ended normally."


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


def test_hand_back_logic_is_said_where_the_agent_can_be_handed_back_to():
    running = rq.frame_questions({"keep_going": "k", "call_agent": "a"}, acting_for_intent=True, handback="running")
    assert running["q"]["instructions"].startswith("Choose what the Body does right now to achieve the Agent's intent. While the work runs, do not hand it back")
    assert "Hand control back to the Agent only when the facts show the Agent's plan itself is wrong" in running["q"]["instructions"]
    result = rq.frame_questions({"repeat": "r", "call_agent": "a"}, handback="result")
    assert result["q"]["instructions"].startswith("Choose what the Body does next. Read the result against the Agent's intent.")
    # No hand-back offered, a protection frame, or no handback asked: unchanged.
    assert rq.frame_questions({"repeat": "r"}, handback="result")["q"]["instructions"] == "Choose what the Body does next."
    assert rq.frame_questions({"move_away": "m", "call_agent": "a"}, protecting=True, handback="running")["q"]["instructions"] == "Choose what the Body does next."
    assert rq.frame_questions({"repeat": "r", "call_agent": "a"})["q"]["instructions"] == "Choose what the Body does next."


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


def test_dead_entities_do_not_remain_actionable_hostiles_in_facts_or_options():
    facts={'health':20,'food':20,'oxygen':300,'entities':[
        {'id':'dead','name':'Former hostile','hostile':True,'health':0,'dist2':1},
        {'id':'alive','name':'Live hostile','hostile':True,'health':10,'dist2':4}]}
    assert [entry['id'] for entry in rq.hostiles(facts)]==['alive']
    assert 'Live hostile' in rq._threat_sentence(facts)
    assert 'Former hostile' not in rq._threat_sentence(facts)
    facts['entities']=facts['entities'][:1]
    assert rq._threat_sentence(facts)=='No hostile mob is in view.'
    assert not {'engage','ranged','move_away'} & rq.protective_options(facts).keys()
    # Missing health does not prove an entity dead.
    facts['entities'][0].pop('health')
    assert rq.hostiles(facts)[0]['id']=='dead'


def test_after_a_protective_action_the_body_may_go_back_to_its_work():
    common = dict(original_call='collect_resource(count=3, item="raw_iron")', intent="get 3 raw iron",
                  default_call="go_to_surface()", result={"success": True}, protective_choice="surface")
    # The action that just ended is named beside the other protective actions,
    # not "Run the same call again" (step5-threat-claude4: repeated 12 times),
    # and only while the facts still allow it (step5-threat-claude9).
    assert rq.after_default_options(facts={"oxygen": 120}, **common) == {
        "resume": 'Go back to the earlier work, collect_resource(count=3, item="raw_iron"), for: “get 3 raw iron”',
        "surface": "Swim up to air now.",
        "call_agent": "Do not go back and hand control back to the Agent for a new plan.",
    }
    assert list(rq.after_default_options(facts={"oxygen": 300}, **common)) == ["resume", "call_agent"]
    shot = rq.after_default_options(original_call="", intent="", default_call="engage_entity()",
        result={"success": False, "reason": "disengaged_low_health"}, protective_choice="engage",
        facts={"health": 6, "health_before": 20, "vital_age_s": 3, "selected_item": "iron_sword",
               "entities": [{"hostile": True, "name": "Skeleton", "dist": 8, "id": "s", "pos": [8, 64, 0]}]})
    assert list(shot) == ["stay", "engage", "move_away", "call_agent"]
    assert shot["stay"] == rq.IDLE_AGAIN


def test_a_breathing_body_is_not_offered_to_swim_up():
    # Replay of track S 2026-10-20: right after go_to_surface ended normally,
    # with air climbing back, swimming up again was chosen 117 of 240 times.
    common = dict(original_call="", intent="", default_call="go_to_surface()", result={"success": True},
                  protective_choice="surface")
    rising = {"oxygen": 288, "oxygen_before": 261, "oxygen_age_s": 0}
    assert "surface" not in rq.after_default_options(facts=rising, **common)
    assert "surface" not in rq.protective_options({"oxygen": 200, "head_in_liquid": False})
    # Still under water, or air falling: swimming up stays on offer.
    assert "surface" in rq.protective_options({**rising, "head_in_liquid": True})
    assert "surface" in rq.protective_options({"oxygen": 150, "oxygen_before": 237})
    assert "surface" in rq.protective_options({"oxygen": 150})


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


def test_a_dead_body_has_no_decision_options():
    # Live run reflex-stage-20261012f: with health 0 the question still
    # offered the surface action again, and Decision took it.
    facts = {"health": 0, "food": 17, "oxygen": 0}
    running = rq.frame_options(tool="collect_resource", params={"item": "raw_iron"}, intent="get iron",
                               facts=facts, running=True, next_call="craft_item()")
    after = rq.after_default_options(original_call="collect_resource()", intent="get iron",
                                     default_call="go_to_surface()", result={"success": False}, facts=facts)
    assert running == after == rq.idle_options(facts) == {}
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


BEFORE = {"position": [171.3, 64.0, 165.4], "inventory": {"cobblestone": 20}, "selected_item": "stone_pickaxe"}


def _repeat_offered(result, *, unchanged):
    return "repeat" in rq.frame_options(tool="go_to_surface", params={}, intent="standing at y 70 or higher",
                                        facts={}, running=False, result=result, unchanged=unchanged)


def test_a_failure_that_left_the_body_as_it_was_is_not_offered_again():
    # life-stage b2: go_to_surface failed 873 times from the same spot.
    failed = {"success": False, "reason": "surface_not_found", "canRetry": True}
    assert not _repeat_offered(failed, unchanged=True)
    assert _repeat_offered(failed, unchanged=False)


def test_a_success_that_left_the_body_as_it_was_is_not_offered_again():
    # house run 2026-10-18: move_to onto the cell the Body already stood in
    # ended normally and was chosen again 94 times in a row.
    arrived = {"success": True, "reason": "arrived"}
    assert not _repeat_offered(arrived, unchanged=True)
    assert _repeat_offered(arrived, unchanged=False)


def test_a_call_stopped_from_outside_may_still_be_run_again():
    for reason in ("cancel_requested", "preempted", "stopped_by_decision"):
        assert _repeat_offered({"success": False, "reason": reason, "canRetry": True}, unchanged=True)
    handed = {"success": False, "reason": "no_path", "metrics": {"direct_call_handoff": {"facts": {}}}}
    assert _repeat_offered(handed, unchanged=True)


def test_unchanged_needs_the_same_block_inventory_and_hand():
    same = dict(BEFORE, position=[171.8, 64.2, 165.9])
    assert rq.body_unchanged_by_call(same, BEFORE)
    assert not rq.body_unchanged_by_call(dict(BEFORE, position=[172.1, 64.0, 165.4]), BEFORE)
    assert not rq.body_unchanged_by_call(dict(BEFORE, inventory={"cobblestone": 21}), BEFORE)
    assert not rq.body_unchanged_by_call(dict(BEFORE, selected_item="oak_planks"), BEFORE)
    # Missing evidence is never read as "unchanged".
    assert not rq.body_unchanged_by_call({"position": [1, 64, 1]}, {"position": [1, 64, 1]})
    assert not rq.body_unchanged_by_call({})


def test_unchanged_from_the_calls_own_execution_facts():
    facts = {"position": [10.4, 64, 3.2], "start_position": [10.1, 64, 3.9], "inventory_delta": {}}
    assert rq.body_unchanged_by_call(facts)
    assert not rq.body_unchanged_by_call(dict(facts, inventory_delta={"dirt": 1}))
    assert not rq.body_unchanged_by_call({"position": [10, 64, 3], "start_position": [10, 64, 3]})


def test_a_task_decision_cannot_read_is_left_out_of_the_frame():
    # Live check 2026-10-18: with the user's Chinese goal as the task line a
    # place_block 0.7 s in was handed back (call_agent 0.75; 0.15 without it).
    chinese = rq.frame_state(goal="在小屋南侧建成石头仓库", intent="dirt placed at [37,63,651]",
                             tool="place_block", params={}, facts={}, running=True)
    assert "task" not in chinese and chinese["agent_intent"]
    english = rq.frame_state(goal="build a stone storehouse", intent="dirt placed at [37,63,651]",
                             tool="place_block", params={}, facts={}, running=True)
    assert english["task"] == "build a stone storehouse" and list(english)[0] == "task"


def test_a_failed_plan_step_may_be_reported_while_the_plan_goes_on():
    failed = {"success": False, "reason": "place_denied:target_occupied", "canRetry": False}
    options = rq.frame_options(tool="place_block", params={}, intent="glass at the window", facts={},
                               running=False, result=failed, next_call="move_to(pos=[39, 64, 644])")
    assert "move_to(pos=[39, 64, 644])" in options["report_and_go_on"]
    assert list(options)[-2:] == ["report_and_go_on", "call_agent"]
    # Nothing to go on to, or nothing failed: not offered.
    assert "report_and_go_on" not in rq.frame_options(tool="place_block", params={}, intent="i", facts={},
                                                      running=False, result=failed)
    assert "report_and_go_on" not in rq.frame_options(tool="place_block", params={}, intent="i", facts={},
                                                      running=False, result={"success": True},
                                                      next_call="move_to(pos=[1, 2, 3])")


def test_in_lava_the_frame_says_so_and_offers_the_way_out():
    """eval-20261020-m1 run-2: every frame of a Body burning in lava said only
    that health fell and offered only staying; it died in 4 s."""
    burning = {"health": 14, "health_before": 20, "food": 20, "oxygen": 300, "in_lava": True, "on_fire": True,
               "lava_exit": [-15, -55, 25], "position": [-16.6, -55.0, 25.4],
               "inventory": {"water_bucket": 1}}
    assert "The Body is in lava and is burning." in rq.idle_question(burning, {})["state"]["body"]
    options = rq.idle_options(burning)
    assert options["leave_lava"] == "Get out of the lava now, onto the dry block at [-15, -55, 25]."
    assert "put_out_fire" not in options  # poured in lava it puts out nothing
    assert list(rq.frame_questions(options)["p"]["criteria"])[0] == "leave_lava"
    running = rq.frame_options(facts=burning, intent="", running=True, **CALL)
    assert running["leave_lava"].startswith("Stop the current action and get out of the lava now")
    assert "leave_lava" not in rq.idle_options({**burning, "lava_exit": None})

    out = {**burning, "in_lava": False, "lava_exit": None, "standing_on": [-16, -56, 24]}
    options = rq.idle_options(out)
    assert "leave_lava" not in options
    assert options["put_out_fire"] == "Pour the water bucket at the Body's feet to put out the fire."
    assert "The Body is on fire." in rq.idle_question(out, {})["state"]["body"]
    assert "put_out_fire" not in rq.idle_options({**out, "dimension": "minecraft:the_nether"})
    assert "put_out_fire" not in rq.idle_options({**out, "inventory": {"bucket": 1}})
    assert "put_out_fire" not in rq.idle_options({**out, "standing_on": None})  # in the air


def test_cloud_frame_names_dragon_breath_or_unknown_potion_and_offers_exit_only_inside():
    dragon = {"health": 20, "food": 20, "oxygen": 300,
              "cloud": {"pos": [1.0, 64.0, 2.0], "radius": 3.0, "particle": "dragon_breath", "inside": True}}
    sentence = rq.idle_question(dragon, {})["state"]["body"]
    # body-preemption-20261106.md §8.1: dragon's breath is said with its cost.
    assert ("The Body is standing in a cloud of dragon's breath (radius 3.00): a Body inside loses health "
            "every second, several points each time.") in sentence
    # Leaving the cloud is one of the steps; without the Body's steps fact none is offered.
    assert not any(key.startswith("step_") for key in rq.idle_options(dragon))
    potion = {**dragon, "cloud": {**dragon["cloud"], "particle": "entity_effect"}}
    assert "The Body is standing in a cloud (radius 3.00)." in rq.idle_question(potion, {})["state"]["body"]
    assert "leave_cloud" not in rq.idle_options(dragon)


def _protect_while_hurt(window, choices, hurts, step=10.0):
    health, facts = 20.0, {}
    for index, (choice, hurt) in enumerate(zip(choices, hurts)):
        start = index * step
        window.note_protection(start, choice)
        window.add(start + 1, {"health": health})
        if hurt:
            health -= 3
        facts = {"health": health}
        facts.update(window.add(start + 2, facts))
    return facts


def test_a_protection_repeated_while_hurt_is_said_as_a_fact():
    # eval-20261020-m2 run-1: each move away ended normally while magma cubes
    # kept reaching the Body; every frame alone looked like the first one.
    facts = _protect_while_hurt(rq.VitalWindow(), ["move_away"] * 3, [True] * 3)
    assert facts["protection_repeat"] == {"choice": "move_away", "count": 3, "hurt": 3, "failed": 0, "age_s": 22.0}
    assert rq._body_sentence(facts).endswith(
        "Moving away has not helped: the Body has moved away 3 times in the last 22 seconds "
        "and was hurt again during 3 of them.")


@pytest.mark.parametrize("choices,hurts", [
    (["move_away"] * 3, [True, False, False]),
    (["move_away", "engage", "move_away"], [True] * 3),
    (["move_away"] * 2, [True] * 2),
])
def test_no_repeat_fact_without_the_same_action_hurt_again(choices, hurts):
    facts = _protect_while_hurt(rq.VitalWindow(), choices, hurts)
    assert "protection_repeat" not in facts
    assert "not helped" not in rq._body_sentence(facts)


def test_a_repeat_older_than_the_window_is_forgotten():
    window = rq.VitalWindow()
    _protect_while_hurt(window, ["move_away"] * 3, [True] * 3)
    assert "protection_repeat" not in window.add(20.0 + rq.REPEAT_WINDOW_S + 1, {"health": 11})


def test_a_move_away_failure_names_the_mob_that_kept_up():
    sentence = rq.result_sentence("move_away(...)", {"success": False, "reason": "hostile_still_near",
                                                     "metrics": {"nearest_hostile": {"name": "Zombie", "dist": 2.6}}})
    assert sentence == ("move_away(...) has ended. It reported failure: hostile_still_near. "
                        "The nearest hostile mob, Zombie, is 3 blocks away.")


def test_engage_names_the_weapon_the_body_will_draw():
    facts = {"hostiles": [{"id": "m1", "name": "Magma Cube", "dist": 2.0, "hostile": True}],
             "selected_item": "cobblestone", "selected_item_known": True,
             "inventory": {"cobblestone": 11, "stone_sword": 1, "iron_sword": 1, "diamond_pickaxe": 1}}
    assert rq.protective_options(facts)["engage"] == (
        "Fight back against the Magma Cube (2 blocks away) in melee with the iron_sword from the inventory.")
    facts["selected_item"] = "iron_sword"
    assert rq.protective_options(facts)["engage"] == (
        "Fight back against the Magma Cube (2 blocks away) in melee with the iron_sword in the main hand.")


def test_a_mob_behind_blocks_is_said_out_of_sight_in_the_options():
    """Easy run-12: sealed in a pit, Decision engaged a skeleton 3 blocks away behind
    the pit wall and the Body dug itself out bare-handed."""
    mob = {"id": "s1", "name": "Skeleton", "type": "skeleton", "dist": 3.0, "hostile": True, "in_sight": False,
           "velocity": [0.0, 0.0, 1.0], "approaching": True, "health": 20, "max_health": 20}
    facts = {"hostiles": [mob], "selected_item": "dirt", "selected_item_known": True, "inventory": {"dirt": 1}}
    options = rq.protective_options(facts)
    assert options["engage"] == (
        "Fight back against the Skeleton (3 blocks away) in melee with the dirt in the main hand. The Skeleton is "
        "out of the Body's sight behind blocks: to reach it, the Body digs through them or goes around.")
    assert "The Skeleton is out of the Body's sight behind blocks." in options["move_away"]
    assert "out of the Body's sight: blocks stand between them" in rq.seen_sentence(facts)
    assert rq.after_default_options(original_call="", intent="", default_call="shelter_in_place", result={},
                                    facts=facts)["stay"].endswith(" Blocks stand between the Body and the Skeleton here.")
    mob["in_sight"] = True
    assert "Blocks stand" not in rq.after_default_options(original_call="", intent="", default_call="x", result={},
                                                          facts=facts)["stay"]
    options = rq.protective_options(facts)
    assert "sight" not in options["engage"] and "sight" not in options["move_away"]
    assert "sight" not in rq.seen_sentence(facts)


def test_engage_says_bare_hands_when_nothing_is_held():
    facts = {"hostiles": [{"id": "z1", "name": "Zombie", "dist": 2.0, "hostile": True}],
             "selected_item": None, "selected_item_known": True, "inventory": {}}
    assert rq.protective_options(facts)["engage"] == "Fight back against the Zombie (2 blocks away) in melee with bare hands."


def test_daylight_sentence_reads_the_overworld_clock():
    assert rq.daylight_sentence(6000) == "It is day."
    assert rq.daylight_sentence(12500) == "It is dusk; night falls soon."
    assert rq.daylight_sentence(24000 * 3 + 18000) == "It is night."
    assert rq.daylight_sentence(None) is None


def _one_mob(kind="magma_cube", name="Magma Cube", **extra):
    facts = {"health": 20.0, "hostiles": [{"id": "m1", "name": name, "type": kind, "dist": 4.0, "hostile": True}],
             "inventory": {"iron_sword": 1, "diamond_pickaxe": 1}}
    facts.update(extra)
    return facts


def test_moving_away_from_a_fast_mob_says_it_keeps_up():
    # The speed fact stays, said as what moving away does; the program's
    # verdict "The Body can deal with it" is gone (§6.y.22).
    assert rq.protective_options(_one_mob())["move_away"] == (
        "Run from the Magma Cube (4 blocks away) until it is 12 blocks away."
        " A Magma Cube is faster than the Body; running does not lose it.")
    assert rq.protective_options(_one_mob("zombie", "Zombie"))["move_away"] == (
        "Run from the Zombie (4 blocks away) until it is 12 blocks away.")
    assert not hasattr(rq, "fight_sentence") and not hasattr(rq, "MELEE_FOES")


def _plan_facts(thinking=False):
    projects = [{"id": "food", "status": "active", "work": {"kind": "sequence"}, "intent": "6 cooked beef.",
                 "when": "a furnace is in the inventory"},
                {"id": "bed", "status": "active", "work": {"kind": "sequence"}, "intent": "1 white_bed"},
                {"id": "village", "status": "paused", "work": {"kind": "sequence"}, "intent": "a village"}]
    return {"health": 20, "food": 20, "position": [1, 64, 2], "inventory": {"oak_log": 3},
            "agent_thinking": thinking, "life_profile": {"projects": projects}}


def test_idle_question_says_the_offered_ladder_items_are_the_agents_plan_in_order():
    facts = _plan_facts()
    options = {"project_food": "f", "project_bed": "b", "stay": "s"}
    state = rq.idle_question(facts, options)["state"]
    assert state["agent_plan"] == (
        "The Agent's plan for the Body, in its order: 1) “food”: 6 cooked beef (it fits when a furnace is in "
        "the inventory); 2) “bed”: 1 white_bed. The Body works on the first item whose condition holds now; "
        "an item whose condition does not hold waits.")
    assert state["agent"] == "The Agent is not directing the Body right now; the Body acts on its own."


def test_idle_question_without_ladder_options_has_no_plan_and_says_the_agent_is_thinking():
    state = rq.idle_question(_plan_facts(thinking=True), {"stay": "s"})["state"]
    assert "agent_plan" not in state
    assert state["agent"] == "The Agent is thinking about its next step and does not need the Body to wait for it."


def test_a_protection_that_keeps_failing_unhurt_is_said_as_a_fact():
    # H6 14:00: go_to_surface failed 33 times in 10 minutes with no route up,
    # health full, and was chosen again each time.
    window = rq.VitalWindow()
    for second in (0.0, 7.0, 14.0):
        window.note_protection(second, "surface")
        window.note_protection_result("surface", False)
    facts = {"health": 20, "food": 20, **window.add(15.0, {"health": 20})}
    assert facts["protection_repeat"]["failed"] == 3 and facts["protection_repeat"]["hurt"] == 0
    assert rq._body_sentence(facts).endswith(
        "Swimming up has not helped: the Body has swum up 3 times in the last 15 seconds "
        "and 3 of them reported failure.")


def test_a_protection_that_succeeds_unhurt_is_no_repeat_fact():
    window = rq.VitalWindow()
    for second in (0.0, 7.0, 14.0):
        window.note_protection(second, "surface")
        window.note_protection_result("surface", True)
    assert "protection_repeat" not in window.add(15.0, {"health": 20})


def test_an_exposed_ore_is_offered_when_a_pickaxe_can_mine_it_and_the_plan_is_there():
    profile = {"projects": [
        {"id": "wayside", "status": "active", "intent": "mine coal and iron ore passed on the way",
         "when": "a coal or iron ore is open to the air within a few blocks"},
        {"id": "home", "status": "paused", "intent": "build a hut"}]}
    ores = [{"block": "deepslate_iron_ore", "pos": [4, 10, 5], "dist": 3.2},
            {"block": "coal_ore", "pos": [6, 10, 5], "dist": 5.0}]
    facts = {"life_profile": profile, "exposed_ores": ores, "inventory": {"wooden_pickaxe": 1}}
    # A wooden pickaxe drops nothing from iron ore: the next ore it can mine.
    assert rq.wayside_ore(facts) == {"block": "coal_ore", "pos": [6, 10, 5], "dist": 5.0, "drop": "coal"}
    running = rq.frame_options(tool="move_to", params={}, intent="reach home", facts=facts, running=True)
    assert running["mine_ore"] == ("Stop the current action and mine the exposed coal_ore at [6, 10, 5], 5 blocks away;"
                                   " then go back to the current work.")
    said = rq.wayside_facts(facts)
    assert said["exposed_ores"] == "Open to the air nearby: coal_ore at [6, 10, 5], 5 blocks away."
    assert said["agent_plan"].startswith("The Agent's plan, in its order: 1) “wayside”")
    asked = rq.frame_questions(running, acting_for_intent=True, handback="running")["q"]["instructions"]
    assert asked.startswith("Choose what the Body does right now for the Agent: its current work, or an item")
    assert list(running)[-1] == "call_agent"
    stone = {**facts, "inventory": {"stone_pickaxe": 1}}
    assert rq.wayside_options(stone) == {"mine_ore": "Mine the exposed deepslate_iron_ore at [4, 10, 5], 3 blocks away."}
    assert rq.wayside_ore({**facts, "inventory": {}}) is None
    assert rq.wayside_ore({key: value for key, value in stone.items() if key != "life_profile"}) is None
    # Judged against the whole active plan, the items the Body never starts included.
    assert rq.agent_plan_whole(stone) == (
        "1) “wayside”: mine coal and iron ore passed on the way "
        "(it fits when a coal or iron ore is open to the air within a few blocks).")
    # Only on the way: not while idle, not between steps (H7, 2026-10-30).
    assert "mine_ore" not in rq.idle_options(stone)
    assert "mine_ore" not in rq.frame_options(tool="move_to", params={}, intent="reach home", facts=stone,
                                              running=False, result={"success": False, "reason": "x"})
    assert rq.runs_default("mine_ore") and rq.runs_default("eat") and not rq.runs_default("stay")


def test_mine_ore_runs_mine_block_collect_on_the_offered_ore():
    from minebot.app.direct_call_supervision import CallSupervisor
    facts = {"life_profile": {"projects": []}, "inventory": {"iron_pickaxe": 1},
             "exposed_ores": [{"block": "diamond_ore", "pos": [1, -50, 2], "dist": 2.0}]}
    assert CallSupervisor._default_action("mine_ore", {"facts": facts}) == (
        "mine_block_collect", {"pos": [1, -50, 2], "target_block_types": ["diamond_ore"], "expected_drops": ["diamond"],
                               "strict_type": True})


def test_seen_creatures_said_the_same_way_for_every_kind():
    dragon = {"id": "d", "name": "Ender Dragon", "type": "ender_dragon", "pos": [0, 70, 10], "dist2": 136,
              "hostile": True, "health": 180.0, "max_health": 200.0, "velocity": [0, -3, -11], "approaching": True,
              "facing_body": True, "boss": True, "aggressive": False, "phase_id": 2, "head_pos": [0, 68, 4],
              "nearest_dist": 5.3}
    zombie = {"id": "z", "name": "Zombie", "type": "zombie", "pos": [3, 64, 0], "dist2": 9, "hostile": True,
              "health": 20.0, "max_health": 20.0, "velocity": [0, 0, 0], "approaching": False, "facing_body": False,
              "boss": False, "aggressive": True, "nearest_dist": 2.6}
    facts = {"position": [0, 64, 0], "entities": [dragon, zombie]}
    near = rq.hostiles(facts)
    # The nearest part decides the distance, not the centre of a long body.
    assert [mob["name"] for mob in near] == ["Zombie", "Ender Dragon"] and near[1]["dist"] == 5.3
    assert rq._threat_sentence(facts) == (
        "Nearest hostile mob: Zombie, 3 blocks away. What the Body sees: "
        "Zombie: health 20 of 20; in attack mode; 3 blocks away; not moving. "
        "Ender Dragon (a boss): health 180 of 200; flying in to land on the exit portal; "
        "its head is at [0.0, 68.0, 4.0], 6 blocks from the Body; 5 blocks away; "
        "moving toward the Body at 11 blocks a second; facing the Body.")


def test_seen_creatures_need_the_body_facts_and_count_bosses_and_attack_mode():
    # Frames without the new Body facts read as before.
    old = {"position": [0, 64, 0], "entities": [{"id": "z", "name": "Zombie", "type": "zombie",
                                                 "pos": [3, 64, 0], "dist2": 9, "hostile": True}]}
    assert rq._threat_sentence(old) == "Nearest hostile mob: Zombie, 3 blocks away."
    wolf = {"id": "w", "name": "Wolf", "type": "wolf", "pos": [4, 64, 0], "dist2": 16, "hostile": False,
            "aggressive": True}
    calm = {"id": "c", "name": "Cow", "type": "cow", "pos": [2, 64, 0], "dist2": 4, "hostile": False,
            "aggressive": False}
    assert [mob["name"] for mob in rq.hostiles({"entities": [wolf, calm]})] == ["Wolf"]


def test_a_walk_with_no_route_says_so_and_what_it_was_allowed_to_change():
    """End baseline run-3, 15:19: five walks out of the arrival cell read only
    "stopped"; the route facts said no route was found."""
    route = {"route_found": False, "may_break": True, "may_place": True, "placeable_blocks": 127}
    said = rq.result_sentence("move_to(pos=[85, 49, 0])",
                              {"success": False, "reason": "no_route_found", "metrics": {"route": route}})
    assert said == ("move_to(pos=[85, 49, 0]) has ended. It reported failure: no_route_found."
                    " The pathfinder found no route to the target. It was allowed to break blocks and place blocks."
                    " It carries 127 blocks it can place.")
    sealed = {**route, "may_break": False, "may_place": False}
    assert rq.result_sentence("m()", {"success": False, "summary": {"route": sealed}}).endswith(
        " It was not allowed to break or place blocks.")
    found = {**route, "route_found": True}
    assert rq.result_sentence("m()", {"success": False, "reason": "timeout", "metrics": {"route": found}}) == \
        "m() has ended. It reported failure: timeout."


def test_steps_are_offered_while_a_threat_is_seen_and_say_where_they_end():
    """End baseline run-3: the Body stood with only "stay" while the dragon came
    on from 74 blocks (dragon-unit-tests-20261030 §6, B)."""
    steps = [{"direction": "north", "to": [0, 64, -4], "cells": 4},
             {"direction": "east", "to": [4, 64, 0], "cells": 4},
             {"direction": "south", "to": [0, 65, 4], "cells": 4}]
    dragon = {"type": "ender_dragon", "name": "Ender Dragon", "boss": True, "dist": 60.0, "pos": [60.5, 70.0, 0.5]}
    facts = {"health": 20, "food": 20, "oxygen": 300, "position": [0.5, 64.0, 0.5], "steps": steps, "entities": [dragon]}
    idle = rq.idle_options(facts)
    assert idle["step_north"].startswith("Step north to [0, 64, -4], ")
    assert idle["step_east"] == "Step east to [4, 64, 0], toward the Ender Dragon (4 blocks nearer)."
    assert "away from the Ender Dragon" in idle["step_north"] or "neither" in idle["step_north"]
    running = rq.frame_options(facts=facts, intent="", running=True, **CALL)
    assert running["step_east"].startswith("Stop the current action and step east")
    # Nothing seen and no cloud: no steps.
    assert not any(key.startswith("step_") for key in rq.idle_options({**facts, "entities": []}))


def test_in_a_cloud_the_steps_out_of_it_say_so_and_cells_in_it_are_not_offered():
    steps = [{"direction": "north", "to": [0, 64, -4], "cells": 4},
             {"direction": "east", "to": [1, 64, 0], "cells": 2}]
    cloud = {"pos": [0.5, 64.0, 0.5], "radius": 3.0, "particle": "dragon_breath", "inside": True}
    facts = {"health": 20, "food": 20, "oxygen": 300, "position": [0.5, 64.0, 0.5], "steps": steps, "cloud": cloud}
    options = rq.idle_options(facts)
    assert options["step_north"] == "Step north to [0, 64, -4], away from the place that is taking the Body's health."
    assert "step_east" not in options  # ends inside the cloud
    assert rq.is_protective("step_north")


def test_a_place_that_keeps_taking_health_is_said_as_a_place_whatever_it_holds():
    # End run-7: breath took the Body from 20 to 3 while frames offered no way out.
    steps = [{"direction": "west", "to": [-4, 64, 0], "cells": 4}]
    hurt = {"times": 3, "seconds": 2.5}
    facts = {"health": 9, "food": 20, "oxygen": 300, "position": [0.5, 64.0, 0.5], "steps": steps, "hurt_here": hurt}
    body = rq.idle_question(facts, {})["state"]["body"]
    assert "The place where the Body stands is taking its health: hurt 3 times here in the last 2 seconds, not by a mob's blow." in body
    options = rq.idle_options(facts)
    assert options["step_west"] == "Step west to [-4, 64, 0], away from the place that is taking the Body's health."
    said = rq.result_sentence("move_away()", {"success": False, "reason": "still_losing_health",
                                              "metrics": {"hurt_here": {"times": 2, "seconds": 1.0}}})
    assert said.endswith("The Body is still losing health where it stands: The place where the Body stands is taking"
                         " its health: hurt 2 times here in the last 1 seconds, not by a mob's blow.")


def test_a_rule_that_keeps_the_body_still_says_so_while_the_place_takes_health():
    rules = [{"when": "the dragon is near", "then": "stand and shoot it",
              "call": {"tool": "ranged_attack", "arguments": {"target": "ender_dragon"}}},
             {"when": "health is low", "then": "flee", "call": {"tool": "move_to", "arguments": {"pos": [0, 64, 0]}}}]
    facts = {"health": 9, "food": 20, "oxygen": 300, "position": [0.5, 64.0, 0.5], "life_profile": {"rules": rules},
             "hurt_here": {"times": 2, "seconds": 1.0}}
    options = rq.rule_options(facts)
    assert options["rule_1"].endswith("Do that now. Doing it, the Body stays where it is losing health.")
    assert options["rule_2"].endswith("Do that now.")
    assert rq.rule_options({**facts, "hurt_here": None})["rule_1"].endswith("Do that now.")


def test_a_missed_arrow_says_where_it_struck():
    said = rq.result_sentence("ranged_attack()", {"success": False, "reason": "missed",
                                                  "metrics": {"arrow_landed": [-2, 129, 4]}})
    assert said == "ranged_attack() has ended. It reported failure: missed. The arrow struck the block at [-2, 129, 4]."


def test_a_missed_arrow_names_the_block_it_struck():
    # End run-8: 33 arrows into a caged crystal's iron bars read only as a cell.
    said = rq.result_sentence("ranged_attack()", {"success": False, "reason": "missed",
                                                  "metrics": {"arrow_landed": [34, 81, -27], "arrow_landed_block": "iron_bars"}})
    assert said.endswith("The arrow struck the iron bars at [34, 81, -27].")


def test_a_missed_lead_shot_says_the_predicted_target_position():
    said = rq.result_sentence("ranged_attack()", {"success": False, "reason": "missed",
                                                  "metrics": {"predicted_target_pos": [34.25, 81.0, -24.5],
                                                              "arrow_landed": [34, 81, -27]}})
    assert said.endswith("The target was predicted to be at [34.25, 81, -24.5]; "
                         "the arrow struck the block at [34, 81, -27].")


def test_a_fall_is_said_as_what_lies_below():
    void = rq.result_sentence("move_away()", {"success": False, "reason": "falling",
                                              "metrics": {"fall": {"fallen": 12.0, "void_below": True}}})
    assert void.endswith("The bot is falling with nothing below it down to the bottom of the world.")
    ground = rq.result_sentence("move_away()", {"success": False, "reason": "falling",
                                                "metrics": {"fall": {"ground_below": 23.5, "lands_on": "end_stone"}}})
    assert ground.endswith("The bot is falling; end stone is 23.5 blocks straight below.")


def test_a_fall_onto_ground_offers_landing_in_water():
    """End run-6: thrown off a pillar by the dragon the Body fell 30 blocks
    with a water bucket in the bag; nothing in the frame said it was falling."""
    falling = {"health": 20, "food": 20, "oxygen": 300, "position": [44.5, 80.0, 18.5],
               "dimension": "minecraft:the_end", "inventory": {"water_bucket": 2},
               "fall": {"fallen": 6.0, "ground_below": 22.5, "lands_on": "end_stone", "landing_cell": [44, 57, 18]}}
    options = rq.idle_options(falling)
    assert options["cushion_fall"] == ("Pour the water bucket just before landing, to land in water: fallen 6 blocks "
                                       "so far, end stone 22.5 blocks straight below; landing in water takes no health.")
    assert rq.is_protective("cushion_fall")
    assert ("The Body is falling; end stone is 22.5 blocks straight below. It has fallen 6 blocks so far."
            " Landing on end stone from 28.5 blocks takes about 26 health; the Body has 20."
            in rq.idle_question(falling, {})["state"]["body"])
    running = rq.frame_options(facts=falling, intent="", running=True, **CALL)
    assert running["cushion_fall"].startswith("Stop the current action and pour the water bucket")
    for fall in ({"fallen": 6.0, "void_below": True},
                 {"fallen": 6.0, "ground_below": 22.5, "lands_on": "water"},
                 {"fallen": 0.5, "ground_below": 2.0, "lands_on": "stone"}):
        assert "cushion_fall" not in rq.idle_options({**falling, "fall": fall})
    assert "cushion_fall" not in rq.idle_options({**falling, "inventory": {"bucket": 1}})
    assert "cushion_fall" not in rq.idle_options({**falling, "dimension": "minecraft:the_nether"})
    assert "cushion_fall" not in rq.idle_options({k: v for k, v in falling.items() if k != "fall"})


def test_landed_in_its_water_the_bucket_can_be_filled_again():
    landed = {"health": 20, "food": 20, "oxygen": 300, "position": [44.5, 58.0, 18.5],
              "dimension": "minecraft:the_end", "inventory": {"bucket": 1, "water_bucket": 1},
              "water_source_in_reach": [44, 58, 18]}
    options = rq.idle_options(landed)
    assert options["scoop_water"] == "Fill the empty bucket from the water source at [44, 58, 18]."
    assert not rq.is_protective("scoop_water")
    assert rq.runs_default("scoop_water")
    assert "scoop_water" not in rq.idle_options({**landed, "water_source_in_reach": None})
    assert "scoop_water" not in rq.idle_options({**landed, "inventory": {"water_bucket": 2}})


def test_the_last_shot_is_said_after_the_action_ended():
    base = {"health": 20, "food": 20, "oxygen": 300}
    flying = {**base, "last_shot": {"target_type": "ender_dragon", "outcome": "in_flight", "age_s": 0.4}}
    assert "The last arrow, shot at the ender dragon just now, is still in flight." in rq._body_sentence(flying)
    hit = {**base, "last_shot": {"target_type": "ender_dragon", "outcome": "hit", "age_s": 3.0,
                                 "health_before": 200.0, "health_after": 194.5}}
    assert "The last arrow, shot 3 seconds ago, hit the ender dragon: its health went from 200 to 194.5." in rq._body_sentence(hit)
    gone = {**base, "last_shot": {"target_type": "end_crystal", "outcome": "hit", "target_gone": True, "age_s": 2}}
    assert "hit the end crystal; it is gone." in rq._body_sentence(gone)
    missed = {**base, "last_shot": {"target_type": "end_crystal", "outcome": "missed", "age_s": 2,
                                    "arrow_landed": [34, 81, -27], "arrow_landed_block": "iron_bars"}}
    assert "missed; it struck the iron bars at [34, 81, -27]." in rq._body_sentence(missed)
    old = {**base, "last_shot": {"target_type": "end_crystal", "outcome": "missed", "age_s": 45}}
    assert "last arrow" not in rq._body_sentence(old)


def test_an_out_of_range_shot_says_the_distance():
    said = rq.result_sentence("ranged_attack()", {"success": False, "reason": "out_of_range",
                                                  "metrics": {"target_distance": 39.4, "max_range": 24.0}})
    assert said == ("ranged_attack() has ended. It reported failure: out_of_range. "
                    "The target is 39 blocks away; the call allowed 24.")
    other = rq.result_sentence("discard_item()", {"success": False, "reason": "target_not_found",
                                                  "metrics": {"target_distance": 5.0}})
    assert "blocks away" not in other


def test_result_frame_says_the_body_changed_dimension_and_where_it_ended():
    # End run-12, 13:50:00: the pearl carried the Body into the End; the sample
    # stream still said the overworld place and the frame said no dimension.
    facts = {"start_position": [-1.5, 74.0, -6.3], "position": [-1.5, 74.0, -6.3],
             "start_dimension": "minecraft:overworld", "dimension": "minecraft:overworld"}
    result = {"success": True, "metrics": {"position": [100.5, 50.0, 0.5], "dimension": "minecraft:the_end"}}
    state = rq.frame_state(goal="", intent="i", tool="use_item", params={"item": "ender_pearl"},
                           facts=facts, running=False, result=result)
    assert state["position"] == ("started at [-1.5, 74.0, -6.3]; now at [100.5, 50.0, 0.5]. "
                                 "The Body is now in the End; it was in the overworld when the call started.")
    # Running, the live dimension fact says it; unchanged, nothing is said.
    running = rq.frame_state(goal="", intent="i", tool="use_item", params={}, running=True,
                             facts={**facts, "dimension": "minecraft:the_end"})
    assert running["position"].endswith("The Body is now in the End; it was in the overworld when the call started.")
    same = rq.frame_state(goal="", intent="i", tool="use_item", params={}, running=False, result=result,
                          facts={**facts, "start_dimension": "minecraft:the_end"})
    assert same["position"] == "started at [-1.5, 74.0, -6.3]; now at [100.5, 50.0, 0.5]."


def _rule_facts(**extra):
    rules = [{"when": "night falls", "then": "eat before sleeping",
              "call": {"tool": "consume_item", "arguments": {"item": "bread"}}},
             {"when": "the dragon is far", "then": "keep working", "call": None}]
    facts = {"health": 20, "food": 20, "oxygen": 300, "position": [0.5, 64.0, 0.5],
             "inventory": {"bread": 3}, "life_profile": {"rules": rules}}
    facts.update(extra)
    return facts


def test_the_agents_rules_are_offered_in_an_idle_frame_with_nothing_else_protective():
    # run-11: 835 of 1111 idle frames offered only "stay"; the Agent's rules
    # were offered only beside other protective options (§6.y.22).
    options = rq.idle_options(_rule_facts())
    assert options["rule_1"] == "The Agent said: “when night falls, eat before sleeping”. Do that now."
    assert "rule_2" not in options
    request = rq.idle_question(_rule_facts(), options)
    assert request["state"]["agent_rules"] == "The Agent's standing rules, in its words: when the dragon is far, keep working."
    questions = request["questions"]
    # No danger question: the rules are part of the work choice.
    assert set(questions) == {"q"} and {"stay", "rule_1"} <= set(questions["q"]["criteria"])
    # An answer naming an instruction is invalid, never translated into staying.
    answer = {"answers": {"q": {"choice": "rule_2"}}}
    assert rq.frame_choice(answer, questions) == "unknown"
    assert rq.frame_choice({"answers": {"q": {"choice": "rule_1"}}}, questions) == "rule_1"


def test_a_rule_the_body_cannot_carry_out_now_is_not_offered():
    assert "rule_1" not in rq.idle_options(_rule_facts(inventory={}))


def test_the_agents_rules_are_offered_in_running_and_result_frames():
    running = rq.frame_options(tool="mine_block", params={}, intent="get stone", facts=_rule_facts(), running=True)
    assert running["rule_1"] == ("Stop the current action and the Agent said: “when night falls, eat before "
                                 "sleeping”. Do that now.")
    questions = rq.frame_questions(running, acting_for_intent=True)
    assert set(questions) == {"q"} and "rule_1" in questions["q"]["criteria"]
    ended = rq.frame_options(tool="mine_block", params={}, intent="get stone", facts=_rule_facts(), running=False,
                             result={"success": True})
    assert "rule_1" in ended and "rule_2" not in ended


def test_beside_other_protective_options_rules_stay_with_the_danger_choice_once():
    facts = _rule_facts(hostiles=[{"id": "z1", "name": "Zombie", "type": "zombie", "dist": 5.0, "hostile": True}],
                        inventory={"bread": 3, "iron_sword": 1}, main_hand="iron_sword")
    options = rq.idle_options(facts)
    questions = rq.frame_questions(options)
    assert set(questions) == {"q", "d", "p"}
    assert {"rule_1", "move_away"} <= set(questions["p"]["criteria"])
    assert "rule_2" not in questions["p"]["criteria"]
    assert not any(key.startswith(rq.RULE_PREFIX) for key in questions["q"]["criteria"])


@pytest.mark.parametrize("kind", ["idle", "work", "result", "protecting", "after_protection"])
@pytest.mark.parametrize("danger", [False, True])
@pytest.mark.parametrize("call_rule", [False, True])
def test_no_call_rules_do_not_change_whether_a_frame_asks_about_danger(kind, danger, call_rule):
    facts = _rule_facts()
    if danger:
        facts["hostiles"] = [{"id": "z1", "name": "Zombie", "type": "zombie", "dist": 5, "hostile": True}]
    rules = facts["life_profile"]["rules"]
    baseline = {**facts, "life_profile": {"rules": rules[:1] if call_rule else []}}
    with_words = {**facts, "life_profile": {"rules": baseline["life_profile"]["rules"] + rules[1:]}}

    def questions(body):
        if kind == "idle":
            options = rq.idle_options(body)
        elif kind == "protecting":
            options = rq.protecting_options(body, "cushion_fall")
        elif kind == "after_protection":
            options = rq.after_default_options(original_call="mine_block", intent="get stone",
                default_call="move_away", result={"success": True}, facts=body)
        else:
            options = rq.frame_options(tool="mine_block", params={}, intent="get stone", facts=body,
                running=kind == "work", result={"success": True} if kind == "result" else None)
        return rq.frame_questions(options, protecting=kind in {"protecting", "after_protection"})

    before, after = questions(baseline), questions(with_words)
    assert before == after
    assert ("d" in after) == (danger and kind not in {"protecting", "after_protection"})
    assert ("p" in after) == ("d" in after)


def test_a_finished_move_into_a_portal_says_the_body_went_in():
    said = rq.result_sentence("move_to(pos=[0, 63, 0])", {"success": True, "reason": "entered_portal"})
    assert said == "move_to(pos=[0, 63, 0]) has ended normally: the Body went into the portal."
    assert rq.result_sentence("move_to()", {"success": True, "reason": "arrived"}) == "move_to() has ended normally."


def test_frames_say_the_legs_keep_moving_and_idle_goes_on_moving():
    """body-channels-20261105 §6.1: Decision chooses hands and dodges knowing the legs keep going."""
    facts = {"health": 20, "health_before": 20, "food": 20, "oxygen": 300, "position": [3.2, 64.0, 7.9],
             "entities": [{"id": "dragon", "name": "Ender Dragon", "boss": True, "dist": 40}],
             "inventory": {"bow": 1, "arrow": 32},
             "movement": {"mode": "keep_moving", "center": [0.5, 64.0, 30.5], "radius": 14.0}}
    request = rq.idle_question(facts, rq.idle_options(facts))
    assert request["state"]["movement"] == ("The Body keeps moving around [0.5, 64.0, 30.5] within 14 blocks "
                                            "(the Agent's setting); shots, eating and the shield do not stop it.")
    assert request["questions"]["q"]["criteria"]["stay"] == (
        "Go on as the Body is: it keeps moving (the Agent's setting); wait for the next step.")
    held = {**facts, "movement": {**facts["movement"], "held_by_action": True}}
    assert rq.movement_sentence(held).endswith("The current action holds the legs; they go on when it ends.")
    standing = {**facts, "movement": {"mode": "stand"}}
    assert "movement" not in rq.idle_question(standing, rq.idle_options(standing))["state"]


def test_a_running_fight_is_offered_on_under_its_own_target():
    """body-preemption-20261106.md §8.3: the perched dragon with an enderman nearer."""
    facts = {"health": 20, "food": 20, "oxygen": 300, "selected_item": "diamond_sword",
             "nearby_entities": [
                 {"id": "e1", "name": "Enderman", "type": "enderman", "hostile": True, "aggressive": True, "dist": 3.0},
                 {"id": "d1", "name": "Ender Dragon", "type": "ender_dragon", "boss": True, "dist": 7.0}]}
    sentence, option = rq.running_fight(facts, "engage", "d1")
    assert sentence == "The Body is fighting the Ender Dragon (7 blocks away) in melee with the diamond_sword."
    options = rq.protecting_options(facts, "engage", "d1")
    assert options["keep_fighting"] == ("Keep fighting the Ender Dragon (7 blocks away) in melee with the "
                                        "diamond_sword, as the Body is doing now.")
    assert "Enderman" in options["engage"]  # the nearest is still offered as a switch
    assert rq.running_fight(facts, "move_away", "d1") is None  # a flight is never said as running
    assert rq.running_fight(facts, "engage", "gone") is None
    assert "keep_fighting" not in rq.protecting_options(facts, "engage")


def test_keep_fighting_carries_the_running_fight_on():
    from minebot.app.body_loop import BodyLoop
    record = {"supervision_context": {"is_reflex_action": True, "reflex_action_choice": "engage"},
              "params": {"target": "d1"}}
    assert BodyLoop._continues_protection(SimpleNamespace(), record, "keep_fighting", {})


def test_no_safe_point_is_said_in_words_with_the_reasons():
    sentence = rq.result_sentence("move_away(...)", {"success": False, "reason": "dodge_no_safe_point", "metrics": {
        "threat_kind": "minecraft:ender_dragon", "final_pos": [4.5, 65.0, 2.5], "final_distance": 30.4,
        "threat_observed": True, "outside_reach_margin": False, "step_reach": 4, "level_step_cells": 12,
        "rejected_cells": {"no_floor_beside_path": 9, "cloud_or_hurting_place": 0, "toward_another_mob": 0,
                           "nearer_the_threat": 3}}})
    assert sentence == (
        "move_away(...) has ended. It reported failure: dodge_no_safe_point. The Body did not move: none of the 12"
        " level cells within 4 blocks was a safe step away from the ender dragon (9 a drop or no floor beside the"
        " way, 3 nearer the ender dragon). The Body is at [4.5, 65.0, 2.5]; the ender dragon is 30.40 blocks away."
        " It is still within the threat's attack reach plus margin.")
    bare = rq.result_sentence("move_away(...)", {"success": False, "reason": "dodge_no_safe_point", "metrics": {
        "threat_kind": "ender_dragon", "step_reach": 4, "level_step_cells": 0, "rejected_cells": {}}})
    assert "The Body did not move: no level ground to step onto within 4 blocks." in bare


def test_move_away_says_a_step_does_not_leave_a_far_reaching_threat():
    facts = {"health": 20.0, "hostiles": [{"id": "d1", "name": "Ender Dragon", "type": "ender_dragon",
                                           "dist": 30.0, "hostile": True}],
             "inventory": {"diamond_sword": 1}}
    assert rq.protective_options(facts)["move_away"].endswith(
        "It is one step of at most 4 blocks on level ground; the Ender Dragon reaches 64 blocks, so the step"
        " does not take the Body out of its reach.")
    facts["hostiles"][0].update(id="z1", name="Zombie", type="zombie", dist=2.0)
    assert "reaches" not in rq.protective_options(facts)["move_away"]


def test_a_wounded_mob_says_its_health_in_the_fight_options():
    # A player sees how far a fight has gone (body-tool-boundaries-20261110 §6, A).
    facts = {"entities": [{"hostile": True, "name": "Zombie", "type": "zombie", "id": "z", "dist": 3,
                           "pos": [3, 64, 0], "health": 8.2, "max_health": 20.0}]}
    options = rq.protective_options(facts)
    assert "Zombie (3 blocks away, health 8 of 20)" in options["engage"]
    facts["entities"][0]["health"] = 20.0
    assert "Zombie (3 blocks away) " in rq.protective_options(facts)["engage"] + " "


def test_melee_meets_the_mob_that_hurts_not_a_creeper():
    # run-15 03:32:48: "fight back against the Creeper (11 blocks away)" with two
    # skeletons shooting; melee goes to the nearest mob that is not a creeper,
    # the run and the bow keep the nearest of all (body-tool-boundaries §13).
    facts = {"entities": [
        {"hostile": True, "name": "Creeper", "type": "creeper", "id": "c", "dist": 11, "pos": [11, 64, 0]},
        {"hostile": True, "name": "Skeleton", "type": "skeleton", "id": "s", "dist": 12, "pos": [0, 64, 12]}]}
    options = rq.protective_options(facts)
    assert "Fight back against the Skeleton (12 blocks away)" in options["engage"]
    assert "Run from the hostile mobs around the Body (Creeper 11 blocks away; Skeleton 12 blocks away)" in options["move_away"]
    assert rq.melee_target(rq.hostiles(facts), facts)["id"] == "s"


def test_a_creeper_alone_is_run_from_not_fought():
    facts = {"entities": [{"hostile": True, "name": "Creeper", "type": "creeper", "id": "c", "dist": 5,
                           "pos": [5, 64, 0]}]}
    options = rq.protective_options(facts)
    assert "engage" not in options
    assert "move_away" in options


def test_an_agent_escape_running_in_danger_can_be_carried_on():
    # run-15 03:33:04 / 03:36:59: the Agent's seal and dig away were stopped by
    # an engage because "go on with it" was not a protective choice (§13).
    options = {"keep_going": "Keep doing the current work, shelter_in_place(action=\"seal\").",
               "engage": "Stop the current action and fight back against the Skeleton (2 blocks away).",
               "call_agent": "Stop the current action and hand control back to the Agent."}
    questions = rq.frame_questions(options, keep_protecting=options["keep_going"])
    assert list(questions["p"]["criteria"])[0] == "keep_going"
    answer = {"answers": {"d": {"choice": "in_danger"}, "p": {"choice": "keep_going"}, "q": {"choice": "keep_going"}}}
    assert rq.frame_choice(answer, questions) == "keep_going"
    assert "keep_going" not in rq.frame_questions(options)["p"]["criteria"]


def test_melee_is_offered_against_a_mob_at_hand_only():
    # run-16 04:37:41: a fight went after a zombie 37 blocks off behind blocks (§13).
    far = {"entities": [
        {"hostile": True, "name": "Creeper", "type": "creeper", "id": "c", "dist": 30, "pos": [30, 64, 0]},
        {"hostile": True, "name": "Zombie", "type": "zombie", "id": "z", "dist": 37, "pos": [0, 64, 37]}]}
    assert "engage" not in rq.protective_options(far)
    near = {"entities": [{"hostile": True, "name": "Zombie", "type": "zombie", "id": "z", "dist": 15, "pos": [0, 64, 15]}]}
    assert "engage" in rq.protective_options(near)


def test_a_search_that_ended_normally_says_what_it_found_and_where():
    # easy run-19 07:11: "ended normally" alone; the same explore_for ran 106 times (§13).
    result = {"success": True, "reason": "found", "metrics": {
        "target_found": True, "net_displacement": 0.0,
        "entities": [{"dist2": 1013.6, "name": "Cow", "pos": [62.01, 120.0, -34.88], "type": "cow"}]}}
    sentence = rq.result_sentence("explore_for(...)", result)
    assert sentence == ("explore_for(...) has ended normally. It found Cow 32 blocks away at [62, 120, -35]."
                        " The Body did not move.")
    assert rq.result_sentence("move_to(...)", {"success": True, "reason": "arrived"}) == "move_to(...) has ended normally."


def test_the_run_option_names_every_mob_it_runs_from():
    """run-44 2.71-2.92: "run from the Skeleton" while a creeper walked up; the run is from them all."""
    facts = {"health": 14.0, "inventory": {"oak_log": 3}, "hostiles": [
        {"id": "s1", "name": "Skeleton", "type": "skeleton", "dist": 2.0, "hostile": True,
         "health": 12.0, "max_health": 20.0},
        {"id": "c1", "name": "Creeper", "type": "creeper", "dist": 9.0, "hostile": True, "approaching": True},
        {"id": "z1", "name": "Zombie", "type": "zombie", "dist": 30.0, "hostile": True},
    ]}
    assert rq.protective_options(facts)["move_away"] == (
        "Run from the hostile mobs around the Body (Skeleton 2 blocks away, health 12 of 20; "
        "Creeper 9 blocks away, coming closer) until the nearest is 23 blocks away.")
    assert "Creeper" not in rq.protective_options(facts).get("engage", "")  # melee still never meets a creeper
