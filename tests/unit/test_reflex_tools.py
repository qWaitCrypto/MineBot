import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from minebot.app.body_control import BodyControl
from minebot.app.reflex_tools import ReflexManager
from minebot.app.work_runtime import WorkPlanError, WorkPlanExecutor, validate_plan
from minebot.app.runner import AgentRuntime
from minebot.brain.registry import RegisteredTool, ToolRegistry, ToolSidecar, WeldContext, execute_tool
from minebot.brain.progress import ProgressAuthority
from minebot.contract import BodyState, ToolResult
from minebot.contract.execution import execution_receipt_scope


class Decision:
    async def ask(self, payload, *, priority=None):
        return SimpleNamespace(choice="step_done", raw={"choice": "step_done"})


class Trace:
    def __init__(self):
        self.events = []
    def emit(self, event, **payload):
        self.events.append((event, payload))


def _bind_execution_observation(runtime):
    """What ``AgentRuntime.run_tool`` needs from a stand-in runtime."""
    from minebot.app.execution_snapshot import ExecutionSnapshots

    runtime.execution_snapshots = ExecutionSnapshots()
    runtime._observed = lambda callback, operation_id: AgentRuntime._observed(runtime, callback, operation_id)


def test_plan_result_question_uses_observations_from_the_call_before_projection():
    """Regression for live seq9186: result had coal12; cached question had coal3."""
    async def scenario():
        trace = Trace()
        runtime = SimpleNamespace(
            trace=trace, weld_context=None,
            registry=SimpleNamespace(get=lambda name: SimpleNamespace(
                name=name, sidecar=SimpleNamespace(timeout_s=None))),
            last_known_body_state={"bot": "Bot", "pos": [153.6, 65, 175.5],
                                   "health": 20, "inventory_counts": {"coal": 3}},
        )
        runtime._emit_last_known_body_state_trace = lambda **kw: (
            AgentRuntime._emit_last_known_body_state_trace(runtime, **kw))
        runtime.remember_tool_body_facts = lambda result: (
            AgentRuntime.remember_tool_body_facts(runtime, result))

        async def run_tool(*args, **kwargs):
            return {"success": True, "reason": "inventory_counted", "canRetry": False,
                    "metrics": {"counts": {"coal": 12}, "inventory_complete": True}}

        def project(name, result, call_id):
            # Projection runs after the ordinary settlement hook, not before it.
            assert runtime.last_known_body_state["inventory_counts"] == {"coal": 12}
            return {"success": result["success"], "reason": result["reason"],
                    "summary": {"counts": result["metrics"]["counts"]}}

        runtime.run_tool = run_tool
        runtime.shown_plan_result = project
        manager = ReflexManager(runtime)
        questions = []

        async def judge(**kwargs):
            questions.append(kwargs["question"])
            assert kwargs["decision_context"]["body_state"]["inventory_counts"] == {"coal": 12}
            assert kwargs["question"]["state"]["inventory"] == "coal 12"
            return "next_step"

        plan = {"language": "minebot-work/0.2", "goal": "coal is in the inventory", "timeout_s": 4,
                "work": {"kind": "call", "intent": "there are twelve coal in the inventory",
                         "tool": "read_inventory", "arguments": {}}}
        executor = WorkPlanExecutor(plan, call=manager._call, state=manager._state,
                                    decision=Decision(), result_judge=judge)
        # The manager's dispatch normally attaches the execution context.
        async def dispatch(name, arguments, *, call_id, timeout_s):
            return await manager._call(name, arguments, call_id=call_id, timeout_s=timeout_s,
                                       supervision_context=executor.call_context(call_id))
        executor.call = dispatch
        result = await executor.run()
        assert result.status == "completed"
        assert len(questions) == 1
        assert runtime.last_known_body_state["health"] == 20
        assert runtime.last_known_body_state["pos"] == [153.6, 65, 175.5]
        assert "metrics" not in executor.records[next(iter(executor.records))].result

    asyncio.run(scenario())


def test_plan_state_samples_current_body_even_when_the_agent_cache_is_populated():
    async def scenario():
        class Lane:
            async def run(self, callback):
                return callback()

        current = {"pos": [149.5, 57, 163.3], "health": 18,
                   "oxygen": 139, "inventory_counts": {"coal": 12}}
        runtime = SimpleNamespace(
            _read_lane=Lane(), body=SimpleNamespace(get_state=lambda: copy.deepcopy(current)),
            last_known_body_state={"pos": [153.6, 65, 175.5], "health": 20,
                                   "oxygen": 300, "inventory_counts": {"coal": 3}},
        )
        manager = ReflexManager(runtime)
        assert await manager._state() == current
        current["pos"][1] = 58
        current["oxygen"] = 134
        assert (await manager._state())["oxygen"] == 134

        def unavailable():
            raise OSError("body sampling unavailable")

        runtime.body.get_state = unavailable
        fallback = await manager._state()
        assert fallback == runtime.last_known_body_state
        fallback["inventory_counts"]["coal"] = 0
        assert runtime.last_known_body_state["inventory_counts"] == {"coal": 3}

    asyncio.run(scenario())


def test_delegate_and_same_tree_read_revision():
    async def scenario():
        trace = Trace()
        registry = SimpleNamespace(get=lambda name: SimpleNamespace(sidecar=SimpleNamespace(timeout_s=None)))
        runtime = SimpleNamespace(
            body_control=BodyControl(), decision_client=Decision(), trace=trace,
            responses_kernel=None, last_known_body_state={"bot": {"observed": True}},
            registry=registry, weld_context=None,
        )
        async def run_tool(tool, callback, *args, **kwargs):
            return {"success": True, "reason": "done", "metrics": {"delivered": args[1]}}
        runtime.run_tool = run_tool
        manager = ReflexManager(runtime)
        manager.bind_loop(asyncio.get_running_loop())
        plan = {"language": "minebot-work/0.2", "goal": "x", "timeout_s": 4,
                "work": {"kind": "call", "intent": "perform the planned physical step", "id": "one", "tool": "fake", "arguments": {"x": 1}}}
        accepted = manager.delegate({"plan": plan})
        assert accepted["success"]
        activity_id = accepted["metrics"]["activity_id"]
        await asyncio.sleep(0.05)
        read = manager.read(activity_id)
        assert read["success"]
        assert read["metrics"]["status"] == "completed"
        assert read["metrics"]["tree"]["work"]["_runtime"]["status"] == "done"
        await manager.close()

    asyncio.run(scenario())


def test_take_control_uses_runtime_stop_boundary():
    async def scenario():
        trace = Trace()
        stopped = asyncio.Event()
        async def cancel(reason):
            stopped.set()
            return {"settled": True, "native_stopped": True, "reason": reason}
        runtime = SimpleNamespace(body_control=BodyControl(), decision_client=Decision(), trace=trace,
                                  responses_kernel=None, last_known_body_state={}, registry={}, weld_context=None,
                                  cancel_active_execution_with_facts=cancel)
        manager = ReflexManager(runtime)
        manager.bind_loop(asyncio.get_running_loop())
        result = manager.control({"operation": "take_control"})
        assert result["success"] and result["reason"] == "take_control_requested"
        await asyncio.sleep(0)
        assert stopped.is_set()
    asyncio.run(scenario())


def test_delegate_waits_for_current_body_owner_then_starts():
    async def scenario():
        trace = Trace()
        control = BodyControl()
        control.claim("direct-call")
        registry = SimpleNamespace(get=lambda name: SimpleNamespace(sidecar=SimpleNamespace(timeout_s=None)))
        runtime = SimpleNamespace(
            body_control=control, decision_client=Decision(), trace=trace,
            responses_kernel=None, last_known_body_state={}, registry=registry,
            weld_context=None,
        )

        async def run_tool(tool, callback, *args, **kwargs):
            return {"success": True, "reason": "done", "metrics": {}}

        runtime.run_tool = run_tool
        manager = ReflexManager(runtime)
        manager.bind_loop(asyncio.get_running_loop())
        plan = {
            "language": "minebot-work/0.2", "goal": "x", "timeout_s": 4,
            "work": {"kind": "call", "intent": "perform the planned physical step", "id": "one", "tool": "fake", "arguments": {}},
        }
        accepted = manager.delegate({"plan": plan})
        activity_id = accepted["metrics"]["activity_id"]
        await asyncio.sleep(0.08)
        assert manager.read(activity_id)["metrics"]["status"] == "waiting"
        control.settle(claim_id="direct-call")
        for _ in range(20):
            await asyncio.sleep(0.01)
            if manager.read(activity_id)["metrics"]["status"] == "completed":
                break
        assert manager.read(activity_id)["metrics"]["status"] == "completed"
        await manager.close()

    asyncio.run(scenario())


