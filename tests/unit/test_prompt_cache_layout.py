"""Prompt-cache layout: usage reporting reaches the trace and the live panel."""

from __future__ import annotations

import asyncio
import json
import unittest

import httpx
from agents.model_settings import ModelSettings
from agents.models.interface import ModelTracing
from agents.usage import Usage
from openai import AsyncOpenAI
from openai.types.completion_usage import CompletionUsage
from openai.types.responses.response_usage import InputTokensDetails

from minebot.app.chat_usage import CacheUsageChatCompletionsModel, normalize_chat_usage
from minebot.app.prompt_cache import current_context_item, is_current_context_item
from minebot.app.runner import model_usage_observation
from minebot.panel.model import fold


class CurrentContextItemTests(unittest.TestCase):
    def test_item_is_a_user_message_with_the_shared_header(self) -> None:
        item = current_context_item("GOAL: mine", "", "STATE: pos=(1, 2, 3)")
        self.assertEqual(item, {"role": "user", "content": "CURRENT_CONTEXT:\nGOAL: mine\n\nSTATE: pos=(1, 2, 3)"})
        self.assertTrue(is_current_context_item(item))

    def test_nothing_live_means_no_item(self) -> None:
        self.assertIsNone(current_context_item("", ""))
        self.assertFalse(is_current_context_item({"role": "user", "content": "CURRENT_CONTEXT"}))
        self.assertFalse(is_current_context_item({"role": "assistant", "content": "CURRENT_CONTEXT:\nx"}))

    def test_notable_entities_refresh_only_the_trailing_context_item(self) -> None:
        from agents.run_config import CallModelData, ModelInputData

        from minebot.app.runner import AgentRuntime, RuntimeRunContext
        from minebot.brain.context import AgentContext
        from minebot.brain.lifecycle import LifecycleController, LifecycleState
        from minebot.brain.modes import ModeRuntime
        from minebot.brain.progress import ProgressAuthority
        from minebot.brain.registry import ToolRegistry
        from minebot.game.real_client_body import RealClientBody
        from tests.unit.test_real_client_body import FakeClientIpc

        class Ipc(FakeClientIpc):
            health = 200
            reads = 0

            def state(self):
                self.reads += 1
                return {**super().state(), "entities": [
                    {"type": "ender_dragon", "boss": True, "health": self.health,
                     "max_health": 200, "phase": "circling", "dist2": 48 ** 2},
                    {"type": "end_crystal", "caged": True, "dist2": 31 ** 2},
                ] if self.health else []}

        ipc = Ipc()
        runtime = AgentRuntime(
            body=RealClientBody("Bot", ipc), registry=ToolRegistry(),
            agent_context=AgentContext(system_prompt="sys", goal_text="fight"),
            lifecycle=LifecycleController(), mode_runtime=ModeRuntime(), authority=ProgressAuthority(),
        )
        self.addCleanup(runtime.close)
        context = RuntimeRunContext(
            agent_context=runtime.agent_context, weld_context=runtime.weld_context,
            profile=runtime.mode_runtime.profile_for(LifecycleState.ACTIVE), runtime=runtime,
        )
        wrapper = type("Wrapper", (), {"context": context})()
        instructions = runtime._instructions(wrapper, runtime.agent)
        history = [{"role": "user", "content": "fight"}]
        data = CallModelData(
            model_data=ModelInputData(input=history, instructions=instructions), agent=None, context=context,
        )

        async def requests():
            first = await runtime._bounded_model_input(data)
            ipc.health = 150
            second = await runtime._bounded_model_input(data)
            ipc.health = 0
            third = await runtime._bounded_model_input(data)
            return first, second, third

        reads = ipc.reads
        first, second, third = asyncio.run(requests())
        self.assertEqual(ipc.reads - reads, 3)  # Only the existing per-request state read.
        self.assertEqual(first.input[:-1], history)
        self.assertEqual(first.input[:-1], second.input[:-1])
        self.assertEqual(second.input[:-1], third.input[:-1])
        self.assertEqual(first.instructions, second.instructions)
        self.assertEqual(instructions, runtime._instructions(wrapper, runtime.agent))
        self.assertNotIn("NOTABLE:", instructions)
        self.assertTrue(is_current_context_item(first.input[-1]))
        self.assertIn("ender_dragon health 200/200", first.input[-1]["content"])
        self.assertIn("ender_dragon health 150/200", second.input[-1]["content"])
        self.assertIn("end_crystal ×1 (1 caged) nearest 31 blocks", second.input[-1]["content"])
        self.assertNotIn("NOTABLE:", third.input[-1]["content"])


