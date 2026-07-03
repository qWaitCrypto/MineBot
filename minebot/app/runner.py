"""openai-agents binding for the Phase-1 runtime spine."""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from uuid import uuid4

from agents import Agent, RunConfig, RunContextWrapper, Runner, RunHooks
from agents.exceptions import MaxTurnsExceeded, UserError
from agents.items import ItemHelpers, MessageOutputItem, ToolCallItem, ToolCallOutputItem
from agents.tool import FunctionTool

from minebot.app.model_provider import ModelProviderRegistry
from minebot.app.observability import ObservationSink, sanitize_observation
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
from minebot.game.errors import BodyProtocolError

RunnerCallable = Callable[..., Awaitable[Any]]
RecoveryHandler = Callable[["AgentRuntime"], Any]
BODY_TRANSPORT_RECOVERY_LIMIT = 3


class BodyRecoveryRequired(RuntimeError):
    """Raised when a Body-critical fact must preempt the model turn."""

    def __init__(self, reason: str, *, facts: dict[str, object] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.facts = dict(facts or {})


@dataclass
class RuntimeRunContext:
    agent_context: AgentContext
    weld_context: WeldContext
    profile: RuntimeProfile
    tool_facts: dict[str, dict[str, object]] = field(default_factory=dict)
    trace: "RuntimeTrace | None" = None
    runtime: "AgentRuntime | None" = None

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


@dataclass(frozen=True)
class RecoveryOutcome:
    """App-layer recovery driver result consumed by AgentSession."""

    success: bool
    reason: str
    facts: dict[str, object] = field(default_factory=dict)
    can_retry: bool = False


@dataclass
class RuntimeTrace:
    """In-memory trace sink for Phase-1 turn/tool observability."""

    session_id: str = "default"
    sink: ObservationSink | None = None
    events: list[dict[str, object]] = field(default_factory=list)
    _seq: int = 0

    def emit(self, event: str, **fields: object) -> None:
        self._seq += 1
        record = sanitize_observation(
            {
                "seq": self._seq,
                "ts": time.time(),
                "session_id": self.session_id,
                "event": event,
                **fields,
            }
        )
        self.events.append(record)
        if self.sink is not None:
            self.sink.write(record)

    def snapshot(self) -> list[dict[str, object]]:
        return [dict(event) for event in self.events]

    def close(self) -> None:
        if self.sink is not None:
            self.sink.close()


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
            for event in extract_model_response_observations(response):
                trace.emit(**event)

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

    The registry remains the single shared tool pool. Runtime profiles may
    foreground capabilities through context, but they must not hide tools as a
    behavior-forcing mechanism.
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
        tool_call_id = f"tool-{uuid4()}-{tool.name}"
        arguments_summary = _tool_arguments_summary_from_json(input_json)
        if trace is not None:
            runtime = getattr(ctx.context, "runtime", None)
            trace.emit(
                "tool_decision_context",
                tool_call_id=tool_call_id,
                tool=tool.name,
                situational=ctx.context.profile.situational,
                lifecycle=ctx.context.profile.lifecycle,
                tool_focus=list(ctx.context.profile.tool_focus),
                policy_tags=list(ctx.context.profile.policy_tags),
                last_known_body_state=dict(runtime.last_known_body_state or {}) if runtime is not None else None,
                recent_tool_results=_tool_result_summaries(runtime.last_tool_results if runtime is not None else []),
                recent_session_messages=_recent_session_messages(ctx.context.agent_context),
            )
            trace.emit(
                "tool_invoke",
                tool_call_id=tool_call_id,
                tool=tool.name,
                source=tool.sidecar.source,
                tool_type=tool.sidecar.tool_type,
                mutating=tool.sidecar.mutating,
                permission=tool.sidecar.permission,
                body_scope=list(tool.sidecar.body_scope),
                terminal_truth=list(tool.sidecar.terminal_truth),
                situational=ctx.context.profile.situational,
                lifecycle=ctx.context.profile.lifecycle,
                arguments_summary=arguments_summary,
            )
        try:
            params = json.loads(input_json) if input_json else {}
        except json.JSONDecodeError as exc:
            result = {
                "success": False,
                "reason": "invalid_tool_json",
                "canRetry": False,
                "nextSuggestion": None,
                "metrics": {"error": str(exc)},
            }
            return _finalize_tool_payload(
                tool=tool,
                result=result,
                trace=trace,
                tool_call_id=tool_call_id,
            )
        if not isinstance(params, dict):
            result = {
                "success": False,
                "reason": "invalid_tool_input",
                "canRetry": False,
                "nextSuggestion": None,
                "metrics": {"expected": "object"},
            }
            return _finalize_tool_payload(
                tool=tool,
                result=result,
                trace=trace,
                tool_call_id=tool_call_id,
            )
        try:
            result = execute_tool(tool, params, ctx.context.weld_context)
            result = _continue_collect_resource_tool(tool, result, ctx.context, tool_call_id=tool_call_id)
        except ProgressAbort:
            raise
        except BodyRecoveryRequired:
            raise
        except Exception as exc:
            result = _tool_exception_payload(exc)
            if trace is not None:
                trace.emit(
                    "tool_exception",
                    tool_call_id=tool_call_id,
                    tool=tool.name,
                    error_type=type(exc).__name__,
                    reason=result["reason"],
                    message=str(exc),
                    await_diagnostics=result.get("metrics", {}).get("await_diagnostics"),
                )
            if result.get("reason") == "transport_error":
                ctx.context.weld_context.authority.invalidate_generation(f"transport_error:{tool.name}")
                ctx.context.trace and ctx.context.trace.emit(
                    "tool_transport_recovery_candidate",
                    tool_call_id=tool_call_id,
                    tool=tool.name,
                    reason=result["reason"],
                    error_type=type(exc).__name__,
                    await_diagnostics=result.get("metrics", {}).get("await_diagnostics"),
                )
                runtime = getattr(ctx.context, "runtime", None)
                if runtime is not None:
                    runtime.record_transport_error(tool.name, result, tool_call_id=tool_call_id)
        if _requires_body_recovery(result):
            if trace is not None:
                trace.emit(
                    "tool_body_recovery_preempt",
                    tool_call_id=tool_call_id,
                    tool=tool.name,
                    reason=str(result.get("reason") or "body_recovery_required"),
                    full_result=result,
            )
            facts = _recovery_facts_from_tool(tool.name, result)
            raise BodyRecoveryRequired(_recovery_reason_from_tool_result(result, facts), facts=facts)

        runtime = getattr(ctx.context, "runtime", None)
        if runtime is not None:
            runtime.remember_tool_result(tool.name, result)
            runtime.remember_tool_body_facts(result)
        return _finalize_tool_payload(
            tool=tool,
            result=result,
            trace=trace,
            tool_call_id=tool_call_id,
        )

    def is_enabled(ctx: RunContextWrapper[RuntimeRunContext], agent: Any) -> bool:
        enabled = tool_is_enabled(tool.sidecar, ctx.context.profile, ctx.context.facts_for_tool(tool.name))
        if ctx.context.trace is not None:
            ctx.context.trace.emit(
                "tool_enabled",
                tool=tool.name,
                enabled=enabled,
                source=tool.sidecar.source,
                tool_type=tool.sidecar.tool_type,
                permission=tool.sidecar.permission,
                body_scope=list(tool.sidecar.body_scope),
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


def _finalize_tool_payload(
    *,
    tool: RegisteredTool,
    result: JsonObject,
    trace: RuntimeTrace | None,
    tool_call_id: str,
) -> JsonObject:
    model_result = _model_tool_payload(tool.name, result, trace_ref=tool_call_id)
    if trace is not None:
        trace.emit(
            "tool_result",
            tool_call_id=tool_call_id,
            tool=tool.name,
            reason=str(result.get("reason")),
            success=bool(result.get("success")),
            full_result=result,
            model_result=model_result,
        )
    return model_result


def _tool_exception_payload(exc: Exception) -> JsonObject:
    reason = "transport_error" if isinstance(exc, (BodyProtocolError, OSError, TimeoutError)) else "tool_runtime_error"
    diagnostics = getattr(exc, "diagnostics", None)
    metrics: JsonObject = {
        "error_type": type(exc).__name__,
        "message": _shorten(str(exc), limit=300),
    }
    if isinstance(diagnostics, dict):
        metrics["await_diagnostics"] = dict(diagnostics)
    return {
        "success": False,
        "reason": reason,
        "canRetry": True,
        "nextSuggestion": "retry after refreshing state; choose a different action if the same failure repeats",
        "metrics": metrics,
    }


def _requires_body_recovery(result: JsonObject) -> bool:
    if _is_body_recovery_reason(result.get("reason")):
        return True
    metrics = result.get("metrics")
    return _metrics_contain_recovery_fact(metrics)


def _metrics_contain_recovery_fact(value: object) -> bool:
    if isinstance(value, dict):
        reason = (
            value.get("reason")
            or value.get("stopped_reason")
            or value.get("event")
            or value.get("error")
        )
        if _is_body_recovery_reason(reason):
            return True
        if value.get("missing") is True:
            return True
        return any(_metrics_contain_recovery_fact(item) for item in value.values())
    if isinstance(value, list):
        return any(_metrics_contain_recovery_fact(item) for item in value)
    return False


def _is_body_recovery_reason(value: object) -> bool:
    if value is None:
        return False
    raw = str(value)
    normalized = "".join(ch for ch in raw.lower() if ch.isalnum())
    return normalized in {
        "death",
        "deathdetected",
        "botdied",
        "died",
        "missingbody",
        "bodymissing",
        "bodytransportunstable",
        "transportunstable",
    } or normalized.startswith(("death", "missingbody", "bodymissing", "bodytransport"))


def _recovery_facts_from_tool(tool_name: str, result: JsonObject) -> dict[str, object]:
    facts: dict[str, object] = {
        "tool": tool_name,
        "tool_result_reason": str(result.get("reason") or ""),
        "tool_success": bool(result.get("success")),
    }
    metrics = result.get("metrics")
    if isinstance(metrics, dict):
        for key in (
            "final_pos",
            "pos",
            "lastPos",
            "target",
            "inventory_hash",
            "inventory_before",
            "inventory_counts_before",
        ):
            if key in metrics:
                facts[key] = metrics[key]
        event = metrics.get("event")
        if isinstance(event, str):
            facts["event"] = event
    return facts


def _recovery_reason_from_tool_result(result: JsonObject, facts: dict[str, object]) -> str:
    for key in ("event", "error", "stopped_reason", "reason"):
        value = facts.get(key)
        if _is_body_recovery_reason(value):
            return str(value)
    metrics = result.get("metrics")
    nested = _first_body_recovery_reason(metrics)
    if nested is not None:
        return nested
    return str(result.get("reason") or "body_recovery_required")


def _first_body_recovery_reason(value: object) -> str | None:
    if isinstance(value, dict):
        for key in ("reason", "stopped_reason", "event", "error"):
            reason = value.get(key)
            if _is_body_recovery_reason(reason):
                return str(reason)
        if value.get("missing") is True:
            return "missing_body"
        for item in value.values():
            nested = _first_body_recovery_reason(item)
            if nested is not None:
                return nested
    if isinstance(value, list):
        for item in value:
            nested = _first_body_recovery_reason(item)
            if nested is not None:
                return nested
    return None


def _model_tool_payload(tool_name: str, result: JsonObject, *, trace_ref: str) -> JsonObject:
    reason = str(result.get("reason") or "")
    success = bool(result.get("success"))
    metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
    payload: JsonObject = {
        "success": success,
        "reason": reason,
        "canRetry": bool(result.get("canRetry")),
        "nextSuggestion": result.get("nextSuggestion"),
        "complete": _result_complete(result),
        "traceRef": trace_ref,
    }
    summary = _metrics_summary(tool_name, reason, metrics)
    if summary:
        payload["summary"] = summary
    return payload


def _result_complete(result: JsonObject) -> bool | None:
    value = result.get("complete")
    if isinstance(value, bool):
        return value
    for cursor_key in ("next", "nextStart", "next_start"):
        if result.get(cursor_key) is not None:
            return False
    metrics = result.get("metrics")
    if isinstance(metrics, dict):
        value = metrics.get("complete")
        if isinstance(value, bool):
            return value
        if metrics.get("truncated") is True:
            return False
        for cursor_key in ("next", "nextStart", "next_start"):
            if metrics.get(cursor_key) is not None:
                return False
        uncertainty = metrics.get("uncertainty")
        if isinstance(uncertainty, list) and uncertainty:
            return False
    return True


def _metrics_summary(tool_name: str, reason: str, metrics: dict[str, object]) -> JsonObject:
    allowed_keys = (
        "item",
        "target_count",
        "before_count",
        "after_count",
        "current_count",
        "collected_delta",
        "remaining_count",
        "candidates_tried",
        "skipped_count",
        "resume_hint",
        "count",
        "radius",
        "limit",
        "truncated",
        "pages_read",
        "total_matches",
        "target",
        "pos",
        "final_pos",
        "goal",
        "distance",
        "final_distance",
        "missing",
        "health",
        "food",
        "oxygen",
        "dimension",
        "inventory_hash",
        "error_type",
        "reflex_handoff",
    )
    summary: JsonObject = {}
    for key in allowed_keys:
        if key in metrics:
            summary[key] = _bounded_summary_value(metrics[key])
    if "skipped" in metrics and isinstance(metrics["skipped"], list):
        skipped = metrics["skipped"]
        summary["skipped_count"] = len(skipped)
        summary["skipped_reasons"] = _top_reasons(skipped)
    if "attempts" in metrics and isinstance(metrics["attempts"], list):
        summary["attempt_count"] = len(metrics["attempts"])
    if "blocks" in metrics and isinstance(metrics["blocks"], list):
        summary["block_count"] = len(metrics["blocks"])
    if "entities" in metrics and isinstance(metrics["entities"], list):
        summary["entity_count"] = len(metrics["entities"])
    if "deltas" in metrics and isinstance(metrics["deltas"], dict):
        summary["deltas"] = {str(k): v for k, v in list(metrics["deltas"].items())[:8]}
    if "uncertainty" in metrics:
        summary["uncertainty"] = _bounded_summary_value(metrics["uncertainty"])
    if isinstance(metrics.get("reflex"), dict):
        reflex = metrics["reflex"]
        summary["reflex"] = {
            key: _bounded_summary_value(reflex[key])
            for key in (
                "kind",
                "escaped_hazard",
                "target_is_dry_stand",
                "final_is_dry_stand",
                "target",
                "final_pos",
                "target_block",
                "target_below",
                "dist_to_escape",
            )
            if key in reflex
        }
    if isinstance(metrics.get("clearance"), dict):
        clearance = metrics["clearance"]
        clearance_metrics = clearance.get("metrics") if isinstance(clearance.get("metrics"), dict) else {}
        legality = clearance_metrics.get("legality") if isinstance(clearance_metrics.get("legality"), dict) else {}
        summary["clearance"] = {
            "reason": clearance.get("reason"),
            "block_type": clearance_metrics.get("block_type"),
            "target": _bounded_summary_value(clearance_metrics.get("target")),
            "stand_block": _bounded_summary_value(
                (clearance_metrics.get("collect_approach_clearance") or {}).get("stand_block")
                if isinstance(clearance_metrics.get("collect_approach_clearance"), dict)
                else None
            ),
            "legality_reason": legality.get("reason"),
        }
    if not summary and reason:
        summary["tool"] = tool_name
    return summary


def _tool_result_summary(result: JsonObject) -> JsonObject:
    metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
    return _metrics_summary("tool", str(result.get("reason") or ""), metrics)


def _tool_result_summaries(results: list[dict[str, Any]]) -> list[JsonObject]:
    out: list[JsonObject] = []
    for item in results[-6:]:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "tool": item.get("tool"),
                "success": item.get("success"),
                "reason": item.get("reason"),
                "summary": item.get("summary"),
            }
        )
    return out


