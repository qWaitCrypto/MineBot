"""Anthropic Messages API adapter for the OpenAI Agents ``Model`` seam.

The Agents SDK speaks in OpenAI Responses input/output items.  Anthropic's
Messages API is deliberately adapted here instead of pretending that its
endpoint is OpenAI-compatible.  The adapter owns only wire conversion;
tool execution, session history and Body state remain in the existing Agent
runtime.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from openai.types.chat import ChatCompletion, ChatCompletionChunk
from openai.types.responses import Response
from openai.types.responses.response_usage import InputTokensDetails

from agents.agent_output import AgentOutputSchemaBase
from agents.handoffs import Handoff
from agents.items import ModelResponse, TResponseInputItem, TResponseStreamEvent
from agents.model_settings import ModelSettings
from agents.models.chatcmpl_converter import Converter
from agents.models.chatcmpl_stream_handler import ChatCmplStreamHandler
from agents.models.interface import Model, ModelTracing
from agents.tool import Tool
from agents.usage import Usage

from minebot.app.prompt_cache import is_current_context_text


class AnthropicAPIError(RuntimeError):
    """A safe Anthropic API error without echoing request credentials."""


class AnthropicMessagesModel(Model):
    """OpenAI Agents model backed by ``POST /v1/messages``."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str | None = None,
        proxy: str | None = None,
        timeout_s: float = 120.0,
        prompt_cache: bool = False,
        prompt_cache_ttl: str | None = None,
        client: httpx.AsyncClient | None = None,
        anthropic_version: str = "2023-06-01",
    ) -> None:
        self.model = model
        self._base_url = (base_url or "https://api.anthropic.com/v1").rstrip("/")
        self._messages_url = (
            self._base_url if self._base_url.endswith("/messages") else self._base_url + "/messages"
        )
        self._api_key = api_key
        self._prompt_cache = prompt_cache
        self._prompt_cache_ttl = prompt_cache_ttl
        self._anthropic_version = anthropic_version
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            proxy=proxy,
            trust_env=not bool(proxy),
            timeout=httpx.Timeout(
                timeout_s,
                connect=min(10.0, timeout_s),
                write=min(30.0, timeout_s),
                pool=min(10.0, timeout_s),
            ),
            limits=httpx.Limits(keepalive_expiry=120.0),
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        *,
        previous_response_id: str | None = None,
        conversation_id: str | None = None,
        prompt: Any = None,
    ) -> ModelResponse:
        del output_schema, tracing, previous_response_id, conversation_id, prompt
        payload = self._payload(system_instructions, input, model_settings, tools, handoffs, stream=False)
        response = await self._client.post(
            self._messages_url,
            headers=self._headers(model_settings),
            json=payload,
        )
        raw = self._decode_response(response)
        chat = self._chat_completion(raw)
        message = chat.choices[0].message
        items = Converter.message_to_output_items(
            message,
            provider_data={"model": raw.get("model", self.model), "response_id": raw.get("id")},
        )
        usage = _usage_from_anthropic(raw.get("usage"))
        return ModelResponse(
            output=items,
            usage=usage,
            response_id=str(raw.get("id")) if raw.get("id") else None,
            request_id=response.headers.get("request-id"),
        )

    async def stream_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        *,
        previous_response_id: str | None = None,
        conversation_id: str | None = None,
        prompt: Any = None,
    ) -> AsyncIterator[TResponseStreamEvent]:
        del output_schema, tracing, previous_response_id, conversation_id, prompt
        payload = self._payload(system_instructions, input, model_settings, tools, handoffs, stream=True)
        async with self._client.stream(
            "POST", self._messages_url, headers=self._headers(model_settings), json=payload
        ) as response:
            self._raise_for_status(response)
            initial = Response.model_construct(
                id="anthropic-pending",
                created_at=time.time(),
                model=self.model,
                object="response",
                output=[],
                status="in_progress",
                tool_choice="auto",
                tools=[],
                parallel_tool_calls=False,
            )
            usage = _AnthropicUsage()
            async for event in ChatCmplStreamHandler.handle_stream(
                initial,
                self._chat_stream(response, usage),
                model=self.model,
            ):
                if event.type == "response.completed":
                    _attach_cache_write(event.response, usage.cache_write)
                yield event

    def _headers(self, settings: ModelSettings) -> dict[str, str]:
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": self._anthropic_version,
            "content-type": "application/json",
            "accept": "application/json",
        }
        for key, value in (settings.extra_headers or {}).items():
            if value is not None:
                headers[str(key)] = str(value)
        return headers

    def _payload(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        settings: ModelSettings,
        tools: list[Tool],
        handoffs: list[Handoff],
        *,
        stream: bool,
    ) -> dict[str, Any]:
        converted = Converter.items_to_messages(
            input,
            model=self.model,
            base_url=self._base_url,
            strict_feature_validation=False,
        )
        system_parts: list[dict[str, Any]] = []
        if system_instructions:
            system_parts.append({"type": "text", "text": system_instructions})
        messages: list[dict[str, Any]] = []
        for message in converted:
            role = str(message.get("role", "user"))
            if role in {"system", "developer"}:
                system_parts.extend(_text_blocks(message.get("content")))
                continue
            messages.append(_anthropic_message(message))
        messages = _merge_messages(messages)

        anthropic_tools = self._tools(tools, handoffs)
        if self._prompt_cache:
            _mark_cache_breakpoint(system_parts, self._prompt_cache_ttl)
            _mark_cache_breakpoint(anthropic_tools, self._prompt_cache_ttl)
            _mark_history_breakpoint(messages, self._prompt_cache_ttl)

        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": settings.max_tokens or 4096,
            "messages": messages,
            "stream": stream,
        }
        if system_parts:
            payload["system"] = system_parts
        if anthropic_tools:
            payload["tools"] = anthropic_tools
        if settings.temperature is not None:
            payload["temperature"] = settings.temperature
        if settings.top_p is not None:
            payload["top_p"] = settings.top_p
        # The Agents SDK exposes one portable reasoning control.  Anthropic's
        # Messages API carries the equivalent at output_config.effort.  Keep
        # ``none`` as provider-default (rather than forcing thinking off), and
        # map the SDK-only ``minimal`` spelling to Anthropic's lowest level.
        reasoning = _anthropic_reasoning(settings)
        if reasoning:
            payload.update(reasoning)
        if settings.tool_choice is not None:
            payload["tool_choice"] = _tool_choice(settings.tool_choice, settings.parallel_tool_calls)
        if settings.metadata:
            payload["metadata"] = settings.metadata
        if settings.extra_body:
            payload.update(settings.extra_body)
        return payload

    def _tools(self, tools: list[Tool], handoffs: list[Handoff]) -> list[dict[str, Any]]:
        converted = [Converter.tool_to_openai(tool) for tool in tools]
        converted.extend(Converter.convert_handoff_tool(handoff) for handoff in handoffs)
        result: list[dict[str, Any]] = []
        for item in converted:
            function = item.get("function") if isinstance(item, Mapping) else None
            if not isinstance(function, Mapping):
                continue
            entry: dict[str, Any] = {
                "name": function.get("name", "tool"),
                "description": function.get("description", ""),
                "input_schema": function.get("parameters") or {"type": "object", "properties": {}},
            }
            result.append(entry)
        return result

    def _decode_response(self, response: httpx.Response) -> dict[str, Any]:
        self._raise_for_status(response)
        try:
            raw = response.json()
        except ValueError as exc:
            raise AnthropicAPIError("Anthropic returned invalid JSON") from exc
        if not isinstance(raw, dict):
            raise AnthropicAPIError("Anthropic returned a non-object response")
        return raw

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.is_success:
            return
        try:
            body = response.json()
            error = body.get("error", {}) if isinstance(body, dict) else {}
            code = error.get("type") if isinstance(error, dict) else None
            message = error.get("message") if isinstance(error, dict) else None
            detail = f"{code}: {message}" if code or message else f"HTTP {response.status_code}"
        except ValueError:
            detail = f"HTTP {response.status_code}"
        raise AnthropicAPIError(f"Anthropic request failed: {detail}")

    def _chat_completion(self, raw: Mapping[str, Any]) -> ChatCompletion:
        content = raw.get("content") or []
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, Mapping):
                continue
            if block.get("type") == "text":
                text_parts.append(str(block.get("text") or ""))
            elif block.get("type") == "tool_use":
                tool_calls.append(
                    {
                        "id": str(block.get("id") or f"tool_{len(tool_calls)}"),
                        "type": "function",
                        "function": {
                            "name": str(block.get("name") or "tool"),
                            "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                        },
                    }
                )
        message: dict[str, Any] = {
            "role": "assistant",
            "content": "".join(text_parts) or None,
        }
        if tool_calls:
            message["tool_calls"] = tool_calls
        usage = _AnthropicUsage()
        usage.update(raw.get("usage"))
        return ChatCompletion.model_validate(
            {
                "id": str(raw.get("id") or "anthropic-response"),
                "object": "chat.completion",
                "created": int(time.time()),
                "model": str(raw.get("model") or self.model),
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": _finish_reason(raw.get("stop_reason")),
                    }
                ],
                "usage": _completion_usage(usage),
            }
        )

    async def _chat_stream(
        self, response: httpx.Response, usage: _AnthropicUsage
    ) -> AsyncIterator[ChatCompletionChunk]:
        message_id = "anthropic-response"
        model = self.model
        tool_indexes: dict[int, int] = {}
        next_tool_index = 0
        finished = False
        async for event_name, event in _sse_events(response):
            if event_name == "error":
                error = event.get("error") if isinstance(event, Mapping) else None
                kind = error.get("type") if isinstance(error, Mapping) else "api_error"
                raise AnthropicAPIError(f"Anthropic stream failed: {kind}")
            if event_name == "message_start":
                message = event.get("message") if isinstance(event, Mapping) else {}
                if isinstance(message, Mapping):
                    message_id = str(message.get("id") or message_id)
                    model = str(message.get("model") or model)
                    usage.update(message.get("usage"))
                yield _chunk(message_id, model, {"role": "assistant"})
                continue
            if event_name == "content_block_start":
                block = event.get("content_block") if isinstance(event, Mapping) else {}
                if not isinstance(block, Mapping) or block.get("type") != "tool_use":
                    continue
                source_index = int(event.get("index") or 0)
                tool_index = tool_indexes.setdefault(source_index, next_tool_index)
                next_tool_index = max(next_tool_index, tool_index + 1)
                initial = block.get("input") or {}
                yield _chunk(
                    message_id,
                    model,
                    {
                        "tool_calls": [
                            {
                                "index": tool_index,
                                "id": str(block.get("id") or f"tool_{tool_index}"),
                                "type": "function",
                                "function": {
                                    "name": str(block.get("name") or "tool"),
                                    "arguments": json.dumps(initial, ensure_ascii=False) if initial else "",
                                },
                            }
                        ]
                    },
                )
                continue
            if event_name == "content_block_delta":
                delta = event.get("delta") if isinstance(event, Mapping) else {}
                if not isinstance(delta, Mapping):
                    continue
                delta_type = delta.get("type")
                if delta_type == "text_delta":
                    yield _chunk(message_id, model, {"content": str(delta.get("text") or "")})
                elif delta_type == "input_json_delta":
                    source_index = int(event.get("index") or 0)
                    tool_index = tool_indexes.setdefault(source_index, next_tool_index)
                    next_tool_index = max(next_tool_index, tool_index + 1)
                    yield _chunk(
                        message_id,
                        model,
                        {"tool_calls": [{"index": tool_index, "function": {"arguments": str(delta.get("partial_json") or "")}}]},
                    )
                continue
            if event_name == "message_delta":
                delta = event.get("delta") if isinstance(event, Mapping) else {}
                usage.update(event.get("usage") if isinstance(event, Mapping) else None)
                stop_reason = delta.get("stop_reason") if isinstance(delta, Mapping) else None
                finish = _finish_reason(stop_reason)
                if finish is not None:
                    finished = True
                    yield _chunk(
                        message_id,
                        model,
                        {},
                        finish_reason=finish,
                        usage=_completion_usage(usage),
                    )
                continue
            if event_name == "message_stop" and not finished:
                finished = True
                yield _chunk(
                    message_id,
                    model,
                    {},
                    finish_reason="stop",
                    usage=_completion_usage(usage),
                )


