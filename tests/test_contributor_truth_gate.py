"""Small, environment-free proof of MineBot's terminal-truth contract."""

import unittest

from minebot.contract.messages import Event
from minebot.contract.results import terminal_event_to_tool_result


class ContributorTruthGateTests(unittest.TestCase):
    def test_authoritative_terminal_event_proves_completion(self):
        event = Event(
            seq=12,
            tick=480,
            bot="ContributorBot",
            name="collectFinished",
            data={
                "completed": True,
                "inventory_before": {"dirt": 0},
                "inventory_after": {"dirt": 3},
            },
        )

        result = terminal_event_to_tool_result(event)

        self.assertTrue(result.success)
        self.assertFalse(result.can_retry)
        self.assertEqual(result.metrics["inventory_before"]["dirt"], 0)
        self.assertEqual(result.metrics["inventory_after"]["dirt"], 3)

    def test_command_acceptance_is_not_completion(self):
        event = Event(
            seq=10,
            tick=460,
            bot="ContributorBot",
            name="collectAccepted",
            data={"accepted": True, "model_claim": "done"},
        )

        result = terminal_event_to_tool_result(event)

        self.assertFalse(result.success)
        self.assertTrue(result.can_retry)

    def test_terminal_failure_remains_failure(self):
        event = Event(
            seq=11,
            tick=470,
            bot="ContributorBot",
            name="collectFinished",
            data={"stopped_reason": "target_missing", "completed": False},
        )

        result = terminal_event_to_tool_result(event)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "target_missing")
        self.assertTrue(result.can_retry)


if __name__ == "__main__":
    unittest.main()
