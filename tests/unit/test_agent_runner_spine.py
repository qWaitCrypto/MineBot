import asyncio
import json
import threading
import time
import unittest

from agents.exceptions import MaxTurnsExceeded, UserError
from agents.items import MessageOutputItem, ToolCallItem, ToolCallOutputItem

from minebot.app.config import agent_language_from_env
from minebot.app.console import parse_collect_goal
from minebot.app.runner import (
    AgentRuntime,
    BodyRecoveryRequired,
    RecoveryOutcome,
    RuntimeRunContext,
    RuntimeTrace,
    SerialExecutionLane,
    ToolExecutionTimeout,
    _model_tool_payload,
    extract_model_response_observations,
    extract_run_observations,
    sdk_tool_for,
    tool_is_enabled,
)
from minebot.app.wiring import build_agent_runtime
from minebot.brain.context import AgentContext
from minebot.brain.lifecycle import LifecycleController, LifecycleState
from minebot.brain.modes import AgentSignal, ModeRuntime
from minebot.brain.progress import ProgressAuthority
from minebot.brain.registry import RegisteredTool, ToolRegistry, ToolSidecar, WeldContext
from minebot.brain.persona import prompt_with_language
from minebot.contract import (
    BodyState,
    LegalityDecision,
    PerceptionResult,
    Result,
    ToolResult,
    execution_checkpoint,
)
from minebot.game.body import ScarpetBody
from minebot.game.errors import BodyActionTimeoutError, RconError


def body_state(x=0.0):
    return BodyState(
        bot="Bot",
        pos=(x, 64.0, 0.0),
        yaw=None,
        pitch=None,
        health=20.0,
        food=20,
        oxygen=300,
        inventory_raw="[]",
        inventory_hash=str(x),
        effects=None,
        time=1000,
        weather=None,
        dimension="overworld",
        complete=True,
    )


def missing_body_state():
    return BodyState(
        bot="Bot",
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
        missing=True,
    )


class FakeBody:
    bot_name = "Bot"

    def __init__(self):
        self.x = 0.0
        self.events = []
        self.interrupt_reasons = []

    def spawn(self, *args, **kwargs):
        return Result(None, self.bot_name, "result", True, True, True)

    def despawn(self):
        return Result(None, self.bot_name, "result", True, True, True)

    def get_state(self):
        return body_state(self.x)

    def perceive(self, scope, params):
        return PerceptionResult(self.bot_name, scope, "perception", True, True, {})

    def execute(self, action):
        return Result(action.id, self.bot_name, "result", True, True, False)

    def await_action_terminal(self, action_id, timeout_s=15.0):
        raise NotImplementedError

    def poll_events(self):
        events = list(self.events)
        self.events.clear()
        return events

    def ignite_block(self, pos, *, item=None, allow_server_substitute=False, timeout_s=8.0):
        raise NotImplementedError

    def sow_crop(self, pos, *, crop_block, seed_item=None, allow_server_substitute=False, timeout_s=8.0):
        raise NotImplementedError

    def interrupt(self, reason=None):
        self.interrupt_reasons.append(reason)
        return Result(None, self.bot_name, "result", True, True, True)


class WeakAgent:
    pass


def make_tool(body: FakeBody, *, mutating=True):
    def callable_(params):
        body.x += float(params.get("dx", 1.0))
        return ToolResult(True, "completed", False, metrics={"x": body.x})

    return RegisteredTool(
        name="move_step",
        description="Move by dx",
        input_schema={
            "type": "object",
            "properties": {"dx": {"type": "number"}},
            "additionalProperties": False,
        },
        callable=callable_,
        sidecar=ToolSidecar(
            progress_key="move_step",
            mutating=mutating,
            permission="move",
            body_scope=("navigation",),
            terminal_truth=("position",),
            timeout_s=2.0,
        ),
    )


