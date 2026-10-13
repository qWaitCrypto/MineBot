"""openai-agents binding for the Phase-1 runtime spine."""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import os
import contextlib
import json
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from concurrent.futures import CancelledError as FutureCancelledError
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any
from uuid import uuid4
from types import SimpleNamespace

from jsonschema import Draft202012Validator
from agents import Agent, RunConfig, RunContextWrapper, Runner, RunHooks, Session
from agents.run_config import ModelInputData
from openai.types.shared import Reasoning
from agents.exceptions import MaxTurnsExceeded, UserError
from agents.items import ItemHelpers, MessageOutputItem, ToolCallItem, ToolCallOutputItem
from agents.tool import FunctionTool

from minebot.app.conversation import (
    WindowedConversationSession,
    bounded_model_input,
    bounded_session_input,
    model_input_chars,
)
from minebot.app.prompt_cache import current_context_item, is_current_context_item
# Spine modules extracted from this file (framework §12 H1/B). They are
# re-exported here so every existing consumer keeps importing one runtime
# surface; the split is about auditability, not a new public boundary.
from minebot.app.execution_lane import (
    EXECUTION_LANE_CANCEL_TIMEOUT_S,
    EXECUTION_LANE_POLL_S,
    BodyRecoveryRequired,
    SerialExecutionLane,
    ToolExecutionTimeout,
)
from minebot.app.body_control import BodyControl
from minebot.app.decision_client import DecisionClient
from minebot.app.progress_epoch_adapter import (
    ProgressEpochAdapter,
    _collapsed_epoch_progress_steps,
    _explicit_evidence_keys,
    _model_function_calls,
    _ModelFunctionCall,
    _ProgressEpoch,
    _ProgressEpochMember,
)
from minebot.app.context_compaction import (
    DEFAULT_COMPACT_THRESHOLD_TOKENS,
    SUMMARY_INSTRUCTIONS,
    ConversationCompactor,
    MAX_ITEM_CHARS,
    bound_item,
    item_fingerprint,
)
from minebot.app.execution_snapshot import ExecutionSnapshots
from minebot.app.runtime_trace import RuntimeTrace
from minebot.app.model_provider import ModelProviderRegistry, is_retryable_model_error
from minebot.app.observation_artifacts import ToolObservationArchive
from minebot.app.progress_epochs import ContinuationEpochBudget, ProgressEpochArchive
from minebot.app.skills import SkillOperationError
from minebot.app.observability import ObservationSink, sanitize_observation
from minebot.app.projection import (
    bounded_summary_value as _bounded_summary_value,
    without_ambient_state,
    projection_values_equal as _projection_values_equal,
    shorten as _shorten,
    top_reasons as _top_reasons,
)
from minebot.brain.context import AgentContext
from minebot.brain.deliberation import (
    DEFAULT_ROUTING_POLICY,
    DecisionContext,
    RouteSpec,
    RoutingPolicy,
    decision_context,
    resolve_route,
)
from minebot.brain.lifecycle import LifecycleController, LifecycleError, LifecycleState
from minebot.brain.modes import (
    AgentSignal,
    ModeRuntime,
    RuntimeProfile,
    mobility_reason_from_tool_results,
    signalize_body_state,
    signalize_events,
    survival_reason_from_tool_results,
)
from minebot.brain.progress import ProgressAuthority, ProgressStep, material_progress_evidence, progress_snapshot
from minebot.brain.registry import (
    ObservationProjector,
    RegisteredTool,
    ToolRegistry,
    WeldContext,
    execute_tool,
)
from minebot.contract import (
    Body,
    BodyState,
    ExecutionCancellation,
    ExecutionCancelled,
    JsonObject,
    ProgressAbort,
    ProgressFacts,
    ProgressStopReason,
    REVIEWABLE_STOP_REASONS,
    ToolResult,
    dominant_progress_abort,
    execution_cancellation_scope,
    is_governance_decision_required,
    is_partial_progress,
)
from minebot.contract.tool_trace import (
    canonical_args_hash_from_json as _shared_canonical_args_hash_from_json,
    canonical_tool_arguments_from_json as _shared_canonical_tool_arguments_from_json,
    tool_tactic_signature_from_json as _shared_tool_tactic_signature_from_json,
)
from minebot.game.errors import (
    ActionReconciliationUnknownError,
    BodyActionTimeoutError,
    BodyProtocolError,
)

RunnerCallable = Callable[..., Awaitable[Any]]
StreamingRunnerCallable = Callable[..., Any]
RecoveryHandler = Callable[["AgentRuntime"], Any]
BODY_TRANSPORT_RECOVERY_LIMIT = 3
EXECUTION_LANE_POLL_S = 0.01
EXECUTION_LANE_CANCEL_TIMEOUT_S = 30.0
BODY_OWNER_SETTLE_POLL_S = 0.10
BODY_WATCH_POLL_S = 0.25
STREAM_CANCEL_DRAIN_TIMEOUT_S = EXECUTION_LANE_CANCEL_TIMEOUT_S + 5.0
MODEL_COUNT_MAP_LIMIT = 48


def _being_cancelled() -> bool:
    """Whether the running task itself was asked to stop, as opposed to
    something it awaited having been cancelled underneath it."""
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


def _execution_outcome(result: Any) -> tuple[str, str | None]:
    """Snapshot terminal state and reason of one finished Body operation."""
    payload = result if isinstance(result, dict) else getattr(result, "result", None)
    if not isinstance(payload, dict):
        error = getattr(result, "error", None)
        return ("failed", type(error).__name__) if error is not None else ("unknown", None)
    reason = str(payload.get("reason") or "") or None
    if payload.get("success"):
        return "succeeded", reason
    return ("cancelled" if reason in {"cancelled", "preempted"} else "failed"), reason


# A direct Body call that has not settled after this many seconds stops holding
# the model turn; unset/empty keeps the default, "off" keeps calls synchronous.
DIRECT_CALL_WINDOW_ENV = "MINEBOT_DIRECT_CALL_WINDOW_S"
DEFAULT_DIRECT_CALL_WINDOW_S = 30.0
UNDELIVERED_RESULT_MAX_AGE_S = 900.0
SUMMARY_MAX_OUTPUT_TOKENS = 8_000
# How long a finished background call stays in CURRENT_CONTEXT for the turn that was running.


def direct_call_window_from_env(env: Mapping[str, str] | None = None) -> float | None:
    raw = (os.environ if env is None else env).get(DIRECT_CALL_WINDOW_ENV, "").strip().lower()
    if not raw:
        return DEFAULT_DIRECT_CALL_WINDOW_S
    if raw in {"off", "none", "false", "0"}:
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{DIRECT_CALL_WINDOW_ENV} must be a number of seconds or 'off'") from exc
    if value <= 0:
        raise ValueError(f"{DIRECT_CALL_WINDOW_ENV} must be positive or 'off'")
    return value


@dataclass
class BackgroundCall:
    """A direct Body call whose claim and work outlived the turn that issued it.

    It is still the same ``agent_call`` claim under the same ``tool_call_id``;
    only the model turn stopped waiting. The Body allows one such call at a time.
    """

    tool_call_id: str
    tool: str
    arguments: JsonObject
    task: "asyncio.Future[JsonObject]"
    started_at: float = field(default_factory=time.monotonic)

    def payload(self) -> JsonObject:
        return {
            "kind": "direct_call",
            "tool_call_id": self.tool_call_id,
            "tool": self.tool,
            "arguments": _bounded_summary_value(self.arguments),
            "status": "running",
            "elapsed_s": round(time.monotonic() - self.started_at, 1),
        }

    def receipt(self) -> JsonObject:
        return {
            "success": True,
            "reason": "running_in_background",
            "complete": False,
            "canRetry": False,
            "nextSuggestion": (
                "This is not the result. The Body keeps working on this call and its real result "
                "arrives later as a REFLEX_RESULT message for the same tool_call_id. Do not assume "
                "it succeeded and do not repeat the call."
            ),
            "metrics": {"background": self.payload()},
        }


@dataclass
class RuntimeRunContext:
    agent_context: AgentContext
    weld_context: WeldContext
    profile: RuntimeProfile
    tool_facts: dict[str, dict[str, object]] = field(default_factory=dict)
    trace: "RuntimeTrace | None" = None
    runtime: "AgentRuntime | None" = None
    instruction_preamble: str = ""
    body_actions_allowed: bool = True
    progress_epochs: "ProgressEpochAdapter | None" = None

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
    progress_stop_reason: ProgressStopReason | None = None


@dataclass(frozen=True)
class RecoveryOutcome:
    """App-layer recovery driver result consumed by AgentSession."""

    success: bool
    reason: str
    facts: dict[str, object] = field(default_factory=dict)
    can_retry: bool = False


class ModelTurnTransportError(RuntimeError):
    """A transient model failure that must resume without replaying the turn."""

    def __init__(self, error_type: str) -> None:
        super().__init__(f"transient model transport failure: {error_type}")
        self.error_type = error_type


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
        observations = extract_model_response_observations(response)
        runtime = getattr(getattr(context, "context", None), "runtime", None)
        if trace is not None:
            trace.emit("llm_end", agent=getattr(agent, "name", None), response_type=type(response).__name__)
            usage = model_usage_observation(getattr(response, "usage", None))
            if usage is not None:
                trace.emit("model_usage", **usage)
                runtime = getattr(getattr(context, "context", None), "runtime", None)
                compactor = getattr(runtime, "conversation_compactor", None)
                if compactor is not None:
                    compactor.note_usage(int(usage["usage"]["input_tokens"]))
                layout = getattr(getattr(agent, "model", None), "last_request_layout", None)
                if layout is None and runtime is not None and getattr(runtime, "model_provider", None) is not None:
                    try:
                        layout = getattr(runtime.model_provider.resolve(runtime.model_provider.default),
                                         "last_request_layout", None)
                    except Exception:
                        layout = None
                if layout:
                    # Which part of the request repeated the previous one.
                    trace.emit("prompt_cache_layout", **layout)
            for event in observations:
                trace.emit(**event)
        progress_epochs = getattr(getattr(context, "context", None), "progress_epochs", None)
        if progress_epochs is not None:
            await progress_epochs.open(response)

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


INTENT_DESCRIPTION = (
    "The result this call should achieve, not what you are thinking. "
    "Good: 'have 4 logs in the inventory'. Bad: 'I will try the trees first'."
)


def agent_input_schema(tool: RegisteredTool, *, required: bool = True):
    """The argument schema the Agent sees for a tool.

    A call that changes the Body also says what it is for.  The Agent's
    schema requires that field, ``run_tool`` refuses the call without it,
    and it is stripped before the Body callback sees the arguments.
    """
    schema = copy.deepcopy(tool.input_schema)
    if not isinstance(schema, dict):
        return schema
    properties = schema.setdefault("properties", {})
    if not isinstance(properties, dict):
        return schema
    if not tool.sidecar.can_mutate_body:
        # A read needs no intent, but saying one is not an error.
        if not required:
            properties.setdefault("intent", {"type": "string"})
        return schema
    properties.setdefault("intent", {"type": "string", "description": INTENT_DESCRIPTION})
    names = schema.setdefault("required", [])
    if required and isinstance(names, list) and "intent" not in names:
        names.append("intent")
    return schema


