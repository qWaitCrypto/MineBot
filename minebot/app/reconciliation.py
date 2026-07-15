"""Authoritative process-start reconciliation for the persistent agent runtime."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
import time
from uuid import uuid4

from minebot.app.body_events import (
    ALWAYS_MATERIAL_BODY_EVENTS,
    BodyEventPump,
    TASK_TERMINAL_BODY_EVENTS,
    event_matches_wait_conditions,
)
from minebot.app.runtime_state import TaskRecord, TaskStatus
from minebot.app.tasks import TaskWorkspace
from minebot.app.work_queue import WorkIntent, WorkIntentKind, WorkIntentQueue
from minebot.contract import Body, BodyState, Event, InventorySlot, perception_next_cursor


class ReconcileDecision(str, Enum):
    IDLE = "idle"
    PARK = "park"
    WAKE = "wake"
    RESUME = "resume"
    RECOVER = "recover"
    COMPLETE = "complete"


class StartupReconciliationError(RuntimeError):
    """Authoritative startup state could not be reconciled safely."""


TerminalProbe = Callable[[TaskRecord, dict[str, int]], dict[str, object]]


@dataclass(frozen=True)
class StartupReconciliation:
    intent: WorkIntent
    decision: ReconcileDecision
    state: BodyState
    inventory_counts: dict[str, int]
    events: tuple[Event, ...]
    orphaned_intents: tuple[WorkIntent, ...]


def enqueue_startup_reconciliation(
    *,
    body: Body,
    event_pump: BodyEventPump,
    queue: WorkIntentQueue,
    workspace: TaskWorkspace,
    orphaned_intents: tuple[WorkIntent, ...] = (),
    app_reloaded: bool,
    terminal_probe: TerminalProbe | None = None,
) -> StartupReconciliation:
    stale = queue.queued_intents(WorkIntentKind.RECOVERY_RECONCILE)
    stale_body_events = queue.queued_intents(WorkIntentKind.BODY_EVENT)
    stale_continuations = queue.queued_intents(WorkIntentKind.TASK_CONTINUE)
    carried_events = _events_from_reconcile_intents(stale)
    carried_events.extend(_events_from_body_intents(stale_body_events))
    if stale or stale_body_events or stale_continuations:
        queue.supersede(
            {
                WorkIntentKind.RECOVERY_RECONCILE,
                WorkIntentKind.BODY_EVENT,
                WorkIntentKind.TASK_CONTINUE,
            },
            reason="startup_reconcile_refreshed",
        )

    interrupt = body.interrupt("startup_reconcile")
    if not (interrupt.ok and interrupt.accepted):
        raise StartupReconciliationError(
            f"startup Body interrupt failed: {interrupt.error or interrupt.data}"
        )
    owner_after_interrupt = _wait_for_owner_release(event_pump)
    fresh_events = event_pump.read_events()
    events = _merge_events(carried_events, fresh_events)
    state = body.get_state()
    inventory_counts = {} if state.missing else _inventory_counts(body)
    task = workspace.current_task
    checkpoint = (
        None
        if task is None
        else workspace.store.get_latest_checkpoint(task.task_id)
    )
    terminal = (
        {"satisfied": False}
        if task is None or terminal_probe is None
        else dict(terminal_probe(task, inventory_counts))
    )
    material_events = []
    for event in events:
        if event.name in ALWAYS_MATERIAL_BODY_EVENTS:
            material_events.append(event)
            continue
        if task is None or event.name not in TASK_TERMINAL_BODY_EVENTS:
            continue
        if task.status is TaskStatus.WAITING_EVENT:
            conditions = () if checkpoint is None else checkpoint.wait_for
            if not event_matches_wait_conditions(event, conditions):
                continue
        material_events.append(event)
    decision = _decision(
        task=task,
        state=state,
        terminal=terminal,
        material_events=material_events,
        has_orphaned_work=bool(orphaned_intents),
    )
    payload = {
        "reconciliation_id": f"reconcile-{uuid4().hex}",
        "decision": decision.value,
        "app_reloaded": app_reloaded,
        "body_owner_before_interrupt": event_pump.initial_owner,
        "body_owner_after_interrupt": owner_after_interrupt,
        "state": _state_payload(state),
        "inventory_counts": inventory_counts,
        "events": [_event_payload(event) for event in events],
        "event_count": len(events),
        "material_event_count": len(material_events),
        "orphaned_intents": [_orphan_payload(intent) for intent in orphaned_intents],
        "task": None if task is None else _task_payload(task),
        "checkpoint": None if checkpoint is None else {
            "checkpoint_id": checkpoint.checkpoint_id,
            "revision": checkpoint.revision,
            "disposition": checkpoint.disposition.value,
            "summary": checkpoint.summary,
            "next_step": checkpoint.next_step,
            "wait_for": list(checkpoint.wait_for),
            "body_fingerprint": checkpoint.body_fingerprint,
            "continuation": (
                None
                if checkpoint.continuation is None
                else {
                    "objective": checkpoint.continuation.objective,
                    "operation_class": checkpoint.continuation.operation_class.value,
                    "target_descriptor": dict(checkpoint.continuation.target_descriptor),
                    "expected_evidence": list(checkpoint.continuation.expected_evidence),
                    "bounded_epoch_budget": checkpoint.continuation.bounded_epoch_budget,
                    "approach_key": checkpoint.continuation.approach_key,
                    "evidence_cursor": checkpoint.continuation.evidence_cursor,
                    "generation": checkpoint.continuation.generation,
                }
            ),
        },
        "terminal": terminal,
    }
    intent = queue.enqueue(
        WorkIntentKind.RECOVERY_RECONCILE,
        source="startup_reconcile",
        payload=payload,
        task_id=None if task is None else task.task_id,
    )
    event_pump.acknowledge_cursor()
    return StartupReconciliation(
        intent=intent,
        decision=decision,
        state=state,
        inventory_counts=inventory_counts,
        events=tuple(events),
        orphaned_intents=tuple(orphaned_intents),
    )


def _wait_for_owner_release(
    event_pump: BodyEventPump,
    *,
    attempts: int = 50,
    delay_s: float = 0.1,
) -> str | None:
    owner = None
    for attempt in range(attempts):
        head = event_pump.body.event_head(event_pump.epoch)
        owner = None if head.get("owner") is None else str(head["owner"])
        if owner is None:
            return None
        if attempt + 1 < attempts:
            time.sleep(delay_s)
    raise StartupReconciliationError(
        f"Body owner remained active after startup interrupt: {owner}"
    )


def _decision(
    *,
    task: TaskRecord | None,
    state: BodyState,
    terminal: dict[str, object],
    material_events: list[Event],
    has_orphaned_work: bool,
) -> ReconcileDecision:
    if state.missing or state.health <= 0:
        return ReconcileDecision.RECOVER
    if task is not None and terminal.get("satisfied") is True:
        return ReconcileDecision.COMPLETE
    if task is None:
        return (
            ReconcileDecision.WAKE
            if material_events or has_orphaned_work
            else ReconcileDecision.IDLE
        )
    if task.status is TaskStatus.RUNNING:
        return ReconcileDecision.RESUME
    if task.status is TaskStatus.WAITING_EVENT and material_events:
        return ReconcileDecision.RESUME
    return ReconcileDecision.WAKE if has_orphaned_work else ReconcileDecision.PARK


def _inventory_counts(body: Body, *, page_size: int = 12) -> dict[str, int]:
    counts: dict[str, int] = {}
    start: int | None = 0
    seen_starts: set[int] = set()
    while start is not None:
        if start in seen_starts:
            raise ValueError(f"startup inventory cursor repeated: {start}")
        seen_starts.add(start)
        perception = body.perceive("inventory", {"start": start, "limit": page_size})
        cursor = perception_next_cursor(perception)
        if not perception.ok or (not perception.complete and cursor is None):
            raise ValueError(
                "startup inventory refresh failed: "
                f"ok={perception.ok} complete={perception.complete} error={perception.error}"
            )
        for payload in perception.data.get("slots") or []:
            if not isinstance(payload, dict):
                continue
            slot = InventorySlot.from_payload(payload)
            if slot.empty or not slot.item:
                continue
            item = str(slot.item).removeprefix("minecraft:")
            counts[item] = counts.get(item, 0) + slot.count
        start = None if cursor is None else int(cursor)
    return counts


def _events_from_reconcile_intents(intents: list[WorkIntent]) -> list[Event]:
    events: list[Event] = []
    for intent in intents:
        raw_events = intent.payload.get("events")
        if not isinstance(raw_events, list):
            continue
        for raw in raw_events:
            if not isinstance(raw, dict):
                continue
            data = raw.get("data")
            events.append(
                Event(
                    seq=int(raw.get("seq") or 0),
                    tick=int(raw.get("tick") or 0),
                    bot=str(raw.get("bot") or ""),
                    name=str(raw.get("name") or ""),
                    data=dict(data) if isinstance(data, dict) else {},
                )
            )
    return events


def _events_from_body_intents(intents: list[WorkIntent]) -> list[Event]:
    events: list[Event] = []
    for intent in intents:
        raw = intent.payload.get("event")
        if not isinstance(raw, dict):
            continue
        data = raw.get("data")
        events.append(
            Event(
                seq=int(raw.get("seq") or 0),
                tick=int(raw.get("tick") or 0),
                bot=str(raw.get("bot") or ""),
                name=str(raw.get("name") or ""),
                data=dict(data) if isinstance(data, dict) else {},
            )
        )
    return events


def _merge_events(previous: list[Event], current: list[Event]) -> list[Event]:
    merged = {(event.bot, event.seq, event.name): event for event in previous}
    for event in current:
        merged[(event.bot, event.seq, event.name)] = event
    return sorted(merged.values(), key=lambda event: (event.seq, event.name))


def _state_payload(state: BodyState) -> dict[str, object]:
    return {
        "bot": state.bot,
        "pos": list(state.pos),
        "health": state.health,
        "food": state.food,
        "oxygen": state.oxygen,
        "inventory_hash": state.inventory_hash,
        "dimension": state.dimension,
        "complete": state.complete,
        "missing": state.missing,
    }


def _event_payload(event: Event) -> dict[str, object]:
    return {
        "seq": event.seq,
        "tick": event.tick,
        "bot": event.bot,
        "name": event.name,
        "data": dict(event.data),
    }


def _orphan_payload(intent: WorkIntent) -> dict[str, object]:
    return {
        "intent_id": intent.intent_id,
        "kind": intent.kind.value,
        "task_id": intent.task_id,
        "generation": intent.generation,
        "attempt_count": intent.attempt_count,
        "state_at_restart": intent.state.value,
    }


def _task_payload(task: TaskRecord) -> dict[str, object]:
    return {
        "task_id": task.task_id,
        "revision": task.revision,
        "goal": task.goal_text,
        "status": task.status.value,
        "completion_authority": task.completion_authority.value,
    }


__all__ = [
    "ReconcileDecision",
    "StartupReconciliationError",
    "StartupReconciliation",
    "enqueue_startup_reconciliation",
]
