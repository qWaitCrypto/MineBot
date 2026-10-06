from contextlib import closing
import asyncio
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from minebot.app.progress_epochs import ContinuationEpochBudget, PersistentProgressEpochArchive
from minebot.app.runtime_state import CheckpointDisposition, RuntimeScope, RuntimeStateStore
from minebot.app.tasks import TaskWorkspace
from tests.unit.test_autonomy import continuation


def seed(store, scope, *, budget=3):
    workspace = TaskWorkspace(store, scope)
    task = workspace.start("prepare tools", source="user")
    _, checkpoint = workspace.checkpoint(
        expected_task_revision=task.revision, disposition=CheckpointDisposition.CONTINUE,
        summary="continue", continuation=continuation(bounded_epoch_budget=budget),
    )
    store.settle_continuation_approach(scope, checkpoint_id=checkpoint.checkpoint_id,
        task_id=task.task_id, approach_key=checkpoint.continuation.approach_key,
        budget_limit=budget, consumed_epochs=1)
    return workspace, checkpoint


def test_reservations_debit_before_effect_and_survive_restart_without_refund(tmp_path):
    path = tmp_path / "state.sqlite3"
    scope = RuntimeScope("server", "world", "Bot")
    with closing(RuntimeStateStore(path)) as store:
        _, checkpoint = seed(store, scope)
        budget = ContinuationEpochBudget(store, scope, checkpoint.checkpoint_id)
        assert budget.reserve("interrupted-before-tool") is True
        assert budget.reserve("interrupted-before-tool") is True
    with closing(RuntimeStateStore(path)) as store:
        budget = ContinuationEpochBudget(store, scope, checkpoint.checkpoint_id)
        assert budget.reserve("next-batch") is True
        assert budget.reserve("overspend") is False
        assert store.continuation_approach_remaining(scope, task_id=checkpoint.task_id,
            approach_key=checkpoint.continuation.approach_key, requested_budget=32) == 0


def test_stale_checkpoint_and_cross_scope_cannot_reserve(tmp_path):
    scope = RuntimeScope("server", "world", "Bot")
    with closing(RuntimeStateStore(tmp_path / "state.sqlite3")) as store:
        workspace, checkpoint = seed(store, scope)
        assert ContinuationEpochBudget(store, RuntimeScope("other", "world", "Bot"), checkpoint.checkpoint_id).reserve("wrong-scope") is False
        workspace.checkpoint(expected_task_revision=workspace.current_task.revision,
            disposition=CheckpointDisposition.WAIT_EVENT, summary="wait")
        assert ContinuationEpochBudget(store, scope, checkpoint.checkpoint_id).reserve("stale") is False


@pytest.mark.parametrize("kind", ["recovery_reconcile", "body_event"])
@pytest.mark.parametrize("explicit_owner", [True, False])
def test_explicit_unleased_owner_is_not_replaced_by_the_latest_checkpoint(tmp_path, kind, explicit_owner):
    from minebot.app.session import AgentSession
    from minebot.app.work_queue import WorkIntentKind
    from tests.unit.test_agent_session import build_parts

    scope = RuntimeScope("server", "world", "Bot")
    with closing(RuntimeStateStore(tmp_path / "state.sqlite3")) as store:
        workspace, checkpoint = seed(store, scope)
        session = AgentSession(lambda goal: build_parts(goal, [], []), task_workspace=workspace)
        try:
            intent = session.work_queue.enqueue(WorkIntentKind(kind), source="test",
                payload={"budget_checkpoint_id": None} if explicit_owner else {})
            budget = session._continuation_budget(intent)
            if explicit_owner:
                assert budget is None
            else:
                assert budget.checkpoint_id == checkpoint.checkpoint_id
        finally:
            session.close()


def test_two_store_connections_cannot_spend_the_last_epoch_twice(tmp_path):
    path = tmp_path / "state.sqlite3"
    scope = RuntimeScope("server", "world", "Bot")
    with closing(RuntimeStateStore(path)) as first, closing(RuntimeStateStore(path)) as second:
        _, checkpoint = seed(first, scope, budget=2)
        budgets = [ContinuationEpochBudget(store, scope, checkpoint.checkpoint_id) for store in (first, second)]
        barrier = threading.Barrier(2)

        def reserve(pair):
            barrier.wait(timeout=5)
            return pair[1].reserve(f"race-{pair[0]}")

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(reserve, enumerate(budgets)))
        assert sum(results) == 1


