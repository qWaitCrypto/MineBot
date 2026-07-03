import asyncio
import json
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
from minebot.contract import BodyState, LegalityDecision, PerceptionResult, Result, ToolResult
from minebot.game.errors import RconError


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

        self.assertIn(("assistant", "I am starting with nearby logs."), context.session_messages())
        self.assertIn(("assistant", "I found the first tree."), context.session_messages())

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

    def test_tool_projection_keeps_proactive_combat_out_of_resource_focus(self):
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

        self.assertFalse(tool_is_enabled(combat_sidecar, normal, {}))
        self.assertFalse(tool_is_enabled(hostile_read_sidecar, normal, {}))
        self.assertTrue(tool_is_enabled(block_read_sidecar, normal, {}))
        self.assertTrue(tool_is_enabled(combat_sidecar, engage, {}))
        self.assertTrue(tool_is_enabled(hostile_read_sidecar, engage, {}))

    def test_run_turn_enters_active_once_and_preserves_active_on_second_turn(self):
        body = FakeBody()
        registry = ToolRegistry()
        calls = []

        async def fake_runner(agent, input_text, *, context=None, max_turns=None, run_config=None, **kwargs):
            calls.append((input_text, context.profile.situational, max_turns, run_config))
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
            calls.append(input_text)
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