def tool_invoker_for(tool: RegisteredTool):
    validator = Draft202012Validator(agent_input_schema(tool, required=False))

    async def on_invoke_tool(ctx: RunContextWrapper[RuntimeRunContext], input_json: str) -> JsonObject:
        trace = ctx.context.trace
        runtime = getattr(ctx.context, "runtime", None)
        native_tool_call_id = getattr(ctx, "tool_call_id", None)
        epoch_adapter = ctx.context.progress_epochs
        epoch_member = (
            None
            if epoch_adapter is None
            else epoch_adapter.claim_member(
                tool.name,
                input_json,
                native_tool_call_id=(
                    str(native_tool_call_id)
                    if isinstance(native_tool_call_id, str) and native_tool_call_id
                    else None
                ),
            )
        )
        tool_call_id = (
            str(native_tool_call_id)
            if isinstance(native_tool_call_id, str) and native_tool_call_id
            else (
                epoch_member.tool_call_id
                if epoch_member is not None
                else f"tool-{uuid4()}-{tool.name}"
            )
        )
        arguments_summary = _tool_arguments_summary_from_json(input_json)
        args_hash = _canonical_args_hash_from_json(input_json)
        tactic_signature = _tool_tactic_signature_from_json(tool.name, input_json)
        strategy_epoch_condition = (
            epoch_member.strategy_epoch_condition
            if epoch_member is not None and tool.sidecar.can_mutate_body
            else None
        )

        post_state: BodyState | None = None
        # True once this call's epoch member has been settled with a background
        # receipt; the real result then arrives outside the model turn.
        epoch_closed = False

        def finalize(
            result: JsonObject,
            *,
            progress_steps: tuple[ProgressStep, ...] = (),
            status: str | None = None,
            pending_abort: ProgressAbort | None = None,
            raise_progress_abort: bool = True,
        ) -> JsonObject:
            if status == "background":
                # Not the call's final receipt: keep the archive slot and the
                # tool_result event for the result that arrives later.
                model_result = _model_tool_payload(tool.name, result, trace_ref=tool_call_id)
                if trace is not None:
                    trace.emit("tool_background", tool_call_id=tool_call_id, tool=tool.name,
                               args_hash=args_hash, model_result=model_result)
            else:
                model_result = _finalize_tool_payload(
                    tool=tool,
                    result=result,
                    trace=trace,
                    tool_call_id=tool_call_id,
                    runtime=runtime,
                    args_hash=args_hash,
                    tactic_signature=tactic_signature,
                )
            if epoch_adapter is not None and epoch_member is not None and not epoch_closed:
                abort = epoch_adapter.settle(
                    epoch_member,
                    result=result,
                    model_result=model_result,
                    progress_steps=progress_steps,
                    status=status,
                    pending_abort=pending_abort,
                    post_state=post_state,
                )
                if abort is not None and raise_progress_abort:
                    if abort is not epoch_adapter.reviewable_abort:
                        raise abort
                    model_result["executionStop"] = {
                        "reason": abort.stop_reason.value,
                        "scope": "work_closed_task_tools_only",
                        "nextSuggestion": (
                            "Work is stopped. Use the existing evidence to update the task plan "
                            "and checkpoint wait_event or yield. No work tool can execute, "
                            "and a continue checkpoint does not renew execution permission."
                        ),
                    }
            return model_result

        if trace is not None:
            trace.emit(
                "tool_decision_context",
                tool_call_id=tool_call_id,
                sdk_tool_call_id_native=tool_call_id == native_tool_call_id,
                sdk_tool_call_id_predeclared=epoch_member is not None,
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
                args_hash=args_hash,
                tactic_signature=tactic_signature,
                arguments_summary=arguments_summary,
                strategy_epoch_condition=strategy_epoch_condition,
            )
            if strategy_epoch_condition is not None:
                trace.emit(
                    "strategy_epoch",
                    tool_call_id=tool_call_id,
                    tool=tool.name,
                    condition=strategy_epoch_condition,
                )
        if (
            epoch_adapter is not None and epoch_adapter.work_stop is not None
            and not (tool.sidecar.source == "agent.task" and not tool.sidecar.can_mutate_body)
        ):
            if epoch_member is None:
                raise ProgressAbort("stopped work has no declared epoch", stop_reason=ProgressStopReason.UNKNOWN)
            return finalize({
                "success": False, "reason": "progress_work_closed", "canRetry": False,
                "nextSuggestion": "Use task tools to record a plan and wait or yield; work remains stopped.",
                "metrics": {"progress_stop_reason": epoch_adapter.work_stop.stop_reason.value},
            }, status="progress_denied")
        if (
            epoch_adapter is not None and epoch_adapter.budget is not None
            and not (tool.sidecar.source == "agent.task" and not tool.sidecar.can_mutate_body)
            and (epoch_member is None or epoch_member.budget_denied)
        ):
            if epoch_member is None:
                raise ProgressAbort(
                    "continuation work has no declared progress epoch",
                    facts=ctx.context.weld_context.authority.facts(ctx.context.agent_context.goal_text),
                    stop_reason=ProgressStopReason.UNKNOWN,
                )
            return finalize({
                "success": False,
                "reason": "continuation_work_budget_closed",
                "canRetry": False,
                "nextSuggestion": (
                    "The incoming continuation is exhausted or no longer current. "
                    "Use task tools to inspect the task, revise the plan, and close a checkpoint. "
                    "This run cannot execute more work under that continuation."
                ),
                "metrics": {"budget_checkpoint_id": epoch_adapter.budget.checkpoint_id},
            }, status="budget_denied")
        if epoch_member is not None and epoch_member.conflict:
            result = epoch_adapter.conflict_result(epoch_member)
            if trace is not None:
                trace.emit(
                    "tool_policy_denied",
                    tool_call_id=tool_call_id,
                    tool=tool.name,
                    reason="body_batch_conflict",
                    policy="progress_epoch_single_body_writer",
                )
            return finalize(
                result,
                progress_steps=epoch_adapter.rejection_steps(
                    epoch_member,
                    reason="body_batch_conflict",
                ),
                status="rejected",
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
            return finalize(
                result,
                progress_steps=(
                    ()
                    if epoch_adapter is None or epoch_member is None
                    else epoch_adapter.rejection_steps(epoch_member, reason="invalid_tool_json")
                ),
                status="rejected",
            )
        if not isinstance(params, dict):
            result = {
                "success": False,
                "reason": "invalid_tool_input",
                "canRetry": False,
                "nextSuggestion": None,
                "metrics": {"expected": "object"},
            }
            return finalize(
                result,
                progress_steps=(
                    ()
                    if epoch_adapter is None or epoch_member is None
                    else epoch_adapter.rejection_steps(epoch_member, reason="invalid_tool_input")
                ),
                status="rejected",
            )
        invalid = next(validator.iter_errors(params), None)
        if invalid is not None:
            return finalize({
                "success": False, "reason": "invalid_tool_input", "canRetry": True,
                "metrics": {"path": list(invalid.absolute_path), "error": invalid.message[:400]},
            }, status="rejected")
        if not tool.sidecar.can_mutate_body and isinstance(params, dict):
            params.pop("intent", None)
        if not ctx.context.body_actions_allowed and tool.sidecar.can_mutate_body:
            result = {
                "success": False,
                "reason": "body_action_denied_during_maintenance",
                "canRetry": False,
                "nextSuggestion": (
                    "Use memory, Skill, Wiki, archive, or read-only observation tools, "
                    "then finish the maintenance turn."
                ),
                "metrics": {
                    "policy": "maintenance_read_only_body",
                    "tool": tool.name,
                    "body_scope": list(tool.sidecar.body_scope),
                },
            }
            if trace is not None:
                trace.emit(
                    "tool_policy_denied",
                    tool_call_id=tool_call_id,
                    tool=tool.name,
                    reason=result["reason"],
                    policy="maintenance_read_only_body",
                )
            return finalize(
                result,
                progress_steps=(
                    ()
                    if epoch_adapter is None or epoch_member is None
                    else epoch_adapter.rejection_steps(
                        epoch_member,
                        reason="body_action_denied_during_maintenance",
                    )
                ),
                status="rejected",
            )
        progress_steps: tuple[ProgressStep, ...] = ()
        pending_abort: ProgressAbort | None = None
        settlement_status: str | None = None

        async def execute() -> JsonObject:
            nonlocal progress_steps, pending_abort, settlement_status, post_state
            try:
                if runtime is not None and epoch_member is not None:
                    captured = await runtime.run_tool(tool,
                        _capture_tool_execution,
                        tool,
                        params,
                        ctx.context,
                        timeout_s=tool.sidecar.timeout_s,
                        tool_call_id=tool_call_id,
                    )
                    if isinstance(captured, _CapturedToolExecution):
                        progress_steps = captured.progress_steps
                        post_state = captured.post_state
                        if captured.error is not None:
                            raise captured.error
                        assert captured.result is not None
                        result = captured.result
                    else:
                        # Admission rejection and a cooperative Decision stop
                        # return the runtime's terminal payload without a
                        # completed callback capture. Preserve that receipt;
                        # neither path proves captured progress or post-state.
                        result = captured
                elif runtime is None:
                    result = execute_tool(tool, params, ctx.context.weld_context)
                else:
                    result = await runtime.run_tool(tool,
                        execute_tool,
                        tool,
                        params,
                        ctx.context.weld_context,
                        timeout_s=tool.sidecar.timeout_s,
                        tool_call_id=tool_call_id,
                    )
            except asyncio.CancelledError:
                if epoch_closed:
                    # Detached: the stop entry that cancelled us owns settlement.
                    raise
                if runtime is not None and (not getattr(runtime, "native_responses", False) or tool.sidecar.can_mutate_body):
                    await runtime.cancel_active_execution(f"tool_cancelled:{tool.name}")
                if epoch_adapter is not None and epoch_member is not None:
                    epoch_adapter.cancel_member(epoch_member, "tool_cancelled")
                raise
            except ProgressAbort as exc:
                if epoch_adapter is None or epoch_member is None:
                    raise
                pending_abort = exc
                result = {
                    "success": False,
                    "reason": "progress_yielded",
                    "canRetry": True,
                    "nextSuggestion": None,
                    "metrics": _progress_abort_metrics(exc),
                }
            except BodyRecoveryRequired as exc:
                owner = runtime.live_body_owner() if runtime is not None and exc.reason == "body_busy" else None
                if owner is not None:
                    # A live plan or background call holds the Body. That is an
                    # ordinary refusal the Agent can act on, not a fault to recover.
                    return {
                        "success": False,
                        "reason": "body_busy",
                        "canRetry": True,
                        "nextSuggestion": (
                            "The Body is still doing earlier work. Its result arrives as a later "
                            "message; use read_reflex to inspect it, or control_reflex to stop it "
                            "before issuing another Body action."
                        ),
                        "metrics": {"owner": owner},
                    }
                if epoch_adapter is not None and epoch_member is not None:
                    result = {
                        "success": False,
                        "reason": exc.reason,
                        "canRetry": True,
                        "nextSuggestion": None,
                        "metrics": dict(exc.facts),
                    }
                    finalize(
                        result,
                        progress_steps=progress_steps,
                        status="body_recovery",
                        raise_progress_abort=False,
                    )
                raise
            except Exception as exc:
                cancellation_reason: str | None = None
                cancellation_facts: JsonObject | None = None
                if isinstance(exc, (ToolExecutionTimeout, BodyActionTimeoutError)):
                    settlement_status = "timeout"
                    if runtime is not None and (not getattr(runtime, "native_responses", False) or tool.sidecar.can_mutate_body):
                        cancellation_reason = _tool_timeout_cancellation_reason(tool.name, exc)
                        cancellation_facts = await runtime.cancel_active_execution_with_facts(
                            cancellation_reason
                        )
                result = _tool_exception_payload(exc)
                if cancellation_facts is not None:
                    metrics = result.get("metrics")
                    if isinstance(metrics, dict):
                        metrics["orphan_cleanup"] = cancellation_facts
                    if cancellation_facts.get("owner_observed") and not cancellation_facts.get("settled"):
                        result["canRetry"] = False
                        result["nextSuggestion"] = (
                            "Body owner cleanup did not settle; do not start another Body action "
                            "until lifecycle recovery clears the owner."
                        )
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
                # An action-level reconciliation unknown is still caused by a
                # transport boundary, so it must invalidate the generation and
                # participate in the existing bounded transport recovery. Keep a
                # distinct public reason so the Agent can observe that the socket
                # recovered without proving whether the mutation landed.
                if result.get("reason") in {"transport_error", "action_reconciliation_unknown"}:
                    ctx.context.weld_context.authority.invalidate_generation(
                        f"{result['reason']}:{tool.name}"
                    )
                    ctx.context.trace and ctx.context.trace.emit(
                        "tool_transport_recovery_candidate",
                        tool_call_id=tool_call_id,
                        tool=tool.name,
                        reason=result["reason"],
                        error_type=type(exc).__name__,
                        await_diagnostics=result.get("metrics", {}).get("await_diagnostics"),
                    )
                    if runtime is not None:
                        pending_abort = runtime.record_transport_error(
                            tool.name,
                            result,
                            tool_call_id=tool_call_id,
                            raise_on_limit=epoch_member is None,
                        )
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
                finalize(
                    result,
                    progress_steps=progress_steps,
                    status="body_recovery",
                    pending_abort=pending_abort,
                    raise_progress_abort=False,
                )
                raise BodyRecoveryRequired(_recovery_reason_from_tool_result(result, facts), facts=facts)
            return result

        def settle(result: JsonObject) -> JsonObject:
            """Record one real result and build what the model reads."""
            status = settlement_status
            if status is None and is_governance_decision_required(
                str(result.get("reason") or ""),
                result.get("metrics") if isinstance(result.get("metrics"), dict) else None,
            ):
                status = "decision_required"
            if status is None and is_partial_progress(
                str(result.get("reason") or ""),
                result.get("metrics") if isinstance(result.get("metrics"), dict) else None,
            ):
                status = "partial"
            if runtime is not None:
                runtime.remember_tool_result(tool.name, result, run_context=ctx.context)
                runtime.remember_tool_body_facts(result, trace=trace)
                if tool.sidecar.body_scope and result.get("reason") not in {
                    "transport_error",
                    "action_reconciliation_unknown",
                }:
                    # The recovery limit is a consecutive-failure guard, not a
                    # lifetime error budget.  A successful authoritative Body
                    # boundary proves the transport recovered and lets a later,
                    # unrelated socket fault start a fresh bounded streak.
                    runtime.note_transport_success()
            return finalize(
                result,
                progress_steps=progress_steps,
                status=status,
                pending_abort=pending_abort,
            )

        window_s = runtime.direct_call_window_s(tool) if runtime is not None else None
        if window_s is None:
            return settle(await execute())

        # The Body claim outlives the model turn (decision-layer §8.4): a call
        # that settles inside the window is answered synchronously; a longer
        # one keeps its claim and running work, and this turn gets a receipt.
        task = asyncio.ensure_future(execute())
        try:
            finished, _ = await asyncio.wait({task}, timeout=window_s)
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        if finished:
            return settle(task.result())

        pre_state = epoch_adapter.pre_body_state if epoch_adapter is not None and epoch_member is not None else None
        call = runtime.adopt_background_call(tool_call_id, tool.name, params, task)

        def deliver(done: "asyncio.Future[JsonObject]") -> None:
            runtime.release_background_call(call)
            if done.cancelled():
                return
            try:
                error = done.exception()
                if error is None:
                    result = done.result()
                    runtime.commit_background_progress(
                        progress_steps, pre_state, post_state, success=bool(result.get("success")),
                        tool_call_id=tool_call_id,
                    )
                    model_result = settle(result)
                    if pending_abort is not None:
                        model_result["progressStop"] = pending_abort.stop_reason.value
                elif isinstance(error, BodyRecoveryRequired):
                    model_result = {"success": False, "reason": error.reason, "canRetry": True,
                                    "metrics": dict(error.facts)}
                else:
                    model_result = _tool_exception_payload(error) if isinstance(error, Exception) else {
                        "success": False, "reason": "cancelled", "canRetry": True}
                    if trace is not None:
                        trace.emit("tool_exception", tool_call_id=tool_call_id, tool=tool.name,
                                   error_type=type(error).__name__, reason=model_result["reason"])
                runtime.deliver_background_result(call, model_result)
            except Exception as exc:
                runtime.trace.emit("background_call_delivery_failed", tool_call_id=tool_call_id,
                                   tool=tool.name, error_type=type(exc).__name__, message=str(exc)[:300])

        task.add_done_callback(deliver)
        try:
            return finalize(call.receipt(), status="background")
        finally:
            epoch_closed = True

    return on_invoke_tool


def sdk_tool_for(tool: RegisteredTool) -> FunctionTool:
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
        params_json_schema=agent_input_schema(tool),
        on_invoke_tool=tool_invoker_for(tool),
        strict_json_schema=False,
        is_enabled=is_enabled,
        timeout_seconds=None,
        _failure_error_function=None,
        _use_default_failure_error_function=False,
    )


@dataclass(frozen=True)
class _CapturedToolExecution:
    result: JsonObject | None
    progress_steps: tuple[ProgressStep, ...]
    error: Exception | None = None
    post_state: BodyState | None = None


def _capture_tool_execution(
    tool: RegisteredTool,
    params: JsonObject,
    context: RuntimeRunContext,
) -> _CapturedToolExecution:
    with context.weld_context.authority.capture_steps() as captured:
        try:
            result = execute_tool(tool, params, context.weld_context)
        except Exception as exc:
            return _CapturedToolExecution(None, tuple(captured), exc)
    try:
        post_state = progress_snapshot(context.weld_context.body.get_state())
    except Exception as exc:
        post_state = None
        if context.trace is not None:
            context.trace.emit("progress_post_state_unavailable", tool=tool.name, error_type=type(exc).__name__)
    return _CapturedToolExecution(result, tuple(captured), post_state=post_state)


def _finalize_tool_payload(
    *,
    tool: RegisteredTool,
    result: JsonObject,
    trace: RuntimeTrace | None,
    tool_call_id: str,
    runtime: "AgentRuntime | None" = None,
    args_hash: str | None = None,
    tactic_signature: str | None = None,
) -> JsonObject:
    observation_handle = _persist_tool_observation(
        runtime,
        tool_name=tool.name,
        tool_call_id=tool_call_id,
        result=result,
    )
    model_result = _model_tool_payload(
        tool.name,
        result,
        trace_ref=tool_call_id,
        observation_handle=observation_handle,
        projector=tool.projector,
    )
    if trace is not None:
        trace.emit(
            "tool_result",
            tool_call_id=tool_call_id,
            tool=tool.name,
            args_hash=args_hash,
            tactic_signature=tactic_signature,
            reason=str(result.get("reason")),
            success=bool(result.get("success")),
            full_result=result,
            model_result=model_result,
            observation_handle=observation_handle,
            projection_complete=model_result["projection"]["complete"],
            omitted_field_count=model_result["projection"]["omittedFieldCount"],
        )
    return model_result


def _persist_tool_observation(
    runtime: "AgentRuntime | None",
    *,
    tool_name: str,
    tool_call_id: str,
    result: JsonObject,
) -> str | None:
    if runtime is None or runtime.observation_archive is None:
        return None
    try:
        return runtime.observation_archive.store(
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            result=result,
            complete=_result_complete(result),
        )
    except Exception as exc:
        runtime.trace.emit(
            "tool_observation_archive_failed",
            tool=tool_name,
            tool_call_id=tool_call_id,
            error_type=type(exc).__name__,
        )
        return None


def _tool_exception_payload(exc: Exception) -> JsonObject:
    if isinstance(exc, ToolExecutionTimeout):
        reason = "tool_timeout"
    elif isinstance(exc, BodyActionTimeoutError):
        reason = "body_action_timeout"
    elif isinstance(exc, ActionReconciliationUnknownError):
        reason = "action_reconciliation_unknown"
    else:
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
        "nextSuggestion": _tool_exception_next_suggestion(reason),
        "metrics": metrics,
    }


def _transport_error_incident_key(
    result: JsonObject,
    *,
    tool_call_id: str,
) -> str:
    """Identify one physical transport incident across composition wrappers.

    Action-level reconciliation diagnostics carry the immutable Body action
    id, which is the correct identity when the same exception is rethrown by
    nested composition tools.  Generic transport failures have no shared
    physical id, so retain the tool call id and let each call count normally.
    """

    metrics = result.get("metrics")
    if isinstance(metrics, dict):
        diagnostics = metrics.get("await_diagnostics")
        if isinstance(diagnostics, dict):
            action_id = diagnostics.get("action_id")
            if isinstance(action_id, str) and action_id:
                return f"action:{action_id}:{result.get('reason') or 'transport_error'}"
        error_type = metrics.get("error_type")
    else:
        error_type = None
    return f"call:{tool_call_id}:{error_type or 'transport_error'}:{result.get('reason') or 'transport_error'}"


