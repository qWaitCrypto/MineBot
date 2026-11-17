import asyncio
import json
import copy
from pathlib import Path

import pytest

from minebot.app.work_runtime import WorkPlanError, WorkPlanExecutor, validate_plan
from minebot.contract import BodyState


class Decision:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.payloads = []

    async def ask(self, payload, *, priority=None):
        self.payloads.append(payload)
        choice = self.answers.pop(0) if self.answers else "step_done"
        return type("Answer", (), {"choice": choice, "raw": {"choice": choice}})()


async def run_plan(plan, answers=("step_done",), calls=None):
    calls = calls if calls is not None else []
    decision = Decision(*answers)

    async def call(tool, arguments, *, call_id, timeout_s):
        calls.append((tool, arguments, call_id, timeout_s))
        return {"success": True, "reason": "ok", "metrics": {"delivered": arguments}}

    executor = WorkPlanExecutor(plan, call=call, state=lambda: _state(), decision=decision)
    result = await executor.run()
    return result, decision


async def _state():
    return {"bot": {"observed": True, "position": [0, 64, 0]}}


@pytest.fixture
def plan_schema(tmp_path, monkeypatch):
    # Exercise the schema-present deployment path without writing under docs/.
    common = {"kind": {}, "intent": {"type": "string"}}
    call = {"type": "object", "properties": {**common, "kind": {"const": "call"},
            "tool": {"type": "string"}, "arguments": {"type": "object"},
            "repeat": {"type": "object", "properties": {"again_when": {"type": "string"}}}},
            "required": ["kind", "tool", "arguments"], "additionalProperties": False}
    sequence = {"type": "object", "properties": {**common, "kind": {"const": "sequence"},
                "steps": {"type": "array", "items": {"$ref": "#/$defs/node"}}},
                "required": ["kind", "intent", "steps"], "additionalProperties": False}
    schema = {"type": "object", "properties": {"work": {"$ref": "#/$defs/node"}},
              "$defs": {"node": {"oneOf": [call, sequence]}}}
    path = tmp_path / "plan.schema.json"
    path.write_text(json.dumps(schema), encoding="utf-8")
    monkeypatch.setattr("minebot.app.work_runtime._schema_path", lambda: path)


@pytest.mark.parametrize("change,expected", [
    ({"again_when": "more needed"}, "Additional properties are not allowed ('again_when' was unexpected)"),
    ({"arguments": []}, "object"),
    ({"tool": None}, "string"),
])
def test_plan_schema_reports_nested_call_rule_without_plan_dump(plan_schema, change, expected):
    node = {"kind": "call", "tool": "read_inventory", "arguments": {}, **change}
    plan = {"language": "minebot-work/0.2", "goal": "x", "timeout_s": 10,
            "work": {"kind": "sequence", "intent": "x", "steps": [
                {"kind": "call", "tool": "read_state", "arguments": {}},
                {"kind": "call", "tool": "read_state", "arguments": {}}, node]}}
    with pytest.raises(WorkPlanError) as exc:
        validate_plan(plan)
    message = str(exc.value)
    assert "work.steps[2]" in message
    assert expected in message
    assert "read_inventory" not in message and "given schemas" not in message


def test_plan_schema_reports_missing_call_property(plan_schema):
    plan = {"language": "minebot-work/0.2", "goal": "x", "timeout_s": 10,
            "work": {"kind": "call", "intent": "x", "arguments": {}}}
    with pytest.raises(WorkPlanError, match="invalid plan at work: 'tool' is a required property"):
        validate_plan(plan)


def test_delegate_schema_rejection_reaches_the_model(plan_schema):
    from types import SimpleNamespace
    from minebot.app.reflex_tools import ReflexManager, register_reflex_tools
    from minebot.app.runner import _model_tool_payload
    from minebot.brain.registry import ToolRegistry

    plan = {"language": "minebot-work/0.2", "goal": "x", "timeout_s": 10,
            "work": {"kind": "call", "intent": "x", "tool": "read_inventory",
                     "arguments": {}, "again_when": "more needed"}}
    registry = ToolRegistry()
    register_reflex_tools(registry, ReflexManager(SimpleNamespace()))
    tool = registry.get("delegate_work")
    raw = tool.callable({"plan": plan})
    shown = _model_tool_payload(tool.name, raw, trace_ref="bad-plan", projector=tool.projector)
    assert shown["reason"] == "invalid_plan"
    assert shown["summary"]["error"] == (
        "invalid plan at work: Additional properties are not allowed ('again_when' was unexpected)")
    assert "metrics.error" not in shown["projection"]["omittedFields"]


