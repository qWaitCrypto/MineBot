import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from minebot.app.runtime_state import RuntimeScope, RuntimeStateConflict, RuntimeStateStore
from minebot.app.work_queue import (
    MemoryWorkIntentQueue,
    PersistentWorkIntentQueue,
    WorkIntentKind,
    WorkIntentState,
    superseded_kinds_for,
)


class MemoryWorkIntentQueueTests(unittest.TestCase):
    def test_cancel_supersedes_every_lower_priority_control_and_work_intent(self):
        superseded = superseded_kinds_for(WorkIntentKind.CANCEL)

        self.assertIn(WorkIntentKind.REPLACE_GOAL, superseded)
        self.assertIn(WorkIntentKind.PAUSE, superseded)
        self.assertIn(WorkIntentKind.BODY_EVENT, superseded)
        self.assertIn(WorkIntentKind.MESSAGE, superseded)
        self.assertNotIn(WorkIntentKind.QUIT, superseded)

    def test_control_supersedes_older_low_priority_work(self):
        queue = MemoryWorkIntentQueue()
        old = queue.enqueue(
            WorkIntentKind.MESSAGE,
            source="user_message",
            payload={"text": "old"},
        )
        queue.supersede(
            superseded_kinds_for(WorkIntentKind.REPLACE_GOAL),
            reason="superseded_by:replace_goal",
        )
        replacement = queue.enqueue(
            WorkIntentKind.REPLACE_GOAL,
            source="goal_replaced",
            payload={"text": "new"},
        )

        leased = queue.lease_next()

        self.assertEqual(leased.intent_id, replacement.intent_id)
        self.assertEqual(leased.kind, WorkIntentKind.REPLACE_GOAL)
        self.assertEqual(queue._records[old.intent_id].state, WorkIntentState.SUPERSEDED)
        queue.complete(leased)
        self.assertFalse(queue.available.is_set())

    def test_dedupe_key_coalesces_live_intent(self):
        queue = MemoryWorkIntentQueue()

        first = queue.enqueue(
            WorkIntentKind.BODY_EVENT,
            source="body_event",
            payload={"name": "damage"},
            dedupe_key="damage:Bot1",
        )
        second = queue.enqueue(
            WorkIntentKind.BODY_EVENT,
            source="body_event",
            payload={"name": "damage", "newer": True},
            dedupe_key="damage:Bot1",
        )

        self.assertEqual(first.intent_id, second.intent_id)
        self.assertEqual(queue.pending_count(), 1)


class PersistentWorkIntentQueueTests(unittest.TestCase):
    def test_queued_intent_survives_reopen_but_orphan_lease_is_not_replayed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite3"
            scope = RuntimeScope("server", "world", "Bot1")
            store = RuntimeStateStore(path)
            first_queue = PersistentWorkIntentQueue(store, scope, lease_owner="process-1")
            queued = first_queue.enqueue(
                WorkIntentKind.MESSAGE,
                source="user_message",
                payload={"text": "resume me"},
            )
            first_lease = first_queue.lease_next()
            self.assertEqual(first_lease.intent_id, queued.intent_id)
            self.assertEqual(first_lease.attempt_count, 1)
            first_queue.close()
            store.close()

            reopened_store = RuntimeStateStore(path)
            second_queue = PersistentWorkIntentQueue(
                reopened_store,
                scope,
                lease_owner="process-2",
            )
            reclaimed = second_queue.lease_next()

            self.assertIsNone(reclaimed)
            self.assertEqual(
                [intent.intent_id for intent in second_queue.orphaned_intents],
                [queued.intent_id],
            )
            self.assertEqual(
                reopened_store.get_work_intent(queued.intent_id)["state"],
                WorkIntentState.FAILED.value,
            )
            self.assertEqual(second_queue.pending_count(), 0)
            second_queue.close()
            reopened_store.close()

    def test_scope_scheduler_lock_rejects_a_second_live_consumer(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite3"
            scope = RuntimeScope("server", "world", "Bot1")
            store = RuntimeStateStore(path)
            first = PersistentWorkIntentQueue(store, scope, lease_owner="process-1")

            with self.assertRaises(RuntimeStateConflict):
                PersistentWorkIntentQueue(store, scope, lease_owner="process-2")

            first.close()
            second = PersistentWorkIntentQueue(store, scope, lease_owner="process-2")
            second.close()
            store.close()

    def test_scope_scheduler_lock_maps_cross_platform_lock_contention(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite3"
            scope = RuntimeScope("server", "world", "Bot1")
            store = RuntimeStateStore(path)

            with patch("minebot.app.work_queue.FileLock.acquire") as acquire:
                from filelock import Timeout

                acquire.side_effect = Timeout("scheduler.lock")
                with self.assertRaisesRegex(
                    RuntimeStateConflict,
                    "runtime scope already has an active scheduler",
                ):
                    PersistentWorkIntentQueue(store, scope, lease_owner="process-1")

            store.close()

    def test_priority_and_dedupe_are_enforced_in_sqlite(self):
        store = RuntimeStateStore(":memory:")
        scope = RuntimeScope("server", "world", "Bot1")
        queue = PersistentWorkIntentQueue(store, scope)
        first = queue.enqueue(
            WorkIntentKind.BODY_EVENT,
            source="body_event",
            payload={"name": "underAttack"},
            dedupe_key="underAttack:Bot1",
        )
        duplicate = queue.enqueue(
            WorkIntentKind.BODY_EVENT,
            source="body_event",
            payload={"name": "underAttack"},
            dedupe_key="underAttack:Bot1",
        )
        queue.enqueue(
            WorkIntentKind.QUIT,
            source="user_quit",
            payload={},
        )

        leased = queue.lease_next()

        self.assertEqual(first.intent_id, duplicate.intent_id)
        self.assertEqual(leased.kind, WorkIntentKind.QUIT)
        queue.complete(leased)
        queue.close()
        store.close()


if __name__ == "__main__":
    unittest.main()
