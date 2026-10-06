from contextlib import closing
from dataclasses import replace
import asyncio
import sqlite3

import pytest

from minebot.app.runtime_state import RuntimeScope, RuntimeStateConflict, RuntimeStateStore
from minebot.app.work_queue import MemoryWorkIntentQueue, PersistentWorkIntentQueue, WorkIntentKind
from tests.unit.test_recovery_attempt_budget import admitted
from tests.unit.test_startup_reconciliation import body_state


@pytest.fixture(autouse=True)
def stable_admission_clock(monkeypatch):
    from minebot.app import runtime_state

    now = runtime_state._utc_now()
    monkeypatch.setattr(runtime_state, "_utc_now", lambda: now)


def startup(queue, task_id):
    queue.enqueue(WorkIntentKind.RECOVERY_RECONCILE, source="startup_reconcile",
        task_id=task_id, payload={"task": {"task_id": task_id}, "decision": "resume",
            "state": {"bot": "Bot1"}})
    intent = queue.lease_next()
    assert intent is not None
    return intent


@pytest.mark.parametrize("persistent", [False, True])
def test_startup_body_confirmation_closes_spent_episode_without_refund(persistent):
    scope = RuntimeScope("server", "world", "Bot1")
    with closing(RuntimeStateStore(":memory:")) as store:
        queue = PersistentWorkIntentQueue(store, scope) if persistent else MemoryWorkIntentQueue()
        try:
            first = queue.reserve_recovery_attempt(admitted(queue), max_attempts=1)
            queue.fail(first, {"reason": "process_restart_unknown_outcome"})
            reconcile = startup(queue, first.task_id)
            state = replace(body_state(), pending_action_count=0)
            queue.reconcile_recovery(reconcile, state=state)
            queue.reconcile_recovery(reconcile, state=state)
            queue.complete(reconcile)
            following = queue.reserve_recovery_attempt(admitted(queue, label="next"), max_attempts=1)
            assert following.payload["recovery_admission"]["attempt"] == 1
            assert following.payload["recovery_admission"]["sequence"] == 2
            assert following.payload["recovery_admission"]["episode_id"] != first.intent_id
            prior = (store.get_work_intent(first.intent_id)["payload"] if persistent
                else queue._records[first.intent_id].payload)
            assert prior["recovery_admission"]["attempt"] == 1
            assert prior["recovery_confirmation"]["intent_id"] == reconcile.intent_id
        finally:
            queue.close()


@pytest.mark.parametrize("change", [
    {"health": 0}, {"health": float("nan")}, {"complete": False}, {"missing": True},
    {"body_owner": "active"}, {"pending_action_count": 1}, {"pending_action_count": None},
    {"hazard_unresolved": {"reason": "unsafe"}}, {"bot": "Other"},
])
def test_unconfirmed_body_does_not_close_old_episode(change):
    with closing(RuntimeStateStore(":memory:")) as store:
        queue = PersistentWorkIntentQueue(store, RuntimeScope("server", "world", "Bot1"))
        try:
            first = queue.reserve_recovery_attempt(admitted(queue), max_attempts=1)
            queue.fail(first, {"reason": "process_restart_unknown_outcome"})
            reconcile = startup(queue, first.task_id)
            queue.reconcile_recovery(reconcile,
                state=replace(body_state(), **{"pending_action_count": 0, **change}))
            queue.complete(reconcile)
            assert queue.reserve_recovery_attempt(admitted(queue, label="next"), max_attempts=1) is None
        finally:
            queue.close()


def test_reconciliation_cannot_confirm_a_live_recovery_or_replaced_task():
    with closing(RuntimeStateStore(":memory:")) as store:
        queue = PersistentWorkIntentQueue(store, RuntimeScope("server", "world", "Bot1"))
        try:
            first = queue.reserve_recovery_attempt(admitted(queue), max_attempts=1)
            reconcile = startup(queue, first.task_id)
            state = replace(body_state(), pending_action_count=0)
            with pytest.raises(RuntimeStateConflict):
                queue.reconcile_recovery(reconcile, state=state)
            queue.fail(first, {"reason": "process_restart_unknown_outcome"})
            store.replace_foreground_task(queue.scope, goal_text="new", source="user")
            with pytest.raises(RuntimeStateConflict):
                queue.reconcile_recovery(reconcile, state=state)
            assert store.get_work_intent(first.intent_id)["payload"]["recovery_admission"]["confirmed"] is False
        finally:
            queue.close()


