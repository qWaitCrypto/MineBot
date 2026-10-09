import asyncio
from types import SimpleNamespace

from minebot.app.body_control import BodyControl
from minebot.app.reflex_tools import ReflexManager


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
