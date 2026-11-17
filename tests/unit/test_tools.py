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

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "preempted")
        self.assertTrue(result.can_retry)
        self.assertTrue(result.metrics["paused"])

    def test_a_failure_the_body_marks_unrepeatable_here_is_not_retryable(self):
        # life-stage b2: "surface_not_found" was always retryable, and Decision
        # chose "run it again" 439 times from the same spot.
        stuck = terminal_event_to_tool_result(Event(seq=1, tick=10, bot="Bot1", name="ascendDone",
            data={"stopped_reason": "surface_not_found", "retryable": False}))
        other = terminal_event_to_tool_result(Event(seq=2, tick=11, bot="Bot1", name="ascendDone",
            data={"stopped_reason": "vertical_no_path"}))
        self.assertFalse(stuck.success)
        self.assertFalse(stuck.can_retry)
        self.assertTrue(other.can_retry)


if __name__ == "__main__":
    unittest.main()
