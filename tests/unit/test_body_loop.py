import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace


from minebot.app.body_loop import BodyLoop
from minebot.app.decision_client import DecisionAnswer
from minebot.app.direct_call_supervision import CallSupervisor
from minebot.app import reflex_question as rq
from minebot.app.work_runtime import NodeRecord, WorkPlanExecutor
from minebot.app.execution_lane import SerialExecutionLane
from minebot.game.real_client_body import RealClientBody


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


def test_idle_reader_retains_body_vital_history_entities_and_food():
    """Exercise the client projection and the separate read lane, not a dict getter."""
    async def run():
        runtime, _trace, _decision = _runtime()
        lane = SerialExecutionLane(thread_name="idle-fact-test")

        class RecordedIpc:
            def connect(self):
                pass

            def state(self):
                return {
                    "position": [1, 60, 2], "health": 6, "food": 10,
                    "oxygen": 139, "inventory": {"apple": 1},
                    "entities": [{"id": "hostile-1", "type": "hostile",
                                  "hostile": True, "dist2": 9, "pos": [4, 60, 2]}],
                }

        runtime.body = RealClientBody("RecordedBody", RecordedIpc())
        runtime._read_lane = lane
        loop = BodyLoop(runtime)
        # The baseline was observed while a previous call was running.
        loop._vitals._samples = [(time.monotonic() - 5, {"health": 10, "oxygen": 243})]
        try:
            facts = await loop._read_idle_facts()
            assert facts["health_before"] == 10
            assert facts["oxygen_before"] == 243
            assert 5 <= facts["vital_age_s"] < 6
            assert rq.hostiles(facts)[0]["dist"] == 3
            assert facts["food_item"] == "apple"
            question = loop._idle_question(facts, loop._idle_options(facts))
            assert "fell from 10 to 6" in question["state"]["body"]
            assert "243 5 seconds ago" in question["state"]["body"]
            assert "3 blocks away" in question["state"]["threats"]
            assert "move_away" in question["questions"]["p"]["criteria"]
            assert "eat" in question["questions"]["q"]["criteria"]
            # Returning to work retains the same Body history.
            running = loop._frame({"facts": {"health": 5, "oxygen": 120}})
            assert running["facts"]["health_before"] == 10
        finally:
            lane.close()

    asyncio.run(run())


def test_idle_entity_read_failure_preserves_successful_state_read():
    async def run():
        runtime, trace, _decision = _runtime()
        lane = SerialExecutionLane(thread_name="idle-read-failure-test")

        def unavailable_entities(scope, params):
            raise OSError("entity reader unavailable")

        runtime.body = SimpleNamespace(
            get_state=lambda: {"position": [8, 64, 9], "health": 12, "oxygen": 200},
            perceive=unavailable_entities,
        )
        runtime._read_lane = lane
        loop = BodyLoop(runtime)
        try:
            facts = await loop._read_idle_facts()
            assert facts["position"] == [8, 64, 9]
            assert facts["health"] == 12
            assert any(e["event"] == "reflex_idle_read_unknown" for e in trace.events)
        finally:
            lane.close()

    asyncio.run(run())


def test_body_loop_stop_cancels_an_inflight_idle_answer():
    async def run():
        runtime, _trace, _decision = _runtime()
        started = asyncio.Event()
        cancelled = asyncio.Event()

        class BlockingDecision:
            async def ask(self, request, *, priority=None):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        runtime.decision_client = BlockingDecision()
        loop = BodyLoop(runtime)
        await loop.start()
        await asyncio.wait_for(started.wait(), 2)
        await loop.stop()
        assert cancelled.is_set()

    asyncio.run(run())


