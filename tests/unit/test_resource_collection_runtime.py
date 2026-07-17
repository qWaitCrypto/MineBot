import unittest

from minebot.body.resource_collection import ResourceCollectionConfig, ResourceCollectionTransactions
from minebot.contract import BodyState, PerceptionResult, ToolResult
from minebot.game.navigation import GoalComposite


class ResourceBody:
    bot_name = "Bot1"

    def __init__(self, targets):
        self.targets = list(targets)
        self.state_pos = (0.5, 65.0, 0.5)
        self.perceptions = []

    def get_state(self):
        return BodyState(
            bot=self.bot_name,
            pos=self.state_pos,
            yaw=None,
            pitch=None,
            health=20.0,
            food=20,
            oxygen=300,
            inventory_raw="[]",
            inventory_hash="hash",
            effects=None,
            time=0,
            weather="clear",
            dimension="overworld",
            complete=True,
        )

    def perceive(self, scope, params):
        self.perceptions.append((scope, dict(params)))
        if scope == "findBlocks":
            wanted = str(params["type"]).removeprefix("minecraft:")
            blocks = [
                {"x": pos[0], "y": pos[1], "z": pos[2], "type": block_type}
                for pos, block_type in self.targets
                if block_type == wanted
            ]
            return PerceptionResult(
                bot=self.bot_name,
                scope=scope,
                type="perception",
                ok=True,
                complete=True,
                data={"blocks": blocks, "totalMatches": len(blocks)},
            )
        if scope == "blockCells":
            cells = []
            for raw in params.get("cells") or []:
                pos = (int(raw[0]), int(raw[1]), int(raw[2]))
                target_type = next((block for target, block in self.targets if target == pos), None)
                if target_type is not None:
                    block_type, state = target_type, "SOLID"
                elif pos[1] == 64:
                    block_type, state = "stone", "SOLID"
                else:
                    block_type, state = "air", "CLEAR"
                cells.append(
                    {
                        "x": pos[0],
                        "y": pos[1],
                        "z": pos[2],
                        "type": block_type,
                        "state": state,
                        "properties": {},
                    }
                )
            return PerceptionResult(
                bot=self.bot_name,
                scope=scope,
                type="perception",
                ok=True,
                complete=True,
                data={"cells": cells, "count": len(cells), "total": len(cells), "next": None},
            )
        raise AssertionError(f"unexpected perception scope {scope}")


class RecordingNavigator:
    def __init__(self, body, selected_goals, outcomes=None):
        self.body = body
        self.selected_goals = list(selected_goals)
        self.outcomes = list(outcomes or [])
        self.calls = []

    def navigate_to(self, goal, **kwargs):
        self.calls.append((goal, kwargs))
        selected = self.selected_goals.pop(0)
        success, reason = self.outcomes.pop(0) if self.outcomes else (True, "arrived")
        if success and reason == "arrived":
            self.body.state_pos = (selected[0] + 0.5, float(selected[1]), selected[2] + 0.5)
        return ToolResult(
            success,
            reason,
            not success,
            metrics={"selected_goal": list(selected), "goal_set_preserved": True},
        )


class RecordingWork:
    MINE_APPROACH_MAX_BREAK_STEPS = 8

    def __init__(self, outcomes=None):
        self.outcomes = list(outcomes or [])
        self.calls = []

    def mine_block_collect(self, pos, **kwargs):
        self.calls.append((pos, kwargs))
        if self.outcomes:
            return self.outcomes.pop(0)
        return ToolResult(True, "collected", False, metrics={"collected_total": 1})


