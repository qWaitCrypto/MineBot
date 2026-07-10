import asyncio
import unittest

from minebot.app.conversation import (
    CONVERSATION_WINDOW_TURNS,
    WindowedConversationSession,
    bounded_session_input,
)
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

    def test_pending_turn_input_is_acknowledged_by_snapshot(self):
        ctx = AgentContext(system_prompt="sys", goal_text="")
        ctx.observe_user_message("hello")
        ctx.observe_system_message("previous goal completed")

        turn_input, count = ctx.pending_turn_input(fallback="fallback")
        ctx.observe_user_message("newer message")
        ctx.acknowledge_turn_input(count)

        self.assertIn("USER_MESSAGE: hello", turn_input)
        self.assertIn("HARNESS_FACT: previous goal completed", turn_input)
        self.assertEqual(
            ctx.pending_turn_input(fallback="fallback"),
            ("newer message", 1),
        )

    def test_pending_turn_input_never_uses_history_window_as_a_drop_limit(self):
        ctx = AgentContext(system_prompt="sys", goal_text="", max_session_messages=2)
        for index in range(5):
            ctx.observe_user_message(f"message-{index}")

        turn_input, count = ctx.pending_turn_input(fallback="fallback")

        self.assertEqual(count, 5)
        self.assertIn("USER_MESSAGE: message-0", turn_input)
        self.assertIn("USER_MESSAGE: message-4", turn_input)
        self.assertEqual(
            ctx.session_messages(),
            [("user", "message-3"), ("user", "message-4")],
        )

    def test_minecraft_sender_is_preserved_as_structured_sdk_input(self):
        ctx = AgentContext(system_prompt="sys", goal_text="")

        ctx.observe_user_message("  follow me  ", sender=" Steve\n ")
        turn_input, count = ctx.pending_turn_input(fallback="fallback")

        self.assertEqual(count, 1)
        self.assertTrue(turn_input.startswith("MINECRAFT_CHAT: "))
        self.assertIn('"sender": "Steve"', turn_input)
        self.assertIn('"message": "follow me"', turn_input)
        self.assertEqual(ctx.session_messages(), [("user", "Steve: follow me")])

    def test_sdk_session_window_drops_only_complete_old_turns(self):
        history = []
        for turn in range(CONVERSATION_WINDOW_TURNS + 2):
            history.extend(
                [
                    {"role": "user", "content": f"turn-{turn}"},
                    {"type": "function_call", "call_id": f"call-{turn}"},
                    {"type": "function_call_output", "call_id": f"call-{turn}"},
                    {"role": "assistant", "content": f"done-{turn}"},
                ]
            )

        bounded = bounded_session_input(history, [])

        self.assertEqual(bounded[0]["content"], "turn-2")
        self.assertEqual(len([item for item in bounded if item.get("role") == "user"]), CONVERSATION_WINDOW_TURNS)
        self.assertEqual(
            {item["call_id"] for item in bounded if item.get("type") == "function_call"},
            {item["call_id"] for item in bounded if item.get("type") == "function_call_output"},
        )

    def test_windowed_sdk_session_storage_is_bounded_by_complete_turn(self):
        session = WindowedConversationSession(max_turns=2)

        async def scenario():
            for turn in range(3):
                await session.add_items(
                    [
                        {"role": "user", "content": f"turn-{turn}"},
                        {"type": "function_call", "call_id": f"call-{turn}"},
                        {"type": "function_call_output", "call_id": f"call-{turn}"},
                        {"role": "assistant", "content": f"done-{turn}"},
                    ]
                )
            return await session.get_items()

        items = asyncio.run(scenario())

        self.assertEqual(items[0]["content"], "turn-1")
        self.assertEqual(items[-1]["content"], "done-2")
        self.assertEqual(len(items), 8)


def build_parts(goal: str, calls: list[str]) -> AgentRuntimeParts:
    body = FakeBody()

    async def fake_runner(agent, input_text, *, context=None, **kwargs):
        calls.append(f"{context.instruction_preamble}\nINPUT: {input_text}")
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
        calls.append(f"{context.instruction_preamble}\nINPUT: {input_text}")
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
    def test_runtime_reuses_one_sdk_session_and_separates_input_from_instructions(self):
        body = FakeBody()
        calls = []

        async def fake_runner(agent, input_text, *, context=None, session=None, **kwargs):
            calls.append((input_text, context.instruction_preamble, session))
            return {"ok": True}

        context = AgentContext(system_prompt="sys", goal_text="collect 64 logs")
        context.observe_user_message("start collecting")
        runtime = AgentRuntime(
            body=body,
            registry=ToolRegistry(),
            agent_context=context,
            lifecycle=LifecycleController(),
            mode_runtime=ModeRuntime(),
            authority=ProgressAuthority(),
            runner_run=fake_runner,
        )

        asyncio.run(runtime.run_turn())
        context.observe_user_message("use oak if possible")
        asyncio.run(runtime.run_turn())

        self.assertEqual(calls[0][0], "start collecting")
        self.assertEqual(calls[1][0], "use oak if possible")
        self.assertIn("GOAL: collect 64 logs", calls[0][1])
        self.assertNotIn("SESSION_MESSAGES:", calls[0][1])
        self.assertIs(calls[0][2], calls[1][2])
        self.assertIs(calls[0][2], runtime.conversation_session)

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

        self.assertNotIn("SESSION_MESSAGES:", calls[1])
        self.assertIn("INPUT: actually prefer oak logs", calls[1])
        self.assertIn(("user", "actually prefer oak logs"), session.parts.context.session_messages())
        self.assertTrue(all("GOAL: collect 64 logs" in call for call in calls))

    def test_assistant_visible_output_reaches_next_model_turn_context(self):
        calls: list[str] = []
        parts = build_speaking_parts("collect 64 logs", calls)

        asyncio.run(parts.runtime.run_turn())
        asyncio.run(parts.runtime.run_turn())

        self.assertNotIn("I will continue collecting logs.", calls[1])
        self.assertIn(
            ("assistant", "I will continue collecting logs."),
            parts.context.session_messages(),
        )


if __name__ == "__main__":
    unittest.main()
