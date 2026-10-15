import asyncio
import sqlite3
from contextlib import closing

import pytest

from minebot.brain.progress import ProgressAuthority, ProgressStep
from minebot.contract import ProgressAbort
from minebot.app.progress_epochs import PersistentProgressEpochArchive
from minebot.app.runtime_state import RuntimeScope, RuntimeStateStore


def test_stop_causes_are_explicit_not_guessed_from_exception_text():
    from minebot.contract import ProgressStopReason

    assert ProgressAbort("body transport unstable").stop_reason is ProgressStopReason.UNKNOWN


def test_high_counters_never_stop_work():
    # The Agent stays live (frame-question doc §21): the counters are facts.
    authority = ProgressAuthority(failure_steps=50, stagnant_steps=50, stalled_steps=50, epistemic_steps=50)
    authority.commit_steps((ProgressStep("note", ("work",), "same", success=False),), "find another route",
                           novel_epistemic_keys=("region:1",))
    assert authority.failure_steps == 51
    assert authority.epistemic_steps == 51


@pytest.mark.parametrize("source,pending,expected", [
    ("agent.task", None, None),
    ("agent.task", "body_transport", "body_transport"),
    ("agent.task", "unknown", "unknown"),
    ("agent.task", "no_progress", "no_progress"),
    ("agent.task", "epistemic_budget", "epistemic_budget"),
    ("body.action", None, None),
])
def test_empty_task_closure_preserves_counters_and_pending_stops(source, pending, expected):
    from tests.unit.test_progress_epochs import EpochBody, make_tool, response, runtime_with

    authority = ProgressAuthority(failure_steps=5)
    tool = make_tool("close", lambda _: None, mutating=False, source=source)
    runtime, adapter, _ = runtime_with(EpochBody(), [tool], authority=authority)

    async def scenario():
        await adapter.open(response(("closure", "close")))
        member = adapter.claim_member("close", "{}", native_tool_call_id="closure")
        result = {"success": pending is None, "reason": "closed"}
        return adapter.settle(member, result=result, model_result=result,
            pending_abort=None if pending is None else ProgressAbort(stop_reason=pending))

    try:
        abort = asyncio.run(scenario())
        assert (None if abort is None else abort.stop_reason.value) == expected
        assert authority.failure_steps == 5
    finally:
        runtime.close()


@pytest.mark.parametrize("first,second,expected", [
    ("no_progress", "body_transport", "body_transport"),
    ("body_transport", "no_progress", "body_transport"),
    ("no_progress", "unknown", "unknown"),
    ("unknown", "no_progress", "unknown"),
    ("no_progress", "epistemic_budget", "epistemic_budget"),
    ("epistemic_budget", "body_transport", "body_transport"),
])
def test_stop_arbitration_cannot_weaken_a_hard_stop(first, second, expected):
    from minebot.contract import dominant_progress_abort

    assert dominant_progress_abort(
        ProgressAbort(stop_reason=first), None, ProgressAbort(stop_reason=second),
    ).stop_reason.value == expected
    assert dominant_progress_abort(None) is None


@pytest.mark.parametrize("reason", ["no_progress", "epistemic_budget", "body_transport", "unknown"])
def test_session_returns_typed_stop_without_implicitly_resuming(reason):
    from minebot.app.session import AgentSession, SessionCommand
    from minebot.contract import ProgressStopReason
    from tests.unit.test_agent_session import build_parts

    def factory(goal):
        parts = build_parts(goal, [], [])

        async def runner(*args, **kwargs):
            raise ProgressAbort("stop", stop_reason=ProgressStopReason(reason))

        parts.runtime.runner_run = runner
        return parts

    session = AgentSession(factory)
    try:
        session.submit(SessionCommand.start("reach a safe area"))
        step = asyncio.run(session.step())
        assert step.status == "yielded"
        assert step.progress_stop_reason.value == reason
        assert session.work_queue.pending_count() == 0
    finally:
        session.close()


