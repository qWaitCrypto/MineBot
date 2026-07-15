import asyncio
import threading
from types import SimpleNamespace

import pytest
from agents import Agent, RunConfig, Runner
from agents.items import ModelResponse
from agents.models.interface import Model
from agents.usage import Usage
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)

from minebot.app.runner import (
    AgentRuntime,
    ProgressEpochAdapter,
    RuntimeHooks,
    RuntimeRunContext,
    RuntimeTrace,
    sdk_tool_for,
)
from minebot.brain.context import AgentContext
from minebot.brain.lifecycle import LifecycleController, LifecycleState
from minebot.brain.modes import ModeRuntime
from minebot.brain.progress import ProgressAuthority
from minebot.brain.registry import RegisteredTool, ToolRegistry, ToolSidecar
from minebot.contract import BodyState, ProgressAbort, ToolResult


class EpochBody:
    bot_name = "Bot"

    def __init__(self) -> None:
        self.x = 0.0
        self.interrupt_reasons: list[str] = []

    def get_state(self) -> BodyState:
        return BodyState(
            bot="Bot",
            pos=(self.x, 64.0, 0.0),
            yaw=0.0,
            pitch=0.0,
            health=20.0,
            food=20,
            oxygen=300,
            inventory_raw="[]",
            inventory_hash=str(self.x),
            effects=None,
            time=1000,
            weather="clear",
            dimension="overworld",
            complete=True,
        )

    def interrupt(self, reason: str):
        self.interrupt_reasons.append(reason)
        return SimpleNamespace(ok=True)


class RecordingObservationArchive:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def store(self, *, tool_name, tool_call_id, result, complete):
        self.calls.append((tool_name, tool_call_id))
        return f"observation:{tool_call_id}"


class RecordingEpochArchive:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    def store(self, record):
        self.records.append(dict(record))
        return {**record, "cursor": len(self.records)}

    def list_after(self, cursor, *, limit=100):
        return self.records[cursor : cursor + limit]


def make_tool(
    name: str,
    callable_,
    *,
    mutating: bool,
    body_mutating: bool | None = None,
    source: str = "body.action",
    timeout_s: float | None = None,
) -> RegisteredTool:
    return RegisteredTool(
        name=name,
        description=name,
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        callable=callable_,
        sidecar=ToolSidecar(
            progress_key=name,
            mutating=mutating,
            body_mutating=body_mutating,
            source=source,
            timeout_s=timeout_s,
        ),
    )


def response(*calls: tuple[str, str]):
    return SimpleNamespace(
        output=[
            SimpleNamespace(type="function_call", call_id=call_id, name=tool_name)
            for call_id, tool_name in calls
        ]
    )


def runtime_with(
    body: EpochBody,
    tools: list[RegisteredTool],
    *,
    authority: ProgressAuthority | None = None,
    observation_archive=None,
    epoch_archive=None,
):
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    runtime = AgentRuntime(
        body=body,
        registry=registry,
        agent_context=AgentContext(system_prompt="sys", goal_text="test autonomy"),
        lifecycle=LifecycleController(),
        mode_runtime=ModeRuntime(),
        authority=authority or ProgressAuthority(),
        runner_run=lambda *_args, **_kwargs: None,
        observation_archive=observation_archive,
        progress_epoch_archive=epoch_archive,
    )
    adapter = ProgressEpochAdapter(runtime=runtime, run_id="run-test", archive=epoch_archive)
    context = RuntimeRunContext(
        agent_context=runtime.agent_context,
        weld_context=runtime.weld_context,
        profile=runtime.mode_runtime.profile_for(LifecycleState.ACTIVE),
        trace=runtime.trace,
        runtime=runtime,
        progress_epochs=adapter,
    )
    return runtime, adapter, context


def invoke(tool: RegisteredTool, context: RuntimeRunContext, call_id: str):
    wrapper = SimpleNamespace(context=context, tool_call_id=call_id)
    return sdk_tool_for(tool).on_invoke_tool(wrapper, "{}")


