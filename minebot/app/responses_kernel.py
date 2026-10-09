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

from httpx import TransportError

from minebot.app.conversation import model_input_chars
from minebot.app.responses_observation import RequestObservation, context_sizes, current_observation, fingerprint


# Relay-reported upstream stream failures are transport incidents, not invalid
# client protocol. Share classification across SSE error and failed responses.
_RETRYABLE_RESPONSE_ERROR_CODES = frozenset({
    "server_error", "rate_limit_exceeded", "request_timeout", "upstream_http2_stream_error",
})


class ResponsesProtocolError(RuntimeError):
    """A concrete API/protocol failure, without silently changing kernels."""


class ResponsesTransportError(TimeoutError):
    """The response stream was lost; session-owned tool work remains valid."""


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

    def __init__(self, client: Any, emit: Callable[..., None] = lambda *a, **kw: None,
                 *, context_mode: str = "native", compact_threshold: int = 128_000) -> None:
        self.client = client
        self.emit = emit
        self.history: list[dict[str, Any]] = []
        self.events: asyncio.Queue[dict[str, Any] | Exception] = asyncio.Queue()
        self.request: asyncio.Task | None = None
        self.context_usage: dict[str, Any] = {}
        self.context_mode, self.compact_threshold = context_mode, compact_threshold
        self._usage_offset = 0
        self._previous_prefix = None
        self._previous_input: list[str] = []
        self._last_snapshot: str | None = None

    async def _settle_stream(self):
        if self.request is None:
            return
        started = time.monotonic()
        # A terminal has already been consumed by the kernel. Normally EOF is
        # immediate and returns the socket to the pool. A missing EOF must not
        # add the whole model read-timeout before the next request can open.
        try:
            await asyncio.wait_for(self.request, timeout=2.0)
        except TimeoutError:
            self.emit("responses_eof_cleanup", status="closed_after_terminal", elapsed_s=round(time.monotonic() - started, 3))
        else:
            self.emit("responses_eof_cleanup", status="drained", elapsed_s=round(time.monotonic() - started, 3))

    async def _summarize(self, payload: dict, inputs: list[dict]) -> list[dict]:
        calls = {i["call_id"] for i in inputs if i.get("type") == "function_call"}
        outputs = {i["call_id"] for i in inputs if i.get("type") == "function_call_output"}
        if calls - outputs:
            self.emit("responses_compaction_deferred", unresolved_calls=sorted(calls - outputs), mode="summary")
            return inputs
        # Ordinary Responses text generation works on third-party endpoints
        # without context_management, /compact or previous_response_id.
        transcript = [{k: v for k, v in i.items() if k != "encrypted_content"} for i in inputs]
        instructions = (
            "Summarize this ongoing Minecraft Agent conversation for its next turn. Do not act or call tools. "
            "Preserve player goals, additions and constraints, current short plan, exact useful coordinates and entity IDs, "
            "verified work, materials, unresolved blockers, failed approaches and why they failed. "
            "Keep historical observations distinct from current facts. Preserve any player cancellation. "
            "The latest CURRENT_CONTEXT is authoritative for the current goal, plan and Body state. "
            "Omit repetitive polling, superseded state and internal bookkeeping. Return only a concise continuation summary."
        )
        obs = RequestObservation(self.emit)
        token = current_observation.set(obs)
        obs.record("responses_compaction_started", mode="summary", before_items=len(inputs))
        try:
            stream = await self.client.responses.create(model=payload["model"], instructions=instructions,
                input=[{"role": "user", "content": json.dumps(transcript, ensure_ascii=False)}],
                store=False, stream=True, **{k: payload[k] for k in ("reasoning", "extra_body") if k in payload})
            parts, response = [], None
            async with stream:
                async for item in stream:
                    event = item.model_dump(mode="json", by_alias=True, exclude_none=True)
                    obs.event(event)
                    if event.get("type") == "response.output_text.delta":
                        parts.append(event["delta"])
                    elif event.get("type") in {"response.completed", "response.failed", "response.incomplete"}:
                        response = event["response"]
                        break
            summary = "".join(parts).strip()
            if not summary and response:
                summary = "\n".join(part.get("text", "") for output in response.get("output", [])
                    for part in output.get("content", []) if part.get("type") == "output_text").strip()
            if not summary or not response or response.get("status") != "completed":
                reason = (response or {}).get("incomplete_details") or (response or {}).get("error")
                obs.record("responses_compaction_failed", status=(response or {}).get("status"),
                           details=reason, usage=(response or {}).get("usage"))
                raise ResponsesProtocolError(f"Context summarization did not complete: {reason or 'missing summary/terminal'}")
            # Keep every original player message plus the latest live state.
            # User constraints are not left to a lossy summary alone.
            users = [i for i in inputs if i.get("role") == "user"]
            snapshot = next((i for i in reversed(inputs) if _is_snapshot(i)), None)
            compacted = users + [{"role": "developer", "content": "CONVERSATION_SUMMARY (historical):\n" + summary}]
            if snapshot:
                compacted.append(snapshot)
            obs.record("responses_compaction_finished", mode="summary", before_items=len(inputs),
                       after_items=len(compacted), usage=response.get("usage"),
                       after_sizes=context_sizes({**payload, "input": compacted}))
            self._usage_offset = 0
            return compacted
        finally:
            obs.finish("summary")
            current_observation.reset(token)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        if self.request is not None:
            self.request.cancel()
            await asyncio.gather(self.request, return_exceptions=True)
            self.request = None
        # History and tool receipts survive. Events from the abandoned stream
        # must never be mistaken for events belonging to its successor.
        while not self.events.empty():
            self.events.get_nowait()

    async def send_raw(self, raw: str) -> None:
        payload = json.loads(raw)
        if payload.pop("type") != "response.create":
            raise ResponsesProtocolError("HTTP Responses does not support native mid-turn steering")
        await self._settle_stream()
        payload.pop("previous_response_id", None)
        inputs = payload["input"]
        if isinstance(inputs, str):
            inputs = [{"role": "user", "content": inputs}]
        returned = {i["call_id"] for i in self.history if i.get("type") == "function_call_output"}
        inputs = [i for i in inputs if i.get("type") != "function_call_output" or i["call_id"] not in returned]
        snapshot = payload.pop("_current_context", "")
        if snapshot and snapshot != self._last_snapshot:
            inputs.append({"role": "developer", "content": "CURRENT_CONTEXT:\n" + snapshot})
        full_input = self.history + inputs
        counted_at = time.monotonic()
        sizes = await asyncio.to_thread(context_sizes, {**payload, "input": full_input})
        self.emit("responses_context_counted", elapsed_s=round(time.monotonic() - counted_at, 3),
                  estimates=sizes, usage_offset=self._usage_offset)
        rendered_estimate = max(0, sum(sizes[k] for k in ("instructions", "tools", "visible_input")) + self._usage_offset)
        if self.context_mode == "summary" and self.compact_threshold and rendered_estimate >= self.compact_threshold:
            full_input = await self._summarize(payload, full_input)
            sizes = context_sizes({**payload, "input": full_input})
            rendered_estimate = sum(sizes[k] for k in ("instructions", "tools", "visible_input"))
        prefix = fingerprint([payload.get("instructions"), payload.get("tools"), payload.get("model")])
        # This route actually needs a stable routing key: replay controls
        # without it reported zero cache hits; the same prefix with it reused
        # over 90% of input. An explicitly configured accounting key wins.
        payload.setdefault("prompt_cache_key", "minebot-" + prefix)
        signatures = [fingerprint(i) for i in full_input]
        shared = 0
        if prefix == self._previous_prefix:
            for old, new in zip(self._previous_input, signatures):
                if old != new:
                    break
                shared += 1
        obs = RequestObservation(self.emit)
        self.context_usage = {"history_chars": model_input_chars(full_input),
                              "input_chars": model_input_chars(full_input), "context_estimates": sizes,
                              "rendered_estimate": rendered_estimate, "context_mode": self.context_mode,
                              "compact_threshold": self.compact_threshold, "request_key": obs.request_key,
                              "static_prefix_hash": prefix, "static_prefix_reused": prefix == self._previous_prefix,
                              "shared_prefix_items": shared, "cache_key_hash": fingerprint(payload["prompt_cache_key"])}
        self._previous_prefix, self._previous_input = prefix, signatures
        payload.update(input=full_input, store=False, stream=True)
        payload["include"] = list(dict.fromkeys(payload.get("include", []) + ["reasoning.encrypted_content"]))
        self.history = list(full_input)
        if snapshot:
            self._last_snapshot = snapshot
        self.request = asyncio.create_task(self._stream(payload, obs))

    async def _stream(self, payload: dict[str, Any], observation: RequestObservation | None = None) -> None:
        output_start = len(self.history)
        started, last_event = time.monotonic(), "none"
        terminal = False
        obs = observation or RequestObservation(self.emit)
        token = current_observation.set(obs)
        try:
            async with await self.client.responses.create(**payload) as stream:
                response = getattr(stream, "response", None)
                if response is not None:
                    self.emit("responses_http_open", request_key=obs.request_key, http_version=response.http_version,
                              status_code=response.status_code,
                              latency_s=round(time.monotonic() - started, 3))
                async for item in stream:
                    if terminal:
                        continue
                    event = item.model_dump(mode="json", by_alias=True, exclude_none=True)
                    obs.event(event)
                    last_event = event.get("type", "unknown")
                    is_terminal = event.get("type") in {"response.completed", "response.incomplete", "response.failed"}
                    if event.get("type") == "response.output_item.done":
                        # A complete call may already be executing when the
                        # stream fails. Keep its original call_id for recovery.
                        self.history.append(event["item"])
                    if is_terminal:
                        usage = event["response"].get("usage") or {}
                        estimates = self.context_usage.get("context_estimates", {})
                        visible = sum(estimates.get(k, 0) for k in ("instructions", "tools", "visible_input"))
                        self._usage_offset = usage.get("input_tokens", visible) - visible
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
                    terminal = is_terminal
                    # Let the SDK consume EOF and return the connection to
                    # HTTPX's pool. Breaking on the terminal event abandons the
                    # body and forces another proxy/TLS handshake next time.
            if not terminal:
                raise ResponsesTransportError("HTTP Responses stream ended without a terminal event")
        except asyncio.CancelledError:
            raise
        except (OSError, TransportError) as exc:
            if terminal:
                return  # A failed tail cleanup cannot revoke a received terminal.
            error = ResponsesTransportError(
                f"HTTP Responses stream interrupted ({type(exc).__name__}, last_event={last_event}): {exc}")
            error.__cause__ = exc
            self.events.put_nowait(error)
        except Exception as exc:
            if not terminal:
                self.events.put_nowait(exc)
        finally:
            obs.finish("terminal" if terminal else "interrupted")
            current_observation.reset(token)

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
            before = len(self.history)
            del self.history[:latest]
            self._usage_offset = 0
            self._last_snapshot = None
            self.emit("responses_history_compacted", mode="native", before_items=before,
                      after_items=len(self.history), removed_items=latest,
                      remaining_sizes=context_sizes({"input": self.history}))
        else:
            self.emit("responses_compaction_deferred", mode="native", unresolved_calls=sorted(calls - results))
    async def recv_bytes(self) -> bytes:
        event = await self.events.get()
        if isinstance(event, Exception):
            raise event
        return json.dumps(event, ensure_ascii=False).encode()


