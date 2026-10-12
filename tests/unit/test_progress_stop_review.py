import asyncio
import json
from contextlib import closing
from types import SimpleNamespace

import pytest

from minebot.app.runner import sdk_tool_for
from minebot.contract import ProgressAbort, ProgressStopReason, ToolResult
from tests.unit.test_progress_epochs import (
    EpochBody, RecordingEpochArchive, invoke, make_tool, response, runtime_with,
)
from minebot.brain.progress import ProgressAuthority


def test_soft_stop_returns_final_feedback_then_allows_only_task_closure():
    effects = []
    blocked = make_tool("blocked", lambda _: ToolResult(False, "no_path", False), mutating=True)
    other = make_tool("other", lambda _: effects.append("body") or ToolResult(True, "done", False), mutating=True)
    memory = make_tool("write_memory", lambda _: effects.append("memory") or ToolResult(True, "saved", False),
        mutating=False, source="agent.memory")
    checkpoint = make_tool("checkpoint_task", lambda _: effects.append("checkpoint") or ToolResult(True, "saved", False),
        mutating=False, source="agent.task")
    archive = RecordingEpochArchive()
    runtime, adapter, context = runtime_with(EpochBody(), [blocked, other, memory, checkpoint],
        authority=ProgressAuthority(failure_steps=4), epoch_archive=archive)
    reservations = []
    adapter.budget = SimpleNamespace(checkpoint_id="incoming",
        reserve=lambda epoch_id: reservations.append(epoch_id) or True)

    async def scenario():
        await adapter.open(response(("seed", "blocked")))
        assert (await invoke(blocked, context, "seed"))["reason"] == "no_path"
        runtime.authority.failure_steps = 4
        await adapter.open(response(("failed", "blocked")))
        result = await invoke(blocked, context, "failed")
        assert result["reason"] == "no_path"
        assert result["executionStop"]["reason"] == "no_progress"
        for tool in (other, memory):
            await adapter.open(response((tool.name, tool.name)))
            assert (await invoke(tool, context, tool.name))["reason"] == "progress_work_closed"
        await adapter.open(response(("close", "checkpoint_task")))
        assert (await invoke(checkpoint, context, "close"))["success"] is True

    try:
        asyncio.run(scenario())
        assert effects == ["checkpoint"]
        assert runtime.authority.failure_steps == 5
        assert archive.records[1]["progress_stop_reason"] == "no_progress"
        assert len(archive.records) == 5
        assert len(reservations) == 2
    finally:
        runtime.close()


@pytest.mark.parametrize("reason", [ProgressStopReason.UNKNOWN, ProgressStopReason.BODY_TRANSPORT])
def test_hard_stop_never_becomes_review(reason):
    def stop(_params):
        raise ProgressAbort("hard", stop_reason=reason)

    blocked = make_tool("blocked", stop, mutating=True)
    checkpoint = make_tool("checkpoint_task", lambda _: ToolResult(True, "saved", False),
        mutating=False, source="agent.task")
    runtime, adapter, context = runtime_with(EpochBody(), [blocked, checkpoint],
        authority=ProgressAuthority(failure_steps=4), epoch_archive=RecordingEpochArchive())

    async def scenario():
        await adapter.open(response(("failed", "blocked")))
        with pytest.raises(ProgressAbort) as stopped:
            await invoke(blocked, context, "failed")
        assert stopped.value.stop_reason is reason

    try:
        asyncio.run(scenario())
    finally:
        runtime.close()


@pytest.mark.parametrize("reason", ["safety_denied", "unclassified_failure"])
def test_unsafe_or_unclassified_failure_does_not_gain_review(reason):
    blocked = make_tool("blocked", lambda _: ToolResult(False, reason, False), mutating=True)
    checkpoint = make_tool("checkpoint_task", lambda _: ToolResult(True, "saved", False),
        mutating=False, source="agent.task")
    runtime, adapter, context = runtime_with(EpochBody(), [blocked, checkpoint],
        authority=ProgressAuthority(failure_steps=4), epoch_archive=RecordingEpochArchive())

    async def scenario():
        await adapter.open(response(("failed", "blocked")))
        with pytest.raises(ProgressAbort):
            await invoke(blocked, context, "failed")
        assert adapter.reviewable_abort is None

    try:
        asyncio.run(scenario())
    finally:
        runtime.close()


