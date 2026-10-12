"""Bounded strategic reconsideration: one debited allowance, never refilled.

A reviewable progress stop already lets the model deliberate. These tests pin
the separate boundary that lets a *genuinely different* approach execute again:
a non-renewable task-owned allowance, stamped onto the successor approach by
the exact stop epoch it reviewed. Renamed approaches, already-attempted
approaches, hard stops, and an exhausted allowance must all stay terminal.
"""

import asyncio
import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from minebot.app.autonomy import AutonomyAction, AutonomyCoordinator
from minebot.app.progress_epochs import PersistentProgressEpochArchive
from minebot.app.runtime_state import (
    TASK_RECONSIDERATION_ALLOWANCE,
    CheckpointDisposition,
    RuntimeScope,
    RuntimeStateStore,
)
from minebot.app.tasks import TaskWorkspace, register_task_tools
from minebot.brain.feedback import failure_evidence_key
from minebot.brain.lifecycle import LifecycleState
from minebot.brain.progress import ProgressAuthority, ProgressStep
from minebot.brain.registry import ToolRegistry
from minebot.contract import ProgressAbort, ProgressStopReason

GENERATION = 3


def descriptor(identifier, *, kind="place"):
    return {"kind": kind, "identifier": identifier, "traits": []}


def continuation_params(task, *, identifier, objective, budget=4):
    return {
        "expected_task_revision": task.revision,
        "disposition": "continue",
        "summary": "reviewed the stop and chose a different approach",
        "continuation": {
            "objective": objective,
            "operation_class": "material",
            "target_descriptor": descriptor(identifier),
            "expected_evidence": ["movement"],
            "bounded_epoch_budget": budget,
        },
    }


def commit_input(task_id, *, failures=1):
    return {
        "version": 1,
        "task_identity_bound": True,
        "task_id": task_id,
        "apply": True,
        "authority_before": asdict(ProgressAuthority(failure_steps=failures).facts("collect")),
        "steps": [asdict(ProgressStep("note", ("work",), "same", success=False))],
        "pending_stop_reasons": [],
    }


def stopped_epoch(archive, task_id, *, epoch_id, reason, stop_reason, signature="move_to:canonical"):
    """One settled body-mutating failure epoch carrying a durable stop cause."""
    key = failure_evidence_key(signature, reason)
    return archive.store({
        "epoch_id": epoch_id,
        "run_id": f"run-{epoch_id}",
        "model_turn": 1,
        "members": [{
            "tool": "move_to", "tactic_signature": signature,
            "status": "failure", "success": False, "reason": reason,
            "body_mutating": True, "failure_evidence_key": key,
        }],
        "epistemic_keys": [key],
        "evidence_refs": [],
        "material_changed": False,
        "progress_aborted": True,
        "progress_stop_reason": stop_reason.value,
        "commit_input": commit_input(task_id),
    })


def harness(path):
    """Task workspace plus the production checkpoint_task tool over one store.

    ``cursor`` and ``stop`` stand in for what the live runtime supplies: the run's
    evidence cursor and the reviewable stop that closed its work lane.
    """
    store = RuntimeStateStore(path)
    scope = RuntimeScope("server", "world", "Bot1")
    workspace = TaskWorkspace(store, scope)
    from minebot.app.work_queue import PersistentWorkIntentQueue

    queue = PersistentWorkIntentQueue(store, scope)
    archive = PersistentProgressEpochArchive(store, scope)
    registry = ToolRegistry()
    bench = SimpleNamespace(
        store=store, scope=scope, workspace=workspace, queue=queue, archive=archive,
        cursor=0, stop=None,
        coordinator=AutonomyCoordinator(workspace, queue, archive),
        close=lambda: (queue.close(), store.close()),
    )
    register_task_tools(
        registry,
        workspace,
        body_fingerprint=lambda: {"dimension": "overworld"},
        evidence_cursor=lambda: bench.cursor,
        generation=lambda: GENERATION,
        review_stop=lambda: bench.stop,
    )
    bench.checkpoint_task = registry.get("checkpoint_task").callable
    return bench


def stop_work(bench, task_id, *, epoch_id, reason, stop_reason, signature="move_to:canonical"):
    """Settle a stop epoch and expose it the way a live stopped run would."""
    bench.cursor = bench.archive.latest_cursor()
    stopped_epoch(bench.archive, task_id, epoch_id=epoch_id, reason=reason,
                  stop_reason=stop_reason, signature=signature)
    bench.stop = ProgressAbort("stopped", stop_reason=stop_reason, stop_epoch_id=epoch_id)


