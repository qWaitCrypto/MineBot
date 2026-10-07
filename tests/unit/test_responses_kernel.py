import asyncio
import json
from types import SimpleNamespace
import unittest

from minebot.app.responses_kernel import ResponsesKernel, ResponsesProtocolError


class Wire:
    def __init__(self, on_send):
        self.sent = []
        self.incoming = asyncio.Queue()
        self.on_send = on_send

    async def send_raw(self, raw):
        event = json.loads(raw)
        self.sent.append(event)
        await self.on_send(event, self)

    async def recv_bytes(self):
        return json.dumps(await self.incoming.get()).encode()

    def feed(self, *events):
        for event in events:
            self.incoming.put_nowait(event)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


def created(rid):
    return {"type": "response.created", "response": {"id": rid}}


def terminal(rid, *, reason=None):
    return {"type": "response.incomplete" if reason else "response.completed",
            "response": {"id": rid, "output": [], "incomplete_details": {"reason": reason}}}


def call(call_id, name="work"):
    return {"type": "response.output_item.done", "item": {
        "type": "function_call", "call_id": call_id, "name": name, "arguments": "{}", "async": name == "work"}}


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.001)


class ResponsesKernelTests(unittest.IsolatedAsyncioTestCase):
    def kernel(self, wire, invoke, instructions=None):
        async def default_instructions():
            return "Current goal"
        client = SimpleNamespace(responses=SimpleNamespace(connect=lambda **kw: wire))
        return ResponsesKernel(client=client, model="test", emit=lambda *a, **kw: None,
            speak=lambda text: None, invoke=invoke, instructions=instructions or default_instructions,
            tools=lambda: [{"type": "function", "name": "work", "async": True},
                           {"type": "function", "name": "read"}], request_timeout_s=1)

    async def test_late_result_follows_chat_on_original_call_and_fresh_context(self):
        release, started = asyncio.Event(), asyncio.Event()
        invokes, context = [], {"value": "before"}
        async def invoke(name, arguments, call_id):
            invokes.append(call_id)
            started.set()
            await release.wait()
            context["value"] = "inventory=3"
            return {"success": True, "count": 3}
        async def instructions():
            return context["value"]
        async def on_send(event, wire):
            if event["type"] == "response.create":
                n = len([e for e in wire.sent if e["type"] == "response.create"])
                wire.feed(created(f"r{n}"))
                if n == 1:
                    wire.feed(call("original"), call("original"))
                wire.feed(terminal(f"r{n}"))
        wire = Wire(on_send)
        kernel = self.kernel(wire, invoke, instructions)
        run = asyncio.create_task(kernel.run("Collect logs"))
        await started.wait()
        await until(lambda: not kernel._active)
        kernel.steer("How are you?")
        await until(lambda: len(wire.sent) == 2)
        self.assertFalse(kernel.calls["original"].task.done())
        self.assertEqual(wire.sent[1]["input"], [{"role": "user", "content": "How are you?"}])
        release.set()
        await asyncio.wait_for(run, 2)
        last = wire.sent[-1]
        self.assertEqual(last["previous_response_id"], "r2")
        self.assertEqual(last["input"][0]["call_id"], "original")
        self.assertIn("inventory=3", last["instructions"])
        self.assertNotIn("before", last["instructions"])
        self.assertEqual(invokes, ["original"])
        self.assertTrue(all("tools" in e for e in wire.sent))
        await kernel.close()

    async def test_native_steering_uses_automatic_successor_without_duplicate_input(self):
        release, started = asyncio.Event(), asyncio.Event()
        async def invoke(*args):
            started.set()
            await release.wait()
            return {"success": True}
        async def on_send(event, wire):
            if event["type"] == "response.create":
                if not event.get("previous_response_id"):
                    wire.feed(created("r1"), call("job"))
                else:
                    wire.feed(created("r3"), terminal("r3"))
            else:
                self.assertEqual(event["previous_response_id"], "r1")
                wire.feed({"type": "response.steer.accepted"}, terminal("r1", reason="steered"),
                          created("r2"), terminal("r2"))
        wire = Wire(on_send)
        kernel = self.kernel(wire, invoke)
        run = asyncio.create_task(kernel.run("Start"))
        await started.wait()
        kernel.steer("Then return home")
        await until(lambda: kernel.previous_response_id == "r2" and not kernel._active)
        self.assertFalse(kernel.calls["job"].task.done())
        self.assertEqual(len(wire.sent), 2)
        release.set()
        await asyncio.wait_for(run, 2)
        self.assertEqual(wire.sent[-1]["previous_response_id"], "r2")
        self.assertEqual(wire.sent[-1]["input"][0]["call_id"], "job")
        self.assertEqual(sum("Then return home" in json.dumps(e) for e in wire.sent), 1)
        await kernel.close()

    async def test_read_does_not_wait_for_writer_and_competing_write_is_rejected(self):
        release, read = asyncio.Event(), asyncio.Event()
        invokes = []
        async def invoke(name, *args):
            invokes.append(name)
            if name == "work":
                await release.wait()
            else:
                read.set()
            return {"success": True}
        async def on_send(event, wire):
            n = len(wire.sent)
            wire.feed(created(f"r{n}"))
            if n == 1:
                wire.feed(call("write1"), call("write2"), call("read1", "read"))
            wire.feed(terminal(f"r{n}"))
        wire = Wire(on_send)
        kernel = self.kernel(wire, invoke)
        run = asyncio.create_task(kernel.run("Start"))
        await read.wait()
        await until(lambda: kernel.calls["write2"].task.done())
        self.assertEqual(kernel.calls["write2"].task.result()["reason"], "owner_busy")
        self.assertEqual(invokes.count("work"), 1)
        self.assertFalse(kernel.calls["write1"].task.done())
        release.set()
        await asyncio.wait_for(run, 2)
        await kernel.close()

    async def test_model_cancellation_preserves_job_until_explicit_control(self):
        started, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def invoke(*args):
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return {"success": True}
        async def on_send(event, wire):
            wire.feed(created("r1"), call("job"), terminal("r1"))
        wire = Wire(on_send)
        kernel = self.kernel(wire, invoke)
        run = asyncio.create_task(kernel.run("Start"))
        await started.wait()
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)
        self.assertFalse(cancelled.is_set())
        self.assertFalse(kernel.calls["job"].task.done())
        kernel.halt()
        kernel._dispatch(call("late")["item"])
        await until(lambda: kernel.calls["late"].task.done())
        self.assertEqual(kernel.calls["late"].task.result()["reason"], "cancelled")
        # The application performs physical cancellation, then settles the job.
        kernel.calls["job"].task.cancel()
        await asyncio.gather(kernel.calls["job"].task)
        self.assertTrue(cancelled.is_set())
        await kernel.reset()
        self.assertEqual(kernel.calls, {})

    async def test_accepted_steering_waits_for_required_sync_result(self):
        release, started = asyncio.Event(), asyncio.Event()
        async def invoke(*args):
            started.set()
            await release.wait()
            return {"success": True}
        async def on_send(event, wire):
            if event["type"] == "response.steer":
                wire.feed({"type": "response.steer.accepted"}, {"type": "response.steer.pending"}, terminal("r1"))
            elif event.get("previous_response_id"):
                wire.feed(created("r2"), terminal("r2"))
            else:
                wire.feed(created("r1"), call("read1", "read"))
        wire = Wire(on_send)
        kernel = self.kernel(wire, invoke)
        run = asyncio.create_task(kernel.run("Read"))
        await started.wait()
        kernel.steer("Also tell me the weather")
        await until(lambda: not kernel._active and len(wire.sent) == 2)
        release.set()
        await asyncio.wait_for(run, 2)
        self.assertEqual(wire.sent[-1]["input"][0]["call_id"], "read1")
        self.assertEqual(len(wire.sent[-1]["input"]), 1)
        self.assertEqual(sum("weather" in json.dumps(e) for e in wire.sent), 1)
        await kernel.close()

    async def test_production_session_steers_chat_and_cancel_stops_execution_worker(self):
        import threading
        import time
        from minebot.app.session import AgentSession, SessionCommand
        from minebot.app.wiring import build_agent_runtime
        from minebot.brain.registry import RegisteredTool, ToolRegistry, ToolSidecar
        from minebot.contract import execution_checkpoint
        from tests.unit.test_agent_runner_spine import FakeBody

        body, started, stopped = FakeBody(), threading.Event(), threading.Event()
        def physical_work(params):
            started.set()
            try:
                while True:
                    execution_checkpoint()
                    time.sleep(0.005)
            finally:
                stopped.set()
        registry = ToolRegistry()
        registry.register(RegisteredTool(name="work", description="Long body action", callable=physical_work,
            input_schema={"type": "object", "properties": {}},
            sidecar=ToolSidecar(progress_key="work", mutating=False, body_mutating=True)))
        async def on_send(event, wire):
            if event["type"] == "response.create":
                wire.feed(created("r1"), call("job"))
            else:
                wire.feed({"type": "response.steer.accepted"}, terminal("r1", reason="steered"),
                          created("r2"), terminal("r2"))
        wire = Wire(on_send)
        client = SimpleNamespace(responses=SimpleNamespace(connect=lambda **kw: wire))
        provider = SimpleNamespace(kernel="responses", responses_connection_config=lambda: (client, "test", {}, 2))
        import tempfile
        from pathlib import Path
        from minebot.app.runtime_state import RuntimeScope, RuntimeStateStore
        from minebot.app.work_queue import PersistentWorkIntentQueue
        temporary = tempfile.TemporaryDirectory()
        store = RuntimeStateStore(Path(temporary.name) / "state.sqlite3")
        queue = PersistentWorkIntentQueue(store, RuntimeScope("server", "world", "Bot"))
        session = AgentSession(lambda goal: build_agent_runtime(
            body=body, registry=registry, goal_text=goal, model_provider=provider), work_queue=queue)
        session.submit(SessionCommand.start("Keep working"))
        work = asyncio.create_task(session.step())
        try:
            await until(started.is_set)
            def slow_read(params):
                while True:
                    execution_checkpoint()
                    time.sleep(0.005)
            registry.register(RegisteredTool(name="read", description="Read with a deadline", callable=slow_read,
                input_schema={"type": "object", "properties": {}},
                sidecar=ToolSidecar(progress_key="read", mutating=False, timeout_s=0.02)))
            result = await session.parts.runtime._invoke_responses_tool("read", "{}", "read-deadline")
            self.assertFalse(result["success"])
            self.assertFalse(stopped.is_set())
            self.assertEqual(body.interrupt_reasons, [])
            session.submit(SessionCommand.message("Then come back", sender="Player"))
            await until(lambda: any(e["type"] == "response.steer" for e in wire.sent))
            self.assertFalse(stopped.is_set())
            self.assertEqual(body.interrupt_reasons, [])
            self.assertFalse(work.done(), (str(work.result()), session.parts.runtime.trace.snapshot()[-8:]) if work.done() else "")
            session.submit(SessionCommand.cancel("Stop now"))
            result = await asyncio.wait_for(work, 3)
            self.assertEqual(result.status, "preempted")
            await asyncio.wait_for(session.step(), 10)
            self.assertTrue(stopped.is_set())
            self.assertTrue(body.interrupt_reasons)
            self.assertEqual(session.parts.runtime.responses_kernel.calls, {})
        finally:
            await session.aclose()
            store.close()
            temporary.cleanup()

    async def test_unfinished_goal_continues_after_text_and_finishes_from_tool_evidence(self):
        from minebot.app.session import AgentSession, SessionCommand
        from minebot.app.wiring import build_agent_runtime
        from minebot.brain.registry import RegisteredTool, ToolRegistry, ToolSidecar
        from minebot.contract import ToolResult
        from tests.unit.test_agent_runner_spine import FakeBody
        body, registry = FakeBody(), ToolRegistry()
        moves = []
        def move(params):
            moves.append(params)
            if len(moves) == 1:
                from minebot.app.execution_lane import ToolExecutionTimeout
                raise ToolExecutionTimeout(timeout_s=0.01, execution_elapsed_s=0.02, queue_wait_s=0)
            body.x = 3.0
            return ToolResult(True, "arrived", can_retry=False, metrics={"pos": [3, 64, 0]})
        registry.register(RegisteredTool("work", "Move", {"type": "object", "properties": {}},
            move, ToolSidecar("work", False, body_mutating=True)))
        task_ids = []
        async def on_send(event, wire):
            n = len(wire.sent)
            wire.feed(created(f"r{n}"))
            if n == 1:
                task_ids.append(workspace.current_task.task_id)
            if n == 2:
                plan = call("plan", "update_plan")
                plan["item"]["arguments"] = json.dumps({"summary": "Move and check", "steps": ["Move three blocks", "Check position"]})
                wire.feed(plan, call("move"))
            elif n == 3:
                wire.feed(call("retry-move"))
            elif n == 4:
                finish = call("finish", "finish_task")
                finish["item"]["arguments"] = json.dumps({"status": "complete", "summary": "Arrived", "evidence": ["position is 3,64,0"]})
                wire.feed(finish)
            wire.feed(terminal(f"r{n}"))
        wire = Wire(on_send)
        client = SimpleNamespace(responses=SimpleNamespace(connect=lambda **kw: wire))
        provider = SimpleNamespace(kernel="responses", responses_connection_config=lambda: (client, "test", {}, 2))
        import tempfile
        from pathlib import Path
        from minebot.app.runtime_state import RuntimeScope, RuntimeStateStore
        from minebot.app.tasks import TaskWorkspace
        temporary = tempfile.TemporaryDirectory()
        store = RuntimeStateStore(Path(temporary.name) / "state.sqlite3")
        workspace = TaskWorkspace(store, RuntimeScope("server", "world", "Bot"))
        def make_parts(goal):
            parts = build_agent_runtime(body=body, registry=registry, goal_text=goal, model_provider=provider)
            parts.runtime.native_task_workspace = workspace
            return parts
        session = AgentSession(make_parts, task_workspace=workspace)
        session.submit(SessionCommand.start("Move to 3,64,0"))
        try:
            result = await asyncio.wait_for(session.step(), 10)
            self.assertEqual(result.status, "completed", (result.message, session.parts.runtime.trace.snapshot()[-8:]) if session.parts else result.message)
            self.assertEqual(body.x, 3)
            self.assertEqual(len(moves), 2)
            self.assertTrue(body.interrupt_reasons)
            self.assertEqual(session.parts.runtime.native_task_outcome["status"], "complete")
            self.assertEqual(store.get_task(task_ids[0]).completion_authority.value, "model")
            self.assertEqual(store.get_plan(task_ids[0]).summary, "Move and check")
            outputs = {item["call_id"]: json.loads(item["output"])
                       for event in wire.sent for item in event.get("input", [])
                       if item.get("type") == "function_call_output"}
            self.assertFalse(outputs["move"]["success"])
            self.assertTrue(outputs["retry-move"]["success"], outputs)
            continuation = next(event for event in wire.sent if any(
                item.get("call_id") == "retry-move" for item in event.get("input", [])))
            self.assertIn("pos=(3.0, 64.0, 0.0)", continuation["instructions"])
        finally:
            await session.aclose()
            store.close()
            temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
