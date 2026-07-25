import unittest

from minebot.brain.progress import ProgressAuthority
from minebot.brain.registry import RegisteredTool, ToolRegistry, ToolSidecar, WeldContext
from minebot.contract import BodyState, PerceptionResult, ToolResult
from minebot.game.errors import ActionReconciliationUnknownError
from tests.e2e_q4_early_output_recovery_probe import _run_tool


class ProbeBody:
    bot_name = "ProbeBot"

    def get_state(self):
        return BodyState(
            bot=self.bot_name,
            pos=(0.0, 64.0, 0.0),
            yaw=None,
            pitch=None,
            health=20.0,
            food=20,
            oxygen=300,
            inventory_raw="[]",
            inventory_hash="empty",
            effects=None,
            time=0,
            weather="clear",
            dimension="overworld",
            complete=True,
        )

    def get_inventory(self):
        return []

    def perceive(self, scope, params):
        return PerceptionResult(self.bot_name, scope, "perception", True, True, {})


class Q4EarlyOutputProbeTests(unittest.TestCase):
    def test_action_reconciliation_unknown_maps_like_production_runner(self):
        def raise_unknown(_params):
            raise ActionReconciliationUnknownError(
                "action outcome unknown",
                diagnostics={"action_id": "ambiguous-1", "status": "unknown"},
            )

        body = ProbeBody()
        registry = ToolRegistry()
        registry.register(
            RegisteredTool(
                "collect_resource",
                "Collect resource",
                {"type": "object", "properties": {}, "additionalProperties": False},
                raise_unknown,
                ToolSidecar("collect_resource", mutating=True),
            )
        )
        weld = WeldContext(body=body, authority=ProgressAuthority(), goal_text="collect logs")

        result = _run_tool(
            registry=registry,
            weld=weld,
            body=body,
            label="probe_collect",
            tool="collect_resource",
            params={},
        )

        self.assertEqual(result["result"]["reason"], "action_reconciliation_unknown")
        self.assertTrue(result["result"]["canRetry"])
        self.assertEqual(result["exception"]["type"], "ActionReconciliationUnknownError")
        self.assertEqual(result["exception"]["mapped_reason"], "action_reconciliation_unknown")
        self.assertEqual(result["result"]["metrics"]["error_type"], "ActionReconciliationUnknownError")
        self.assertEqual(
            result["result"]["metrics"]["await_diagnostics"]["action_id"],
            "ambiguous-1",
        )


if __name__ == "__main__":
    unittest.main()