def _should_continue_collect(result: JsonObject, metrics: dict[str, object]) -> bool:
    if not bool(result.get("success")):
        return False
    if metrics.get("complete") is True:
        return False
    if str(metrics.get("resume_hint") or "") != "reselect_candidates":
        return False
    if str(result.get("reason") or "") not in {
        "partial_budget_exhausted",
        "partial_candidate_targets_exhausted",
        "candidate_targets_exhausted",
    }:
        return False
    collected_delta = int(metrics.get("collected_delta") or 0)
    remaining_count = int(metrics.get("remaining_count") or 0)
    after_count = int(metrics.get("after_count") or 0)
    before_count = int(metrics.get("before_count") or 0)
    return collected_delta > 0 and after_count > before_count and remaining_count > 0


def _continuation_constraints(metrics: dict[str, object]) -> dict[str, object]:
    budget = metrics.get("budget")
    if not isinstance(budget, dict):
        return {}
    out: dict[str, object] = {}
    for key in ("max_candidates", "max_mutating_calls", "max_wall_s"):
        value = budget.get(key)
        if isinstance(value, (int, float)) and value > 0:
            out[key] = value
    return out


def _continue_collect_resource_tool(
    tool: RegisteredTool,
    result: JsonObject,
    context: RuntimeRunContext,
    *,
    tool_call_id: str,
) -> JsonObject:
    if tool.name != "collect_resource":
        return result
    trace = context.trace
    current = result
    iterations = 0
    while iterations < 8:
        metrics = current.get("metrics") if isinstance(current, dict) else None
        if not isinstance(metrics, dict) or not _should_continue_collect(current, metrics):
            return current
        item = str(metrics.get("requested_item") or metrics.get("item") or "")
        target_count = int(metrics.get("target_count") or 0)
        after_count = int(metrics.get("after_count") or 0)
        if not item or target_count <= 0 or after_count <= 0:
            return current
        params: JsonObject = {
            "item": item,
            "count": max(1, target_count - after_count),
            "constraints": _continuation_constraints(metrics),
        }
        iterations += 1
        if trace is not None:
            trace.emit(
                "tool_continuation",
                tool=tool.name,
                tool_call_id=tool_call_id,
                iteration=iterations,
                reason="collect_partial_progress",
                item=item,
                target_count=target_count,
                current_count=after_count,
                arguments_summary=_summarize_tool_arguments(json.dumps(params, sort_keys=True)),
            )
        current = execute_tool(tool, params, context.weld_context)
        if trace is not None:
            trace.emit(
                "tool_continuation_result",
                tool=tool.name,
                tool_call_id=tool_call_id,
                iteration=iterations,
                reason=str(current.get("reason") or ""),
                success=bool(current.get("success")),
                summary=_tool_result_summary(current),
            )
    if trace is not None:
        trace.emit(
            "tool_continuation_ceiling",
            tool=tool.name,
            tool_call_id=tool_call_id,
            iteration_limit=8,
            reason=str(current.get("reason") or ""),
        )
    return current


