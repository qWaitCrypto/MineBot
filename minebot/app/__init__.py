"""Binding ring + composition root for the MineBot agent layer.

This package is the ONLY place allowed to import the agent framework
(`openai-agents`) and the only place that wires `brain/` + `body/` + `game/`
together. The agent core (`brain/`) stays framework-agnostic; all coupling to a
specific LLM provider or SDK lives here.

See `docs/design-docs/agent-layer-architecture.md` §6 (binding ring) and §10
(walking skeleton).
"""

__all__: list[str] = []