@pytest.mark.parametrize("persistent", [False, True])
@pytest.mark.parametrize("owner", [None, "incoming-checkpoint"])
def test_budget_owner_binding_is_write_once_without_invalidating_the_lease(tmp_path, persistent, owner):
    from minebot.app.work_queue import MemoryWorkIntentQueue, PersistentWorkIntentQueue, WorkIntentKind
    from minebot.app.runtime_state import RuntimeStateConflict

    scope = RuntimeScope("server", "world", "Bot")
    with closing(RuntimeStateStore(tmp_path / "state.sqlite3")) as store:
        queue = PersistentWorkIntentQueue(store, scope) if persistent else MemoryWorkIntentQueue()
        try:
            queue.enqueue(WorkIntentKind.RECOVERY_RECONCILE, source="test", payload={})
            leased = queue.lease_next()
            bound = queue.bind_admission(leased, owner, 7)
            assert bound.revision == leased.revision
            assert bound.payload["budget_checkpoint_id"] == owner
            assert queue.bind_admission(leased, owner, 7).payload == bound.payload
            with pytest.raises((RuntimeError, RuntimeStateConflict)):
                queue.bind_admission(leased, "different-checkpoint", 7)
            with pytest.raises((RuntimeError, RuntimeStateConflict)):
                queue.bind_admission(leased, owner, 8)
            completed = queue.complete(leased)
            assert completed.payload["budget_checkpoint_id"] == owner
            with pytest.raises((RuntimeError, RuntimeStateConflict)):
                queue.bind_admission(leased, owner, 7)
        finally:
            queue.close()


@pytest.mark.parametrize("owner", [None, "incoming-checkpoint"])
def test_crash_reconciliation_retains_admission_and_rejects_the_old_holder(tmp_path, owner):
    from minebot.app.work_queue import PersistentWorkIntentQueue, WorkIntentKind
    from minebot.app.runtime_state import RuntimeStateConflict
    from minebot.app.body_events import BodyEventPump
    from minebot.app.reconciliation import enqueue_startup_reconciliation
    from tests.unit.test_startup_reconciliation import ReconcileBody

    path = tmp_path / "state.sqlite3"
    scope = RuntimeScope("server", "world", "Bot")
    with closing(RuntimeStateStore(path)) as store:
        task = TaskWorkspace(store, scope).start("resume work", source="user")
        queue = PersistentWorkIntentQueue(store, scope, lease_owner="old-process")
        queue.enqueue(WorkIntentKind.RECOVERY_RECONCILE, source="test", payload={})
        leased = queue.lease_next()
        queue.bind_admission(leased, owner, 7, task.task_id)
        queue.close()
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE work_intents SET lease_expires_at = ? WHERE intent_id = ?",
            ("2000-01-01T00:00:00+00:00", leased.intent_id))
    with closing(RuntimeStateStore(path)) as store:
        queue = PersistentWorkIntentQueue(store, scope, lease_owner="new-process")
        try:
            assert queue.lease_next() is None
            assert len(queue.orphaned_intents) == 1
            orphan = queue.orphaned_intents[0]
            assert orphan.payload["budget_checkpoint_id"] == owner
            assert orphan.payload["admission_evidence_cursor"] == 7
            with pytest.raises(RuntimeStateConflict):
                store.bind_work_intent_admission(scope, intent_id=leased.intent_id,
                    expected_revision=leased.revision, lease_owner="old-process", checkpoint_id=owner, evidence_cursor=7)
            workspace = TaskWorkspace(store, scope)
            body = ReconcileBody()
            result = enqueue_startup_reconciliation(body=body,
                event_pump=BodyEventPump(body, queue, store, scope), queue=queue,
                workspace=workspace, orphaned_intents=queue.orphaned_intents, app_reloaded=False)
            assert result.intent.payload["budget_checkpoint_id"] == owner
            assert result.intent.payload["evidence_cursor"] == 7
            # Another restart before the new recovery is leased must not lose
            # the inherited admission when stale recovery work is superseded.
            refreshed = enqueue_startup_reconciliation(body=body,
                event_pump=BodyEventPump(body, queue, store, scope), queue=queue,
                workspace=workspace, app_reloaded=False)
            assert refreshed.intent.payload["budget_checkpoint_id"] == owner
            assert refreshed.intent.payload["evidence_cursor"] == 7
        finally:
            queue.close()


