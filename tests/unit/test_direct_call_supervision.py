import asyncio
import json
import threading
import time
from types import SimpleNamespace

from minebot.app.client_body_tools import client_body_tools
from minebot.app.construction_facts import read_construction
from minebot.app.decision_client import DecisionAnswer
from minebot.app.wiring import build_agent_runtime
from minebot.app.reflex_tools import ReflexManager
from minebot.brain.registry import ToolRegistry, RegisteredTool, ToolSidecar, WeldContext, execute_tool
from minebot.brain.progress import ProgressAuthority
from minebot.game.real_client_body import RealClientBody, _body_state
from minebot.game.real_client_ipc import RealClientIpcError, RealClientTerminal
from minebot.contract import Action, ToolResult
from minebot.contract.execution import tool_observation_scope
from minebot.app.execution_snapshot import ExecutionSnapshots
import minebot.app.direct_call_supervision as supervision_module
import minebot.app.execution_snapshot as snapshot_module


class NativeIpc:
    def __init__(self, *, complete=True, stock=0):
        self.complete, self.stock = complete, stock
        self.cancelled = threading.Event()
        self.started = threading.Event()
        self.active = None
        self.matched = 1

    def connect(self):
        pass

    def close(self):
        pass

    def state(self):
        return {"position": [0, 5, 0], "health": 20, "food": 20, "creative": False,
                "disposable_world": True, "inventory": {"cobblestone": self.stock} if self.stock else {},
                "inventory_complete": self.complete, "inventory_includes_selected": True,
                "selected_item": "", "selected_item_known": True,
                "builder_active": self.active is not None, "builder_paused": self.active is not None,
                "any_process_active": self.active is not None, "pathing": False,
                "construction_progress": {"matching_cells": self.matched, "material_item": "cobblestone"},
                "active_action_known": True, "active_action_id": self.active}

    def command(self, objective, *, action_id, timeout_ms, on_progress):
        self.active = action_id
        self.started.set()
        while not self.cancelled.wait(.01):
            on_progress({**self.state(), "type": "progress", "action_id": action_id})
        self.active = None
        return RealClientTerminal(action_id, "canceled", "cancel_requested", self.state())

    def cancel(self, action_id):
        assert action_id == self.active
        self.cancelled.set()

    def world_read(self, scope, params):
        return {"ok": True, "complete": True, "data": {"cells": [
            {"x": x, "y": y, "z": z, "type": "minecraft:cobblestone" if x < self.matched else "minecraft:air"}
            for x, y, z in params["cells"]]}}


def make_runtime(ipc):
    body = RealClientBody("Bot", ipc)
    registry = ToolRegistry()
    for tool in client_body_tools(body):
        registry.register(tool)
    runtime = build_agent_runtime(body=body, registry=registry, goal_text="Fill the original cuboid").runtime
    runtime.direct_build_supervisor.suspicion_s = .04
    return runtime, registry.get("build_structure")


PARAMS = {"anchor": [0, 5, 0], "size": [3, 1, 1], "block": "cobblestone", "timeout_s": 30,
          "intent": "complete the declared building operation"}


def test_confirmed_blocker_stops_and_preserves_preexisting_world_and_native_receipt():
    asyncio.run(_test_confirmed_blocker_stops_and_preserves_preexisting_world_and_native_receipt())


async def _test_confirmed_blocker_stops_and_preserves_preexisting_world_and_native_receipt():
    ipc = NativeIpc()
    runtime, tool = make_runtime(ipc)
    # The slice-1 replay chooses the bounded-read branch explicitly; it must
    # not depend on the external Decision endpoint.
    runtime.call_supervisor._client = StubDecision(("inspect",))
    try:
        result = await asyncio.wait_for(runtime.run_tool(tool, execute_tool, tool, PARAMS,
            runtime.weld_context, tool_call_id="original-call", timeout_s=10), 3)
        original = result["metrics"]["original_result"]
        construction = original["metrics"]["construction"]
        assert original["reason"] == "cancel_requested"
        assert construction["matched_cells"] == construction["previously_matching_cells"] == 1
        assert construction["newly_matching_cells"] == 0
        assert len(construction["remaining_cells"]) == 2
        handoff = result["metrics"]["direct_call_handoff"]
        assert handoff["facts"]["reason"] == "material_exhausted"
        assert handoff["settlement"]["native_stopped"] and handoff["settlement"]["settled"]
        raw = [e for e in runtime.trace.events if e["event"] == "tool_raw_result"]
        assert len(raw) == 1 and raw[0]["tool_call_id"] == "original-call"
    finally:
        await runtime.aclose()