def test_validate_accepts_repeat_and_plan_interrupt_with_condition_and_handler():
    base = {"language": "minebot-work/0.2", "goal": "x", "timeout_s": 10,
            "work": {"kind": "wait", "condition": {"text": "x"}, "timeout_s": 1}}
    validate_plan(base)
    repeated = {**base, "work": {**base["work"], "repeat": {"again_when": {"text": "x"}, "until": {"text": "y"}}}}
    validate_plan(repeated)
    interrupted = {**base, "interrupt_when": {"condition": {"text": "stop"},
        "handler": {"kind": "call", "intent": "Reach shelter", "tool": "move_to", "arguments": {}}}}
    validate_plan(interrupted)
    with pytest.raises(WorkPlanError, match="condition and handler|invalid plan"):
        validate_plan({**base, "interrupt_when": {"text": "stop"}})


def test_sequence_batch_and_ref_are_executed_and_annotated():
    plan = {"language": "minebot-work/0.2", "goal": "build", "timeout_s": 10,
            "work": {"kind": "sequence", "id": "root", "intent": "prepare then build", "steps": [
                {"kind": "call", "id": "prepare", "tool": "collect", "arguments": {"item": "stone"}},
                {"kind": "batch", "id": "place", "intent": "place all", "items": [{"x": 1}, {"x": 2},],
                 "body": {"kind": "call", "id": "place_one", "tool": "place", "arguments": {"at": {"ref": "item.x"}}}},
            ]}}
    calls = []
    result, decision = asyncio.run(run_plan(plan, calls=calls))
    assert result.status == "completed"
    assert [call[0] for call in calls] == ["collect", "place", "place"]
    assert [call[1] for call in calls[1:]] == [{"at": 1}, {"at": 2}]
    assert result.tree["work"]["_runtime"]["status"] == "done"
    call_questions = [p for p in decision.payloads if "finished_call" in p.get("state", {})]
    assert call_questions and call_questions[0]["state"]["agent_intent"] == "prepare then build"


def test_wait_unknown_is_waiting_and_does_not_call_body():
    plan = {"language": "minebot-work/0.2", "goal": "wait", "timeout_s": 10,
            "work": {"kind": "wait", "id": "arrival", "intent": "wait for player", "timeout_s": 3,
                      "condition": {"text": "player is here", "bindings": {"player": "qwait"}}}}
    result, decision = asyncio.run(run_plan(plan, answers=("unknown",), calls=[]))
    assert result.status == "waiting"
    assert result.tree["work"]["_runtime"]["status"] == "waiting"
    state = decision.payloads[0]["state"]
    assert state["condition"] == "player is here"
    # The player binding was not read: it is shown as an unread object.
    assert state["other_objects"]["qwait"]["observed"] is False
    assert state["unknown_fields"] == [{"object": "qwait", "reason": "no_observation"}]


def test_wait_condition_says_the_body_in_frame_sentences():
    # Villa run 2026-10-15: with the raw Body state (clock as a tick number)
    # "daytime has begun" was answered unknown 49 times in full daylight;
    # with these sentences the same recorded frames gave satisfied 48/48 and
    # night controls never did.
    async def state():
        return {"health": 20.0, "food": 20, "oxygen": 300, "time": 991, "pos": [-5.5, 72.0, 7.5],
                "inventory_counts": {}, "inventory_complete": True, "selected_item": "", "selected_item_known": True}
    plan = {"language": "minebot-work/0.2", "goal": "wait for day", "timeout_s": 10,
            "work": {"kind": "wait", "id": "day", "intent": "wait for day", "timeout_s": 3,
                     "condition": {"text": "Minecraft daytime has begun and the bot health is above 0"}}}
    decision = Decision("satisfied")
    result = asyncio.run(WorkPlanExecutor(plan, call=None, state=state, decision=decision).run())
    assert result.status == "completed"
    shown = decision.payloads[0]["state"]
    assert shown["condition"] == "Minecraft daytime has begun and the bot health is above 0"
    # The clock is said beside the sentence, not as raw state: the Agent
    # writes conditions in its numbers too (replay of track S 2026-10-20).
    assert shown["daylight"] == "It is day. The game clock reads 991 of 24000 ticks."
    assert shown["body"].startswith("Health is 20 of 20.")
    assert "facts" not in shown and "field_vocabulary" not in shown and "intent_chain" not in shown
    assert '"time"' not in json.dumps(shown)


