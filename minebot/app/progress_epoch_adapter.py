"""Model-response batch -> one atomic progress commit (autonomy-engine.md §5).

Extracted verbatim from ``app/runner.py``. This is the framework-binding seam
that decides *when* one complete tool batch is ready to be judged; it is not a
second progress authority. ``ProgressAuthority`` remains the sole stop
authority, and the adapter never makes a strategy decision.

The SDK can execute a tool batch concurrently, so settlement must wait for
every member: propagating a ``ProgressAbort`` on the first completed sibling
would cancel evidence the authority has not seen yet.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from minebot.app.progress_epochs import ContinuationEpochBudget, ProgressEpochArchive
from minebot.brain.feedback import failure_evidence_key
from minebot.brain.progress import ProgressStep, combat_progress_evidence, material_progress_evidence
from minebot.contract import (
    BodyState,
    JsonObject,
    ProgressAbort,
    ProgressStopReason,
    dominant_progress_abort,
)
from minebot.contract.tool_trace import (
    canonical_args_hash_from_json as _canonical_args_hash_from_json,
    canonical_tool_arguments_from_json as _canonical_tool_arguments,
    tool_tactic_signature_from_json,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an import cycle
    from minebot.app.runner import AgentRuntime


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
    strategy_epoch_condition: str = "strategy-objective-start"
    claimed: bool = False
    conflict: bool = False
    status: str = "pending"
    success: bool | None = None
    reason: str = ""
    progress_steps: tuple[ProgressStep, ...] = ()
    observation_handle: str | None = None
    epistemic_keys: tuple[str, ...] = ()
    pending_abort: ProgressAbort | None = None
    failure_evidence_key: str | None = None
    material_evidence: tuple[str, ...] = ()


@dataclass
class _ProgressEpoch:
    epoch_id: str
    run_id: str
    model_turn: int
    pre_body_fingerprint: str | None
    members: list[_ProgressEpochMember]
    pre_body_state: BodyState | None = None
    budget_checkpoint_id: str | None = None
    task_identity_bound: bool = False
    task_id: str | None = None
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
        budget: ContinuationEpochBudget | None = None,
    ) -> None:
        self.runtime = runtime
        self.run_id = run_id
        self.archive = archive
        self.budget = budget
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
        epoch_id = f"epoch-{uuid4().hex}"
        task_identity_bound = self.runtime.progress_task_id is not None
        task_id = (
            await self.runtime.run_read(self.runtime.progress_task_id)
            if task_identity_bound else None
        )
        work_calls = [call for call in calls if not _task_closure_tool(self.runtime, call.tool_name)]
        if self.budget is not None and work_calls:
            # A record of the work run under this continuation, not a permit:
            # the Agent stays live however many epochs it spends.
            await self.runtime.run_read(self.budget.reserve, epoch_id)
        pre_state = await self.runtime.read_progress_state()
        pre_fingerprint = None if pre_state is None else self.runtime.authority.fingerprint(pre_state)
        strategy_epoch_condition = _strategy_epoch_condition(self.runtime)
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
                strategy_epoch_condition=strategy_epoch_condition,
                conflict=call.tool_call_id in conflicting_ids,
            )
            for index, call in enumerate(calls)
        ]
        epoch = _ProgressEpoch(
            epoch_id=epoch_id,
            run_id=self.run_id,
            model_turn=self._model_turn,
            pre_body_fingerprint=pre_fingerprint,
            members=members,
            pre_body_state=pre_state,
            budget_checkpoint_id=None if self.budget is None else self.budget.checkpoint_id,
            task_identity_bound=task_identity_bound,
            task_id=task_id,
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
            strategy_epoch_condition=strategy_epoch_condition,
            pre_body_fingerprint=pre_fingerprint,
            budget_checkpoint_id=epoch.budget_checkpoint_id,
        )

    @property
    def pre_body_state(self) -> BodyState | None:
        """Body state read when the open epoch began."""
        return None if self._active is None else self._active.pre_body_state

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
        post_state: BodyState | None = None,
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
        if member.body_mutating and member.status == "failure" and pending_abort is None:
            member.failure_evidence_key = failure_evidence_key(
                tool_tactic_signature_from_json(member.tool_name, member.arguments),
                member.reason,
            )
            if member.failure_evidence_key is not None:
                member.epistemic_keys = tuple(dict.fromkeys(
                    (*member.epistemic_keys, member.failure_evidence_key)
                ))
        member.pending_abort = pending_abort
        if member.status in {"success", "failure", "partial"} and member.reason != "preempted":
            member.material_evidence = material_progress_evidence(
                epoch.pre_body_state, post_state,
                successful_body_action=member.body_mutating and member.status == "success",
            )
            if (
                member.body_mutating and member.tool_name == "engage_entity" and pending_abort is None
                and epoch.pre_body_state is not None and post_state is not None
                and epoch.pre_body_state.bot == post_state.bot
                and epoch.pre_body_state.dimension == post_state.dimension
            ):
                member.material_evidence += combat_progress_evidence(result)
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
        closure_only = not committed_steps and all(
            _task_closure_tool(self.runtime, member.tool_name)
            for member in ordered
        )
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
        material_changed = any(member.material_evidence for member in ordered)
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
                    "task_closure": _task_closure_tool(self.runtime, member.tool_name),
                    "strategy_epoch_condition": member.strategy_epoch_condition,
                    "progress_step_count": len(member.progress_steps),
                    "observation_handle": member.observation_handle,
                    "tactic_signature": tool_tactic_signature_from_json(member.tool_name, member.arguments),
                    "failure_evidence_key": member.failure_evidence_key,
                    "material_evidence": list(member.material_evidence),
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
            "budget_checkpoint_id": epoch.budget_checkpoint_id,
            "captured_progress_step_count": len(steps),
            "committed_progress_step_count": len(committed_steps),
            "commit_input": {
                "version": 1,
                "task_identity_bound": epoch.task_identity_bound,
                "task_id": epoch.task_id,
                "apply": not closure_only,
                "authority_before": asdict(
                    self.runtime.authority.facts(self.runtime.agent_context.goal_text)
                ),
                "steps": [asdict(step) for step in committed_steps],
                "pending_stop_reasons": [
                    member.pending_abort.stop_reason.value
                    for member in ordered if member.pending_abort is not None
                ],
            },
        }
        cursor: int | None = None
        archive_abort: ProgressAbort | None = None
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
                archive_abort = ProgressAbort("progress epoch persistence failed")
                self.runtime.trace.emit(
                    "progress_epoch_archive_failed",
                    epoch_id=epoch.epoch_id,
                    error_type=type(exc).__name__,
                )
        self._seen_epistemic_keys.update(epistemic_keys)

        # Bookkeeping and rejected work do not move the counters. The counters
        # never stop work; only hard stops (transport, unknown) arbitrate below.
        if not closure_only:
            self.runtime.authority.commit_steps(
                committed_steps,
                self.runtime.agent_context.goal_text,
                novel_epistemic_keys=novel_epistemic_keys,
                material_changed=material_changed,
            )
        progress_abort = dominant_progress_abort(
            archive_abort, *(member.pending_abort for member in ordered),
        )
        record["epistemic_steps"] = self.runtime.authority.epistemic_steps
        if progress_abort is not None and self.archive is not None:
            mark_aborted = getattr(self.archive, "mark_progress_aborted", None)
            if callable(mark_aborted):
                try:
                    mark_aborted(epoch.epoch_id, stop_reason=progress_abort.stop_reason)
                except Exception as exc:
                    progress_abort = dominant_progress_abort(
                        progress_abort, ProgressAbort("progress stop persistence failed"),
                    )
                    self.runtime.trace.emit(
                        "progress_epoch_archive_failed",
                        epoch_id=epoch.epoch_id,
                        operation="mark_progress_aborted",
                        error_type=type(exc).__name__,
                    )
        record["progress_aborted"] = progress_abort is not None
        record["progress_stop_reason"] = None if progress_abort is None else progress_abort.stop_reason.value
        if progress_abort is not None:
            # Bind the stop to the epoch that durably recorded it.
            progress_abort.stop_epoch_id = epoch.epoch_id
        epoch.finalized = True
        self.runtime.trace.emit(
            "progress_epoch_settled",
            **record,
            cursor=cursor,
        )
        self._members = {}
        self._active = None
        return progress_abort


def _task_closure_tool(runtime: "AgentRuntime", tool_name: str) -> bool:
    sidecar = runtime.registry.get(tool_name).sidecar
    return sidecar.source == "agent.task" and not sidecar.can_mutate_body


def _strategy_epoch_condition(runtime: "AgentRuntime") -> str:
    decision_context = getattr(runtime, "_current_decision_context", None)
    if getattr(decision_context, "value", decision_context) == "social":
        return "human"

    recent = runtime.last_tool_results[-1] if runtime.last_tool_results else None
    if not isinstance(recent, dict):
        return "strategy-objective-start"
    reason = str(recent.get("reason") or "").strip().lower()
    summary = recent.get("summary") if isinstance(recent.get("summary"), dict) else {}
    resume_hint = str(summary.get("resume_hint") or "").strip().lower()
    if (
        resume_hint == "reselect_candidates"
        or reason in {"target_not_found", "frontier_exhausted"}
        or "candidate_domain_exhausted" in reason
        or "domain_partial_exhausted" in reason
    ):
        return "domain-exhausted"
    if (
        "budget" in reason
        or "timeout" in reason
        or resume_hint in {"budget_exhausted", "timeout"}
    ):
        return "budget"
    if recent.get("success") is False:
        return "novel-failure"
    return "strategy-objective-start"


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


__all__ = [
    "ProgressEpochAdapter",
]