def test_agent_arrival_supersedes_an_idle_answer_that_finishes_at_the_same_time():
    async def run():
        runtime, _trace, _decision = _runtime()
        monitor_started = asyncio.Event()
        protected = []
        worker = asyncio.create_task(asyncio.Event().wait())
        record = {"call_id": "arriving-work", "cancel_requested": False}

        class ArrivingDecision:
            async def ask(self, request, *, priority=None):
                loop._start_monitor(record, SimpleNamespace(name="move_to"), {}, asyncio.Event(), worker)
                return DecisionAnswer("surface", {"answers": {
                    "q": {"choice": "stay"}, "d": {"choice": "in_danger"},
                    "p": {"choice": "surface"},
                }}, 0.001, 1)

        runtime.last_known_body_state["oxygen"] = 100
        runtime.decision_client = ArrivingDecision()
        loop = BodyLoop(runtime)

        async def monitor(*args):
            monitor_started.set()
            await asyncio.Event().wait()

        async def protect(choice, facts):
            protected.append(choice)

        loop._monitor = monitor
        loop._run_idle_protection = protect
        try:
            await loop.start()
            await asyncio.wait_for(monitor_started.wait(), 2)
            assert protected == []
        finally:
            await loop.stop()
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(run())


def test_protective_result_unknown_is_reasked_without_repeating_the_action(monkeypatch):
    async def run():
        runtime, _trace, _decision = _runtime()
        writes = []
        requests = []
        replies = iter(("invalid", "unknown", "resume"))
        runtime.registry = SimpleNamespace(get=lambda name: SimpleNamespace(name=name))

        async def run_tool(*args, **kwargs):
            writes.append(kwargs)
            raise AssertionError("unknown must not execute another action")

        class Decision:
            async def ask(self, request, *, priority=None):
                requests.append(request)
                runtime.last_known_body_state["position"] = [len(requests), 64, 2]
                return DecisionAnswer("unknown", {"answers": {"q": {"choice": next(replies)}}}, .001, len(requests))

        runtime.run_tool = run_tool
        runtime.decision_client = Decision()
        loop = BodyLoop(runtime)
        record = {
            "call_id": "interrupted-work", "tool": "collect_resource",
            "params": {"item": "oak_log", "count": 3}, "questions": 0,
            "unknown_answers": 0,
            "supervision_context": {"intent": "have three oak logs"},
        }
        monkeypatch.setattr("minebot.app.body_loop.FACT_FRAME_INTERVAL_S", .001)
        choice = await loop._after_default_action(
            record, ("go_to_surface", {}), {"success": True, "reason": "surface_reached"}, "surface-call",
        )
        assert choice == "resume"
        assert len(requests) == 3
        assert record["unknown_answers"] == 2
        assert not writes
        assert all("earlier_attempts_of_this_call" not in r["state"] for r in requests)
        assert requests[0]["state"]["position"] != requests[-1]["state"]["position"]

    asyncio.run(run())


def test_protective_result_unknown_remains_cancellable(monkeypatch):
    async def run():
        runtime, _trace, _decision = _runtime()
        runtime.registry = SimpleNamespace(get=lambda name: SimpleNamespace(name=name))
        requests = []

        class Decision:
            async def ask(self, request, *, priority=None):
                requests.append(request)
                return DecisionAnswer("unknown", None, .001, len(requests))

        runtime.decision_client = Decision()
        loop = BodyLoop(runtime)
        record = {"call_id": "original", "tool": "move_to", "params": {},
                  "questions": 0, "unknown_answers": 0,
                  "supervision_context": {"intent": "reach the requested position"}}
        monkeypatch.setattr("minebot.app.body_loop.FACT_FRAME_INTERVAL_S", .001)
        job = asyncio.create_task(loop._after_default_action(
            record, ("go_to_surface", {}), {"success": True}, "surface-call",
        ))
        await asyncio.sleep(.02)
        assert not job.done()
        assert len(requests) > 1
        job.cancel()
        result = await asyncio.gather(job, return_exceptions=True)
        assert isinstance(result[0], asyncio.CancelledError)

    asyncio.run(run())


