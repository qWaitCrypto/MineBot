import unittest

from minebot.contract import BodyState


class BodyStateMessageTests(unittest.TestCase):
    def test_from_envelope_data_parses_quality_trace_fields(self):
        state = BodyState.from_envelope_data(
            "Bot",
            True,
            {
                "pos": [1, 64, 2],
                "yaw": None,
                "pitch": None,
                "health": 20,
                "food": 20,
                "oxygen": 300,
                "inventory_raw": "",
                "inventory_hash": "hash",
                "inventory_counts": {"minecraft:oak_log": "3", "minecraft:air": 0},
                "effects": None,
                "time": 1000,
                "weather": None,
                "dimension": "overworld",
                "selected_slot": 0,
                "selected_item": "minecraft:stone_pickaxe",
                "offhand_item": "minecraft:shield",
                "body_owner": "moveTo",
                "pending_action_count": 1,
            },
        )

        self.assertEqual(state.inventory_counts, {"oak_log": 3})
        self.assertEqual(state.selected_item, "stone_pickaxe")
        self.assertEqual(state.offhand_item, "shield")
        self.assertEqual(state.body_owner, "moveTo")
        self.assertEqual(state.pending_action_count, 1)


if __name__ == "__main__":
    unittest.main()