def _recent_session_messages(context: AgentContext, *, limit: int = 3) -> list[JsonObject]:
    return [
        {"role": role, "content": _shorten(content, limit=300)}
        for role, content in context.session_messages()[-limit:]
    ]


def _bounded_summary_value(value: object) -> object:
    if isinstance(value, dict):
        out: JsonObject = {}
        for key, item in list(value.items())[:12]:
            if isinstance(item, (dict, list, tuple)):
                out[str(key)] = _bounded_summary_value(item)
            else:
                out[str(key)] = item
        return out
    if isinstance(value, (list, tuple)):
        if len(value) <= 8 and all(not isinstance(item, (dict, list, tuple)) for item in value):
            return list(value)
        return {
            "count": len(value),
            "sample": [_bounded_summary_value(item) for item in list(value)[:3]],
        }
    if isinstance(value, str):
        return _shorten(value, limit=300)
    return value


def _top_reasons(items: list[object]) -> list[str]:
    counts: dict[str, int] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        reason = str(item.get("reason") or "")
        if reason:
            counts[reason] = counts.get(reason, 0) + 1
    return [f"{reason}:{count}" for reason, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:6]]


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
        max_turns: int | None = None,
        tool_facts: dict[str, dict[str, object]] | None = None,
        trace: RuntimeTrace | None = None,
        recovery_handler: RecoveryHandler | None = None,
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
        self.recovery_handler = recovery_handler
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
        self.last_known_body_state: dict[str, object] | None = None
        self.consecutive_transport_errors = 0

    def set_tool_facts(self, tool_name: str, facts: dict[str, object]) -> None:
        self.tool_facts[tool_name] = dict(facts)

    async def run_turn(self, extra_signals: list[AgentSignal] | None = None) -> AgentTurnOutcome:
        self._ensure_active()
        self.agent_context.begin_turn()

        state = self.body.get_state()
        events = self.body.poll_events()
        self._remember_body_state(state)
        self.trace.emit(
            "body_state",
            bot=state.bot,
            pos=list(state.pos),
            health=state.health,
            food=state.food,
            oxygen=state.oxygen,
            inventory_hash=state.inventory_hash,
            dimension=state.dimension,
            complete=state.complete,
            missing=state.missing,
        )
        self.trace.emit(
            "body_events",
            count=len(events),
            names=[event.name for event in events],
            seqs=[event.seq for event in events],
        )
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
            effort=profile.effort,
            policy_tags=list(profile.policy_tags),
            context_frame=profile.context_frame,
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
            runtime=self,
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
        except BodyRecoveryRequired as exc:
            return self._enter_recovery_from_body_fact(exc.reason, exc.facts)
        except MaxTurnsExceeded as exc:
            return self._yield_from_runaway_ceiling(exc)
        except UserError as exc:
            progress_abort = _find_progress_abort(exc)
            if progress_abort is None:
                recovery_required = _find_body_recovery_required(exc)
                if recovery_required is not None:
                    return self._enter_recovery_from_body_fact(recovery_required.reason, recovery_required.facts)
                raise
            return self._yield_from_progress_abort(progress_abort)

        self.trace.emit("turn_completed", lifecycle=self.lifecycle.state.value, situational=profile.situational)
        self._reset_transport_errors()
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
            if event.get("event") in {"assistant_message", "assistant_final_output"}:
                content = event.get("content")
                if isinstance(content, str) and content.strip():
                    self.agent_context.observe_assistant_message(content)
        has_content = any(
            event.get("event") in {"assistant_message", "assistant_final_output"} and event.get("content")
            for event in extracted
        )
        has_tool_call = any(event.get("event") == "model_tool_call" for event in extracted)
        if has_tool_call and not has_content:
            self.trace.emit("assistant_no_content_tool_only")

    def remember_tool_result(self, tool_name: str, result: JsonObject) -> None:
        self.last_tool_results.append(
            {
                "tool": tool_name,
                "success": bool(result.get("success")),
                "reason": str(result.get("reason") or ""),
                "summary": _tool_result_summary(result),
            }
        )
        if len(self.last_tool_results) > 12:
            del self.last_tool_results[: len(self.last_tool_results) - 12]

    def _remember_body_state(self, state: Any) -> None:
        if getattr(state, "missing", False):
            return
        self.last_known_body_state = {
            "bot": getattr(state, "bot", None),
            "pos": list(getattr(state, "pos", ())),
            "yaw": getattr(state, "yaw", None),
            "pitch": getattr(state, "pitch", None),
            "health": getattr(state, "health", None),
            "food": getattr(state, "food", None),
            "oxygen": getattr(state, "oxygen", None),
            "dimension": getattr(state, "dimension", None),
            "inventory_hash": getattr(state, "inventory_hash", None),
        }

    def remember_tool_body_facts(self, result: JsonObject) -> None:
        metrics = result.get("metrics") if isinstance(result, dict) else None
        if not isinstance(metrics, dict):
            return
        pos = metrics.get("pos")
        if not (isinstance(pos, list) and len(pos) == 3):
            return
        if metrics.get("missing") is True:
            return
        previous = dict(self.last_known_body_state or {})
        previous.update(
            {
                "bot": metrics.get("bot", previous.get("bot")),
                "pos": list(pos),
                "yaw": metrics.get("yaw", previous.get("yaw")),
                "pitch": metrics.get("pitch", previous.get("pitch")),
                "health": metrics.get("health", previous.get("health")),
                "food": metrics.get("food", previous.get("food")),
                "oxygen": metrics.get("oxygen", previous.get("oxygen")),
                "dimension": metrics.get("dimension", previous.get("dimension")),
                "inventory_hash": metrics.get("inventory_hash", previous.get("inventory_hash")),
            }
        )
        self.last_known_body_state = previous

    def _yield_from_progress_abort(self, exc: ProgressAbort) -> AgentTurnOutcome:
        facts = exc.facts or self.authority.facts(self.agent_context.goal_text)
        return self._yield_with_facts(
            facts,
            trace_event="progress_yielded",
            message=_yield_message(facts, self.agent_context.goal_text),
        )

    def _yield_from_runaway_ceiling(self, exc: MaxTurnsExceeded) -> AgentTurnOutcome:
        facts = self.authority.facts(self.agent_context.goal_text)
        self.trace.emit(
            "runaway_ceiling_hit",
            error_type=type(exc).__name__,
            error_message=str(exc),
            sdk_max_turns=self.max_turns,
        )
        return self._yield_with_facts(
            facts,
            trace_event="runaway_ceiling_yielded",
            message=_runaway_yield_message(facts, self.agent_context.goal_text, self.max_turns),
        )

    def _enter_recovery_from_body_fact(self, reason: str, facts: dict[str, object] | None = None) -> AgentTurnOutcome:
        payload = dict(facts or {})
        signal = AgentSignal.death_detected(reason, **payload)
        reduction = self.mode_runtime.reduce([signal], self.lifecycle.state, goal_text=self.agent_context.goal_text)
        self._apply_lifecycle_request(reduction.requested_lifecycle)
        profile = self.mode_runtime.profile_for(self.lifecycle.state)
        self.agent_context.observe_profile(profile)
        self.authority.invalidate_generation(f"body_recovery:{reason}")
        self.trace.emit(
            "body_recovery_required",
            reason=reason,
            facts=payload,
            lifecycle=self.lifecycle.state.value,
            situational=profile.situational,
        )
        return AgentTurnOutcome(
            status="stopped",
            lifecycle=self.lifecycle.state,
            profile=profile,
            message=reason,
        )

    def record_transport_error(self, tool_name: str, result: JsonObject, *, tool_call_id: str) -> None:
        self.consecutive_transport_errors += 1
        metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
        self.trace.emit(
            "body_transport_error",
            tool=tool_name,
            tool_call_id=tool_call_id,
            count=self.consecutive_transport_errors,
            threshold=BODY_TRANSPORT_RECOVERY_LIMIT,
            error_type=metrics.get("error_type"),
            reason=str(result.get("reason") or ""),
            await_diagnostics=metrics.get("await_diagnostics"),
        )
        if self.consecutive_transport_errors >= BODY_TRANSPORT_RECOVERY_LIMIT:
            facts = self.authority.facts(self.agent_context.goal_text)
            facts.recent_events.append(
                "body_transport_unstable:"
                f"tool={tool_name}:"
                f"count={self.consecutive_transport_errors}:"
                f"error_type={metrics.get('error_type')}:"
                f"reason={str(result.get('reason') or '')}"
            )
            raise ProgressAbort(
                "body transport unstable: yielding for supervisor review",
                facts=facts,
            )

    def _reset_transport_errors(self) -> None:
        if self.consecutive_transport_errors:
            self.trace.emit("body_transport_recovered", count=self.consecutive_transport_errors)
        self.consecutive_transport_errors = 0

    def _yield_with_facts(
        self,
        facts: ProgressFacts,
        *,
        trace_event: str,
        message: str,
    ) -> AgentTurnOutcome:
        yielded = self.mode_runtime.reduce(
            [AgentSignal.progress_abort(facts)],
            self.lifecycle.state,
            goal_text=self.agent_context.goal_text,
        )
        self._apply_lifecycle_request(yielded.requested_lifecycle)
        yielded_profile = self.mode_runtime.profile_for(self.lifecycle.state)
        self.agent_context.observe_profile(yielded_profile)
        self.trace.emit(
            trace_event,
            stagnant_steps=facts.stagnant_steps,
            stalled_steps=facts.stalled_steps,
            failure_steps=facts.failure_steps,
            recent_events=list(facts.recent_events),
            lifecycle=self.lifecycle.state.value,
            situational=yielded_profile.situational,
        )
        return AgentTurnOutcome(
            status="yielded",
            lifecycle=self.lifecycle.state,
            profile=yielded_profile,
            yielded_facts=facts,
            message=message,
        )


