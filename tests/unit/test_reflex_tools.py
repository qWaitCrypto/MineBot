import asyncio
import copy
from types import SimpleNamespace

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

        first = await WorkPlanExecutor(plan, call=call, records=records).run()
        assert first.status == "completed"
        changed = {**plan, "work": {**plan["work"], "arguments": {"pos": [4, 2, 3]}}}
        second = await WorkPlanExecutor(changed, call=call, records=records).run()
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


def test_plan_step_context_says_whether_the_intent_is_its_own():
    from types import SimpleNamespace
    from minebot.app.reflex_tools import ReflexActivity, ReflexManager

    manager = ReflexManager(SimpleNamespace())
    plan = {"goal": "stone pickaxe", "work": {"kind": "sequence", "intent": "make a stone pickaxe", "steps": [
        {"kind": "call", "id": "stone", "tool": "collect_resource", "arguments": {"item": "cobblestone", "count": 3}},
        {"kind": "call", "id": "sticks", "tool": "craft_item", "intent": "have 2 sticks", "arguments": {"item": "stick"}},
        {"kind": "call", "id": "pick", "tool": "craft_item",
         "arguments": {"item": "stone_pickaxe", "intent": "hold a stone pickaxe"}},
    ]}}
    manager._activities["a"] = ReflexActivity("a", plan)
    manager._active_activity_id = "a"
    stone = manager._plan_supervision_context("stone", "collect_resource")
    assert stone["intent"] == "make a stone pickaxe" and stone["intent_of_plan"] is True
    sticks = manager._plan_supervision_context("sticks", "craft_item")
    assert sticks["intent"] == "have 2 sticks" and sticks["intent_of_plan"] is False
    pick = manager._plan_supervision_context("pick__repeat_1", "craft_item")
    assert pick["intent"] == "hold a stone pickaxe" and pick["intent_of_plan"] is False


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
    assert not authority.should_yield()


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
