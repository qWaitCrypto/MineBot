import unittest

from minebot.body.pickup import PickupConfig, PickupTransactions
from minebot.contract import PerceptionResult, ToolResult
from minebot.game.navigation import GoalComposite


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
    def __init__(self, body, selections, *, collect_on_calls=()):
        self.body = body
        self.selections = list(selections)
        self.collect_on_calls = set(collect_on_calls)
        self.calls = []

    def navigate_to(self, goal, **kwargs):
        self.calls.append((goal, kwargs))
        selected = self.selections.pop(0)
        if len(self.calls) in self.collect_on_calls:
            self.body.item_count += 1
        return ToolResult(
            True,
            "arrived",
            False,
            metrics={"selected_goal": list(selected), "goal_set_preserved": True},
        )


def item(entity_id, pos):
    return {"id": entity_id, "type": "minecraft:item", "name": "Dirt", "pos": list(pos), "dist2": 1.0}


class PickupRuntimeTests(unittest.TestCase):
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
        self.assertEqual({child.pos for child in navigator.calls[1][0].goals}, {(5, 64, 0)})
        process = result.metrics["pickup_process"]
        self.assertIn("entity:first", process["candidate_blacklist"])

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


if __name__ == "__main__":
    unittest.main()
