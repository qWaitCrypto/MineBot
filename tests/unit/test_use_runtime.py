import unittest

from minebot.body import UseTransactions
from minebot.contract import Action, BodyState, Event, PerceptionResult, Result, ToolResult


def state_at(*, food: int, pos=(0, 64, 0), effects=None):
    return BodyState(
        bot="Bot1",
        pos=(float(pos[0]), float(pos[1]), float(pos[2])),
        yaw=0.0,
        pitch=0.0,
        health=20.0,
        food=food,
        oxygen=None,
        inventory_raw="[]",
        inventory_hash="inv",
        effects=effects,
        time=0,
        weather=None,
        dimension="overworld",
        complete=True,
    )


def perception(slots, *, complete=True, ok=True):
    return PerceptionResult(
        bot="Bot1",
        scope="inventory",
        type="perception",
        ok=ok,
        complete=complete,
        data={
            "start": 0,
            "limit": len(slots),
            "nextStart": None,
            "totalSlots": len(slots),
            "slots": slots,
        },
        uncertainty=[] if complete else [{"reason": "truncated"}],
        next=None,
        error=None if ok else "failed",
    )


def slot(index, item=None, count=0):
    return {"slot": index, "empty": item is None or count <= 0, "item": item, "count": count}


def block_perception(pos, block_type, state, *, complete=True, ok=True):
    return PerceptionResult(
        bot="Bot1",
        scope="blockAt",
        type="perception",
        ok=ok,
        complete=complete,
        data={"x": pos[0], "y": pos[1], "z": pos[2], "type": block_type, "state": state, "properties": {}},
        uncertainty=[] if complete else [{"reason": "truncated"}],
        next=None,
        error=None if ok else "failed",
    )


def block_perception_with_properties(pos, block_type, state, properties, *, complete=True, ok=True):
    return PerceptionResult(
        bot="Bot1",
        scope="blockAt",
        type="perception",
        ok=ok,
        complete=complete,
        data={
            "x": pos[0],
            "y": pos[1],
            "z": pos[2],
            "type": block_type,
            "state": state,
            "properties": dict(properties),
        },
        uncertainty=[] if complete else [{"reason": "truncated"}],
        next=None,
        error=None if ok else "failed",
    )


def stop_done(seq=1, tick=10):
    return Event(
        seq=seq,
        tick=tick,
        bot="Bot1",
        name="stopDone",
        data={"action_id": f"a{seq}", "success": True, "stopped_reason": "completed"},
    )


def look_done(seq=2, tick=20):
    return Event(
        seq=seq,
        tick=tick,
        bot="Bot1",
        name="lookDone",
        data={"action_id": f"a{seq}", "success": True, "stopped_reason": "completed"},
    )


def use_done(seq=3, tick=30, *, success=True, reason="completed", item=None):
    data = {"action_id": f"a{seq}", "success": success, "stopped_reason": reason}
    if item is not None:
        data["item"] = item
    return Event(seq=seq, tick=tick, bot="Bot1", name="useDone", data=data)


def nearby_entities_perception(entities, *, complete=True, ok=True):
    return PerceptionResult(
        bot="Bot1",
        scope="nearbyEntities",
        type="perception",
        ok=ok,
        complete=complete,
        data={
            "center": [0.0, 64.0, 0.0],
            "radius": 24,
            "limit": 64,
            "count": len(entities),
            "entities": entities,
        },
        uncertainty=[] if complete else [{"reason": "truncated"}],
        next=None,
        error=None if ok else "failed",
    )


class FakeEquipInventory:
    def __init__(self, result: ToolResult):
        self.result = result
        self.calls = []

    def equip_item(self, *, item, target="auto", timeout_s=2.0):
        self.calls.append({"item": item, "target": target, "timeout_s": timeout_s})
        return self.result


class FakeUseNavigator:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def navigate_to(self, goal, **kwargs):
        self.calls.append((goal, kwargs))
        if len(self.results) == 1:
            return self.results[0]
        return self.results.pop(0)


class FakeUseBody:
    bot_name = "Bot1"

    def __init__(self, *, states, perceptions, events, accepted=True, block_perceptions=None, entity_perceptions=None):
        self.states = list(states)
        self.perceptions = list(perceptions)
        self.events = list(events)
        self.accepted = accepted
        self.block_perceptions = list(block_perceptions or [])
        self.entity_perceptions = list(entity_perceptions or [])
        self.actions: list[Action] = []
        self.perception_calls: list[tuple[str, dict[str, object]]] = []

    def get_state(self):
        if len(self.states) == 1:
            return self.states[0]
        return self.states.pop(0)

    def perceive(self, scope: str, params: dict[str, object]) -> PerceptionResult:
        self.perception_calls.append((scope, params))
        if scope == "blockAt":
            if not self.block_perceptions:
                raise AssertionError("unexpected blockAt without prepared perception")
            return self.block_perceptions.pop(0)
        if scope == "nearbyEntities":
            if not self.entity_perceptions:
                raise AssertionError("unexpected nearbyEntities without prepared perception")
            return self.entity_perceptions.pop(0)
        if scope != "inventory":
            raise AssertionError(f"unexpected scope {scope}")
        return self.perceptions.pop(0)

    def execute(self, action: Action) -> Result:
        self.actions.append(action)
        return Result(
            id=action.id,
            bot="Bot1",
            type="result",
            ok=self.accepted,
            accepted=self.accepted,
            complete=True,
            data={"action": action.name},
            error=None if self.accepted else "rejected",
        )

    def await_action_terminal(self, action_id: str, timeout_s: float = 15.0) -> Event:
        if not self.events:
            raise AssertionError("no event left")
        return self.events.pop(0)

    def ignite_block(self, pos, *, item=None, allow_server_substitute: bool = False, timeout_s: float = 8.0):
        if not self.events:
            raise AssertionError("no event left")
        return self.events.pop(0)

    def sow_crop(
        self,
        pos,
        *,
        crop_block: str,
        seed_item: str | None = None,
        allow_server_substitute: bool = False,
        timeout_s: float = 8.0,
    ):
        if not self.events:
            raise AssertionError("no event left")
        return self.events.pop(0)

    def poll_events(self):
        return []