def _is_snapshot(item: dict) -> bool:
    return item.get("role") == "developer" and str(item.get("content", "")).startswith("CURRENT_CONTEXT:\n")


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
        context: Callable[[], Awaitable[str]] | None = None,
        is_mutating: Callable[[str], bool] | None = None,
    ) -> None:
        self.client, self.model = client, model
        self.tools, self.instructions, self.invoke = tools, instructions, invoke
        self.emit, self.speak = emit, speak
        self.settings = dict(settings or {})
        self.context_mode = self.settings.pop("_context_mode", "native")
        self.compact_threshold = self.settings.pop("_compact_threshold", 128_000)
        self.context = context
        self.is_mutating = is_mutating
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
        self._facts: list[dict[str, Any]] = []
        self._fact_wake = asyncio.Event()

    def halt(self) -> None:
        self._halted = True

    def resume(self) -> None:
        self._halted = False

    @property
    def pending(self) -> list[PendingCall]:
        return [call for call in self.calls.values() if not call.delivered]

    @property
    def is_running(self) -> bool:
        """Whether a reader loop currently owns this kernel."""
        return self._running

    def steer(self, text: str) -> None:
        self._messages.put_nowait(text)

    def notify_fact(self, fact: dict[str, Any]) -> None:
        """Execution evidence; distinct from a user command and a tool's final output."""
        self._facts.append(dict(fact))
        self._fact_wake.set()

    async def _connect(self) -> None:
        if self._connection is None:
            # Reconnecting would lose accepted steering held by the server.
            if self.transport == "http":
                self._manager = _HttpResponsesConnection(self.client, self.emit,
                    context_mode=self.context_mode, compact_threshold=self.compact_threshold)
            else:
                options = {"proxy": self.proxy} if self.proxy else {}
                self._manager = self.client.responses.connect(max_retries=0, websocket_connection_options=options)
            self._connection = await self._manager.__aenter__()

    async def _send(self, event: dict[str, Any]) -> None:
        await self._connection.send_raw(json.dumps(event, ensure_ascii=False))

    async def _create(self, inputs: list[dict[str, Any]]) -> None:
        prepared_at = time.monotonic()
        instructions = await self.instructions()
        snapshot = await self.context() if self.context else ""
        tools = self.tools()
        if self.tool_namespace and tools:
            tools = [{"type": "namespace", "name": self.tool_namespace,
                      "description": "Minecraft observations, actions and current task controls.", "tools": tools}]
        pending = [{"call_id": c.call_id, "tool": c.name, "done": c.task.done()} for c in self.pending]
        if pending:
            if self.context:
                snapshot += "\nRUNNING_TOOLS: " + json.dumps(pending, ensure_ascii=False)
            else:
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
        if self.transport == "http":
            event["_current_context"] = snapshot
        elif snapshot:
            event["input"] = [*inputs, {"role": "developer", "content": "CURRENT_CONTEXT:\n" + snapshot}]
        self.emit("responses_prepare", elapsed_s=round(time.monotonic() - prepared_at, 3),
                  instruction_chars=len(instructions), snapshot_chars=len(snapshot), tool_count=len(tools))
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
        mutating = self.is_mutating(name) if self.is_mutating else bool(definitions.get(name, {}).get("async"))
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
        reader = message = fact_wait = None
        final_text, response_count = "", 0
        compactions = 0
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
            fact_wait = asyncio.create_task(self._fact_wake.wait())
            while True:
                # Reader is checked first below: a response.created queued on
                # the wire must be seen before deciding where to steer input.
                if not self._active and not self._auto_successor:
                    sync_waiting = any(not c.asynchronous and not c.task.done() for c in self.pending)
                    ready = [c for c in self.pending if c.task.done()]
                    inputs = [] if sync_waiting else [{"type": "function_call_output", "call_id": c.call_id,
                               "output": json.dumps(c.task.result(), ensure_ascii=False)} for c in ready]
                    if not sync_waiting:
                        inputs.extend({"role": "developer", "content": "EXECUTION_FACT: " + json.dumps(value, ensure_ascii=False)}
                                      for value in self._facts)
                        inputs.extend({"role": "user", "content": value} for value in queued)
                    if inputs:
                        if max_responses is not None and response_count >= max_responses:
                            raise ResponsesProtocolError("Response limit reached with unfinished work")
                        await self._create(inputs)
                        response_count += 1
                        queued.clear()
                        self._facts.clear()
                    elif not self.pending and self._messages.empty() and not message.done():
                        if self.needs_work():
                            queued.append("The current goal is still active. Continue executing it, or use finish_task with the actual result or concrete blocker.")
                            continue
                        return ResponsesResult(final_text, self.previous_response_id)
                futures = {reader, message, fact_wait, *(c.task for c in self.pending if not c.task.done())}
                # HTTPX measures actual read inactivity, including SSE comments
                # filtered by the SDK. An event deadline would kill a healthy
                # stream while the server is reasoning or compacting history.
                done, _ = await asyncio.wait(futures, return_when=asyncio.FIRST_COMPLETED,
                                            timeout=self.request_timeout_s if self.transport == "websocket"
                                            and (self._active or self._auto_successor) else None)
                if not done:
                    raise ResponsesTransportError(f"Timed out waiting for a Responses {self.transport} event")
                if fact_wait in done:
                    self._fact_wake.clear()
                    fact_wait = asyncio.create_task(self._fact_wake.wait())
                if reader in done:
                    event = json.loads(reader.result())
                    reader = asyncio.create_task(self._connection.recv_bytes())
                    kind = event.get("type")
                    if kind == "response.created":
                        compactions = 0
                        self.emit("responses_created", response_model=event["response"].get("model"),
                                  request_key=getattr(self._connection, "context_usage", {}).get("request_key"),
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
                        compactions += event["item"].get("type") == "compaction"
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
                        self.emit("responses_terminal", response_id=self.previous_response_id, reason=reason,
                                  error_code=(response.get("error") or {}).get("code"),
                                  status=response.get("status"), response_model=response.get("model"),
                                  usage=response.get("usage"),
                                  compactions=max(compactions, sum(item.get("type") == "compaction" for item in response.get("output", []))),
                                  elapsed_s=round(time.monotonic() - self._request_started_at, 3),
                                  request_key=getattr(self._connection, "context_usage", {}).get("request_key"),
                                  cache_read_ratio=((response.get("usage") or {}).get("input_tokens_details", {}).get("cached_tokens", 0)
                                                    / max(1, (response.get("usage") or {}).get("input_tokens", 0))))
                        if kind == "response.failed" or (kind == "response.incomplete" and reason != "steered"):
                            error = response.get("error") or {}
                            if error.get("code") in _RETRYABLE_RESPONSE_ERROR_CODES:
                                raise ResponsesTransportError(f"{kind}: {error}")
                            raise ResponsesProtocolError(f"{kind}: {reason or response.get('error')}")
                    elif kind == "error":
                        error = event.get("error", event)
                        if error.get("code") in _RETRYABLE_RESPONSE_ERROR_CODES:
                            raise ResponsesTransportError(f"{error.get('code')}: {error.get('message', '')}")
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
            for future in (reader, message, fact_wait):
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
