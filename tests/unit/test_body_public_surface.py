import unittest

import minebot.body as body
from minebot.body.combat import CombatTransactions, find_hostiles
from minebot.body.navigation import make_block_at_prism_world_update


class BodyPublicSurfaceTests(unittest.TestCase):
    def test_legacy_world_refresh_adapter_is_not_top_level_body_export(self):
        self.assertNotIn("make_block_at_prism_world_update", body.__all__)
        self.assertFalse(hasattr(body, "make_block_at_prism_world_update"))
        self.assertTrue(callable(make_block_at_prism_world_update))

    def test_combat_transactions_are_top_level_body_capabilities(self):
        self.assertIs(body.CombatTransactions, CombatTransactions)
        self.assertIs(body.find_hostiles, find_hostiles)
        self.assertIn("CombatTransactions", body.__all__)
        self.assertIn("find_hostiles", body.__all__)


if __name__ == "__main__":
    unittest.main()
