import unittest

from minebot.app.body_events import BodyEventPump
from minebot.app.runtime_state import RuntimeScope, RuntimeStateStore
from minebot.app.work_queue import MemoryWorkIntentQueue, WorkIntentKind
from minebot.contract import Event


class EventBody:
    def __init__(self, *, epoch="app-1", event_seq=0, chat_seq=0):
        self.epoch = epoch
        self.head_event_seq = event_seq
        self.head_chat_seq = chat_seq
        self.last_seq = 0
        self.last_chat_seq = 0
        self.events = []
        self.chat_events = []

    def event_head(self, _proposed_epoch):
        return {
            "event_seq": self.head_event_seq,
            "chat_seq": self.head_chat_seq,
            "tick": 100,
            "epoch": self.epoch,
        }

    def poll_events(self):
        fresh = [event for event in self.events if event.seq > self.last_seq]
        for event in fresh:
            self.last_seq = max(self.last_seq, event.seq)
        return fresh

    def poll_chat_events(self):
        fresh = [event for event in self.chat_events if event.seq > self.last_chat_seq]
        for event in fresh:
            self.last_chat_seq = max(self.last_chat_seq, event.seq)
        return fresh


def event(seq, name, *, data=None):
    return Event(seq=seq, tick=100 + seq, bot="Bot1", name=name, data=data or {})


class BodyEventPumpTests(unittest.TestCase):
    def setUp(self):
        self.store = RuntimeStateStore(":memory:")
        self.scope = RuntimeScope("server", "world", "Bot1")
        self.queue = MemoryWorkIntentQueue()

    def tearDown(self):
        self.store.close()

    def test_material_events_wake_queue_and_repeated_attack_facts_coalesce(self):
        body = EventBody()
        body.events = [
            event(1, "moveStarted"),
            event(2, "underAttack", data={"health": 18}),
            event(3, "underAttack", data={"health": 16}),
        ]
        pump = BodyEventPump(body, self.queue, self.store, self.scope)

        result = pump.poll_once()
        leased = self.queue.lease_next()

        self.assertEqual(result.observed, 3)
        self.assertEqual(result.material, 1)
        self.assertEqual(leased.kind, WorkIntentKind.BODY_EVENT)
        self.assertEqual(leased.payload["event"]["seq"], 3)
        self.assertEqual(leased.payload["event"]["data"]["health"], 16)
        self.assertEqual(self.store.get_event_cursor(self.scope), (3, 0, "app-1"))

    def test_completed_event_dedupe_prevents_replay_after_cursor_write_loss(self):
        body = EventBody()
        body.events = [event(1, "death")]
        pump = BodyEventPump(body, self.queue, self.store, self.scope)
        pump.poll_once()
        first = self.queue.lease_next()
        self.queue.complete(first)

        body.last_seq = 0
        pump.poll_once()

        self.assertEqual(self.queue.pending_count(), 0)

    def test_resolved_reflex_and_taskless_action_terminal_do_not_wake_model(self):
        body = EventBody()
        body.events = [
            event(1, "reflexCompleted", data={"reason": "escaped"}),
            event(2, "moveDone", data={"action_id": "old-action"}),
        ]
        pump = BodyEventPump(body, self.queue, self.store, self.scope)

        result = pump.poll_once()

        self.assertEqual(result.observed, 2)
        self.assertEqual(result.material, 0)
        self.assertEqual(result.enqueued, 0)
        self.assertEqual(self.queue.pending_count(), 0)

    def test_running_task_terminal_does_not_wake_without_explicit_wait(self):
        body = EventBody()
        body.events = [event(1, "moveDone", data={"action_id": "task-action"})]
        pump = BodyEventPump(body, self.queue, self.store, self.scope)

        result = pump.poll_once(task_id="task-1", generation=7)

        self.assertEqual(result.enqueued, 0)
        self.assertEqual(self.queue.pending_count(), 0)

    def test_waiting_task_terminal_binds_checkpoint_generation(self):
        body = EventBody()
        body.events = [event(1, "moveDone", data={"action_id": "task-action"})]
        pump = BodyEventPump(body, self.queue, self.store, self.scope)

        result = pump.poll_once(
            task_id="task-1",
            generation=7,
            task_waiting=True,
            wait_checkpoint_id="checkpoint-1",
            wait_for=("action:task-action",),
        )
        intent = self.queue.lease_next()

        self.assertEqual(result.enqueued, 1)
        self.assertEqual(intent.task_id, "task-1")
        self.assertEqual(intent.generation, 7)
        self.assertEqual(intent.payload["wait_checkpoint_id"], "checkpoint-1")

    def test_waiting_task_wakes_only_for_declared_event_or_action(self):
        body = EventBody()
        body.events = [
            event(1, "moveDone", data={"action_id": "other"}),
            event(2, "furnaceDone", data={"action_id": "smelt-1"}),
            event(3, "moveDone", data={"action_id": "wanted-action"}),
        ]
        pump = BodyEventPump(body, self.queue, self.store, self.scope)

        result = pump.poll_once(
            task_id="task-1",
            generation=4,
            task_waiting=True,
            wait_checkpoint_id="checkpoint-1",
            wait_for=("event:furnaceDone", "action:wanted-action"),
        )
        first = self.queue.lease_next()
        self.queue.complete(first)
        second = self.queue.lease_next()

        self.assertEqual(result.material, 2)
        self.assertEqual(
            {first.payload["event"]["seq"], second.payload["event"]["seq"]},
            {2, 3},
        )

    def test_waiting_task_free_text_condition_does_not_guess_a_wake(self):
        body = EventBody()
        body.events = [event(1, "furnaceDone", data={"action_id": "smelt-1"})]
        pump = BodyEventPump(body, self.queue, self.store, self.scope)

        result = pump.poll_once(
            task_id="task-1",
            task_waiting=True,
            wait_for=("furnace output available",),
        )

        self.assertEqual(result.material, 0)
        self.assertEqual(result.enqueued, 0)

    def test_cursor_restores_only_with_matching_app_epoch(self):
        self.store.set_event_cursor(
            self.scope,
            last_seq=7,
            last_chat_seq=4,
            event_epoch="old-app",
        )
        same = EventBody(epoch="old-app", event_seq=9, chat_seq=5)
        BodyEventPump(same, self.queue, self.store, self.scope)
        self.assertEqual((same.last_seq, same.last_chat_seq), (7, 4))

        changed = EventBody(epoch="new-app", event_seq=2, chat_seq=1)
        BodyEventPump(changed, self.queue, self.store, self.scope)
        self.assertEqual((changed.last_seq, changed.last_chat_seq), (0, 0))

    def test_chat_cursor_is_persisted_with_body_cursor(self):
        body = EventBody()
        body.chat_events = [event(1, "agentChat", data={"sender": "Steve", "message": "hi"})]
        pump = BodyEventPump(body, self.queue, self.store, self.scope)

        events = pump.poll_chat_events()

        self.assertEqual(len(events), 1)
        self.assertEqual(self.store.get_event_cursor(self.scope), (0, 0, "app-1"))
        pump.acknowledge_cursor()
        self.assertEqual(self.store.get_event_cursor(self.scope), (0, 1, "app-1"))


if __name__ == "__main__":
    unittest.main()
