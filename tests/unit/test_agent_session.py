import asyncio
import unittest

from agents.exceptions import MaxTurnsExceeded

from minebot.app.runner import AgentRuntime, RecoveryOutcome
from minebot.app.session import AgentSession, SessionCommand, SessionStep
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

    def test_goal_driver_can_handle_collect_goal_before_model_tool_choice(self):
        calls: list[str] = []
        bodies: list[FakeBody] = []
        driven_goals: list[str] = []

        def driver(parts: AgentRuntimeParts, _signals) -> SessionStep | None:
            driven_goals.append(parts.context.goal_text)
            return SessionStep("completed_turn", parts.lifecycle.state, "driver_handled")

        session = AgentSession(lambda goal: build_parts(goal, calls, bodies), goal_driver=driver)
        session.submit(SessionCommand.start("collect 64 logs"))

        first = asyncio.run(session.step())
        second = asyncio.run(session.step())

        self.assertEqual(first.status, "completed_turn")
        self.assertEqual(first.message, "driver_handled")
        self.assertEqual(driven_goals, ["collect 64 logs"])
        self.assertEqual(len(calls), 1)
        self.assertIn("GOAL: collect 64 logs", calls[0])
        self.assertEqual(second.status, "completed_turn")

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

    def test_goal_driver_retries_after_preflight_recovery(self):
        driver_calls: list[tuple[str, str]] = []
        model_calls: list[str] = []

        def parts_factory(goal: str) -> AgentRuntimeParts:
            body = FakeBody()

            async def fake_runner(agent, input_text, *, context=None, **kwargs):
                model_calls.append(input_text)
                return {"ok": True}

            def recover(_runtime: AgentRuntime) -> RecoveryOutcome:
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

        def driver(parts: AgentRuntimeParts, signals) -> SessionStep | None:
            driver_calls.append((parts.context.goal_text, parts.lifecycle.state.value))
            if len(driver_calls) == 1:
                parts.lifecycle.ready()
                parts.lifecycle.start()
                parts.lifecycle.enter_recovery()
                return SessionStep("stopped", parts.lifecycle.state, "death_detected")
            if parts.lifecycle.state is LifecycleState.RECOVERING:
                parts.lifecycle.resume()
                return SessionStep("stopped", parts.lifecycle.state, "respawned")
            if parts.lifecycle.state is LifecycleState.RESUMING:
                parts.lifecycle.reenter_active()
            return SessionStep("completed_turn", parts.lifecycle.state, "driver_handled")

        session = AgentSession(parts_factory, goal_driver=driver)
        session.submit(SessionCommand.start("collect 64 logs"))

        first = asyncio.run(session.step())
        second = asyncio.run(session.step())
        third = asyncio.run(session.step())
        fourth = asyncio.run(session.step())

        self.assertEqual(first.lifecycle, LifecycleState.RECOVERING)
        self.assertEqual(second.lifecycle, LifecycleState.RESUMING)
        self.assertEqual(third.message, "driver_handled")
        self.assertEqual(third.lifecycle, LifecycleState.ACTIVE)
        self.assertEqual(fourth.status, "completed_turn")
        self.assertEqual(
            driver_calls,
            [("collect 64 logs", "init"), ("collect 64 logs", "recovering"), ("collect 64 logs", "resuming")],
        )
        self.assertEqual(len(model_calls), 1)

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


if __name__ == "__main__":
    unittest.main()