def test_incomplete_inventory_and_unknown_decision_do_not_stop_and_user_stop_needs_no_model():
    asyncio.run(_test_incomplete_inventory_and_unknown_decision_do_not_stop_and_user_stop_needs_no_model())


async def _test_incomplete_inventory_and_unknown_decision_do_not_stop_and_user_stop_needs_no_model():
    ipc = NativeIpc(complete=False)
    runtime, tool = make_runtime(ipc)
    judging = asyncio.Event()
    async def slow_unknown(*args):
        judging.set()
        await asyncio.sleep(60)
        return "yield"  # stale advice must never act after user stop
    runtime.direct_build_supervisor.judge = slow_unknown
    job = asyncio.create_task(runtime.run_tool(tool, execute_tool, tool, PARAMS,
        runtime.weld_context, tool_call_id="call", timeout_s=10))
    try:
        await asyncio.wait_for(judging.wait(), 2)
        assert not ipc.cancelled.is_set()
        facts = await asyncio.wait_for(runtime.cancel_active_execution_with_facts("user_stop"), 1)
        assert facts["native_stopped"] and facts["settled"]
        await asyncio.wait_for(job, 1)
        assert not runtime.direct_call_handoffs
    finally:
        await runtime.aclose()


def test_empty_hand_and_absent_inventory_keep_independent_unknowns():
    state = _body_state("Bot", {"position": [1, 2, 3]}, pending=False)
    assert state.selected_item_known is None
    assert state.inventory_counts is None and state.inventory_complete is None
    assert state.inventory_raw == ""
    known = _body_state("Bot", {"position": [1, 2, 3], "selected_item": "",
        "selected_item_known": True, "inventory": {}, "inventory_complete": True}, pending=False)
    assert known.selected_item is None and known.selected_item_known
    assert known.inventory_counts == {} and known.inventory_complete


def test_progress_observer_failure_does_not_replace_native_terminal():
    class Ipc(NativeIpc):
        def command(self, objective, *, action_id, timeout_ms, on_progress):
            on_progress({**self.state(), "type": "progress", "action_id": action_id})
            return RealClientTerminal(action_id, "completed", "world_result_verified", self.state())
    body = RealClientBody("Bot", Ipc())
    def fail(payload):
        raise ValueError("observer failure")
    with tool_observation_scope("call", fail):
        body.execute(Action("a", "buildStructure", PARAMS))
    event = body.await_action_terminal("a")
    assert event.data["terminal_source"] == "native" and event.data["success"]


def test_synthetic_transport_terminal_does_not_release_native_control():
    class Ipc(NativeIpc):
        def take_late_terminal(self, action_id):
            if self.active is None:
                return {"type": "terminal", "action_id": action_id, "status": "canceled",
                        "reason": "cancel_requested", **self.state()}
        def command(self, objective, *, action_id, timeout_ms, on_progress):
            self.active = action_id
            raise RealClientIpcError("disconnected while native action may still run")
    ipc = Ipc()
    body = RealClientBody("Bot", ipc)
    body.execute(Action("a", "buildStructure", PARAMS))
    event = body.await_action_terminal("a")
    assert event.data["terminal_source"] == "python_transport"
    assert not body.execute(Action("b", "buildStructure", PARAMS)).accepted
    assert not body.confirm_control_idle()["native_stopped"]
    ipc.active = None
    assert body.confirm_control_idle()["native_stopped"]
    late = [e for e in body.event_log if e.data.get("late_native_terminal")]
    assert len(late) == 1 and late[0].data["advice_superseded"]
    assert body.await_action_terminal("a") is event  # no second final receipt
    assert body.execute(Action("c", "buildStructure", PARAMS)).accepted
    body.await_action_terminal("c")


def test_post_state_failure_keeps_original_result_and_stale_generation_label():
    class Body:
        def __init__(self):
            self.reads = 0
        def get_state(self):
            self.reads += 1
            if self.reads > 1:
                raise TimeoutError("post-state unavailable")
            return _body_state("Bot", {"position": [1,2,3], "health":20}, pending=False)
    saved = []
    authority = ProgressAuthority()
    def invoke(params):
        authority.invalidate_generation("user_stop")
        return ToolResult(False, "cancel_requested", True, metrics={"matched_cells": 8})
    tool = RegisteredTool("build", "", {}, invoke, ToolSidecar("build", True))
    ctx = WeldContext(body=Body(), authority=authority, goal_text="build",
                      raw_result_observer=lambda *args: saved.append(args[-1]))
    result = execute_tool(tool, {}, ctx)
    assert saved[0].metrics["matched_cells"] == 8
    assert result["reason"] == "preempted"
    assert result["metrics"]["original_result"]["reason"] == "cancel_requested"
    assert result["metrics"]["post_state_unknown"] == "TimeoutError"


