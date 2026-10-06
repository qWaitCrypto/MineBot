import asyncio
import copy
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from minebot.app.progress_epochs import PersistentProgressEpochArchive
from minebot.app.progress_recovery import replay_progress_epoch
from minebot.app.runtime_state import RUNTIME_SCHEMA_VERSION, RuntimeScope, RuntimeStateStore
from minebot.app.tasks import TaskWorkspace
from minebot.brain.progress import ProgressAuthority, ProgressStep
from minebot.contract import ProgressAbort, ProgressStopReason
from tests.unit.test_progress_epochs import EpochBody, make_tool, response, runtime_with


@pytest.mark.parametrize("case", ["failure", "epistemic", "duplicate", "material", "transport", "closure"])
def test_durable_commit_inputs_reproduce_authority_without_success_inference(tmp_path, case):
    path = tmp_path / "state.sqlite3"
    scope = RuntimeScope("server", "world", "Bot")
    body = EpochBody()
    authority = ProgressAuthority(
        failure_steps=4, epistemic_steps=7, last_action=("previous", ("target", 1)),
        last_fingerprint="unchanged", current_fingerprint="unchanged",
        last_epistemic_keys=("previous-evidence",), recent_events=["prior-progress"],
    )
    closure = case == "closure"
    tool = make_tool("work", lambda _: None, mutating=case == "material",
        source="agent.task" if closure else "body.perception")
    with closing(RuntimeStateStore(path)) as store:
        archive = PersistentProgressEpochArchive(store, scope)
        if case == "duplicate":
            archive.store({"epoch_id": "earlier", "run_id": "earlier-run", "model_turn": 1,
                "members": [], "epistemic_keys": ["new-evidence"]})
        runtime, adapter, _ = runtime_with(body, [tool], authority=authority, epoch_archive=archive)
        owner = ["task-at-admission"]
        runtime.progress_task_id = lambda: owner[0]
        baseline = asdict(authority.facts(runtime.agent_context.goal_text))

        async def scenario():
            await adapter.open(response(("call", "work")))
            owner[0] = "replacement-task"
            member = adapter.claim_member("work", "{}", native_tool_call_id="call")
            result = {"success": case == "material", "reason": "tested"}
            if case in {"epistemic", "duplicate"}:
                result["evidence_keys"] = ["new-evidence"]
            if case == "material":
                body.x = 1.0
            return adapter.settle(member, result=result, model_result=result,
                progress_steps=() if closure else (
                    ProgressStep("observe", ("inner", 1), "intermediate"),
                    ProgressStep("note", ("work", ("target", 2)), "unchanged", success=False),
                ),
                post_state=body.get_state(),
                pending_abort=(ProgressAbort("transport", stop_reason=ProgressStopReason.BODY_TRANSPORT)
                    if case == "transport" else None))

        try:
            abort = asyncio.run(scenario())
            final_facts = authority.facts(runtime.agent_context.goal_text)
        finally:
            runtime.close()

    with closing(RuntimeStateStore(path)) as reopened:
        record = reopened.list_progress_epochs_after(scope, cursor=0)[-1]
    commit = record["commit_input"]
    assert commit["version"] == 1
    assert commit["task_identity_bound"] is True
    assert commit["task_id"] == "task-at-admission"
    assert commit["apply"] is not closure
    if case == "epistemic":
        assert record["novel_epistemic_keys"] == ["new-evidence"]
        assert abort.stop_reason is ProgressStopReason.EPISTEMIC_BUDGET
    if case == "duplicate":
        assert record["epistemic_keys"] == ["new-evidence"]
        assert record["novel_epistemic_keys"] == []
        assert abort.stop_reason is ProgressStopReason.NO_PROGRESS
    if case == "material":
        assert record["material_changed"] is True
        assert final_facts.failure_steps == final_facts.epistemic_steps == 0
    assert len(commit["steps"]) == (0 if closure else 1)
    assert commit["authority_before"] == json.loads(json.dumps(baseline))
    restored_facts, replay_abort = replay_progress_epoch(record)
    assert restored_facts == final_facts
    if case == "transport":
        assert commit["pending_stop_reasons"] == ["body_transport"]
        assert abort.stop_reason is ProgressStopReason.BODY_TRANSPORT
        assert replay_abort.stop_reason is ProgressStopReason.BODY_TRANSPORT
    else:
        assert commit["pending_stop_reasons"] == []
        assert (None if replay_abort is None else replay_abort.stop_reason) == (
            None if abort is None else abort.stop_reason)


