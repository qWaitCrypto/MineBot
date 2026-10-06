"""Session-boundary continuation policy over durable checkpoints and evidence."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from minebot.app.progress_epochs import ProgressEpochArchive
from minebot.app.runtime_state import (
    REVIEWABLE_STOP_REASONS,
    CheckpointDisposition,
    ContinuationOperationClass,
    TaskStatus,
)
from minebot.app.tasks import TaskWorkspace
from minebot.app.work_queue import WorkIntent, WorkIntentQueue, WorkIntentState
from minebot.brain.feedback import FAILURE_EVIDENCE_PREFIX, failure_evidence_key
from minebot.brain.lifecycle import LifecycleState


class AutonomyAction(str, Enum):
    CONTINUE = "continue"
    PARK = "park"
    YIELD = "yield"
    VERIFY = "verify"


@dataclass(frozen=True)
class AutonomyDecision:
    action: AutonomyAction
    reason: str
    intent: WorkIntent | None = None
    consumed_epochs: int = 0
    remaining_epochs: int = 0
    material_changed: bool = False
    novel_epistemic_keys: tuple[str, ...] = ()


class AutonomyCoordinator:
    """Validate one model-authored checkpoint without choosing a game strategy."""

    def __init__(
        self,
        workspace: TaskWorkspace,
        queue: WorkIntentQueue,
        epoch_archive: ProgressEpochArchive,
    ) -> None:
        self.workspace = workspace
        self.queue = queue
        self.epoch_archive = epoch_archive

    def decide(
        self,
        *,
        current_generation: int,
        body_fingerprint: dict[str, object] | None,
        lifecycle: LifecycleState,
        handoff_only: bool = False,
    ) -> AutonomyDecision:
        """Validate a checkpoint and optionally issue its successor intent.

        ``handoff_only`` is used while a process is already shutting down. It
        settles the exact same durable evidence and budget as a normal
        continuation, but neither reuses the old process lease nor lets a
        normal shutdown generation change invalidate a material handoff.
        """
        task = self.workspace.current_task
        if task is None:
            return AutonomyDecision(AutonomyAction.PARK, "no_active_task")
        checkpoint = self.workspace.store.get_latest_checkpoint(task.task_id)
        if checkpoint is None:
            return AutonomyDecision(AutonomyAction.PARK, "no_checkpoint")
        if checkpoint.disposition is CheckpointDisposition.COMPLETE:
            return AutonomyDecision(AutonomyAction.VERIFY, "completion_verification_requested")
        if checkpoint.disposition is CheckpointDisposition.WAIT_EVENT:
            return AutonomyDecision(AutonomyAction.PARK, "checkpoint_wait_event")
        if checkpoint.disposition is CheckpointDisposition.YIELD:
            return AutonomyDecision(AutonomyAction.YIELD, checkpoint.summary or "checkpoint_yield")
        contract = checkpoint.continuation
        if contract is None:
            return AutonomyDecision(AutonomyAction.YIELD, "continuation_contract_missing")
        if task.status is not TaskStatus.RUNNING:
            return AutonomyDecision(AutonomyAction.PARK, f"task_not_running:{task.status.value}")
        if lifecycle is not LifecycleState.ACTIVE:
            return AutonomyDecision(AutonomyAction.PARK, f"lifecycle_not_active:{lifecycle.value}")
        if body_fingerprint is None or body_fingerprint.get("missing") is True:
            return AutonomyDecision(AutonomyAction.PARK, "continuation_body_unavailable")
        checkpoint_dimension = (
            None
            if checkpoint.body_fingerprint is None
            else checkpoint.body_fingerprint.get("dimension")
        )
        current_dimension = body_fingerprint.get("dimension")
        if (
            checkpoint_dimension is not None
            and current_dimension is not None
            and str(checkpoint_dimension) != str(current_dimension)
        ):
            return AutonomyDecision(AutonomyAction.YIELD, "continuation_dimension_changed")
        if not handoff_only and contract.generation != int(current_generation):
            return AutonomyDecision(AutonomyAction.YIELD, "continuation_generation_changed")
        try:
            epochs = []
            cursor = contract.evidence_cursor
            while True:
                page = self.epoch_archive.list_after(cursor, limit=100)
                epochs.extend(page)
                if len(page) < 100:
                    break
                next_cursor = int(page[-1]["cursor"])
                if next_cursor <= cursor:
                    raise ValueError("progress epoch cursor did not advance")
                cursor = next_cursor
        except Exception:
            return AutonomyDecision(AutonomyAction.PARK, "continuation_evidence_unavailable")
        aborted = [epoch for epoch in epochs if bool(epoch.get("progress_aborted"))]
        # Once work is closed, the closure batch that records the plan inherits
        # the same stop. It executed nothing, so it re-records the reviewed stop
        # rather than introducing a new one; the stop under review is the last
        # aborted epoch that actually ran work.
        reviewed = _reviewed_stop_epoch(aborted)
        # An aborted epoch executes again only if this exact approach paid a task
        # allowance to review that stop, and every stop in the reviewed window
        # was a reviewable one. Transport and unclassified aborts, and any
        # un-granted approach, stay terminal.
        review_grant = (
            self.workspace.store.approach_review_grant(
                self.workspace.scope,
                task_id=task.task_id,
                approach_key=contract.approach_key,
            )
            if aborted
            else None
        )
        reconsidered = bool(
            aborted
            and reviewed is not None
            and review_grant is not None
            and review_grant == str(reviewed.get("epoch_id") or "")
            and all(
                str(epoch.get("progress_stop_reason") or "") in {
                    reason.value for reason in REVIEWABLE_STOP_REASONS
                }
                for epoch in aborted
            )
        )
        # Work up to and including the reviewed stop belongs to the exhausted
        # approach and was accounted there. The reviewed evidence still has to
        # stay visible below, so narrow the charge window, not the evidence one.
        charge_floor = int(reviewed["cursor"]) if reconsidered else 0
        # Successor work was debited to its incoming checkpoint before execution.
        # Do not charge it again to a model-authored outgoing strategy.
        # Explicit task-only batches do not execute work. Historical/unknown
        # member metadata stays chargeable; never infer exemptions from names.
        consumed = sum(
            int(epoch["cursor"]) > charge_floor
            and epoch.get("budget_checkpoint_id") is None
            and any(member.get("task_closure") is not True for member in (epoch.get("members") or [{}]))
            for epoch in epochs
        )
        material_changed = any(bool(epoch.get("material_changed")) for epoch in epochs)
        novel_keys = tuple(
            dict.fromkeys(
                str(key)
                for epoch in epochs
                for key in (epoch.get("novel_epistemic_keys") or [])
                if isinstance(key, str) and key
            )
        )
        approach_state = self.workspace.store.settle_continuation_approach(
            self.workspace.scope,
            checkpoint_id=checkpoint.checkpoint_id,
            task_id=task.task_id,
            approach_key=contract.approach_key,
            budget_limit=contract.bounded_epoch_budget,
            consumed_epochs=consumed,
        )
        if approach_state is None:
            return AutonomyDecision(
                AutonomyAction.PARK,
                "checkpoint_changed_before_budget_settlement",
            )
        remaining = approach_state["remaining_epochs"]
        facts = {
            "consumed_epochs": consumed,
            "remaining_epochs": remaining,
            "material_changed": material_changed,
            "novel_epistemic_keys": novel_keys,
        }
        if consumed > contract.bounded_epoch_budget:
            return AutonomyDecision(
                AutonomyAction.YIELD,
                "continuation_epoch_budget_exhausted",
                **facts,
            )
        if aborted and not reconsidered:
            return AutonomyDecision(
                AutonomyAction.YIELD,
                "continuation_progress_aborted",
                **facts,
            )
        if remaining < 1:
            return AutonomyDecision(
                AutonomyAction.YIELD,
                "continuation_epoch_budget_exhausted",
                **facts,
            )
        failure_keys = _novel_failure_keys(epochs, novel_keys)
        observation_keys = tuple(
            key for key in novel_keys if not key.startswith(FAILURE_EVIDENCE_PREFIX)
        )
        qualifies = bool(failure_keys) or {
            ContinuationOperationClass.MATERIAL: material_changed,
            ContinuationOperationClass.EPISTEMIC: bool(observation_keys),
            ContinuationOperationClass.MIXED: material_changed or bool(observation_keys),
        }[contract.operation_class]
        if not qualifies:
            return AutonomyDecision(
                AutonomyAction.YIELD,
                "continuation_without_qualifying_evidence",
                **facts,
            )
        if handoff_only:
            return AutonomyDecision(
                AutonomyAction.CONTINUE,
                "continuation_handoff_settled",
                **facts,
            )
        dedupe_key = (
            f"task_continue:{self.workspace.scope.key}:"
            f"{checkpoint.checkpoint_id}:g{contract.generation}"
        )
        payload = {
            "checkpoint_id": checkpoint.checkpoint_id,
            "checkpoint_revision": checkpoint.revision,
            "objective": contract.objective,
            "operation_class": contract.operation_class.value,
            "target_descriptor": dict(contract.target_descriptor),
            "expected_evidence": list(contract.expected_evidence),
            "approach_key": contract.approach_key,
            "evidence_cursor": contract.evidence_cursor,
            "consumed_epochs": consumed,
            "remaining_epochs": remaining,
            "material_changed": material_changed,
            "novel_epistemic_keys": list(novel_keys),
            "continuation_mode": "reconsider" if failure_keys else "continue",
            "novel_failure_keys": list(failure_keys),
            "failed_approaches": [
                {key: member.get(key) for key in (
                    "tool", "reason", "tactic_signature", "observation_handle",
                )}
                for epoch in epochs
                for member in (epoch.get("members") or [])
                if isinstance(member, dict) and member.get("failure_evidence_key") in failure_keys
            ][:16],
        }
        intent = self.queue.issue_task_continuation(
            checkpoint_id=checkpoint.checkpoint_id,
            checkpoint_revision=checkpoint.revision,
            task_id=task.task_id,
            payload=payload,
            dedupe_key=dedupe_key,
            generation=contract.generation,
        )
        if intent is None:
            return AutonomyDecision(
                AutonomyAction.PARK,
                "checkpoint_changed_before_lease",
                **facts,
            )
        if intent.state is WorkIntentState.QUEUED:
            return AutonomyDecision(
                AutonomyAction.CONTINUE,
                "reconsideration_lease_queued" if failure_keys else "continuation_lease_queued",
                intent=intent,
                **facts,
            )
        reason = (
            "continuation_lease_active"
            if intent.state is WorkIntentState.LEASED
            else "continuation_lease_consumed"
        )
        return AutonomyDecision(
            AutonomyAction.PARK,
            reason,
            intent=intent,
            **facts,
        )


def _reviewed_stop_epoch(
    aborted: list[dict[str, object]],
) -> dict[str, object] | None:
    """The last aborted epoch that executed work, i.e. the stop under review.

    Epochs whose every member is a task-closure call ran no work; when they are
    marked aborted they carry the stop that closed the lane before them. An
    epoch with no member metadata counts as work, so unknown history can never
    be mistaken for bookkeeping.
    """
    for epoch in reversed(aborted):
        members = epoch.get("members") or [{}]
        if any(member.get("task_closure") is not True for member in members):
            return epoch
    return None


def _novel_failure_keys(
    epochs: list[dict[str, object]],
    novel_keys: tuple[str, ...],
) -> tuple[str, ...]:
    keys: list[str] = []
    for epoch in epochs:
        for member in epoch.get("members") or []:
            if not isinstance(member, dict) or member.get("success") is not False:
                continue
            if member.get("status") == "budget_denied" and member.get("reason") == "continuation_work_budget_closed":
                continue
            key = failure_evidence_key(
                str(member.get("tactic_signature") or ""),
                str(member.get("reason") or ""),
            )
            # A mixed batch containing a safety/control/infrastructure failure
            # cannot borrow a sibling's gameplay failure to request reconsideration.
            if member.get("status") != "failure" or key is None:
                return ()
            if (
                member.get("body_mutating") is True
                and member.get("failure_evidence_key") == key
                and key in novel_keys
            ):
                keys.append(key)
    return tuple(dict.fromkeys(keys))


__all__ = ["AutonomyAction", "AutonomyCoordinator", "AutonomyDecision"]