def _tool_timeout_cancellation_reason(tool_name: str, exc: TimeoutError) -> str:
    if isinstance(exc, BodyActionTimeoutError):
        action_id = exc.diagnostics.get("action_id")
        if isinstance(action_id, str) and action_id:
            return f"body_action_timeout:{tool_name}:action_id={_shorten(action_id, limit=96)}"
        return f"body_action_timeout:{tool_name}"
    return f"tool_timeout:{tool_name}"


def _tool_exception_next_suggestion(reason: str) -> str:
    if reason == "tool_timeout":
        return "Refresh state before retrying; the timed-out Body action was interrupted."
    if reason == "body_action_timeout":
        return (
            "Refresh Body state before retrying; server-owner cleanup was requested "
            "for the timed-out action."
        )
    if reason == "action_reconciliation_unknown":
        return (
            "The Body transport recovered but the mutation outcome is unknown; "
            "observe authoritative state or replan before mutating again."
        )
    return "retry after refreshing state; choose a different action if the same failure repeats"


def _progress_abort_metrics(exc: ProgressAbort) -> JsonObject:
    facts = exc.facts
    if facts is None:
        return {"progress_stop_reason": exc.stop_reason.value}
    return {
        "progress_stop_reason": exc.stop_reason.value,
        "stagnant_steps": facts.stagnant_steps,
        "stalled_steps": facts.stalled_steps,
        "failure_steps": facts.failure_steps,
        "epistemic_steps": facts.epistemic_steps,
        "last_epistemic_keys": list(facts.last_epistemic_keys),
        "last_fingerprint": facts.last_fingerprint,
        "current_fingerprint": facts.current_fingerprint,
        "recent_events": list(facts.recent_events),
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
    } or normalized.startswith((
        "death",
        "missingbody",
        "bodymissing",
        "bodytransport",
    ))


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


def _model_tool_payload(
    tool_name: str,
    result: JsonObject,
    *,
    trace_ref: str,
    observation_handle: str | None = None,
    projector: ObservationProjector | None = None,
) -> JsonObject:
    reason = str(result.get("reason") or "")
    success = bool(result.get("success"))
    metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
    decision_required = is_governance_decision_required(reason, metrics)
    partial = is_partial_progress(reason, metrics)
    payload: JsonObject = {
        "success": success,
        "reason": reason,
        "canRetry": bool(result.get("canRetry")),
        "nextSuggestion": result.get("nextSuggestion"),
        "complete": _result_complete(result),
        "traceRef": trace_ref,
    }
    if decision_required:
        payload["outcome"] = "decision_required"
    elif partial:
        payload["outcome"] = "partial"
    # The stored result keeps everything. What the model reads leaves out the
    # client's state frame wherever an action reply carried a copy of it.
    shown = without_ambient_state(metrics)
    summary = _metrics_summary(tool_name, reason, shown, projector=projector)
    if summary:
        payload["summary"] = summary
    if observation_handle is not None:
        payload["observationHandle"] = observation_handle
    # A supervised stop is a decision result, not ambient Body metrics. Keep
    # the finite choice and the bounded facts beside the projected tool result
    # so the Agent can act on why this call was handed back without polling
    # ``read_reflex`` or guessing from ``preempted`` alone.
    handoff = metrics.get("direct_call_handoff")
    if isinstance(handoff, dict):
        payload["supervision"] = handoff
    payload["projection"] = _projection_metadata(
        shown,
        summary,
        observation_handle=observation_handle,
    )
    return payload


def _projection_metadata(
    metrics: dict[str, object],
    summary: JsonObject,
    *,
    observation_handle: str | None,
) -> JsonObject:
    omitted: list[str] = []
    omitted_counts: JsonObject = {}
    for key, value in metrics.items():
        projected = summary.get(str(key), _MISSING_PROJECTION_VALUE)
        if projected is not _MISSING_PROJECTION_VALUE and _projection_values_equal(value, projected):
            continue
        path = f"metrics.{key}"
        omitted.append(path)
        count = _projection_value_count(value, projected)
        if count is not None:
            omitted_counts[path] = count
    visible_omissions = omitted[:32]
    projection: JsonObject = {
        "complete": not omitted,
        "queryable": observation_handle is not None,
        "omittedFieldCount": len(omitted),
        "omittedFields": visible_omissions,
    }
    if len(visible_omissions) < len(omitted):
        projection["omittedFieldsComplete"] = False
    else:
        projection["omittedFieldsComplete"] = True
    if omitted_counts:
        projection["omittedValueCounts"] = omitted_counts
    return projection


_MISSING_PROJECTION_VALUE = object()


def _projection_value_count(source: object, projected: object) -> int | None:
    if isinstance(source, (list, tuple)):
        if isinstance(projected, list):
            return max(0, len(source) - len(projected))
        if isinstance(projected, dict) and isinstance(projected.get("sample"), list):
            return max(0, len(source) - len(projected["sample"]))
        return len(source)
    if isinstance(source, dict):
        if isinstance(projected, dict):
            return max(0, len(source) - len(projected))
        return len(source)
    if isinstance(source, str) and isinstance(projected, str):
        return max(0, len(source) - len(projected))
    return None


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


def _metrics_summary(
    tool_name: str,
    reason: str,
    metrics: dict[str, object],
    *,
    projector: ObservationProjector | None = None,
) -> JsonObject:
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
        "governance_confirmation",
    )
    summary: JsonObject = {}
    if projector is not None:
        projected = projector(reason, metrics)
        if isinstance(projected, dict):
            summary.update(projected)
    for key in allowed_keys:
        if key in metrics:
            summary[key] = _bounded_summary_value(metrics[key])
    # An item-count map is the answer of an inventory or state read. The
    # generic bound keeps twelve entries, which would drop most of a real
    # inventory, so these maps have their own limit and say when it applies.
    for counts_key in ("counts", "inventory_counts"):
        if not isinstance(metrics.get(counts_key), dict):
            continue
        count_items = sorted(metrics[counts_key].items(), key=lambda item: str(item[0]))
        visible_items = count_items[:MODEL_COUNT_MAP_LIMIT]
        summary[counts_key] = {
            str(key): _bounded_summary_value(value)
            for key, value in visible_items
        }
        prefix = "" if counts_key == "counts" else f"{counts_key}_"
        summary[f"{prefix}distinct_item_count"] = len(count_items)
        summary[f"{prefix}counts_complete"] = len(visible_items) == len(count_items)
        if len(visible_items) < len(count_items):
            summary[f"{prefix}omitted_item_count"] = len(count_items) - len(visible_items)
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
    _include_generic_small_metric_facts(summary, metrics)
    if not summary and reason:
        summary["tool"] = tool_name
    return summary


def _include_generic_small_metric_facts(
    summary: JsonObject,
    metrics: dict[str, object],
) -> None:
    for raw_key, value in metrics.items():
        key = str(raw_key)
        if key in summary:
            continue
        safe_mapping = sanitize_observation({key: value})
        if not isinstance(safe_mapping, dict):
            continue
        safe_value = safe_mapping.get(key)
        if safe_value == "<redacted>":
            continue
        # A small map is a fact and is shown as it is; bounding it first used
        # to drop any map with more than a dozen entries entirely. A list of
        # records is per-step detail and stays behind the handle.
        # Long text (a document body, a log) stays behind it too.
        if isinstance(safe_value, (list, tuple, str)) and not _projection_values_equal(
                safe_value, _bounded_summary_value(safe_value)):
            continue
        encoded = json.dumps(safe_value, ensure_ascii=False, sort_keys=True, default=str)
        if len(encoded) <= 1200:
            summary[key] = safe_value


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