def test_protective_retry_retains_its_action_identity_and_counts_only_real_attempts(monkeypatch):
    async def run():
        runtime, _trace, _decision = _runtime()
        runtime.registry = SimpleNamespace(get=lambda name: SimpleNamespace(name=name))
        requests, writes = [], []
        replies = iter(("unknown", "repeat", "resume"))

        class Decision:
            async def ask(self, request, *, priority=None):
                requests.append(request)
                return DecisionAnswer("unknown", {"answers": {"q": {"choice": next(replies)}}}, .001, len(requests))

        async def run_tool(*args, **kwargs):
            writes.append(kwargs)
            return {"success": True, "reason": "surface_reached"}

        runtime.decision_client = Decision()
        runtime.run_tool = run_tool
        loop = BodyLoop(runtime)
        record = {"call_id": "original", "tool": "move_to", "params": {},
                  "questions": 0, "unknown_answers": 0,
                  "supervision_context": {"intent": "reach the requested position"}}
        monkeypatch.setattr("minebot.app.body_loop.FACT_FRAME_INTERVAL_S", .001)
        choice = await loop._after_default_action(
            record, ("go_to_surface", {}), {"success": True}, "surface-call",
            protective_choice="surface",
        )
        assert choice == "resume"
        assert len(writes) == 1
        assert writes[0]["supervision_context"]["reflex_action_choice"] == "surface"
        assert writes[0]["tool_call_id"] == "surface-call-1"
        assert "earlier_attempts_of_this_call" not in requests[1]["state"]
        assert requests[2]["state"]["earlier_attempts_of_this_call"] == ["Attempt 1: ended normally."]

    asyncio.run(run())


def test_reflex_experiences_are_bounded_and_available_to_live_context():
    runtime, _trace, _decision = _runtime()
    loop = BodyLoop(runtime)
    for index in range(7):
        loop.record_experience(f"experience {index}")
    assert list(loop.recent_experiences) == [f"experience {index}" for index in range(2, 7)]


def test_plan_result_raw_facts_reproduce_the_same_request_through_body_loop():
    """All sixteen historical result frames retain their protocol.

    This compares raw node facts through the actual executor -> BodyLoop path;
    it neither asks a real model nor claims to recover the 118 running frames.
    """
    path = Path(__file__).parents[1] / "fixtures" / "reflex_step2_plan_result_frames.json"
    cases = json.loads(path.read_text(encoding="utf-8"))["cases"]
    assert len(cases) == 16

    async def run():
        for case in cases:
            runtime, trace, _decision = _runtime()
            loop = BodyLoop(runtime)
            executor = WorkPlanExecutor(
                case["plan"], call=None, result_judge=loop.decide_plan_result,
            )
            record = NodeRecord(decision_facts=case["raw_decision_facts"])
            assert executor._call_question(
                case["node"], case["node_id"], case["intents"], record,
            ) == case["expected_request"]
            await executor._decide_call(
                case["node"], case["node_id"], case["intents"], record,
            )
            requests = [e["request"] for e in trace.events
                        if e["event"] == "call_supervision_request"]
            assert requests == [case["expected_request"]], case["seq"]
            assert json.dumps(requests[0], ensure_ascii=False, separators=(",", ":")) == json.dumps(
                case["expected_request"], ensure_ascii=False, separators=(",", ":"),
            ), case["seq"]
            assert not loop._result_pending

    asyncio.run(run())


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


def test_result_pending_suppresses_idle_until_released(monkeypatch):
    async def run():
        runtime, trace, _decision = _runtime()
        monkeypatch.setattr("minebot.app.body_loop.FACT_FRAME_INTERVAL_S", 0.001)
        loop = BodyLoop(runtime)
        loop._result_pending["call-1"] = {"call_id": "call-1"}
        await loop.start()
        await asyncio.sleep(0.01)
        assert not [e for e in trace.events if e["event"] == "call_supervision_request"]
        loop._release_result_pending("call-1")
        await asyncio.sleep(0.01)
        await loop.stop()
        assert any(e["event"] == "call_supervision_request" for e in trace.events)

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