def test_two_connections_cannot_bind_different_admissions_to_one_lease(tmp_path):
    from minebot.app.work_queue import PersistentWorkIntentQueue, WorkIntentKind
    from minebot.app.runtime_state import RuntimeStateConflict

    path = tmp_path / "state.sqlite3"
    scope = RuntimeScope("server", "world", "Bot")
    with closing(RuntimeStateStore(path)) as first, closing(RuntimeStateStore(path)) as second:
        queue = PersistentWorkIntentQueue(first, scope, lease_owner="holder")
        try:
            queue.enqueue(WorkIntentKind.RECOVERY_RECONCILE, source="test", payload={})
            leased = queue.lease_next()
            with pytest.raises(RuntimeStateConflict):
                second.bind_work_intent_admission(scope, intent_id=leased.intent_id,
                    expected_revision=leased.revision, lease_owner="other-holder",
                    checkpoint_id="other", evidence_cursor=1)
            barrier = threading.Barrier(2)

            def bind(pair):
                index, store = pair
                barrier.wait(timeout=5)
                try:
                    store.bind_work_intent_admission(scope, intent_id=leased.intent_id,
                        expected_revision=leased.revision, lease_owner="holder",
                        checkpoint_id=f"owner-{index}", evidence_cursor=index)
                    return index
                except RuntimeStateConflict:
                    return None

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(bind, enumerate((first, second))))
            winners = [index for index in results if index is not None]
            assert len(winners) == 1
            payload = first.get_work_intent(leased.intent_id)["payload"]
            assert payload["budget_checkpoint_id"] == f"owner-{winners[0]}"
            assert payload["admission_evidence_cursor"] == winners[0]
            queue.complete(leased)
        finally:
            queue.close()


def test_interrupted_startup_keeps_orphans_until_a_recovery_admission_takes_over(tmp_path):
    from minebot.app.work_queue import PersistentWorkIntentQueue, WorkIntentKind

    path = tmp_path / "state.sqlite3"
    scope = RuntimeScope("server", "world", "Bot")
    with closing(RuntimeStateStore(path)) as store:
        queue = PersistentWorkIntentQueue(store, scope)
        queue.enqueue(WorkIntentKind.START, source="user", payload={})
        original = queue.lease_next()
        queue.bind_admission(original, None, 0)
        queue.close()
    for attempt in range(3):
        with closing(RuntimeStateStore(path)) as store:
            queue = PersistentWorkIntentQueue(store, scope)
            try:
                assert [orphan.intent_id for orphan in queue.orphaned_intents] == [original.intent_id]
                assert queue.lease_next() is None
                if attempt == 2:
                    queue.enqueue(WorkIntentKind.RECOVERY_RECONCILE, source="startup_reconcile", payload={
                        "orphaned_intents": [{"intent_id": original.intent_id}],
                    })
                    recovery = queue.lease_next()
                    queue.bind_admission(recovery, None, 0)
            finally:
                queue.close()
    with closing(RuntimeStateStore(path)) as store:
        queue = PersistentWorkIntentQueue(store, scope)
        try:
            assert [orphan.intent_id for orphan in queue.orphaned_intents] == [recovery.intent_id]
            assert queue.orphaned_intents[0].payload["admission_evidence_cursor"] == 0
            assert queue.orphaned_intents[0].payload["budget_checkpoint_id"] is None
            assert queue.lease_next() is None
        finally:
            queue.close()