def _recent_session_messages(context: AgentContext, *, limit: int = 3) -> list[JsonObject]:
    return [
        {"role": role, "content": _shorten(content, limit=300)}
        for role, content in context.session_messages()[-limit:]
    ]


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
        runner_run_streamed: StreamingRunnerCallable | None = None,
        agent_name: str = "MineBot",
        max_turns: int | None = None,
        tool_facts: dict[str, dict[str, object]] | None = None,
        trace: RuntimeTrace | None = None,
        recovery_handler: RecoveryHandler | None = None,
        speech_sink: Callable[[str], None] | None = None,
        conversation_session: Session | None = None,
        observation_archive: ToolObservationArchive | None = None,
        progress_epoch_archive: ProgressEpochArchive | None = None,
        progress_task_id: Callable[[], str | None] | None = None,
        routing_policy: RoutingPolicy | None = None,
    ) -> None:
        self.body = body
        self.registry = registry
        self.agent_context = agent_context
        self.lifecycle = lifecycle
        self.mode_runtime = mode_runtime
        self.authority = authority
        self.model_provider = model_provider
        self.runner_run: RunnerCallable = runner_run or Runner.run
        self.runner_run_streamed = runner_run_streamed if runner_run is not None else (runner_run_streamed or Runner.run_streamed)
        self.max_turns = max_turns
        self.tool_facts: dict[str, dict[str, object]] = tool_facts or {}
        self.trace = trace or RuntimeTrace()
        self.recovery_handler = recovery_handler
        self.speech_sink = speech_sink
        self.conversation_session = (
            conversation_session
            if conversation_session is not None
            else WindowedConversationSession(session_id=f"{agent_name}-{uuid4()}")
        )
        self.observation_archive = observation_archive
        self.progress_epoch_archive = progress_epoch_archive
        self.progress_task_id = progress_task_id
        self._progress_task_initialized = False
        self._progress_task_owner: str | None = None
        self._progress_restore_abort: ProgressAbort | None = None
        self._progress_work_stop: ProgressAbort | None = None
        # Set per turn from the admitted continuation: the stop epoch whose
        # review was bought with one non-renewable task allowance.
        self._turn_review_grant: str | None = None
        self._run_evidence_cursor = 0
        self.hooks = RuntimeHooks()
        self.weld_context = WeldContext(
            body=body,
            authority=authority,
            goal_text=agent_context.goal_text,
            body_state_observer=self._trace_mutation_body_state,
            raw_result_observer=self._save_raw_tool_result,
        )
        self.agent = Agent[RuntimeRunContext](
            name=agent_name,
            tools=[sdk_tool_for(self.registry.get(name)) for name in self.registry.model_names()],
            instructions=self._instructions,
            model="primary",
        )
        # F1 deliberation economy (brain-cognitive-framework.md §4). With no
        # routing_policy the classification is telemetry-only and the identity
        # route keeps model selection and the "full" context profile exactly
        # as before; a provided policy is the explicit per-class opt-in.
        self.routing_policy = routing_policy
        self._current_decision_context: DecisionContext | None = None
        self._current_route: RouteSpec | None = None
        self._turn_intent_kind: str | None = None
        self._turn_has_durable_goal: bool | None = None
        self.last_tool_results: list[dict[str, Any]] = []
        self._pending_mobility_terminal: dict[str, object] | None = None
        self.last_known_body_state: dict[str, object] | None = None
        self._last_traced_body_event_seq = 0
        self.direct_call_handoffs = {}
        # One physical Body claim for direct calls and plans.  This replaces
        # the old boolean: an unknown stop is a state that must be recovered
        # by native confirmation, never by elapsed time.
        self.body_control = BodyControl(authority)
        self._body_stop_lock = asyncio.Lock()
        self._responses_turn_lock = asyncio.Lock()
        from minebot.app.body_loop import BodyLoop
        self.decision_client = DecisionClient(emit=self.trace.emit)
        from minebot.app.panel_telemetry import PanelTelemetry
        self.panel_telemetry = PanelTelemetry(self.trace)
        # One supervisor covers direct Agent calls and work-plan call nodes.
        # Keep the old attribute as a compatibility alias for slice-1 tools.
        # One lifecycle owner for all SDK Body calls.  ``call_supervisor`` is
        # retained as the compatibility name used by older integrations.
        self.body_loop = BodyLoop(self)
        self.call_supervisor = self.body_loop
        self.direct_build_supervisor = self.call_supervisor
        self.reflex_manager = None
        self.background_call: BackgroundCall | None = None
        self.execution_snapshots = ExecutionSnapshots()
        # Finished work whose result has not reached the conversation yet.
        self._undelivered_results: dict[str, JsonObject] = {}
        threshold = getattr(model_provider, "compact_threshold_for", None)
        self.conversation_compactor = ConversationCompactor(
            summarize=self._summarize_conversation,
            emit=self.trace.emit,
            threshold_tokens=threshold() if callable(threshold) else DEFAULT_COMPACT_THRESHOLD_TOKENS,
        )
        self.direct_call_window = direct_call_window_from_env()
        self._body_event_trace_lock = threading.Lock()
        self.consecutive_transport_errors = 0
        # A single Body dispatch can surface through several composition
        # layers (for example collect_block_domain -> collect_resource ->
        # ensure_tool_for).  Count physical transport incidents, not each
        # wrapper's copy of the same exception.
        self._transport_error_incidents: set[str] = set()
        self.execution_lane = SerialExecutionLane(thread_name=f"minebot-{agent_name}")
        self.context_refreshers: list[Callable[[AgentContext], None]] = []
        self.responses_kernel = None
        self._reflex_wakeup_task: asyncio.Task[Any] | None = None
        self._sdk_reflex_fact_listener: Callable[[dict[str, Any]], None] | None = None
        self._sdk_body_event_listener: Callable[[Any], None] | None = None
        self._pending_body_death: dict[str, Any] | None = None
        self._native_context = None
        self.native_task_workspace = None
        self.native_task_outcome = None
        self.native_plan = None
        self._read_lane = SerialExecutionLane(thread_name=f"minebot-read-{agent_name}")
        self.event_wait_mode = False
        self.native_responses = runner_run is None and getattr(model_provider, "kernel", "sdk") == "responses"
        if self.native_responses:
            from minebot.app.responses_kernel import ResponsesKernel
            client, model, settings, timeout = model_provider.responses_connection_config()
            self.responses_kernel = ResponsesKernel(
                client=client, model=model, settings=settings, request_timeout_s=timeout,
                tools=self._responses_tools, instructions=self._responses_instructions,
                context=self._responses_context,
                invoke=self._invoke_responses_tool, emit=self.trace.emit, speak=self._responses_speak,
                needs_work=lambda: (not self.event_wait_mode)
                and bool(self.agent_context.goal_text.strip())
                and self.native_task_outcome is None,
                proxy=getattr(model_provider, "responses_proxy", None),
                transport=getattr(model_provider, "responses_transport", "websocket"),
                tool_namespace=getattr(model_provider, "responses_tool_namespace", None),
                is_mutating=lambda name: name in self.registry and self.registry.get(name).sidecar.can_mutate_body,
            )

    def _responses_tools(self) -> list[dict[str, Any]]:
        ctx = self._native_context
        if ctx is None:
            return []
        definitions = []
        for name in self.registry.model_names():
            tool = self.registry.get(name)
            if tool.sidecar.source == "agent.task":
                continue
            if not tool_is_enabled(tool.sidecar, ctx.profile, ctx.facts_for_tool(name)):
                continue
            if tool.sidecar.can_mutate_body and not ctx.body_actions_allowed:
                continue
            definition = {"type": "function", "name": name, "description": tool.description,
                          "parameters": agent_input_schema(tool), "strict": False}
            if tool.sidecar.can_mutate_body:
                definition["async"] = True
            definitions.append(definition)
        definitions.append({
            "type": "function", "name": "take_control",
            "description": "Stop this session's current direct Body call now. Waits for real native stop; does not undo completed work.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False}, "strict": True,
        })
        if self.agent_context.goal_text.strip():
            definitions.extend([
                {"type": "function", "name": "update_plan", "description": "Keep a short current plan, including player additions, relevant coordinates, concrete blockers and why you changed strategy. Update it when evidence or player requests change.",
                 "parameters": {"type": "object", "properties": {"summary": {"type": "string"}, "steps": {"type": "array", "items": {"type": "string"}}}, "required": ["summary", "steps"], "additionalProperties": False}, "strict": True},
                {"type": "function", "name": "finish_task", "description": "Close this goal after checking real results, or report the concrete reason it failed. Running body actions must settle first.",
                 "parameters": {"type": "object", "properties": {"status": {"type": "string", "enum": ["complete", "blocked"]}, "summary": {"type": "string"}, "evidence": {"type": "array", "items": {"type": "string"}}}, "required": ["status", "summary", "evidence"], "additionalProperties": False}, "strict": True},
            ])
        return definitions

    async def _responses_instructions(self) -> str:
        # Cacheable rules are independent of position, inventory, plans and
        # observations. Live facts belong at the end of the native input.
        parts = [
            self.agent_context.system_prompt,
            "CONTINUOUS_EXECUTION: Use asynchronous calls for long Body tools so independent chat can continue. "
            "Ordinary chat and the end of a model response do not stop physical work. "
            "Tool results automatically resume the conversation when ready. Do not issue a competing body write. "
            "Finish actionable goals by executing and checking real results, not by only describing the next steps. "
            "Only report success supported by tool/state evidence; report a concrete blocker on failure.",
            "CURRENT_CONTEXT snapshots are read before requests; the latest snapshot supersedes older current-state facts. "
            "Historical tool observations describe the state when observed, not the current inventory or position. "
            "Use observationHandle with observation tools to recover omitted details when needed. "
            "When a current snapshot already answers a question, use it to select the next action; reread only when new evidence is needed.",
        ]
        if self.reflex_manager is not None:
            parts.append(
                "REFLEX_PLANS: When a goal has multiple dependent physical steps, a long-running physical "
                "action followed by later actions, a condition that may remain pending, or work that may need "
                "pause, resume, cancellation, or revision, delegate one minebot-work/0.2 JSON plan so the "
                "runtime owns sequencing, waiting, control, and authoritative terminal state. The tree is a "
                "range of allowed work, not a perfect script. For ‘do this until enough’, wrap a call or choice "
                "in repeat and give one natural-language ending condition; Decision walks it one step at a time. "
                "For one short independent observation or physical action, a direct tool is appropriate. Choose "
                "the plan structure from the goal and evidence, put the steps already known into one plan, and "
                "do not delegate merely to follow a fixed order. After delegating, do other planning; do not poll "
                "or wait for progress because the runtime delivers terminal facts asynchronously. "
                "Use only call, sequence, batch, choice and wait; write concrete intent and preserve complex Body "
                "arguments. Conditions may use requires as AND. Read and revise the annotated tree with read_reflex "
                "and control_reflex; never claim _runtime results yourself. interrupt_when is deferred."
            )
        return "\n\n".join(parts)

    async def _responses_context(self) -> str:
        # Reads have their own worker: a long write must not freeze context.
        state = await self._read_lane.run(self.body.get_state)
        self._remember_body_state(state)
        self.agent_context.observe_state(state)
        self._refresh_dynamic_context()
        if self.native_task_workspace is not None:
            self.native_task_workspace.sync_context(self.agent_context)
        self.weld_context.goal_text = self.agent_context.goal_text
        # Native input already contains the conversation; do not repeat the
        # last eight messages in every request's instructions.
        preamble = self.agent_context.compile(include_session_messages=False, continuous_tools=True).text
        return "\n\n".join(filter(None, [
            preamble, self.agent_context.skill_preamble(),
            "BODY: " + json.dumps(self.last_known_body_state, ensure_ascii=False, sort_keys=True),
            "BODY_CONTROL: " + json.dumps(self.body_control.snapshot().to_payload(), ensure_ascii=False, sort_keys=True),
            "CURRENT_PLAN: " + json.dumps(self.native_plan, ensure_ascii=False) if self.native_plan else "",
            "TASK_OUTCOME: " + json.dumps(self.native_task_outcome, ensure_ascii=False) if self.native_task_outcome else "",
        ]))

    async def _invoke_responses_tool(self, name: str, arguments: str, call_id: str) -> JsonObject:
        ctx = self._native_context
        if ctx is None:
            raise RuntimeError("Native tool called outside its session")
        if name == "take_control":
            return await self.cancel_active_execution_with_facts("agent_take_control")
        if name in {"finish_task", "update_plan"}:
            params = json.loads(arguments)
            if name == "update_plan":
                plan = {"summary": str(params["summary"]), "steps": list(params["steps"])}
                if self.native_task_workspace is not None:
                    workspace = self.native_task_workspace
                    current = workspace.store.get_plan(workspace.current_task.task_id)
                    workspace.update_plan(expected_revision=0 if current is None else current.revision,
                        summary=plan["summary"], steps=[{"title": step} for step in plan["steps"]])
                else:
                    self.native_plan = plan
                return {"success": True, "reason": "plan_updated"}
            if any(c.mutating for c in self.responses_kernel.pending):
                return {"success": False, "reason": "body_work_pending", "canRetry": True}
            if params.get("status") not in {"complete", "blocked"} or not params.get("summary") or not params.get("evidence"):
                return {"success": False, "reason": "task_outcome_requires_actual_evidence", "canRetry": True}
            outcome = {key: params[key] for key in ("status", "summary", "evidence")}
            if self.native_task_workspace is not None:
                from minebot.app.tasks import CheckpointDisposition
                workspace = self.native_task_workspace
                workspace.checkpoint(expected_task_revision=workspace.current_task.revision,
                    disposition=CheckpointDisposition.COMPLETE if params["status"] == "complete" else CheckpointDisposition.YIELD,
                    summary=params["summary"], evidence=params["evidence"])
            self.native_task_outcome = outcome
            self.trace.emit("task_outcome", **outcome)
            return {"success": True, "reason": "task_outcome_recorded", "summary": outcome}
        tool = self.registry.get(name)
        if self.native_task_outcome is not None and tool.sidecar.can_mutate_body:
            return {"success": False, "reason": "task_finished", "canRetry": False}
        if not tool_is_enabled(tool.sidecar, ctx.profile, ctx.facts_for_tool(name)):
            return {"success": False, "reason": "tool_disabled", "canRetry": False}
        try:
            return await tool_invoker_for(tool)(SimpleNamespace(context=ctx, tool_call_id=call_id), arguments)
        except ProgressAbort as exc:
            return {"success": False, "reason": "progress_yielded", "canRetry": True,
                    "metrics": _progress_abort_metrics(exc)}
        except BodyRecoveryRequired as exc:
            return {"success": False, "reason": exc.reason, "canRetry": False, "metrics": exc.facts}

    def _responses_speak(self, text: str) -> None:
        self.trace.emit("assistant_message", content=text)
        self.agent_context.observe_assistant_message(text)
        if self.speech_sink:
            try:
                self.speech_sink(text)
            except Exception as exc:
                self.trace.emit("speech_sink_failed", error_type=type(exc).__name__)

    def steer_pending_input(self) -> None:
        if self.responses_kernel is None:
            return
        text, count = self.agent_context.pending_turn_input(fallback="")
        if count:
            # The model decides whether new input changes an already proposed
            # outcome; the runtime must allow the newly requested work.
            self.native_task_outcome = None
            self.responses_kernel.steer(text)
            self.agent_context.acknowledge_turn_input(count)

    def notify_reflex_fact(self, fact: dict[str, Any]) -> None:
        """Deliver plan completion to the active Agent channel.

        SDK sessions do not have a Responses kernel.  They install a listener
        owned by ``AgentSession`` so the fact becomes a queued outer-session
        turn instead of being silently discarded.

        ``notify_fact`` wakes a kernel that is already inside ``run``.  When a
        session has returned to its event-wait boundary there is no reader
        task left to observe that event, so start one bounded successor turn.
        The guard prevents a plan completion from racing an already active
        user/model turn.
        """
        kernel = self.responses_kernel
        if kernel is None:
            listener = getattr(self, "_sdk_reflex_fact_listener", None)
            if callable(listener):
                activity_id = str(fact.get("activity_id") or "unknown")
                result = fact.get("result") if isinstance(fact.get("result"), dict) else {}
                self.__dict__.setdefault("_undelivered_results", {})[activity_id] = {
                    key: value for key, value in {
                        "ended_monotonic": time.monotonic(),
                        "activity_id": activity_id,
                        "kind": fact.get("kind") or "plan",
                        "tool": fact.get("tool"),
                        "status": fact.get("status"),
                        "reason": result.get("reason"),
                    }.items() if value is not None
                }
                listener(dict(fact))
            return
        kernel.notify_fact(dict(fact))
        if not self.native_responses or not self.agent_context.goal_text.strip():
            return
        if bool(getattr(kernel, "is_running", False)):
            return
        wake_task = getattr(self, "_reflex_wakeup_task", None)
        if wake_task is not None and not wake_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        async def wake() -> None:
            try:
                # A plan fact is an execution event, not a user command. Keep
                # event-wait mode enabled so the kernel cannot manufacture a
                # mechanical "continue" turn after delivering it. The lock
                # also serializes this wake with a user turn that arrived at
                # the same time.
                lock = getattr(self, "_responses_turn_lock", None)
                if lock is None:
                    if not getattr(kernel, "has_facts", True) and not kernel.is_running:
                        return
                    await self._run_responses_turn([], body_actions_allowed=True)
                else:
                    async with lock:
                        if not getattr(kernel, "has_facts", True) and not kernel.is_running:
                            return
                        await self._run_responses_turn([], body_actions_allowed=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.trace.emit("reflex_wakeup_failed", error_type=type(exc).__name__, message=str(exc)[:300])

        self._reflex_wakeup_task = loop.create_task(wake())

    def set_sdk_reflex_fact_listener(
        self, listener: Callable[[dict[str, Any]], None] | None,
    ) -> None:
        """Attach the outer SDK session's reflex-result wake callback."""
        self._sdk_reflex_fact_listener = listener

    def set_sdk_body_event_listener(self, listener: Callable[[Any], None] | None) -> None:
        """Use the SDK session's existing BODY_EVENT/recovery queue."""
        self._sdk_body_event_listener = listener

    def notify_body_lifecycle_event(self, event: Any) -> None:
        from minebot.app.body_events import agent_event_data
        from minebot.contract import Event
        event = Event(seq=event.seq, tick=event.tick, bot=event.bot, name=event.name,
                      data=agent_event_data(event))
        if event.name == "death":
            self._pending_body_death = dict(event.data)
        listener = self._sdk_body_event_listener
        if callable(listener):
            listener(event)

    async def _run_responses_turn(self, extra_signals, *, body_actions_allowed: bool) -> AgentTurnOutcome:
        prepared = await self._read_lane.run(self._prepare_turn, extra_signals)
        if isinstance(prepared, AgentTurnOutcome):
            return prepared
        self._native_context = RuntimeRunContext(
            agent_context=self.agent_context, weld_context=self.weld_context,
            profile=prepared, tool_facts=self.tool_facts, trace=self.trace, runtime=self,
            body_actions_allowed=body_actions_allowed,
        )
        fact_pending = bool(getattr(self.responses_kernel, "has_facts", False))
        text, count = self.agent_context.pending_turn_input(
            fallback=("" if fact_pending else
                      ("Respond to the latest message." if not self.agent_context.goal_text else
                       "Continue the current goal from real results.")))
        # The kernel queues input before any await; a transport error is visible
        # rather than invoking the same tool again through an SDK fallback.
        self.agent_context.acknowledge_turn_input(count)
        self.responses_kernel.resume()
        try:
            result = await self.responses_kernel.run(text, max_responses=self.max_turns)
        except asyncio.CancelledError:
            self.trace.emit("turn_cancelled", kernel="responses", pending_calls=[c.call_id for c in self.responses_kernel.pending])
            raise
        except Exception as exc:
            self.trace.emit("responses_failed", error_type=type(exc).__name__, message=str(exc)[:500],
                            pending_calls=[c.call_id for c in self.responses_kernel.pending])
            self._raise_model_transport_error_if_retryable(exc)
            return AgentTurnOutcome("failed", self.lifecycle.state, prepared, message=f"{type(exc).__name__}: {str(exc)[:400]}")
        self.trace.emit("turn_completed", kernel="responses", lifecycle=self.lifecycle.state.value)
        return AgentTurnOutcome("completed_turn", self.lifecycle.state, prepared, result=result)

    def _save_raw_tool_result(self, tool_name, params, result):
        from minebot.contract.execution import current_tool_observation
        observation = current_tool_observation()
        self.trace.emit("tool_raw_result", tool=tool_name, arguments=params,
                        tool_call_id=observation[0] if observation else None,
                        raw_result=result.to_payload())

    async def run_tool(
        self,
        tool: RegisteredTool,
        callback: Callable[..., Any],
        *args,
        timeout_s=None,
        tool_call_id=None,
        claim_holder="agent_call",
        body_claim_id=None,
        retain_claim=False,
        return_execution_receipt=False,
        supervision_context: Mapping[str, object] | None = None,
    ):
        call_args = args
        call_intent = ""
        if len(args) > 1 and isinstance(args[1], dict):
            raw_arguments = dict(args[1])
            value = raw_arguments.pop("intent", None)
            if isinstance(value, str) and value.strip():
                call_intent = value.strip()[:500]
            if value is not None:
                call_args = (*args[:1], raw_arguments, *args[2:])
        if call_intent:
            supervision_context = dict(supervision_context or {})
            supervision_context.setdefault("intent", call_intent)
            supervision_context.setdefault("node_description", call_intent)
        if tool.sidecar.can_mutate_body and not call_intent and not (supervision_context or {}).get("intent"):
            # Agreed rule: a call that changes the Body says what it is for.
            return ToolResult(
                False, "missing_intent", False,
                next_suggestion="Add the intent field: the result this call should achieve.",
                metrics={"missing": "intent", "tool": tool.name},
            ).to_payload()
        claim_id = body_claim_id or tool_call_id or str(uuid4())
        claim_generation: int | None = None
        if tool.sidecar.can_mutate_body:
            control = self.body_control
            # Life actions may be interrupted immediately when an Agent call
            # arrives.  A threat action keeps the Body until it settles; an
            # Agent call waits only for its own timeout and then receives the
            # ordinary body_busy result.  Both paths use the existing stop and
            # claim records; no second admission lock is introduced.
            if claim_holder in {"agent_call", "agent_plan"}:
                snapshot = control.snapshot()
                if snapshot.holder == "life" and snapshot.stop_state == "running":
                    await self.cancel_active_execution_with_facts(
                        "agent_call_arrived", invalidate_generation=False, cancel_reflex=False,
                    )
                elif snapshot.holder == "threat" and snapshot.stop_state in {"running", "stopping"}:
                    deadline = time.monotonic() + max(0.0, float(timeout_s or 0.0))
                    self.trace.emit(
                        "body_call_waiting", tool_call_id=tool_call_id,
                        holder="threat", timeout_s=max(0.0, float(timeout_s or 0.0)),
                    )
                    while True:
                        snapshot = control.snapshot()
                        if snapshot.holder != "threat" or snapshot.stop_state not in {"running", "stopping"}:
                            break
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            # Protection still owns a healthy Body. Admission
                            # timed out; this is a receipt, not a recovery fault.
                            return ToolResult(
                                False, "body_busy", True,
                                metrics={"holder": snapshot.to_payload()},
                            ).to_payload()
                        await asyncio.sleep(min(0.05, remaining))
            if control.unknown:
                probe = getattr(self.body, "confirm_control_idle", None)
                native = None
                if callable(probe):
                    try:
                        native = await self._read_lane.run(probe)
                    except Exception as exc:
                        self.trace.emit("body_control_probe_failed", error_type=type(exc).__name__)
                if not isinstance(native, dict) or native.get("native_stopped") is not True:
                    raise BodyRecoveryRequired("body_control_unknown", facts={
                        "control_unknown": True,
                        "native_stopped": False,
                    })
                control.confirm_native_stopped(True)
            try:
                claim = control.claim(claim_id, holder=claim_holder)
                claim_generation = claim.generation
            except RuntimeError as exc:
                reason = str(exc)
                if reason == "body_control_unknown":
                    raise BodyRecoveryRequired(reason, facts={"control_unknown": True}) from exc
                raise BodyRecoveryRequired("body_busy", facts={"holder": control.snapshot().to_payload()}) from exc
        receipt: dict[str, object] = {}

        def readmit_callback():
            nonlocal claim_generation
            if claim_generation is not None:
                claim_generation = self.body_control.claim(claim_id, holder=claim_holder).generation
                self.trace.emit("body_call_readmitted", tool_call_id=tool_call_id,
                                generation=claim_generation, holder=claim_holder)

        def admitted_callback(*values):
            from minebot.contract.execution import body_claim_scope, execution_checkpoint, execution_receipt_scope
            execution_checkpoint()
            with execution_receipt_scope(receipt):
                if claim_generation is None:
                    return callback(*values)
                if not self.body_control.owns(claim_id, claim_generation):
                    raise ExecutionCancelled("claim_revoked")
                with body_claim_scope(claim_generation):
                    return callback(*values)

        def with_execution_receipt(result):
            """Attach execution truth only for internal plan consumers."""
            if not return_execution_receipt or receipt.get("delivered") is not True:
                return result
            if not isinstance(result, dict):
                return result
            enriched = dict(result)
            enriched["_execution"] = {
                "delivered": True,
                "tool": receipt.get("tool", tool.name),
            }
            explanation = getattr(tool, "supervision_explanation", None)
            if explanation:
                enriched["_execution"]["normal_work"] = explanation
            return enriched

        operation_id = tool_call_id or claim_id
        outcome: tuple[str, str | None] = ("unknown", None)
        if tool.sidecar.can_mutate_body:
            arguments = call_args[1] if len(call_args) > 1 and isinstance(call_args[1], dict) else {}
            self.execution_snapshots.begin(operation_id, tool.name, arguments)
        try:
            supervisor = getattr(self, "call_supervisor", None)
            if supervisor is None:
                # Older lightweight test runtimes may intentionally omit the
                # optional supervisor; preserve their direct execution seam.
                supervisor = getattr(self, "direct_build_supervisor", None)
            # R2 owns the SDK runner path.  The hand-written native Responses
            # kernel has its own pending-call protocol and is explicitly out
            # of this slice; keep its existing timing/settlement semantics.
            if (tool.sidecar.can_mutate_body and supervisor is not None
                    and not getattr(self, "native_responses", False)
                    ):
                result = await supervisor.run(tool, admitted_callback, call_args,
                    call_id=operation_id, timeout_s=timeout_s,
                    supervision_context=supervision_context, readmit=readmit_callback)
                outcome = _execution_outcome(result)
                return with_execution_receipt(result)
            if tool.sidecar.can_mutate_body:
                admitted_callback = self._observed(admitted_callback, operation_id)
                result = await self.execution_lane.run(admitted_callback, *call_args, timeout_s=timeout_s)
            else:
                try:
                    result = await self._off_writer_lane().run(admitted_callback, *call_args, timeout_s=timeout_s)
                except asyncio.CancelledError:
                    if _being_cancelled():
                        raise
                    # Swept off the writer lane by a stop aimed at Body work.
                    # It may have run in part, so it is reported, not repeated.
                    self.trace.emit("tool_swept_by_body_stop", tool=tool.name, tool_call_id=tool_call_id)
                    result = ToolResult(
                        False, "interrupted_by_body_stop", True,
                        next_suggestion="A stop of running Body work interrupted this call. Call it again if still needed.",
                    ).to_payload()
            outcome = _execution_outcome(result)
            return with_execution_receipt(result)
        except asyncio.CancelledError:
            outcome = ("cancelled", "cancelled")
            raise
        except Exception as exc:
            # A timeout or a Python failure does not prove the client stopped.
            outcome = ("unknown" if self.body_control.unknown else "failed", type(exc).__name__)
            raise
        finally:
            if tool.sidecar.can_mutate_body and not retain_claim and self.body_control.owns(claim_id, claim_generation):
                self.body_control.settle(claim_id=claim_id)
            if tool.sidecar.can_mutate_body:
                self.execution_snapshots.finish(
                    operation_id, state=outcome[0], reason=outcome[1],
                    control="unknown" if self.body_control.unknown else "released",
                )

    def _observed(self, callback: Callable[..., Any], operation_id: str) -> Callable[..., Any]:
        """Route this call's native progress to its snapshot and to the panel."""
        from minebot.contract.execution import tool_observation_scope
        telemetry = getattr(self, "panel_telemetry", None)
        snapshots = self.execution_snapshots

        def notify(payload: JsonObject) -> None:
            snapshots.observe(operation_id, payload)
            if telemetry is not None:
                telemetry.observe(operation_id, payload)

        def observed(*args: Any, **kwargs: Any) -> Any:
            snapshots.running(operation_id)
            with tool_observation_scope(operation_id, notify):
                return callback(*args, **kwargs)

        return observed

    async def _drain_responses_calls(self, reason: str) -> None:
        # Physical idle precedes the async wrappers observing cancellation.
        # Keep ownership until both lanes and those wrappers have settled.
        deadline = time.monotonic() + EXECUTION_LANE_CANCEL_TIMEOUT_S
        self._read_lane.request_cancel(reason)
        reads_idle = await self._read_lane.wait_idle(timeout_s=max(0.0, deadline - time.monotonic()))
        if not reads_idle or self.execution_lane.active_count:
            raise RuntimeError("Responses tools did not settle: execution workers are still running")
        tasks = [c.task for c in self.responses_kernel.pending if not c.task.done()]
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=max(0.0, deadline - time.monotonic()))
            if pending:
                raise RuntimeError("Responses tools did not settle: async results are still pending")

    async def reset_responses(self) -> None:
        if self.responses_kernel is not None:
            self.responses_kernel.halt()
            await self._drain_responses_calls("session_reset")
            await self.responses_kernel.reset()
            self.native_task_outcome = self.native_plan = None

    def set_tool_facts(self, tool_name: str, facts: dict[str, object]) -> None:
        self.tool_facts[tool_name] = dict(facts)

    def add_context_refresher(self, refresher: Callable[[AgentContext], None]) -> None:
        self.context_refreshers.append(refresher)

    async def run_turn(
        self,
        extra_signals: list[AgentSignal] | None = None,
        *,
        body_actions_allowed: bool = True,
        continuation_evidence_cursor: int | None = None,
        intent_kind: str | None = None,
        has_durable_goal: bool | None = None,
        continuation_budget: ContinuationEpochBudget | None = None,
        review_grant_stop_epoch_id: str | None = None,
    ) -> AgentTurnOutcome:
        if hasattr(self, "body_loop") and not self.native_responses:
            await self.body_loop.start()
        if self.reflex_manager is not None:
            self.reflex_manager.bind_loop(asyncio.get_running_loop())
        if self.native_responses:
            async with self._responses_turn_lock:
                return await self._run_responses_turn(extra_signals, body_actions_allowed=body_actions_allowed)
        self._turn_intent_kind = intent_kind
        self._turn_has_durable_goal = has_durable_goal
        self._turn_review_grant = review_grant_stop_epoch_id
        prepared = await self.run_read(self._prepare_turn, extra_signals)
        if isinstance(prepared, AgentTurnOutcome):
            return prepared
        profile = prepared
        restore_abort = await self.run_read(self.restore_task_progress)
        if restore_abort is not None:
            return self._yield_from_progress_abort(restore_abort)
        try:
            self._refresh_dynamic_context()
        except SkillOperationError as exc:
            return self._yield_from_skill_context_error(exc, profile)

        fallback_input = (
            "Continue the current goal from the latest authoritative state."
            if self.agent_context.goal_text.strip()
            else "Respond to the latest user message."
        )
        input_text, pending_input_count = self.agent_context.pending_turn_input(
            fallback=fallback_input
        )
        # F2 compiler: the identity route's "full" profile is golden-locked
        # byte-identical to the historical turn_preamble output.
        instruction_preamble = self.agent_context.compile(
            self._current_route.context_profile if self._current_route is not None else "full",
            include_session_messages=False,
        ).text
        skill_preamble = self.agent_context.skill_preamble()
        context_budget = self.agent_context.budget_facts()
        new_turn_frame_chars = (
            len(input_text)
            + len(self.agent_context.system_prompt)
            + len(instruction_preamble)
            + len(skill_preamble)
        )
        self.trace.emit(
            "context_budget",
            input_chars=len(input_text),
            instruction_chars=(
                len(self.agent_context.system_prompt)
                + len(instruction_preamble)
                + len(skill_preamble)
            ),
            new_turn_frame_chars=new_turn_frame_chars,
            estimated_context_chars=(
                new_turn_frame_chars
                + int(context_budget.get("conversation_live_item_chars") or 0)
            ),
            live_window_turns=getattr(self.conversation_session, "max_turns", None),
            **context_budget,
        )

        latest_evidence_cursor = self.latest_progress_evidence_cursor()
        self._run_evidence_cursor = (
            latest_evidence_cursor
            if continuation_evidence_cursor is None
            else max(0, min(int(continuation_evidence_cursor), latest_evidence_cursor))
        )
        run_id = f"run-{uuid4().hex}"
        progress_epochs = ProgressEpochAdapter(
            runtime=self,
            run_id=run_id,
            archive=self.progress_epoch_archive,
            budget=continuation_budget,
        )
        run_context = RuntimeRunContext(
            agent_context=self.agent_context,
            weld_context=self.weld_context,
            profile=profile,
            tool_facts={name: dict(facts) for name, facts in self.tool_facts.items()},
            trace=self.trace,
            runtime=self,
            instruction_preamble=instruction_preamble,
            body_actions_allowed=body_actions_allowed,
            progress_epochs=progress_epochs,
        )
        run_config = self._run_config(profile)
        turn_agent = self._agent_for_profile(profile)
        turn_trace_start = self.trace.last_seq

        runner_kwargs = {
            "context": run_context,
            "max_turns": self.max_turns,
            "run_config": run_config,
            "hooks": self.hooks,
            "session": self.conversation_session,
        }
        self.agent_context.acknowledge_turn_input(pending_input_count)
        try:
            if self.runner_run_streamed is not None:
                streamed = self.runner_run_streamed(turn_agent, input_text, **runner_kwargs)
                streamed_result = await self._supervise_streamed_run(streamed)
                if isinstance(streamed_result, AgentTurnOutcome):
                    return streamed_result
                result = streamed_result
            else:
                result = await self.runner_run(turn_agent, input_text, **runner_kwargs)
        except asyncio.CancelledError:
            progress_epochs.finalize_unsettled("turn_cancelled")
            self.authority.invalidate_generation("turn_cancelled")
            self.trace.emit("turn_cancelled", lifecycle=self.lifecycle.state.value)
            raise
        except ProgressAbort as exc:
            deferred = progress_epochs.finalize_unsettled("progress_abort")
            return self._yield_from_progress_abort(dominant_progress_abort(deferred, exc) or exc)
        except BodyRecoveryRequired as exc:
            progress_epochs.finalize_unsettled("body_recovery")
            return self._enter_recovery_from_body_fact(exc.reason, exc.facts)
        except MaxTurnsExceeded as exc:
            deferred = progress_epochs.finalize_unsettled("max_turns_exceeded")
            if progress_epochs.work_stop is not None:
                return self._yield_from_progress_abort(
                    dominant_progress_abort(deferred, progress_epochs.work_stop) or progress_epochs.work_stop)
            return self._yield_from_runaway_ceiling(exc)
        except UserError as exc:
            deferred = progress_epochs.finalize_unsettled("sdk_user_error")
            progress_abort = dominant_progress_abort(_find_progress_abort(exc), deferred)
            if progress_abort is None:
                recovery_required = _find_body_recovery_required(exc)
                if recovery_required is not None:
                    return self._enter_recovery_from_body_fact(recovery_required.reason, recovery_required.facts)
                self._raise_model_transport_error_if_retryable(exc)
                raise
            return self._yield_from_progress_abort(progress_abort)
        except SkillOperationError as exc:
            progress_epochs.finalize_unsettled("skill_operation_error")
            return self._yield_from_skill_context_error(exc, profile)
        except Exception as exc:
            progress_epochs.finalize_unsettled("runner_error")
            self._raise_model_transport_error_if_retryable(exc)
            raise

        deferred = progress_epochs.finalize_unsettled("runner_completed_with_unsettled_epoch")
        if deferred is not None:
            return self._yield_from_progress_abort(deferred)
        self.trace.emit("turn_completed", lifecycle=self.lifecycle.state.value, situational=profile.situational)
        self._reset_transport_errors()
        self._record_run_result(result, trace_after_seq=turn_trace_start)
        return AgentTurnOutcome(
            status="completed_turn",
            lifecycle=self.lifecycle.state,
            profile=profile,
            result=result,
            progress_stop_reason=None if progress_epochs.work_stop is None else progress_epochs.work_stop.stop_reason,
        )

    def _raise_model_transport_error_if_retryable(self, exc: BaseException) -> None:
        if not is_retryable_model_error(exc):
            return
        error_type = type(exc).__name__
        self.trace.emit(
            "model_turn_transport_failed",
            error_type=error_type,
            retryable=True,
        )
        raise ModelTurnTransportError(error_type) from exc

    def restore_task_progress(self) -> ProgressAbort | None:
        """Prepare task progress before model or lifecycle-recovery execution."""
        if self.progress_task_id is None:
            return None
        try:
            task_id = self.progress_task_id()
            if task_id is None:
                return self._progress_restore_abort
            if self._progress_task_initialized and task_id == self._progress_task_owner:
                self._reopen_reviewed_work_lane()
                return self._progress_restore_abort
            if self.progress_epoch_archive is not None:
                abort = self.progress_epoch_archive.restore_authority(self.authority, task_id=task_id)
            else:
                if self._progress_task_initialized:
                    self.authority.restore_facts(ProgressAuthority().facts(self.agent_context.goal_text))
                abort = None
        except Exception as exc:
            self.trace.emit("progress_restore_failed", error_type=type(exc).__name__)
            return ProgressAbort("progress state could not be restored")
        self._progress_task_owner = task_id
        self._progress_task_initialized = True
        self._progress_work_stop = (
            abort if abort is not None and abort.stop_reason in REVIEWABLE_STOP_REASONS
            else None
        )
        self._progress_restore_abort = (
            abort if abort is not None and abort.stop_reason in {
                ProgressStopReason.UNKNOWN, ProgressStopReason.BODY_TRANSPORT,
            } else None
        )
        self.trace.emit("progress_restored", task_id=task_id,
            progress_stop_reason=None if abort is None else abort.stop_reason.value,
            **asdict(self.authority.facts(self.agent_context.goal_text)))
        self._reopen_reviewed_work_lane()
        return self._progress_restore_abort

    def _reopen_reviewed_work_lane(self) -> None:
        """Open a closed work lane whose stop was reviewed with an allowance.

        The continuation gate already established that this exact approach
        debited one non-renewable task allowance to review that stop, that the
        approach is a different one, and that every stop in the window was
        reviewable. The exhausted approach's progress evidence cannot judge a
        different approach, so the successor run starts a fresh window.

        Nothing here refills an allowance, and a transport or unclassified stop
        never qualifies: those stay terminal in ``_progress_restore_abort``.
        """
        stop = self._progress_work_stop
        if self._turn_review_grant is None or stop is None:
            return
        if stop.stop_reason not in REVIEWABLE_STOP_REASONS:
            return
        self._progress_work_stop = None
        self.authority.restore_facts(ProgressAuthority().facts(self.agent_context.goal_text))
        self.trace.emit(
            "progress_work_lane_reopened",
            review_grant_stop_epoch_id=self._turn_review_grant,
            progress_stop_reason=stop.stop_reason.value,
        )

    async def run_sync(
        self,
        callback: Callable[..., Any],
        *args: object,
        timeout_s: float | None = None,
    ) -> Any:
        return await self.execution_lane.run(callback, *args, timeout_s=timeout_s)

    def _off_writer_lane(self) -> SerialExecutionLane:
        """Lane for work that reads but never writes the Body.

        The writer lane is one thread. While a plan or a background call owns
        the Body across turns, anything queued there waits for that work to
        end, which would freeze the Agent it was meant to free.
        """
        if self.native_responses or self.live_body_owner() is not None:
            return self._read_lane
        return self.execution_lane

    async def run_read(
        self,
        callback: Callable[..., Any],
        *args: object,
        timeout_s: float | None = None,
    ) -> Any:
        """Run read-only work off the loop without queueing behind a Body owner."""
        try:
            return await self._off_writer_lane().run(callback, *args, timeout_s=timeout_s)
        except asyncio.CancelledError:
            if _being_cancelled():
                raise
            # A stop aimed at Body work cancels everything queued on the writer
            # lane, and a read is routed there while nothing owns the Body.
            # Nobody asked to cancel this read or the task that waits for it.
            self.trace.emit("read_swept_by_body_stop")
            return await self._read_lane.run(callback, *args, timeout_s=timeout_s)

    def live_body_owner(self) -> JsonObject | None:
        """The plan or background call that holds the Body across model turns."""
        call = self.background_call
        if call is not None and not call.task.done():
            return call.payload()
        snapshot = self.body_control.snapshot()
        if snapshot.holder == "agent_plan" and snapshot.stop_state == "running":
            return {"kind": "plan", "activity_id": snapshot.claim_id, "status": "running"}
        return None

    def direct_call_window_s(self, tool: RegisteredTool) -> float | None:
        """Seconds a direct Body call may hold the model turn, or None for all of it.

        Only the SDK kernel needs this: its wire protocols answer a tool call
        before the next model request. A result can leave the turn only when
        the session is listening for its later delivery.
        """
        if (
            self.native_responses
            or not tool.sidecar.can_mutate_body
            or self.direct_call_window is None
            or self._sdk_reflex_fact_listener is None
        ):
            return None
        return self.direct_call_window

    def adopt_background_call(
        self, tool_call_id: str, tool_name: str, arguments: JsonObject, task: "asyncio.Future[JsonObject]",
    ) -> BackgroundCall:
        call = BackgroundCall(tool_call_id, tool_name, dict(arguments), task)
        self.background_call = call
        self.trace.emit("background_call_started", tool_call_id=tool_call_id, tool=tool_name,
                        body_control=self.body_control.snapshot().to_payload())
        return call

    def release_background_call(self, call: BackgroundCall) -> None:
        if self.background_call is call:
            self.background_call = None

    def commit_background_progress(
        self,
        steps: tuple[ProgressStep, ...],
        pre_state: BodyState | None,
        post_state: BodyState | None,
        *,
        success: bool,
        tool_call_id: str,
    ) -> None:
        """Give the progress authority what a detached call really did.

        Its epoch closed on the receipt, before any of this existed. A stop
        this raises is not lost: the counters persist and the next epoch of
        the woken turn meets the same limit.
        """
        material = material_progress_evidence(pre_state, post_state, successful_body_action=success)
        try:
            self.authority.commit_steps(
                _collapsed_epoch_progress_steps([SimpleNamespace(progress_steps=steps)]),
                self.agent_context.goal_text,
                material_changed=bool(material),
            )
        except ProgressAbort as exc:
            self.trace.emit("background_progress_stop", tool_call_id=tool_call_id,
                            stop_reason=exc.stop_reason.value)

    def shown_plan_result(self, tool_name: str, result: JsonObject, trace_ref: str) -> JsonObject:
        """A plan call's result as the Agent reads it: same projection as a direct call.

        The original is stored first and the projection carries its handle.
        """
        try:
            tool = self.registry.get(tool_name)
        except Exception:
            return _model_tool_payload(tool_name, result, trace_ref=trace_ref)
        return _finalize_tool_payload(tool=tool, result=result, trace=None,
                                      tool_call_id=trace_ref, runtime=self)

    def deliver_background_result(self, call: BackgroundCall, model_result: JsonObject) -> None:
        """The one final receipt of a detached call, sent as an execution fact."""
        reason = str(model_result.get("reason") or "")
        status = ("completed" if model_result.get("success")
                  else "cancelled" if reason in {"cancelled", "preempted"} else "blocked")
        self.trace.emit("background_call_finished", tool_call_id=call.tool_call_id, tool=call.tool,
                        status=status, reason=reason,
                        elapsed_s=round(time.monotonic() - call.started_at, 3))
        self.notify_reflex_fact({
            "kind": "direct_call",
            "activity_id": call.tool_call_id,
            "tool_call_id": call.tool_call_id,
            "tool": call.tool,
            "status": status,
            "result": model_result,
        })

    def background_work(self) -> JsonObject | None:
        """Turn-independent Body work, for CURRENT_CONTEXT and read_reflex."""
        work: JsonObject = {}
        call = self.background_call
        if call is not None and not call.task.done():
            work["direct_call"] = call.payload()
        summary = getattr(self.reflex_manager, "active_summary", None)
        plan = summary() if callable(summary) else None
        if plan is not None:
            work["plan"] = plan
        loop = getattr(self, "body_loop", None)
        experiences = getattr(loop, "recent_experiences", None)
        if experiences:
            work["reflex_experiences"] = list(experiences)
        if work:
            execution = self.execution_snapshots.current()
            if execution is not None:
                work["execution"] = execution
        now = time.monotonic()
        # A fact whose intent was discarded with its task never gets delivered.
        self._undelivered_results = {
            key: value for key, value in self.__dict__.get("_undelivered_results", {}).items()
            if now - float(value["ended_monotonic"]) <= UNDELIVERED_RESULT_MAX_AGE_S
        }
        if self._undelivered_results:
            # A result that ends while a turn is running reaches the
            # conversation only after that turn. Until then the turn must not
            # go on believing the work is still running.
            work["finished"] = {
                "results": [
                    {**{key: item for key, item in value.items() if key != "ended_monotonic"},
                     "ended_s_ago": round(now - float(value["ended_monotonic"]), 1)}
                    for value in self._undelivered_results.values()
                ],
                "note": "This work has ended. Its full result arrives as a REFLEX_RESULT message after this turn.",
            }
        return work or None

    def reflex_fact_delivered(self, activity_id: str) -> None:
        """The fact is now in the conversation, or was dropped for good."""
        self.__dict__.get("_undelivered_results", {}).pop(activity_id, None)

    def latest_progress_evidence_cursor(self) -> int:
        if self.progress_epoch_archive is None:
            return 0
        latest_cursor = getattr(self.progress_epoch_archive, "latest_cursor", None)
        if not callable(latest_cursor):
            return 0
        try:
            return max(0, int(latest_cursor()))
        except Exception as exc:
            self.trace.emit(
                "progress_epoch_cursor_failed",
                error_type=type(exc).__name__,
            )
            return 0

    def current_run_evidence_cursor(self) -> int:
        return self._run_evidence_cursor

    def current_progress_work_stop(self) -> ProgressAbort | None:
        """The reviewable stop that closed this run's work lane, if any."""
        return self._progress_work_stop

    async def read_progress_state(self) -> BodyState | None:
        try:
            return await self.run_read(self._read_progress_state)
        except Exception as exc:
            self.trace.emit(
                "progress_epoch_fingerprint_failed",
                error_type=type(exc).__name__,
            )
            return None

    def _read_progress_state(self) -> BodyState | None:
        return progress_snapshot(self.body.get_state())

    async def wait_for_execution_idle(self, *, timeout_s: float = EXECUTION_LANE_CANCEL_TIMEOUT_S) -> bool:
        return await self.execution_lane.wait_idle(timeout_s=timeout_s)

    async def wait_for_turn_execution_idle(self, *, timeout_s: float = EXECUTION_LANE_CANCEL_TIMEOUT_S) -> bool:
        """Whether the work a model turn owned has left the writer lane.

        A plan or background call keeps running there after the turn is
        cancelled or ends; that is its claim, not an orphan of the turn.
        """
        if self.live_body_owner() is not None:
            return True
        return await self.wait_for_execution_idle(timeout_s=timeout_s)

    def request_execution_cancel(self, reason: str) -> int:
        return self.execution_lane.request_cancel(reason)

    async def cancel_active_execution(self, reason: str) -> bool:
        facts = await self.cancel_active_execution_with_facts(reason)
        return bool(facts.get("settled"))

    async def cancel_active_execution_with_facts(
        self,
        reason: str,
        *,
        invalidate_generation: bool = True,
        cancel_reflex: bool = True,
    ) -> JsonObject:
        cancellation_started = time.monotonic()
        if cancel_reflex and self.reflex_manager is not None:
            # Revoke the plan before touching the native Body.  A late
            # Decision result must not be able to admit its next write.
            self.reflex_manager.request_cancel(reason)
        self.body_control.mark_stopping()
        self.execution_snapshots.stopping()
        supervisor = self.direct_build_supervisor.current
        if supervisor is not None:
            supervisor["cancel_requested"] = True
        if invalidate_generation:
            self.authority.invalidate_generation(reason)
        cancellation_scope_count = self.request_execution_cancel(reason)
        interrupt_ok: bool | None = None
        interrupt_accepted: bool | None = None
        interrupt_complete: bool | None = None
        interrupt_error: JsonObject | None = None
        try:
            interrupt = await self.execution_lane.run_control(self.body.interrupt, reason)
            interrupt_ok = _optional_bool_attr(interrupt, "ok")
            interrupt_accepted = _optional_bool_attr(interrupt, "accepted")
            interrupt_complete = _optional_bool_attr(interrupt, "complete")
        except Exception as exc:
            self.trace.emit("body_interrupt_failed", reason=reason, error_type=type(exc).__name__)
            interrupt_error = {
                "error_type": type(exc).__name__,
                "message": _shorten(str(exc), limit=300),
            }
        remaining_s = max(
            0.0,
            EXECUTION_LANE_CANCEL_TIMEOUT_S - (time.monotonic() - cancellation_started),
        )
        execution_idle = await self.wait_for_execution_idle(timeout_s=remaining_s)
        remaining_s = max(
            0.0,
            EXECUTION_LANE_CANCEL_TIMEOUT_S - (time.monotonic() - cancellation_started),
        )
        owner_facts = await self._wait_for_body_owner_idle(
            timeout_s=remaining_s,
        )
        owner_observed = bool(owner_facts.get("owner_observed"))
        owner = owner_facts.get("owner")
        owner_settled = owner_observed and owner is None
        if not owner_observed:
            owner_settled = (
                "owner_probe_error" not in owner_facts
                and interrupt_complete is True
            )
        native_probe = getattr(self.body, "confirm_control_idle", None)
        native_facts = {}
        if callable(native_probe):
            try:
                native_facts = await self._read_lane.run(native_probe)
            except Exception as exc:
                native_facts = {"native_stopped": False, "control_unknown": True,
                                "probe_error": type(exc).__name__}
            owner_settled = native_facts.get("native_stopped") is True
        settled = execution_idle and owner_settled
        if settled:
            self.body_control.settle()
        else:
            self.body_control.mark_unknown()
        facts: JsonObject = {
            "interrupt_requested": True,
            "cancellation_scope_count": cancellation_scope_count,
            "interrupt_ok": interrupt_ok,
            "interrupt_accepted": interrupt_accepted,
            "interrupt_complete": interrupt_complete,
            "execution_idle": execution_idle,
            **owner_facts,
            **native_facts,
            "body_control": self.body_control.snapshot().to_payload(),
            "settled": settled,
            "reason": reason,
        }
        if interrupt_error is not None:
            facts["interrupt_error"] = interrupt_error
        self.trace.emit(
            "execution_cancelled",
            **facts,
            idle=execution_idle,
            active_count=self.execution_lane.active_count,
        )
        return facts

    async def _wait_for_body_owner_idle(self, *, timeout_s: float) -> JsonObject:
        read_head = getattr(self.body, "event_head", None)
        if not callable(read_head):
            return {
                "owner_observed": False,
                "owner": None,
                "owner_checks": 0,
                "owner_wait_ms": 0.0,
            }
        started = time.monotonic()
        deadline = started + max(0.0, timeout_s)
        proposed_epoch = f"cancel-{uuid4().hex}"
        owner: str | None = None
        checks = 0
        while True:
            checks += 1
            try:
                # The interrupt must run out-of-band so it can preempt a stuck
                # writer. Once that writer is idle, owner reconciliation goes
                # back through the one serialized Body lane like every other
                # read; a second executor would violate Body call ordering.
                head = await self.run_read(read_head, proposed_epoch)
            except Exception as exc:
                return {
                    "owner_observed": False,
                    "owner": owner,
                    "owner_checks": checks,
                    "owner_wait_ms": round((time.monotonic() - started) * 1000.0, 3),
                    "owner_probe_error": {
                        "error_type": type(exc).__name__,
                        "message": _shorten(str(exc), limit=300),
                    },
                }
            raw_owner = head.get("owner") if isinstance(head, dict) else None
            owner = None if raw_owner is None else str(raw_owner)
            if owner is None or time.monotonic() >= deadline:
                return {
                    "owner_observed": True,
                    "owner": owner,
                    "owner_checks": checks,
                    "owner_wait_ms": round((time.monotonic() - started) * 1000.0, 3),
                }
            await asyncio.sleep(BODY_OWNER_SETTLE_POLL_S)

    async def aclose(self) -> None:
        call = self.background_call
        if call is not None and not call.task.done():
            await self.cancel_active_execution("runtime_closed")
            call.task.cancel()
            await asyncio.gather(call.task, return_exceptions=True)
        if self.reflex_manager is not None:
            await self.reflex_manager.close()
        body_loop = getattr(self, "body_loop", None)
        if body_loop is not None:
            await body_loop.stop()
            await body_loop.close()
        else:
            await self.direct_build_supervisor.close()
        if self.responses_kernel is not None:
            self.responses_kernel.halt()
            if any(c.mutating and not c.task.done() for c in self.responses_kernel.pending):
                await self.cancel_active_execution("runtime_closed")
            try:
                await self._drain_responses_calls("runtime_closed")
            except RuntimeError as exc:
                self.trace.emit("runtime_close_unsettled", message=str(exc),
                                writer_count=self.execution_lane.active_count)
            await self.responses_kernel.close()
        self.close()

    def close(self) -> None:
        self._read_lane.close()
        self.execution_lane.close()
        close_session = getattr(self.conversation_session, "close", None)
        if callable(close_session):
            close_session()

    async def _supervise_streamed_run(self, streamed: Any) -> Any | AgentTurnOutcome:
        stream_task = asyncio.create_task(self._drain_stream(streamed))
        body_task = asyncio.create_task(self._wait_for_critical_body_fact())
        try:
            done, _ = await asyncio.wait({stream_task, body_task}, return_when=asyncio.FIRST_COMPLETED)
            if body_task in done and body_task.cancelled():
                # The watcher is an observer. Losing it is not a Body fact and
                # must never be read as this turn being cancelled.
                self.trace.emit("body_watch_lost")
                await asyncio.wait({stream_task})
                return stream_task.result()
            if body_task in done:
                reason, facts = body_task.result()
                self.authority.invalidate_generation(f"body_critical:{reason}")
                try:
                    await self.execution_lane.run_control(self.body.interrupt, reason)
                except Exception as exc:
                    self.trace.emit("body_interrupt_failed", reason=reason, error_type=type(exc).__name__)
                streamed.cancel(mode="immediate")
                await self._finish_stream_cancellation(stream_task)
                idle = await self.wait_for_execution_idle()
                self.trace.emit("turn_body_preempted", reason=reason, facts=facts, execution_idle=idle)
                return self._enter_recovery_from_body_fact(reason, facts)
            return stream_task.result()
        except asyncio.CancelledError:
            streamed.cancel(mode="immediate")
            await self._finish_stream_cancellation(stream_task)
            idle = await self.wait_for_turn_execution_idle()
            self.trace.emit("stream_cancelled", execution_idle=idle, active_count=self.execution_lane.active_count)
            raise
        finally:
            body_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await body_task


    @staticmethod
    async def _drain_stream(streamed: Any) -> Any:
        async for _event in streamed.stream_events():
            pass
        return streamed

    async def _finish_stream_cancellation(self, stream_task: asyncio.Task[Any]) -> None:
        try:
            await asyncio.wait_for(stream_task, timeout=STREAM_CANCEL_DRAIN_TIMEOUT_S)
        except asyncio.TimeoutError:
            stream_task.cancel()
            self.trace.emit("stream_cancel_drain_timeout", timeout_s=STREAM_CANCEL_DRAIN_TIMEOUT_S)
            with contextlib.suppress(asyncio.CancelledError):
                await stream_task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.trace.emit("stream_cancel_drain_failed", error_type=type(exc).__name__)

    async def _wait_for_critical_body_fact(self) -> tuple[str, dict[str, object]]:
        failures = 0
        while True:
            await asyncio.sleep(BODY_WATCH_POLL_S)
            if self._pending_body_death is not None:
                facts = self._pending_body_death
                self._pending_body_death = None
                return "death", facts
            try:
                state = await self.run_read(self.body.get_state)
            except Exception as exc:
                failures += 1
                self.trace.emit(
                    "body_watch_failed",
                    error_type=type(exc).__name__,
                    count=failures,
                    threshold=BODY_TRANSPORT_RECOVERY_LIMIT,
                )
                if failures >= BODY_TRANSPORT_RECOVERY_LIMIT:
                    return (
                        "body_transport_unstable",
                        {"error_type": type(exc).__name__, "consecutive_failures": failures},
                    )
                continue
            failures = 0
            if state.missing or state.health <= 0:
                return (
                    "missing_body" if state.missing else "death_detected",
                    {"missing": state.missing, "health": state.health, "pos": list(state.pos)},
                )

    def _instructions(
        self,
        ctx: RunContextWrapper[RuntimeRunContext],
        agent: Agent[RuntimeRunContext],
    ) -> str:
        # Cache layout (prompt_cache.py): this text is the stable head of every
        # request, so it carries nothing that moves between model calls. The
        # live preamble (goal, state, stance) is delivered as the trailing
        # CURRENT_CONTEXT input item by _bounded_model_input. Skills change
        # only on load/unload, so they sit last and invalidate the least.
        context = ctx.context.agent_context
        self._refresh_dynamic_context()
        parts = [context.system_prompt]
        if self.reflex_manager is not None:
            parts.append(
                "REFLEX_PLANS: Use a minebot-work/0.2 plan when the goal has multiple dependent physical steps, "
                "a long-running physical action followed by later actions, a condition that may remain pending, "
                "or work that may need pause, resume, cancellation, or revision; the runtime then owns sequencing, "
                "waiting, control, and authoritative terminal state. Use direct tools for one short independent "
                "observation or physical action. The tree is a range for the reflex layer, not a perfect script. "
                "Use repeat around a call or choice for ‘do this until enough’, with one natural-language ending "
                "condition; Decision chooses whether to repeat or finish from facts. Use delegate_work for one "
                "minebot-work/0.2 tree (call/sequence/batch/choice/wait), put known steps into it, then work on "
                "later planning instead of polling for progress; terminal facts arrive asynchronously. "
                "Runtime owns _runtime status and real results; "
                "use read_reflex/control_reflex to inspect or revise it."
            )
            parts.append(
                "BACKGROUND_WORK: The Body does one piece of work at a time and that work does not stop when "
                "your turn ends. A direct Body tool that is still running after a short window returns "
                "reason=running_in_background: that is a receipt, not a result. The real result of that same "
                "call arrives later as a REFLEX_RESULT message with kind=direct_call. Meanwhile CURRENT_CONTEXT "
                "lists running work under BACKGROUND_WORK; you can talk, observe and plan, read it with "
                "read_reflex, or stop it with control_reflex (cancel or take_control). Another Body action "
                "returns body_busy until the running work has settled."
            )
        skill_preamble = context.skill_preamble()
        if skill_preamble:
            parts.append(skill_preamble)
        return "\n\n".join(parts)

    def _refresh_dynamic_context(self) -> None:
        for refresher in self.context_refreshers:
            refresher(self.agent_context)

    def _yield_from_skill_context_error(
        self,
        exc: SkillOperationError,
        profile: RuntimeProfile,
    ) -> AgentTurnOutcome:
        if self.lifecycle.state is LifecycleState.ACTIVE:
            self.lifecycle.yield_()
        yielded_profile = self.mode_runtime.profile_for(self.lifecycle.state)
        self.agent_context.observe_profile(yielded_profile)
        self.trace.emit(
            "skill_context_recovery_required",
            reason=exc.code,
            error=str(exc),
            lifecycle=self.lifecycle.state.value,
        )
        return AgentTurnOutcome(
            status="yielded",
            lifecycle=self.lifecycle.state,
            profile=yielded_profile if self.lifecycle.state is LifecycleState.YIELDED else profile,
            message=exc.code,
        )

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

    def _route_model_for(self, profile: RuntimeProfile) -> str:
        """Logical model for this turn: F1 route when a policy opted in."""
        if self.routing_policy is not None and self._current_route is not None:
            return self._current_route.model
        return profile.model_route

    def _agent_for_profile(self, profile: RuntimeProfile) -> Agent[RuntimeRunContext]:
        logical_model = self._route_model_for(profile)
        kwargs: dict[str, Any] = {"model": logical_model}
        if self.model_provider is not None:
            kwargs["model_settings"] = self.model_provider.model_settings_for(logical_model)
        return self.agent.clone(**kwargs)

    def _run_config(self, profile: RuntimeProfile) -> RunConfig:
        if self.model_provider is None:
            return RunConfig(
                session_input_callback=bounded_session_input,
                call_model_input_filter=self._bounded_model_input,
            )
        logical_model = self._route_model_for(profile)
        return RunConfig(
            model_provider=self.model_provider,
            model_settings=self.model_provider.model_settings_for(logical_model),
            session_input_callback=bounded_session_input,
            call_model_input_filter=self._bounded_model_input,
        )

    async def _bounded_model_input(self, data: Any) -> Any:
        """Whole conversation behind a stable prefix, then the live context.

        History is compacted once when the request reaches the threshold
        (context_compaction.py); it is never windowed per request, because a
        moving start turns every request into a full prompt-cache write.
        """
        run_context = data.context
        await self._refresh_live_context(run_context)
        live_context = getattr(run_context, "instruction_preamble", "") or ""
        work = self.background_work()
        if work is not None:
            # Read at request time, so each model call sees where the Body is now.
            live_context = "\n".join(part for part in (
                live_context, "BACKGROUND_WORK: " + json.dumps(work, ensure_ascii=False, sort_keys=True),
            ) if part)
        original = data.model_data.input
        items = [{"role": "user", "content": original}] if isinstance(original, str) else list(original)
        instructions = data.model_data.instructions
        received = items
        items = self._bounded_items(items)
        view = await self.conversation_compactor.view(
            items, static_chars=len(instructions or ""), live_chars=len(live_context))
        context_item = current_context_item(live_context)
        if context_item is not None:
            view = [*view, context_item]
            if isinstance(work, dict) and work.get("reflex_experiences"):
                self.trace.emit("reflex_context_included",
                    experiences=list(work["reflex_experiences"]),
                    context_item_role=context_item["role"])
        elif len(view) == len(items) and not isinstance(original, str) and all(
                shown is given for shown, given in zip(items, received)):
            return data.model_data
        return ModelInputData(input=view, instructions=instructions)

    def _bounded_items(self, items: list[Any]) -> list[Any]:
        """Cut any single item that no projection bounded, and say so once."""
        reported = self.__dict__.setdefault("_oversized_items_reported", set())
        out: list[Any] = []
        for item in items:
            bounded, removed = bound_item(item)
            if removed:
                key = item_fingerprint(item)
                if key not in reported:
                    reported.add(key)
                    self.trace.emit(
                        "model_input_item_bounded",
                        item_type=str(item.get("type") or item.get("role") or "unknown"),
                        removed_chars=removed, limit_chars=MAX_ITEM_CHARS,
                        head=_shorten(json.dumps(item, ensure_ascii=False, default=str), limit=200),
                    )
            out.append(bounded)
        return out

    async def _refresh_live_context(self, run_context: Any) -> None:
        """Read the Body before each request so CURRENT_CONTEXT is current.

        The preamble is presented to the model as the latest state. A snapshot
        taken when the turn began contradicts every tool result after it.
        """
        if not isinstance(run_context, RuntimeRunContext) or run_context.runtime is not self:
            return
        try:
            state = await self.run_read(self.body.get_state)
        except Exception as exc:
            self.trace.emit("live_context_refresh_failed", error_type=type(exc).__name__)
            return
        if getattr(state, "missing", False):
            return  # the tool path owns Body recovery; do not publish a missing Body as state
        self.agent_context.observe_state(state)
        run_context.instruction_preamble = self.agent_context.compile(
            self._current_route.context_profile if self._current_route is not None else "full",
            include_session_messages=False,
        ).text

    async def _summarize_conversation(self, previous: str, transcript: str) -> str:
        """One plain text request to the primary model; no tools, no cache write."""
        from agents.models.interface import ModelTracing

        logical = self.model_provider.default
        model = self.model_provider.resolve(logical)
        base = self.model_provider.model_settings_for(logical)
        settings = dataclasses.replace(
            base, tool_choice=None, parallel_tool_calls=None,
            # Reasoning shares the output budget; a summary needs the room for text.
            max_tokens=max(base.max_tokens or 0, SUMMARY_MAX_OUTPUT_TOKENS),
            reasoning=None if base.reasoning is None else Reasoning(effort="low"),
        )
        if hasattr(model, "without_prompt_cache"):
            # A one-off request that large must not be written to the cache.
            model = model.without_prompt_cache()
        text = (f"PREVIOUS_SUMMARY:\n{previous}\n\n" if previous else "") + "CONVERSATION_ITEMS_JSON:\n" + transcript
        response = await model.get_response(
            SUMMARY_INSTRUCTIONS, [{"role": "user", "content": text}], settings, [], None, [],
            ModelTracing.DISABLED, previous_response_id=None, conversation_id=None, prompt=None,
        )
        usage = model_usage_observation(getattr(response, "usage", None))
        if usage is not None:
            self.trace.emit("model_usage", purpose="conversation_summary", **usage)
        parts: list[str] = []
        for output in getattr(response, "output", None) or []:
            for part in getattr(output, "content", None) or []:
                value = getattr(part, "text", None)
                if isinstance(value, str):
                    parts.append(value)
        return "\n".join(parts)

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

    def _record_run_result(self, result: Any, *, trace_after_seq: int = 0) -> None:
        extracted = extract_run_observations(result)
        existing = {
            key
            for event in self.trace.snapshot()
            if int(event.get("seq") or 0) > trace_after_seq
            if (key := _run_observation_key(event)) is not None
        }
        for event in extracted:
            key = _run_observation_key(event)
            if key is None or key not in existing:
                self.trace.emit(**event)
                if key is not None:
                    existing.add(key)
        # Keep the model's spoken explanation available at the Body boundary.
        # SDK tool calls often arrive in the same response as a short assistant
        # message ("I am going to..."), while ``final_output`` is empty or is
        # only the turn's closing text. The reflex layer must use that
        # explanation as the direct-call intent instead of inventing a
        # placeholder.
        assistant_contents = [
            str(event.get("content") or "")
            for event in extracted
            if event.get("event") in {"assistant_message", "assistant_final_output"}
            and str(event.get("content") or "").strip()
        ]
        for content in assistant_contents:
            self.agent_context.observe_assistant_message(content)
        final_text = _final_assistant_text(result, extracted)
        if final_text:
            if final_text not in assistant_contents:
                self.agent_context.observe_assistant_message(final_text)
            if self.speech_sink is not None:
                try:
                    self.speech_sink(final_text)
                except Exception as exc:
                    self.trace.emit("speech_sink_failed", error_type=type(exc).__name__)
        has_content = any(
            event.get("event") in {"assistant_message", "assistant_final_output"} and event.get("content")
            for event in extracted
        )
        has_tool_call = any(event.get("event") == "model_tool_call" for event in extracted)
        if has_tool_call and not has_content:
            self.trace.emit("assistant_no_content_tool_only")

    def remember_tool_result(
        self,
        tool_name: str,
        result: JsonObject,
        *,
        run_context: RuntimeRunContext | None = None,
    ) -> None:
        tool_result = {
            "tool": tool_name,
            "success": bool(result.get("success")),
            "reason": str(result.get("reason") or ""),
            "summary": _tool_result_summary(result),
        }
        self.last_tool_results.append(tool_result)
        if len(self.last_tool_results) > 12:
            del self.last_tool_results[: len(self.last_tool_results) - 12]

        survival_reason = survival_reason_from_tool_results([tool_result])
        if survival_reason is not None:
            terminal = {
                "tool": tool_name,
                "reason": survival_reason,
                "summary": tool_result["summary"],
            }
            self.trace.emit(
                "survival_terminal_preserved",
                tool=tool_name,
                reason=survival_reason,
                summary=tool_result["summary"],
            )
            if run_context is not None:
                self._apply_survival_terminal_to_live_context(run_context, terminal)

        reason = mobility_reason_from_tool_results([tool_result])
        if reason is None:
            return
        terminal = {
            "tool": tool_name,
            "reason": reason,
            "summary": tool_result["summary"],
        }
        self._pending_mobility_terminal = terminal
        self.trace.emit(
            "mobility_terminal_preserved",
            tool=tool_name,
            reason=reason,
            summary=tool_result["summary"],
        )
        if run_context is not None:
            self._apply_mobility_terminal_to_live_context(run_context, terminal)

    def _apply_survival_terminal_to_live_context(
        self,
        run_context: RuntimeRunContext,
        terminal: dict[str, object],
    ) -> None:
        reason = str(terminal["reason"])
        previous = run_context.profile
        reduction = self.mode_runtime.reduce(
            [
                AgentSignal.tool_results(
                    [
                        {
                            "tool": str(terminal["tool"]),
                            "reason": reason,
                            "summary": terminal["summary"],
                        }
                    ]
                )
            ],
            self.lifecycle.state,
            goal_text=self.agent_context.goal_text,
        )
        self._apply_lifecycle_request(reduction.requested_lifecycle)
        profile = self.mode_runtime.profile_for(self.lifecycle.state)
        self.agent_context.observe_profile(profile)
        run_context.profile = profile
        run_context.instruction_preamble = self.agent_context.turn_preamble(
            include_session_messages=False
        )
        self.trace.emit(
            "survival_terminal_live_handoff",
            tool=str(terminal["tool"]),
            reason=reason,
            previous_situational=previous.situational,
            situational=profile.situational,
            instruction_preamble_refreshed=True,
        )

    def _apply_mobility_terminal_to_live_context(
        self,
        run_context: RuntimeRunContext,
        terminal: dict[str, object],
    ) -> None:
        reason = str(terminal["reason"])
        previous = run_context.profile
        reduction = self.mode_runtime.reduce(
            [
                AgentSignal.mobility_blocked(
                    reason,
                    tool=str(terminal["tool"]),
                    source="tool_terminal",
                )
            ],
            self.lifecycle.state,
            goal_text=self.agent_context.goal_text,
        )
        self._apply_lifecycle_request(reduction.requested_lifecycle)
        profile = self.mode_runtime.profile_for(self.lifecycle.state)
        self.agent_context.observe_profile(profile)
        run_context.profile = profile
        run_context.instruction_preamble = self.agent_context.turn_preamble(
            include_session_messages=False
        )
        self.trace.emit(
            "mobility_terminal_live_handoff",
            tool=str(terminal["tool"]),
            reason=reason,
            previous_situational=previous.situational,
            situational=profile.situational,
            instruction_preamble_refreshed=True,
        )

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
            "inventory_counts": getattr(state, "inventory_counts", None),
            "selected_slot": getattr(state, "selected_slot", None),
            "selected_item": getattr(state, "selected_item", None),
            "selected_item_known": getattr(state, "selected_item_known", None),
            "inventory_complete": getattr(state, "inventory_complete", None),
            "inventory_includes_selected": getattr(state, "inventory_includes_selected", None),
            "offhand_item": getattr(state, "offhand_item", None),
            "body_owner": getattr(state, "body_owner", None),
            "pending_action_count": getattr(state, "pending_action_count", None),
            "hazard_unresolved": getattr(state, "hazard_unresolved", None),
        }

    def _trace_mutation_body_state(self, state: Any, source: str) -> None:
        """Trace one post-mutation server snapshot without inferring a delta."""

        if getattr(state, "missing", False):
            self.trace.emit(
                "body_state",
                source=source,
                bot=getattr(state, "bot", None),
                pos=list(getattr(state, "pos", ())),
                health=getattr(state, "health", None),
                food=getattr(state, "food", None),
                oxygen=getattr(state, "oxygen", None),
                inventory_hash=getattr(state, "inventory_hash", None),
                inventory_counts=dict(getattr(state, "inventory_counts", None) or {}),
                selected_slot=getattr(state, "selected_slot", None),
                selected_item=getattr(state, "selected_item", None),
                offhand_item=getattr(state, "offhand_item", None),
                body_owner=getattr(state, "body_owner", None),
                pending_action_count=getattr(state, "pending_action_count", None),
                hazard_unresolved=getattr(state, "hazard_unresolved", None),
                dimension=getattr(state, "dimension", None),
                complete=bool(getattr(state, "complete", False)),
                missing=True,
            )
            return
        self._remember_body_state(state)
        self._emit_last_known_body_state_trace(source=source)

    def trace_body_events(self, events: object, *, source: str = "turn_boundary") -> None:
        """Emit a complete, non-consuming Body-event observation.

        Body transactions and the idle event pump may already have consumed
        events from the provider.  This method records the payload at the
        point it is observed and de-duplicates by the authoritative sequence
        number, so telemetry never competes with the single event consumer.
        An empty list is still a meaningful heartbeat: it distinguishes a
        covered interval with no event from a missing event observation.
        """

        with self._body_event_trace_lock:
            payload: list[JsonObject] = []
            max_seq = self._last_traced_body_event_seq
            for event in events or ():
                if isinstance(event, dict):
                    seq = int(event.get("seq") or 0)
                    tick = int(event.get("tick") or 0)
                    bot = event.get("bot")
                    name = event.get("name")
                    data = event.get("data")
                else:
                    seq = int(getattr(event, "seq", 0) or 0)
                    tick = int(getattr(event, "tick", 0) or 0)
                    bot = getattr(event, "bot", None)
                    name = getattr(event, "name", None)
                    data = getattr(event, "data", None)
                if seq <= self._last_traced_body_event_seq:
                    continue
                payload.append(
                    {
                        "seq": seq,
                        "tick": tick,
                        "bot": bot,
                        "name": name,
                        "data": dict(data or {}) if isinstance(data, dict) else {},
                    }
                )
                max_seq = max(max_seq, seq)
            self.trace.emit(
                "body_events",
                source=source,
                count=len(payload),
                names=[item["name"] for item in payload],
                seqs=[item["seq"] for item in payload],
                events=payload,
            )
            self._last_traced_body_event_seq = max_seq

    def remember_tool_body_facts(self, result: JsonObject, *, trace: RuntimeTrace | None = None) -> None:
        metrics = result.get("metrics") if isinstance(result, dict) else None
        if not isinstance(metrics, dict):
            return
        if metrics.get("missing") is True:
            return
        previous = dict(self.last_known_body_state or {})
        updated = False
        pos = metrics.get("pos")
        if isinstance(pos, list) and len(pos) == 3:
            previous["pos"] = list(pos)
            updated = True
        counts = metrics.get("inventory_counts")
        if not isinstance(counts, dict):
            counts = metrics.get("counts")
        if isinstance(counts, dict):
            previous["inventory_counts"] = dict(counts)
            updated = True
        for key in (
            "bot",
            "yaw",
            "pitch",
            "health",
            "food",
            "oxygen",
            "dimension",
            "inventory_hash",
            "selected_slot",
            "selected_item",
            "offhand_item",
            "body_owner",
            "pending_action_count",
            "hazard_unresolved",
            "complete",
        ):
            if key in metrics:
                previous[key] = metrics.get(key)
                updated = True
        if not updated:
            return
        self.last_known_body_state = previous
        self._emit_last_known_body_state_trace(source="tool_result_metrics", trace=trace)

    def _emit_last_known_body_state_trace(self, *, source: str, trace: RuntimeTrace | None = None) -> None:
        state = self.last_known_body_state
        if not isinstance(state, dict):
            return
        pos = state.get("pos")
        if not (isinstance(pos, list) and len(pos) == 3):
            return
        inventory_counts = state.get("inventory_counts")
        (trace or self.trace).emit(
            "body_state",
            source=source,
            bot=state.get("bot"),
            pos=list(pos),
            yaw=state.get("yaw"),
            pitch=state.get("pitch"),
            health=state.get("health"),
            food=state.get("food"),
            oxygen=state.get("oxygen"),
            inventory_hash=state.get("inventory_hash"),
            inventory_counts=dict(inventory_counts) if isinstance(inventory_counts, dict) else {},
            selected_slot=state.get("selected_slot"),
            selected_item=state.get("selected_item"),
            offhand_item=state.get("offhand_item"),
            body_owner=state.get("body_owner"),
            pending_action_count=state.get("pending_action_count"),
            hazard_unresolved=state.get("hazard_unresolved"),
            dimension=state.get("dimension"),
            complete=bool(state.get("complete", True)),
            missing=False,
        )

    def _yield_from_progress_abort(self, exc: ProgressAbort) -> AgentTurnOutcome:
        facts = exc.facts or self.authority.facts(self.agent_context.goal_text)
        return self._yield_with_facts(
            facts,
            trace_event="progress_yielded",
            message=_yield_message(facts, self.agent_context.goal_text),
            progress_stop_reason=exc.stop_reason,
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
        self._pending_body_death = None
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

    def record_transport_error(
        self,
        tool_name: str,
        result: JsonObject,
        *,
        tool_call_id: str,
        raise_on_limit: bool = True,
    ) -> ProgressAbort | None:
        metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
        incident_key = _transport_error_incident_key(
            result,
            tool_call_id=tool_call_id,
        )
        duplicate = incident_key in self._transport_error_incidents
        if not duplicate:
            self._transport_error_incidents.add(incident_key)
            self.consecutive_transport_errors += 1
        self.trace.emit(
            "body_transport_error",
            tool=tool_name,
            tool_call_id=tool_call_id,
            count=self.consecutive_transport_errors,
            threshold=BODY_TRANSPORT_RECOVERY_LIMIT,
            duplicate=duplicate,
            incident_key=incident_key,
            error_type=metrics.get("error_type"),
            reason=str(result.get("reason") or ""),
            await_diagnostics=metrics.get("await_diagnostics"),
        )
        if not duplicate and self.consecutive_transport_errors >= BODY_TRANSPORT_RECOVERY_LIMIT:
            facts = self.authority.facts(self.agent_context.goal_text)
            facts.recent_events.append(
                "body_transport_unstable:"
                f"tool={tool_name}:"
                f"count={self.consecutive_transport_errors}:"
                f"error_type={metrics.get('error_type')}:"
                f"reason={str(result.get('reason') or '')}"
            )
            abort = ProgressAbort(
                "body transport unstable: yielding for supervisor review",
                facts=facts,
                stop_reason=ProgressStopReason.BODY_TRANSPORT,
            )
            if raise_on_limit:
                raise abort
            return abort
        return None

    def _reset_transport_errors(self) -> None:
        if self.consecutive_transport_errors:
            self.trace.emit("body_transport_recovered", count=self.consecutive_transport_errors)
        self.consecutive_transport_errors = 0
        self._transport_error_incidents.clear()

    def note_transport_success(self) -> None:
        """Reset the bounded transport-failure streak after a Body success."""

        self._reset_transport_errors()

    def _yield_with_facts(
        self,
        facts: ProgressFacts,
        *,
        trace_event: str,
        message: str,
        progress_stop_reason: ProgressStopReason | None = None,
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
            progress_stop_reason=None if progress_stop_reason is None else progress_stop_reason.value,
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
            progress_stop_reason=progress_stop_reason,
        )

    def _prepare_turn(self, extra_signals: list[AgentSignal] | None = None) -> RuntimeProfile | AgentTurnOutcome:
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
            inventory_counts=dict(state.inventory_counts or {}),
            selected_slot=state.selected_slot,
            selected_item=state.selected_item,
            offhand_item=state.offhand_item,
            body_owner=state.body_owner,
            pending_action_count=state.pending_action_count,
            hazard_unresolved=state.hazard_unresolved,
            dimension=state.dimension,
            complete=state.complete,
            missing=state.missing,
        )
        self.trace_body_events(events)
        signals = [
            *signalize_body_state(state),
            *signalize_events(events),
            *(extra_signals or []),
        ]
        if any(signal.kind == "death_detected" for signal in signals):
            self._pending_body_death = None
        pending_mobility_terminal = self._pending_mobility_terminal
        if pending_mobility_terminal is not None:
            signals.append(
                AgentSignal.mobility_blocked(
                    str(pending_mobility_terminal["reason"]),
                    tool=str(pending_mobility_terminal["tool"]),
                    source="deferred_tool_terminal",
                )
            )
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
        if pending_mobility_terminal is not None:
            self._pending_mobility_terminal = None
            self.trace.emit(
                "mobility_terminal_carried_to_outer_turn",
                tool=str(pending_mobility_terminal["tool"]),
                reason=str(pending_mobility_terminal["reason"]),
            )
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

        # F1 deliberation economy: classify the turn from deterministic facts
        # and resolve its route. Telemetry always; the route changes model
        # selection and context profile only when a routing_policy was
        # explicitly provided (identity default preserves today's behavior).
        context_class = decision_context(
            intent_kind=self._turn_intent_kind,
            lifecycle_state=self.lifecycle.state,
            mode_facts=reduction,
            last_epoch_facts=None,
            has_durable_goal=(
                True
                if self._turn_has_durable_goal is None
                else self._turn_has_durable_goal
            ),
        )
        route = resolve_route(self.routing_policy or DEFAULT_ROUTING_POLICY, context_class)
        self._current_decision_context = context_class
        self._current_route = route
        self.trace.emit(
            "turn_decision_context",
            decision_context=context_class.value,
            route_model=route.model,
            route_effort=route.effort,
            route_context_profile=route.context_profile,
            routing_active=self.routing_policy is not None,
            intent_kind=self._turn_intent_kind,
            has_durable_goal=self._turn_has_durable_goal,
            situational=profile.situational,
            lifecycle=self.lifecycle.state.value,
        )

        if not self.lifecycle.is_active:
            self.trace.emit("turn_stopped", lifecycle=self.lifecycle.state.value, reason=reduction.reason)
            return AgentTurnOutcome(
                status="stopped",
                lifecycle=self.lifecycle.state,
                profile=profile,
                message=reduction.reason,
            )
        return profile


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