def test_management_tool_mapping_uses_the_common_execute_path():
    registry = ToolRegistry()
    registry.register(RegisteredTool(
        "read_reflex", "read", {"type": "object"},
        lambda _params: {"success": True, "reason": "ok", "metrics": {"tree": {}}},
        ToolSidecar("reflex_read", mutating=False),
    ))
    ctx = WeldContext(
        body=SimpleNamespace(), authority=ProgressAuthority(), goal_text="goal",
    )
    payload = execute_tool(registry.get("read_reflex"), {}, ctx)
    assert payload["success"] is True


def test_execution_channel_records_delivery_without_public_metrics_field():
    state = BodyState(
        bot="MineBot", pos=(0.0, 64.0, 0.0), yaw=0.0, pitch=0.0,
        health=20.0, food=20, oxygen=None, inventory_raw="{}",
        inventory_hash="inventory", effects=[], time=0, weather="clear",
        dimension="minecraft:overworld", complete=True,
    )

    class Body:
        def get_state(self):
            return state

    registry = ToolRegistry()
    registry.register(RegisteredTool(
        "place_block", "place", {"type": "object"},
        lambda _params: ToolResult(True, "placed_server_confirmed", True),
        ToolSidecar("place", mutating=True, body_mutating=True),
    ))
    ctx = WeldContext(body=Body(), authority=ProgressAuthority(), goal_text="place")
    receipt = {}
    with execution_receipt_scope(receipt):
        payload = execute_tool(registry.get("place_block"), {}, ctx)
    assert payload["success"] is True
    assert "delivered" not in (payload.get("metrics") or {})
    assert receipt == {"delivered": True, "tool": "place_block"}


def test_delivery_receipt_survives_post_state_read_failure():
    state = BodyState(
        bot="MineBot", pos=(0.0, 64.0, 0.0), yaw=0.0, pitch=0.0,
        health=20.0, food=20, oxygen=None, inventory_raw="{}",
        inventory_hash="inventory", effects=[], time=0, weather="clear",
        dimension="minecraft:overworld", complete=True,
    )

    class FlakyBody:
        calls = 0
        def get_state(self):
            self.calls += 1
            if self.calls > 1:
                raise OSError("post-state unavailable")
            return state

    registry = ToolRegistry()
    registry.register(RegisteredTool(
        "place_block", "place", {"type": "object"},
        lambda _params: ToolResult(True, "placed_server_confirmed", True),
        ToolSidecar("place", mutating=True, body_mutating=True),
    ))
    ctx = WeldContext(body=FlakyBody(), authority=ProgressAuthority(), goal_text="place")
    receipt = {}
    with execution_receipt_scope(receipt):
        payload = execute_tool(registry.get("place_block"), {}, ctx)
    assert payload["success"] is True
    assert payload["post_state_unknown"] == "OSError"
    assert receipt == {"delivered": True, "tool": "place_block"}


def test_build_structure_run_tool_keeps_receipt_on_direct_supervisor_path():
    async def scenario():
        state = BodyState(
            bot="MineBot", pos=(0.0, 64.0, 0.0), yaw=0.0, pitch=0.0,
            health=20.0, food=20, oxygen=None, inventory_raw="{}",
            inventory_hash="inventory", effects=[], time=0, weather="clear",
            dimension="minecraft:overworld", complete=True,
        )

        class Body:
            def get_state(self):
                return state
            def confirm_control_idle(self):
                return {"native_stopped": True}

        class Lane:
            async def run(self, callback, *args, **kwargs):
                return callback(*args)

        seen_contexts = []

        class Supervisor:
            async def run(self, _tool, callback, args, **kwargs):
                seen_contexts.append(dict(kwargs.get("supervision_context") or {}))
                assert args[1] == {}
                return callback(*args)

        tool = RegisteredTool(
            "build_structure", "build", {"type": "object"},
            lambda _params: ToolResult(True, "structure_verified", True),
            ToolSidecar("build", mutating=True, body_mutating=True),
        )
        authority = ProgressAuthority()
        runtime = SimpleNamespace(
            body_control=BodyControl(authority), body=Body(), _read_lane=Lane(), execution_lane=Lane(),
            native_responses=False, direct_build_supervisor=Supervisor(),
        )
        weld = WeldContext(body=runtime.body, authority=authority, goal_text="build")
        _bind_execution_observation(runtime)
        result = await AgentRuntime.run_tool(
            runtime, tool, execute_tool, tool,
            {"intent": "lay the first wall and stop when it is complete"}, weld,
            return_execution_receipt=True,
        )
        assert result["success"] is True
        assert result["_execution"] == {"delivered": True, "tool": "build_structure"}
        direct_result = await AgentRuntime.run_tool(
            runtime, tool, execute_tool, tool,
            {"intent": "repeat the declared wall operation"}, weld,
        )
        assert "_execution" not in direct_result
        assert seen_contexts[0]["intent"] == "lay the first wall and stop when it is complete"
        assert seen_contexts[1]["intent"] == "repeat the declared wall operation"

    asyncio.run(scenario())


def test_run_tool_to_plan_executor_records_delivery_and_blocks_changed_write():
    async def scenario():
        state = BodyState(
            bot="MineBot", pos=(0.0, 64.0, 0.0), yaw=0.0, pitch=0.0,
            health=20.0, food=20, oxygen=None, inventory_raw="{}",
            inventory_hash="inventory", effects=[], time=0, weather="clear",
            dimension="minecraft:overworld", complete=True,
        )

        class Body:
            def get_state(self):
                return state

        class Lane:
            async def run(self, callback, *args, **kwargs):
                return callback(*args)

        class Trace:
            def emit(self, *_args, **_kwargs):
                pass

        calls = []
        tool = RegisteredTool(
            "place_block", "place", {"type": "object"},
            lambda params: (calls.append(dict(params)) or ToolResult(True, "placed_server_confirmed", True)),
            ToolSidecar("place", mutating=True, body_mutating=True),
        )
        registry = SimpleNamespace(get=lambda name: tool)
        weld = WeldContext(body=Body(), authority=ProgressAuthority(), goal_text="place")
        runtime = SimpleNamespace(
            body_control=BodyControl(weld.authority), body=Body(), _read_lane=Lane(), execution_lane=Lane(),
            native_responses=False, direct_build_supervisor=None, registry=registry,
            weld_context=weld, trace=Trace(), responses_kernel=None,
            last_known_body_state={}, decision_client=Decision(),
        )
        runtime.run_tool = lambda *args, **kwargs: AgentRuntime.run_tool(runtime, *args, **kwargs)
        _bind_execution_observation(runtime)

        manager = ReflexManager(runtime)
        manager.bind_loop(asyncio.get_running_loop())
        plan = {"language": "minebot-work/0.2", "goal": "place", "timeout_s": 4,
                "work": {"kind": "call", "intent": "perform the planned physical step", "id": "one", "tool": "place_block",
                          "arguments": {"pos": [1, 64, 1]}}}
        accepted = manager.delegate({"plan": plan})
        activity_id = accepted["metrics"]["activity_id"]
        for _ in range(20):
            await asyncio.sleep(0.01)
            if manager.read(activity_id)["metrics"]["status"] == "completed":
                break
        activity = manager._activities[activity_id]
        assert len(calls) == 1
        assert activity.records["one"].delivered is not None
        changed = copy.deepcopy(plan)
        changed["work"]["arguments"]["pos"] = [2, 64, 1]
        runtime.cancel_active_execution_with_facts = lambda *_args, **_kwargs: asyncio.sleep(0, result={"settled": True})
        manager.control({"activity_id": activity_id, "operation": "update", "spec": changed})
        for _ in range(20):
            await asyncio.sleep(0.01)
            if manager.read(activity_id)["metrics"]["status"] == "blocked":
                break
        assert len(calls) == 1
        assert manager.read(activity_id)["metrics"]["facts"].get("status") == "blocked"
        await manager.close()

    asyncio.run(scenario())


def test_waiting_activity_stays_live_and_rechecks_before_completion():
    async def scenario():
        class Answers:
            def __init__(self):
                self.calls = 0
            async def ask(self, payload, *, priority=None):
                self.calls += 1
                choice = "pending" if self.calls == 1 else "satisfied"
                return SimpleNamespace(choice=choice, raw={"choice": choice})

        trace = Trace()
        runtime = SimpleNamespace(
            body_control=BodyControl(), decision_client=Answers(), trace=trace,
            responses_kernel=None, last_known_body_state={}, registry={}, weld_context=None,
        )
        manager = ReflexManager(runtime)
        manager.bind_loop(asyncio.get_running_loop())
        plan = {"language": "minebot-work/0.2", "goal": "wait", "timeout_s": 2,
                "work": {"kind": "wait", "id": "arrival", "intent": "wait", "timeout_s": 1,
                          "condition": {"text": "arrived"}}}
        accepted = manager.delegate({"plan": plan})
        activity_id = accepted["metrics"]["activity_id"]
        await asyncio.sleep(0.01)
        assert manager.read(activity_id)["metrics"]["status"] == "waiting"
        for _ in range(30):
            await asyncio.sleep(0.01)
            if manager.read(activity_id)["metrics"]["status"] == "completed":
                break
        assert manager.read(activity_id)["metrics"]["status"] == "completed"
        assert runtime.decision_client.calls >= 2
        await manager.close()
    asyncio.run(scenario())


