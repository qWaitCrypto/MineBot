import tempfile
from dataclasses import replace
from pathlib import Path

from minebot.app.autonomy import AutonomyAction, AutonomyCoordinator
from minebot.app.progress_epochs import PersistentProgressEpochArchive
from minebot.app.runtime_state import (
    CheckpointDisposition,
    ContinuationContract,
    ContinuationOperationClass,
    RuntimeScope,
    RuntimeStateStore,
)
from minebot.app.tasks import TaskWorkspace
from minebot.app.work_queue import PersistentWorkIntentQueue, WorkIntentKind
from minebot.brain.lifecycle import LifecycleState
from minebot.brain.feedback import failure_evidence_key


def continuation(
    *,
    generation: int = 3,
    bounded_epoch_budget: int = 4,
) -> ContinuationContract:
    return ContinuationContract(
        objective="continue collecting wood",
        operation_class=ContinuationOperationClass.MATERIAL,
        target_descriptor={"kind": "resource", "identifier": "oak_log", "traits": []},
        expected_evidence=("inventory delta",),
        bounded_epoch_budget=bounded_epoch_budget,
        approach_key="approach:wood",
        evidence_cursor=0,
        generation=generation,
    )


def store_material_epoch(archive: PersistentProgressEpochArchive) -> None:
    archive.store(
        {
            "epoch_id": "epoch-material",
            "run_id": "run-material",
            "model_turn": 1,
            "members": [],
            "pre_body_fingerprint": "before",
            "post_body_fingerprint": "after",
            "evidence_refs": [],
            "epistemic_keys": [],
            "material_changed": True,
            "progress_aborted": False,
        }
    )


def test_persistent_one_hop_lease_is_idempotent_and_latest_checkpoint_atomic():
    with tempfile.TemporaryDirectory() as tmp:
        store = RuntimeStateStore(Path(tmp) / "state.sqlite3")
        scope = RuntimeScope("server", "world", "Bot1")
        workspace = TaskWorkspace(store, scope)
        queue = PersistentWorkIntentQueue(store, scope)
        archive = PersistentProgressEpochArchive(store, scope)
        task = workspace.start("collect logs", source="user")
        store_material_epoch(archive)
        _, checkpoint = workspace.checkpoint(
            expected_task_revision=task.revision,
            disposition=CheckpointDisposition.CONTINUE,
            summary="made material progress",
            body_fingerprint={"dimension": "overworld"},
            continuation=continuation(),
        )
        coordinator = AutonomyCoordinator(workspace, queue, archive)

        first = coordinator.decide(
            current_generation=3,
            body_fingerprint={"dimension": "overworld", "missing": False},
            lifecycle=LifecycleState.ACTIVE,
        )
        duplicate = coordinator.decide(
            current_generation=3,
            body_fingerprint={"dimension": "overworld", "missing": False},
            lifecycle=LifecycleState.ACTIVE,
        )

        assert first.action is AutonomyAction.CONTINUE
        assert duplicate.action is AutonomyAction.CONTINUE
        assert duplicate.intent.intent_id == first.intent.intent_id
        assert queue.count_for_task(WorkIntentKind.TASK_CONTINUE, task.task_id) == 1

        leased = queue.lease_next()
        assert leased is not None
        queue.complete(leased)
        consumed = coordinator.decide(
            current_generation=3,
            body_fingerprint={"dimension": "overworld", "missing": False},
            lifecycle=LifecycleState.ACTIVE,
        )
        assert consumed.action is AutonomyAction.PARK
        assert consumed.reason == "continuation_lease_consumed"

        current = workspace.current_task
        workspace.checkpoint(
            expected_task_revision=current.revision,
            disposition=CheckpointDisposition.WAIT_EVENT,
            summary="wait",
        )
        stale = store.issue_checkpoint_continuation(
            scope,
            checkpoint_id=checkpoint.checkpoint_id,
            checkpoint_revision=checkpoint.revision,
            task_id=task.task_id,
            generation=3,
            kind=WorkIntentKind.TASK_CONTINUE.value,
            source="test",
            priority=50,
            payload={},
            dedupe_key="stale-checkpoint-attempt",
        )
        assert stale is None
        queue.close()
        store.close()