def test_transport_stop_dominates_batch_stagnation_and_survives_restart(tmp_path):
    from minebot.contract import ProgressStopReason
    from tests.unit.test_progress_epochs import EpochBody, make_tool, response, runtime_with

    path = tmp_path / "runtime.sqlite3"
    scope = RuntimeScope("server", "world", "Bot")
    store = RuntimeStateStore(path)
    archive = PersistentProgressEpochArchive(store, scope)
    authority = ProgressAuthority(failure_steps=4)
    tool = make_tool("work", lambda _: None, mutating=True)
    runtime, adapter, _ = runtime_with(EpochBody(), [tool], authority=authority, epoch_archive=archive)
    runtime.lifecycle.ready()
    runtime.lifecycle.start()

    async def scenario():
        await adapter.open(response(("call-1", "work")))
        member = adapter.claim_member("work", "{}", native_tool_call_id="call-1")
        failure = {"success": False, "reason": "body_transport_error"}
        return adapter.settle(member, result=failure, model_result=failure,
            progress_steps=(ProgressStep("note", ("work",), "unchanged", success=False),),
            pending_abort=ProgressAbort("transport failed", stop_reason=ProgressStopReason.BODY_TRANSPORT))

    try:
        abort = asyncio.run(scenario())
        assert abort.stop_reason is ProgressStopReason.BODY_TRANSPORT
        outcome = runtime._yield_from_progress_abort(abort)
        assert outcome.progress_stop_reason is ProgressStopReason.BODY_TRANSPORT
        assert runtime.trace.snapshot()[-1]["progress_stop_reason"] == "body_transport"
    finally:
        runtime.close()
        store.close()
    with closing(RuntimeStateStore(path)) as reopened:
        rows = reopened.list_progress_epochs_after(scope, cursor=0)
        assert rows[0]["progress_aborted"] is True
        assert rows[0]["progress_stop_reason"] == "body_transport"
        reopened.mark_progress_epoch_aborted(scope, rows[0]["epoch_id"])
        assert reopened.list_progress_epochs_after(scope, cursor=0)[0]["progress_stop_reason"] == "body_transport"


def test_v16_migration_does_not_invent_causes_for_historical_aborts(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    scope = RuntimeScope("server", "world", "Bot")
    with closing(RuntimeStateStore(path)) as store:
        for index, aborted in enumerate((False, True)):
            store.create_progress_epoch(scope, record={
                "epoch_id": f"epoch-{index}", "run_id": f"run-{index}", "model_turn": 1,
                "members": [], "progress_aborted": aborted,
            })
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE progress_epochs DROP COLUMN progress_stop_reason")
        connection.execute("UPDATE minebot_schema SET version = 16")
    with closing(RuntimeStateStore(path)) as migrated:
        rows = migrated.list_progress_epochs_after(scope, cursor=0)
        assert rows[0]["progress_stop_reason"] is None
        assert rows[1]["progress_stop_reason"] == "unknown"


@pytest.mark.parametrize("mutating", [False, True])
def test_progress_counters_cannot_hide_real_tool_receipts_or_block_the_next_call(mutating):
    from minebot.brain.registry import WeldContext, execute_tool
    from minebot.contract import ToolResult
    from tests.unit.test_progress_epochs import EpochBody, make_tool

    authority = ProgressAuthority(failure_steps=5, stagnant_steps=5, stalled_steps=5)
    body = EpochBody()
    weld = WeldContext(body=body, authority=authority, goal_text="continue")
    receipt = ToolResult(False, "world_result_mismatch", True, metrics={"matched": 9, "missing": 1})
    tool = make_tool("work", lambda _: receipt, mutating=mutating)
    for _ in range(12):
        assert execute_tool(tool, {}, weld) == receipt.to_payload()
    if not mutating:
        assert (authority.failure_steps, authority.stagnant_steps, authority.stalled_steps) == (5, 5, 5)
    next_receipt = ToolResult(True, "structure_verified", False, metrics={"matched": 10})
    next_tool = make_tool("work", lambda _: next_receipt, mutating=mutating)
    assert execute_tool(next_tool, {}, weld) == next_receipt.to_payload()