def test_plan_cancel_revokes_activity_before_native_stop():
    async def scenario():
        stopped = asyncio.Event()
        class PendingDecision:
            async def ask(self, payload, *, priority=None):
                return SimpleNamespace(choice="pending", raw={"choice": "pending"})
        async def stop(reason, **kwargs):
            stopped.set()
            return {"settled": True, "native_stopped": True, "reason": reason}
        trace = Trace()
        runtime = SimpleNamespace(
            body_control=BodyControl(), decision_client=PendingDecision(), trace=trace,
            responses_kernel=None, last_known_body_state={}, registry={}, weld_context=None,
            cancel_active_execution_with_facts=stop,
        )
        manager = ReflexManager(runtime)
        manager.bind_loop(asyncio.get_running_loop())
        plan = {"language": "minebot-work/0.2", "goal": "wait", "timeout_s": 5,
                "work": {"kind": "wait", "id": "arrival", "intent": "wait", "timeout_s": 4,
                          "condition": {"text": "arrived"}}}
        accepted = manager.delegate({"plan": plan})
        activity_id = accepted["metrics"]["activity_id"]
        await asyncio.sleep(0.01)
        result = manager.control({"operation": "cancel", "activity_id": activity_id})
        assert result["success"] and result["reason"] == "stop_requested"
        for _ in range(30):
            await asyncio.sleep(0.01)
            if manager.read(activity_id)["metrics"]["status"] == "cancelled":
                break
        assert stopped.is_set()
        assert manager.read(activity_id)["metrics"]["status"] == "cancelled"
        assert manager.read(activity_id)["metrics"]["facts"]["status"] == "cancelled"
        await manager.close()
    asyncio.run(scenario())


def test_idle_responses_runtime_starts_one_reflex_wakeup_turn():
    async def scenario():
        class Kernel:
            is_running = False
            def __init__(self):
                self.facts = []
            def notify_fact(self, fact):
                self.facts.append(fact)
        kernel = Kernel()
        runtime = object.__new__(AgentRuntime)
        runtime.responses_kernel = kernel
        runtime.native_responses = True
        runtime.event_wait_mode = True
        runtime.agent_context = SimpleNamespace(goal_text="continue the plan")
        runtime.trace = Trace()
        seen = asyncio.Event()
        async def run_responses(*_args, **_kwargs):
            seen.set()
        runtime._run_responses_turn = run_responses
        runtime._reflex_wakeup_task = None
        runtime.notify_reflex_fact({"activity_id": "a", "status": "completed"})
        await asyncio.wait_for(seen.wait(), timeout=1)
        assert kernel.facts[0]["status"] == "completed"
        await asyncio.sleep(0)
        assert runtime.event_wait_mode is True
    asyncio.run(scenario())


def test_sdk_runtime_delivers_reflex_fact_to_outer_session_listener():
    seen = []
    runtime = object.__new__(AgentRuntime)
    runtime.responses_kernel = None
    runtime._sdk_reflex_fact_listener = seen.append
    runtime.notify_reflex_fact({"activity_id": "a", "status": "blocked"})
    assert seen == [{"activity_id": "a", "status": "blocked"}]


def test_composite_plan_reports_missing_intent_at_node():
    plan = {
        "language": "minebot-work/0.2",
        "goal": "move then place",
        "timeout_s": 30,
        "work": {
            "kind": "sequence",
            "steps": [{"kind": "call", "tool": "move_to", "arguments": {}}],
        },
    }
    try:
        validate_plan(plan)
    except WorkPlanError as exc:
        assert str(exc) == "'intent' is a required property at work"
    else:
        raise AssertionError("missing sequence intent was accepted")


def test_delegate_rejects_plan_without_outer_intent():
    runtime = SimpleNamespace(
        trace=SimpleNamespace(emit=lambda *args, **kwargs: None),
        responses_kernel=None, registry={},
    )
    manager = ReflexManager(runtime)
    result = manager.delegate({
        "plan": {"language": "minebot-work/0.2", "goal": "move", "timeout_s": 5,
                  "work": {"kind": "call", "tool": "move_to", "arguments": {}}}
    })
    assert result["reason"] == "missing_intent"
    assert result["metrics"]["missing"] == "work.intent"


def test_changed_completed_call_uses_execution_receipt_not_tool_metrics():
    async def scenario():
        calls = []
        records = {}
        plan = {
            "language": "minebot-work/0.2", "goal": "place", "timeout_s": 4,
            "work": {"kind": "call", "intent": "perform the planned physical step", "id": "place", "tool": "place_block", "arguments": {"pos": [1, 2, 3]}},
        }

        async def call(_tool, arguments, *, call_id, timeout_s):
            calls.append(arguments)
            return {
                "success": True,
                "reason": "placed_server_confirmed",
                "metrics": {},
                "_execution": {"delivered": True, "tool": "place_block"},
            }

        first = await WorkPlanExecutor(plan, call=call, records=records, decision=Decision()).run()
        assert first.status == "completed"
        changed = {**plan, "work": {**plan["work"], "arguments": {"pos": [4, 2, 3]}}}
        second = await WorkPlanExecutor(changed, call=call, records=records, decision=Decision()).run()
        assert second.status == "blocked"
        assert second.tree["work"]["_runtime"]["blocked"]["reason"] == "completed_obligation_changed"
        assert calls == [{"pos": [1, 2, 3]}]

    asyncio.run(scenario())


def test_update_publishes_new_revision_before_async_native_settlement():
    async def scenario():
        trace = Trace()
        registry = SimpleNamespace(get=lambda name: SimpleNamespace(sidecar=SimpleNamespace(timeout_s=None)))
        runtime = SimpleNamespace(
            body_control=BodyControl(), decision_client=Decision(), trace=trace,
            responses_kernel=None, last_known_body_state={"bot": {"observed": True}},
            registry=registry, weld_context=None,
        )

        async def run_tool(tool, callback, *args, **kwargs):
            return {"success": True, "reason": "done", "metrics": {}}

        async def stop(reason, **kwargs):
            return {"settled": True, "native_stopped": True, "reason": reason}

        runtime.run_tool = run_tool
        runtime.cancel_active_execution_with_facts = stop
        manager = ReflexManager(runtime)
        manager.bind_loop(asyncio.get_running_loop())
        first = {"language": "minebot-work/0.2", "goal": "first", "timeout_s": 4,
                 "work": {"kind": "call", "intent": "perform the planned physical step", "id": "one", "tool": "fake", "arguments": {"x": 1}}}
        second = {"language": "minebot-work/0.2", "goal": "second", "timeout_s": 4,
                  "work": {"kind": "call", "intent": "perform the planned physical step", "id": "one", "tool": "fake", "arguments": {"x": 2}}}
        accepted = manager.delegate({"plan": first})
        activity_id = accepted["metrics"]["activity_id"]
        for _ in range(20):
            await asyncio.sleep(0.01)
            if manager.read(activity_id)["metrics"]["status"] == "completed":
                break
        updated = manager.control({"operation": "update", "activity_id": activity_id, "spec": second})
        assert updated["metrics"]["status"] == "updating"
        immediate = manager.read(activity_id)["metrics"]
        assert immediate["status"] == "updating"
        assert immediate["revision"] == 2
        assert immediate["tree"]["work"]["arguments"] == {"x": 2}
        await manager.close()

    asyncio.run(scenario())


