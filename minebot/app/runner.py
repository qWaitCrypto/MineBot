"""openai-agents binding for the Phase-1 runtime spine."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from agents import Agent, RunConfig, RunContextWrapper, Runner
from agents.tool import FunctionTool

from minebot.app.model_provider import ModelProviderRegistry
from minebot.brain.context import AgentContext
from minebot.brain.lifecycle import LifecycleController, LifecycleError, LifecycleState
from minebot.brain.modes import (
    AgentSignal,
    ModeRuntime,
    RuntimeProfile,
    signalize_body_state,
    signalize_events,
)
from minebot.brain.progress import ProgressAuthority
from minebot.brain.registry import RegisteredTool, ToolRegistry, WeldContext, execute_tool
from minebot.contract import Body, JsonObject, ProgressAbort, ProgressFacts

RunnerCallable = Callable[..., Awaitable[Any]]


@dataclass
class RuntimeRunContext:
    agent_context: AgentContext
    weld_context: WeldContext
    profile: RuntimeProfile
    enabled_facts: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentTurnOutcome:
    status: str
    lifecycle: LifecycleState
    profile: RuntimeProfile
    result: Any | None = None
    yielded_facts: ProgressFacts | None = None
    message: str | None = None


def tool_is_enabled(
    sidecar: Any,
    profile: RuntimeProfile,
    facts: dict[str, object] | None = None,
) -> bool:
    """Q1 default shared-pool predicate.

    State foregrounds capabilities through context and ordering; it does not
    hide tools. Q2 will feed governance facts here.
    """
    facts = facts or {}
    if facts.get("disabled") is True:
        return False
    return True


def sdk_tool_for(tool: RegisteredTool) -> FunctionTool:
    async def on_invoke_tool(ctx: RunContextWrapper[RuntimeRunContext], input_json: str) -> JsonObject:
        try:
            params = json.loads(input_json) if input_json else {}
        except json.JSONDecodeError as exc:
            return {
                "success": False,
                "reason": "invalid_tool_json",
                "canRetry": False,
                "nextSuggestion": None,
                "metrics": {"error": str(exc)},
            }
        if not isinstance(params, dict):
            return {
                "success": False,
                "reason": "invalid_tool_input",
                "canRetry": False,
                "nextSuggestion": None,
                "metrics": {"expected": "object"},
            }
        return execute_tool(tool, params, ctx.context.weld_context)

    def is_enabled(ctx: RunContextWrapper[RuntimeRunContext], agent: Any) -> bool:
        return tool_is_enabled(tool.sidecar, ctx.context.profile, ctx.context.enabled_facts)

    return FunctionTool(
        name=tool.name,
        description=tool.description,
        params_json_schema=tool.input_schema,
        on_invoke_tool=on_invoke_tool,
        strict_json_schema=False,
        is_enabled=is_enabled,
        timeout_seconds=tool.sidecar.timeout_s,
        _failure_error_function=None,
        _use_default_failure_error_function=False,
    )


class AgentRuntime:
    """Lifecycle-controlled shell around one SDK Runner turn."""

    def __init__(
        self,
        *,
        body: Body,
        registry: ToolRegistry,
        agent_context: AgentContext,
        lifecycle: LifecycleController,
        mode_runtime: ModeRuntime,
        authority: ProgressAuthority,
        model_provider: ModelProviderRegistry | None = None,
        runner_run: RunnerCallable | None = None,
        agent_name: str = "MineBot",
        max_turns: int = 10,
    ) -> None:
        self.body = body
        self.registry = registry
        self.agent_context = agent_context
        self.lifecycle = lifecycle
        self.mode_runtime = mode_runtime
        self.authority = authority
        self.model_provider = model_provider
        self.runner_run: RunnerCallable = runner_run or Runner.run
        self.max_turns = max_turns
        self.weld_context = WeldContext(
            body=body,
            authority=authority,
            goal_text=agent_context.goal_text,
        )
        self.agent = Agent[RuntimeRunContext](
            name=agent_name,
            tools=[sdk_tool_for(self.registry.get(name)) for name in self.registry.names()],
            instructions=self._instructions,
            model="primary",
        )
        self.last_tool_results: list[dict[str, Any]] = []

    async def run_turn(self, extra_signals: list[AgentSignal] | None = None) -> AgentTurnOutcome:
        self._ensure_active()

        state = self.body.get_state()
        events = self.body.poll_events()
        signals = [
            *signalize_body_state(state),
            *signalize_events(events),
            *(extra_signals or []),
        ]
        if self.last_tool_results:
            signals.append(AgentSignal.tool_results(list(self.last_tool_results)))

        reduction = self.mode_runtime.reduce(
            signals,
            self.lifecycle.state,
            goal_text=self.agent_context.goal_text,
        )
        self._apply_lifecycle_request(reduction.requested_lifecycle)
        profile = self.mode_runtime.profile_for(self.lifecycle.state)
        self.agent_context.observe_state(state)
        self.agent_context.observe_profile(profile)
        self.weld_context.goal_text = self.agent_context.goal_text

        if not self.lifecycle.is_active:
            return AgentTurnOutcome(
                status="stopped",
                lifecycle=self.lifecycle.state,
                profile=profile,
                message=reduction.reason,
            )

        run_context = RuntimeRunContext(
            agent_context=self.agent_context,
            weld_context=self.weld_context,
            profile=profile,
        )
        run_config = self._run_config(profile)
        turn_agent = self._agent_for_profile(profile)

        try:
            result = await self.runner_run(
                turn_agent,
                self.agent_context.turn_preamble() or "Continue the current goal.",
                context=run_context,
                max_turns=self.max_turns,
                run_config=run_config,
            )
        except ProgressAbort as exc:
            facts = exc.facts or self.authority.facts(self.agent_context.goal_text)
            yielded = self.mode_runtime.reduce(
                [AgentSignal.progress_abort(facts)],
                self.lifecycle.state,
                goal_text=self.agent_context.goal_text,
            )
            self._apply_lifecycle_request(yielded.requested_lifecycle)
            yielded_profile = self.mode_runtime.profile_for(self.lifecycle.state)
            self.agent_context.observe_profile(yielded_profile)
            return AgentTurnOutcome(
                status="yielded",
                lifecycle=self.lifecycle.state,
                profile=yielded_profile,
                yielded_facts=facts,
                message=_yield_message(facts, self.agent_context.goal_text),
            )

        return AgentTurnOutcome(
            status="completed_turn",
            lifecycle=self.lifecycle.state,
            profile=profile,
            result=result,
        )

    def _instructions(
        self,
        ctx: RunContextWrapper[RuntimeRunContext],
        agent: Agent[RuntimeRunContext],
    ) -> str:
        context = ctx.context.agent_context
        context.begin_turn()
        preamble = context.turn_preamble()
        if preamble:
            return f"{context.system_prompt}\n\n{preamble}"
        return context.system_prompt

    def _ensure_active(self) -> None:
        if self.lifecycle.state is LifecycleState.INIT:
            self.lifecycle.ready()
        if self.lifecycle.state is LifecycleState.IDLE:
            self.lifecycle.start()
        elif self.lifecycle.state is LifecycleState.RESUMING:
            self.lifecycle.reenter_active()

    def _apply_lifecycle_request(self, target: LifecycleState | None) -> None:
        if target is None or target is self.lifecycle.state:
            return
        try:
            if target is LifecycleState.YIELDED:
                self.lifecycle.yield_()
            elif target is LifecycleState.INTERRUPTED:
                self.lifecycle.interrupt()
            elif target is LifecycleState.RECOVERING:
                self.lifecycle.enter_recovery()
            elif target is LifecycleState.RESUMING:
                self.lifecycle.resume()
            elif target is LifecycleState.ACTIVE:
                self.lifecycle.reenter_active()
            elif target is LifecycleState.IDLE:
                self.lifecycle.stand_down()
            else:
                self.lifecycle.transition(target)
        except LifecycleError:
            raise

    def _agent_for_profile(self, profile: RuntimeProfile) -> Agent[RuntimeRunContext]:
        kwargs: dict[str, Any] = {"model": profile.model_route}
        if self.model_provider is not None:
            kwargs["model_settings"] = self.model_provider.model_settings_for(profile.model_route)
        return self.agent.clone(**kwargs)

    def _run_config(self, profile: RuntimeProfile) -> RunConfig:
        if self.model_provider is None:
            return RunConfig()
        return RunConfig(
            model_provider=self.model_provider,
            model_settings=self.model_provider.model_settings_for(profile.model_route),
        )


def _yield_message(facts: ProgressFacts, goal_text: str) -> str:
    return (
        "Progress authority yielded.\n"
        f"GOAL: {goal_text}\n"
        f"stagnant={facts.stagnant_steps} stalled={facts.stalled_steps} "
        f"failures={facts.failure_steps}\n"
        "How should I continue?"
    )


__all__ = [
    "AgentRuntime",
    "AgentTurnOutcome",
    "RuntimeRunContext",
    "sdk_tool_for",
    "tool_is_enabled",
]
