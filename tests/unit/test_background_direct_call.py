"""A direct Body call keeps its claim and its work when the model turn moves on."""

import asyncio
import threading
from types import SimpleNamespace

import pytest

from minebot.app.body_control import BodyControl
from minebot.app.reflex_tools import ReflexManager
from minebot.app.runner import (
    DIRECT_CALL_WINDOW_ENV,
    AgentRuntime,
    BackgroundCall,
    RuntimeRunContext,
    direct_call_window_from_env,
    sdk_tool_for,
)
from minebot.brain.context import AgentContext
from minebot.brain.lifecycle import LifecycleController, LifecycleState
from minebot.brain.modes import ModeRuntime
from minebot.brain.progress import ProgressAuthority
from minebot.brain.registry import RegisteredTool, ToolRegistry, ToolSidecar
from minebot.contract import ToolResult
from tests.unit.test_agent_runner_spine import FakeBody


def _tool(name, callable_, *, mutating=True):
    return RegisteredTool(
        name=name,
        description=name,
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        callable=callable_,
        sidecar=ToolSidecar(progress_key=name, mutating=mutating),
    )


def _runtime(body, *, window_s=0.05, listening=True):
    runtime = AgentRuntime(
        body=body,
        registry=ToolRegistry(),
        agent_context=AgentContext(system_prompt="sys", goal_text="test"),
        lifecycle=LifecycleController(),
        mode_runtime=ModeRuntime(),
        authority=ProgressAuthority(),
        runner_run=lambda *_args, **_kwargs: None,
    )
    runtime.direct_call_window = window_s
    facts = []
    if listening:
        runtime.set_sdk_reflex_fact_listener(facts.append)
    context = RuntimeRunContext(
        agent_context=runtime.agent_context,
        weld_context=runtime.weld_context,
        profile=ModeRuntime().profile_for(LifecycleState.ACTIVE),
        runtime=runtime,
        trace=runtime.trace,
    )
    return runtime, SimpleNamespace(context=context), facts


def test_long_call_leaves_the_turn_and_delivers_its_one_result_later():
    body = FakeBody()
    started, release = threading.Event(), threading.Event()

    def long_work(_params):
        started.set()
        release.wait(timeout=5)
        body.x += 1
        return ToolResult(True, "completed", False, metrics={"x": body.x})

    runtime, wrapper, facts = _runtime(body)
    long_tool = sdk_tool_for(_tool("long_work", long_work))
    other_write = sdk_tool_for(_tool("other_write", lambda _p: ToolResult(True, "completed", False)))

    async def scenario():
        receipt = await long_tool.on_invoke_tool(wrapper, '{"intent":"finish the requested body work"}')
        owner = runtime.live_body_owner()
        holder = runtime.body_control.snapshot().holder
        # Reads and the next turn's preflight must not queue behind the writer.
        state = await asyncio.wait_for(runtime.run_read(body.get_state), timeout=1)
        busy = await asyncio.wait_for(other_write.on_invoke_tool(wrapper, '{"intent":"finish the requested body work"}'), timeout=1)
        work = runtime.background_work()
        # A cancelled turn has nothing of its own left on the writer lane.
        idle = (await runtime.wait_for_turn_execution_idle(timeout_s=0.2),
                await runtime.wait_for_execution_idle(timeout_s=0.05))
        release.set()
        for _ in range(200):
            if facts:
                break
            await asyncio.sleep(0.01)
        return receipt, owner, holder, state, busy, work, idle

    try:
        receipt, owner, holder, state, busy, work, idle = asyncio.run(scenario())
    finally:
        release.set()
        runtime.close()

    assert started.is_set()
    assert receipt["success"] and receipt["reason"] == "running_in_background"
    assert owner["kind"] == "direct_call" and owner["tool"] == "long_work"
    assert holder == "agent_call"
    assert state.pos[0] == 0.0
    assert busy["success"] is False and busy["reason"] == "body_busy"
    assert work["direct_call"]["tool"] == "long_work"
    assert work["execution"]["tool"] == "long_work" and work["execution"]["state"] == "running"
    assert work["execution"]["operation_id"] == owner["tool_call_id"]
    finished = runtime.execution_snapshots.last()
    assert finished["state"] == "succeeded" and finished["control"] == "released"
    assert runtime.execution_snapshots.current() is None
    assert idle == (True, False)

    assert len(facts) == 1
    fact = facts[0]
    assert fact["kind"] == "direct_call" and fact["status"] == "completed"
    assert fact["tool_call_id"] == owner["tool_call_id"] == fact["activity_id"]
    assert fact["result"]["success"] is True
    assert runtime.background_call is None
    assert runtime.body_control.snapshot().holder == "none"
    trace = runtime.trace.snapshot()
    events = [event["event"] for event in trace]
    results = [event for event in trace if event["event"] == "tool_result" and event.get("tool") == "long_work"]
    assert [event["reason"] for event in results] == ["completed"]  # the receipt is not a result
    assert "tool_background" in events and "background_call_finished" in events