def _anthropic_message(message: Mapping[str, Any]) -> dict[str, Any]:
    role = "assistant" if message.get("role") == "assistant" else "user"
    blocks = _content_blocks(message.get("content"))
    if role == "assistant":
        for call in message.get("tool_calls") or []:
            function = call.get("function") if isinstance(call, Mapping) else {}
            if not isinstance(function, Mapping):
                continue
            try:
                arguments = json.loads(str(function.get("arguments") or "{}"))
            except json.JSONDecodeError:
                arguments = {}
            blocks.append(
                {
                    "type": "tool_use",
                    "id": str(call.get("id") or f"tool_{len(blocks)}"),
                    "name": str(function.get("name") or "tool"),
                    "input": arguments,
                }
            )
    if message.get("role") == "tool":
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": str(message.get("tool_call_id") or "tool"),
                    "content": _tool_result_content(message.get("content")),
                }
            ],
        }
    return {"role": role, "content": blocks or [{"type": "text", "text": ""}]}


def _content_blocks(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        return [{"type": "text", "text": value}]
    if not isinstance(value, list):
        return []
    blocks: list[dict[str, Any]] = []
    for part in value:
        if isinstance(part, str):
            blocks.append({"type": "text", "text": part})
            continue
        if not isinstance(part, Mapping):
            continue
        kind = part.get("type")
        if kind in {"text", "input_text", "output_text"}:
            blocks.append({"type": "text", "text": str(part.get("text") or "")})
        elif kind in {"image_url", "input_image"}:
            image = part.get("image_url") if kind == "image_url" else part
            if isinstance(image, Mapping):
                url = image.get("url") or image.get("image_url")
            else:
                url = image
            converted = _image_block(url)
            if converted:
                blocks.append(converted)
        elif kind == "document":
            blocks.append(dict(part))
    return blocks


def _text_blocks(value: Any) -> list[dict[str, Any]]:
    blocks = _content_blocks(value)
    return [block for block in blocks if block.get("type") == "text"]


def _image_block(url: Any) -> dict[str, Any] | None:
    if not isinstance(url, str) or not url:
        return None
    if url.startswith("data:"):
        header, separator, data = url.partition(",")
        if not separator or ";base64" not in header:
            return None
        media_type = header[5:].split(";", 1)[0] or "image/png"
        try:
            base64.b64decode(data, validate=True)
        except ValueError:
            return None
        return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}
    parsed = urlsplit(url)
    if parsed.scheme in {"http", "https"}:
        return {"type": "image", "source": {"type": "url", "url": url}}
    return None


