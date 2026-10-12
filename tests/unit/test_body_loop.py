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
        goal="", intent="", tool="move_to", params={}, facts=facts,
        running=True, phase="moving", elapsed_s=1.0,
    )
    running_fact_keys = set(running) - {"current_work", "progress"}
    assert set(idle["state"]) == running_fact_keys


def test_reflex_experiences_are_bounded_and_available_to_live_context():
    runtime, _trace, _decision = _runtime()
    loop = BodyLoop(runtime)
    for index in range(7):
        loop.record_experience(f"experience {index}")
    assert list(loop.recent_experiences) == [f"experience {index}" for index in range(2, 7)]


def test_idle_protection_is_executed_once_through_threat_claim():
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
        assert calls and calls[0]["claim_holder"] == "threat"
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


def test_body_loop_owns_the_running_frame_consumer():
    runtime, _trace, _decision = _runtime()
    assert "_monitor" not in CallSupervisor.__dict__
    assert "_monitor" in BodyLoop.__dict__


def test_body_loop_reuses_lifecycle_slot_for_a_running_call():
    async def run():
        runtime, _trace, _decision = _runtime()
        loop = BodyLoop(runtime)
        await loop.start()
        idle_task = loop._lifecycle_task
        worker = asyncio.create_task(asyncio.sleep(0.2))
        record = {"call_id": "slot", "cancel_requested": False}
        monitor = loop._start_monitor(
            record, SimpleNamespace(name="move_to"), {}, asyncio.Event(), worker,
        )
        await asyncio.sleep(0)
        assert idle_task is loop._lifecycle_task
        assert monitor is None
        assert loop._active_monitor is not None
        record["cancel_requested"] = True
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await loop.stop()

    asyncio.run(run())


def test_agent_call_wakes_lifecycle_and_cancels_idle_question():
    async def run():
        runtime, trace, _decision = _runtime()
        started = asyncio.Event()
        cancelled = asyncio.Event()

        class BlockingDecision:
            async def ask(self, request, *, priority=None):
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise

            async def close(self):
                return None

        runtime.decision_client = BlockingDecision()
        loop = BodyLoop(runtime)
        worker = asyncio.create_task(asyncio.sleep(1))
        record = {"call_id": "wake", "cancel_requested": False}
        try:
            await loop.start()
            await asyncio.wait_for(started.wait(), 2)
            monitor = loop._start_monitor(
                record, SimpleNamespace(name="move_to"), {}, asyncio.Event(), worker,
            )
            assert monitor is None
            await asyncio.wait_for(cancelled.wait(), 1)
            assert loop._active_monitor is not None
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            await loop.stop()

    asyncio.run(run())


def test_running_question_trace_identifies_the_body_loop_state():
    async def run():
        runtime, trace, _decision = _runtime()
        loop = BodyLoop(runtime)
        loop._set_state("work", "agent_call")
        choice = await loop.judge(
            "call-1", SimpleNamespace(name="move_to"), {},
            {"state": "running", "facts": runtime.last_known_body_state, "fact_revision": 1},
            0.0, options={"keep_going": "Continue the current work."},
        )
        assert choice == "keep_going"
        request = next(item for item in trace.events if item["event"] == "call_supervision_request")
        assert request["loop_state"] == "work"

    asyncio.run(run())


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
    """Replay raw body facts through the production question builder.

    The test deliberately never calls DecisionClient.  It walks the trace in
    time order, carries forward the latest raw ``body_progress`` sample, and
    feeds those fields into the same running and result question compiler used
    online.  It does not read the recorded prose as input.
    """
    path = Path("/home/qwait/MineBot-reflex/logs/reflex-stage-20261012d/trace.jsonl")
    rows = [json.loads(line) for line in path.open(encoding="utf-8")]
    running: list[tuple[dict, dict]] = []
    results: list[tuple[dict, dict]] = []
    latest_progress: dict | None = None
    progress_by_call: dict[str, dict] = {}

    for row in rows:
        if row.get("event") == "body_progress":
            latest_progress = row
            call_id = row.get("tool_call_id")
            if call_id:
                progress_by_call[str(call_id)] = row
            continue
        if row.get("event") == "call_supervision_request":
            call_id = str(row.get("tool_call_id") or "")
            raw = progress_by_call.get(call_id) or latest_progress
            assert raw is not None, "a running frame must have a preceding raw body sample"
            running.append((row, raw))
        elif row.get("event") == "work_decision_request":
            assert latest_progress is not None, "a result frame must have a raw body sample"
            results.append((row, latest_progress))

    assert len(running) == 118
    assert len(results) == 16

    runtime = SimpleNamespace(
        agent_context=None, native_task_workspace=None, decision_client=None,
        trace=SimpleNamespace(emit=lambda *args, **kwargs: None),
    )
    supervisor = CallSupervisor(runtime)
    def facts_from(raw: dict) -> dict:
        facts = {
            key: raw[key] for key in (
                "health", "food", "oxygen", "on_ground", "selected_item",
            ) if key in raw
        }
        if isinstance(raw.get("pos"), list):
            facts["position"] = list(raw["pos"])
        if isinstance(raw.get("inventory_counts"), dict):
            facts["inventory_counts"] = dict(raw["inventory_counts"])
        if isinstance(raw.get("entities_near"), list):
            facts["entities_near"] = list(raw["entities_near"])
        return facts

    for row, raw in running:
        state = row.get("request", {}).get("state", {})
        execution = {
            "operation_id": row.get("tool_call_id"),
            "tool": row.get("tool"),
            "state": "running",
            "phase": "running",
            "elapsed_s": row.get("idle_s", 0),
            "fact_revision": row.get("fact_revision"),
            "facts": facts_from(raw),
        }
        request = supervisor._question(
            row.get("tool_call_id", "replay"),
            SimpleNamespace(name=row.get("tool", "unknown")),
            {}, execution, idle_s=float(row.get("idle_s") or 0),
            purpose="replay", supervision_context={"intent": state.get("agent_intent", "")},
        )
        assert request["questions"]
        assert request["state"].get("body")
        assert "Health" in request["state"]["body"]

    for row, raw in results:
        facts = facts_from(raw)
        request = supervisor._question(
            row.get("node_id", "result"),
            SimpleNamespace(name=row.get("node_id", "unknown")), {},
            {"tool": row.get("node_id", "unknown"), "state": "succeeded", "facts": facts},
            idle_s=0.0, purpose="replay_result",
            supervision_context={"intent": row.get("request", {}).get("state", {}).get("agent_intent", "")},
            decision_context={"tool_result": {"success": True}, "body_state": facts},
        )
        assert request["questions"]
        assert request["state"].get("finished_call")