def test_call_that_settles_inside_the_window_is_answered_in_the_turn():
    body = FakeBody()
    runtime, wrapper, facts = _runtime(body, window_s=5.0)
    quick = sdk_tool_for(_tool("quick", lambda _p: ToolResult(True, "completed", False)))
    try:
        result = asyncio.run(quick.on_invoke_tool(wrapper, '{"intent":"finish the requested body work"}'))
    finally:
        runtime.close()
    assert result["success"] and result["reason"] == "completed"
    assert facts == [] and runtime.background_call is None


def test_call_stays_in_the_turn_when_nobody_listens_for_a_later_result():
    body = FakeBody()
    release = threading.Event()

    def slow(_params):
        release.wait(timeout=0.3)
        return ToolResult(True, "completed", False)

    runtime, wrapper, _facts = _runtime(body, listening=False)
    try:
        result = asyncio.run(sdk_tool_for(_tool("slow", slow)).on_invoke_tool(wrapper, '{"intent":"finish the requested body work"}'))
    finally:
        runtime.close()
    assert result["reason"] == "completed"


def test_failed_background_call_reports_blocked():
    body = FakeBody()
    release = threading.Event()

    def fails(_params):
        release.wait(timeout=5)
        return ToolResult(False, "no_path", True)

    runtime, wrapper, facts = _runtime(body)

    async def scenario():
        receipt = await sdk_tool_for(_tool("fails", fails)).on_invoke_tool(wrapper, '{"intent":"finish the requested body work"}')
        release.set()
        for _ in range(200):
            if facts:
                break
            await asyncio.sleep(0.01)
        return receipt

    try:
        receipt = asyncio.run(scenario())
    finally:
        release.set()
        runtime.close()
    assert receipt["reason"] == "running_in_background"
    assert facts[0]["status"] == "blocked" and facts[0]["result"]["reason"] == "no_path"


def test_reflex_tools_show_and_stop_the_direct_call():
    async def scenario():
        stops = []

        async def stop(reason, cancel_reflex=True):
            stops.append((reason, cancel_reflex))
            return {"settled": True}

        pending = asyncio.get_running_loop().create_future()
        runtime = SimpleNamespace(
            body_control=BodyControl(), responses_kernel=None, trace=SimpleNamespace(emit=lambda *a, **k: None),
            background_call=BackgroundCall("call-1", "collect_resource", {"item": "oak_log"}, pending),
            cancel_active_execution_with_facts=stop,
        )
        manager = ReflexManager(runtime)
        manager.bind_loop(asyncio.get_running_loop())
        read = manager.read()
        stopped = manager.control({"operation": "cancel"})
        await asyncio.sleep(0.01)
        pending.cancel()
        after = manager.read()
        return read, stopped, stops, after

    read, stopped, stops, after = asyncio.run(scenario())
    assert read["metrics"]["direct_call"]["tool"] == "collect_resource"
    assert read["metrics"]["activity_id"] == "call-1" and read["metrics"]["status"] == "running"
    assert stopped["reason"] == "take_control_requested"
    assert stops == [("reflex_take_control", False)]
    assert after["reason"] == "no_reflex"