def test_inspect_budget_exhaustion_does_not_stop_body():
    asyncio.run(_inspect_budget())


def test_partial_world_read_keeps_known_cells_and_preserves_unknown():
    class Ipc(NativeIpc):
        def world_read(self, scope, params):
            result = super().world_read(scope, params)
            result["complete"] = False
            result["data"]["cells"][1] = {"x":1, "y":5, "z":0, "state":"UNKNOWN"}
            return result
    facts = read_construction(RealClientBody("Bot", Ipc()), PARAMS)
    assert facts["matched_cells"] == 1
    assert len(facts["remaining_cells"]) == 1
    assert facts["unknown_cells"] == [[1,5,0]] and not facts["complete"]


async def _inspect_budget():
    ipc = NativeIpc(complete=False)
    runtime, tool = make_runtime(ipc)
    runtime.direct_build_supervisor.review_interval_s = .04
    count = 0
    async def inspect(*args):
        nonlocal count
        count += 1
        return "inspect"
    runtime.direct_build_supervisor.judge = inspect
    job = asyncio.create_task(runtime.run_tool(tool, execute_tool, tool, PARAMS,
        runtime.weld_context, tool_call_id="budget", timeout_s=10))
    try:
        deadline = time.monotonic()+2
        while count < 4 and time.monotonic() < deadline:
            await asyncio.sleep(.01)
        assert count >= 1 and not ipc.cancelled.is_set()
        await asyncio.sleep(.1)
        assert count >= 1
        await runtime.cancel_active_execution_with_facts("user_stop")
        await asyncio.wait_for(job, 1)
        final = [e for e in runtime.trace.events if e["event"] == "call_supervision_finished"][-1]
        assert final["reads"] <= final["questions"] + 1 and 1 <= final["reads"] <= 4
    finally:
        await runtime.aclose()


class GenericCallIpc(NativeIpc):
    """A small native stand-in for non-build long calls."""

    def __init__(self, *, progressing=False):
        super().__init__(complete=True)
        self.progressing = progressing
        self.x = 0.0

    def state(self):
        state = super().state()
        state["position"] = [self.x, 5, 0]
        state["pathing"] = self.progressing
        state["navigation"] = {"pathing": self.progressing, "path_position": int(self.x)}
        return state

    def command(self, objective, *, action_id, timeout_ms, on_progress):
        self.active = action_id
        self.started.set()
        deadline = time.monotonic() + .35
        while time.monotonic() < deadline and not self.cancelled.wait(.01):
            if self.progressing:
                self.x += .75
            on_progress({**self.state(), "type": "progress", "action_id": action_id})
        self.active = None
        if self.cancelled.is_set():
            return RealClientTerminal(action_id, "canceled", "cancel_requested", self.state())
        return RealClientTerminal(action_id, "completed", "world_result_verified", self.state())


class ResetGenericCallIpc(GenericCallIpc):
    def command(self, objective, *, action_id, timeout_ms, on_progress):
        self.cancelled.clear()
        return super().command(objective, action_id=action_id, timeout_ms=timeout_ms, on_progress=on_progress)


class StubDecision:
    def __init__(self, answers):
        self.answers = iter(answers)
        self.requests = []

    async def ask(self, payload, *, priority):
        self.requests.append((payload, priority))
        choice = next(self.answers)
        # Tests written before the options were renamed still say step_done/continue.
        choice = {"step_done": "next_step", "continue": "keep_going"}.get(choice, choice)
        return DecisionAnswer(choice, {"answers": {"q": {"choice": choice}}}, 0.01, len(self.requests))

    async def close(self):
        return None


def _register_generic(runtime, registry, body, name="move_to"):
    def invoke(params):
        action = Action.create("moveTo", {"target": params.get("target", [1, 5, 0])})
        accepted = body.execute(action)
        if not accepted.accepted:
            return ToolResult(False, "action_rejected", True)
        terminal = body.await_action_terminal(action.id)
        return ToolResult(terminal.data.get("success", False), terminal.data.get("reason", "unknown"),
                          can_retry=not terminal.data.get("success", False), metrics=dict(terminal.data))
    tool = RegisteredTool(name, "Move to a target using native navigation.",
                          {"type": "object", "properties": {"target": {"type": "array"}},
                           "required": [], "additionalProperties": True}, invoke,
                          ToolSidecar(name, True, source="test", body_scope=("moveTo",), timeout_s=10),
                          supervision_explanation="Native navigation advances through movement and returns a terminal result.")
    registry.register(tool)
    return tool