def test_choice_and_requires_need_all_satisfied():
    plan = {"language": "minebot-work/0.2", "goal": "deliver", "timeout_s": 10,
            "work": {"kind": "choice", "id": "route", "intent": "choose route", "options": [
                {"id": "near", "intent": "near route", "work": {"kind": "call", "tool": "deliver", "arguments": {"route": "near"}}},
                {"id": "far", "intent": "far route", "work": {"kind": "call", "tool": "deliver", "arguments": {"route": "far"}}},
            ]}}
    calls = []
    result, _ = asyncio.run(run_plan(plan, answers=("near",), calls=calls))
    assert result.status == "completed" and calls[0][1] == {"route": "near"}


def test_call_completion_is_decision_owned_and_keeps_real_cancel_result():
    plan = {"language": "minebot-work/0.2", "goal": "collect", "timeout_s": 10,
            "work": {"kind": "call", "id": "collect", "tool": "collect", "arguments": {}}}
    calls = []

    async def call(tool, arguments, *, call_id, timeout_s):
        calls.append(call_id)
        return {"success": False, "reason": "cancelled", "metrics": {"real": True}}

    # A failed call cannot be passed over as done: only repeat or the Agent remain.
    decision = Decision("call_agent")
    result = asyncio.run(WorkPlanExecutor(plan, call=call, state=lambda: _state(), decision=decision).run())
    assert result.status == "needs_agent"
    assert result.results["collect"]["success"] is False
    assert "next_step" not in decision.payloads[0]["questions"]["q"]["criteria"]
    assert result.tree["work"]["_runtime"]["decision"]["choice"] == "call_agent"
    assert calls == ["collect"]
    assert decision.payloads[0]["state"]["task"] == "collect"


def test_repeat_is_decision_driven_and_each_reentry_gets_a_new_call_id():
    plan = {"language": "minebot-work/0.2", "goal": "collect enough", "timeout_s": 10,
            "work": {"kind": "call", "id": "collect", "tool": "collect", "arguments": {},
                     "repeat": {"again_when": "more is needed", "until": "enough is present"}}}
    calls = []
    decision = Decision("repeat", "repeat", "step_done")
    async def call(tool, arguments, *, call_id, timeout_s):
        calls.append(call_id)
        return {"success": True, "reason": "ok", "metrics": {}}
    result = asyncio.run(WorkPlanExecutor(plan, call=call, state=lambda: _state(), decision=decision).run())
    assert result.status == "completed"
    assert calls == ["collect", "collect__repeat_1", "collect__repeat_2"]
    assert "more is needed" in decision.payloads[0]["questions"]["q"]["criteria"]["repeat"]


def test_repeating_a_call_stops_when_the_plan_time_is_up():
    # life-stage b2: a 240 s plan repeated an instant go_to_surface failure for
    # 16 minutes; re-entering the call never looked at the plan deadline.
    import time

    plan = {"language": "minebot-work/0.2", "goal": "get out", "timeout_s": 0.2,
            "work": {"kind": "call", "id": "up", "tool": "go_to_surface", "arguments": {},
                     "intent": "standing at y 70 or higher"}}
    calls = []

    class AlwaysRepeat(Decision):
        async def ask(self, payload, *, priority=None):
            self.payloads.append(payload)
            return type("Answer", (), {"choice": "repeat", "raw": {"choice": "repeat"}})()

    async def call(tool, arguments, *, call_id, timeout_s):
        calls.append(call_id)
        await asyncio.sleep(0.02)
        return {"success": False, "reason": "surface_not_found", "canRetry": True, "metrics": {}}

    started = time.monotonic()
    result = asyncio.run(WorkPlanExecutor(plan, call=call, state=lambda: _state(), decision=AlwaysRepeat()).run())
    assert result.status == "blocked"
    assert time.monotonic() - started < 2
    assert 2 <= len(calls) < 50


def test_a_failed_call_that_changed_nothing_is_not_offered_again():
    # 873 identical go_to_surface failures in life-stage b2: the Body was in
    # the same block with the same inventory before and after every attempt.
    plan = {"language": "minebot-work/0.2", "goal": "get out", "timeout_s": 5,
            "work": {"kind": "call", "id": "up", "tool": "go_to_surface", "arguments": {},
                     "intent": "standing at y 70 or higher"}}
    calls = []

    class AlwaysRepeat(Decision):
        async def ask(self, payload, *, priority=None):
            self.payloads.append(payload)
            return type("Answer", (), {"choice": "repeat", "raw": {"choice": "repeat"}})()

    async def call(tool, arguments, *, call_id, timeout_s):
        calls.append(call_id)
        return {"success": False, "reason": "surface_not_found", "canRetry": True, "metrics": {}}

    async def same_state():
        return {"position": [171.3, 64.0, 165.4], "inventory": {"cobblestone": 20}, "selected_item": "stone_pickaxe"}

    decision = AlwaysRepeat()
    result = asyncio.run(WorkPlanExecutor(plan, call=call, state=same_state, decision=decision).run())
    # Without "run it again" the only thing left is handing back to the Agent.
    assert calls == ["up"]
    assert result.status == "needs_agent"


