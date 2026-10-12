import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from minebot.app.body_loop import BodyLoop
from minebot.app.decision_client import DecisionAnswer
from minebot.app.direct_call_supervision import CallSupervisor
from minebot.app import reflex_question as rq
from minebot.app.work_runtime import WorkPlanExecutor


class _Trace:
    def __init__(self):
        self.events = []

    def emit(self, event, **payload):
        self.events.append({"event": event, **payload})


class _Decision:
    def __init__(self):
        self.requests = []

    async def ask(self, request, *, priority=None):
        self.requests.append(request)
        return DecisionAnswer(
            "stay", {"answers": {"q": {"choice": "stay"}}}, 0.001, len(self.requests)
        )

    async def close(self):
        return None


def _runtime():
    trace = _Trace()
    decision = _Decision()
    return SimpleNamespace(
        trace=trace,
        decision_client=decision,
        last_known_body_state={
            "health": 20,
            "food": 20,
            "oxygen": 300,
            "position": [1, 64, 2],
            "inventory": {},
            "entities": [],
        },
        execution_snapshots=SimpleNamespace(current=lambda: None),
    ), trace, decision


def test_body_loop_lifecycle_emits_idle_frames_without_a_running_call(monkeypatch):
    async def run():
        runtime, trace, decision = _runtime()
        monkeypatch.setattr("minebot.app.body_loop.FACT_FRAME_INTERVAL_S", 0.001)
        loop = BodyLoop(runtime)
        await loop.start()
        await asyncio.sleep(0.01)
        await loop.stop()
        assert any(item["event"] == "reflex_loop_started" for item in trace.events)
        assert any(item["event"] == "reflex_loop_stopped" for item in trace.events)
        requests = [item for item in trace.events if item["event"] == "call_supervision_request"]
        assert requests and requests[0]["purpose"] == "idle_frame"
        assert decision.requests

    asyncio.run(run())


def test_idle_and_running_frames_share_body_fact_keys():
    runtime, _trace, _decision = _runtime()
    loop = BodyLoop(runtime)
    facts = runtime.last_known_body_state
    idle = loop._idle_question(facts, loop._idle_options(facts))
    running = rq.frame_state(
        goal="", intent="wait", tool="move_to", params={}, facts=facts,
        running=True, phase="moving", elapsed_s=1.0,
    )
    assert {"body", "threats", "inventory"} <= set(idle["state"])
    assert {"body", "threats", "inventory"} <= set(running)


def test_reflex_experiences_are_bounded_and_available_to_live_context():
    runtime, _trace, _decision = _runtime()
    loop = BodyLoop(runtime)
    for index in range(7):
        loop.record_experience(f"experience {index}")
    assert list(loop.recent_experiences) == [f"experience {index}" for index in range(2, 7)]


def test_idle_protection_is_executed_once_through_life_claim():
    async def run():
        runtime, trace, _decision = _runtime()
        calls = []

        class Tool:
            name = "go_to_surface"

        runtime.registry = SimpleNamespace(get=lambda name: Tool())
        runtime.weld_context = None

        async def run_tool(*args, **kwargs):
            calls.append(kwargs)
            return {"success": True, "reason": "surface_reached"}

        runtime.run_tool = run_tool
        loop = BodyLoop(runtime)
        await loop._run_idle_protection("surface", {"oxygen": 100, "position": [1, 60, 2]})
        assert calls and calls[0]["claim_holder"] == "life"
        assert calls[0]["supervision_context"]["is_reflex_action"] is True
        assert calls[0]["supervision_context"]["reflex_action_choice"] == "surface"
        assert any(e["event"] == "call_supervision_default_finished" for e in trace.events)

    asyncio.run(run())


def test_reflex_action_cannot_offer_the_same_protective_action_again():
    runtime, _trace, _decision = _runtime()
    record = {
        "tool": "move_to",
        "params": {},
        "supervision_context": {
            "is_reflex_action": True,
            "reflex_action_choice": "surface",
            "intent": "swim up to air",
        },
    }
    options = CallSupervisor._options_for(
        SimpleNamespace(name="go_to_surface"), record,
        execution={"state": "running", "facts": {"oxygen": 100}},
    )
    assert "surface" not in options
    assert "keep_going" in options


def test_body_loop_state_changes_are_reported_with_standard_fields():
    runtime, trace, _decision = _runtime()
    loop = BodyLoop(runtime)
    loop._set_state("survival", "decision")
    assert loop.loop_state == "survival"
    event = next(item for item in trace.events if item["event"] == "reflex_state_changed")
    assert event["from"] == "idle"
    assert event["to"] == "survival"
    assert event["cause"] == "decision"


def test_plan_result_uses_the_same_body_loop_judge_once():
    async def run():
        calls = []

        async def physical_call(tool, arguments, *, call_id, timeout_s):
            return {"success": True, "reason": "ended"}

        async def result_judge(**kwargs):
            calls.append(kwargs)
            return "next_step"

        executor = WorkPlanExecutor(
            {
                "language": "minebot-work/0.2",
                "goal": "make one tool",
                "timeout_s": 30,
                "work": {
                    "kind": "call", "id": "one", "tool": "move_to",
                    "intent": "reach the requested place", "arguments": {"x": 1, "y": 2, "z": 3},
                },
            },
            call=physical_call,
            result_judge=result_judge,
        )
        outcome = await executor.run()
        assert outcome.status == "completed"
        assert len(calls) == 1
        assert calls[0]["call_id"] == "one"

    asyncio.run(run())


@pytest.mark.skipif(
    not Path("/home/qwait/MineBot-reflex/logs/reflex-stage-20261012d/trace.jsonl").exists(),
    reason="recorded Stage-2 trace is not available on this host",
)
def test_stage2_replay_baseline_builds_all_recorded_frames_without_network():
    """Pin the offline corpus before changing the loop ownership.

    The test deliberately never calls DecisionClient.  It consumes raw
    body_progress facts and proves that every recorded running frame still
    compiles through the production question builder; result frames remain in
    the corpus count so the later result-path move cannot silently drop them.
    """
    path = Path("/home/qwait/MineBot-reflex/logs/reflex-stage-20261012d/trace.jsonl")
    rows = [json.loads(line) for line in path.open(encoding="utf-8")]
    running = [row for row in rows if row.get("event") == "call_supervision_request"]
    results = [row for row in rows if row.get("event") == "work_decision_request"]
    assert len(running) == 118
    assert len(results) == 16

    runtime = SimpleNamespace(
        agent_context=None, native_task_workspace=None, decision_client=None,
        trace=SimpleNamespace(emit=lambda *args, **kwargs: None),
    )
    supervisor = CallSupervisor(runtime)
    for row in running:
        state = row.get("request", {}).get("state", {})
        execution = {
            "operation_id": row.get("tool_call_id"),
            "tool": row.get("tool"),
            "state": "running",
            "phase": "running",
            "elapsed_s": row.get("idle_s", 0),
            "fact_revision": row.get("fact_revision"),
            "facts": {"health": 20, "food": 20, "oxygen": 300},
        }
        request = supervisor._question(
            row.get("tool_call_id", "replay"),
            SimpleNamespace(name=row.get("tool", "unknown")),
            {}, execution, idle_s=float(row.get("idle_s") or 0),
            purpose="replay", supervision_context={"intent": state.get("agent_intent", "")},
        )
        assert request["questions"]
