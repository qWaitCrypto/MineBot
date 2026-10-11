import asyncio

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


def test_validate_accepts_repeat_but_keeps_interrupt_deferred():
    base = {"language": "minebot-work/0.2", "goal": "x", "timeout_s": 10,
            "work": {"kind": "wait", "condition": {"text": "x"}, "timeout_s": 1}}
    validate_plan(base)
    repeated = {**base, "work": {**base["work"], "repeat": {"again_when": {"text": "x"}, "until": {"text": "y"}}}}
    validate_plan(repeated)
    interrupted = {**base, "work": {**base["work"], "interrupt_when": {"text": "stop"}}}
    with pytest.raises(WorkPlanError, match="unsupported_in_slice2"):
        validate_plan(interrupted)


def test_sequence_batch_and_ref_are_executed_and_annotated():
    plan = {"language": "minebot-work/0.2", "goal": "build", "timeout_s": 10,
            "work": {"kind": "sequence", "id": "root", "intent": "prepare then build", "steps": [
                {"kind": "call", "id": "prepare", "tool": "collect", "arguments": {"item": "stone"}},
                {"kind": "batch", "id": "place", "intent": "place all", "items": [{"x": 1}, {"x": 2},],
                 "body": {"kind": "call", "id": "place_one", "tool": "place", "arguments": {"at": {"ref": "item.x"}}}},
            ]}}
    calls = []
    result, _ = asyncio.run(run_plan(plan, calls=calls))
    assert result.status == "completed"
    assert [call[0] for call in calls] == ["collect", "place", "place"]
    assert [call[1] for call in calls[1:]] == [{"at": 1}, {"at": 2}]
    assert result.tree["work"]["_runtime"]["status"] == "done"


def test_wait_unknown_is_waiting_and_does_not_call_body():
    plan = {"language": "minebot-work/0.2", "goal": "wait", "timeout_s": 10,
            "work": {"kind": "wait", "id": "arrival", "intent": "wait for player", "timeout_s": 3,
                      "condition": {"text": "player is here", "bindings": {"player": "qwait"}}}}
    result, decision = asyncio.run(run_plan(plan, answers=("unknown",), calls=[]))
    assert result.status == "waiting"
    assert result.tree["work"]["_runtime"]["status"] == "waiting"
    assert decision.payloads[0]["state"]["intent_chain"] == ["wait for player"]


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

    decision = Decision("step_done")
    result = asyncio.run(WorkPlanExecutor(plan, call=call, state=lambda: _state(), decision=decision).run())
    assert result.status == "completed"
    assert result.results["collect"]["success"] is False
    assert result.tree["work"]["_runtime"]["decision"]["choice"] == "step_done"
    assert calls == ["collect"]
    assert decision.payloads[0]["state"]["plan"]["goal"] == "collect"


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
    assert "again_when" in decision.payloads[0]["questions"]["q"]["criteria"]["repeat"]


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


def test_unknown_call_can_be_resolved_by_the_idle_supervisor_without_a_second_call():
    plan = {"language": "minebot-work/0.2", "goal": "collect", "timeout_s": 10,
            "work": {"kind": "call", "id": "collect", "tool": "collect", "arguments": {}}}
    calls = []
    decisions = Decision("unknown")
    idle_requests = []

    async def call(tool, arguments, *, call_id, timeout_s):
        calls.append(call_id)
        return {"success": False, "reason": "cancelled", "metrics": {}}

    async def idle(tool, *, call_id, plan_context, options):
        idle_requests.append((tool, call_id, plan_context, options))
        return {"choice": "step_done", "questions": 1, "unknown": 0}

    result = asyncio.run(WorkPlanExecutor(
        plan, call=call, state=lambda: _state(), decision=decisions,
        idle_supervise=idle,
    ).run())
    assert result.status == "completed"
    assert calls == ["collect"]
    assert idle_requests and "step_done" in idle_requests[0][3]


def test_inspect_is_one_bounded_read_before_the_next_decision():
    plan = {"language": "minebot-work/0.2", "goal": "inspect", "timeout_s": 10,
            "work": {"kind": "call", "id": "inspect", "tool": "read", "arguments": {}}}
    reads = []
    decision = Decision("inspect", "step_done")

    async def state():
        reads.append(True)
        return {"bot": {"observed": True, "position": [len(reads), 64, 0]}}

    async def call(tool, arguments, *, call_id, timeout_s):
        return {"success": True, "reason": "read", "metrics": {}}

    result = asyncio.run(WorkPlanExecutor(plan, call=call, state=state, decision=decision).run())
    assert result.status == "completed"
    assert len(reads) == 3  # argument facts, post-call facts, one bounded inspect read
    assert len(decision.payloads) == 2


async def _cancelled_call(calls, call_id):
    calls.append(call_id)
    return {"success": False, "reason": "cancelled", "metrics": {}}


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


def test_condition_request_contains_body_state_facts_and_vocabulary():
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
    assert payload["facts"]["bot"]["observed"] is True
    assert payload["facts"]["bot"]["values"]["inventory_counts"] == {"minecraft:stone": 4}
    assert "inventory_complete" in payload["field_vocabulary"]
    assert payload["unknown_fields"] == []


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
    asyncio.run(WorkPlanExecutor(plan(first_steps), call=call, records=records).run())
    asyncio.run(WorkPlanExecutor(plan(revised_steps), call=call, records=records).run())
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
    assert first.status == "blocked"
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
    assert result.status == "blocked"
    assert result.tree["work"]["_runtime"]["blocked"]["reason"] == "decision_unknown_limit"


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
    assert "same state.tool call" in decision.payloads[0]["questions"]["q"]["criteria"]["repeat"]
