from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from agents import Agent, Runner, function_tool
from agents.model_settings import ModelSettings, Reasoning
from agents.tool import FunctionTool

from minebot.app.anthropic_model import AnthropicAPIError, AnthropicMessagesModel
from minebot.app.config import AppConfigError, provider_registry_from_env


def _tool() -> FunctionTool:
    async def invoke(_context, _arguments):
        return "ok"

    return FunctionTool(
        name="move_to",
        description="Move the bot",
        params_json_schema={
            "type": "object",
            "properties": {"x": {"type": "integer"}},
            "required": ["x"],
        },
        on_invoke_tool=invoke,
    )


def _tracing():
    return SimpleNamespace(is_disabled=True)


def test_anthropic_env_defaults_to_messages_sdk_and_standard_key() -> None:
    provider = provider_registry_from_env(
        {
            "MINEBOT_LLM_KIND": "anthropic_messages",
            "MINEBOT_LLM_MODEL": "claude-sonnet-4-5",
            "ANTHROPIC_API_KEY": "test-key",
            "MINEBOT_LLM_REASONING_EFFORT": "high",
        }
    )
    assert provider.kernel == "sdk"
    assert provider._configs["primary"].api_key_env == "ANTHROPIC_API_KEY"
    assert provider._configs["primary"].api_shape == "messages"
    assert isinstance(provider.resolve("primary"), AnthropicMessagesModel)
    assert provider.model_settings_for("primary").reasoning.effort == "high"
    asyncio.run(provider.aclose())

    with pytest.raises(AppConfigError, match="Responses kernel"):
        provider_registry_from_env(
            {
                "MINEBOT_AGENT_KERNEL": "responses",
                "MINEBOT_LLM_KIND": "anthropic_messages",
                "MINEBOT_LLM_MODEL": "claude-sonnet-4-5",
                "ANTHROPIC_API_KEY": "test-key",
            }
        )


def test_anthropic_messages_maps_images_tools_and_cache() -> None:
    asyncio.run(_test_anthropic_messages_maps_images_tools_and_cache())


async def _test_anthropic_messages_maps_images_tools_and_cache() -> None:
    captured: dict = {}
    captured_headers: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured_headers.update({key.lower(): value for key, value in request.headers.items()})
        captured.update(json.loads((await request.aread()).decode()))
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-5",
                "content": [
                    {"type": "tool_use", "id": "tool_1", "name": "move_to", "input": {"x": 4}}
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 12, "output_tokens": 5},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    model = AnthropicMessagesModel(
        model="claude-sonnet-4-5",
        api_key="test-key",
        client=client,
        prompt_cache=True,
        prompt_cache_ttl="5m",
    )
    try:
        result = await model.get_response(
            "You are MineBot.",
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "Go there."},
                        {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
                    ],
                }
            ],
            ModelSettings(
                max_tokens=100,
                reasoning=Reasoning(effort="high"),
                tool_choice="move_to",
            ),
            [_tool()],
            None,
            [],
            _tracing(),
        )
    finally:
        await client.aclose()

    assert result.response_id == "msg_1"
    assert captured_headers["x-api-key"] == "test-key"
    assert captured_headers["anthropic-version"] == "2023-06-01"
    assert "authorization" not in captured_headers
    assert result.output[0].type == "function_call"
    assert captured["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "5m"}
    assert captured["tools"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "5m"}
    assert captured["messages"][0]["content"][1] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"},
        # last block of the history: the third breakpoint
        "cache_control": {"type": "ephemeral", "ttl": "5m"},
    }
    assert captured["tools"][0]["input_schema"]["properties"]["x"]["type"] == "integer"
    assert captured["output_config"] == {"effort": "high"}
    assert captured["tool_choice"] == {"type": "tool", "name": "move_to"}


def test_anthropic_effort_none_keeps_provider_default_and_minimal_maps_to_low() -> None:
    model = AnthropicMessagesModel(model="claude", api_key="test-key")
    try:
        minimal = model._payload(None, "hello", ModelSettings(reasoning=Reasoning(effort="minimal")), [], [], stream=False)
        default = model._payload(None, "hello", ModelSettings(reasoning=Reasoning(effort="none")), [], [], stream=False)
    finally:
        asyncio.run(model.close())
    assert minimal["output_config"] == {"effort": "low"}
    assert "output_config" not in default


