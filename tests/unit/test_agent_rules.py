"""Call-rules offer actions; rules without calls are the Agent's frame context."""
import asyncio
from types import SimpleNamespace

import pytest

from minebot.app import reflex_question as rq
from minebot.app.body_loop import BodyLoop
from minebot.app.direct_call_supervision import CallSupervisor
from minebot.app.life_profile import LifeStore, life_summary, register_life_tools
from minebot.app.runtime_state import RuntimeScope
from minebot.brain.registry import RegisteredTool, ToolRegistry, ToolSidecar
from tests.unit.test_body_loop import _runtime

FIGHT = {"when": "a zombie, skeleton, spider or magma cube comes at me and my health is 10 of 20 or more",
         "then": "fight it with my best sword",
         "call": {"tool": "engage_entity", "arguments": {"target": "nearest_hostile"}}}
CREEPER = {"when": "a creeper comes near", "then": "get away from it; never fight a creeper in melee",
           "call": {"tool": "move_away", "arguments": {"danger_pos": "nearest_hostile", "min_distance": 16,
                                                       "from_hostiles": True}}}
EAT = {"when": "my health is under 10 of 20", "then": "eat bread", "call": {"tool": "consume_item",
                                                                           "arguments": {"item": "bread"}}}


def _facts(**extra):
    facts = {"health": 20.0, "inventory": {"iron_sword": 1},
             "hostiles": [{"id": "m1", "name": "Zombie", "type": "zombie", "dist": 3.0, "hostile": True,
                           "pos": [10.5, 64.0, -3.5]}],
             "life_profile": {"rules": [FIGHT, CREEPER, EAT]}}
    facts.update(extra)
    return facts


def test_each_rule_is_offered_in_the_agents_words_after_the_protective_actions():
    options = rq.protecting_options(_facts(), "move_away")
    assert list(options)[-3:] == ["rule_1", "rule_2", "call_agent"]
    assert options["rule_1"] == ("The Agent said: “when a zombie, skeleton, spider or magma cube comes at me "
                                 "and my health is 10 of 20 or more, fight it with my best sword”. Do that now.")
    assert options["rule_2"].startswith("The Agent said: “when a creeper comes near, get away")


def test_a_rule_the_body_cannot_carry_out_now_is_not_offered():
    # No bread: the eating rule is gone. No hostile mob: nothing to bind.
    assert "rule_3" not in rq.protective_options(_facts())
    assert "rule_3" in rq.protective_options(_facts(inventory={"iron_sword": 1, "bread": 2}))
    assert rq.protective_options(_facts(hostiles=[])) == {}
    hungry = rq.protective_options(_facts(hostiles=[], food=12, food_item="bread", inventory={"bread": 2}))
    assert set(hungry) == {"eat", "rule_3"}


def test_in_work_frames_the_rules_go_with_the_protective_question():
    facts = _facts()
    options = rq.frame_options(tool="collect_resource", params={}, intent="4 oak_log in the inventory",
                               facts=facts, running=True)
    questions = rq.frame_questions(options, acting_for_intent=True)
    assert "rule_1" not in questions["q"]["criteria"]
    assert questions["p"]["criteria"]["rule_1"].startswith("Stop the current action and the Agent said: “when")
    assert list(questions["p"]["criteria"])[-1] == "call_agent"


def test_a_chosen_rule_runs_the_agents_call_bound_to_the_frame():
    facts = _facts()
    assert CallSupervisor._default_action("rule_1", {"facts": facts}) == (
        "engage_entity", {"target": "nearest_hostile"})
    assert CallSupervisor._default_action("rule_2", {"facts": facts}) == (
        "move_away", {"danger_pos": [10.5, 64.0, -3.5], "min_distance": 16, "from_hostiles": True})
    assert CallSupervisor._default_action("rule_3", {"facts": facts}) is None
    assert CallSupervisor._default_action("rule_9", {"facts": facts}) is None


def test_repeated_rule_actions_are_said_like_any_protective_action():
    sentence = rq._body_sentence({"health": 12.0, "protection_repeat": {
        "choice": "rule_1", "count": 3, "hurt": 3, "age_s": 16.0}})
    assert sentence.endswith("Following the Agent's rule has not helped: the Body has followed the Agent's "
                             "rule 3 times in the last 16 seconds and was hurt again during 3 of them.")


