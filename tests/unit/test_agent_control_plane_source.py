import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "minebot" / "app"


class AgentControlPlaneSourceTests(unittest.TestCase):
    def test_only_agent_session_admits_runtime_turns_in_production_entrypoints(self):
        callers = []
        for path in APP.glob("*.py"):
            if ".runtime.run_turn(" in path.read_text(encoding="utf-8"):
                callers.append(path.name)

        self.assertEqual(sorted(callers), ["session.py"])

    def test_session_has_no_legacy_command_deque(self):
        source = (APP / "session.py").read_text(encoding="utf-8")

        self.assertNotIn("from collections import deque", source)
        self.assertNotIn("_commands", source)
        self.assertIn("work_queue.lease_next()", source)
        self.assertIn("work_queue.complete(intent)", source)

    def test_production_control_plane_has_no_fixed_strategy_driver(self):
        session_source = (APP / "session.py").read_text(encoding="utf-8")
        runner_source = (APP / "runner.py").read_text(encoding="utf-8")

        self.assertNotIn("goal_driver", session_source)
        self.assertNotIn("drive_tool_once", runner_source)


if __name__ == "__main__":
    unittest.main()
