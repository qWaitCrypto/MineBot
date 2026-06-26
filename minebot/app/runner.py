"""openai-agents binding for the Phase-1 runtime spine."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from agents import Agent, RunConfig, RunContextWrapper, Runner, RunHooks
from agents.exceptions import UserError
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
    tool_facts: dict[str, dict[str, object]] = field(default_factory=dict)
    trace: "RuntimeTrace | None" = None

    def facts_for_tool(self, tool_name: str) -> dict[str, object]:
        return dict(self.tool_facts.get(tool_name, {}))


@dataclass(frozen=True)
class AgentTurnOutcome:
    status: str
    lifecycle: LifecycleState
    profile: RuntimeProfile
    result: Any | None = None
    yielded_facts: ProgressFacts | None = None
    message: str | None = None


@dataclass
class RuntimeTrace:
    """In-memory trace sink for Phase-1 turn/tool observability."""

    events: list[dict[str, object]] = field(default_factory=list)

    def emit(self, event: str, **fields: object) -> None:
        self.events.append({"event": event, **fields})

    def snapshot(self) -> list[dict[str, object]]:
        return [dict(event) for event in self.events]


class RuntimeHooks(RunHooks[RuntimeRunContext]):
    """SDK hook bridge into RuntimeTrace."""

    async def on_agent_start(self, context: Any, agent: Any) -> None:
        trace = _trace_from_context(context)
        if trace is not None:
            trace.emit("agent_start", agent=getattr(agent, "name", None))

    async def on_agent_end(self, context: Any, agent: Any, output: Any) -> None:
        trace = _trace_from_context(context)
        if trace is not None:
            trace.emit("agent_end", agent=getattr(agent, "name", None), output_type=type(output).__name__)

    async def on_llm_start(self, context: Any, agent: Any, system_prompt: str | None, input_items: list[Any]) -> None:
        trace = _trace_from_context(context)
        if trace is not None:
            trace.emit(
                "llm_start",
                agent=getattr(agent, "name", None),
                input_count=len(input_items),
                has_system_prompt=system_prompt is not None,
            )

    async def on_llm_end(self, context: Any, agent: Any, response: Any) -> None:
        trace = _trace_from_context(context)
        if trace is not None:
            trace.emit("llm_end", agent=getattr(agent, "name", None), response_type=type(response).__name__)

    async def on_tool_start(self, context: Any, agent: Any, tool: Any) -> None:
        trace = _trace_from_context(context)
        if trace is not None:
            trace.emit("tool_start", agent=getattr(agent, "name", None), tool=getattr(tool, "name", None))

    async def on_tool_end(self, context: Any, agent: Any, tool: Any, result: object) -> None:
        trace = _trace_from_context(context)
        if trace is not None:
            reason = result.get("reason") if isinstance(result, dict) else None
            trace.emit(
                "tool_end",
                agent=getattr(agent, "name", None),
                tool=getattr(tool, "name", None),
                reason=reason,
                result_type=type(result).__name__,
            )


def tool_is_enabled(
    sidecar: Any,
    profile: RuntimeProfile,
    facts: dict[str, object] | None = None,
) -> bool:
    """Shared-pool tool projection predicate.

    State foregrounds capabilities through context and ordering; it does not
    hide tools. Only governance/refusal facts and hard preconditions disable a
    tool.
    """
    facts = facts or {}
    if facts.get("disabled") is True or facts.get("precondition_missing") is True:
        return False
    decision = facts.get("governance")
    if hasattr(decision, "allowed"):
        return bool(decision.allowed)
    if isinstance(decision, dict) and decision.get("allowed") is False:
        return False
    return True


def sdk_tool_for(tool: RegisteredTool) -> FunctionTool:
    async def on_invoke_tool(ctx: RunContextWrapper[RuntimeRunContext], input_json: str) -> JsonObject:
        trace = ctx.context.trace
        if trace is not None:
            trace.emit(
                "tool_invoke",
                tool=tool.name,
                mutating=tool.sidecar.mutating,
                permission=tool.sidecar.permission,
                situational=ctx.context.profile.situational,
                lifecycle=ctx.context.profile.lifecycle,
            )
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
        result = execute_tool(tool, params, ctx.context.weld_context)
        if trace is not None:
            trace.emit("tool_result", tool=tool.name, reason=str(result.get("reason")), success=bool(result.get("success")))
        return result

    def is_enabled(ctx: RunContextWrapper[RuntimeRunContext], agent: Any) -> bool:
        enabled = tool_is_enabled(tool.sidecar, ctx.context.profile, ctx.context.facts_for_tool(tool.name))
        if ctx.context.trace is not None:
            ctx.context.trace.emit(
                "tool_enabled",
                tool=tool.name,
                enabled=enabled,
                situational=ctx.context.profile.situational,
                lifecycle=ctx.context.profile.lifecycle,
            )
        return enabled

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
        tool_facts: dict[str, dict[str, object]] | None = None,
        trace: RuntimeTrace | None = None,
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
        self.tool_facts: dict[str, dict[str, object]] = tool_facts or {}
        self.trace = trace or RuntimeTrace()
        self.hooks = RuntimeHooks()
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

    def set_tool_facts(self, tool_name: str, facts: dict[str, object]) -> None:
        self.tool_facts[tool_name] = dict(facts)

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
        self.trace.emit(
            "turn_profile",
            relationship=profile.relationship,
            situational=profile.situational,
            lifecycle=profile.lifecycle,
            tool_focus=list(profile.tool_focus),
            model_route=profile.model_route,
            policy_tags=list(profile.policy_tags),
        )

        if not self.lifecycle.is_active:
            self.trace.emit("turn_stopped", lifecycle=self.lifecycle.state.value, reason=reduction.reason)
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
            tool_facts={name: dict(facts) for name, facts in self.tool_facts.items()},
            trace=self.trace,
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
                hooks=self.hooks,
            )
        except ProgressAbort as exc:
            return self._yield_from_progress_abort(exc)
        except UserError as exc:
            progress_abort = _find_progress_abort(exc)
            if progress_abort is None:
                raise
            return self._yield_from_progress_abort(progress_abort)

        self.trace.emit("turn_completed", lifecycle=self.lifecycle.state.value, situational=profile.situational)
        self._record_run_result(result)
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
            self._inject_resume_context()
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

    def _inject_resume_context(self) -> None:
        slot = self.mode_runtime.consume_suspend_slot()
        if slot is None:
            self.trace.emit("resume_without_suspend", lifecycle=self.lifecycle.state.value)
            return
        facts = {
            "goal": slot.goal_text,
            "composition_id": slot.composition_id,
            "reason": slot.reason,
            "last_progress": dict(slot.last_progress),
        }
        self.agent_context.observe_resume(facts)
        self.trace.emit(
            "resume_context",
            goal=slot.goal_text,
            reason=slot.reason,
            composition_id=slot.composition_id,
        )

    def _record_run_result(self, result: Any) -> None:
        extracted = extract_run_observations(result)
        for event in extracted:
            self.trace.emit(**event)
        has_content = any(
            event.get("event") in {"assistant_message", "assistant_final_output"} and event.get("content")
            for event in extracted
        )
        has_tool_call = any(event.get("event") == "model_tool_call" for event in extracted)
        if has_tool_call and not has_content:
            self.trace.emit("assistant_no_content_tool_only")

    def _yield_from_progress_abort(self, exc: ProgressAbort) -> AgentTurnOutcome:
        facts = exc.facts or self.authority.facts(self.agent_context.goal_text)
        yielded = self.mode_runtime.reduce(
            [AgentSignal.progress_abort(facts)],
            self.lifecycle.state,
            goal_text=self.agent_context.goal_text,
        )
        self._apply_lifecycle_request(yielded.requested_lifecycle)
        yielded_profile = self.mode_runtime.profile_for(self.lifecycle.state)
        self.agent_context.observe_profile(yielded_profile)
        self.trace.emit(
            "progress_yielded",
            stagnant_steps=facts.stagnant_steps,
            stalled_steps=facts.stalled_steps,
            failure_steps=facts.failure_steps,
            lifecycle=self.lifecycle.state.value,
            situational=yielded_profile.situational,
        )
        return AgentTurnOutcome(
            status="yielded",
            lifecycle=self.lifecycle.state,
            profile=yielded_profile,
            yielded_facts=facts,
            message=_yield_message(facts, self.agent_context.goal_text),
        )


def _yield_message(facts: ProgressFacts, goal_text: str) -> str:
    return (
        "Progress authority yielded.\n"
        f"GOAL: {goal_text}\n"
        f"stagnant={facts.stagnant_steps} stalled={facts.stalled_steps} "
        f"failures={facts.failure_steps}\n"
        "How should I continue?"
    )


def _trace_from_context(context: Any) -> RuntimeTrace | None:
    runtime_context = getattr(context, "context", None)
    return getattr(runtime_context, "trace", None)


def _find_progress_abort(exc: BaseException) -> ProgressAbort | None:
    seen: set[int] = set()
    cursor: BaseException | None = exc
    while cursor is not None and id(cursor) not in seen:
        seen.add(id(cursor))
        if isinstance(cursor, ProgressAbort):
            return cursor
        cause = cursor.__cause__
        context = cursor.__context__
        cursor = cause if cause is not None else context
    return None


def extract_run_observations(result: Any) -> list[dict[str, object]]:
    """Extract model-visible observations from an SDK run result.

    This is intentionally best-effort: SDK item shapes vary across versions and
    providers, and observation failure must never downgrade task execution.
    """
    events: list[dict[str, object]] = []
    to_input_list = getattr(result, "to_input_list", None)
    if not callable(to_input_list):
        final_output = getattr(result, "final_output", None)
        if final_output not in {None, ""}:
            events.append({"event": "assistant_final_output", "content": _shorten(_public_text(final_output), limit=2000)})
        return events
    try:
        items = to_input_list()
    except Exception as exc:  # pragma: no cover - defensive SDK compatibility guard
        events.append({"event": "run_observation_failed", "error_type": type(exc).__name__})
        return events
    if not isinstance(items, list):
        return events
    assistant_texts: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        item_type = _public_text(item.get("type") or "")
        role = item.get("role")
        if role == "assistant" or item_type in {"message", "output_text"}:
            content = _text_from_item(item)
            if content:
                shortened = _shorten(content, limit=2000)
                assistant_texts.add(shortened)
                events.append({"event": "assistant_message", "content": shortened})
        if item_type in {"function_call_output", "tool_output"}:
            events.append({"event": "model_tool_output", "summary": _shorten(_public_text(item.get("output") or ""))})
        elif item_type in {"function_call", "tool_call"}:
            events.append(
                {
                    "event": "model_tool_call",
                    "tool": _tool_name_from_item(item, fallback=item_type),
                    "arguments_summary": _tool_arguments_summary(item),
                }
            )
    final_output = getattr(result, "final_output", None)
    if final_output not in {None, ""}:
        final_text = _shorten(_public_text(final_output), limit=2000)
        if final_text not in assistant_texts:
            events.append({"event": "assistant_final_output", "content": final_text})
    return events


def _text_from_item(item: dict[str, object]) -> str:
    content = item.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                text = part.get("text") or part.get("content")
                if text is not None:
                    parts.append(_public_text(text))
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(part for part in parts if part)
    text = item.get("text")
    return _public_text(text) if text is not None else ""


def _tool_name_from_item(item: dict[str, object], *, fallback: str) -> str:
    direct = item.get("name")
    if direct:
        return _public_text(direct)
    function = item.get("function")
    if isinstance(function, dict) and function.get("name"):
        return _public_text(function["name"])
    return fallback


def _tool_arguments_summary(item: dict[str, object]) -> str | None:
    raw = item.get("arguments")
    function = item.get("function")
    if raw is None and isinstance(function, dict):
        raw = function.get("arguments")
    if raw is None:
        return None
    return _shorten(_public_text(raw), limit=500)


def _public_text(value: object) -> str:
    if isinstance(value, Enum):
        return value.value if isinstance(value.value, str) else str(value.value)
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=True, sort_keys=True)
        except TypeError:
            return str(value)
    return str(value)


def _shorten(text: str, *, limit: int = 500) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


__all__ = [
    "AgentRuntime",
    "AgentTurnOutcome",
    "RuntimeHooks",
    "RuntimeRunContext",
    "RuntimeTrace",
    "extract_run_observations",
    "sdk_tool_for",
    "tool_is_enabled",
]