@pytest.mark.parametrize("outcome", ["completed", "failed", "superseded"])
def test_no_model_startup_completion_can_retire_orphans_but_failure_cannot(tmp_path, outcome):
    from minebot.app.work_queue import PersistentWorkIntentQueue, WorkIntentKind

    path = tmp_path / "state.sqlite3"
    scope = RuntimeScope("server", "world", "Bot")
    with closing(RuntimeStateStore(path)) as store:
        queue = PersistentWorkIntentQueue(store, scope)
        queue.enqueue(WorkIntentKind.START, source="user", payload={})
        original = queue.lease_next()
        queue.close()
    with closing(RuntimeStateStore(path)) as store:
        queue = PersistentWorkIntentQueue(store, scope)
        queue.enqueue(WorkIntentKind.RECOVERY_RECONCILE, source="startup_reconcile", payload={
            "decision": "idle", "orphaned_intents": [{"intent_id": original.intent_id}],
        })
        recovery = queue.lease_next()
        store.finish_work_intent(recovery.intent_id, expected_revision=recovery.revision,
            state=outcome, error=None if outcome == "completed" else {"reason": "test"})
        queue.close()
    with closing(RuntimeStateStore(path)) as store:
        queue = PersistentWorkIntentQueue(store, scope)
        try:
            assert [orphan.intent_id for orphan in queue.orphaned_intents] == (
                [] if outcome == "completed" else [original.intent_id]
            )
        finally:
            queue.close()


def test_schema_17_epochs_keep_their_original_unreserved_accounting(tmp_path):
    path = tmp_path / "state.sqlite3"
    scope = RuntimeScope("server", "world", "Bot")
    with closing(RuntimeStateStore(path)) as store:
        _, checkpoint = seed(store, scope)
        store.create_progress_epoch(scope, record={
            "epoch_id": "old", "run_id": "old", "model_turn": 1, "members": [],
        })
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE progress_epochs DROP COLUMN budget_checkpoint_id")
        connection.execute("DROP TABLE continuation_epoch_reservations")
        connection.execute("UPDATE minebot_schema SET version = 17")
    with closing(RuntimeStateStore(path)) as store:
        assert store.list_progress_epochs_after(scope, cursor=0)[0]["budget_checkpoint_id"] is None
        assert ContinuationEpochBudget(store, scope, checkpoint.checkpoint_id).reserve("new") is True


@pytest.mark.parametrize("task_only", [True, False, None])
@pytest.mark.parametrize("material_changed", [True, False])
def test_initial_settlement_distinguishes_explicit_task_only_epochs_after_restart(tmp_path, task_only, material_changed):
    from minebot.app.autonomy import AutonomyAction, AutonomyCoordinator
    from minebot.app.work_queue import PersistentWorkIntentQueue
    from minebot.brain.lifecycle import LifecycleState

    scope = RuntimeScope("server", "world", "Bot")
    path = tmp_path / "state.sqlite3"
    with closing(RuntimeStateStore(path)) as store:
        workspace = TaskWorkspace(store, scope)
        task = workspace.start("prepare tools", source="user")
        archive = PersistentProgressEpochArchive(store, scope)
        archive.store({"epoch_id": "work", "run_id": "initial", "model_turn": 1,
            "members": [{"tool": "prepare", "task_closure": False}], "material_changed": material_changed})
        for index in range(3):
            member = {"tool": "checkpoint_task"}
            if task_only is not None:
                member["task_closure"] = task_only
            archive.store({"epoch_id": f"task-{index}", "run_id": "initial", "model_turn": index + 2,
                "members": [member]})
        workspace.checkpoint(expected_task_revision=task.revision,
            disposition=CheckpointDisposition.CONTINUE, summary="continue",
            continuation=continuation(bounded_epoch_budget=2))
    with closing(RuntimeStateStore(path)) as store:
        queue = PersistentWorkIntentQueue(store, scope)
        try:
            decision = AutonomyCoordinator(TaskWorkspace(store, scope), queue,
                PersistentProgressEpochArchive(store, scope)).decide(
                    current_generation=3, body_fingerprint={"dimension": "overworld"},
                    lifecycle=LifecycleState.ACTIVE)
            assert decision.consumed_epochs == (1 if task_only is True else 4)
            assert decision.action is (
                AutonomyAction.CONTINUE if task_only is True and material_changed else AutonomyAction.YIELD
            )
            if task_only is True and not material_changed:
                assert decision.reason == "continuation_without_qualifying_evidence"
        finally:
            queue.close()


