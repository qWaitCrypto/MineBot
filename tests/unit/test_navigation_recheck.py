import unittest

from minebot.game.governance import BreakContext, GovernancePolicy, Region
from minebot.game.navigation import (
    AStarPlanner,
    GridCell,
    GridWorld,
    MoveKind,
    NavigationCostModel,
    PathRechecker,
    PathResult,
    PathStep,
)


def grid(width, depth, y=64):
    return {(x, y, z): GridCell() for x in range(width) for z in range(depth)}


class NavigationRecheckTests(unittest.TestCase):
    def test_recheck_accepts_unchanged_path(self):
        cells = grid(4, 1)
        policy = GovernancePolicy(natural_regions=[Region("mine", (0, 0, 0), (3, 100, 0))])
        costs = NavigationCostModel(policy)
        path = AStarPlanner(GridWorld(cells), costs).plan((0, 64, 0), (3, 64, 0))

        result = PathRechecker(GridWorld(cells), costs).recheck(path, lookahead=3)

        self.assertTrue(result.ok)
        self.assertEqual(result.reason, "valid")
        self.assertEqual(result.checked, 3)

    def test_recheck_rejects_newly_unloaded_step(self):
        cells = grid(4, 1)
        policy = GovernancePolicy(natural_regions=[Region("mine", (0, 0, 0), (3, 100, 0))])
        costs = NavigationCostModel(policy)
        path = AStarPlanner(GridWorld(cells), costs).plan((0, 64, 0), (3, 64, 0))
        current = dict(cells)
        del current[(2, 64, 0)]

        result = PathRechecker(GridWorld(current), costs).recheck(path, lookahead=3)

        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "unloaded")
        self.assertEqual(result.failing_step.pos, (2, 64, 0))

    def test_recheck_rejects_step_that_became_protected_break(self):
        cells = grid(3, 1)
        cells[(1, 64, 0)] = GridCell(block_type="stone", walkable=False)
        initial_policy = GovernancePolicy(natural_regions=[Region("mine", (0, 0, 0), (2, 100, 0))])
        path = AStarPlanner(GridWorld(cells), NavigationCostModel(initial_policy)).plan(
            (0, 64, 0), (2, 64, 0), break_context=BreakContext.TRAVEL
        )
        protected_policy = GovernancePolicy(
            natural_regions=[Region("mine", (0, 0, 0), (2, 100, 0))],
            protected_regions=[Region("new_build", (1, 0, 0), (1, 100, 0))],
        )

        result = PathRechecker(GridWorld(cells), NavigationCostModel(protected_policy)).recheck(
            path, break_context=BreakContext.TRAVEL
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "break_denied:protected_region")
        self.assertEqual(result.failing_step.pos, (1, 64, 0))

    def test_recheck_rejects_break_step_when_block_type_changed(self):
        cells = grid(3, 1)
        cells[(1, 64, 0)] = GridCell(block_type="stone", walkable=False)
        policy = GovernancePolicy(natural_regions=[Region("mine", (0, 0, 0), (2, 100, 0))])
        costs = NavigationCostModel(policy)
        path = AStarPlanner(GridWorld(cells), costs).plan(
            (0, 64, 0), (2, 64, 0), break_context=BreakContext.TRAVEL
        )
        current = dict(cells)
        current[(1, 64, 0)] = GridCell(block_type="diamond_ore", walkable=False)

        result = PathRechecker(GridWorld(current), costs).recheck(path, break_context=BreakContext.TRAVEL)

        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "block_changed")
        self.assertEqual(result.diagnostics["planned_block_type"], "stone")
        self.assertEqual(result.diagnostics["observed_block_type"], "diamond_ore")

    def test_recheck_rejects_break_step_when_gravity_stack_appears(self):
        cells = grid(3, 1)
        cells[(1, 64, 0)] = GridCell(block_type="sand", walkable=False)
        policy = GovernancePolicy(natural_regions=[Region("mine", (0, 0, 0), (2, 100, 0))])
        costs = NavigationCostModel(policy)
        path = AStarPlanner(GridWorld(cells), costs).plan(
            (0, 64, 0), (2, 64, 0), break_context=BreakContext.TRAVEL
        )
        current = dict(cells)
        current[(1, 65, 0)] = GridCell(block_type="sand", walkable=False)

        result = PathRechecker(GridWorld(current), costs).recheck(path, break_context=BreakContext.TRAVEL)

        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "break_denied:gravity_stack")
        self.assertEqual(result.failing_step.pos, (1, 64, 0))

    def test_recheck_rejects_break_step_when_gravel_stack_appears(self):
        cells = grid(3, 1)
        cells[(1, 64, 0)] = GridCell(block_type="gravel", walkable=False)
        policy = GovernancePolicy(natural_regions=[Region("mine", (0, 0, 0), (2, 100, 0))])
        costs = NavigationCostModel(policy)
        path = AStarPlanner(GridWorld(cells), costs).plan(
            (0, 64, 0), (2, 64, 0), break_context=BreakContext.TRAVEL
        )
        current = dict(cells)
        current[(1, 65, 0)] = GridCell(block_type="gravel", walkable=False)

        result = PathRechecker(GridWorld(current), costs).recheck(path, break_context=BreakContext.TRAVEL)

        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "break_denied:gravity_stack")
        self.assertEqual(result.failing_step.pos, (1, 64, 0))

    def test_recheck_rejects_break_step_when_liquid_becomes_adjacent_to_gravity_block(self):
        cells = grid(3, 1)
        cells[(1, 64, 0)] = GridCell(block_type="sand", walkable=False)
        policy = GovernancePolicy(natural_regions=[Region("mine", (0, 0, 0), (2, 100, 0))])
        costs = NavigationCostModel(policy)
        path = AStarPlanner(GridWorld(cells), costs).plan(
            (0, 64, 0), (2, 64, 0), break_context=BreakContext.TRAVEL
        )
        current = dict(cells)
        current[(1, 64, 1)] = GridCell(block_type="water", walkable=False, liquid=True)

        result = PathRechecker(GridWorld(current), costs).recheck(path, break_context=BreakContext.TRAVEL)

        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "break_denied:gravity_liquid_adjacent")
        self.assertEqual(result.failing_step.pos, (1, 64, 0))

    def test_recheck_rejects_break_step_when_vertical_liquid_becomes_adjacent_to_gravity_block(self):
        cells = grid(3, 1)
        cells[(1, 64, 0)] = GridCell(block_type="sand", walkable=False)
        policy = GovernancePolicy(natural_regions=[Region("mine", (0, 0, 0), (2, 100, 0))])
        costs = NavigationCostModel(policy)
        path = AStarPlanner(GridWorld(cells), costs).plan(
            (0, 64, 0), (2, 64, 0), break_context=BreakContext.TRAVEL
        )
        current = dict(cells)
        current[(1, 65, 0)] = GridCell(block_type="water", walkable=False, liquid=True)

        result = PathRechecker(GridWorld(current), costs).recheck(path, break_context=BreakContext.TRAVEL)

        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "break_denied:gravity_liquid_adjacent")
        self.assertEqual(result.failing_step.pos, (1, 64, 0))

    def test_recheck_rejects_fall_step_that_became_unsafe(self):
        cells = {
            (0, 64, 0): GridCell(),
            (0, 63, 0): GridCell(fall_depth=2),
        }
        costs = NavigationCostModel(GovernancePolicy())
        path = AStarPlanner(GridWorld(cells), costs).plan((0, 64, 0), (0, 63, 0))
        current = dict(cells)
        current[(0, 63, 0)] = GridCell(fall_depth=6)

        result = PathRechecker(GridWorld(current), costs).recheck(path)

        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "fall_denied:unsafe_depth")
        self.assertEqual(result.failing_step.pos, (0, 63, 0))

    def test_recheck_applies_virtual_place_before_supported_walk(self):
        cells = {
            (1, 63, 0): GridCell(block_type="air", walkable=True),
            (1, 64, 0): GridCell(requires_support=True),
        }
        policy = GovernancePolicy(natural_regions=[Region("mine", (0, 0, 0), (2, 100, 0))])
        costs = NavigationCostModel(policy)
        path = PathResult(
            success=True,
            reason="arrived",
            path=(
                PathStep(
                    pos=(1, 63, 0),
                    move=MoveKind.PLACE,
                    cost=NavigationCostModel.PLACE_COST,
                    reason="place_allowed:allowed_place",
                    block_type="minecraft:cobblestone",
                    virtual_effect="place_solid",
                ),
                PathStep(pos=(1, 64, 0), move=MoveKind.WALK, cost=NavigationCostModel.WALK_COST, reason="walk"),
            ),
        )

        result = PathRechecker(GridWorld(cells), costs).recheck(path, lookahead=2)

        self.assertTrue(result.ok)
        self.assertEqual(result.reason, "valid")
        self.assertEqual(result.checked, 2)
        self.assertEqual(result.diagnostics["virtual_overlay"][0]["pos"], [1, 63, 0])
        self.assertEqual(result.diagnostics["virtual_overlay"][0]["block_type"], "minecraft:cobblestone")

    def test_recheck_virtual_overlay_respects_lookahead_boundary(self):
        cells = {
            (1, 63, 0): GridCell(block_type="air", walkable=True),
            (1, 64, 0): GridCell(requires_support=True),
        }
        policy = GovernancePolicy(natural_regions=[Region("mine", (0, 0, 0), (2, 100, 0))])
        costs = NavigationCostModel(policy)
        path = PathResult(
            success=True,
            reason="arrived",
            path=(
                PathStep(
                    pos=(1, 63, 0),
                    move=MoveKind.PLACE,
                    cost=NavigationCostModel.PLACE_COST,
                    reason="place_allowed:allowed_place",
                    block_type="minecraft:cobblestone",
                    virtual_effect="place_solid",
                ),
                PathStep(pos=(9, 64, 0), move=MoveKind.WALK, cost=NavigationCostModel.WALK_COST, reason="walk"),
            ),
        )

        result = PathRechecker(GridWorld(cells), costs).recheck(path, lookahead=1)

        self.assertTrue(result.ok)
        self.assertEqual(result.checked, 1)
        self.assertEqual(len(result.diagnostics["virtual_overlay"]), 1)

    def test_recheck_rejects_supported_walk_without_planned_or_real_support(self):
        cells = {
            (1, 63, 0): GridCell(block_type="air", walkable=True),
            (1, 64, 0): GridCell(requires_support=True),
        }
        costs = NavigationCostModel(GovernancePolicy(natural_regions=[Region("mine", (0, 0, 0), (2, 100, 0))]))
        path = PathResult(
            success=True,
            reason="arrived",
            path=(PathStep(pos=(1, 64, 0), move=MoveKind.WALK, cost=NavigationCostModel.WALK_COST, reason="walk"),),
        )

        result = PathRechecker(GridWorld(cells), costs).recheck(path)

        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "support_missing")
        self.assertEqual(result.failing_step.pos, (1, 64, 0))

    def test_recheck_applies_virtual_headroom_break_before_walk(self):
        cells = {
            (1, 64, 0): GridCell(headroom_block="stone"),
            (1, 65, 0): GridCell(block_type="stone", walkable=False),
        }
        costs = NavigationCostModel(GovernancePolicy(natural_regions=[Region("mine", (0, 0, 0), (2, 100, 0))]))
        path = PathResult(
            success=True,
            reason="arrived",
            path=(
                PathStep(
                    pos=(1, 65, 0),
                    move=MoveKind.BREAK,
                    cost=NavigationCostModel.NATURAL_BREAK_COST,
                    reason="break_allowed:allowed_natural",
                    block_type="stone",
                    virtual_effect="break_to_air",
                ),
                PathStep(pos=(1, 64, 0), move=MoveKind.WALK, cost=NavigationCostModel.WALK_COST, reason="walk"),
            ),
        )

        result = PathRechecker(GridWorld(cells), costs).recheck(path, lookahead=2)

        self.assertTrue(result.ok)
        self.assertEqual(result.reason, "valid")
        self.assertEqual(result.diagnostics["virtual_overlay"][0]["pos"], [1, 65, 0])
        self.assertEqual(result.diagnostics["virtual_overlay"][0]["block_type"], "air")

    def test_recheck_rejects_walk_when_headroom_was_not_planned_clear(self):
        cells = {
            (1, 64, 0): GridCell(headroom_block="stone"),
            (1, 65, 0): GridCell(block_type="stone", walkable=False),
        }
        costs = NavigationCostModel(GovernancePolicy(natural_regions=[Region("mine", (0, 0, 0), (2, 100, 0))]))
        path = PathResult(
            success=True,
            reason="arrived",
            path=(PathStep(pos=(1, 64, 0), move=MoveKind.WALK, cost=NavigationCostModel.WALK_COST, reason="walk"),),
        )

        result = PathRechecker(GridWorld(cells), costs).recheck(path)

        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "headroom_blocked")
        self.assertEqual(result.failing_step.pos, (1, 64, 0))

    def test_recheck_only_checks_requested_lookahead(self):
        cells = grid(5, 1)
        policy = GovernancePolicy(natural_regions=[Region("mine", (0, 0, 0), (4, 100, 0))])
        costs = NavigationCostModel(policy)
        path = AStarPlanner(GridWorld(cells), costs).plan((0, 64, 0), (4, 64, 0))
        current = dict(cells)
        del current[(4, 64, 0)]

        result = PathRechecker(GridWorld(current), costs).recheck(path, lookahead=2)

        self.assertTrue(result.ok)
        self.assertEqual(result.checked, 2)


if __name__ == "__main__":
    unittest.main()