def test_window_setting():
    assert direct_call_window_from_env({}) == 30.0
    assert direct_call_window_from_env({DIRECT_CALL_WINDOW_ENV: "off"}) is None
    assert direct_call_window_from_env({DIRECT_CALL_WINDOW_ENV: "2.5"}) == 2.5
    with pytest.raises(ValueError):
        direct_call_window_from_env({DIRECT_CALL_WINDOW_ENV: "soon"})


def test_result_without_a_goal_waits_for_the_next_turn_instead_of_vanishing():
    from minebot.app.session import AgentSession, SessionCommand
    from tests.unit.test_agent_session import build_parts

    calls, bodies = [], []
    session = AgentSession(lambda goal: build_parts(goal, calls, bodies))
    session.submit(SessionCommand.message("come over here"))
    asyncio.run(session.step())
    assert session.parts is not None
    session.parts.runtime.notify_reflex_fact({
        "kind": "direct_call", "activity_id": "call-9", "tool_call_id": "call-9", "tool": "move_to",
        "status": "completed", "result": {"success": True, "reason": "arrived"},
    })
    session.submit(SessionCommand.message("are you here yet?"))
    asyncio.run(session.step())
    session.close()
    assert "REFLEX_RESULT" in calls[-1] and "call-9" in calls[-1]


def test_current_context_is_read_again_before_every_request():
    from agents.run_config import CallModelData, ModelInputData

    body = FakeBody()
    runtime, wrapper, _facts = _runtime(body)

    def request():
        data = CallModelData(model_data=ModelInputData(input=[{"role": "user", "content": "go"}], instructions="sys"),
                             agent=None, context=wrapper.context)
        return asyncio.run(runtime._bounded_model_input(data)).input[-1]["content"]

    try:
        first = request()
        body.x = 42.0  # the Body moved during the turn
        second = request()
    finally:
        runtime.close()
    assert first.startswith("CURRENT_CONTEXT:") and "42" not in first
    assert "42" in second


def test_an_execution_fact_preempts_the_running_turn_for_immediate_delivery():
    from minebot.app.session import _queued_preemption
    from minebot.app.work_queue import MemoryWorkIntentQueue, WorkIntentKind

    queue = MemoryWorkIntentQueue()
    queue.enqueue(WorkIntentKind.REFLEX_FACT, source="reflex_activity", payload={"fact": {}})
    assert _queued_preemption(queue, "agent_turn") is not None
    queue.enqueue(WorkIntentKind.MESSAGE, source="player", payload={"text": "stop that"})
    assert _queued_preemption(queue, "agent_turn") is not None


def test_rejected_plan_update_names_the_revision_to_use():
    from minebot.app.runtime_state import RuntimeScope, RuntimeStateStore
    from minebot.app.tasks import TaskWorkspace, register_task_tools

    workspace = TaskWorkspace(RuntimeStateStore(":memory:"), RuntimeScope("server", "world", "Bot1"))
    workspace.start("build a house", source="test")
    registry = ToolRegistry()
    register_task_tools(registry, workspace)
    result = registry.get("update_plan").callable(
        {"expected_revision": 1, "summary": "s", "steps": [{"title": "collect logs", "status": "in_progress"}]})
    assert result.reason == "task_plan_update_rejected"
    assert result.metrics["current_plan_revision"] == 0
    assert "expected_revision=0" in result.next_suggestion


def test_plan_update_accepts_the_null_blocker_the_tools_themselves_report():
    import jsonschema

    from minebot.app.runtime_state import RuntimeScope, RuntimeStateStore
    from minebot.app.tasks import TaskWorkspace, register_task_tools

    workspace = TaskWorkspace(RuntimeStateStore(":memory:"), RuntimeScope("server", "world", "Bot1"))
    workspace.start("build a house", source="test")
    registry = ToolRegistry()
    register_task_tools(registry, workspace)
    tool = registry.get("update_plan")
    arguments = {"expected_revision": 0, "summary": "s",
                 "steps": [{"title": "collect logs", "status": "completed", "blocker": None}]}
    jsonschema.validate(arguments, tool.input_schema)
    assert tool.callable(arguments).success is True