def test_generation_change_rejects_contract_without_issuing_work():
    with tempfile.TemporaryDirectory() as tmp:
        store = RuntimeStateStore(Path(tmp) / "state.sqlite3")
        scope = RuntimeScope("server", "world", "Bot1")
        workspace = TaskWorkspace(store, scope)
        queue = PersistentWorkIntentQueue(store, scope)
        archive = PersistentProgressEpochArchive(store, scope)
        task = workspace.start("collect logs", source="user")
        store_material_epoch(archive)
        workspace.checkpoint(
            expected_task_revision=task.revision,
            disposition=CheckpointDisposition.CONTINUE,
            summary="continue",
            body_fingerprint={"dimension": "overworld"},
            continuation=continuation(generation=8),
        )

        decision = AutonomyCoordinator(workspace, queue, archive).decide(
            current_generation=9,
            body_fingerprint={"dimension": "overworld", "missing": False},
            lifecycle=LifecycleState.ACTIVE,
        )

        assert decision.action is AutonomyAction.YIELD
        assert decision.reason == "continuation_generation_changed"
        assert queue.pending_count() == 0
        queue.close()
        store.close()


def test_handoff_settles_a_generation_changed_contract_without_issuing_work():
    with tempfile.TemporaryDirectory() as tmp:
        store = RuntimeStateStore(Path(tmp) / "state.sqlite3")
        scope = RuntimeScope("server", "world", "Bot1")
        workspace = TaskWorkspace(store, scope)
        queue = PersistentWorkIntentQueue(store, scope)
        archive = PersistentProgressEpochArchive(store, scope)
        task = workspace.start("collect logs", source="user")
        store_material_epoch(archive)
        _, checkpoint = workspace.checkpoint(
            expected_task_revision=task.revision,
            disposition=CheckpointDisposition.CONTINUE,
            summary="continue after process exit",
            body_fingerprint={"dimension": "overworld"},
            continuation=continuation(generation=8),
        )

        decision = AutonomyCoordinator(workspace, queue, archive).decide(
            current_generation=9,
            body_fingerprint={"dimension": "overworld", "missing": False},
            lifecycle=LifecycleState.ACTIVE,
            handoff_only=True,
        )

        assert decision.action is AutonomyAction.CONTINUE
        assert decision.reason == "continuation_handoff_settled"
        assert decision.intent is None
        assert queue.pending_count() == 0
        assert store.reserve_continuation_epoch(
            scope,
            checkpoint_id=checkpoint.checkpoint_id,
            epoch_id="handoff-work",
        )
        queue.close()
        store.close()


def failure_epoch(archive, *, suffix="1", reason="no_path"):
    key = failure_evidence_key("move_to:canonical", reason)
    return archive.store({
        "epoch_id": f"failure-{suffix}", "run_id": f"run-{suffix}", "model_turn": 1,
        "members": [{
            "tool": "move_to", "tactic_signature": "move_to:canonical",
            "status": "failure", "success": False, "reason": reason,
            "body_mutating": True, "failure_evidence_key": key,
        }],
        "epistemic_keys": [key] if key else [], "evidence_refs": [],
        "material_changed": False, "progress_aborted": False,
    })


