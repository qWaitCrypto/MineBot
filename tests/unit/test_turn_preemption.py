"""A running Agent turn is cancelled only for what the queue leases next.

Villa run 2026-10-16: a task continuation (priority 50) was queued at 02:20 and
stayed queued for 3 hours, because reflex facts (75) were always leased ahead of
it. The check for "is something waiting that may cancel the turn" ignored the
reflex facts and saw only the continuation, so every turn was cancelled as soon
as the next fact arrived: 292 of 345 turns, 63% of the wall time.
"""

from __future__ import annotations

import asyncio

import pytest

from minebot.app.runtime_state import RuntimeScope, RuntimeStateStore
from minebot.app.session import _WORK_PREEMPTED, AgentSession, SessionCommand, _queued_preemption
from minebot.app.work_queue import MemoryWorkIntentQueue, PersistentWorkIntentQueue, WorkIntentKind


def _queue(kind: str):
    if kind == "memory":
        return MemoryWorkIntentQueue()
    return PersistentWorkIntentQueue(RuntimeStateStore(":memory:"), RuntimeScope("server", "world", "Bot1"))


def _fact(queue, ident: str = "a1", status: str = "completed"):
    return queue.enqueue(
        WorkIntentKind.REFLEX_FACT, source="reflex_activity",
        payload={"fact": {"activity_id": ident, "status": status}},
        dedupe_key=f"reflex-fact:{ident}:{status}",
    )


QUEUES = pytest.mark.parametrize("kind", ["memory", "persistent"])


@QUEUES
def test_a_queued_task_continuation_does_not_cancel_the_running_turn(kind):
    queue = _queue(kind)
    queue.enqueue(WorkIntentKind.TASK_CONTINUE, source="task_checkpoint_continue", payload={})
    assert _queued_preemption(queue, "agent_turn") is None
    _fact(queue)
    assert _queued_preemption(queue, "agent_turn") is None


@QUEUES
def test_boundary_and_maintenance_wait_for_the_turn_too(kind):
    queue = _queue(kind)
    queue.enqueue(WorkIntentKind.TASK_BOUNDARY, source="session", payload={})
    queue.enqueue(WorkIntentKind.MAINTENANCE, source="session", payload={})
    assert _queued_preemption(queue, "agent_turn") is None


@QUEUES
def test_a_person_speaking_cancels_the_turn_even_behind_reflex_facts(kind):
    queue = _queue(kind)
    _fact(queue, "a1")
    _fact(queue, "a2")
    queue.enqueue(WorkIntentKind.MESSAGE, source="player", payload={"text": "stop that"})
    reason, interrupt_body = _queued_preemption(queue, "agent_turn")
    assert reason == "player" and interrupt_body is False
    # and the message really is what is leased next
    assert queue.lease_next().kind is WorkIntentKind.MESSAGE


@QUEUES
def test_death_is_leased_and_cancels_ahead_of_older_reflex_facts(kind):
    queue = _queue(kind)
    _fact(queue, "a1")
    queue.enqueue(WorkIntentKind.BODY_EVENT, source="body_event", payload={"event": {"name": "death"}})
    assert _queued_preemption(queue, "agent_turn") is not None
    assert queue.lease_next().kind is WorkIntentKind.BODY_EVENT


@QUEUES
def test_a_control_command_cancels_the_turn_and_the_body(kind):
    queue = _queue(kind)
    _fact(queue)
    queue.enqueue(WorkIntentKind.PAUSE, source="user", payload={"reason": "pause"})
    assert _queued_preemption(queue, "agent_turn") == ("pause", True)


def _supervise(kind: str, arrive):
    """A turn is running one reflex fact; `arrive` queues something while it runs."""
    queue = _queue(kind)
    _fact(queue, "running")
    queue.lease_next()
    session = object.__new__(AgentSession)
    session.work_queue, session.parts = queue, None
    session._trace = lambda *_a, **_k: None

    async def scenario():
        async def turn():
            await asyncio.sleep(0.6)
            return "finished"

        task = asyncio.create_task(session._run_supervised(
            turn(), work_kind="agent_turn", admission_version=queue.notification_version))
        await asyncio.sleep(0.1)
        arrive(queue)
        _fact(queue, "next")
        return await task

    return asyncio.run(scenario()), queue


@QUEUES
def test_the_turn_runs_to_its_end_while_only_facts_and_continuations_wait(kind):
    result, queue = _supervise(
        kind, lambda q: q.enqueue(WorkIntentKind.TASK_CONTINUE, source="task_checkpoint_continue", payload={}))
    assert result == "finished"
    assert queue.lease_next().kind is WorkIntentKind.REFLEX_FACT


@QUEUES
def test_the_turn_is_cancelled_for_a_message_and_the_message_is_served_next(kind):
    result, queue = _supervise(
        kind, lambda q: q.enqueue(WorkIntentKind.MESSAGE, source="player", payload={"text": "come here"}))
    assert result is _WORK_PREEMPTED
    assert queue.lease_next().kind is WorkIntentKind.MESSAGE


def test_every_waiting_reflex_fact_is_told_in_one_turn():
    from tests.unit.test_agent_session import build_parts

    calls, bodies = [], []
    session = AgentSession(lambda goal: build_parts(goal, calls, bodies))
    session.submit(SessionCommand.message("get started"))
    asyncio.run(session.step())
    assert session.parts is not None
    for index in range(3):
        session.parts.runtime.notify_reflex_fact({
            "kind": "direct_call", "activity_id": f"call-{index}", "tool_call_id": f"call-{index}",
            "tool": "move_to", "status": "completed", "result": {"success": True, "reason": "arrived"},
        })
    assert session.work_queue.pending_count() == 3
    turns_before = len(calls)
    asyncio.run(session.step())
    session.close()
    assert len(calls) == turns_before + 1
    assert session.work_queue.pending_count() == 0
    assert all(f"call-{index}" in calls[-1] for index in range(3))