def test_update_cannot_be_overwritten_by_old_run_finishing_late():
    async def scenario():
        trace = Trace()
        gate = asyncio.Event()
        calls = []
        registry = SimpleNamespace(get=lambda name: SimpleNamespace(sidecar=SimpleNamespace(timeout_s=None)))
        runtime = SimpleNamespace(
            body_control=BodyControl(), decision_client=Decision(), trace=trace,
            responses_kernel=None, last_known_body_state={}, registry=registry, weld_context=None,
        )

        async def run_tool(_tool, _callback, _tool_arg, arguments, *_args, **_kwargs):
            value = arguments["x"]
            calls.append(value)
            if value == 1:
                try:
                    await gate.wait()
                except asyncio.CancelledError:
                    await gate.wait()
            return {"success": True, "reason": f"done-{value}", "metrics": {"value": value}}

        async def stop(_reason, **_kwargs):
            return {"settled": True, "native_stopped": True}

        runtime.run_tool = run_tool
        runtime.cancel_active_execution_with_facts = stop
        manager = ReflexManager(runtime)
        manager.bind_loop(asyncio.get_running_loop())
        first = {"language": "minebot-work/0.2", "goal": "first", "timeout_s": 4,
                 "work": {"kind": "call", "intent": "perform the planned physical step", "id": "one", "tool": "fake", "arguments": {"x": 1}}}
        second = {"language": "minebot-work/0.2", "goal": "second", "timeout_s": 4,
                  "work": {"kind": "call", "intent": "perform the planned physical step", "id": "one", "tool": "fake", "arguments": {"x": 2}}}
        accepted = manager.delegate({"plan": first})
        activity_id = accepted["metrics"]["activity_id"]
        await asyncio.sleep(0.03)
        updated = manager.control({"operation": "update", "activity_id": activity_id, "spec": second})
        assert updated["metrics"]["status"] == "updating"
        assert manager.read(activity_id)["metrics"]["status"] == "updating"
        gate.set()
        for _ in range(50):
            await asyncio.sleep(0.01)
            if manager.read(activity_id)["metrics"]["status"] == "completed":
                break
        activity = manager._activities[activity_id]
        assert calls == [1, 2]
        assert activity.result is not None
        assert activity.result.results["one"]["metrics"]["value"] == 2
        assert manager.read(activity_id)["metrics"]["facts"]["status"] == "completed"
        await manager.close()

    asyncio.run(scenario())


def test_update_of_a_waiting_plan_starts_the_new_plan():
    """A waiting plan sleeps in the manager between checks; the update cancels that
    sleep. The replacement must still start (easy run-11: "updating" for 16 minutes)."""
    async def scenario():
        answer = {"choice": "pending"}

        class SwitchingDecision:
            async def ask(self, payload, *, priority=None):
                return SimpleNamespace(choice=answer["choice"], raw=dict(answer))

        calls = []
        registry = SimpleNamespace(get=lambda name: SimpleNamespace(sidecar=SimpleNamespace(timeout_s=None)))
        runtime = SimpleNamespace(
            body_control=BodyControl(), decision_client=SwitchingDecision(), trace=Trace(),
            responses_kernel=None, last_known_body_state={}, registry=registry, weld_context=None,
        )

        async def run_tool(_tool, _callback, _tool_arg, arguments, *_args, **_kwargs):
            calls.append(arguments["x"])
            return {"success": True, "reason": "done", "metrics": {"value": arguments["x"]}}

        async def stop(_reason, **_kwargs):
            return {"settled": True, "native_stopped": True}

        runtime.run_tool = run_tool
        runtime.cancel_active_execution_with_facts = stop
        manager = ReflexManager(runtime)
        manager.bind_loop(asyncio.get_running_loop())
        waiting = {"language": "minebot-work/0.2", "goal": "wait", "timeout_s": 30,
                   "work": {"kind": "wait", "id": "dawn", "intent": "it is day", "timeout_s": 20,
                            "condition": {"text": "it is day"}}}
        second = {"language": "minebot-work/0.2", "goal": "second", "timeout_s": 4,
                  "work": {"kind": "call", "intent": "perform the planned physical step", "id": "one", "tool": "fake", "arguments": {"x": 2}}}
        activity_id = manager.delegate({"plan": waiting})["metrics"]["activity_id"]
        for _ in range(50):
            await asyncio.sleep(0.01)
            if manager.read(activity_id)["metrics"]["status"] == "waiting":
                break
        assert manager.read(activity_id)["metrics"]["status"] == "waiting"
        answer["choice"] = "step_done"
        assert manager.control({"operation": "update", "activity_id": activity_id, "spec": second})["metrics"]["status"] == "updating"
        for _ in range(100):
            await asyncio.sleep(0.01)
            if manager.read(activity_id)["metrics"]["status"] == "completed":
                break
        assert calls == [2]
        assert manager.read(activity_id)["metrics"]["status"] == "completed"
        await manager.close()

    asyncio.run(scenario())


def test_unknown_body_control_blocks_plan_and_wakes_agent_instead_of_waiting():
    async def scenario():
        trace = Trace()
        facts = []

        class Body:
            def confirm_control_idle(self):
                return {"native_stopped": False}

        runtime = SimpleNamespace(
            body=Body(), body_control=BodyControl(), decision_client=Decision(), trace=trace,
            responses_kernel=None, last_known_body_state={}, registry={}, weld_context=None,
            notify_reflex_fact=facts.append,
        )
        runtime.body_control.mark_unknown()
        manager = ReflexManager(runtime)
        manager.bind_loop(asyncio.get_running_loop())
        plan = {"language": "minebot-work/0.2", "goal": "blocked", "timeout_s": 2,
                "work": {"kind": "call", "intent": "perform the planned physical step", "id": "one", "tool": "fake", "arguments": {}}}
        accepted = manager.delegate({"plan": plan})
        activity_id = accepted["metrics"]["activity_id"]
        for _ in range(30):
            await asyncio.sleep(0.01)
            if manager.read(activity_id)["metrics"]["status"] == "blocked":
                break
        result = manager.read(activity_id)["metrics"]
        assert result["status"] == "blocked"
        assert result["facts"]["reason"] == "control_unknown"
        assert facts and facts[-1]["status"] == "blocked"
        assert not any(event == "reflex_activity_waiting_current" for event, _ in trace.events)
        await manager.close()

    asyncio.run(scenario())


def test_plan_that_raises_tells_the_agent_the_error(monkeypatch):
    # Villa run 2026-10-15: three plans died on a storage ValueError and the
    # Agent only saw "blocked" with empty facts, so it blamed the terrain.
    import minebot.app.reflex_tools as reflex_tools

    async def boom(self):
        raise ValueError("observations exceeds 128 items")

    monkeypatch.setattr(reflex_tools.WorkPlanExecutor, "run", boom)

    async def scenario():
        facts = []
        runtime = SimpleNamespace(
            body=SimpleNamespace(), body_control=BodyControl(), decision_client=Decision(), trace=Trace(),
            responses_kernel=None, last_known_body_state={}, registry={}, weld_context=None,
            notify_reflex_fact=facts.append,
        )
        manager = ReflexManager(runtime)
        manager.bind_loop(asyncio.get_running_loop())
        plan = {"language": "minebot-work/0.2", "goal": "survey", "timeout_s": 2,
                "work": {"kind": "call", "intent": "perform the planned physical step", "id": "one", "tool": "fake", "arguments": {}}}
        activity_id = manager.delegate({"plan": plan})["metrics"]["activity_id"]
        for _ in range(50):
            await asyncio.sleep(0.01)
            if facts:
                break
        assert facts and facts[-1]["status"] == "blocked"
        assert "internal error" in facts[-1]["error"] and "observations exceeds 128 items" in facts[-1]["error"]
        assert "observations exceeds 128 items" in manager.read(activity_id)["metrics"]["error"]
        await manager.close()

    asyncio.run(scenario())


def test_plan_step_context_says_whether_the_intent_is_its_own():
    from types import SimpleNamespace
    plan = {"language": "minebot-work/0.2", "timeout_s": 4, "goal": "stone pickaxe", "work": {"kind": "sequence", "intent": "make a stone pickaxe", "steps": [
        {"kind": "call", "id": "stone", "tool": "collect_resource", "arguments": {"item": "cobblestone", "count": 3}},
        {"kind": "call", "id": "sticks", "tool": "craft_item", "intent": "have 2 sticks", "arguments": {"item": "stick"}},
        {"kind": "call", "id": "pick", "tool": "craft_item",
         "arguments": {"item": "stone_pickaxe", "intent": "hold a stone pickaxe"}},
    ]}}
    executor = WorkPlanExecutor(plan, call=None)
    steps = plan["work"]["steps"]
    stone = executor._call_plan(steps[0], ["make a stone pickaxe"])
    assert stone["intent"] == "make a stone pickaxe" and stone["intent_of_plan"] is True
    sticks = executor._call_plan(steps[1], ["make a stone pickaxe", "have 2 sticks"])
    assert sticks["intent"] == "have 2 sticks" and sticks["intent_of_plan"] is False
    pick = executor._call_plan(steps[2], ["make a stone pickaxe"])
    assert pick["intent"] == "hold a stone pickaxe" and pick["intent_of_plan"] is False


