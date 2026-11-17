import asyncio
import unittest

import httpx
from agents import ModelRetryAdvice, ModelRetryNormalizedError, RetryDecision, RetryPolicyContext
from agents.run_internal.model_retry import stream_response_with_retry
from openai import APIError, RateLimitError

from minebot.app.config import AppConfigError, provider_registry_from_env
from minebot.app.model_provider import is_retryable_model_error


class AgentConfigTests(unittest.TestCase):
    def test_api_key_responses_provider_defaults_to_the_sdk_kernel(self):
        env = {"MINEBOT_LLM_MODEL": "gpt-6.1-sol", "MINEBOT_LLM_KIND": "openai_responses",
               "MINEBOT_LLM_API_KEY": "provider-key"}
        default = provider_registry_from_env(env)
        self.assertEqual(default.kernel, "sdk")
        asyncio.run(default.aclose())
        provider = provider_registry_from_env({**env, "MINEBOT_AGENT_KERNEL": "responses"})
        self.assertEqual((provider.kernel, provider.responses_transport), ("responses", "http"))
        client, model, settings, _ = provider.responses_connection_config()
        self.assertEqual((client.max_retries, model), (2, "gpt-6.1-sol"))
        self.assertEqual((settings["_context_mode"], settings["_compact_threshold"]), ("summary", 128_000))
        self.assertNotIn("context_management", settings)
        self.assertNotIn("prompt_cache_options", settings)
        self.assertNotIn("extra_body", settings)
        asyncio.run(provider.aclose())
        legacy = provider_registry_from_env({**env, "MINEBOT_AGENT_KERNEL": "sdk"})
        self.assertEqual(legacy.kernel, "sdk")

    def test_api_can_explicitly_enable_supported_native_compaction(self):
        env = {"MINEBOT_LLM_MODEL": "gpt-6.1-sol", "MINEBOT_LLM_KIND": "openai_responses",
               "MINEBOT_LLM_API_KEY": "provider-key", "MINEBOT_RESPONSES_CONTEXT_MODE": "native",
               "MINEBOT_RESPONSES_COMPACT_THRESHOLD": "128000"}
        provider = provider_registry_from_env(env)
        _, _, settings, _ = provider.responses_connection_config()
        self.assertEqual(settings["context_management"], [{"type": "compaction", "compact_threshold": 128_000}])
        asyncio.run(provider.aclose())
        with self.assertRaises(AppConfigError):
            provider_registry_from_env({**env, "MINEBOT_RESPONSES_CONTEXT_MODE": "unknown"})

    def test_provider_config_prefers_minebot_env(self):
        provider = provider_registry_from_env(
            {
                "MINEBOT_LLM_MODEL": "minebot-model",
                "MINEBOT_LLM_API_KEY": "minebot-key",
                "MINEBOT_LLM_BASE_URL": "https://minebot.example/v1",
                "OPENAI_MODEL": "openai-model",
                "OPENAI_API_KEY": "openai-key",
                "OPENAI_BASE_URL": "https://openai.example/v1",
            }
        )

        cfg = provider._configs["primary"]
        self.assertEqual(cfg.model, "minebot-model")
        self.assertEqual(cfg.api_key_env, "MINEBOT_LLM_API_KEY")
        self.assertEqual(cfg.base_url, "https://minebot.example/v1")

    def test_provider_config_accepts_openai_compatible_env(self):
        provider = provider_registry_from_env(
            {
                "OPENAI_MODEL": "glm-5.2",
                "OPENAI_API_KEY": "openai-compatible-key",
                "OPENAI_BASE_URL": "https://maas-openapi.example/api/v1",
            }
        )

        cfg = provider._configs["primary"]
        self.assertEqual(cfg.model, "glm-5.2")
        self.assertEqual(cfg.kind, "openai_chat")
        self.assertEqual(cfg.api_key_env, "OPENAI_API_KEY")
        self.assertEqual(cfg.base_url, "https://maas-openapi.example/api/v1")

    def test_provider_trace_configs_are_public_and_sanitized(self):
        provider = provider_registry_from_env(
            {
                "MINEBOT_LLM_MODEL": "glm-5.2",
                "MINEBOT_LLM_API_KEY_ENV": "ANTHROPIC_AUTH_TOKEN",
                "ANTHROPIC_AUTH_TOKEN": "secret-token-value",
                "MINEBOT_LLM_BASE_URL": "https://maas-openapi.wanjiedata.com/api/v1/chat/completions",
            }
        )

        rows = provider.trace_configs()

        self.assertEqual(rows[0]["name"], "primary")
        self.assertEqual(rows[0]["kind"], "openai_chat")
        self.assertEqual(rows[0]["model"], "glm-5.2")
        self.assertEqual(rows[0]["base_url_host"], "https://maas-openapi.wanjiedata.com")
        self.assertEqual(rows[0]["api_key_env"], "ANTHROPIC_AUTH_TOKEN")
        self.assertEqual(rows[0]["model_retry"]["max_retries"], 2)
        self.assertEqual(rows[0]["model_retry"]["client_max_retries"], 0)
        self.assertEqual(rows[0]["model_retry"]["request_timeout_s"], 120.0)
        self.assertNotIn("secret-token-value", repr(rows))

    def test_provider_configures_one_retry_layer_and_bounded_request_timeout(self):
        provider = provider_registry_from_env(
            {
                "MINEBOT_LLM_MODEL": "gpt-5.6-luna",
                "MINEBOT_LLM_KIND": "openai_responses",
                "MINEBOT_LLM_API_KEY": "provider-key",
                "MINEBOT_LLM_REQUEST_TIMEOUT_S": "45",
            }
        )

        provider.resolve("primary")
        client = provider._clients[0]
        self.assertEqual(client.max_retries, 0)
        self.assertEqual((client.timeout.connect, client.timeout.read,
                          client.timeout.write, client.timeout.pool), (10.0, 45.0, 30.0, 10.0))
        asyncio.run(provider.aclose())

    def test_provider_rejects_invalid_request_timeout(self):
        for value in ("soon", "9", "601"):
            with self.subTest(value=value), self.assertRaises(AppConfigError):
                provider_registry_from_env(
                    {
                        "MINEBOT_LLM_MODEL": "gpt-5.6-luna",
                        "MINEBOT_LLM_API_KEY": "provider-key",
                        "MINEBOT_LLM_REQUEST_TIMEOUT_S": value,
                    }
                )

    def test_provider_retries_explicit_overload_but_honours_unsafe_veto(self):
        provider = provider_registry_from_env(
            {
                "MINEBOT_LLM_MODEL": "gpt-5.6-luna",
                "MINEBOT_LLM_KIND": "openai_responses",
                "MINEBOT_LLM_API_KEY": "provider-key",
            }
        )
        retry = provider.model_settings_for("primary").retry
        self.assertIsNotNone(retry)
        self.assertEqual(retry.max_retries, 2)
        self.assertIsNotNone(retry.policy)

        normalized = ModelRetryNormalizedError(
            message="Our servers are currently overloaded. Please try again later."
        )

        async def decide(provider_advice=None):
            decision = await retry.policy(
                RetryPolicyContext(
                    error=RuntimeError(normalized.message),
                    attempt=1,
                    max_retries=retry.max_retries,
                    stream=True,
                    normalized=normalized,
                    provider_advice=provider_advice,
                )
            )
            return decision if isinstance(decision, RetryDecision) else RetryDecision(bool(decision))

        self.assertTrue(asyncio.run(decide()).retry)
        veto = ModelRetryAdvice(suggested=False, replay_safety="unsafe", reason="ambiguous replay")
        self.assertFalse(asyncio.run(decide(veto)).retry)

    def test_stream_retry_replays_overload_only_before_unsafe_output(self):
        provider = provider_registry_from_env(
            {
                "MINEBOT_LLM_MODEL": "gpt-5.6-luna",
                "MINEBOT_LLM_KIND": "openai_responses",
                "MINEBOT_LLM_API_KEY": "provider-key",
            }
        )
        retry = provider.model_settings_for("primary").retry
        attempts = 0
        rewinds = 0

        def get_stream():
            async def stream():
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise _overload_error()
                yield {"type": "response.created"}

            return stream()

        async def rewind():
            nonlocal rewinds
            rewinds += 1

        async def run():
            return [
                event
                async for event in stream_response_with_retry(
                    get_stream=get_stream,
                    rewind=rewind,
                    retry_settings=retry,
                    get_retry_advice=lambda _request: None,
                    previous_response_id=None,
                    conversation_id=None,
                )
            ]

        self.assertEqual(asyncio.run(run()), [{"type": "response.created"}])
        self.assertEqual(attempts, 2)
        self.assertEqual(rewinds, 1)

    def test_stream_retry_honours_provider_retry_directive(self):
        provider = provider_registry_from_env(
            {
                "MINEBOT_LLM_MODEL": "gpt-5.6-luna",
                "MINEBOT_LLM_KIND": "openai_responses",
                "MINEBOT_LLM_API_KEY": "provider-key",
            }
        )
        retry = provider.model_settings_for("primary").retry
        attempts = 0

        def get_stream():
            async def stream():
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise _provider_retry_directive_error()
                yield {"type": "response.created"}

            return stream()

        async def run():
            return [
                event
                async for event in stream_response_with_retry(
                    get_stream=get_stream,
                    rewind=lambda: _noop(),
                    retry_settings=retry,
                    get_retry_advice=lambda _request: None,
                    previous_response_id=None,
                    conversation_id=None,
                )
            ]

        self.assertEqual(asyncio.run(run()), [{"type": "response.created"}])
        self.assertEqual(attempts, 2)

    def test_stream_retry_does_not_replay_after_model_output(self):
        provider = provider_registry_from_env(
            {
                "MINEBOT_LLM_MODEL": "gpt-5.6-luna",
                "MINEBOT_LLM_KIND": "openai_responses",
                "MINEBOT_LLM_API_KEY": "provider-key",
            }
        )
        retry = provider.model_settings_for("primary").retry
        attempts = 0
        rewinds = 0

        def get_stream():
            async def stream():
                nonlocal attempts
                attempts += 1
                yield {"type": "response.output_text.delta"}
                raise _overload_error()

            return stream()

        async def rewind():
            nonlocal rewinds
            rewinds += 1

        async def run():
            async for _event in stream_response_with_retry(
                get_stream=get_stream,
                rewind=rewind,
                retry_settings=retry,
                get_retry_advice=lambda _request: None,
                previous_response_id=None,
                conversation_id=None,
            ):
                pass

        with self.assertRaises(APIError):
            asyncio.run(run())
        self.assertEqual(attempts, 1)
        self.assertEqual(rewinds, 0)

    def test_stream_retry_does_not_replay_insufficient_quota(self):
        provider = provider_registry_from_env(
            {
                "MINEBOT_LLM_MODEL": "gpt-5.6-luna",
                "MINEBOT_LLM_KIND": "openai_responses",
                "MINEBOT_LLM_API_KEY": "provider-key",
            }
        )
        model = provider.resolve("primary")
        retry = provider.model_settings_for("primary").retry
        attempts = 0
        rewinds = 0

        def get_stream():
            async def stream():
                nonlocal attempts
                attempts += 1
                raise _rate_limit_error("insufficient_quota")
                yield

            return stream()

        async def rewind():
            nonlocal rewinds
            rewinds += 1

        async def run():
            async for _event in stream_response_with_retry(
                get_stream=get_stream,
                rewind=rewind,
                retry_settings=retry,
                get_retry_advice=model.get_retry_advice,
                previous_response_id=None,
                conversation_id=None,
            ):
                pass

        try:
            with self.assertRaises(RateLimitError):
                asyncio.run(run())
            self.assertEqual(attempts, 1)
            self.assertEqual(rewinds, 0)
            self.assertFalse(is_retryable_model_error(_rate_limit_error("insufficient_quota")))
        finally:
            asyncio.run(provider.aclose())

    def test_typed_rate_limit_and_retry_after_remain_retryable(self):
        provider = provider_registry_from_env(
            {
                "MINEBOT_LLM_MODEL": "gpt-5.6-luna",
                "MINEBOT_LLM_KIND": "openai_responses",
                "MINEBOT_LLM_API_KEY": "provider-key",
            }
        )
        model = provider.resolve("primary")
        retry = provider.model_settings_for("primary").retry
        attempts = 0

        def get_stream():
            async def stream():
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise _rate_limit_error("rate_limit_exceeded", retry_after="0")
                yield {"type": "response.created"}

            return stream()

        async def run():
            return [
                event
                async for event in stream_response_with_retry(
                    get_stream=get_stream,
                    rewind=lambda: _noop(),
                    retry_settings=retry,
                    get_retry_advice=model.get_retry_advice,
                    previous_response_id=None,
                    conversation_id=None,
                )
            ]

        try:
            self.assertEqual(asyncio.run(run()), [{"type": "response.created"}])
            self.assertEqual(attempts, 2)
            self.assertTrue(
                is_retryable_model_error(
                    _rate_limit_error("rate_limit_exceeded", retry_after="0")
                )
            )
        finally:
            asyncio.run(provider.aclose())

    def test_provider_config_maps_responses_reasoning_effort(self):
        provider = provider_registry_from_env(
            {
                "MINEBOT_LLM_MODEL": "gpt-5.6-luna",
                "MINEBOT_LLM_KIND": "openai_responses",
                "MINEBOT_LLM_API_KEY": "provider-key",
                "MINEBOT_LLM_BASE_URL": "https://provider.example/v1",
                "MINEBOT_LLM_REASONING_EFFORT": "xhigh",
            }
        )

        cfg = provider._configs["primary"]
        settings = provider.model_settings_for("primary")
        self.assertEqual(cfg.kind, "openai_responses")
        self.assertIsNotNone(settings.reasoning)
        self.assertEqual(settings.reasoning.effort, "xhigh")

    def test_provider_config_rejects_unknown_reasoning_effort(self):
        with self.assertRaises(AppConfigError) as ctx:
            provider_registry_from_env(
                {
                    "MINEBOT_LLM_MODEL": "gpt-5.6-luna",
                    "MINEBOT_LLM_API_KEY": "provider-key",
                    "MINEBOT_LLM_REASONING_EFFORT": "maximum",
                }
            )

        self.assertIn("MINEBOT_LLM_REASONING_EFFORT must be one of", str(ctx.exception))

    def test_provider_config_error_names_missing_env_without_value(self):
        with self.assertRaises(AppConfigError) as ctx:
            provider_registry_from_env({"OPENAI_MODEL": "glm-5.2"})

        self.assertEqual(str(ctx.exception), "OPENAI_API_KEY is unset or empty")


def _overload_error() -> APIError:
    return APIError(
        "Our servers are currently overloaded. Please try again later.",
        request=httpx.Request("POST", "https://provider.example/v1/responses"),
        body=None,
    )


def _provider_retry_directive_error() -> APIError:
    return APIError(
        "An error occurred while processing your request. You can retry your request.",
        request=httpx.Request("POST", "https://provider.example/v1/responses"),
        body=None,
    )


def _rate_limit_error(code: str, *, retry_after: str | None = None) -> RateLimitError:
    body = {
        "error": {
            "message": (
                "You exceeded your current quota"
                if code == "insufficient_quota"
                else "Rate limit reached"
            ),
            "type": code,
            "code": code,
        }
    }
    headers = {} if retry_after is None else {"retry-after": retry_after}
    response = httpx.Response(
        429,
        request=httpx.Request("POST", "https://provider.example/v1/responses"),
        headers=headers,
        json=body,
    )
    return RateLimitError("Error code: 429", response=response, body=body)


async def _noop() -> None:
    return None


if __name__ == "__main__":
    unittest.main()