def test_normal_long_non_build_call_uses_shared_supervisor_without_question():
    async def run():
        ipc = GenericCallIpc(progressing=True)
        runtime, _ = make_runtime(ipc)
        tool = _register_generic(runtime, runtime.registry, runtime.body)
        runtime.call_supervisor.suspicion_s = .04
        runtime.call_supervisor.review_interval_s = .02
        decision = StubDecision(("continue",))
        runtime.call_supervisor._client = decision
        try:
            # Exercise the per-Body lifecycle task rather than the
            # pre-session compatibility child monitor.
            await runtime.body_loop.start()
            result = await runtime.run_tool(tool, execute_tool, tool, {"target": [1, 5, 0], "intent": "reach the requested target"},
                                            runtime.weld_context, tool_call_id="move-call", timeout_s=2)
            assert result["success"]
            assert [e for e in runtime.trace.events
                    if e["event"] == "call_supervision_request" and e["tool_call_id"] == "move-call"]
            assert len(decision.requests) >= 1
        finally:
            await runtime.aclose()
    asyncio.run(run())


def test_shared_supervisor_yield_stops_generic_call_and_delivers_one_fact():
    async def run():
        ipc = GenericCallIpc(progressing=False)
        runtime, _ = make_runtime(ipc)
        tool = _register_generic(runtime, runtime.registry, runtime.body, name="collect_resource")
        runtime.call_supervisor.suspicion_s = .04
        runtime.call_supervisor.review_interval_s = .02
        decision = StubDecision(("step_done",))
        runtime.call_supervisor._client = decision
        try:
            await runtime.body_loop.start()
            result = await asyncio.wait_for(runtime.run_tool(
                tool, execute_tool, tool, {"target": [1, 5, 0], "intent": "reach the requested target"}, runtime.weld_context,
                tool_call_id="original-generic", timeout_s=2), 2)
            assert result["metrics"]["direct_call_handoff"]["facts"]["reason"] == "decision_step_done"
            assert result["reason"] != "decision_step_done"
            assert "supervision_step_done" not in result.get("metrics", {})
            original = result.get("metrics", {}).get("original_result", {})
            assert original.get("reason") == "cancel_requested"
            assert ipc.cancelled.is_set()
            raw = [e for e in runtime.trace.events if e["event"] == "tool_raw_result"]
            assert len(raw) == 1 and raw[0]["tool_call_id"] == "original-generic"
            assert len(decision.requests) == 1
        finally:
            await runtime.aclose()
    asyncio.run(run())


def test_unknown_answer_and_read_budget_never_trigger_supervisor_stop():
    async def run():
        ipc = GenericCallIpc(progressing=False)
        runtime, _ = make_runtime(ipc)
        tool = _register_generic(runtime, runtime.registry, runtime.body, name="smelt_item")
        runtime.call_supervisor.suspicion_s = .03
        runtime.call_supervisor.review_interval_s = .01
        runtime.call_supervisor.max_reads = 2
        # The second answer is deliberately outside the three-choice protocol;
        # it must be recorded as unknown just like an explicit unknown answer.
        decision = StubDecision(("inspect", "garbage"))
        runtime.call_supervisor._client = decision
        job = asyncio.create_task(runtime.run_tool(tool, execute_tool, tool, {"target": [1, 5, 0], "intent": "reach the requested target"},
                                                   runtime.weld_context, tool_call_id="bounded", timeout_s=2))
        try:
            await asyncio.sleep(.18)
            assert not [e for e in runtime.trace.events if e["event"] == "call_supervision_stop_requested"]
            assert not [e for e in runtime.trace.events if e["event"] == "call_supervision_read"]
            assert 1 <= len(decision.requests) <= 2
            await runtime.cancel_active_execution_with_facts("user_stop")
            await asyncio.wait_for(job, 1)
        finally:
            await runtime.aclose()
    asyncio.run(run())


def test_decision_timeout_is_unknown_and_never_stops_body():
    class TimeoutDecision:
        def __init__(self):
            self.requests = 0

        async def ask(self, payload, *, priority):
            self.requests += 1
            raise TimeoutError("decision timed out")

        async def close(self):
            return None

    async def run():
        ipc = GenericCallIpc(progressing=False)
        runtime, _ = make_runtime(ipc)
        tool = _register_generic(runtime, runtime.registry, runtime.body, name="collect_resource")
        runtime.call_supervisor.suspicion_s = .03
        runtime.call_supervisor.review_interval_s = .01
        decision = TimeoutDecision()
        runtime.call_supervisor._client = decision
        job = asyncio.create_task(runtime.run_tool(
            tool, execute_tool, tool, {"target": [1, 5, 0], "intent": "reach the requested target"}, runtime.weld_context,
            tool_call_id="decision-timeout", timeout_s=2))
        try:
            await asyncio.sleep(.10)
            assert decision.requests >= 1
            assert not [e for e in runtime.trace.events
                        if e["event"] == "call_supervision_stop_requested"]
            results = [e for e in runtime.trace.events
                       if e["event"] == "call_supervision_result"]
            assert results and all(e["choice"] == "unknown" for e in results)
            await runtime.cancel_active_execution_with_facts("user_stop")
            await asyncio.wait_for(job, 1)
        finally:
            await runtime.aclose()
    asyncio.run(run())