def test_untracked_tool_call_cannot_bypass_incoming_budget(tmp_path):
    from minebot.contract import ProgressAbort, ToolResult
    from tests.unit.test_progress_epochs import EpochBody, invoke, make_tool, runtime_with

    scope = RuntimeScope("server", "world", "Bot")
    effects = []
    with closing(RuntimeStateStore(tmp_path / "state.sqlite3")) as store:
        _, checkpoint = seed(store, scope)
        tool = make_tool("work", lambda _: effects.append("ran") or ToolResult(True, "done", False), mutating=True)
        runtime, adapter, context = runtime_with(EpochBody(), [tool])
        adapter.budget = ContinuationEpochBudget(store, scope, checkpoint.checkpoint_id)
        try:
            with pytest.raises(ProgressAbort) as stopped:
                asyncio.run(invoke(tool, context, "undeclared"))
            assert stopped.value.stop_reason.value == "unknown"
            assert effects == []
        finally:
            runtime.close()


def test_initial_model_recovery_keeps_unleased_work_after_an_outgoing_checkpoint(tmp_path):
    from minebot.app.runner import ModelTurnTransportError, sdk_tool_for
    from minebot.app.session import AgentSession, SessionCommand
    from minebot.app.tasks import register_task_tools
    from minebot.contract import ToolResult
    from tests.unit.test_agent_session import build_parts
    from tests.unit.test_progress_epochs import make_tool

    store = RuntimeStateStore(tmp_path / "state.sqlite3")
    scope = RuntimeScope("server", "world", "Bot1")
    workspace = TaskWorkspace(store, scope)
    archive = PersistentProgressEpochArchive(store, scope)
    effects = []
    inputs = []

    def factory(goal):
        parts = build_parts(goal, [], [])
        parts.runtime.progress_epoch_archive = archive
        register_task_tools(parts.registry, workspace,
            evidence_cursor=parts.runtime.current_run_evidence_cursor,
            generation=parts.authority.current_generation)

        def work(_params):
            effects.append("work")
            parts.runtime.body.x += 1
            return ToolResult(True, "arrived", False)

        parts.registry.register(make_tool("work", work, mutating=True))

        async def runner(_agent, input_text, *, context, **_kwargs):
            inputs.append(input_text)
            assert context.progress_epochs.budget is None

            async def call(name, params):
                args = json.dumps(params)
                call_id = f"call-{archive.latest_cursor()}"
                await context.progress_epochs.open(SimpleNamespace(output=[SimpleNamespace(
                    type="function_call", call_id=call_id, name=name, arguments=args)]))
                return await sdk_tool_for(parts.registry.get(name)).on_invoke_tool(
                    SimpleNamespace(context=context, tool_call_id=call_id), args)

            if len(inputs) == 2:
                assert "RECOVERY_RECONCILE" in input_text
                assert parts.runtime.current_run_evidence_cursor() == 0
            assert (await call("work", {}))["success"] is True
            params = {"expected_task_revision": workspace.current_task.revision,
                "disposition": "continue" if len(inputs) == 1 else "wait_event", "summary": "checkpoint"}
            if len(inputs) == 1:
                params["continuation"] = {
                    "objective": "continue travel", "operation_class": "material",
                    "target_descriptor": {"kind": "state", "identifier": "travel", "traits": []},
                    "expected_evidence": ["movement"], "bounded_epoch_budget": 3,
                }
            assert (await call("checkpoint_task", params))["success"] is True
            if len(inputs) == 1:
                raise ModelTurnTransportError("test")
            return {"ok": True}

        parts.runtime.runner_run = runner
        return parts

    session = AgentSession(factory, task_workspace=workspace)
    try:
        session.submit(SessionCommand.start("prepare tools"))
        assert asyncio.run(session.step()).status == "retrying"
        assert asyncio.run(session.step()).status == "completed_turn"
        assert effects == ["work", "work"]
        assert workspace.current_task.status.value == "waiting_event"
        assert session.work_queue.pending_count() == 0
        assert all(row["budget_checkpoint_id"] is None for row in archive.list_after(0))
    finally:
        session.close()
        store.close()


