"""Session-boundary continuation policy over durable checkpoints and evidence."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from minebot.app.progress_epochs import ProgressEpochArchive
from minebot.app.runtime_state import (
    CheckpointDisposition,
    ContinuationOperationClass,
    TaskStatus,
)
from minebot.app.tasks import TaskWorkspace
from minebot.app.work_queue import WorkIntent, WorkIntentQueue, WorkIntentState
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
    ) -> AutonomyDecision:
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
        if contract.generation != int(current_generation):
            return AutonomyDecision(AutonomyAction.YIELD, "continuation_generation_changed")
        try:
            epochs = self.epoch_archive.list_after(
                contract.evidence_cursor,
                limit=contract.bounded_epoch_budget + 1,
            )
        except Exception:
            return AutonomyDecision(AutonomyAction.PARK, "continuation_evidence_unavailable")
        consumed = len(epochs)
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
        if any(bool(epoch.get("progress_aborted")) for epoch in epochs):
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
        qualifies = {
            ContinuationOperationClass.MATERIAL: material_changed,
            ContinuationOperationClass.EPISTEMIC: bool(novel_keys),
            ContinuationOperationClass.MIXED: material_changed or bool(novel_keys),
        }[contract.operation_class]
        if not qualifies:
            return AutonomyDecision(
                AutonomyAction.YIELD,
                "continuation_without_qualifying_evidence",
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
                "continuation_lease_queued",
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


__all__ = ["AutonomyAction", "AutonomyCoordinator", "AutonomyDecision"]
