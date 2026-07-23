import unittest

from minebot.body.pickup import PickupConfig, PickupTransactions
from minebot.contract import PerceptionResult, ToolResult
from minebot.game.navigation import GoalComposite
from minebot.body.navigation import NavigationRunConfig


class PickupBody:
    bot_name = "Bot1"

    def __init__(self, entities):
        self.entities = list(entities)
        self.item_count = 0
        self.perceptions = []

    def perceive(self, scope, params):
        self.perceptions.append((scope, dict(params)))
        if scope == "inventory":
            slots = []
            if self.item_count:
                slots.append({"slot": 0, "item": "minecraft:dirt", "count": self.item_count, "empty": False})
            return PerceptionResult(
                bot=self.bot_name,
                scope=scope,
                type="perception",
                ok=True,
                complete=True,
                data={"slots": slots},
            )
        if scope == "nearbyEntities":
            return PerceptionResult(
                bot=self.bot_name,
                scope=scope,
                type="perception",
                ok=True,
                complete=True,
                data={"entities": list(self.entities)},
            )
        raise AssertionError(f"unexpected perception scope {scope}")


class PickupNavigator:
    def __init__(self, body, selections, *, collect_on_calls=(), outcomes=()):
        self.body = body
        self.selections = list(selections)
        self.collect_on_calls = set(collect_on_calls)
        self.outcomes = list(outcomes)
        self.calls = []

    def navigate_to(self, goal, **kwargs):
        self.calls.append((goal, kwargs))
        selected = self.selections.pop(0)
        if len(self.calls) in self.collect_on_calls:
            self.body.item_count += 1
        success, reason = self.outcomes.pop(0) if self.outcomes else (True, "arrived")
        return ToolResult(
            success,
            reason,
            not success,
            metrics={"selected_goal": list(selected), "goal_set_preserved": True},
        )


class MovingPickupBody(PickupBody):
    def __init__(self, entities):
        super().__init__(entities)
        self.nearby_calls = 0

    def perceive(self, scope, params):
        if scope == "nearbyEntities":
            self.nearby_calls += 1
            if self.nearby_calls >= 2:
                self.entities[0]["pos"] = [4, 64, 0]
        return super().perceive(scope, params)


def item(entity_id, pos, *, name="Dirt"):
    return {"id": entity_id, "type": "minecraft:item", "name": name, "pos": list(pos), "dist2": 1.0}


