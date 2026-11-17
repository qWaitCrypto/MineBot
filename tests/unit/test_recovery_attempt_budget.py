from contextlib import closing
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

import pytest

from minebot.app.runtime_state import RuntimeScope, RuntimeStateConflict, RuntimeStateStore
from minebot.app.work_queue import MemoryWorkIntentQueue, PersistentWorkIntentQueue, WorkIntentKind


@pytest.fixture(autouse=True)
def recovery_budget_clock(monkeypatch):
    """Budget accounting is independent of wall-clock corrections on the host.

    Immediate queue admission uses UTC; a backwards host correction can make
    a freshly queued intent temporarily unavailable. Expired-lease tests below
    set their own past expiry explicitly, so keep testing that boundary.
    """
    instant = datetime(2026, 10, 13, tzinfo=UTC)
    monkeypatch.setattr("minebot.app.runtime_state._utc_now",
        lambda: instant.isoformat(timespec="milliseconds"))
    monkeypatch.setattr("minebot.app.runtime_state._utc_after",
        lambda seconds: (instant + timedelta(seconds=max(0, seconds))).isoformat(timespec="milliseconds"))


def admitted(queue, *, task_id=None, label="attempt"):
    if task_id is None:
        if isinstance(queue, PersistentWorkIntentQueue):
            task = queue.store.get_foreground_task(queue.scope)
            task = task or queue.store.create_task(queue.scope, goal_text="recover", source="user")
            task_id = task.task_id
        else:
            task_id = "task"
    queue.enqueue(WorkIntentKind.RECOVERY_RECONCILE, source="lifecycle_recovery",
        payload={"reason": "lifecycle_recovering"}, task_id=task_id, dedupe_key=label)
    intent = queue.lease_next()
    return queue.bind_admission(intent, None, 0, task_id)


@pytest.mark.parametrize("persistent", [False, True])
def test_recovery_reservation_is_before_effect_nonrefundable_and_not_replayable(tmp_path, persistent):
    scope = RuntimeScope("server", "world", "Bot")
    with closing(RuntimeStateStore(tmp_path / "state.sqlite3")) as store:
        queue = PersistentWorkIntentQueue(store, scope) if persistent else MemoryWorkIntentQueue()
        try:
            first = admitted(queue, label="one")
            reserved = queue.reserve_recovery_attempt(first, max_attempts=2)
            assert reserved.payload["recovery_admission"]["attempt"] == 1
            assert reserved.revision == first.revision
            with pytest.raises(RuntimeStateConflict):
                queue.reserve_recovery_attempt(first, max_attempts=2)
            queue.fail(first, {"reason": "outcome_unknown"})
            second = admitted(queue, label="two")
            reserved = queue.reserve_recovery_attempt(second, max_attempts=99)
            assert reserved.payload["recovery_admission"]["attempt"] == 2
            assert reserved.payload["recovery_admission"]["limit"] == 2
            queue.complete(second)
            third = admitted(queue, label="three")
            assert queue.reserve_recovery_attempt(third, max_attempts=99) is None
        finally:
            queue.close()


def test_unfinished_recovery_spend_survives_store_and_scheduler_restart(tmp_path):
    path = tmp_path / "state.sqlite3"
    scope = RuntimeScope("server", "world", "Bot")
    for attempt in (1, 2, 3, 4):
        with closing(RuntimeStateStore(path)) as store:
            queue = PersistentWorkIntentQueue(store, scope)
            try:
                intent = admitted(queue, label=f"run-{attempt}")
                reserved = queue.reserve_recovery_attempt(intent, max_attempts=2)
                if attempt <= 2:
                    assert reserved.payload["recovery_admission"]["attempt"] == attempt
                else:
                    assert reserved is None
            finally:
                queue.close()


@pytest.mark.parametrize("persistent", [False, True])
def test_only_confirmed_recovery_or_a_new_task_starts_another_episode(tmp_path, persistent):
    with closing(RuntimeStateStore(tmp_path / "state.sqlite3")) as store:
        queue = (PersistentWorkIntentQueue(store, RuntimeScope("server", "world", "Bot"))
            if persistent else MemoryWorkIntentQueue())
        try:
            first = admitted(queue, label="first")
            first = queue.reserve_recovery_attempt(first, max_attempts=1)
            queue.confirm_recovery(first)
            queue.complete(first)
            second = admitted(queue, label="second")
            second = queue.reserve_recovery_attempt(second, max_attempts=1)
            assert second.payload["recovery_admission"]["attempt"] == 1
            assert second.payload["recovery_admission"]["episode_id"] != first.payload["recovery_admission"]["episode_id"]
            assert second.payload["recovery_admission"]["sequence"] == 2
            queue.fail(second, {"reason": "not_confirmed"})
            task_id = (store.replace_foreground_task(queue.scope, goal_text="recover", source="user").task_id
                if persistent else "new-task")
            third = admitted(queue, task_id=task_id, label="new-task")
            assert queue.reserve_recovery_attempt(third, max_attempts=1).payload["recovery_admission"]["attempt"] == 1
        finally:
            queue.close()


