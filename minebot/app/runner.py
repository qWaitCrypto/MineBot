"""openai-agents binding for the Phase-1 runtime spine."""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import CancelledError as FutureCancelledError
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from uuid import uuid4

from agents import Agent, RunConfig, RunContextWrapper, Runner, RunHooks, Session
from agents.exceptions import MaxTurnsExceeded, UserError
from agents.items import ItemHelpers, MessageOutputItem, ToolCallItem, ToolCallOutputItem
from agents.tool import FunctionTool

from minebot.app.conversation import WindowedConversationSession, bounded_session_input
from minebot.app.model_provider import ModelProviderRegistry
from minebot.app.observation_artifacts import ToolObservationArchive
from minebot.app.progress_epochs import ProgressEpochArchive
from minebot.app.skills import SkillOperationError
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
from minebot.brain.progress import ProgressAuthority, ProgressStep
from minebot.brain.registry import RegisteredTool, ToolRegistry, WeldContext, execute_tool
from minebot.contract import (
    Body,
    ExecutionCancellation,
    ExecutionCancelled,
    JsonObject,
    ProgressAbort,
    ProgressFacts,
    execution_cancellation_scope,
)
from minebot.game.errors import BodyActionTimeoutError, BodyProtocolError

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


class SerialExecutionLane:
    """Run synchronous Body work off-loop and serialize it per runtime."""

    def __init__(self, *, thread_name: str = "minebot-body") -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=thread_name)
        self._futures: dict[Future[Any], ExecutionCancellation] = {}
        self._lock = threading.Lock()
        self._closed = False

    async def run(
        self,
        callback: Callable[..., Any],
        *args: object,
        timeout_s: float | None = None,
    ) -> Any:
        if timeout_s is not None and timeout_s <= 0:
            raise ValueError("execution timeout_s must be > 0")
        submitted_at = time.monotonic()
        started = threading.Event()
        started_at: list[float] = []
        cancellation = ExecutionCancellation()

        def invoke() -> Any:
            started_at.append(time.monotonic())
            started.set()
            with execution_cancellation_scope(cancellation):
                return callback(*args)

        with self._lock:
            if self._closed:
                raise RuntimeError("execution lane is closed")
            future = self._executor.submit(invoke)
            self._futures[future] = cancellation
        future.add_done_callback(self._discard)
        try:
            while not future.done():
                if timeout_s is not None and started.is_set():
                    elapsed_s = time.monotonic() - started_at[0]
                    if elapsed_s >= timeout_s:
                        cancellation.cancel("execution_timeout")
                        future.cancel()
                        raise ToolExecutionTimeout(
                            timeout_s=timeout_s,
                            execution_elapsed_s=elapsed_s,
                            queue_wait_s=started_at[0] - submitted_at,
                        )
                await asyncio.sleep(EXECUTION_LANE_POLL_S)
            if future.cancelled():
                raise asyncio.CancelledError
            try:
                return future.result()
            except (FutureCancelledError, ExecutionCancelled) as exc:
                raise asyncio.CancelledError from exc
        except asyncio.CancelledError:
            cancellation.cancel("asyncio_cancelled")
            future.cancel()
            raise

    def request_cancel(self, reason: str) -> int:
        """Signal every running or queued callback without violating serialization."""

        with self._lock:
            pending = list(self._futures.items())
        cancellation_scope_count = sum(
            not future.done() for future, _cancellation in pending
        )
        for future, cancellation in pending:
            cancellation.cancel(reason)
            future.cancel()
        return cancellation_scope_count

    async def wait_idle(self, *, timeout_s: float = EXECUTION_LANE_CANCEL_TIMEOUT_S) -> bool:
        deadline = time.monotonic() + max(0.0, timeout_s)
        while self.active_count:
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(EXECUTION_LANE_POLL_S)
        return True

    @property
    def active_count(self) -> int:
        with self._lock:
            return sum(not future.done() for future in self._futures)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.request_cancel("execution_lane_closed")
        self._executor.shutdown(wait=True, cancel_futures=True)

    def _discard(self, future: Future[Any]) -> None:
        with self._lock:
            self._futures.pop(future, None)


class ToolExecutionTimeout(TimeoutError):
    """A tool exceeded its budget after entering the serialized execution lane."""

    def __init__(
        self,
        *,
        timeout_s: float,
        execution_elapsed_s: float,
        queue_wait_s: float,
    ) -> None:
        super().__init__(f"tool execution exceeded {timeout_s:.3f}s")
        self.diagnostics = {
            "timeout_s": timeout_s,
            "execution_elapsed_s": execution_elapsed_s,
            "queue_wait_s": queue_wait_s,
        }


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
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def emit(self, event: str, **fields: object) -> None:
        with self._lock:
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
        with self._lock:
            return [dict(event) for event in self.events]

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._seq

    def close(self) -> None:
        with self._lock:
            if self.sink is not None:
                self.sink.close()


@dataclass(frozen=True)
class _ModelFunctionCall:
    tool_call_id: str
    tool_name: str
    arguments: str


@dataclass
class _ProgressEpochMember:
    tool_call_id: str
    tool_name: str
    order: int
    body_mutating: bool
    arguments: str = ""
    claimed: bool = False
    conflict: bool = False
    status: str = "pending"
    success: bool | None = None
    reason: str = ""
    progress_steps: tuple[ProgressStep, ...] = ()
    observation_handle: str | None = None
    epistemic_keys: tuple[str, ...] = ()
    pending_abort: ProgressAbort | None = None


@dataclass
class _ProgressEpoch:
    epoch_id: str
    run_id: str
    model_turn: int
    pre_body_fingerprint: str | None
    members: list[_ProgressEpochMember]
    finalized: bool = False


def _collapsed_epoch_progress_steps(
    members: list[_ProgressEpochMember],
) -> tuple[ProgressStep, ...]:
    steps = [step for member in members for step in member.progress_steps]
    if not steps:
        return ()
    action_key = (
        "progress_epoch",
        tuple(step.action_key for step in steps),
    )
    final_fingerprint = next(
        (step.fingerprint for step in reversed(steps) if step.fingerprint),
        "",
    )
    non_neutral_notes = [
        step for step in steps if step.kind == "note" and not step.neutral
    ]
    if non_neutral_notes:
        terminal = non_neutral_notes[-1]
        return (
            ProgressStep(
                "note",
                action_key,
                final_fingerprint or terminal.fingerprint,
                success=terminal.success,
            ),
        )
    observations = [step for step in steps if step.kind == "observe"]
    if observations:
        terminal = observations[-1]
        return (
            ProgressStep(
                "observe",
                action_key,
                final_fingerprint or terminal.fingerprint,
            ),
        )
    terminal = steps[-1]
    return (
        ProgressStep(
            "note",
            action_key,
            final_fingerprint or terminal.fingerprint,
            success=terminal.success,
            neutral=True,
        ),
    )


