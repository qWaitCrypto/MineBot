import asyncio
import unittest

from minebot.app.recall import FanOutRecall
from minebot.app.runner import AgentRuntime
from minebot.brain.context import HARNESS_FACT_PREFIX, AgentContext
from minebot.brain.recall import RecallItem, RecallKind, RecallResult


class _Trace:
    def emit(self, *args, **kwargs):
        pass


class _Recall:
    def __init__(self, result):
        self.result = result

    def recall(self, query):
        return self.result


def _runtime(recall):
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.recall = recall
    runtime.last_known_body_state = {"pos": [4.0, 70.0, 9.0], "dimension": "overworld"}
    runtime.agent_context = AgentContext(system_prompt="stable", goal_text="collect iron")
    runtime._last_recalled_handles = set()
    runtime.trace = _Trace()

    async def run_read(callback, query, timeout_s=None):
        return callback(query)

    runtime.run_read = run_read
    return runtime


class RecallInjectionTests(unittest.TestCase):
    def test_recall_is_one_harness_fact_and_previous_items_are_not_repeated(self):
        item = RecallItem(
            RecallKind.MEMORY,
            "memory:danger",
            {"excerpt": "lava damage here", "point": [4, 70, 9]},
            "observed",
        )
        runtime = _runtime(_Recall(RecallResult(items=(item,))))
        stable_before = runtime.agent_context.compile().text

        asyncio.run(runtime._attach_turn_recall())
        first, count = runtime.agent_context.pending_turn_input(fallback="fallback")
        self.assertEqual(count, 1)
        self.assertTrue(first.startswith(HARNESS_FACT_PREFIX + "Recalled: lava damage here"))
        runtime.agent_context.acknowledge_turn_input(count)

        asyncio.run(runtime._attach_turn_recall())
        second, second_count = runtime.agent_context.pending_turn_input(fallback="fallback")
        self.assertEqual((second, second_count), ("fallback", 0))
        asyncio.run(runtime._attach_turn_recall())
        self.assertEqual(runtime.agent_context.pending_turn_input(fallback="fallback"), ("fallback", 0))
        self.assertEqual(stable_before, runtime.agent_context.compile().text)

    def test_recall_error_does_not_create_turn_input(self):
        class Broken:
            def recall(self, query):
                raise TimeoutError("recall budget elapsed")

        runtime = _runtime(Broken())
        asyncio.run(runtime._attach_turn_recall())
        self.assertEqual(runtime.agent_context.pending_turn_input(fallback="continue"), ("continue", 0))

    def test_recall_timeout_does_not_create_turn_input(self):
        runtime = _runtime(_Recall(RecallResult()))

        async def stalled_read(callback, query, timeout_s=None):
            await asyncio.sleep(1)

        runtime.run_read = stalled_read
        asyncio.run(runtime._attach_turn_recall())
        self.assertEqual(runtime.agent_context.pending_turn_input(fallback="continue"), ("continue", 0))

    def test_two_turn_stable_prefix_is_byte_identical(self):
        items = tuple(
            RecallItem(RecallKind.MEMORY, f"memory:{i}", {"excerpt": f"fact {i}"}, "observed")
            for i in range(4)
        )
        runtime = _runtime(_Recall(RecallResult(items=items)))
        first_prefix = runtime.agent_context.compile().text
        asyncio.run(runtime._attach_turn_recall())
        input_one, count = runtime.agent_context.pending_turn_input(fallback="continue")
        runtime.agent_context.acknowledge_turn_input(count)
        second_prefix = runtime.agent_context.compile().text
        asyncio.run(runtime._attach_turn_recall())
        input_two, _ = runtime.agent_context.pending_turn_input(fallback="continue")
        self.assertEqual(first_prefix, second_prefix)
        self.assertIn("fact 0", input_one)
        self.assertNotIn("fact 3", input_one)
        self.assertIn("fact 3", input_two)


if __name__ == "__main__":
    unittest.main()