def test_a_result_that_ended_mid_turn_stays_visible_until_it_is_delivered():
    runtime, _wrapper, facts = _runtime(FakeBody())
    runtime.notify_reflex_fact({"kind": "direct_call", "activity_id": "call-7", "tool": "collect_resource",
                                "status": "blocked", "result": {"success": False, "reason": "no_progress"}})
    runtime.notify_reflex_fact({"activity_id": "activity-1", "status": "blocked", "tree": {}, "facts": {}})

    finished = runtime.background_work()["finished"]
    assert [(item["activity_id"], item["kind"], item["status"]) for item in finished["results"]] == [
        ("call-7", "direct_call", "blocked"), ("activity-1", "plan", "blocked")]
    assert finished["results"][0]["reason"] == "no_progress" and "ended_s_ago" in finished["results"][0]
    assert len(facts) == 2

    runtime.reflex_fact_delivered("call-7")
    assert [item["activity_id"] for item in runtime.background_work()["finished"]["results"]] == ["activity-1"]
    runtime.reflex_fact_delivered("activity-1")
    assert runtime.background_work() is None


def test_a_read_swept_by_a_stop_of_body_work_is_still_answered():
    import threading

    runtime, _wrapper, _facts = _runtime(FakeBody())
    gate = threading.Event()

    async def scenario():
        writer = asyncio.create_task(runtime.execution_lane.run(gate.wait))
        await asyncio.sleep(0.02)
        read = asyncio.create_task(runtime.run_read(lambda: "state"))  # no owner: queued behind the writer
        await asyncio.sleep(0.02)
        runtime.request_execution_cancel("reflex_take_control")
        answer = await asyncio.wait_for(read, timeout=1)
        gate.set()
        await writer
        return answer

    try:
        assert asyncio.run(scenario()) == "state"
    finally:
        gate.set()


def test_losing_the_body_watcher_does_not_cancel_the_turn():
    runtime, _wrapper, _facts = _runtime(FakeBody())

    class Streamed:
        cancelled = False

        async def stream_events(self):
            await asyncio.sleep(0.05)
            return
            yield

        def cancel(self, mode="immediate"):
            self.cancelled = True

    async def lost_watcher():
        raise asyncio.CancelledError

    runtime._wait_for_critical_body_fact = lost_watcher
    streamed = Streamed()
    assert asyncio.run(runtime._supervise_streamed_run(streamed)) is streamed
    assert streamed.cancelled is False


def test_an_idle_agent_with_a_goal_is_woken_at_growing_intervals():
    from minebot.app import session as session_module
    from minebot.app.session import AgentSession, SessionCommand
    from tests.unit.test_agent_session import build_parts

    calls, bodies = [], []
    session = AgentSession(lambda goal: build_parts(goal, calls, bodies))
    session.submit(SessionCommand.start("build a house"))
    asyncio.run(session.step())
    assert session.has_active_goal and not session.queue_heartbeat_if_due()  # just worked

    session._last_work_monotonic -= session_module.HEARTBEAT_BASE_S + 1
    assert session.queue_heartbeat_if_due() and session.has_pending_work
    asyncio.run(session.step())
    assert "HEARTBEAT" in calls[-1] and "idle_check" in calls[-1]
    assert session.heartbeat_interval_s() == 2 * session_module.HEARTBEAT_BASE_S

    session._last_work_monotonic -= session_module.HEARTBEAT_BASE_S + 1
    assert not session.queue_heartbeat_if_due()  # the second one waits twice as long
    session.submit(SessionCommand.message("how is it going?"))
    asyncio.run(session.step())
    assert session.heartbeat_interval_s() == session_module.HEARTBEAT_BASE_S  # real work resets the backoff
    session.close()
