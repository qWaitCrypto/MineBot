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
DEFAULT_ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"
DEFAULT_AGENT_LANGUAGE = "English"
REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}


class AppConfigError(RuntimeError):
    """Configuration is incomplete for a real model-backed run."""


def provider_registry_from_env(env: Mapping[str, str] | None = None) -> ModelProviderRegistry:
    env = os.environ if env is None else env
    auth = env.get("MINEBOT_LLM_AUTH", "api_key")
    if auth not in {"api_key", "chatgpt"}:
        raise AppConfigError("MINEBOT_LLM_AUTH must be api_key or chatgpt")
    model = env.get("MINEBOT_LLM_MODEL") or env.get("OPENAI_MODEL")
    if not model:
        raise AppConfigError("MINEBOT_LLM_MODEL or OPENAI_MODEL is unset")

    default_kernel = "responses" if auth == "chatgpt" or env.get("MINEBOT_LLM_KIND") == "openai_responses" else "sdk"
    kernel = env.get("MINEBOT_AGENT_KERNEL", default_kernel)
    if kernel not in {"sdk", "responses"}:
        raise AppConfigError("MINEBOT_AGENT_KERNEL must be responses or sdk")
    kind = env.get("MINEBOT_LLM_KIND", "openai_responses" if kernel == "responses" else "openai_chat")
    if kind not in {"openai_chat", "openai_responses", "anthropic_messages", "litellm"}:
        raise AppConfigError(
            "MINEBOT_LLM_KIND must be openai_chat, openai_responses, anthropic_messages, or litellm"
        )
    if kernel == "responses" and kind != "openai_responses":
        raise AppConfigError("The Responses kernel requires MINEBOT_LLM_KIND=openai_responses")

    api_key_env = env.get("MINEBOT_LLM_API_KEY_ENV")
    if not api_key_env:
        if env.get(DEFAULT_API_KEY_ENV):
            api_key_env = DEFAULT_API_KEY_ENV
        elif kind == "anthropic_messages" and env.get(DEFAULT_ANTHROPIC_API_KEY_ENV):
            api_key_env = DEFAULT_ANTHROPIC_API_KEY_ENV
        else:
            api_key_env = DEFAULT_OPENAI_API_KEY_ENV
    if auth == "api_key" and not env.get(api_key_env):
        raise AppConfigError(f"{api_key_env} is unset or empty")

    # Keep the two provider families independent: a process-wide
    # OPENAI_BASE_URL must not silently redirect Anthropic Messages traffic.
    base_url = env.get("MINEBOT_LLM_BASE_URL") or (
        None if kind == "anthropic_messages" else env.get("OPENAI_BASE_URL")
    ) or None
    if auth == "chatgpt":
        if kernel != "responses" or kind != "openai_responses":
            raise AppConfigError("ChatGPT login requires the Responses kernel and openai_responses")
        if base_url and base_url.rstrip("/") != "https://api.openai.com/v1":
            raise AppConfigError("ChatGPT login requires the official base URL; unset custom provider URLs")
        base_url, api_key_env = "https://api.openai.com/v1", None
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
            auth=auth,
        ),
        ProviderConfig(
            name="fast",
            kind=kind,
            model=fast_model,
            base_url=base_url,
            api_key_env=api_key_env,
            settings=settings,
            auth=auth,
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
    if env.get("MINEBOT_RESPONSES_COMPACT_THRESHOLD"):
        try:
            threshold = int(env["MINEBOT_RESPONSES_COMPACT_THRESHOLD"])
        except ValueError:
            raise AppConfigError("MINEBOT_RESPONSES_COMPACT_THRESHOLD must be an integer") from None
        if threshold < 0:
            raise AppConfigError("MINEBOT_RESPONSES_COMPACT_THRESHOLD must be nonnegative (0 disables)")
        settings["compact_threshold"] = threshold
    if env.get("MINEBOT_RESPONSES_CONTEXT_MODE"):
        mode = env["MINEBOT_RESPONSES_CONTEXT_MODE"].strip().lower()
        if mode not in {"native", "summary"}:
            raise AppConfigError("MINEBOT_RESPONSES_CONTEXT_MODE must be native or summary")
        settings["context_mode"] = mode
    if env.get("MINEBOT_LLM_PROMPT_CACHE"):
        value = env["MINEBOT_LLM_PROMPT_CACHE"].strip().lower()
        if value not in {"1", "true", "yes", "0", "false", "no"}:
            raise AppConfigError("MINEBOT_LLM_PROMPT_CACHE must be a boolean")
        settings["prompt_cache"] = value in {"1", "true", "yes"}
    if env.get("MINEBOT_LLM_PROMPT_CACHE_TTL"):
        ttl = env["MINEBOT_LLM_PROMPT_CACHE_TTL"].strip().lower()
        if ttl not in {"5m", "1h"}:
            raise AppConfigError("MINEBOT_LLM_PROMPT_CACHE_TTL must be 5m or 1h")
        settings["prompt_cache_ttl"] = ttl
    return settings


__all__ = [
    "AppConfigError",
    "DEFAULT_AGENT_LANGUAGE",
    "DEFAULT_API_KEY_ENV",
    "DEFAULT_OPENAI_API_KEY_ENV",
    "DEFAULT_ANTHROPIC_API_KEY_ENV",
    "agent_language_from_env",
    "provider_registry_from_env",
]
