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
        receipt = await long_tool.on_invoke_tool(wrapper, "{}")
        owner = runtime.live_body_owner()
        holder = runtime.body_control.snapshot().holder
        # Reads and the next turn's preflight must not queue behind the writer.
        state = await asyncio.wait_for(runtime.run_read(body.get_state), timeout=1)
        busy = await asyncio.wait_for(other_write.on_invoke_tool(wrapper, "{}"), timeout=1)
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
        result = asyncio.run(quick.on_invoke_tool(wrapper, "{}"))
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
        result = asyncio.run(sdk_tool_for(_tool("slow", slow)).on_invoke_tool(wrapper, "{}"))
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
        receipt = await sdk_tool_for(_tool("fails", fails)).on_invoke_tool(wrapper, "{}")
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


def test_an_execution_fact_waits_for_the_running_turn():
    from minebot.app.session import _queued_preemption
    from minebot.app.work_queue import MemoryWorkIntentQueue, WorkIntentKind

    queue = MemoryWorkIntentQueue()
    queue.enqueue(WorkIntentKind.REFLEX_FACT, source="reflex_activity", payload={"fact": {}})
    assert _queued_preemption(queue, "agent_turn") is None
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