def test_a_successful_call_that_changed_nothing_is_not_run_again():
    plan = {"language": "minebot-work/0.2", "goal": "dig", "timeout_s": 5,
            "work": {"kind": "sequence", "intent": "dig the step", "steps": [
                {"kind": "call", "id": "walk", "tool": "move_to", "arguments": {"pos": [36, 35, 675]},
                 "intent": "standing at x 36, z 675 with feet at y 35"},
                {"kind": "call", "id": "dig", "tool": "clear_area", "arguments": {}, "intent": "cells are air"},
            ]}}
    calls = []

    class PrefersRepeat(Decision):
        async def ask(self, payload, *, priority=None):
            self.payloads.append(payload)
            options = payload["questions"]["q"]["criteria"]
            choice = "repeat" if "repeat" in options else "next_step" if "next_step" in options else "step_done"
            return type("Answer", (), {"choice": choice, "raw": {"choice": choice}})()

    async def call(tool, arguments, *, call_id, timeout_s):
        calls.append(call_id)
        return {"success": True, "reason": "arrived", "canRetry": False, "metrics": {}}

    async def same_state():
        return {"position": [36.5, 35.0, 675.7], "inventory": {"cobblestone": 51}, "selected_item": "stone_pickaxe"}

    asyncio.run(WorkPlanExecutor(plan, call=call, state=same_state, decision=PrefersRepeat()).run())
    assert calls == ["walk", "dig"]


async def _cancelled_call(calls, call_id):
    calls.append(call_id)
    return {"success": False, "reason": "cancelled", "metrics": {}}


def test_unknown_call_decision_does_not_advance_or_write_again():
    plan = {"language": "minebot-work/0.2", "goal": "collect", "timeout_s": 10,
            "work": {"kind": "call", "id": "collect", "tool": "collect", "arguments": {}}}
    calls = []
    executor = WorkPlanExecutor(
        plan,
        call=lambda tool, arguments, *, call_id, timeout_s: _cancelled_call(calls, call_id),
        state=lambda: _state(),
        decision=Decision("unknown"),
    )
    first = asyncio.run(executor.run())
    assert first.status == "waiting" and calls == ["collect"]
    assert first.tree["work"]["_runtime"]["decision_pending"] is True


def test_unknown_call_waits_for_the_single_body_loop_without_a_second_call():
    plan = {"language": "minebot-work/0.2", "goal": "collect", "timeout_s": 10,
            "work": {"kind": "call", "id": "collect", "tool": "collect", "arguments": {}}}
    calls = []
    decisions = Decision("unknown")

    async def call(tool, arguments, *, call_id, timeout_s):
        calls.append(call_id)
        return {"success": False, "reason": "cancelled", "metrics": {}}

    result = asyncio.run(WorkPlanExecutor(
        plan, call=call, state=lambda: _state(), decision=decisions,
    ).run())
    assert result.status == "waiting"
    assert calls == ["collect"]




def test_runtime_choice_provider_supplies_options():
    plan = {
        "language": "minebot-work/0.2", "goal": "choose", "timeout_s": 10,
        "work": {"kind": "choice", "id": "route", "intent": "choose route", "source": "runtime"},
    }
    calls = []
    decision = Decision("safe")

    async def call(tool, arguments, *, call_id, timeout_s):
        calls.append((tool, arguments))
        return {"success": True, "reason": "ok", "metrics": {}}

    def provider(node, *, intents, state, item):
        assert node["source"] == "runtime"
        assert intents == ["choose route"]
        return [{"id": "safe", "intent": "safe route", "work": {
            "kind": "call", "tool": "deliver", "arguments": {"route": "safe"},
        }}]

    executor = WorkPlanExecutor(
        plan, call=call, state=lambda: _state(), decision=decision,
        choice_provider=provider,
    )
    result = asyncio.run(executor.run())
    assert result.status == "completed"
    assert calls == [("deliver", {"route": "safe"})]


