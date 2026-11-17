"""F1 identity-activation wiring: per-turn classification telemetry + routing.

brain-cognitive-framework.md §4 (P1 construction). With no routing policy
the runtime must behave exactly as before (primary model, full context
profile) while emitting the ``turn_decision_context`` trace event; a
provided policy must change model selection and the compiled context
profile for the classified class only.
"""

from __future__ import annotations

import asyncio
import unittest

from minebot.brain.context import AgentContext
from minebot.brain.deliberation import DecisionContext, RouteSpec
from minebot.brain.lifecycle import LifecycleController
from minebot.brain.modes import AgentSignal, ModeRuntime
from minebot.brain.progress import ProgressAuthority
from minebot.brain.registry import ToolRegistry
from minebot.contract import BodyState, PerceptionResult, Result
from minebot.app.runner import AgentRuntime


class _FakeBody:
    bot_name = "Bot"

    def __init__(self) -> None:
        self.events: list[object] = []

    def get_state(self) -> BodyState:
        return BodyState(
            bot="Bot",
            pos=(0.0, 64.0, 0.0),
            yaw=None,
            pitch=None,
            health=20.0,
            food=20,
            oxygen=300,
            inventory_raw="[]",
            inventory_hash="0",
            effects=None,
            time=1000,
            weather=None,
            dimension="overworld",
            complete=True,
        )

    def perceive(self, scope, params):
        return PerceptionResult(self.bot_name, scope, "perception", True, True, {})

    def poll_events(self):
        return []

    def interrupt(self, reason=None):
        return Result(None, self.bot_name, "result", True, True, True)


class _EmptyRunResult:
    final_output = ""
    new_items: list[object] = []

    def to_input_list(self) -> list[object]:
        return []


class _CapturingRunner:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def __call__(self, agent, *args, **kwargs):
        self.calls.append({"agent": agent, "kwargs": kwargs})
        return _EmptyRunResult()

    @property
    def last_agent(self):
        return self.calls[-1]["agent"]

    @property
    def last_context(self):
        return self.calls[-1]["kwargs"].get("context")


def _runtime(
    runner: _CapturingRunner,
    *,
    routing_policy=None,
    goal_text: str = "collect 64 logs",
) -> AgentRuntime:
    context = AgentContext(system_prompt="sys", goal_text=goal_text)
    context.observe_task({"task": {"task_id": "t1", "status": "running"}})
    return AgentRuntime(
        body=_FakeBody(),
        registry=ToolRegistry(),
        agent_context=context,
        lifecycle=LifecycleController(),
        mode_runtime=ModeRuntime(),
        authority=ProgressAuthority(),
        runner_run=runner,
        routing_policy=routing_policy,
    )


def _decision_events(runtime: AgentRuntime) -> list[dict[str, object]]:
    return [
        event
        for event in runtime.trace.snapshot()
        if event["event"] == "turn_decision_context"
    ]


class IdentityTelemetryTests(unittest.TestCase):
    def test_default_turn_emits_normal_class_with_identity_route(self) -> None:
        runner = _CapturingRunner()
        runtime = _runtime(runner)

        outcome = asyncio.run(runtime.run_turn())

        self.assertEqual(outcome.status, "completed_turn")
        events = _decision_events(runtime)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["decision_context"], "normal")
        self.assertEqual(event["route_model"], "primary")
        self.assertIsNone(event["route_effort"])
        self.assertEqual(event["route_context_profile"], "full")
        self.assertIs(event["routing_active"], False)

    def test_default_turn_keeps_primary_model_and_full_preamble(self) -> None:
        runner = _CapturingRunner()
        runtime = _runtime(runner)

        asyncio.run(runtime.run_turn())

        self.assertEqual(runner.last_agent.model, "primary")
        preamble = runner.last_context.instruction_preamble
        self.assertIn("GOAL: collect 64 logs", preamble)
        self.assertIn("TASK_ARTIFACT:", preamble)

    def test_message_intent_without_goal_classifies_social(self) -> None:
        runner = _CapturingRunner()
        runtime = _runtime(runner, goal_text="")

        asyncio.run(runtime.run_turn(intent_kind="message", has_durable_goal=False))

        self.assertEqual(_decision_events(runtime)[0]["decision_context"], "social")

    def test_task_boundary_intent_classifies_boundary(self) -> None:
        runner = _CapturingRunner()
        runtime = _runtime(runner)

        asyncio.run(runtime.run_turn(intent_kind="task_boundary", has_durable_goal=True))

        self.assertEqual(_decision_events(runtime)[0]["decision_context"], "boundary")

    def test_mobility_stance_classifies_mobility(self) -> None:
        runner = _CapturingRunner()
        runtime = _runtime(runner)

        asyncio.run(
            runtime.run_turn(
                extra_signals=[AgentSignal.mobility_blocked("no_path")],
                intent_kind="task_continue",
                has_durable_goal=True,
            )
        )

        self.assertEqual(_decision_events(runtime)[0]["decision_context"], "mobility")

    def test_unspecified_goal_presence_fails_safe_to_normal_not_social(self) -> None:
        runner = _CapturingRunner()
        runtime = _runtime(runner, goal_text="")

        asyncio.run(runtime.run_turn(intent_kind="message"))

        self.assertEqual(_decision_events(runtime)[0]["decision_context"], "normal")


class OptInRoutingTests(unittest.TestCase):
    POLICY = {
        DecisionContext.SOCIAL: RouteSpec(model="fast", effort="low", context_profile="social")
    }

    def test_policy_routes_social_turns_to_fast_model_and_social_profile(self) -> None:
        runner = _CapturingRunner()
        runtime = _runtime(runner, routing_policy=self.POLICY, goal_text="")

        asyncio.run(runtime.run_turn(intent_kind="message", has_durable_goal=False))

        event = _decision_events(runtime)[0]
        self.assertIs(event["routing_active"], True)
        self.assertEqual(event["route_model"], "fast")
        self.assertEqual(runner.last_agent.model, "fast")
        # Social profile excludes the task artifact section.
        self.assertNotIn("TASK_ARTIFACT:", runner.last_context.instruction_preamble)

    def test_unlisted_class_fails_closed_to_primary_default(self) -> None:
        runner = _CapturingRunner()
        runtime = _runtime(runner, routing_policy=self.POLICY)

        asyncio.run(runtime.run_turn(intent_kind="start", has_durable_goal=True))

        event = _decision_events(runtime)[0]
        self.assertEqual(event["decision_context"], "normal")
        self.assertEqual(event["route_model"], "primary")
        self.assertEqual(runner.last_agent.model, "primary")
        self.assertIn("TASK_ARTIFACT:", runner.last_context.instruction_preamble)


if __name__ == "__main__":
    unittest.main()