@pytest.mark.parametrize("persistent", [False, True])
def test_same_reconciliation_cannot_confirm_a_later_episode(persistent):
    with closing(RuntimeStateStore(":memory:")) as store:
        queue = (PersistentWorkIntentQueue(store, RuntimeScope("server", "world", "Bot1"))
            if persistent else MemoryWorkIntentQueue())
        try:
            first = queue.reserve_recovery_attempt(admitted(queue), max_attempts=1)
            queue.fail(first, {"reason": "unknown"})
            reconcile = startup(queue, first.task_id)
            state = replace(body_state(), pending_action_count=0)
            queue.reconcile_recovery(reconcile, state=state)
            second = queue.reserve_recovery_attempt(admitted(queue, label="second"), max_attempts=1)
            queue.fail(second, {"reason": "failed"})
            queue.reconcile_recovery(reconcile, state=state)
            queue.complete(reconcile)
            assert queue.reserve_recovery_attempt(admitted(queue, label="third"), max_attempts=1) is None
        finally:
            queue.close()


@pytest.mark.parametrize("persistent", [False, True])
def test_reconciliation_without_an_episode_cannot_confirm_future_work(persistent):
    scope = RuntimeScope("server", "world", "Bot1")
    with closing(RuntimeStateStore(":memory:")) as store:
        task_id = store.create_task(scope, goal_text="collect", source="user").task_id
        queue = PersistentWorkIntentQueue(store, scope) if persistent else MemoryWorkIntentQueue()
        try:
            reconcile = startup(queue, task_id)
            state = replace(body_state(), pending_action_count=0)
            queue.reconcile_recovery(reconcile, state=state)
            first = queue.reserve_recovery_attempt(admitted(queue, task_id=task_id), max_attempts=1)
            queue.fail(first, {"reason": "failed"})
            queue.reconcile_recovery(reconcile, state=state)
            queue.complete(reconcile)
            assert queue.reserve_recovery_attempt(admitted(queue, task_id=task_id, label="next"), max_attempts=1) is None
        finally:
            queue.close()


def test_confirmation_and_its_consumed_receipt_are_atomic():
    with closing(RuntimeStateStore(":memory:")) as store:
        queue = PersistentWorkIntentQueue(store, RuntimeScope("server", "world", "Bot1"))
        try:
            first = queue.reserve_recovery_attempt(admitted(queue), max_attempts=1)
            queue.fail(first, {"reason": "unknown"})
            reconcile = startup(queue, first.task_id)
            store._connection.execute("""CREATE TEMP TRIGGER fail_confirmation_receipt
                BEFORE UPDATE OF payload_json ON work_intents
                WHEN NEW.source = 'startup_reconcile'
                BEGIN SELECT RAISE(ABORT, 'receipt write failed'); END""")
            with pytest.raises(sqlite3.IntegrityError, match="receipt write failed"):
                queue.reconcile_recovery(reconcile, state=replace(body_state(), pending_action_count=0))
            assert store.get_work_intent(first.intent_id)["payload"]["recovery_admission"]["confirmed"] is False
            assert "recovery_reconciled_intent_id" not in store.get_work_intent(reconcile.intent_id)["payload"]
        finally:
            queue.close()


@pytest.mark.parametrize("case", ["owner", "revision", "scope", "expired", "completed"])
def test_only_current_live_reconciler_can_settle(case):
    scope = RuntimeScope("server", "world", "Bot1")
    with closing(RuntimeStateStore(":memory:")) as store:
        queue = PersistentWorkIntentQueue(store, scope)
        try:
            first = queue.reserve_recovery_attempt(admitted(queue), max_attempts=1)
            queue.fail(first, {"reason": "unknown"})
            reconcile = startup(queue, first.task_id)
            if case == "expired":
                with store._connection:
                    store._connection.execute("UPDATE work_intents SET lease_expires_at = ? WHERE intent_id = ?",
                        ("2000-01-01T00:00:00+00:00", reconcile.intent_id))
            if case == "completed":
                queue.complete(reconcile)
            with pytest.raises(RuntimeStateConflict):
                store.reconcile_recovery(RuntimeScope("other", "world", "Bot1") if case == "scope" else scope,
                    intent_id=reconcile.intent_id,
                    expected_revision=reconcile.revision + (case == "revision"),
                    lease_owner="other" if case == "owner" else queue.lease_owner,
                    state=replace(body_state(), pending_action_count=0))
            assert store.get_work_intent(first.intent_id)["payload"]["recovery_admission"]["confirmed"] is False
        finally:
            queue.close()


