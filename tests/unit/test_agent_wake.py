"""Wake telemetry describes admitted turns without changing scheduling."""
import asyncio
from types import SimpleNamespace

import pytest

from minebot.app.runner import AgentRuntime, RecoveryOutcome
from minebot.app.session import AgentSession, SessionCommand
from minebot.app.work_queue import WorkIntentKind
from tests.unit.test_agent_session import build_parts


class Trace:
    def __init__(self):
        self.rows = []

    def emit(self, event, **fields):
        self.rows.append({"event": event, **fields})


def test_session_emits_one_wake_per_actual_turn_and_none_for_idle_or_cancel():
    calls, bodies = [], []
    session = AgentSession(lambda goal: build_parts(goal, calls, bodies))
    try:
        session.submit(SessionCommand.start("live"))
        asyncio.run(session.step())
        session.parts.runtime.notify_reflex_fact({"activity_id": "a", "status": "handoff_requested", "reason": "decision_call_agent"})
        asyncio.run(session.step())
        asyncio.run(session.step())  # No queued intent.
        session.submit(SessionCommand.message("hello"))
        asyncio.run(session.step())
        session.submit(SessionCommand.cancel())
        asyncio.run(session.step())
        events = [r for r in session.parts.runtime.trace.snapshot() if r["event"] == "agent_wake"]
        assert [r["reason"] for r in events] == ["user_message", "decision_handback", "user_message"]
        assert len(calls) == len(events) == 3
        assert events[1]["detail"]["reason"] == "decision_call_agent"
    finally:
        session.close()


@pytest.mark.parametrize("kind,payload,reason", [
    (WorkIntentKind.BODY_EVENT, {"event": {"name": "death"}}, "death"),
    (WorkIntentKind.BODY_EVENT, {"event": {"name": "world_disconnected"}}, "world_disconnected"),
    (WorkIntentKind.BODY_EVENT, {"event": {"name": "world_ready"}}, "world_ready"),
    (WorkIntentKind.BODY_EVENT, {"event": {"name": "respawned"}}, "respawned"),
    (WorkIntentKind.BODY_EVENT, {"event": {"name": "decision_unavailable"}}, "body_event"),
    (WorkIntentKind.REFLEX_FACT, {"fact": {"kind": "heartbeat"}}, "heartbeat"),
    (WorkIntentKind.REFLEX_FACT, {"fact": {"kind": "habit"}}, "habit"),
    (WorkIntentKind.REFLEX_FACT, {"fact": {"kind": "life"}}, "life"),
    (WorkIntentKind.REFLEX_FACT, {"fact": {"status": "completed"}}, "reflex_result"),
    (WorkIntentKind.REFLEX_FACT, {"fact": {"status": "handoff_requested", "reason": "decision_call_agent"}},
     "decision_handback"),
    (WorkIntentKind.REFLEX_FACT, {"fact": {"status": "handoff_requested", "reason": "decision_step_done"}},
     "call_result"),
    (WorkIntentKind.REFLEX_FACT, {"fact": {"kind": "ladder_low"}}, "ladder_low"),
    (WorkIntentKind.REFLEX_FACT, {"fact": {"kind": "ladder_low", "reason": "none_started"}},
     "ladder_low:none_started"),
    (WorkIntentKind.TASK_CONTINUE, {}, "task_continue"),
    (WorkIntentKind.TASK_BOUNDARY, {}, "task_boundary"),
    (WorkIntentKind.MAINTENANCE, {"action": "reflection"}, "maintenance"),
    (WorkIntentKind.COMPLETION_REVIEW, {}, "completion_review"),
    (WorkIntentKind.RECOVERY_RECONCILE, {}, "recovery_reconcile"),
])
def test_wake_reason_uses_admitted_intent(kind, payload, reason):
    session = AgentSession(lambda _: None)
    trace = Trace()
    session.parts = SimpleNamespace(runtime=SimpleNamespace(trace=trace))
    intent = session.work_queue.enqueue(kind, source="test", payload=payload)
    trace.emit("agent_wake", **session._agent_wake(intent))
    assert trace.rows[0]["reason"] == reason
    assert trace.rows[0]["detail"]["intent_id"] == intent.intent_id


