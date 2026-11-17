import unittest

from minebot.contract.harvest import (
    PICKAXE_BY_TIER,
    best_owned_pickaxe,
    required_pickaxe_tier,
    tier_satisfies,
)


class HarvestContractTests(unittest.TestCase):
    def test_required_pickaxe_tier_normalizes_block_names(self):
        self.assertEqual(required_pickaxe_tier("minecraft:diamond_ore"), "iron")
        self.assertEqual(required_pickaxe_tier("deepslate_iron_ore"), "stone")
        self.assertEqual(required_pickaxe_tier("oak_log"), None)

    def test_tier_satisfies_orders_pickaxe_tiers(self):
        self.assertTrue(tier_satisfies("iron", "stone"))
        self.assertTrue(tier_satisfies("netherite", "diamond"))
        self.assertTrue(tier_satisfies("golden", "wooden"))
        self.assertFalse(tier_satisfies("stone", "iron"))
        self.assertFalse(tier_satisfies(None, "wooden"))
        self.assertFalse(tier_satisfies("unknown", "wooden"))

    def test_best_owned_pickaxe_returns_highest_available_tier(self):
        counts = {
            "minecraft:wooden_pickaxe": 1,
            "stone_pickaxe": 0,
            "minecraft:iron_pickaxe": 2,
            "diamond": 64,
        }

        self.assertEqual(best_owned_pickaxe(counts), ("iron_pickaxe", "iron"))

    def test_best_owned_pickaxe_treats_golden_as_wooden_tier(self):
        self.assertEqual(best_owned_pickaxe({"minecraft:golden_pickaxe": 1}), ("golden_pickaxe", "wooden"))
        self.assertEqual(PICKAXE_BY_TIER["iron"], "iron_pickaxe")

    def test_best_owned_pickaxe_ignores_invalid_counts(self):
        self.assertIsNone(best_owned_pickaxe({"iron_pickaxe": 0, "diamond_pickaxe": "bad"}))


if __name__ == "__main__":
    unittest.main()
