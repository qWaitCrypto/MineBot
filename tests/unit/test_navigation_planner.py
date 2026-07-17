import unittest

from minebot.game.governance import BreakContext, GovernancePolicy, Region
from minebot.game.navigation import (
    AStarPlanner,
    GoalAvoid,
    GoalBlock,
    GoalComposite,
    GoalNear,
    GoalXZ,
    GoalYLevel,
    GridCell,
    GridWorld,
    MoveKind,
    NavigationCostModel,
    PathStep,
    VirtualBlockOverlay,
)


def grid(width, depth, y=64):
    return {(x, y, z): GridCell() for x in range(width) for z in range(depth)}


class NavigationPlannerTests(unittest.TestCase):
    def test_astar_routes_around_protected_break_candidate(self):
        cells = grid(3, 3)
        cells[(1, 64, 1)] = GridCell(block_type="stone", walkable=False)
        policy = GovernancePolicy(
            natural_regions=[Region("mine", (0, 0, 0), (2, 100, 2))],
            protected_regions=[Region("protected", (1, 0, 1), (1, 100, 1))],
        )
        planner = AStarPlanner(GridWorld(cells), NavigationCostModel(policy))

        result = planner.plan((0, 64, 1), (2, 64, 1), break_context=BreakContext.TRAVEL)

        self.assertTrue(result.success)
        self.assertNotIn((1, 64, 1), [step.pos for step in result.path])
        self.assertTrue(all(step.move == MoveKind.WALK for step in result.path))

    def test_astar_reports_no_path_when_only_route_has_unknown_provenance_break(self):
        cells = grid(3, 1)
        cells[(1, 64, 0)] = GridCell(block_type="stone", walkable=False)
        policy = GovernancePolicy()
        planner = AStarPlanner(GridWorld(cells), NavigationCostModel(policy))

        result = planner.plan((0, 64, 0), (2, 64, 0), break_context=BreakContext.TRAVEL)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "no_path")
        self.assertGreater(result.diagnostics["blocked_count"], 0)
        self.assertTrue(
            any(item["reason"] == "break_denied:unknown_provenance" for item in result.diagnostics["blocked"])
        )

    def test_astar_uses_break_step_when_governance_allows_travel_break(self):
        cells = grid(3, 1)
        cells[(1, 64, 0)] = GridCell(block_type="stone", walkable=False)
        policy = GovernancePolicy(natural_regions=[Region("mine", (0, 0, 0), (2, 100, 0))])
        planner = AStarPlanner(GridWorld(cells), NavigationCostModel(policy))

        result = planner.plan((0, 64, 0), (2, 64, 0), break_context=BreakContext.TRAVEL)

        self.assertTrue(result.success)
        self.assertEqual([step.pos for step in result.path], [(1, 64, 0), (2, 64, 0)])
        self.assertEqual(result.path[0].move, MoveKind.BREAK)
        self.assertEqual(result.path[0].reason, "break_allowed:allowed_natural")
        self.assertEqual(result.path[1].move, MoveKind.WALK)

    def test_astar_uses_break_step_for_single_sand_when_no_stack_exists(self):
        cells = grid(3, 1)
        cells[(1, 64, 0)] = GridCell(block_type="sand", walkable=False)
        policy = GovernancePolicy(natural_regions=[Region("mine", (0, 0, 0), (2, 100, 0))])
        planner = AStarPlanner(GridWorld(cells), NavigationCostModel(policy))

        result = planner.plan((0, 64, 0), (2, 64, 0), break_context=BreakContext.TRAVEL)

        self.assertTrue(result.success)
        self.assertEqual([step.pos for step in result.path], [(1, 64, 0), (2, 64, 0)])
        self.assertEqual(result.path[0].move, MoveKind.BREAK)
        self.assertEqual(result.path[0].reason, "break_allowed:allowed_natural")
        self.assertEqual(result.path[1].move, MoveKind.WALK)

    def test_astar_uses_break_step_for_single_gravel_when_no_stack_exists(self):
        cells = grid(3, 1)
        cells[(1, 64, 0)] = GridCell(block_type="gravel", walkable=False)
        policy = GovernancePolicy(natural_regions=[Region("mine", (0, 0, 0), (2, 100, 0))])
        planner = AStarPlanner(GridWorld(cells), NavigationCostModel(policy))

        result = planner.plan((0, 64, 0), (2, 64, 0), break_context=BreakContext.TRAVEL)

        self.assertTrue(result.success)
        self.assertEqual([step.pos for step in result.path], [(1, 64, 0), (2, 64, 0)])
        self.assertEqual(result.path[0].move, MoveKind.BREAK)
        self.assertEqual(result.path[0].reason, "break_allowed:allowed_natural")
        self.assertEqual(result.path[1].move, MoveKind.WALK)

    def test_astar_refuses_breaking_bottom_of_gravity_stack(self):
        cells = grid(3, 1)
        cells[(1, 64, 0)] = GridCell(block_type="sand", walkable=False)
        cells[(1, 65, 0)] = GridCell(block_type="sand", walkable=False)
        policy = GovernancePolicy(natural_regions=[Region("mine", (0, 0, 0), (2, 100, 0))])
        planner = AStarPlanner(GridWorld(cells), NavigationCostModel(policy))

        result = planner.plan((0, 64, 0), (2, 64, 0), break_context=BreakContext.TRAVEL)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "no_path")
        self.assertTrue(any(item["reason"] == "break_denied:gravity_stack" for item in result.diagnostics["blocked"]))

    def test_astar_refuses_breaking_gravity_block_adjacent_to_liquid(self):
        cells = grid(3, 1)
        cells[(1, 64, 0)] = GridCell(block_type="sand", walkable=False)
        cells[(1, 64, 1)] = GridCell(block_type="water", walkable=False, liquid=True)
        policy = GovernancePolicy(natural_regions=[Region("mine", (0, 0, 0), (2, 100, 0))])
        planner = AStarPlanner(GridWorld(cells), NavigationCostModel(policy))

        result = planner.plan((0, 64, 0), (2, 64, 0), break_context=BreakContext.TRAVEL)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "no_path")
        self.assertTrue(
            any(item["reason"] == "break_denied:gravity_liquid_adjacent" for item in result.diagnostics["blocked"])
        )

    def test_astar_refuses_breaking_gravity_block_with_vertical_liquid_adjacency(self):
        cells = grid(3, 1)
        cells[(1, 64, 0)] = GridCell(block_type="sand", walkable=False)
        cells[(1, 63, 0)] = GridCell(block_type="water", walkable=False, liquid=True)
        policy = GovernancePolicy(natural_regions=[Region("mine", (0, 0, 0), (2, 100, 0))])
        planner = AStarPlanner(GridWorld(cells), NavigationCostModel(policy))

        result = planner.plan((0, 64, 0), (2, 64, 0), break_context=BreakContext.TRAVEL)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "no_path")
        self.assertTrue(
            any(item["reason"] == "break_denied:gravity_liquid_adjacent" for item in result.diagnostics["blocked"])
        )

    def test_astar_virtual_overlay_treats_planned_broken_support_as_missing(self):
        world = GridWorld({(1, 63, 0): GridCell(block_type="stone", walkable=False)})
        overlay = VirtualBlockOverlay().after_step(
            PathStep(
                pos=(1, 63, 0),
                move=MoveKind.BREAK,
                cost=NavigationCostModel.NATURAL_BREAK_COST,
                reason="break_allowed:allowed_natural",
                block_type="stone",
            )
        )

        virtual_support = overlay.cell_at(world, (1, 63, 0))

        self.assertIsNotNone(virtual_support)
        self.assertEqual(virtual_support.block_type, "air")
        self.assertTrue(virtual_support.walkable)

    def test_astar_virtual_overlay_can_place_support_then_walk(self):
        cells = {
            (0, 64, 0): GridCell(),
            (1, 63, 0): GridCell(block_type="air", walkable=True),
            (1, 64, 0): GridCell(requires_support=True),
        }
        policy = GovernancePolicy(natural_regions=[Region("mine", (0, 0, 0), (2, 100, 0))])
        planner = AStarPlanner(GridWorld(cells), NavigationCostModel(policy))

        result = planner.plan((0, 64, 0), (1, 64, 0), break_context=BreakContext.TRAVEL)

        self.assertTrue(result.success)
        self.assertEqual([step.move for step in result.path], [MoveKind.PLACE, MoveKind.WALK])
        self.assertEqual(result.path[0].pos, (1, 63, 0))
        self.assertEqual(result.path[0].virtual_effect, "place_solid")
        self.assertEqual(result.path[0].place_face, "west")
        self.assertEqual(result.path[1].pos, (1, 64, 0))

    def test_astar_virtual_overlay_can_clear_headroom_then_walk(self):
        cells = {
            (0, 64, 0): GridCell(),
            (1, 64, 0): GridCell(headroom_block="stone"),
            (1, 65, 0): GridCell(block_type="stone", walkable=False),
        }
        policy = GovernancePolicy(natural_regions=[Region("mine", (0, 0, 0), (2, 100, 0))])
        planner = AStarPlanner(GridWorld(cells), NavigationCostModel(policy))

        result = planner.plan((0, 64, 0), (1, 64, 0), break_context=BreakContext.TRAVEL)

        self.assertTrue(result.success)
        self.assertEqual([step.move for step in result.path], [MoveKind.BREAK, MoveKind.WALK])
        self.assertEqual(result.path[0].pos, (1, 65, 0))
        self.assertEqual(result.path[0].block_type, "stone")
        self.assertEqual(result.path[0].virtual_effect, "break_to_air")
        self.assertEqual(result.path[1].pos, (1, 64, 0))

    def test_astar_can_open_closed_fence_gate_then_walk(self):
        cells = {
            (0, 64, 0): GridCell(),
            (1, 64, 0): GridCell(block_type="oak_fence_gate", walkable=False),
            (2, 64, 0): GridCell(),
        }
        policy = GovernancePolicy(natural_regions=[Region("nav", (0, 0, 0), (2, 100, 0))])
        planner = AStarPlanner(GridWorld(cells), NavigationCostModel(policy))

        result = planner.plan((0, 64, 0), (2, 64, 0), break_context=BreakContext.TRAVEL)

        self.assertTrue(result.success)
        self.assertEqual([step.move for step in result.path], [MoveKind.OPEN, MoveKind.WALK, MoveKind.WALK])
        self.assertEqual(result.path[0].pos, (1, 64, 0))
        self.assertEqual(result.path[0].interaction_target, (1, 64, 0))
        self.assertEqual(result.path[0].virtual_effect, "open_passage")

    def test_astar_can_open_closed_door_then_walk(self):
        cells = {
            (0, 64, 0): GridCell(),
            (1, 64, 0): GridCell(block_type="oak_door", walkable=False, headroom_block="oak_door"),
            (1, 65, 0): GridCell(block_type="oak_door", walkable=False),
            (2, 64, 0): GridCell(),
        }
        policy = GovernancePolicy(natural_regions=[Region("nav", (0, 0, 0), (2, 100, 0))])
        planner = AStarPlanner(GridWorld(cells), NavigationCostModel(policy))

        result = planner.plan((0, 64, 0), (2, 64, 0), break_context=BreakContext.TRAVEL)

        self.assertTrue(result.success)
        self.assertEqual(result.path[0].move, MoveKind.OPEN)
        self.assertEqual(result.path[0].interaction_target, (1, 64, 0))
        self.assertEqual(result.path[1].move, MoveKind.WALK)
        self.assertEqual(result.path[2].move, MoveKind.WALK)

    def test_astar_refuses_protected_headroom_clearance_without_walk_collision(self):
        cells = {
            (0, 64, 0): GridCell(),
            (1, 64, 0): GridCell(headroom_block="stone"),
            (1, 65, 0): GridCell(block_type="stone", walkable=False),
            (2, 64, 0): GridCell(),
        }
        policy = GovernancePolicy(
            natural_regions=[Region("mine", (0, 0, 0), (2, 100, 0))],
            protected_regions=[Region("protected_ceiling", (1, 65, 0), (1, 65, 0))],
        )
        planner = AStarPlanner(GridWorld(cells), NavigationCostModel(policy))

        result = planner.plan((0, 64, 0), (2, 64, 0), break_context=BreakContext.TRAVEL)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "no_path")
        self.assertEqual(result.path, ())
        self.assertTrue(
            any(item["reason"] == "break_denied:protected_region" for item in result.diagnostics["blocked"])
        )

    def test_astar_path_context_refuses_to_strip_mine_ore_even_if_shortest(self):
        cells = grid(3, 1)
        cells[(1, 64, 0)] = GridCell(block_type="diamond_ore", walkable=False)
        policy = GovernancePolicy(natural_regions=[Region("mine", (0, 0, 0), (2, 100, 0))])
        planner = AStarPlanner(GridWorld(cells), NavigationCostModel(policy))

        result = planner.plan((0, 64, 0), (2, 64, 0), break_context=BreakContext.PATH)

        self.assertFalse(result.success)
        self.assertTrue(
            any(item["reason"] == "break_denied:path_no_terrain_break" for item in result.diagnostics["blocked"])
        )

    def test_astar_returns_partial_path_on_expansion_limit_with_meaningful_progress(self):
        cells = grid(8, 1)
        policy = GovernancePolicy(natural_regions=[Region("mine", (0, 0, 0), (7, 100, 0))])
        planner = AStarPlanner(GridWorld(cells), NavigationCostModel(policy))

        result = planner.plan((0, 64, 0), (7, 64, 0), max_expansions=3, min_partial_progress=2)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "partial")
        self.assertGreaterEqual(result.diagnostics["progress"], 2)
        self.assertEqual(result.diagnostics["original_goal"], [7, 64, 0])
        self.assertEqual(result.diagnostics["partial_backoff_coefficients"], [1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 10.0])
        self.assertIn(result.diagnostics["selected_coefficient"], result.diagnostics["partial_backoff_coefficients"])
        self.assertEqual([step.move for step in result.path], [MoveKind.WALK, MoveKind.WALK])

    def test_astar_default_partial_floor_matches_baritone_min_distance(self):
        cells = grid(8, 1)
        policy = GovernancePolicy(natural_regions=[Region("mine", (0, 0, 0), (7, 100, 0))])
        planner = AStarPlanner(GridWorld(cells), NavigationCostModel(policy))

        result = planner.plan((0, 64, 0), (7, 64, 0), max_expansions=3)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "expansion_limit")
        self.assertEqual(result.path, ())
        self.assertNotIn("partial_target", result.diagnostics)

    def test_astar_does_not_report_partial_when_progress_floor_not_met(self):
        cells = grid(8, 1)
        policy = GovernancePolicy(natural_regions=[Region("mine", (0, 0, 0), (7, 100, 0))])
        planner = AStarPlanner(GridWorld(cells), NavigationCostModel(policy))

        result = planner.plan((0, 64, 0), (7, 64, 0), max_expansions=2, min_partial_progress=3)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "expansion_limit")
        self.assertEqual(result.path, ())
        self.assertNotIn("partial_target", result.diagnostics)

    def test_astar_can_disable_partial_paths(self):
        cells = grid(8, 1)
        policy = GovernancePolicy(natural_regions=[Region("mine", (0, 0, 0), (7, 100, 0))])
        planner = AStarPlanner(GridWorld(cells), NavigationCostModel(policy))

        result = planner.plan((0, 64, 0), (7, 64, 0), max_expansions=3, allow_partial=False)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "expansion_limit")
        self.assertEqual(result.path, ())

    def test_goal_near_arrives_at_first_position_inside_radius(self):
        cells = grid(8, 1)
        policy = GovernancePolicy(natural_regions=[Region("mine", (0, 0, 0), (7, 100, 0))])
        planner = AStarPlanner(GridWorld(cells), NavigationCostModel(policy))

        result = planner.plan((0, 64, 0), GoalNear((6, 64, 0), radius=2))

        self.assertTrue(result.success)
        self.assertEqual(result.path[-1].pos, (4, 64, 0))
        self.assertEqual(result.diagnostics["goal"]["kind"], "near")
        self.assertEqual(result.diagnostics["goal"]["radius"], 2)

    def test_goal_xz_ignores_vertical_offset(self):
        cells = {(0, 64, 0): GridCell(), (1, 64, 0): GridCell(), (2, 64, 0): GridCell()}
        policy = GovernancePolicy(natural_regions=[Region("mine", (0, 0, 0), (2, 100, 0))])
        planner = AStarPlanner(GridWorld(cells), NavigationCostModel(policy))

        result = planner.plan((0, 64, 0), GoalXZ(2, 0))

        self.assertTrue(result.success)
        self.assertEqual(result.path[-1].pos, (2, 64, 0))
        self.assertEqual(result.diagnostics["goal"], {"kind": "xz", "x": 2, "z": 0})

    def test_goal_y_level_uses_pillar_for_same_column_vertical_ascent(self):
        cells = {(0, y, 0): GridCell() for y in range(64, 68)}
        policy = GovernancePolicy(natural_regions=[Region("mine", (0, 0, 0), (0, 100, 0))])
        planner = AStarPlanner(GridWorld(cells), NavigationCostModel(policy))

        result = planner.plan((0, 64, 0), GoalYLevel(67))

        self.assertTrue(result.success)
        self.assertEqual(result.path[-1].pos, (0, 67, 0))
        self.assertEqual([step.pos for step in result.path], [(0, 65, 0), (0, 66, 0), (0, 67, 0)])
        self.assertEqual([step.move for step in result.path], [MoveKind.PILLAR, MoveKind.PILLAR, MoveKind.PILLAR])
        self.assertFalse(result.path[0].safe_to_cancel)
        self.assertEqual(result.path[0].cancel_policy, "finish_or_abort_controller")

    def test_astar_uses_horizontal_ascend_for_one_block_step_up(self):
        cells = {
            (0, 64, 0): GridCell(),
            (1, 65, 0): GridCell(),
        }
        policy = GovernancePolicy(natural_regions=[Region("mine", (0, 0, 0), (1, 100, 0))])
        planner = AStarPlanner(GridWorld(cells), NavigationCostModel(policy))

        result = planner.plan((0, 64, 0), (1, 65, 0), allow_partial=False)

        self.assertTrue(result.success)
        self.assertEqual([step.pos for step in result.path], [(1, 65, 0)])
        self.assertEqual(result.path[0].move, MoveKind.ASCEND)
        self.assertEqual(result.path[0].cancel_policy, "settle_on_support")

    def test_astar_uses_downward_controller_for_same_column_floor_opening(self):
        cells = {
            (0, 64, 0): GridCell(),
            (0, 63, 0): GridCell(block_type="stone", walkable=False),
        }
        policy = GovernancePolicy(natural_regions=[Region("shaft", (0, 0, 0), (0, 100, 0))])
        planner = AStarPlanner(GridWorld(cells), NavigationCostModel(policy))

        result = planner.plan((0, 64, 0), (0, 63, 0), break_context=BreakContext.TRAVEL, allow_partial=False)

        self.assertTrue(result.success)
        self.assertEqual([step.pos for step in result.path], [(0, 63, 0)])
        self.assertEqual(result.path[0].move, MoveKind.DOWNWARD)
        self.assertEqual(result.path[0].reason, "downward")
        self.assertFalse(result.path[0].safe_to_cancel)
        self.assertEqual(result.path[0].cancel_policy, "finish_or_abort_controller")

    def test_astar_refuses_downward_controller_when_floor_break_is_protected(self):
        cells = {
            (0, 64, 0): GridCell(),
            (0, 63, 0): GridCell(block_type="stone", walkable=False),
        }
        policy = GovernancePolicy(
            natural_regions=[Region("shaft", (0, 0, 0), (0, 100, 0))],
            protected_regions=[Region("protected_floor", (0, 63, 0), (0, 63, 0))],
        )
        planner = AStarPlanner(GridWorld(cells), NavigationCostModel(policy))

        result = planner.plan((0, 64, 0), (0, 63, 0), break_context=BreakContext.TRAVEL, allow_partial=False)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "no_path")
        self.assertEqual(result.path, ())
        self.assertTrue(
            any(item["reason"] == "break_denied:protected_region" for item in result.diagnostics["blocked"])
        )

    def test_astar_uses_swim_step_for_liquid_cell(self):
        cells = {
            (0, 64, 0): GridCell(),
            (1, 64, 0): GridCell(block_type="water", liquid=True),
            (2, 64, 0): GridCell(),
        }
        planner = AStarPlanner(GridWorld(cells), NavigationCostModel(GovernancePolicy()))

        result = planner.plan((0, 64, 0), (2, 64, 0))

        self.assertTrue(result.success)
        self.assertEqual(result.path[0].move, MoveKind.SWIM)
        self.assertEqual(result.path[0].reason, "swim")
        self.assertFalse(result.path[0].safe_to_cancel)
        self.assertEqual(result.path[0].cancel_policy, "surface_or_stable_water")

    def test_astar_uses_diagonal_step_when_both_corners_are_clear(self):
        cells = {
            (0, 64, 0): GridCell(),
            (1, 64, 0): GridCell(),
            (0, 64, 1): GridCell(),
            (1, 64, 1): GridCell(),
        }
        planner = AStarPlanner(GridWorld(cells), NavigationCostModel(GovernancePolicy()))

        result = planner.plan((0, 64, 0), (1, 64, 1), allow_partial=False)

        self.assertTrue(result.success)
        self.assertEqual([step.pos for step in result.path], [(1, 64, 1)])
        self.assertEqual(result.path[0].move, MoveKind.DIAGONAL)
        self.assertEqual(result.path[0].reason, "diagonal")
        self.assertTrue(result.path[0].safe_to_cancel)
        self.assertEqual(result.path[0].cost, NavigationCostModel.DIAGONAL_COST)

    def test_astar_refuses_diagonal_corner_cut_through_protected_block(self):
        cells = {
            (0, 64, 0): GridCell(),
            (1, 64, 0): GridCell(block_type="stone", walkable=False),
            (1, 64, 1): GridCell(),
        }
        policy = GovernancePolicy(
            natural_regions=[Region("mine", (0, 0, 0), (1, 100, 1))],
            protected_regions=[Region("protected_corner", (1, 64, 0), (1, 64, 0))],
        )
        planner = AStarPlanner(GridWorld(cells), NavigationCostModel(policy))

        result = planner.plan((0, 64, 0), (1, 64, 1), allow_partial=False)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "no_path")
        self.assertEqual(result.path, ())
        self.assertTrue(
            any(
                item["reason"] == "diagonal_corner_blocked:break_denied:protected_region"
                for item in result.diagnostics.get("blocked", [])
            )
        )

    def test_astar_blocks_unsafe_fall_step(self):
        cells = {
            (0, 64, 0): GridCell(),
            (0, 63, 0): GridCell(fall_depth=6),
        }
        planner = AStarPlanner(GridWorld(cells), NavigationCostModel(GovernancePolicy()))

        result = planner.plan((0, 64, 0), (0, 63, 0))

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "no_path")
        self.assertTrue(any(item["reason"] == "fall_denied:unsafe_depth" for item in result.diagnostics["blocked"]))

    def test_goal_composite_any_selects_nearest_satisfied_child(self):
        cells = grid(8, 1)
        policy = GovernancePolicy(natural_regions=[Region("mine", (0, 0, 0), (7, 100, 0))])
        planner = AStarPlanner(GridWorld(cells), NavigationCostModel(policy))

        result = planner.plan(
            (0, 64, 0),
            GoalComposite((GoalBlock((7, 64, 0)), GoalBlock((3, 64, 0))), mode="any"),
        )

        self.assertTrue(result.success)
        self.assertEqual(result.path[-1].pos, (3, 64, 0))
        self.assertEqual(result.diagnostics["goal"]["kind"], "composite")

    def test_goal_avoid_with_fallback_requires_destination_outside_danger_band(self):
        cells = grid(8, 1)
        policy = GovernancePolicy(natural_regions=[Region("mine", (0, 0, 0), (7, 100, 0))])
        planner = AStarPlanner(GridWorld(cells), NavigationCostModel(policy))

        result = planner.plan(
            (0, 64, 0),
            GoalAvoid((2, 64, 0), min_distance=3, fallback=GoalBlock((6, 64, 0))),
        )

        self.assertTrue(result.success)
        self.assertEqual(result.path[-1].pos, (6, 64, 0))
        self.assertEqual(result.diagnostics["goal"]["kind"], "avoid")
        self.assertEqual(result.diagnostics["goal"]["fallback"]["pos"], [6, 64, 0])

    def test_astar_favors_previous_segment_nodes_with_positive_reduced_cost(self):
        cells = grid(3, 3)
        cells[(1, 64, 1)] = GridCell(block_type="stone", walkable=False)
        policy = GovernancePolicy(natural_regions=[Region("mine", (0, 0, 0), (2, 100, 2))])
        planner = AStarPlanner(GridWorld(cells), NavigationCostModel(policy))

        result = planner.plan(
            (0, 64, 0),
            (2, 64, 2),
            previous_segment=((1, 64, 0), (2, 64, 0), (2, 64, 1)),
            backtrack_cost_factor=0.5,
        )

        self.assertTrue(result.success)
        self.assertEqual([step.pos for step in result.path], [(1, 64, 0), (2, 64, 0), (2, 64, 1), (2, 64, 2)])
        self.assertEqual([step.cost for step in result.path[:3]], [0.5, 0.5, 0.5])
        self.assertEqual(result.path[0].reason, "walk")

    def test_astar_rejects_invalid_backtrack_factor(self):
        cells = grid(2, 1)
        planner = AStarPlanner(GridWorld(cells), NavigationCostModel(GovernancePolicy()))

        with self.assertRaises(ValueError):
            planner.plan((0, 64, 0), (1, 64, 0), backtrack_cost_factor=0.0)

    def test_astar_stops_at_unloaded_boundary_budget_without_softening_to_no_path(self):
        cells = {(0, 64, 0): GridCell()}
        planner = AStarPlanner(GridWorld(cells), NavigationCostModel(GovernancePolicy()))

        result = planner.plan((0, 64, 0), (10, 64, 0), unloaded_boundary_limit=2)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "unloaded_boundary")
        self.assertEqual(result.diagnostics["unloaded_boundary_count"], 2)
        self.assertEqual(result.diagnostics["unloaded_boundary_limit"], 2)
        self.assertTrue(all(item["reason"] == "unloaded" for item in result.diagnostics["blocked"]))

    def test_astar_unloaded_boundary_can_yield_safe_partial_progress(self):
        cells = grid(4, 1)
        policy = GovernancePolicy(natural_regions=[Region("mine", (0, 0, 0), (3, 100, 0))])
        planner = AStarPlanner(GridWorld(cells), NavigationCostModel(policy))

        result = planner.plan(
            (0, 64, 0),
            (10, 64, 0),
            min_partial_progress=2,
            unloaded_boundary_limit=40,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "partial")
        self.assertEqual(result.diagnostics["stop_reason"], "unloaded_boundary")
        self.assertGreaterEqual(result.diagnostics["progress"], 2)
        self.assertEqual(result.path[-1].pos, (3, 64, 0))

    def test_astar_does_not_yield_a_partial_with_a_liquid_endpoint(self):
        cells = {
            (0, 64, 0): GridCell(),
            (1, 64, 0): GridCell(block_type="water", liquid=True),
            (2, 64, 0): GridCell(block_type="water", liquid=True),
            (3, 64, 0): GridCell(block_type="water", liquid=True),
        }
        planner = AStarPlanner(GridWorld(cells), NavigationCostModel(GovernancePolicy()))

        result = planner.plan(
            (0, 64, 0),
            (10, 64, 0),
            min_partial_progress=2,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "no_path")
        self.assertNotEqual(result.reason, "partial")
        self.assertEqual(result.path, ())

    def test_astar_rejects_invalid_unloaded_boundary_limit(self):
        cells = grid(2, 1)
        planner = AStarPlanner(GridWorld(cells), NavigationCostModel(GovernancePolicy()))

        with self.assertRaises(ValueError):
            planner.plan((0, 64, 0), (1, 64, 0), unloaded_boundary_limit=0)


if __name__ == "__main__":
    unittest.main()