def test_requires_pending_wins_and_does_not_release_body_write():
    plan = {"language": "minebot-work/0.2", "goal": "deliver", "timeout_s": 10,
            "work": {"kind": "call", "id": "deliver", "tool": "give", "arguments": {"count": 2},
                      "when": {"text": "ready", "requires": [
                          {"text": "material is present", "about": "inventory"},
                          {"text": "player is ready", "about": "player"},
                      ]}}}
    calls = []
    result, decision = asyncio.run(run_plan(plan, answers=("satisfied", "pending"), calls=calls))
    assert result.status == "waiting"
    assert calls == []
    assert len(decision.payloads) == 2


def test_condition_request_says_the_bound_body_in_sentences():
    plan = {"language": "minebot-work/0.2", "goal": "observe", "timeout_s": 2,
            "work": {"kind": "wait", "id": "ready", "intent": "wait", "timeout_s": 1,
                      "condition": {"text": "the bot is ready", "bindings": {"bot": "bot"}}}}
    state = BodyState(
        bot="MineBot", pos=(1.0, 64.0, 2.0), yaw=None, pitch=None, health=20.0,
        food=20, oxygen=None, inventory_raw="[]", inventory_hash="h", effects=None,
        time=10, weather=None, dimension="overworld", complete=True,
        inventory_counts={"minecraft:stone": 4}, inventory_complete=True,
        selected_item_known=True, selected_item=None,
    )
    decision = Decision("unknown")
    async def state_reader():
        return state
    executor = WorkPlanExecutor(plan, call=lambda *args, **kwargs: None,
                                state=state_reader, decision=decision)
    result = asyncio.run(executor.run())
    payload = decision.payloads[0]["state"]
    assert result.status == "waiting"
    assert "stone 4" in payload["inventory"]
    assert payload["daylight"] == "It is day. The game clock reads 10 of 24000 ticks."
    assert payload["position"] == "now at [1.0, 64.0, 2.0]."
    assert "facts" not in payload and "field_vocabulary" not in payload and "unknown_fields" not in payload


def test_changed_obligation_with_same_id_cannot_inherit_done():
    first_plan = {"language": "minebot-work/0.2", "goal": "give", "timeout_s": 2,
                  "work": {"kind": "call", "id": "give", "tool": "give",
                            "arguments": {"count": 2}}}
    revised_plan = {"language": "minebot-work/0.2", "goal": "give", "timeout_s": 2,
                    "work": {"kind": "call", "id": "give", "tool": "give",
                              "arguments": {"count": 10}}}
    calls = []
    async def call(tool, arguments, *, call_id, timeout_s):
        calls.append(dict(arguments))
        return {"success": True, "reason": "done", "metrics": {}}
    records = {}
    first = asyncio.run(WorkPlanExecutor(first_plan, call=call, decision=Decision("step_done"), records=records).run())
    second = asyncio.run(WorkPlanExecutor(revised_plan, call=call, decision=Decision("step_done"), records=records).run())
    assert first.status == second.status == "completed"
    assert calls == [{"count": 2}, {"count": 10}]


def test_inserting_unnamed_node_does_not_replay_unchanged_siblings():
    def plan(steps):
        return {"language": "minebot-work/0.2", "goal": "sequence", "timeout_s": 2,
                "work": {"kind": "sequence", "intent": "run", "steps": steps}}
    first_steps = [
        {"kind": "call", "tool": "a", "arguments": {}},
        {"kind": "call", "tool": "b", "arguments": {}},
    ]
    revised_steps = [
        {"kind": "call", "tool": "new", "arguments": {}},
        *first_steps,
    ]
    calls = []
    async def call(tool, arguments, *, call_id, timeout_s):
        calls.append(tool)
        return {"success": True, "reason": "done", "metrics": {}}
    records = {}
    asyncio.run(WorkPlanExecutor(plan(first_steps), call=call, records=records, decision=Decision("step_done", "step_done", "step_done")).run())
    asyncio.run(WorkPlanExecutor(plan(revised_steps), call=call, records=records, decision=Decision("step_done", "step_done", "step_done")).run())
    assert calls == ["a", "b", "new"]


