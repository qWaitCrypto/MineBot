"""Idle-safe Body event ingestion into the unified WorkIntent queue."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from minebot.app.runtime_state import RuntimeScope, RuntimeStateStore
from minebot.app.work_queue import WorkIntentKind, WorkIntentQueue, WorkIntentState
from minebot.contract import Event


ALWAYS_MATERIAL_BODY_EVENTS = {
    "death",
    "bodyMissing",
    "respawned",
    "underAttack",
    "mobilityBlocked",
}

TASK_TERMINAL_BODY_EVENTS = {
    "moveDone",
    "navigateDone",
    "lookDone",
    "jumpDone",
    "selectSlotDone",
    "selectItemDone",
    "stopDone",
    "mineDone",
    "placeDone",
    "useDone",
    "rangedDone",
    "igniteDone",
    "sowDone",
    "attackDone",
    "engageDone",
    "followDone",
    "craftDone",
    "containerDone",
    "furnaceDone",
    "dropDone",
    "handoffDone",
    "moveItemDone",
    "itemPickup",
}

MATERIAL_BODY_EVENTS = ALWAYS_MATERIAL_BODY_EVENTS | TASK_TERMINAL_BODY_EVENTS


class EventBody(Protocol):
    last_seq: int
    last_chat_seq: int

    def event_head(self, proposed_epoch: str) -> dict[str, object]: ...

    def poll_events(self) -> list[Event]: ...

    def poll_chat_events(self) -> list[Event]: ...


@dataclass(frozen=True)
class BodyEventPollResult:
    observed: int
    material: int
    enqueued: int
    last_seq: int
    epoch: str


class BodyEventPump:
    """Poll Body events only while the outer session loop is idle."""

    def __init__(
        self,
        body: EventBody,
        queue: WorkIntentQueue,
        store: RuntimeStateStore,
        scope: RuntimeScope,
    ) -> None:
        self.body = body
        self.queue = queue
        self.store = store
        self.scope = scope
        head = body.event_head(f"app-{uuid4().hex}")
        self.epoch = str(head["epoch"])
        self.initial_owner = None if head.get("owner") is None else str(head["owner"])
        head_seq = max(0, int(head.get("event_seq") or 0))
        head_chat_seq = max(0, int(head.get("chat_seq") or 0))
        persisted = store.get_event_cursor(scope)
        if persisted is None:
            body.last_seq = head_seq
            body.last_chat_seq = head_chat_seq
        elif (
            persisted[2] == self.epoch
            and persisted[0] <= head_seq
            and persisted[1] <= head_chat_seq
        ):
            body.last_seq = persisted[0]
            body.last_chat_seq = persisted[1]
        else:
            body.last_seq = 0
            body.last_chat_seq = 0
        store.set_event_cursor(
            scope,
            last_seq=body.last_seq,
            last_chat_seq=body.last_chat_seq,
            event_epoch=self.epoch,
        )

    def poll_once(
        self,
        *,
        task_id: str | None = None,
        generation: int | None = None,
        task_waiting: bool = False,
        wait_checkpoint_id: str | None = None,
        wait_for: tuple[str, ...] = (),
    ) -> BodyEventPollResult:
        events = self.read_events()
        selected = _coalesce_material_events(
            events,
            task_waiting=task_waiting,
            wait_for=wait_for,
        )
        enqueued = 0
        for event in selected:
            waits_for_terminal = event.name in TASK_TERMINAL_BODY_EVENTS
            intent = self.queue.enqueue(
                WorkIntentKind.BODY_EVENT,
                source="body_event",
                payload={
                    "event": {
                        "seq": event.seq,
                        "tick": event.tick,
                        "bot": event.bot,
                        "name": event.name,
                        "data": dict(event.data),
                    },
                    "wait_checkpoint_id": (
                        wait_checkpoint_id if waits_for_terminal else None
                    ),
                },
                dedupe_key=f"body:{self.epoch}:{event.seq}",
                task_id=task_id if task_id is not None else None,
                generation=generation if waits_for_terminal else None,
            )
            if intent.state is WorkIntentState.QUEUED:
                enqueued += 1
        self.acknowledge_cursor()
        return BodyEventPollResult(
            observed=len(events),
            material=len(selected),
            enqueued=enqueued,
            last_seq=self.body.last_seq,
            epoch=self.epoch,
        )

    def poll_chat_events(self) -> list[Event]:
        return self.body.poll_chat_events()

    def read_events(self) -> list[Event]:
        return self.body.poll_events()

    def acknowledge_cursor(self) -> None:
        self.store.set_event_cursor(
            self.scope,
            last_seq=self.body.last_seq,
            last_chat_seq=self.body.last_chat_seq,
            event_epoch=self.epoch,
        )


def _coalesce_material_events(
    events: list[Event],
    *,
    task_waiting: bool = False,
    wait_for: tuple[str, ...] = (),
) -> list[Event]:
    selected: dict[tuple[str, str], Event] = {}
    for event in events:
        always_material = event.name in ALWAYS_MATERIAL_BODY_EVENTS
        task_terminal = (
            task_waiting
            and event.name in TASK_TERMINAL_BODY_EVENTS
            and event_matches_wait_conditions(event, wait_for)
        )
        if not always_material and not task_terminal:
            continue
        action_id = str(event.data.get("action_id") or "")
        key = (event.name, action_id)
        selected[key] = event
    return sorted(selected.values(), key=lambda event: event.seq)


def event_matches_wait_conditions(event: Event, conditions: tuple[str, ...]) -> bool:
    event_name = event.name.strip().lower()
    action_id = str(event.data.get("action_id") or "").strip().lower()
    entity_values = {
        str(event.data.get(key) or "").strip().lower()
        for key in ("entity", "entity_id", "uuid", "target", "target_uuid", "name", "attacker")
    }
    for raw in conditions:
        condition = str(raw).strip()
        if not condition:
            continue
        prefix, separator, value = condition.partition(":")
        if not separator:
            if condition.lower() == event_name:
                return True
            continue
        normalized = value.strip().lower()
        if prefix.strip().lower() == "event" and normalized == event_name:
            return True
        if prefix.strip().lower() == "action" and normalized and normalized == action_id:
            return True
        if prefix.strip().lower() == "entity" and normalized and normalized in entity_values:
            return True
    return False


__all__ = [
    "ALWAYS_MATERIAL_BODY_EVENTS",
    "BodyEventPollResult",
    "BodyEventPump",
    "MATERIAL_BODY_EVENTS",
    "TASK_TERMINAL_BODY_EVENTS",
    "event_matches_wait_conditions",
]
