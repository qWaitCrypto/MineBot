import asyncio
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx
from agents.exceptions import MaxTurnsExceeded
from openai import APIError, RateLimitError

from minebot.app.autonomy import AutonomyCoordinator
from minebot.app.progress_epochs import PersistentProgressEpochArchive
from minebot.app.runner import AgentRuntime, RecoveryOutcome, sdk_tool_for
from minebot.app.memory import MemoryWorkspace, register_memory_tools
from minebot.app.runtime_state import (
    CheckpointDisposition,
    ContinuationContract,
    ContinuationOperationClass,
    RuntimeScope,
    RuntimeStateStore,
    TaskStatus,
)
from minebot.app.session import AgentSession, SessionCommand
from minebot.app.skills import SkillCatalog, SkillWorkspace, register_skill_tools
from minebot.app.tasks import TaskWorkspace
from minebot.app.work_queue import WorkIntentKind, WorkIntentState
from minebot.app.wiring import AgentRuntimeParts
from minebot.brain.context import AgentContext
from minebot.brain.lifecycle import LifecycleController, LifecycleState
from minebot.brain.modes import ModeRuntime
from minebot.brain.progress import ProgressAuthority
from minebot.brain.registry import RegisteredTool, ToolRegistry, ToolSidecar
from minebot.contract import BodyState, ToolResult, execution_checkpoint

from tests.unit.test_agent_runner_spine import FakeBody


def build_parts(goal: str, calls: list[str], bodies: list[FakeBody]) -> AgentRuntimeParts:
    body = FakeBody()
    bodies.append(body)

    async def fake_runner(agent, input_text, *, context=None, **kwargs):
        calls.append(f"{context.instruction_preamble}\nINPUT: {input_text}")
        return {"ok": True}

    registry = ToolRegistry()
    context = AgentContext(system_prompt="sys", goal_text=goal)
    lifecycle = LifecycleController()
    modes = ModeRuntime()
    authority = ProgressAuthority()
    runtime = AgentRuntime(
        body=body,
        registry=registry,
        agent_context=context,
        lifecycle=lifecycle,
        mode_runtime=modes,
        authority=authority,
        runner_run=fake_runner,
    )
    return AgentRuntimeParts(
        runtime=runtime,
        registry=registry,
        context=context,
        lifecycle=lifecycle,
        modes=modes,
        authority=authority,
    )


def build_skill_parts(
    goal: str,
    workspace: SkillWorkspace,
    loads: list[str],
) -> AgentRuntimeParts:
    body = FakeBody()
    registry = ToolRegistry()
    register_skill_tools(registry, workspace)
    required = {
        tool
        for name in workspace.catalog.names
        for tool in workspace.catalog.load(name).tools
    }
    for name in sorted(required):
        if name in registry:
            continue
        registry.register(
            RegisteredTool(
                name,
                f"test tool {name}",
                {"type": "object", "properties": {}, "additionalProperties": False},
                lambda _params: ToolResult(True, "ok", False),
                ToolSidecar(name, False, "test", "test", name, (), ("test",)),
            )
        )
    workspace.bind_registry(registry)

    async def fake_runner(agent, input_text, *, context=None, **kwargs):
        loaded, _activation = workspace.load("skill-authoring")
        loads.append(loaded.version)
        return {"ok": True}

    context = AgentContext(system_prompt="sys", goal_text=goal)
    lifecycle = LifecycleController()
    modes = ModeRuntime()
    authority = ProgressAuthority()
    runtime = AgentRuntime(
        body=body,
        registry=registry,
        agent_context=context,
        lifecycle=lifecycle,
        mode_runtime=modes,
        authority=authority,
        runner_run=fake_runner,
    )
    runtime.add_context_refresher(workspace.sync_context)
    workspace.sync_context(context)
    return AgentRuntimeParts(
        runtime,
        registry,
        context,
        lifecycle,
        modes,
        authority,
        skill_workspace=workspace,
    )


