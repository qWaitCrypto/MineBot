"""The frame question wording, checked against the sentences settled offline."""

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
        "agent_intent": "get 3 raw iron",
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


def test_vital_window_gives_values_from_a_few_seconds_ago():
    window = rq.VitalWindow()
    assert window.add(0.0, {"health": 20, "oxygen": 300}) == {}
    assert window.add(1.0, {"health": 20, "oxygen": 280}) == {}
    before = window.add(5.0, {"health": 14, "oxygen": 200})
    assert before == {"health_before": 20.0, "oxygen_before": 300.0, "vital_age_s": 5.0}
    assert window.add(30.0, {"health": 14}) == {}


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