def test_session_restarts_cannot_repeat_the_first_recovery_attempt(tmp_path):
    import asyncio
    from minebot.app.runner import RecoveryOutcome
    from minebot.app.tasks import TaskWorkspace
    from tests.unit.test_progress_recovery_inputs import _record, _recovering_session

    path = tmp_path / "state.sqlite3"
    scope = RuntimeScope("server", "world", "Bot")
    calls = []
    for run in range(4):
        with closing(RuntimeStateStore(path)) as store:
            workspace = TaskWorkspace(store, scope)
            if workspace.current_task is None:
                task = workspace.start("recover", source="user")
                store.create_progress_epoch(scope, record=_record(task.task_id))

            def recover(_runtime):
                payload = store.get_work_intent(intent_id)["payload"]
                calls.append(payload["recovery_admission"]["attempt"])
                return RecoveryOutcome(False, "not_recovered", can_retry=True)

            session = _recovering_session(workspace, store, recover)
            session.max_recovery_attempts = 2
            intent_id = session.work_queue.queued_intents()[0].intent_id
            try:
                outcome = asyncio.run(session.step())
                if run >= 2:
                    assert outcome.message == "recovery_failed:recovery_attempt_budget_exhausted"
            finally:
                session.close()
    assert calls == [1, 2]


def test_expired_lease_denies_new_work_but_accepts_its_owned_terminal_result(tmp_path):
    path = tmp_path / "state.sqlite3"
    scope = RuntimeScope("server", "world", "Bot")
    with closing(RuntimeStateStore(path)) as store:
        queue = PersistentWorkIntentQueue(store, scope)
        try:
            first = queue.reserve_recovery_attempt(admitted(queue, label="first"), max_attempts=2)
            with sqlite3.connect(path) as connection:
                connection.execute("UPDATE work_intents SET lease_expires_at = ? WHERE intent_id = ?",
                    ("2000-01-01T00:00:00.000+00:00", first.intent_id))
            assert queue.confirm_recovery(first).payload["recovery_admission"]["confirmed"] is True
            queue.complete(first)
            second = admitted(queue, label="second")
            with sqlite3.connect(path) as connection:
                connection.execute("UPDATE work_intents SET lease_expires_at = ? WHERE intent_id = ?",
                    ("2000-01-01T00:00:00.000+00:00", second.intent_id))
            with pytest.raises(RuntimeStateConflict):
                queue.reserve_recovery_attempt(second, max_attempts=2)
            with pytest.raises(RuntimeStateConflict):
                queue.confirm_recovery(first)
        finally:
            queue.close()


def test_two_connections_cannot_reserve_the_same_last_recovery_slot(tmp_path):
    path = tmp_path / "state.sqlite3"
    scope = RuntimeScope("server", "world", "Bot")
    with closing(RuntimeStateStore(path)) as first, closing(RuntimeStateStore(path)) as second:
        task = first.create_task(scope, goal_text="recover", source="user")
        leases = []
        for index, store in enumerate((first, second)):
            store.enqueue_work_intent(scope, kind="recovery_reconcile", source="lifecycle_recovery",
                priority=80, payload={}, task_id=task.task_id)
            intent = store.lease_next_work_intent(scope, lease_owner=f"owner-{index}")
            leases.append(store.bind_work_intent_admission(scope, intent_id=intent["intent_id"],
                expected_revision=intent["revision"], lease_owner=f"owner-{index}",
                checkpoint_id=None, evidence_cursor=0, task_id=task.task_id))
        barrier = threading.Barrier(2)

        def reserve(index):
            barrier.wait(timeout=5)
            intent = leases[index]
            return (first, second)[index].reserve_recovery_attempt(scope,
                intent_id=intent["intent_id"], expected_revision=intent["revision"],
                lease_owner=f"owner-{index}", max_attempts=1)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(reserve, (0, 1)))
        assert sum(result is not None for result in results) == 1


