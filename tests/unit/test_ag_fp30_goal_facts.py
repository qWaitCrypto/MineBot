import unittest

from minebot.app.real_server_session import AG_FP30_GOAL, ag_fp30_goal_facts
from minebot.contract import BodyState, PerceptionResult


class FactsBody:
    bot_name = "Bot"

    def perceive(self, scope, params):
        if scope != "inventory":
            return PerceptionResult("Bot", scope, "perception", False, False, {}, error="unsupported")
        slots = [
            {"slot": 0, "item": "minecraft:crafting_table", "count": 1, "empty": False, "slotLabel": "hotbar.0"},
            {"slot": 40, "item": "minecraft:shield", "count": 1, "empty": False, "slotLabel": "offhand"},
            {"slot": 1, "item": "minecraft:iron_pickaxe", "count": 1, "empty": False, "slotLabel": "hotbar.1"},
            {"slot": 2, "item": "minecraft:torch", "count": 16, "empty": False, "slotLabel": "hotbar.2"},
            {"slot": 3, "item": "minecraft:iron_ingot", "count": 3, "empty": False, "slotLabel": "hotbar.3"},
        ]
        start = int(params.get("start") or 0)
        limit = int(params.get("limit") or 12)
        page = slots[start : start + limit]
        next_start = start + limit if start + limit < len(slots) else None
        return PerceptionResult("Bot", "inventory", "perception", True, next_start is None, {"slots": page, "nextStart": next_start})

    def get_state(self):
        return BodyState(
            bot="Bot", pos=(0.0, 64.0, 0.0), yaw=None, pitch=None,
            health=20.0, food=20, oxygen=300, inventory_raw="", inventory_hash="x",
            effects=None, time=0, weather=None, dimension="overworld", complete=True,
            selected_slot=0,
        )

    def event_head(self, _epoch):
        return {"owner": None, "pending_action_count": 0}


class GoalFactsTests(unittest.TestCase):
    def test_facts_expose_unmet_terminal_clauses_without_actions(self):
        facts = ag_fp30_goal_facts(FactsBody(), AG_FP30_GOAL)
        self.assertIsNotNone(facts)
        self.assertFalse(facts["complete"])
        self.assertIn("flowers:0/3", facts["unmet"])
        self.assertIn("pig_drop", facts["unmet"])
        self.assertIn("mainhand:crafting_table!=iron_pickaxe", facts["unmet"])
        self.assertEqual(facts["inventory"], {"iron_ingot_count": 3, "torch_count": 16})
        self.assertNotIn("action", facts)

    def test_non_ag_goal_has_no_extra_projection(self):
        self.assertIsNone(ag_fp30_goal_facts(FactsBody(), "collect 3 logs"))


if __name__ == "__main__":
    unittest.main()