def test_v18_migration_keeps_legacy_commit_inputs_unknown(tmp_path):
    path = tmp_path / "state.sqlite3"
    scope = RuntimeScope("server", "world", "Bot")
    with closing(RuntimeStateStore(path)) as store:
        store.create_progress_epoch(scope, record={
            "epoch_id": "legacy", "run_id": "legacy-run", "model_turn": 1,
            "members": [{"success": False, "progress_step_count": 7}],
        })
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE progress_epochs DROP COLUMN commit_input_json")
        connection.execute("UPDATE minebot_schema SET version = 18")
    with closing(RuntimeStateStore(path)) as store:
        record = store.get_progress_epoch(scope, "legacy")
        assert store.schema_version == RUNTIME_SCHEMA_VERSION
        assert record["commit_input"] is None
        assert record["members"][0]["progress_step_count"] == 7


def test_production_task_binding_follows_workspace_without_freezing_initial_task(tmp_path):
    from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_agent_runtime
    from minebot.app.tasks import TaskWorkspace
    from minebot.contract import Region
    from tests.unit.test_agent_real_server_entrypoint import HarnessBody

    with closing(RuntimeStateStore(tmp_path / "state.sqlite3")) as store:
        workspace = TaskWorkspace(store, RuntimeScope("server", "world", "Bot"))
        parts = build_phase1_agent_runtime(body=HarnessBody(), goal_text="wait",
            model_provider=None, config=Phase1RuntimeConfig(
                natural_region=Region("test", (0, 0, 0), (16, 128, 16)),
                task_workspace=workspace))
        try:
            assert parts.runtime.progress_task_id() is None
            first = workspace.start("collect wood", source="test")
            assert parts.runtime.progress_task_id() == first.task_id
            second = workspace.replace("collect stone", source="test")
            assert second.task_id != first.task_id
            assert parts.runtime.progress_task_id() == second.task_id
        finally:
            parts.runtime.close()


def _record(task_id, *, failures=1):
    return {
        "epoch_id": "seed", "run_id": "seed-run", "model_turn": 1, "members": [],
        "material_changed": False, "progress_aborted": False,
        "novel_epistemic_keys": [],
        "commit_input": {
            "version": 1, "task_identity_bound": True, "task_id": task_id, "apply": True,
            "authority_before": asdict(ProgressAuthority(failure_steps=failures).facts("collect")),
            "steps": [asdict(ProgressStep("note", ("work",), "same", success=False))],
            "pending_stop_reasons": [],
        },
    }


def _runtime_for_workspace(workspace, archive, runner):
    body = EpochBody()
    body.poll_events = lambda: []
    tool = make_tool("work", lambda _: None, mutating=False, source="body.perception")
    runtime, _, _ = runtime_with(body, [tool], epoch_archive=archive)
    runtime.progress_task_id = lambda: workspace.current_task.task_id
    runtime.runner_run = runner
    return runtime


def test_run_turn_restores_before_model_and_only_new_task_identity_resets(tmp_path):
    path = tmp_path / "state.sqlite3"
    scope = RuntimeScope("server", "world", "Bot")
    observed = []

    async def model(_agent, _text, *, context, **_kwargs):
        observed.append(context.runtime.authority.failure_steps)
        adapter = context.progress_epochs
        await adapter.open(response(("call", "work")))
        member = adapter.claim_member("work", "{}", native_tool_call_id="call")
        result = {"success": False, "reason": "fixture"}
        abort = adapter.settle(member, result=result, model_result=result,
            progress_steps=(ProgressStep("note", ("work",), "same", success=False),))
        if abort is not None:
            raise abort
        return SimpleNamespace(final_output="recorded", new_items=[])

    with closing(RuntimeStateStore(path)) as store:
        workspace = TaskWorkspace(store, scope)
        task = workspace.start("collect", source="user")
        store.create_progress_epoch(scope, record=_record(task.task_id))
        runtime = _runtime_for_workspace(workspace, PersistentProgressEpochArchive(store, scope), model)
        runtime.authority.next_generation()
        try:
            assert asyncio.run(runtime.run_turn()).status == "completed_turn"
            assert observed == [2]
            assert runtime.authority.failure_steps == 3
            assert runtime.authority.current_generation() == 1
        finally:
            runtime.close()
    with closing(RuntimeStateStore(path)) as store:
        workspace = TaskWorkspace(store, scope)
        runtime = _runtime_for_workspace(workspace, PersistentProgressEpochArchive(store, scope), model)
        try:
            assert asyncio.run(runtime.run_turn()).status == "completed_turn"
            assert observed == [2, 3]
            assert asyncio.run(runtime.run_turn()).progress_stop_reason is ProgressStopReason.NO_PROGRESS
            assert observed == [2, 3, 4]
            assert runtime.authority.failure_steps == 5
            replacement = workspace.replace("collect", source="user")
            assert replacement.task_id != task.task_id
            runtime.lifecycle.resume()
            assert asyncio.run(runtime.run_turn()).status == "completed_turn"
            assert observed == [2, 3, 4, 0]
            runtime.agent_context.set_goal("reworded collect goal")
            assert asyncio.run(runtime.run_turn()).status == "completed_turn"
            assert observed == [2, 3, 4, 0, 1]
            assert runtime.authority.failure_steps == 2
        finally:
            runtime.close()