def test_revision_reuses_caller_owned_records_and_skips_done_nodes():
    first_plan = {
        "language": "minebot-work/0.2", "goal": "build", "timeout_s": 10,
        "work": {"kind": "sequence", "id": "root", "intent": "build", "steps": [
            {"kind": "call", "id": "done", "tool": "place", "arguments": {"at": 1}},
            {"kind": "call", "id": "blocked", "tool": "blocked", "arguments": {"at": 2}},
        ]},
    }
    revised_plan = {
        **first_plan,
        "work": {**first_plan["work"], "steps": [
            first_plan["work"]["steps"][0],
            {"kind": "call", "id": "blocked", "tool": "place", "arguments": {"at": 2}},
        ]},
    }
    calls = []
    records = {}

    async def call(tool, arguments, *, call_id, timeout_s):
        calls.append(tool)
        return {"success": tool != "blocked", "reason": "blocked" if tool == "blocked" else "done", "metrics": {}}

    decision = Decision("step_done", "call_agent", "step_done")
    first = asyncio.run(WorkPlanExecutor(first_plan, call=call, decision=decision, records=records).run())
    second = asyncio.run(WorkPlanExecutor(revised_plan, call=call, decision=decision, records=records).run())
    assert first.status == "needs_agent"
    assert second.status == "completed"
    assert calls == ["place", "blocked", "place"]
    assert records["done"].status == "done"


def test_choice_keeps_selected_branch_while_a_child_is_waiting():
    plan = {"language": "minebot-work/0.2", "goal": "choose", "timeout_s": 10,
            "work": {"kind": "choice", "id": "route", "intent": "choose a route", "options": [
                {"id": "a", "intent": "route a", "work": {"kind": "call", "id": "a_call", "tool": "a", "arguments": {}}},
                {"id": "b", "intent": "route b", "work": {"kind": "call", "id": "b_call", "tool": "b", "arguments": {}}},
            ]}}
    calls = []

    async def call(tool, arguments, *, call_id, timeout_s):
        calls.append(tool)
        return {"success": True, "reason": "done", "metrics": {}}

    class Answers(Decision):
        pass

    decision = Decision("a", "unknown", "unknown")
    executor = WorkPlanExecutor(plan, call=call, state=lambda: _state(), decision=decision)
    first = asyncio.run(executor.run())
    second = asyncio.run(executor.run())
    assert first.status == second.status == "waiting"
    assert calls == ["a"]
    assert len([p for p in decision.payloads if p["questions"]["q"].get("criteria", {}).get("a")]) == 1
    assert second.tree["work"]["_runtime"]["choice_id"] == "a"


def test_repeat_is_iterative_and_retains_only_recent_invocations():
    plan = {"language": "minebot-work/0.2", "goal": "collect", "timeout_s": 30,
            "work": {"kind": "call", "id": "collect", "tool": "collect", "arguments": {}}}
    calls = []
    decision = Decision(*(["repeat"] * 500 + ["step_done"]))

    async def call(tool, arguments, *, call_id, timeout_s):
        calls.append(call_id)
        return {"success": True, "reason": "ok", "metrics": {}}

    executor = WorkPlanExecutor(plan, call=call, state=lambda: _state(), decision=decision)
    result = asyncio.run(executor.run())
    assert result.status == "completed"
    assert len(calls) == 501 and len(set(calls)) == 501
    assert executor.records["collect"].repeat_count == 500
    assert len([key for key in executor.records if key.startswith("collect__repeat_")]) == 3


def test_unknown_streak_calls_agent_without_a_permanent_question_silence():
    plan = {"language": "minebot-work/0.2", "goal": "collect", "timeout_s": 10,
            "work": {"kind": "call", "id": "collect", "tool": "collect", "arguments": {}}}
    decision = Decision("unknown", "unknown", "unknown")
    executor = WorkPlanExecutor(
        plan, call=lambda *args, **kwargs: _cancelled_call([], "collect"),
        state=lambda: _state(), decision=decision,
    )
    assert asyncio.run(executor.run()).status == "waiting"
    assert asyncio.run(executor.run()).status == "waiting"
    result = asyncio.run(executor.run())
    assert result.status == "needs_agent"
    assert result.tree["work"]["_runtime"]["needs_agent"]["reason"] == "decision_unknown_limit"


def test_repeat_is_available_as_a_base_call_option_without_agent_repeat_metadata():
    plan = {"language": "minebot-work/0.2", "goal": "collect", "timeout_s": 10,
            "work": {"kind": "call", "id": "collect", "tool": "collect", "arguments": {}}}
    decision = Decision("repeat", "step_done")
    calls = []

    async def call(tool, arguments, *, call_id, timeout_s):
        calls.append(call_id)
        return {"success": True, "reason": "ok", "metrics": {}}

    result = asyncio.run(WorkPlanExecutor(plan, call=call, state=lambda: _state(), decision=decision).run())
    assert result.status == "completed"
    assert calls == ["collect", "collect__repeat_1"]
    assert "same call" in decision.payloads[0]["questions"]["q"]["criteria"]["repeat"]