def _tool_result_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except TypeError:
        return str(value)


def _merge_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for message in messages:
        if merged and merged[-1]["role"] == message["role"]:
            previous = merged[-1]["content"]
            current = message["content"]
            if isinstance(previous, list) and isinstance(current, list):
                previous.extend(current)
            else:
                merged[-1]["content"] = [
                    *(_content_blocks(previous)),
                    *(_content_blocks(current)),
                ]
        else:
            merged.append(message)
    return merged


def _cache_control(ttl: str | None) -> dict[str, Any]:
    cache_control: dict[str, Any] = {"type": "ephemeral"}
    if ttl:
        cache_control["ttl"] = ttl
    return cache_control


def _mark_cache_breakpoint(items: list[dict[str, Any]], ttl: str | None) -> None:
    if not items:
        return
    items[-1]["cache_control"] = _cache_control(ttl)


def _mark_history_breakpoint(messages: list[dict[str, Any]], ttl: str | None) -> None:
    """Cache the conversation up to the volatile tail.

    The prefix order is tools -> system -> messages, so the system/tool
    breakpoints alone never cache history. The runner ends every request with
    one CURRENT_CONTEXT block that changes on each call; the breakpoint goes on
    the last block in front of it, so the next call (which only appends) finds
    this call's cache entry inside Anthropic's 20-block lookback. Without that
    tail the last block of the last message is the natural breakpoint.
    """
    if not messages:
        return
    for index in range(len(messages) - 1, -1, -1):
        content = messages[index].get("content")
        if not isinstance(content, list):
            continue
        end = len(content)
        if index == len(messages) - 1:
            while end and _is_volatile_block(content[end - 1]):
                end -= 1
        for block_index in range(end - 1, -1, -1):
            if _can_carry_cache_control(content[block_index]):
                content[block_index]["cache_control"] = _cache_control(ttl)
                return