def test_anthropic_stream_is_translated_to_agents_events() -> None:
    asyncio.run(_test_anthropic_stream_is_translated_to_agents_events())


async def _test_anthropic_stream_is_translated_to_agents_events() -> None:
    events = [
        ("message_start", {"message": {"id": "msg_2", "model": "claude", "usage": {"input_tokens": 3}}}),
        ("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "hello"}}),
        ("content_block_stop", {"index": 0}),
        ("message_delta", {"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}}),
        ("message_stop", {}),
    ]
    body = "".join(
        f"event: {name}\ndata: {json.dumps(payload)}\n\n" for name, payload in events
    ).encode()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/event-stream"},
            content=body,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    model = AnthropicMessagesModel(model="claude", api_key="test-key", client=client)
    try:
        received = [
            event
            async for event in model.stream_response(
                None, "hello", ModelSettings(max_tokens=20), [], None, [], _tracing()
            )
        ]
    finally:
        await client.aclose()

    assert [event.type for event in received][0] == "response.created"
    assert received[-1].type == "response.completed"
    assert received[-1].response.output[0].content[0].text == "hello"
    assert received[-1].response.usage.input_tokens == 3
    assert received[-1].response.usage.output_tokens == 2


def test_anthropic_stream_tool_use_is_translated_to_function_call() -> None:
    asyncio.run(_test_anthropic_stream_tool_use_is_translated_to_function_call())


async def _test_anthropic_stream_tool_use_is_translated_to_function_call() -> None:
    events = [
        ("message_start", {"message": {"id": "msg_3", "model": "claude", "usage": {"input_tokens": 3}}}),
        (
            "content_block_start",
            {"index": 0, "content_block": {"type": "tool_use", "id": "tool_3", "name": "move_to", "input": {}}},
        ),
        (
            "content_block_delta",
            {"index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"x":4}'}},
        ),
        ("content_block_stop", {"index": 0}),
        ("message_delta", {"delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 2}}),
        ("message_stop", {}),
    ]
    body = "".join(
        f"event: {name}\ndata: {json.dumps(payload)}\n\n" for name, payload in events
    ).encode()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/event-stream"},
            content=body,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    model = AnthropicMessagesModel(model="claude", api_key="test-key", client=client)
    try:
        received = [
            event
            async for event in model.stream_response(
                None, "move", ModelSettings(max_tokens=20), [_tool()], None, [], _tracing()
            )
        ]
    finally:
        await client.aclose()

    completed = received[-1].response
    function_calls = [item for item in completed.output if item.type == "function_call"]
    assert len(function_calls) == 1
    assert function_calls[0].name == "move_to"
    assert json.loads(function_calls[0].arguments) == {"x": 4}


def test_anthropic_model_runs_an_agents_tool_loop_over_streaming_messages() -> None:
    asyncio.run(_test_anthropic_model_runs_an_agents_tool_loop_over_streaming_messages())


async def _test_anthropic_model_runs_an_agents_tool_loop_over_streaming_messages() -> None:
    requests: list[dict] = []

    def stream_body(index: int) -> bytes:
        if index == 1:
            events = [
                ("message_start", {"message": {"id": "loop_1", "model": "claude", "usage": {"input_tokens": 3}}}),
                (
                    "content_block_start",
                    {"index": 0, "content_block": {"type": "tool_use", "id": "tool_loop", "name": "echo_tool", "input": {}}},
                ),
                (
                    "content_block_delta",
                    {"index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"value":"hi"}'}},
                ),
                ("content_block_stop", {"index": 0}),
                ("message_delta", {"delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 2}}),
                ("message_stop", {}),
            ]
        else:
            events = [
                ("message_start", {"message": {"id": "loop_2", "model": "claude", "usage": {"input_tokens": 3}}}),
                ("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
                ("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "done"}}),
                ("content_block_stop", {"index": 0}),
                ("message_delta", {"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}}),
                ("message_stop", {}),
            ]
        return "".join(
            f"event: {name}\ndata: {json.dumps(payload)}\n\n" for name, payload in events
        ).encode()

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads((await request.aread()).decode()))
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/event-stream"},
            content=stream_body(len(requests)),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    model = AnthropicMessagesModel(model="claude", api_key="test-key", client=client)

    @function_tool
    async def echo_tool(value: str) -> str:
        return value

    try:
        agent = Agent(name="anthropic-test", instructions="Use the tool.", model=model, tools=[echo_tool])
        run = Runner.run_streamed(agent, "Call the tool.")
        async for _event in run.stream_events():
            pass
    finally:
        await client.aclose()

    assert run.final_output == "done"
    assert len(requests) == 2
    assert requests[1]["messages"][-1]["content"][0]["type"] == "tool_result"


def _history_with_context(context_text: str) -> list[dict]:
    return [
        {"role": "user", "content": "Go there."},
        {"type": "function_call", "call_id": "c1", "name": "move_to", "arguments": '{"x": 1}'},
        {"type": "function_call_output", "call_id": "c1", "output": "ok"},
        {"role": "user", "content": f"CURRENT_CONTEXT:\n{context_text}"},
    ]


def test_anthropic_history_breakpoint_sits_in_front_of_the_volatile_context() -> None:
    model = AnthropicMessagesModel(
        model="claude", api_key="test-key", prompt_cache=True, prompt_cache_ttl="5m"
    )
    try:
        first = model._payload("sys", _history_with_context("STATE: pos=(1, 2, 3)"), ModelSettings(), [_tool()], [], stream=False)
        second = model._payload("sys", _history_with_context("STATE: pos=(9, 9, 9)"), ModelSettings(), [_tool()], [], stream=False)
    finally:
        asyncio.run(model.close())

    for payload in (first, second):
        blocks = [block for message in payload["messages"] for block in message["content"]]
        marked = [block for block in blocks if "cache_control" in block]
        assert len(marked) == 1
        assert marked[0]["type"] == "tool_result"  # last block before CURRENT_CONTEXT
        assert blocks[-1]["text"].startswith("CURRENT_CONTEXT:")
        assert "cache_control" not in blocks[-1]
        # tools + system + history: within Anthropic's four-breakpoint limit
        assert "cache_control" in payload["system"][-1] and "cache_control" in payload["tools"][-1]
    # Only the tail differs, so everything up to the breakpoint is a cache hit.
    assert first["system"] == second["system"] and first["tools"] == second["tools"]
    assert first["messages"][:-1] == second["messages"][:-1]
    assert first["messages"][-1]["content"][:-1] == second["messages"][-1]["content"][:-1]


def test_anthropic_history_breakpoint_without_context_marks_the_last_block() -> None:
    model = AnthropicMessagesModel(model="claude", api_key="test-key", prompt_cache=True)
    try:
        payload = model._payload("sys", [{"role": "user", "content": "hello"}], ModelSettings(), [], [], stream=False)
    finally:
        asyncio.run(model.close())
    assert payload["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}


def test_anthropic_without_prompt_cache_sends_no_breakpoints() -> None:
    model = AnthropicMessagesModel(model="claude", api_key="test-key", prompt_cache=False)
    try:
        payload = model._payload("sys", _history_with_context("STATE: x"), ModelSettings(), [_tool()], [], stream=False)
    finally:
        asyncio.run(model.close())
    assert "cache_control" not in json.dumps(payload)


def test_anthropic_cache_is_on_by_default_and_can_be_disabled() -> None:
    env = {
        "MINEBOT_LLM_KIND": "anthropic_messages",
        "MINEBOT_LLM_MODEL": "claude-sonnet-4-5",
        "ANTHROPIC_API_KEY": "test-key",
    }
    default = provider_registry_from_env(env)
    disabled = provider_registry_from_env({**env, "MINEBOT_LLM_PROMPT_CACHE": "false"})
    try:
        assert default.resolve("primary")._prompt_cache is True
        assert disabled.resolve("primary")._prompt_cache is False
    finally:
        asyncio.run(default.aclose())
        asyncio.run(disabled.aclose())


def test_anthropic_usage_counts_cached_and_written_tokens_as_input() -> None:
    asyncio.run(_test_anthropic_usage_counts_cached_and_written_tokens_as_input())


async def _test_anthropic_usage_counts_cached_and_written_tokens_as_input() -> None:
    usage = {
        "input_tokens": 16,
        "cache_read_input_tokens": 14415,
        "cache_creation_input_tokens": 37,
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads((await request.aread()).decode())
        if payload["stream"]:
            events = [
                ("message_start", {"message": {"id": "u1", "model": "claude", "usage": usage}}),
                ("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
                ("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "ok"}}),
                ("content_block_stop", {"index": 0}),
                # a gateway may zero the counters it does not forward on message_delta
                ("message_delta", {"delta": {"stop_reason": "end_turn"}, "usage": {"input_tokens": 0, "output_tokens": 5}}),
                ("message_stop", {}),
            ]
            body = "".join(f"event: {n}\ndata: {json.dumps(p)}\n\n" for n, p in events).encode()
            return httpx.Response(200, request=request, headers={"content-type": "text/event-stream"}, content=body)
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "u2", "type": "message", "role": "assistant", "model": "claude",
                "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
                "usage": {**usage, "output_tokens": 5},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    model = AnthropicMessagesModel(model="claude", api_key="test-key", client=client)
    try:
        blocking = await model.get_response("sys", "hi", ModelSettings(max_tokens=20), [], None, [], _tracing())
        streamed = [e async for e in model.stream_response("sys", "hi", ModelSettings(max_tokens=20), [], None, [], _tracing())]
    finally:
        await client.aclose()

    streamed_usage = streamed[-1].response.usage
    for counted in (blocking.usage, streamed_usage):
        assert counted.input_tokens == 16 + 14415 + 37
        assert counted.output_tokens == 5
        assert counted.total_tokens == 16 + 14415 + 37 + 5
        assert counted.input_tokens_details.cached_tokens == 14415
        assert counted.input_tokens_details.model_dump()["cache_write_tokens"] == 37


def test_anthropic_cache_counts_survive_a_runner_loop() -> None:
    asyncio.run(_test_anthropic_cache_counts_survive_a_runner_loop())


async def _test_anthropic_cache_counts_survive_a_runner_loop() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        events = [
            ("message_start", {"message": {"id": "r1", "model": "claude", "usage": {
                "input_tokens": 4, "cache_read_input_tokens": 900, "cache_creation_input_tokens": 100}}}),
            ("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "done"}}),
            ("content_block_stop", {"index": 0}),
            ("message_delta", {"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}}),
            ("message_stop", {}),
        ]
        body = "".join(f"event: {n}\ndata: {json.dumps(p)}\n\n" for n, p in events).encode()
        return httpx.Response(200, request=request, headers={"content-type": "text/event-stream"}, content=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    model = AnthropicMessagesModel(model="claude", api_key="test-key", client=client)
    try:
        run = Runner.run_streamed(Agent(name="usage", instructions="x", model=model), "hi")
        async for _event in run.stream_events():
            pass
    finally:
        await client.aclose()

    # The SDK's running total keeps only cached_tokens; the per-request response
    # (what the on_llm_end hook reports to the trace) must also carry the writes.
    total = run.context_wrapper.usage
    assert total.input_tokens == 1004
    assert total.input_tokens_details.cached_tokens == 900
    request = run.raw_responses[-1].usage
    assert request.input_tokens_details.cached_tokens == 900
    assert request.input_tokens_details.model_dump().get("cache_write_tokens") == 100


def test_anthropic_stream_error_status_reports_the_api_error() -> None:
    asyncio.run(_test_anthropic_stream_error_status_reports_the_api_error())


async def _test_anthropic_stream_error_status_reports_the_api_error() -> None:
    class Unread(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield json.dumps({"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}).encode()

    async def handler(request: httpx.Request) -> httpx.Response:
        # A real streamed response: the body is not loaded until it is read.
        return httpx.Response(529, request=request, stream=Unread())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    model = AnthropicMessagesModel(model="claude", api_key="test-key", client=client)
    try:
        with pytest.raises(AnthropicAPIError, match="overloaded_error: busy"):
            async for _event in model.stream_response(
                None, "hello", ModelSettings(max_tokens=20), [], None, [], _tracing()
            ):
                pass
    finally:
        await client.aclose()