@pytest.mark.parametrize("case", ["resume", "park", "write_failure", "unknown_progress", "died_again"])
def test_real_startup_and_session_settle_before_model_without_resetting_progress(tmp_path, case):
    from minebot.app.body_events import BodyEventPump
    from minebot.app.progress_epochs import PersistentProgressEpochArchive
    from minebot.app.reconciliation import enqueue_startup_reconciliation
    from minebot.app.session import AgentSession
    from minebot.app.tasks import TaskWorkspace
    from minebot.app.runtime_state import TaskStatus
    from tests.unit.test_agent_session import build_parts
    from tests.unit.test_progress_recovery_inputs import _record
    from tests.unit.test_startup_reconciliation import ReconcileBody

    path = tmp_path / "state.sqlite3"
    scope = RuntimeScope("server", "world", "Bot1")
    with closing(RuntimeStateStore(path)) as store:
        workspace = TaskWorkspace(store, scope)
        task = workspace.start("collect", source="user")
        record = _record(task.task_id)
        if case == "unknown_progress":
            record["progress_aborted"] = True
            record["progress_stop_reason"] = "unknown"
        store.create_progress_epoch(scope, record=record)
        queue = PersistentWorkIntentQueue(store, scope)
        first = queue.reserve_recovery_attempt(admitted(queue), max_attempts=1)
        queue.close()

    with closing(RuntimeStateStore(path)) as store:
        workspace = TaskWorkspace(store, scope)
        if case == "park":
            workspace.pause()
        queue = PersistentWorkIntentQueue(store, scope)
        body = ReconcileBody(state=replace(body_state(), pending_action_count=0))
        if case == "park":
            # A previous completed reconciliation can leave the old episode
            # unconfirmed without any orphan still waiting for adoption.
            orphans = ()
        else:
            orphans = queue.orphaned_intents
        result = enqueue_startup_reconciliation(body=body,
            event_pump=BodyEventPump(body, queue, store, scope), queue=queue,
            workspace=workspace, orphaned_intents=orphans, app_reloaded=True)
        if case == "died_again":
            body.state = replace(body.state, health=0)
        if case == "write_failure":
            def fail_write(*_args, **_kwargs):
                raise OSError("fixture persistence failure")
            queue.reconcile_recovery = fail_write
        model_calls = []

        def factory(goal):
            parts = build_parts(goal, [], [])
            parts.runtime.body = body
            parts.runtime.progress_epoch_archive = PersistentProgressEpochArchive(store, scope)
            parts.runtime.progress_task_id = lambda: workspace.current_task.task_id

            async def model(*_args, **_kwargs):
                journal = store.get_work_intent(first.intent_id)["payload"]
                assert journal["recovery_admission"]["confirmed"] is True
                model_calls.append(parts.authority.failure_steps)
                return {"ok": True}

            parts.runtime.runner_run = model
            return parts

        session = AgentSession(factory, task_workspace=workspace, work_queue=queue)
        try:
            outcome = asyncio.run(session.step())
            confirmed = store.get_work_intent(first.intent_id)["payload"]["recovery_admission"]["confirmed"]
            assert confirmed is (case in {"resume", "park"})
            if case == "resume":
                assert outcome.status == "completed_turn"
                assert model_calls == [2]
            else:
                assert model_calls == []
            if case == "park":
                assert result.decision.value == "park"
                assert workspace.current_task.status is TaskStatus.PAUSED
            if case in {"write_failure", "unknown_progress"}:
                assert outcome.progress_stop_reason.value == "unknown"
                assert queue.queued_intents(WorkIntentKind.RECOVERY_RECONCILE) == []
            assert session.parts.authority.failure_steps == 2
        finally:
            session.close()

    with closing(RuntimeStateStore(path)) as store:
        queue = PersistentWorkIntentQueue(store, scope)
        try:
            next_intent = admitted(queue, label="later-death")
            reserved = queue.reserve_recovery_attempt(next_intent, max_attempts=1)
            assert (reserved is not None) is (case in {"resume", "park"})
        finally:
            queue.close()