def test_a_protection_frame_with_rules_offers_them_and_no_fight_verdict():
    runtime, _, _ = _runtime()
    loop = BodyLoop(runtime)
    context = {"is_reflex_action": True, "reflex_action_choice": "move_away"}
    record = {"tool": "move_away", "params": {}, "supervision_context": context}
    facts = _facts(hostiles=[{"hostile": True, "id": "m1", "name": "Magma Cube", "type": "magma_cube",
                              "dist": 4, "pos": [1, 2, 3]}])
    execution = {"state": "running", "facts": facts}
    tool = SimpleNamespace(name="move_away")
    options = loop._options_for(tool, record, execution=execution)
    request = loop._question("protecting", tool, {}, execution, idle_s=0, purpose="running_frame",
                             supervision_context=context, options=options)
    assert "rule_1" in request["questions"]["q"]["criteria"]
    assert "deal with" not in request["state"]["threats"]


def _registry():
    registry = ToolRegistry()
    schemas = {
        "engage_entity": {"type": "object", "properties": {"target": {"type": "string"}},
                          "required": ["target"], "additionalProperties": False},
        "move_away": {"type": "object", "properties": {
            "danger_pos": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3},
            "min_distance": {"type": "number"}, "from_hostiles": {"type": "boolean"}},
            "required": ["danger_pos"], "additionalProperties": False},
        "consume_item": {"type": "object", "properties": {"item": {"type": "string"}}, "required": ["item"]},
    }
    for name, schema in schemas.items():
        registry.register(RegisteredTool(name, name, schema, lambda params: None,
                                         ToolSidecar(name, mutating=True, body_scope=("combat",))))
    return registry


def test_the_agent_writes_rules_with_update_life(tmp_path):
    store = LifeStore(tmp_path, RuntimeScope("127.0.0.1:25569", "world-a", "RuleBot"))
    registry = _registry()
    register_life_tools(registry, store)
    update = registry.get("update_life").callable
    written = update({"changes": {"rules": [FIGHT, CREEPER, EAT]}, "expected_revision": 0,
                      "reason": "The magma cube kept up while I ran and killed me."})
    assert written.success and written.metrics["profile"]["rules"][1] == CREEPER
    assert life_summary(store.read())["rules"][0]["then"] == "fight it with my best sword"
    for bad, message in (
        ({**FIGHT, "call": {"tool": "fly", "arguments": {}}}, "not a Body tool"),
        ({**FIGHT, "call": {"tool": "update_life", "arguments": {}}}, "not a Body tool"),
        ({**FIGHT, "call": {"tool": "engage_entity", "arguments": {}}}, "'target' is a required property"),
        ({**CREEPER, "call": {"tool": "move_away", "arguments": {"danger_pos": "zombie"}}}, "is not of type"),
        ({"when": "a creeper comes near", "call": CREEPER["call"]}, "rules[0].then must be non-empty text"),
    ):
        rejected = update({"changes": {"rules": [bad]}, "expected_revision": 1, "reason": "r"})
        assert rejected.success is False and message in rejected.metrics["error"], (bad, rejected.metrics)


KEEP = {"when": "the nearest hostile mob is more than 12 blocks away", "then": "keep working"}


def _answer(**answers):
    return {"answers": {name: {"choice": choice} for name, choice in answers.items()}}


def test_a_rule_without_a_call_is_written_as_context_and_call_rule_ids_stay_stable(tmp_path):
    store = LifeStore(tmp_path, RuntimeScope("127.0.0.1:25569", "world-a", "RuleBot"))
    registry = _registry()
    register_life_tools(registry, store)
    written = registry.get("update_life").callable(
        {"changes": {"rules": [FIGHT, KEEP, CREEPER]}, "expected_revision": 0,
         "reason": "Fleeing far mobs cost me work."})
    assert written.success and written.metrics["profile"]["rules"][1] == KEEP
    facts = _facts(life_profile=written.metrics["profile"])
    assert set(rq.rule_options(facts)) == {"rule_1", "rule_3"}
    assert rq.agent_rules_sentence(facts) == KEEP_WORDS
    assert CallSupervisor._default_action("rule_2", {"facts": facts}) is None
    assert CallSupervisor._default_action("rule_3", {"facts": facts}) == (
        "move_away", {"danger_pos": [10.5, 64.0, -3.5], "min_distance": 16, "from_hostiles": True})


KEEP_WORDS = ("The Agent's standing rules, in its words: when the nearest hostile mob is more than "
              "12 blocks away, keep working.")


def _compiler():
    return CallSupervisor(SimpleNamespace(trace=SimpleNamespace(emit=lambda *a, **k: None)))


@pytest.mark.parametrize("kind", ["idle", "idle_life", "work", "work_without_intent", "work_result", "protection",
                                 "protection_result", "after_protection_work", "after_protection_idle",
                                 "life", "life_result", "project", "interrupt_result"])
