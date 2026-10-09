import asyncio
from types import SimpleNamespace

from minebot.app.body_control import BodyControl
from minebot.app.reflex_tools import ReflexManager
from minebot.app.runner import AgentRuntime
from minebot.brain.registry import RegisteredTool, ToolRegistry, ToolSidecar, WeldContext, execute_tool
from minebot.brain.progress import ProgressAuthority


class Decision:
    async def ask(self, payload, *, priority=None):
        return SimpleNamespace(choice="satisfied", raw={"choice": "satisfied"})


class Trace:
    def __init__(self):
        self.events = []
    def emit(self, event, **payload):
        self.events.append((event, payload))


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
                "work": {"kind": "call", "id": "one", "tool": "fake", "arguments": {"x": 1}}}
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
            "work": {"kind": "call", "id": "one", "tool": "fake", "arguments": {}},
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
    assert payload["metrics"]["tree"] == {}


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
