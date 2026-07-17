import unittest

from minebot.app.config import AppConfigError, provider_registry_from_env


class AgentConfigTests(unittest.TestCase):
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
        self.assertNotIn("secret-token-value", repr(rows))

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


if __name__ == "__main__":
    unittest.main()
