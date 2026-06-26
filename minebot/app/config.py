"""Environment-backed application config for real agent runs.

This module is deliberately small: it translates env vars into provider-blind
configs and never logs or stores credential values.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from minebot.app.model_provider import ModelProviderRegistry
from minebot.brain.provider import ProviderConfig


DEFAULT_API_KEY_ENV = "MINEBOT_LLM_API_KEY"


class AppConfigError(RuntimeError):
    """Configuration is incomplete for a real model-backed run."""


def provider_registry_from_env(env: Mapping[str, str] | None = None) -> ModelProviderRegistry:
    env = os.environ if env is None else env
    model = env.get("MINEBOT_LLM_MODEL")
    if not model:
        raise AppConfigError("MINEBOT_LLM_MODEL is unset")

    api_key_env = env.get("MINEBOT_LLM_API_KEY_ENV", DEFAULT_API_KEY_ENV)
    if not env.get(api_key_env):
        raise AppConfigError(f"{api_key_env} is unset or empty")

    kind = env.get("MINEBOT_LLM_KIND", "openai_chat")
    if kind not in {"openai_chat", "openai_responses"}:
        raise AppConfigError("MINEBOT_LLM_KIND must be openai_chat or openai_responses")

    base_url = env.get("MINEBOT_LLM_BASE_URL") or None
    fast_model = env.get("MINEBOT_LLM_FAST_MODEL", model)
    settings = _settings_from_env(env)
    configs = [
        ProviderConfig(
            name="primary",
            kind=kind,
            model=model,
            base_url=base_url,
            api_key_env=api_key_env,
            settings=settings,
        ),
        ProviderConfig(
            name="fast",
            kind=kind,
            model=fast_model,
            base_url=base_url,
            api_key_env=api_key_env,
            settings=settings,
        ),
    ]
    return ModelProviderRegistry(configs, default="primary")


def _settings_from_env(env: Mapping[str, str]) -> dict[str, object]:
    settings: dict[str, object] = {}
    if env.get("MINEBOT_LLM_TEMPERATURE"):
        settings["temperature"] = float(env["MINEBOT_LLM_TEMPERATURE"])
    if env.get("MINEBOT_LLM_MAX_TOKENS"):
        settings["max_tokens"] = int(env["MINEBOT_LLM_MAX_TOKENS"])
    if env.get("MINEBOT_LLM_PARALLEL_TOOL_CALLS"):
        settings["parallel_tool_calls"] = env["MINEBOT_LLM_PARALLEL_TOOL_CALLS"].lower() in {"1", "true", "yes"}
    return settings


__all__ = ["AppConfigError", "DEFAULT_API_KEY_ENV", "provider_registry_from_env"]
