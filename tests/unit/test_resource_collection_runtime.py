import unittest

from minebot.body.interaction_support import NearbyBlockSearch, NearbyBlockTarget
from minebot.body.resource_collection import (
    ResourceCollectionConfig,
    ResourceCollectionTransactions,
    _active_targets,
)
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
                else:
                    high_target_support = {
                        (target[0][0] + dx, target[0][1] - 2, target[0][2] + dz)
                        for target in self.targets
                        if target[0][1] >= 68
                        for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1))
                    }
                    low_target_support = {
                        (target[0][0] + dx, target[0][1], target[0][2] + dz)
                        for target in self.targets
                        if target[0][1] < 64
                        for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1))
                    }
                    if pos[1] == 64 or pos in high_target_support or pos in low_target_support:
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


class EgressRepositioningResourceBody(ResourceBody):
    """The original target batch becomes invisible after a successful egress."""

    def perceive(self, scope, params):
        if scope == "findBlocks" and self.state_pos[0] > 10:
            original = self.targets
            self.targets = []
            try:
                return super().perceive(scope, params)
            finally:
                self.targets = original
        return super().perceive(scope, params)


class ScopedTreeResourceBody(ResourceBody):
    """Expose authoritative block cells for the tree-domain retarget probe."""

    def __init__(self, primary_targets, tree_targets):
        super().__init__([*primary_targets, *tree_targets])
        self.primary_targets = list(primary_targets)
        self.tree_targets = list(tree_targets)

    def perceive(self, scope, params):
        if scope == "findBlocks":
            wanted = str(params["type"]).removeprefix("minecraft:")
            blocks = [
                {"x": pos[0], "y": pos[1], "z": pos[2], "type": block_type}
                for pos, block_type in self.primary_targets
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
        return super().perceive(scope, params)


class RecordingNavigator:
    def __init__(self, body, selected_goals, outcomes=None, metrics=None):
        self.body = body
        self.selected_goals = list(selected_goals)
        self.outcomes = list(outcomes or [])
        self.metrics = list(metrics or [])
        self.calls = []

    def navigate_to(self, goal, **kwargs):
        self.calls.append((goal, kwargs))
        selected = self.selected_goals.pop(0)
        success, reason = self.outcomes.pop(0) if self.outcomes else (True, "arrived")
        if success and reason == "arrived":
            self.body.state_pos = (selected[0] + 0.5, float(selected[1]), selected[2] + 0.5)
        result_metrics = {"selected_goal": list(selected), "goal_set_preserved": True}
        if self.metrics:
            result_metrics.update(self.metrics.pop(0))
        return ToolResult(
            success,
            reason,
            not success,
            metrics=result_metrics,
        )


ZERO_PROGRESS_NAVIGATION_METRICS = {
    "segments": [
        {
            "diagnostics": {
                "path_length": 0,
                "waypoints": 0,
                "move_ticks": 0,
                "partial_distance": 0.0,
                "movement_counts": {
                    "walk": 0,
                    "diagonal": 0,
                    "ascend": 0,
                    "descend": 0,
                    "swim": 0,
                    "fall": 0,
                    "break": 0,
                    "place": 0,
                    "pillar": 0,
                },
            }
        }
    ]
}

POSITIVE_PATH_NAVIGATION_METRICS = {
    "segments": [
        {
            "diagnostics": {
                "path_length": 3,
                "waypoints": 3,
                "move_ticks": 12,
                "partial_distance": 2.0,
                "movement_counts": {"walk": 3, "swim": 0},
            }
        }
    ]
}


class RecordingWork:
    MINE_APPROACH_MAX_BREAK_STEPS = 8

    def __init__(self, outcomes=None, egress_outcomes=None):
        self.outcomes = list(outcomes or [])
        self.egress_outcomes = list(egress_outcomes or [])
        self.calls = []
        self.egress_calls = []

    def egress_to_dry(self, **kwargs):
        self.egress_calls.append(kwargs)
        if self.egress_outcomes:
            return self.egress_outcomes.pop(0)
        return ToolResult(True, "dry_stand", False)

    def mine_block_collect(self, pos, **kwargs):
        self.calls.append((pos, kwargs))
        if self.outcomes:
            return self.outcomes.pop(0)
        return ToolResult(True, "collected", False, metrics={"collected_total": 1})


class ResourceCollectionRuntimeTests(unittest.TestCase):
    def test_exact_blacklist_filters_priority_targets_without_erasing_neighboring_cluster(self):
        occluded = NearbyBlockTarget((5, 64, 0), "iron_ore", 5.0)
        neighbor = NearbyBlockTarget((6, 64, 0), "iron_ore", 6.0)
        search = NearbyBlockSearch(
            targets=[occluded, neighbor],
            truncated=False,
            uncertainty=[],
            errors=[],
            pages_read=1,
            total_matches=2,
        )

        active = _active_targets(
            search,
            candidate_blacklist=set(),
            exact_candidate_blacklist={occluded.pos},
            patch_blacklist=[],
            limit=2,
            priority_targets=(occluded, neighbor),
        )

        self.assertEqual(active, (neighbor,))

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

    def test_resource_domain_expands_tree_stands_through_interaction_range(self):
        target = (5, 67, 0)
        body = ResourceBody([(target, "oak_log")])
        navigator = RecordingNavigator(body, [(5, 65, -1)])
        runtime = ResourceCollectionTransactions(body, navigator, RecordingWork())

        result = runtime.collect_block_domain(
            block_types=("oak_log",),
            expected_drops=("oak_log",),
            remaining_count=1,
            config=ResourceCollectionConfig(candidate_budget=1, mutation_budget=1),
        )

        self.assertTrue(result.success, result.to_payload())
        goal_positions = {child.pos for child in navigator.calls[0][0].goals}
        self.assertIn((5, 65, -1), goal_positions)

    def test_resource_domain_does_not_navigate_on_unverified_geometric_fallback(self):
        class NoStandFactsBody(ResourceBody):
            def perceive(self, scope, params):
                if scope == "blockCells":
                    cells = [
                        {
                            "x": int(raw[0]),
                            "y": int(raw[1]),
                            "z": int(raw[2]),
                            "type": "air",
                            "state": "CLEAR",
                            "properties": {},
                        }
                        for raw in params.get("cells") or []
                    ]
                    return PerceptionResult(
                        bot=self.bot_name,
                        scope=scope,
                        type="perception",
                        ok=True,
                        complete=True,
                        data={"cells": cells, "count": len(cells), "total": len(cells), "next": None},
                    )
                return super().perceive(scope, params)

        target = (5, 64, 0)
        body = NoStandFactsBody([(target, "dirt")])
        navigator = RecordingNavigator(body, [(5, 65, -1)])
        runtime = ResourceCollectionTransactions(body, navigator, RecordingWork())

        result = runtime.collect_block_domain(
            block_types=("dirt",),
            expected_drops=("dirt",),
            remaining_count=1,
            config=ResourceCollectionConfig(candidate_budget=1, mutation_budget=1),
        )

        self.assertFalse(result.success, result.to_payload())
        self.assertEqual(result.reason, "resource_candidate_domain_exhausted")
        self.assertEqual(navigator.calls, [])

    def test_collection_approach_uses_dry_land_profile_without_disabling_governed_clearance(self):
        body = ResourceBody([((5, 64, 0), "dirt")])
        navigator = RecordingNavigator(body, [(5, 65, -1)])
        work = RecordingWork()
        runtime = ResourceCollectionTransactions(body, navigator, work)

        result = runtime.collect_block_domain(
            block_types=("dirt",),
            expected_drops=("dirt",),
            remaining_count=1,
            config=ResourceCollectionConfig(candidate_budget=1, mutation_budget=1),
        )

        self.assertTrue(result.success, result.to_payload())
        config = navigator.calls[0][1]["config"]
        self.assertFalse(config.allow_swim)
        self.assertTrue(config.allow_break)
        self.assertEqual(config.max_break_steps, work.MINE_APPROACH_MAX_BREAK_STEPS)
        self.assertEqual(config.server_max_expand, 1200)
        self.assertEqual(config.server_grid_radius, 48)
        self.assertEqual(config.max_segments, 16)
        self.assertEqual(config.max_partial_segments, 8)
        self.assertEqual(work.egress_calls, [{"timeout_s": 15.0}])
        self.assertEqual(result.metrics["navigation_fallback_attempts"], 0)

    def test_zero_progress_no_path_upgrades_to_governed_mobility_once(self):
        target = (5, 64, 0)
        selected = (5, 65, -1)
        body = ResourceBody([(target, "dirt")])
        navigator = RecordingNavigator(
            body,
            [selected, selected],
            outcomes=[(False, "no_path"), (True, "arrived")],
            metrics=[ZERO_PROGRESS_NAVIGATION_METRICS],
        )
        runtime = ResourceCollectionTransactions(body, navigator, RecordingWork())

        result = runtime.collect_block_domain(
            block_types=("dirt",),
            expected_drops=("dirt",),
            remaining_count=1,
            config=ResourceCollectionConfig(candidate_budget=1, mutation_budget=1),
        )

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(len(navigator.calls), 2)
        dry_config = navigator.calls[0][1]["config"]
        fallback_config = navigator.calls[1][1]["config"]
        self.assertFalse(dry_config.allow_swim)
        self.assertTrue(fallback_config.allow_swim)
        self.assertTrue(fallback_config.aquatic_traversal)
        for field in ("allow_break", "allow_place", "allow_pillar", "allow_downward"):
            self.assertTrue(getattr(fallback_config, field))
        self.assertEqual(
            fallback_config.max_break_steps,
            dry_config.max_break_steps,
        )
        self.assertEqual(result.metrics["navigation_fallback_attempts"], 1)
        fallback = result.metrics["attempts"][0]["navigation_fallback"]
        self.assertEqual(fallback["profile"], "governed_mobility")
        self.assertEqual(fallback["dry_terminal_reason"], "no_path")
        self.assertEqual(fallback["dry_result"]["reason"], "no_path")

    def test_zero_progress_budget_exceeded_upgrades_to_governed_mobility(self):
        target = (5, 64, 0)
        selected = (5, 65, -1)
        body = ResourceBody([(target, "dirt")])
        navigator = RecordingNavigator(
            body,
            [selected, selected],
            outcomes=[(False, "budget_exceeded"), (True, "arrived")],
            metrics=[ZERO_PROGRESS_NAVIGATION_METRICS],
        )
        runtime = ResourceCollectionTransactions(body, navigator, RecordingWork())

        result = runtime.collect_block_domain(
            block_types=("dirt",),
            expected_drops=("dirt",),
            remaining_count=1,
            config=ResourceCollectionConfig(candidate_budget=1, mutation_budget=1),
        )

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(len(navigator.calls), 2)
        self.assertFalse(navigator.calls[0][1]["config"].allow_swim)
        self.assertTrue(navigator.calls[1][1]["config"].allow_swim)
        self.assertTrue(navigator.calls[1][1]["config"].aquatic_traversal)
        self.assertEqual(result.metrics["navigation_fallback_attempts"], 1)

    def test_recovery_exhausted_no_path_upgrades_to_governed_mobility(self):
        target = (5, 64, 0)
        selected = (5, 65, -1)
        body = ResourceBody([(target, "dirt")])
        navigator = RecordingNavigator(
            body,
            [selected, selected],
            outcomes=[(False, "recovery_exhausted:no_path"), (True, "arrived")],
            metrics=[ZERO_PROGRESS_NAVIGATION_METRICS],
        )
        runtime = ResourceCollectionTransactions(body, navigator, RecordingWork())

        result = runtime.collect_block_domain(
            block_types=("dirt",),
            expected_drops=("dirt",),
            remaining_count=1,
            config=ResourceCollectionConfig(candidate_budget=1, mutation_budget=1),
        )

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(len(navigator.calls), 2)
        fallback_config = navigator.calls[1][1]["config"]
        self.assertTrue(fallback_config.allow_swim)
        self.assertTrue(fallback_config.aquatic_traversal)
        self.assertEqual(result.metrics["navigation_fallback_attempts"], 1)
        fallback = result.metrics["attempts"][0]["navigation_fallback"]
        self.assertEqual(fallback["dry_terminal_reason"], "recovery_exhausted:no_path")
        self.assertEqual(fallback["profile"], "governed_mobility")

    def test_navigation_progress_does_not_trigger_governed_mobility_fallback(self):
        target = (5, 64, 0)
        selected = (5, 65, -1)
        body = ResourceBody([(target, "dirt")])
        navigator = RecordingNavigator(
            body,
            [selected],
            outcomes=[(False, "budget_exceeded")],
            metrics=[POSITIVE_PATH_NAVIGATION_METRICS],
        )
        runtime = ResourceCollectionTransactions(body, navigator, RecordingWork())

        result = runtime.collect_block_domain(
            block_types=("dirt",),
            expected_drops=("dirt",),
            remaining_count=1,
            config=ResourceCollectionConfig(candidate_budget=1, mutation_budget=1),
        )

        self.assertFalse(result.success)
        self.assertEqual(len(navigator.calls), 1)
        self.assertEqual(result.metrics["navigation_fallback_attempts"], 0)
        self.assertNotIn("navigation_fallback", result.metrics["attempts"][0])

    def test_governed_mobility_fallback_failure_remains_honest(self):
        target = (5, 64, 0)
        selected = (5, 65, -1)
        body = ResourceBody([(target, "dirt")])
        navigator = RecordingNavigator(
            body,
            [selected, selected],
            outcomes=[(False, "no_path"), (False, "budget_exceeded")],
            metrics=[ZERO_PROGRESS_NAVIGATION_METRICS],
        )
        runtime = ResourceCollectionTransactions(body, navigator, RecordingWork())

        result = runtime.collect_block_domain(
            block_types=("dirt",),
            expected_drops=("dirt",),
            remaining_count=1,
            config=ResourceCollectionConfig(candidate_budget=1, mutation_budget=1),
        )

        self.assertFalse(result.success)
        self.assertEqual(result.metrics["navigation_fallback_attempts"], 1)
        self.assertEqual(result.metrics["attempts"][0]["navigation_profile"], "governed_mobility")
        self.assertFalse(result.metrics["attempts"][0]["navigation_fallback"]["result"]["success"])

    def test_ambiguous_governance_replans_to_an_alternative_candidate(self):
        first = (5, 64, 0)
        second = (12, 64, 0)
        body = ResourceBody([(first, "dirt"), (second, "dirt")])
        navigator = RecordingNavigator(
            body,
            [(5, 65, -1), (12, 65, -1)],
        )
        work = RecordingWork(
            [
                ToolResult(False, "break_denied:structure_risk_unknown", True),
                ToolResult(True, "collected", False, metrics={"collected_total": 1}),
            ]
        )
        runtime = ResourceCollectionTransactions(body, navigator, work)

        result = runtime.collect_block_domain(
            block_types=("dirt",),
            expected_drops=("dirt",),
            remaining_count=1,
            config=ResourceCollectionConfig(candidate_budget=2, mutation_budget=2),
        )

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(len(work.calls), 2)
        self.assertEqual(work.calls[0][0], first)
        self.assertEqual(work.calls[1][0], second)
        self.assertNotIn("inspection", result.metrics)

    def test_ambiguous_governance_skips_candidates_then_requests_inspection(self):
        target = (5, 64, 0)
        selected = (5, 65, -1)
        body = ResourceBody([(target, "dirt")])
        navigator = RecordingNavigator(body, [selected])
        work = RecordingWork(
            [
                ToolResult(
                    False,
                    "break_denied:structure_risk_unknown",
                    True,
                    metrics={"evidence_state": "needs_inspection"},
                )
            ]
        )
        runtime = ResourceCollectionTransactions(body, navigator, work)

        result = runtime.collect_block_domain(
            block_types=("dirt",),
            expected_drops=("dirt",),
            remaining_count=1,
            config=ResourceCollectionConfig(candidate_budget=1, mutation_budget=2),
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "needs_inspection")
        self.assertTrue(result.can_retry)
        self.assertEqual(result.metrics["inspection"]["state"], "needs_inspection")
        self.assertEqual(result.metrics["inspection"]["candidate_count"], 1)
        self.assertEqual(result.metrics["inspection"]["candidates"][0]["target"], list(target))
        self.assertEqual(len(work.calls), 1)
        self.assertEqual(work.calls[0][0], target)

    def test_dry_egress_terminal_prevents_resource_search_and_navigation(self):
        body = ResourceBody([((5, 64, 0), "dirt")])
        navigator = RecordingNavigator(body, [(5, 65, -1)])
        work = RecordingWork(
            egress_outcomes=[ToolResult(False, "dry_egress_unavailable", True)]
        )
        runtime = ResourceCollectionTransactions(body, navigator, work)

        result = runtime.collect_block_domain(
            block_types=("dirt",),
            expected_drops=("dirt",),
            remaining_count=1,
            config=ResourceCollectionConfig(candidate_budget=1, mutation_budget=1),
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "resource_dry_egress_unavailable")
        self.assertTrue(result.can_retry)
        self.assertEqual(navigator.calls, [])
        self.assertEqual(work.calls, [])
        self.assertEqual(result.metrics["last_failure"]["reason"], "dry_egress_unavailable")

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

    def test_failed_tree_does_not_hide_lower_trunk_in_other_tree(self):
        first = (5, 64, 0)
        second_high = (20, 68, 0)
        second_low = (20, 64, 0)
        body = ResourceBody(
            [
                (first, "oak_log"),
                (second_high, "oak_log"),
                (second_low, "oak_log"),
            ]
        )
        navigator = RecordingNavigator(
            body,
            [(5, 65, -1), (20, 65, -1)],
            outcomes=[(False, "budget_exceeded"), (True, "arrived")],
        )
        runtime = ResourceCollectionTransactions(body, navigator, RecordingWork())

        result = runtime.collect_block_domain(
            block_types=("oak_log",),
            expected_drops=("oak_log",),
            remaining_count=1,
            config=ResourceCollectionConfig(candidate_budget=2, mutation_budget=1),
        )

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual([call[0] for call in runtime.work.calls], [second_low])
        self.assertEqual(
            result.metrics["searches"][1]["active_candidates"],
            [list(second_low)],
        )
        self.assertEqual(result.metrics["candidate_blacklist"], [list(first)])

    def test_navigation_budget_expands_tree_domain_before_candidate_budget_terminal(self):
        high = (5, 70, 0)
        lower = (5, 64, 0)
        body = ScopedTreeResourceBody(
            primary_targets=[(high, "oak_log")],
            tree_targets=[(lower, "oak_log")],
        )
        navigator = RecordingNavigator(
            body,
            [(5, 71, 0), (1, 65, -1)],
            outcomes=[(False, "budget_exceeded"), (True, "arrived")],
        )
        runtime = ResourceCollectionTransactions(body, navigator, RecordingWork())

        result = runtime.collect_block_domain(
            block_types=("oak_log",),
            expected_drops=("oak_log",),
            remaining_count=1,
            config=ResourceCollectionConfig(candidate_budget=1, mutation_budget=1),
        )

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual([call[0] for call in runtime.work.calls], [lower])
        self.assertEqual(
            result.metrics["attempts"][0]["tree_domain_retarget"]["candidates"],
            [list(lower)],
        )
        self.assertEqual(result.metrics["searches"][1]["active_candidates"], [list(lower)])
        self.assertEqual(result.metrics["candidate_blacklist"], [list(high)])

    def test_failed_tree_retarget_preserves_untried_trunks(self):
        high = (5, 70, 0)
        lower_trunks = [(5, y, 0) for y in (64, 65, 66)]
        body = ScopedTreeResourceBody(
            primary_targets=[(high, "oak_log")],
            tree_targets=[(pos, "oak_log") for pos in lower_trunks],
        )
        navigator = RecordingNavigator(
            body,
            [(5, 71, 0), (5, 65, -1)],
            outcomes=[(False, "budget_exceeded"), (True, "arrived")],
        )
        runtime = ResourceCollectionTransactions(body, navigator, RecordingWork())

        result = runtime.collect_block_domain(
            block_types=("oak_log",),
            expected_drops=("oak_log",),
            remaining_count=1,
            config=ResourceCollectionConfig(candidate_budget=1, mutation_budget=1),
        )

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(len(runtime.work.calls), 1)
        self.assertIn(runtime.work.calls[0][0], lower_trunks)
        self.assertEqual(len(navigator.calls), 2)
        self.assertEqual(
            result.metrics["attempts"][0]["tree_domain_retarget"]["candidates"],
            [list(pos) for pos in (lower_trunks[1], lower_trunks[0], lower_trunks[2])],
        )

    def test_empty_tree_domain_preserves_candidate_exhaustion_terminal(self):
        high = (5, 72, 0)
        body = ScopedTreeResourceBody(
            primary_targets=[(high, "oak_log")],
            tree_targets=[],
        )
        navigator = RecordingNavigator(
            body,
            [(5, 71, 0)],
            outcomes=[(False, "budget_exceeded")],
        )
        runtime = ResourceCollectionTransactions(body, navigator, RecordingWork())

        result = runtime.collect_block_domain(
            block_types=("oak_log",),
            expected_drops=("oak_log",),
            remaining_count=1,
            config=ResourceCollectionConfig(candidate_budget=1, mutation_budget=1),
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "resource_candidate_domain_exhausted")
        self.assertEqual(runtime.work.calls, [])
        tree = result.metrics["attempts"][0]["tree_domain_retarget"]
        self.assertEqual(tree["candidates"], [])
        self.assertEqual(tree["search_result"]["reason"], "tree_domain_log_not_found")

    def test_no_path_uses_one_body_mobility_egress_before_replanning(self):
        target = (5, 64, 0)
        body = ResourceBody([(target, "dirt")])
        navigator = RecordingNavigator(
            body,
            [(5, 65, -1), (5, 65, -1)],
            outcomes=[(False, "no_path"), (True, "arrived")],
        )
        egress_calls = []

        def egress(timeout_s):
            egress_calls.append(timeout_s)
            body.state_pos = (16.5, 65.0, 0.5)
            return ToolResult(
                True,
                "surface_reached",
                False,
                metrics={"final_pos": [16, 65, 0], "terminal_surface_verified": True},
            )

        work = RecordingWork()
        runtime = ResourceCollectionTransactions(body, navigator, work, mobility_egress=egress)

        result = runtime.collect_block_domain(
            block_types=("dirt",),
            expected_drops=("dirt",),
            remaining_count=1,
            config=ResourceCollectionConfig(candidate_budget=1, mutation_budget=1),
        )

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(egress_calls, [30.0])
        self.assertEqual(len(navigator.calls), 2)
        self.assertEqual([call[0] for call in work.calls], [target])
        self.assertTrue(result.metrics["attempts"][0]["mobility_egress"]["success"])

    def test_successful_egress_preserves_untried_candidate_domain(self):
        first = (5, 64, 0)
        second = (8, 64, 0)
        body = EgressRepositioningResourceBody([(first, "dirt"), (second, "dirt")])
        navigator = RecordingNavigator(
            body,
            [(5, 65, -1), (8, 65, -1)],
            outcomes=[(False, "no_path"), (True, "arrived")],
        )

        def egress(timeout_s):
            body.state_pos = (16.5, 65.0, 0.5)
            return ToolResult(True, "surface_reached", False)

        runtime = ResourceCollectionTransactions(
            body,
            navigator,
            RecordingWork(),
            mobility_egress=egress,
        )

        result = runtime.collect_block_domain(
            block_types=("dirt",),
            expected_drops=("dirt",),
            remaining_count=1,
            config=ResourceCollectionConfig(candidate_budget=2, mutation_budget=1),
        )

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual([call[0] for call in runtime.work.calls], [second])
        self.assertEqual(
            result.metrics["attempts"][1]["selected_targets"],
            [list(second)],
        )
        self.assertIn(list(first), result.metrics["candidate_blacklist"])

    def test_navigation_budget_exhaustion_prioritizes_untried_tree_trunk(self):
        trunk = tuple((5, y, 0) for y in range(64, 68))
        far_tree = (20, 64, 0)
        targets = [(target, "oak_log") for target in trunk]
        targets.append((far_tree, "oak_log"))
        body = ScopedTreeResourceBody(targets, [])
        navigator = RecordingNavigator(
            body,
            [(5, 65, -1), (5, 65, -1)],
            outcomes=[(False, "budget_exceeded"), (True, "arrived")],
        )
        work = RecordingWork()
        runtime = ResourceCollectionTransactions(body, navigator, work)

        result = runtime.collect_block_domain(
            block_types=("oak_log",),
            expected_drops=("oak_log",),
            remaining_count=1,
            config=ResourceCollectionConfig(candidate_budget=2, mutation_budget=1),
        )

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(len(navigator.calls), 2)
        self.assertEqual([call[0] for call in work.calls], [trunk[1]])
        self.assertEqual(
            result.metrics["searches"][0]["active_candidates"],
            [list(trunk[0]), list(far_tree)],
        )
        self.assertEqual(
            result.metrics["searches"][1]["active_candidates"],
            [list(trunk[1])],
        )
        self.assertEqual(result.metrics["candidate_blacklist"], [list(trunk[0])])

    def test_route_only_no_path_exhaustion_preserves_resource_navigation_terminal(self):
        first = (5, 64, 0)
        second = (8, 64, 0)
        body = ResourceBody([(first, "dirt"), (second, "dirt")])
        navigator = RecordingNavigator(
            body,
            [(5, 65, -1), (8, 65, -1)],
            outcomes=[(False, "no_path"), (False, "no_path")],
        )
        work = RecordingWork()
        runtime = ResourceCollectionTransactions(body, navigator, work)

        result = runtime.collect_block_domain(
            block_types=("dirt",),
            expected_drops=("dirt",),
            remaining_count=1,
            config=ResourceCollectionConfig(candidate_budget=2, mutation_budget=1),
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "resource_navigation_no_path")
        self.assertTrue(result.can_retry)
        self.assertEqual(work.calls, [])
        self.assertEqual(result.metrics["navigation_failure_reasons"], ["no_path", "no_path"])
        self.assertEqual([call[1]["config"].allow_swim for call in navigator.calls], [False, False])

    def test_navigation_budget_exhaustion_remains_generic_resource_budget_terminal(self):
        first = (5, 64, 0)
        second = (8, 64, 0)
        third = (11, 64, 0)
        body = ResourceBody([(first, "dirt"), (second, "dirt"), (third, "dirt")])
        navigator = RecordingNavigator(
            body,
            [(5, 65, -1), (8, 65, -1)],
            outcomes=[(False, "budget_exceeded"), (False, "budget_exceeded")],
        )
        runtime = ResourceCollectionTransactions(body, navigator, RecordingWork())

        result = runtime.collect_block_domain(
            block_types=("dirt",),
            expected_drops=("dirt",),
            remaining_count=1,
            config=ResourceCollectionConfig(candidate_budget=2, mutation_budget=1),
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "resource_domain_budget_exhausted")
        self.assertNotEqual(result.reason, "resource_navigation_no_path")

    def test_candidate_batch_spans_different_spatial_regions(self):
        nearest = (5, 64, 0)
        near_middle = (6, 64, 5)
        middle = (7, 64, 10)
        far_region = (8, 64, 30)
        targets = (nearest, near_middle, middle, far_region)
        body = ResourceBody([(target, "oak_log") for target in targets])
        navigator = RecordingNavigator(body, [(5, 65, -1)])
        work = RecordingWork()
        runtime = ResourceCollectionTransactions(body, navigator, work)

        result = runtime.collect_block_domain(
            block_types=("oak_log",),
            expected_drops=("oak_log",),
            remaining_count=1,
            config=ResourceCollectionConfig(candidate_budget=3, mutation_budget=1, find_limit=4),
        )

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(
            result.metrics["searches"][0]["active_candidates"],
            [list(nearest), list(far_region), list(middle)],
        )

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