@pytest.mark.parametrize("work,expected", [
    ({"kind": "sequence", "intent": "make a stone pickaxe", "steps": [
        {"kind": "call", "tool": "collect_resource", "arguments": {
            "item": "logs", "count": 4, "intent": "have 4 logs in the inventory"}},
        {"kind": "call", "tool": "craft_item", "arguments": {"item": "planks"}},
    ]}, [("have 4 logs in the inventory", False), ("make a stone pickaxe", True)]),
    ({"kind": "batch", "intent": "collect the required materials", "items": ["logs", "coal"],
      "body": {"kind": "call", "id": "gather", "tool": "collect_resource",
               "arguments": {"item": {"ref": "item"}, "count": 1}}},
     [("collect the required materials", True), ("collect the required materials", True)]),
])
def test_dispatched_plan_call_keeps_agent_intent_without_matching_explicit_ids(work, expected):
    async def scenario():
        seen = []
        runtime = SimpleNamespace(
            body_control=BodyControl(), decision_client=Decision(), trace=Trace(),
            responses_kernel=None, last_known_body_state={}, weld_context=None,
            registry=SimpleNamespace(get=lambda name: SimpleNamespace(
                name=name, sidecar=SimpleNamespace(timeout_s=1))),
        )

        async def run_tool(tool, callback, *args, **kwargs):
            seen.append((kwargs["tool_call_id"], copy.deepcopy(kwargs["supervision_context"])))
            return {"success": True, "reason": "done", "canRetry": True, "metrics": {}}

        runtime.run_tool = run_tool
        manager = ReflexManager(runtime)
        manager.bind_loop(asyncio.get_running_loop())
        accepted = manager.delegate({"plan": {
            "language": "minebot-work/0.2", "goal": "stone pickaxe", "timeout_s": 4,
            "work": work,
        }})
        activity = manager._activities[accepted["metrics"]["activity_id"]]
        await activity.task
        assert activity.status == "completed"
        assert [(context["intent"], context["intent_of_plan"]) for _, context in seen] == expected
        assert all(context["node_description"] == context["intent"] for _, context in seen)
        if work["kind"] == "sequence":
            assert "craft_item" in seen[0][1]["next_call"]
            assert seen[0][0].startswith("plan-legacy:")
        else:
            assert [call_id for call_id, _ in seen] == ["plan-gather__item_0", "plan-gather__item_1"]
        await manager.close()

    asyncio.run(scenario())


def _progress_state(**inventory):
    return BodyState(
        bot="Bot", pos=(0.0, 64.0, 0.0), yaw=0.0, pitch=0.0, health=20.0, food=20, oxygen=300,
        inventory_raw="[]", inventory_hash="inventory", effects=[], time=0, weather="clear",
        dimension="minecraft:overworld", complete=True, inventory_counts=dict(inventory),
    )


def test_plan_calls_do_not_feed_the_agents_progress_counters(monkeypatch):
    # Live run reflex-stage-20261012d: a plan's own failed attempts filled the
    # counters, and the Agent's next, different plan was stopped for no progress.
    import minebot.app.reflex_tools as reflex_tools

    authority = ProgressAuthority()

    def failing(tool, arguments, context):
        context.authority.note_step(("collect", "iron"), False, "same-place")
        return {"success": False, "reason": "not_found"}

    monkeypatch.setattr(reflex_tools, "execute_tool", failing)
    for _ in range(6):
        reflex_tools._execute_plan_call(None, {}, SimpleNamespace(authority=authority))
    assert (authority.stagnant_steps, authority.stalled_steps, authority.failure_steps) == (0, 0, 0)


def test_a_finished_plan_is_one_step_for_the_agents_progress_counters():
    async def scenario(result, after, choice):
        trace = Trace()
        authority = ProgressAuthority()
        states = iter([_progress_state(), after])
        registry = SimpleNamespace(get=lambda name: SimpleNamespace(sidecar=SimpleNamespace(timeout_s=None)))

        class Chooser:
            async def ask(self, payload, *, priority=None):
                return SimpleNamespace(choice=choice, raw={"choice": choice})

        async def read_progress_state():
            return next(states)

        async def run_tool(tool, callback, *args, **kwargs):
            return dict(result)

        runtime = SimpleNamespace(
            body_control=BodyControl(), decision_client=Chooser(), trace=trace, responses_kernel=None,
            last_known_body_state={}, registry=registry, weld_context=None, authority=authority,
            read_progress_state=read_progress_state, run_tool=run_tool,
        )
        manager = ReflexManager(runtime)
        manager.bind_loop(asyncio.get_running_loop())
        plan = {"language": "minebot-work/0.2", "goal": "x", "timeout_s": 4,
                "work": {"kind": "call", "intent": "get the iron", "id": "one", "tool": "fake", "arguments": {}}}
        activity_id = manager.delegate({"plan": plan})["metrics"]["activity_id"]
        for _ in range(40):
            if manager.read(activity_id)["metrics"]["status"] not in {"accepted", "running"}:
                break
            await asyncio.sleep(0.02)
        for _ in range(40):
            noted = [payload for event, payload in trace.events if event == "reflex_activity_progress_noted"]
            if noted:
                break
            await asyncio.sleep(0.02)
        await manager.close()
        return authority, noted

    failed = {"success": False, "reason": "not_found", "canRetry": True, "metrics": {}}
    authority, noted = asyncio.run(scenario(failed, _progress_state(), "call_agent"))
    assert len(noted) == 1 and noted[0]["material_evidence"] == []
    assert authority.failure_steps == 1

    authority, noted = asyncio.run(scenario(failed, _progress_state(raw_iron=2), "call_agent"))
    assert noted[0]["material_evidence"] == ["inventory_gain"]
    assert authority.failure_steps == 0

    done = {"success": True, "reason": "done", "metrics": {}}
    authority, noted = asyncio.run(scenario(done, _progress_state(), "next_step"))
    assert noted[0]["status"] == "completed" and authority.failure_steps == 0


class ScriptedDecision:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.payloads = []

    async def ask(self, payload, *, priority=None):
        self.payloads.append(payload)
        choice = self.answers.pop(0) if self.answers else "step_done"
        return SimpleNamespace(choice=choice, raw={"choice": choice})


def test_resuming_a_handed_back_plan_waits_for_the_previous_run_to_settle():
    """Review 2026-10-17: a resume while the old run was still settling let
    the old run's finally release the claim of the resumed call."""
    async def scenario():
        trace = Trace()
        settling = asyncio.Event()
        repeat_started = asyncio.Event()
        finish_repeat = asyncio.Event()
        reads = []
        calls = []
        owned_during_repeat = []
        registry = SimpleNamespace(get=lambda name: SimpleNamespace(sidecar=SimpleNamespace(timeout_s=None)))
        runtime = SimpleNamespace(
            body_control=BodyControl(), decision_client=ScriptedDecision("call_agent", "repeat", "step_done"),
            trace=trace, responses_kernel=None, last_known_body_state={}, registry=registry, weld_context=None,
        )

        async def read_progress_state():
            reads.append(1)
            if len(reads) == 2:  # the first run noting its outcome
                await settling.wait()
            return None

        async def run_tool(_tool, _callback, _tool_arg, arguments, *_args, **_kwargs):
            calls.append(arguments["x"])
            if len(calls) == 2:
                repeat_started.set()
                await finish_repeat.wait()
                owned_during_repeat.append(runtime.body_control.owns(activity_id))
            return {"success": True, "reason": "done", "metrics": {}}

        runtime.read_progress_state = read_progress_state
        runtime.run_tool = run_tool
        manager = ReflexManager(runtime)
        manager.bind_loop(asyncio.get_running_loop())
        plan = {"language": "minebot-work/0.2", "goal": "x", "timeout_s": 4,
                "work": {"kind": "call", "intent": "perform the planned physical step", "id": "one",
                         "tool": "fake", "arguments": {"x": 1}}}
        activity_id = manager.delegate({"plan": plan})["metrics"]["activity_id"]
        for _ in range(50):
            await asyncio.sleep(0.01)
            if manager.read(activity_id)["metrics"]["status"] == "needs_agent":
                break
        assert manager.read(activity_id)["metrics"]["status"] == "needs_agent"
        manager.control({"operation": "resume", "activity_id": activity_id})
        await asyncio.sleep(0.05)
        assert calls == [1]  # nothing new runs while the first run is settling
        settling.set()
        await asyncio.wait_for(repeat_started.wait(), 1.0)
        await asyncio.sleep(0.05)
        finish_repeat.set()
        for _ in range(50):
            await asyncio.sleep(0.01)
            if manager.read(activity_id)["metrics"]["status"] == "completed":
                break
        assert calls == [1, 1]
        assert owned_during_repeat == [True]
        assert manager.read(activity_id)["metrics"]["status"] == "completed"
        await manager.close()

    asyncio.run(scenario())


def _queue_runtime(decision, release):
    """A runtime whose calls run until ``release`` is set for their x."""
    trace = Trace()
    facts = []
    calls = []
    registry = SimpleNamespace(get=lambda name: SimpleNamespace(sidecar=SimpleNamespace(timeout_s=None)))
    runtime = SimpleNamespace(
        body_control=BodyControl(), decision_client=decision, trace=trace,
        responses_kernel=None, last_known_body_state={}, registry=registry, weld_context=None,
        notify_reflex_fact=facts.append,
    )

    async def run_tool(_tool, _callback, _tool_arg, arguments, *_args, **_kwargs):
        calls.append(arguments["x"])
        await release[arguments["x"]].wait()
        return {"success": True, "reason": "done", "metrics": {}}

    runtime.run_tool = run_tool
    return runtime, facts, calls


