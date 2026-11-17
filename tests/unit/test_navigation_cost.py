import math
import unittest

from minebot.game.governance import BreakContext, GovernancePolicy, PlaceContext, Region
from minebot.game.navigation import MovementCandidate, MoveKind, NavigationCostModel


class NavigationCostTests(unittest.TestCase):
    def setUp(self):
        self.policy = GovernancePolicy(
            natural_regions=[Region("mine", (-10, 0, -10), (10, 100, 10))],
            protected_regions=[Region("base", (20, 0, 20), (30, 100, 30))],
        )
        self.costs = NavigationCostModel(self.policy)

    def test_walk_candidate_has_normal_cost(self):
        decision = self.costs.evaluate(MovementCandidate(kind=MoveKind.WALK, pos=(0, 64, 0)))

        self.assertTrue(decision.passable)
        self.assertEqual(decision.cost, NavigationCostModel.WALK_COST)
        self.assertEqual(decision.reason, "walk")

    def test_vertical_and_water_candidates_have_distinct_costs(self):
        ascend = self.costs.evaluate(MovementCandidate(kind=MoveKind.ASCEND, pos=(0, 65, 0)))
        pillar = self.costs.evaluate(MovementCandidate(kind=MoveKind.PILLAR, pos=(0, 65, 0)))
        descend = self.costs.evaluate(MovementCandidate(kind=MoveKind.DESCEND, pos=(0, 63, 0)))
        downward = self.costs.evaluate(MovementCandidate(kind=MoveKind.DOWNWARD, pos=(0, 63, 0)))
        swim = self.costs.evaluate(MovementCandidate(kind=MoveKind.SWIM, pos=(1, 64, 0), block_type="water"))

        self.assertTrue(ascend.passable)
        self.assertEqual(ascend.cost, NavigationCostModel.ASCEND_COST)
        self.assertEqual(ascend.reason, "ascend")
        self.assertTrue(pillar.passable)
        self.assertEqual(pillar.cost, NavigationCostModel.PILLAR_COST)
        self.assertEqual(pillar.reason, "pillar")
        self.assertTrue(descend.passable)
        self.assertEqual(descend.cost, NavigationCostModel.DESCEND_COST)
        self.assertEqual(descend.reason, "descend")
        self.assertTrue(downward.passable)
        self.assertEqual(downward.cost, NavigationCostModel.DOWNWARD_COST)
        self.assertEqual(downward.reason, "downward")
        self.assertTrue(swim.passable)
        self.assertEqual(swim.cost, NavigationCostModel.SWIM_COST)
        self.assertEqual(swim.reason, "swim")

    def test_movement_candidates_expose_cancel_profiles(self):
        walk = self.costs.evaluate(MovementCandidate(kind=MoveKind.WALK, pos=(0, 64, 0)))
        ascend = self.costs.evaluate(MovementCandidate(kind=MoveKind.ASCEND, pos=(0, 65, 0)))
        pillar = self.costs.evaluate(MovementCandidate(kind=MoveKind.PILLAR, pos=(0, 65, 0)))
        downward = self.costs.evaluate(MovementCandidate(kind=MoveKind.DOWNWARD, pos=(0, 63, 0)))
        swim = self.costs.evaluate(MovementCandidate(kind=MoveKind.SWIM, pos=(1, 64, 0), block_type="water"))
        fall = self.costs.evaluate(MovementCandidate(kind=MoveKind.FALL, pos=(0, 62, 0), fall_depth=2))

        self.assertTrue(walk.safe_to_cancel)
        self.assertEqual(walk.cancel_policy, "immediate")
        self.assertFalse(ascend.safe_to_cancel)
        self.assertEqual(ascend.cancel_policy, "settle_on_support")
        self.assertFalse(pillar.safe_to_cancel)
        self.assertEqual(pillar.cancel_policy, "finish_or_abort_controller")
        self.assertFalse(downward.safe_to_cancel)
        self.assertEqual(downward.cancel_policy, "finish_or_abort_controller")
        self.assertFalse(swim.safe_to_cancel)
        self.assertEqual(swim.cancel_policy, "surface_or_stable_water")
        self.assertFalse(fall.safe_to_cancel)
        self.assertEqual(fall.cancel_policy, "land_first")

    def test_fall_candidate_is_bounded_by_safe_fall_depth(self):
        safe = self.costs.evaluate(MovementCandidate(kind=MoveKind.FALL, pos=(0, 61, 0), fall_depth=3))
        unsafe = self.costs.evaluate(MovementCandidate(kind=MoveKind.FALL, pos=(0, 50, 0), fall_depth=6))

        self.assertTrue(safe.passable)
        self.assertEqual(safe.reason, "fall")
        self.assertEqual(safe.diagnostics["fall_depth"], 3)
        self.assertFalse(unsafe.passable)
        self.assertTrue(math.isinf(unsafe.cost))
        self.assertEqual(unsafe.reason, "fall_denied:unsafe_depth")

    def test_unknown_provenance_break_is_blocked_not_high_cost(self):
        decision = self.costs.evaluate(
            MovementCandidate(
                kind=MoveKind.BREAK,
                pos=(100, 64, 100),
                block_type="stone",
                context=BreakContext.TRAVEL,
            )
        )

        self.assertFalse(decision.passable)
        self.assertTrue(math.isinf(decision.cost))
        self.assertEqual(decision.reason, "break_denied:unknown_provenance")

    def test_protected_region_break_is_blocked(self):
        decision = self.costs.evaluate(
            MovementCandidate(
                kind=MoveKind.BREAK,
                pos=(25, 64, 25),
                block_type="stone",
                context=BreakContext.COLLECT,
            )
        )

        self.assertFalse(decision.passable)
        self.assertTrue(math.isinf(decision.cost))
        self.assertEqual(decision.reason, "break_denied:protected_region")

    def test_path_context_cannot_break_collectable_ore(self):
        decision = self.costs.evaluate(
            MovementCandidate(
                kind=MoveKind.BREAK,
                pos=(0, 64, 0),
                block_type="diamond_ore",
                context=BreakContext.PATH,
            )
        )

        self.assertFalse(decision.passable)
        self.assertEqual(decision.reason, "break_denied:path_no_terrain_break")

    def test_collect_context_can_break_collectable_ore(self):
        decision = self.costs.evaluate(
            MovementCandidate(
                kind=MoveKind.BREAK,
                pos=(0, 64, 0),
                block_type="diamond_ore",
                context=BreakContext.COLLECT,
            )
        )

        self.assertTrue(decision.passable)
        self.assertEqual(decision.cost, NavigationCostModel.NATURAL_BREAK_COST)
        self.assertEqual(decision.reason, "break_allowed:allowed_natural")
        self.assertEqual(decision.diagnostics["context"], "collect")

    def test_bot_cleanup_break_gets_lower_cost_when_ledger_matches(self):
        self.policy.record_bot_placement((1, 64, 1), "cobblestone", "bridge", "Bot1")

        decision = self.costs.evaluate(
            MovementCandidate(
                kind=MoveKind.BREAK,
                pos=(1, 64, 1),
                block_type="minecraft:cobblestone",
                context=BreakContext.BOT_CLEANUP,
            )
        )

        self.assertTrue(decision.passable)
        self.assertEqual(decision.cost, NavigationCostModel.BOT_CLEANUP_BREAK_COST)
        self.assertTrue(decision.diagnostics["legality"]["bot_owned"])

    def test_place_in_unknown_region_is_blocked(self):
        decision = self.costs.evaluate(
            MovementCandidate(
                kind=MoveKind.PLACE,
                pos=(100, 64, 100),
                block_type="cobblestone",
                context=PlaceContext.TRAVEL,
            )
        )

        self.assertFalse(decision.passable)
        self.assertTrue(math.isinf(decision.cost))
        self.assertEqual(decision.reason, "place_denied:unknown_provenance")

    def test_place_in_natural_region_has_place_cost(self):
        decision = self.costs.evaluate(
            MovementCandidate(
                kind=MoveKind.PLACE,
                pos=(0, 64, 0),
                block_type="cobblestone",
                context=PlaceContext.TRAVEL,
                purpose="bridge",
            )
        )

        self.assertTrue(decision.passable)
        self.assertEqual(decision.cost, NavigationCostModel.PLACE_COST)
        self.assertEqual(decision.reason, "place_allowed:allowed_place")
        self.assertEqual(decision.diagnostics["purpose"], "bridge")


if __name__ == "__main__":
    unittest.main()