class ModelUsageObservationTests(unittest.TestCase):
    def test_reports_cache_hit_ratio_and_writes(self) -> None:
        usage = Usage(
            requests=1,
            input_tokens=1000,
            output_tokens=50,
            total_tokens=1050,
            input_tokens_details=InputTokensDetails(cached_tokens=900, cache_write_tokens=60),
        )

        observation = model_usage_observation(usage)

        self.assertEqual(observation["cache_read_ratio"], 0.9)
        self.assertEqual(observation["usage"]["input_tokens"], 1000)
        self.assertEqual(observation["usage"]["input_tokens_details"], {"cached_tokens": 900, "cache_write_tokens": 60})

    def test_empty_or_missing_usage_is_not_reported(self) -> None:
        self.assertIsNone(model_usage_observation(None))
        self.assertIsNone(model_usage_observation(Usage()))

    def test_panel_folds_sdk_kernel_usage(self) -> None:
        observation = model_usage_observation(
            Usage(requests=1, input_tokens=200, output_tokens=10, total_tokens=210,
                  input_tokens_details=InputTokensDetails(cached_tokens=150))
        )
        records = [{"seq": index, "ts": 100.0 + index, "event": "model_usage", **observation} for index in (1, 2)]

        state = fold(records)

        self.assertEqual(state.usage["input"], 400)
        self.assertEqual(state.usage["cached"], 300)
        self.assertEqual(state.usage["output"], 20)
        self.assertEqual(state.cache_ratio, 0.75)
        self.assertEqual(state.context_tokens, 200)
        self.assertNotIn("model_usage", state.unknown)


def _usage(**fields: object) -> CompletionUsage:
    return CompletionUsage.model_validate(
        {"prompt_tokens": 100, "completion_tokens": 5, "total_tokens": 105, **fields}
    )


class ChatCompletionsCacheUsageTests(unittest.TestCase):
    def test_vendor_fields_become_the_standard_cached_tokens(self) -> None:
        for vendor_fields in (
            {"prompt_cache_hit_tokens": 80, "prompt_cache_miss_tokens": 20},  # DeepSeek
            {"cached_tokens": 80},  # Moonshot
            {"cache_read_input_tokens": 80},  # Anthropic-shaped gateways
        ):
            usage = _usage(**vendor_fields)
            normalize_chat_usage(usage)
            self.assertEqual(usage.prompt_tokens_details.cached_tokens, 80, vendor_fields)

    def test_standard_field_wins_and_misses_stay_zero(self) -> None:
        standard = _usage(prompt_tokens_details={"cached_tokens": 60}, prompt_cache_hit_tokens=80)
        normalize_chat_usage(standard)
        self.assertEqual(standard.prompt_tokens_details.cached_tokens, 60)

        cold = _usage(prompt_cache_hit_tokens=0, prompt_cache_miss_tokens=100)
        normalize_chat_usage(cold)
        self.assertIsNone(cold.prompt_tokens_details)
        normalize_chat_usage(None)

    def test_model_reports_deepseek_hits_for_blocking_and_streamed_calls(self) -> None:
        asyncio.run(self._model_reports_deepseek_hits())

    async def _model_reports_deepseek_hits(self) -> None:
        usage = {
            "prompt_tokens": 100, "completion_tokens": 5, "total_tokens": 105,
            "prompt_cache_hit_tokens": 80, "prompt_cache_miss_tokens": 20,
        }
        base = {"id": "c1", "created": 1, "model": "deepseek-chat"}

        async def handler(request: httpx.Request) -> httpx.Response:
            if json.loads((await request.aread()).decode()).get("stream"):
                chunks = [
                    {**base, "object": "chat.completion.chunk",
                     "choices": [{"index": 0, "delta": {"role": "assistant", "content": "ok"}, "finish_reason": None}]},
                    {**base, "object": "chat.completion.chunk",
                     "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": usage},
                ]
                body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
                return httpx.Response(200, request=request, headers={"content-type": "text/event-stream"}, content=body.encode())
            return httpx.Response(200, request=request, json={
                **base, "object": "chat.completion", "usage": usage,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            })

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = AsyncOpenAI(api_key="k", base_url="http://mock.test/v1", http_client=http, max_retries=0)
        # Platform discovery adds a worker-thread hop unrelated to this mock
        # protocol test; avoid relying on a sandbox's cross-thread wakeups.
        client._platform = "Linux"
        model = CacheUsageChatCompletionsModel(model="deepseek-chat", openai_client=client)
        try:
            blocking = await model.get_response("sys", "hi", ModelSettings(), [], None, [], ModelTracing.DISABLED)
            events = [e async for e in model.stream_response("sys", "hi", ModelSettings(), [], None, [], ModelTracing.DISABLED)]
        finally:
            await http.aclose()

        self.assertEqual(blocking.usage.input_tokens, 100)
        self.assertEqual(blocking.usage.input_tokens_details.cached_tokens, 80)
        streamed = events[-1].response.usage
        self.assertEqual(streamed.input_tokens, 100)
        self.assertEqual(streamed.input_tokens_details.cached_tokens, 80)


if __name__ == "__main__":
    unittest.main()