def test_no_call_rules_are_context_in_every_action_frame(kind):
    facts = _facts(life_profile={"rules": [FIGHT, KEEP, CREEPER]})
    if kind in {"idle_life", "life", "life_result", "project"}:
        facts["life_intent"] = "look after the home"
    if kind.startswith("idle"):
        request = rq.idle_question(facts, rq.idle_options(facts))
    elif kind == "interrupt_result":
        request = rq.interrupt_result_question(goal="get wood", intent="4 oak_log in the inventory",
            original_call="collect_resource", handler_intent="get clear of danger",
            handler_call="move_away", facts=facts)
    else:
        compiler = _compiler()
        context = {} if kind == "work_without_intent" else {"intent": "4 oak_log in the inventory"}
        if "protection" in kind:
            context.update(is_reflex_action=True, reflex_action_choice="cushion_fall")
        if kind in {"life", "life_result"}:
            context.update(is_reflex_action=True, is_life_action=True, reflex_action_choice="look_player")
        if kind == "project":
            context["is_life_project"] = True
        running = "result" not in kind and not kind.startswith("after_")
        execution = {"state": "running" if running else "succeeded", "facts": facts,
                     "result": {"success": True}}
        tool = SimpleNamespace(name="__reflex_cushion_fall" if "protection" in kind else "collect_resource")
        options = compiler._options_for(tool, {"tool": tool.name, "params": {},
            "supervision_context": context}, execution=execution)
        purpose = "running_frame" if running else "result_frame"
        if kind.startswith("after_"):
            purpose = "default_result_frame"
            options = rq.after_default_options(original_call="collect_resource" if kind.endswith("work") else "",
                intent=context["intent"], default_call=tool.name, result={"success": True}, facts=facts)
        request = compiler._question("rules", tool, {}, execution, idle_s=0, purpose=purpose,
            supervision_context=context, options=options,
            decision_context=None if running else {"tool_result": {"success": True}, "body_state": facts})
    state = request["state"]
    assert state["agent_rules"] == KEEP_WORDS
    keys = list(state)
    if "agent_intent" in state:
        assert keys[keys.index("agent_intent") + 1] == "agent_rules"
    elif kind.startswith("idle") or kind in {"life", "life_result"}:
        assert keys[:2] == ["body", "agent_rules"]
    else:
        assert keys[0] == "agent_rules"
    assert "rule_2" not in {key for q in request["questions"].values() for key in q["criteria"]}


def test_multiple_no_call_rules_are_said_once_in_order_without_call_rules_or_consequences():
    facts = _facts(life_profile={"rules": [FIGHT, KEEP, {
        "when": "When a dragon breath cloud covers the Body", "then": "move out of the breath cloud.", "call": None}]})
    request = rq.idle_question(facts, rq.idle_options(facts))
    assert request["state"]["agent_rules"] == (
        "The Agent's standing rules, in its words: when the nearest hostile mob is more than 12 blocks away, "
        "keep working; when a dragon breath cloud covers the Body, move out of the breath cloud.")
    assert set(rq.rule_options(facts)) == {"rule_1"}


def test_running_cushion_fall_choices_are_never_translated_into_step_done():
    async def check():
        facts = _facts(life_profile={"rules": [KEEP, FIGHT]}, inventory={"water_bucket": 1},
            fall={"fallen": 9, "ground_below": 20, "lands_on": "stone"})
        execution = {"state": "running", "facts": facts}
        context = {"is_reflex_action": True, "reflex_action_choice": "cushion_fall"}
        tool = SimpleNamespace(name="__reflex_cushion_fall")
        compiler = _compiler()
        options = compiler._options_for(tool, {"tool": tool.name, "params": {},
            "supervision_context": context}, execution=execution)
        assert "rule_1" not in options and "rule_2" in options
        assert "step_done" not in options and "next_step" not in options
        for selected in [*options, "rule_1"]:
            async def ask(request, **kwargs):
                assert request["state"]["agent_rules"] == KEEP_WORDS
                return SimpleNamespace(raw=_answer(q=selected))
            compiler._client = SimpleNamespace(ask=ask)
            choice = await compiler.judge("landing", tool, {}, execution, 0,
                supervision_context=context, options=options)
            assert choice == (selected if selected in options else "unknown")
            assert choice != "step_done"
            if choice == "cushion_fall":
                assert compiler._default_action(choice, execution)[0] == "__reflex_cushion_fall"
            if choice == "rule_2":
                assert compiler._default_action(choice, execution) == ("engage_entity", {"target": "nearest_hostile"})
    asyncio.run(check())