@pytest.mark.parametrize("kind", [
    "legacy", "missing_owner", "corrupt", "unknown", "body_transport",
    "pending_unknown", "pending_body_transport",
])
def test_unrestorable_or_infrastructure_stop_prevents_model_admission(tmp_path, kind):
    scope = RuntimeScope("server", "world", "Bot")
    calls = []

    async def model(*_args, **_kwargs):
        calls.append(True)
        raise AssertionError("model must not be admitted")

    with closing(RuntimeStateStore(tmp_path / "state.sqlite3")) as store:
        workspace = TaskWorkspace(store, scope)
        task = workspace.start("collect", source="user")
        record = _record(task.task_id)
        if kind == "legacy":
            record.pop("commit_input")
        elif kind == "missing_owner":
            record["commit_input"].pop("task_id")
        elif kind == "corrupt":
            record["commit_input"]["authority_before"]["failure_steps"] = True
        elif kind.startswith("pending_"):
            record["commit_input"]["pending_stop_reasons"] = [kind.removeprefix("pending_")]
        else:
            record["progress_aborted"] = True
            record["progress_stop_reason"] = kind
        store.create_progress_epoch(scope, record=record)
        runtime = _runtime_for_workspace(workspace, PersistentProgressEpochArchive(store, scope), model)
        try:
            outcome = asyncio.run(runtime.run_turn())
            assert outcome.status == "yielded"
            assert outcome.progress_stop_reason.value == (
                "body_transport" if kind.endswith("body_transport") else "unknown")
            assert calls == []
            assert runtime.restore_task_progress().stop_reason is outcome.progress_stop_reason
        finally:
            runtime.close()


@pytest.mark.parametrize("path,value", [
    (("version",), True),
    (("apply",), None),
    (("authority_before", "failure_steps"), -1),
    (("authority_before", "failure_steps"), True),
    (("authority_before", "last_action"), {"invalid": "key"}),
    (("authority_before", "epistemic_steps"), "3"),
    (("steps",), [{"kind": "note"}]),
    (("pending_stop_reasons",), ["invented"]),
])
def test_corrupt_replay_inputs_are_rejected(path, value):
    record = copy.deepcopy(_record("task"))
    target = record["commit_input"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError):
        replay_progress_epoch(record)


def test_owned_epoch_is_not_lost_on_wall_clock_regression_and_scope_is_checked(tmp_path):
    from minebot.app.runtime_state import RuntimeStateConflict

    path = tmp_path / "state.sqlite3"
    scope = RuntimeScope("server", "world", "Bot")
    with closing(RuntimeStateStore(path)) as store:
        task = TaskWorkspace(store, scope).start("collect", source="user")
        store.create_progress_epoch(scope, record=_record(task.task_id))
        with sqlite3.connect(path) as connection:
            connection.execute("UPDATE progress_epochs SET settled_at = '2000-01-01T00:00:00.000+00:00'")
        authority = ProgressAuthority()
        assert PersistentProgressEpochArchive(store, scope).restore_authority(authority, task_id=task.task_id) is None
        assert authority.failure_steps == 2
        with pytest.raises(RuntimeStateConflict):
            PersistentProgressEpochArchive(store, RuntimeScope("other", "world", "Bot")).restore_authority(
                authority, task_id=task.task_id)
        assert authority.failure_steps == 2


def test_restoration_does_not_replace_generation_or_active_capture():
    authority = ProgressAuthority(failure_steps=4)
    authority.next_generation()
    facts, _ = replay_progress_epoch(_record("task"))
    with authority.capture_steps():
        with pytest.raises(RuntimeError):
            authority.restore_facts(facts)
        assert authority.failure_steps == 4
    authority.restore_facts(facts)
    assert authority.failure_steps == 2
    assert authority.current_generation() == 1


def _recovering_session(workspace, store, handler):
    from minebot.app.session import AgentSession
    from minebot.app.work_queue import PersistentWorkIntentQueue
    from tests.unit.test_agent_session import build_parts

    def factory(goal):
        parts = build_parts(goal, [], [])
        parts.runtime.progress_epoch_archive = PersistentProgressEpochArchive(store, workspace.scope)
        parts.runtime.progress_task_id = lambda: workspace.current_task.task_id
        parts.runtime.recovery_handler = handler
        return parts

    session = AgentSession(factory, task_workspace=workspace,
        work_queue=PersistentWorkIntentQueue(store, workspace.scope))
    session.parts = factory(workspace.current_task.goal_text)
    session._goal_active = True
    session.parts.lifecycle.ready()
    session.parts.lifecycle.start()
    session.parts.lifecycle.enter_recovery()
    session._queue_recovery_if_required()
    return session