@pytest.mark.parametrize("persistent", [False, True])
def test_unbound_or_legacy_recovery_cannot_claim_a_fresh_limit(tmp_path, persistent):
    scope = RuntimeScope("server", "world", "Bot")
    with closing(RuntimeStateStore(tmp_path / "state.sqlite3")) as store:
        queue = PersistentWorkIntentQueue(store, scope) if persistent else MemoryWorkIntentQueue()
        try:
            queue.enqueue(WorkIntentKind.RECOVERY_RECONCILE, source="lifecycle_recovery", payload={})
            unbound = queue.lease_next()
            with pytest.raises(RuntimeStateConflict):
                queue.reserve_recovery_attempt(unbound, max_attempts=3)
            bound = queue.bind_admission(unbound, None, 0, None)
            queue.complete(bound)
            queue.enqueue(WorkIntentKind.RECOVERY_RECONCILE, source="lifecycle_recovery", payload={})
            next_intent = queue.bind_admission(queue.lease_next(), None, 0, None)
            with pytest.raises(RuntimeStateConflict):
                queue.reserve_recovery_attempt(next_intent, max_attempts=3)
        finally:
            queue.close()


def test_recovery_confirmation_rejects_foreign_scope_owner_and_revision(tmp_path):
    scope = RuntimeScope("server", "world", "Bot")
    with closing(RuntimeStateStore(tmp_path / "state.sqlite3")) as store:
        queue = PersistentWorkIntentQueue(store, scope)
        try:
            reserved = queue.reserve_recovery_attempt(admitted(queue), max_attempts=3)
            for actual_scope, owner, revision in (
                (RuntimeScope("other", "world", "Bot"), queue.lease_owner, reserved.revision),
                (scope, "foreign-owner", reserved.revision),
                (scope, queue.lease_owner, reserved.revision + 1),
            ):
                with pytest.raises(RuntimeStateConflict):
                    store.confirm_recovery(actual_scope, intent_id=reserved.intent_id,
                        expected_revision=revision, lease_owner=owner)
            assert store.get_work_intent(reserved.intent_id)["payload"]["recovery_admission"]["confirmed"] is False
        finally:
            queue.close()


@pytest.mark.parametrize("phase", ["reserve", "confirm"])
def test_recovery_journal_write_failure_stops_without_renewing_or_resuming(tmp_path, phase):
    import asyncio
    from minebot.app.runner import RecoveryOutcome
    from minebot.app.tasks import TaskWorkspace
    from minebot.brain.lifecycle import LifecycleState
    from minebot.contract import ProgressStopReason
    from tests.unit.test_progress_recovery_inputs import _record, _recovering_session

    path = tmp_path / "state.sqlite3"
    scope = RuntimeScope("server", "world", "Bot")
    with closing(RuntimeStateStore(path)) as store:
        workspace = TaskWorkspace(store, scope)
        task = workspace.start("recover", source="user")
        store.create_progress_epoch(scope, record=_record(task.task_id))
        calls = []

        def recover(_runtime):
            calls.append(True)
            return RecoveryOutcome(True, "recovered")

        def fail_write(*_args, **_kwargs):
            raise OSError("fixture write failure")

        session = _recovering_session(workspace, store, recover)
        session.max_recovery_attempts = 1
        intent_id = session.work_queue.queued_intents()[0].intent_id
        if phase == "reserve":
            session.work_queue.reserve_recovery_attempt = fail_write
        else:
            session.work_queue.confirm_recovery = fail_write
        try:
            outcome = asyncio.run(session.step())
            assert outcome.progress_stop_reason is ProgressStopReason.UNKNOWN
            assert outcome.lifecycle is LifecycleState.IDLE
            assert session.work_queue.pending_count() == 0
            assert calls == ([] if phase == "reserve" else [True])
            payload = store.get_work_intent(intent_id)["payload"]
            if phase == "reserve":
                assert "recovery_admission" not in payload
            else:
                assert payload["recovery_admission"]["confirmed"] is False
        finally:
            session.close()
        if phase == "confirm":
            resumed = _recovering_session(workspace, store, recover)
            try:
                outcome = asyncio.run(resumed.step())
                assert outcome.message == "recovery_failed:recovery_attempt_budget_exhausted"
                assert calls == [True]
            finally:
                resumed.close()