class FakePagedUseBody(FakeUseBody):
    def __init__(self, *, pages, states, events=()):
        super().__init__(states=states, perceptions=[], events=list(events))
        self.pages = list(pages)

    def perceive(self, scope: str, params: dict[str, object]) -> PerceptionResult:
        self.perception_calls.append((scope, params))
        if scope != "inventory":
            return super().perceive(scope, params)
        if not self.pages:
            raise AssertionError("unexpected inventory page read")
        page = self.pages.pop(0)
        return page


class UseRuntimeTests(unittest.TestCase):
    def test_consume_item_reports_completed_on_item_and_food_delta(self):
        body = FakeUseBody(
            states=[state_at(food=5), state_at(food=9)],
            perceptions=[
                perception([slot(0, "minecraft:bread", 2)]),
                perception([slot(0, "minecraft:bread", 1)]),
            ],
            events=[
                Event(seq=1, tick=10, bot="Bot1", name="selectItemDone", data={"action_id": "a1", "success": True, "stopped_reason": "completed", "slot": 0, "count": 2}),
                Event(seq=2, tick=20, bot="Bot1", name="useDone", data={"action_id": "a2", "success": True, "stopped_reason": "completed", "item": "minecraft:bread"}),
            ],
        )
        inventory = FakeEquipInventory(ToolResult(success=True, reason="completed", can_retry=False, metrics={"slot": 0}))
        runtime = UseTransactions(body, inventory=inventory)

        result = runtime.consume_item(item="minecraft:bread")

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "completed")
        self.assertEqual(result.metrics["item_delta"], 1)
        self.assertEqual(result.metrics["food_delta"], 4)
        self.assertEqual([action.name for action in body.actions], ["selectItem", "useItem"])

    def test_consume_item_reports_already_full_for_food_without_delta(self):
        body = FakeUseBody(
            states=[state_at(food=20), state_at(food=20)],
            perceptions=[
                perception([slot(0, "minecraft:bread", 2)]),
                perception([slot(0, "minecraft:bread", 2)]),
            ],
            events=[
                Event(seq=1, tick=10, bot="Bot1", name="selectItemDone", data={"action_id": "a1", "success": True, "stopped_reason": "completed", "slot": 0, "count": 2}),
                Event(seq=2, tick=20, bot="Bot1", name="useDone", data={"action_id": "a2", "success": False, "stopped_reason": "no_effect", "item": "minecraft:bread"}),
            ],
        )
        inventory = FakeEquipInventory(ToolResult(success=True, reason="completed", can_retry=False, metrics={"slot": 0}))
        runtime = UseTransactions(body, inventory=inventory)

        result = runtime.consume_item(item="minecraft:bread")

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "already_full")
        self.assertEqual(result.metrics["item_delta"], 0)
        self.assertEqual(result.metrics["food_delta"], 0)

    def test_consume_item_reports_no_effect_when_not_full_and_no_delta(self):
        body = FakeUseBody(
            states=[state_at(food=10), state_at(food=10)],
            perceptions=[
                perception([slot(0, "minecraft:bread", 2)]),
                perception([slot(0, "minecraft:bread", 2)]),
            ],
            events=[
                Event(seq=1, tick=10, bot="Bot1", name="selectItemDone", data={"action_id": "a1", "success": True, "stopped_reason": "completed", "slot": 0, "count": 2}),
                Event(seq=2, tick=20, bot="Bot1", name="useDone", data={"action_id": "a2", "success": False, "stopped_reason": "no_effect", "item": "minecraft:bread"}),
            ],
        )
        inventory = FakeEquipInventory(ToolResult(success=True, reason="completed", can_retry=False, metrics={"slot": 0}))
        runtime = UseTransactions(body, inventory=inventory)

        result = runtime.consume_item(item="minecraft:bread")

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "consume_no_effect")

    def test_consume_item_reads_inventory_in_small_pages(self):
        body = FakePagedUseBody(
            states=[state_at(food=5), state_at(food=9)],
            pages=[
                PerceptionResult(
                    bot="Bot1",
                    scope="inventory",
                    type="perception",
                    ok=True,
                    complete=True,
                    data={
                        "start": 0,
                        "limit": 12,
                        "nextStart": 12,
                        "totalSlots": 46,
                        "slots": [slot(0, "minecraft:bread", 2)],
                    },
                    uncertainty=[],
                    next=None,
                ),
                PerceptionResult(
                    bot="Bot1",
                    scope="inventory",
                    type="perception",
                    ok=True,
                    complete=True,
                    data={
                        "start": 12,
                        "limit": 12,
                        "nextStart": None,
                        "totalSlots": 46,
                        "slots": [],
                    },
                    uncertainty=[],
                    next=None,
                ),
                PerceptionResult(
                    bot="Bot1",
                    scope="inventory",
                    type="perception",
                    ok=True,
                    complete=True,
                    data={
                        "start": 0,
                        "limit": 12,
                        "nextStart": 12,
                        "totalSlots": 46,
                        "slots": [slot(0, "minecraft:bread", 1)],
                    },
                    uncertainty=[],
                    next=None,
                ),
                PerceptionResult(
                    bot="Bot1",
                    scope="inventory",
                    type="perception",
                    ok=True,
                    complete=True,
                    data={
                        "start": 12,
                        "limit": 12,
                        "nextStart": None,
                        "totalSlots": 46,
                        "slots": [],
                    },
                    uncertainty=[],
                    next=None,
                ),
            ],
            events=[
                Event(seq=1, tick=10, bot="Bot1", name="selectItemDone", data={"action_id": "a1", "success": True, "stopped_reason": "completed", "slot": 0, "count": 2}),
                Event(seq=2, tick=20, bot="Bot1", name="useDone", data={"action_id": "a2", "success": True, "stopped_reason": "completed", "item": "minecraft:bread"}),
            ],
        )
        inventory = FakeEquipInventory(ToolResult(success=True, reason="completed", can_retry=False, metrics={"slot": 0}))
        runtime = UseTransactions(body, inventory=inventory)

        result = runtime.consume_item(item="minecraft:bread")

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "completed")
        inventory_calls = [params for scope, params in body.perception_calls if scope == "inventory"]
        self.assertEqual(inventory_calls, [
            {"start": 0, "limit": 12},
            {"start": 12, "limit": 12},
            {"start": 0, "limit": 12},
            {"start": 12, "limit": 12},
        ])

    def test_consume_item_reports_effect_add_for_non_food_consumable(self):
        body = FakeUseBody(
            states=[
                state_at(food=20, effects=[]),
                state_at(food=20, effects=[{"id": "speed", "amplifier": 0, "duration": 600}]),
            ],
            perceptions=[
                perception([slot(0, "minecraft:potion", 1)]),
                perception([slot(0, "minecraft:glass_bottle", 1)]),
            ],
            events=[
                Event(seq=1, tick=10, bot="Bot1", name="selectItemDone", data={"action_id": "a1", "success": True, "stopped_reason": "completed", "slot": 0, "count": 1}),
                Event(seq=2, tick=20, bot="Bot1", name="useDone", data={"action_id": "a2", "success": True, "stopped_reason": "completed", "item": "minecraft:potion"}),
            ],
        )
        inventory = FakeEquipInventory(ToolResult(success=True, reason="completed", can_retry=False, metrics={"slot": 0}))
        runtime = UseTransactions(body, inventory=inventory)

        result = runtime.consume_item(item="minecraft:potion")

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "completed")
        self.assertEqual(result.metrics["item_delta"], 1)
        self.assertEqual(result.metrics["food_delta"], 0)
        self.assertEqual(result.metrics["effect_delta"], 1)
        self.assertEqual(result.metrics["effects_added"], [{"id": "speed", "amplifier": 0, "duration": 600}])

    def test_consume_item_reports_effect_removal_for_milk_bucket(self):
        body = FakeUseBody(
            states=[
                state_at(food=20, effects=[{"id": "speed", "amplifier": 0, "duration": 600}]),
                state_at(food=20, effects=[]),
            ],
            perceptions=[
                perception([slot(0, "minecraft:milk_bucket", 1)]),
                perception([slot(0, "minecraft:bucket", 1)]),
            ],
            events=[
                Event(seq=1, tick=10, bot="Bot1", name="selectItemDone", data={"action_id": "a1", "success": True, "stopped_reason": "completed", "slot": 0, "count": 1}),
                Event(seq=2, tick=20, bot="Bot1", name="useDone", data={"action_id": "a2", "success": True, "stopped_reason": "completed", "item": "minecraft:milk_bucket"}),
            ],
        )
        inventory = FakeEquipInventory(ToolResult(success=True, reason="completed", can_retry=False, metrics={"slot": 0}))
        runtime = UseTransactions(body, inventory=inventory)

        result = runtime.consume_item(item="minecraft:milk_bucket")

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "completed")
        self.assertEqual(result.metrics["item_delta"], 1)
        self.assertEqual(result.metrics["food_delta"], 0)
        self.assertEqual(result.metrics["effect_delta"], 1)
        self.assertEqual(result.metrics["effects_removed"], [{"id": "speed", "amplifier": 0, "duration": 600}])

    def test_use_item_reports_completed_on_position_delta(self):
        body = FakeUseBody(
            states=[
                state_at(food=20, pos=(0, 64, 0), effects=[]),
                state_at(food=20, pos=(9, 64, 0), effects=[]),
            ],
            perceptions=[
                perception([slot(0, "minecraft:ender_pearl", 2)]),
                perception([slot(0, "minecraft:ender_pearl", 1)]),
            ],
            events=[
                Event(seq=1, tick=10, bot="Bot1", name="selectItemDone", data={"action_id": "a1", "success": True, "stopped_reason": "completed", "slot": 0, "count": 2}),
                Event(seq=2, tick=20, bot="Bot1", name="lookDone", data={"action_id": "a2", "success": True, "stopped_reason": "completed"}),
                Event(seq=3, tick=40, bot="Bot1", name="useDone", data={"action_id": "a3", "success": True, "stopped_reason": "completed", "item": "minecraft:ender_pearl"}),
            ],
        )
        inventory = FakeEquipInventory(ToolResult(success=True, reason="completed", can_retry=False, metrics={"slot": 0}))
        runtime = UseTransactions(body, inventory=inventory)

        result = runtime.use_item(
            item="minecraft:ender_pearl",
            look_target=(20.0, 70.0, 0.0),
            min_position_delta=5.0,
        )

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "completed")
        self.assertGreaterEqual(result.metrics["position_delta"], 5.0)
        self.assertEqual(result.metrics["watched_item_deltas"], {})

    def test_use_on_entity_reports_not_found(self):
        body = FakeUseBody(
            states=[state_at(food=20)],
            perceptions=[],
            entity_perceptions=[nearby_entities_perception([])],
            events=[],
        )
        inventory = FakeEquipInventory(ToolResult(success=True, reason="completed", can_retry=False, metrics={"slot": 0}))
        runtime = UseTransactions(body, inventory=inventory)

        result = runtime.use_on_entity(item="minecraft:bucket", entity_types=("cow",))

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "use_entity_not_found")

    def test_use_on_entity_reports_completed_on_watched_item_delta(self):
        body = FakeUseBody(
            states=[
                state_at(food=20, pos=(0, 64, 0), effects=[]),
                state_at(food=20, pos=(0, 64, 0), effects=[]),
                state_at(food=20, pos=(0, 64, 0), effects=[]),
                state_at(food=20, pos=(0, 64, 0), effects=[]),
            ],
            perceptions=[
                perception([slot(0, "minecraft:bucket", 1)]),
                perception([slot(0, "minecraft:milk_bucket", 1)]),
            ],
            entity_perceptions=[
                nearby_entities_perception([{"id": "cow-1", "name": None, "type": "minecraft:cow", "pos": [2.0, 64.0, 0.0], "health": 10.0}]),
                nearby_entities_perception([{"id": "cow-1", "name": None, "type": "minecraft:cow", "pos": [2.0, 64.0, 0.0], "health": 10.0}]),
            ],
            events=[
                Event(seq=1, tick=10, bot="Bot1", name="selectItemDone", data={"action_id": "a1", "success": True, "stopped_reason": "completed", "slot": 0, "count": 1}),
                Event(seq=2, tick=20, bot="Bot1", name="lookDone", data={"action_id": "a2", "success": True, "stopped_reason": "completed"}),
                Event(seq=3, tick=30, bot="Bot1", name="useDone", data={"action_id": "a3", "success": True, "stopped_reason": "completed", "item": "minecraft:bucket"}),
            ],
        )
        inventory = FakeEquipInventory(ToolResult(success=True, reason="completed", can_retry=False, metrics={"slot": 0}))
        runtime = UseTransactions(body, inventory=inventory)

        result = runtime.use_on_entity(
            item="minecraft:bucket",
            entity_types=("cow",),
            watched_items=("minecraft:milk_bucket",),
            required_watched_item_deltas={"minecraft:milk_bucket": 1},
        )

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "completed")
        self.assertEqual(result.metrics["watched_item_deltas"]["milk_bucket"], 1)
        self.assertEqual(result.metrics["target"]["type"], "cow")

    def test_use_on_block_requires_watched_item_delta_when_requested(self):
        body = FakeUseBody(
            states=[
                state_at(food=20, pos=(0, 64, 0), effects=[]),
                state_at(food=20, pos=(0, 64, 0), effects=[]),
            ],
            perceptions=[
                perception([slot(0, "minecraft:water_bucket", 1)]),
                perception([slot(0, "minecraft:water_bucket", 1)]),
            ],
            block_perceptions=[
                block_perception((1, 64, 0), "minecraft:air", "CLEAR"),
                block_perception((1, 64, 0), "minecraft:water", "LIQUID"),
            ],
            events=[
                stop_done(),
                look_done(),
                use_done(item="minecraft:water_bucket"),
            ],
        )
        inventory = FakeEquipInventory(ToolResult(success=True, reason="completed", can_retry=False, metrics={"slot": 0}))
        runtime = UseTransactions(body, inventory=inventory)

        result = runtime.use_on_block(
            pos=(1, 64, 0),
            item="minecraft:water_bucket",
            expected_block_types=("water",),
            watched_items=("bucket",),
            required_watched_item_deltas={"bucket": 1},
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "targeted_use_unverified")
        self.assertEqual(result.metrics["watched_item_deltas"]["bucket"], 0)

    def test_consume_item_maps_missing_inventory_to_item_not_available(self):
        body = FakeUseBody(
            states=[state_at(food=10)],
            perceptions=[perception([slot(0, "minecraft:dirt", 1)])],
            events=[
                Event(seq=1, tick=10, bot="Bot1", name="selectItemDone", data={"action_id": "a1", "success": False, "stopped_reason": "not_in_inventory", "slot": -1, "count": 0}),
            ],
        )
        runtime = UseTransactions(body)

        result = runtime.consume_item(item="minecraft:bread")

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "item_not_available")
        self.assertEqual([action.name for action in body.actions], ["selectItem"])

    def test_consume_item_reports_hotbar_full_from_select_stage(self):
        body = FakeUseBody(
            states=[state_at(food=10)],
            perceptions=[perception([slot(0, "minecraft:dirt", 1)])],
            events=[
                Event(seq=1, tick=10, bot="Bot1", name="selectItemDone", data={"action_id": "a1", "success": False, "stopped_reason": "hotbar_full", "slot": -1, "count": 1}),
            ],
        )
        runtime = UseTransactions(body)

        result = runtime.consume_item(item="minecraft:bread")

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "hotbar_full")

    def test_use_on_block_reports_completed_when_target_matches_expected_after_use(self):
        body = FakeUseBody(
            states=[state_at(food=20, pos=(0, 64, 0)), state_at(food=20, pos=(0, 64, 0))],
            perceptions=[],
            block_perceptions=[
                block_perception((1, 64, 0), "minecraft:air", "CLEAR"),
                block_perception((1, 64, 0), "minecraft:fire", "SOLID"),
            ],
            events=[
                stop_done(),
                look_done(),
                use_done(item="minecraft:flint_and_steel"),
            ],
        )
        inventory = FakeEquipInventory(ToolResult(success=True, reason="completed", can_retry=False, metrics={"slot": 0}))
        runtime = UseTransactions(body, inventory=inventory)

        result = runtime.use_on_block(
            pos=(1, 64, 0),
            item="minecraft:flint_and_steel",
            expected_block_types=("fire",),
        )

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "completed")
        self.assertEqual([action.name for action in body.actions], ["stop", "lookAt", "useItem"])
        self.assertEqual(inventory.calls[0]["target"], "mainhand")
        self.assertEqual(result.metrics["target_after"]["type"], "fire")

    def test_use_on_block_reports_already_in_expected_state(self):
        body = FakeUseBody(
            states=[state_at(food=20, pos=(0, 64, 0))],
            perceptions=[],
            block_perceptions=[block_perception((1, 64, 0), "minecraft:fire", "SOLID")],
            events=[],
        )
        inventory = FakeEquipInventory(ToolResult(success=True, reason="completed", can_retry=False))
        runtime = UseTransactions(body, inventory=inventory)

        result = runtime.use_on_block(
            pos=(1, 64, 0),
            item="minecraft:flint_and_steel",
            expected_block_types=("fire",),
        )

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "already_in_expected_state")
        self.assertEqual(inventory.calls, [])
        self.assertEqual(body.actions, [])

    def test_use_on_block_reports_already_in_expected_property_state(self):
        body = FakeUseBody(
            states=[state_at(food=20, pos=(0, 64, 0))],
            perceptions=[],
            block_perceptions=[
                block_perception_with_properties(
                    (1, 64, 0),
                    "minecraft:oak_door",
                    "SOLID",
                    {"open": "true", "half": "lower"},
                )
            ],
            events=[],
        )
        inventory = FakeEquipInventory(ToolResult(success=True, reason="completed", can_retry=False))
        runtime = UseTransactions(body, inventory=inventory)

        result = runtime.use_on_block(
            pos=(1, 64, 0),
            item="minecraft:air",
            expected_block_types=("oak_door",),
            expected_properties={"open": "true"},
        )

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "already_in_expected_state")
        self.assertEqual(result.metrics["target_before"]["properties"]["open"], "true")
        self.assertEqual(inventory.calls, [])
        self.assertEqual(body.actions, [])

    def test_use_on_block_requires_navigation_when_target_out_of_range(self):
        body = FakeUseBody(
            states=[state_at(food=20, pos=(0, 64, 0))],
            perceptions=[],
            block_perceptions=[block_perception((10, 64, 0), "minecraft:air", "CLEAR")],
            events=[],
        )
        inventory = FakeEquipInventory(ToolResult(success=True, reason="completed", can_retry=False))
        runtime = UseTransactions(body, inventory=inventory)

        result = runtime.use_on_block(
            pos=(10, 64, 0),
            item="minecraft:flint_and_steel",
            expected_block_types=("fire",),
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "use_navigation_missing")
        self.assertEqual(body.actions, [])

    def test_use_on_block_reports_no_effect_when_target_unchanged(self):
        body = FakeUseBody(
            states=[state_at(food=20, pos=(0, 64, 0)), state_at(food=20, pos=(0, 64, 0))],
            perceptions=[],
            block_perceptions=[
                block_perception((1, 64, 0), "minecraft:air", "CLEAR"),
                block_perception((1, 64, 0), "minecraft:air", "CLEAR"),
            ],
            events=[
                stop_done(),
                look_done(),
                use_done(success=False, reason="no_effect", item="minecraft:flint_and_steel"),
            ],
        )
        inventory = FakeEquipInventory(ToolResult(success=True, reason="completed", can_retry=False))
        runtime = UseTransactions(body, inventory=inventory)

        result = runtime.use_on_block(
            pos=(1, 64, 0),
            item="minecraft:flint_and_steel",
            expected_block_types=("fire",),
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "targeted_use_no_effect")
        self.assertEqual([action.name for action in body.actions], ["stop", "lookAt", "useItem"])

    def test_use_on_block_reports_completed_when_expected_property_changes(self):
        body = FakeUseBody(
            states=[state_at(food=20, pos=(0, 64, 0)), state_at(food=20, pos=(0, 64, 0))],
            perceptions=[],
            block_perceptions=[
                block_perception_with_properties(
                    (1, 64, 0),
                    "minecraft:oak_door",
                    "SOLID",
                    {"open": "false", "half": "lower"},
                ),
                block_perception_with_properties(
                    (1, 64, 0),
                    "minecraft:oak_door",
                    "SOLID",
                    {"open": "true", "half": "lower"},
                ),
            ],
            events=[
                stop_done(),
                look_done(),
                use_done(item="minecraft:air"),
            ],
        )
        inventory = FakeEquipInventory(ToolResult(success=True, reason="completed", can_retry=False, metrics={"slot": 0}))
        runtime = UseTransactions(body, inventory=inventory)

        result = runtime.use_on_block(
            pos=(1, 64, 0),
            item="minecraft:air",
            expected_block_types=("oak_door",),
            expected_properties={"open": "true"},
        )

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "completed")
        self.assertEqual(result.metrics["target_before"]["properties"]["open"], "false")
        self.assertEqual(result.metrics["target_after"]["properties"]["open"], "true")
        self.assertEqual([action.name for action in body.actions], ["stop", "lookAt", "useItem"])

    def test_use_on_block_treats_empty_hand_as_first_class_contract(self):
        body = FakeUseBody(
            states=[state_at(food=20, pos=(0, 64, 0)), state_at(food=20, pos=(0, 64, 0))],
            perceptions=[],
            block_perceptions=[
                block_perception_with_properties(
                    (1, 64, 0),
                    "minecraft:oak_door",
                    "SOLID",
                    {"open": "false", "half": "lower"},
                ),
                block_perception_with_properties(
                    (1, 64, 0),
                    "minecraft:oak_door",
                    "SOLID",
                    {"open": "true", "half": "lower"},
                ),
            ],
            events=[
                stop_done(),
                look_done(),
                use_done(),
            ],
        )
        inventory = FakeEquipInventory(ToolResult(success=True, reason="completed", can_retry=False))
        runtime = UseTransactions(body, inventory=inventory)

        result = runtime.use_on_block(
            pos=(1, 64, 0),
            item=None,
            expected_block_types=("oak_door",),
            expected_properties={"open": "true"},
        )

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "completed")
        self.assertEqual(inventory.calls, [])
        self.assertTrue(result.metrics["empty_hand"])
        self.assertNotIn("item", body.actions[2].params)

    def test_use_on_block_can_observe_a_different_block_than_the_click_target(self):
        body = FakeUseBody(
            states=[state_at(food=20, pos=(0, 64, 0)), state_at(food=20, pos=(0, 64, 0))],
            perceptions=[],
            block_perceptions=[
                block_perception_with_properties(
                    (1, 64, 0),
                    "minecraft:farmland",
                    "SOLID",
                    {"moisture": "7"},
                ),
                block_perception((1, 65, 0), "minecraft:air", "CLEAR"),
                block_perception_with_properties(
                    (1, 64, 0),
                    "minecraft:farmland",
                    "SOLID",
                    {"moisture": "7"},
                ),
                block_perception_with_properties(
                    (1, 65, 0),
                    "minecraft:wheat",
                    "SOLID",
                    {"age": "0"},
                ),
            ],
            events=[
                stop_done(),
                look_done(),
                use_done(item="minecraft:wheat_seeds"),
            ],
        )
        inventory = FakeEquipInventory(ToolResult(success=True, reason="completed", can_retry=False, metrics={"slot": 0}))
        runtime = UseTransactions(body, inventory=inventory)

        result = runtime.use_on_block(
            pos=(1, 64, 0),
            observe_pos=(1, 65, 0),
            item="minecraft:wheat_seeds",
            expected_block_types=("wheat",),
        )

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "completed")
        self.assertEqual(result.metrics["target_before"]["type"], "farmland")
        self.assertEqual(result.metrics["observed_before"]["type"], "air")
        self.assertEqual(result.metrics["observed_after"]["type"], "wheat")

    def test_use_on_block_repositions_and_retries_after_no_effect(self):
        body = FakeUseBody(
            states=[
                state_at(food=20, pos=(0, 64, 0)),
                state_at(food=20, pos=(0, 64, 0)),
                state_at(food=20, pos=(0, 64, 0)),
                state_at(food=20, pos=(2, 64, 0)),
            ],
            perceptions=[],
            block_perceptions=[
                block_perception((1, 64, 0), "minecraft:air", "CLEAR"),
                block_perception((1, 64, 0), "minecraft:air", "CLEAR"),
                block_perception((2, 64, 0), "minecraft:air", "CLEAR"),
                block_perception((2, 65, 0), "minecraft:air", "CLEAR"),
                block_perception((2, 63, 0), "minecraft:stone", "SOLID"),
                block_perception((0, 64, 0), "minecraft:air", "CLEAR"),
                block_perception((0, 65, 0), "minecraft:air", "CLEAR"),
                block_perception((0, 63, 0), "minecraft:stone", "SOLID"),
                block_perception((1, 64, 1), "minecraft:stone", "SOLID"),
                block_perception((1, 65, 1), "minecraft:stone", "SOLID"),
                block_perception((1, 63, 1), "minecraft:stone", "SOLID"),
                block_perception((1, 64, -1), "minecraft:stone", "SOLID"),
                block_perception((1, 65, -1), "minecraft:stone", "SOLID"),
                block_perception((1, 63, -1), "minecraft:stone", "SOLID"),
                block_perception((1, 64, 0), "minecraft:fire", "SOLID"),
            ],
            events=[
                stop_done(seq=1, tick=10),
                look_done(seq=2, tick=20),
                use_done(seq=3, tick=30, success=False, reason="no_effect", item="minecraft:flint_and_steel"),
                stop_done(seq=4, tick=40),
                look_done(seq=5, tick=50),
                use_done(seq=6, tick=60, item="minecraft:flint_and_steel"),
            ],
        )
        inventory = FakeEquipInventory(ToolResult(success=True, reason="completed", can_retry=False))
        navigator = FakeUseNavigator([ToolResult(success=True, reason="arrived", can_retry=False, metrics={"goal": [2, 64, 0]})])
        runtime = UseTransactions(body, navigator=navigator, inventory=inventory)

        result = runtime.use_on_block(
            pos=(1, 64, 0),
            item="minecraft:flint_and_steel",
            expected_block_types=("fire",),
            line_of_sight_retries=1,
        )

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "completed")
        self.assertEqual([action.name for action in body.actions], ["stop", "lookAt", "useItem", "stop", "lookAt", "useItem"])
        self.assertTrue(result.metrics["line_of_sight_recovery"]["repositioned"])
        self.assertEqual(len(navigator.calls), 1)

    def test_use_on_block_repositions_when_completed_use_leaves_target_unchanged(self):
        body = FakeUseBody(
            states=[
                state_at(food=20, pos=(0, 64, 0)),
                state_at(food=20, pos=(0, 64, 0)),
            ],
            perceptions=[],
            block_perceptions=[
                block_perception((1, 64, 0), "minecraft:air", "CLEAR"),
                block_perception((1, 64, 0), "minecraft:air", "CLEAR"),
                block_perception((2, 64, 0), "minecraft:air", "CLEAR"),
                block_perception((2, 65, 0), "minecraft:air", "CLEAR"),
                block_perception((2, 63, 0), "minecraft:stone", "SOLID"),
                block_perception((0, 64, 0), "minecraft:air", "CLEAR"),
                block_perception((0, 65, 0), "minecraft:air", "CLEAR"),
                block_perception((0, 63, 0), "minecraft:stone", "SOLID"),
                block_perception((1, 64, 1), "minecraft:air", "CLEAR"),
                block_perception((1, 65, 1), "minecraft:air", "CLEAR"),
                block_perception((1, 63, 1), "minecraft:stone", "SOLID"),
                block_perception((1, 64, -1), "minecraft:air", "CLEAR"),
                block_perception((1, 65, -1), "minecraft:air", "CLEAR"),
                block_perception((1, 63, -1), "minecraft:stone", "SOLID"),
                block_perception((1, 64, 0), "minecraft:fire", "SOLID"),
            ],
            events=[
                stop_done(seq=1, tick=10),
                look_done(seq=2, tick=20),
                use_done(seq=3, tick=30, item="minecraft:flint_and_steel"),
                stop_done(seq=4, tick=40),
                look_done(seq=5, tick=50),
                use_done(seq=6, tick=60, item="minecraft:flint_and_steel"),
            ],
        )
        inventory = FakeEquipInventory(ToolResult(success=True, reason="completed", can_retry=False))
        navigator = FakeUseNavigator([ToolResult(success=True, reason="arrived", can_retry=False, metrics={"goal": [2, 64, 0]})])
        runtime = UseTransactions(body, navigator=navigator, inventory=inventory)

        result = runtime.use_on_block(
            pos=(1, 64, 0),
            item="minecraft:flint_and_steel",
            expected_block_types=("fire",),
            line_of_sight_retries=1,
        )

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "completed")
        self.assertEqual([action.name for action in body.actions], ["stop", "lookAt", "useItem", "stop", "lookAt", "useItem"])
        self.assertTrue(result.metrics["line_of_sight_recovery"]["repositioned"])
        self.assertEqual(len(navigator.calls), 1)

    def test_use_on_block_reports_no_effect_when_no_alternate_stand_point_exists(self):
        body = FakeUseBody(
            states=[
                state_at(food=20, pos=(0, 64, 0)),
                state_at(food=20, pos=(0, 64, 0)),
                state_at(food=20, pos=(0, 64, 0)),
            ],
            perceptions=[],
            block_perceptions=[
                block_perception((1, 64, 0), "minecraft:air", "CLEAR"),
                block_perception((1, 64, 0), "minecraft:air", "CLEAR"),
                block_perception((2, 64, 0), "minecraft:stone", "SOLID"),
                block_perception((2, 65, 0), "minecraft:stone", "SOLID"),
                block_perception((2, 63, 0), "minecraft:stone", "SOLID"),
                block_perception((0, 64, 0), "minecraft:air", "CLEAR"),
                block_perception((0, 65, 0), "minecraft:stone", "SOLID"),
                block_perception((0, 63, 0), "minecraft:stone", "SOLID"),
                block_perception((1, 64, 1), "minecraft:stone", "SOLID"),
                block_perception((1, 65, 1), "minecraft:stone", "SOLID"),
                block_perception((1, 63, 1), "minecraft:stone", "SOLID"),
                block_perception((1, 64, -1), "minecraft:stone", "SOLID"),
                block_perception((1, 65, -1), "minecraft:stone", "SOLID"),
                block_perception((1, 63, -1), "minecraft:stone", "SOLID"),
                block_perception((2, 63, 0), "minecraft:stone", "SOLID"),
                block_perception((2, 64, 0), "minecraft:stone", "SOLID"),
                block_perception((2, 62, 0), "minecraft:stone", "SOLID"),
                block_perception((0, 63, 0), "minecraft:stone", "SOLID"),
                block_perception((0, 64, 0), "minecraft:stone", "SOLID"),
                block_perception((0, 62, 0), "minecraft:stone", "SOLID"),
                block_perception((1, 63, 1), "minecraft:stone", "SOLID"),
                block_perception((1, 64, 1), "minecraft:stone", "SOLID"),
                block_perception((1, 62, 1), "minecraft:stone", "SOLID"),
                block_perception((1, 63, -1), "minecraft:stone", "SOLID"),
                block_perception((1, 64, -1), "minecraft:stone", "SOLID"),
                block_perception((1, 62, -1), "minecraft:stone", "SOLID"),
            ],
            events=[
                stop_done(),
                look_done(),
                use_done(success=False, reason="no_effect", item="minecraft:flint_and_steel"),
            ],
        )
        inventory = FakeEquipInventory(ToolResult(success=True, reason="completed", can_retry=False))
        navigator = FakeUseNavigator([])
        runtime = UseTransactions(body, navigator=navigator, inventory=inventory)

        result = runtime.use_on_block(
            pos=(1, 64, 0),
            item="minecraft:flint_and_steel",
            expected_block_types=("fire",),
            line_of_sight_retries=1,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "targeted_use_no_effect")
        self.assertFalse(result.metrics["line_of_sight_recovery"]["repositioned"])
        self.assertEqual(result.metrics["line_of_sight_recovery"]["reason"], "no_alternate_stand_point")
        self.assertEqual(navigator.calls, [])

    def test_sow_crop_on_farmland_reports_completed_when_crop_appears_and_seed_decrements(self):
        body = FakeUseBody(
            states=[state_at(food=20, pos=(0, 64, 0))],
            perceptions=[
                perception([slot(0, "minecraft:wheat_seeds", 3)]),
                perception([slot(0, "minecraft:wheat_seeds", 2)]),
            ],
            block_perceptions=[
                block_perception_with_properties((1, 64, 0), "minecraft:farmland", "SOLID", {"moisture": "7"}),
                block_perception((1, 65, 0), "minecraft:air", "CLEAR"),
                block_perception_with_properties((1, 64, 0), "minecraft:farmland", "SOLID", {"moisture": "7"}),
                block_perception_with_properties((1, 65, 0), "minecraft:wheat", "SOLID", {"age": "0"}),
            ],
            events=[
                Event(
                    seq=1,
                    tick=20,
                    bot="Bot1",
                    name="sowDone",
                    data={"action_id": "a1", "success": True, "stopped_reason": "completed", "item": "minecraft:wheat_seeds", "method": "physical"},
                ),
            ],
        )
        inventory = FakeEquipInventory(ToolResult(success=True, reason="completed", can_retry=False, metrics={"slot": 0}))
        runtime = UseTransactions(body, inventory=inventory)

        result = runtime._sow_crop_on_farmland(
            pos=(1, 64, 0),
            observe_pos=(1, 65, 0),
            seed_item="minecraft:wheat_seeds",
            crop_block="wheat",
        )

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "completed")
        self.assertEqual(result.metrics["observed_after"]["type"], "wheat")
        self.assertEqual(result.metrics["seed_delta"], 1)
        self.assertEqual(result.metrics["use"]["metrics"]["method"], "physical")

    def test_sow_crop_on_farmland_rejects_unconsumed_seed_even_if_crop_appears(self):
        body = FakeUseBody(
            states=[state_at(food=20, pos=(0, 64, 0))],
            perceptions=[
                perception([slot(0, "minecraft:wheat_seeds", 3)]),
                perception([slot(0, "minecraft:wheat_seeds", 3)]),
            ],
            block_perceptions=[
                block_perception_with_properties((1, 64, 0), "minecraft:farmland", "SOLID", {"moisture": "7"}),
                block_perception((1, 65, 0), "minecraft:air", "CLEAR"),
                block_perception_with_properties((1, 64, 0), "minecraft:farmland", "SOLID", {"moisture": "7"}),
                block_perception_with_properties((1, 65, 0), "minecraft:wheat", "SOLID", {"age": "0"}),
            ],
            events=[
                Event(
                    seq=1,
                    tick=20,
                    bot="Bot1",
                    name="sowDone",
                    data={"action_id": "a1", "success": True, "stopped_reason": "completed", "item": "minecraft:wheat_seeds", "method": "substitute"},
                ),
            ],
        )
        runtime = UseTransactions(body, inventory=FakeEquipInventory(ToolResult(success=True, reason="completed", can_retry=False)))

        result = runtime._sow_crop_on_farmland(
            pos=(1, 64, 0),
            observe_pos=(1, 65, 0),
            seed_item="minecraft:wheat_seeds",
            crop_block="wheat",
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "sow_seed_not_consumed")
        self.assertEqual(result.metrics["use"]["metrics"]["method"], "substitute")

if __name__ == "__main__":
    unittest.main()