def test_plan_call_and_direct_call_share_the_same_supervisor():
    from minebot.app.work_runtime import WorkPlanExecutor

    async def run():
        ipc = GenericCallIpc(progressing=True)
        runtime, _ = make_runtime(ipc)
        tool = _register_generic(runtime, runtime.registry, runtime.body, name="craft_item")
        runtime.call_supervisor.suspicion_s = .04
        runtime.call_supervisor.review_interval_s = .02
        runtime.call_supervisor._client = StubDecision(("continue",))

        async def call(tool_name, arguments, *, call_id, timeout_s):
            return await runtime.run_tool(
                tool, execute_tool, tool, arguments, runtime.weld_context,
                tool_call_id=call_id, timeout_s=timeout_s or 2,
            )

        plan = {
            "language": "minebot-work/0.2",
            "timeout_s": 10,
            "goal": "perform one native call",
            "work": {"kind": "call", "id": "planned-call", "tool": "craft_item",
                     "arguments": {"target": [1, 5, 0], "intent": "reach the requested target"}},
        }
        try:
            result = await WorkPlanExecutor(plan, call=call).run()
            assert result.status == "completed"
            assert runtime.call_supervisor is runtime.direct_build_supervisor
            assert [e for e in runtime.trace.events if e["event"] == "call_supervision_finished"
                    and e["tool_call_id"] == "planned-call"]
        finally:
            await runtime.aclose()
    asyncio.run(run())


def test_mutating_call_without_intent_is_refused_before_the_body_moves():
    from minebot.app.runner import sdk_tool_for

    async def run():
        ipc = GenericCallIpc(progressing=True)
        runtime, _ = make_runtime(ipc)
        tool = _register_generic(runtime, runtime.registry, runtime.body, name="missing_intent_call")
        schema = sdk_tool_for(tool).params_json_schema
        assert "intent" in schema["required"] and "intent" in schema["properties"]
        try:
            result = await runtime.run_tool(
                tool, execute_tool, tool, {"target": [1, 5, 0]}, runtime.weld_context,
                tool_call_id="missing-intent", timeout_s=2,
            )
            assert result["reason"] == "missing_intent"
            assert not ipc.started.is_set()
        finally:
            await runtime.aclose()
    asyncio.run(run())


def test_plan_step_done_settles_and_allows_sequence_to_continue_without_agent_wake():
    from minebot.app.work_runtime import WorkPlanExecutor

    async def run():
        ipc = ResetGenericCallIpc(progressing=False)
        runtime, _ = make_runtime(ipc)
        first = _register_generic(runtime, runtime.registry, runtime.body, name="first_step")
        second = _register_generic(runtime, runtime.registry, runtime.body, name="second_step")
        runtime.call_supervisor._client = StubDecision(("next_step", "next_step"))

        async def call(tool_name, arguments, *, call_id, timeout_s):
            tool = runtime.registry.get(tool_name)
            return await runtime.run_tool(
                tool, execute_tool, tool, arguments, runtime.weld_context,
                tool_call_id=call_id, timeout_s=timeout_s or 2,
            )

        plan = {
            "language": "minebot-work/0.2", "timeout_s": 10,
            "goal": "finish two calls",
            "work": {"kind": "sequence", "intent": "complete both calls", "steps": [
                {"kind": "call", "id": "first", "tool": first.name, "arguments": {"intent": "complete the first body step"}},
                {"kind": "call", "id": "second", "tool": second.name, "arguments": {"intent": "complete the second body step"}},
            ]},
        }
        try:
            result = await asyncio.wait_for(WorkPlanExecutor(plan, call=call, decision=StubDecision(("next_step", "next_step"))).run(), 5)
            assert result.status == "completed"
            assert result.tree["work"]["_runtime"]["status"] == "done"
            assert result.tree["work"]["steps"][0]["_runtime"]["status"] == "done"
            assert result.tree["work"]["steps"][1]["_runtime"]["status"] == "done"
            assert not [e for e in runtime.trace.events if e["event"] == "call_supervision_idle_agent"]
        finally:
            await runtime.aclose()
    asyncio.run(run())


