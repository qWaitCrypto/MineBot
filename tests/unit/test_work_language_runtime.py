import asyncio

import pytest

from minebot.app.work_runtime import WorkPlanError, WorkPlanExecutor, validate_plan


class Decision:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.payloads = []

    async def ask(self, payload, *, priority=None):
        self.payloads.append(payload)
        return type("Answer", (), {"choice": self.answers.pop(0), "raw": {"choice": self.answers[0] if self.answers else None}})()


async def run_plan(plan, answers=("satisfied",), calls=None):
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


def test_validate_rejects_deferred_repeat_and_interrupt():
    base = {"language": "minebot-work/0.2", "goal": "x", "timeout_s": 10,
            "work": {"kind": "wait", "condition": {"text": "x"}, "timeout_s": 1}}
    validate_plan(base)
    repeated = {**base, "work": {**base["work"], "repeat": {"again_when": {"text": "x"}, "until": {"text": "y"}}}}
    with pytest.raises(WorkPlanError, match="unsupported_in_slice2"):
        validate_plan(repeated)


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

    first = asyncio.run(WorkPlanExecutor(first_plan, call=call, records=records).run())
    second = asyncio.run(WorkPlanExecutor(revised_plan, call=call, records=records).run())
    assert first.status == "blocked"
    assert second.status == "completed"
    assert calls == ["place", "blocked", "place"]
    assert records["done"].status == "done"