def _yield_message(facts: ProgressFacts, goal_text: str) -> str:
    recent = ""
    if facts.recent_events:
        recent = "\nrecent_events=" + "; ".join(facts.recent_events[-3:])
    return (
        "Progress authority yielded.\n"
        f"GOAL: {goal_text}\n"
        f"stagnant={facts.stagnant_steps} stalled={facts.stalled_steps} "
        f"failures={facts.failure_steps}{recent}\n"
        "How should I continue?"
    )


def _runaway_yield_message(facts: ProgressFacts, goal_text: str, max_turns: int | None) -> str:
    ceiling = "the SDK runaway ceiling" if max_turns is None else f"the SDK runaway ceiling ({max_turns})"
    return (
        f"Autonomous run yielded after hitting {ceiling}.\n"
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


def _find_body_recovery_required(exc: BaseException) -> BodyRecoveryRequired | None:
    seen: set[int] = set()
    cursor: BaseException | None = exc
    while cursor is not None and id(cursor) not in seen:
        seen.add(id(cursor))
        if isinstance(cursor, BodyRecoveryRequired):
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
    new_items = getattr(result, "new_items", None)
    if isinstance(new_items, list) and new_items:
        extracted = _extract_observations_from_new_items(new_items)
        if extracted:
            events.extend(extracted)
            final_output = getattr(result, "final_output", None)
            if final_output not in {None, ""}:
                final_text = _shorten(_public_text(final_output), limit=2000)
                if not any(
                    event.get("event") == "assistant_final_output" and event.get("content") == final_text
                    for event in events
                ):
                    events.append({"event": "assistant_final_output", "content": final_text})
            return events
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


def extract_model_response_observations(response: Any) -> list[dict[str, object]]:
    output = getattr(response, "output", None)
    if not isinstance(output, list):
        return []
    events: list[dict[str, object]] = []
    assistant_texts: set[str] = set()
    for item in output:
        item_type = _public_text(getattr(item, "type", None) or "")
        if item_type == "message":
            content = _text_from_raw_message(item)
            if content:
                shortened = _shorten(content, limit=2000)
                if shortened not in assistant_texts:
                    assistant_texts.add(shortened)
                    events.append({"event": "assistant_message", "content": shortened})
            continue
        if item_type in {"function_call", "tool_call"}:
            name = getattr(item, "name", None) or getattr(item, "tool_name", None) or item_type
            arguments = getattr(item, "arguments", None)
            events.append(
                {
                    "event": "model_tool_call",
                    "tool": _public_text(name),
                    "arguments_summary": _summarize_tool_arguments(arguments),
                }
            )
            continue
        if item_type in {"function_call_output", "tool_output"}:
            output_value = getattr(item, "output", None)
            events.append({"event": "model_tool_output", "summary": _shorten(_public_text(output_value), limit=500)})
    if any(event["event"] == "model_tool_call" for event in events) and not any(
        event["event"] == "assistant_message" and event.get("content") for event in events
    ):
        events.append({"event": "assistant_no_content_tool_only"})
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
    return _summarize_tool_arguments(raw)


def _tool_arguments_summary_from_json(input_json: str) -> str | None:
    if not input_json:
        return None
    try:
        parsed = json.loads(input_json)
    except json.JSONDecodeError:
        return _shorten(input_json, limit=500)
    return _summarize_tool_arguments(parsed)


def _extract_observations_from_new_items(new_items: list[Any]) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    assistant_texts: set[str] = set()
    for item in new_items:
        if isinstance(item, MessageOutputItem):
            content = ItemHelpers.text_message_output(item) or _text_from_raw_message(getattr(item, "raw_item", None))
            if content:
                shortened = _shorten(content, limit=2000)
                if shortened not in assistant_texts:
                    assistant_texts.add(shortened)
                    events.append({"event": "assistant_message", "content": shortened})
            continue
        if isinstance(item, ToolCallItem):
            tool_name = item.tool_name or item.type
            raw = item.raw_item
            arguments = getattr(raw, "arguments", None) if not isinstance(raw, dict) else raw.get("arguments")
            events.append(
                {
                    "event": "model_tool_call",
                    "tool": _public_text(tool_name),
                    "arguments_summary": _summarize_tool_arguments(arguments),
                }
            )
            continue
        if isinstance(item, ToolCallOutputItem):
            events.append(
                {
                    "event": "model_tool_output",
                    "summary": _shorten(_public_text(item.output), limit=500),
                }
            )
    return events


def _text_from_raw_message(raw_item: object) -> str:
    content = getattr(raw_item, "content", None)
    if content is None and isinstance(raw_item, dict):
        content = raw_item.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
            continue
        text = None
        if isinstance(part, dict):
            text = part.get("text") or part.get("content")
        else:
            text = getattr(part, "text", None) or getattr(part, "content", None)
        if text is not None:
            parts.append(_public_text(text))
    return "\n".join(part for part in parts if part)


def _summarize_tool_arguments(raw: object) -> str | None:
    if raw is None:
        return None
    try:
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                return _shorten(raw, limit=500)
            return _shorten(json.dumps(sanitize_observation(parsed), ensure_ascii=True, sort_keys=True), limit=500)
        if isinstance(raw, (dict, list, tuple)):
            return _shorten(json.dumps(sanitize_observation(raw), ensure_ascii=True, sort_keys=True), limit=500)
        return _shorten(_public_text(sanitize_observation(raw)), limit=500)
    except Exception:
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
