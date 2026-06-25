"""Model Provider seam — the binding ring's first and most important job.

Resolves a provider-blind :class:`ProviderConfig` (a logical name pointing at a
vendor endpoint) into a concrete openai-agents ``Model``. This is the single
translation point between MineBot's vendor-blind core and a specific LLM
provider: swapping the vendor behind ``"primary"`` is a config change with zero
core change.

See ``docs/design-docs/agent-layer-architecture.md`` §6.1. The pure-data half
(`ProviderConfig` / `Capabilities`) lives in
:mod:`minebot.brain.provider` and is re-exported here for ergonomics.

Only this module and ``app/runner.py`` may import openai-agents.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import Any

from agents import (
    Model,
    ModelProvider,
    ModelSettings,
    OpenAIChatCompletionsModel,
    OpenAIResponsesModel,
    set_tracing_disabled,
)
from openai import AsyncOpenAI

from minebot.brain.provider import ApiShape, Capabilities, ProviderConfig, ProviderKind

__all__ = [
    "ApiShape",
    "Capabilities",
    "ProviderConfig",
    "ProviderKind",
    "ModelProviderRegistry",
    "ProviderConfigError",
]

# OpenAI-compatible local servers (vLLM, llama.cpp, …) reject an empty key but
# accept any non-empty placeholder. Used only when base_url is set and no
# api_key_env is configured.
_DUMMY_LOCAL_KEY = "EMPTY"

# Portable subset of ProviderConfig.settings forwarded into ModelSettings. The
# core depends on no provider superpowers, so only this allowlist is plumbed.
_SETTINGS_ALLOWLIST = (
    "temperature",
    "top_p",
    "max_tokens",
    "frequency_penalty",
    "presence_penalty",
    "tool_choice",
    "parallel_tool_calls",
)


class ProviderConfigError(RuntimeError):
    """Invalid or unresolvable provider configuration.

    Messages reference the env-var **name** only and never a credential value,
    honouring the "no keys/tokens in output or logs" constraint.
    """


class ModelProviderRegistry(ModelProvider):
    """openai-agents ``ModelProvider`` backed by provider-blind configs.

    The SDK calls :meth:`get_model` with ``model_name`` taken from an Agent's
    ``model=`` field — that value IS our logical name ("primary"/"fast"/
    "judge"), so logical names flow straight through. Resolution is cached per
    logical name; one ``AsyncOpenAI`` client is created per resolved provider.
    """

    def __init__(
        self,
        configs: Sequence[ProviderConfig] | Mapping[str, ProviderConfig],
        *,
        default: str | None = None,
        env: Mapping[str, str] | None = None,
        install_tracing_policy: bool = True,
    ) -> None:
        items = list(configs.values()) if isinstance(configs, Mapping) else list(configs)
        if not items:
            raise ProviderConfigError(
                "ModelProviderRegistry requires at least one ProviderConfig"
            )
        self._configs: dict[str, ProviderConfig] = {}
        for cfg in items:
            if cfg.name in self._configs:
                raise ProviderConfigError(f"duplicate provider logical name: {cfg.name!r}")
            self._configs[cfg.name] = cfg
        self._default = default or items[0].name
        if self._default not in self._configs:
            raise ProviderConfigError(f"default provider {self._default!r} is not configured")
        self._env: Mapping[str, str] = os.environ if env is None else env
        self._models: dict[str, Model] = {}
        self._clients: list[AsyncOpenAI] = []
        if install_tracing_policy:
            self.install_tracing_policy()

    # -- SDK ModelProvider contract -------------------------------------------

    def get_model(self, model_name: str | None) -> Model:
        return self.resolve(model_name or self._default)

    # -- public seam API ------------------------------------------------------

    def resolve(self, logical_name: str) -> Model:
        """Logical name -> concrete openai-agents ``Model`` (cached)."""
        if logical_name not in self._models:
            self._models[logical_name] = self._build(self._require(logical_name))
        return self._models[logical_name]

    def capabilities_for(self, logical_name: str) -> Capabilities:
        return self._require(logical_name).capabilities

    def model_settings_for(self, logical_name: str) -> ModelSettings:
        return _model_settings(self._require(logical_name))

    def logical_names(self) -> list[str]:
        return list(self._configs)

    @property
    def default(self) -> str:
        return self._default

    def install_tracing_policy(self) -> None:
        """Tracing OFF by default.

        SDK tracing exports to OpenAI's servers using an OpenAI key. MineBot may
        have no such key and must not phone home, so tracing is disabled unless a
        provider explicitly opts in via ``ProviderConfig.tracing``.
        """
        any_tracing = any(cfg.tracing for cfg in self._configs.values())
        set_tracing_disabled(not any_tracing)

    async def aclose(self) -> None:
        for client in self._clients:
            await client.close()
        self._clients.clear()

    # -- internals ------------------------------------------------------------

    def _require(self, logical_name: str) -> ProviderConfig:
        try:
            return self._configs[logical_name]
        except KeyError:
            known = ", ".join(sorted(self._configs)) or "<none>"
            raise ProviderConfigError(
                f"unknown logical model {logical_name!r}; configured: {known}"
            ) from None

    def _build(self, cfg: ProviderConfig) -> Model:
        if cfg.kind == "openai_chat":
            return OpenAIChatCompletionsModel(model=cfg.model, openai_client=self._client(cfg))
        if cfg.kind == "openai_responses":
            return OpenAIResponsesModel(model=cfg.model, openai_client=self._client(cfg))
        if cfg.kind == "litellm":
            return self._build_litellm(cfg)
        raise ProviderConfigError(
            f"provider {cfg.name!r}: kind={cfg.kind!r} is a named slot with no resolver yet"
        )

    def _client(self, cfg: ProviderConfig) -> AsyncOpenAI:
        client = AsyncOpenAI(api_key=self._api_key(cfg), base_url=cfg.base_url)
        self._clients.append(client)
        return client

    def _api_key(self, cfg: ProviderConfig) -> str:
        if cfg.api_key_env:
            value = self._env.get(cfg.api_key_env)
            if not value:
                raise ProviderConfigError(
                    f"provider {cfg.name!r}: env var {cfg.api_key_env!r} is unset or empty"
                )
            return value
        # No key configured: only sane for a local OpenAI-compatible endpoint,
        # which still wants a non-empty placeholder.
        if cfg.base_url:
            return _DUMMY_LOCAL_KEY
        raise ProviderConfigError(
            f"provider {cfg.name!r}: no api_key_env and no base_url; cannot authenticate"
        )

    def _build_litellm(self, cfg: ProviderConfig) -> Model:
        try:
            from agents.extensions.models.litellm_model import LitellmModel
        except ImportError as exc:
            raise ProviderConfigError(
                f"provider {cfg.name!r}: kind=litellm needs the optional dependency; "
                "install with: pip install 'openai-agents[litellm]'"
            ) from exc
        api_key = self._env.get(cfg.api_key_env) if cfg.api_key_env else None
        if cfg.api_key_env and not api_key:
            raise ProviderConfigError(
                f"provider {cfg.name!r}: env var {cfg.api_key_env!r} is unset or empty"
            )
        return LitellmModel(model=cfg.model, base_url=cfg.base_url, api_key=api_key)


def _model_settings(cfg: ProviderConfig) -> ModelSettings:
    """Translate the portable subset of ``ProviderConfig.settings`` into
    ``ModelSettings``.

    Only the explicit allowlist is forwarded; ``include_usage`` rides here so the
    usage-metric quirk stays contained in the seam.
    """
    kwargs: dict[str, Any] = {
        key: cfg.settings[key] for key in _SETTINGS_ALLOWLIST if key in cfg.settings
    }
    if cfg.include_usage:
        kwargs["include_usage"] = True
    for passthrough in ("extra_body", "extra_headers"):
        if cfg.settings.get(passthrough) is not None:
            kwargs[passthrough] = cfg.settings[passthrough]
    return ModelSettings(**kwargs)
