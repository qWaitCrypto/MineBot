#!/usr/bin/env python3
"""Deterministic unit test for the Model Provider seam.

No Body, no live server, no network: it only proves provider-blind config
resolves to the right concrete openai-agents Model with credentials plumbed
from an env-var NAME (never a literal key). Runs anywhere openai-agents is
installed; not gated by the live-server SKIP-77 convention.

Run:  python tests/agent_provider_test.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents import OpenAIChatCompletionsModel, OpenAIResponsesModel  # noqa: E402

from minebot.app.model_provider import (  # noqa: E402
    Capabilities,
    ModelProviderRegistry,
    ProviderConfig,
    ProviderConfigError,
)

SECRET = "sk-do-not-leak-12345"
ENV = {"PRIMARY_KEY": SECRET, "FAST_KEY": "sk-fast-9"}

_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        _failures.append(name)


def expect_error(name: str, fn, *, contains: str = "", absent: str = "") -> None:
    try:
        fn()
    except ProviderConfigError as exc:
        msg = str(exc)
        ok = (contains in msg) and (absent == "" or absent not in msg)
        check(name, ok, f"msg={msg!r}")
    except Exception as exc:  # noqa: BLE001 - wrong exception type is a failure
        check(name, False, f"wrong exception {type(exc).__name__}: {exc}")
    else:
        check(name, False, "no exception raised")


def base_configs() -> list[ProviderConfig]:
    return [
        ProviderConfig(
            name="primary",
            kind="openai_chat",
            model="big-model",
            base_url="http://localhost:8000/v1",
            api_key_env="PRIMARY_KEY",
            settings={"temperature": 0.4, "max_tokens": 1024},
            include_usage=True,
        ),
        ProviderConfig(
            name="fast",
            kind="openai_responses",
            model="small-model",
            base_url="http://localhost:8001/v1",
            api_key_env="FAST_KEY",
        ),
    ]


def test_resolution_and_routing() -> None:
    reg = ModelProviderRegistry(base_configs(), env=ENV, install_tracing_policy=False)

    primary = reg.resolve("primary")
    check("primary -> ChatCompletionsModel", isinstance(primary, OpenAIChatCompletionsModel))
    check("primary model id", primary.model == "big-model")
    check("primary base_url plumbed", "localhost:8000" in str(primary._client.base_url))
    check("primary key from env", primary._client.api_key == SECRET)

    fast = reg.resolve("fast")
    check("fast -> ResponsesModel", isinstance(fast, OpenAIResponsesModel))
    check("fast model id", fast.model == "small-model")

    check("get_model(None) == default(primary)", reg.get_model(None) is primary)
    check("default name is first config", reg.default == "primary")
    check("get_model('fast') routes correctly", reg.get_model("fast") is fast)
    check("resolve caches identity", reg.resolve("primary") is primary)
    check("logical_names", set(reg.logical_names()) == {"primary", "fast"})


def test_credential_safety() -> None:
    reg = ModelProviderRegistry(base_configs(), env=ENV, install_tracing_policy=False)
    cfg = base_configs()[0]
    # The secret value must never live in config or any registry repr.
    check("secret not in ProviderConfig", SECRET not in repr(cfg))
    check("secret not in registry repr", SECRET not in repr(reg))
    check("config holds env NAME only", cfg.api_key_env == "PRIMARY_KEY")

    missing = [ProviderConfig(name="primary", kind="openai_chat", model="m",
                              base_url="http://x/v1", api_key_env="UNSET_VAR")]
    reg2 = ModelProviderRegistry(missing, env={}, install_tracing_policy=False)
    expect_error(
        "missing env key -> error names the var, not a value",
        lambda: reg2.resolve("primary"),
        contains="UNSET_VAR",
    )


def test_local_endpoint_placeholder_key() -> None:
    cfg = [ProviderConfig(name="primary", kind="openai_chat", model="m",
                          base_url="http://localhost:1234/v1")]  # no api_key_env
    reg = ModelProviderRegistry(cfg, env={}, install_tracing_policy=False)
    model = reg.resolve("primary")
    check("local endpoint uses placeholder key", model._client.api_key == "EMPTY")


def test_no_key_no_baseurl_rejected() -> None:
    cfg = [ProviderConfig(name="primary", kind="openai_chat", model="m")]
    reg = ModelProviderRegistry(cfg, env={}, install_tracing_policy=False)
    expect_error(
        "no api_key_env and no base_url -> rejected",
        lambda: reg.resolve("primary"),
        contains="cannot authenticate",
    )


def test_unknown_and_duplicate() -> None:
    reg = ModelProviderRegistry(base_configs(), env=ENV, install_tracing_policy=False)
    expect_error(
        "unknown logical name lists configured",
        lambda: reg.resolve("judge"),
        contains="configured:",
    )
    dup = base_configs() + [ProviderConfig(name="primary", kind="openai_chat", model="z",
                                           base_url="http://y/v1", api_key_env="FAST_KEY")]
    expect_error(
        "duplicate logical name rejected",
        lambda: ModelProviderRegistry(dup, env=ENV, install_tracing_policy=False),
        contains="duplicate",
    )


def test_api_shape_and_capabilities() -> None:
    chat = ProviderConfig(name="a", kind="openai_chat", model="m", base_url="http://x/v1")
    resp = ProviderConfig(name="b", kind="openai_responses", model="m", base_url="http://x/v1")
    check("openai_chat derives chat_completions", chat.api_shape == "chat_completions")
    check("openai_responses derives responses", resp.api_shape == "responses")
    check("capabilities default to portable LCD (all False)",
          Capabilities() == Capabilities(False, False, False, False))

    try:
        ProviderConfig(name="bad", kind="openai_responses", model="m",
                       api_shape="chat_completions")
    except ValueError:
        check("mismatched api_shape rejected", True)
    else:
        check("mismatched api_shape rejected", False, "no ValueError")


def test_litellm_requires_extra() -> None:
    cfg = [ProviderConfig(name="primary", kind="litellm", model="anthropic/claude-x",
                          api_key_env="PRIMARY_KEY")]
    reg = ModelProviderRegistry(cfg, env=ENV, install_tracing_policy=False)
    # litellm optional dependency is not installed in the base environment.
    try:
        from agents.extensions.models.litellm_model import LitellmModel  # noqa: F401
        check("litellm installed -> skip extras-error assertion", True)
    except ImportError:
        expect_error(
            "litellm kind without extra -> actionable error",
            lambda: reg.resolve("primary"),
            contains="openai-agents[litellm]",
        )


def test_model_settings_translation() -> None:
    reg = ModelProviderRegistry(base_configs(), env=ENV, install_tracing_policy=False)
    settings = reg.model_settings_for("primary")
    check("temperature plumbed", settings.temperature == 0.4)
    check("max_tokens plumbed", settings.max_tokens == 1024)
    check("include_usage plumbed", settings.include_usage is True)


def test_tracing_policy_runs() -> None:
    # Default off, and opt-in does not raise. (Global flag; we assert no error.)
    ModelProviderRegistry(base_configs(), env=ENV)  # default install_tracing_policy=True
    traced = [ProviderConfig(name="primary", kind="openai_chat", model="m",
                             base_url="http://x/v1", api_key_env="PRIMARY_KEY", tracing=True)]
    ModelProviderRegistry(traced, env=ENV)
    check("tracing policy install does not raise", True)


def test_aclose() -> None:
    reg = ModelProviderRegistry(base_configs(), env=ENV, install_tracing_policy=False)
    reg.resolve("primary")
    reg.resolve("fast")
    asyncio.run(reg.aclose())
    check("aclose closes clients without error", True)


def main() -> int:
    for test in (
        test_resolution_and_routing,
        test_credential_safety,
        test_local_endpoint_placeholder_key,
        test_no_key_no_baseurl_rejected,
        test_unknown_and_duplicate,
        test_api_shape_and_capabilities,
        test_litellm_requires_extra,
        test_model_settings_translation,
        test_tracing_policy_runs,
        test_aclose,
    ):
        test()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