def test_recovery_preparation_does_not_emit_an_extra_model_wake():
    calls, bodies = [], []
    session = AgentSession(lambda goal: build_parts(goal, calls, bodies))
    try:
        session.submit(SessionCommand.start("live"))
        asyncio.run(session.step())
        runtime = session.parts.runtime
        runtime.recovery_handler = lambda _: RecoveryOutcome(True, "respawned")
        session.parts.lifecycle.enter_recovery()
        asyncio.run(session.step())
        asyncio.run(session.step())
        wakes = [r for r in runtime.trace.snapshot() if r["event"] == "agent_wake"]
        assert len(wakes) == len(calls) == 2
        assert wakes[-1]["reason"] == "recovery_reconcile"
        assert wakes[-1]["detail"]["intent_kind"] == "recovery_reconcile"
    finally:
        session.close()


@pytest.mark.parametrize("lock", [False, True])
def test_native_fact_successor_emits_once_and_active_kernel_does_not_wake(lock):
    async def scenario():
        class Kernel:
            is_running = False
            has_facts = False

            def notify_fact(self, fact):
                self.has_facts = True

        runtime = object.__new__(AgentRuntime)
        kernel = Kernel()
        runtime.responses_kernel = kernel
        runtime.native_responses = True
        runtime.agent_context = SimpleNamespace(goal_text="live")
        runtime.trace = Trace()
        runtime._reflex_wakeup_task = None
        if lock:
            runtime._responses_turn_lock = asyncio.Lock()
        calls = []

        async def run_responses(*_args, **_kwargs):
            calls.append(_kwargs["wake"])
            kernel.has_facts = False

        runtime._run_responses_turn = run_responses
        runtime.notify_reflex_fact({"kind": "life", "status": "home_blocked"})
        runtime.notify_reflex_fact({"status": "completed"})
        await runtime._reflex_wakeup_task
        assert len(calls) == 1
        assert calls[0]["reason"] == "life"
        kernel.is_running = True
        runtime.notify_reflex_fact({"status": "completed"})
        await asyncio.sleep(0)
        assert len(calls) == 1

    asyncio.run(scenario())


def test_native_runtime_emits_at_kernel_start_but_not_for_preparation_abort():
    from minebot.app.runner import AgentTurnOutcome
    from minebot.brain.lifecycle import LifecycleState

    async def scenario():
        runtime = object.__new__(AgentRuntime)
        runtime.trace = Trace()
        runtime.lifecycle = SimpleNamespace(state=LifecycleState.ACTIVE)
        runtime.agent_context = SimpleNamespace(
            goal_text="live", pending_turn_input=lambda **_: ("continue", 0), acknowledge_turn_input=lambda _: None)
        runtime.weld_context = SimpleNamespace()
        runtime.tool_facts = {}
        runtime.max_turns = 5
        prepared = object()

        async def read(callback, signals):
            return prepared

        runtime._prepare_turn = lambda _: prepared
        runtime._read_lane = SimpleNamespace(run=read)
        calls = []

        async def run(*args, **kwargs):
            calls.append(args)
            return None

        runtime.responses_kernel = SimpleNamespace(has_facts=True, resume=lambda: None, run=run)
        metadata = {"reason": "decision_handback", "detail": {"status": "handoff_requested"}}
        await runtime._run_responses_turn([], body_actions_allowed=True, wake=metadata)
        assert [r for r in runtime.trace.rows if r["event"] == "agent_wake"] == [{"event": "agent_wake", **metadata}]
        prepared = AgentTurnOutcome("waiting", LifecycleState.ACTIVE, None)
        await runtime._run_responses_turn([], body_actions_allowed=True, wake=metadata)
        assert len(calls) == 1
        assert len([r for r in runtime.trace.rows if r["event"] == "agent_wake"]) == 1

    asyncio.run(scenario())