def test_result_question_contains_recent_raw_history_without_running_execution_fields():
    plan = {"language": "minebot-work/0.2", "goal": "collect", "timeout_s": 10,
            "work": {"kind": "call", "id": "collect", "tool": "collect", "arguments": {}}}
    decision = Decision("repeat", "repeat", "call_agent")
    state_number = 0
    calls = []

    async def state():
        nonlocal state_number
        state_number += 1
        return {"position": [state_number, 64, 0], "inventory": {"stone": state_number}}

    async def call(tool, arguments, *, call_id, timeout_s):
        calls.append(call_id)
        return {"success": False, "reason": "same_failure", "canRetry": True}

    result = asyncio.run(WorkPlanExecutor(plan, call=call, state=state, decision=decision).run())
    assert result.status == "needs_agent"
    payload = decision.payloads[-1]
    history = payload["state"].get("earlier_attempts_of_this_call", [])
    assert len(history) == 2  # the two earlier attempts; the current one is finished_call
    assert "same_failure" in json.dumps(history, ensure_ascii=False)
    assert "current_work" not in payload["state"]
    assert "elapsed" not in json.dumps(payload, ensure_ascii=False).lower()


def test_node_history_uses_native_state_fields_from_recorded_crafting_attempts():
    """Stage-d table failed, then succeeded with a changed inventory."""
    source = Path(__file__).parents[1] / "fixtures" / "reflex_step2_plan_result_frames.json"
    cases = {case["seq"]: case for case in json.loads(source.read_text())['cases']}
    attempts = [cases[1523]["raw_decision_facts"], cases[1538]["raw_decision_facts"]]
    state = copy.deepcopy(attempts[0]["body_state"])
    # The first attempt moved items, as a craft does; a repeat is offered only
    # after the Body changed (an identical failed attempt is not run again).
    state["inventory_counts"] = {**state["inventory_counts"], "oak_planks": state["inventory_counts"].get("oak_planks", 0) + 4}
    decision = Decision("repeat", "step_done")
    calls = []

    async def native_state():
        return copy.deepcopy(state)

    async def call(tool, arguments, *, call_id, timeout_s):
        nonlocal state
        attempt = attempts[len(calls)]
        calls.append(call_id)
        state = copy.deepcopy(attempt["body_state"])
        return copy.deepcopy(attempt["tool_result"])

    node = copy.deepcopy(cases[1523]["node"])
    node.pop("next_call", None)
    node["intent"] = cases[1523]["intents"][-1]
    plan = {"language": "minebot-work/0.2", "goal": "make the crafting table",
            "timeout_s": 10, "work": node}
    executor = WorkPlanExecutor(plan, call=call, state=native_state, decision=decision)
    outcome = asyncio.run(executor.run())
    assert outcome.status == "completed"
    history = executor._call_histories["table"]
    assert history[0]["body"]["position"] == attempts[0]["body_state"]["pos"]
    assert history[0]["body"]["inventory"] == attempts[0]["body_state"]["inventory_counts"]
    assert history[1]["body"]["inventory"] == attempts[1]["body_state"]["inventory_counts"]
    assert decision.payloads[-1]["state"]["earlier_attempts_of_this_call"] == [
        "Attempt 1: failure: craft_recipe_failed:recipe_output_mismatch; inventory or position changed since."
    ]


def test_coming_back_to_a_handed_back_success_judges_it_without_running_it_again():
    """Review 2026-10-17: a node Decision handed to the Agent after its call
    succeeded was rerun as new when the plan went on, repeating the build."""
    plan = {"language": "minebot-work/0.2", "goal": "build", "timeout_s": 10,
            "work": {"kind": "call", "id": "floor", "tool": "build_structure", "intent": "floor built",
                     "arguments": {"size": [16, 1, 16]}}}
    calls = []
    records = {}

    async def call(tool, arguments, *, call_id, timeout_s):
        calls.append(call_id)
        return {"success": True, "reason": "structure_verified", "metrics": {}}

    first = asyncio.run(WorkPlanExecutor(plan, call=call, decision=Decision("call_agent"), records=records).run())
    assert first.status == "needs_agent"
    assert records["floor"].result["success"] is True
    assert records["floor"].needs_agent["reason"] == "decision_call_agent"

    second = asyncio.run(WorkPlanExecutor(plan, call=call, decision=Decision("step_done"), records=records).run())
    assert second.status == "completed"
    assert calls == ["floor"]


