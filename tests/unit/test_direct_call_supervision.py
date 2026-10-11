import asyncio
import threading
import time

from minebot.app.client_body_tools import client_body_tools
from minebot.app.construction_facts import read_construction
from minebot.app.decision_client import DecisionAnswer
from minebot.app.wiring import build_agent_runtime
from minebot.brain.registry import ToolRegistry, RegisteredTool, ToolSidecar, WeldContext, execute_tool
from minebot.brain.progress import ProgressAuthority
from minebot.game.real_client_body import RealClientBody, _body_state
from minebot.game.real_client_ipc import RealClientIpcError, RealClientTerminal
from minebot.contract import Action, ToolResult
from minebot.contract.execution import tool_observation_scope
from minebot.app.execution_snapshot import ExecutionSnapshots


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


PARAMS = {"anchor": [0, 5, 0], "size": [3, 1, 1], "block": "cobblestone", "timeout_s": 30}


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
        final = [e for e in runtime.trace.events if e["event"] == "direct_supervision_finished"][-1]
        assert final["reads"] <= final["questions"] and final["reads"] == 4
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
            result = await runtime.run_tool(tool, execute_tool, tool, {"target": [1, 5, 0]},
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
            result = await asyncio.wait_for(runtime.run_tool(
                tool, execute_tool, tool, {"target": [1, 5, 0]}, runtime.weld_context,
                tool_call_id="original-generic", timeout_s=2), 2)
            assert result["metrics"]["direct_call_handoff"]["facts"]["reason"] == "decision_step_done"
            assert result["success"] and result["reason"] == "decision_step_done"
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
        job = asyncio.create_task(runtime.run_tool(tool, execute_tool, tool, {"target": [1, 5, 0]},
                                                   runtime.weld_context, tool_call_id="bounded", timeout_s=2))
        try:
            await asyncio.sleep(.18)
            assert not [e for e in runtime.trace.events if e["event"] == "call_supervision_stop_requested"]
            assert len([e for e in runtime.trace.events if e["event"] == "call_supervision_read"]) >= 1
            assert len(decision.requests) == 2
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
            tool, execute_tool, tool, {"target": [1, 5, 0]}, runtime.weld_context,
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
                     "arguments": {"target": [1, 5, 0]}},
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


def test_plan_step_done_settles_and_allows_sequence_to_continue_without_agent_wake():
    from minebot.app.work_runtime import WorkPlanExecutor

    async def run():
        ipc = ResetGenericCallIpc(progressing=False)
        runtime, _ = make_runtime(ipc)
        first = _register_generic(runtime, runtime.registry, runtime.body, name="first_step")
        second = _register_generic(runtime, runtime.registry, runtime.body, name="second_step")
        runtime.call_supervisor._client = StubDecision(("step_done", "step_done"))

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
                {"kind": "call", "id": "first", "tool": first.name, "arguments": {}},
                {"kind": "call", "id": "second", "tool": second.name, "arguments": {}},
            ]},
        }
        try:
            result = await asyncio.wait_for(WorkPlanExecutor(plan, call=call).run(), 5)
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


def test_question_contains_goal_step_node_intent_and_fact_criteria():
    async def run():
        runtime, tool = make_runtime(GenericCallIpc())
        try:
            runtime.native_task_workspace = None
            execution = {"operation_id": "question", "state": "running", "phase": "moving",
                         "fact_revision": 4, "facts": {"position": [1, 5, 0]}}
            request = runtime.call_supervisor._question(
                "question", tool, {"target": [1, 5, 0]}, execution,
                idle_s=0.2, purpose="running_frame",
                supervision_context={"goal": "collect stone", "current_step": "travel to vein",
                                     "node_description": "reach the selected vein",
                                     "intent_chain": ["prepare materials", "reach target"],
                                     "is_plan": True},
            )
            state = request["state"]
            assert state["plan"]["goal"] == "collect stone"
            assert state["plan"]["current_step"] == "travel to vein"
            criteria = request["questions"]["q"]["criteria"]
            assert set(criteria) == {"continue", "inspect", "step_done", "call_agent"}
            assert all("reach the selected vein" in text for text in criteria.values())
            assert "real Body progress" in criteria["continue"]
            assert "insufficient" in criteria["inspect"]
            assert "requested amount is already enough" in criteria["step_done"]
            assert "off the intent" in criteria["call_agent"]
            assert state["request_chars"] <= 16_000
        finally:
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
                tool, execute_tool, tool, {}, runtime.weld_context,
                tool_call_id="frame-test", timeout_s=2,
            ))
            await asyncio.wait_for(decision.started.wait(), 1)
            await asyncio.sleep(.05)
            assert len(decision.payloads) == 1
            decision.release.set()
            await asyncio.wait_for(job, 2)
            assert len(decision.payloads) == 2
            assert decision.payloads[1]["state"]["execution"]["phase"] == "builder_paused"
        finally:
            await runtime.aclose()
    asyncio.run(run())


def test_idle_unknown_limit_calls_agent_without_stopping_body():
    async def run():
        runtime, tool = make_runtime(GenericCallIpc())
        runtime.execution_snapshots.begin("idle", tool.name, {})
        runtime.execution_snapshots.observe("idle", {"position": [0, 5, 0], "pathing": False})
        runtime.execution_snapshots.finish("idle", state="succeeded", reason="done", control="released")
        runtime.call_supervisor._client = StubDecision(("unknown", "unknown", "unknown"))
        delivered = []
        runtime.notify_reflex_fact = delivered.append
        try:
            counts = await runtime.call_supervisor.supervise_idle(
                tool, call_id="idle", duration_s=1, frame_interval_s=.001,
                plan_context={"goal": "collect", "current_step": "collect ore", "is_plan": False},
            )
            assert counts == {"questions": 3, "unknown": 3, "agent": 1}
            assert delivered and delivered[0]["status"] == "call_agent"
            assert not [e for e in runtime.trace.events if e["event"] == "call_supervision_stop_requested"]
        finally:
            await runtime.aclose()
    asyncio.run(run())