class ProgressEpochAdapter:
    """Bind one complete SDK model-response tool batch to one progress commit."""

    def __init__(
        self,
        *,
        runtime: "AgentRuntime",
        run_id: str,
        archive: ProgressEpochArchive | None = None,
    ) -> None:
        self.runtime = runtime
        self.run_id = run_id
        self.archive = archive
        self._model_turn = 0
        self._active: _ProgressEpoch | None = None
        self._members: dict[str, _ProgressEpochMember] = {}
        self._seen_epistemic_keys: set[str] = set()

    async def open(self, response: Any) -> None:
        calls = [
            call
            for call in _model_function_calls(response)
            if call.tool_name in self.runtime.registry
        ]
        if not calls:
            return
        if self._active is not None and not self._active.finalized:
            self.finalize_unsettled("next_model_response")
        self._model_turn += 1
        pre_fingerprint = await self.runtime.read_progress_fingerprint()
        body_calls = [
            call
            for call in calls
            if self.runtime.registry.get(call.tool_name).sidecar.can_mutate_body
        ]
        conflicting_ids = (
            {call.tool_call_id for call in body_calls}
            if len(body_calls) > 1
            else set()
        )
        members = [
            _ProgressEpochMember(
                tool_call_id=call.tool_call_id,
                tool_name=call.tool_name,
                order=index,
                body_mutating=self.runtime.registry.get(call.tool_name).sidecar.can_mutate_body,
                arguments=call.arguments,
                conflict=call.tool_call_id in conflicting_ids,
            )
            for index, call in enumerate(calls)
        ]
        epoch = _ProgressEpoch(
            epoch_id=f"epoch-{uuid4().hex}",
            run_id=self.run_id,
            model_turn=self._model_turn,
            pre_body_fingerprint=pre_fingerprint,
            members=members,
        )
        self._active = epoch
        self._members = {member.tool_call_id: member for member in members}
        self.runtime.trace.emit(
            "progress_epoch_opened",
            epoch_id=epoch.epoch_id,
            run_id=self.run_id,
            model_turn=self._model_turn,
            member_tool_call_ids=[member.tool_call_id for member in members],
            member_tools=[member.tool_name for member in members],
            body_mutating_count=len(body_calls),
            body_batch_conflict=bool(conflicting_ids),
            pre_body_fingerprint=pre_fingerprint,
        )

    def claim_member(
        self,
        tool_name: str,
        input_json: str,
        *,
        native_tool_call_id: str | None = None,
    ) -> _ProgressEpochMember | None:
        member = (
            self._members.get(native_tool_call_id)
            if native_tool_call_id is not None
            else None
        )
        if member is not None and member.tool_name != tool_name:
            self.runtime.trace.emit(
                "progress_epoch_member_mismatch",
                tool_call_id=native_tool_call_id,
                expected_tool=member.tool_name,
                actual_tool=tool_name,
            )
            return None
        if member is None:
            candidates = [
                candidate
                for candidate in (self._active.members if self._active is not None else ())
                if not candidate.claimed and candidate.tool_name == tool_name
            ]
            normalized_input = _canonical_tool_arguments(input_json)
            exact = [
                candidate
                for candidate in candidates
                if _canonical_tool_arguments(candidate.arguments) == normalized_input
            ]
            member = (exact or candidates or [None])[0]
        if member is None or member.claimed:
            return None
        member.claimed = True
        return member

    def conflict_result(self, member: _ProgressEpochMember) -> JsonObject:
        assert self._active is not None
        conflicts = [
            {
                "tool_call_id": candidate.tool_call_id,
                "tool": candidate.tool_name,
            }
            for candidate in self._active.members
            if candidate.conflict
        ]
        return {
            "success": False,
            "reason": "body_batch_conflict",
            "canRetry": True,
            "nextSuggestion": "Issue at most one Body-mutating tool in the next model response.",
            "metrics": {
                "epoch_id": self._active.epoch_id,
                "tool_call_id": member.tool_call_id,
                "conflicts": conflicts,
            },
        }

    def rejection_steps(
        self,
        member: _ProgressEpochMember,
        *,
        reason: str,
    ) -> tuple[ProgressStep, ...]:
        epoch = self._active
        if epoch is None:
            return ()
        fingerprint = (
            epoch.pre_body_fingerprint
            or self.runtime.authority.current_fingerprint
            or self.runtime.authority.last_fingerprint
        )
        if not fingerprint:
            return ()
        return (
            ProgressStep(
                "note",
                ("epoch_rejection", reason, member.tool_name, member.tool_call_id),
                fingerprint,
                success=False,
            ),
        )

    def settle(
        self,
        member: _ProgressEpochMember,
        *,
        result: JsonObject,
        model_result: JsonObject,
        progress_steps: tuple[ProgressStep, ...] = (),
        status: str | None = None,
        pending_abort: ProgressAbort | None = None,
    ) -> ProgressAbort | None:
        epoch = self._active
        if epoch is None or epoch.finalized:
            return None
        if member.status != "pending":
            self.runtime.trace.emit(
                "progress_epoch_member_duplicate_settlement",
                epoch_id=epoch.epoch_id,
                tool_call_id=member.tool_call_id,
                status=member.status,
            )
            return None
        member.status = status or ("success" if bool(result.get("success")) else "failure")
        member.success = bool(result.get("success"))
        member.reason = str(result.get("reason") or "")
        member.progress_steps = progress_steps
        handle = model_result.get("observationHandle")
        member.observation_handle = str(handle) if isinstance(handle, str) and handle else None
        member.epistemic_keys = _explicit_evidence_keys(result)
        member.pending_abort = pending_abort
        self.runtime.trace.emit(
            "progress_epoch_member_settled",
            epoch_id=epoch.epoch_id,
            tool_call_id=member.tool_call_id,
            tool=member.tool_name,
            status=member.status,
            reason=member.reason,
            progress_step_count=len(progress_steps),
            observation_handle=member.observation_handle,
        )
        if any(candidate.status == "pending" for candidate in epoch.members):
            return None
        return self._finalize(epoch)

    def cancel_member(self, member: _ProgressEpochMember, reason: str) -> ProgressAbort | None:
        result: JsonObject = {
            "success": False,
            "reason": reason,
            "canRetry": True,
            "nextSuggestion": None,
            "metrics": {},
        }
        return self.settle(
            member,
            result=result,
            model_result=result,
            status="cancelled",
        )

    def finalize_unsettled(self, reason: str) -> ProgressAbort | None:
        epoch = self._active
        if epoch is None or epoch.finalized:
            return None
        for member in epoch.members:
            if member.status != "pending":
                continue
            member.status = "cancelled"
            member.success = False
            member.reason = reason
            self.runtime.trace.emit(
                "progress_epoch_member_settled",
                epoch_id=epoch.epoch_id,
                tool_call_id=member.tool_call_id,
                tool=member.tool_name,
                status="cancelled",
                reason=reason,
                progress_step_count=0,
                observation_handle=None,
            )
        return self._finalize(epoch)

    def _finalize(self, epoch: _ProgressEpoch) -> ProgressAbort | None:
        if epoch.finalized:
            return None
        ordered = sorted(epoch.members, key=lambda member: member.order)
        steps = [step for member in ordered for step in member.progress_steps]
        committed_steps = _collapsed_epoch_progress_steps(ordered)
        post_fingerprint = next(
            (
                step.fingerprint
                for member in reversed(ordered)
                for step in reversed(member.progress_steps)
                if step.fingerprint
            ),
            self.runtime.authority.current_fingerprint or epoch.pre_body_fingerprint,
        )
        evidence_refs = [
            member.observation_handle
            for member in ordered
            if member.observation_handle is not None
        ]
        epistemic_keys = list(
            dict.fromkeys(
                key
                for member in ordered
                for key in member.epistemic_keys
            )
        )
        material_changed = bool(
            epoch.pre_body_fingerprint
            and post_fingerprint
            and epoch.pre_body_fingerprint != post_fingerprint
        )
        local_novel_epistemic_keys = [
            key for key in epistemic_keys if key not in self._seen_epistemic_keys
        ]
        novel_epistemic_keys = (
            local_novel_epistemic_keys if self.archive is None else []
        )
        record: dict[str, object] = {
            "epoch_id": epoch.epoch_id,
            "run_id": epoch.run_id,
            "model_turn": epoch.model_turn,
            "members": [
                {
                    "tool_call_id": member.tool_call_id,
                    "tool": member.tool_name,
                    "status": member.status,
                    "success": member.success,
                    "reason": member.reason,
                    "body_mutating": member.body_mutating,
                    "progress_step_count": len(member.progress_steps),
                    "observation_handle": member.observation_handle,
                }
                for member in ordered
            ],
            "pre_body_fingerprint": epoch.pre_body_fingerprint,
            "post_body_fingerprint": post_fingerprint,
            "evidence_refs": evidence_refs,
            "epistemic_keys": epistemic_keys,
            "novel_epistemic_keys": local_novel_epistemic_keys,
            "material_changed": material_changed,
            "progress_aborted": False,
            "captured_progress_step_count": len(steps),
            "committed_progress_step_count": len(committed_steps),
        }
        cursor: int | None = None
        if self.archive is not None:
            try:
                stored = self.archive.store(record)
                cursor = int(stored.get("cursor") or 0) or None
                stored_novel = stored.get("novel_epistemic_keys")
                if isinstance(stored_novel, list) and all(
                    isinstance(key, str) for key in stored_novel
                ):
                    novel_epistemic_keys = list(stored_novel)
                    record["novel_epistemic_keys"] = novel_epistemic_keys
            except Exception as exc:
                record["novel_epistemic_keys"] = []
                self.runtime.trace.emit(
                    "progress_epoch_archive_failed",
                    epoch_id=epoch.epoch_id,
                    error_type=type(exc).__name__,
                )
        self._seen_epistemic_keys.update(epistemic_keys)

        progress_abort: ProgressAbort | None = None
        try:
            self.runtime.authority.commit_steps(
                committed_steps,
                self.runtime.agent_context.goal_text,
                novel_epistemic_keys=novel_epistemic_keys,
                material_changed=material_changed,
            )
        except ProgressAbort as exc:
            progress_abort = exc
        if progress_abort is None:
            progress_abort = next(
                (
                    member.pending_abort
                    for member in ordered
                    if member.pending_abort is not None
                ),
                None,
            )
        record["progress_aborted"] = progress_abort is not None
        record["epistemic_steps"] = self.runtime.authority.epistemic_steps
        if progress_abort is not None and self.archive is not None:
            mark_aborted = getattr(self.archive, "mark_progress_aborted", None)
            if callable(mark_aborted):
                try:
                    mark_aborted(epoch.epoch_id)
                except Exception as exc:
                    self.runtime.trace.emit(
                        "progress_epoch_archive_failed",
                        epoch_id=epoch.epoch_id,
                        operation="mark_progress_aborted",
                        error_type=type(exc).__name__,
                    )
        epoch.finalized = True
        self.runtime.trace.emit(
            "progress_epoch_settled",
            **record,
            cursor=cursor,
        )
        self._members = {}
        self._active = None
        return progress_abort


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