def test_snapshot_reuses_same_fact_frame_and_marks_phase_change():
    snapshots = ExecutionSnapshots()
    snapshots.begin("frame", "move_to", {})
    payload = {"position": [0, 5, 0], "pathing": True,
               "navigation": {"pathing": True, "path_position": 0},
               "goal": True}
    snapshots.observe("frame", payload)
    first = snapshots.current()
    snapshots.observe("frame", payload)
    same = snapshots.current()
    assert first["fact_revision"] == same["fact_revision"] == 1
    snapshots.observe("frame", {**payload, "builder_paused": True})
    changed = snapshots.current()
    assert changed["fact_revision"] == 2


def test_snapshot_tick_emits_latest_change_after_rate_limit():
    snapshots = ExecutionSnapshots()
    snapshots.begin("tick", "move_to", {})
    snapshots.observe("tick", {"position": [0, 5, 0], "pathing": True})
    first = snapshots.current()
    assert first["fact_revision"] == 1
    # A changed sample inside the cadence window is retained as facts but is
    # not yet a new frame. The next clock tick must expose that latest state.
    snapshots.observe("tick", {"position": [3, 5, 0], "pathing": True})
    assert snapshots.current()["fact_revision"] == 1
    snapshots._current.fact_frame_at -= snapshot_module.FACT_FRAME_INTERVAL_S
    snapshots.tick("tick")
    latest = snapshots.current()
    assert latest["fact_revision"] == 2
    assert latest["facts"]["position"] == [3.0, 5.0, 0.0]


def test_supervisor_clock_questions_when_body_sends_no_samples(monkeypatch):
    async def run():
        ipc = GenericCallIpc(progressing=False)
        runtime, _ = make_runtime(ipc)
        tool = _register_generic(runtime, runtime.registry, runtime.body, name="silent_long_call")
        # Keep the test short while preserving the one-frame-per-cadence rule.
        monkeypatch.setattr(supervision_module, "FACT_FRAME_INTERVAL_S", 0.05)
        monkeypatch.setattr(snapshot_module, "FACT_FRAME_INTERVAL_S", 0.05)

        decisions = []

        async def always_continue(*_args, **_kwargs):
            decisions.append(True)
            return "continue"

        runtime.call_supervisor.judge = always_continue
        original = ipc.command

        def silent_command(objective, *, action_id, timeout_ms, on_progress):
            # The native call remains active but never reports progress.
            return original(objective, action_id=action_id, timeout_ms=timeout_ms, on_progress=lambda _payload: None)

        ipc.command = silent_command
        try:
            result = await asyncio.wait_for(runtime.run_tool(
                tool, execute_tool, tool, {"target": [1, 5, 0], "intent": "reach the requested target"}, runtime.weld_context,
                tool_call_id="silent", timeout_s=2), 2)
            assert result["success"]
            assert len(decisions) >= 5
        finally:
            await runtime.aclose()
    asyncio.run(run())


def test_throttled_body_change_is_seen_on_the_next_clock_frame(monkeypatch):
    async def run():
        ipc = GenericCallIpc(progressing=False)
        runtime, _ = make_runtime(ipc)
        runtime.call_supervisor.suspicion_s = 0
        monkeypatch.setattr(supervision_module, "FACT_FRAME_INTERVAL_S", 0.05)
        monkeypatch.setattr(snapshot_module, "FACT_FRAME_INTERVAL_S", 0.05)
        seen = []

        def invoke(_params):
            observation = __import__("minebot.contract.execution", fromlist=["current_tool_observation"]).current_tool_observation()
            _call_id, notify = observation
            notify({"position": [0, 5, 0], "pathing": True})
            time.sleep(0.02)
            notify({"position": [3, 5, 0], "pathing": True})
            time.sleep(0.12)
            return ToolResult(True, "world_result_verified", False)

        tool = RegisteredTool(
            "moving_then_stuck", "A moving call", {"type": "object"}, invoke,
            ToolSidecar("moving_then_stuck", True, source="test"),
            supervision_explanation="The call reports movement and then keeps its latest state.",
        )
        runtime.registry.register(tool)

        class CaptureDecision:
            async def ask(self, payload, *, priority):
                seen.append(payload)
                return DecisionAnswer("continue", {"choice": "continue"}, 0.001, len(seen))
            async def close(self):
                return None

        runtime.call_supervisor._client = CaptureDecision()
        try:
            await runtime.run_tool(tool, execute_tool, tool, {"intent": "perform the requested body action"}, runtime.weld_context,
                                   tool_call_id="moving-stuck", timeout_s=2)
            locations = [payload["state"].get("position", "")
                         for payload in seen]
            assert any("now at [3.0, 5.0, 0.0]" in location for location in locations)
        finally:
            await runtime.aclose()
    asyncio.run(run())








