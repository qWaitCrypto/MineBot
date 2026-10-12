import asyncio
import threading
from dataclasses import replace
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
from minebot.brain.deliberation import DecisionContext
from minebot.brain.lifecycle import LifecycleController, LifecycleState
from minebot.brain.modes import ModeRuntime
from minebot.brain.progress import ProgressAuthority
from minebot.brain.registry import RegisteredTool, ToolRegistry, ToolSidecar
from minebot.contract import BodyState, ProgressAbort, ProgressStopReason, ToolResult
from minebot.game.errors import BodyActionTimeoutError


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
        return SimpleNamespace(ok=True, accepted=True, complete=True)


class RecordingObservationArchive:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def store(self, *, tool_name, tool_call_id, result, complete):
        self.calls.append((tool_name, tool_call_id))
        return f"observation:{tool_call_id}"


class RecordingEpochArchive:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []
        self.seen_epistemic_keys: set[str] = set()

    def store(self, record):
        stored = dict(record)
        epistemic_keys = [
            str(key) for key in stored.get("epistemic_keys", ()) if str(key)
        ]
        stored["novel_epistemic_keys"] = [
            key for key in epistemic_keys if key not in self.seen_epistemic_keys
        ]
        self.seen_epistemic_keys.update(epistemic_keys)
        self.records.append(stored)
        return {**stored, "cursor": len(self.records)}

    def list_after(self, cursor, *, limit=100):
        return self.records[cursor : cursor + limit]

    def mark_progress_aborted(self, epoch_id, *, stop_reason="unknown"):
        record = next(record for record in self.records if record["epoch_id"] == epoch_id)
        record["progress_aborted"] = True
        record["progress_stop_reason"] = str(getattr(stop_reason, "value", stop_reason))


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
    return sdk_tool_for(tool).on_invoke_tool(wrapper, '{"intent":"finish the requested body work"}')


@pytest.mark.parametrize("change,success,expected", [
    ("time", False, False),
    ("time", True, False),
    ("movement", False, False),
    ("movement", True, True),
    ("damage", False, False),
    ("inventory_loss", False, False),
    ("inventory_loss", True, True),
    ("inventory_gain", False, True),
    ("inventory_gain", True, True),
    ("incomplete", True, False),
    ("preempted", True, False),
])
def test_epoch_material_evidence_distinguishes_effect_from_incidental_change(change, success, expected):
    class ChangingBody(EpochBody):
        changed = False

        def get_state(self):
            current = replace(super().get_state(), inventory_hash="unchanged", inventory_counts={"oak_log": 2})
            if not self.changed:
                return current
            changes = {
                "time": {"time": 5000},
                "movement": {"pos": (3.0, 64.0, 0.0)},
                "damage": {"health": 18.0, "food": 18},
                "inventory_loss": {"inventory_hash": "loss", "inventory_counts": {"oak_log": 1}},
                "inventory_gain": {"inventory_hash": "gain", "inventory_counts": {"oak_log": 3}},
                "incomplete": {"pos": (3.0, 64.0, 0.0), "complete": False},
                "preempted": {"pos": (3.0, 64.0, 0.0), "inventory_counts": {"oak_log": 3}},
            }
            return replace(current, **changes[change])

    body = ChangingBody()

    def action(_params):
        body.changed = True
        reason = "preempted" if change == "preempted" else "arrived" if success else "no_path"
        return ToolResult(success, reason, False)

    tool = make_tool("work", action, mutating=True)
    archive = RecordingEpochArchive()
    runtime, adapter, context = runtime_with(body, [tool], epoch_archive=archive)

    async def scenario():
        await adapter.open(response(("call-work", "work")))
        return await invoke(tool, context, "call-work")

    try:
        result = asyncio.run(scenario())
        assert result["success"] is success
        assert archive.records[0]["material_changed"] is expected
        assert bool(archive.records[0]["members"][0]["material_evidence"]) is expected
    finally:
        runtime.close()


