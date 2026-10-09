import asyncio
import json
from types import SimpleNamespace
import unittest

import httpx
from openai import AsyncOpenAI

from minebot.app.responses_kernel import ResponsesKernel, ResponsesProtocolError, ResponsesTransportError


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
            "response": {"id": rid, "status": "incomplete" if reason else "completed",
                         "output": [], "incomplete_details": {"reason": reason}}}


def call(call_id, name="work"):
    return {"type": "response.output_item.done", "item": {
        "type": "function_call", "call_id": call_id, "name": name, "arguments": "{}", "async": name == "work"}}


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.001)


class ResponsesKernelTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_tail_failure_cannot_revoke_received_terminal(self):
        from minebot.app.responses_kernel import _HttpResponsesConnection
        class Stream:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def __aiter__(self):
                for event in (created("done"), terminal("done")):
                    yield SimpleNamespace(model_dump=lambda event=event, **kw: event)
                raise httpx.RemoteProtocolError("peer closed the chunked tail")
        async def create(**params): return Stream()
        conn = _HttpResponsesConnection(SimpleNamespace(responses=SimpleNamespace(create=create)))
        await conn._stream({})
        self.assertEqual([conn.events.get_nowait()["type"] for _ in range(conn.events.qsize())],
                         ["response.created", "response.completed"])

    async def test_http_finished_stream_reuses_connection(self):
        handlers = []
        async def serve(reader, writer):
            handlers.append(asyncio.current_task())
            try:
                for _ in range(2):
                    headers = await reader.readuntil(b"\r\n\r\n")
                    length = next(int(line.split(b":", 1)[1]) for line in headers.split(b"\r\n")
                                  if line.lower().startswith(b"content-length:"))
                    await reader.readexactly(length)
                    data = b"".join(("data: " + json.dumps(event) + "\n\n").encode()
                                    for event in (created("reuse"), terminal("reuse")))
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n")
                    writer.write(f"{len(data):x}\r\n".encode() + data + b"\r\n0\r\n\r\n")
                    await writer.drain()
            except (ConnectionError, asyncio.IncompleteReadError):
                pass
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        client = AsyncOpenAI(api_key="local-test", base_url=f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1",
                             max_retries=0, http_client=httpx.AsyncClient(trust_env=False))
        async def instructions(): return "Local reuse test"
        kernel = ResponsesKernel(client=client, model="test", tools=lambda: [], instructions=instructions,
                                 invoke=None, emit=lambda *a, **kw: None, speak=lambda text: None, transport="http")
        try:
            await asyncio.wait_for(kernel.run("first"), 3)
            await asyncio.wait_for(kernel.run("second"), 3)
            self.assertEqual(len(handlers), 1)
        finally:
            await kernel.close()
            await client.close()
            server.close()
            await server.wait_closed()
            for handler in handlers:
                handler.cancel()
            await asyncio.gather(*handlers, return_exceptions=True)

    async def test_http_keepalive_survives_event_gap_but_idle_and_eof_fail(self):
        from minebot.app.responses_observation import observe_http_request, observe_http_response
        # Real SDK and loopback SSE: MockTransport cannot enforce socket read
        # timeouts, and its event iterator cannot represent filtered comments.
        for mode in ("keepalive", "silent", "eof"):
            with self.subTest(mode=mode):
                handlers, heartbeats = [], []
                async def serve(reader, writer):
                    handlers.append(asyncio.current_task())
                    try:
                        headers = await reader.readuntil(b"\r\n\r\n")
                        length = next(int(line.split(b":", 1)[1]) for line in headers.split(b"\r\n")
                                      if line.lower().startswith(b"content-length:"))
                        await reader.readexactly(length)
                        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n")
                        async def send(event):
                            writer.write(("data: " + json.dumps(event) + "\n\n").encode())
                            await writer.drain()
                        await send(created("loopback"))
                        if mode == "eof":
                            return
                        for _ in range(10):
                            await asyncio.sleep(0.025)
                            if mode == "keepalive":
                                writer.write(b": keepalive\n\n")
                                await writer.drain()
                                heartbeats.append(True)
                        await send(terminal("loopback"))
                    except ConnectionError:
                        pass
                    finally:
                        writer.close()
                        await writer.wait_closed()
                server = await asyncio.start_server(serve, "127.0.0.1", 0)
                port = server.sockets[0].getsockname()[1]
                observations = []
                client = AsyncOpenAI(api_key="local-test", base_url=f"http://127.0.0.1:{port}/v1",
                                      max_retries=0, timeout=httpx.Timeout(0.1, connect=1),
                                      http_client=httpx.AsyncClient(trust_env=False,
                                          event_hooks={"request": [observe_http_request], "response": [observe_http_response]}))
                async def instructions(): return "Local stream test"
                kernel = ResponsesKernel(client=client, model="test", tools=lambda: [],
                    instructions=instructions, invoke=None, emit=lambda event, **kw: observations.append((event, kw)),
                    speak=lambda text: None, transport="http", request_timeout_s=0.1)
                try:
                    if mode == "keepalive":
                        result = await asyncio.wait_for(kernel.run("hello"), 3)
                        self.assertEqual(result.response_id, "loopback")
                        self.assertEqual(len(heartbeats), 10)
                        await kernel._connection.request
                        summary = next(f for e, f in observations if e == "responses_stream_summary")
                        self.assertEqual(summary["comment_count"], 10)
                        self.assertTrue(any(e == "responses_http_phase" for e, f in observations))
                        self.assertNotIn("local-test", json.dumps(observations))
                    else:
                        with self.assertRaisesRegex(ResponsesTransportError,
                                "ReadTimeout.*last_event=response.created" if mode == "silent" else "without a terminal"):
                            await asyncio.wait_for(kernel.run("hello"), 3)
                finally:
                    await kernel.close()
                    await client.close()
                    server.close()
                    await server.wait_closed()
                    for handler in handlers:
                        handler.cancel()
                    await asyncio.gather(*handlers, return_exceptions=True)

    async def test_namespaced_async_calls_and_compaction_keep_native_results(self):
        from minebot.app.responses_kernel import _HttpResponsesConnection

        invokes = []
        async def invoke(name, arguments, call_id):
            invokes.append((name, call_id))
            return {"success": True}
        async def on_send(event, wire):
            self.assertEqual(event["tools"][0]["type"], "namespace")
            self.assertEqual(event["tools"][0]["name"], "minebot")
            if len(wire.sent) == 1:
                item = {**call("native")["item"], "namespace": "minebot"}
                wire.feed(created("r1"), {"type": "response.output_item.done", "item": item},
                          {"type": "response.output_item.done", "item": {"type": "compaction", "encrypted_content": "opaque"}}, terminal("r1"))
            else:
                self.assertEqual(event["input"][0]["call_id"], "native")
                wire.feed(created("r2"), terminal("r2"))
        wire = Wire(on_send)
        kernel = self.kernel(wire, invoke)
        observed = []
        kernel.emit = lambda name, **kw: observed.append((name, kw))
        kernel.tool_namespace = "minebot"
        await kernel.run("work")
        self.assertEqual(invokes, [("work", "native")])
        self.assertEqual([kw["compactions"] for name, kw in observed if name == "responses_terminal"], [1, 0])
        await kernel.close()

        compact = {"type": "compaction", "encrypted_content": "opaque"}
        pending = call("pending")["item"]
        output = {"type": "function_call_output", "call_id": "pending", "output": '{"success":true}'}
        requests = []
        class Stream:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def __aiter__(self):
                event = terminal("compacted")
                event["response"]["output"] = [compact] if len(requests) == 1 else []
                yield SimpleNamespace(model_dump=lambda **kw: event)
        async def create(**params):
            requests.append(params)
            return Stream()
        connection = _HttpResponsesConnection(SimpleNamespace(responses=SimpleNamespace(create=create)))
        connection.history = [{"role": "user", "content": "goal"}, pending, compact]
        async with connection:
            for inputs in ([{"role": "user", "content": "status?"}], [output], [{"role": "user", "content": "next"}]):
                await connection.send_raw(json.dumps({"type": "response.create", "input": inputs}))
                await connection.recv_bytes()
        self.assertIn(pending, requests[1]["input"])
        self.assertNotIn(pending, requests[2]["input"])
        self.assertEqual(requests[2]["input"], [compact, output, {"role": "user", "content": "next"}])

    async def test_http_snapshot_updates_leave_history_prefix_intact(self):
        from minebot.app.responses_kernel import _HttpResponsesConnection
        requests = []
        class Stream:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def __aiter__(self):
                yield SimpleNamespace(model_dump=lambda **kw: terminal("r1"))
        async def create(**params):
            requests.append(params)
            return Stream()
        connection = _HttpResponsesConnection(SimpleNamespace(responses=SimpleNamespace(create=create)))
        async with connection:
            for text, snapshot in [("Build at 10,70,20", "inventory=3"), ("Continue", "inventory=4")]:
                await connection.send_raw(json.dumps({"type": "response.create", "model": "test",
                    "instructions": "Stable rules", "input": text, "_current_context": snapshot}))
                await connection.recv_bytes()
        first, second = requests
        self.assertEqual(second["input"][:len(first["input"])], first["input"])
        self.assertEqual(second["input"][-1]["content"], "CURRENT_CONTEXT:\ninventory=4")
        self.assertTrue(connection.context_usage["static_prefix_reused"])
        self.assertEqual(connection.context_usage["shared_prefix_items"], len(first["input"]))
        self.assertNotIn("_current_context", second)
        self.assertEqual(first["prompt_cache_key"], second["prompt_cache_key"])
        self.assertEqual(connection.context_usage["context_estimates"]["unit"], "tokens")

    async def test_api_summary_preserves_player_requirements_and_latest_state(self):
        from minebot.app.responses_kernel import _HttpResponsesConnection
        requests = []
        class Stream:
            def __init__(self, summarize): self.summarize = summarize
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def __aiter__(self):
                if self.summarize:
                    yield SimpleNamespace(model_dump=lambda **kw: {"type": "response.output_text.delta", "delta": "House unfinished; roof at 10,70,20."})
                yield SimpleNamespace(model_dump=lambda **kw: terminal("r1"))
        async def create(**params):
            requests.append(params)
            return Stream("tools" not in params)
        conn = _HttpResponsesConnection(SimpleNamespace(responses=SimpleNamespace(create=create)),
            context_mode="summary", compact_threshold=400)
        goal = {"role": "user", "content": "Build a two-storey house; never remove player blocks."}
        addition = {"role": "user", "content": "Add a peaked roof at 10,70,20."}
        conn.history = [goal, call("old")["item"], {"type": "function_call_output", "call_id": "old",
            "output": json.dumps({"success": True, "old_observations": "stone 123,70,89 " * 1000})}, addition]
        async with conn:
            await conn.send_raw(json.dumps({"type": "response.create", "model": "test", "instructions": "Rules",
                "input": "Continue", "_current_context": "inventory=4; next=roof"}))
            await conn.recv_bytes()
        self.assertEqual(len(requests), 2)
        self.assertTrue(requests[0]["stream"])
        self.assertNotIn("context_management", requests[0])
        final = requests[1]["input"]
        self.assertIn(goal, final)
        self.assertIn(addition, final)
        self.assertEqual(final[-1]["content"], "CURRENT_CONTEXT:\n" + "inventory=4; next=roof")
        self.assertFalse(any(i.get("type") == "function_call" for i in final))

    async def test_terminal_without_eof_does_not_hold_next_request_for_model_timeout(self):
        from minebot.app.responses_kernel import _HttpResponsesConnection
        requests = []
        class Stream:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def __aiter__(self):
                yield SimpleNamespace(model_dump=lambda **kw: terminal("r1"))
                if len(requests) == 1:
                    await asyncio.Event().wait()
        async def create(**params):
            requests.append(params)
            return Stream()
        facts = []
        conn = _HttpResponsesConnection(SimpleNamespace(responses=SimpleNamespace(create=create)),
                                       lambda e, **f: facts.append((e, f)))
        async with conn:
            for n in range(2):
                await asyncio.wait_for(conn.send_raw(json.dumps({"type": "response.create", "input": str(n)})), 3)
                await conn.recv_bytes()
        self.assertEqual(len(requests), 2)
        self.assertTrue(any(f.get("status") == "closed_after_terminal" for e, f in facts))

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

    async def test_http_keeps_work_alive_and_replays_native_history_for_late_result(self):
        release, started, finish_response = asyncio.Event(), asyncio.Event(), asyncio.Event()
        requests, spoken = [], []
        class Stream:
            def __init__(self, number):
                self.number = number
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            async def __aiter__(self):
                rid = f"http{self.number}"
                yield SimpleNamespace(model_dump=lambda **kw: created(rid))
                output = []
                if self.number == 1:
                    event = call("http-original")
                    reasoning = {"type": "reasoning", "encrypted_content": "opaque"}
                    yield SimpleNamespace(model_dump=lambda **kw: {"type": "response.output_item.done", "item": reasoning})
                    yield SimpleNamespace(model_dump=lambda **kw: event)
                    await finish_response.wait()
                else:
                    item = {"type": "message", "role": "assistant", "id": rid + "-text",
                            "content": [{"type": "output_text", "text": "ACK" if self.number == 2 else "COUNT=3"}]}
                    output = [item]
                event = terminal(rid)
                event["response"]["output"] = output
                yield SimpleNamespace(model_dump=lambda **kw: event)
        async def create(**params):
            requests.append(params)
            return Stream(len(requests))
        async def invoke(*args):
            started.set()
            await release.wait()
            return {"success": True, "count": 3}
        async def instructions():
            return "inventory=3" if release.is_set() else "inventory=0"
        kernel = ResponsesKernel(client=SimpleNamespace(responses=SimpleNamespace(create=create)),
            model="test", tools=lambda: [{"type": "function", "name": "work", "async": True}],
            instructions=instructions, invoke=invoke, emit=lambda *a, **kw: None,
            speak=spoken.append, transport="http", request_timeout_s=2)
        run = asyncio.create_task(kernel.run("Collect"))
        try:
            await started.wait()
            kernel.steer("How is it going?")
            await asyncio.sleep(0.01)
            self.assertEqual(len(requests), 1)  # HTTP input waits for the model, not the body.
            finish_response.set()
            await until(lambda: "ACK" in spoken)
            self.assertFalse(kernel.calls["http-original"].task.done())
            self.assertEqual(requests[1]["input"][-1]["content"], "How is it going?")
            release.set()
            await asyncio.wait_for(run, 2)
            self.assertEqual(spoken, ["ACK", "COUNT=3"])
            last = requests[-1]
            self.assertEqual(sum(i.get("type") == "function_call_output" for i in last["input"]), 1)
            self.assertEqual(last["input"][-1]["call_id"], "http-original")
            self.assertEqual(sum(i.get("type") == "function_call" for i in last["input"]), 1)
            self.assertTrue(any(i.get("encrypted_content") == "opaque" for i in last["input"]))
            self.assertIn("inventory=3", last["instructions"])
            self.assertNotIn("inventory=0", last["instructions"])
            self.assertTrue(all(not p["store"] and "previous_response_id" not in p for p in requests))
        finally:
            release.set()
            finish_response.set()
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)
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

    async def test_http_failed_stream_preserves_running_call_and_resumes_once(self):
        for failure in ("interrupted", "timeout", "eof", "api_error"):
            with self.subTest(failure=failure):
                await self._http_failed_stream_preserves_running_call_and_resumes_once(failure)

    async def _http_failed_stream_preserves_running_call_and_resumes_once(self, failure):
        started, release = asyncio.Event(), asyncio.Event()
        requests, invoked = [], []
        class Stream:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def __aiter__(self):
                yield SimpleNamespace(model_dump=lambda **kw: created(f"r{len(requests)}"))
                if len(requests) == 1:
                    yield SimpleNamespace(model_dump=lambda **kw: call("original"))
                    await started.wait()
                    if failure == "interrupted":
                        raise OSError("stream interrupted")
                    if failure == "timeout":
                        raise httpx.ReadTimeout("No network bytes within the SDK read timeout")
                    if failure == "api_error":
                        yield SimpleNamespace(model_dump=lambda **kw: {"type": "error", "code": "server_error", "message": "unavailable"})
                    return
                yield SimpleNamespace(model_dump=lambda **kw: terminal("r2"))
        async def create(**params):
            requests.append(params)
            return Stream()
        async def invoke(name, arguments, call_id):
            invoked.append(call_id)
            started.set()
            await release.wait()
            return {"success": True, "count": 24}
        async def instructions(): return "Fresh world state"
        kernel = ResponsesKernel(client=SimpleNamespace(responses=SimpleNamespace(create=create)),
            model="test", tools=lambda: [{"name": "work", "async": True}], instructions=instructions,
            invoke=invoke, emit=lambda *a, **kw: None, speak=lambda text: None, transport="http", request_timeout_s=0.1)
        try:
            with self.assertRaises(ResponsesTransportError) as failure:
                await kernel.run("Collect 24 logs")
            from minebot.app.model_provider import is_retryable_model_error
            self.assertTrue(is_retryable_model_error(failure.exception))
            self.assertFalse(kernel.calls["original"].task.done())
            release.set()
            await asyncio.wait_for(kernel.run("Continue from the actual result"), 2)
            history = requests[-1]["input"]
            self.assertEqual(invoked, ["original"])
            self.assertEqual(sum(i.get("call_id") == "original" and i.get("type") == "function_call" for i in history), 1)
            self.assertEqual(sum(i.get("call_id") == "original" and i.get("type") == "function_call_output" for i in history), 1)
        finally:
            release.set()
            await kernel.close()

    async def test_http_context_failure_keeps_unsubmitted_user_message(self):
        requests, failures = [], [True]
        class Stream:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def __aiter__(self):
                yield SimpleNamespace(model_dump=lambda **kw: created("r1"))
                yield SimpleNamespace(model_dump=lambda **kw: terminal("r1"))
        async def create(**params):
            requests.append(params)
            return Stream()
        async def instructions():
            if failures.pop() if failures else False:
                raise OSError("state read failed")
            return "Current state"
        kernel = ResponsesKernel(client=SimpleNamespace(responses=SimpleNamespace(create=create)),
            model="test", tools=lambda: [], instructions=instructions, invoke=None,
            emit=lambda *a, **kw: None, speak=lambda text: None, transport="http")
        try:
            with self.assertRaisesRegex(OSError, "state read failed"):
                await kernel.run("After handoff, return home")
            await kernel.run("")
            self.assertEqual(requests[0]["input"], [{"role": "user", "content": "After handoff, return home"}])
        finally:
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

    async def test_native_binding_rejects_bad_input_and_waits_for_result_delivery_before_finish(self):
        from minebot.app.runner import RuntimeRunContext, tool_invoker_for
        from minebot.app.wiring import build_agent_runtime
        from minebot.app.responses_kernel import PendingCall
        from minebot.brain.registry import RegisteredTool, ToolRegistry, ToolSidecar
        from tests.unit.test_agent_runner_spine import FakeBody
        calls, registry = [], ToolRegistry()
        tool = registry.register(RegisteredTool("work", "Collect", {
            "type": "object", "properties": {"count": {"type": "integer", "minimum": 1}},
            "required": ["count"], "additionalProperties": False},
            lambda params: calls.append(params), ToolSidecar("work", False, body_mutating=True)))
        wire = Wire(lambda *args: None)
        provider = SimpleNamespace(kernel="responses", responses_connection_config=lambda: (
            SimpleNamespace(responses=SimpleNamespace(connect=lambda **kw: wire)), "test", {}, 2))
        parts = build_agent_runtime(body=FakeBody(), registry=registry, goal_text="Collect", model_provider=provider)
        runtime = parts.runtime
        ctx = RuntimeRunContext(agent_context=parts.context, weld_context=runtime.weld_context,
            profile=parts.modes.profile_for(parts.lifecycle.state), trace=runtime.trace, runtime=runtime)
        runtime._native_context = ctx
        try:
            for arguments in ({}, {"count": -1}, {"count": "3"}, {"count": 3, "typo": True}):
                result = await tool_invoker_for(tool)(SimpleNamespace(context=ctx, tool_call_id="bad"), json.dumps(arguments))
                self.assertEqual(result["reason"], "invalid_tool_input")
            self.assertEqual(calls, [])
            task = asyncio.create_task(asyncio.sleep(0, result={"success": True, "count": 3}))
            await task
            call_result = PendingCall("original", "work", '{"count":3}', task, True, True)
            runtime.responses_kernel.calls["original"] = call_result
            finish = json.dumps({"status": "complete", "summary": "Collected", "evidence": ["count=3"]})
            result = await runtime._invoke_responses_tool("finish_task", finish, "finish")
            self.assertEqual(result["reason"], "body_work_pending")
            call_result.delivered = True
            self.assertTrue((await runtime._invoke_responses_tool("finish_task", finish, "finish2"))["success"])
        finally:
            await runtime.aclose()

    async def test_continue_reopens_blocked_native_task_without_resetting_plan_or_history(self):
        from minebot.app.session import AgentSession, SessionCommand
        from minebot.app.wiring import build_agent_runtime
        from minebot.app.runtime_state import RuntimeScope, RuntimeStateStore
        from minebot.app.tasks import TaskWorkspace
        from minebot.brain.registry import RegisteredTool, ToolRegistry, ToolSidecar
        from minebot.contract import ToolResult
        from tests.unit.test_agent_runner_spine import FakeBody
        from pathlib import Path
        import tempfile

        for guidance in ("", "Continue ground work first"):
            with self.subTest(guidance=guidance), tempfile.TemporaryDirectory() as directory:
                store = RuntimeStateStore(Path(directory) / "state.sqlite3")
                workspace = TaskWorkspace(store, RuntimeScope("server", "world", "Bot"))
                body, registry, moves = FakeBody(), ToolRegistry(), []
                def move(params):
                    moves.append(params)
                    body.x = 3.0
                    return ToolResult(True, "arrived", can_retry=False)
                registry.register(RegisteredTool("work", "Move", {"type": "object", "properties": {}},
                    move, ToolSidecar("work", False, body_mutating=True)))
                async def on_send(event, wire):
                    n = len(wire.sent)
                    wire.feed(created(f"r{n}"))
                    if n in (1, 4):
                        finish = call(f"finish{n}", "finish_task")
                        finish["item"]["arguments"] = json.dumps({
                            "status": "blocked" if n == 1 else "complete",
                            "summary": "Route obstructed" if n == 1 else "Arrived",
                            "evidence": ["route failed" if n == 1 else "position checked"]})
                        wire.feed(finish)
                    elif n == 3:
                        wire.feed(call("resumed-move"))
                    wire.feed(terminal(f"r{n}"))
                wire = Wire(on_send)
                provider = SimpleNamespace(kernel="responses", responses_connection_config=lambda: (
                    SimpleNamespace(responses=SimpleNamespace(connect=lambda **kw: wire)), "test", {}, 2))
                def make_parts(goal):
                    parts = build_agent_runtime(body=body, registry=registry, goal_text=goal, model_provider=provider)
                    parts.runtime.native_task_workspace = workspace
                    return parts
                session = AgentSession(make_parts, task_workspace=workspace)
                session.submit(SessionCommand.start("Move and verify"))
                try:
                    await asyncio.wait_for(session.step(), 10)
                    runtime = session.parts.runtime
                    task_id = workspace.current_task.task_id
                    workspace.update_plan(expected_revision=0, summary="Preserve this plan",
                        steps=[{"title": "Move"}, {"title": "Verify"}])
                    self.assertEqual(runtime.native_task_outcome["status"], "blocked")
                    # A read/chat does not remove the terminal action restriction.
                    rejected = await runtime._invoke_responses_tool("work", "{}", "before-resume")
                    self.assertEqual(rejected["reason"], "task_finished")
                    self.assertEqual(moves, [])
                    session.submit(SessionCommand.continue_(guidance))
                    await asyncio.wait_for(session.step(), 10)
                    self.assertIs(session.parts.runtime, runtime)
                    self.assertEqual(len(moves), 1)
                    self.assertEqual(body.x, 3.0)
                    self.assertEqual(store.get_plan(task_id).summary, "Preserve this plan")
                    self.assertEqual(store.get_task(task_id).status.value, "completed")
                    history = [item for event in wire.sent for item in event.get("input", [])]
                    self.assertTrue(any(item.get("call_id") == "finish1" for item in history))
                    outputs = {item["call_id"]: json.loads(item["output"]) for item in history
                               if item.get("type") == "function_call_output"}
                    self.assertTrue(outputs["resumed-move"]["success"])
                    self.assertTrue(any(e["event"] == "native_task_resumed" for e in runtime.trace.snapshot()))
                finally:
                    await session.aclose()
                    store.close()

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
            self.assertIn("pos=(3.0, 64.0, 0.0)", json.dumps(continuation["input"]))
        finally:
            await session.aclose()
            store.close()
            temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