def test_epoch_preflight_rejects_every_conflicting_body_call_without_execution():
    body = EpochBody()
    executed: list[str] = []
    first = make_tool(
        "move_to",
        lambda _params: executed.append("move_to") or ToolResult(True, "arrived", False),
        mutating=True,
    )
    second = make_tool(
        "mine_block",
        lambda _params: executed.append("mine_block") or ToolResult(True, "collected", False),
        mutating=True,
    )
    archive = RecordingEpochArchive()
    runtime, adapter, context = runtime_with(body, [first, second], epoch_archive=archive)

    async def scenario():
        await adapter.open(response(("call-move", "move_to"), ("call-mine", "mine_block")))
        return await invoke(first, context, "call-move"), await invoke(second, context, "call-mine")

    first_result, second_result = asyncio.run(scenario())
    runtime.close()

    assert executed == []
    assert first_result["reason"] == "body_batch_conflict"
    assert second_result["reason"] == "body_batch_conflict"
    assert len(archive.records) == 1
    assert [member["status"] for member in archive.records[0]["members"]] == [
        "rejected",
        "rejected",
    ]


def test_epoch_defers_progress_abort_until_read_only_sibling_settles():
    body = EpochBody()
    authority = ProgressAuthority()
    fingerprint = authority.fingerprint(body.get_state())
    for index in range(4):
        authority.note_step(("prior", index), success=False, fingerprint=fingerprint)
    observed: list[str] = []
    failing = make_tool(
        "move_to",
        lambda _params: ToolResult(False, "blocked", True),
        mutating=True,
    )
    reader = make_tool(
        "read_state",
        lambda _params: observed.append("read") or ToolResult(True, "state_read", False),
        mutating=False,
        source="body.perception",
    )
    archive = RecordingEpochArchive()
    runtime, adapter, context = runtime_with(
        body,
        [failing, reader],
        authority=authority,
        epoch_archive=archive,
    )

    async def scenario():
        await adapter.open(response(("call-move", "move_to"), ("call-read", "read_state")))
        first_result = await invoke(failing, context, "call-move")
        with pytest.raises(ProgressAbort):
            await invoke(reader, context, "call-read")
        return first_result

    first_result = asyncio.run(scenario())
    runtime.close()

    assert first_result["reason"] == "blocked"
    assert observed == ["read"]
    assert authority.failure_steps == 5
    assert archive.records[0]["progress_aborted"] is True
    assert [member["status"] for member in archive.records[0]["members"]] == [
        "failure",
        "success",
    ]


def test_native_sdk_call_id_reaches_observation_and_epoch_archives():
    body = EpochBody()
    observations = RecordingObservationArchive()
    epochs = RecordingEpochArchive()
    reader = make_tool(
        "read_state",
        lambda _params: ToolResult(
            True,
            "state_read",
            False,
            metrics={"evidence_keys": ["state:overworld:0,64,0"]},
        ),
        mutating=False,
        source="body.perception",
    )
    runtime, adapter, context = runtime_with(
        body,
        [reader],
        observation_archive=observations,
        epoch_archive=epochs,
    )

    async def scenario():
        await adapter.open(response(("sdk-call-1", "read_state")))
        return await invoke(reader, context, "sdk-call-1")

    result = asyncio.run(scenario())
    runtime.close()

    assert result["traceRef"] == "sdk-call-1"
    assert observations.calls == [("read_state", "sdk-call-1")]
    assert epochs.records[0]["evidence_refs"] == ["observation:sdk-call-1"]
    assert epochs.records[0]["epistemic_keys"] == ["state:overworld:0,64,0"]
    decision = next(
        event for event in runtime.trace.snapshot() if event["event"] == "tool_decision_context"
    )
    assert decision["sdk_tool_call_id_native"] is True


def test_run_finalization_marks_uninvoked_epoch_members_cancelled():
    body = EpochBody()
    reader = make_tool(
        "read_state",
        lambda _params: ToolResult(True, "state_read", False),
        mutating=False,
        source="body.perception",
    )
    archive = RecordingEpochArchive()
    runtime, adapter, _context = runtime_with(body, [reader], epoch_archive=archive)

    async def scenario():
        await adapter.open(response(("call-read", "read_state")))
        adapter.finalize_unsettled("turn_cancelled")

    asyncio.run(scenario())
    runtime.close()

    assert archive.records[0]["members"][0]["status"] == "cancelled"
    assert archive.records[0]["members"][0]["reason"] == "turn_cancelled"


