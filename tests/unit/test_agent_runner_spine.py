import asyncio
import json
import unittest

from minebot.app.runner import AgentRuntime, RuntimeRunContext, sdk_tool_for
from minebot.brain.context import AgentContext
from minebot.brain.lifecycle import LifecycleController, LifecycleState
from minebot.brain.modes import ModeRuntime
from minebot.brain.progress import ProgressAuthority
from minebot.brain.registry import RegisteredTool, ToolRegistry, ToolSidecar, WeldContext
from minebot.contract import BodyState, PerceptionResult, Result, ToolResult


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


class FakeBody:
    bot_name = "Bot"

    def __init__(self):
        self.x = 0.0
        self.events = []

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
        return Result(None, self.bot_name, "result", True, True, True)


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
        sidecar=ToolSidecar(progress_key="move_step", mutating=mutating, timeout_s=2.0),
    )


class AgentRunnerSpineTests(unittest.TestCase):
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
        self.assertEqual(body.x, 2.0)
        self.assertIsNotNone(authority.last_action)
        self.assertIsNone(runtime_context.weld_context.writer.holder)
        self.assertIsNone(sdk_tool._failure_error_function)
        self.assertFalse(sdk_tool._use_default_failure_error_function)

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
            max_turns=3,
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


if __name__ == "__main__":
    unittest.main()
