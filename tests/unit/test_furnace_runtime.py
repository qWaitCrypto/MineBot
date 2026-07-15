import unittest

from minebot.body import BlockWork, FurnaceTransactions
from minebot.body.furnace import resolve_smelt_output, select_fuel
from minebot.contract import Action, BodyState, BreakContext, Event, PerceptionResult, Result, ToolResult
from minebot.game.governance import GovernancePolicy, Region
from minebot.game.navigation import GoalComposite
from tests.unit._body_batch_helper import batch_block_cells_from_blockat


class FakeFurnaceBody:
    bot_name = "Bot1"

    def __init__(
        self,
        furnace,
        inventory,
        *,
        accepted: bool = True,
        block_states=None,
        mutable: bool = False,
        terminal_count: int | None = None,
        applied_count: int | None = None,
        auto_smelt_output: tuple[str, int] | None = None,
        auto_smelt_after_reads: int = 1,
    ):
        self.furnace = furnace
        self.inventory = inventory
        self.accepted = accepted
        self.mutable = mutable
        self.terminal_count = terminal_count
        self.applied_count = applied_count
        self.auto_smelt_output = auto_smelt_output
        self.auto_smelt_after_reads = auto_smelt_after_reads
        self.auto_smelt_done = False
        self.container_reads = 0
        self.block_states = dict(block_states or {(1, 59, 0): ("minecraft:furnace", "SOLID")})
        self.state_pos = (0.5, 59.0, 0.5)
        self.actions: list[Action] = []
        self.moved_by_action: dict[str, int] = {}
        self.perceptions: list[tuple[str, dict[str, object]]] = []
        self.furnace_slots = {
            int(entry["slot"]): dict(entry) for entry in (self.furnace.data.get("slots") or [])
        }
        self.inventory_slots = {
            int(entry["slot"]): dict(entry) for entry in (self.inventory.data.get("slots") or [])
        }

    def perceive(self, scope: str, params: dict[str, object]) -> PerceptionResult:
        self.perceptions.append((scope, params))
        if scope == "container":
            if self.mutable:
                self.container_reads += 1
                self._maybe_auto_smelt()
            if not self.mutable:
                return self.furnace
            return perception(
                "container",
                [
                    self.furnace_slots.get(index, slot(index))
                    for index in range(3)
                ],
            )
        if scope == "inventory":
            if not self.mutable:
                return self.inventory
            max_slot = max(self.inventory_slots.keys(), default=-1)
            start = int(params.get("start", 0))
            limit = int(params.get("limit", max_slot + 1))
            end = min(max_slot + 1, start + limit)
            next_start = None if end >= max_slot + 1 else end
            return perception(
                "inventory",
                [
                    self.inventory_slots.get(index, slot(index))
                    for index in range(start, end)
                ],
                complete=next_start is None,
                next_start=next_start,
                total_slots=max_slot + 1,
                start=start,
                limit=limit,
            )
        if scope == "blockCells":
            return batch_block_cells_from_blockat(self, params)
        if scope == "blockAt":
            pos = (int(params["x"]), int(params["y"]), int(params["z"]))
            block_type, state = self.block_states[pos]
            return PerceptionResult(
                bot="Bot1",
                scope="blockAt",
                type="perception",
                ok=True,
                complete=True,
                data={"x": pos[0], "y": pos[1], "z": pos[2], "type": block_type, "state": state},
                uncertainty=[],
                next=None,
                error=None,
            )
        raise AssertionError(f"unexpected scope {scope}")

    def get_state(self) -> BodyState:
        return state_at(self.state_pos)

    def _maybe_auto_smelt(self) -> None:
        if (
            self.auto_smelt_output is None
            or self.auto_smelt_done
            or self.container_reads < self.auto_smelt_after_reads
        ):
            return
        input_slot = self.furnace_slots.get(0, slot(0))
        fuel_slot = self.furnace_slots.get(1, slot(1))
        output_slot = self.furnace_slots.get(2, slot(2))
        if input_slot.get("empty") or fuel_slot.get("empty") or not output_slot.get("empty"):
            return
        item, count = self.auto_smelt_output
        consumed = min(int(input_slot.get("count") or 0), count)
        self.furnace_slots[0] = _adjust_slot(input_slot, -consumed)
        self.furnace_slots[1] = _adjust_slot(fuel_slot, -1)
        self.furnace_slots[2] = slot(2, item, consumed)
        self.auto_smelt_done = True

    def execute(self, action: Action) -> Result:
        self.actions.append(action)
        if self.accepted and action.name == "furnaceTransfer":
            self._apply_furnace_transfer(action)
        if self.accepted and action.name == "placeBlock":
            target = tuple(action.params["target"])
            self.block_states[target] = (str(action.params["block_type"]), "SOLID")
            self.furnace_slots = {0: slot(0), 1: slot(1), 2: slot(2)}
        if self.accepted and action.name == "mineBlock":
            target = tuple(action.params["target"])
            self.block_states[target] = ("minecraft:air", "CLEAR")
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
        if action.name == "selectItem":
            return Event(
                seq=len(self.actions),
                tick=20,
                bot="Bot1",
                name="selectItemDone",
                data={
                    "action_id": action_id,
                    "success": True,
                    "stopped_reason": "completed",
                    "item": action.params["item"],
                    "slot": 0,
                },
            )
        if action.name == "placeBlock":
            return Event(
                seq=len(self.actions),
                tick=20,
                bot="Bot1",
                name="placeDone",
                data={
                    "action_id": action_id,
                    "success": True,
                    "stopped_reason": "completed",
                    "block_at_target": action.params["block_type"],
                },
            )
        if action.name == "mineBlock":
            return Event(
                seq=len(self.actions),
                tick=20,
                bot="Bot1",
                name="mineDone",
                data={
                    "action_id": action_id,
                    "success": True,
                    "stopped_reason": "completed",
                    "block_gone": True,
                },
            )
        count = self.terminal_count if self.terminal_count is not None else self.moved_by_action.get(action_id)
        if count is None:
            count = self.applied_count
        if count is None:
            count = 1
        return Event(
            seq=len(self.actions),
            tick=20,
            bot="Bot1",
            name="furnaceDone",
            data={
                "action_id": action_id,
                "success": True,
                "stopped_reason": "completed",
                "count": count,
                "furnace_slot": action.params["furnace_slot"],
            },
        )

    def _apply_furnace_transfer(self, action: Action) -> None:
        direction = str(action.params["direction"])
        furnace_slot_name = str(action.params["furnace_slot"])
        furnace_slot_index = {"input": 0, "fuel": 1, "output": 2}[furnace_slot_name]
        bot_slot_index = int(action.params["bot_slot"])
        moved = self.applied_count if self.applied_count is not None else (
            self.terminal_count if self.terminal_count is not None else 1
        )
        if action.params.get("count") is not None:
            moved = min(moved, int(action.params["count"]))
        if moved <= 0:
            return

        furnace_slot_entry = self.furnace_slots.get(furnace_slot_index, slot(furnace_slot_index))
        bot_slot_entry = self.inventory_slots.get(bot_slot_index, slot(bot_slot_index))

        if direction == "furnace_to_bot":
            item = furnace_slot_entry.get("item")
            moved = min(moved, int(furnace_slot_entry.get("count") or 0))
            self.furnace_slots[furnace_slot_index] = _adjust_slot(furnace_slot_entry, -moved)
            self.inventory_slots[bot_slot_index] = _adjust_slot(bot_slot_entry, moved, item=item)
            self.moved_by_action[action.id] = moved
            return

        item = bot_slot_entry.get("item")
        moved = min(moved, int(bot_slot_entry.get("count") or 0))
        self.inventory_slots[bot_slot_index] = _adjust_slot(bot_slot_entry, -moved)
        self.furnace_slots[furnace_slot_index] = _adjust_slot(furnace_slot_entry, moved, item=item)
        self.moved_by_action[action.id] = moved


def perception(scope, slots, *, complete=True, ok=True, next_start=None, total_slots=None, start=0, limit=None):
    return PerceptionResult(
        bot="Bot1",
        scope=scope,
        type="perception",
        ok=ok,
        complete=complete,
        data={
            "start": start,
            "limit": len(slots) if limit is None else limit,
            "nextStart": next_start,
            "totalSlots": len(slots) if total_slots is None else total_slots,
            "slots": slots,
        },
        uncertainty=[] if complete else [{"reason": "truncated"}],
        next=None if complete else str(next_start),
        error=None if ok else "failed",
    )