def test_epoch_records_execution_timeout_and_interrupts_orphaned_body_work():
    body = EpochBody()
    timed = make_tool(
        "timed_read",
        lambda _params: threading.Event().wait(0.08)
        or ToolResult(True, "late_result", False),
        mutating=False,
        source="body.perception",
        timeout_s=0.02,
    )
    archive = RecordingEpochArchive()
    runtime, adapter, context = runtime_with(body, [timed], epoch_archive=archive)

    async def scenario():
        await adapter.open(response(("call-timeout", "timed_read")))
        return await invoke(timed, context, "call-timeout")

    result = asyncio.run(scenario())
    runtime.close()

    assert result["reason"] == "tool_timeout"
    assert body.interrupt_reasons == ["tool_timeout:timed_read"]
    assert archive.records[0]["members"][0]["status"] == "timeout"
    assert archive.records[0]["members"][0]["reason"] == "tool_timeout"


def test_duplicate_same_name_calls_claim_predeclared_ids_in_fifo_order():
    body = EpochBody()
    observations = RecordingObservationArchive()
    epochs = RecordingEpochArchive()
    reader = make_tool(
        "read_state",
        lambda _params: ToolResult(True, "state_read", False),
        mutating=False,
        source="body.perception",
    )
    runtime, adapter, context = runtime_with(
        body,
        [reader],
        observation_archive=observations,
        epoch_archive=epochs,
    )
    batch = SimpleNamespace(
        output=[
            SimpleNamespace(
                type="function_call",
                call_id=call_id,
                name="read_state",
                arguments="{}",
            )
            for call_id in ("call-first", "call-second")
        ]
    )
    wrapper = SimpleNamespace(context=context)
    sdk_tool = sdk_tool_for(reader)

    async def scenario():
        await adapter.open(batch)
        return (
            await sdk_tool.on_invoke_tool(wrapper, "{}"),
            await sdk_tool.on_invoke_tool(wrapper, "{}"),
        )

    asyncio.run(scenario())
    runtime.close()

    assert observations.calls == [
        ("read_state", "call-first"),
        ("read_state", "call-second"),
    ]
    assert [member["tool_call_id"] for member in epochs.records[0]["members"]] == [
        "call-first",
        "call-second",
    ]


def test_real_sdk_hook_and_tool_context_share_the_same_call_id():
    class TwoTurnModel(Model):
        def __init__(self) -> None:
            self.calls = 0

        async def get_response(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(
                    output=[
                        ResponseFunctionToolCall(
                            arguments="{}",
                            call_id="sdk-native-call",
                            name="read_state",
                            type="function_call",
                            status="completed",
                        )
                    ],
                    usage=Usage(),
                    response_id="response-1",
                )
            return ModelResponse(
                output=[
                    ResponseOutputMessage(
                        id="message-1",
                        content=[
                            ResponseOutputText(
                                annotations=[],
                                text="Observed.",
                                type="output_text",
                            )
                        ],
                        role="assistant",
                        status="completed",
                        type="message",
                    )
                ],
                usage=Usage(),
                response_id="response-2",
            )

        async def stream_response(self, *args, **kwargs):
            if False:
                yield None

    body = EpochBody()
    observations = RecordingObservationArchive()
    epochs = RecordingEpochArchive()
    reader = make_tool(
        "read_state",
        lambda _params: ToolResult(True, "state_read", False),
        mutating=False,
        source="body.perception",
    )
    runtime, adapter, context = runtime_with(
        body,
        [reader],
        observation_archive=observations,
        epoch_archive=epochs,
    )
    agent = Agent(
        name="EpochSdkProbe",
        instructions="Use the tool.",
        model=TwoTurnModel(),
        tools=[sdk_tool_for(reader)],
    )

    async def scenario():
        return await Runner.run(
            agent,
            "observe",
            context=context,
            hooks=RuntimeHooks(),
            max_turns=3,
            run_config=RunConfig(tracing_disabled=True),
        )

    result = asyncio.run(scenario())
    runtime.close()

    assert result.final_output == "Observed."
    assert observations.calls == [("read_state", "sdk-native-call")]
    assert epochs.records[0]["members"][0]["tool_call_id"] == "sdk-native-call"
