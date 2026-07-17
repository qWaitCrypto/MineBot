import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INTERACTION = (ROOT / "minebot" / "body" / "interaction.py").read_text()
INTERACTION_SUPPORT = (ROOT / "minebot" / "body" / "interaction_support.py").read_text()
USE = (ROOT / "minebot" / "body" / "use.py").read_text()
CONTAINER = (ROOT / "minebot" / "body" / "container.py").read_text()
FURNACE = (ROOT / "minebot" / "body" / "furnace.py").read_text()
BLOCK_WORK = (ROOT / "minebot" / "body" / "block_work.py").read_text()
RESOURCE_COLLECTION = (ROOT / "minebot" / "body" / "resource_collection.py").read_text()
PICKUP = (ROOT / "minebot" / "body" / "pickup.py").read_text()
NAVIGATION = (ROOT / "minebot" / "body" / "navigation.py").read_text()
COMPOSITION = (ROOT / "minebot" / "brain" / "composition.py").read_text()


def function_body(source: str, name: str) -> str:
    marker = f"    def {name}("
    next_marker = "\n    def "
    start = source.find(marker)
    if start == -1:
        marker = f"def {name}("
        next_marker = "\ndef "
        start = source.find(marker)
    if start == -1:
        raise AssertionError(f"function {name} not found")
    next_def = source.find(next_marker, start + len(marker))
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

    def test_interaction_stand_candidates_are_submitted_as_goal_sets(self):
        for source, name in (
            (INTERACTION_SUPPORT, "ensure_interaction_range"),
            (INTERACTION_SUPPORT, "ensure_entity_range"),
            (INTERACTION, "_approach_openable_target"),
            (INTERACTION, "_approach_bed_target"),
            (USE, "_recover_line_of_sight"),
        ):
            body = function_body(source, name)
            self.assertIn("_navigation_goal_for_stands(", body)
            self.assertNotRegex(body, r"for stand in .*:\n\s+nav_result = .*navigate_to\(stand")
        self.assertNotIn("class DirectInteractionNavigator", INTERACTION_SUPPORT)
        self.assertIn(
            '"config": pure_movement_navigation_config(navigation_config)',
            function_body(INTERACTION_SUPPORT, "ensure_entity_range"),
        )

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
        surface = function_body(BLOCK_WORK, "go_to_surface")
        self.assertIn("_find_surface_domain(", surface)
        self.assertIn("_find_surface_egress_domain(", surface)
        self.assertIn("_find_lateral_surface_domain(", surface)
        self.assertIn("GoalComposite(", surface)
        self.assertEqual(surface.count("self.navigator.navigate_to("), 2)
        self.assertIn("pure_movement_navigation_config(", surface)
        self.assertNotIn("self.dig_up_to_y(", surface)
        self.assertNotIn("_approach_surface_candidate(", BLOCK_WORK)
        self.assertNotIn("_approach_surface_column(", BLOCK_WORK)
        self.assertIn("read_surface_columns(", function_body(BLOCK_WORK, "_find_lateral_surface_domain"))

    def test_mining_stands_are_one_governed_goal_set(self):
        approach = function_body(BLOCK_WORK, "_approach_mining_target")
        self.assertIn("GoalComposite(", approach)
        self.assertEqual(approach.count("self.navigator.navigate_to("), 1)
        self.assertIn("break_context=BreakContext.COLLECT_APPROACH", approach)
        self.assertNotIn("self.body.execute(", approach)
        self.assertNotIn("_clear_collect_approach_stand", BLOCK_WORK)
        self.assertNotIn('Action.create("moveTo"', BLOCK_WORK)

    def test_resource_candidate_and_stand_choice_is_body_owned(self):
        collect = function_body(COMPOSITION, "collect_resource")
        self.assertIn('context.registry.get("collect_block_domain")', collect)
        self.assertNotIn('context.registry.get("search_for_block")', collect)
        self.assertNotIn('context.registry.get("mine_block_collect")', collect)
        self.assertNotIn("tried_positions", collect)
        self.assertNotIn("blocked_log_patches", collect)
        for retired_brain_selector in (
            "_execute_candidate_probe_tool",
            "_first_untried_target",
            "_sort_log_targets",
            "_diversify_targets",
            "_mark_log_patch_blocked",
        ):
            self.assertNotIn(retired_brain_selector, COMPOSITION)

        body_process = function_body(RESOURCE_COLLECTION, "collect_block_domain")
        self.assertIn("find_nearby_block_search(", body_process)
        self.assertIn("_build_stand_domain(", body_process)
        self.assertIn("GoalComposite(", body_process)
        self.assertIn("self.navigator.navigate_to(", body_process)
        self.assertIn("candidate_blacklist", body_process)
        self.assertIn("prepositioned=True", body_process)

    def test_pickup_candidates_are_one_non_mutating_goal_domain(self):
        collect_delta = function_body(BLOCK_WORK, "_collect_inventory_delta")
        self.assertIn("self.pickup._collect_inventory_delta(", collect_delta)
        self.assertNotIn("self.navigator.navigate_to(", collect_delta)

        pickup = function_body(PICKUP, "_collect_inventory_delta")
        self.assertIn("GoalComposite(", pickup)
        self.assertEqual(pickup.count("self.navigator.navigate_to("), 1)
        self.assertIn("allow_break=False", pickup)
        self.assertIn("allow_place=False", pickup)
        self.assertIn("allow_pillar=False", pickup)
        self.assertIn("allow_downward=False", pickup)
        self.assertNotRegex(pickup, r"for .*target.*:\n\s+.*navigate_to\(")

    def test_move_away_submits_the_complete_bounded_escape_domain(self):
        move_away = function_body(NAVIGATION, "move_away")
        self.assertIn("GoalComposite(", move_away)
        self.assertIn("_selected_candidate(", move_away)
        self.assertIn("pure_movement_navigation_config(config)", move_away)
        self.assertNotIn("GoalAvoid(", move_away)

    def test_live_navigation_gates_do_not_construct_the_conformance_planner(self):
        for path in sorted((ROOT / "tests").glob("e2e*.py")):
            source = path.read_text()
            for retired_constructor in ("SegmentedNavigator(", "GridWorld(", "NavigationCostModel("):
                self.assertNotIn(retired_constructor, source, f"{path.name} constructs {retired_constructor}")


if __name__ == "__main__":
    unittest.main()