def _plan(x, goal):
    return {"language": "minebot-work/0.2", "goal": goal, "timeout_s": 4,
            "work": {"kind": "call", "intent": f"step {x} done", "id": f"n{x}", "tool": "fake", "arguments": {"x": x}}}


async def _until(predicate, steps=100):
    for _ in range(steps):
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


def test_a_plan_delegated_while_another_runs_waits_on_the_ladder_not_in_a_queue(tmp_path):
    async def scenario():
        release = {1: asyncio.Event(), 2: asyncio.Event()}
        runtime, facts, calls = _ladder_runtime(tmp_path, Decision(), release)
        manager = ReflexManager(runtime)
        manager.bind_loop(asyncio.get_running_loop())
        manager.delegate({"plan": _plan(1, "dig the stairs")})
        await _until(lambda: calls == [1])
        queued = manager.delegate({"plan": _plan(2, "build the room")})
        assert queued["success"] and queued["reason"] == "on_ladder_after_running_plan"
        assert manager.body_busy()
        release[1].set()
        assert await _until(lambda: len(facts) == 1)
        # Nothing starts by itself: Decision picks the next item in an idle frame.
        await asyncio.sleep(0.05)
        assert calls == [1] and "next_plan_started" not in facts[0]
        assert runtime.life_store.project_state({"id": "work-2"})["state"] == "waiting"
        assert not manager.body_busy()
        await manager.close()

    asyncio.run(scenario())


def test_without_a_ladder_a_second_plan_is_refused_while_one_runs():
    async def scenario():
        release = {1: asyncio.Event()}
        runtime, facts, calls = _queue_runtime(Decision(), release)
        manager = ReflexManager(runtime)
        manager.bind_loop(asyncio.get_running_loop())
        manager.delegate({"plan": _plan(1, "dig the stairs")})
        await _until(lambda: calls == [1])
        assert manager.delegate({"plan": _plan(2, "build the room")})["reason"] == "plan_running"
        release[1].set()
        await manager.close()

    asyncio.run(scenario())


def test_cancelling_a_plan_leaves_the_next_item_on_the_ladder(tmp_path):
    async def scenario():
        release = {1: asyncio.Event(), 2: asyncio.Event()}
        runtime, facts, calls = _ladder_runtime(tmp_path, Decision(), release)

        async def cancel(reason, cancel_reflex=False):
            release[1].set()
            return {"settled": True, "native_stopped": True}

        runtime.cancel_active_execution_with_facts = cancel
        manager = ReflexManager(runtime)
        manager.bind_loop(asyncio.get_running_loop())
        first = manager.delegate({"plan": _plan(1, "dig the stairs")})["metrics"]["activity_id"]
        await _until(lambda: calls == [1])
        manager.delegate({"plan": _plan(2, "build the room")})
        manager.control({"operation": "cancel", "activity_id": first})
        await asyncio.sleep(0.1)
        assert calls == [1]
        assert [item["id"] for item in runtime.life_store.profile()["projects"]] == ["work-1", "work-2"]
        assert runtime.life_store.project_state({"id": "work-2"})["state"] == "waiting"
        await manager.close()

    asyncio.run(scenario())


def test_ended_activity_reports_only_plans_that_are_no_longer_going():
    async def scenario():
        release = {1: asyncio.Event()}
        runtime, facts, calls = _queue_runtime(ScriptedDecision("call_agent"), release)
        manager = ReflexManager(runtime)
        manager.bind_loop(asyncio.get_running_loop())
        activity_id = manager.delegate({"plan": _plan(1, "dig the stairs")})["metrics"]["activity_id"]
        await _until(lambda: calls == [1])
        assert manager.ended_activity(activity_id) is None
        release[1].set()
        assert await _until(lambda: len(facts) == 1)
        assert manager.ended_activity(activity_id)["status"] == "needs_agent"
        assert manager.ended_activity("activity-unknown") is None
        await manager.close()

    asyncio.run(scenario())


def test_a_reported_step_reaches_the_agent_while_the_plan_keeps_the_body():
    async def scenario():
        release = {1: asyncio.Event(), 2: asyncio.Event()}
        runtime, facts, calls = _queue_runtime(ScriptedDecision("report_and_go_on"), release)

        async def run_tool(_tool, _callback, _tool_arg, arguments, *_args, **_kwargs):
            calls.append(arguments["x"])
            await release[arguments["x"]].wait()
            if arguments["x"] == 1:
                return {"success": False, "reason": "place_denied:target_occupied", "canRetry": False, "metrics": {}}
            return {"success": True, "reason": "done", "metrics": {}}

        runtime.run_tool = run_tool
        manager = ReflexManager(runtime)
        manager.bind_loop(asyncio.get_running_loop())
        plan = {"language": "minebot-work/0.2", "goal": "two steps", "timeout_s": 4,
                "work": {"kind": "sequence", "intent": "both done", "steps": [
                    {"kind": "call", "id": "n1", "tool": "fake", "arguments": {"x": 1}, "intent": "first"},
                    {"kind": "call", "id": "n2", "tool": "fake", "arguments": {"x": 2}, "intent": "second"}]}}
        manager.delegate({"plan": plan})
        release[1].set()
        assert await _until(lambda: calls == [1, 2])
        # Told while the second step is still running.
        assert [fact["status"] for fact in facts] == ["step_reported"]
        assert facts[0]["node"] == "n1" and facts[0]["plan_status"] == "running"
        assert manager.body_busy()
        release[2].set()
        assert await _until(lambda: len(facts) == 2)
        # It went on past the reported step: it completed, and only tells.
        assert facts[1]["status"] == "completed" and facts[1]["wake"] is False
        assert facts[0]["wake"] is False
        assert facts[0].get("item") == facts[1].get("item")  # the step notice names the same item
        await manager.close()

    asyncio.run(scenario())


# -- plan ladder S2 (plan-ladder-20261028.md §7.2) ----------------------------


def _ladder_runtime(tmp_path, decision, release):
    from minebot.app.life_profile import LifeStore
    from minebot.app.runtime_state import RuntimeScope

    runtime, facts, calls = _queue_runtime(decision, release)
    runtime.life_store = LifeStore(tmp_path, RuntimeScope("s", "w", "LadderBot"))
    return runtime, facts, calls


def _ladder(runtime):
    return [item["id"] for item in runtime.life_store.profile().get("projects") or []]


def test_delegate_work_writes_the_top_ladder_item_and_logs_its_run(tmp_path):
    async def scenario():
        release = {1: asyncio.Event(), 2: asyncio.Event()}
        runtime, facts, calls = _ladder_runtime(tmp_path, Decision(), release)
        runtime.life_store.update_project("upsert", {"id": "stronghold", "intent": "Find the stronghold."},
                                          expected_revision=0, reason="think")
        manager = ReflexManager(runtime)
        manager.bind_loop(asyncio.get_running_loop())
        first = manager.delegate({"plan": _plan(1, "dig the stairs")})
        assert first["metrics"]["item_id"] == "work-1" and first["metrics"]["life_revision"] == 2
        await _until(lambda: calls == [1])
        assert runtime.life_store.project_state({"id": "work-1"})["state"] == "running"
        # Delegated while its own item runs: right after it on the ladder.
        queued = manager.delegate({"plan": _plan(2, "build the room"), "id": "room"})
        assert queued["reason"] == "on_ladder_after_running_plan" and queued["metrics"]["item_id"] == "room"
        assert _ladder(runtime) == ["work-1", "room", "stronghold"]
        item = runtime.life_store.profile()["projects"][1]
        assert item["intent"] == "build the room" and item["timeout_s"] == 4 and item["work"]["tool"] == "fake"
        release[1].set()
        assert await _until(lambda: runtime.life_store.project_state({"id": "work-1"})["state"] == "done")
        assert runtime.life_store.project_state({"id": "room"})["state"] == "waiting"
        rows = [json.loads(line) for line in runtime.life_store.log_path.read_text().splitlines()]
        assert [(row["project"], row.get("event") or row["outcome"], row.get("who"))
                for row in rows if row["project"] != "stronghold"] == [
            ("work-1", "agent_changed", None), ("work-1", "started", "agent"),
            ("room", "agent_changed", None), ("work-1", "completed", None)]
        await manager.close()

    asyncio.run(scenario())