def _is_volatile_block(block: Mapping[str, Any]) -> bool:
    return block.get("type") == "text" and is_current_context_text(block.get("text"))


def _can_carry_cache_control(block: Mapping[str, Any]) -> bool:
    # Anthropic rejects cache_control on empty text blocks.
    if block.get("type") == "text":
        return bool(block.get("text"))
    return block.get("type") in {"image", "document", "tool_use", "tool_result"}


def _tool_choice(value: Any, parallel: bool | None) -> dict[str, Any]:
    if isinstance(value, str):
        if value == "required":
            choice: dict[str, Any] = {"type": "any"}
        elif value == "none":
            choice = {"type": "none"}
        elif value == "auto":
            choice = {"type": "auto"}
        else:
            # Agents accepts a tool name as a string for forced selection;
            # Messages represents that as a named tool choice.
            choice = {"type": "tool", "name": value}
    elif getattr(value, "name", None):
        choice = {"type": "tool", "name": str(value.name)}
    else:
        choice = {"type": "auto"}
    if parallel is False and choice["type"] == "auto":
        choice["disable_parallel_tool_use"] = True
    return choice


def _anthropic_reasoning(settings: ModelSettings) -> dict[str, Any]:
    """Translate Agents ``Reasoning.effort`` to Messages ``output_config``.

    Anthropic's current adaptive-thinking API uses ``output_config.effort``;
    it does not accept the OpenAI/Agents ``minimal`` literal.  ``minimal`` is
    therefore lowered to Anthropic's ``low`` level.  Omitting the field for
    ``none`` preserves the provider/model default and keeps this adapter
    compatible with models whose thinking mode is selected server-side.
    """
    reasoning = settings.reasoning
    effort = getattr(reasoning, "effort", None) if reasoning is not None else None
    if not effort or effort == "none":
        return {}
    if effort == "minimal":
        effort = "low"
    return {"output_config": {"effort": effort}}