def decide(bench):
    return bench.coordinator.decide(
        current_generation=GENERATION,
        body_fingerprint={"dimension": "overworld", "missing": False},
        lifecycle=LifecycleState.ACTIVE,
    )


def test_reviewable_stop_admits_one_different_approach_and_debits_the_allowance(tmp_path):
    path = tmp_path / "state.sqlite3"
    bench = harness(path)
    try:
        task = bench.workspace.start("collect wood", source="user")
        stop_work(bench, task.task_id, epoch_id="stop-1",
                  reason="no_path", stop_reason=ProgressStopReason.NO_PROGRESS)

        recorded = bench.checkpoint_task(continuation_params(
            bench.workspace.current_task,
            identifier="river bank", objective="reach the river bank instead"))
        assert recorded.success is True, recorded.metrics

        decision = decide(bench)
        assert decision.action is AutonomyAction.CONTINUE
        assert decision.reason == "reconsideration_lease_queued"
        assert decision.intent.payload["continuation_mode"] == "reconsider"
        assert bench.store.task_reconsiderations_remaining(
            bench.scope, task_id=task.task_id) == TASK_RECONSIDERATION_ALLOWANCE - 1
    finally:
        bench.close()

    # The debit is durable: a restart must not restore the spent allowance.
    reopened = harness(path)
    try:
        assert reopened.store.task_reconsiderations_remaining(
            reopened.scope, task_id=reopened.workspace.current_task.task_id,
        ) == TASK_RECONSIDERATION_ALLOWANCE - 1
    finally:
        reopened.close()


def test_repeating_or_renaming_an_attempted_approach_earns_no_grant(tmp_path):
    bench = harness(tmp_path / "state.sqlite3")
    try:
        task = bench.workspace.start("collect wood", source="user")
        bench.cursor = bench.archive.latest_cursor()
        bench.archive.store({
            "epoch_id": "seed", "run_id": "run-seed", "model_turn": 1, "members": [],
            "evidence_refs": [], "epistemic_keys": [], "material_changed": True,
            "progress_aborted": False, "commit_input": commit_input(task.task_id),
        })
        assert bench.checkpoint_task(continuation_params(
            bench.workspace.current_task,
            identifier="north ridge", objective="climb the north ridge")).success is True
        first = decide(bench)
        assert first.action is AutonomyAction.CONTINUE
        bench.queue.complete(bench.queue.lease_next())

        stop_work(bench, task.task_id, epoch_id="stop-1",
                  reason="no_path", stop_reason=ProgressStopReason.NO_PROGRESS)
        renamed = bench.checkpoint_task(continuation_params(
            bench.workspace.current_task,
            identifier="north ridge", objective="a completely new sounding plan"))

        assert renamed.success is False
        assert renamed.reason == "task_checkpoint_rejected"
        assert bench.store.task_reconsiderations_remaining(
            bench.scope, task_id=task.task_id) == TASK_RECONSIDERATION_ALLOWANCE
    finally:
        bench.close()


@pytest.mark.parametrize(
    "stop_reason", [ProgressStopReason.UNKNOWN, ProgressStopReason.BODY_TRANSPORT]
)
def test_hard_stop_may_be_deliberated_but_never_executed(tmp_path, stop_reason):
    bench = harness(tmp_path / f"{stop_reason.value}.sqlite3")
    try:
        task = bench.workspace.start("collect wood", source="user")
        stop_work(bench, task.task_id, epoch_id="stop-1",
                  reason="no_path", stop_reason=stop_reason)

        # Recording a plan is deliberation and stays allowed.
        assert bench.checkpoint_task(continuation_params(
            bench.workspace.current_task,
            identifier="river bank", objective="reach the river bank instead")).success is True

        decision = decide(bench)
        assert decision.action is AutonomyAction.YIELD
        assert decision.reason == "continuation_progress_aborted"
        assert bench.queue.pending_count() == 0
        assert bench.store.task_reconsiderations_remaining(
            bench.scope, task_id=task.task_id) == TASK_RECONSIDERATION_ALLOWANCE
    finally:
        bench.close()


