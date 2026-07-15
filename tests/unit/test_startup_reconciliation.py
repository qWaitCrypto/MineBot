import unittest

from minebot.app.body_events import BodyEventPump
from minebot.app.reconciliation import ReconcileDecision, enqueue_startup_reconciliation
from minebot.app.runtime_state import CheckpointDisposition, RuntimeScope, RuntimeStateStore, TaskStatus
from minebot.app.tasks import TaskWorkspace
from minebot.app.work_queue import MemoryWorkIntentQueue, WorkIntentKind, WorkIntentState
from minebot.contract import BodyState, Event, PerceptionResult, Result


def body_state(*, missing=False, health=20.0, inventory_hash="inv"):
    return BodyState(
        bot="Bot1",
        pos=(1.0, 64.0, 2.0),
        yaw=0.0,
        pitch=0.0,
        health=health,
        food=20,
        oxygen=300,
        inventory_raw="",
        inventory_hash=inventory_hash,
        effects=[],
        time=1000,
        weather=None,
        dimension="minecraft:overworld",
        complete=True,
        missing=missing,
    )


class ReconcileBody:
    def __init__(self, *, state=None, events=(), inventory=None, epoch="app-1"):
        self.state = state or body_state()
        self.events = list(events)
        self.inventory = dict(inventory or {})
        self.epoch = epoch
        self.last_seq = 0
        self.last_chat_seq = 0
        self.calls = []

    def event_head(self, _proposed_epoch):
        return {
            "event_seq": 0,
            "chat_seq": 0,
            "tick": 100,
            "epoch": self.epoch,
        }

    def interrupt(self, reason=None):
        self.calls.append(("interrupt", reason))
        return Result(None, "Bot1", "result", True, True, True)

    def poll_events(self):
        self.calls.append(("events", None))
        fresh = [event for event in self.events if event.seq > self.last_seq]
        if fresh:
            self.last_seq = max(event.seq for event in fresh)
        return fresh

    def poll_chat_events(self):
        return []

    def get_state(self):
        self.calls.append(("state", None))
        return self.state

    def perceive(self, scope, params):
        self.calls.append(("perceive", scope))
        if self.state.missing:
            return PerceptionResult(
                "Bot1",
                scope,
                "perception",
                False,
                True,
                {},
                error="missing_body",
            )
        slots = [
            {
                "slot": index,
                "item": item,
                "count": count,
                "empty": False,
            }
            for index, (item, count) in enumerate(sorted(self.inventory.items()))
        ]
        return PerceptionResult(
            "Bot1",
            scope,
            "perception",
            True,
            True,
            {"slots": slots},
        )


class StartupReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.store = RuntimeStateStore(":memory:")
        self.scope = RuntimeScope("server", "world", "Bot1")
        self.workspace = TaskWorkspace(self.store, self.scope)
        self.queue = MemoryWorkIntentQueue()

    def tearDown(self):
        self.queue.close()
        self.store.close()

    def reconcile(self, body, **kwargs):
        pump = BodyEventPump(body, self.queue, self.store, self.scope)
        return enqueue_startup_reconciliation(
            body=body,
            event_pump=pump,
            queue=self.queue,
            workspace=self.workspace,
            app_reloaded=False,
            **kwargs,
        )

    def test_running_task_refreshes_truth_and_enqueues_exactly_one_resume(self):
        task = self.workspace.start("prepare for the End", source="user")
        body = ReconcileBody(
            events=[Event(1, 101, "Bot1", "moveDone", {"action_id": "a1"})],
            inventory={"oak_log": 7},
        )

        result = self.reconcile(body)

        self.assertEqual(result.decision, ReconcileDecision.RESUME)
        self.assertEqual(result.inventory_counts, {"oak_log": 7})
        self.assertEqual(body.calls[0], ("interrupt", "startup_reconcile"))
        self.assertLess(body.calls.index(("events", None)), body.calls.index(("state", None)))
        queued = self.queue.queued_intents(WorkIntentKind.RECOVERY_RECONCILE)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0].task_id, task.task_id)
        self.assertEqual(queued[0].payload["decision"], "resume")
        self.assertEqual(queued[0].payload["inventory_counts"], {"oak_log": 7})
        self.assertEqual(self.store.get_event_cursor(self.scope), (1, 0, "app-1"))

    def test_paused_task_is_parked_without_model_wake(self):
        task = self.workspace.start("prepare for the End", source="user")
        self.store.transition_task(
            task.task_id,
            expected_revision=task.revision,
            status=TaskStatus.PAUSED,
        )

        result = self.reconcile(ReconcileBody())

        self.assertEqual(result.decision, ReconcileDecision.PARK)
        self.assertEqual(result.intent.payload["decision"], "park")

    def test_missing_body_requires_recovery_even_without_task(self):
        body = ReconcileBody(state=body_state(missing=True, health=0.0))

        result = self.reconcile(body)

        self.assertEqual(result.decision, ReconcileDecision.RECOVER)
        self.assertEqual(result.inventory_counts, {})
        self.assertNotIn(("perceive", "inventory"), body.calls)

    def test_waiting_task_resumes_only_when_replayed_event_matches_condition(self):
        task = self.workspace.start("smelt iron", source="user")
        self.workspace.checkpoint(
            expected_task_revision=task.revision,
            disposition=CheckpointDisposition.WAIT_EVENT,
            summary="wait for smelting",
            wait_for=("event:furnaceDone",),
        )

        matching = self.reconcile(
            ReconcileBody(events=[Event(1, 101, "Bot1", "furnaceDone", {"action_id": "f1"})])
        )

        self.assertEqual(matching.decision, ReconcileDecision.RESUME)

    def test_waiting_task_ignores_unrelated_replayed_terminal(self):
        task = self.workspace.start("smelt iron", source="user")
        self.workspace.checkpoint(
            expected_task_revision=task.revision,
            disposition=CheckpointDisposition.WAIT_EVENT,
            summary="wait for smelting",
            wait_for=("event:furnaceDone",),
        )

        unrelated = self.reconcile(
            ReconcileBody(events=[Event(1, 101, "Bot1", "moveDone", {"action_id": "m1"})])
        )

        self.assertEqual(unrelated.decision, ReconcileDecision.PARK)

    def test_authoritative_terminal_probe_completes_before_resume(self):
        self.workspace.start("collect 3 oak_log", source="user")

        result = self.reconcile(
            ReconcileBody(inventory={"oak_log": 3}),
            terminal_probe=lambda _task, counts: {
                "satisfied": counts.get("oak_log", 0) >= 3,
                "inventory_count": counts.get("oak_log", 0),
            },
        )

        self.assertEqual(result.decision, ReconcileDecision.COMPLETE)
        self.assertTrue(result.intent.payload["terminal"]["satisfied"])

    def test_stale_event_and_reconcile_intents_are_merged_then_superseded(self):
        orphan = self.queue.enqueue(
            WorkIntentKind.MESSAGE,
            source="user_message",
            payload={"text": "old"},
        )
        orphan = self.queue.lease_next()
        stale_event = self.queue.enqueue(
            WorkIntentKind.BODY_EVENT,
            source="body_event",
            payload={
                "event": {
                    "seq": 1,
                    "tick": 101,
                    "bot": "Bot1",
                    "name": "underAttack",
                    "data": {"health": 18},
                }
            },
        )
        stale_reconcile = self.queue.enqueue(
            WorkIntentKind.RECOVERY_RECONCILE,
            source="startup_reconcile",
            payload={
                "events": [
                    {
                        "seq": 2,
                        "tick": 102,
                        "bot": "Bot1",
                        "name": "mobilityBlocked",
                        "data": {},
                    }
                ]
            },
        )
        body = ReconcileBody(
            events=[Event(3, 103, "Bot1", "respawned", {})]
        )

        result = self.reconcile(body, orphaned_intents=(orphan,))

        self.assertEqual([event.seq for event in result.events], [1, 2, 3])
        self.assertEqual(result.intent.payload["orphaned_intents"][0]["intent_id"], orphan.intent_id)
        self.assertEqual(self.queue._records[stale_event.intent_id].state, WorkIntentState.SUPERSEDED)
        self.assertEqual(self.queue._records[stale_reconcile.intent_id].state, WorkIntentState.SUPERSEDED)
        self.assertEqual(len(self.queue.queued_intents(WorkIntentKind.RECOVERY_RECONCILE)), 1)


if __name__ == "__main__":
    unittest.main()
