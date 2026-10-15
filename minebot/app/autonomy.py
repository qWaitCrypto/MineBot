"""Session-boundary continuation policy over durable checkpoints and evidence."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from minebot.app.progress_epochs import ProgressEpochArchive
from minebot.app.runtime_state import (
    CheckpointDisposition,
    TaskStatus,
)
from minebot.app.tasks import TaskWorkspace
from minebot.app.work_queue import WorkIntent, WorkIntentQueue, WorkIntentState
from minebot.brain.feedback import failure_evidence_key
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
        body_fingerprint: dict[str, object] | None,
        lifecycle: LifecycleState,
        handoff_only: bool = False,
    ) -> AutonomyDecision:
        """Validate a checkpoint and optionally issue its successor intent.

        ``handoff_only`` is used while a process is already shutting down. It
        settles the same durable evidence as a normal continuation but does
        not reuse the old process lease.
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
        # The Agent is always live: a continuation it wrote is admitted on its
        # own say-so. Generation and dimension changes (the reflex moves the
        # Body all the time; a respawn changes dimension), spent epochs,
        # earlier stops and the kind of evidence are facts handed to the next
        # turn, never a reason to refuse it.
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
        # Explicit task-only batches do not execute work. Historical/unknown
        # member metadata counts as work; never infer exemptions from names.
        consumed = sum(
            epoch.get("budget_checkpoint_id") is None
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
        failure_keys = _novel_failure_keys(epochs, novel_keys)
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