def test_ambiguous_decision_is_unknown_and_does_not_stop():
    async def run():
        runtime, tool = make_runtime(GenericCallIpc())

        class Ambiguous:
            async def ask(self, payload, *, priority):
                return DecisionAnswer(
                    "call_agent",
                    {"answers": {"q": {"choice": "call_agent", "probabilities": {
                        "call_agent": 0.46, "continue": 0.44, "step_done": 0.10,
                    }}}},
                    0.2, 1,
                )

            async def close(self):
                return None

        runtime.call_supervisor._client = Ambiguous()
        try:
            choice = await runtime.call_supervisor.judge(
                "ambiguous", tool, {},
                {"state": "running", "phase": "moving", "fact_revision": 1,
                 "facts": {"position": [0, 5, 0]}},
                0.2, purpose="running_frame", supervision_context={"intent": "move safely"},
                options={"continue": "", "step_done": "", "call_agent": ""},
            )
            assert choice == "call_agent"
            assert not any(e["event"] == "decision_ambiguous" for e in runtime.trace.events)
        finally:
            await runtime.aclose()
    asyncio.run(run())








def test_plan_call_agent_has_one_manager_fact_and_no_supervisor_duplicate():
    async def run():
        ipc = GenericCallIpc(progressing=False)
        runtime, _ = make_runtime(ipc)
        tool = _register_generic(runtime, runtime.registry, runtime.body, name="plan_call_agent")
        runtime.call_supervisor._client = StubDecision(("call_agent",))
        delivered = []
        kernel_facts = []

        class Kernel:
            pending = []
            def notify_fact(self, fact):
                kernel_facts.append(fact)
            def halt(self):
                return None
            async def close(self):
                return None

        runtime.responses_kernel = Kernel()
        runtime.notify_reflex_fact = delivered.append
        manager = ReflexManager(runtime)
        manager.bind_loop(asyncio.get_running_loop())
        plan = {
            "language": "minebot-work/0.2", "goal": "finish the planned call", "timeout_s": 5,
            "work": {"kind": "call", "intent": "complete the planned body step", "id": "planned", "tool": tool.name, "arguments": {}},
        }
        try:
            accepted = manager.delegate({"plan": plan})
            activity_id = accepted["metrics"]["activity_id"]
            for _ in range(100):
                await asyncio.sleep(0.01)
                if manager.read(activity_id)["metrics"]["status"] in {"completed", "blocked"}:
                    break
            assert len(delivered) == 1, (
                manager._activities[activity_id].status,
                [(event["event"], event.get("reason")) for event in runtime.trace.events],
            )
            assert not kernel_facts
            fact = delivered[0]
            tree = fact["tree"]
            node_runtime = tree["work"]["_runtime"]
            assert fact["activity_id"] == activity_id
            assert fact["status"] == manager.read(activity_id)["metrics"]["status"]
            assert node_runtime["status"] in {"done", "blocked"}
            assert node_runtime.get("result", {}).get("reason") == "preempted"
            assert "plan-planned" in runtime.direct_call_handoffs
        finally:
            await manager.close()
            await runtime.aclose()
    asyncio.run(run())


def test_one_decision_in_flight_keeps_only_latest_fact_frame():
    async def run():
        ipc = GenericCallIpc(progressing=False)
        runtime, _ = make_runtime(ipc)

        def invoke(_params):
            observation = __import__("minebot.contract.execution", fromlist=["current_tool_observation"]).current_tool_observation()
            assert observation is not None
            _call_id, notify = observation
            notify({"position": [0, 5, 0], "pathing": True,
                    "navigation": {"pathing": True, "path_position": 0}, "goal": True})
            time.sleep(.03)
            notify({"position": [0, 5, 0], "pathing": False,
                    "builder_paused": True, "navigation": {"pathing": False, "path_position": 0}})
            time.sleep(.08)
            return ToolResult(True, "world_result_verified", False)

        tool = RegisteredTool(
            "frame_test", "A frame test", {"type": "object"}, invoke,
            ToolSidecar("frame_test", True, source="test"),
            supervision_explanation="The test call reports navigation and then a builder phase.",
        )
        runtime.registry.register(tool)

        class DelayedDecision:
            def __init__(self):
                self.started = asyncio.Event(); self.release = asyncio.Event(); self.payloads = []
            async def ask(self, payload, *, priority):
                self.payloads.append(payload); self.started.set()
                if len(self.payloads) == 1:
                    await self.release.wait()
                return DecisionAnswer("continue", {"answers": {"q": {"choice": "continue"}}}, .001, len(self.payloads))
            async def close(self):
                return None

        decision = DelayedDecision()
        runtime.call_supervisor._client = decision
        try:
            job = asyncio.create_task(runtime.run_tool(
                tool, execute_tool, tool, {"intent": "perform the requested body action"}, runtime.weld_context,
                tool_call_id="frame-test", timeout_s=2,
            ))
            await asyncio.wait_for(decision.started.wait(), 1)
            await asyncio.sleep(.05)
            assert len(decision.payloads) == 1
            decision.release.set()
            await asyncio.wait_for(job, 2)
            assert len(decision.payloads) == 2
            assert "builder_paused" in json.dumps(decision.payloads[1], ensure_ascii=False)
        finally:
            await runtime.aclose()
    asyncio.run(run())