@pytest.mark.parametrize("closure", ["wait_event", "omitted", "continue", "ceiling"])
def test_session_reviews_within_the_same_run_and_never_queues_more_work(closure):
    from agents.exceptions import MaxTurnsExceeded
    from minebot.app.progress_epochs import PersistentProgressEpochArchive
    from minebot.app.runtime_state import RuntimeScope, RuntimeStateStore
    from minebot.app.session import AgentSession, SessionCommand
    from minebot.app.tasks import TaskWorkspace, register_task_tools
    from tests.unit.test_agent_session import build_parts

    with closing(RuntimeStateStore(":memory:")) as store:
        scope = RuntimeScope("server", "world", "Bot1")
        workspace = TaskWorkspace(store, scope)
        archive = PersistentProgressEpochArchive(store, scope)
        phases = []

        def factory(goal):
            parts = build_parts(goal, [], [])
            parts.runtime.progress_epoch_archive = archive
            parts.runtime.progress_task_id = lambda: workspace.current_task.task_id
            register_task_tools(parts.registry, workspace)
            parts.registry.register(make_tool("blocked", lambda _: ToolResult(False, "no_path", False), mutating=True))

            async def runner(_agent, _input, *, context, **_kwargs):
                phases.append("run")

                async def call(name, params):
                    args = json.dumps({"intent": "finish the requested body work", **params})
                    call_id = f"call-{archive.latest_cursor()}"
                    await context.progress_epochs.open(SimpleNamespace(output=[SimpleNamespace(
                        type="function_call", call_id=call_id, name=name, arguments=args)]))
                    return await sdk_tool_for(parts.registry.get(name)).on_invoke_tool(
                        SimpleNamespace(context=context, tool_call_id=call_id), args)

                assert (await call("blocked", {}))["reason"] == "no_path"
                parts.authority.failure_steps = 4
                assert (await call("blocked", {}))["reason"] == "no_path"
                phases.append("review")
                assert (await call("blocked", {}))["reason"] == "progress_work_closed"
                if closure == "ceiling":
                    raise MaxTurnsExceeded("fixture existing ceiling")
                if closure != "omitted":
                    params = {
                        "expected_task_revision": workspace.current_task.revision,
                        "disposition": closure, "summary": "Need a different route; wait for new evidence",
                    }
                    if closure == "continue":
                        params["continuation"] = {
                            "objective": "try another route", "operation_class": "material",
                            "target_descriptor": {"kind": "state", "identifier": "another-route", "traits": []},
                            "expected_evidence": ["movement"], "bounded_epoch_budget": 3,
                        }
                    assert (await call("checkpoint_task", params))["success"] is True
                return {"ok": True}

            parts.runtime.runner_run = runner
            return parts

        session = AgentSession(factory, task_workspace=workspace)
        try:
            session.submit(SessionCommand.start("collect wood"))
            step = asyncio.run(session.step())
            assert phases == ["run", "review"]
            assert step.progress_stop_reason is ProgressStopReason.NO_PROGRESS
            assert workspace.current_task.status.value == ("waiting_event" if closure == "wait_event" else "yielded")
            assert session.work_queue.pending_count() == 0
            assert session.parts.authority.failure_steps == 5
            restored = ProgressAuthority()
            assert archive.restore_authority(restored, task_id=workspace.current_task.task_id).stop_reason is ProgressStopReason.NO_PROGRESS
            assert restored.failure_steps == 5
        finally:
            session.close()

        if closure == "wait_event":
            resumed = []

            def replay_factory(goal):
                parts = factory(goal)

                async def replay_runner(_agent, _input, *, context, **_kwargs):
                    await context.progress_epochs.open(response(("after-restart", "blocked")))
                    result = await invoke(parts.registry.get("blocked"), context, "after-restart")
                    resumed.append(result["reason"])
                    return {"ok": True}

                parts.runtime.runner_run = replay_runner
                return parts

            rebuilt = AgentSession(replay_factory, task_workspace=workspace)
            try:
                rebuilt.submit(SessionCommand.continue_())
                step = asyncio.run(rebuilt.step())
                assert resumed == ["progress_work_closed"]
                assert step.progress_stop_reason is ProgressStopReason.NO_PROGRESS
                assert rebuilt.parts.authority.failure_steps == 5
                assert workspace.current_task.status.value == "yielded"
                assert rebuilt.work_queue.pending_count() == 0
            finally:
                rebuilt.close()