def test_automatic_recovery_pins_the_incoming_owner_before_model_changes_checkpoint(tmp_path):
    from minebot.app.runner import ModelTurnTransportError
    from minebot.app.session import AgentSession
    from minebot.app.work_queue import WorkIntentKind
    from tests.unit.test_agent_session import build_parts

    scope = RuntimeScope("server", "world", "Bot")
    with closing(RuntimeStateStore(tmp_path / "state.sqlite3")) as store:
        workspace, incoming = seed(store, scope)

        def factory(goal):
            parts = build_parts(goal, [], [])

            async def runner(_agent, _input, *, context, **_kwargs):
                assert context.progress_epochs.budget.checkpoint_id == incoming.checkpoint_id
                workspace.checkpoint(expected_task_revision=workspace.current_task.revision,
                    disposition=CheckpointDisposition.CONTINUE, summary="outgoing plan",
                    continuation=continuation())
                raise ModelTurnTransportError("test")

            parts.runtime.runner_run = runner
            return parts

        session = AgentSession(factory, task_workspace=workspace)
        try:
            session.work_queue.enqueue(WorkIntentKind.RECOVERY_RECONCILE,
                source="test", payload={"decision": "resume", "reason": "restart"})
            assert asyncio.run(session.step()).status == "retrying"
            recovery = session.work_queue.lease_next()
            assert recovery is not None
            assert recovery.payload["budget_checkpoint_id"] == incoming.checkpoint_id
        finally:
            session.close()


def test_charged_history_pagination_cannot_hide_a_later_hard_stop(tmp_path):
    from minebot.app.autonomy import AutonomyAction, AutonomyCoordinator
    from minebot.app.work_queue import PersistentWorkIntentQueue
    from minebot.brain.lifecycle import LifecycleState

    scope = RuntimeScope("server", "world", "Bot")
    with closing(RuntimeStateStore(tmp_path / "state.sqlite3")) as store:
        workspace, checkpoint = seed(store, scope)
        archive = PersistentProgressEpochArchive(store, scope)
        for index in range(101):
            archive.store({
                "epoch_id": f"epoch-{index}", "run_id": f"run-{index}", "model_turn": 1,
                "members": [], "budget_checkpoint_id": checkpoint.checkpoint_id,
                "material_changed": index == 0, "progress_aborted": index == 100,
            })
        queue = PersistentWorkIntentQueue(store, scope)
        try:
            decision = AutonomyCoordinator(workspace, queue, archive).decide(
                current_generation=3, body_fingerprint={"dimension": "overworld"}, lifecycle=LifecycleState.ACTIVE)
            assert decision.action is AutonomyAction.YIELD
            assert decision.reason == "continuation_progress_aborted"
            assert queue.pending_count() == 0
        finally:
            queue.close()