def _finish_reason(value: Any) -> str | None:
    return {
        "tool_use": "tool_calls",
        "end_turn": "stop",
        "stop_sequence": "stop",
        "max_tokens": "length",
    }.get(str(value), None)


@dataclass
class _AnthropicUsage:
    """Anthropic's four token counters, merged across message_start/message_delta.

    Anthropic's ``input_tokens`` excludes cached tokens (the cache read and the
    cache write are reported beside it), unlike OpenAI's ``prompt_tokens``.
    ``prompt_tokens`` here is the OpenAI-convention total, so downstream hit
    ratios and context sizes are not under-reported by the cached part.
    """

    input: int = 0
    cache_read: int = 0
    cache_write: int = 0
    output: int = 0

    def update(self, raw: Any) -> None:
        if not isinstance(raw, Mapping):
            return
        # message_delta repeats cumulative counters; a gateway that zeroes the
        # ones it does not know must not erase what message_start reported.
        self.input = max(self.input, _count(raw.get("input_tokens")))
        self.cache_read = max(self.cache_read, _count(raw.get("cache_read_input_tokens")))
        self.cache_write = max(self.cache_write, self._cache_write(raw))
        self.output = max(self.output, _count(raw.get("output_tokens")))

    @staticmethod
    def _cache_write(raw: Mapping[str, Any]) -> int:
        if raw.get("cache_creation_input_tokens") is not None:
            return _count(raw["cache_creation_input_tokens"])
        breakdown = raw.get("cache_creation")
        if isinstance(breakdown, Mapping):
            return sum(_count(value) for value in breakdown.values())
        return 0

    @property
    def prompt_tokens(self) -> int:
        return self.input + self.cache_read + self.cache_write


def _count(value: Any) -> int:
    return value if type(value) is int and value > 0 else 0


def _usage_from_anthropic(raw: Any) -> Usage:
    counted = _AnthropicUsage()
    counted.update(raw)
    return Usage(
        requests=1,
        input_tokens=counted.prompt_tokens,
        output_tokens=counted.output,
        total_tokens=counted.prompt_tokens + counted.output,
        input_tokens_details=InputTokensDetails(
            cached_tokens=counted.cache_read, cache_write_tokens=counted.cache_write
        ),
        output_tokens_details={"reasoning_tokens": 0},
    )


def _attach_cache_write(response: Any, cache_write: int) -> None:
    """Carry the cache-write count through the chat-completions detour.

    The chat-completions shape has no field for it, so the stream handler drops
    it; the response model accepts extra detail fields, which the trace keeps.
    """
    details = getattr(getattr(response, "usage", None), "input_tokens_details", None)
    if cache_write and details is not None:
        details.cache_write_tokens = cache_write


def _completion_usage(usage: _AnthropicUsage) -> dict[str, Any]:
    return {
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.output,
        "total_tokens": usage.prompt_tokens + usage.output,
        "prompt_tokens_details": {"cached_tokens": usage.cache_read},
    }


def _chunk(
    response_id: str,
    model: str,
    delta: dict[str, Any],
    *,
    finish_reason: str | None = None,
    usage: dict[str, Any] | None = None,
) -> ChatCompletionChunk:
    return ChatCompletionChunk.model_validate(
        {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
            "usage": usage,
        }
    )


async def _sse_events(response: httpx.Response) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    event_name = "message"
    data_lines: list[str] = []
    async for line in response.aiter_lines():
        if line.startswith("event:"):
            event_name = line[6:].strip() or "message"
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        elif not line:
            if not data_lines:
                continue
            raw = "\n".join(data_lines)
            data_lines = []
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise AnthropicAPIError("Anthropic stream returned invalid JSON") from exc
            if isinstance(payload, dict):
                yield event_name, payload
            event_name = "message"
    if data_lines:
        try:
            payload = json.loads("\n".join(data_lines))
        except json.JSONDecodeError as exc:
            raise AnthropicAPIError("Anthropic stream returned invalid JSON") from exc
        if isinstance(payload, dict):
            yield event_name, payload


__all__ = ["AnthropicAPIError", "AnthropicMessagesModel"]