class PickupRuntimeTests(unittest.TestCase):
    def test_negative_fractional_entity_position_maps_to_nearest_cell(self):
        body = PickupBody([item("drift", (14.153, 72.0, -0.029))])
        runtime = PickupTransactions(body, None, settle=lambda _seconds: None)

        candidates, metrics = runtime._scan_candidates(PickupConfig())

        self.assertTrue(metrics["ok"])
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].pos, (14, 72, 0))

    def test_planner_receives_complete_drop_domain_and_uses_non_mutating_profile(self):
        body = PickupBody([item("near", (2.4, 64.1, 0.2)), item("far", (6.2, 64.1, 0.2))])
        navigator = PickupNavigator(body, [(6, 64, 0)], collect_on_calls={1})
        runtime = PickupTransactions(body, navigator, settle=lambda _seconds: None)

        result = runtime.pickup_items(
            expected_items=("dirt",),
            config=PickupConfig(poll_timeout_s=0, max_scan_rounds=1),
        )

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(result.reason, "pickup_collected")
        self.assertEqual(result.metrics["deltas"], {"dirt": 1})
        self.assertEqual(len(navigator.calls), 1)
        goal, kwargs = navigator.calls[0]
        self.assertIsInstance(goal, GoalComposite)
        self.assertEqual({child.pos for child in goal.goals}, {(2, 64, 0), (6, 64, 0)})
        config = kwargs["config"]
        self.assertFalse(config.allow_break)
        self.assertFalse(config.allow_place)
        self.assertFalse(config.allow_pillar)
        self.assertFalse(config.allow_downward)
        self.assertEqual(config.max_break_steps, 0)
        self.assertEqual(config.max_place_steps, 0)

    def test_expected_item_filter_skips_unrelated_nearby_drops(self):
        body = PickupBody(
            [
                item("junk", (2, 64, 0), name="Stone"),
                item("wanted", (6, 64, 0), name="Dirt"),
            ]
        )
        navigator = PickupNavigator(body, [(6, 64, 0)], collect_on_calls={1})
        runtime = PickupTransactions(body, navigator, settle=lambda _seconds: None)

        result = runtime.pickup_items(
            expected_items=("dirt",),
            config=PickupConfig(poll_timeout_s=0, max_scan_rounds=1),
        )

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(
            {child.pos for child in navigator.calls[0][0].goals},
            {(6, 64, 0)},
        )
        self.assertEqual(result.metrics["pickup_process"]["scans"][0]["filtered_count"], 1)

    def test_no_delta_blacklists_selected_entity_and_replans_remaining_domain(self):
        body = PickupBody([item("first", (2, 64, 0)), item("second", (5, 64, 0))])
        navigator = PickupNavigator(body, [(2, 64, 0), (5, 64, 0)], collect_on_calls={2})
        runtime = PickupTransactions(body, navigator, settle=lambda _seconds: None)

        result = runtime.pickup_items(
            expected_items=("dirt",),
            config=PickupConfig(poll_timeout_s=0, max_scan_rounds=2, candidate_budget=2),
        )

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(len(navigator.calls), 2)
        self.assertEqual({child.pos for child in navigator.calls[0][0].goals}, {(2, 64, 0), (5, 64, 0)})
        second_goals = {child.pos for child in navigator.calls[1][0].goals}
        self.assertIn((5, 64, 0), second_goals)
        self.assertIn((4, 64, -1), second_goals)
        process = result.metrics["pickup_process"]
        self.assertIn("entity:first", process["candidate_blacklist"])

    def test_moved_same_entity_is_retried_at_its_new_position(self):
        body = MovingPickupBody([item("drifting", (2, 64, 0))])
        navigator = PickupNavigator(body, [(2, 64, 0), (4, 64, 0)], collect_on_calls={2})
        runtime = PickupTransactions(body, navigator, settle=lambda _seconds: None)

        result = runtime.pickup_items(
            expected_items=("dirt",),
            config=PickupConfig(poll_timeout_s=0, max_scan_rounds=2, candidate_budget=2),
        )

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(len(navigator.calls), 2)
        self.assertEqual({child.pos for child in navigator.calls[0][0].goals}, {(2, 64, 0)})
        second_goals = {child.pos for child in navigator.calls[1][0].goals}
        self.assertIn((4, 64, 0), second_goals)
        self.assertIn((3, 64, -1), second_goals)

    def test_rejected_entity_cell_retries_a_local_stand_neighbor(self):
        body = PickupBody([item("blocked", (2, 64, 0))])
        navigator = PickupNavigator(
            body,
            [(2, 64, 0), (1, 64, 0)],
            outcomes=[(False, "budget_exceeded"), (True, "arrived")],
            collect_on_calls={2},
        )
        runtime = PickupTransactions(body, navigator, settle=lambda _seconds: None)

        result = runtime.pickup_items(
            expected_items=("dirt",),
            config=PickupConfig(poll_timeout_s=0, max_scan_rounds=2, candidate_budget=2),
        )

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(len(navigator.calls), 2)
        self.assertEqual({child.pos for child in navigator.calls[0][0].goals}, {(2, 64, 0)})
        self.assertIn((1, 64, 0), {child.pos for child in navigator.calls[1][0].goals})

    def test_rejected_entity_prioritizes_its_stand_domain_over_origin_fallbacks(self):
        body = PickupBody([item("blocked", (6, 70, 0))])
        navigator = PickupNavigator(
            body,
            [(6, 70, 0), (6, 71, 0)],
            outcomes=[(False, "budget_exceeded"), (True, "arrived")],
            collect_on_calls={2},
        )
        runtime = PickupTransactions(body, navigator, settle=lambda _seconds: None)

        result = runtime._collect_inventory_delta(
            before={},
            expected=("dirt",),
            minimum_count=1,
            fallback_positions=((1, 71, 0),),
            config=PickupConfig(poll_timeout_s=0, max_scan_rounds=2, candidate_budget=2),
        )

        self.assertEqual(result["collected_total"], 1)
        self.assertEqual(len(navigator.calls), 2)
        second_goals = {child.pos for child in navigator.calls[1][0].goals}
        self.assertIn((6, 71, 0), second_goals)
        self.assertNotIn((1, 71, 0), second_goals)

    def test_empty_domain_returns_typed_exhaustion_without_navigation(self):
        body = PickupBody([])
        navigator = PickupNavigator(body, [])
        runtime = PickupTransactions(body, navigator, settle=lambda _seconds: None)

        result = runtime.pickup_items(
            config=PickupConfig(poll_timeout_s=0, max_scan_rounds=1),
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "pickup_candidate_domain_exhausted")
        self.assertTrue(result.can_retry)
        self.assertEqual(navigator.calls, [])

    def test_resource_navigation_profile_is_preserved_for_pickup(self):
        body = PickupBody([item("drop", (2, 64, 0))])
        navigator = PickupNavigator(body, [(2, 64, 0)], collect_on_calls={1})
        runtime = PickupTransactions(body, navigator, settle=lambda _seconds: None)

        result = runtime.pickup_items(
            expected_items=("dirt",),
            config=PickupConfig(
                poll_timeout_s=0,
                max_scan_rounds=1,
                navigation_config=NavigationRunConfig(
                    server_grid_radius=48,
                    server_max_expand=1200,
                    max_segments=16,
                    max_partial_segments=8,
                ),
            ),
        )

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(len(navigator.calls), 1)
        navigation = navigator.calls[0][1]["config"]
        self.assertEqual(navigation.server_grid_radius, 48)
        self.assertEqual(navigation.server_max_expand, 1200)
        self.assertEqual(navigation.max_segments, 3)
        self.assertEqual(navigation.max_partial_segments, 3)
        self.assertFalse(navigation.allow_break)


if __name__ == "__main__":
    unittest.main()