@pytest.mark.parametrize("mode,expected", [
    ("killed", True), ("target_lost", False), ("preempted", False),
    ("incomplete", False), ("dimension_changed", False), ("wrong_tool", False),
])
def test_stationary_combat_credits_observed_target_effect_through_transaction(mode, expected):
    from minebot.body.combat import CombatTransactions
    from tests.unit.test_engage_runtime import FakeBody

    class CombatBody(FakeBody):
        finished = False

        def await_action_terminal(self, *args, **kwargs):
            terminal = super().await_action_terminal(*args, **kwargs)
            self.finished = True
            return terminal

        def get_state(self):
            state = super().get_state()
            if self.finished and mode == "incomplete":
                return replace(state, complete=False)
            if self.finished and mode == "dimension_changed":
                return replace(state, dimension="nether")
            return state

    body = CombatBody(terminal_reason=mode if mode in {"target_lost", "preempted"} else "killed")
    combat = CombatTransactions(body)
    tool_name = "unrelated_tool" if mode == "wrong_tool" else "engage_entity"
    tool = make_tool(tool_name, lambda _params: combat.engage_entity("nearest_hostile"), mutating=True)
    archive = RecordingEpochArchive()
    runtime, adapter, context = runtime_with(body, [tool], epoch_archive=archive)

    async def scenario():
        await adapter.open(response(("call-combat", tool_name)))
        return await invoke(tool, context, "call-combat")

    try:
        result = asyncio.run(scenario())
        assert result["success"] is (mode not in {"target_lost", "preempted"})
        record = archive.records[0]
        assert record["pre_body_fingerprint"] == record["post_body_fingerprint"]
        assert record["material_changed"] is expected
        assert ("observed_target_defeat" in record["members"][0]["material_evidence"]) is expected
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("recent", "decision_context", "expected"),
    [
        (None, None, "strategy-objective-start"),
        (
            {
                "tool": "collect_resource",
                "success": False,
                "reason": "resource_candidate_domain_exhausted",
                "summary": {"resume_hint": "reselect_candidates"},
            },
            None,
            "domain-exhausted",
        ),
        (
            {
                "tool": "collect_resource",
                "success": False,
                "reason": "resource_domain_budget_exhausted",
                "summary": {},
            },
            None,
            "budget",
        ),
        (
            {
                "tool": "collect_resource",
                "success": False,
                "reason": "unclassified_body_terminal",
                "summary": {},
            },
            None,
            "novel-failure",
        ),
        (None, DecisionContext.SOCIAL, "human"),
    ],
)
def test_mutating_invocation_persists_and_traces_closed_strategy_epoch_condition(
    recent,
    decision_context,
    expected,
):
    body = EpochBody()
    tool = make_tool(
        "collect_resource",
        lambda _params: ToolResult(True, "resource_domain_collected", False),
        mutating=True,
    )
    archive = RecordingEpochArchive()
    runtime, adapter, context = runtime_with(body, [tool], epoch_archive=archive)
    runtime.last_tool_results = [] if recent is None else [recent]
    runtime._current_decision_context = decision_context

    async def scenario():
        await adapter.open(response(("call-collect", "collect_resource")))
        return await invoke(tool, context, "call-collect")

    asyncio.run(scenario())
    trace = runtime.trace.snapshot()
    runtime.close()

    assert archive.records[0]["members"][0]["strategy_epoch_condition"] == expected
    event = next(item for item in trace if item["event"] == "strategy_epoch")
    assert event["condition"] == expected


def test_body_mutating_composite_records_novel_failure_strategy_epoch():
    body = EpochBody()
    tool = make_tool(
        "collect_resource",
        lambda _params: ToolResult(False, "new_physics_failure", False),
        mutating=False,
        body_mutating=True,
        source="agent.composition",
    )
    archive = RecordingEpochArchive()
    runtime, adapter, context = runtime_with(body, [tool], epoch_archive=archive)
    runtime.last_tool_results = [
        {
            "tool": "collect_resource",
            "success": False,
            "reason": "new_physics_failure",
            "summary": {},
        }
    ]

    async def scenario():
        await adapter.open(response(("call-collect", "collect_resource")))
        return await invoke(tool, context, "call-collect")

    asyncio.run(scenario())
    trace = runtime.trace.snapshot()
    runtime.close()

    assert archive.records[0]["members"][0]["strategy_epoch_condition"] == "novel-failure"
    event = next(item for item in trace if item["event"] == "strategy_epoch")
    assert event["tool"] == "collect_resource"
    assert event["condition"] == "novel-failure"


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


def test_governance_confirmation_settles_as_decision_required_not_failure():
    body = EpochBody()
    tool = make_tool(
        "go_to_surface",
        lambda _params: ToolResult(
            False,
            "governance_confirmation_required",
            True,
            metrics={"governance_confirmation": {"challenge_id": "gc-test"}},
        ),
        mutating=True,
    )
    archive = RecordingEpochArchive()
    runtime, adapter, context = runtime_with(body, [tool], epoch_archive=archive)

    async def scenario():
        await adapter.open(response(("call-review", "go_to_surface")))
        return await invoke(tool, context, "call-review")

    result = asyncio.run(scenario())
    runtime.close()

    assert result["success"] is False
    assert result["outcome"] == "decision_required"
    assert runtime.authority.failure_steps == 0
    assert archive.records[0]["members"][0]["status"] == "decision_required"