class AgentRunnerSpineTests(unittest.TestCase):
    def test_prompt_language_template_keeps_ids_canonical(self):
        prompt = prompt_with_language("base", language="Chinese")

        self.assertIn("Use Chinese", prompt)
        self.assertIn("canonical English IDs", prompt)

    def test_agent_language_from_env_defaults_and_overrides(self):
        self.assertEqual(agent_language_from_env({}, default="English"), "English")
        self.assertEqual(agent_language_from_env({"MINEBOT_AGENT_LANGUAGE": "Chinese"}, default="English"), "Chinese")

    def test_build_agent_runtime_injects_language_prompt(self):
        parts = build_agent_runtime(
            body=FakeBody(),
            registry=ToolRegistry(),
            system_prompt="base",
            language="Chinese",
            goal_text="collect",
        )

        self.assertIn("Use Chinese", parts.context.system_prompt)

    def test_parse_collect_goal_extracts_common_terminal_goal_shapes(self):
        self.assertEqual(parse_collect_goal("collect 3 dirt"), ("dirt", 3))
        self.assertEqual(parse_collect_goal("gather minecraft:oak_log 12"), ("oak_log", 12))
        self.assertIsNone(parse_collect_goal("come here"))

    def test_extract_run_observations_captures_speech_and_tool_items(self):
        class FakeRunResult:
            final_output = "Done."

            def to_input_list(self):
                return [
                    {"role": "assistant", "content": [{"type": "output_text", "text": "I will gather dirt."}]},
                    {"type": "function_call", "name": "collect_resource", "arguments": '{"resource":"dirt"}'},
                    {"type": "function_call_output", "output": '{"success":true,"reason":"completed"}'},
                ]

        events = extract_run_observations(FakeRunResult())

        self.assertIn({"event": "assistant_final_output", "content": "Done."}, events)
        self.assertIn({"event": "assistant_message", "content": "I will gather dirt."}, events)
        self.assertTrue(
            any(
                event["event"] == "model_tool_call"
                and event["tool"] == "collect_resource"
                and event["arguments_summary"] == '{"resource": "dirt"}'
                for event in events
            )
        )
        self.assertTrue(any(event["event"] == "model_tool_output" for event in events))

    def test_extract_run_observations_prefers_typed_new_items(self):
        class RawFunctionCall:
            def __init__(self, name, arguments):
                self.name = name
                self.arguments = arguments
                self.call_id = "call-1"
                self.id = "call-1"

        class FakeMessage:
            def __init__(self, text):
                self.content = [type("Txt", (), {"text": text})()]

        class FakeRunResult:
            final_output = "Collected one log."

            def __init__(self):
                agent = WeakAgent()
                self.new_items = [
                    MessageOutputItem(agent=agent, raw_item=FakeMessage("I will try the nearest tree.")),  # type: ignore[arg-type]
                    ToolCallItem(agent=agent, raw_item=RawFunctionCall("collect_resource", '{"resource":"logs","count":64}')),  # type: ignore[arg-type]
                    ToolCallOutputItem(
                        agent=agent,  # type: ignore[arg-type]
                        raw_item={"type": "function_call_output", "call_id": "call-1", "output": '{"success":true}'},
                        output={"success": True, "reason": "progress"},
                    ),
                ]

        events = extract_run_observations(FakeRunResult())

        self.assertIn({"event": "assistant_message", "content": "I will try the nearest tree."}, events)
        self.assertIn({"event": "assistant_final_output", "content": "Collected one log."}, events)
        self.assertTrue(
            any(
                event["event"] == "model_tool_call"
                and event["tool"] == "collect_resource"
                and '"count": 64' in (event["arguments_summary"] or "")
                for event in events
            )
        )
        self.assertTrue(any(event["event"] == "model_tool_output" for event in events))

    def test_extract_run_observations_failure_is_non_fatal(self):
        class BrokenRunResult:
            final_output = None

            def to_input_list(self):
                raise RuntimeError("sdk drift")

        events = extract_run_observations(BrokenRunResult())

        self.assertEqual(events, [{"event": "run_observation_failed", "error_type": "RuntimeError"}])

    def test_tool_only_run_is_marked_as_observation_gap(self):
        body = FakeBody()

        class ToolOnlyRunResult:
            final_output = None

            def to_input_list(self):
                return [{"type": "function_call", "name": "collect_resource", "arguments": "{}"}]

        async def fake_runner(*args, **kwargs):
            return ToolOnlyRunResult()

        runtime = AgentRuntime(
            body=body,
            registry=ToolRegistry(),
            agent_context=AgentContext(system_prompt="sys", goal_text="collect"),
            lifecycle=LifecycleController(),
            mode_runtime=ModeRuntime(),
            authority=ProgressAuthority(),
            runner_run=fake_runner,
        )

        outcome = asyncio.run(runtime.run_turn())

        self.assertEqual(outcome.status, "completed_turn")
        self.assertTrue(any(event["event"] == "model_tool_call" for event in runtime.trace.snapshot()))
        self.assertTrue(any(event["event"] == "assistant_no_content_tool_only" for event in runtime.trace.snapshot()))

    def test_extract_model_response_observations_reads_model_output_directly(self):
        class FakeFunctionCall:
            type = "function_call"
            name = "collect_resource"
            arguments = '{"item":"oak_log","count":16}'

        class FakeMessage:
            type = "message"

            def __init__(self, text):
                self.content = [type("Txt", (), {"text": text})()]

        class FakeModelResponse:
            output = [FakeMessage("I will collect nearby logs first."), FakeFunctionCall()]

        events = extract_model_response_observations(FakeModelResponse())

        self.assertIn({"event": "assistant_message", "content": "I will collect nearby logs first."}, events)
        self.assertTrue(
            any(
                event["event"] == "model_tool_call"
                and event["tool"] == "collect_resource"
                and '"oak_log"' in (event["arguments_summary"] or "")
                for event in events
            )
        )

    def test_visible_assistant_output_is_recorded_into_agent_context(self):
        class SpeechRunResult:
            final_output = "I found the first tree."

            def to_input_list(self):
                return [{"role": "assistant", "content": "I am starting with nearby logs."}]

        async def fake_runner(*args, **kwargs):
            return SpeechRunResult()

        context = AgentContext(system_prompt="sys", goal_text="collect 64 logs")
        runtime = AgentRuntime(
            body=FakeBody(),
            registry=ToolRegistry(),
            agent_context=context,
            lifecycle=LifecycleController(),
            mode_runtime=ModeRuntime(),
            authority=ProgressAuthority(),
            runner_run=fake_runner,
        )

        asyncio.run(runtime.run_turn())

        self.assertIn(("assistant", "I found the first tree."), context.session_messages())
        self.assertNotIn(("assistant", "I am starting with nearby logs."), context.session_messages())

    def test_visible_assistant_output_is_sent_to_optional_speech_sink_once_per_turn(self):
        class SpeechRunResult:
            final_output = "I found the first tree."

            def to_input_list(self):
                return [{"role": "assistant", "content": "I am starting with nearby logs."}]

        async def fake_runner(*args, **kwargs):
            return SpeechRunResult()

        spoken = []
        runtime = AgentRuntime(
            body=FakeBody(),
            registry=ToolRegistry(),
            agent_context=AgentContext(system_prompt="sys", goal_text="collect 64 logs"),
            lifecycle=LifecycleController(),
            mode_runtime=ModeRuntime(),
            authority=ProgressAuthority(),
            runner_run=fake_runner,
            speech_sink=spoken.append,
        )

        asyncio.run(runtime.run_turn())

        self.assertEqual(spoken, ["I found the first tree."])

    def test_tool_only_turn_does_not_trigger_speech_sink(self):
        class ToolOnlyRunResult:
            final_output = None

            def to_input_list(self):
                return [{"type": "function_call", "name": "collect_resource", "arguments": "{}"}]

        async def fake_runner(*args, **kwargs):
            return ToolOnlyRunResult()

        spoken = []
        runtime = AgentRuntime(
            body=FakeBody(),
            registry=ToolRegistry(),
            agent_context=AgentContext(system_prompt="sys", goal_text="collect"),
            lifecycle=LifecycleController(),
            mode_runtime=ModeRuntime(),
            authority=ProgressAuthority(),
            runner_run=fake_runner,
            speech_sink=spoken.append,
        )

        asyncio.run(runtime.run_turn())

        self.assertEqual(spoken, [])

    def test_sdk_tool_invokes_registered_tool_through_weld(self):
        body = FakeBody()
        tool = make_tool(body)
        sdk_tool = sdk_tool_for(tool)
        agent_context = AgentContext(system_prompt="sys", goal_text="collect")
        authority = ProgressAuthority()
        runtime_context = RuntimeRunContext(
            agent_context=agent_context,
            weld_context=WeldContext(body=body, authority=authority, goal_text="collect"),
            profile=ModeRuntime().profile_for(LifecycleState.ACTIVE),
        )

        class Wrapper:
            context = runtime_context

        out = asyncio.run(sdk_tool.on_invoke_tool(Wrapper(), json.dumps({"dx": 2})))

        self.assertTrue(out["success"])
        self.assertEqual(out["reason"], "completed")
        self.assertTrue(out["complete"])
        self.assertEqual(body.x, 2.0)
        self.assertIsNotNone(authority.last_action)
        self.assertIsNone(runtime_context.weld_context.writer.holder)
        self.assertIsNone(sdk_tool._failure_error_function)
        self.assertFalse(sdk_tool._use_default_failure_error_function)

    def test_sdk_tool_body_work_does_not_block_event_loop(self):
        body = FakeBody()
        started = threading.Event()
        release = threading.Event()

        def callable_(_params):
            started.set()
            release.wait(timeout=2)
            body.x += 1
            return ToolResult(True, "completed", False, metrics={"x": body.x})

        tool = RegisteredTool(
            name="blocking_step",
            description="Blocking body step",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            callable=callable_,
            sidecar=ToolSidecar(progress_key="blocking_step", mutating=True),
        )
        runtime = AgentRuntime(
            body=body,
            registry=ToolRegistry(),
            agent_context=AgentContext(system_prompt="sys", goal_text="test"),
            lifecycle=LifecycleController(),
            mode_runtime=ModeRuntime(),
            authority=ProgressAuthority(),
            runner_run=lambda *_args, **_kwargs: None,
        )
        runtime_context = RuntimeRunContext(
            agent_context=runtime.agent_context,
            weld_context=runtime.weld_context,
            profile=ModeRuntime().profile_for(LifecycleState.ACTIVE),
            runtime=runtime,
        )
        sdk_tool = sdk_tool_for(tool)

        class Wrapper:
            context = runtime_context

        async def scenario():
            fallback = threading.Timer(1.0, release.set)
            fallback.start()
            task = asyncio.create_task(sdk_tool.on_invoke_tool(Wrapper(), "{}"))
            while not started.is_set():
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
            responsive = not task.done()
            release.set()
            result = await task
            fallback.cancel()
            return responsive, result

        responsive, result = asyncio.run(scenario())
        runtime.close()

        self.assertTrue(responsive)
        self.assertTrue(result["success"])
        self.assertEqual(body.x, 1.0)

    def test_turn_preflight_body_read_does_not_block_event_loop(self):
        started = threading.Event()
        release = threading.Event()

        class SlowStateBody(FakeBody):
            def get_state(self):
                started.set()
                release.wait(timeout=2)
                return super().get_state()

        async def fake_runner(*_args, **_kwargs):
            return {"ok": True}

        runtime = AgentRuntime(
            body=SlowStateBody(),
            registry=ToolRegistry(),
            agent_context=AgentContext(system_prompt="sys", goal_text="talk"),
            lifecycle=LifecycleController(),
            mode_runtime=ModeRuntime(),
            authority=ProgressAuthority(),
            runner_run=fake_runner,
        )

        async def scenario():
            fallback = threading.Timer(1.0, release.set)
            fallback.start()
            task = asyncio.create_task(runtime.run_turn())
            while not started.is_set():
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
            responsive = not task.done() and not release.is_set()
            release.set()
            outcome = await task
            fallback.cancel()
            return responsive, outcome

        responsive, outcome = asyncio.run(scenario())
        runtime.close()

        self.assertTrue(responsive)
        self.assertEqual(outcome.status, "completed_turn")

    def test_streamed_turn_cancel_uses_sdk_cancel_and_drains_stream(self):
        class FakeStream:
            final_output = None
            new_items = []

            def __init__(self):
                self.started = threading.Event()
                self.cancelled = threading.Event()
                self.cancel_modes = []
                self.drained = False

            def cancel(self, mode="immediate"):
                self.cancel_modes.append(mode)
                self.cancelled.set()

            async def stream_events(self):
                self.started.set()
                while not self.cancelled.is_set():
                    await asyncio.sleep(0.01)
                self.drained = True
                if False:
                    yield None

        stream = FakeStream()
        runtime = AgentRuntime(
            body=FakeBody(),
            registry=ToolRegistry(),
            agent_context=AgentContext(system_prompt="sys", goal_text="talk"),
            lifecycle=LifecycleController(),
            mode_runtime=ModeRuntime(),
            authority=ProgressAuthority(),
            runner_run_streamed=lambda *_args, **_kwargs: stream,
        )

        async def scenario():
            task = asyncio.create_task(runtime.run_turn())
            while not stream.started.is_set():
                await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())
        runtime.close()

        self.assertEqual(stream.cancel_modes, ["immediate"])
        self.assertTrue(stream.drained)
        self.assertTrue(any(event["event"] == "stream_cancelled" for event in runtime.trace.snapshot()))

    def test_cancelled_sdk_tool_interrupts_body_and_leaves_no_lane_orphan(self):
        release = threading.Event()

        class InterruptibleBody(FakeBody):
            def interrupt(self, reason=None):
                release.set()
                return super().interrupt(reason)

        body = InterruptibleBody()
        started = threading.Event()

        def callable_(_params):
            started.set()
            release.wait(timeout=2)
            return ToolResult(False, "preempted", True)

        tool = RegisteredTool(
            name="long_action",
            description="Long action",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            callable=callable_,
            sidecar=ToolSidecar(progress_key="long_action", mutating=True),
        )
        runtime = AgentRuntime(
            body=body,
            registry=ToolRegistry(),
            agent_context=AgentContext(system_prompt="sys", goal_text="test"),
            lifecycle=LifecycleController(),
            mode_runtime=ModeRuntime(),
            authority=ProgressAuthority(),
            runner_run=lambda *_args, **_kwargs: None,
        )
        runtime_context = RuntimeRunContext(
            agent_context=runtime.agent_context,
            weld_context=runtime.weld_context,
            profile=ModeRuntime().profile_for(LifecycleState.ACTIVE),
            runtime=runtime,
        )
        sdk_tool = sdk_tool_for(tool)

        class Wrapper:
            context = runtime_context

        async def scenario():
            task = asyncio.create_task(sdk_tool.on_invoke_tool(Wrapper(), "{}"))
            while not started.is_set():
                await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            return await runtime.wait_for_execution_idle(timeout_s=1.0)

        idle = asyncio.run(scenario())
        runtime.close()

        self.assertTrue(idle)
        self.assertEqual(runtime.execution_lane.active_count, 0)
        self.assertEqual(body.interrupt_reasons, ["tool_cancelled:long_action"])

    def test_execution_lane_timeout_starts_after_queued_call_begins(self):
        lane = SerialExecutionLane(thread_name="timeout-origin-test")
        first_started = threading.Event()

        def first_call():
            first_started.set()
            threading.Event().wait(0.12)
            return "first"

        def second_call():
            threading.Event().wait(0.01)
            return "second"

        async def scenario():
            first = asyncio.create_task(lane.run(first_call, timeout_s=0.5))
            while not first_started.is_set():
                await asyncio.sleep(0.005)
            queued_at = asyncio.get_running_loop().time()
            second = asyncio.create_task(lane.run(second_call, timeout_s=0.05))
            second_result = await second
            elapsed = asyncio.get_running_loop().time() - queued_at
            return await first, second_result, elapsed

        try:
            first_result, second_result, elapsed = asyncio.run(scenario())
        finally:
            lane.close()

        self.assertEqual(first_result, "first")
        self.assertEqual(second_result, "second")
        self.assertGreater(elapsed, 0.05)

    def test_execution_lane_timeout_cooperatively_settles_callback(self):
        lane = SerialExecutionLane(thread_name="timeout-cancel-test")
        started = threading.Event()

        def callback():
            started.set()
            while True:
                execution_checkpoint()
                time.sleep(0.002)

        async def scenario():
            with self.assertRaises(ToolExecutionTimeout):
                await lane.run(callback, timeout_s=0.03)
            return await lane.wait_idle(timeout_s=0.25)

        try:
            idle = asyncio.run(scenario())
        finally:
            lane.close()

        self.assertTrue(started.is_set())
        self.assertTrue(idle)
        self.assertEqual(lane.active_count, 0)

    def test_scarpet_body_request_boundary_settles_cancelled_execution(self):
        class SlowTransport:
            def __init__(self):
                self.started = threading.Event()

            def request(self, _command):
                self.started.set()
                time.sleep(0.05)
                return "{}"

        transport = SlowTransport()
        body = ScarpetBody("Bot1", transport)
        lane = SerialExecutionLane(thread_name="body-cancel-test")

        async def scenario():
            task = asyncio.create_task(lane.run(body.get_state))
            while not transport.started.is_set():
                await asyncio.sleep(0.002)
            cancelled_count = lane.request_cancel("session_command:quit")
            with self.assertRaises(asyncio.CancelledError):
                await task
            idle = await lane.wait_idle(timeout_s=0.25)
            return cancelled_count, idle

        try:
            cancelled_count, idle = asyncio.run(scenario())
        finally:
            lane.close()

        self.assertEqual(cancelled_count, 1)
        self.assertTrue(idle)
        self.assertEqual(lane.active_count, 0)

    def test_execution_lane_close_waits_for_cooperative_worker_exit(self):
        lane = SerialExecutionLane(thread_name="close-cancel-test")
        started = threading.Event()

        def callback():
            started.set()
            while True:
                execution_checkpoint()
                time.sleep(0.002)

        async def scenario():
            task = asyncio.create_task(lane.run(callback))
            while not started.is_set():
                await asyncio.sleep(0.002)
            await asyncio.to_thread(lane.close)
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())

        self.assertEqual(lane.active_count, 0)

    def test_tool_execution_timeout_interrupts_body_and_is_not_transport_error(self):
        release = threading.Event()

        class InterruptibleBody(FakeBody):
            def interrupt(self, reason=None):
                release.set()
                return super().interrupt(reason)

        body = InterruptibleBody()

        def callable_(_params):
            release.wait(timeout=2)
            return ToolResult(False, "preempted", True)

        tool = RegisteredTool(
            name="timed_action",
            description="Timed action",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            callable=callable_,
            sidecar=ToolSidecar(
                progress_key="timed_action",
                mutating=True,
                timeout_s=0.05,
            ),
        )
        runtime = AgentRuntime(
            body=body,
            registry=ToolRegistry(),
            agent_context=AgentContext(system_prompt="sys", goal_text="test"),
            lifecycle=LifecycleController(),
            mode_runtime=ModeRuntime(),
            authority=ProgressAuthority(),
            runner_run=lambda *_args, **_kwargs: None,
        )
        runtime_context = RuntimeRunContext(
            agent_context=runtime.agent_context,
            weld_context=runtime.weld_context,
            profile=ModeRuntime().profile_for(LifecycleState.ACTIVE),
            runtime=runtime,
        )
        sdk_tool = sdk_tool_for(tool)

        class Wrapper:
            context = runtime_context

        try:
            result = asyncio.run(sdk_tool.on_invoke_tool(Wrapper(), "{}"))
        finally:
            runtime.close()

        self.assertIsNone(sdk_tool.timeout_seconds)
        self.assertEqual(result["reason"], "tool_timeout")
        self.assertEqual(body.interrupt_reasons, ["tool_timeout:timed_action"])
        self.assertEqual(runtime.execution_lane.active_count, 0)
        self.assertFalse(
            any(event["event"] == "tool_transport_recovery_candidate" for event in runtime.trace.snapshot())
        )

    def test_body_action_timeout_requests_owner_cleanup_without_transport_recovery(self):
        action_id = "action-timeout-1"

        class OwnerBody(FakeBody):
            def __init__(self):
                super().__init__()
                self.owner = "moveTo"
                self.owner_checks = 0

            def event_head(self, _proposed_epoch):
                self.owner_checks += 1
                if self.owner_checks >= 2:
                    self.owner = None
                return {"owner": self.owner}

            def interrupt(self, reason=None):
                return super().interrupt(reason)

        body = OwnerBody()
        calls = 0

        def callable_(_params):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise BodyActionTimeoutError(
                    f"timed out waiting for terminal event for action {action_id}",
                    diagnostics={
                        "action_id": action_id,
                        "terminal_events": ["moveDone", "navigateDone"],
                        "poll_count": 168,
                        "wait_ms": 17099.98,
                        "observed_events": 3,
                        "observed": [
                            {
                                "seq": 1667,
                                "name": "moveCancelDelayed",
                                "action_id": action_id,
                            }
                        ],
                    },
                )
            body.x += 1.0
            return ToolResult(True, "arrived", False, metrics={"x": body.x})

        tool = RegisteredTool(
            name="move_to",
            description="Move",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            callable=callable_,
            sidecar=ToolSidecar(
                progress_key="move_to",
                mutating=True,
                permission="move",
                body_scope=("navigation",),
                terminal_truth=("position",),
            ),
        )
        runtime = AgentRuntime(
            body=body,
            registry=ToolRegistry(),
            agent_context=AgentContext(system_prompt="sys", goal_text="test"),
            lifecycle=LifecycleController(),
            mode_runtime=ModeRuntime(),
            authority=ProgressAuthority(),
            runner_run=lambda *_args, **_kwargs: None,
        )
        runtime_context = RuntimeRunContext(
            agent_context=runtime.agent_context,
            weld_context=runtime.weld_context,
            profile=ModeRuntime().profile_for(LifecycleState.ACTIVE),
            runtime=runtime,
        )
        sdk_tool = sdk_tool_for(tool)

        class Wrapper:
            context = runtime_context

        try:
            timeout_result = asyncio.run(sdk_tool.on_invoke_tool(Wrapper(), "{}"))
            next_result = asyncio.run(sdk_tool.on_invoke_tool(Wrapper(), "{}"))
            trace = runtime.trace.snapshot()
        finally:
            runtime.close()

        self.assertEqual(timeout_result["reason"], "body_action_timeout")
        self.assertEqual(
            body.interrupt_reasons,
            [f"body_action_timeout:move_to:action_id={action_id}"],
        )
        self.assertEqual(runtime.consecutive_transport_errors, 0)
        self.assertFalse(
            any(event["event"] == "tool_transport_recovery_candidate" for event in trace)
        )
        self.assertFalse(any(event["event"] == "body_transport_error" for event in trace))
        cancelled = next(event for event in trace if event["event"] == "execution_cancelled")
        self.assertTrue(cancelled["owner_observed"])
        self.assertIsNone(cancelled["owner"])
        self.assertEqual(cancelled["owner_checks"], 2)
        self.assertTrue(cancelled["settled"])
        self.assertTrue(next_result["success"])
        self.assertEqual(next_result["reason"], "arrived")
        self.assertEqual(calls, 2)
        self.assertEqual(runtime.execution_lane.active_count, 0)

    def test_streamed_turn_body_death_preempts_into_recovery(self):
        class FakeStream:
            final_output = None
            new_items = []

            def __init__(self):
                self.cancelled = threading.Event()
                self.drained = False

            def cancel(self, mode="immediate"):
                self.cancelled.set()

            async def stream_events(self):
                while not self.cancelled.is_set():
                    await asyncio.sleep(0.01)
                self.drained = True
                if False:
                    yield None

        body = FakeBody()
        state_calls = {"count": 0}

        def state():
            state_calls["count"] += 1
            return body_state() if state_calls["count"] == 1 else missing_body_state()

        body.get_state = state  # type: ignore[method-assign]
        stream = FakeStream()
        runtime = AgentRuntime(
            body=body,
            registry=ToolRegistry(),
            agent_context=AgentContext(system_prompt="sys", goal_text="collect"),
            lifecycle=LifecycleController(),
            mode_runtime=ModeRuntime(),
            authority=ProgressAuthority(),
            runner_run_streamed=lambda *_args, **_kwargs: stream,
        )

        outcome = asyncio.run(runtime.run_turn())
        runtime.close()

        self.assertEqual(outcome.status, "stopped")
        self.assertEqual(outcome.lifecycle, LifecycleState.RECOVERING)
        self.assertTrue(stream.drained)
        self.assertIn("missing_body", body.interrupt_reasons)

    def test_sdk_tool_returns_compact_model_payload_and_traces_full_result(self):
        def callable_(_params):
            return ToolResult(
                False,
                "partial_budget_exhausted",
                True,
                next_suggestion="reselect candidates",
                metrics={
                    "item": "oak_log",
                    "target_count": 64,
                    "before_count": 0,
                    "after_count": 7,
                    "remaining_count": 57,
                    "attempts": [{"target": [i, 64, 0], "mine": {"metrics": {"huge": "x" * 100}}} for i in range(20)],
                    "skipped": [{"reason": "navigation_blocked:no_path"} for _ in range(4)],
                    "uncertainty": [{"reason": "limit_exceeded"}],
                },
            )

        tool = RegisteredTool(
            name="collect_resource",
            description="Collect",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            callable=callable_,
            sidecar=ToolSidecar(progress_key="collect_resource", mutating=False, tool_type="resource"),
        )
        sdk_tool = sdk_tool_for(tool)
        trace = RuntimeTrace()
        runtime_context = RuntimeRunContext(
            agent_context=AgentContext(system_prompt="sys", goal_text="collect"),
            weld_context=WeldContext(body=FakeBody(), authority=ProgressAuthority(), goal_text="collect"),
            profile=ModeRuntime().profile_for(LifecycleState.ACTIVE),
            trace=trace,
        )

        class Wrapper:
            context = runtime_context

        out = asyncio.run(sdk_tool.on_invoke_tool(Wrapper(), "{}"))

        self.assertEqual(out["reason"], "partial_budget_exhausted")
        self.assertFalse(out["complete"])
        self.assertIn("traceRef", out)
        self.assertEqual(out["summary"]["item"], "oak_log")
        self.assertEqual(out["summary"]["remaining_count"], 57)
        self.assertEqual(out["summary"]["attempt_count"], 20)
        self.assertNotIn("metrics", out)
        result_event = next(event for event in trace.snapshot() if event["event"] == "tool_result")
        self.assertIn("full_result", result_event)
        self.assertIn("model_result", result_event)
        self.assertIn("attempts", result_event["full_result"]["metrics"])

    def test_resource_domain_projection_keeps_actionable_body_blockers(self):
        result = ToolResult(
            False,
            "resource_domain_budget_exhausted",
            True,
            metrics={
                "collected_total": 0,
                "remaining_delta": 3,
                "attempts": [
                    {
                        "navigation": {
                            "success": False,
                            "reason": "water_egress_failed",
                            "metrics": {
                                "selected_goal": [-81, 73, 16],
                                "segments": [
                                    {
                                        "diagnostics": {
                                            "event_data": {"final_pos": [-74.7, 63.0, 14.5]},
                                            "movement_counts": {"swim": 6, "ascend": 2},
                                            "capability_snapshot": {
                                                "allow_break": True,
                                                "allow_place": False,
                                                "allow_pillar": False,
                                                "allow_downward": True,
                                                "allow_swim": True,
                                                "scaffold_item": None,
                                                "scaffold_count": 0,
                                            },
                                            "mutation_events": [
                                                {
                                                    "event": "navigateMutationDone",
                                                    "data": {
                                                        "success": False,
                                                        "reason": "mutation_denied",
                                                        "decision_reason": "structure_risk_unknown",
                                                    },
                                                }
                                            ],
                                        }
                                    }
                                ],
                            },
                        }
                    },
                    {
                        "navigation": {
                            "success": False,
                            "reason": "stuck",
                            "metrics": {"segments": []},
                        }
                    },
                ],
                "searches": [
                    {
                        "truncated": True,
                        "uncertainty": [{"reason": "page_limit"}],
                    }
                ],
            },
        ).to_payload()

        payload = _model_tool_payload(
            "collect_block_domain",
            result,
            trace_ref="resource-domain-trace",
        )

        summary = payload["summary"]
        self.assertEqual(summary["process_blockers"], ["stuck:1", "water_egress_failed:1"])
        self.assertEqual(summary["governance_blockers"], ["structure_risk_unknown:1"])
        self.assertEqual(summary["movement_counts"], {"ascend": 2, "swim": 6})
        self.assertEqual(summary["final_pos"], [-74.7, 63.0, 14.5])
        self.assertEqual(summary["selected_goal"], [-81, 73, 16])
        self.assertFalse(summary["capability_snapshot"]["allow_pillar"])
        self.assertEqual(summary["capability_snapshot"]["scaffold_count"], 0)
        self.assertTrue(summary["search_truncated"])
        self.assertEqual(summary["search_uncertainty"], ["page_limit:1"])
        self.assertIn("retry the same Body domain", summary["resume_hint"])
        self.assertFalse(payload["projection"]["complete"])
        self.assertIn("metrics.attempts", payload["projection"]["omittedFields"])
        self.assertIn("metrics.searches", payload["projection"]["omittedFields"])

    def test_navigation_projection_keeps_typed_governance_exhaustion_facts(self):
        result = ToolResult(
            False,
            "protected_or_denied",
            True,
            metrics={
                "goal": [19, 75, -72],
                "navigation_goal": {"kind": "near", "pos": [19, 75, -72], "radius": 2},
                "segment_count": 32,
                "segments": [{"status": "mutation_denied"} for _ in range(32)],
                "denied_mutation_count": 32,
                "governance_blockers": {"structure_risk_unknown": 32},
                "mutation_blockers": {
                    "break:stone:structure_risk_unknown": 25,
                    "downward:stone:structure_risk_unknown": 7,
                },
                "movement_counts": {"ascend": 17, "break": 37, "downward": 7, "walk": 41},
                "final_pos": [20.501, 66.0, -75.034],
                "selected_goal": [19, 75, -72],
                "capability_snapshot": {
                    "allow_break": True,
                    "allow_place": False,
                    "allow_pillar": False,
                    "allow_downward": True,
                    "scaffold_item": None,
                    "scaffold_count": 0,
                },
            },
        ).to_payload()

        payload = _model_tool_payload(
            "move_to",
            result,
            trace_ref="navigation-denial-trace",
            observation_handle="observation:navigation-denial",
        )

        summary = payload["summary"]
        self.assertEqual(summary["final_pos"], [20.501, 66.0, -75.034])
        self.assertEqual(summary["denied_mutation_count"], 32)
        self.assertEqual(summary["governance_blockers"], {"structure_risk_unknown": 32})
        self.assertEqual(
            summary["mutation_blockers"],
            {
                "break:stone:structure_risk_unknown": 25,
                "downward:stone:structure_risk_unknown": 7,
            },
        )
        self.assertFalse(summary["capability_snapshot"]["allow_pillar"])
        self.assertEqual(summary["capability_snapshot"]["scaffold_count"], 0)
        self.assertNotIn("segments", summary)
        self.assertIn("metrics.segments", payload["projection"]["omittedFields"])

    def test_model_tool_payload_preserves_authoritative_inventory_counts(self):
        result = ToolResult(
            True,
            "inventory_counted",
            False,
            metrics={"counts": {"rotten_flesh": 2, "oak_log": 4}},
        ).to_payload()

        payload = _model_tool_payload("read_inventory", result, trace_ref="inventory-trace")

        self.assertEqual(payload["summary"]["counts"], {"oak_log": 4, "rotten_flesh": 2})
        self.assertEqual(payload["summary"]["distinct_item_count"], 2)
        self.assertTrue(payload["summary"]["counts_complete"])
        self.assertNotIn("omitted_item_count", payload["summary"])
        self.assertNotIn("metrics", payload)

    def test_model_tool_payload_marks_inventory_count_projection_truncation(self):
        counts = {f"item_{index:02d}": index for index in range(50)}
        result = ToolResult(
            True,
            "inventory_counted",
            False,
            metrics={"counts": counts},
        ).to_payload()

        payload = _model_tool_payload("read_inventory", result, trace_ref="inventory-trace")

        self.assertEqual(len(payload["summary"]["counts"]), 48)
        self.assertEqual(payload["summary"]["distinct_item_count"], 50)
        self.assertFalse(payload["summary"]["counts_complete"])
        self.assertEqual(payload["summary"]["omitted_item_count"], 2)
        self.assertIn("item_00", payload["summary"]["counts"])
        self.assertNotIn("item_49", payload["summary"]["counts"])

    def test_model_tool_payload_projects_actionable_exploration_truth(self):
        blocks = [
            {"kind": "block", "type": "oak_log", "pos": [index, 64, 0]}
            for index in range(10)
        ]
        result = ToolResult(
            True,
            "found",
            False,
            metrics={
                "targets": {
                    "requested": {"blocks": ["#logs"], "entities": []},
                    "expanded": {"blocks": ["oak_log", "spruce_log"], "entities": []},
                    "query_signature": "signature",
                },
                "dimension": "minecraft:overworld",
                "origin": [0, 64, 0],
                "final_pos": [8, 64, 8],
                "budget": {"max_regions": 4, "regions_consumed": 2},
                "covered_regions": [[0, 0], [0, 1]],
                "coverage_revision": 7,
                "blocks": blocks,
                "entities": [],
                "candidate_failures": [{"reason": "stuck"}, {"reason": "stuck"}],
                "evidence_keys": ["coverage:one", "block:one"],
                "resume_cursor": None,
                "continuation": None,
                "complete": True,
            },
        ).to_payload()

        payload = _model_tool_payload("explore_for", result, trace_ref="explore-trace")

        self.assertEqual(payload["summary"]["targets"], {"blocks": ["#logs"], "entities": []})
        self.assertEqual(payload["summary"]["block_count"], 10)
        self.assertEqual(len(payload["summary"]["blocks"]), 8)
        self.assertFalse(payload["summary"]["blocks_complete"])
        self.assertEqual(payload["summary"]["candidate_failure_count"], 2)
        self.assertEqual(payload["summary"]["candidate_failure_reasons"], ["stuck:2"])
        self.assertEqual(payload["summary"]["evidence_key_count"], 2)
        self.assertIsNone(payload["summary"]["continuation"])
        self.assertFalse(payload["projection"]["complete"])

    def test_model_tool_payload_preserves_typed_exploration_continuation(self):
        block_targets = [f"flower_{index}" for index in range(16)]
        cursor = {
            "query_signature": "signature",
            "dimension": "minecraft:overworld",
            "coverage_revision": 7,
        }
        continuation = {
            "kind": "resume_operation",
            "tool": "explore_for",
            "target_descriptor": {
                "block_targets": block_targets,
                "entity_targets": ["#farm_animals"],
            },
            "resume_cursor": cursor,
            "target_descriptor_must_match": True,
        }
        result = ToolResult(
            False,
            "mobility_blocked",
            True,
            metrics={
                "targets": {
                    "requested": {
                        "blocks": block_targets,
                        "entities": ["#farm_animals"],
                    },
                },
                "resume_cursor": cursor,
                "continuation": continuation,
                "candidate_failures": [{"reason": "stuck"}],
                "complete": False,
            },
        ).to_payload()

        payload = _model_tool_payload("explore_for", result, trace_ref="explore-trace")

        self.assertEqual(payload["summary"]["continuation"], continuation)
        self.assertEqual(payload["summary"]["resume_cursor"], cursor)
        self.assertNotIn("metrics.continuation", payload["projection"]["omittedFields"])

    def test_model_tool_payload_preserves_unknown_small_terminal_facts_generically(self):
        result = ToolResult(
            True,
            "action_terminal",
            False,
            metrics={
                "owner": "navigateTo",
                "action_id": "action-17",
                "stopped_reason": "arrived",
                "terminal_tick": 8123,
            },
        ).to_payload()

        payload = _model_tool_payload("future_safe_capability", result, trace_ref="future-trace")

        self.assertEqual(payload["summary"]["owner"], "navigateTo")
        self.assertEqual(payload["summary"]["action_id"], "action-17")
        self.assertEqual(payload["summary"]["stopped_reason"], "arrived")
        self.assertEqual(payload["summary"]["terminal_tick"], 8123)
        self.assertTrue(payload["projection"]["complete"])

    def test_model_tool_payload_preserves_task_revisions_and_step_statuses(self):
        result = ToolResult(
            True,
            "task_plan_updated",
            False,
            metrics={
                "plan": {
                    "plan_id": "plan-1",
                    "revision": 4,
                    "summary": "Acquire supplies",
                    "steps": [
                        {
                            "step_id": "step-1",
                            "ordinal": 0,
                            "title": "Acquire iron",
                            "status": "in_progress",
                            "evidence": ["inventory iron=3"],
                            "blocker": None,
                        }
                    ],
                },
                "current": {
                    "active": True,
                    "task": {
                        "task_id": "task-1",
                        "revision": 7,
                        "goal": "prepare for the End",
                        "status": "running",
                    },
                },
            },
        ).to_payload()

        payload = _model_tool_payload("update_plan", result, trace_ref="task-trace")
        artifact = payload["summary"]["task_artifact"]

        self.assertEqual(artifact["task"]["revision"], 7)
        self.assertEqual(artifact["plan"]["revision"], 4)
        self.assertEqual(artifact["plan"]["steps"][0]["status"], "in_progress")
        self.assertTrue(artifact["plan"]["steps_complete"])
        self.assertNotIn("metrics", payload)

    def test_model_tool_payload_exposes_conversation_archive_handles(self):
        result = ToolResult(
            True,
            "conversation_archive_query",
            False,
            metrics={
                "query": "diamond",
                "start": 0,
                "limit": 5,
                "total_matches": 1,
                "results": [
                    {
                        "handle": "conversation:scope:turn:7",
                        "turn": 7,
                        "user": "Where was the diamond vein?",
                        "assistant": "Near the ravine.",
                        "tools": ["read_state"],
                        "tool_reasons": ["state_read"],
                        "item_count": 4,
                    }
                ],
                "next_start": None,
                "complete": True,
            },
        ).to_payload()

        payload = _model_tool_payload(
            "query_conversation_archive",
            result,
            trace_ref="archive-query",
        )

        self.assertEqual(
            payload["summary"]["results"][0]["handle"],
            "conversation:scope:turn:7",
        )
        self.assertTrue(payload["summary"]["results_complete"])
        self.assertTrue(payload["summary"]["complete"])

    def test_model_tool_payload_exposes_bounded_conversation_turn_items(self):
        items = [
            {"role": "user", "content": "question"},
            {"type": "function_call", "call_id": "call-1", "name": "read_state"},
            {"type": "function_call_output", "call_id": "call-1", "output": "{}"},
            {"role": "assistant", "content": "answer"},
        ]
        result = ToolResult(
            True,
            "conversation_archive_read",
            False,
            metrics={
                "handle": "conversation:scope:turn:2",
                "turn": 2,
                "start": 0,
                "limit": 20,
                "item_count": 4,
                "items": items,
                "next_start": None,
                "complete": True,
            },
        ).to_payload()

        payload = _model_tool_payload(
            "read_conversation_archive",
            result,
            trace_ref="archive-read",
        )

        self.assertEqual(payload["summary"]["handle"], "conversation:scope:turn:2")
        self.assertEqual(payload["summary"]["items"], items)
        self.assertTrue(payload["summary"]["items_complete"])

    def test_sdk_tool_records_tool_decision_context(self):
        def callable_(_params):
            return ToolResult(
                True,
                "state_read",
                False,
                metrics={"pos": [1, 70, 2], "health": 10.0, "food": 4, "oxygen": 300},
            )

        tool = RegisteredTool(
            name="read_state",
            description="Read",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            callable=callable_,
            sidecar=ToolSidecar(progress_key="read_state", mutating=False, tool_type="state"),
        )
        sdk_tool = sdk_tool_for(tool)
        trace = RuntimeTrace()
        runtime = AgentRuntime(
            body=FakeBody(),
            registry=ToolRegistry(),
            agent_context=AgentContext(system_prompt="sys", goal_text="collect"),
            lifecycle=LifecycleController(),
            mode_runtime=ModeRuntime(),
            authority=ProgressAuthority(),
        )
        runtime.last_known_body_state = {"pos": [0.5, 70.0, 0.5], "health": 12.0, "food": 3}
        runtime.last_tool_results = [
            {
                "tool": "collect_resource",
                "success": False,
                "reason": "partial_budget_exhausted",
                "summary": {"after_count": 12},
            }
        ]
        runtime.agent_context.observe_assistant_message("I will look for food, then continue logs.")
        runtime_context = RuntimeRunContext(
            agent_context=runtime.agent_context,
            weld_context=runtime.weld_context,
            profile=ModeRuntime().profile_for(LifecycleState.ACTIVE),
            trace=trace,
            runtime=runtime,
        )

        class Wrapper:
            context = runtime_context

        out = asyncio.run(sdk_tool.on_invoke_tool(Wrapper(), "{}"))

        self.assertTrue(out["success"])
        events = trace.snapshot()
        decision = next(event for event in events if event["event"] == "tool_decision_context")
        self.assertEqual(decision["tool"], "read_state")
        self.assertEqual(decision["last_known_body_state"]["food"], 3)
        self.assertEqual(decision["recent_tool_results"][0]["reason"], "partial_budget_exhausted")
        self.assertEqual(decision["recent_session_messages"][0]["role"], "assistant")
        self.assertTrue(runtime.last_tool_results[-1]["success"])

    def test_mobility_terminal_refreshes_live_run_context(self):
        tool = RegisteredTool(
            name="explore_for",
            description="Explore",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            callable=lambda _params: ToolResult(
                False,
                "mobility_blocked",
                True,
                metrics={"candidate_failure_count": 1},
            ),
            sidecar=ToolSidecar(
                progress_key="explore_for",
                mutating=True,
                source="body.exploration",
                tool_type="exploration",
                permission="explore_world",
                body_scope=("navigation",),
            ),
        )
        runtime = AgentRuntime(
            body=FakeBody(),
            registry=ToolRegistry(),
            agent_context=AgentContext(system_prompt="sys", goal_text="collect logs"),
            lifecycle=LifecycleController(),
            mode_runtime=ModeRuntime(),
            authority=ProgressAuthority(),
        )
        self.addCleanup(runtime.close)
        profile = ModeRuntime().profile_for(LifecycleState.ACTIVE)
        runtime.agent_context.observe_profile(profile)
        runtime_context = RuntimeRunContext(
            agent_context=runtime.agent_context,
            weld_context=runtime.weld_context,
            profile=profile,
            trace=runtime.trace,
            runtime=runtime,
            instruction_preamble=runtime.agent_context.turn_preamble(
                include_session_messages=False
            ),
        )

        class Wrapper:
            context = runtime_context

        output = asyncio.run(sdk_tool_for(tool).on_invoke_tool(Wrapper(), "{}"))

        self.assertFalse(output["success"])
        self.assertEqual(runtime_context.profile.situational, "mobility")
        self.assertIn("situational=mobility", runtime_context.instruction_preamble)
        instructions = runtime._instructions(Wrapper(), runtime.agent)
        self.assertIn("Mobility/reachability issue", instructions)
        self.assertEqual(runtime._pending_mobility_terminal["reason"], "mobility_blocked")
        self.assertTrue(
            any(event["event"] == "mobility_terminal_live_handoff" for event in runtime.trace.snapshot())
        )

    def test_resource_navigation_terminal_alone_refreshes_live_run_context(self):
        runtime = AgentRuntime(
            body=FakeBody(),
            registry=ToolRegistry(),
            agent_context=AgentContext(system_prompt="sys", goal_text="collect logs"),
            lifecycle=LifecycleController(),
            mode_runtime=ModeRuntime(),
            authority=ProgressAuthority(),
        )
        self.addCleanup(runtime.close)
        profile = ModeRuntime().profile_for(LifecycleState.ACTIVE)
        runtime.agent_context.observe_profile(profile)
        runtime_context = RuntimeRunContext(
            agent_context=runtime.agent_context,
            weld_context=runtime.weld_context,
            profile=profile,
            trace=runtime.trace,
            runtime=runtime,
            instruction_preamble=runtime.agent_context.turn_preamble(include_session_messages=False),
        )

        runtime.remember_tool_result(
            "collect_resource",
            ToolResult(False, "partial_budget_exhausted", True).to_payload(),
            run_context=runtime_context,
        )
        self.assertIsNone(runtime._pending_mobility_terminal)

        runtime.remember_tool_result(
            "collect_resource",
            ToolResult(False, "resource_navigation_no_path", True).to_payload(),
            run_context=runtime_context,
        )

        self.assertEqual(runtime_context.profile.situational, "mobility")
        self.assertIn("situational=mobility", runtime_context.instruction_preamble)
        self.assertEqual(runtime._pending_mobility_terminal["reason"], "resource_navigation_no_path")

    def test_pending_mobility_terminal_survives_bookkeeping_until_next_outer_turn(self):
        calls = []

        async def fake_runner(agent, input_text, *, context=None, **kwargs):
            calls.append((context.profile.situational, context.instruction_preamble))
            return {"ok": True}

        runtime = AgentRuntime(
            body=FakeBody(),
            registry=ToolRegistry(),
            agent_context=AgentContext(system_prompt="sys", goal_text="collect logs"),
            lifecycle=LifecycleController(),
            mode_runtime=ModeRuntime(),
            authority=ProgressAuthority(),
            runner_run=fake_runner,
        )
        self.addCleanup(runtime.close)
        runtime.remember_tool_result(
            "explore_for",
            ToolResult(False, "mobility_blocked", True, metrics={"candidate_failure_count": 1}).to_payload(),
        )
        for _ in range(13):
            runtime.remember_tool_result(
                "read_state",
                ToolResult(True, "state_read", False).to_payload(),
            )

        self.assertEqual(len(runtime.last_tool_results), 12)
        self.assertFalse(any(item["reason"] == "mobility_blocked" for item in runtime.last_tool_results))
        self.assertEqual(runtime._pending_mobility_terminal["reason"], "mobility_blocked")

        outcome = asyncio.run(runtime.run_turn())

        self.assertEqual(outcome.status, "completed_turn")
        self.assertEqual(outcome.profile.situational, "mobility")
        self.assertEqual(calls[0][0], "mobility")
        self.assertIn("situational=mobility", calls[0][1])
        self.assertIsNone(runtime._pending_mobility_terminal)
        self.assertTrue(
            any(
                event["event"] == "mobility_terminal_carried_to_outer_turn"
                for event in runtime.trace.snapshot()
            )
        )

    def test_sdk_tool_marks_truncated_result_incomplete_for_model(self):
        def callable_(_params):
            return ToolResult(
                True,
                "block_in_range",
                False,
                metrics={
                    "target": [3, 70, 0],
                    "truncated": True,
                    "pages_read": 2,
                    "total_matches": 128,
                },
            )

        tool = RegisteredTool(
            name="search_for_block",
            description="Search",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            callable=callable_,
            sidecar=ToolSidecar(progress_key="search_for_block", mutating=False, tool_type="perception"),
        )
        sdk_tool = sdk_tool_for(tool)
        trace = RuntimeTrace()
        runtime_context = RuntimeRunContext(
            agent_context=AgentContext(system_prompt="sys", goal_text="collect"),
            weld_context=WeldContext(body=FakeBody(), authority=ProgressAuthority(), goal_text="collect"),
            profile=ModeRuntime().profile_for(LifecycleState.ACTIVE),
            trace=trace,
        )

        class Wrapper:
            context = runtime_context

        out = asyncio.run(sdk_tool.on_invoke_tool(Wrapper(), "{}"))

        self.assertTrue(out["success"])
        self.assertFalse(out["complete"])
        self.assertEqual(out["summary"]["truncated"], True)
        self.assertEqual(out["summary"]["pages_read"], 2)
        self.assertEqual(out["summary"]["total_matches"], 128)

    def test_sdk_tool_marks_top_level_next_result_incomplete_for_model(self):
        projected = _model_tool_payload(
            "debug_page",
            {"success": True, "reason": "page_read", "canRetry": False, "next": "64", "metrics": {"count": 64}},
            trace_ref="trace-1",
        )

        self.assertFalse(projected["complete"])
        self.assertEqual(projected["summary"]["count"], 64)

    def test_sdk_tool_returns_partial_collect_without_hidden_continuation(self):
        body = FakeBody()
        registry = ToolRegistry()
        collect_calls = []

        def collect_callable(params):
            collect_calls.append(dict(params))
            if len(collect_calls) == 1:
                return ToolResult(
                    True,
                    "partial_budget_exhausted",
                    True,
                    metrics={
                        "requested_item": "oak_log",
                        "item": "oak_log",
                        "target_count": 64,
                        "before_count": 0,
                        "after_count": 10,
                        "collected_delta": 10,
                        "remaining_count": 54,
                        "complete": False,
                        "resume_hint": "reselect_candidates",
                        "budget": {"max_candidates": 80, "max_mutating_calls": 80, "max_wall_s": 300},
                    },
                )
            return ToolResult(
                True,
                "collected",
                False,
                metrics={
                    "requested_item": "oak_log",
                    "item": "oak_log",
                    "target_count": 64,
                    "before_count": 10,
                    "after_count": 64,
                    "collected_delta": 54,
                    "remaining_count": 0,
                    "complete": True,
                    "resume_hint": "complete",
                },
            )

        registry.register(
            RegisteredTool(
                name="collect_resource",
                description="Collect",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                callable=collect_callable,
                sidecar=ToolSidecar(progress_key="collect_resource", mutating=False, tool_type="resource"),
            )
        )

        runtime = AgentRuntime(
            body=body,
            registry=registry,
            agent_context=AgentContext(system_prompt="sys", goal_text="collect 64 logs"),
            lifecycle=LifecycleController(),
            mode_runtime=ModeRuntime(),
            authority=ProgressAuthority(),
        )
        sdk_tool = sdk_tool_for(registry.get("collect_resource"))
        runtime_context = RuntimeRunContext(
            agent_context=runtime.agent_context,
            weld_context=runtime.weld_context,
            profile=ModeRuntime().profile_for(LifecycleState.ACTIVE),
            trace=runtime.trace,
            runtime=runtime,
        )

        class Wrapper:
            pass

        wrapper = Wrapper()
        wrapper.context = runtime_context
        out = asyncio.run(sdk_tool.on_invoke_tool(wrapper, json.dumps({"item": "oak_log", "count": 64})))

        self.assertTrue(out["success"])
        self.assertFalse(out["complete"])
        self.assertEqual(out["reason"], "partial_budget_exhausted")
        self.assertEqual(collect_calls, [{"item": "oak_log", "count": 64}])
        events = runtime.trace.snapshot()
        self.assertFalse(any(event["event"].startswith("tool_continuation") for event in events))
        result_event = next(event for event in events if event["event"] == "tool_result")
        self.assertEqual(result_event["full_result"]["reason"], "partial_budget_exhausted")

    def test_sdk_tool_does_not_continue_collect_without_inventory_delta(self):
        body = FakeBody()
        registry = ToolRegistry()
        collect_calls = []

        def collect_callable(params):
            collect_calls.append(dict(params))
            return ToolResult(
                True,
                "partial_budget_exhausted",
                True,
                metrics={
                    "requested_item": "oak_log",
                    "item": "oak_log",
                    "target_count": 64,
                    "before_count": 10,
                    "after_count": 10,
                    "collected_delta": 0,
                    "remaining_count": 54,
                    "complete": False,
                    "resume_hint": "reselect_candidates",
                },
            )

        registry.register(
            RegisteredTool(
                name="collect_resource",
                description="Collect",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                callable=collect_callable,
                sidecar=ToolSidecar(progress_key="collect_resource", mutating=False, tool_type="resource"),
            )
        )

        runtime = AgentRuntime(
            body=body,
            registry=registry,
            agent_context=AgentContext(system_prompt="sys", goal_text="collect 64 logs"),
            lifecycle=LifecycleController(),
            mode_runtime=ModeRuntime(),
            authority=ProgressAuthority(),
        )
        sdk_tool = sdk_tool_for(registry.get("collect_resource"))
        runtime_context = RuntimeRunContext(
            agent_context=runtime.agent_context,
            weld_context=runtime.weld_context,
            profile=ModeRuntime().profile_for(LifecycleState.ACTIVE),
            trace=runtime.trace,
            runtime=runtime,
        )

        class Wrapper:
            pass

        wrapper = Wrapper()
        wrapper.context = runtime_context
        out = asyncio.run(sdk_tool.on_invoke_tool(wrapper, json.dumps({"item": "oak_log", "count": 64})))

        self.assertEqual(out["reason"], "partial_budget_exhausted")
        self.assertEqual(len(collect_calls), 1)
        self.assertFalse(any(event["event"] == "tool_continuation" for event in runtime.trace.snapshot()))

    def test_sdk_tool_returns_collect_prerequisite_partial_to_model(self):
        body = FakeBody()
        registry = ToolRegistry()
        collect_calls = []

        def collect_callable(params):
            collect_calls.append(dict(params))
            if len(collect_calls) == 1:
                return ToolResult(
                    False,
                    "ensure_step_incomplete",
                    True,
                    metrics={
                        "item": "diamond",
                        "requested_item": "diamond",
                        "target_count": 3,
                        "required_tool": "iron_pickaxe",
                        "resume_hint": "reinvoke_ensure",
                        "ensure_result": {
                            "success": False,
                            "reason": "ensure_step_incomplete",
                            "canRetry": True,
                            "metrics": {
                                "item": "iron_pickaxe",
                                "target_count": 1,
                                "resume_hint": "reinvoke_ensure",
                            },
                        },
                    },
                )
            return ToolResult(
                True,
                "collected",
                False,
                metrics={
                    "requested_item": "diamond",
                    "item": "diamond",
                    "target_count": 3,
                    "after_count": 3,
                    "collected_delta": 3,
                    "remaining_count": 0,
                    "complete": True,
                    "resume_hint": "complete",
                },
            )

        registry.register(
            RegisteredTool(
                name="collect_resource",
                description="Collect",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                callable=collect_callable,
                sidecar=ToolSidecar(progress_key="collect_resource", mutating=False, tool_type="resource"),
            )
        )

        runtime = AgentRuntime(
            body=body,
            registry=registry,
            agent_context=AgentContext(system_prompt="sys", goal_text="collect 3 diamond"),
            lifecycle=LifecycleController(),
            mode_runtime=ModeRuntime(),
            authority=ProgressAuthority(),
        )
        sdk_tool = sdk_tool_for(registry.get("collect_resource"))
        runtime_context = RuntimeRunContext(
            agent_context=runtime.agent_context,
            weld_context=runtime.weld_context,
            profile=ModeRuntime().profile_for(LifecycleState.ACTIVE),
            trace=runtime.trace,
            runtime=runtime,
        )

        class Wrapper:
            pass

        wrapper = Wrapper()
        wrapper.context = runtime_context
        out = asyncio.run(sdk_tool.on_invoke_tool(wrapper, json.dumps({"item": "diamond", "count": 3})))

        self.assertFalse(out["success"])
        self.assertEqual(out["reason"], "ensure_step_incomplete")
        self.assertEqual(collect_calls, [{"item": "diamond", "count": 3}])
        self.assertFalse(
            any(
                event["event"].startswith("tool_continuation")
                for event in runtime.trace.snapshot()
            )
        )

    def test_sdk_tool_returns_candidate_exhaustion_without_hidden_continuation(self):
        body = FakeBody()
        registry = ToolRegistry()
        collect_calls = []

        def collect_callable(params):
            collect_calls.append(dict(params))
            if len(collect_calls) == 1:
                return ToolResult(
                    True,
                    "candidate_targets_exhausted",
                    True,
                    metrics={
                        "requested_item": "logs",
                        "item": "oak_log",
                        "target_count": 64,
                        "before_count": 0,
                        "after_count": 2,
                        "collected_delta": 2,
                        "remaining_count": 62,
                        "complete": False,
                        "resume_hint": "reselect_candidates",
                        "budget": {"max_candidates": 30, "max_mutating_calls": 80, "max_wall_s": 120},
                    },
                )
            return ToolResult(
                True,
                "partial_budget_exhausted",
                True,
                metrics={
                    "requested_item": "logs",
                    "item": "oak_log",
                    "target_count": 64,
                    "before_count": 2,
                    "after_count": 3,
                    "collected_delta": 1,
                    "remaining_count": 61,
                    "complete": False,
                    "resume_hint": "reselect_candidates",
                },
            )

        registry.register(
            RegisteredTool(
                name="collect_resource",
                description="Collect",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                callable=collect_callable,
                sidecar=ToolSidecar(progress_key="collect_resource", mutating=False, tool_type="resource"),
            )
        )

        runtime = AgentRuntime(
            body=body,
            registry=registry,
            agent_context=AgentContext(system_prompt="sys", goal_text="collect 64 logs"),
            lifecycle=LifecycleController(),
            mode_runtime=ModeRuntime(),
            authority=ProgressAuthority(),
        )
        sdk_tool = sdk_tool_for(registry.get("collect_resource"))
        runtime_context = RuntimeRunContext(
            agent_context=runtime.agent_context,
            weld_context=runtime.weld_context,
            profile=ModeRuntime().profile_for(LifecycleState.ACTIVE),
            trace=runtime.trace,
            runtime=runtime,
        )

        class Wrapper:
            pass

        wrapper = Wrapper()
        wrapper.context = runtime_context
        out = asyncio.run(sdk_tool.on_invoke_tool(wrapper, json.dumps({"item": "log", "count": 64})))

        self.assertEqual(out["reason"], "candidate_targets_exhausted")
        self.assertEqual(collect_calls, [{"item": "log", "count": 64}])
        self.assertFalse(
            any(
                event["event"].startswith("tool_continuation")
                for event in runtime.trace.snapshot()
            )
        )

    def test_sdk_tool_returns_ensure_resume_hint_without_hidden_continuation(self):
        body = FakeBody()
        registry = ToolRegistry()
        ensure_calls = []

        def ensure_callable(params):
            ensure_calls.append(dict(params))
            if len(ensure_calls) == 1:
                return ToolResult(
                    False,
                    "partial_budget_exhausted",
                    True,
                    metrics={
                        "item": "iron_pickaxe",
                        "target_count": 1,
                        "plan": [],
                        "completed_steps": [],
                        "resume_hint": "reinvoke_ensure",
                    },
                )
            return ToolResult(
                True,
                "ensured",
                False,
                metrics={
                    "item": "iron_pickaxe",
                    "target_count": 1,
                    "current_count": 1,
                    "plan": [],
                    "completed_steps": [],
                    "resume_hint": "complete",
                },
            )

        registry.register(
            RegisteredTool(
                name="ensure_tool_for",
                description="Ensure",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                callable=ensure_callable,
                sidecar=ToolSidecar(progress_key="ensure_tool_for", mutating=False, tool_type="resource"),
            )
        )
        runtime = AgentRuntime(
            body=body,
            registry=registry,
            agent_context=AgentContext(system_prompt="sys", goal_text="craft an iron pickaxe"),
            lifecycle=LifecycleController(),
            mode_runtime=ModeRuntime(),
            authority=ProgressAuthority(),
        )
        sdk_tool = sdk_tool_for(registry.get("ensure_tool_for"))
        runtime_context = RuntimeRunContext(
            agent_context=runtime.agent_context,
            weld_context=runtime.weld_context,
            profile=ModeRuntime().profile_for(LifecycleState.ACTIVE),
            trace=runtime.trace,
            runtime=runtime,
        )

        class Wrapper:
            pass

        wrapper = Wrapper()
        wrapper.context = runtime_context
        out = asyncio.run(sdk_tool.on_invoke_tool(wrapper, json.dumps({"resource": "iron_pickaxe"})))

        self.assertFalse(out["success"])
        self.assertEqual(out["reason"], "partial_budget_exhausted")
        self.assertEqual(ensure_calls, [{"resource": "iron_pickaxe"}])
        events = runtime.trace.snapshot()
        self.assertFalse(any(event["event"].startswith("tool_continuation") for event in events))

    def test_sdk_tool_converts_transport_exception_to_tool_result(self):
        def callable_(_params):
            raise RconError("RCON socket closed")

        tool = RegisteredTool(
            name="read_state",
            description="Read state",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            callable=callable_,
            sidecar=ToolSidecar(
                progress_key="read_state",
                mutating=False,
                permission="read_state",
                body_scope=("state",),
                terminal_truth=(),
            ),
        )
        sdk_tool = sdk_tool_for(tool)
        trace = RuntimeTrace()
        runtime_context = RuntimeRunContext(
            agent_context=AgentContext(system_prompt="sys", goal_text="collect"),
            weld_context=WeldContext(body=FakeBody(), authority=ProgressAuthority(), goal_text="collect"),
            profile=ModeRuntime().profile_for(LifecycleState.ACTIVE),
            trace=trace,
        )

        class Wrapper:
            context = runtime_context

        out = asyncio.run(sdk_tool.on_invoke_tool(Wrapper(), "{}"))

        self.assertFalse(out["success"])
        self.assertEqual(out["reason"], "transport_error")
        self.assertTrue(out["canRetry"])
        self.assertNotIn("metrics", out)
        self.assertEqual(out["summary"]["error_type"], "RconError")
        self.assertTrue(any(event["event"] == "tool_exception" and event["tool"] == "read_state" for event in trace.snapshot()))

    def test_sdk_tool_invalid_json_uses_same_model_projection_and_full_trace(self):
        def callable_(_params):
            raise AssertionError("invalid JSON should not call tool")

        tool = RegisteredTool(
            name="read_state",
            description="Read state",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            callable=callable_,
            sidecar=ToolSidecar(progress_key="read_state", mutating=False),
        )
        sdk_tool = sdk_tool_for(tool)
        trace = RuntimeTrace()
        runtime_context = RuntimeRunContext(
            agent_context=AgentContext(system_prompt="sys", goal_text="collect"),
            weld_context=WeldContext(body=FakeBody(), authority=ProgressAuthority(), goal_text="collect"),
            profile=ModeRuntime().profile_for(LifecycleState.ACTIVE),
            trace=trace,
        )

        class Wrapper:
            context = runtime_context

        out = asyncio.run(sdk_tool.on_invoke_tool(Wrapper(), "{bad json"))

        self.assertFalse(out["success"])
        self.assertEqual(out["reason"], "invalid_tool_json")
        self.assertIn("traceRef", out)
        self.assertNotIn("metrics", out)
        result_event = next(event for event in trace.snapshot() if event["event"] == "tool_result")
        self.assertEqual(result_event["full_result"]["reason"], "invalid_tool_json")
        self.assertEqual(result_event["model_result"], out)

    def test_sdk_tool_invalid_input_uses_same_model_projection_and_full_trace(self):
        def callable_(_params):
            raise AssertionError("invalid input should not call tool")

        tool = RegisteredTool(
            name="read_state",
            description="Read state",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            callable=callable_,
            sidecar=ToolSidecar(progress_key="read_state", mutating=False),
        )
        sdk_tool = sdk_tool_for(tool)
        trace = RuntimeTrace()
        runtime_context = RuntimeRunContext(
            agent_context=AgentContext(system_prompt="sys", goal_text="collect"),
            weld_context=WeldContext(body=FakeBody(), authority=ProgressAuthority(), goal_text="collect"),
            profile=ModeRuntime().profile_for(LifecycleState.ACTIVE),
            trace=trace,
        )

        class Wrapper:
            context = runtime_context

        out = asyncio.run(sdk_tool.on_invoke_tool(Wrapper(), "[]"))

        self.assertFalse(out["success"])
        self.assertEqual(out["reason"], "invalid_tool_input")
        self.assertIn("traceRef", out)
        self.assertNotIn("metrics", out)
        result_event = next(event for event in trace.snapshot() if event["event"] == "tool_result")
        self.assertEqual(result_event["full_result"]["reason"], "invalid_tool_input")
        self.assertEqual(result_event["model_result"], out)

    def test_sdk_tool_preempts_missing_body_result_into_recovery_required(self):
        def callable_(_params):
            return ToolResult(
                False,
                "missing_body",
                True,
                metrics={"final_pos": [0, 0, 0], "stopped_reason": "missing_body"},
            )

        tool = RegisteredTool(
            name="move_to",
            description="Move",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            callable=callable_,
            sidecar=ToolSidecar(progress_key="move_to", mutating=False, tool_type="navigation"),
        )
        sdk_tool = sdk_tool_for(tool)
        trace = RuntimeTrace()
        runtime_context = RuntimeRunContext(
            agent_context=AgentContext(system_prompt="sys", goal_text="collect"),
            weld_context=WeldContext(body=FakeBody(), authority=ProgressAuthority(), goal_text="collect"),
            profile=ModeRuntime().profile_for(LifecycleState.ACTIVE),
            trace=trace,
        )

        class Wrapper:
            context = runtime_context

        with self.assertRaises(BodyRecoveryRequired):
            asyncio.run(sdk_tool.on_invoke_tool(Wrapper(), "{}"))
        self.assertTrue(any(event["event"] == "tool_body_recovery_preempt" for event in trace.snapshot()))

    def test_sdk_tool_preempts_nested_missing_body_perception_failure(self):
        def callable_(_params):
            return ToolResult(
                False,
                "perception_failed",
                True,
                metrics={
                    "scope": "findBlocks",
                    "error": "missing_body",
                    "uncertainty": [{"reason": "missing_body"}],
                },
            )

        tool = RegisteredTool(
            name="search_for_block",
            description="Search",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            callable=callable_,
            sidecar=ToolSidecar(progress_key="search_for_block", mutating=False, tool_type="perception"),
        )
        sdk_tool = sdk_tool_for(tool)
        trace = RuntimeTrace()
        runtime_context = RuntimeRunContext(
            agent_context=AgentContext(system_prompt="sys", goal_text="collect"),
            weld_context=WeldContext(body=FakeBody(), authority=ProgressAuthority(), goal_text="collect"),
            profile=ModeRuntime().profile_for(LifecycleState.ACTIVE),
            trace=trace,
        )

        class Wrapper:
            context = runtime_context

        with self.assertRaises(BodyRecoveryRequired) as raised:
            asyncio.run(sdk_tool.on_invoke_tool(Wrapper(), "{}"))

        self.assertEqual(raised.exception.facts["tool"], "search_for_block")
        self.assertEqual(raised.exception.reason, "missing_body")
        self.assertEqual(raised.exception.facts["tool_result_reason"], "perception_failed")
        self.assertTrue(any(event["event"] == "tool_body_recovery_preempt" for event in trace.snapshot()))

    def test_sdk_tool_success_state_metrics_refresh_last_known_body_state(self):
        def callable_(_params):
            return ToolResult(
                True,
                "state_read",
                False,
                metrics={
                    "bot": "Bot",
                    "pos": [4.5, 70.0, -2.25],
                    "health": 13.0,
                    "food": 20,
                    "oxygen": 300,
                    "dimension": "overworld",
                    "inventory_hash": "inv2",
                    "missing": False,
                },
            )

        tool = RegisteredTool(
            name="read_state",
            description="Read",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            callable=callable_,
            sidecar=ToolSidecar(progress_key="read_state", mutating=False, tool_type="state"),
        )
        sdk_tool = sdk_tool_for(tool)
        runtime = AgentRuntime(
            body=FakeBody(),
            registry=ToolRegistry(),
            agent_context=AgentContext(system_prompt="sys", goal_text="collect"),
            lifecycle=LifecycleController(),
            mode_runtime=ModeRuntime(),
            authority=ProgressAuthority(),
        )
        runtime_context = RuntimeRunContext(
            agent_context=runtime.agent_context,
            weld_context=runtime.weld_context,
            profile=ModeRuntime().profile_for(LifecycleState.ACTIVE),
            trace=RuntimeTrace(),
            runtime=runtime,
        )

        class Wrapper:
            context = runtime_context

        asyncio.run(sdk_tool.on_invoke_tool(Wrapper(), "{}"))

        self.assertEqual(runtime.last_known_body_state["pos"], [4.5, 70.0, -2.25])
        self.assertEqual(runtime.last_known_body_state["health"], 13.0)
        self.assertEqual(runtime.last_known_body_state["oxygen"], 300)
        self.assertEqual(runtime.last_known_body_state["inventory_hash"], "inv2")

    def test_sdk_tool_preempts_body_missing_camel_case_event(self):
        def callable_(_params):
            return ToolResult(False, "perception_failed", True, metrics={"event": "bodyMissing"})

        tool = RegisteredTool(
            name="read_state",
            description="Read",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            callable=callable_,
            sidecar=ToolSidecar(progress_key="read_state", mutating=False, tool_type="perception"),
        )
        sdk_tool = sdk_tool_for(tool)
        runtime_context = RuntimeRunContext(
            agent_context=AgentContext(system_prompt="sys", goal_text="collect"),
            weld_context=WeldContext(body=FakeBody(), authority=ProgressAuthority(), goal_text="collect"),
            profile=ModeRuntime().profile_for(LifecycleState.ACTIVE),
            trace=RuntimeTrace(),
        )

        class Wrapper:
            context = runtime_context

        with self.assertRaises(BodyRecoveryRequired):
            asyncio.run(sdk_tool.on_invoke_tool(Wrapper(), "{}"))

    def test_sdk_tool_preserves_death_inventory_counts_for_recovery_driver(self):
        def callable_(_params):
            return ToolResult(
                False,
                "death",
                True,
                metrics={
                    "event": "death",
                    "inventory_counts_before": {"minecraft:oak_log": 8},
                    "inventory_hash": "dead-inventory",
                    "pos": [1, 64, 1],
                },
            )

        tool = RegisteredTool(
            name="mine_block_collect",
            description="Mine",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            callable=callable_,
            sidecar=ToolSidecar(progress_key="mine_block_collect", mutating=False, tool_type="work"),
        )
        sdk_tool = sdk_tool_for(tool)
        runtime_context = RuntimeRunContext(
            agent_context=AgentContext(system_prompt="sys", goal_text="collect"),
            weld_context=WeldContext(body=FakeBody(), authority=ProgressAuthority(), goal_text="collect"),
            profile=ModeRuntime().profile_for(LifecycleState.ACTIVE),
            trace=RuntimeTrace(),
        )

        class Wrapper:
            context = runtime_context

        with self.assertRaises(BodyRecoveryRequired) as raised:
            asyncio.run(sdk_tool.on_invoke_tool(Wrapper(), "{}"))

        self.assertEqual(raised.exception.facts["inventory_counts_before"], {"minecraft:oak_log": 8})
        self.assertEqual(raised.exception.facts["inventory_hash"], "dead-inventory")

    def test_tool_projection_uses_governance_and_preconditions_not_mode_hiding(self):
        body = FakeBody()
        sidecar = make_tool(body).sidecar
        normal = ModeRuntime().profile_for(LifecycleState.ACTIVE)
        modes = ModeRuntime()
        survival = modes.reduce([], LifecycleState.ACTIVE).profile

        self.assertTrue(tool_is_enabled(sidecar, normal, {}))
        self.assertTrue(tool_is_enabled(sidecar, survival, {}))
        self.assertFalse(tool_is_enabled(sidecar, normal, {"precondition_missing": True}))
        self.assertFalse(
            tool_is_enabled(
                sidecar,
                normal,
                {"governance": LegalityDecision(False, "protected_region", protected=True)},
            )
        )
        self.assertTrue(
            tool_is_enabled(
                sidecar,
                normal,
                {"governance": LegalityDecision(True, "allowed_natural")},
            )
        )

    def test_tool_projection_keeps_shared_pool_visible_across_modes(self):
        normal = ModeRuntime().profile_for(LifecycleState.ACTIVE)
        modes = ModeRuntime()
        engage = modes.reduce([AgentSignal.hostile_nearby("zombie")], LifecycleState.ACTIVE).profile
        combat_sidecar = ToolSidecar(
            "engage_entity",
            mutating=True,
            tool_type="combat",
            permission="combat",
            body_scope=("combat", "nearby_entities"),
        )
        hostile_read_sidecar = ToolSidecar(
            "find_hostiles",
            mutating=False,
            tool_type="perception",
            permission="read_world",
            body_scope=("nearby_entities",),
        )
        block_read_sidecar = ToolSidecar(
            "search_for_block",
            mutating=False,
            tool_type="perception",
            permission="read_world",
            body_scope=("blocks",),
        )

        self.assertTrue(tool_is_enabled(combat_sidecar, normal, {}))
        self.assertTrue(tool_is_enabled(hostile_read_sidecar, normal, {}))
        self.assertTrue(tool_is_enabled(block_read_sidecar, normal, {}))
        self.assertTrue(tool_is_enabled(combat_sidecar, engage, {}))
        self.assertTrue(tool_is_enabled(hostile_read_sidecar, engage, {}))

    def test_run_turn_enters_active_once_and_preserves_active_on_second_turn(self):
        body = FakeBody()
        registry = ToolRegistry()
        calls = []

        async def fake_runner(agent, input_text, *, context=None, max_turns=None, run_config=None, **kwargs):
            calls.append((context.instruction_preamble, context.profile.situational, max_turns, run_config))
            return {"ok": True}

        runtime = AgentRuntime(
            body=body,
            registry=registry,
            agent_context=AgentContext(system_prompt="sys", goal_text="collect"),
            lifecycle=LifecycleController(),
            mode_runtime=ModeRuntime(),
            authority=ProgressAuthority(),
            runner_run=fake_runner,
        )

        first = asyncio.run(runtime.run_turn())
        second = asyncio.run(runtime.run_turn())

        self.assertEqual(first.status, "completed_turn")
        self.assertEqual(second.status, "completed_turn")
        self.assertEqual(runtime.lifecycle.state, LifecycleState.ACTIVE)
        self.assertEqual(
            [state.value for state in runtime.lifecycle.history],
            ["init", "idle", "active"],
        )
        self.assertEqual(len(calls), 2)
        self.assertIn("PROFILE:", calls[0][0])
        self.assertIsNone(calls[0][2])

    def test_sdk_max_turns_exceeded_yields_instead_of_failing(self):
        body = FakeBody()
        registry = ToolRegistry()

        async def quota_runner(*args, **kwargs):
            raise MaxTurnsExceeded("runaway guard hit")

        runtime = AgentRuntime(
            body=body,
            registry=registry,
            agent_context=AgentContext(system_prompt="sys", goal_text="collect"),
            lifecycle=LifecycleController(),
            mode_runtime=ModeRuntime(),
            authority=ProgressAuthority(),
            runner_run=quota_runner,
            max_turns=999,
        )

        outcome = asyncio.run(runtime.run_turn())

        self.assertEqual(outcome.status, "yielded")
        self.assertEqual(outcome.lifecycle, LifecycleState.YIELDED)
        self.assertIn("GOAL: collect", outcome.message)
        events = runtime.trace.snapshot()
        self.assertTrue(any(event["event"] == "runaway_ceiling_hit" for event in events))
        self.assertTrue(any(event["event"] == "runaway_ceiling_yielded" for event in events))

    def test_tool_facts_and_trace_are_projected_into_sdk_tool(self):
        body = FakeBody()
        registry = ToolRegistry()
        registry.register(make_tool(body))
        trace = RuntimeTrace()
        runtime = AgentRuntime(
            body=body,
            registry=registry,
            agent_context=AgentContext(system_prompt="sys", goal_text="collect"),
            lifecycle=LifecycleController(),
            mode_runtime=ModeRuntime(),
            authority=ProgressAuthority(),
            runner_run=lambda *args, **kwargs: None,
            tool_facts={"move_step": {"precondition_missing": True}},
            trace=trace,
        )
        sdk_tool = next(tool for tool in runtime.agent.tools if tool.name == "move_step")
        context = RuntimeRunContext(
            agent_context=runtime.agent_context,
            weld_context=runtime.weld_context,
            profile=ModeRuntime().profile_for(LifecycleState.ACTIVE),
            tool_facts=runtime.tool_facts,
            trace=trace,
        )

        class Wrapper:
            def __init__(self, context):
                self.context = context

        wrapper = Wrapper(context)
        self.assertFalse(sdk_tool.is_enabled(wrapper, runtime.agent))
        runtime.set_tool_facts("move_step", {})
        context.tool_facts = runtime.tool_facts
        self.assertTrue(sdk_tool.is_enabled(wrapper, runtime.agent))
        out = asyncio.run(sdk_tool.on_invoke_tool(wrapper, json.dumps({"dx": 1})))

        self.assertTrue(out["success"])
        events = trace.snapshot()
        self.assertTrue(any(event["event"] == "tool_enabled" and event["enabled"] is False for event in events))
        self.assertTrue(
            any(
                event["event"] == "tool_invoke"
                and event["tool"] == "move_step"
                and event["source"] == "unknown"
                and event["tool_type"] == "general"
                and event["permission"] == "move"
                and event["body_scope"] == ["navigation"]
                and event["terminal_truth"] == ["position"]
                and event["arguments_summary"] == '{"dx": 1}'
                for event in events
            )
        )
        self.assertTrue(any(event["event"] == "tool_result" and event["reason"] == "completed" for event in events))

    def test_progress_abort_from_runner_becomes_lifecycle_yield(self):
        body = FakeBody()
        registry = ToolRegistry()
        authority = ProgressAuthority()
        fp = authority.fingerprint(body.get_state())
        for i in range(5):
            authority.note_step(("action", i), success=False, fingerprint=fp)
        facts = authority.facts("collect")

        async def fake_runner(*args, **kwargs):
            from minebot.contract import ProgressAbort

            raise ProgressAbort("yield", facts=facts)

        runtime = AgentRuntime(
            body=body,
            registry=registry,
            agent_context=AgentContext(system_prompt="sys", goal_text="collect"),
            lifecycle=LifecycleController(),
            mode_runtime=ModeRuntime(),
            authority=authority,
            runner_run=fake_runner,
        )

        outcome = asyncio.run(runtime.run_turn())

        self.assertEqual(outcome.status, "yielded")
        self.assertEqual(outcome.lifecycle, LifecycleState.YIELDED)
        self.assertEqual(outcome.profile.lifecycle, "yielded")
        self.assertIs(outcome.yielded_facts, facts)
        self.assertIn("How should I continue?", outcome.message)

    def test_progress_abort_wrapped_by_sdk_user_error_becomes_lifecycle_yield(self):
        body = FakeBody()
        registry = ToolRegistry()
        authority = ProgressAuthority()
        fp = authority.fingerprint(body.get_state())
        for i in range(5):
            authority.note_step(("action", i), success=False, fingerprint=fp)
        facts = authority.facts("collect")

        async def fake_runner(*args, **kwargs):
            from minebot.contract import ProgressAbort

            try:
                raise ProgressAbort("yield", facts=facts)
            except ProgressAbort as exc:
                raise UserError("Error running tool mine_block_collect: progress authority yielded") from exc

        runtime = AgentRuntime(
            body=body,
            registry=registry,
            agent_context=AgentContext(system_prompt="sys", goal_text="collect"),
            lifecycle=LifecycleController(),
            mode_runtime=ModeRuntime(),
            authority=authority,
            runner_run=fake_runner,
        )

        outcome = asyncio.run(runtime.run_turn())

        self.assertEqual(outcome.status, "yielded")
        self.assertEqual(outcome.lifecycle, LifecycleState.YIELDED)
        self.assertIs(outcome.yielded_facts, facts)

    def test_body_recovery_required_from_runner_enters_recovering(self):
        body = FakeBody()

        async def fake_runner(*args, **kwargs):
            raise BodyRecoveryRequired("missing_body", facts={"tool": "move_to"})

        runtime = AgentRuntime(
            body=body,
            registry=ToolRegistry(),
            agent_context=AgentContext(system_prompt="sys", goal_text="collect"),
            lifecycle=LifecycleController(),
            mode_runtime=ModeRuntime(),
            authority=ProgressAuthority(),
            runner_run=fake_runner,
        )

        outcome = asyncio.run(runtime.run_turn())

        self.assertEqual(outcome.status, "stopped")
        self.assertEqual(outcome.lifecycle, LifecycleState.RECOVERING)
        self.assertEqual(outcome.profile.situational, "death")
        self.assertTrue(any(event["event"] == "body_recovery_required" for event in runtime.trace.snapshot()))

    def test_repeated_tool_transport_errors_yield_without_death_recovery(self):
        body = FakeBody()
        registry = ToolRegistry()

        def broken_tool(_params):
            raise RconError("RCON socket closed")

        registry.register(
            RegisteredTool(
                "read_state",
                "Read state",
                {"type": "object", "properties": {}, "additionalProperties": False},
                broken_tool,
                ToolSidecar("read_state", mutating=False, permission="read_state", body_scope=("state",)),
            )
        )

        async def fake_runner(agent, input_text, *, context=None, **kwargs):
            tool = next(tool for tool in agent.tools if tool.name == "read_state")

            class Wrapper:
                def __init__(self, context):
                    self.context = context

            for _ in range(3):
                await tool.on_invoke_tool(Wrapper(context), "{}")

        runtime = AgentRuntime(
            body=body,
            registry=registry,
            agent_context=AgentContext(system_prompt="sys", goal_text="collect"),
            lifecycle=LifecycleController(),
            mode_runtime=ModeRuntime(),
            authority=ProgressAuthority(),
            runner_run=fake_runner,
        )

        outcome = asyncio.run(runtime.run_turn())

        self.assertEqual(outcome.status, "yielded")
        self.assertEqual(outcome.lifecycle, LifecycleState.YIELDED)
        self.assertIn("body_transport_unstable", outcome.message)
        self.assertIn("body_transport_unstable", outcome.yielded_facts.recent_events[-1])
        trace = runtime.trace.snapshot()
        self.assertEqual(len([event for event in trace if event["event"] == "body_transport_error"]), 3)
        self.assertFalse(any(event["event"] == "body_recovery_required" for event in trace))
        self.assertTrue(any(event["event"] == "progress_yielded" for event in trace))

    def test_body_recovery_required_wrapped_by_sdk_user_error_enters_recovering(self):
        body = FakeBody()

        async def fake_runner(*args, **kwargs):
            try:
                raise BodyRecoveryRequired("death", facts={"tool": "mine_block_collect", "inventory_counts_before": {"oak_log": 3}})
            except BodyRecoveryRequired as exc:
                raise UserError("Error running tool mine_block_collect: body recovery required") from exc

        runtime = AgentRuntime(
            body=body,
            registry=ToolRegistry(),
            agent_context=AgentContext(system_prompt="sys", goal_text="collect"),
            lifecycle=LifecycleController(),
            mode_runtime=ModeRuntime(),
            authority=ProgressAuthority(),
            runner_run=fake_runner,
        )

        outcome = asyncio.run(runtime.run_turn())

        self.assertEqual(outcome.status, "stopped")
        self.assertEqual(outcome.lifecycle, LifecycleState.RECOVERING)
        self.assertEqual(outcome.profile.situational, "death")
        event = next(event for event in runtime.trace.snapshot() if event["event"] == "body_recovery_required")
        self.assertEqual(event["reason"], "death")
        self.assertEqual(event["facts"]["inventory_counts_before"], {"oak_log": 3})

    def test_missing_body_state_enters_recovering_before_model_call(self):
        body = FakeBody()

        def missing_state():
            return missing_body_state()

        body.get_state = missing_state  # type: ignore[method-assign]

        async def fake_runner(*args, **kwargs):
            raise AssertionError("model runner should not be called while body is missing")

        runtime = AgentRuntime(
            body=body,
            registry=ToolRegistry(),
            agent_context=AgentContext(system_prompt="sys", goal_text="collect"),
            lifecycle=LifecycleController(),
            mode_runtime=ModeRuntime(),
            authority=ProgressAuthority(),
            runner_run=fake_runner,
        )

        outcome = asyncio.run(runtime.run_turn())

        self.assertEqual(outcome.status, "stopped")
        self.assertEqual(outcome.lifecycle, LifecycleState.RECOVERING)
        self.assertEqual(outcome.profile.situational, "death")
        self.assertTrue(any(event["event"] == "turn_stopped" for event in runtime.trace.snapshot()))

    def test_recovery_resume_consumes_suspend_slot_and_injects_resume_context_once(self):
        body = FakeBody()
        registry = ToolRegistry()
        calls = []

        async def fake_runner(agent, input_text, *, context=None, **kwargs):
            calls.append(context.instruction_preamble)
            return {"ok": True}

        modes = ModeRuntime()
        lifecycle = LifecycleController()
        runtime = AgentRuntime(
            body=body,
            registry=registry,
            agent_context=AgentContext(system_prompt="sys", goal_text="collect 64 dirt"),
            lifecycle=lifecycle,
            mode_runtime=modes,
            authority=ProgressAuthority(),
            runner_run=fake_runner,
        )

        first = asyncio.run(runtime.run_turn(extra_signals=[AgentSignal.death_detected("death", composition_id="c1")]))
        self.assertEqual(first.status, "stopped")
        self.assertEqual(runtime.lifecycle.state, LifecycleState.RECOVERING)
        self.assertIsNotNone(modes.suspend_slot)

        second = asyncio.run(runtime.run_turn(extra_signals=[AgentSignal.recovery_completed("respawned")]))
        self.assertEqual(second.status, "stopped")
        self.assertEqual(runtime.lifecycle.state, LifecycleState.RESUMING)

        third = asyncio.run(runtime.run_turn())
        fourth = asyncio.run(runtime.run_turn())

        self.assertEqual(third.status, "completed_turn")
        self.assertEqual(fourth.status, "completed_turn")
        self.assertEqual(runtime.lifecycle.state, LifecycleState.ACTIVE)
        self.assertIn("RESUME: reason=death", calls[0])
        self.assertNotIn("RESUME: reason=death", calls[1])
        self.assertIsNone(modes.suspend_slot)
        self.assertTrue(any(event["event"] == "resume_context" for event in runtime.trace.snapshot()))


if __name__ == "__main__":
    unittest.main()