def test_allowance_is_finite_and_survives_restart_without_refilling(tmp_path):
    path = tmp_path / "state.sqlite3"
    task_id = None
    for attempt in range(TASK_RECONSIDERATION_ALLOWANCE + 1):
        bench = harness(path)
        try:
            if attempt == 0:
                task_id = bench.workspace.start("collect wood", source="user").task_id
            stop_work(bench, task_id, epoch_id=f"stop-{attempt}",
                      reason="no_path",
                      signature=f"move_to:variant-{attempt}",
                      stop_reason=ProgressStopReason.NO_PROGRESS)
            recorded = bench.checkpoint_task(continuation_params(
                bench.workspace.current_task,
                identifier=f"approach {attempt}", objective=f"try approach {attempt}"))
            remaining = bench.store.task_reconsiderations_remaining(
                bench.scope, task_id=task_id)
            if attempt < TASK_RECONSIDERATION_ALLOWANCE:
                assert recorded.success is True, (attempt, recorded.metrics)
                assert decide(bench).action is AutonomyAction.CONTINUE
                assert remaining == TASK_RECONSIDERATION_ALLOWANCE - attempt - 1
                bench.queue.complete(bench.queue.lease_next())
            else:
                assert recorded.success is False, attempt
                assert remaining == 0
                assert bench.queue.pending_count() == 0
        finally:
            bench.close()


def test_granted_reconsideration_reopens_the_work_lane_in_the_successor_run(tmp_path):
    from minebot.app.runner import sdk_tool_for
    from minebot.app.session import AgentSession, SessionCommand
    from minebot.app.work_queue import MemoryWorkIntentQueue, WorkIntentKind
    from minebot.contract import ToolResult
    from tests.unit.test_agent_session import build_parts
    from tests.unit.test_progress_epochs import make_tool

    store = RuntimeStateStore(tmp_path / "state.sqlite3")
    scope = RuntimeScope("server", "world", "Bot1")
    workspace = TaskWorkspace(store, scope)
    archive = PersistentProgressEpochArchive(store, scope)
    queue = MemoryWorkIntentQueue()
    runs = []

    def factory(goal):
        parts = build_parts(goal, [], [])
        parts.runtime.progress_epoch_archive = archive
        parts.runtime.progress_task_id = lambda: workspace.current_task.task_id
        register_task_tools(
            parts.registry, workspace,
            body_fingerprint=lambda: {"dimension": "overworld"},
            evidence_cursor=parts.runtime.current_run_evidence_cursor,
            generation=parts.authority.current_generation,
            review_stop=parts.runtime.current_progress_work_stop,
        )
        parts.registry.register(make_tool(
            "blocked", lambda _: ToolResult(False, "no_path", False), mutating=True))
        parts.registry.register(make_tool(
            "detour", lambda _: ToolResult(True, "arrived", False), mutating=True))

        async def runner(_agent, _input, *, context, **_kwargs):
            phase = len(runs)
            observed = []

            async def call(name, params):
                args = json.dumps({"intent": "finish the requested body work", **params})
                call_id = f"call-{phase}-{name}-{archive.latest_cursor()}"
                await context.progress_epochs.open(SimpleNamespace(output=[SimpleNamespace(
                    type="function_call", call_id=call_id, name=name, arguments=args)]))
                return await sdk_tool_for(parts.registry.get(name)).on_invoke_tool(
                    SimpleNamespace(context=context, tool_call_id=call_id), args)

            if phase == 0:
                observed.append((await call("blocked", {}))["reason"])
                parts.authority.failure_steps = 4
                observed.append((await call("blocked", {}))["reason"])
                observed.append((await call("checkpoint_task", continuation_params(
                    workspace.current_task,
                    identifier="river bank",
                    objective="the ridge is impassable; take the river bank",
                )))["success"])
            else:
                observed.append((await call("detour", {}))["reason"])
            runs.append(observed)
            return {"ok": True}

        parts.runtime.runner_run = runner
        return parts

    session = AgentSession(
        factory,
        task_workspace=workspace,
        work_queue=queue,
        autonomy_coordinator=AutonomyCoordinator(workspace, queue, archive),
    )
    try:
        session.submit(SessionCommand.start("collect wood"))
        first = asyncio.run(session.step())
        assert first.progress_stop_reason is ProgressStopReason.NO_PROGRESS
        assert runs[0] == ["no_path", "no_path", True]
        # The model's reviewed, granted strategy is not overwritten by a yield.
        assert workspace.current_task.status.value == "running"
        assert session.work_queue.count_for_task(
            WorkIntentKind.TASK_CONTINUE, workspace.current_task.task_id) == 1
        assert store.task_reconsiderations_remaining(
            scope, task_id=workspace.current_task.task_id
        ) == TASK_RECONSIDERATION_ALLOWANCE - 1

        asyncio.run(session.step())
        # The debited grant reopens execution; it does not clear the counters.
        assert runs[1] == ["arrived"]
        assert session.parts.authority.failure_steps == 0
    finally:
        session.close()
        store.close()