def test_physical_recovery_restores_and_binds_admission_before_driver(tmp_path):
    from minebot.app.runner import RecoveryOutcome
    from minebot.brain.lifecycle import LifecycleState
    from tests.unit.test_continuation_budget import seed

    with closing(RuntimeStateStore(tmp_path / "state.sqlite3")) as store:
        scope = RuntimeScope("server", "world", "Bot")
        workspace, checkpoint = seed(store, scope)
        store.create_progress_epoch(scope, record=_record(workspace.current_task.task_id))
        observed = []

        def recover(runtime):
            intent = store.get_work_intent(intent_id)
            observed.append((runtime.authority.failure_steps, dict(intent["payload"])))
            runtime.authority.note_step(("recovery-attempt",), False, "recovery-state")
            return RecoveryOutcome(True, "recovered")

        session = _recovering_session(workspace, store, recover)
        intent_id = session.work_queue.queued_intents()[0].intent_id
        confirmations = []
        original_run_turn = session.parts.runtime.run_turn

        async def confirm(*args, **kwargs):
            confirmations.append(kwargs)
            return await original_run_turn(*args, **kwargs)

        session.parts.runtime.run_turn = confirm
        try:
            outcome = asyncio.run(session.step())
            assert outcome.lifecycle is LifecycleState.RESUMING
            assert observed[0][0] == 2
            assert observed[0][1]["budget_checkpoint_id"] == checkpoint.checkpoint_id
            assert observed[0][1]["admission_task_id"] == workspace.current_task.task_id
            assert observed[0][1]["admission_evidence_cursor"] == 1
            assert confirmations[0]["continuation_budget"].checkpoint_id == checkpoint.checkpoint_id
            assert confirmations[0]["continuation_evidence_cursor"] == 1
            assert session.parts.authority.failure_steps == 3
            assert asyncio.run(session.step()).status == "completed_turn"
            assert session.parts.authority.failure_steps == 3
        finally:
            session.close()


@pytest.mark.parametrize("cause", list(ProgressStopReason))
def test_recovery_confirmation_preserves_typed_progress_stop(tmp_path, cause, monkeypatch):
    from minebot.app.runner import AgentTurnOutcome, RecoveryOutcome
    from minebot.brain.lifecycle import LifecycleState
    from minebot.app import runtime_state

    now = runtime_state._utc_now()
    monkeypatch.setattr(runtime_state, "_utc_now", lambda: now)

    with closing(RuntimeStateStore(tmp_path / "state.sqlite3")) as store:
        workspace = TaskWorkspace(store, RuntimeScope("server", "world", "Bot"))
        task = workspace.start("collect", source="user")
        store.create_progress_epoch(workspace.scope, record=_record(task.task_id))
        session = _recovering_session(workspace, store, lambda _: RecoveryOutcome(True, "recovered"))

        async def confirm(**_kwargs):
            session.parts.lifecycle.stand_down()
            return AgentTurnOutcome("yielded", LifecycleState.IDLE,
                session.parts.modes.profile_for(LifecycleState.IDLE), progress_stop_reason=cause)

        session.parts.runtime.run_turn = confirm
        try:
            outcome = asyncio.run(session.step())
            assert outcome.progress_stop_reason is cause
        finally:
            session.close()


@pytest.mark.parametrize("cause", ["legacy", "unknown", "body_transport"])
def test_unrestorable_progress_stops_before_physical_recovery_driver(tmp_path, cause):
    from minebot.app.runner import RecoveryOutcome

    with closing(RuntimeStateStore(tmp_path / "state.sqlite3")) as store:
        scope = RuntimeScope("server", "world", "Bot")
        workspace = TaskWorkspace(store, scope)
        task = workspace.start("collect", source="user")
        record = _record(task.task_id)
        if cause == "legacy":
            record.pop("commit_input")
        else:
            record["progress_aborted"] = True
            record["progress_stop_reason"] = cause
        store.create_progress_epoch(scope, record=record)
        calls = []

        def recover(_runtime):
            calls.append(True)
            return RecoveryOutcome(True, "must not execute")

        session = _recovering_session(workspace, store, recover)
        try:
            outcome = asyncio.run(session.step())
            assert calls == []
            assert outcome.status == "yielded"
            assert outcome.progress_stop_reason.value == ("unknown" if cause == "legacy" else cause)
            assert session._recovery_attempts == 0
        finally:
            session.close()
