"""Configured base OpenAI clients and the explicit legacy Agents SDK binding.

The Responses kernel uses responses_connection_config; the old SDK path uses
resolve/model_settings_for. Both reuse private provider configuration without
silently falling back between execution kernels.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlparse

from agents import (
    Model,
    ModelProvider,
    ModelRetryBackoffSettings,
    ModelRetrySettings,
    ModelSettings,
    OpenAIChatCompletionsModel,
    OpenAIResponsesModel,
    RetryDecision,
    RetryPolicyContext,
    retry_policies,
    set_tracing_disabled,
)
from httpx import AsyncClient, TransportError
from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError, AsyncOpenAI

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
    "reasoning",
)

_MODEL_RETRY_MAX_RETRIES = 2
DEFAULT_MODEL_REQUEST_TIMEOUT_S = 120.0
MIN_MODEL_REQUEST_TIMEOUT_S = 10.0
MAX_MODEL_REQUEST_TIMEOUT_S = 600.0
_MODEL_RETRY_HTTP_STATUSES = frozenset({408, 409, 425, *range(500, 600)})
_MODEL_RETRY_ERROR_CODES = frozenset(
    {
        "overloaded",
        "rate_limit_exceeded",
        "server_error",
        "service_unavailable",
        "temporarily_unavailable",
    }
)
_MODEL_RETRY_MESSAGE_FRAGMENTS = (
    "overloaded",
    "server is busy",
    "service unavailable",
    "temporarily unavailable",
    "try again later",
    "you can retry your request",
)
_PERMANENT_MODEL_ERROR_CODES = frozenset(
    {
        "account_deactivated",
        "account_suspended",
        "billing_hard_limit_reached",
        "billing_not_active",
        "credit_balance_too_low",
        "insufficient_balance",
        "insufficient_quota",
        "organization_deactivated",
        "organization_suspended",
        "payment_required",
        "usage_limit_reached",
    }
)
_PERMANENT_MODEL_ERROR_MESSAGE_FRAGMENTS = (
    "account has been deactivated",
    "account is deactivated",
    "account is suspended",
    "billing hard limit",
    "billing is not active",
    "credit balance is too low",
    "exceeded your current quota",
    "insufficient balance",
    "insufficient quota",
    "insufficient_quota",
    "organization has been deactivated",
    "organization is suspended",
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
        kernel: str = "sdk",
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
        if kernel not in {"sdk", "responses"}:
            raise ProviderConfigError("kernel must be sdk or responses")
        self.kernel = kernel
        self.responses_proxy = (os.environ if env is None else env).get("MINEBOT_LLM_PROXY") or None
        self._responses_clients: dict[str, AsyncOpenAI] = {}
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

    def responses_connection_config(self, logical_name: str = "primary") -> tuple[AsyncOpenAI, str, dict[str, Any], float]:
        """Base SDK connection and wire settings for the native Responses loop."""
        cfg = self._require(logical_name)
        if cfg.kind != "openai_responses":
            raise ProviderConfigError("Responses kernel requires MINEBOT_LLM_KIND=openai_responses")
        if logical_name not in self._responses_clients:
            self._responses_clients[logical_name] = self._client(cfg)
        settings = {k: v for k, v in cfg.settings.items()
                    if k in {"temperature", "top_p", "tool_choice", "parallel_tool_calls", "reasoning"}}
        if "max_tokens" in cfg.settings:
            settings["max_output_tokens"] = cfg.settings["max_tokens"]
        return self._responses_clients[logical_name], cfg.model, settings, _request_timeout_s(cfg)

    def capabilities_for(self, logical_name: str) -> Capabilities:
        return self._require(logical_name).capabilities

    def model_settings_for(self, logical_name: str) -> ModelSettings:
        return _model_settings(self._require(logical_name))

    def logical_names(self) -> list[str]:
        return list(self._configs)

    def trace_configs(self) -> list[dict[str, object]]:
        """Public, sanitized provider metadata for session observability.

        This intentionally records credential env-var names, never credential
        values. The base URL is reduced to scheme+host so logs can identify the
        backend family without persisting path/query details.
        """
        rows: list[dict[str, object]] = []
        for name in self.logical_names():
            cfg = self._require(name)
            parsed = urlparse(cfg.base_url) if cfg.base_url else None
            base_url_host = None
            if parsed is not None and parsed.netloc:
                base_url_host = f"{parsed.scheme}://{parsed.netloc}"
            rows.append(
                {
                    "name": cfg.name,
                    "kind": cfg.kind,
                    "model": cfg.model,
                    "api_shape": cfg.api_shape,
                    "base_url_host": base_url_host,
                    "api_key_env": cfg.api_key_env,
                    "capabilities": {
                        "json_schema": cfg.capabilities.json_schema,
                        "multimodal": cfg.capabilities.multimodal,
                        "hosted_tools": cfg.capabilities.hosted_tools,
                        "prompt_caching": cfg.capabilities.prompt_caching,
                    },
                    "include_usage": cfg.include_usage,
                    "tracing": cfg.tracing,
                    "model_retry": {
                        "max_retries": _MODEL_RETRY_MAX_RETRIES,
                        "client_max_retries": 0,
                        "request_timeout_s": _request_timeout_s(cfg),
                        "replay_guard": "sdk_stream_events",
                    },
                }
            )
        return rows

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
        # The agents SDK owns replay-aware retries. Disabling the OpenAI
        # client's hidden retry layer keeps one bounded attempt budget and
        # prevents nested retries from multiplying model wait time.
        client = AsyncOpenAI(
            api_key=self._api_key(cfg),
            base_url=cfg.base_url,
            max_retries=0,
            timeout=_request_timeout_s(cfg),
            http_client=AsyncClient(proxy=self.responses_proxy) if self.responses_proxy else None,
        )
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
    kwargs["retry"] = _model_retry_settings()
    for passthrough in ("extra_body", "extra_headers"):
        if cfg.settings.get(passthrough) is not None:
            kwargs[passthrough] = cfg.settings[passthrough]
    return ModelSettings(**kwargs)


def _model_retry_settings() -> ModelRetrySettings:
    return ModelRetrySettings(
        max_retries=_MODEL_RETRY_MAX_RETRIES,
        backoff=ModelRetryBackoffSettings(
            initial_delay=1.0,
            max_delay=2.0,
            multiplier=2.0,
            jitter=True,
        ),
        # OpenAI's generic advice marks every HTTP 429 retryable. The leading
        # guard keeps real rate limiting recoverable while preventing quota and
        # billing failures from being replayed as if waiting could fix them.
        policy=retry_policies.all(
            _retry_unless_permanent_model_error,
            retry_policies.any(
                retry_policies.provider_suggested(),
                retry_policies.network_error(),
                retry_policies.retry_after(),
                retry_policies.http_status(_MODEL_RETRY_HTTP_STATUSES),
                _retry_explicit_transient_model_error,
            ),
        ),
    )


def _retry_explicit_transient_model_error(context: RetryPolicyContext) -> bool:
    normalized = context.normalized
    error_code = str(normalized.error_code or "").strip().lower()
    if error_code in _MODEL_RETRY_ERROR_CODES:
        return True
    message = str(normalized.message or "").strip().lower()
    return any(fragment in message for fragment in _MODEL_RETRY_MESSAGE_FRAGMENTS)


def _retry_unless_permanent_model_error(
    context: RetryPolicyContext,
) -> bool | RetryDecision:
    if not _is_permanent_model_error(
        context.error,
        error_code=context.normalized.error_code,
        message=context.normalized.message,
    ):
        return True
    return RetryDecision(retry=False, reason="permanent_model_provider_error")


def is_retryable_model_error(exc: BaseException) -> bool:
    """Return whether a failed model turn may continue from current world truth.

    SDK request replay remains guarded separately. This classifier is for the
    outer session, which starts a new continuation turn instead of replaying
    the interrupted turn and its already committed tool effects.
    """

    errors = tuple(_iter_model_error_chain(exc))
    if _is_permanent_model_error(exc):
        return False

    error_codes = _model_error_codes(errors)
    for current in errors:
        if isinstance(current, (APIConnectionError, APITimeoutError, TransportError)):
            return True
        if isinstance(current, APIStatusError):
            if current.status_code in _MODEL_RETRY_HTTP_STATUSES:
                return True
            if current.status_code == 429 and (
                bool(error_codes & _MODEL_RETRY_ERROR_CODES)
                or _has_retry_after(current)
            ):
                return True
        if isinstance(current, APIError):
            message = str(current).strip().lower()
            if any(fragment in message for fragment in _MODEL_RETRY_MESSAGE_FRAGMENTS):
                return True
    return False


def _is_permanent_model_error(
    exc: BaseException,
    *,
    error_code: str | None = None,
    message: str | None = None,
) -> bool:
    errors = tuple(_iter_model_error_chain(exc))
    codes = _model_error_codes(errors)
    if error_code:
        codes.add(error_code.strip().lower())
    if codes & _PERMANENT_MODEL_ERROR_CODES:
        return True

    messages = [str(current).strip().lower() for current in errors]
    if message:
        messages.append(message.strip().lower())
    return any(
        fragment in candidate
        for candidate in messages
        for fragment in _PERMANENT_MODEL_ERROR_MESSAGE_FRAGMENTS
    )


def _iter_model_error_chain(exc: BaseException):
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _model_error_codes(errors: Sequence[BaseException]) -> set[str]:
    codes: set[str] = set()
    for error in errors:
        for value in (getattr(error, "code", None), getattr(error, "type", None)):
            if isinstance(value, str) and value.strip():
                codes.add(value.strip().lower())
        body = getattr(error, "body", None)
        if not isinstance(body, Mapping):
            continue
        nested = body.get("error")
        payloads = (body, nested) if isinstance(nested, Mapping) else (body,)
        for payload in payloads:
            for key in ("code", "type"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    codes.add(value.strip().lower())
    return codes


def _has_retry_after(error: BaseException) -> bool:
    response = getattr(error, "response", None)
    header_sources = [getattr(response, "headers", None)]
    header_sources.extend(
        (getattr(error, "headers", None), getattr(error, "response_headers", None))
    )
    for headers in header_sources:
        if not isinstance(headers, Mapping):
            continue
        for key, value in headers.items():
            if str(key).lower() in {"retry-after", "retry-after-ms"} and str(value).strip():
                return True
    return False


def _request_timeout_s(cfg: ProviderConfig) -> float:
    raw = cfg.settings.get("request_timeout_s", DEFAULT_MODEL_REQUEST_TIMEOUT_S)
    try:
        timeout_s = float(raw)
    except (TypeError, ValueError) as exc:
        raise ProviderConfigError(
            f"provider {cfg.name!r}: request_timeout_s must be numeric"
        ) from exc
    if not MIN_MODEL_REQUEST_TIMEOUT_S <= timeout_s <= MAX_MODEL_REQUEST_TIMEOUT_S:
        raise ProviderConfigError(
            f"provider {cfg.name!r}: request_timeout_s must be between "
            f"{MIN_MODEL_REQUEST_TIMEOUT_S:g} and {MAX_MODEL_REQUEST_TIMEOUT_S:g} seconds"
        )
    return timeout_s