def recipe_perception(item, recipe_raw, *, ok=True):
    return PerceptionResult(
        bot="Bot1",
        scope="recipeData",
        type="perception",
        ok=ok,
        complete=True,
        data={"item": item, "recipe_raw": recipe_raw} if ok else {},
        uncertainty=[],
        next=None,
        error=None if ok else "recipe_not_found",
    )


def slot(index, item=None, count=0):
    return {"slot": index, "empty": item is None or count <= 0, "item": item, "count": count}


def _adjust_slot(current, delta, *, item=None):
    new_count = int(current.get("count") or 0) + delta
    if new_count <= 0:
        return slot(int(current["slot"]))
    chosen_item = item if item is not None else current.get("item")
    return slot(int(current["slot"]), chosen_item, new_count)


def state_at(pos):
    return BodyState(
        bot="Bot1",
        pos=(float(pos[0]), float(pos[1]), float(pos[2])),
        yaw=0.0,
        pitch=0.0,
        health=20.0,
        food=20,
        oxygen=None,
        inventory_raw="[]",
        inventory_hash="inv",
        effects=None,
        time=0,
        weather=None,
        dimension="overworld",
        complete=True,
    )


class FakeInteractionNavigator:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def navigate_to(self, goal, **kwargs):
        self.calls.append((goal, kwargs))
        if len(self.results) == 1:
            return self.results[0]
        return self.results.pop(0)