def test_only_a_fresh_repeat_runs_a_handed_back_call_again():
    plan = {"language": "minebot-work/0.2", "goal": "logs", "timeout_s": 10,
            "work": {"kind": "call", "id": "logs", "tool": "collect", "intent": "at least 4 oak_log in the inventory",
                     "arguments": {"count": 4}}}
    calls = []
    records = {}

    async def call(tool, arguments, *, call_id, timeout_s):
        calls.append(call_id)
        return {"success": False, "reason": "no_path", "canRetry": True, "metrics": {}}

    asyncio.run(WorkPlanExecutor(plan, call=call, decision=Decision("call_agent"), records=records).run())
    again = asyncio.run(WorkPlanExecutor(plan, call=call, decision=Decision("repeat", "call_agent"),
                                         records=records).run())
    assert again.status == "needs_agent"
    assert calls == ["logs", "logs__repeat_1"]


def test_coming_back_resumes_the_repeat_the_node_was_on():
    """Review 2026-10-17: a handoff on build__repeat_1 left build active, and
    coming back ran build again before any Decision."""
    plan = {"language": "minebot-work/0.2", "goal": "build", "timeout_s": 10,
            "work": {"kind": "call", "id": "build", "tool": "build_structure", "intent": "wall built",
                     "arguments": {"size": [4, 1, 1]}}}
    calls = []
    records = {}

    async def call(tool, arguments, *, call_id, timeout_s):
        calls.append(call_id)
        return {"success": True, "reason": "structure_verified", "metrics": {}}

    first = asyncio.run(WorkPlanExecutor(plan, call=call, decision=Decision("repeat", "call_agent"),
                                         records=records).run())
    assert first.status == "needs_agent"
    second = asyncio.run(WorkPlanExecutor(plan, call=call, decision=Decision("step_done"), records=records).run())
    assert second.status == "completed"
    assert calls == ["build", "build__repeat_1"]


def test_a_later_step_can_still_refer_to_a_result_judged_on_coming_back():
    plan = {"language": "minebot-work/0.2", "goal": "use a reading", "timeout_s": 10,
            "work": {"kind": "sequence", "id": "root", "intent": "reading used", "steps": [
                {"kind": "call", "id": "read", "tool": "read_state", "arguments": {}},
                {"kind": "call", "id": "use", "tool": "move_to", "intent": "moved",
                 "arguments": {"x": {"ref": "result.read.metrics.x"}}},
            ]}}
    calls = []
    records = {}

    async def call(tool, arguments, *, call_id, timeout_s):
        calls.append((call_id, arguments))
        return {"success": True, "reason": "ok", "metrics": {"x": 7}}

    asyncio.run(WorkPlanExecutor(plan, call=call, decision=Decision("call_agent"), records=records).run())
    result = asyncio.run(WorkPlanExecutor(plan, call=call, decision=Decision("step_done", "step_done"),
                                          records=records).run())
    assert result.status == "completed"
    assert calls == [("read", {}), ("use", {"x": 7})]


def test_a_reported_step_lets_the_plan_go_on_and_the_plan_completes():
    plan = {"language": "minebot-work/0.2", "goal": "windows", "timeout_s": 5,
            "work": {"kind": "sequence", "intent": "glass in both windows", "steps": [
                {"kind": "call", "id": "south", "tool": "place_block", "arguments": {}, "intent": "glass south"},
                {"kind": "call", "id": "west", "tool": "place_block", "arguments": {}, "intent": "glass west"},
            ]}}
    calls, reports = [], []

    class GoOn(Decision):
        async def ask(self, payload, *, priority=None):
            self.payloads.append(payload)
            options = payload["questions"]["q"]["criteria"]
            choice = "report_and_go_on" if "report_and_go_on" in options else "next_step"
            return type("Answer", (), {"choice": choice, "raw": {"choice": choice}})()

    async def call(tool, arguments, *, call_id, timeout_s):
        calls.append(call_id)
        if call_id == "south":
            return {"success": False, "reason": "place_denied:target_occupied", "canRetry": False, "metrics": {}}
        return {"success": True, "reason": "placed", "metrics": {}}

    result = asyncio.run(WorkPlanExecutor(plan, call=call, state=_state, decision=GoOn(),
                                          report=reports.append).run())
    assert calls == ["south", "west"]
    assert [report["node"] for report in reports] == ["south"]
    assert result.status == "completed" and result.facts["reported_steps"] == ["south"]
    assert result.tree["work"]["steps"][0]["_runtime"]["status"] == "reported"
    assert result.tree["work"]["steps"][1]["_runtime"]["status"] == "done"