def test_partial_progress_settles_as_partial_not_failure():
    body = EpochBody()
    tool = make_tool(
        "collect_block_domain",
        lambda _params: ToolResult(
            False,
            "partial",
            True,
            metrics={"collected_delta": 2, "remaining_count": 1},
        ),
        mutating=True,
    )
    archive = RecordingEpochArchive()
    runtime, adapter, context = runtime_with(body, [tool], epoch_archive=archive)

    async def scenario():
        await adapter.open(response(("call-partial", "collect_block_domain")))
        return await invoke(tool, context, "call-partial")

    result = asyncio.run(scenario())
    runtime.close()

    assert result["success"] is False
    assert result["outcome"] == "partial"
    assert runtime.authority.failure_steps == 0
    assert archive.records[0]["members"][0]["status"] == "partial"


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


def test_novel_epistemic_evidence_is_archived_before_it_defers_abort():
    body = EpochBody()
    authority = ProgressAuthority()
    fingerprint = authority.fingerprint(body.get_state())
    for index in range(4):
        authority.note_step(("prior", index), success=False, fingerprint=fingerprint)
    failing = make_tool(
        "move_to",
        lambda _params: ToolResult(False, "blocked", True),
        mutating=True,
    )
    reader = make_tool(
        "read_state",
        lambda _params: ToolResult(
            True,
            "new_region_observed",
            False,
            metrics={"evidence_keys": ["region:overworld:1,0"]},
        ),
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
        second_result = await invoke(reader, context, "call-read")
        return first_result, second_result

    first_result, second_result = asyncio.run(scenario())
    runtime.close()

    assert first_result["reason"] == "blocked"
    assert second_result["reason"] == "new_region_observed"
    assert authority.failure_steps == 5
    assert authority.epistemic_steps == 1
    assert authority.last_epistemic_keys == ("region:overworld:1,0",)
    assert archive.records[0]["novel_epistemic_keys"] == ["region:overworld:1,0"]
    assert archive.records[0]["progress_aborted"] is False


def test_archive_failure_does_not_grant_unverified_epistemic_progress():
    class FailingArchive(RecordingEpochArchive):
        def store(self, record):
            raise RuntimeError("archive unavailable")

    body = EpochBody()
    authority = ProgressAuthority()
    fingerprint = authority.fingerprint(body.get_state())
    for index in range(4):
        authority.note_step(("prior", index), success=False, fingerprint=fingerprint)
    failing = make_tool(
        "move_to",
        lambda _params: ToolResult(
            False,
            "blocked_after_observation",
            True,
            metrics={"evidence_keys": ["region:overworld:1,0"]},
        ),
        mutating=True,
    )
    runtime, adapter, context = runtime_with(
        body,
        [failing],
        authority=authority,
        epoch_archive=FailingArchive(),
    )

    async def scenario():
        await adapter.open(response(("call-move", "move_to")))
        with pytest.raises(ProgressAbort):
            await invoke(failing, context, "call-move")

    asyncio.run(scenario())
    events = runtime.trace.snapshot()
    runtime.close()

    assert authority.epistemic_steps == 0
    assert any(event["event"] == "progress_epoch_archive_failed" for event in events)


def test_epoch_archive_failure_stops_even_a_materially_successful_batch():
    class FailingArchive(RecordingEpochArchive):
        def store(self, record):
            raise OSError("unavailable")

    body = EpochBody()
    def move(_params):
        body.x += 1
        return ToolResult(True, "moved", False)

    tool = make_tool("move_to", move, mutating=True)
    runtime, adapter, context = runtime_with(body, [tool], epoch_archive=FailingArchive())
    async def scenario():
        await adapter.open(response(("move", "move_to")))
        with pytest.raises(ProgressAbort) as caught:
            await invoke(tool, context, "move")
        assert caught.value.stop_reason is ProgressStopReason.UNKNOWN
    try:
        asyncio.run(scenario())
        settled = [event for event in runtime.trace.snapshot() if event["event"] == "progress_epoch_settled"]
        assert settled[-1]["material_changed"] is True
        assert settled[-1]["progress_stop_reason"] == "unknown"
        assert body.x == 1
    finally:
        runtime.close()


def test_failure_to_persist_a_strategy_stop_does_not_allow_reconsideration():
    class FailingStopArchive(RecordingEpochArchive):
        def mark_progress_aborted(self, epoch_id, *, stop_reason):
            raise OSError("unavailable")

    body = EpochBody()
    authority = ProgressAuthority(failure_steps=4)
    tool = make_tool("move_to", lambda _: ToolResult(False, "blocked", True), mutating=True)
    runtime, adapter, context = runtime_with(body, [tool], authority=authority,
        epoch_archive=FailingStopArchive())
    async def scenario():
        await adapter.open(response(("move", "move_to")))
        with pytest.raises(ProgressAbort) as caught:
            await invoke(tool, context, "move")
        assert caught.value.stop_reason is ProgressStopReason.UNKNOWN
    try:
        asyncio.run(scenario())
        settled = [event for event in runtime.trace.snapshot() if event["event"] == "progress_epoch_settled"]
        assert settled[-1]["progress_stop_reason"] == "unknown"
        assert settled[-1]["progress_aborted"] is True
    finally:
        runtime.close()


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


def test_epoch_records_body_action_timeout_without_transport_recovery():
    body = EpochBody()
    action_id = "action-epoch-timeout"

    def timeout(_params):
        raise BodyActionTimeoutError(
            f"timed out waiting for terminal event for action {action_id}",
            diagnostics={
                "action_id": action_id,
                "terminal_events": ["navigateDone"],
                "poll_count": 42,
                "wait_ms": 15000.0,
                "observed_events": 1,
            },
        )

    timed = make_tool(
        "move_to",
        timeout,
        mutating=True,
    )
    archive = RecordingEpochArchive()
    runtime, adapter, context = runtime_with(body, [timed], epoch_archive=archive)

    async def scenario():
        await adapter.open(response(("call-body-timeout", "move_to")))
        return await invoke(timed, context, "call-body-timeout")

    result = asyncio.run(scenario())
    trace = runtime.trace.snapshot()
    runtime.close()

    timeout_event = next(
        event
        for event in trace
        if event["event"] == "tool_result"
        and event["reason"] == "body_action_timeout"
    )
    assert result["reason"] == "body_action_timeout"
    assert timeout_event["full_result"]["metrics"]["await_diagnostics"]["action_id"] == action_id
    cleanup = timeout_event["full_result"]["metrics"]["orphan_cleanup"]
    assert cleanup["interrupt_requested"] is True
    assert cleanup["execution_idle"] is True
    assert cleanup["settled"] is True
    assert cleanup["reason"] == f"body_action_timeout:move_to:action_id={action_id}"
    assert body.interrupt_reasons == [
        f"body_action_timeout:move_to:action_id={action_id}"
    ]
    assert archive.records[0]["members"][0]["status"] == "timeout"
    assert archive.records[0]["members"][0]["reason"] == "body_action_timeout"
    assert runtime.consecutive_transport_errors == 0
    assert not any(event["event"] == "tool_transport_recovery_candidate" for event in trace)
    assert not any(event["event"] == "body_transport_error" for event in trace)


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
                arguments='{"intent":"finish the requested body work"}',
            )
            for call_id in ("call-first", "call-second")
        ]
    )
    wrapper = SimpleNamespace(context=context)
    sdk_tool = sdk_tool_for(reader)

    async def scenario():
        await adapter.open(batch)
        return (
            await sdk_tool.on_invoke_tool(wrapper, '{"intent":"finish the requested body work"}'),
            await sdk_tool.on_invoke_tool(wrapper, '{"intent":"finish the requested body work"}'),
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


def test_read_only_model_batch_does_not_count_as_physical_stall():
    body = EpochBody()
    authority = ProgressAuthority()
    fingerprint = authority.fingerprint(body.get_state())
    authority.observe_step(("prior_epoch",), fingerprint)
    reader = make_tool(
        "read_recipe",
        lambda _params: ToolResult(True, "recipe_read", False),
        mutating=False,
        source="body.inventory",
    )
    archive = RecordingEpochArchive()
    runtime, adapter, context = runtime_with(
        body,
        [reader],
        authority=authority,
        epoch_archive=archive,
    )
    call_ids = tuple(f"call-recipe-{index}" for index in range(9))

    async def scenario():
        await adapter.open(response(*((call_id, "read_recipe") for call_id in call_ids)))
        return [await invoke(reader, context, call_id) for call_id in call_ids]

    results = asyncio.run(scenario())
    runtime.close()

    assert all(result["reason"] == "recipe_read" for result in results)
    assert authority.stalled_steps == 0
    assert authority.failure_steps == 0
    assert archive.records[0]["captured_progress_step_count"] == 0
    assert archive.records[0]["committed_progress_step_count"] == 0
    assert archive.records[0]["progress_aborted"] is False


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
                            arguments='{"intent":"finish the requested body work"}',
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
