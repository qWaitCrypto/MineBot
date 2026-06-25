import unittest

from minebot.game.governance import BreakContext, GovernancePolicy, InteractionContext, PlaceContext, Region


class GovernanceTests(unittest.TestCase):
    def setUp(self):
        self.policy = GovernancePolicy(
            natural_regions=[Region("test_mine", (-10, 0, -10), (10, 100, 10))],
            protected_regions=[Region("base", (20, 0, 20), (30, 100, 30))],
        )

    def test_unknown_provenance_blocks_even_natural_looking_stone(self):
        decision = self.policy.can_break((100, 64, 100), "minecraft:stone", BreakContext.TRAVEL)

        self.assertFalse(decision.allowed)
        self.assertTrue(decision.protected)
        self.assertEqual(decision.reason, "unknown_provenance")

    def test_protected_region_wins_over_natural_classification(self):
        decision = self.policy.can_break((25, 64, 25), "stone", BreakContext.COLLECT)

        self.assertFalse(decision.allowed)
        self.assertTrue(decision.protected)
        self.assertEqual(decision.reason, "protected_region")
        self.assertEqual(decision.details["region"], "base")

    def test_strongly_protected_types_are_not_breakable_inside_natural_region(self):
        decision = self.policy.can_break((0, 64, 0), "chest", BreakContext.DIRECT)

        self.assertFalse(decision.allowed)
        self.assertTrue(decision.protected)
        self.assertEqual(decision.reason, "protected_type")

    def test_path_context_does_not_strip_mine_collectable_ore(self):
        decision = self.policy.can_break((0, 64, 0), "diamond_ore", BreakContext.PATH)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "path_no_terrain_break")

    def test_collect_context_allows_collect_target_inside_natural_region(self):
        decision = self.policy.can_break((0, 64, 0), "diamond_ore", BreakContext.COLLECT)

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "allowed_natural")
        self.assertEqual(decision.natural_region, "test_mine")

    def test_collect_context_rejects_non_target_trash(self):
        decision = self.policy.can_break((0, 64, 0), "stone", BreakContext.COLLECT)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "collect_target_required")

    def test_recovery_context_is_bounded_and_does_not_break_resource_targets(self):
        decision = self.policy.can_break((0, 64, 0), "iron_ore", BreakContext.RECOVERY)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "recovery_no_resource_break")

    def test_farm_context_allows_supported_crop_inside_natural_region(self):
        decision = self.policy.can_break((0, 64, 0), "wheat", BreakContext.FARM)

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "allowed_natural_farm")
        self.assertEqual(decision.natural_region, "test_mine")

    def test_farm_context_rejects_supported_crop_with_unknown_provenance(self):
        decision = self.policy.can_break((100, 64, 100), "wheat", BreakContext.FARM)

        self.assertFalse(decision.allowed)
        self.assertTrue(decision.protected)
        self.assertEqual(decision.reason, "unknown_provenance")

    def test_bot_cleanup_allows_matching_bot_owned_temporary_block(self):
        self.policy.record_bot_placement((1, 64, 1), "cobblestone", "scaffold", "Bot1")

        decision = self.policy.can_break((1, 64, 1), "minecraft:cobblestone", BreakContext.BOT_CLEANUP)

        self.assertTrue(decision.allowed)
        self.assertTrue(decision.bot_owned)
        self.assertEqual(decision.reason, "allowed_bot_owned")

    def test_bot_ledger_type_mismatch_blocks_cleanup(self):
        self.policy.record_bot_placement((1, 64, 1), "cobblestone", "scaffold", "Bot1")

        decision = self.policy.can_break((1, 64, 1), "stone", BreakContext.BOT_CLEANUP)

        self.assertFalse(decision.allowed)
        self.assertTrue(decision.protected)
        self.assertEqual(decision.reason, "bot_ledger_type_mismatch")

    def test_place_requires_known_region_unless_direct(self):
        decision = self.policy.can_place((100, 64, 100), "cobblestone", PlaceContext.TRAVEL, "Bot1")

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "unknown_provenance")

    def test_place_allowed_in_natural_region(self):
        decision = self.policy.can_place((0, 64, 0), "cobblestone", PlaceContext.TRAVEL, "Bot1")

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "allowed_place")
        self.assertEqual(decision.natural_region, "test_mine")

    def test_place_blocked_in_protected_region(self):
        decision = self.policy.can_place((25, 64, 25), "cobblestone", PlaceContext.DIRECT, "Bot1")

        self.assertFalse(decision.allowed)
        self.assertTrue(decision.protected)
        self.assertEqual(decision.reason, "protected_region")

    def test_interaction_allowed_inside_natural_region(self):
        decision = self.policy.can_interact((0, 64, 0), "oak_door", InteractionContext.ACTIVATE)

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "allowed_interaction")
        self.assertEqual(decision.natural_region, "test_mine")
        self.assertEqual(decision.details["context"], "activate")

    def test_interaction_blocked_in_protected_region(self):
        decision = self.policy.can_interact((25, 64, 25), "oak_door", InteractionContext.ACTIVATE)

        self.assertFalse(decision.allowed)
        self.assertTrue(decision.protected)
        self.assertEqual(decision.reason, "protected_region")
        self.assertEqual(decision.details["context"], "activate")

    def test_interaction_blocks_unknown_provenance(self):
        decision = self.policy.can_interact((100, 64, 100), "farmland", InteractionContext.FARM)

        self.assertFalse(decision.allowed)
        self.assertTrue(decision.protected)
        self.assertEqual(decision.reason, "unknown_provenance")
        self.assertEqual(decision.details["context"], "farm")


if __name__ == "__main__":
    unittest.main()