def _run_observation_key(event: dict[str, object]) -> tuple[object, ...] | None:
    name = event.get("event")
    if name in {"assistant_message", "assistant_final_output"}:
        return (name, event.get("content"))
    if name == "model_tool_call":
        return (name, event.get("tool"), event.get("arguments_summary"))
    if name == "model_tool_output":
        return (name, event.get("summary"))
    return None


def _final_assistant_text(result: Any, extracted: list[dict[str, object]]) -> str | None:
    final_output = getattr(result, "final_output", None)
    if final_output not in {None, ""}:
        text = _shorten(_public_text(final_output), limit=2000).strip()
        return text or None
    for event in reversed(extracted):
        if event.get("event") not in {"assistant_final_output", "assistant_message"}:
            continue
        content = event.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
    return None


def model_usage_observation(usage: Any) -> dict[str, object] | None:
    """Provider-neutral per-request token usage for the trace and live panel.

    ``input_tokens`` follows the OpenAI convention (cached + cache-written
    tokens included), so ``cached / input`` is a real hit ratio for every
    provider; adapters normalise their wire format before it reaches here.
    """
    if usage is None:
        return None

    def count(owner: Any, name: str) -> int:
        value = owner.get(name) if isinstance(owner, dict) else getattr(owner, name, None)
        return value if type(value) is int and value >= 0 else 0

    def details(name: str) -> Any:
        value = getattr(usage, name, None)
        return value[-1] if isinstance(value, list) and value else value

    input_tokens = count(usage, "input_tokens")
    output_tokens = count(usage, "output_tokens")
    if not (input_tokens or output_tokens):
        return None
    input_details, output_details = details("input_tokens_details"), details("output_tokens_details")
    cached = count(input_details, "cached_tokens")
    written = count(input_details, "cache_write_tokens")
    input_detail_row = {"cached_tokens": cached}
    if written:
        input_detail_row["cache_write_tokens"] = written
    return {
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": count(usage, "total_tokens") or input_tokens + output_tokens,
            "input_tokens_details": input_detail_row,
            "output_tokens_details": {"reasoning_tokens": count(output_details, "reasoning_tokens")},
        },
        "cache_read_ratio": round(cached / input_tokens, 4) if input_tokens else 0.0,
    }


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