def sdk_tool_for(tool: RegisteredTool) -> FunctionTool:
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

        def finalize(
            result: JsonObject,
            *,
            progress_steps: tuple[ProgressStep, ...] = (),
            status: str | None = None,
            pending_abort: ProgressAbort | None = None,
            raise_progress_abort: bool = True,
        ) -> JsonObject:
            model_result = _finalize_tool_payload(
                tool=tool,
                result=result,
                trace=trace,
                tool_call_id=tool_call_id,
                runtime=runtime,
            )
            if epoch_adapter is not None and epoch_member is not None:
                abort = epoch_adapter.settle(
                    epoch_member,
                    result=result,
                    model_result=model_result,
                    progress_steps=progress_steps,
                    status=status,
                    pending_abort=pending_abort,
                )
                if abort is not None and raise_progress_abort:
                    raise abort
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
                arguments_summary=arguments_summary,
            )
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
        try:
            if runtime is not None and epoch_member is not None:
                captured = await runtime.run_sync(
                    _capture_tool_execution,
                    tool,
                    params,
                    ctx.context,
                    timeout_s=tool.sidecar.timeout_s,
                )
                progress_steps = captured.progress_steps
                if captured.error is not None:
                    raise captured.error
                assert captured.result is not None
                result = captured.result
            elif runtime is None:
                result = execute_tool(tool, params, ctx.context.weld_context)
            else:
                result = await runtime.run_sync(
                    execute_tool,
                    tool,
                    params,
                    ctx.context.weld_context,
                    timeout_s=tool.sidecar.timeout_s,
                )
        except asyncio.CancelledError:
            if runtime is not None:
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
                if runtime is not None:
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

        if runtime is not None:
            runtime.remember_tool_result(tool.name, result)
            runtime.remember_tool_body_facts(result)
        return finalize(
            result,
            progress_steps=progress_steps,
            status=settlement_status,
            pending_abort=pending_abort,
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
        timeout_seconds=None,
        _failure_error_function=None,
        _use_default_failure_error_function=False,
    )


