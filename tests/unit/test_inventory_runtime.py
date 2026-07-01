import unittest

from minebot.body import InventoryTransactions
from minebot.contract import Action, Event, PerceptionResult, Result


class FakeInventoryBody:
    bot_name = "Bot1"

    def __init__(
        self,
        pages,
        *,
        accepted: bool = True,
        drop_delta: int | None = None,
        select_success: bool = True,
        select_reason: str = "completed",
        recipe_data: dict[str, str] | None = None,
    ):
        self.pages = list(pages)
        self.accepted = accepted
        self.drop_delta = drop_delta
        self.select_success = select_success
        self.select_reason = select_reason
        self.recipe_data = recipe_data or {}
        self.actions: list[Action] = []
        self.perceptions: list[tuple[str, dict[str, object]]] = []

    def perceive(self, scope: str, params: dict[str, object]) -> PerceptionResult:
        self.perceptions.append((scope, params))
        if scope == "inventory":
            return self.pages.pop(0)
        if scope == "recipeData":
            item = params.get("item")
            raw = self.recipe_data.get(str(item))
            return PerceptionResult(
                bot="Bot1",
                scope="recipeData",
                type="perception",
                ok=raw is not None,
                complete=True,
                data={"item": item, "recipe_raw": raw} if raw is not None else {},
                uncertainty=[],
                next=None,
                error=None if raw is not None else "recipe_not_found",
            )
        raise AssertionError(f"unexpected scope {scope}")

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
        action = next(action for action in self.actions if action.id == action_id)
        if action.name == "moveItem":
            return Event(
                seq=len(self.actions),
                tick=20,
                bot="Bot1",
                name="moveItemDone",
                data={
                    "action_id": action_id,
                    "success": True,
                    "stopped_reason": "completed",
                    "count": action.params.get("count", 1),
                },
            )
        if action.name == "selectItem":
            return Event(
                seq=len(self.actions),
                tick=20,
                bot="Bot1",
                name="selectItemDone",
                data={
                    "action_id": action_id,
                    "success": self.select_success,
                    "item": action.params["item"],
                    "slot": 0 if self.select_success else -1,
                    "count": 1 if self.select_success else 0,
                    "stopped_reason": self.select_reason,
                },
            )
        if action.name == "dropItem":
            count = self.drop_delta if self.drop_delta is not None else 64
            after = 0 if action.params.get("mode") == "all" else max(0, count - 1)
            return Event(
                seq=len(self.actions),
                tick=20,
                bot="Bot1",
                name="dropDone",
                data={
                    "action_id": action_id,
                    "success": True,
                    "stopped_reason": "completed",
                    "slot": action.params["slot"],
                    "count_before": count,
                    "count_after": after,
                },
            )
        if action.name == "craftItem":
            remainders = {entry["slot"]: entry for entry in action.params.get("remainders") or []}
            input_after = []
            for entry in action.params["inputs"]:
                remainder = remainders.get(entry["slot"])
                if remainder is not None:
                    input_after.append(
                        {
                            "slot": entry["slot"],
                            "empty": False,
                            "item": remainder["item"],
                            "count": remainder["count"],
                        }
                    )
                else:
                    input_after.append({"slot": entry["slot"], "empty": True, "item": None, "count": 0})
            return Event(
                seq=len(self.actions),
                tick=20,
                bot="Bot1",
                name="craftDone",
                data={
                    "action_id": action_id,
                    "success": True,
                    "item": action.params["output"]["item"],
                    "count": action.params["output"]["count"],
                    "output_slot": action.params["output"]["slot"],
                    "stopped_reason": "completed",
                    "inputs_after": input_after,
                },
            )
        raise AssertionError(f"unexpected action {action.name}")


def perception(slots, *, complete=True, next_start=None, ok=True):
    return PerceptionResult(
        bot="Bot1",
        scope="inventory",
        type="perception",
        ok=ok,
        complete=complete,
        data={
            "start": 0,
            "limit": len(slots),
            "nextStart": next_start,
            "totalSlots": len(slots),
            "slots": slots,
        },
        uncertainty=[] if complete else [{"reason": "page_limit"}],
        next=None if complete else str(next_start),
        error=None if ok else "failed",
    )


def slot(index, item=None, count=0):
    return {"slot": index, "empty": item is None or count <= 0, "item": item, "count": count}