def test_take_control_holds_the_ladder_until_resume(tmp_path):
    async def scenario():
        release = {1: asyncio.Event()}
        runtime, facts, calls = _ladder_runtime(tmp_path, Decision(), release)

        async def stop(reason, cancel_reflex=False):
            release[1].set()
            return {"settled": True, "native_stopped": True}

        runtime.cancel_active_execution_with_facts = stop
        manager = ReflexManager(runtime)
        manager.bind_loop(asyncio.get_running_loop())
        assert manager.control({"operation": "resume"})["reason"] == "no_reflex"
        manager.delegate({"plan": _plan(1, "dig the stairs")})
        await _until(lambda: calls == [1])
        manager.control({"operation": "take_control"})
        assert manager.ladder_held()
        assert await _until(lambda: runtime.life_store.project_state({"id": "work-1"})["state"] == "set_aside")
        resumed = manager.control({"operation": "resume"})
        assert resumed["reason"] == "ladder_resumed" and not manager.ladder_held()
        assert manager.control({"operation": "resume"})["reason"] == "nothing_to_resume"
        # delegate_work releases the hold too.
        manager.control({"operation": "take_control"})
        release[2] = asyncio.Event()
        manager.delegate({"plan": _plan(2, "build the room")})
        assert not manager.ladder_held()
        release[2].set()
        await manager.close()

    asyncio.run(scenario())


def test_cancel_marks_the_running_item_paused(tmp_path):
    async def scenario():
        release = {1: asyncio.Event()}
        runtime, facts, calls = _ladder_runtime(tmp_path, Decision(), release)

        async def stop(reason, cancel_reflex=False):
            release[1].set()
            return {"settled": True, "native_stopped": True}

        runtime.cancel_active_execution_with_facts = stop
        manager = ReflexManager(runtime)
        manager.bind_loop(asyncio.get_running_loop())
        first = manager.delegate({"plan": _plan(1, "dig the stairs")})["metrics"]["activity_id"]
        await _until(lambda: calls == [1])
        manager.control({"operation": "cancel", "activity_id": first})
        assert runtime.life_store.profile()["projects"][0]["status"] == "paused"
        assert await _until(lambda: runtime.life_store.project_results("work-1") != [])
        assert runtime.life_store.project_results("work-1")[-1]["outcome"] == "cancelled"
        await manager.close()

    asyncio.run(scenario())


def test_a_full_ladder_makes_room_as_finished_items_leave(tmp_path):
    from minebot.app.life_profile import MAX_PROJECTS, LifeStore
    from minebot.app.runtime_state import RuntimeScope

    store = LifeStore(tmp_path, RuntimeScope("s", "w", "LadderBot"))
    for index in range(MAX_PROJECTS):
        store.put_delegated(_plan(index, f"plan {index}"))
    assert len(store.profile()["projects"]) == MAX_PROJECTS
    import pytest
    from minebot.app.life_profile import LifeProfileError
    with pytest.raises(LifeProfileError, match="full"):
        store.put_delegated(_plan(99, "one more"))
    store.record_project_result("work-3", {"outcome": "completed"})
    store.record_project_result("work-5", {"outcome": "completed"})
    written = store.put_delegated(_plan(99, "one more"))
    ids = [item["id"] for item in store.profile()["projects"]]
    assert written["item_id"] == "work-33" and ids[0] == "work-33"
    # Every finished item left with that write (s7-later-20261029.md §8 D5).
    assert "work-3" not in ids and "work-5" not in ids and len(ids) == MAX_PROJECTS - 1
    assert {row["id"] for row in store.finished_items()} == {"work-3", "work-5"}
    assert {row["id"] for row in written["left_ladder"]} == {"work-3", "work-5"}


def _ticking(monkeypatch):
    """Each profile and log row one second after the one before."""
    from datetime import datetime, timedelta, timezone
    from minebot.app import life_profile
    start, ticks = datetime(2026, 11, 9, 3, 0, 0, tzinfo=timezone.utc), iter(range(10**6))
    monkeypatch.setattr(life_profile, "_now", lambda: (start + timedelta(seconds=next(ticks))
                                                        ).strftime("%Y-%m-%dT%H:%M:%SZ"))


def test_a_full_ladder_makes_room_with_the_items_set_aside_longest_ago(tmp_path, monkeypatch):
    """run-9 2026-11-09: 20 of 32 items set aside 8-12 h and untouched filled the ladder."""
    from minebot.app.life_profile import MAX_PROJECTS, LifeStore
    from minebot.app.runtime_state import RuntimeScope

    _ticking(monkeypatch)
    store = LifeStore(tmp_path, RuntimeScope("s", "w", "LadderBot"))
    for index in range(MAX_PROJECTS):
        store.put_delegated(_plan(index, f"plan {index}"))
    for item in ("work-7", "work-2", "work-9"):          # set aside in this order
        store.record_project_result(item, {"outcome": "needs_agent"})
    written = store.put_delegated(_plan(99, "one more"))
    ids = [item["id"] for item in store.profile()["projects"]]
    assert len(ids) == MAX_PROJECTS and written["item_id"] in ids
    assert "work-7" not in ids and "work-2" in ids and "work-9" in ids
    assert written["left_ladder"] == [{"id": "work-7", "intent": "plan 6", "why": "set_aside_room"}]
    # Writing its id again brings it back with its history; the next one set aside makes room.
    record = store.update_project("upsert", {"id": "work-7", "when": "it is daytime"},
                                  expected_revision=store.read()["revision"], reason="r")
    ids = [item["id"] for item in record["profile"]["projects"]]
    assert "work-7" in ids and "work-2" not in ids
    assert record["left_ladder"] == [{"id": "work-2", "intent": "plan 1", "why": "set_aside_room"}]


def test_an_item_done_past_a_reported_step_leaves_once_the_agent_has_written_after(tmp_path, monkeypatch):
    from minebot.app.life_profile import LifeStore
    from minebot.app.runtime_state import RuntimeScope

    _ticking(monkeypatch)
    store = LifeStore(tmp_path, RuntimeScope("s", "w", "LadderBot"))
    store.put_delegated(_plan(1, "plan 1"))
    store.record_project_result("work-1", {"outcome": "completed", "reported_steps": [{"node": "a"}]})
    # The first write after that end: the Agent may not have seen it yet.
    written = store.put_delegated(_plan(2, "plan 2"))
    assert "work-1" in [item["id"] for item in store.profile()["projects"]] and "left_ladder" not in written
    # A later write: it has.
    written = store.put_delegated(_plan(3, "plan 3"))
    assert "work-1" not in [item["id"] for item in store.profile()["projects"]]
    assert written["left_ladder"] == [{"id": "work-1", "intent": "plan 1", "why": "completed"}]


def test_a_result_frame_does_not_offer_set_aside(tmp_path):
    # Bench 2026-10-28: set_aside in result frames was picked on 2/30 steps
    # that ended normally (bar 5%); it stays in running frames only (§7.4).
    async def scenario():
        release = {1: asyncio.Event()}
        release[1].set()
        decision = ScriptedDecision("call_agent")
        runtime, facts, calls = _ladder_runtime(tmp_path, decision, release)

        async def run_tool(_tool, _callback, _tool_arg, arguments, *_args, **_kwargs):
            calls.append(arguments["x"])
            return {"success": False, "reason": "target_not_found", "canRetry": True, "metrics": {}}

        runtime.run_tool = run_tool
        manager = ReflexManager(runtime)
        manager.bind_loop(asyncio.get_running_loop())
        manager.delegate({"plan": _plan(1, "hunt a cow"), "id": "hunt"})
        assert await _until(lambda: len(facts) == 1)
        assert facts[0]["status"] == "needs_agent"
        criteria = decision.payloads[0]["questions"]["q"]["criteria"]
        assert "set_aside" not in criteria and list(criteria)[-1] == "call_agent"
        assert facts[0]["wake"] is True
        await manager.close()

    asyncio.run(scenario())


def test_only_decision_handing_the_plan_back_wakes_the_agent(tmp_path):
    async def scenario():
        release = {1: asyncio.Event(), 2: asyncio.Event()}
        release[1].set()
        release[2].set()
        runtime, facts, calls = _ladder_runtime(tmp_path, ScriptedDecision("call_agent", "step_done"), release)
        manager = ReflexManager(runtime)
        manager.bind_loop(asyncio.get_running_loop())
        manager.delegate({"plan": _plan(1, "dig the stairs")})
        assert await _until(lambda: len(facts) == 1)
        manager.delegate({"plan": _plan(2, "build the room")})
        assert await _until(lambda: len(facts) == 2)
        assert [(fact["status"], fact["wake"], fact["item"]) for fact in facts] == [
            ("needs_agent", True, "work-1"), ("completed", False, "work-2")]
        assert facts[1]["ended"]["outcome"] == "completed"
        # A clean one-shot completion is off the ladder; the handed-back item is not.
        assert facts[1]["off_ladder"] == "completed" and "off_ladder" not in facts[0]
        timeline = manager.body_timeline()
        assert timeline["busy_since"] is None and timeline["idle_since"] is not None
        await manager.close()

    asyncio.run(scenario())


