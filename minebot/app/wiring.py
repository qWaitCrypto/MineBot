"""Composition helpers for the Agent Phase-1 runtime."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from agents import Session

from minebot.app.model_provider import ModelProviderRegistry
from minebot.app.observation_artifacts import ToolObservationArchive
from minebot.app.progress_epochs import ProgressEpochArchive
from minebot.app.skills import SkillWorkspace
from minebot.app.runner import AgentRuntime, RecoveryHandler, RuntimeTrace
from minebot.brain.context import AgentContext
from minebot.brain.lifecycle import LifecycleController
from minebot.brain.modes import ModeRuntime
from minebot.brain.persona import MINEBOT_SYSTEM_PROMPT, prompt_with_language
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
    skill_workspace: SkillWorkspace | None = None


def build_agent_runtime(
    *,
    body: Body,
    registry: ToolRegistry,
    system_prompt: str = MINEBOT_SYSTEM_PROMPT,
    language: str = "English",
    goal_text: str,
    model_provider: ModelProviderRegistry | None = None,
    agent_name: str = "MineBot",
    trace: RuntimeTrace | None = None,
    recovery_handler: RecoveryHandler | None = None,
    authority: ProgressAuthority | None = None,
    speech_sink: Callable[[str], None] | None = None,
    conversation_session: Session | None = None,
    observation_archive: ToolObservationArchive | None = None,
    progress_epoch_archive: ProgressEpochArchive | None = None,
) -> AgentRuntimeParts:
    context = AgentContext(
        system_prompt=prompt_with_language(system_prompt, language=language),
        goal_text=goal_text,
        language=language,
    )
    lifecycle = LifecycleController()
    modes = ModeRuntime()
    authority = authority or ProgressAuthority()
    runtime = AgentRuntime(
        body=body,
        registry=registry,
        agent_context=context,
        lifecycle=lifecycle,
        mode_runtime=modes,
        authority=authority,
        model_provider=model_provider,
        agent_name=agent_name,
        trace=trace,
        recovery_handler=recovery_handler,
        speech_sink=speech_sink,
        conversation_session=conversation_session,
        observation_archive=observation_archive,
        progress_epoch_archive=progress_epoch_archive,
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
