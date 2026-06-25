"""Composition helpers for the Agent Phase-1 runtime."""

from __future__ import annotations

from dataclasses import dataclass

from minebot.app.model_provider import ModelProviderRegistry
from minebot.app.runner import AgentRuntime
from minebot.brain.context import AgentContext
from minebot.brain.lifecycle import LifecycleController
from minebot.brain.modes import ModeRuntime
from minebot.brain.progress import ProgressAuthority
from minebot.brain.registry import ToolRegistry
from minebot.contract import Body


@dataclass(frozen=True)
class AgentRuntimeParts:
    runtime: AgentRuntime
    registry: ToolRegistry
    context: AgentContext
    lifecycle: LifecycleController
    modes: ModeRuntime
    authority: ProgressAuthority


def build_agent_runtime(
    *,
    body: Body,
    registry: ToolRegistry,
    system_prompt: str,
    goal_text: str,
    model_provider: ModelProviderRegistry | None = None,
    agent_name: str = "MineBot",
) -> AgentRuntimeParts:
    context = AgentContext(system_prompt=system_prompt, goal_text=goal_text)
    lifecycle = LifecycleController()
    modes = ModeRuntime()
    authority = ProgressAuthority()
    runtime = AgentRuntime(
        body=body,
        registry=registry,
        agent_context=context,
        lifecycle=lifecycle,
        mode_runtime=modes,
        authority=authority,
        model_provider=model_provider,
        agent_name=agent_name,
    )
    return AgentRuntimeParts(
        runtime=runtime,
        registry=registry,
        context=context,
        lifecycle=lifecycle,
        modes=modes,
        authority=authority,
    )


__all__ = ["AgentRuntimeParts", "build_agent_runtime"]
