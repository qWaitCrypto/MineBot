import unittest

from minebot.app.memory import MemoryWorkspace
from minebot.app.recall import FanOutRecall, MemoryRecallSource
from minebot.app.runner import AgentRuntime
from minebot.app.runtime_state import RuntimeScope, RuntimeStateStore
from minebot.brain.recall import RecallQuery


class _Trace:
    def emit(self, *args, **kwargs):
        pass


class _Event:
    def __init__(self, name, data):
        self.name = name
        self.data = data


class DangerMemoryTests(unittest.TestCase):
    def setUp(self):
        self.store = RuntimeStateStore(":memory:")
        self.workspace = MemoryWorkspace(self.store, RuntimeScope("server", "world", "Bot1"))
        self.runtime = AgentRuntime.__new__(AgentRuntime)
        self.runtime.memory_workspace = self.workspace
        self.runtime.trace = _Trace()
        self.runtime._danger_memories_written = []

    def tearDown(self):
        self.store.close()

    def test_death_writes_killer_and_spatial_recall_finds_it(self):
        event = _Event("death", {
            "position": [12.0, 64.0, -3.0],
            "dimension": "overworld",
            "hurt_source": {"name": "Zombie", "type": "zombie", "damage_type": "minecraft:mob_attack"},
        })
        self.runtime.record_body_event_memory(event)
        self.runtime.record_body_event_memory(event)
        rows = self.workspace.search({"query": "", "center": [12, 64, -3], "radius": 4, "limit": 10})["results"]
        self.assertEqual(len(rows), 1)
        self.assertIn("died here; killed by Zombie", rows[0]["excerpt"])

        recalled = FanOutRecall([MemoryRecallSource(self.workspace)]).recall(
            RecallQuery(text="collect iron", center=(12, 64, -3), dimension="overworld")
        )
        self.assertTrue(any("died here; killed by Zombie" in item.projection["excerpt"] for item in recalled.items))

    def test_death_without_observed_killer_does_not_infer_nearest_hostile(self):
        self.runtime.record_body_event_memory(_Event("death", {
            "position": [12.0, 64.0, -3.0],
            "dimension": "overworld",
            "nearby_hostiles": {"nearest": {"type": "zombie"}},
        }))
        rows = self.workspace.search({"query": "", "center": [12, 64, -3], "radius": 4, "limit": 10})["results"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["excerpt"], "died here")

    def test_repeated_fire_damage_near_one_place_writes_one_memory(self):
        for position in ([0, 70, 0], [1, 70, 1], [2, 70, 0], [0, 70, 2]):
            self.runtime.record_body_facts_memory({
                "position": position,
                "dimension": "overworld",
                "health": 9,
                "health_before": 10,
                "on_fire": True,
            })
        rows = self.workspace.search({"query": "", "center": [0, 70, 0], "radius": 4, "limit": 10})["results"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["excerpt"], "fire damage here")

    def test_damage_type_names_the_danger_and_mob_hits_are_not_places(self):
        self.runtime.record_body_facts_memory({
            "position": [5, 80, 5], "dimension": "overworld", "health": 14, "health_before": 20,
            "hurt_source": {"damage_type": "minecraft:fall"},
        })
        self.runtime.record_body_facts_memory({
            "position": [30, 64, 30], "dimension": "overworld", "health": 16, "health_before": 20,
            "on_fire": True, "hurt_source": {"type": "zombie", "damage_type": "minecraft:mob_attack"},
        })
        rows = self.workspace.search({"query": "", "center": [5, 80, 5], "radius": 4, "limit": 10})["results"]
        self.assertEqual([row["excerpt"] for row in rows], ["fall damage here"])
        rows = self.workspace.search({"query": "", "center": [30, 64, 30], "radius": 4, "limit": 10})["results"]
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
