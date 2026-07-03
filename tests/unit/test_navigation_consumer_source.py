import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INTERACTION = (ROOT / "minebot" / "body" / "interaction.py").read_text()
CONTAINER = (ROOT / "minebot" / "body" / "container.py").read_text()
FURNACE = (ROOT / "minebot" / "body" / "furnace.py").read_text()
BLOCK_WORK = (ROOT / "minebot" / "body" / "block_work.py").read_text()


def function_body(source: str, name: str) -> str:
    marker = f"    def {name}("
    start = source.find(marker)
    if start == -1:
        raise AssertionError(f"function {name} not found")
    next_def = source.find("\n    def ", start + len(marker))
    if next_def == -1:
        next_def = len(source)
    block = source[start:next_def]
    header_end = block.find(":\n")
    if header_end == -1:
        raise AssertionError(f"function {name} header not found")
    return block[header_end + 2 :]


class NavigationConsumerSourceTests(unittest.TestCase):
    def test_interaction_transactions_route_through_shared_helpers(self):
        self.assertIn("_enter_player_distance_band(", function_body(INTERACTION, "go_to_player"))
        self.assertIn("_enter_player_distance_band(", function_body(INTERACTION, "follow_player"))
        self.assertIn("ensure_entity_range(", function_body(INTERACTION, "_enter_player_distance_band"))
        self.assertIn("_approach_bed_target(", function_body(INTERACTION, "go_to_bed"))
        self.assertIn("_approach_openable_target(", function_body(INTERACTION, "set_openable_state"))
        self.assertIn("self.use.use_on_block(", function_body(INTERACTION, "set_openable_state"))
        self.assertIn("self.use.use_on_block(", function_body(INTERACTION, "set_switch_state"))
        self.assertIn("self.use.use_on_block(", function_body(INTERACTION, "till_farmland"))
        self.assertIn("ensure_interaction_range(", function_body(INTERACTION, "sow_crop"))
        self.assertIn("self.sow_crop(", function_body(INTERACTION, "harvest_and_resow"))
        self.assertNotIn('Action.create("moveTo"', INTERACTION)

    def test_container_and_furnace_nearest_transactions_use_shared_interaction_range(self):
        self.assertIn("ensure_interaction_range(", function_body(CONTAINER, "transfer_nearest_container"))
        self.assertIn("ensure_interaction_range(", function_body(FURNACE, "clear_nearest_furnace"))
        self.assertIn("ensure_interaction_range(", function_body(FURNACE, "smelt_nearest_furnace"))
        self.assertNotIn('Action.create("moveTo"', CONTAINER)
        self.assertNotIn('Action.create("moveTo"', FURNACE)

    def test_block_work_consumers_use_shared_navigation_entrypoints(self):
        search_body = function_body(BLOCK_WORK, "search_for_block")
        self.assertIn("find_nearby_block_search(", search_body)
        self.assertNotIn("interaction_stand_points(", search_body)
        self.assertNotIn("self.navigator.navigate_to(", search_body)
        self.assertIn("_approach_place_candidate(", function_body(BLOCK_WORK, "place_here"))
        self.assertIn("_approach_surface_candidate(", function_body(BLOCK_WORK, "go_to_surface"))
        self.assertIn("_approach_surface_column(", function_body(BLOCK_WORK, "go_to_surface"))
        self.assertIn("self.dig_up_to_y(", function_body(BLOCK_WORK, "go_to_surface"))


if __name__ == "__main__":
    unittest.main()