def test_idle_supervision_is_owned_by_body_loop():
    async def run():
        runtime, _tool = make_runtime(GenericCallIpc())
        try:
            assert runtime.call_supervisor is runtime.body_loop
            assert not hasattr(runtime.call_supervisor, "supervise_idle")
        finally:
            await runtime.aclose()
    asyncio.run(run())


def test_plan_idle_result_uses_body_loop_result_judge():
    async def run():
        runtime, _tool = make_runtime(GenericCallIpc())
        try:
            assert callable(runtime.body_loop.decide_plan_result)
        finally:
            await runtime.aclose()
    asyncio.run(run())


def test_plan_step_stopped_by_decision_through_a_cancellation_still_advances():
    """A stopped tool may leave through its cancellation checkpoint instead of
    returning a result.  That ends the step, not the plan."""
    from minebot.app.work_runtime import WorkPlanExecutor
    from minebot.contract.execution import execution_checkpoint

    async def run():
        ipc = ResetGenericCallIpc(progressing=False)
        runtime, _ = make_runtime(ipc)
        second = _register_generic(runtime, runtime.registry, runtime.body, name="second_step")

        def invoke(params):
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                execution_checkpoint()
                time.sleep(.01)
            return ToolResult(False, "never_stopped", True)

        first = RegisteredTool(
            "checkpoint_step", "Work that leaves through its cancellation checkpoint.",
            {"type": "object", "properties": {}, "required": [], "additionalProperties": True}, invoke,
            ToolSidecar("checkpoint_step", True, source="test", body_scope=("moveTo",), timeout_s=10),
            supervision_explanation="Cooperative work that checks for a stop between small steps.")
        runtime.registry.register(first)
        runtime.call_supervisor.suspicion_s = .04
        runtime.call_supervisor._client = StubDecision(("next_step", "next_step"))

        async def call(tool_name, arguments, *, call_id, timeout_s):
            tool = runtime.registry.get(tool_name)
            return await runtime.run_tool(
                tool, execute_tool, tool, arguments, runtime.weld_context,
                tool_call_id=call_id, timeout_s=timeout_s or 5,
            )

        plan = {
            "language": "minebot-work/0.2", "timeout_s": 10,
            "goal": "finish two calls",
            "work": {"kind": "sequence", "intent": "complete both calls", "steps": [
                {"kind": "call", "id": "first", "tool": first.name, "arguments": {"intent": "complete the first body step"}},
                {"kind": "call", "id": "second", "tool": second.name, "arguments": {"intent": "complete the second body step"}},
            ]},
        }
        try:
            result = await asyncio.wait_for(WorkPlanExecutor(plan, call=call, decision=StubDecision(("next_step", "next_step"))).run(), 8)
            assert result.status == "completed"
            assert result.tree["work"]["steps"][0]["_runtime"]["status"] == "done"
            assert result.tree["work"]["steps"][1]["_runtime"]["status"] == "done"
            events = [e["event"] for e in runtime.trace.events if e.get("tool_call_id") == "first"]
            assert "call_supervision_stop_settled" in events and "direct_call_handoff" in events
        finally:
            await runtime.aclose()
    asyncio.run(run())


def test_plan_reports_blocked_when_a_call_is_cancelled_underneath_it():
    from minebot.app.work_runtime import WorkPlanExecutor

    async def run():
        async def call(tool_name, arguments, *, call_id, timeout_s):
            raise asyncio.CancelledError

        emitted = []
        plan = {
            "language": "minebot-work/0.2", "timeout_s": 10, "goal": "one call",
            "work": {"kind": "call", "id": "only", "tool": "anything", "arguments": {"intent": "do the step"}},
        }
        result = await WorkPlanExecutor(plan, call=call, emit=lambda event, **p: emitted.append((event, p))).run()
        assert result.status == "blocked"
        assert ("work_plan_blocked", {"reason": "call_cancelled"}) in emitted
    asyncio.run(run())
