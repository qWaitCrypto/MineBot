import asyncio
import unittest

from minebot.app.runner import AgentRuntime
from minebot.brain.context import AgentContext
from minebot.brain.lifecycle import LifecycleController, LifecycleState
from minebot.brain.modes import AgentSignal, ModeRuntime
from minebot.brain.progress import ProgressAuthority
from minebot.brain.registry import ToolRegistry
from minebot.contract import BodyState, Event

from tests.unit.test_agent_runner_spine import FakeBody, body_state


def make_state(**overrides):
    data = dict(
        bot="Bot",
        pos=(0.0, 64.0, 0.0),
        yaw=None,
        pitch=None,
        health=20.0,
        food=20,
        oxygen=300,
        inventory_raw="[]",
        inventory_hash="empty",
        effects=None,
        time=1000,
        weather=None,
        dimension="overworld",
        complete=True,
    )
    data.update(overrides)
    return BodyState(**data)


class EventBody(FakeBody):
    def __init__(self, *, state: BodyState | None = None, events=None):
        super().__init__()
        self._state = state
        self.events = list(events or [])

    def get_state(self):
        return self._state or body_state(self.x)


def runtime_for(body, calls):
    async def fake_runner(agent, input_text, *, context=None, run_config=None, **kwargs):
        calls.append(
            {
                "input": input_text,
                "situational": context.profile.situational,
                "focus": context.profile.tool_focus,
                "model": getattr(agent, "model", None),
                "run_config": run_config,
            }
        )
        return {"ok": True}

    return AgentRuntime(
        body=body,
        registry=ToolRegistry(),
        agent_context=AgentContext(system_prompt="sys", goal_text="collect 64 logs"),
        lifecycle=LifecycleController(),
        mode_runtime=ModeRuntime(),
        authority=ProgressAuthority(),
        runner_run=fake_runner,
    )


class AgentStateRuntimeIntegrationTests(unittest.TestCase):
    def test_survival_state_changes_context_profile_tool_focus_and_route(self):
        calls = []
        runtime = runtime_for(EventBody(state=make_state(health=4.0)), calls)

        outcome = asyncio.run(runtime.run_turn())

        self.assertEqual(outcome.profile.situational, "survival")
        self.assertIn("survival", outcome.profile.tool_focus)
        self.assertEqual(outcome.profile.model_route, "fast")
        self.assertIn("PROFILE: relationship=autonomous.user_request situational=survival", calls[0]["input"])
        self.assertIn("Do not initiate combat as a food strategy", calls[0]["input"])
        self.assertTrue(
            any(
                event["event"] == "turn_profile"
                and event["situational"] == "survival"
                and event["model_route"] == "fast"
                and "survival" in event["tool_focus"]
                and "Do not initiate combat as a food strategy" in event["context_frame"]
                for event in runtime.trace.snapshot()
            )
        )

    def test_mobility_event_foregrounds_navigation_without_lifecycle_stop(self):
        calls = []
        body = EventBody(
            events=[
                Event(seq=1, tick=20, bot="Bot", name="navigationBlocked", data={"reason": "blocked"}),
            ]
        )
        runtime = runtime_for(body, calls)

        outcome = asyncio.run(runtime.run_turn())

        self.assertEqual(outcome.status, "completed_turn")
        self.assertEqual(outcome.lifecycle, LifecycleState.ACTIVE)
        self.assertEqual(outcome.profile.situational, "mobility")
        self.assertIn("navigation", outcome.profile.tool_focus)
        self.assertIn("frame=Mobility/reachability issue", calls[0]["input"])

    def test_death_signal_requests_recovery_and_resume_injects_suspend_context(self):
        calls = []
        runtime = runtime_for(EventBody(), calls)

        death = asyncio.run(runtime.run_turn(extra_signals=[AgentSignal.death_detected("death", composition_id="collect")]))
        self.assertEqual(death.status, "stopped")
        self.assertEqual(death.lifecycle, LifecycleState.RECOVERING)
        self.assertEqual(death.profile.situational, "death")
        self.assertIsNotNone(runtime.mode_runtime.suspend_slot)

        recovered = asyncio.run(runtime.run_turn(extra_signals=[AgentSignal.recovery_completed("respawned")]))
        self.assertEqual(recovered.lifecycle, LifecycleState.RESUMING)

        resumed = asyncio.run(runtime.run_turn())
        self.assertEqual(resumed.status, "completed_turn")
        self.assertEqual(resumed.lifecycle, LifecycleState.ACTIVE)
        self.assertIn("RESUME: reason=death", calls[0]["input"])
        self.assertTrue(any(event["event"] == "resume_context" for event in runtime.trace.snapshot()))


if __name__ == "__main__":
    unittest.main()