def test_new_failure_reconsiders_but_restart_and_approach_rename_cannot_refill():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state.sqlite3"
        scope = RuntimeScope("server", "world", "Bot1")
        for iteration in range(2):
            store = RuntimeStateStore(path)
            workspace = TaskWorkspace(store, scope)
            queue = PersistentWorkIntentQueue(store, scope)
            archive = PersistentProgressEpochArchive(store, scope)
            try:
                if iteration == 0:
                    workspace.start("collect wood", source="user")
                cursor = archive.latest_cursor()
                failure_epoch(archive, suffix=str(iteration))
                task = workspace.current_task
                workspace.checkpoint(
                    expected_task_revision=task.revision,
                    disposition=CheckpointDisposition.CONTINUE,
                    summary="another description of the plan",
                    body_fingerprint={"dimension": "overworld"},
                    continuation=replace(continuation(), evidence_cursor=cursor,
                                         approach_key=f"renamed-{iteration}"),
                )
                coordinator = AutonomyCoordinator(workspace, queue, archive)
                decision = coordinator.decide(current_generation=3,
                    body_fingerprint={"dimension": "overworld"}, lifecycle=LifecycleState.ACTIVE)
                if iteration == 0:
                    assert decision.action is AutonomyAction.CONTINUE
                    assert decision.reason == "reconsideration_lease_queued"
                    assert decision.remaining_epochs == 3
                    assert decision.material_changed is False
                    assert decision.intent.payload["continuation_mode"] == "reconsider"
                    assert decision.intent.payload["failed_approaches"][0]["reason"] == "no_path"
                    duplicate = coordinator.decide(current_generation=3,
                        body_fingerprint={"dimension": "overworld"}, lifecycle=LifecycleState.ACTIVE)
                    assert duplicate.intent.intent_id == decision.intent.intent_id
                    leased = queue.lease_next()
                    queue.complete(leased)
                else:
                    assert decision.action is AutonomyAction.YIELD
                    assert decision.reason == "continuation_without_qualifying_evidence"
                    assert queue.pending_count() == 0
            finally:
                queue.close()
                store.close()


def test_failure_novelty_does_not_override_exhaustion_abort_or_safety_failure():
    for blocker in ("budget", "abort", "safety", "generation"):
        store = RuntimeStateStore(":memory:")
        scope = RuntimeScope("server", "world", "Bot1")
        workspace = TaskWorkspace(store, scope)
        queue = PersistentWorkIntentQueue(store, scope)
        archive = PersistentProgressEpochArchive(store, scope)
        try:
            task = workspace.start("collect wood", source="user")
            failure_epoch(archive)
            if blocker == "abort":
                archive.mark_progress_aborted("failure-1")
            if blocker == "safety":
                failure_epoch(archive, suffix="denied", reason="governance_denied")
            workspace.checkpoint(
                expected_task_revision=task.revision,
                disposition=CheckpointDisposition.CONTINUE, summary="continue",
                body_fingerprint={"dimension": "overworld"},
                continuation=continuation(bounded_epoch_budget=1 if blocker == "budget" else 4),
            )
            decision = AutonomyCoordinator(workspace, queue, archive).decide(
                current_generation=4 if blocker == "generation" else 3,
                body_fingerprint={"dimension": "overworld"}, lifecycle=LifecycleState.ACTIVE)
            assert decision.action is AutonomyAction.YIELD, blocker
            assert queue.pending_count() == 0
        finally:
            queue.close()
            store.close()


def test_exhausted_contract_cannot_issue_a_successor_even_with_material_evidence():
    with tempfile.TemporaryDirectory() as tmp:
        store = RuntimeStateStore(Path(tmp) / "state.sqlite3")
        scope = RuntimeScope("server", "world", "Bot1")
        workspace = TaskWorkspace(store, scope)
        queue = PersistentWorkIntentQueue(store, scope)
        archive = PersistentProgressEpochArchive(store, scope)
        task = workspace.start("collect logs", source="user")
        store_material_epoch(archive)
        _, checkpoint = workspace.checkpoint(
            expected_task_revision=task.revision,
            disposition=CheckpointDisposition.CONTINUE,
            summary="continue",
            body_fingerprint={"dimension": "overworld"},
            continuation=continuation(bounded_epoch_budget=1),
        )

        decision = AutonomyCoordinator(workspace, queue, archive).decide(
            current_generation=3,
            body_fingerprint={"dimension": "overworld", "missing": False},
            lifecycle=LifecycleState.ACTIVE,
        )
        direct = store.issue_checkpoint_continuation(
            scope,
            checkpoint_id=checkpoint.checkpoint_id,
            checkpoint_revision=checkpoint.revision,
            task_id=task.task_id,
            generation=3,
            kind=WorkIntentKind.TASK_CONTINUE.value,
            source="test",
            priority=50,
            payload={},
            dedupe_key="exhausted-contract-attempt",
        )

        assert decision.action is AutonomyAction.YIELD
        assert decision.reason == "continuation_epoch_budget_exhausted"
        assert decision.remaining_epochs == 0
        assert direct is None
        assert queue.pending_count() == 0
        queue.close()
        store.close()
