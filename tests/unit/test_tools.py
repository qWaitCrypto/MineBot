import unittest

from minebot.brain.tools import terminal_event_to_tool_result
from minebot.contract import Event


class ToolTests(unittest.TestCase):
    def test_terminal_event_to_tool_result_success_from_arrived_truth(self):
        result = terminal_event_to_tool_result(
            Event(seq=1, tick=10, bot="Bot1", name="moveDone", data={"arrived": True, "stopped_reason": "arrived"})
        )

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "arrived")
        self.assertFalse(result.can_retry)


    def test_preempted_is_truthy_neutral_retryable_sentinel(self):
        result = terminal_event_to_tool_result(
            Event(seq=1, tick=10, bot="Bot1", name="moveDone", data={"stopped_reason": "preempted"})
        )

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "preempted")
        self.assertTrue(result.can_retry)
        self.assertTrue(result.metrics["paused"])


if __name__ == "__main__":
    unittest.main()
