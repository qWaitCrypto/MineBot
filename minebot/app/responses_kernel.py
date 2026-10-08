"""Native Responses loop; tool work belongs to the session, not a response.

Uses the base SDK's HTTP event stream or raw WebSocket connection. Tool calls
use native async semantics; mid-turn steering is available on WebSocket.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from minebot.app.conversation import model_input_chars, responses_working_input


class ResponsesProtocolError(RuntimeError):
    """A concrete API/protocol failure, without silently changing kernels."""


@dataclass
class PendingCall:
    call_id: str
    name: str
    arguments: str
    task: asyncio.Task[dict[str, Any]]
    mutating: bool
    asynchronous: bool
    delivered: bool = False
    namespace: str | None = None


@dataclass(frozen=True)
class ResponsesResult:
    final_output: str
    response_id: str | None


class _HttpResponsesConnection:
    """Read native SSE events; retain full history for stateless continuation."""

    def __init__(self, client: Any) -> None:
        self.client = client
        self.history: list[dict[str, Any]] = []
        self.events: asyncio.Queue[dict[str, Any] | Exception] = asyncio.Queue()
        self.request: asyncio.Task | None = None
        self.context_usage: dict[str, int] = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        if self.request is not None:
            self.request.cancel()
            await asyncio.gather(self.request, return_exceptions=True)

    async def send_raw(self, raw: str) -> None:
        payload = json.loads(raw)
        if payload.pop("type") != "response.create":
            raise ResponsesProtocolError("HTTP Responses does not support native mid-turn steering")
        if self.request is not None:
            await self.request
        payload.pop("previous_response_id", None)
        inputs = payload["input"]
        if isinstance(inputs, str):
            inputs = [{"role": "user", "content": inputs}]
        returned = {i["call_id"] for i in self.history if i.get("type") == "function_call_output"}
        inputs = [i for i in inputs if i.get("type") != "function_call_output" or i["call_id"] not in returned]
        full_input = self.history + inputs
        working_input = responses_working_input(full_input)
        self.context_usage = {"history_chars": model_input_chars(full_input),
                              "input_chars": model_input_chars(working_input)}
        payload.update(input=working_input, store=False, stream=True)
        payload["include"] = list(dict.fromkeys(payload.get("include", []) + ["reasoning.encrypted_content"]))
        self.history.extend(inputs)
        self.request = asyncio.create_task(self._stream(payload))

    async def _stream(self, payload: dict[str, Any]) -> None:
        output_start = len(self.history)
        try:
            terminal = False
            async with await self.client.responses.create(**payload) as stream:
                async for item in stream:
                    event = item.model_dump(mode="json", by_alias=True, exclude_none=True)
                    terminal = event.get("type") in {"response.completed", "response.incomplete", "response.failed"}
                    if event.get("type") == "response.output_item.done":
                        # A complete call may already be executing when the
                        # stream fails. Keep its original call_id for recovery.
                        self.history.append(event["item"])
                    if terminal:
                        # Some streams omit completed items from the terminal
                        # response. Keep calls already dispatched on their SSE
                        # events; merge final versions without duplicating them.
                        output = self.history[output_start:]
                        for item in event["response"].get("output", []):
                            key = item.get("call_id") or item.get("id")
                            index = next((i for i, old in enumerate(output)
                                          if key and key == (old.get("call_id") or old.get("id"))), None)
                            if index is not None:
                                output[index] = item
                            elif item not in output:
                                output.append(item)
                        self.history[output_start:] = output
                        if event["type"] == "response.completed":
                            self._prune_compacted_history()
                    self.events.put_nowait(event)
                    if terminal:
                        break
            if not terminal:
                raise ResponsesProtocolError("HTTP Responses stream ended without a terminal event")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.events.put_nowait(exc)

    def _prune_compacted_history(self) -> None:
        # Only inline compaction output has this pruning rule. Do not apply it
        # to /responses/compact's returned window. Keep unresolved native calls
        # visible until their outputs have been supplied.
        latest = next((i for i in range(len(self.history) - 1, -1, -1)
                       if self.history[i].get("type") == "compaction"), None)
        if latest is None:
            return
        calls = {item["call_id"] for item in self.history[:latest] if item.get("type") == "function_call"}
        results = {item["call_id"] for item in self.history if item.get("type") == "function_call_output"}
        if calls <= results:
            del self.history[:latest]

    async def recv_bytes(self) -> bytes:
        event = await self.events.get()
        if isinstance(event, Exception):
            raise event
        return json.dumps(event, ensure_ascii=False).encode()


class ResponsesKernel:
    def __init__(
        self,
        *,
        client: Any,
        model: str,
        tools: Callable[[], list[dict[str, Any]]],
        instructions: Callable[[], Awaitable[str]],
        invoke: Callable[[str, str, str], Awaitable[dict[str, Any]]],
        emit: Callable[..., None],
        speak: Callable[[str], None],
        settings: dict[str, Any] | None = None,
        request_timeout_s: float = 120.0,
        needs_work: Callable[[], bool] = lambda: False,
        proxy: str | None = None,
        transport: str = "websocket",
        tool_namespace: str | None = None,
    ) -> None:
        self.client, self.model = client, model
        self.tools, self.instructions, self.invoke = tools, instructions, invoke
        self.emit, self.speak = emit, speak
        self.settings = settings or {}
        self.request_timeout_s = request_timeout_s
        self.needs_work = needs_work
        self.proxy = proxy
        if transport not in {"websocket", "http"}:
            raise ValueError("Responses transport must be websocket or http")
        self.transport = transport
        self.tool_namespace = tool_namespace
        self._request_started_at = 0.0
        self.calls: dict[str, PendingCall] = {}
        self.previous_response_id: str | None = None
        self._connection: Any = None
        self._manager: Any = None
        self._messages: asyncio.Queue[str] = asyncio.Queue()
        self._active = False
        self._active_response_id: str | None = None
        self._auto_successor = False
        self._steering: list[str] = []
        self._spoken_items: set[str] = set()
        self._running = False
        self._halted = False
        self._submitted_calls: list[str] = []
        self._queued_inputs: list[str] = []

    def halt(self) -> None:
        self._halted = True

    def resume(self) -> None:
        self._halted = False

    @property
    def pending(self) -> list[PendingCall]:
        return [call for call in self.calls.values() if not call.delivered]

    def steer(self, text: str) -> None:
        self._messages.put_nowait(text)

    async def _connect(self) -> None:
        if self._connection is None:
            # Reconnecting would lose accepted steering held by the server.
            if self.transport == "http":
                self._manager = _HttpResponsesConnection(self.client)
            else:
                options = {"proxy": self.proxy} if self.proxy else {}
                self._manager = self.client.responses.connect(max_retries=0, websocket_connection_options=options)
            self._connection = await self._manager.__aenter__()

    async def _send(self, event: dict[str, Any]) -> None:
        await self._connection.send_raw(json.dumps(event, ensure_ascii=False))

    async def _create(self, inputs: list[dict[str, Any]]) -> None:
        instructions = await self.instructions()
        tools = self.tools()
        if self.tool_namespace and tools:
            tools = [{"type": "namespace", "name": self.tool_namespace,
                      "description": "Minecraft observations, actions and current task controls.", "tools": tools}]
        pending = [{"call_id": c.call_id, "tool": c.name, "done": c.task.done()} for c in self.pending]
        if pending:
            instructions += "\nRUNNING_TOOLS: " + json.dumps(pending, ensure_ascii=False)
        event = {
            "type": "response.create", "model": self.model,
            "instructions": instructions, "tools": tools,
            "input": inputs, "store": True, **self.settings,
        }
        if any(not call.task.done() for call in self.pending):
            # Don't request compaction while a previous physical action still
            # owes its native result. The next settled request can compact.
            event.pop("context_management", None)
        if self.previous_response_id:
            event["previous_response_id"] = self.previous_response_id
        self._request_started_at = time.monotonic()
        await self._send(event)
        self._submitted_calls = [item["call_id"] for item in inputs if item.get("type") == "function_call_output"]
        self._active = True
        self._active_response_id = None
        self.emit("responses_request", transport=self.transport, previous_response_id=self.previous_response_id,
                  pending_calls=[c.call_id for c in self.pending], instruction_chars=len(instructions),
                  **getattr(self._connection, "context_usage", {}))

    async def _invoke(self, name: str, arguments: str, call_id: str) -> dict[str, Any]:
        try:
            return await self.invoke(name, arguments, call_id)
        except asyncio.CancelledError:
            return {"success": False, "reason": "cancelled", "canRetry": False}
        except Exception as exc:
            # Execution errors are real tool results; model transport errors
            # are raised by the connection and never disguised as results.
            return {"success": False, "reason": "tool_exception", "canRetry": False,
                    "metrics": {"error_type": type(exc).__name__, "message": str(exc)[:400]}}

    def _dispatch(self, item: dict[str, Any]) -> None:
        if item.get("type") != "function_call":
            return
        call_id, name = item["call_id"], item["name"]
        namespace = item.get("namespace")
        arguments = item.get("arguments", "{}")
        if call_id in self.calls:
            old = self.calls[call_id]
            if (name, arguments, namespace) != (old.name, old.arguments, old.namespace):
                raise ResponsesProtocolError("Conflicting duplicate function call: " + call_id)
            return
        definitions = {tool["name"]: tool for tool in self.tools()}
        mutating = bool(definitions.get(name, {}).get("async"))
        writer = next((c for c in self.pending if c.mutating and not c.task.done()), None)
        if self._halted:
            job = self._constant({"success": False, "reason": "cancelled", "canRetry": False})
        elif name not in definitions or namespace not in {None, self.tool_namespace}:
            job = self._constant({"success": False, "reason": "unknown_or_disabled_tool", "tool": name})
        elif mutating and writer:
            job = self._constant({"success": False, "reason": "owner_busy", "canRetry": True,
                                  "metrics": {"call_id": writer.call_id, "tool": writer.name}})
        else:
            job = self._invoke(name, arguments, call_id)
        asynchronous = bool(item.get("async", False))
        self.calls[call_id] = PendingCall(call_id, name, arguments, asyncio.create_task(job), mutating, asynchronous)
        self.calls[call_id].namespace = namespace
        self.emit("responses_tool_started", tool=name, tool_call_id=call_id, asynchronous=asynchronous)

    @staticmethod
    async def _constant(value: dict[str, Any]) -> dict[str, Any]:
        return value

    def _observe_item(self, item: dict[str, Any]) -> str:
        self._dispatch(item)
        if item.get("type") != "message" or item.get("role") != "assistant":
            return ""
        item_id = item.get("id")
        if item_id in self._spoken_items:
            return ""
        if item_id:
            self._spoken_items.add(item_id)
        text = "\n".join(part.get("text", "") for part in item.get("content", [])
                         if part.get("type") == "output_text").strip()
        if text:
            self.speak(text)
        return text

    async def run(self, text: str, *, max_responses: int | None = None) -> ResponsesResult:
        if self._running:
            raise RuntimeError("Responses kernel already running")
        self._running = True
        reader = message = None
        final_text, response_count = "", 0
        queued = self._queued_inputs
        if text:
            queued.append(text)
        try:
            await self._connect()
            if self.transport == "http":
                for item in self._connection.history:
                    self._dispatch(item)
            reader = asyncio.create_task(self._connection.recv_bytes())
            message = asyncio.create_task(self._messages.get())
            while True:
                # Reader is checked first below: a response.created queued on
                # the wire must be seen before deciding where to steer input.
                if not self._active and not self._auto_successor:
                    sync_waiting = any(not c.asynchronous and not c.task.done() for c in self.pending)
                    ready = [c for c in self.pending if c.task.done()]
                    inputs = [] if sync_waiting else [{"type": "function_call_output", "call_id": c.call_id,
                               "output": json.dumps(c.task.result(), ensure_ascii=False)} for c in ready]
                    if not sync_waiting:
                        inputs.extend({"role": "user", "content": value} for value in queued)
                    if inputs:
                        if max_responses is not None and response_count >= max_responses:
                            raise ResponsesProtocolError("Response limit reached with unfinished work")
                        await self._create(inputs)
                        response_count += 1
                        queued.clear()
                    elif not self.pending and self._messages.empty() and not message.done():
                        if self.needs_work():
                            queued.append("The current goal is still active. Continue executing it, or use finish_task with the actual result or concrete blocker.")
                            continue
                        return ResponsesResult(final_text, self.previous_response_id)
                futures = {reader, message, *(c.task for c in self.pending if not c.task.done())}
                done, _ = await asyncio.wait(futures, return_when=asyncio.FIRST_COMPLETED,
                                            timeout=self.request_timeout_s if self._active or self._auto_successor else None)
                if not done:
                    raise ResponsesProtocolError(f"Timed out waiting for a Responses {self.transport} event")
                if reader in done:
                    event = json.loads(reader.result())
                    reader = asyncio.create_task(self._connection.recv_bytes())
                    kind = event.get("type")
                    if kind == "response.created":
                        self.emit("responses_created", response_model=event["response"].get("model"),
                                  latency_s=round(time.monotonic() - self._request_started_at, 3))
                        for call_id in self._submitted_calls:
                            call = self.calls[call_id]
                            call.delivered = True
                            self.emit("responses_tool_output", tool=call.name, tool_call_id=call_id)
                        self._submitted_calls.clear()
                        self.previous_response_id = event["response"]["id"]
                        self._active_response_id = self.previous_response_id
                        self._active, self._auto_successor = True, False
                        if self.transport == "websocket":
                            for value in queued:
                                await self._send({"type": "response.steer", "previous_response_id": self.previous_response_id, "input": value})
                                self._steering.append(value)
                            queued.clear()
                    elif kind == "response.output_item.done":
                        final_text = self._observe_item(event["item"]) or final_text
                    elif kind == "response.steer.accepted":
                        self._auto_successor = True
                        if self._steering:
                            self._steering.pop(0)
                        self.emit("responses_steer_accepted", response_id=self.previous_response_id)
                    elif kind == "response.steer.pending":
                        self._auto_successor = False
                        self.emit("responses_steer_pending", response_id=self.previous_response_id)
                    elif kind in {"response.completed", "response.incomplete", "response.failed"}:
                        response = event["response"]
                        self.previous_response_id = response["id"]
                        self._active = False
                        self._active_response_id = None
                        for item in response.get("output", []):
                            final_text = self._observe_item(item) or final_text
                        reason = (response.get("incomplete_details") or {}).get("reason")
                        if kind == "response.failed" or (kind == "response.incomplete" and reason != "steered"):
                            raise ResponsesProtocolError(f"{kind}: {reason or response.get('error')}")
                        self.emit("responses_terminal", response_id=self.previous_response_id, reason=reason,
                                  status=response.get("status"), response_model=response.get("model"),
                                  usage=response.get("usage"),
                                  compactions=sum(item.get("type") == "compaction" for item in response.get("output", [])),
                                  elapsed_s=round(time.monotonic() - self._request_started_at, 3))
                    elif kind == "error":
                        error = event.get("error", event)
                        raise ResponsesProtocolError(f"{error.get('code', 'api_error')}: {error.get('message', '')}")
                if message in done:
                    value = message.result()
                    message = asyncio.create_task(self._messages.get())
                    if self.transport == "websocket" and self._active and self._active_response_id:
                        await self._send({"type": "response.steer", "previous_response_id": self._active_response_id, "input": value})
                        self._steering.append(value)
                    else:
                        queued.append(value)
        except BaseException:
            if self.transport == "http":
                # Preserve jobs, completed items and queued user input. A new
                # run can continue the same history after a failed SSE stream.
                if self._manager is not None:
                    await self._manager.__aexit__(None, None, None)
                self._active = False
                self._active_response_id = None
                self._submitted_calls.clear()
            raise
        finally:
            self._running = False
            # Tool tasks intentionally survive model cancellation/failure.
            for future in (reader, message):
                if future is not None:
                    if future is message and future.done() and not future.cancelled():
                        queued.append(future.result())
                    future.cancel()
                    await asyncio.gather(future, return_exceptions=True)

    async def close(self) -> None:
        if self._connection is not None:
            await self._manager.__aexit__(None, None, None)
            self._connection = self._manager = None
        self._active = self._auto_successor = False
        self._active_response_id = None
        if self._steering:
            self.emit("responses_steering_unconfirmed", count=len(self._steering))
            self._steering.clear()

    async def reset(self) -> None:
        """Called only after an explicit control has settled physical work."""
        if any(not c.task.done() for c in self.pending):
            raise RuntimeError("Cannot reset while tools are still running")
        await self.close()
        self.calls.clear()
        self._submitted_calls.clear()
        self.previous_response_id = None
        self._spoken_items.clear()
        self._queued_inputs.clear()
        while not self._messages.empty():
            self._messages.get_nowait()
        self._halted = False
