"""Provider-blind model configuration (framework-agnostic, no SDK import).

This is the *data* half of the Model Provider seam
(`docs/design-docs/agent-layer-architecture.md` §6.1). It carries zero coupling
to any agent framework or LLM vendor, so the agent core (`brain/`) can read a
provider's capabilities to gate behaviour without importing the SDK.

The resolver that turns a `ProviderConfig` into a concrete openai-agents
`Model` lives in the binding ring (`minebot/app/model_provider.py`), which is the
only side allowed to import the framework.

Design rules realised here:
- The core references a model by a *logical name* ("primary"/"fast"/"judge").
  Which vendor backs that name is entirely contained in a `ProviderConfig`.
- Credentials are referenced by env-var **name** only; a key value never lives
  in config, git, or logs.
- `Capabilities` flags default to ``False`` because the core uses only the
  portable lowest-common-denominator feature set; a ``True`` flag gates an
  additive, opt-in path, never a behaviour the core assumes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ProviderKind = Literal["openai_responses", "openai_chat", "litellm", "custom"]
ApiShape = Literal["responses", "chat_completions"]


@dataclass(frozen=True)
class Capabilities:
    """Opt-in provider superpowers the core must NOT assume by default.

    The core behaves identically whether or not a provider supports any of
    these; a ``True`` flag only unlocks an additive path at the binding ring.
    """

    json_schema: bool = False      # provider enforces structured-output schemas
    multimodal: bool = False       # accepts image / audio inputs
    hosted_tools: bool = False     # provider-side tools (web search, code interp.)
    prompt_caching: bool = False   # provider honours prompt-cache hints


@dataclass(frozen=True)
class ProviderConfig:
    """One logically-named model endpoint, vendor-blind.

    ``name`` is the logical handle the core references; which vendor backs it is
    fully contained here and resolved at the seam. Swapping the vendor behind a
    logical name is a config change with zero core change.
    """

    name: str
    kind: ProviderKind
    model: str
    base_url: str | None = None
    api_key_env: str | None = None
    api_shape: ApiShape | None = None
    capabilities: Capabilities = field(default_factory=Capabilities)
    include_usage: bool = False
    tracing: bool = False
    settings: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("ProviderConfig.name must be non-empty")
        if not self.model:
            raise ValueError(f"ProviderConfig[{self.name}].model must be non-empty")
        if self.api_shape is None:
            derived: ApiShape = (
                "responses" if self.kind == "openai_responses" else "chat_completions"
            )
            object.__setattr__(self, "api_shape", derived)
        if self.kind == "openai_responses" and self.api_shape != "responses":
            raise ValueError(
                f"ProviderConfig[{self.name}]: kind=openai_responses requires api_shape=responses"
            )
        if self.kind == "openai_chat" and self.api_shape != "chat_completions":
            raise ValueError(
                f"ProviderConfig[{self.name}]: kind=openai_chat requires api_shape=chat_completions"
            )


__all__ = ["ApiShape", "Capabilities", "ProviderConfig", "ProviderKind"]