@pytest.mark.parametrize("boundary", ["none", "omitted", "model_error"])
@pytest.mark.parametrize("near_failure_limit", [False, True])
def test_exhausted_approach_can_close_then_run_a_model_selected_alternative(tmp_path, boundary, near_failure_limit):
    from minebot.app.autonomy import AutonomyCoordinator
    from minebot.app.runner import ModelTurnTransportError, sdk_tool_for
    from minebot.app.session import AgentSession, SessionCommand
    from minebot.app.tasks import register_task_tools
    from minebot.contract import ToolResult
    from tests.unit.test_agent_session import build_parts
    from tests.unit.test_progress_epochs import make_tool

    store = RuntimeStateStore(tmp_path / "state.sqlite3")
    scope = RuntimeScope("server", "world", "Bot1")
    workspace = TaskWorkspace(store, scope)
    archive = PersistentProgressEpochArchive(store, scope)
    effects, checkpoints, inputs = [], [], []

    def factory(goal):
        parts = build_parts(goal, [], [])
        parts.runtime.progress_epoch_archive = archive
        register_task_tools(parts.registry, workspace,
            body_fingerprint=lambda: {"dimension": "overworld"},
            evidence_cursor=parts.runtime.current_run_evidence_cursor,
            generation=parts.authority.current_generation)

        def work(name):
            def execute(_params):
                effects.append(name)
                if name == "blocked":
                    return ToolResult(False, "no_path", False)
                parts.runtime.body.x += 1
                return ToolResult(True, "arrived", False)
            return execute

        for name in ("prepare", "blocked", "alternative"):
            parts.registry.register(make_tool(name, work(name), mutating=True))

        async def runner(_agent, input_text, *, context, **_kwargs):
            inputs.append(input_text)

            async def call(name, params=None):
                args = json.dumps(params or {})
                call_id = f"call-{len(inputs)}-{archive.latest_cursor()}"
                await context.progress_epochs.open(SimpleNamespace(output=[SimpleNamespace(
                    type="function_call", call_id=call_id, name=name, arguments=args)]))
                return await sdk_tool_for(parts.registry.get(name)).on_invoke_tool(
                    SimpleNamespace(context=context, tool_call_id=call_id), args)

            async def checkpoint(target=None):
                params = {"expected_task_revision": workspace.current_task.revision,
                          "disposition": "wait_event" if target is None else "continue",
                          "summary": "wait" if target is None else f"try {target}"}
                if target is not None:
                    params["continuation"] = {
                        "objective": f"try {target}", "operation_class": "material",
                        "target_descriptor": {"kind": "state", "identifier": target, "traits": []},
                        "expected_evidence": ["movement"], "bounded_epoch_budget": 2 if target == "travel" else 3,
                    }
                result = await call("checkpoint_task", params)
                assert result["success"] is True, result
                checkpoints.append(store.get_latest_checkpoint(workspace.current_task.task_id))

            phase = len(inputs)
            if phase == 1:
                assert (await call("prepare"))["success"] is True
                await checkpoint("travel")
                rows = archive.list_after(0)
                assert rows[0]["members"][0]["task_closure"] is False
                assert rows[1]["members"][0]["task_closure"] is True
            elif phase == 2:
                if near_failure_limit:
                    from minebot.brain.progress import FAILURE_STORM_LIMIT
                    parts.authority.failure_steps = FAILURE_STORM_LIMIT - 1
                assert (await call("blocked"))["reason"] == "no_path"
                assert (await call("blocked"))["reason"] == "continuation_work_budget_closed"
                if boundary == "model_error":
                    raise ModelTurnTransportError("test")
                if boundary == "none":
                    await checkpoint("exploration")
            elif boundary != "none" and phase == 3:
                assert ("TASK_BOUNDARY" if boundary == "omitted" else "RECOVERY_RECONCILE") in input_text
                assert (await call("blocked"))["reason"] == "continuation_work_budget_closed"
                await checkpoint("exploration")
            else:
                assert (await call("alternative"))["success"] is True
                await checkpoint()
            return {"ok": True}

        parts.runtime.runner_run = runner
        return parts

    session = AgentSession(factory, task_workspace=workspace)
    session.autonomy_coordinator = AutonomyCoordinator(workspace, session.work_queue, archive)
    try:
        session.submit(SessionCommand.start("prepare tools"))
        for index in range(4 if boundary != "none" else 3):
            step = asyncio.run(session.step())
            assert step.status == ("retrying" if boundary == "model_error" and index == 1 else "completed_turn"), step
        assert effects == ["prepare", "blocked", "alternative"]
        assert session.work_queue.pending_count() == 0
        assert workspace.current_task.status.value == "waiting_event"
        for checkpoint, expected in zip(checkpoints[:2], (0, 2)):
            assert store.continuation_approach_remaining(scope, task_id=checkpoint.task_id,
                approach_key=checkpoint.continuation.approach_key, requested_budget=32) == expected
        rejected = session.parts.registry.get("checkpoint_task").callable({
            "expected_task_revision": workspace.current_task.revision,
            "disposition": "continue", "summary": "renamed original approach",
            "continuation": {
                "objective": "try the original idea with new wording", "operation_class": "material",
                "target_descriptor": checkpoints[0].continuation.target_descriptor,
                "expected_evidence": ["movement"], "bounded_epoch_budget": 32,
            },
        })
        assert rejected.success is False
        assert rejected.reason == "task_checkpoint_rejected"
        assert workspace.current_task.status.value == "waiting_event"
    finally:
        session.close()
        store.close()
