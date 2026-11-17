import unittest

from minebot.game.governance import BreakContext, GovernancePolicy, Region
from minebot.game.navigation import GoalNear, GoalXZ, GridCell, GridWorld, NavigationCostModel, SegmentedNavigator


def grid(width, depth, y=64):
    return {(x, y, z): GridCell() for x in range(width) for z in range(depth)}


class SegmentedNavigationTests(unittest.TestCase):
    def test_next_segment_arrived_for_complete_local_path(self):
        cells = grid(4, 1)
        policy = GovernancePolicy(natural_regions=[Region("mine", (0, 0, 0), (3, 100, 0))])
        nav = SegmentedNavigator(GridWorld(cells), NavigationCostModel(policy))

        segment = nav.next_segment((0, 64, 0), (3, 64, 0))

        self.assertEqual(segment.status, "arrived")
        self.assertEqual(segment.target, (3, 64, 0))
        self.assertTrue(segment.plan.success)
        self.assertTrue(segment.recheck.ok)

    def test_next_segment_advanced_for_partial_path(self):
        cells = grid(8, 1)
        policy = GovernancePolicy(natural_regions=[Region("mine", (0, 0, 0), (7, 100, 0))])
        nav = SegmentedNavigator(GridWorld(cells), NavigationCostModel(policy))

        segment = nav.next_segment((0, 64, 0), (7, 64, 0), max_expansions=3, min_partial_progress=2)

        self.assertEqual(segment.status, "advanced")
        self.assertEqual(segment.target, (2, 64, 0))
        self.assertFalse(segment.plan.success)
        self.assertEqual(segment.plan.reason, "partial")
        self.assertTrue(segment.recheck.ok)
        self.assertNotIn("tail_trimmed_steps", segment.plan.diagnostics)

    def test_next_segment_blocked_when_no_safe_path_or_partial(self):
        cells = grid(3, 1)
        cells[(1, 64, 0)] = GridCell(block_type="stone", walkable=False)
        nav = SegmentedNavigator(GridWorld(cells), NavigationCostModel(GovernancePolicy()))

        segment = nav.next_segment((0, 64, 0), (2, 64, 0), break_context=BreakContext.TRAVEL)

        self.assertEqual(segment.status, "blocked")
        self.assertIsNone(segment.target)
        self.assertEqual(segment.plan.reason, "no_path")

    def test_next_segment_replan_required_when_recheck_fails(self):
        cells = grid(3, 1)
        cells[(1, 64, 0)] = GridCell(block_type="stone", walkable=False)
        initial_policy = GovernancePolicy(natural_regions=[Region("mine", (0, 0, 0), (2, 100, 0))])
        nav = SegmentedNavigator(GridWorld(cells), NavigationCostModel(initial_policy))

        protected_policy = GovernancePolicy(
            natural_regions=[Region("mine", (0, 0, 0), (2, 100, 0))],
            protected_regions=[Region("new_build", (1, 0, 0), (1, 100, 0))],
        )

        segment = nav.next_segment(
            (0, 64, 0),
            (2, 64, 0),
            break_context=BreakContext.TRAVEL,
            recheck_costs=NavigationCostModel(protected_policy),
        )

        self.assertEqual(segment.status, "replan_required")
        self.assertIsNone(segment.target)
        self.assertTrue(segment.plan.path)
        self.assertFalse(segment.recheck.ok)
        self.assertEqual(segment.recheck.reason, "break_denied:protected_region")

    def test_next_segment_accepts_typed_near_goal(self):
        cells = grid(8, 1)
        policy = GovernancePolicy(natural_regions=[Region("mine", (0, 0, 0), (7, 100, 0))])
        nav = SegmentedNavigator(GridWorld(cells), NavigationCostModel(policy))

        segment = nav.next_segment((0, 64, 0), GoalNear((6, 64, 0), radius=2))

        self.assertEqual(segment.status, "arrived")
        self.assertEqual(segment.target, (4, 64, 0))
        self.assertEqual(segment.plan.diagnostics["goal"]["kind"], "near")

    def test_next_segment_accepts_typed_xz_goal(self):
        cells = grid(4, 1)
        policy = GovernancePolicy(natural_regions=[Region("mine", (0, 0, 0), (3, 100, 0))])
        nav = SegmentedNavigator(GridWorld(cells), NavigationCostModel(policy))

        segment = nav.next_segment((0, 64, 0), GoalXZ(3, 0))

        self.assertEqual(segment.status, "arrived")
        self.assertEqual(segment.target, (3, 64, 0))
        self.assertEqual(segment.plan.diagnostics["goal"]["kind"], "xz")

    def test_next_segment_passes_previous_segment_backtrack_favoring(self):
        cells = grid(3, 3)
        cells[(1, 64, 1)] = GridCell(block_type="stone", walkable=False)
        policy = GovernancePolicy(natural_regions=[Region("mine", (0, 0, 0), (2, 100, 2))])
        nav = SegmentedNavigator(GridWorld(cells), NavigationCostModel(policy))

        segment = nav.next_segment(
            (0, 64, 0),
            (2, 64, 2),
            previous_segment=((1, 64, 0), (2, 64, 0), (2, 64, 1)),
            backtrack_cost_factor=0.5,
        )

        self.assertEqual(segment.status, "arrived")
        self.assertEqual([step.pos for step in segment.plan.path], [(1, 64, 0), (2, 64, 0), (2, 64, 1), (2, 64, 2)])
        self.assertEqual([step.cost for step in segment.plan.path[:3]], [0.5, 0.5, 0.5])

    def test_next_segment_reports_unloaded_boundary_as_blocked_reason(self):
        nav = SegmentedNavigator(GridWorld({(0, 64, 0): GridCell()}), NavigationCostModel(GovernancePolicy()))

        segment = nav.next_segment((0, 64, 0), (10, 64, 0), unloaded_boundary_limit=2)

        self.assertEqual(segment.status, "blocked")
        self.assertIsNone(segment.target)
        self.assertEqual(segment.plan.reason, "unloaded_boundary")
        self.assertEqual(segment.diagnostics["reason"], "unloaded_boundary")

    def test_next_segment_advances_to_partial_target_at_unloaded_boundary(self):
        cells = grid(4, 1)
        policy = GovernancePolicy(natural_regions=[Region("mine", (0, 0, 0), (3, 100, 0))])
        nav = SegmentedNavigator(GridWorld(cells), NavigationCostModel(policy))

        segment = nav.next_segment(
            (0, 64, 0),
            (10, 64, 0),
            min_partial_progress=2,
            unloaded_boundary_limit=40,
        )

        self.assertEqual(segment.status, "advanced")
        self.assertEqual(segment.target, (2, 64, 0))
        self.assertEqual(segment.plan.reason, "partial")
        self.assertEqual(segment.plan.diagnostics["stop_reason"], "unloaded_boundary")
        self.assertEqual(segment.plan.diagnostics["original_partial_target"], [3, 64, 0])
        self.assertEqual(segment.plan.diagnostics["partial_target"], [2, 64, 0])
        self.assertEqual(segment.plan.diagnostics["tail_trimmed_steps"], 1)

    def test_next_segment_can_disable_unloaded_boundary_tail_trim(self):
        cells = grid(4, 1)
        policy = GovernancePolicy(natural_regions=[Region("mine", (0, 0, 0), (3, 100, 0))])
        nav = SegmentedNavigator(GridWorld(cells), NavigationCostModel(policy))

        segment = nav.next_segment(
            (0, 64, 0),
            (10, 64, 0),
            min_partial_progress=2,
            unloaded_boundary_limit=40,
            partial_tail_trim=0,
        )

        self.assertEqual(segment.status, "advanced")
        self.assertEqual(segment.target, (3, 64, 0))
        self.assertNotIn("tail_trimmed_steps", segment.plan.diagnostics)


if __name__ == "__main__":
    unittest.main()