@dataclass(frozen=True)
class _CapturedToolExecution:
    result: JsonObject | None
    progress_steps: tuple[ProgressStep, ...]
    error: Exception | None = None


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
    return _CapturedToolExecution(result, tuple(captured))


def _finalize_tool_payload(
    *,
    tool: RegisteredTool,
    result: JsonObject,
    trace: RuntimeTrace | None,
    tool_call_id: str,
    runtime: "AgentRuntime | None" = None,
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
    )
    if trace is not None:
        trace.emit(
            "tool_result",
            tool_call_id=tool_call_id,
            tool=tool.name,
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
    return "retry after refreshing state; choose a different action if the same failure repeats"


def _progress_abort_metrics(exc: ProgressAbort) -> JsonObject:
    facts = exc.facts
    if facts is None:
        return {}
    return {
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


def _model_tool_payload(
    tool_name: str,
    result: JsonObject,
    *,
    trace_ref: str,
    observation_handle: str | None = None,
) -> JsonObject:
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
    if observation_handle is not None:
        payload["observationHandle"] = observation_handle
    payload["projection"] = _projection_metadata(
        metrics,
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


def _projection_values_equal(source: object, projected: object) -> bool:
    try:
        return sanitize_observation(source) == sanitize_observation(projected)
    except Exception:
        return False


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
    if tool_name in {"read_task", "update_plan", "checkpoint_task"}:
        summary["task_artifact"] = _task_artifact_summary(metrics)
    elif tool_name == "query_conversation_archive":
        summary.update(_conversation_archive_query_summary(metrics))
    elif tool_name == "read_conversation_archive":
        summary.update(_conversation_archive_turn_summary(metrics))
    elif tool_name == "query_tool_observations":
        summary.update(_tool_observation_query_summary(metrics))
    elif tool_name == "read_tool_observation":
        summary.update(_tool_observation_read_summary(metrics))
    elif tool_name in {
        "search_memory",
        "read_memory",
        "write_memory",
        "update_memory",
        "delete_memory",
    }:
        summary.update(_memory_tool_summary(tool_name, metrics))
    elif tool_name in {
        "list_skills",
        "read_skill",
        "load_skill",
        "create_skill",
        "update_skill",
        "delete_skill",
    }:
        summary.update(_skill_tool_summary(tool_name, metrics))
    elif tool_name in {"wiki_search", "wiki_read"}:
        summary.update(_wiki_tool_summary(tool_name, metrics))
    elif tool_name == "explore_for":
        summary.update(_exploration_tool_summary(metrics))
    elif tool_name == "collect_block_domain":
        summary.update(_resource_domain_tool_summary(reason, metrics))
    for key in allowed_keys:
        if key in metrics:
            summary[key] = _bounded_summary_value(metrics[key])
    if isinstance(metrics.get("counts"), dict):
        count_items = sorted(metrics["counts"].items(), key=lambda item: str(item[0]))
        visible_items = count_items[:MODEL_COUNT_MAP_LIMIT]
        summary["counts"] = {
            str(key): _bounded_summary_value(value)
            for key, value in visible_items
        }
        summary["distinct_item_count"] = len(count_items)
        summary["counts_complete"] = len(visible_items) == len(count_items)
        if len(visible_items) < len(count_items):
            summary["omitted_item_count"] = len(count_items) - len(visible_items)
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


def _resource_domain_tool_summary(reason: str, metrics: dict[str, object]) -> JsonObject:
    attempts = metrics.get("attempts")
    searches = metrics.get("searches")
    blocker_counts: dict[str, int] = {}
    governance_counts: dict[str, int] = {}
    movement_counts: dict[str, int] = {}
    final_pos: object | None = None
    selected_goal: object | None = None
    capability_snapshot: dict[str, object] | None = None

    def count_reason(counts: dict[str, int], value: object) -> None:
        normalized = str(value or "").strip()
        if normalized:
            counts[normalized] = counts.get(normalized, 0) + 1

    if isinstance(attempts, list):
        for attempt in attempts:
            if not isinstance(attempt, dict):
                continue
            for phase in ("navigation", "mine"):
                phase_result = attempt.get(phase)
                if not isinstance(phase_result, dict) or phase_result.get("success") is True:
                    continue
                count_reason(blocker_counts, phase_result.get("reason"))
            navigation = attempt.get("navigation")
            if not isinstance(navigation, dict):
                continue
            navigation_metrics = navigation.get("metrics")
            if not isinstance(navigation_metrics, dict):
                continue
            if navigation_metrics.get("selected_goal") is not None:
                selected_goal = navigation_metrics["selected_goal"]
            if navigation_metrics.get("final_pos") is not None:
                final_pos = navigation_metrics["final_pos"]
            segments = navigation_metrics.get("segments")
            if not isinstance(segments, list):
                continue
            for segment in segments:
                if not isinstance(segment, dict):
                    continue
                diagnostics = segment.get("diagnostics")
                if not isinstance(diagnostics, dict):
                    continue
                event_data = diagnostics.get("event_data")
                if isinstance(event_data, dict) and event_data.get("final_pos") is not None:
                    final_pos = event_data["final_pos"]
                segment_counts = diagnostics.get("movement_counts")
                if isinstance(segment_counts, dict):
                    for movement, value in segment_counts.items():
                        if isinstance(value, (int, float)) and int(value) > 0:
                            key = str(movement)
                            movement_counts[key] = movement_counts.get(key, 0) + int(value)
                snapshot = diagnostics.get("capability_snapshot")
                if isinstance(snapshot, dict):
                    capability_snapshot = snapshot
                mutation_events = diagnostics.get("mutation_events")
                if not isinstance(mutation_events, list):
                    continue
                for event in mutation_events:
                    if not isinstance(event, dict):
                        continue
                    data = event.get("data")
                    if not isinstance(data, dict) or data.get("success") is not False:
                        continue
                    count_reason(governance_counts, data.get("decision_reason") or data.get("reason"))

    summary: JsonObject = {}
    if blocker_counts:
        summary["process_blockers"] = _bounded_reason_counts(blocker_counts)
    if governance_counts:
        summary["governance_blockers"] = _bounded_reason_counts(governance_counts)
    if movement_counts:
        summary["movement_counts"] = {
            key: movement_counts[key]
            for key in sorted(movement_counts)
        }
    if final_pos is not None:
        summary["final_pos"] = _bounded_summary_value(final_pos)
    if selected_goal is not None:
        summary["selected_goal"] = _bounded_summary_value(selected_goal)
    if capability_snapshot is not None:
        summary["capability_snapshot"] = {
            key: _bounded_summary_value(capability_snapshot[key])
            for key in (
                "allow_break",
                "allow_place",
                "allow_pillar",
                "allow_downward",
                "allow_swim",
                "break_budget",
                "place_budget",
                "pillar_budget",
                "downward_budget",
                "scaffold_item",
                "scaffold_count",
            )
            if key in capability_snapshot
        }
    if isinstance(searches, list):
        summary["search_count"] = len(searches)
        summary["search_truncated"] = any(
            isinstance(search, dict) and search.get("truncated") is True
            for search in searches
        )
        search_uncertainty = [
            uncertainty
            for search in searches
            if isinstance(search, dict)
            for uncertainty in (search.get("uncertainty") or [])
            if isinstance(uncertainty, dict)
        ]
        if search_uncertainty:
            summary["search_uncertainty"] = _top_reasons(search_uncertainty)
    if reason == "resource_domain_budget_exhausted" and final_pos is not None:
        summary["resume_hint"] = (
            "bounded resource work ended after physical progress; retry the same Body domain from final_pos "
            "or choose a different domain or prerequisite from the blocker facts"
        )
    return summary


def _bounded_reason_counts(counts: dict[str, int]) -> list[str]:
    return [
        f"{reason}:{count}"
        for reason, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:6]
    ]


def _exploration_tool_summary(metrics: dict[str, object]) -> JsonObject:
    summary: JsonObject = {
        key: _bounded_summary_value(metrics[key])
        for key in (
            "dimension",
            "origin",
            "final_pos",
            "budget",
            "coverage_revision",
            "resume_cursor",
            "complete",
        )
        if key in metrics
    }
    if "continuation" in metrics:
        continuation = sanitize_observation(metrics["continuation"])
        summary["continuation"] = (
            continuation
            if isinstance(continuation, dict) or continuation is None
            else _bounded_summary_value(continuation)
        )
    targets = metrics.get("targets")
    if isinstance(targets, dict):
        summary["targets"] = _bounded_summary_value(targets.get("requested") or targets)
    covered = metrics.get("covered_regions")
    if isinstance(covered, list):
        summary["covered_region_count"] = len(covered)
        summary["covered_regions"] = [
            _bounded_summary_value(item) for item in covered[:16]
        ]
        summary["covered_regions_complete"] = len(covered) <= 16
    for field, count_field in (("blocks", "block_count"), ("entities", "entity_count")):
        values = metrics.get(field)
        if not isinstance(values, list):
            continue
        summary[count_field] = len(values)
        summary[field] = [_bounded_summary_value(item) for item in values[:8]]
        summary[f"{field}_complete"] = len(values) <= 8
    failures = metrics.get("candidate_failures")
    if isinstance(failures, list):
        summary["candidate_failure_count"] = len(failures)
        summary["candidate_failure_reasons"] = _top_reasons(failures)
    evidence_keys = metrics.get("evidence_keys")
    if isinstance(evidence_keys, list):
        summary["evidence_key_count"] = len(evidence_keys)
    if "source_reason" in metrics:
        summary["source_reason"] = _bounded_summary_value(metrics["source_reason"])
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
        projected = _bounded_summary_value(safe_value)
        if not _projection_values_equal(safe_value, projected):
            continue
        encoded = json.dumps(projected, ensure_ascii=False, sort_keys=True, default=str)
        if len(encoded) <= 1200:
            summary[key] = projected


def _conversation_archive_query_summary(metrics: dict[str, object]) -> JsonObject:
    summary: JsonObject = {
        key: _bounded_summary_value(metrics[key])
        for key in (
            "query",
            "start",
            "limit",
            "total_matches",
            "next_start",
            "complete",
        )
        if key in metrics
    }
    results = metrics.get("results")
    if not isinstance(results, list):
        return summary
    visible = [
        {
            key: _bounded_summary_value(result[key])
            for key in (
                "handle",
                "turn",
                "user",
                "assistant",
                "tools",
                "tool_reasons",
                "item_count",
            )
            if key in result
        }
        for result in results[:10]
        if isinstance(result, dict)
    ]
    summary["results"] = visible
    summary["results_complete"] = len(visible) == len(results)
    if len(visible) < len(results):
        summary["omitted_result_count"] = len(results) - len(visible)
    return summary


def _conversation_archive_turn_summary(metrics: dict[str, object]) -> JsonObject:
    summary: JsonObject = {
        key: _bounded_summary_value(metrics[key])
        for key in (
            "handle",
            "turn",
            "start",
            "limit",
            "item_count",
            "next_start",
            "complete",
        )
        if key in metrics
    }
    items = metrics.get("items")
    if not isinstance(items, list):
        return summary
    visible = [_conversation_item_summary(item) for item in items[:8]]
    summary["items"] = visible
    summary["items_complete"] = len(visible) == len(items)
    if len(visible) < len(items):
        summary["omitted_page_item_count"] = len(items) - len(visible)
    return summary


def _conversation_item_summary(item: object) -> object:
    if not isinstance(item, dict):
        return _bounded_summary_value(item)
    return {
        key: _bounded_summary_value(item[key])
        for key in ("type", "role", "call_id", "id", "name", "content", "output")
        if key in item
    }


def _tool_observation_query_summary(metrics: dict[str, object]) -> JsonObject:
    summary: JsonObject = {
        key: _bounded_summary_value(metrics[key])
        for key in (
            "query",
            "tool",
            "reason",
            "start",
            "limit",
            "total_matches",
            "next_start",
            "complete",
        )
        if key in metrics
    }
    results = metrics.get("results")
    if not isinstance(results, list):
        return summary
    visible = [
        {
            key: _bounded_summary_value(result[key])
            for key in (
                "handle",
                "tool",
                "tool_call_id",
                "success",
                "reason",
                "complete",
                "payload_bytes",
                "created_at",
            )
            if key in result
        }
        for result in results[:20]
        if isinstance(result, dict)
    ]
    summary["results"] = visible
    summary["results_complete"] = len(visible) == len(results)
    if len(visible) < len(results):
        summary["omitted_result_count"] = len(results) - len(visible)
    return summary


def _tool_observation_read_summary(metrics: dict[str, object]) -> JsonObject:
    summary: JsonObject = {
        key: _bounded_summary_value(metrics[key])
        for key in (
            "handle",
            "tool",
            "tool_call_id",
            "success",
            "reason",
            "source_complete",
            "payload_bytes",
            "created_at",
            "path",
            "value_type",
            "start",
            "limit",
            "max_chars",
            "total_count",
            "char_count",
            "next_start",
            "omitted_count",
            "complete",
        )
        if key in metrics
    }
    if "value" in metrics:
        value = metrics["value"]
        if isinstance(value, str):
            summary["value"] = _shorten(value, limit=4000)
            summary["value_complete"] = len(summary["value"]) == len(value)
        else:
            summary["value"] = _bounded_summary_value(value)
            summary["value_complete"] = _projection_values_equal(value, summary["value"])
    items = metrics.get("items")
    if isinstance(items, list):
        visible = [_bounded_summary_value(item) for item in items[:12]]
        summary["items"] = visible
        summary["items_complete"] = len(visible) == len(items) and all(
            _projection_values_equal(source, projected)
            for source, projected in zip(items, visible, strict=True)
        )
        if len(visible) < len(items):
            summary["omitted_page_item_count"] = len(items) - len(visible)
    return summary


def _memory_tool_summary(tool_name: str, metrics: dict[str, object]) -> JsonObject:
    summary: JsonObject = {
        key: _bounded_summary_value(metrics[key])
        for key in (
            "memory_id",
            "revision",
            "kind",
            "source",
            "subject_key",
            "title",
            "evidence_ref",
            "dimension",
            "point",
            "region",
            "query",
            "filters",
            "start",
            "limit",
            "candidate_count",
            "next_start",
            "complete",
            "candidate_truncated",
            "lanes",
            "error",
        )
        if key in metrics
    }
    results = metrics.get("results")
    if isinstance(results, list):
        visible = [
            _memory_record_summary(item, include_content=False)
            for item in results[:8]
            if isinstance(item, dict)
        ]
        summary["results"] = visible
        summary["results_complete"] = len(visible) == len(results)
        if len(visible) < len(results):
            summary["omitted_result_count"] = len(results) - len(visible)
    elif tool_name in {"read_memory", "write_memory", "update_memory"}:
        summary.update(
            _memory_record_summary(
                metrics,
                include_content=tool_name == "read_memory",
            )
        )
    return summary


def _memory_record_summary(
    record: dict[str, object],
    *,
    include_content: bool,
) -> JsonObject:
    summary: JsonObject = {
        key: _bounded_summary_value(record[key])
        for key in (
            "memory_id",
            "revision",
            "kind",
            "source",
            "subject_key",
            "title",
            "evidence_ref",
            "dimension",
            "point",
            "region",
            "updated_at",
            "retrieval_score",
            "match_lanes",
            "distance",
            "content_truncated",
        )
        if key in record
    }
    excerpt = record.get("excerpt")
    if isinstance(excerpt, str):
        summary["excerpt"] = _shorten(excerpt, limit=500)
        summary["excerpt_complete"] = len(summary["excerpt"]) == len(excerpt)
    if include_content and isinstance(record.get("content"), str):
        content = str(record["content"])
        summary["content"] = _shorten(content, limit=4000)
        summary["content_complete"] = len(summary["content"]) == len(content)
    return summary


def _skill_tool_summary(tool_name: str, metrics: dict[str, object]) -> JsonObject:
    summary: JsonObject = {
        key: _bounded_summary_value(metrics[key])
        for key in (
            "name",
            "description",
            "version",
            "head_version",
            "revision",
            "origin",
            "status",
            "tools",
            "loadable",
            "missing_tools",
            "derived_from",
            "count",
            "total_matches",
            "start",
            "limit",
            "next_start",
            "complete",
            "error",
            "retired_at",
            "reason",
            "evidence_refs",
            "change_reason",
        )
        if key in metrics
    }
    skills = metrics.get("skills")
    if isinstance(skills, list):
        visible = [
            {
                key: _bounded_summary_value(item[key])
                for key in (
                    "name",
                    "description",
                    "version",
                    "head_version",
                    "revision",
                    "origin",
                    "loadable",
                    "missing_tools",
                )
                if key in item
            }
            for item in skills[:10]
            if isinstance(item, dict)
        ]
        summary["skills"] = visible
        summary["skills_complete"] = len(visible) == len(skills)
        if len(visible) < len(skills):
            summary["omitted_skill_count"] = len(skills) - len(visible)
    if tool_name in {"read_skill", "load_skill"} and isinstance(metrics.get("instructions"), str):
        instructions = str(metrics["instructions"])
        summary["instructions"] = _shorten(instructions, limit=8000)
        summary["instructions_complete"] = len(summary["instructions"]) == len(instructions)
    activation = metrics.get("activation")
    if isinstance(activation, dict):
        summary["activation"] = {
            key: _bounded_summary_value(activation[key])
            for key in (
                "activation_id",
                "task_id",
                "owner_kind",
                "owner_id",
                "skill_id",
                "skill_name",
                "skill_version",
                "activated_at",
                "ended_at",
            )
            if key in activation
        }
    return summary


def _wiki_tool_summary(tool_name: str, metrics: dict[str, object]) -> JsonObject:
    summary: JsonObject = {
        key: _bounded_summary_value(metrics[key])
        for key in (
            "query",
            "count",
            "title",
            "source",
            "source_url",
            "revision_id",
            "revision_timestamp",
            "retrieved_at",
            "omitted_sections",
            "complete",
            "stale",
            "cache_status",
            "cache_fetched_at",
            "refresh_error",
            "advisory",
            "error",
        )
        if key in metrics
    }
    results = metrics.get("results")
    if isinstance(results, list):
        visible = [
            {
                key: (
                    _shorten(str(item[key]), limit=500)
                    if key == "snippet"
                    else _bounded_summary_value(item[key])
                )
                for key in ("title", "snippet", "page_id", "word_count")
                if key in item
            }
            for item in results[:8]
            if isinstance(item, dict)
        ]
        summary["results"] = visible
        summary["results_complete"] = len(visible) == len(results)
        if len(visible) < len(results):
            summary["omitted_result_count"] = len(results) - len(visible)
    if tool_name == "wiki_read" and isinstance(metrics.get("markdown"), str):
        markdown = str(metrics["markdown"])
        summary["markdown"] = _shorten(markdown, limit=6000)
        summary["markdown_complete"] = len(summary["markdown"]) == len(markdown)
    return summary


def _task_artifact_summary(metrics: dict[str, object]) -> JsonObject:
    current = metrics.get("current")
    source = current if isinstance(current, dict) else metrics
    summary: JsonObject = {}
    if "active" in source:
        summary["active"] = bool(source.get("active"))
    if source.get("scope_key") is not None:
        summary["scope_key"] = str(source.get("scope_key"))

    task = metrics.get("task")
    if not isinstance(task, dict):
        task = source.get("task")
    if isinstance(task, dict):
        summary["task"] = {
            key: _bounded_summary_value(task[key])
            for key in (
                "task_id",
                "revision",
                "goal",
                "status",
                "completion_authority",
                "active_plan_id",
                "latest_checkpoint_id",
            )
            if key in task
        }

    plan = metrics.get("plan")
    if not isinstance(plan, dict):
        plan = source.get("plan")
    if isinstance(plan, dict):
        plan_summary: JsonObject = {
            key: _bounded_summary_value(plan[key])
            for key in ("plan_id", "revision", "summary")
            if key in plan
        }
        steps = plan.get("steps")
        if isinstance(steps, list):
            plan_summary["steps"] = [
                {
                    key: _bounded_summary_value(step[key])
                    for key in ("step_id", "ordinal", "title", "status", "blocker")
                    if key in step
                }
                for step in steps[:16]
                if isinstance(step, dict)
            ]
            plan_summary["step_count"] = len(steps)
            plan_summary["steps_complete"] = len(steps) <= 16
        summary["plan"] = plan_summary

    checkpoint = metrics.get("checkpoint")
    if not isinstance(checkpoint, dict):
        checkpoint = source.get("checkpoint")
    if isinstance(checkpoint, dict):
        summary["checkpoint"] = {
            key: _bounded_summary_value(checkpoint[key])
            for key in (
                "checkpoint_id",
                "revision",
                "disposition",
                "summary",
                "next_step",
                "wait_for",
            )
            if key in checkpoint
        }
    if metrics.get("error") is not None:
        summary["error"] = _bounded_summary_value(metrics["error"])
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
        self._run_evidence_cursor = 0
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
        self.execution_lane = SerialExecutionLane(thread_name=f"minebot-{agent_name}")
        self.context_refreshers: list[Callable[[AgentContext], None]] = []

    def set_tool_facts(self, tool_name: str, facts: dict[str, object]) -> None:
        self.tool_facts[tool_name] = dict(facts)

    def add_context_refresher(self, refresher: Callable[[AgentContext], None]) -> None:
        self.context_refreshers.append(refresher)

    async def run_turn(
        self,
        extra_signals: list[AgentSignal] | None = None,
        *,
        body_actions_allowed: bool = True,
    ) -> AgentTurnOutcome:
        prepared = await self.run_sync(self._prepare_turn, extra_signals)
        if isinstance(prepared, AgentTurnOutcome):
            return prepared
        profile = prepared
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
        instruction_preamble = self.agent_context.turn_preamble(
            include_session_messages=False
        )
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

        self._run_evidence_cursor = self.latest_progress_evidence_cursor()
        run_id = f"run-{uuid4().hex}"
        progress_epochs = ProgressEpochAdapter(
            runtime=self,
            run_id=run_id,
            archive=self.progress_epoch_archive,
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
            return self._yield_from_progress_abort(deferred or exc)
        except BodyRecoveryRequired as exc:
            progress_epochs.finalize_unsettled("body_recovery")
            return self._enter_recovery_from_body_fact(exc.reason, exc.facts)
        except MaxTurnsExceeded as exc:
            progress_epochs.finalize_unsettled("max_turns_exceeded")
            return self._yield_from_runaway_ceiling(exc)
        except UserError as exc:
            deferred = progress_epochs.finalize_unsettled("sdk_user_error")
            progress_abort = _find_progress_abort(exc)
            if progress_abort is None:
                progress_abort = deferred
            if progress_abort is None:
                recovery_required = _find_body_recovery_required(exc)
                if recovery_required is not None:
                    return self._enter_recovery_from_body_fact(recovery_required.reason, recovery_required.facts)
                raise
            return self._yield_from_progress_abort(progress_abort)
        except SkillOperationError as exc:
            progress_epochs.finalize_unsettled("skill_operation_error")
            return self._yield_from_skill_context_error(exc, profile)
        except Exception:
            progress_epochs.finalize_unsettled("runner_error")
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
        )

    async def run_sync(
        self,
        callback: Callable[..., Any],
        *args: object,
        timeout_s: float | None = None,
    ) -> Any:
        return await self.execution_lane.run(callback, *args, timeout_s=timeout_s)

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

    async def read_progress_fingerprint(self) -> str | None:
        try:
            return await self.run_sync(self._read_progress_fingerprint)
        except Exception as exc:
            self.trace.emit(
                "progress_epoch_fingerprint_failed",
                error_type=type(exc).__name__,
            )
            return self.authority.current_fingerprint or self.authority.last_fingerprint or None

    def _read_progress_fingerprint(self) -> str | None:
        state = self.body.get_state()
        if state.missing:
            return self.authority.current_fingerprint or self.authority.last_fingerprint or None
        return self.authority.fingerprint(state)

    async def wait_for_execution_idle(self, *, timeout_s: float = EXECUTION_LANE_CANCEL_TIMEOUT_S) -> bool:
        return await self.execution_lane.wait_idle(timeout_s=timeout_s)

    def request_execution_cancel(self, reason: str) -> int:
        return self.execution_lane.request_cancel(reason)

    async def cancel_active_execution(self, reason: str) -> bool:
        facts = await self.cancel_active_execution_with_facts(reason)
        return bool(facts.get("settled"))

    async def cancel_active_execution_with_facts(self, reason: str) -> JsonObject:
        cancellation_started = time.monotonic()
        self.authority.invalidate_generation(reason)
        cancellation_scope_count = self.request_execution_cancel(reason)
        interrupt_ok: bool | None = None
        interrupt_accepted: bool | None = None
        interrupt_complete: bool | None = None
        interrupt_error: JsonObject | None = None
        try:
            interrupt = await asyncio.to_thread(self.body.interrupt, reason)
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
        settled = execution_idle and owner_settled
        facts: JsonObject = {
            "interrupt_requested": True,
            "cancellation_scope_count": cancellation_scope_count,
            "interrupt_ok": interrupt_ok,
            "interrupt_accepted": interrupt_accepted,
            "interrupt_complete": interrupt_complete,
            "execution_idle": execution_idle,
            **owner_facts,
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
                head = await asyncio.to_thread(read_head, proposed_epoch)
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

    def close(self) -> None:
        self.execution_lane.close()
        close_session = getattr(self.conversation_session, "close", None)
        if callable(close_session):
            close_session()

    async def _supervise_streamed_run(self, streamed: Any) -> Any | AgentTurnOutcome:
        stream_task = asyncio.create_task(self._drain_stream(streamed))
        body_task = asyncio.create_task(self._wait_for_critical_body_fact())
        try:
            done, _ = await asyncio.wait({stream_task, body_task}, return_when=asyncio.FIRST_COMPLETED)
            if body_task in done:
                reason, facts = body_task.result()
                self.authority.invalidate_generation(f"body_critical:{reason}")
                try:
                    await asyncio.to_thread(self.body.interrupt, reason)
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
            idle = await self.wait_for_execution_idle()
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
            try:
                state = await asyncio.to_thread(self.body.get_state)
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
        context = ctx.context.agent_context
        self._refresh_dynamic_context()
        preamble = ctx.context.instruction_preamble
        skill_preamble = context.skill_preamble()
        parts = [context.system_prompt]
        if preamble:
            parts.append(preamble)
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

    def _agent_for_profile(self, profile: RuntimeProfile) -> Agent[RuntimeRunContext]:
        kwargs: dict[str, Any] = {"model": profile.model_route}
        if self.model_provider is not None:
            kwargs["model_settings"] = self.model_provider.model_settings_for(profile.model_route)
        return self.agent.clone(**kwargs)

    def _run_config(self, profile: RuntimeProfile) -> RunConfig:
        if self.model_provider is None:
            return RunConfig(session_input_callback=bounded_session_input)
        return RunConfig(
            model_provider=self.model_provider,
            model_settings=self.model_provider.model_settings_for(profile.model_route),
            session_input_callback=bounded_session_input,
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
        final_text = _final_assistant_text(result, extracted)
        if final_text:
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

    def record_transport_error(
        self,
        tool_name: str,
        result: JsonObject,
        *,
        tool_call_id: str,
        raise_on_limit: bool = True,
    ) -> ProgressAbort | None:
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
            abort = ProgressAbort(
                "body transport unstable: yielding for supervisor review",
                facts=facts,
            )
            if raise_on_limit:
                raise abort
            return abort
        return None

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


def _model_function_calls(response: Any) -> list[_ModelFunctionCall]:
    output = getattr(response, "output", None)
    if not isinstance(output, list):
        return []
    calls: list[_ModelFunctionCall] = []
    for item in output:
        if isinstance(item, dict):
            item_type = str(item.get("type") or "")
            tool_name = item.get("name") or item.get("tool_name")
            tool_call_id = item.get("call_id") or item.get("id")
            arguments = item.get("arguments")
        else:
            item_type = str(getattr(item, "type", None) or "")
            tool_name = getattr(item, "name", None) or getattr(item, "tool_name", None)
            tool_call_id = getattr(item, "call_id", None) or getattr(item, "id", None)
            arguments = getattr(item, "arguments", None)
        if item_type not in {"function_call", "tool_call"}:
            continue
        if not isinstance(tool_name, str) or not tool_name:
            continue
        if not isinstance(tool_call_id, str) or not tool_call_id:
            continue
        calls.append(
            _ModelFunctionCall(
                tool_call_id,
                tool_name,
                arguments if isinstance(arguments, str) else "",
            )
        )
    return calls


def _canonical_tool_arguments(arguments: str) -> str:
    if not arguments:
        return "{}"
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError:
        return arguments.strip()
    return json.dumps(parsed, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _explicit_evidence_keys(result: JsonObject) -> tuple[str, ...]:
    metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
    raw_values: list[object] = []
    for container in (result, metrics):
        for field_name in ("evidence_keys", "evidenceKeys"):
            value = container.get(field_name)
            if isinstance(value, (list, tuple)):
                raw_values.extend(value)
            elif value is not None:
                raw_values.append(value)
        evidence = container.get("evidence")
        if isinstance(evidence, (list, tuple)):
            raw_values.extend(evidence)
    keys: list[str] = []
    for value in raw_values:
        if isinstance(value, str):
            key = value.strip()
        elif isinstance(value, dict):
            raw_key = value.get("key") or value.get("id") or value.get("evidence_key")
            kind = str(value.get("kind") or "").strip()
            key = str(raw_key or "").strip()
            if key and kind:
                key = f"{kind}:{key}"
        else:
            key = ""
        if key and len(key) <= 512 and key not in keys:
            keys.append(key)
        if len(keys) >= 256:
            break
    return tuple(keys)


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


def _optional_bool_attr(value: object, name: str) -> bool | None:
    raw = getattr(value, name, None)
    return raw if isinstance(raw, bool) else None


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
