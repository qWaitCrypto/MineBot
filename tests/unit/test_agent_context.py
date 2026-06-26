import asyncio
import unittest

from minebot.app.runner import AgentRuntime
from minebot.app.session import AgentSession, SessionCommand
from minebot.app.wiring import AgentRuntimeParts
from minebot.brain.context import AgentContext
from minebot.brain.lifecycle import LifecycleController
from minebot.brain.modes import ModeRuntime
from minebot.brain.progress import ProgressAuthority
from minebot.brain.registry import ToolRegistry

from tests.unit.test_agent_runner_spine import FakeBody


class AgentContextTests(unittest.TestCase):
    def test_goal_is_visible_on_every_turn_independent_of_cadence(self):
        ctx = AgentContext(system_prompt="sys", goal_text="collect 64 logs", goal_reinject_every=99)

        for _ in range(4):
            ctx.begin_turn()
            self.assertIn("GOAL: collect 64 logs", ctx.turn_preamble())

    def test_session_window_keeps_recent_user_and_assistant_messages(self):
        ctx = AgentContext(
            system_prompt="sys",
            goal_text="collect",
            language="Chinese",
            max_session_messages=3,
        )

        ctx.observe_user_message("  collect 64 logs  ")
        ctx.observe_assistant_message("I will start.")
        ctx.observe_user_message("pause please")
        ctx.observe_user_message("continue")
        ctx.begin_turn()
        preamble = ctx.turn_preamble()

        self.assertIn("SESSION: turn=1 language=Chinese", preamble)
        self.assertIn("SESSION_MESSAGES:", preamble)
        self.assertNotIn("collect 64 logs", [message for _, message in ctx.session_messages()])
        self.assertEqual(
            ctx.session_messages(),
            [
                ("assistant", "I will start."),
                ("user", "pause please"),
                ("user", "continue"),
            ],
        )

    def test_set_goal_resets_turn_and_keeps_new_goal_visible(self):
        ctx = AgentContext(system_prompt="sys", goal_text="old")
        ctx.begin_turn()
        ctx.begin_turn()
        ctx.set_goal("new goal")

        self.assertIn("GOAL: new goal", ctx.turn_preamble())
        self.assertNotIn("GOAL: old", ctx.turn_preamble())


def build_parts(goal: str, calls: list[str]) -> AgentRuntimeParts:
    body = FakeBody()

    async def fake_runner(agent, input_text, *, context=None, **kwargs):
        calls.append(input_text)
        return {"ok": True}

    context = AgentContext(system_prompt="sys", goal_text=goal, language="Chinese")
    lifecycle = LifecycleController()
    modes = ModeRuntime()
    authority = ProgressAuthority()
    registry = ToolRegistry()
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


def build_speaking_parts(goal: str, calls: list[str]) -> AgentRuntimeParts:
    body = FakeBody()

    class SpeechRunResult:
        final_output = "I will continue collecting logs."

        def to_input_list(self):
            return []

    async def fake_runner(agent, input_text, *, context=None, **kwargs):
        calls.append(input_text)
        return SpeechRunResult()

    context = AgentContext(system_prompt="sys", goal_text=goal, language="Chinese")
    lifecycle = LifecycleController()
    modes = ModeRuntime()
    authority = ProgressAuthority()
    registry = ToolRegistry()
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


class AgentContextRuntimeTests(unittest.TestCase):
    def test_runner_advances_context_turn_once_per_outer_turn(self):
        calls: list[str] = []
        parts = build_parts("collect 64 logs", calls)

        asyncio.run(parts.runtime.run_turn())
        asyncio.run(parts.runtime.run_turn())

        self.assertEqual(parts.context._turn, 2)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all("GOAL: collect 64 logs" in call for call in calls))

    def test_session_messages_reach_next_model_turn_context(self):
        calls: list[str] = []
        session = AgentSession(lambda goal: build_parts(goal, calls))

        session.submit(SessionCommand.start("collect 64 logs"))
        asyncio.run(session.step())
        session.submit(SessionCommand.message("actually prefer oak logs"))
        asyncio.run(session.step())

        self.assertTrue(any("SESSION_MESSAGES:" in call for call in calls))
        self.assertTrue(any("actually prefer oak logs" in call for call in calls))
        self.assertTrue(all("GOAL: collect 64 logs" in call for call in calls))

    def test_assistant_visible_output_reaches_next_model_turn_context(self):
        calls: list[str] = []
        parts = build_speaking_parts("collect 64 logs", calls)

        asyncio.run(parts.runtime.run_turn())
        asyncio.run(parts.runtime.run_turn())

        self.assertIn("I will continue collecting logs.", calls[1])


if __name__ == "__main__":
    unittest.main()
