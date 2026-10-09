import asyncio
import threading
import time

from minebot.app.client_body_tools import client_body_tools
from minebot.app.construction_facts import read_construction
from minebot.app.wiring import build_agent_runtime
from minebot.brain.registry import ToolRegistry, RegisteredTool, ToolSidecar, WeldContext, execute_tool
from minebot.brain.progress import ProgressAuthority
from minebot.game.real_client_body import RealClientBody, _body_state
from minebot.game.real_client_ipc import RealClientIpcError, RealClientTerminal
from minebot.contract import Action, ToolResult
from minebot.contract.execution import tool_observation_scope


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
        assert count == 4 and not ipc.cancelled.is_set()
        await asyncio.sleep(.1)
        assert count == 4
        await runtime.cancel_active_execution_with_facts("user_stop")
        await asyncio.wait_for(job, 1)
        final = [e for e in runtime.trace.events if e["event"] == "direct_supervision_finished"][-1]
        assert final["reads"] == final["questions"] == 4
    finally:
        await runtime.aclose()