def test_the_body_timeline_never_has_the_body_busy_when_no_run_works_it():
    # A run's status ends before its close reads the Body once more. Read in
    # that moment, the timeline still had the Body busy, and a resting Agent's
    # heartbeat rang as the item ended (T7 2026-10-29, s7-later §8.13).
    import time

    manager = ReflexManager(SimpleNamespace(trace=Trace()))
    run = SimpleNamespace(status="running")
    manager._activities["work-1"] = run
    manager._busy_since, manager._busy_runs = time.time() - 50, 2
    assert manager.body_timeline()["idle_since"] is None  # the run works the Body
    run.status = "completed"  # its close has not run yet
    timeline = manager.body_timeline()
    assert timeline["busy_since"] is None and time.time() - timeline["idle_since"] < 1
    # A run cancelled before it began leaves no busy Body behind either.
    del manager._activities["work-1"]
    manager._busy_since = time.time() - 50
    assert manager.body_timeline()["busy_since"] is None


def test_an_item_end_says_where_the_body_is_what_changed_and_where_it_stopped(tmp_path):
    # S7: of 46 runs of seven items only the cancelled ones logged a reason,
    # and no end said what the run changed (s7-later-20261029.md §8 D4).
    async def scenario():
        release = {1: asyncio.Event()}
        release[1].set()
        runtime, facts, calls = _ladder_runtime(tmp_path, ScriptedDecision("call_agent"), release)
        runtime.last_known_body_state = {"pos": [0.0, 64.0, 0.0], "dimension": "minecraft:overworld",
                                         "inventory_counts": {"stick": 2}}

        async def run_tool(_tool, _callback, _tool_arg, arguments, *_args, **_kwargs):
            calls.append(arguments["x"])
            runtime.last_known_body_state = {"pos": [5.04, 64.0, 0.0], "dimension": "minecraft:overworld",
                                             "inventory_counts": {"stick": 1, "arrow": 4}}
            return {"success": False, "reason": "target_not_found", "canRetry": True, "metrics": {}}

        runtime.run_tool = run_tool
        manager = ReflexManager(runtime)
        manager.bind_loop(asyncio.get_running_loop())
        manager.delegate({"plan": _plan(1, "hunt a chicken"), "id": "hunt"})
        assert await _until(lambda: len(facts) == 1)
        ended = facts[0]["ended"]
        assert ended["outcome"] == "needs_agent" and ended["dimension"] == "minecraft:overworld"
        assert ended["start_position"] == [0.0, 64.0, 0.0] and ended["position"] == [5.0, 64.0, 0.0]
        assert ended["inventory_counts"] == {"stick": 1, "arrow": 4}
        assert ended["inventory_change"] == {"arrow": 4, "stick": -1}
        assert ended["stopped_at"] == {"node": "n1", "tool": "fake", "result": "target_not_found",
                                       "why": "decision_call_agent"}
        assert runtime.life_store.project_results("hunt")[-1]["stopped_at"] == ended["stopped_at"]
        await manager.close()

    asyncio.run(scenario())


def test_an_item_that_went_on_past_a_reported_step_stays_on_the_ladder(tmp_path):
    async def scenario():
        release = {1: asyncio.Event(), 2: asyncio.Event()}
        release[1].set()
        release[2].set()
        runtime, facts, calls = _ladder_runtime(tmp_path, ScriptedDecision("report_and_go_on"), release)

        async def run_tool(_tool, _callback, _tool_arg, arguments, *_args, **_kwargs):
            calls.append(arguments["x"])
            if arguments["x"] == 1:
                return {"success": False, "reason": "place_denied:target_occupied", "canRetry": False, "metrics": {}}
            return {"success": True, "reason": "done", "metrics": {}}

        runtime.run_tool = run_tool
        manager = ReflexManager(runtime)
        manager.bind_loop(asyncio.get_running_loop())
        plan = {"language": "minebot-work/0.2", "goal": "two steps", "timeout_s": 4,
                "work": {"kind": "sequence", "intent": "both done", "steps": [
                    {"kind": "call", "id": "n1", "tool": "fake", "arguments": {"x": 1}, "intent": "first"},
                    {"kind": "call", "id": "n2", "tool": "fake", "arguments": {"x": 2}, "intent": "second"}]}}
        manager.delegate({"plan": plan, "id": "room"})
        assert await _until(lambda: any(fact["status"] == "completed" for fact in facts))
        done = next(fact for fact in facts if fact["status"] == "completed")
        assert done["ended"]["reported_steps"] == [
            {"node": "n1", "tool": "fake", "result": "place_denied:target_occupied"}]
        assert "off_ladder" not in done
        state = runtime.life_store.project_state(runtime.life_store.profile()["projects"][0])
        assert state["state"] == "done" and "off_ladder" not in state
        await manager.close()

    asyncio.run(scenario())


def test_a_running_frame_of_an_agent_ladder_item_offers_set_aside_before_call_agent():
    from minebot.app.direct_call_supervision import CallSupervisor

    supervisor = CallSupervisor.__new__(CallSupervisor)
    supervisor.runtime = SimpleNamespace()
    tool = SimpleNamespace(name="collect_resource")
    ask = lambda context: supervisor._question(
        "call-1", tool, {"item": "oak_log", "count": 8}, {"phase": "running", "elapsed_s": 12.0, "facts": {}},
        idle_s=0.0, purpose="running_frame",
        supervision_context={"goal": "gather wood", "intent": "at least 8 oak_log in the inventory", **context})
    criteria = ask({"is_ladder_item": True})["questions"]["q"]["criteria"]
    assert list(criteria)[-2:] == ["set_aside", "call_agent"]
    assert criteria["set_aside"] == "Stop the current action and set this work aside for now; tell the Agent."
    assert "set_aside" not in ask({})["questions"]["q"]["criteria"]


def test_an_agent_call_pauses_the_running_plan_instead_of_body_busy(tmp_path):
    """body-preemption-20261106.md §3: the Agent's call takes the Body from its own plan."""
    async def scenario():
        release = {1: asyncio.Event()}
        runtime, _facts, calls = _ladder_runtime(tmp_path, Decision(), release)
        manager = ReflexManager(runtime)
        manager.bind_loop(asyncio.get_running_loop())
        manager.delegate({"plan": _plan(1, "dig the stairs")})
        await _until(lambda: calls == [1])
        taken = await manager.yield_to_agent_call("agent_call_arrived")
        assert [item["kind"] for item in taken] == ["plan"]
        assert taken[0]["goal"] == "dig the stairs" and "resume" in taken[0]["now"]
        activity = next(iter(manager._activities.values()))
        assert activity.status == "paused" and activity.cancel_reason == "agent_call_arrived"
        assert activity.task is None or activity.task.done()
        assert await manager.yield_to_agent_call("agent_call_arrived") == []  # nothing left to take
        await manager.close()

    asyncio.run(scenario())


def test_protection_chains_yield_to_an_agent_call():
    """Two chains alive refused each other's steps as body_busy (run-24)."""
    from minebot.app.body_loop import BodyLoop

    async def scenario():
        events = []
        never = asyncio.Event()
        idle_chain = asyncio.create_task(never.wait())
        result_chain = asyncio.create_task(never.wait())
        loop = SimpleNamespace(
            _protection_task=idle_chain, _result_protections={result_chain},
            _protection_doing={"choice": "ranged", "tool": "ranged_attack"},
            runtime=SimpleNamespace(trace=SimpleNamespace(emit=lambda event, **data: events.append(event))),
        )
        taken = await BodyLoop.yield_protection_to_agent(loop)
        assert taken == [{"kind": "protection", "choice": "ranged", "tool": "ranged_attack",
                          "now": "stopped; your call runs instead"}]
        assert idle_chain.cancelled() and result_chain.cancelled() and loop._protection_task is None
        assert events == ["reflex_protection_yielded"]
        assert await BodyLoop.yield_protection_to_agent(loop) == []

    asyncio.run(scenario())


def test_the_running_protective_action_is_found_by_tool_and_target():
    from minebot.app.body_loop import BodyLoop

    async def scenario():
        done = asyncio.get_running_loop().create_future()
        loop = SimpleNamespace(
            _protection_running={"choice": "engage", "tool": "engage_entity", "arguments": {"target": "d1"}, "done": done},
            _idle_facts=lambda: {"nearby_entities": [{"id": "d1", "type": "ender_dragon", "boss": True, "dist": 7.0}]},
        )
        same = lambda tool, args: BodyLoop.same_running_action(loop, tool, args)
        assert same("engage_entity", {"target": "d1"})[1]["arguments"] == {"target": "d1"}
        assert same("engage_entity", {"target": "ender_dragon"}) is not None   # the type the Agent named
        assert same("engage_entity", {"target": "minecraft:ender_dragon"}) is not None
        assert same("engage_entity", {"target": "enderman"}) is None
        assert same("ranged_attack", {"target": "d1"}) is None
        done.set_result({})
        assert same("engage_entity", {"target": "d1"}) is None                  # it has ended

    asyncio.run(scenario())
