"""Environment-backed application config for real agent runs.

This module is deliberately small: it translates env vars into provider-blind
configs and never logs or stores credential values.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from minebot.app.model_provider import ModelProviderRegistry
from minebot.app.model_provider import (
    MAX_MODEL_REQUEST_TIMEOUT_S,
    MIN_MODEL_REQUEST_TIMEOUT_S,
)
from minebot.brain.provider import ProviderConfig


DEFAULT_API_KEY_ENV = "MINEBOT_LLM_API_KEY"
DEFAULT_OPENAI_API_KEY_ENV = "OPENAI_API_KEY"
DEFAULT_AGENT_LANGUAGE = "English"
REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}


class AppConfigError(RuntimeError):
    """Configuration is incomplete for a real model-backed run."""


def provider_registry_from_env(env: Mapping[str, str] | None = None) -> ModelProviderRegistry:
    env = os.environ if env is None else env
    model = env.get("MINEBOT_LLM_MODEL") or env.get("OPENAI_MODEL")
    if not model:
        raise AppConfigError("MINEBOT_LLM_MODEL or OPENAI_MODEL is unset")

    api_key_env = env.get("MINEBOT_LLM_API_KEY_ENV")
    if not api_key_env:
        api_key_env = DEFAULT_API_KEY_ENV if env.get(DEFAULT_API_KEY_ENV) else DEFAULT_OPENAI_API_KEY_ENV
    if not env.get(api_key_env):
        raise AppConfigError(f"{api_key_env} is unset or empty")

    default_kernel = "responses" if env.get("MINEBOT_LLM_KIND") == "openai_responses" else "sdk"
    kernel = env.get("MINEBOT_AGENT_KERNEL", default_kernel)
    if kernel not in {"sdk", "responses"}:
        raise AppConfigError("MINEBOT_AGENT_KERNEL must be responses or sdk")
    kind = env.get("MINEBOT_LLM_KIND", "openai_responses" if kernel == "responses" else "openai_chat")
    if kind not in {"openai_chat", "openai_responses", "litellm"}:
        raise AppConfigError("MINEBOT_LLM_KIND must be openai_chat, openai_responses, or litellm")

    base_url = env.get("MINEBOT_LLM_BASE_URL") or env.get("OPENAI_BASE_URL") or None
    fast_model = env.get("MINEBOT_LLM_FAST_MODEL") or env.get("OPENAI_FAST_MODEL") or model
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
    return ModelProviderRegistry(configs, default="primary", env=env, kernel=kernel)


def agent_language_from_env(
    env: Mapping[str, str] | None = None,
    *,
    default: str = DEFAULT_AGENT_LANGUAGE,
) -> str:
    env = os.environ if env is None else env
    language = env.get("MINEBOT_AGENT_LANGUAGE") or default
    return language.strip() or default


def _settings_from_env(env: Mapping[str, str]) -> dict[str, object]:
    settings: dict[str, object] = {}
    if env.get("MINEBOT_LLM_TEMPERATURE"):
        settings["temperature"] = float(env["MINEBOT_LLM_TEMPERATURE"])
    if env.get("MINEBOT_LLM_MAX_TOKENS"):
        settings["max_tokens"] = int(env["MINEBOT_LLM_MAX_TOKENS"])
    if env.get("MINEBOT_LLM_PARALLEL_TOOL_CALLS"):
        settings["parallel_tool_calls"] = env["MINEBOT_LLM_PARALLEL_TOOL_CALLS"].lower() in {"1", "true", "yes"}
    if env.get("MINEBOT_LLM_REASONING_EFFORT"):
        effort = env["MINEBOT_LLM_REASONING_EFFORT"].strip().lower()
        if effort not in REASONING_EFFORTS:
            allowed = ", ".join(sorted(REASONING_EFFORTS))
            raise AppConfigError(
                f"MINEBOT_LLM_REASONING_EFFORT must be one of: {allowed}"
            )
        settings["reasoning"] = {"effort": effort}
    if env.get("MINEBOT_LLM_REQUEST_TIMEOUT_S"):
        raw_timeout = env["MINEBOT_LLM_REQUEST_TIMEOUT_S"]
        try:
            request_timeout_s = float(raw_timeout)
        except ValueError as exc:
            raise AppConfigError("MINEBOT_LLM_REQUEST_TIMEOUT_S must be numeric") from exc
        if not MIN_MODEL_REQUEST_TIMEOUT_S <= request_timeout_s <= MAX_MODEL_REQUEST_TIMEOUT_S:
            raise AppConfigError(
                "MINEBOT_LLM_REQUEST_TIMEOUT_S must be between "
                f"{MIN_MODEL_REQUEST_TIMEOUT_S:g} and {MAX_MODEL_REQUEST_TIMEOUT_S:g} seconds"
            )
        settings["request_timeout_s"] = request_timeout_s
    return settings


__all__ = [
    "AppConfigError",
    "DEFAULT_AGENT_LANGUAGE",
    "DEFAULT_API_KEY_ENV",
    "DEFAULT_OPENAI_API_KEY_ENV",
    "agent_language_from_env",
    "provider_registry_from_env",
]