class ResourceCollectionRuntimeTests(unittest.TestCase):
    def test_planner_selects_target_and_stand_from_one_combined_domain(self):
        body = ResourceBody(
            [
                ((5, 64, 0), "dirt"),
                ((8, 64, 0), "dirt"),
            ]
        )
        selected = (8, 65, -1)
        navigator = RecordingNavigator(body, [selected])
        work = RecordingWork()
        runtime = ResourceCollectionTransactions(body, navigator, work)

        result = runtime.collect_block_domain(
            block_types=("dirt",),
            expected_drops=("dirt",),
            remaining_count=1,
            config=ResourceCollectionConfig(candidate_budget=2, mutation_budget=1),
        )

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(len(navigator.calls), 1)
        goal, kwargs = navigator.calls[0]
        self.assertIsInstance(goal, GoalComposite)
        goal_positions = {child.pos for child in goal.goals}
        self.assertIn((5, 65, -1), goal_positions)
        self.assertIn((8, 65, -1), goal_positions)
        self.assertEqual(work.calls[0][0], (8, 64, 0))
        self.assertEqual(work.calls[0][1]["prepositioned"], True)
        self.assertEqual([scope for scope, _params in body.perceptions].count("blockCells"), 1)

    def test_candidate_failure_is_blacklisted_and_remaining_domain_replanned(self):
        first = (5, 64, 0)
        second = (8, 64, 0)
        body = ResourceBody([(first, "dirt"), (second, "dirt")])
        navigator = RecordingNavigator(body, [(5, 65, -1), (8, 65, -1)])
        work = RecordingWork(
            [
                ToolResult(False, "collect_no_inventory_delta", True, metrics={"collected_total": 0}),
                ToolResult(True, "collected", False, metrics={"collected_total": 1}),
            ]
        )
        runtime = ResourceCollectionTransactions(body, navigator, work)

        result = runtime.collect_block_domain(
            block_types=("dirt",),
            expected_drops=("dirt",),
            remaining_count=1,
            config=ResourceCollectionConfig(candidate_budget=3, mutation_budget=2),
        )

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual([call[0] for call in work.calls], [first, second])
        second_goal_positions = {child.pos for child in navigator.calls[1][0].goals}
        self.assertIn((8, 65, -1), second_goal_positions)
        self.assertEqual(
            [entry["pos"] for entry in result.metrics["attempts"][1]["domain"]["candidate_targets"]],
            [list(second)],
        )
        self.assertIn(list(first), result.metrics["candidate_blacklist"])

    def test_navigation_candidate_failure_replans_without_serial_brain_choice(self):
        first = (5, 64, 0)
        second = (8, 64, 0)
        body = ResourceBody([(first, "dirt"), (second, "dirt")])
        navigator = RecordingNavigator(
            body,
            [(5, 65, -1), (8, 65, -1)],
            outcomes=[(False, "stuck"), (True, "arrived")],
        )
        work = RecordingWork()
        runtime = ResourceCollectionTransactions(body, navigator, work)

        result = runtime.collect_block_domain(
            block_types=("dirt",),
            expected_drops=("dirt",),
            remaining_count=1,
            config=ResourceCollectionConfig(candidate_budget=3, mutation_budget=1),
        )

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(len(navigator.calls), 2)
        self.assertEqual([call[0] for call in work.calls], [second])
        self.assertIn(list(first), result.metrics["candidate_blacklist"])

    def test_successful_preemption_is_terminal_before_mining(self):
        body = ResourceBody([((5, 64, 0), "dirt")])
        navigator = RecordingNavigator(
            body,
            [(5, 65, -1)],
            outcomes=[(True, "preempted")],
        )
        work = RecordingWork()
        runtime = ResourceCollectionTransactions(body, navigator, work)

        result = runtime.collect_block_domain(
            block_types=("dirt",),
            expected_drops=("dirt",),
            remaining_count=1,
            config=ResourceCollectionConfig(candidate_budget=1, mutation_budget=1),
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "resource_navigation_preempted")
        self.assertTrue(result.can_retry)
        self.assertEqual(work.calls, [])

    def test_missing_tool_is_terminal_not_a_candidate_skip(self):
        body = ResourceBody([((5, -55, 0), "diamond_ore"), ((8, -55, 0), "diamond_ore")])
        navigator = RecordingNavigator(body, [(5, -54, -1)])
        work = RecordingWork(
            [ToolResult(False, "missing_required_tool", False, metrics={"required_tier": "iron"})]
        )
        runtime = ResourceCollectionTransactions(body, navigator, work)

        result = runtime.collect_block_domain(
            block_types=("diamond_ore",),
            expected_drops=("diamond",),
            remaining_count=1,
            config=ResourceCollectionConfig(candidate_budget=2, mutation_budget=2),
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "missing_required_tool")
        self.assertFalse(result.can_retry)
        self.assertEqual(len(work.calls), 1)
        self.assertEqual(len(navigator.calls), 1)

    def test_candidate_budget_boundary_rescans_and_reports_exhausted_domain(self):
        first = (5, 64, 0)
        second = (8, 64, 0)
        body = ResourceBody([(first, "dirt"), (second, "dirt")])
        navigator = RecordingNavigator(body, [(5, 65, -1), (8, 65, -1)])
        work = RecordingWork(
            [
                ToolResult(False, "break_denied:protected_region", True),
                ToolResult(False, "break_denied:protected_region", True),
            ]
        )
        runtime = ResourceCollectionTransactions(body, navigator, work)

        result = runtime.collect_block_domain(
            block_types=("dirt",),
            expected_drops=("dirt",),
            remaining_count=1,
            config=ResourceCollectionConfig(candidate_budget=2, mutation_budget=3),
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "resource_candidate_domain_exhausted")
        self.assertTrue(result.can_retry)
        self.assertEqual(len(navigator.calls), 2)
        self.assertEqual([call[0] for call in work.calls], [first, second])
        self.assertEqual(result.metrics["candidate_blacklist"], [list(first), list(second)])
        self.assertEqual([scope for scope, _params in body.perceptions].count("findBlocks"), 3)
        self.assertEqual(result.metrics["searches"][-1]["active_candidates"], [])

    def test_candidate_budget_boundary_preserves_budget_exhaustion_when_candidates_remain(self):
        first = (5, 64, 0)
        second = (8, 64, 0)
        third = (11, 64, 0)
        body = ResourceBody([(first, "dirt"), (second, "dirt"), (third, "dirt")])
        navigator = RecordingNavigator(body, [(5, 65, -1), (8, 65, -1)])
        work = RecordingWork(
            [
                ToolResult(False, "collect_no_inventory_delta", True),
                ToolResult(False, "collect_no_inventory_delta", True),
            ]
        )
        runtime = ResourceCollectionTransactions(body, navigator, work)

        result = runtime.collect_block_domain(
            block_types=("dirt",),
            expected_drops=("dirt",),
            remaining_count=1,
            config=ResourceCollectionConfig(candidate_budget=2, mutation_budget=3),
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "resource_domain_budget_exhausted")
        self.assertEqual(len(navigator.calls), 2)
        self.assertEqual([call[0] for call in work.calls], [first, second])
        self.assertIn(list(third), result.metrics["searches"][-1]["active_candidates"])
        self.assertEqual([scope for scope, _params in body.perceptions].count("findBlocks"), 3)


if __name__ == "__main__":
    unittest.main()