class AgentSessionTests(unittest.TestCase):
    def test_sdk_reflex_fact_queues_a_follow_up_turn(self):
        calls: list[str] = []
        bodies: list[FakeBody] = []
        session = AgentSession(lambda goal: build_parts(goal, calls, bodies))
        session.submit(SessionCommand.start("finish the physical goal"))
        first = asyncio.run(session.step())
        self.assertEqual(first.status, "completed_turn")

        assert session.parts is not None
        session.parts.runtime.notify_reflex_fact({
            "activity_id": "activity-sdk",
            "status": "blocked",
            "facts": {"reason": "deadline_exceeded"},
        })
        self.assertTrue(session.has_pending_work)
        follow_up = asyncio.run(session.step())
        self.assertEqual(follow_up.status, "completed_turn")
        self.assertIn("REFLEX_RESULT", calls[-1])
        session.close()

    def test_skill_activation_lifetime_is_owned_by_turn_or_durable_task(self):
        store = RuntimeStateStore(":memory:")
        scope = RuntimeScope("server", "world", "Bot1")
        task_workspace = TaskWorkspace(store, scope)
        skill_workspace = SkillWorkspace(
            store,
            scope,
            SkillCatalog(),
            task_workspace=task_workspace,
        )
        loads: list[str] = []
        session = AgentSession(
            lambda goal: build_skill_parts(goal, skill_workspace, loads),
            task_workspace=task_workspace,
        )

        session.submit(SessionCommand.message("hello"))
        chat = asyncio.run(session.step())
        chat_history = skill_workspace.activations(include_ended=True)

        self.assertEqual(chat.status, "completed_turn")
        self.assertEqual(skill_workspace.active_documents(), ())
        self.assertEqual(chat_history[0].owner_kind, "turn")
        self.assertIsNotNone(chat_history[0].ended_at)

        session.submit(SessionCommand.start("prepare tools"))
        task_turn = asyncio.run(session.step())
        task = task_workspace.current_task

        self.assertEqual(task_turn.status, "completed_turn")
        self.assertEqual(len(skill_workspace.active_documents()), 1)
        active = next(item for item in skill_workspace.activations() if item.ended_at is None)
        self.assertEqual(active.owner_kind, "task")
        self.assertEqual(active.owner_id, task.task_id)

        session.complete_current_goal()
        self.assertEqual(skill_workspace.active_documents(), ())
        self.assertEqual(len(loads), 2)
        session.close()
        store.close()

    def test_intent_remains_leased_until_the_sdk_run_finishes(self):
        bodies: list[FakeBody] = []
        observed_states: list[WorkIntentState] = []
        session: AgentSession

        def parts_factory(goal: str) -> AgentRuntimeParts:
            parts = build_parts(goal, [], bodies)

            async def runner(_agent, _input_text, **_kwargs):
                record = next(iter(session.work_queue._records.values()))
                observed_states.append(record.state)
                return {"ok": True}

            parts.runtime.runner_run = runner
            return parts

        session = AgentSession(parts_factory)
        session.submit(SessionCommand.message("hello"))

        result = asyncio.run(session.step())

        self.assertEqual(result.status, "completed_turn")
        self.assertEqual(observed_states, [WorkIntentState.LEASED])
        record = next(iter(session.work_queue._records.values()))
        self.assertEqual(record.state, WorkIntentState.COMPLETED)

    def test_checkpoint_continue_enqueues_one_successor_after_world_progress(self):
        store = RuntimeStateStore(":memory:")
        scope = RuntimeScope("server", "world", "Bot1")
        workspace = TaskWorkspace(store, scope)
        archive = PersistentProgressEpochArchive(store, scope)
        bodies: list[FakeBody] = []
        calls: list[int] = []

        def parts_factory(goal: str) -> AgentRuntimeParts:
            parts = build_parts(goal, [], bodies)

            async def runner(_agent, _input_text, **_kwargs):
                calls.append(len(calls) + 1)
                task = workspace.current_task
                if len(calls) == 1:
                    parts.runtime.body.x += 1.0
                    archive.store(
                        {
                            "epoch_id": "epoch-material",
                            "run_id": "run-material",
                            "model_turn": 1,
                            "members": [],
                            "pre_body_fingerprint": "before",
                            "post_body_fingerprint": "after",
                            "evidence_refs": [],
                            "epistemic_keys": [],
                            "material_changed": True,
                            "progress_aborted": False,
                        }
                    )
                    workspace.checkpoint(
                        expected_task_revision=task.revision,
                        disposition=CheckpointDisposition.CONTINUE,
                        summary="made progress",
                        next_step="continue the task",
                        body_fingerprint={"dimension": "overworld"},
                        continuation=ContinuationContract(
                            objective="continue preparing",
                            operation_class=ContinuationOperationClass.MATERIAL,
                            target_descriptor={"kind": "state", "identifier": "prepared"},
                            expected_evidence=("material_change",),
                            bounded_epoch_budget=4,
                            approach_key="approach:prepare",
                            evidence_cursor=0,
                            generation=parts.authority.current_generation(),
                        ),
                    )
                else:
                    workspace.checkpoint(
                        expected_task_revision=task.revision,
                        disposition=CheckpointDisposition.WAIT_EVENT,
                        summary="wait",
                    )
                return {"ok": True}

            parts.runtime.runner_run = runner
            return parts

        session = AgentSession(parts_factory, task_workspace=workspace)
        session.autonomy_coordinator = AutonomyCoordinator(
            workspace,
            session.work_queue,
            archive,
        )
        session.submit(SessionCommand.start("prepare for the End"))

        first = asyncio.run(session.step())
        self.assertEqual(first.status, "completed_turn")
        self.assertEqual(session.work_queue.pending_count(), 1)
        self.assertEqual(
            session.work_queue.count_for_task(
                WorkIntentKind.TASK_CONTINUE,
                workspace.current_task.task_id,
            ),
            1,
        )

        second = asyncio.run(session.step())
        self.assertEqual(second.status, "completed_turn")
        self.assertEqual(calls, [1, 2])
        self.assertEqual(session.work_queue.pending_count(), 0)
        self.assertEqual(workspace.current_task.status, TaskStatus.WAITING_EVENT)
        session.close()
        store.close()

    def test_pending_process_exit_does_not_replace_committed_continuation(self):
        store = RuntimeStateStore(":memory:")
        scope = RuntimeScope("server", "world", "Bot1")
        workspace = TaskWorkspace(store, scope)
        archive = PersistentProgressEpochArchive(store, scope)
        session = AgentSession(lambda goal: build_parts(goal, [], []), task_workspace=workspace)
        session.parts = build_parts("collect logs", [], [])
        session.autonomy_coordinator = AutonomyCoordinator(
            workspace,
            session.work_queue,
            archive,
        )
        task = workspace.start("collect logs", source="user")
        archive.store(
            {
                "epoch_id": "epoch-handoff-material",
                "run_id": "run-handoff-material",
                "model_turn": 1,
                "members": [],
                "pre_body_fingerprint": "before",
                "post_body_fingerprint": "after",
                "evidence_refs": [],
                "epistemic_keys": [],
                "material_changed": True,
                "progress_aborted": False,
            }
        )
        _, checkpoint = workspace.checkpoint(
            expected_task_revision=task.revision,
            disposition=CheckpointDisposition.CONTINUE,
            summary="collect another log",
            body_fingerprint={"dimension": "overworld"},
            continuation=ContinuationContract(
                objective="collect another log",
                operation_class=ContinuationOperationClass.MATERIAL,
                target_descriptor={"kind": "resource", "identifier": "oak_log"},
                expected_evidence=("inventory delta",),
                bounded_epoch_budget=2,
                approach_key="approach:oak-log",
                evidence_cursor=0,
                generation=4,
            ),
        )
        session.parts.lifecycle.ready()
        session.parts.lifecycle.start()
        session.parts.authority.invalidate_generation("process_exit")
        intent = session.work_queue.enqueue(
            WorkIntentKind.START,
            source="test",
            payload={},
            task_id=task.task_id,
        )
        session.work_queue.enqueue(WorkIntentKind.QUIT, source="test", payload={})

        session._apply_task_checkpoint_lifecycle(
            intent=intent,
            checkpoint_before=None,
        )

        latest = store.get_latest_checkpoint(task.task_id)
        self.assertEqual(latest.checkpoint_id, checkpoint.checkpoint_id)
        self.assertEqual(latest.disposition, CheckpointDisposition.CONTINUE)
        self.assertEqual(store.get_task(task.task_id).status, TaskStatus.RUNNING)
        self.assertTrue(
            store.reserve_continuation_epoch(
                scope,
                checkpoint_id=checkpoint.checkpoint_id,
                epoch_id="handoff-successor-work",
            )
        )
        self.assertTrue(
            any(
                event["event"] == "autonomy_continuation_deferred"
                and event["reason"] == "continuation_handoff_settled"
                for event in session.parts.runtime.trace.snapshot()
            )
        )
        session.close()
        store.close()

    def test_rejected_completion_keeps_only_the_immediate_continuation_budget(self):
        store = RuntimeStateStore(":memory:")
        scope = RuntimeScope("server", "world", "Bot1")
        workspace = TaskWorkspace(store, scope)
        task = workspace.start("collect 2 logs", source="user")
        _, owner = workspace.checkpoint(
            expected_task_revision=task.revision,
            disposition=CheckpointDisposition.CONTINUE,
            summary="collect another log",
            body_fingerprint={"dimension": "overworld"},
            continuation=ContinuationContract(
                objective="collect another log",
                operation_class=ContinuationOperationClass.MATERIAL,
                target_descriptor={"kind": "resource", "identifier": "logs"},
                expected_evidence=("inventory delta",),
                bounded_epoch_budget=2,
                approach_key="approach:logs",
                evidence_cursor=0,
                generation=1,
            ),
        )
        store.settle_continuation_approach(
            scope,
            checkpoint_id=owner.checkpoint_id,
            task_id=task.task_id,
            approach_key=owner.continuation.approach_key,
            budget_limit=owner.continuation.bounded_epoch_budget,
            consumed_epochs=0,
        )
        task = store.get_task(task.task_id)
        _, completion = workspace.checkpoint(
            expected_task_revision=task.revision,
            disposition=CheckpointDisposition.COMPLETE,
            summary="the logs are collected",
            evidence=["inventory was checked"],
        )
        session = AgentSession(lambda goal: build_parts(goal, [], []), task_workspace=workspace)

        review = session.reject_completion_request(
            {"satisfied": False, "inventory_count": 1, "target": {"count": 2}}
        )

        self.assertIsNotNone(review)
        self.assertEqual(review.kind, WorkIntentKind.COMPLETION_REVIEW)
        self.assertEqual(review.payload["checkpoint_id"], completion.checkpoint_id)
        self.assertEqual(review.payload["budget_checkpoint_id"], owner.checkpoint_id)
        self.assertEqual(store.get_task(task.task_id).status, TaskStatus.RUNNING)
        self.assertFalse(workspace.completion_requested)
        self.assertTrue(
            store.reserve_continuation_epoch(
                scope,
                checkpoint_id=owner.checkpoint_id,
                epoch_id="completion-review-work",
            )
        )
        session.close()
        store.close()

    def test_checkpoint_continue_without_world_progress_yields_instead_of_looping(self):
        store = RuntimeStateStore(":memory:")
        scope = RuntimeScope("server", "world", "Bot1")
        workspace = TaskWorkspace(store, scope)
        archive = PersistentProgressEpochArchive(store, scope)
        bodies: list[FakeBody] = []

        def parts_factory(goal: str) -> AgentRuntimeParts:
            parts = build_parts(goal, [], bodies)

            async def runner(_agent, _input_text, **_kwargs):
                task = workspace.current_task
                archive.store(
                    {
                        "epoch_id": "epoch-static",
                        "run_id": "run-static",
                        "model_turn": 1,
                        "members": [],
                        "pre_body_fingerprint": "same",
                        "post_body_fingerprint": "same",
                        "evidence_refs": [],
                        "epistemic_keys": [],
                        "material_changed": False,
                        "progress_aborted": False,
                    }
                )
                workspace.checkpoint(
                    expected_task_revision=task.revision,
                    disposition=CheckpointDisposition.CONTINUE,
                    summary="continue without changing the world",
                    body_fingerprint={"dimension": "overworld"},
                    continuation=ContinuationContract(
                        objective="continue preparing",
                        operation_class=ContinuationOperationClass.MATERIAL,
                        target_descriptor={"kind": "state", "identifier": "prepared"},
                        expected_evidence=("material_change",),
                        bounded_epoch_budget=4,
                        approach_key="approach:prepare",
                        evidence_cursor=0,
                        generation=parts.authority.current_generation(),
                    ),
                )
                return {"ok": True}

            parts.runtime.runner_run = runner
            return parts

        session = AgentSession(parts_factory, task_workspace=workspace)
        session.autonomy_coordinator = AutonomyCoordinator(
            workspace,
            session.work_queue,
            archive,
        )
        session.submit(SessionCommand.start("prepare for the End"))

        result = asyncio.run(session.step())

        self.assertEqual(result.status, "completed_turn")
        self.assertEqual(result.lifecycle, LifecycleState.YIELDED)
        self.assertEqual(workspace.current_task.status, TaskStatus.YIELDED)
        self.assertEqual(session.work_queue.pending_count(), 0)
        latest = store.get_latest_checkpoint(workspace.current_task.task_id)
        self.assertEqual(latest.disposition, CheckpointDisposition.YIELD)
        self.assertEqual(latest.summary, "continuation_without_qualifying_evidence")
        session.close()
        store.close()

    def test_new_body_failure_reaches_next_model_run_for_a_different_action(self):
        self._exercise_failure_reconsideration(repeat_failure=False)

    def test_repeating_the_same_body_failure_does_not_queue_another_reconsideration(self):
        self._exercise_failure_reconsideration(repeat_failure=True)

    def test_target_loss_reaches_model_but_repeated_loss_stops(self):
        for repeat in (False, True):
            with self.subTest(repeat=repeat):
                self._exercise_failure_reconsideration(repeat_failure=repeat, failure_reason="target_lost")

    def _exercise_failure_reconsideration(self, *, repeat_failure, failure_reason="no_path"):
        store = RuntimeStateStore(":memory:")
        scope = RuntimeScope("server", "world", "Bot1")
        workspace = TaskWorkspace(store, scope)
        archive = PersistentProgressEpochArchive(store, scope)
        actions = []
        calls = []

        def parts_factory(goal):
            parts = build_parts(goal, [], [])
            parts.runtime.progress_epoch_archive = archive

            def blocked(_params):
                actions.append("blocked_route")
                return ToolResult(False, failure_reason, False)

            def alternative(_params):
                actions.append("alternative_route")
                parts.runtime.body.x += 1
                return ToolResult(True, "arrived", False)

            for name, callback in (("blocked_route", blocked), ("alternative_route", alternative)):
                parts.registry.register(RegisteredTool(
                    name, name, {"type": "object", "properties": {}}, callback,
                    ToolSidecar(name, True, "body.navigation", "navigation", name, (), ("position",)),
                ))

            async def runner(_agent, _input_text, *, context, **_kwargs):
                model_input = context.instruction_preamble + "\n" + _input_text
                calls.append(model_input)
                reconsidering = "TASK_RECONSIDER" in model_input
                name = "alternative_route" if reconsidering and not repeat_failure else "blocked_route"
                call_id = f"call-{len(calls)}"
                await context.progress_epochs.open(SimpleNamespace(output=[SimpleNamespace(
                    type="function_call", call_id=call_id, name=name, arguments="{}",
                )]))
                result = await sdk_tool_for(parts.registry.get(name)).on_invoke_tool(
                    SimpleNamespace(context=context, tool_call_id=call_id), "{}",
                )
                task = workspace.current_task
                if reconsidering and not repeat_failure:
                    self.assertTrue(result["success"])
                    workspace.checkpoint(expected_task_revision=task.revision,
                        disposition=CheckpointDisposition.WAIT_EVENT, summary="arrived; wait for the next event")
                else:
                    self.assertFalse(result["success"])
                    workspace.checkpoint(expected_task_revision=task.revision,
                        disposition=CheckpointDisposition.CONTINUE, summary="route blocked; reconsider",
                        body_fingerprint={"dimension": "overworld"},
                        continuation=ContinuationContract(
                            objective="reach destination", operation_class=ContinuationOperationClass.MATERIAL,
                            target_descriptor={"kind": "state", "identifier": "arrived"},
                            expected_evidence=("arrival",), bounded_epoch_budget=4,
                            approach_key="approach:travel",
                            evidence_cursor=parts.runtime.current_run_evidence_cursor(),
                            generation=parts.authority.current_generation(),
                        ))
                return {"ok": True}

            parts.runtime.runner_run = runner
            return parts

        session = AgentSession(parts_factory, task_workspace=workspace)
        session.autonomy_coordinator = AutonomyCoordinator(workspace, session.work_queue, archive)
        try:
            session.submit(SessionCommand.start("reach destination"))
            first = asyncio.run(session.step())
            self.assertEqual(first.status, "completed_turn", first)
            self.assertEqual(session.work_queue.pending_count(), 1)
            self.assertFalse(archive.list_after(0)[0]["material_changed"])
            second = asyncio.run(session.step())
            self.assertEqual(second.status, "completed_turn", second)
            self.assertEqual(actions, ["blocked_route", "blocked_route" if repeat_failure else "alternative_route"])
            self.assertEqual(len(calls), 2)
            self.assertEqual(workspace.current_task.status,
                             TaskStatus.YIELDED if repeat_failure else TaskStatus.WAITING_EVENT)
            self.assertEqual(session.work_queue.pending_count(), 0)
        finally:
            session.close()
            store.close()

    def test_missing_checkpoint_gets_one_bounded_task_boundary_retry_then_yields(self):
        store = RuntimeStateStore(":memory:")
        scope = RuntimeScope("server", "world", "Bot1")
        workspace = TaskWorkspace(store, scope)
        calls: list[str] = []
        bodies: list[FakeBody] = []
        session = AgentSession(
            lambda goal: build_parts(goal, calls, bodies),
            task_workspace=workspace,
        )
        session.submit(SessionCommand.start("prepare for the End"))

        first = asyncio.run(session.step())
        task = workspace.current_task
        boundary = session.work_queue.queued_intents(WorkIntentKind.TASK_BOUNDARY)

        self.assertEqual(first.status, "completed_turn")
        self.assertEqual(first.lifecycle, LifecycleState.ACTIVE)
        self.assertEqual(task.status, TaskStatus.RUNNING)
        self.assertEqual(len(boundary), 1)
        self.assertEqual(boundary[0].task_id, task.task_id)
        self.assertEqual(boundary[0].payload["evidence_cursor"], 0)

        second = asyncio.run(session.step())
        checkpoint = store.get_latest_checkpoint(task.task_id)

        self.assertEqual(second.status, "completed_turn")
        self.assertEqual(second.lifecycle, LifecycleState.YIELDED)
        self.assertEqual(workspace.current_task.status, TaskStatus.YIELDED)
        self.assertEqual(session.work_queue.pending_count(), 0)
        self.assertEqual(
            checkpoint.summary,
            "model_final_without_continuation_after_boundary_retry",
        )
        self.assertIn("TASK_BOUNDARY:", calls[-1])
        session.close()
        store.close()

    def test_task_boundary_continuation_uses_origin_run_evidence_cursor(self):
        store = RuntimeStateStore(":memory:")
        scope = RuntimeScope("server", "world", "Bot1")
        workspace = TaskWorkspace(store, scope)
        archive = PersistentProgressEpochArchive(store, scope)
        bodies: list[FakeBody] = []
        calls: list[int] = []
        observed_cursors: list[int] = []

        def parts_factory(goal: str) -> AgentRuntimeParts:
            parts = build_parts(goal, [], bodies)

            async def runner(_agent, _input_text, **_kwargs):
                calls.append(len(calls) + 1)
                observed_cursors.append(parts.runtime.current_run_evidence_cursor())
                task = workspace.current_task
                if len(calls) == 1:
                    parts.runtime.body.x += 1.0
                    archive.store(
                        {
                            "epoch_id": "epoch-before-boundary",
                            "run_id": "run-before-boundary",
                            "model_turn": 1,
                            "members": [],
                            "pre_body_fingerprint": "before",
                            "post_body_fingerprint": "after",
                            "evidence_refs": [],
                            "epistemic_keys": [],
                            "material_changed": True,
                            "progress_aborted": False,
                        }
                    )
                elif len(calls) == 2:
                    workspace.checkpoint(
                        expected_task_revision=task.revision,
                        disposition=CheckpointDisposition.CONTINUE,
                        summary="resume after finite SDK boundary",
                        body_fingerprint={"dimension": "overworld"},
                        continuation=ContinuationContract(
                            objective="continue preparing",
                            operation_class=ContinuationOperationClass.MATERIAL,
                            target_descriptor={"kind": "state", "identifier": "prepared"},
                            expected_evidence=("material_change",),
                            bounded_epoch_budget=4,
                            approach_key="approach:boundary",
                            evidence_cursor=parts.runtime.current_run_evidence_cursor(),
                            generation=parts.authority.current_generation(),
                        ),
                    )
                else:
                    workspace.checkpoint(
                        expected_task_revision=task.revision,
                        disposition=CheckpointDisposition.WAIT_EVENT,
                        summary="wait",
                    )
                return {"ok": True}

            parts.runtime.runner_run = runner
            return parts

        session = AgentSession(parts_factory, task_workspace=workspace)
        session.autonomy_coordinator = AutonomyCoordinator(
            workspace,
            session.work_queue,
            archive,
        )
        session.submit(SessionCommand.start("prepare for the End"))

        asyncio.run(session.step())
        self.assertEqual(archive.latest_cursor(), 1)

        boundary_result = asyncio.run(session.step())
        task = workspace.current_task

        self.assertEqual(boundary_result.lifecycle, LifecycleState.ACTIVE)
        self.assertEqual(observed_cursors, [0, 0])
        self.assertEqual(task.status, TaskStatus.RUNNING)
        self.assertEqual(
            session.work_queue.count_for_task(
                WorkIntentKind.TASK_CONTINUE,
                task.task_id,
            ),
            1,
        )

        asyncio.run(session.step())
        self.assertEqual(workspace.current_task.status, TaskStatus.WAITING_EVENT)
        self.assertEqual(calls, [1, 2, 3])
        session.close()
        store.close()

    def test_persistent_task_is_separate_from_plain_chat_and_reaches_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RuntimeStateStore(Path(tmp) / "state.sqlite3")
            workspace = TaskWorkspace(store, RuntimeScope("server", "world", "Bot1"))
            calls: list[str] = []
            bodies: list[FakeBody] = []
            session = AgentSession(
                lambda goal: build_parts(goal, calls, bodies),
                task_workspace=workspace,
            )

            session.submit(SessionCommand.message("hello"))
            asyncio.run(session.step())
            self.assertIsNone(workspace.current_task)

            session.submit(SessionCommand.start("prepare for the End", sender="Steve"))
            asyncio.run(session.step())

            task = workspace.current_task
            self.assertIsNotNone(task)
            self.assertEqual(task.goal_text, "prepare for the End")
            self.assertEqual(task.requested_by, "Steve")
            self.assertIn("TASK_ARTIFACT:", calls[-1])
            self.assertIn(task.task_id, calls[-1])
            session.close()
            store.close()

    def test_checkpoint_wait_event_stands_down_without_completing_task(self):
        store = RuntimeStateStore(":memory:")
        workspace = TaskWorkspace(store, RuntimeScope("server", "world", "Bot1"))
        bodies: list[FakeBody] = []

        def parts_factory(goal: str) -> AgentRuntimeParts:
            parts = build_parts(goal, [], bodies)

            async def runner(_agent, _input_text, **_kwargs):
                task = workspace.current_task
                workspace.checkpoint(
                    expected_task_revision=task.revision,
                    disposition=CheckpointDisposition.WAIT_EVENT,
                    summary="Waiting for a factual event",
                    wait_for=["furnace output available"],
                )
                return {"ok": True}

            parts.runtime.runner_run = runner
            return parts

        session = AgentSession(parts_factory, task_workspace=workspace)
        session.submit(SessionCommand.start("smelt iron"))

        result = asyncio.run(session.step())

        self.assertEqual(result.status, "completed_turn")
        self.assertEqual(result.lifecycle, LifecycleState.IDLE)
        self.assertEqual(workspace.current_task.status, TaskStatus.WAITING_EVENT)
        self.assertTrue(session.has_active_goal)
        session.close()
        store.close()

    def test_cancel_transitions_persisted_task_instead_of_hiding_it(self):
        store = RuntimeStateStore(":memory:")
        workspace = TaskWorkspace(store, RuntimeScope("server", "world", "Bot1"))
        calls: list[str] = []
        bodies: list[FakeBody] = []
        session = AgentSession(
            lambda goal: build_parts(goal, calls, bodies),
            task_workspace=workspace,
        )
        session.submit(SessionCommand.start("long task"))
        asyncio.run(session.step())
        task_id = workspace.current_task.task_id

        session.submit(SessionCommand.cancel())
        asyncio.run(session.step())

        self.assertIsNone(workspace.current_task)
        self.assertEqual(store.get_task(task_id).status, TaskStatus.CANCELLED)
        session.close()
        store.close()

    def test_plain_message_is_one_agent_turn_without_creating_goal(self):
        calls: list[str] = []
        bodies: list[FakeBody] = []
        session = AgentSession(lambda goal: build_parts(goal, calls, bodies))

        session.submit(SessionCommand.message("hello", reason="chat_session_started"))
        first = asyncio.run(session.step())
        second = asyncio.run(session.step())

        self.assertEqual(first.status, "completed_turn")
        self.assertEqual(first.lifecycle, LifecycleState.IDLE)
        self.assertEqual(second.status, "idle")
        self.assertIsNone(session.current_goal)
        self.assertFalse(session.has_active_goal)
        self.assertEqual(
            [state.value for state in session.parts.lifecycle.history],
            ["init", "idle", "active", "idle"],
        )
        self.assertEqual(len(calls), 1)
        self.assertNotIn("GOAL:", calls[0])
        self.assertIn("INPUT: hello", calls[0])

    def test_replace_supersedes_queued_plain_message_before_model_admission(self):
        calls: list[str] = []
        bodies: list[FakeBody] = []
        session = AgentSession(lambda goal: build_parts(goal, calls, bodies))

        session.submit(SessionCommand.message("obsolete question"))
        session.submit(SessionCommand.replace_goal("collect 3 oak_log"))
        result = asyncio.run(session.step())

        self.assertEqual(result.status, "completed_turn")
        self.assertEqual(len(calls), 1)
        self.assertIn("GOAL: collect 3 oak_log", calls[0])
        self.assertNotIn("obsolete question", calls[0])

    def test_material_body_event_wakes_one_agent_turn_without_inventing_goal(self):
        calls: list[str] = []
        bodies: list[FakeBody] = []
        session = AgentSession(lambda goal: build_parts(goal, calls, bodies))
        session.work_queue.enqueue(
            WorkIntentKind.BODY_EVENT,
            source="body_event",
            payload={
                "event": {
                    "seq": 11,
                    "tick": 220,
                    "bot": "Bot",
                    "name": "underAttack",
                    "data": {"health": 12},
                }
            },
            dedupe_key="body:app-1:11",
        )

        result = asyncio.run(session.step())

        self.assertEqual(result.status, "completed_turn")
        self.assertEqual(result.lifecycle, LifecycleState.IDLE)
        self.assertEqual(len(calls), 1)
        self.assertIn("BODY_EVENT:", calls[0])
        self.assertIn("underAttack", calls[0])
        self.assertNotIn("GOAL:", calls[0])
        self.assertTrue(
            any(event["event"] == "body_event_wake" for event in session.parts.runtime.trace.snapshot())
        )

    def test_body_event_from_superseded_generation_is_completed_without_model_turn(self):
        calls: list[str] = []
        bodies: list[FakeBody] = []
        session = AgentSession(lambda goal: build_parts(goal, calls, bodies))
        session.submit(SessionCommand.message("hello"))
        asyncio.run(session.step())
        generation = session.parts.authority.current_generation()
        session.work_queue.enqueue(
            WorkIntentKind.BODY_EVENT,
            source="body_event",
            payload={
                "event": {
                    "seq": 12,
                    "tick": 221,
                    "bot": "Bot",
                    "name": "underAttack",
                    "data": {"health": 10},
                }
            },
            generation=generation,
        )
        session.parts.authority.invalidate_generation("replacement")

        dropped = asyncio.run(session.step())

        self.assertEqual(dropped.status, "idle")
        self.assertEqual(len(calls), 1)
        self.assertTrue(
            any(
                event["event"] == "body_event_intent_dropped"
                and event["reason"] == "runtime_generation_changed"
                for event in session.parts.runtime.trace.snapshot()
            )
        )

    def test_body_event_cannot_revive_paused_task(self):
        store = RuntimeStateStore(":memory:")
        workspace = TaskWorkspace(store, RuntimeScope("server", "world", "Bot1"))
        calls: list[str] = []
        bodies: list[FakeBody] = []
        session = AgentSession(
            lambda goal: build_parts(goal, calls, bodies),
            task_workspace=workspace,
        )
        session.submit(SessionCommand.start("collect 3 oak_log"))
        asyncio.run(session.step())
        session.submit(SessionCommand.pause("user_pause"))
        asyncio.run(session.step())
        self.assertEqual(workspace.current_task.status, TaskStatus.PAUSED)

        session.work_queue.enqueue(
            WorkIntentKind.BODY_EVENT,
            source="body_event",
            payload={
                "event": {
                    "seq": 13,
                    "tick": 222,
                    "bot": "Bot",
                    "name": "underAttack",
                    "data": {"health": 10},
                }
            },
        )
        dropped = asyncio.run(session.step())

        self.assertEqual(dropped.status, "waiting")
        self.assertEqual(len(calls), 1)
        self.assertEqual(workspace.current_task.status, TaskStatus.PAUSED)
        self.assertTrue(
            any(
                event["event"] == "body_event_intent_dropped"
                and event["reason"] == "task_not_wakeable"
                for event in session.parts.runtime.trace.snapshot()
            )
        )
        session.close()
        store.close()

    def test_plain_messages_reuse_conversation_runtime_across_turns(self):
        calls: list[str] = []
        bodies: list[FakeBody] = []
        session = AgentSession(lambda goal: build_parts(goal, calls, bodies))

        session.submit(SessionCommand.message("hello"))
        asyncio.run(session.step())
        session.submit(SessionCommand.message("who are you"))
        second = asyncio.run(session.step())

        self.assertEqual(second.lifecycle, LifecycleState.IDLE)
        self.assertEqual(len(bodies), 1)
        self.assertEqual(len(calls), 2)
        self.assertNotIn("INPUT: hello", calls[1])
        self.assertIn("INPUT: who are you", calls[1])
        self.assertIn(("user", "hello"), session.parts.context.session_messages())

    def test_two_queued_plain_messages_are_two_distinct_sdk_turns(self):
        calls: list[str] = []
        bodies: list[FakeBody] = []
        session = AgentSession(lambda goal: build_parts(goal, calls, bodies))

        session.submit(SessionCommand.message("first"))
        session.submit(SessionCommand.message("second"))

        first = asyncio.run(session.step())
        second = asyncio.run(session.step())

        self.assertEqual(first.status, "completed_turn")
        self.assertEqual(second.status, "completed_turn")
        self.assertEqual(len(calls), 2)
        self.assertIn("INPUT: first", calls[0])
        self.assertNotIn("second", calls[0])
        self.assertIn("INPUT: second", calls[1])

    def test_replayed_chat_event_dedupe_key_produces_one_sdk_turn(self):
        calls: list[str] = []
        bodies: list[FakeBody] = []
        session = AgentSession(lambda goal: build_parts(goal, calls, bodies))

        first = session.submit(
            SessionCommand.message("hello", sender="Steve"),
            dedupe_key="chat:app-1:7",
        )
        replay = session.submit(
            SessionCommand.message("hello", sender="Steve"),
            dedupe_key="chat:app-1:7",
        )
        result = asyncio.run(session.step())

        self.assertEqual(first.intent_id, replay.intent_id)
        self.assertEqual(result.status, "completed_turn")
        self.assertEqual(len(calls), 1)

    def test_cancel_supersedes_queued_replacement_instead_of_reviving_task(self):
        calls: list[str] = []
        bodies: list[FakeBody] = []
        session = AgentSession(lambda goal: build_parts(goal, calls, bodies))
        session.submit(SessionCommand.start("collect 64 logs"))
        asyncio.run(session.step())

        session.submit(SessionCommand.replace_goal("collect 64 sand"))
        session.submit(SessionCommand.cancel("stop_now"))
        cancelled = asyncio.run(session.step())
        after = asyncio.run(session.step())

        self.assertEqual(cancelled.status, "waiting")
        self.assertEqual(after.status, "idle")
        self.assertIsNone(session.current_goal)
        self.assertFalse(session.has_pending_work)

    def test_plain_minecraft_message_preserves_sender_in_input_and_trace(self):
        calls: list[str] = []
        bodies: list[FakeBody] = []
        session = AgentSession(lambda goal: build_parts(goal, calls, bodies))

        session.submit(
            SessionCommand.message(
                "hello",
                reason="chat_session_started",
                sender="Steve",
            )
        )
        completed = asyncio.run(session.step())

        self.assertEqual(completed.status, "completed_turn")
        self.assertIn('MINECRAFT_CHAT: {"message": "hello", "sender": "Steve"}', calls[0])
        self.assertIn(("user", "Steve: hello"), session.parts.context.session_messages())
        events = session.parts.runtime.trace.snapshot()
        self.assertTrue(
            any(event["event"] == "chat_message" and event["sender"] == "Steve" for event in events)
        )
        self.assertTrue(
            any(event["event"] == "user_message" and event["sender"] == "Steve" for event in events)
        )

    def test_start_on_existing_conversation_reuses_runtime_and_sdk_session(self):
        calls: list[str] = []
        bodies: list[FakeBody] = []
        session = AgentSession(lambda goal: build_parts(goal, calls, bodies))
        session.submit(SessionCommand.message("hello"))
        asyncio.run(session.step())
        runtime = session.parts.runtime
        conversation_session = runtime.conversation_session

        session.submit(SessionCommand.start("collect 3 oak_log"))
        started = asyncio.run(session.step())

        self.assertEqual(started.status, "completed_turn")
        self.assertEqual(len(bodies), 1)
        self.assertIs(session.parts.runtime, runtime)
        self.assertIs(session.parts.runtime.conversation_session, conversation_session)
        self.assertEqual(session.current_goal, "collect 3 oak_log")

    def test_replace_goal_can_start_a_fresh_session(self):
        calls: list[str] = []
        bodies: list[FakeBody] = []
        session = AgentSession(lambda goal: build_parts(goal, calls, bodies))

        session.submit(SessionCommand.replace_goal("collect 3 oak_log"))
        started = asyncio.run(session.step())

        self.assertEqual(started.status, "completed_turn")
        self.assertEqual(session.current_goal, "collect 3 oak_log")
        self.assertEqual(len(bodies), 1)

    def test_active_goal_final_output_parks_without_implicit_reprompt(self):
        calls: list[str] = []
        bodies: list[FakeBody] = []
        session = AgentSession(lambda goal: build_parts(goal, calls, bodies))

        session.submit(SessionCommand.start("collect 64 logs"))
        first = asyncio.run(session.step())
        second = asyncio.run(session.step())

        self.assertEqual(first.status, "completed_turn")
        self.assertEqual(second.status, "waiting")
        self.assertEqual(len(bodies), 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(session.current_goal, "collect 64 logs")
        self.assertEqual(session.lifecycle_state, LifecycleState.ACTIVE)
        self.assertEqual([state.value for state in session.parts.lifecycle.history], ["init", "idle", "active"])
        self.assertTrue(any(event["event"] == "user_message" and event["command"] == "start" for event in session.parts.runtime.trace.snapshot()))
        self.assertTrue(any("GOAL: collect 64 logs" in call for call in calls))

    def test_run_until_waiting_does_not_manufacture_second_turn(self):
        calls: list[str] = []
        bodies: list[FakeBody] = []
        session = AgentSession(lambda goal: build_parts(goal, calls, bodies))
        session.submit(SessionCommand.start("collect 64 logs"))

        final = asyncio.run(session.run_until_waiting(max_steps=10, should_stop=lambda _step: False))

        self.assertEqual(final.status, "completed_turn")
        self.assertEqual(len(calls), 1)

    def test_new_message_preempts_in_flight_turn_and_starts_one_fresh_turn(self):
        bodies: list[FakeBody] = []
        started = threading.Event()
        cancelled = threading.Event()
        calls: list[str] = []
        queued_at_cancellation: list[int] = []

        def parts_factory(goal: str) -> AgentRuntimeParts:
            parts = build_parts(goal, [], bodies)
            original_request_cancel = parts.runtime.request_execution_cancel

            def request_cancel(reason: str) -> int:
                queued_at_cancellation.append(
                    len(session.work_queue.queued_intents(WorkIntentKind.MESSAGE))
                )
                return original_request_cancel(reason)

            parts.runtime.request_execution_cancel = request_cancel

            async def runner(_agent, input_text, **_kwargs):
                calls.append(input_text)
                if len(calls) == 1:
                    started.set()
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        cancelled.set()
                        raise
                return {"ok": True}

            parts.runtime.runner_run = runner
            return parts

        session = AgentSession(parts_factory)
        session.submit(SessionCommand.message("hello"))

        async def scenario():
            first = asyncio.create_task(session.step())
            while not started.is_set():
                await asyncio.sleep(0.01)
            session.submit(SessionCommand.message("actually, who are you?"))
            preempted = await first
            completed = await session.step()
            return preempted, completed

        preempted, completed = asyncio.run(scenario())

        self.assertEqual(preempted.status, "preempted")
        self.assertEqual(completed.status, "completed_turn")
        self.assertTrue(cancelled.is_set())
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[1], "hello")
        self.assertEqual(calls[1], "actually, who are you?")
        self.assertIn(("user", "hello"), session.parts.context.session_messages())
        self.assertEqual(bodies[0].interrupt_reasons, [])
        self.assertEqual(queued_at_cancellation, [])

    def test_chat_control_is_queued_before_a_blocking_body_interrupt_settles(self):
        bodies: list[FakeBody] = []
        runner_started = threading.Event()
        interrupt_started = threading.Event()
        release_interrupt = threading.Event()

        class BlockingInterruptBody(FakeBody):
            def interrupt(self, reason=None):
                interrupt_started.set()
                release_interrupt.wait(timeout=1.0)
                return super().interrupt(reason)

        def parts_factory(goal: str) -> AgentRuntimeParts:
            parts = build_parts(goal, [], bodies)
            body = BlockingInterruptBody()
            bodies[-1] = body
            parts.runtime.body = body

            async def runner(_agent, _input_text, **_kwargs):
                runner_started.set()
                await asyncio.Event().wait()

            parts.runtime.runner_run = runner
            return parts

        session = AgentSession(parts_factory)
        session.submit(SessionCommand.message("hello"))

        async def scenario():
            active = asyncio.create_task(session.step())
            while not runner_started.is_set():
                await asyncio.sleep(0.01)

            submitted = threading.Event()

            def submit_quit():
                session.submit(SessionCommand.quit("chat_quit"))
                submitted.set()

            thread = threading.Thread(target=submit_quit)
            thread.start()
            thread.join(timeout=0.1)
            queued = session.work_queue.queued_intents(WorkIntentKind.QUIT)
            while not interrupt_started.is_set():
                await asyncio.sleep(0.01)
            pending_before_release = not active.done()
            release_interrupt.set()
            preempted = await active
            quit_step = await session.step()
            return submitted.is_set(), queued, pending_before_release, preempted, quit_step

        submitted, queued, pending_before_release, preempted, quit_step = asyncio.run(scenario())

        self.assertTrue(submitted)
        self.assertEqual(len(queued), 1)
        self.assertTrue(pending_before_release)
        self.assertEqual(preempted.status, "preempted")
        self.assertEqual(quit_step.status, "quit")
        self.assertEqual(bodies[0].interrupt_reasons, ["chat_quit"])

    def test_quit_after_turn_cancellation_still_releases_body_owner(self):
        bodies: list[FakeBody] = []
        runner_started = asyncio.Event()
        release_runner = asyncio.Event()

        class OwnerBody(FakeBody):
            def __init__(self):
                super().__init__()
                self.owner = "navigation-owner"

            def event_head(self, _proposed_epoch):
                return {"owner": self.owner}

            def interrupt(self, reason=None):
                self.owner = None
                return super().interrupt(reason)

        def parts_factory(goal: str) -> AgentRuntimeParts:
            body = OwnerBody()
            bodies.append(body)
            parts = build_parts(goal, [], [])
            parts.runtime.body = body

            async def runner(_agent, _input_text, **_kwargs):
                runner_started.set()
                await release_runner.wait()
                raise asyncio.CancelledError

            parts.runtime.runner_run = runner
            return parts

        session = AgentSession(parts_factory)
        session.submit(SessionCommand.start("collect 64 logs"))

        async def scenario():
            active = asyncio.create_task(session.step())
            await runner_started.wait()
            session.submit(SessionCommand.quit("gate_deadline"))
            # Let the turn surface cancellation before the supervisor's next
            # queue-notification poll, reproducing the live teardown race.
            release_runner.set()
            preempted = await active
            quit_step = await session.step()
            return preempted, quit_step

        preempted, quit_step = asyncio.run(scenario())
        trace = session.parts.runtime.trace.snapshot()
        session.close()

        self.assertEqual(preempted.status, "preempted")
        self.assertEqual(quit_step.status, "quit")
        self.assertEqual(bodies[0].interrupt_reasons, ["gate_deadline"])
        self.assertIsNone(bodies[0].owner)
        cancelled = [event for event in trace if event["event"] == "execution_cancelled"]
        self.assertEqual(len(cancelled), 1)
        self.assertTrue(cancelled[0]["settled"])

    def test_preempt_timeout_quarantines_lane_instead_of_starting_new_work(self):
        bodies: list[FakeBody] = []
        started = threading.Event()
        calls: list[str] = []

        def parts_factory(goal: str) -> AgentRuntimeParts:
            parts = build_parts(goal, [], bodies)

            async def runner(_agent, input_text, **_kwargs):
                calls.append(input_text)
                started.set()
                await asyncio.Event().wait()

            async def never_idle(*, timeout_s=30.0):
                return False

            parts.runtime.runner_run = runner
            parts.runtime.wait_for_execution_idle = never_idle
            return parts

        session = AgentSession(parts_factory)
        session.submit(SessionCommand.message("hello"))

        async def scenario():
            first = asyncio.create_task(session.step())
            while not started.is_set():
                await asyncio.sleep(0.01)
            session.submit(SessionCommand.message("new instruction"))
            preempted = await first
            quarantined = await session.step()
            return preempted, quarantined

        preempted, quarantined = asyncio.run(scenario())

        self.assertEqual(preempted.status, "preempted")
        self.assertEqual(quarantined.status, "yielded")
        self.assertEqual(quarantined.message, "execution_not_idle_after_preempt")
        self.assertEqual(len(calls), 1)
        self.assertTrue(
            any(
                event["event"] == "session_execution_quarantined"
                for event in session.parts.runtime.trace.snapshot()
            )
        )

    def test_quarantine_trace_is_emitted_once_across_rechecks(self):
        bodies: list[FakeBody] = []
        started = threading.Event()

        def parts_factory(goal: str) -> AgentRuntimeParts:
            parts = build_parts(goal, [], bodies)

            async def runner(_agent, _input_text, **_kwargs):
                started.set()
                await asyncio.Event().wait()

            async def never_idle(*, timeout_s=30.0):
                return False

            parts.runtime.runner_run = runner
            parts.runtime.wait_for_execution_idle = never_idle
            return parts

        session = AgentSession(parts_factory)
        session.submit(SessionCommand.message("hello"))

        async def scenario():
            active = asyncio.create_task(session.step())
            while not started.is_set():
                await asyncio.sleep(0.01)
            session.submit(SessionCommand.message("new instruction"))
            await active
            first = await session.step()
            second = await session.step()
            return first, second

        first, second = asyncio.run(scenario())
        traces = [
            event
            for event in session.parts.runtime.trace.snapshot()
            if event["event"] == "session_execution_quarantined"
        ]

        self.assertEqual(first.message, "execution_not_idle_after_preempt")
        self.assertEqual(second.message, "execution_not_idle_after_preempt")
        self.assertEqual(len(traces), 1)

    def test_quit_cooperatively_settles_sync_execution_and_completes_intent(self):
        bodies: list[FakeBody] = []
        started = threading.Event()

        def parts_factory(goal: str) -> AgentRuntimeParts:
            parts = build_parts(goal, [], bodies)

            def body_scan():
                started.set()
                while True:
                    execution_checkpoint()
                    time.sleep(0.002)

            async def runner(_agent, _input_text, **_kwargs):
                return await parts.runtime.run_sync(body_scan)

            parts.runtime.runner_run = runner
            return parts

        session = AgentSession(parts_factory)
        session.submit(SessionCommand.start("explore and gather materials"))

        async def scenario():
            active = asyncio.create_task(session.step())
            while not started.is_set():
                await asyncio.sleep(0.002)
            quit_intent = session.submit(
                SessionCommand.quit("30m_gate_complete"),
                dedupe_key="quit:30m_gate_complete",
            )
            preempted = await active
            quit_step = await session.step()
            idle = await session.parts.runtime.wait_for_execution_idle(timeout_s=0.25)
            return quit_intent, preempted, quit_step, idle

        quit_intent, preempted, quit_step, idle = asyncio.run(scenario())
        completed_intent = session.work_queue.get_by_dedupe("quit:30m_gate_complete")
        quarantine_traces = [
            event
            for event in session.parts.runtime.trace.snapshot()
            if event["event"] == "session_execution_quarantined"
        ]
        session.close()

        self.assertEqual(preempted.status, "preempted")
        self.assertEqual(quit_step.status, "quit")
        self.assertEqual(quit_step.lifecycle, LifecycleState.IDLE)
        self.assertTrue(idle)
        self.assertEqual(session.parts.runtime.execution_lane.active_count, 0)
        self.assertEqual(quit_intent.kind, WorkIntentKind.QUIT)
        self.assertIsNotNone(completed_intent)
        self.assertEqual(completed_intent.state, WorkIntentState.COMPLETED)
        self.assertLessEqual(len(quarantine_traces), 1)
        self.assertIn("30m_gate_complete", bodies[0].interrupt_reasons)

    def test_pause_continue_resumes_inflight_plain_turn_without_creating_goal(self):
        bodies: list[FakeBody] = []
        started = threading.Event()
        calls: list[str] = []

        def parts_factory(goal: str) -> AgentRuntimeParts:
            parts = build_parts(goal, [], bodies)

            async def runner(_agent, input_text, **_kwargs):
                calls.append(input_text)
                if len(calls) == 1:
                    started.set()
                    await asyncio.Event().wait()
                return {"ok": True}

            parts.runtime.runner_run = runner
            return parts

        session = AgentSession(parts_factory)
        session.submit(SessionCommand.message("follow me", sender="Steve"))

        async def scenario():
            first = asyncio.create_task(session.step())
            while not started.is_set():
                await asyncio.sleep(0.01)
            session.submit(SessionCommand.pause("user_pause", sender="Steve"))
            preempted = await first
            paused = await session.step()
            session.submit(SessionCommand.continue_(sender="Steve"))
            resumed = await session.step()
            return preempted, paused, resumed

        preempted, paused, resumed = asyncio.run(scenario())

        self.assertEqual(preempted.status, "preempted")
        self.assertEqual(paused.status, "stopped")
        self.assertEqual(paused.lifecycle, LifecycleState.YIELDED)
        self.assertEqual(resumed.status, "completed_turn")
        self.assertEqual(resumed.lifecycle, LifecycleState.IDLE)
        self.assertIsNone(session.current_goal)
        self.assertEqual(len(calls), 2)
        self.assertIn("HARNESS_FACT: Resume the interrupted user request represented by this JSON:", calls[1])
        self.assertIn('{"message": "follow me", "sender": "Steve"}', calls[1])

    def test_pause_while_active_interrupts_body_and_yields_lifecycle(self):
        calls: list[str] = []
        bodies: list[FakeBody] = []
        session = AgentSession(lambda goal: build_parts(goal, calls, bodies))
        session.submit(SessionCommand.start("collect 64 logs"))
        asyncio.run(session.step())

        session.submit(SessionCommand.pause("user_said_wait"))
        self.assertEqual(bodies[0].interrupt_reasons, [])
        paused = asyncio.run(session.step())

        self.assertEqual(paused.status, "stopped")
        self.assertEqual(paused.lifecycle, LifecycleState.YIELDED)
        self.assertEqual(session.lifecycle_state, LifecycleState.YIELDED)
        self.assertEqual(bodies[0].interrupt_reasons, ["user_said_wait"])
        self.assertTrue(any(event["event"] == "user_message" and event["command"] == "pause" for event in session.parts.runtime.trace.snapshot()))

    def test_continue_after_pause_resumes_existing_runtime(self):
        calls: list[str] = []
        bodies: list[FakeBody] = []
        session = AgentSession(lambda goal: build_parts(goal, calls, bodies))
        session.submit(SessionCommand.start("collect 64 logs"))
        asyncio.run(session.step())
        session.submit(SessionCommand.pause("user_pause"))
        asyncio.run(session.step())

        session.submit(SessionCommand.continue_())
        resumed = asyncio.run(session.step())

        self.assertEqual(resumed.status, "completed_turn")
        self.assertEqual(session.lifecycle_state, LifecycleState.ACTIVE)
        self.assertEqual(len(bodies), 1)
        self.assertEqual(
            [state.value for state in session.parts.lifecycle.history],
            ["init", "idle", "active", "yielded", "resuming", "active"],
        )

    def test_continue_guidance_does_not_replace_active_goal(self):
        calls: list[str] = []
        bodies: list[FakeBody] = []
        session = AgentSession(lambda goal: build_parts(goal, calls, bodies))
        session.submit(SessionCommand.start("collect 64 logs"))
        asyncio.run(session.step())
        session.submit(SessionCommand.pause("user_pause"))
        asyncio.run(session.step())

        session.submit(SessionCommand.continue_("try the other side", sender="Steve"))
        resumed = asyncio.run(session.step())

        self.assertEqual(resumed.status, "completed_turn")
        self.assertEqual(session.current_goal, "collect 64 logs")
        self.assertIn("GOAL: collect 64 logs", calls[-1])
        self.assertNotIn("GOAL: try the other side", calls[-1])
        self.assertIn('"message": "try the other side"', calls[-1])

    def test_continue_without_active_goal_returns_idle_without_model_turn(self):
        calls: list[str] = []
        bodies: list[FakeBody] = []
        session = AgentSession(lambda goal: build_parts(goal, calls, bodies))
        session.submit(SessionCommand.message("hello"))
        asyncio.run(session.step())
        session.submit(SessionCommand.pause("user_pause"))
        asyncio.run(session.step())

        session.submit(SessionCommand.continue_())
        resumed = asyncio.run(session.step())

        self.assertEqual(resumed.status, "idle")
        self.assertEqual(resumed.lifecycle, LifecycleState.IDLE)
        self.assertEqual(len(calls), 1)

    def test_replace_goal_updates_context_and_invalidates_generation_without_rebuilding(self):
        calls: list[str] = []
        bodies: list[FakeBody] = []
        session = AgentSession(lambda goal: build_parts(goal, calls, bodies))
        session.submit(SessionCommand.start("collect 64 logs"))
        asyncio.run(session.step())
        before_generation = session.parts.authority._generation

        session.submit(SessionCommand.replace_goal("collect 64 sand"))
        self.assertEqual(bodies[0].interrupt_reasons, [])
        replaced = asyncio.run(session.step())

        self.assertEqual(replaced.status, "completed_turn")
        self.assertEqual(len(bodies), 1)
        self.assertEqual(session.current_goal, "collect 64 sand")
        self.assertEqual(session.parts.runtime.weld_context.goal_text, "collect 64 sand")
        self.assertGreater(session.parts.authority._generation, before_generation)
        self.assertTrue(any("GOAL: collect 64 sand" in call for call in calls))

    def test_cancel_interrupts_and_stands_down(self):
        calls: list[str] = []
        bodies: list[FakeBody] = []
        session = AgentSession(lambda goal: build_parts(goal, calls, bodies))
        session.submit(SessionCommand.start("collect 64 logs"))
        asyncio.run(session.step())

        session.submit(SessionCommand.cancel("stop_now"))
        self.assertEqual(bodies[0].interrupt_reasons, [])
        cancelled = asyncio.run(session.step())

        self.assertEqual(cancelled.status, "waiting")
        self.assertEqual(cancelled.lifecycle, LifecycleState.IDLE)
        self.assertEqual(session.lifecycle_state, LifecycleState.IDLE)
        self.assertEqual(bodies[0].interrupt_reasons, ["stop_now"])

    def test_cancel_can_stand_down_while_resume_handoff_is_pending(self):
        calls: list[str] = []
        bodies: list[FakeBody] = []
        session = AgentSession(lambda goal: build_parts(goal, calls, bodies))
        session.submit(SessionCommand.start("collect 64 logs"))
        asyncio.run(session.step())
        session.parts.lifecycle.yield_()
        session.parts.lifecycle.resume()

        session.submit(SessionCommand.cancel("cancel_during_resume"))
        cancelled = asyncio.run(session.step())

        self.assertEqual(cancelled.status, "waiting")
        self.assertEqual(cancelled.lifecycle, LifecycleState.IDLE)
        self.assertEqual(session.lifecycle_state, LifecycleState.IDLE)
        self.assertEqual(bodies[0].interrupt_reasons, ["cancel_during_resume"])

    def test_quit_interrupts_and_returns_quit_step(self):
        calls: list[str] = []
        bodies: list[FakeBody] = []
        session = AgentSession(lambda goal: build_parts(goal, calls, bodies))
        session.submit(SessionCommand.start("collect 64 logs"))
        asyncio.run(session.step())

        session.submit(SessionCommand.quit("user_quit"))
        self.assertEqual(bodies[0].interrupt_reasons, [])
        quit_step = asyncio.run(session.step())

        self.assertEqual(quit_step.status, "quit")
        self.assertEqual(quit_step.lifecycle, LifecycleState.IDLE)
        self.assertEqual(bodies[0].interrupt_reasons, ["user_quit"])

    def test_complete_current_goal_stands_down_from_active(self):
        calls: list[str] = []
        bodies: list[FakeBody] = []
        session = AgentSession(lambda goal: build_parts(goal, calls, bodies))
        session.submit(SessionCommand.start("collect 64 logs"))
        asyncio.run(session.step())

        completed = session.complete_current_goal("terminal_truth_satisfied")

        self.assertEqual(completed.status, "completed")
        self.assertEqual(completed.lifecycle, LifecycleState.IDLE)
        self.assertEqual(session.lifecycle_state, LifecycleState.IDLE)
        self.assertIn(
            ("system", "Goal completed: collect 64 logs. Terminal reason: terminal_truth_satisfied."),
            session.parts.context.session_messages(),
        )
        self.assertTrue(
            any(event["event"] == "session_goal_completed" for event in session.parts.runtime.trace.snapshot())
        )

    def test_completed_task_enqueues_one_reflection_through_maintenance_intent(self):
        store = RuntimeStateStore(":memory:")
        scope = RuntimeScope("server", "world", "Bot1")
        workspace = TaskWorkspace(store, scope)
        calls: list[str] = []
        bodies: list[FakeBody] = []

        body_action_policies: list[bool] = []

        def parts_factory(goal: str) -> AgentRuntimeParts:
            parts = build_parts(goal, calls, bodies)
            register_memory_tools(parts.registry, MemoryWorkspace(store, scope))

            original_runner = parts.runtime.runner_run

            async def runner(agent, input_text, *, context=None, **kwargs):
                body_action_policies.append(context.body_actions_allowed)
                return await original_runner(
                    agent,
                    input_text,
                    context=context,
                    **kwargs,
                )

            parts.runtime.runner_run = runner
            return parts

        session = AgentSession(parts_factory, task_workspace=workspace)
        session.submit(SessionCommand.start("collect useful resources"))
        asyncio.run(session.step())

        completed = session.complete_current_goal("inventory_truth_satisfied")
        self.assertEqual(completed.status, "completed")
        self.assertEqual(session.work_queue.pending_count(), 1)
        queued = session.work_queue.queued_intents(WorkIntentKind.MAINTENANCE)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0].payload["action"], "reflection")

        reflected = asyncio.run(session.step())

        self.assertEqual(reflected.status, "completed_turn")
        self.assertIn("REFLECTION_MAINTENANCE", calls[-1])
        self.assertIn("do not perform Body actions", calls[-1])
        self.assertEqual(body_action_policies, [True, False])
        self.assertEqual(session.work_queue.pending_count(), 0)
        self.assertTrue(
            any(
                event["event"] == "reflection_maintenance_started"
                for event in session.parts.runtime.trace.snapshot()
            )
        )
        session.close()
        store.close()

    def test_runner_exception_is_reported_as_failed_session_step(self):
        bodies: list[FakeBody] = []

        def parts_factory(goal: str) -> AgentRuntimeParts:
            body = FakeBody()
            bodies.append(body)

            async def broken_runner(*args, **kwargs):
                raise RuntimeError("provider down")

            context = AgentContext(system_prompt="sys", goal_text=goal)
            lifecycle = LifecycleController()
            modes = ModeRuntime()
            authority = ProgressAuthority()
            runtime = AgentRuntime(
                body=body,
                registry=ToolRegistry(),
                agent_context=context,
                lifecycle=lifecycle,
                mode_runtime=modes,
                authority=authority,
                runner_run=broken_runner,
            )
            return AgentRuntimeParts(
                runtime=runtime,
                registry=runtime.registry,
                context=context,
                lifecycle=lifecycle,
                modes=modes,
                authority=authority,
            )

        session = AgentSession(parts_factory)
        session.submit(SessionCommand.start("collect 64 logs"))

        failed = asyncio.run(session.step())

        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.lifecycle, LifecycleState.IDLE)
        self.assertEqual(failed.message, "runtime_error:RuntimeError")
        self.assertTrue(any(event["event"] == "session_step_failed" for event in session.parts.runtime.trace.snapshot()))

    def test_transient_model_failure_continues_without_replaying_start_input(self):
        calls: list[str] = []

        def parts_factory(goal: str) -> AgentRuntimeParts:
            body = FakeBody()

            async def interrupted_then_healthy(agent, input_text, *, context=None, **kwargs):
                calls.append(input_text)
                if len(calls) == 1:
                    raise httpx.RemoteProtocolError(
                        "peer closed connection without sending complete message body"
                    )
                return {"ok": True}

            context = AgentContext(system_prompt="sys", goal_text=goal)
            lifecycle = LifecycleController()
            modes = ModeRuntime()
            authority = ProgressAuthority()
            runtime = AgentRuntime(
                body=body,
                registry=ToolRegistry(),
                agent_context=context,
                lifecycle=lifecycle,
                mode_runtime=modes,
                authority=authority,
                runner_run=interrupted_then_healthy,
            )
            return AgentRuntimeParts(
                runtime=runtime,
                registry=runtime.registry,
                context=context,
                lifecycle=lifecycle,
                modes=modes,
                authority=authority,
            )

        session = AgentSession(parts_factory)
        session.submit(SessionCommand.start("collect 64 logs"))

        retrying = asyncio.run(session.step())
        self.assertEqual(retrying.status, "retrying")
        self.assertTrue(session.has_pending_work)

        resumed = asyncio.run(session.step())
        self.assertEqual(resumed.status, "completed_turn")
        self.assertIn("collect 64 logs", calls[0])
        self.assertIn("RECOVERY_RECONCILE", calls[1])
        self.assertIn("Read current state, inventory, and task evidence", calls[1])
        self.assertNotIn("collect 64 logs", calls[1])
        events = session.parts.runtime.trace.snapshot()
        self.assertTrue(any(event["event"] == "model_transport_recovery_queued" for event in events))
        self.assertTrue(any(event["event"] == "model_transport_recovered" for event in events))
        self.assertFalse(any(event["event"] == "session_step_failed" for event in events))
        session.close()

    def test_transient_model_outage_keeps_session_online_and_cancelable(self):
        def parts_factory(goal: str) -> AgentRuntimeParts:
            body = FakeBody()

            async def unavailable_model(*args, **kwargs):
                raise _retryable_model_error()

            context = AgentContext(system_prompt="sys", goal_text=goal)
            lifecycle = LifecycleController()
            modes = ModeRuntime()
            authority = ProgressAuthority()
            runtime = AgentRuntime(
                body=body,
                registry=ToolRegistry(),
                agent_context=context,
                lifecycle=lifecycle,
                mode_runtime=modes,
                authority=authority,
                runner_run=unavailable_model,
            )
            return AgentRuntimeParts(
                runtime=runtime,
                registry=runtime.registry,
                context=context,
                lifecycle=lifecycle,
                modes=modes,
                authority=authority,
            )

        session = AgentSession(parts_factory, max_model_recovery_attempts=2)
        session.submit(SessionCommand.start("collect 64 logs"))

        self.assertEqual(asyncio.run(session.step()).status, "retrying")
        self.assertEqual(asyncio.run(session.step()).status, "retrying")
        exhausted = asyncio.run(session.step())

        self.assertEqual(exhausted.status, "retrying")
        self.assertTrue(session.has_pending_work)
        self.assertTrue(session.has_active_goal)
        events = session.parts.runtime.trace.snapshot()
        self.assertEqual(
            sum(event["event"] == "model_transport_recovery_queued" for event in events),
            3,
        )
        self.assertEqual(events[-1]["retry_delay_s"], 60)

        async def cancel_during_wait():
            waiting = asyncio.create_task(session.step())
            await asyncio.sleep(0.03)
            session.submit(SessionCommand.cancel())
            self.assertEqual((await asyncio.wait_for(waiting, 1)).status, "preempted")
            await session.step()
        asyncio.run(cancel_during_wait())
        self.assertFalse(session.has_active_goal)
        session.close()

    def test_insufficient_quota_fails_once_without_outer_recovery(self):
        calls = 0

        def parts_factory(goal: str) -> AgentRuntimeParts:
            body = FakeBody()

            async def quota_exhausted(*args, **kwargs):
                nonlocal calls
                calls += 1
                raise _quota_model_error()

            context = AgentContext(system_prompt="sys", goal_text=goal)
            lifecycle = LifecycleController()
            modes = ModeRuntime()
            authority = ProgressAuthority()
            runtime = AgentRuntime(
                body=body,
                registry=ToolRegistry(),
                agent_context=context,
                lifecycle=lifecycle,
                mode_runtime=modes,
                authority=authority,
                runner_run=quota_exhausted,
            )
            return AgentRuntimeParts(
                runtime=runtime,
                registry=runtime.registry,
                context=context,
                lifecycle=lifecycle,
                modes=modes,
                authority=authority,
            )

        session = AgentSession(parts_factory)
        session.submit(SessionCommand.start("collect 64 logs"))

        failed = asyncio.run(session.step())

        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.message, "runtime_error:RateLimitError")
        self.assertEqual(calls, 1)
        self.assertFalse(session.has_pending_work)
        events = session.parts.runtime.trace.snapshot()
        self.assertFalse(
            any(event["event"] == "model_transport_recovery_queued" for event in events)
        )
        session.close()

    def test_sdk_runaway_guard_yields_session_instead_of_failing(self):
        bodies: list[FakeBody] = []

        def parts_factory(goal: str) -> AgentRuntimeParts:
            body = FakeBody()
            bodies.append(body)

            async def quota_runner(*args, **kwargs):
                raise MaxTurnsExceeded("runaway guard hit")

            context = AgentContext(system_prompt="sys", goal_text=goal)
            lifecycle = LifecycleController()
            modes = ModeRuntime()
            authority = ProgressAuthority()
            runtime = AgentRuntime(
                body=body,
                registry=ToolRegistry(),
                agent_context=context,
                lifecycle=lifecycle,
                mode_runtime=modes,
                authority=authority,
                runner_run=quota_runner,
                max_turns=999,
            )
            return AgentRuntimeParts(
                runtime=runtime,
                registry=runtime.registry,
                context=context,
                lifecycle=lifecycle,
                modes=modes,
                authority=authority,
            )

        session = AgentSession(parts_factory)
        session.submit(SessionCommand.start("collect 64 logs"))

        yielded = asyncio.run(session.step())

        self.assertEqual(yielded.status, "yielded")
        self.assertEqual(yielded.lifecycle, LifecycleState.YIELDED)
        events = session.parts.runtime.trace.snapshot()
        self.assertFalse(any(event["event"] == "session_step_failed" for event in events))
        self.assertTrue(any(event["event"] == "runaway_ceiling_yielded" for event in events))

    def test_recovering_session_drives_recovery_handler_and_resumes_active(self):
        calls: list[str] = []
        recovered_calls: list[str] = []

        def parts_factory(goal: str) -> AgentRuntimeParts:
            body = FakeBody()

            async def fake_runner(agent, input_text, *, context=None, **kwargs):
                calls.append(input_text)
                return {"ok": True}

            def recover(runtime: AgentRuntime) -> RecoveryOutcome:
                recovered_calls.append(runtime.lifecycle.state.value)
                return RecoveryOutcome(True, "respawned", facts={"state_after_pos": [1, 64, 0]})

            context = AgentContext(system_prompt="sys", goal_text=goal)
            lifecycle = LifecycleController()
            modes = ModeRuntime()
            authority = ProgressAuthority()
            runtime = AgentRuntime(
                body=body,
                registry=ToolRegistry(),
                agent_context=context,
                lifecycle=lifecycle,
                mode_runtime=modes,
                authority=authority,
                runner_run=fake_runner,
                recovery_handler=recover,
            )
            return AgentRuntimeParts(
                runtime=runtime,
                registry=runtime.registry,
                context=context,
                lifecycle=lifecycle,
                modes=modes,
                authority=authority,
            )

        session = AgentSession(parts_factory)
        session.submit(SessionCommand.start("collect 64 logs"))
        first = asyncio.run(session.step())
        self.assertEqual(first.lifecycle, LifecycleState.ACTIVE)
        session.parts.lifecycle.enter_recovery()

        recovery_step = asyncio.run(session.step())
        active_step = asyncio.run(session.step())

        self.assertEqual(recovered_calls, ["recovering"])
        self.assertEqual(recovery_step.lifecycle, LifecycleState.RESUMING)
        self.assertEqual(active_step.lifecycle, LifecycleState.ACTIVE)
        self.assertEqual(len(calls), 2)
        self.assertTrue(any(event["event"] == "session_recovery_result" for event in session.parts.runtime.trace.snapshot()))

    def test_recovery_successor_reconciles_without_minting_task_continuation(self):
        store = RuntimeStateStore(":memory:")
        workspace = TaskWorkspace(store, RuntimeScope("server", "world", "Bot1"))
        workspace.start("collect 64 logs", source="user")

        def parts_factory(goal: str) -> AgentRuntimeParts:
            parts = build_parts(goal, [], [])
            parts.runtime.recovery_handler = lambda _runtime: RecoveryOutcome(
                True,
                "respawned",
            )
            return parts

        session = AgentSession(parts_factory, task_workspace=workspace)
        session.parts = parts_factory("collect 64 logs")
        session._goal_active = True
        session.parts.lifecycle.ready()
        session.parts.lifecycle.start()
        session.parts.lifecycle.enter_recovery()

        recovered = asyncio.run(session.step())
        queued = session.work_queue.queued_intents()

        self.assertEqual(recovered.lifecycle, LifecycleState.RESUMING)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0].kind, WorkIntentKind.RECOVERY_RECONCILE)
        self.assertEqual(queued[0].payload["decision"], "resume")
        self.assertEqual(queued[0].source, "recovery_completed")
        self.assertEqual(
            session.work_queue.count_for_task(
                WorkIntentKind.TASK_CONTINUE,
                workspace.current_task.task_id,
            ),
            0,
        )
        session.close()
        store.close()

    def test_recovering_session_yields_on_recovery_failure(self):
        def parts_factory(goal: str) -> AgentRuntimeParts:
            body = FakeBody()

            async def fake_runner(*args, **kwargs):
                return {"ok": True}

            def recover(_runtime: AgentRuntime) -> RecoveryOutcome:
                return RecoveryOutcome(False, "respawn_failed", facts={"reason": "spawn_refused"}, can_retry=False)

            context = AgentContext(system_prompt="sys", goal_text=goal)
            lifecycle = LifecycleController()
            modes = ModeRuntime()
            authority = ProgressAuthority()
            runtime = AgentRuntime(
                body=body,
                registry=ToolRegistry(),
                agent_context=context,
                lifecycle=lifecycle,
                mode_runtime=modes,
                authority=authority,
                runner_run=fake_runner,
                recovery_handler=recover,
            )
            return AgentRuntimeParts(
                runtime=runtime,
                registry=runtime.registry,
                context=context,
                lifecycle=lifecycle,
                modes=modes,
                authority=authority,
            )

        session = AgentSession(parts_factory)
        session.submit(SessionCommand.start("collect 64 logs"))
        asyncio.run(session.step())
        session.parts.lifecycle.enter_recovery()

        yielded = asyncio.run(session.step())

        self.assertEqual(yielded.status, "yielded")
        self.assertEqual(yielded.message, "recovery_failed:respawn_failed")
        self.assertEqual(yielded.lifecycle, LifecycleState.IDLE)

    def test_recovering_session_retries_bounded_can_retry_failures_then_resumes(self):
        attempts: list[int] = []

        def parts_factory(goal: str) -> AgentRuntimeParts:
            body = FakeBody()

            async def fake_runner(*args, **kwargs):
                return {"ok": True}

            def recover(_runtime: AgentRuntime) -> RecoveryOutcome:
                attempts.append(len(attempts) + 1)
                if len(attempts) < 3:
                    return RecoveryOutcome(False, "respawn_waiting", facts={"attempt": len(attempts)}, can_retry=True)
                return RecoveryOutcome(True, "respawned", facts={"attempt": len(attempts)}, can_retry=False)

            context = AgentContext(system_prompt="sys", goal_text=goal)
            lifecycle = LifecycleController()
            modes = ModeRuntime()
            authority = ProgressAuthority()
            runtime = AgentRuntime(
                body=body,
                registry=ToolRegistry(),
                agent_context=context,
                lifecycle=lifecycle,
                mode_runtime=modes,
                authority=authority,
                runner_run=fake_runner,
                recovery_handler=recover,
            )
            return AgentRuntimeParts(
                runtime=runtime,
                registry=runtime.registry,
                context=context,
                lifecycle=lifecycle,
                modes=modes,
                authority=authority,
            )

        session = AgentSession(parts_factory)
        session.submit(SessionCommand.start("collect 64 logs"))
        asyncio.run(session.step())
        session.parts.lifecycle.enter_recovery()

        first = asyncio.run(session.step())
        second = asyncio.run(session.step())
        third = asyncio.run(session.step())

        self.assertEqual(first.status, "recovery_retry")
        self.assertEqual(second.status, "recovery_retry")
        self.assertEqual(third.lifecycle, LifecycleState.RESUMING)
        self.assertEqual(attempts, [1, 2, 3])
        events = session.parts.runtime.trace.snapshot()
        self.assertEqual(len([event for event in events if event["event"] == "session_recovery_result"]), 3)

    def test_recovering_session_gives_up_after_retry_budget(self):
        attempts: list[int] = []

        def parts_factory(goal: str) -> AgentRuntimeParts:
            body = FakeBody()

            async def fake_runner(*args, **kwargs):
                return {"ok": True}

            def recover(_runtime: AgentRuntime) -> RecoveryOutcome:
                attempts.append(len(attempts) + 1)
                return RecoveryOutcome(False, "respawn_waiting", facts={"attempt": len(attempts)}, can_retry=True)

            context = AgentContext(system_prompt="sys", goal_text=goal)
            lifecycle = LifecycleController()
            modes = ModeRuntime()
            authority = ProgressAuthority()
            runtime = AgentRuntime(
                body=body,
                registry=ToolRegistry(),
                agent_context=context,
                lifecycle=lifecycle,
                mode_runtime=modes,
                authority=authority,
                runner_run=fake_runner,
                recovery_handler=recover,
            )
            return AgentRuntimeParts(
                runtime=runtime,
                registry=runtime.registry,
                context=context,
                lifecycle=lifecycle,
                modes=modes,
                authority=authority,
            )

        session = AgentSession(parts_factory)
        session.submit(SessionCommand.start("collect 64 logs"))
        asyncio.run(session.step())
        session.parts.lifecycle.enter_recovery()

        first = asyncio.run(session.step())
        second = asyncio.run(session.step())
        final = asyncio.run(session.step())

        self.assertEqual(first.status, "recovery_retry")
        self.assertEqual(second.status, "recovery_retry")
        self.assertEqual(final.status, "yielded")
        self.assertEqual(final.lifecycle, LifecycleState.IDLE)
        self.assertEqual(attempts, [1, 2, 3])
        self.assertTrue(any(event["event"] == "session_recovery_gave_up" for event in session.parts.runtime.trace.snapshot()))

    def test_recovery_success_with_dead_confirmation_retries_without_hot_loop(self):
        attempts: list[int] = []
        bodies: list[FakeBody] = []

        class ConfirmationBody(FakeBody):
            dead = False

            def get_state(self):
                if not self.dead:
                    return super().get_state()
                return BodyState(
                    bot=self.bot_name,
                    pos=(0.0, 0.0, 0.0),
                    yaw=None,
                    pitch=None,
                    health=0.0,
                    food=0,
                    oxygen=None,
                    inventory_raw="",
                    inventory_hash="",
                    effects=None,
                    time=1000,
                    weather=None,
                    dimension=None,
                    complete=True,
                    missing=False,
                )

        def parts_factory(goal: str) -> AgentRuntimeParts:
            body = ConfirmationBody()
            bodies.append(body)

            async def fake_runner(*args, **kwargs):
                return {"ok": True}

            def recover(_runtime: AgentRuntime) -> RecoveryOutcome:
                attempts.append(len(attempts) + 1)
                return RecoveryOutcome(True, "body_reconciled", can_retry=False)

            context = AgentContext(system_prompt="sys", goal_text=goal)
            lifecycle = LifecycleController()
            modes = ModeRuntime()
            authority = ProgressAuthority()
            runtime = AgentRuntime(
                body=body,
                registry=ToolRegistry(),
                agent_context=context,
                lifecycle=lifecycle,
                mode_runtime=modes,
                authority=authority,
                runner_run=fake_runner,
                recovery_handler=recover,
            )
            return AgentRuntimeParts(
                runtime=runtime,
                registry=runtime.registry,
                context=context,
                lifecycle=lifecycle,
                modes=modes,
                authority=authority,
            )

        session = AgentSession(parts_factory)
        session.submit(SessionCommand.start("collect 64 logs"))
        asyncio.run(session.step())
        bodies[0].dead = True
        session.parts.lifecycle.enter_recovery()

        first = asyncio.run(session.step())
        second = asyncio.run(session.step())
        final = asyncio.run(session.step())

        self.assertEqual(first.status, "recovery_retry")
        self.assertEqual(second.status, "recovery_retry")
        self.assertEqual(final.status, "yielded")
        self.assertEqual(final.lifecycle, LifecycleState.IDLE)
        self.assertEqual(attempts, [1, 2, 3])
        queued = [
            event
            for event in session.parts.runtime.trace.snapshot()
            if event["event"] == "recovery_intent_queued"
        ]
        self.assertEqual(len({event["intent_id"] for event in queued}), 3)

    def test_continue_during_recovering_does_not_bypass_recovery_driver(self):
        attempts: list[int] = []
        calls: list[str] = []

        def parts_factory(goal: str) -> AgentRuntimeParts:
            body = FakeBody()

            async def fake_runner(agent, input_text, *, context=None, **kwargs):
                calls.append(input_text)
                return {"ok": True}

            def recover(_runtime: AgentRuntime) -> RecoveryOutcome:
                attempts.append(len(attempts) + 1)
                return RecoveryOutcome(True, "respawned", facts={"attempt": len(attempts)}, can_retry=False)

            context = AgentContext(system_prompt="sys", goal_text=goal)
            lifecycle = LifecycleController()
            modes = ModeRuntime()
            authority = ProgressAuthority()
            runtime = AgentRuntime(
                body=body,
                registry=ToolRegistry(),
                agent_context=context,
                lifecycle=lifecycle,
                mode_runtime=modes,
                authority=authority,
                runner_run=fake_runner,
                recovery_handler=recover,
            )
            return AgentRuntimeParts(
                runtime=runtime,
                registry=runtime.registry,
                context=context,
                lifecycle=lifecycle,
                modes=modes,
                authority=authority,
            )

        session = AgentSession(parts_factory)
        session.submit(SessionCommand.start("collect 64 logs"))
        asyncio.run(session.step())
        session.parts.lifecycle.enter_recovery()
        session.submit(SessionCommand.continue_("继续原任务"))

        recovering_step = asyncio.run(session.step())
        active_step = asyncio.run(session.step())

        self.assertEqual(recovering_step.lifecycle, LifecycleState.RESUMING)
        self.assertEqual(active_step.lifecycle, LifecycleState.ACTIVE)
        self.assertEqual(attempts, [1])
        self.assertEqual(len(calls), 2)
        self.assertTrue(
            any(event["event"] == "session_continue_deferred_during_recovery" for event in session.parts.runtime.trace.snapshot())
        )

    def test_message_during_recovery_is_deferred_without_cancelling_recovery(self):
        bodies: list[FakeBody] = []
        calls: list[str] = []
        recovery_started = threading.Event()
        release_recovery = threading.Event()

        def parts_factory(goal: str) -> AgentRuntimeParts:
            parts = build_parts(goal, calls, bodies)

            async def recover(_runtime: AgentRuntime) -> RecoveryOutcome:
                recovery_started.set()
                await asyncio.to_thread(release_recovery.wait, 1.0)
                return RecoveryOutcome(True, "respawned", can_retry=False)

            parts.runtime.recovery_handler = recover
            return parts

        session = AgentSession(parts_factory)
        session.submit(SessionCommand.start("collect 64 logs"))
        asyncio.run(session.step())
        session.parts.lifecycle.enter_recovery()

        async def scenario():
            recovery = asyncio.create_task(session.step())
            while not recovery_started.is_set():
                await asyncio.sleep(0.01)
            message = session.submit(SessionCommand.message("请继续当前任务"))
            release_recovery.set()
            recovery_step = await recovery
            message_step = await session.step()
            return message, recovery_step, message_step

        message, recovery_step, message_step = asyncio.run(scenario())

        self.assertEqual(message.kind, WorkIntentKind.MESSAGE)
        self.assertNotEqual(recovery_step.status, "preempted")
        self.assertEqual(message_step.status, "completed_turn")
        self.assertEqual(len(calls), 2, calls)
        self.assertIn("请继续当前任务", calls[-1])
        self.assertEqual(bodies[0].interrupt_reasons, [])


def _retryable_model_error() -> APIError:
    return APIError(
        "An error occurred while processing your request. You can retry your request.",
        request=httpx.Request("POST", "https://provider.example/v1/responses"),
        body=None,
    )


def _quota_model_error() -> RateLimitError:
    body = {
        "error": {
            "message": "You exceeded your current quota",
            "type": "insufficient_quota",
            "code": "insufficient_quota",
        }
    }
    response = httpx.Response(
        429,
        request=httpx.Request("POST", "https://provider.example/v1/responses"),
        json=body,
    )
    return RateLimitError("Error code: 429", response=response, body=body)


if __name__ == "__main__":
    unittest.main()