class InventoryRuntimeTests(unittest.TestCase):
    def test_inventory_reads_small_pages_and_merges_slots(self):
        body = FakeInventoryBody(
            [
                PerceptionResult(
                    bot="Bot1",
                    scope="inventory",
                    type="perception",
                    ok=True,
                    complete=False,
                    data={"start": 0, "limit": 12, "nextStart": 12, "totalSlots": 46, "slots": [slot(0)]},
                    uncertainty=[{"reason": "page_limit"}],
                    next="12",
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
                        "slots": [slot(12, "minecraft:diamond", 3)],
                    },
                    uncertainty=[],
                    next=None,
                ),
            ]
        )
        runtime = InventoryTransactions(body)

        result = runtime.discard_item(item="minecraft:diamond", count=1)

        self.assertTrue(result.success, result.to_payload())
        inventory_reads = [params for scope, params in body.perceptions if scope == "inventory"]
        self.assertEqual(inventory_reads, [{"start": 0, "limit": 12}, {"start": 12, "limit": 12}])
        self.assertEqual(body.actions[0].name, "moveItem")
        self.assertEqual(body.actions[0].params["from_slot"], 12)

    def test_inventory_reads_follow_envelope_next_when_data_cursor_is_absent(self):
        body = FakeInventoryBody(
            [
                PerceptionResult(
                    bot="Bot1",
                    scope="inventory",
                    type="perception",
                    ok=True,
                    complete=False,
                    data={"start": 0, "limit": 12, "totalSlots": 46, "slots": [slot(0)]},
                    uncertainty=[{"reason": "page_limit"}],
                    next="12",
                ),
                PerceptionResult(
                    bot="Bot1",
                    scope="inventory",
                    type="perception",
                    ok=True,
                    complete=True,
                    data={"start": 12, "limit": 12, "totalSlots": 46, "slots": [slot(12, "minecraft:diamond", 1)]},
                    uncertainty=[],
                    next=None,
                ),
            ],
            drop_delta=1,
        )
        runtime = InventoryTransactions(body)

        result = runtime.discard_item(item="diamond", count=1)

        self.assertTrue(result.success, result.to_payload())
        inventory_reads = [params for scope, params in body.perceptions if scope == "inventory"]
        self.assertEqual(inventory_reads, [{"start": 0, "limit": 12}, {"start": 12, "limit": 12}])

    def test_discards_matching_hotbar_stack_without_staging(self):
        body = FakeInventoryBody([perception([slot(0, "minecraft:dirt", 5), slot(1)])], drop_delta=5)
        runtime = InventoryTransactions(body)

        result = runtime.discard_item(item="minecraft:dirt", count=5)

        self.assertTrue(result.success)
        self.assertEqual(result.metrics["dropped_count"], 5)
        self.assertEqual([action.name for action in body.actions], ["dropItem"])
        self.assertEqual(body.actions[0].params, {"slot": 0, "mode": "all"})

    def test_partial_hotbar_discard_uses_single_item_drops(self):
        body = FakeInventoryBody([perception([slot(0, "minecraft:dirt", 5), slot(1)])], drop_delta=5)
        runtime = InventoryTransactions(body)

        result = runtime.discard_item(item="minecraft:dirt", count=2)

        self.assertTrue(result.success)
        self.assertEqual(result.metrics["dropped_count"], 2)
        self.assertEqual([action.name for action in body.actions], ["dropItem", "dropItem"])
        self.assertEqual([action.params["mode"] for action in body.actions], ["one", "one"])

    def test_stages_non_hotbar_item_to_empty_hotbar_then_drops(self):
        body = FakeInventoryBody([perception([slot(0), slot(9, "minecraft:diamond", 3)])], drop_delta=3)
        runtime = InventoryTransactions(body)

        result = runtime.discard_item(item="diamond", count=3)

        self.assertTrue(result.success)
        self.assertEqual([action.name for action in body.actions], ["moveItem", "dropItem"])
        self.assertEqual(body.actions[0].params["from_slot"], 9)
        self.assertEqual(body.actions[0].params["to_slot"], 0)
        self.assertEqual(body.actions[0].params["count"], 3)
        self.assertEqual(body.actions[1].params["slot"], 0)

    def test_reports_item_not_available_without_executing(self):
        body = FakeInventoryBody([perception([slot(0, "minecraft:stone", 5), slot(1)])])
        runtime = InventoryTransactions(body)

        result = runtime.discard_item(item="minecraft:diamond", count=1)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "item_not_available")
        self.assertEqual(body.actions, [])

    def test_reports_hotbar_full_for_non_hotbar_item_without_overwriting(self):
        slots = [slot(i, f"minecraft:item_{i}", 1) for i in range(9)]
        slots.append(slot(9, "minecraft:diamond", 2))
        body = FakeInventoryBody([perception(slots)])
        runtime = InventoryTransactions(body)

        result = runtime.discard_item(item="minecraft:diamond", count=1)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "hotbar_full")
        self.assertEqual(body.actions, [])
        self.assertEqual(result.metrics["available_count"], 2)

    def test_refuses_incomplete_inventory(self):
        body = FakeInventoryBody(
            [
                PerceptionResult(
                    bot="Bot1",
                    scope="inventory",
                    type="perception",
                    ok=True,
                    complete=False,
                    data={
                        "start": 0,
                        "limit": 1,
                        "nextStart": None,
                        "totalSlots": 1,
                        "slots": [slot(0, "minecraft:dirt", 1)],
                    },
                    uncertainty=[{"reason": "truncated"}],
                    next=None,
                )
            ]
        )
        runtime = InventoryTransactions(body)

        result = runtime.discard_item(item="minecraft:dirt", count=1)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "perception_failed")
        self.assertEqual(body.actions, [])

    def test_refuses_missing_body_inventory_perception(self):
        body = FakeInventoryBody(
            [
                PerceptionResult(
                    bot="Bot1",
                    scope="inventory",
                    type="perception",
                    ok=False,
                    complete=True,
                    data={},
                    uncertainty=[{"reason": "missing_body"}],
                    next=None,
                    error="missing_body",
                )
            ]
        )
        runtime = InventoryTransactions(body)

        result = runtime.discard_item(item="minecraft:dirt", count=1)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "perception_failed")
        self.assertEqual(body.actions, [])

    def test_reports_body_rejection_with_plan_facts(self):
        body = FakeInventoryBody([perception([slot(0, "minecraft:dirt", 1)])], accepted=False)
        runtime = InventoryTransactions(body)

        result = runtime.discard_item(item="minecraft:dirt", count=1)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "body_rejected")
        self.assertEqual(result.metrics["planned_count"], 1)
        self.assertEqual(len(body.actions), 1)

    def test_equip_mainhand_uses_select_item(self):
        body = FakeInventoryBody([perception([slot(0), slot(12, "minecraft:diamond_sword", 1)])])
        runtime = InventoryTransactions(body)

        result = runtime.equip_item(item="minecraft:diamond_sword", target="mainhand")

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "completed")
        self.assertEqual([action.name for action in body.actions], ["selectItem"])
        self.assertEqual(result.metrics["target"], "mainhand")

    def test_equip_mainhand_maps_missing_item(self):
        body = FakeInventoryBody(
            [perception([slot(0), slot(12, "minecraft:dirt", 1)])],
            select_success=False,
            select_reason="not_in_inventory",
        )
        runtime = InventoryTransactions(body)

        result = runtime.equip_item(item="minecraft:diamond_sword", target="mainhand")

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "item_not_available")

    def test_equip_offhand_moves_item_into_slot_40(self):
        body = FakeInventoryBody(
            [
                perception([slot(0), slot(18, "minecraft:arrow", 16), slot(40)]),
                perception([slot(0), slot(18), slot(40, "minecraft:arrow", 16)]),
            ]
        )
        runtime = InventoryTransactions(body)

        result = runtime.equip_item(item="minecraft:arrow", target="offhand")

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "completed")
        self.assertEqual([action.name for action in body.actions], ["moveItem"])
        self.assertEqual(body.actions[0].params["from_slot"], 18)
        self.assertEqual(body.actions[0].params["to_slot"], 40)
        self.assertNotIn("count", body.actions[0].params)

    def test_equip_armor_stages_existing_target_item_before_equip(self):
        body = FakeInventoryBody(
            [
                perception(
                    [
                        slot(0, "minecraft:cobblestone", 1),
                        slot(1, "minecraft:stick", 1),
                        slot(2, "minecraft:dirt", 1),
                        slot(3, "minecraft:torch", 1),
                        slot(4, "minecraft:bread", 1),
                        slot(5),
                        slot(22, "minecraft:diamond_helmet", 1),
                        slot(39, "minecraft:iron_helmet", 1),
                    ]
                ),
                perception(
                    [
                        slot(0, "minecraft:cobblestone", 1),
                        slot(1, "minecraft:stick", 1),
                        slot(2, "minecraft:dirt", 1),
                        slot(3, "minecraft:torch", 1),
                        slot(4, "minecraft:bread", 1),
                        slot(5, "minecraft:iron_helmet", 1),
                        slot(22),
                        slot(39, "minecraft:diamond_helmet", 1),
                    ]
                ),
            ]
        )
        runtime = InventoryTransactions(body)

        result = runtime.equip_item(item="minecraft:diamond_helmet", target="head")

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "completed")
        self.assertEqual([action.name for action in body.actions], ["moveItem", "moveItem"])
        self.assertEqual(body.actions[0].params["from_slot"], 39)
        self.assertEqual(body.actions[0].params["to_slot"], 5)
        self.assertEqual(body.actions[1].params["from_slot"], 22)
        self.assertEqual(body.actions[1].params["to_slot"], 39)
        self.assertEqual(body.actions[1].params["count"], 1)

    def test_equip_refuses_to_overwrite_when_no_swap_space(self):
        slots = [slot(i, f"minecraft:item_{i}", 1) for i in range(36)]
        slots[22] = slot(22, "minecraft:diamond_helmet", 1)
        slots.append(slot(39, "minecraft:iron_helmet", 1))
        slots.append(slot(40))
        body = FakeInventoryBody([perception(slots)])
        runtime = InventoryTransactions(body)

        result = runtime.equip_item(item="minecraft:diamond_helmet", target="head")

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "no_swap_space")
        self.assertEqual(body.actions, [])

    def test_equip_reports_already_equipped(self):
        body = FakeInventoryBody([perception([slot(39, "minecraft:diamond_helmet", 1)])])
        runtime = InventoryTransactions(body)

        result = runtime.equip_item(item="minecraft:diamond_helmet", target="head")

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "already_equipped")
        self.assertEqual(body.actions, [])

    def test_craft_exact_dispatches_explicit_recipe_and_verifies_slot_deltas(self):
        body = FakeInventoryBody(
            [
                perception([slot(0, "minecraft:oak_log", 1), slot(1)]),
                perception([slot(0), slot(1, "minecraft:oak_planks", 4)]),
            ]
        )
        runtime = InventoryTransactions(body)

        result = runtime.craft_exact(
            inputs=[{"slot": 0, "item": "minecraft:oak_log", "count": 1}],
            output={"slot": 1, "item": "minecraft:oak_planks", "count": 4},
        )

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "completed")
        self.assertEqual([action.name for action in body.actions], ["craftItem"])
        self.assertEqual(
            body.actions[0].params,
            {
                "inputs": [{"slot": 0, "item": "minecraft:oak_log", "count": 1}],
                "output": {"slot": 1, "item": "minecraft:oak_planks", "count": 4},
                "remainders": [],
                "max_stack": 64,
            },
        )
        self.assertEqual(result.metrics["input_before"][0]["count"], 1)
        self.assertEqual(result.metrics["input_after"][0]["count"], 0)
        self.assertEqual(result.metrics["output_after"]["count"], 4)

    def test_craft_exact_rejects_invalid_request_without_dispatch(self):
        body = FakeInventoryBody([perception([slot(0, "minecraft:oak_log", 1)])])
        runtime = InventoryTransactions(body)

        result = runtime.craft_exact(inputs=[], output={"slot": 1, "item": "minecraft:oak_planks", "count": 4})

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "invalid_craft_request")
        self.assertEqual(body.actions, [])

    def test_craft_exact_reports_craft_unverified_when_post_slots_do_not_match(self):
        body = FakeInventoryBody(
            [
                perception([slot(0, "minecraft:oak_log", 1), slot(1)]),
                perception([slot(0), slot(1, "minecraft:stick", 2)]),
            ]
        )
        runtime = InventoryTransactions(body)

        result = runtime.craft_exact(
            inputs=[{"slot": 0, "item": "minecraft:oak_log", "count": 1}],
            output={"slot": 1, "item": "minecraft:oak_planks", "count": 4},
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "craft_unverified")
        self.assertEqual([action.name for action in body.actions], ["craftItem"])

    def test_craft_exact_reports_craft_unverified_when_unrelated_slot_changes(self):
        body = FakeInventoryBody(
            [
                perception([slot(0, "minecraft:oak_log", 1), slot(1), slot(5, "minecraft:torch", 3)]),
                perception([slot(0), slot(1, "minecraft:oak_planks", 4), slot(5, "minecraft:stick", 1)]),
            ]
        )
        runtime = InventoryTransactions(body)

        result = runtime.craft_exact(
            inputs=[{"slot": 0, "item": "minecraft:oak_log", "count": 1}],
            output={"slot": 1, "item": "minecraft:oak_planks", "count": 4},
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "craft_unverified")
        self.assertEqual([action.name for action in body.actions], ["craftItem"])

    def test_craft_exact_verifies_same_slot_remainder_output(self):
        body = FakeInventoryBody(
            [
                perception([slot(0, "minecraft:milk_bucket", 1), slot(1, "minecraft:wheat", 3), slot(9)]),
                perception([slot(0, "minecraft:bucket", 1), slot(1), slot(9, "minecraft:cake", 1)]),
            ]
        )
        runtime = InventoryTransactions(body)

        result = runtime.craft_exact(
            inputs=[
                {"slot": 0, "item": "minecraft:milk_bucket", "count": 1},
                {"slot": 1, "item": "minecraft:wheat", "count": 3},
            ],
            output={"slot": 9, "item": "minecraft:cake", "count": 1},
            remainders=[{"slot": 0, "item": "minecraft:bucket", "count": 1}],
        )

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "completed")
        self.assertEqual(body.actions[0].params["remainders"], [{"slot": 0, "item": "minecraft:bucket", "count": 1}])

    def test_craft_recipe_uses_runtime_recipe_truth_for_non_table_recipe(self):
        body = FakeInventoryBody(
            [
                perception([slot(0), slot(41), slot(42)]),
                perception([slot(0, "minecraft:oak_log", 1), slot(1), slot(41), slot(42)]),
                perception([slot(0, "minecraft:oak_log", 1), slot(1)]),
                perception([slot(0), slot(1, "minecraft:oak_planks", 4)]),
                perception([slot(0), slot(1, "minecraft:oak_planks", 4), slot(41), slot(42)]),
            ],
            recipe_data={
                "minecraft:oak_planks": '[[[[oak_planks, 4, {count:4,id:"minecraft:oak_planks"}]], [[oak_log, oak_wood]], [shapeless]]]'
            },
        )
        runtime = InventoryTransactions(body)

        result = runtime.craft_recipe(item="minecraft:oak_planks", count=4, output_slot=1)

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "completed")
        self.assertEqual([action.name for action in body.actions], ["craftItem"])
        self.assertEqual(body.actions[0].params["inputs"], [{"slot": 0, "item": "minecraft:oak_log", "count": 1}])
        self.assertEqual(body.actions[0].params["output"], {"slot": 1, "item": "minecraft:oak_planks", "count": 4})

    def test_cleanup_crafting_residue_merges_then_moves_to_empty_inventory_slot(self):
        body = FakeInventoryBody(
            [
                perception(
                    [
                        slot(0, "minecraft:stick", 60),
                        slot(1),
                        slot(41, "minecraft:stick", 6),
                        slot(42, "minecraft:string", 2),
                    ]
                ),
                perception(
                    [
                        slot(0, "minecraft:stick", 64),
                        slot(1),
                        slot(41, "minecraft:stick", 2),
                        slot(42, "minecraft:string", 2),
                    ]
                ),
                perception(
                    [
                        slot(0, "minecraft:stick", 64),
                        slot(1, "minecraft:stick", 2),
                        slot(41),
                        slot(42, "minecraft:string", 2),
                    ]
                ),
                perception(
                    [
                        slot(0, "minecraft:stick", 64),
                        slot(1, "minecraft:stick", 2),
                        slot(2, "minecraft:string", 2),
                        slot(41),
                        slot(42),
                    ]
                ),
            ]
        )
        runtime = InventoryTransactions(body)

        result = runtime.cleanup_crafting_residue(residue_slots=(41, 42), destination_slots=(0, 1, 2))

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "completed")
        self.assertEqual([action.name for action in body.actions], ["moveItem", "moveItem", "moveItem"])
        self.assertEqual(body.actions[0].params, {"from_slot": 41, "to_slot": 0, "count": 4})
        self.assertEqual(body.actions[1].params, {"from_slot": 41, "to_slot": 1, "count": 2})
        self.assertEqual(body.actions[2].params, {"from_slot": 42, "to_slot": 2, "count": 2})

    def test_cleanup_crafting_residue_reports_already_clean_without_dispatch(self):
        body = FakeInventoryBody([perception([slot(0), slot(41), slot(42)])])
        runtime = InventoryTransactions(body)

        result = runtime.cleanup_crafting_residue(residue_slots=(41, 42), destination_slots=(0, 1))

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "already_clean")
        self.assertEqual(body.actions, [])

    def test_cleanup_crafting_residue_refuses_when_no_destination_space(self):
        body = FakeInventoryBody(
            [
                perception(
                    [
                        slot(0, "minecraft:stone", 64),
                        slot(1, "minecraft:dirt", 64),
                        slot(41, "minecraft:stick", 1),
                    ]
                )
            ]
        )
        runtime = InventoryTransactions(body)

        result = runtime.cleanup_crafting_residue(residue_slots=(41,), destination_slots=(0, 1))

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "crafting_residue_no_space")
        self.assertEqual(body.actions, [])


if __name__ == "__main__":
    unittest.main()
