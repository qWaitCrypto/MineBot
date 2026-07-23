import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "tests" / "fixtures" / "fakeplayer_generalization_corpus.json"


class FakePlayerGeneralizationCorpusTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads(CORPUS.read_text(encoding="utf-8"))

    def test_freezes_two_independent_terminal_evaluators(self):
        evaluators = self.data["evaluators"]
        self.assertEqual(set(evaluators), {"AG-FP30", "AG-FP30-X"})
        self.assertEqual(evaluators["AG-FP30"]["active_window_seconds"], 1800)
        self.assertEqual(evaluators["AG-FP30-X"]["active_window_seconds"], 1800)
        self.assertEqual(
            evaluators["AG-FP30"]["equipment_requirements"],
            [
                {"item": "shield", "slot": "offhand"},
                {"item": "iron_pickaxe", "slot": "mainhand"},
            ],
        )
        self.assertEqual(
            evaluators["AG-FP30-X"]["equipment_requirements"],
            [{"item": "stone_pickaxe", "slot": "mainhand"}],
        )
        self.assertEqual(evaluators["AG-FP30"]["inventory_requirements"]["torch"]["minimum"], 16)
        self.assertEqual(evaluators["AG-FP30"]["inventory_requirements"]["iron_ingot"]["minimum"], 3)
        self.assertEqual(evaluators["AG-FP30-X"]["excluded_domains"], ["flowers", "passive_animals"])

    def test_primary_flower_and_drop_families_are_explicit(self):
        primary = self.data["evaluators"]["AG-FP30"]
        flowers = primary["inventory_requirements"]["flower_distinct_types"]
        self.assertGreaterEqual(len(flowers["accepted_items"]), flowers["minimum"])
        self.assertEqual(
            primary["drop_requirements"],
            {
                "pig": {"accepted_items": ["porkchop"]},
                "cow": {"accepted_items": ["beef", "leather"]},
                "sheep": {"accepted_items": ["mutton", "white_wool", "wool"]},
            },
        )

    def test_scenario_schema_covers_required_mechanisms_and_compound_case(self):
        required = set(self.data["required_mechanisms"])
        scenarios = self.data["scenarios"]
        self.assertGreaterEqual(len(scenarios), 8)
        self.assertEqual(len({scenario["id"] for scenario in scenarios}), len(scenarios))
        self.assertTrue(
            any(
                scenario.get("compound")
                and {"candidate_stand_domain", "water_vertical_reachability", "mutation_governance"}
                <= set(scenario["mechanisms"])
                for scenario in scenarios
            )
        )
        covered = set()
        for scenario in scenarios:
            covered.update(scenario["mechanisms"])
            for field in ("world_fixture", "start", "target", "terminal", "mechanisms", "replay", "evidence"):
                self.assertIn(field, scenario, scenario["id"])
            self.assertTrue(scenario["evidence"], scenario["id"])
            self.assertIn(scenario["replay"]["status"], {"available", "pending", "debt"})
            if scenario["replay"]["status"] != "available":
                self.assertTrue(scenario["replay"]["debt"], scenario["id"])
        self.assertTrue(required <= covered)

    def test_corpus_uses_the_fixed_world_reset_and_keeps_coordinate_data_historical(self):
        self.assertEqual(self.data["world_fixture"]["id"], "world-golden")
        self.assertEqual(self.data["world_fixture"]["reset_command"], "tools/reset-world.sh")
        compound = next(s for s in self.data["scenarios"] if s["id"] == "r45-water-adjacent-tree-approach")
        self.assertEqual(compound["replay"]["status"], "available")
        self.assertEqual(compound["replay"]["repeat_runs"], 2)
        observed = compound["replay"]["observed"]
        self.assertEqual(observed["governance_target_after"], "stone")
        self.assertEqual(observed["owner"], None)
        self.assertIn("no_path", observed["dry_terminal_reasons"])
        self.assertIn("budget_exceeded", observed["dry_terminal_reasons"])
        self.assertTrue(observed["dry_zero_progress"])
        self.assertEqual(observed["navigation_fallback_attempts"], 1)
        self.assertEqual(observed["fallback_profile"], "governed_mobility")
        self.assertGreaterEqual(observed["fallback_movement_minimums"]["swim"], 1)
        self.assertGreaterEqual(observed["fallback_movement_minimums"]["ascend"], 1)
        egress = next(s for s in self.data["scenarios"] if s["id"] == "r37-water-pocket-vertical-egress")
        self.assertTrue(egress["compound"])
        self.assertEqual(egress["replay"]["status"], "available")
        self.assertEqual(egress["replay"]["repeat_runs"], 2)
        self.assertEqual(egress["replay"]["observed"]["reason"], "surface_reached")
        self.assertEqual(egress["replay"]["observed"]["movement_counts"]["swim"], 16)
        self.assertEqual(egress["replay"]["observed"]["movement_counts"]["ascend"], 1)
        self.assertEqual(egress["replay"]["observed"]["mutation_events"], [])
        for scenario in self.data["scenarios"]:
            self.assertEqual(scenario["world_fixture"], "world-golden")
            self.assertNotIn("seed", scenario["start"])
            self.assertNotIn("seed", scenario["target"])


if __name__ == "__main__":
    unittest.main()
