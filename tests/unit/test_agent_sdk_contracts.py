import inspect
import unittest

from agents import Agent, RunConfig, RunContextWrapper, Runner, function_tool
from agents.run import RunResultStreaming
from agents.run_config import CallModelData, ModelInputData
from agents.usage import Usage

from minebot.contract import ProgressAbort, ProgressFacts


class AgentSdkContractTests(unittest.TestCase):
    def test_progress_abort_carries_structured_facts(self):
        facts = ProgressFacts(
            goal="collect 64 dirt",
            last_action=("move_to", 1, 2, 3),
            stagnant_steps=3,
            stalled_steps=8,
            failure_steps=0,
            last_fingerprint="before",
            current_fingerprint="after",
            recent_events=["progress_yielded"],
        )

        exc = ProgressAbort("yield", facts=facts)

        self.assertIs(exc.facts, facts)
        self.assertEqual(str(exc), "yield")

    def test_agent_accepts_dynamic_instructions_callable(self):
        def instructions(ctx: RunContextWrapper[dict[str, str]], agent: Agent[dict[str, str]]) -> str:
            return f"goal={ctx.context['goal']} agent={agent.name}"

        agent = Agent(name="minebot", instructions=instructions)

        self.assertIs(agent.instructions, instructions)

    def test_function_tool_supports_failure_escape_is_enabled_and_timeout(self):
        async def move_to(x: int, y: int, z: int) -> str:
            return f"{x},{y},{z}"

        enabled = lambda ctx, agent: True
        tool = function_tool(
            move_to,
            failure_error_function=None,
            is_enabled=enabled,
            timeout=1.5,
        )

        self.assertIs(tool.is_enabled, enabled)
        self.assertIsNone(tool._failure_error_function)
        self.assertFalse(tool._use_default_failure_error_function)
        self.assertEqual(tool.timeout_seconds, 1.5)

    def test_runconfig_accepts_model_input_filter(self):
        def input_filter(data: CallModelData[dict[str, str]]):
            self.assertEqual(data.context, {"goal": "collect"})
            return ModelInputData(
                input=list(data.model_data.input),
                instructions=(data.model_data.instructions or "") + "\nPROFILE: normal",
            )

        config = RunConfig(call_model_input_filter=input_filter)
        agent = Agent(name="minebot", instructions="static")
        wrapped = CallModelData(
            model_data=ModelInputData(input=[], instructions="GOAL: collect"),
            agent=agent,
            context={"goal": "collect"},
        )

        filtered = config.call_model_input_filter(wrapped)

        self.assertIsInstance(filtered, ModelInputData)
        self.assertIn("PROFILE: normal", filtered.instructions)

    def test_run_context_wrapper_fields_match_runtime_design(self):
        ctx = RunContextWrapper(
            context={"goal": "collect"},
            usage=Usage(requests=1),
            turn_input=[{"type": "message", "role": "user", "content": "collect"}],
            tool_input={"item": "dirt"},
        )

        self.assertEqual(ctx.context, {"goal": "collect"})
        self.assertEqual(ctx.usage.requests, 1)
        self.assertEqual(ctx.turn_input[0]["role"], "user")
        self.assertEqual(ctx.tool_input, {"item": "dirt"})

    def test_runner_and_streaming_interfaces_match_q0_contract(self):
        run_sig = inspect.signature(Runner.run)
        streamed_sig = inspect.signature(Runner.run_streamed)
        cancel_sig = inspect.signature(RunResultStreaming.cancel)
        stream_events_sig = inspect.signature(RunResultStreaming.stream_events)

        self.assertIn("context", run_sig.parameters)
        self.assertIn("run_config", run_sig.parameters)
        self.assertIn("hooks", run_sig.parameters)
        self.assertIn("context", streamed_sig.parameters)
        self.assertIn("run_config", streamed_sig.parameters)
        self.assertIn("mode", cancel_sig.parameters)
        self.assertEqual(cancel_sig.parameters["mode"].default, "immediate")
        self.assertEqual(list(stream_events_sig.parameters), ["self"])


if __name__ == "__main__":
    unittest.main()