class FurnaceRuntimeTests(unittest.TestCase):
    def test_select_fuel_prefers_dedicated_fuels_and_normalizes_counts(self):
        result = select_fuel(
            {
                "minecraft:oak_log": 4,
                "minecraft:oak_planks": 1,
                "charcoal": 1,
                "coal": 1,
            },
            30.0,
        )

        self.assertEqual(result, ("coal", 1))

    def test_select_fuel_requires_one_kind_to_cover_budget(self):
        result = select_fuel({"minecraft:stick": 1, "minecraft:bamboo": 2}, 10.0)

        self.assertIsNone(result)

    def test_select_fuel_uses_planks_before_logs(self):
        result = select_fuel({"oak_log": 2, "oak_planks": 2}, 30.0)

        self.assertEqual(result, ("oak_planks", 2))

    def test_select_fuel_returns_none_for_empty_or_nonpositive_budget(self):
        self.assertIsNone(select_fuel({}, 10.0))
        self.assertIsNone(select_fuel({"coal": 1}, 0.0))

    def test_resolve_smelt_output_static_candidates(self):
        self.assertEqual(resolve_smelt_output("minecraft:raw_iron"), ("iron_ingot", 1))
        self.assertEqual(resolve_smelt_output("sand"), ("glass", 1))
        self.assertEqual(resolve_smelt_output("oak_log"), ("charcoal", 1))
        self.assertIsNone(resolve_smelt_output("minecraft:diamond"))

    def test_resolve_smelt_output_verifies_runtime_recipe_input(self):
        calls = []

        def lookup(output_item):
            calls.append(output_item)
            return recipe_perception(
                output_item,
                '[[[[iron_ingot, 1, {count:1,id:"minecraft:iron_ingot"}]], [[deepslate_iron_ore]], [smelting, 200, 0.699999988079]], '
                '[[[iron_ingot, 1, {count:1,id:"minecraft:iron_ingot"}]], [[iron_ore]], [smelting, 200, 0.699999988079]], '
                '[[[iron_ingot, 1, {count:1,id:"minecraft:iron_ingot"}]], [[raw_iron]], [smelting, 200, 0.699999988079]]]'
            )

        result = resolve_smelt_output("minecraft:raw_iron", lookup)

        self.assertEqual(result, ("iron_ingot", 1))
        self.assertEqual(calls, ["iron_ingot"])

    def test_resolve_smelt_output_falls_back_when_runtime_recipe_unavailable(self):
        self.assertEqual(resolve_smelt_output("minecraft:raw_iron", lambda output_item: None), ("iron_ingot", 1))
        self.assertEqual(
            resolve_smelt_output("minecraft:raw_iron", lambda output_item: recipe_perception(output_item, "", ok=False)),
            ("iron_ingot", 1),
        )
        self.assertEqual(
            resolve_smelt_output("minecraft:raw_iron", lambda output_item: recipe_perception(output_item, "")),
            ("iron_ingot", 1),
        )

    def test_resolve_smelt_output_rejects_runtime_recipe_without_input(self):
        def lookup(output_item):
            return recipe_perception(
                output_item,
                '[[[[iron_ingot, 1, {count:1,id:"minecraft:iron_ingot"}]], [[iron_ore]], [smelting, 200, 0.699999988079]]]'
            )

        self.assertIsNone(resolve_smelt_output("minecraft:raw_iron", lookup))

    def test_smelt_preflight_reads_inventory_in_small_pages(self):
        body = FakeFurnaceBody(
            perception("container", [slot(0), slot(1), slot(2)]),
            perception("inventory", [slot(0, "minecraft:raw_iron", 1), slot(13, "minecraft:coal", 1), slot(14)]),
            mutable=True,
        )
        runtime = FurnaceTransactions(body)

        result = runtime.smelt_once(
            (1, 59, 0),
            input_item="minecraft:raw_iron",
            input_count=1,
            fuel_item="minecraft:coal",
            fuel_count=1,
            output_item="minecraft:iron_ingot",
            output_count=1,
            output_slot=14,
            smelt_timeout_s=0.01,
        )

        self.assertFalse(result.success)
        self.assertIn(result.reason, {"smelt_timeout", "smelt_output_missing", "smelt_partial_timeout"})
        inventory_reads = [params for scope, params in body.perceptions if scope == "inventory"]
        self.assertEqual(inventory_reads[:2], [{"start": 0, "limit": 12}, {"start": 12, "limit": 12}])

    def test_clear_furnace_moves_output_input_then_fuel(self):
        body = FakeFurnaceBody(
            perception(
                "container",
                [
                    slot(0, "minecraft:iron_ore", 1),
                    slot(1, "minecraft:coal", 1),
                    slot(2, "minecraft:iron_ingot", 1),
                ],
            ),
            perception("inventory", [slot(0), slot(1), slot(2)]),
            mutable=True,
        )
        runtime = FurnaceTransactions(body)

        result = runtime.clear_furnace((1, 59, 0))

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "completed")
        self.assertEqual([action.params["furnace_slot"] for action in body.actions], ["output", "input", "fuel"])
        self.assertEqual([action.params["bot_slot"] for action in body.actions], [0, 1, 2])
        self.assertEqual(result.metrics["moved_count"], 3)

    def test_clear_furnace_reports_already_empty_without_actions(self):
        body = FakeFurnaceBody(
            perception("container", [slot(0), slot(1), slot(2)]),
            perception("inventory", [slot(0)]),
        )
        runtime = FurnaceTransactions(body)

        result = runtime.clear_furnace((1, 59, 0))

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "already_empty")
        self.assertEqual(body.actions, [])

    def test_clear_furnace_reports_full_inventory_without_actions(self):
        body = FakeFurnaceBody(
            perception("container", [slot(0, "minecraft:iron_ore", 1), slot(1), slot(2)]),
            perception("inventory", [slot(0, "minecraft:dirt", 64)]),
        )
        runtime = FurnaceTransactions(body)

        result = runtime.clear_furnace((1, 59, 0))

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "bot_inventory_full")
        self.assertEqual(body.actions, [])

    def test_clear_furnace_reports_body_rejection_with_plan_facts(self):
        body = FakeFurnaceBody(
            perception("container", [slot(0), slot(1), slot(2, "minecraft:iron_ingot", 1)]),
            perception("inventory", [slot(0)]),
            accepted=False,
        )
        runtime = FurnaceTransactions(body)

        result = runtime.clear_furnace((1, 59, 0))

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "furnace_clear_failed:body_rejected")
        self.assertEqual(result.metrics["occupied_furnace_slots"], 1)
        self.assertEqual(len(body.actions), 1)

    def test_clear_furnace_respects_governance_protection(self):
        body = FakeFurnaceBody(
            perception("container", [slot(0), slot(1), slot(2, "minecraft:iron_ingot", 1)]),
            perception("inventory", [slot(0)]),
        )
        runtime = FurnaceTransactions(body, governance=GovernancePolicy())

        result = runtime.clear_furnace((1, 59, 0))

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "furnace_denied")
        self.assertEqual(result.metrics["legality"]["reason"], "unknown_provenance")
        self.assertEqual(body.actions, [])

    def test_clear_furnace_rejects_wrong_block_type(self):
        body = FakeFurnaceBody(
            perception("container", [slot(0), slot(1), slot(2, "minecraft:iron_ingot", 1)]),
            perception("inventory", [slot(0)]),
            block_states={(1, 59, 0): ("minecraft:chest", "SOLID")},
        )
        runtime = FurnaceTransactions(body)

        result = runtime.clear_furnace((1, 59, 0))

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "furnace_wrong_type")
        self.assertEqual(body.actions, [])

    def test_clear_furnace_refuses_incomplete_perception(self):
        body = FakeFurnaceBody(
            perception("container", [slot(0, "minecraft:iron_ore", 1)], complete=False),
            perception("inventory", [slot(0)]),
        )
        runtime = FurnaceTransactions(body)

        result = runtime.clear_furnace((1, 59, 0))

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "perception_failed")
        self.assertEqual(body.actions, [])

    def test_clear_furnace_refuses_missing_body_perception(self):
        body = FakeFurnaceBody(
            PerceptionResult(
                bot="Bot1",
                scope="container",
                type="perception",
                ok=False,
                complete=True,
                data={},
                uncertainty=[{"reason": "missing_body"}],
                next=None,
                error="missing_body",
            ),
            perception("inventory", [slot(0)]),
        )
        runtime = FurnaceTransactions(body)

        result = runtime.clear_furnace((1, 59, 0))

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "perception_failed")
        self.assertEqual(body.actions, [])

    def test_clear_nearest_furnace_reports_not_found(self):
        body = FakeFurnaceBody(
            perception("container", []),
            perception("inventory", []),
        )
        body.get_state = lambda: state_at((0, 64, 0))
        original_perceive = body.perceive

        def perceive(scope, params):
            if scope == "findBlocks":
                return PerceptionResult(
                    bot="Bot1",
                    scope="findBlocks",
                    type="perception",
                    ok=True,
                    complete=True,
                    data={"blocks": []},
                    uncertainty=[],
                    next=None,
                    error=None,
                )
            return original_perceive(scope, params)

        body.perceive = perceive
        runtime = FurnaceTransactions(body)

        result = runtime.clear_nearest_furnace()

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "furnace_not_found")

    def test_clear_nearest_furnace_requires_navigation_when_out_of_range(self):
        body = FakeFurnaceBody(
            perception("container", []),
            perception("inventory", []),
        )
        body.get_state = lambda: state_at((0, 64, 0))

        def perceive(scope, params):
            if scope == "blockCells":
                return batch_block_cells_from_blockat(body, params)
            if scope == "findBlocks":
                return PerceptionResult(
                    bot="Bot1",
                    scope="findBlocks",
                    type="perception",
                    ok=True,
                    complete=True,
                    data={"blocks": [{"x": 8, "y": 64, "z": 0, "type": "minecraft:furnace"}]},
                    uncertainty=[],
                    next=None,
                    error=None,
                )
            if scope == "blockAt":
                pos = (int(params["x"]), int(params["y"]), int(params["z"]))
                mapping = {
                    (9, 64, 0): ("minecraft:air", "CLEAR"),
                    (9, 65, 0): ("minecraft:air", "CLEAR"),
                    (9, 63, 0): ("minecraft:stone", "SOLID"),
                    (7, 64, 0): ("minecraft:air", "CLEAR"),
                    (7, 65, 0): ("minecraft:air", "CLEAR"),
                    (7, 63, 0): ("minecraft:stone", "SOLID"),
                    (8, 64, 1): ("minecraft:air", "CLEAR"),
                    (8, 65, 1): ("minecraft:air", "CLEAR"),
                    (8, 63, 1): ("minecraft:stone", "SOLID"),
                    (8, 64, -1): ("minecraft:air", "CLEAR"),
                    (8, 65, -1): ("minecraft:air", "CLEAR"),
                    (8, 63, -1): ("minecraft:stone", "SOLID"),
                }
                block_type, state = mapping[pos]
                return PerceptionResult(
                    bot="Bot1",
                    scope="blockAt",
                    type="perception",
                    ok=True,
                    complete=True,
                    data={"x": pos[0], "y": pos[1], "z": pos[2], "type": block_type, "state": state},
                    uncertainty=[],
                    next=None,
                    error=None,
                )
            return body.furnace if scope == "container" else body.inventory

        body.perceive = perceive
        runtime = FurnaceTransactions(body)

        result = runtime.clear_nearest_furnace()

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "furnace_navigation_missing")
        self.assertEqual(result.metrics["attempted_targets"][0]["furnace_target"], [8, 64, 0])

    def test_clear_nearest_furnace_reports_navigation_failure(self):
        body = FakeFurnaceBody(
            perception("container", []),
            perception("inventory", []),
        )
        body.get_state = lambda: state_at((0, 64, 0))

        def perceive(scope, params):
            if scope == "blockCells":
                return batch_block_cells_from_blockat(body, params)
            if scope == "findBlocks":
                return PerceptionResult(
                    bot="Bot1",
                    scope="findBlocks",
                    type="perception",
                    ok=True,
                    complete=True,
                    data={"blocks": [{"x": 8, "y": 64, "z": 0, "type": "minecraft:furnace"}]},
                    uncertainty=[],
                    next=None,
                    error=None,
                )
            if scope == "blockAt":
                pos = (int(params["x"]), int(params["y"]), int(params["z"]))
                mapping = {
                    (9, 64, 0): ("minecraft:air", "CLEAR"),
                    (9, 65, 0): ("minecraft:air", "CLEAR"),
                    (9, 63, 0): ("minecraft:stone", "SOLID"),
                    (7, 64, 0): ("minecraft:air", "CLEAR"),
                    (7, 65, 0): ("minecraft:air", "CLEAR"),
                    (7, 63, 0): ("minecraft:stone", "SOLID"),
                    (8, 64, 1): ("minecraft:air", "CLEAR"),
                    (8, 65, 1): ("minecraft:air", "CLEAR"),
                    (8, 63, 1): ("minecraft:stone", "SOLID"),
                    (8, 64, -1): ("minecraft:air", "CLEAR"),
                    (8, 65, -1): ("minecraft:air", "CLEAR"),
                    (8, 63, -1): ("minecraft:stone", "SOLID"),
                }
                block_type, state = mapping[pos]
                return PerceptionResult(
                    bot="Bot1",
                    scope="blockAt",
                    type="perception",
                    ok=True,
                    complete=True,
                    data={"x": pos[0], "y": pos[1], "z": pos[2], "type": block_type, "state": state},
                    uncertainty=[],
                    next=None,
                    error=None,
                )
            return body.furnace if scope == "container" else body.inventory

        body.perceive = perceive
        navigator = FakeInteractionNavigator(
            [
                ToolResult(success=False, reason="blocked", can_retry=True),
                ToolResult(success=False, reason="blocked", can_retry=True),
                ToolResult(success=False, reason="blocked", can_retry=True),
                ToolResult(success=False, reason="blocked", can_retry=True),
            ]
        )
        runtime = FurnaceTransactions(body, navigator=navigator)

        result = runtime.clear_nearest_furnace()

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "furnace_navigation_failed:blocked")
        self.assertEqual(len(navigator.calls), 1)
        self.assertIsInstance(navigator.calls[0][0], GoalComposite)

    def test_clear_nearest_furnace_navigates_then_clears(self):
        body = FakeFurnaceBody(
            perception("container", [slot(0), slot(1), slot(2, "minecraft:iron_ingot", 1)]),
            perception("inventory", [slot(0)]),
            mutable=True,
        )
        states = [state_at((0, 64, 0)), state_at((0, 64, 0)), state_at((7, 64, 0))]
        body.get_state = lambda: states[0] if len(states) == 1 else states.pop(0)
        original_perceive = body.perceive

        def perceive(scope, params):
            if scope == "findBlocks":
                return PerceptionResult(
                    bot="Bot1",
                    scope="findBlocks",
                    type="perception",
                    ok=True,
                    complete=True,
                    data={"blocks": [{"x": 8, "y": 64, "z": 0, "type": "minecraft:furnace"}]},
                    uncertainty=[],
                    next=None,
                    error=None,
                )
            if scope == "blockAt":
                pos = (int(params["x"]), int(params["y"]), int(params["z"]))
                mapping = {
                    (8, 64, 0): ("minecraft:furnace", "SOLID"),
                    (9, 64, 0): ("minecraft:air", "CLEAR"),
                    (9, 65, 0): ("minecraft:air", "CLEAR"),
                    (9, 63, 0): ("minecraft:stone", "SOLID"),
                    (7, 64, 0): ("minecraft:air", "CLEAR"),
                    (7, 65, 0): ("minecraft:air", "CLEAR"),
                    (7, 63, 0): ("minecraft:stone", "SOLID"),
                    (8, 64, 1): ("minecraft:air", "CLEAR"),
                    (8, 65, 1): ("minecraft:air", "CLEAR"),
                    (8, 63, 1): ("minecraft:stone", "SOLID"),
                    (8, 64, -1): ("minecraft:air", "CLEAR"),
                    (8, 65, -1): ("minecraft:air", "CLEAR"),
                    (8, 63, -1): ("minecraft:stone", "SOLID"),
                }
                block_type, state = mapping[pos]
                return PerceptionResult(
                    bot="Bot1",
                    scope="blockAt",
                    type="perception",
                    ok=True,
                    complete=True,
                    data={"x": pos[0], "y": pos[1], "z": pos[2], "type": block_type, "state": state},
                    uncertainty=[],
                    next=None,
                    error=None,
                )
            return original_perceive(scope, params)

        body.perceive = perceive
        navigator = FakeInteractionNavigator([ToolResult(success=True, reason="arrived", can_retry=False)])
        runtime = FurnaceTransactions(body, navigator=navigator)

        result = runtime.clear_nearest_furnace()

        self.assertTrue(result.success)
        self.assertEqual(result.metrics["furnace_target"], [8, 64, 0])
        self.assertTrue(result.metrics["approach"]["navigated"])
        self.assertEqual(len(body.actions), 1)

    def test_clear_nearest_furnace_respects_governance_protection(self):
        body = FakeFurnaceBody(
            perception("container", [slot(0), slot(1), slot(2, "minecraft:iron_ingot", 1)]),
            perception("inventory", [slot(0)]),
        )
        body.get_state = lambda: state_at((0, 64, 0))

        def perceive(scope, params):
            if scope == "findBlocks":
                return PerceptionResult(
                    bot="Bot1",
                    scope="findBlocks",
                    type="perception",
                    ok=True,
                    complete=True,
                    data={"blocks": [{"x": 8, "y": 64, "z": 0, "type": "minecraft:furnace"}]},
                    uncertainty=[],
                    next=None,
                    error=None,
                )
            if scope == "blockAt":
                pos = (int(params["x"]), int(params["y"]), int(params["z"]))
                mapping = {
                    (8, 64, 0): ("minecraft:furnace", "SOLID"),
                    (9, 64, 0): ("minecraft:air", "CLEAR"),
                    (9, 65, 0): ("minecraft:air", "CLEAR"),
                    (9, 63, 0): ("minecraft:stone", "SOLID"),
                    (7, 64, 0): ("minecraft:air", "CLEAR"),
                    (7, 65, 0): ("minecraft:air", "CLEAR"),
                    (7, 63, 0): ("minecraft:stone", "SOLID"),
                    (8, 64, 1): ("minecraft:air", "CLEAR"),
                    (8, 65, 1): ("minecraft:air", "CLEAR"),
                    (8, 63, 1): ("minecraft:stone", "SOLID"),
                    (8, 64, -1): ("minecraft:air", "CLEAR"),
                    (8, 65, -1): ("minecraft:air", "CLEAR"),
                    (8, 63, -1): ("minecraft:stone", "SOLID"),
                }
                block_type, state = mapping[pos]
                return PerceptionResult(
                    bot="Bot1",
                    scope="blockAt",
                    type="perception",
                    ok=True,
                    complete=True,
                    data={"x": pos[0], "y": pos[1], "z": pos[2], "type": block_type, "state": state},
                    uncertainty=[],
                    next=None,
                    error=None,
                )
            return body.furnace if scope == "container" else body.inventory

        body.perceive = perceive
        policy = GovernancePolicy(protected_regions=[Region("base", (8, 60, 0), (8, 70, 0))])
        navigator = FakeInteractionNavigator([ToolResult(success=True, reason="arrived", can_retry=False)])
        runtime = FurnaceTransactions(body, navigator=navigator, governance=policy)

        result = runtime.clear_nearest_furnace()

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "furnace_denied")
        self.assertEqual(result.metrics["legality"]["reason"], "protected_region")
        self.assertEqual(navigator.calls, [])
        self.assertEqual(body.actions, [])

    def test_smelt_once_deposits_polls_and_collects_output(self):
        body = FakeFurnaceBody(
            perception("container", [slot(0), slot(1), slot(2)]),
            perception("inventory", [slot(0, "minecraft:iron_ore", 1), slot(1, "minecraft:coal", 1), slot(2)]),
            mutable=True,
            auto_smelt_output=("minecraft:iron_ingot", 1),
            auto_smelt_after_reads=6,
        )
        runtime = FurnaceTransactions(body)

        result = runtime.smelt_once(
            (1, 59, 0),
            input_item="minecraft:iron_ore",
            input_count=1,
            fuel_item="minecraft:coal",
            output_item="minecraft:iron_ingot",
            output_count=1,
            poll_interval_s=0.0,
            smelt_timeout_s=1.0,
        )

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "completed")
        self.assertEqual(
            [(action.params["direction"], action.params["furnace_slot"], action.params["bot_slot"]) for action in body.actions],
            [("bot_to_furnace", "input", 0), ("bot_to_furnace", "fuel", 1), ("furnace_to_bot", "output", 2)],
        )

    def test_smelt_once_refuses_non_empty_furnace_before_mutation(self):
        body = FakeFurnaceBody(
            perception("container", [slot(0, "minecraft:gold_ore", 1), slot(1), slot(2)]),
            perception("inventory", [slot(0, "minecraft:iron_ore", 1), slot(1, "minecraft:coal", 1), slot(2)]),
            mutable=True,
        )
        runtime = FurnaceTransactions(body)

        result = runtime.smelt_once(
            (1, 59, 0),
            input_item="minecraft:iron_ore",
            input_count=1,
            fuel_item="minecraft:coal",
            output_item="minecraft:iron_ingot",
            output_count=1,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "smelt_furnace_not_empty")
        self.assertEqual(body.actions, [])

    def test_smelt_once_timeout_reclaims_input_and_fuel(self):
        body = FakeFurnaceBody(
            perception("container", [slot(0), slot(1), slot(2)]),
            perception("inventory", [slot(0, "minecraft:iron_ore", 1), slot(1, "minecraft:coal", 1), slot(2), slot(3)]),
            mutable=True,
        )
        runtime = FurnaceTransactions(body)

        result = runtime.smelt_once(
            (1, 59, 0),
            input_item="minecraft:iron_ore",
            input_count=1,
            fuel_item="minecraft:coal",
            output_item="minecraft:iron_ingot",
            output_count=1,
            poll_interval_s=0.0,
            smelt_timeout_s=0.0,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "smelt_timeout")
        self.assertEqual(
            [(action.params["direction"], action.params["furnace_slot"]) for action in body.actions],
            [
                ("bot_to_furnace", "input"),
                ("bot_to_furnace", "fuel"),
                ("furnace_to_bot", "input"),
                ("furnace_to_bot", "fuel"),
            ],
        )

    def test_smelt_once_timeout_collects_partial_output_before_reclaim(self):
        body = FakeFurnaceBody(
            perception("container", [slot(0), slot(1), slot(2)]),
            perception("inventory", [slot(0, "minecraft:iron_ore", 2), slot(1, "minecraft:coal", 1), slot(2), slot(3)]),
            mutable=True,
            auto_smelt_output=("minecraft:iron_ingot", 1),
            auto_smelt_after_reads=6,
            applied_count=64,
        )
        runtime = FurnaceTransactions(body)

        result = runtime.smelt_once(
            (1, 59, 0),
            input_item="minecraft:iron_ore",
            input_count=2,
            fuel_item="minecraft:coal",
            output_item="minecraft:iron_ingot",
            output_count=2,
            output_slot=2,
            poll_interval_s=0.0,
            smelt_timeout_s=1.0,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "smelt_partial_timeout")
        self.assertEqual(result.metrics["partial_output"]["result"]["reason"], "completed")
        self.assertEqual(result.metrics["partial_output"]["result"]["metrics"]["count"], 1)
        self.assertEqual(
            [(action.params["direction"], action.params["furnace_slot"], action.params.get("count")) for action in body.actions],
            [
                ("bot_to_furnace", "input", 2),
                ("bot_to_furnace", "fuel", 1),
                ("furnace_to_bot", "output", 1),
                ("furnace_to_bot", "input", None),
            ],
        )
        self.assertEqual(result.metrics["reclaim"][1]["furnace_slot"], "fuel")
        self.assertEqual(result.metrics["reclaim"][1]["reason"], "already_empty")

    def test_smelt_once_auto_budgets_low_value_fuel(self):
        body = FakeFurnaceBody(
            perception("container", [slot(0), slot(1), slot(2)]),
            perception("inventory", [slot(0, "minecraft:iron_ore", 1), slot(1, "minecraft:bamboo", 4), slot(2)]),
            mutable=True,
            auto_smelt_output=("minecraft:iron_ingot", 1),
            auto_smelt_after_reads=6,
        )
        runtime = FurnaceTransactions(body)

        result = runtime.smelt_once(
            (1, 59, 0),
            input_item="minecraft:iron_ore",
            input_count=1,
            fuel_item="minecraft:bamboo",
            output_item="minecraft:iron_ingot",
            output_count=1,
            poll_interval_s=0.0,
            smelt_timeout_s=1.0,
        )

        self.assertTrue(result.success)
        self.assertEqual(body.actions[1].params["furnace_slot"], "fuel")
        self.assertEqual(body.actions[1].params["count"], 4)
        self.assertTrue(result.metrics["fuel"]["auto"])
        self.assertEqual(result.metrics["fuel"]["count"], 4)

    def test_smelt_once_auto_budgets_stick_fuel(self):
        body = FakeFurnaceBody(
            perception("container", [slot(0), slot(1), slot(2)]),
            perception("inventory", [slot(0, "minecraft:iron_ore", 1), slot(1, "minecraft:stick", 2), slot(2)]),
            mutable=True,
            auto_smelt_output=("minecraft:iron_ingot", 1),
            auto_smelt_after_reads=6,
        )
        runtime = FurnaceTransactions(body)

        result = runtime.smelt_once(
            (1, 59, 0),
            input_item="minecraft:iron_ore",
            input_count=1,
            fuel_item="minecraft:stick",
            output_item="minecraft:iron_ingot",
            output_count=1,
            poll_interval_s=0.0,
            smelt_timeout_s=1.0,
        )

        self.assertTrue(result.success)
        self.assertEqual(body.actions[1].params["furnace_slot"], "fuel")
        self.assertEqual(body.actions[1].params["count"], 2)
        self.assertTrue(result.metrics["fuel"]["auto"])
        self.assertEqual(result.metrics["fuel"]["count"], 2)
        self.assertEqual(result.metrics["fuel"]["seconds_available"], 10.0)

    def test_smelt_once_auto_budgets_wood_planks_and_logs_as_fuel(self):
        fuels = (
            "oak_planks",
            "spruce_planks",
            "birch_planks",
            "jungle_planks",
            "acacia_planks",
            "dark_oak_planks",
            "oak_log",
            "spruce_log",
            "birch_log",
            "jungle_log",
            "acacia_log",
            "dark_oak_log",
        )
        for fuel in fuels:
            with self.subTest(fuel=fuel):
                body = FakeFurnaceBody(
                    perception("container", [slot(0), slot(1), slot(2)]),
                    perception("inventory", [slot(0, "minecraft:iron_ore", 1), slot(1, f"minecraft:{fuel}", 1), slot(2)]),
                    mutable=True,
                    auto_smelt_output=("minecraft:iron_ingot", 1),
                    auto_smelt_after_reads=6,
                )
                runtime = FurnaceTransactions(body)

                result = runtime.smelt_once(
                    (1, 59, 0),
                    input_item="minecraft:iron_ore",
                    input_count=1,
                    fuel_item=f"minecraft:{fuel}",
                    output_item="minecraft:iron_ingot",
                    output_count=1,
                    poll_interval_s=0.0,
                    smelt_timeout_s=1.0,
                )

                self.assertTrue(result.success, result.to_payload())
                self.assertEqual(body.actions[1].params["furnace_slot"], "fuel")
                self.assertEqual(body.actions[1].params["count"], 1)
                self.assertTrue(result.metrics["fuel"]["auto"])
                self.assertEqual(result.metrics["fuel"]["count"], 1)
                self.assertEqual(result.metrics["fuel"]["seconds_available"], 15.0)

    def test_smelt_once_auto_fuel_refuses_insufficient_budget_before_mutation(self):
        body = FakeFurnaceBody(
            perception("container", [slot(0), slot(1), slot(2)]),
            perception("inventory", [slot(0, "minecraft:iron_ore", 1), slot(1, "minecraft:bamboo", 3), slot(2)]),
            mutable=True,
        )
        runtime = FurnaceTransactions(body)

        result = runtime.smelt_once(
            (1, 59, 0),
            input_item="minecraft:iron_ore",
            input_count=1,
            fuel_item="minecraft:bamboo",
            output_item="minecraft:iron_ingot",
            output_count=1,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "smelt_fuel_not_available")
        self.assertEqual(result.metrics["fuel_count"], 4)
        self.assertEqual(result.metrics["available_count"], 3)
        self.assertEqual(body.actions, [])

    def test_smelt_nearest_furnace_navigates_then_smelt_once(self):
        body = FakeFurnaceBody(
            perception("container", [slot(0), slot(1), slot(2)]),
            perception("inventory", [slot(0, "minecraft:iron_ore", 1), slot(1, "minecraft:coal", 1), slot(2)]),
            mutable=True,
            auto_smelt_output=("minecraft:iron_ingot", 1),
            auto_smelt_after_reads=6,
        )
        states = [state_at((0, 64, 0)), state_at((0, 64, 0)), state_at((7, 64, 0))]
        body.get_state = lambda: states[0] if len(states) == 1 else states.pop(0)
        original_perceive = body.perceive

        def perceive(scope, params):
            if scope == "findBlocks":
                return PerceptionResult(
                    bot="Bot1",
                    scope="findBlocks",
                    type="perception",
                    ok=True,
                    complete=True,
                    data={"blocks": [{"x": 8, "y": 64, "z": 0, "type": "minecraft:furnace"}]},
                    uncertainty=[],
                    next=None,
                    error=None,
                )
            if scope == "blockAt":
                pos = (int(params["x"]), int(params["y"]), int(params["z"]))
                mapping = {
                    (8, 64, 0): ("minecraft:furnace", "SOLID"),
                    (9, 64, 0): ("minecraft:air", "CLEAR"),
                    (9, 65, 0): ("minecraft:air", "CLEAR"),
                    (9, 63, 0): ("minecraft:stone", "SOLID"),
                    (7, 64, 0): ("minecraft:air", "CLEAR"),
                    (7, 65, 0): ("minecraft:air", "CLEAR"),
                    (7, 63, 0): ("minecraft:stone", "SOLID"),
                    (8, 64, 1): ("minecraft:air", "CLEAR"),
                    (8, 65, 1): ("minecraft:air", "CLEAR"),
                    (8, 63, 1): ("minecraft:stone", "SOLID"),
                    (8, 64, -1): ("minecraft:air", "CLEAR"),
                    (8, 65, -1): ("minecraft:air", "CLEAR"),
                    (8, 63, -1): ("minecraft:stone", "SOLID"),
                }
                block_type, state = mapping[pos]
                return PerceptionResult(
                    bot="Bot1",
                    scope="blockAt",
                    type="perception",
                    ok=True,
                    complete=True,
                    data={"x": pos[0], "y": pos[1], "z": pos[2], "type": block_type, "state": state},
                    uncertainty=[],
                    next=None,
                    error=None,
                )
            return original_perceive(scope, params)

        body.perceive = perceive
        navigator = FakeInteractionNavigator([ToolResult(success=True, reason="arrived", can_retry=False)])
        runtime = FurnaceTransactions(body, navigator=navigator)

        result = runtime.smelt_nearest_furnace(
            input_item="minecraft:iron_ore",
            input_count=1,
            fuel_item="minecraft:coal",
            output_item="minecraft:iron_ingot",
            output_count=1,
            search_radius=12,
            poll_interval_s=0.0,
            smelt_timeout_s=1.0,
        )

        self.assertTrue(result.success)
        self.assertEqual(result.metrics["furnace_target"], [8, 64, 0])
        self.assertTrue(result.metrics["approach"]["navigated"])
        self.assertEqual([action.params["furnace_slot"] for action in body.actions], ["input", "fuel", "output"])

    def test_smelt_nearest_furnace_reports_not_found_without_actions(self):
        body = FakeFurnaceBody(
            perception("container", [slot(0), slot(1), slot(2)]),
            perception("inventory", [slot(0, "minecraft:iron_ore", 1), slot(1, "minecraft:coal", 1), slot(2)]),
            mutable=True,
        )
        body.get_state = lambda: state_at((0, 64, 0))
        original_perceive = body.perceive

        def perceive(scope, params):
            if scope == "findBlocks":
                return PerceptionResult(
                    bot="Bot1",
                    scope="findBlocks",
                    type="perception",
                    ok=True,
                    complete=True,
                    data={"blocks": []},
                    uncertainty=[],
                    next=None,
                    error=None,
                )
            return original_perceive(scope, params)

        body.perceive = perceive
        runtime = FurnaceTransactions(body)

        result = runtime.smelt_nearest_furnace(
            input_item="minecraft:iron_ore",
            input_count=1,
            fuel_item="minecraft:coal",
            output_item="minecraft:iron_ingot",
            output_count=1,
            search_radius=6,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "furnace_not_found")
        self.assertEqual(body.actions, [])

    def test_smelt_with_temporary_furnace_places_smelts_and_reclaims_bot_owned_furnace(self):
        body = FakeFurnaceBody(
            perception("container", [slot(0), slot(1), slot(2)]),
            perception("inventory", [slot(0, "minecraft:furnace", 1), slot(1, "minecraft:iron_ore", 1), slot(2, "minecraft:coal", 1), slot(3)]),
            mutable=True,
            block_states={(2, 59, 0): ("minecraft:air", "CLEAR")},
            auto_smelt_output=("minecraft:iron_ingot", 1),
            auto_smelt_after_reads=6,
        )
        policy = GovernancePolicy(natural_regions=[Region("work", (0, 0, -2), (4, 100, 2))])
        work = BlockWork(body, policy)
        runtime = FurnaceTransactions(body, governance=policy, work=work)

        result = runtime.smelt_with_temporary_furnace(
            (2, 59, 0),
            input_item="minecraft:iron_ore",
            input_count=1,
            fuel_item="minecraft:coal",
            output_item="minecraft:iron_ingot",
            output_count=1,
            output_slot=3,
            poll_interval_s=0.0,
            smelt_timeout_s=1.0,
        )

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "completed")
        self.assertEqual(
            [action.name for action in body.actions],
            ["selectItem", "placeBlock", "furnaceTransfer", "furnaceTransfer", "furnaceTransfer", "mineBlock"],
        )
        self.assertEqual(body.actions[1].params["purpose"], "temporary_furnace")
        self.assertEqual(body.actions[-1].params["context"], "bot_cleanup")
        self.assertEqual(body.block_states[(2, 59, 0)], ("minecraft:air", "CLEAR"))
        self.assertTrue(result.metrics["smelt"]["success"])
        self.assertTrue(result.metrics["reclaim"]["success"])

    def test_smelt_with_temporary_furnace_retries_reclaim_timeout_inside_transaction(self):
        body = FakeFurnaceBody(
            perception("container", [slot(0), slot(1), slot(2)]),
            perception("inventory", [slot(0, "minecraft:furnace", 1), slot(1, "minecraft:iron_ore", 1), slot(2, "minecraft:coal", 1), slot(3)]),
            mutable=True,
            block_states={(2, 59, 0): ("minecraft:air", "CLEAR")},
            auto_smelt_output=("minecraft:iron_ingot", 1),
            auto_smelt_after_reads=6,
        )
        policy = GovernancePolicy(natural_regions=[Region("work", (0, 0, -2), (4, 100, 2))])

        class RetryReclaimWork(BlockWork):
            def __init__(self, body, governance):
                super().__init__(body, governance)
                self.reclaim_calls = 0

            def mine_block(self, pos, *, context=BreakContext.DIRECT, timeout_s=30.0):
                self.reclaim_calls += 1
                if self.reclaim_calls == 1:
                    return ToolResult(False, "timeout", True, metrics={"target": list(pos)})
                return super().mine_block(pos, context=context, timeout_s=timeout_s)

        work = RetryReclaimWork(body, policy)
        runtime = FurnaceTransactions(body, governance=policy, work=work)

        result = runtime.smelt_with_temporary_furnace(
            (2, 59, 0),
            input_item="minecraft:iron_ore",
            input_count=1,
            fuel_item="minecraft:coal",
            output_item="minecraft:iron_ingot",
            output_count=1,
            output_slot=3,
            poll_interval_s=0.0,
            smelt_timeout_s=1.0,
        )

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(result.reason, "completed")
        self.assertEqual(work.reclaim_calls, 2)
        self.assertEqual(result.metrics["reclaim_attempts"][0]["reason"], "timeout")
        self.assertTrue(result.metrics["reclaim"]["success"])
        self.assertEqual(body.block_states[(2, 59, 0)], ("minecraft:air", "CLEAR"))

    def test_smelt_with_temporary_furnace_keeps_success_when_reclaim_times_out(self):
        body = FakeFurnaceBody(
            perception("container", [slot(0), slot(1), slot(2)]),
            perception("inventory", [slot(0, "minecraft:furnace", 1), slot(1, "minecraft:iron_ore", 1), slot(2, "minecraft:coal", 1), slot(3)]),
            mutable=True,
            block_states={(2, 59, 0): ("minecraft:air", "CLEAR")},
            auto_smelt_output=("minecraft:iron_ingot", 1),
            auto_smelt_after_reads=6,
        )
        policy = GovernancePolicy(natural_regions=[Region("work", (0, 0, -2), (4, 100, 2))])

        class TimeoutReclaimWork(BlockWork):
            def mine_block(self, pos, *, context=BreakContext.DIRECT, timeout_s=30.0):
                return ToolResult(False, "timeout", True, metrics={"target": list(pos)})

        runtime = FurnaceTransactions(body, governance=policy, work=TimeoutReclaimWork(body, policy))

        result = runtime.smelt_with_temporary_furnace(
            (2, 59, 0),
            input_item="minecraft:iron_ore",
            input_count=1,
            fuel_item="minecraft:coal",
            output_item="minecraft:iron_ingot",
            output_count=1,
            output_slot=3,
            poll_interval_s=0.0,
            smelt_timeout_s=1.0,
        )

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(result.reason, "completed")
        self.assertFalse(result.metrics["reclaim"]["success"])
        self.assertEqual(result.metrics["reclaim"]["reason"], "timeout")
        self.assertEqual(len(result.metrics["reclaim_attempts"]), 2)

    def test_smelt_with_temporary_furnace_requires_block_work_runtime(self):
        body = FakeFurnaceBody(
            perception("container", [slot(0), slot(1), slot(2)]),
            perception("inventory", [slot(0, "minecraft:furnace", 1), slot(1, "minecraft:iron_ore", 1), slot(2, "minecraft:coal", 1), slot(3)]),
            mutable=True,
            block_states={(2, 59, 0): ("minecraft:air", "CLEAR")},
        )
        runtime = FurnaceTransactions(body)

        result = runtime.smelt_with_temporary_furnace(
            (2, 59, 0),
            input_item="minecraft:iron_ore",
            input_count=1,
            fuel_item="minecraft:coal",
            output_item="minecraft:iron_ingot",
            output_count=1,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "furnace_work_runtime_missing")
        self.assertEqual(body.actions, [])

    def test_smelt_with_nearby_temporary_furnace_selects_supported_clear_site(self):
        block_states = {
            (-1, 59, -1): ("minecraft:air", "CLEAR"),
            (-1, 58, -1): ("minecraft:air", "CLEAR"),
            (0, 59, -1): ("minecraft:air", "CLEAR"),
            (0, 58, -1): ("minecraft:stone", "SOLID"),
            (1, 59, -1): ("minecraft:air", "CLEAR"),
            (1, 58, -1): ("minecraft:air", "CLEAR"),
            (-1, 59, 0): ("minecraft:stone", "SOLID"),
            (-1, 58, 0): ("minecraft:stone", "SOLID"),
            (1, 59, 0): ("minecraft:air", "CLEAR"),
            (1, 58, 0): ("minecraft:air", "CLEAR"),
            (-1, 59, 1): ("minecraft:air", "CLEAR"),
            (-1, 58, 1): ("minecraft:air", "CLEAR"),
            (0, 59, 1): ("minecraft:air", "CLEAR"),
            (0, 58, 1): ("minecraft:air", "CLEAR"),
            (1, 59, 1): ("minecraft:air", "CLEAR"),
            (1, 58, 1): ("minecraft:air", "CLEAR"),
        }
        body = FakeFurnaceBody(
            perception("container", [slot(0), slot(1), slot(2)]),
            perception("inventory", [slot(0, "minecraft:furnace", 1), slot(1, "minecraft:iron_ore", 1), slot(2, "minecraft:coal", 1), slot(3)]),
            mutable=True,
            block_states=block_states,
            auto_smelt_output=("minecraft:iron_ingot", 1),
            auto_smelt_after_reads=6,
        )
        policy = GovernancePolicy(natural_regions=[Region("work", (-2, 0, -2), (2, 100, 2))])
        work = BlockWork(body, policy)
        runtime = FurnaceTransactions(body, governance=policy, work=work)

        result = runtime.smelt_with_nearby_temporary_furnace(
            input_item="minecraft:iron_ore",
            input_count=1,
            fuel_item="minecraft:coal",
            output_item="minecraft:iron_ingot",
            output_count=1,
            output_slot=3,
            radius=1,
            poll_interval_s=0.0,
            smelt_timeout_s=1.0,
        )

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "completed")
        self.assertEqual(result.metrics["temporary_furnace_site"], [0, 59, -1])
        self.assertEqual(body.actions[1].params["target"], [0, 59, -1])
        self.assertEqual(body.actions[1].params["purpose"], "temporary_furnace_auto_site")
        self.assertEqual(body.block_states[(0, 59, -1)], ("minecraft:air", "CLEAR"))
        self.assertEqual(
            [action.name for action in body.actions],
            ["selectItem", "placeBlock", "furnaceTransfer", "furnaceTransfer", "furnaceTransfer", "mineBlock"],
        )

    def test_smelt_with_nearby_temporary_furnace_retries_next_site_after_body_collision(self):
        block_states = {
            (-1, 59, -1): ("minecraft:air", "CLEAR"),
            (-1, 58, -1): ("minecraft:air", "CLEAR"),
            (0, 59, -1): ("minecraft:air", "CLEAR"),
            (0, 58, -1): ("minecraft:stone", "SOLID"),
            (1, 59, -1): ("minecraft:air", "CLEAR"),
            (1, 58, -1): ("minecraft:air", "CLEAR"),
            (-1, 59, 0): ("minecraft:air", "CLEAR"),
            (-1, 58, 0): ("minecraft:stone", "SOLID"),
            (1, 59, 0): ("minecraft:air", "CLEAR"),
            (1, 58, 0): ("minecraft:air", "CLEAR"),
            (-1, 59, 1): ("minecraft:air", "CLEAR"),
            (-1, 58, 1): ("minecraft:air", "CLEAR"),
            (0, 59, 1): ("minecraft:air", "CLEAR"),
            (0, 58, 1): ("minecraft:air", "CLEAR"),
            (1, 59, 1): ("minecraft:air", "CLEAR"),
            (1, 58, 1): ("minecraft:air", "CLEAR"),
        }

        class DriftingFurnaceBody(FakeFurnaceBody):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.state_reads = 0

            def get_state(self) -> BodyState:
                self.state_reads += 1
                if self.state_reads <= 2:
                    return state_at((0.5, 59.0, 0.5))
                return state_at((0.5, 59.0, -0.5))

        body = DriftingFurnaceBody(
            perception("container", [slot(0), slot(1), slot(2)]),
            perception("inventory", [slot(0, "minecraft:furnace", 1), slot(1, "minecraft:iron_ore", 1), slot(2, "minecraft:coal", 1), slot(3)]),
            mutable=True,
            block_states=block_states,
            auto_smelt_output=("minecraft:iron_ingot", 1),
            auto_smelt_after_reads=6,
        )
        policy = GovernancePolicy(natural_regions=[Region("work", (-2, 0, -2), (2, 100, 2))])
        work = BlockWork(body, policy)
        runtime = FurnaceTransactions(body, governance=policy, work=work)

        result = runtime.smelt_with_nearby_temporary_furnace(
            input_item="minecraft:iron_ore",
            input_count=1,
            fuel_item="minecraft:coal",
            output_item="minecraft:iron_ingot",
            output_count=1,
            output_slot=3,
            radius=1,
            poll_interval_s=0.0,
            smelt_timeout_s=1.0,
        )

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(result.reason, "completed")
        self.assertEqual(result.metrics["attempted_sites"][0]["target"], [0, 59, -1])
        self.assertEqual(
            result.metrics["attempted_sites"][0]["result"]["reason"],
            "temporary_furnace_place_failed:place_denied:body_collision",
        )
        self.assertEqual(result.metrics["attempted_sites"][1]["target"], [-1, 59, 0])
        self.assertEqual(result.metrics["temporary_furnace_site"], [-1, 59, 0])
        place_actions = [action for action in body.actions if action.name == "placeBlock"]
        self.assertEqual(len(place_actions), 1)
        self.assertEqual(place_actions[0].params["target"], [-1, 59, 0])

    def test_smelt_with_nearby_temporary_furnace_retries_next_site_after_place_timeout(self):
        block_states = {
            (-1, 59, -1): ("minecraft:air", "CLEAR"),
            (-1, 58, -1): ("minecraft:air", "CLEAR"),
            (0, 59, -1): ("minecraft:air", "CLEAR"),
            (0, 58, -1): ("minecraft:stone", "SOLID"),
            (1, 59, -1): ("minecraft:air", "CLEAR"),
            (1, 58, -1): ("minecraft:air", "CLEAR"),
            (-1, 59, 0): ("minecraft:air", "CLEAR"),
            (-1, 58, 0): ("minecraft:stone", "SOLID"),
            (1, 59, 0): ("minecraft:air", "CLEAR"),
            (1, 58, 0): ("minecraft:air", "CLEAR"),
            (-1, 59, 1): ("minecraft:air", "CLEAR"),
            (-1, 58, 1): ("minecraft:air", "CLEAR"),
            (0, 59, 1): ("minecraft:air", "CLEAR"),
            (0, 58, 1): ("minecraft:air", "CLEAR"),
            (1, 59, 1): ("minecraft:air", "CLEAR"),
            (1, 58, 1): ("minecraft:air", "CLEAR"),
        }
        body = FakeFurnaceBody(
            perception("container", [slot(0), slot(1), slot(2)]),
            perception("inventory", [slot(0, "minecraft:furnace", 1), slot(1, "minecraft:iron_ore", 1), slot(2, "minecraft:coal", 1), slot(3)]),
            mutable=True,
            block_states=block_states,
            auto_smelt_output=("minecraft:iron_ingot", 1),
            auto_smelt_after_reads=6,
        )
        policy = GovernancePolicy(natural_regions=[Region("work", (-2, 0, -2), (2, 100, 2))])

        class TimeoutFirstPlaceWork(BlockWork):
            def __init__(self, body, governance):
                super().__init__(body, governance)
                self.place_calls = 0

            def place_block(
                self,
                pos,
                block_type,
                *,
                face=None,
                context="work",
                purpose="scaffold",
                allow_replace_liquid=False,
                timeout_s=30.0,
            ):
                self.place_calls += 1
                if self.place_calls == 1:
                    return ToolResult(False, "timeout", True, metrics={"target": list(pos)})
                return super().place_block(
                    pos,
                    block_type,
                    face=face,
                    context=context,
                    purpose=purpose,
                    allow_replace_liquid=allow_replace_liquid,
                    timeout_s=timeout_s,
                )

        work = TimeoutFirstPlaceWork(body, policy)
        runtime = FurnaceTransactions(body, governance=policy, work=work)

        result = runtime.smelt_with_nearby_temporary_furnace(
            input_item="minecraft:iron_ore",
            input_count=1,
            fuel_item="minecraft:coal",
            output_item="minecraft:iron_ingot",
            output_count=1,
            output_slot=3,
            radius=1,
            poll_interval_s=0.0,
            smelt_timeout_s=1.0,
        )

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(result.reason, "completed")
        self.assertEqual(result.metrics["attempted_sites"][0]["target"], [0, 59, -1])
        self.assertEqual(result.metrics["attempted_sites"][0]["result"]["reason"], "temporary_furnace_place_failed:timeout")
        self.assertEqual(result.metrics["attempted_sites"][1]["target"], [-1, 59, 0])
        self.assertEqual(result.metrics["temporary_furnace_site"], [-1, 59, 0])
        self.assertEqual(work.place_calls, 2)

    def test_smelt_with_nearby_temporary_furnace_no_supported_site_is_no_action(self):
        block_states = {
            (-1, 59, -1): ("minecraft:air", "CLEAR"),
            (-1, 58, -1): ("minecraft:air", "CLEAR"),
            (0, 59, -1): ("minecraft:air", "CLEAR"),
            (0, 58, -1): ("minecraft:air", "CLEAR"),
            (1, 59, -1): ("minecraft:air", "CLEAR"),
            (1, 58, -1): ("minecraft:air", "CLEAR"),
            (-1, 59, 0): ("minecraft:stone", "SOLID"),
            (-1, 58, 0): ("minecraft:stone", "SOLID"),
            (1, 59, 0): ("minecraft:air", "CLEAR"),
            (1, 58, 0): ("minecraft:air", "CLEAR"),
            (-1, 59, 1): ("minecraft:air", "CLEAR"),
            (-1, 58, 1): ("minecraft:air", "CLEAR"),
            (0, 59, 1): ("minecraft:air", "CLEAR"),
            (0, 58, 1): ("minecraft:air", "CLEAR"),
            (1, 59, 1): ("minecraft:air", "CLEAR"),
            (1, 58, 1): ("minecraft:air", "CLEAR"),
        }
        body = FakeFurnaceBody(
            perception("container", [slot(0), slot(1), slot(2)]),
            perception("inventory", [slot(0, "minecraft:furnace", 1), slot(1, "minecraft:iron_ore", 1), slot(2, "minecraft:coal", 1), slot(3)]),
            mutable=True,
            block_states=block_states,
        )
        policy = GovernancePolicy(natural_regions=[Region("work", (-2, 0, -2), (2, 100, 2))])
        work = BlockWork(body, policy)
        runtime = FurnaceTransactions(body, governance=policy, work=work)

        result = runtime.smelt_with_nearby_temporary_furnace(
            input_item="minecraft:iron_ore",
            input_count=1,
            fuel_item="minecraft:coal",
            output_item="minecraft:iron_ingot",
            output_count=1,
            output_slot=3,
            radius=1,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "temporary_furnace_no_supported_site")
        self.assertEqual(body.actions, [])
        self.assertFalse(any(candidate["candidate"] for candidate in result.metrics["candidates"]))

    def test_transfer_slot_furnace_to_bot_verifies_authoritative_deltas(self):
        body = FakeFurnaceBody(
            perception("container", [slot(0), slot(1), slot(2, "minecraft:iron_ingot", 2)]),
            perception("inventory", [slot(0), slot(1)]),
            mutable=True,
            terminal_count=1,
        )
        runtime = FurnaceTransactions(body)

        result = runtime.transfer_slot((1, 59, 0), direction="furnace_to_bot", furnace_slot="output", bot_slot=1)

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "completed")
        self.assertEqual(result.metrics["furnace_before"]["count"], 2)
        self.assertEqual(result.metrics["furnace_after"]["count"], 1)
        self.assertEqual(result.metrics["bot_before"]["count"], 0)
        self.assertEqual(result.metrics["bot_after"]["count"], 1)
        self.assertEqual(result.metrics["bot_after"]["item"], "minecraft:iron_ingot")

    def test_transfer_slot_count_moves_partial_stack_into_matching_destination(self):
        body = FakeFurnaceBody(
            perception("container", [slot(0), slot(1), slot(2, "minecraft:iron_ingot", 5)]),
            perception("inventory", [slot(0, "minecraft:iron_ingot", 60), slot(1)]),
            mutable=True,
            applied_count=64,
        )
        runtime = FurnaceTransactions(body)

        result = runtime.transfer_slot((1, 59, 0), direction="furnace_to_bot", furnace_slot="output", bot_slot=0, count=2)

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "completed")
        self.assertEqual(result.metrics["count"], 2)
        self.assertEqual(result.metrics["furnace_after"]["count"], 3)
        self.assertEqual(result.metrics["bot_after"]["count"], 62)

    def test_transfer_slot_bot_to_furnace_verifies_authoritative_deltas(self):
        body = FakeFurnaceBody(
            perception("container", [slot(0), slot(1, "minecraft:coal", 1), slot(2)]),
            perception("inventory", [slot(0, "minecraft:coal", 3), slot(1)]),
            mutable=True,
            terminal_count=1,
        )
        runtime = FurnaceTransactions(body)

        result = runtime.transfer_slot((1, 59, 0), direction="bot_to_furnace", furnace_slot="fuel", bot_slot=0)

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "completed")
        self.assertEqual(result.metrics["bot_before"]["count"], 3)
        self.assertEqual(result.metrics["bot_after"]["count"], 2)
        self.assertEqual(result.metrics["furnace_before"]["count"], 1)
        self.assertEqual(result.metrics["furnace_after"]["count"], 2)

    def test_transfer_slot_accepts_fuel_consumed_before_authoritative_reread(self):
        body = FakeFurnaceBody(
            perception("container", [slot(0), slot(1), slot(2)]),
            perception("inventory", [slot(0, "minecraft:coal", 1)]),
            mutable=True,
        )
        original_apply = body._apply_furnace_transfer

        def apply_and_consume(action):
            original_apply(action)
            if action.params["direction"] == "bot_to_furnace" and action.params["furnace_slot"] == "fuel":
                body.furnace_slots[1] = slot(1)

        body._apply_furnace_transfer = apply_and_consume
        runtime = FurnaceTransactions(body)

        result = runtime.transfer_slot((1, 59, 0), direction="bot_to_furnace", furnace_slot="fuel", bot_slot=0, count=1)

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "completed")
        self.assertEqual(result.metrics["bot_after"]["count"], 0)
        self.assertEqual(result.metrics["furnace_after"]["count"], 0)

    def test_transfer_slot_rejects_invalid_direction(self):
        body = FakeFurnaceBody(
            perception("container", [slot(0), slot(1), slot(2)]),
            perception("inventory", [slot(0)]),
        )
        runtime = FurnaceTransactions(body)

        result = runtime.transfer_slot((1, 59, 0), direction="sideways")

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "invalid_direction")
        self.assertEqual(body.actions, [])

    def test_transfer_slot_rejects_invalid_furnace_slot(self):
        body = FakeFurnaceBody(
            perception("container", [slot(0), slot(1), slot(2)]),
            perception("inventory", [slot(0)]),
        )
        runtime = FurnaceTransactions(body)

        result = runtime.transfer_slot((1, 59, 0), direction="furnace_to_bot", furnace_slot="trash")

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "invalid_furnace_slot")
        self.assertEqual(body.actions, [])

    def test_transfer_slot_rejects_invalid_bot_slot(self):
        body = FakeFurnaceBody(
            perception("container", [slot(0), slot(1), slot(2)]),
            perception("inventory", [slot(0)]),
        )
        runtime = FurnaceTransactions(body)

        result = runtime.transfer_slot((1, 59, 0), direction="furnace_to_bot", bot_slot=99)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "invalid_bot_slot")
        self.assertEqual(body.actions, [])

    def test_transfer_slot_reports_unverified_when_terminal_count_disagrees_with_truth(self):
        body = FakeFurnaceBody(
            perception("container", [slot(0), slot(1), slot(2, "minecraft:iron_ingot", 2)]),
            perception("inventory", [slot(0)]),
            terminal_count=2,
            applied_count=1,
            mutable=True,
        )
        runtime = FurnaceTransactions(body)

        result = runtime.transfer_slot((1, 59, 0), direction="furnace_to_bot", furnace_slot="output", bot_slot=0)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "furnace_transfer_unverified")

    def test_transfer_slot_respects_governance_protection(self):
        body = FakeFurnaceBody(
            perception("container", [slot(0), slot(1), slot(2, "minecraft:iron_ingot", 1)]),
            perception("inventory", [slot(0)]),
        )
        runtime = FurnaceTransactions(body, governance=GovernancePolicy())

        result = runtime.transfer_slot((1, 59, 0), direction="furnace_to_bot", furnace_slot="output", bot_slot=0)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "furnace_denied")
        self.assertEqual(body.actions, [])

    def test_transfer_slot_rejects_wrong_block_type(self):
        body = FakeFurnaceBody(
            perception("container", [slot(0), slot(1), slot(2, "minecraft:iron_ingot", 1)]),
            perception("inventory", [slot(0)]),
            block_states={(1, 59, 0): ("minecraft:barrel", "SOLID")},
        )
        runtime = FurnaceTransactions(body)

        result = runtime.transfer_slot((1, 59, 0), direction="furnace_to_bot", furnace_slot="output", bot_slot=0)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "furnace_wrong_type")
        self.assertEqual(body.actions, [])


if __name__ == "__main__":
    unittest.main()