_TACTIC_NUMERIC_KNOBS = frozenset(
    {
        "budget",
        "count",
        "deadline",
        "distance",
        "find_limit",
        "grid_radius",
        "limit",
        "max_distance",
        "max_pages",
        "max_regions",
        "max_steps",
        "page_limit",
        "radius",
        "scan_radius",
        "search_radius",
        "start",
        "timeout",
        "timeout_s",
        "y_radius",
        "yRadius",
    }
)
_TACTIC_POSITION_KEYS = frozenset(
    {
        "block",
        "center",
        "destination",
        "from",
        "goal",
        "origin",
        "pos",
        "position",
        "target",
        "to",
        "x",
        "y",
        "z",
    }
)


def _canonical_args_hash_from_json(input_json: str) -> str:
    return _shared_canonical_args_hash_from_json(input_json)


def _canonical_tool_arguments(input_json: str) -> str:
    return _shared_canonical_tool_arguments_from_json(input_json)


def _tool_tactic_signature_from_json(tool_name: str, input_json: str) -> str:
    return _shared_tool_tactic_signature_from_json(tool_name, input_json)


def _semantic_tactic_payload(value: object, *, key: str | None = None) -> object:
    if isinstance(value, dict):
        semantic: dict[str, object] = {}
        for item_key, item_value in sorted(value.items()):
            clean_key = str(item_key)
            if _is_tactic_knob_key(clean_key):
                continue
            semantic[clean_key] = _semantic_tactic_payload(item_value, key=clean_key)
        return semantic
    if isinstance(value, list):
        if key is not None and _is_tactic_knob_key(key):
            return []
        return [_semantic_tactic_payload(item, key=key) for item in value]
    if isinstance(value, (int, float)) and key is not None and _is_tactic_knob_key(key):
        return None
    return value


def _is_tactic_knob_key(key: str) -> bool:
    lowered = key.casefold()
    if lowered in _TACTIC_POSITION_KEYS or lowered in _TACTIC_NUMERIC_KNOBS:
        return True
    return lowered.endswith(("_pos", "_position", "_radius", "_limit", "_timeout", "_timeout_s"))


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


def _optional_bool_attr(value: object, name: str) -> bool | None:
    raw = getattr(value, name, None)
    return raw if isinstance(raw, bool) else None


__all__ = [
    "AgentRuntime",
    "AgentTurnOutcome",
    "ModelTurnTransportError",
    "RuntimeHooks",
    "RuntimeRunContext",
    "RuntimeTrace",
    "extract_run_observations",
    "sdk_tool_for",
    "tool_is_enabled",
]