def test_actual_sdk_gets_stop_feedback_and_finishes_without_more_work():
    from agents import Agent, RunConfig, Runner
    from agents.items import ModelResponse
    from agents.models.interface import Model
    from agents.usage import Usage
    from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText
    from minebot.app.runner import RuntimeHooks

    effects = []
    closed = []

    class ReviewModel(Model):
        calls = 0

        async def get_response(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 5:
                assert "executionStop" in str(kwargs.get("input", args))
            if self.calls == 6:
                assert "progress_work_closed" in str(kwargs.get("input", args))
            names = (["blocked"] if self.calls <= 4 else
                ["other", "write_memory"] if self.calls == 5 else
                ["checkpoint_task"] if self.calls == 6 else [])
            output = [ResponseFunctionToolCall(arguments='{"intent":"finish the requested body work"}', call_id=f"{self.calls}-{name}",
                name=name, type="function_call", status="completed") for name in names]
            if not output:
                output = [ResponseOutputMessage(id="final", content=[ResponseOutputText(
                    annotations=[], text="Waiting for new route evidence.", type="output_text")],
                    role="assistant", status="completed", type="message")]
            return ModelResponse(output=output, usage=Usage(), response_id=f"response-{self.calls}")

        async def stream_response(self, *args, **kwargs):
            if False:
                yield None

    tools = [
        make_tool("blocked", lambda _: effects.append("blocked") or ToolResult(False, "no_path", False), mutating=True),
        make_tool("other", lambda _: effects.append("other") or ToolResult(True, "arrived", False), mutating=True),
        make_tool("write_memory", lambda _: effects.append("memory") or ToolResult(True, "saved", False),
            mutating=False, source="agent.memory"),
        make_tool("checkpoint_task", lambda _: closed.append(True) or ToolResult(True, "saved", False),
            mutating=False, source="agent.task"),
    ]
    archive = RecordingEpochArchive()
    runtime, _, context = runtime_with(EpochBody(), tools, epoch_archive=archive)
    model = ReviewModel()
    agent = Agent(name="StopReviewProbe", instructions="Use the tools.", model=model,
        tools=[sdk_tool_for(tool) for tool in tools])
    try:
        result = asyncio.run(Runner.run(agent, "collect", context=context, hooks=RuntimeHooks(),
            max_turns=7, run_config=RunConfig(tracing_disabled=True)))
        assert result.final_output == "Waiting for new route evidence."
        assert effects == ["blocked"] * 4
        assert closed == [True]
        assert model.calls == 7
        assert all(record["progress_stop_reason"] == "no_progress" for record in archive.records[3:])
    finally:
        runtime.close()


def test_epistemic_limit_closes_observation_but_not_checkpoint():
    observed = []
    reader = make_tool("read", lambda _: observed.append(True) or ToolResult(True, "read", False,
        metrics={"evidence_keys": ["new-observation"]}), mutating=False, source="body.perception")
    checkpoint = make_tool("checkpoint_task", lambda _: ToolResult(True, "saved", False),
        mutating=False, source="agent.task")
    runtime, adapter, context = runtime_with(EpochBody(), [reader, checkpoint],
        authority=ProgressAuthority(epistemic_steps=7), epoch_archive=RecordingEpochArchive())

    async def scenario():
        await adapter.open(response(("read", "read")))
        result = await invoke(reader, context, "read")
        assert result["executionStop"]["reason"] == "epistemic_budget"
        await adapter.open(response(("again", "read")))
        assert (await invoke(reader, context, "again"))["reason"] == "progress_work_closed"
        await adapter.open(response(("close", "checkpoint_task")))
        assert (await invoke(checkpoint, context, "close"))["success"] is True

    try:
        asyncio.run(scenario())
        assert observed == [True]
        assert runtime.authority.epistemic_steps == 8
    finally:
        runtime.close()
