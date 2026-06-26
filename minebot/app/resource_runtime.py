"""Resource-collection runtime compatibility helpers.

The formal Phase 1 real-server harness lives in ``minebot.app.phase1_runtime``.
This module keeps the older resource-runtime API while delegating to that full
registry so callers do not silently lose navigation or state tools.
"""

from __future__ import annotations

from dataclasses import dataclass

from minebot.app.model_provider import ModelProviderRegistry
from minebot.app.phase1_runtime import (
    Phase1RuntimeConfig,
    build_phase1_agent_runtime,
    build_phase1_registry,
    inventory_count,
)
from minebot.app.runner import RuntimeTrace
from minebot.app.wiring import AgentRuntimeParts
from minebot.brain.composition import CompositionBudget
from minebot.brain.registry import ToolRegistry
from minebot.contract import Region
from minebot.game import ScarpetBody


@dataclass(frozen=True)
class ResourceRuntimeConfig:
    natural_region: Region
    budget: CompositionBudget = CompositionBudget(max_candidates=96, max_mutating_calls=96, max_wall_s=900.0)


def build_resource_agent_runtime(
    *,
    body: ScarpetBody,
    goal_text: str,
    model_provider: ModelProviderRegistry | None,
    config: ResourceRuntimeConfig,
    agent_name: str = "MineBot",
    language: str = "English",
    trace: RuntimeTrace | None = None,
) -> AgentRuntimeParts:
    return build_phase1_agent_runtime(
        body=body,
        goal_text=goal_text,
        model_provider=model_provider,
        config=Phase1RuntimeConfig(natural_region=config.natural_region, budget=config.budget),
        agent_name=agent_name,
        language=language,
        trace=trace,
    )


def build_resource_registry(body: ScarpetBody, config: ResourceRuntimeConfig) -> ToolRegistry:
    return build_phase1_registry(body, Phase1RuntimeConfig(natural_region=config.natural_region, budget=config.budget))


__all__ = [
    "ResourceRuntimeConfig",
    "build_resource_agent_runtime",
    "build_resource_registry",
    "inventory_count",
]
