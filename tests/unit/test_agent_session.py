import asyncio
import unittest

from agents.exceptions import MaxTurnsExceeded

from minebot.app.runner import AgentRuntime
from minebot.app.session import AgentSession, SessionCommand
from minebot.app.wiring import AgentRuntimeParts
from minebot.brain.context import AgentContext
from minebot.brain.lifecycle import LifecycleController, LifecycleState
from minebot.brain.modes import ModeRuntime
from minebot.brain.progress import ProgressAuthority
from minebot.brain.registry import ToolRegistry

from tests.unit.test_agent_runner_spine import FakeBody


def build_parts(goal: str, calls: list[str], bodies: list[FakeBody]) -> AgentRuntimeParts:
    body = FakeBody()
    bodies.append(body)

    async def fake_runner(agent, input_text, *, context=None, **kwargs):
        calls.append(input_text)
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


class AgentSessionTests(unittest.TestCase):
    def test_start_persists_runtime_across_steps_and_records_user_message(self):
        calls: list[str] = []
        bodies: list[FakeBody] = []
        session = AgentSession(lambda goal: build_parts(goal, calls, bodies))

        session.submit(SessionCommand.start("collect 64 logs"))
        first = asyncio.run(session.step())
        second = asyncio.run(session.step())

        self.assertEqual(first.status, "completed_turn")
        self.assertEqual(second.status, "completed_turn")
        self.assertEqual(len(bodies), 1)
        self.assertEqual(session.current_goal, "collect 64 logs")
        self.assertEqual(session.lifecycle_state, LifecycleState.ACTIVE)
        self.assertEqual([state.value for state in session.parts.lifecycle.history], ["init", "idle", "active"])
        self.assertTrue(any(event["event"] == "user_message" and event["command"] == "start" for event in session.parts.runtime.trace.snapshot()))
        self.assertTrue(any("GOAL: collect 64 logs" in call for call in calls))

    def test_run_until_waiting_stops_when_terminal_truth_predicate_passes(self):
        calls: list[str] = []
        bodies: list[FakeBody] = []
        session = AgentSession(lambda goal: build_parts(goal, calls, bodies))
        session.submit(SessionCommand.start("collect 64 logs"))

        final = asyncio.run(session.run_until_waiting(max_steps=10, should_stop=lambda _step: len(calls) >= 2))

        self.assertEqual(final.status, "completed_turn")
        self.assertEqual(len(calls), 2)

    def test_pause_while_active_interrupts_body_and_yields_lifecycle(self):
        calls: list[str] = []
        bodies: list[FakeBody] = []
        session = AgentSession(lambda goal: build_parts(goal, calls, bodies))
        session.submit(SessionCommand.start("collect 64 logs"))
        asyncio.run(session.step())

        session.submit(SessionCommand.pause("user_said_wait"))
        self.assertEqual(bodies[0].interrupt_reasons, ["user_said_wait"])
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

    def test_replace_goal_updates_context_and_invalidates_generation_without_rebuilding(self):
        calls: list[str] = []
        bodies: list[FakeBody] = []
        session = AgentSession(lambda goal: build_parts(goal, calls, bodies))
        session.submit(SessionCommand.start("collect 64 logs"))
        asyncio.run(session.step())
        before_generation = session.parts.authority._generation

        session.submit(SessionCommand.replace_goal("collect 64 sand"))
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
        self.assertEqual(bodies[0].interrupt_reasons, ["stop_now"])
        cancelled = asyncio.run(session.step())

        self.assertEqual(cancelled.status, "waiting")
        self.assertEqual(cancelled.lifecycle, LifecycleState.IDLE)
        self.assertEqual(session.lifecycle_state, LifecycleState.IDLE)
        self.assertEqual(bodies[0].interrupt_reasons, ["stop_now"])

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
        self.assertTrue(
            any(event["event"] == "session_goal_completed" for event in session.parts.runtime.trace.snapshot())
        )

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


if __name__ == "__main__":
    unittest.main()
