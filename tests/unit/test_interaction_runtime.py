import unittest
from unittest.mock import patch

from minebot.body import InteractionTransactions
from minebot.body.interaction import _openable_stand_points
from minebot.body.interaction_support import interaction_stand_points
from minebot.body.interaction_support import ensure_interaction_range
from minebot.body.use import UseTransactions
from minebot.contract import Action, BodyState, Event, PerceptionResult, Result, ToolResult
from minebot.game.governance import GovernancePolicy, Region
from minebot.game.navigation import GoalComposite
from tests.unit._body_batch_helper import batch_block_cells_from_blockat


def state_at(pos, *, sleeping=None, time=0):
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
        time=time,
        weather=None,
        dimension="overworld",
        complete=True,
        sleeping=sleeping,
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


class FakeInventoryTransaction:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def discard_item(self, *, item, count, timeout_s=2.0):
        self.calls.append({"item": item, "count": count, "timeout_s": timeout_s})
        return self.result


class FakeUseTransaction:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def use_on_block(self, **kwargs):
        self.calls.append(dict(kwargs))
        return self.result

    def _prepare_use_item(self, item, timeout_s=8.0):
        self.calls.append({"phase": "prepare", "item": item, "timeout_s": timeout_s})
        return ToolResult(success=True, reason="completed", can_retry=False, metrics={"slot": 0, "item": item})

    def _sow_crop_on_farmland(self, **kwargs):
        self.calls.append({"phase": "sow", **kwargs})
        return self.result


class FakeWorkTransaction:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def mine_block_collect(self, pos, **kwargs):
        self.calls.append({"pos": pos, **kwargs})
        return self.result


class FakeInteractionBody:
    bot_name = "Bot1"

    def __init__(self, *, entities, block_states, states, events=None, accepted=True, look_success=True):
        self.entities = list(entities)
        self.block_states = dict(block_states)
        self.states = list(states)
        self.accepted = accepted
        self.look_success = look_success
        self.events = list(events or [])
        self.actions: list[Action] = []
        self.perceptions: list[tuple[str, dict[str, object]]] = []
        self.poll_calls = 0

    def get_state(self):
        if len(self.states) == 1:
            return self.states[0]
        return self.states.pop(0)

    def perceive(self, scope: str, params: dict[str, object]) -> PerceptionResult:
        self.perceptions.append((scope, params))
        if scope == "findBlocks":
            blocks = []
            for (x, y, z), block_data in self.block_states.items():
                block_type, state, _properties = _normalize_block_state(block_data)
                if state != "CLEAR":
                    blocks.append({"x": x, "y": y, "z": z, "type": block_type})
            return PerceptionResult(
                bot="Bot1",
                scope="findBlocks",
                type="perception",
                ok=True,
                complete=True,
                data={"blocks": blocks},
                uncertainty=[],
                next=None,
                error=None,
            )
        if scope == "nearbyEntities":
            return PerceptionResult(
                bot="Bot1",
                scope="nearbyEntities",
                type="perception",
                ok=True,
                complete=True,
                data={"entities": list(self.entities)},
                uncertainty=[],
                next=None,
                error=None,
            )
        if scope == "blockCells":
            return batch_block_cells_from_blockat(self, params)
        if scope == "blockAt":
            pos = (int(params["x"]), int(params["y"]), int(params["z"]))
            default_block = ("minecraft:stone", "SOLID") if pos[1] == 63 else ("minecraft:air", "CLEAR")
            block_type, state, properties = _normalize_block_state(self.block_states.get(pos, default_block))
            return PerceptionResult(
                bot="Bot1",
                scope="blockAt",
                type="perception",
                ok=True,
                complete=True,
                data={"x": pos[0], "y": pos[1], "z": pos[2], "type": block_type, "state": state, "properties": properties},
                uncertainty=[],
                next=None,
                error=None,
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
        if action.name == "lookAt":
            return Event(
                seq=1,
                tick=20,
                bot="Bot1",
                name="lookDone",
                data={
                    "action_id": action_id,
                    "success": self.look_success,
                    "stopped_reason": "completed" if self.look_success else "blocked",
                },
            )
        if action.name == "moveTo":
            return Event(
                seq=2,
                tick=25,
                bot="Bot1",
                name="moveDone",
                data={
                    "action_id": action_id,
                    "arrived": True,
                    "success": True,
                    "stopped_reason": "arrived",
                    "dist_to_target": 0.25,
                },
            )
        if action.name == "stop":
            return Event(
                seq=2,
                tick=25,
                bot="Bot1",
                name="moveDone",
                data={
                    "action_id": action_id,
                    "arrived": True,
                    "success": True,
                    "stopped_reason": "completed",
                    "dist_to_target": 0.0,
                },
            )
        if action.name == "useItem":
            return Event(
                seq=3,
                tick=30,
                bot="Bot1",
                name="useDone",
                data={
                    "action_id": action_id,
                    "success": True,
                    "stopped_reason": "completed",
                },
            )
        if action.name == "handoffItem":
            return Event(
                seq=4,
                tick=35,
                bot="Bot1",
                name="handoffDone",
                data={
                    "action_id": action_id,
                    "success": True,
                    "stopped_reason": "spawned_item",
                    "receiver": action.params.get("receiver"),
                    "item": action.params.get("item"),
                    "requested_count": action.params.get("count"),
                    "spawned_count": action.params.get("count"),
                    "source_slot": 0,
                },
            )
        return Event(
            seq=99,
            tick=99,
            bot="Bot1",
            name="unexpected",
            data={"action_id": action_id, "success": False, "stopped_reason": "unexpected_action"},
        )

    def poll_events(self):
        self.poll_calls += 1
        if not self.events:
            return []
        return self.events.pop(0)


class SequencedEntityBody(FakeInteractionBody):
    def __init__(self, *, entity_batches, **kwargs):
        super().__init__(entities=entity_batches[0] if entity_batches else [], **kwargs)
        self.entity_batches = [list(batch) for batch in entity_batches]

    def perceive(self, scope: str, params: dict[str, object]) -> PerceptionResult:
        if scope == "nearbyEntities":
            batch = self.entity_batches[0] if len(self.entity_batches) == 1 else self.entity_batches.pop(0)
            self.perceptions.append((scope, params))
            return PerceptionResult(
                bot="Bot1",
                scope="nearbyEntities",
                type="perception",
                ok=True,
                complete=True,
                data={"entities": list(batch)},
                uncertainty=[],
                next=None,
                error=None,
            )
        return super().perceive(scope, params)


def player_entity(name="Receiver", pos=(3.0, 64.0, 0.0), *, dist2=9.0, entity_id="receiver-1"):
    return {"id": entity_id, "type": "minecraft:player", "name": name, "pos": list(pos), "health": 20.0, "dist2": dist2}


def _normalize_block_state(raw):
    if len(raw) == 2:
        block_type, state = raw
        return block_type, state, {}
    block_type, state, properties = raw
    return block_type, state, dict(properties)


class InteractionRuntimeTests(unittest.TestCase):
    def test_open_door_prefers_opposite_side_stand_before_closed_side(self):
        stands = _openable_stand_points(
            (2, 64, 0),
            "minecraft:oak_door",
            {"open": "true", "facing": "east", "hinge": "left"},
        )

        self.assertEqual(stands[0], (3, 64, 0))
        self.assertEqual(stands[-1], (1, 64, 0))

    def test_search_for_entity_requires_filter(self):
        body = FakeInteractionBody(entities=[], block_states={}, states=[state_at((0, 64, 0))])
        runtime = InteractionTransactions(body)

        result = runtime.search_for_entity()

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "search_entity_filter_missing")

    def test_search_for_entity_reports_not_found(self):
        body = FakeInteractionBody(entities=[], block_states={}, states=[state_at((0, 64, 0))])
        runtime = InteractionTransactions(body)

        result = runtime.search_for_entity(entity_types=("villager",), search_radius=12)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "search_entity_not_found")

    def test_search_for_entity_requires_navigation_when_target_out_of_range(self):
        body = FakeInteractionBody(
            entities=[{"type": "minecraft:villager", "name": None, "pos": [8.0, 64.0, 0.0], "health": 20.0, "dist2": 64.0}],
            block_states={
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
            },
            states=[state_at((0, 64, 0)), state_at((0, 64, 0))],
        )
        runtime = InteractionTransactions(body)

        result = runtime.search_for_entity(entity_types=("villager",), search_radius=12)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "search_entity_navigation_missing")

    def test_search_for_entity_reaches_target_after_navigation(self):
        body = FakeInteractionBody(
            entities=[
                {"type": "minecraft:villager", "name": None, "pos": [8.0, 64.0, 0.0], "health": 20.0, "dist2": 64.0},
                {"type": "minecraft:villager", "name": None, "pos": [8.0, 64.0, 0.0], "health": 20.0, "dist2": 9.0},
            ],
            block_states={
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
            },
            states=[state_at((0, 64, 0)), state_at((0, 64, 0)), state_at((5, 64, 0)), state_at((5, 64, 0))],
        )
        navigator = FakeInteractionNavigator([ToolResult(success=True, reason="arrived", can_retry=False)])
        runtime = InteractionTransactions(body, navigator=navigator)

        result = runtime.search_for_entity(entity_types=("villager",), search_radius=12)

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "entity_in_range")
        self.assertEqual(result.metrics["target"]["type"], "villager")
        self.assertLessEqual(result.metrics["final_distance"], 4.5)

    def test_search_for_entity_reports_target_lost_after_navigation(self):
        body = SequencedEntityBody(
            entity_batches=[
                [{"type": "minecraft:villager", "name": None, "pos": [8.0, 64.0, 0.0], "health": 20.0, "dist2": 64.0}],
                [],
            ],
            block_states={
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
            },
            states=[state_at((0, 64, 0)), state_at((0, 64, 0)), state_at((5, 64, 0))],
        )
        navigator = FakeInteractionNavigator([ToolResult(success=True, reason="arrived", can_retry=False)])
        runtime = InteractionTransactions(body, navigator=navigator)

        result = runtime.search_for_entity(entity_types=("villager",), search_radius=12)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "search_entity_target_lost")

    def test_search_for_entity_refuses_different_entity_id_after_navigation(self):
        body = SequencedEntityBody(
            entity_batches=[
                [{"id": "villager-a", "type": "minecraft:villager", "name": None, "pos": [8.0, 64.0, 0.0], "health": 20.0, "dist2": 64.0}],
                [{"id": "villager-b", "type": "minecraft:villager", "name": None, "pos": [8.0, 64.0, 0.0], "health": 20.0, "dist2": 9.0}],
            ],
            block_states={
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
            },
            states=[state_at((0, 64, 0)), state_at((0, 64, 0)), state_at((5, 64, 0))],
        )
        navigator = FakeInteractionNavigator([ToolResult(success=True, reason="arrived", can_retry=False)])
        runtime = InteractionTransactions(body, navigator=navigator)

        result = runtime.search_for_entity(entity_types=("villager",), search_radius=12)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "search_entity_target_lost")
        self.assertEqual(result.metrics["original_entity_id"], "villager-a")
        self.assertEqual(result.metrics["refreshed_entity_id"], "villager-b")

    def test_go_to_player_reports_target_not_found(self):
        body = FakeInteractionBody(entities=[], block_states={}, states=[state_at((0, 64, 0))])
        runtime = InteractionTransactions(body)

        result = runtime.go_to_player(player_name="Receiver")

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "goto_player_target_not_found")

    def test_go_to_player_requires_navigation_when_target_out_of_range(self):
        body = FakeInteractionBody(
            entities=[player_entity(pos=(8.0, 64.0, 0.0), dist2=64.0)],
            block_states={
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
            },
            states=[state_at((0, 64, 0)), state_at((0, 64, 0))],
        )
        runtime = InteractionTransactions(body)

        result = runtime.go_to_player(player_name="Receiver")

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "goto_player_navigation_missing")

    def test_go_to_player_reaches_player_after_navigation(self):
        body = FakeInteractionBody(
            entities=[player_entity(pos=(8.0, 64.0, 0.0), dist2=64.0), player_entity(pos=(8.0, 64.0, 0.0), dist2=9.0)],
            block_states={
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
            },
            states=[state_at((0, 64, 0)), state_at((0, 64, 0)), state_at((5, 64, 0)), state_at((5, 64, 0))],
        )
        navigator = FakeInteractionNavigator([ToolResult(success=True, reason="arrived", can_retry=False)])
        runtime = InteractionTransactions(body, navigator=navigator)

        result = runtime.go_to_player(player_name="Receiver")

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "player_reached")
        self.assertEqual(len(navigator.calls), 1)
        self.assertEqual(navigator.calls[0][1]["arrival_radius"], 0.25)
        self.assertGreaterEqual(result.metrics["final_distance"], 1.0)
        self.assertLessEqual(result.metrics["final_distance"], 4.5)

    def test_go_to_player_reports_target_lost_after_navigation(self):
        body = SequencedEntityBody(
            entity_batches=[
                [player_entity(pos=(8.0, 64.0, 0.0), dist2=64.0)],
                [],
            ],
            block_states={
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
            },
            states=[state_at((0, 64, 0)), state_at((0, 64, 0)), state_at((5, 64, 0))],
        )
        navigator = FakeInteractionNavigator([ToolResult(success=True, reason="arrived", can_retry=False)])
        runtime = InteractionTransactions(body, navigator=navigator)

        result = runtime.go_to_player(player_name="Receiver")

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "goto_player_target_lost")

    def test_follow_player_reports_target_not_found(self):
        body = FakeInteractionBody(entities=[], block_states={}, states=[state_at((0, 64, 0))])
        runtime = InteractionTransactions(body)

        result = runtime.follow_player(player_name="Receiver")

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "follow_target_not_found")

    def test_follow_player_requires_navigation_when_target_out_of_band(self):
        body = FakeInteractionBody(
            entities=[player_entity(pos=(8.0, 64.0, 0.0), dist2=64.0)],
            block_states={
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
            },
            states=[state_at((0, 64, 0)), state_at((0, 64, 0))],
        )
        runtime = InteractionTransactions(body)

        result = runtime.follow_player(player_name="Receiver")

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "follow_navigation_missing")

    def test_follow_player_reaches_distance_band_after_navigation(self):
        body = FakeInteractionBody(
            entities=[player_entity(pos=(8.0, 64.0, 0.0), dist2=64.0), player_entity(pos=(8.0, 64.0, 0.0), dist2=9.0)],
            block_states={
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
            },
            states=[state_at((0, 64, 0)), state_at((0, 64, 0)), state_at((5, 64, 0)), state_at((5, 64, 0))],
        )
        navigator = FakeInteractionNavigator([ToolResult(success=True, reason="arrived", can_retry=False)])
        runtime = InteractionTransactions(body, navigator=navigator)

        result = runtime.follow_player(player_name="Receiver", min_distance=2.0, max_distance=4.5)

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "distance_band_reached")
        self.assertEqual(len(navigator.calls), 1)
        self.assertGreaterEqual(result.metrics["final_distance"], 2.0)
        self.assertLessEqual(result.metrics["final_distance"], 4.5)

    def test_follow_player_reports_target_lost_after_navigation(self):
        body = SequencedEntityBody(
            entity_batches=[
                [player_entity(pos=(8.0, 64.0, 0.0), dist2=64.0)],
                [],
            ],
            block_states={
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
            },
            states=[state_at((0, 64, 0)), state_at((0, 64, 0)), state_at((5, 64, 0))],
        )
        navigator = FakeInteractionNavigator([ToolResult(success=True, reason="arrived", can_retry=False)])
        runtime = InteractionTransactions(body, navigator=navigator)

        result = runtime.follow_player(player_name="Receiver")

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "follow_target_lost")

    def test_follow_player_maintains_band_after_target_moves(self):
        block_states = {}
        for target_x in (8, 11):
            for pos in (
                (target_x + 1, 64, 0),
                (target_x - 1, 64, 0),
                (target_x, 64, 1),
                (target_x, 64, -1),
            ):
                block_states[pos] = ("minecraft:air", "CLEAR")
                block_states[(pos[0], pos[1] + 1, pos[2])] = ("minecraft:air", "CLEAR")
                block_states[(pos[0], pos[1] - 1, pos[2])] = ("minecraft:stone", "SOLID")
        body = SequencedEntityBody(
            entity_batches=[
                [player_entity(pos=(8.0, 64.0, 0.0), dist2=64.0)],
                [player_entity(pos=(8.0, 64.0, 0.0), dist2=9.0)],
                [player_entity(pos=(11.0, 64.0, 0.0), dist2=36.0)],
                [player_entity(pos=(11.0, 64.0, 0.0), dist2=9.0)],
            ],
            block_states=block_states,
            states=[
                state_at((0, 64, 0)),
                state_at((0, 64, 0)),
                state_at((0, 64, 0)),
                state_at((5, 64, 0)),
                state_at((5, 64, 0)),
                state_at((5, 64, 0)),
                state_at((5, 64, 0)),
                state_at((5, 64, 0)),
                state_at((5, 64, 0)),
                state_at((8, 64, 0)),
                state_at((8, 64, 0)),
                state_at((8, 64, 0)),
            ],
        )
        navigator = FakeInteractionNavigator(
            [
                ToolResult(success=True, reason="arrived", can_retry=False),
                ToolResult(success=True, reason="arrived", can_retry=False),
            ]
        )
        runtime = InteractionTransactions(body, navigator=navigator)

        result = runtime.follow_player(
            player_name="Receiver",
            min_distance=2.0,
            max_distance=4.5,
            maintenance_checks=2,
        )

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "distance_band_reached")
        self.assertEqual(len(navigator.calls), 2)
        self.assertEqual(len(result.metrics["maintenance_attempts"]), 2)

    def test_follow_player_reports_target_lost_during_maintenance(self):
        body = SequencedEntityBody(
            entity_batches=[
                [player_entity(pos=(8.0, 64.0, 0.0), dist2=64.0)],
                [player_entity(pos=(8.0, 64.0, 0.0), dist2=9.0)],
                [],
            ],
            block_states={
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
            },
            states=[
                state_at((0, 64, 0)),
                state_at((0, 64, 0)),
                state_at((0, 64, 0)),
                state_at((5, 64, 0)),
                state_at((5, 64, 0)),
                state_at((5, 64, 0)),
            ],
        )
        navigator = FakeInteractionNavigator([ToolResult(success=True, reason="arrived", can_retry=False)])
        runtime = InteractionTransactions(body, navigator=navigator)

        result = runtime.follow_player(player_name="Receiver", maintenance_checks=2)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "follow_target_lost")
        self.assertEqual(len(navigator.calls), 1)

    def test_go_to_bed_reports_not_found(self):
        body = FakeInteractionBody(entities=[], block_states={}, states=[state_at((0, 64, 0), sleeping=False)])
        runtime = InteractionTransactions(body)

        result = runtime.go_to_bed(search_radius=8)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "bed_not_found")

    def test_go_to_bed_requires_navigation_when_bed_out_of_range(self):
        body = FakeInteractionBody(
            entities=[],
            block_states={
                (8, 64, 0): ("minecraft:red_bed", "SOLID"),
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
            },
            states=[state_at((0, 64, 0), sleeping=False), state_at((0, 64, 0), sleeping=False)],
        )
        runtime = InteractionTransactions(body)

        result = runtime.go_to_bed(search_radius=12)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "bed_navigation_missing")

    def test_go_to_bed_succeeds_when_sleeping_after_use(self):
        body = FakeInteractionBody(
            entities=[],
            block_states={
                (8, 64, 0): ("minecraft:red_bed", "SOLID"),
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
            },
            states=[
                state_at((0, 64, 0), sleeping=False),
                state_at((0, 64, 0), sleeping=False),
                state_at((5, 64, 0), sleeping=False),
                state_at((5, 64, 0), sleeping=True),
            ],
        )
        navigator = FakeInteractionNavigator([ToolResult(success=True, reason="arrived", can_retry=False)])
        runtime = InteractionTransactions(body, navigator=navigator)

        result = runtime.go_to_bed(search_radius=12)

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "sleeping")
        self.assertEqual([action.name for action in body.actions], ["moveTo", "lookAt", "useItem"])

    def test_go_to_bed_reports_not_entered_when_use_succeeds_without_sleeping(self):
        body = FakeInteractionBody(
            entities=[],
            block_states={
                (2, 64, 0): ("minecraft:red_bed", "SOLID"),
            },
            states=[
                state_at((0, 64, 0), sleeping=False, time=18000),
                state_at((0, 64, 0), sleeping=False, time=18000),
            ],
        )
        runtime = InteractionTransactions(body)

        result = runtime.go_to_bed(search_radius=8)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "bed_not_entered")

    def test_go_to_bed_reports_not_night_when_use_succeeds_outside_sleep_window(self):
        body = FakeInteractionBody(
            entities=[],
            block_states={
                (2, 64, 0): ("minecraft:red_bed", "SOLID"),
            },
            states=[
                state_at((0, 64, 0), sleeping=False, time=6000),
                state_at((0, 64, 0), sleeping=False, time=6000),
            ],
        )
        runtime = InteractionTransactions(body)

        result = runtime.go_to_bed(search_radius=8)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "bed_not_night")

    def test_go_to_bed_reports_occupied_when_bed_stays_occupied_at_night(self):
        body = FakeInteractionBody(
            entities=[],
            block_states={
                (2, 64, 0): ("minecraft:red_bed", "SOLID", {"occupied": "true"}),
            },
            states=[
                state_at((0, 64, 0), sleeping=False, time=18000),
                state_at((0, 64, 0), sleeping=False, time=18000),
            ],
        )
        runtime = InteractionTransactions(body)

        result = runtime.go_to_bed(search_radius=8)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "bed_occupied")
        self.assertEqual(result.metrics["target_after"]["properties"]["occupied"], "true")

    def test_go_to_bed_accepts_base_bed_type_from_find_blocks(self):
        body = FakeInteractionBody(
            entities=[],
            block_states={
                (2, 64, 0): ("minecraft:bed", "SOLID", {"occupied": "false"}),
            },
            states=[
                state_at((0, 64, 0), sleeping=False, time=18000),
                state_at((0, 64, 0), sleeping=False, time=18000),
            ],
        )
        runtime = InteractionTransactions(body)

        result = runtime.go_to_bed(search_radius=8)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "bed_not_entered")

    def test_go_to_bed_queries_bed_variants_separately(self):
        body = FakeInteractionBody(
            entities=[],
            block_states={
                (2, 64, 0): ("minecraft:red_bed", "SOLID", {"occupied": "false"}),
            },
            states=[
                state_at((0, 64, 0), sleeping=False, time=18000),
                state_at((0, 64, 0), sleeping=False, time=18000),
            ],
        )
        runtime = InteractionTransactions(body)

        result = runtime.go_to_bed(search_radius=8)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "bed_not_entered")
        find_blocks_calls = [params for scope, params in body.perceptions if scope == "findBlocks"]
        self.assertGreaterEqual(len(find_blocks_calls), 2)
        self.assertTrue(all(isinstance(call["type"], str) for call in find_blocks_calls))

    def test_go_to_bed_looks_at_bed_head_when_target_is_foot(self):
        body = FakeInteractionBody(
            entities=[],
            block_states={
                (2, 64, 0): ("minecraft:red_bed", "SOLID", {"occupied": "false", "part": "foot", "facing": "east"}),
                (3, 64, 0): ("minecraft:red_bed", "SOLID", {"occupied": "false", "part": "head", "facing": "east"}),
            },
            states=[
                state_at((0, 64, 0), sleeping=False, time=18000),
                state_at((0, 64, 0), sleeping=False, time=18000),
            ],
        )
        runtime = InteractionTransactions(body)

        result = runtime.go_to_bed(search_radius=8)

        self.assertFalse(result.success)
        look_action = next(action for action in body.actions if action.name == "lookAt")
        self.assertEqual(look_action.params["target"], [3.5, 64.5, 0.5])
        self.assertEqual(result.metrics["interaction_target"], [3, 64, 0])

    def test_open_openable_reports_not_found(self):
        body = FakeInteractionBody(entities=[], block_states={}, states=[state_at((0, 64, 0))])
        runtime = InteractionTransactions(body)

        result = runtime.open_openable(search_radius=8)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "openable_not_found")

    def test_open_openable_rejects_redstone_only_openables(self):
        body = FakeInteractionBody(
            entities=[],
            block_states={(2, 64, 0): ("minecraft:iron_door", "SOLID")},
            states=[state_at((0, 64, 0))],
        )
        runtime = InteractionTransactions(body)

        result = runtime.open_openable(pos=(2, 64, 0))

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "openable_requires_redstone")

    def test_open_openable_reports_already_open(self):
        body = FakeInteractionBody(
            entities=[],
            block_states={
                (2, 64, 0): (
                    "minecraft:oak_door",
                    "SOLID",
                    {"open": "true", "facing": "east", "hinge": "left"},
                )
            },
            states=[state_at((0, 64, 0))],
        )
        use = FakeUseTransaction(
            ToolResult(
                success=True,
                reason="already_in_expected_state",
                can_retry=False,
                metrics={"target_after": {"properties": {"open": "true"}}},
            )
        )
        runtime = InteractionTransactions(body, use=use)

        result = runtime.open_openable(pos=(2, 64, 0))

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "already_open")
        self.assertEqual(use.calls, [])

    def test_open_openable_uses_empty_hand_and_reports_opened(self):
        body = FakeInteractionBody(
            entities=[],
            block_states={(2, 64, 0): ("minecraft:oak_door", "SOLID")},
            states=[state_at((0, 64, 0))],
        )
        use = FakeUseTransaction(
            ToolResult(
                success=True,
                reason="completed",
                can_retry=False,
                metrics={
                    "target_before": {"properties": {"open": "false"}},
                    "target_after": {"properties": {"open": "true"}},
                    "empty_hand": True,
                },
            )
        )
        runtime = InteractionTransactions(body, use=use)

        result = runtime.open_openable(pos=(2, 64, 0))

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "opened")
        self.assertTrue(result.metrics["empty_hand"])
        self.assertEqual(use.calls[0]["item"], None)
        self.assertEqual(use.calls[0]["expected_block_types"], ("oak_door",))
        self.assertEqual(use.calls[0]["look_target"], (2.5, 64.5, 0.5))
        self.assertIsNone(use.calls[0]["navigation_arrival_radius"])

    def test_open_openable_respects_governance_protection(self):
        body = FakeInteractionBody(
            entities=[],
            block_states={(2, 64, 0): ("minecraft:oak_door", "SOLID")},
            states=[state_at((0, 64, 0))],
        )
        policy = GovernancePolicy(protected_regions=[Region("base", (0, 0, -1), (5, 100, 1))])
        use = FakeUseTransaction(ToolResult(success=True, reason="completed", can_retry=False))
        runtime = InteractionTransactions(body, use=use, governance=policy)

        result = runtime.open_openable(pos=(2, 64, 0))

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "openable_denied")
        self.assertEqual(result.metrics["legality"]["reason"], "protected_region")
        self.assertEqual(use.calls, [])

    def test_open_openable_uses_gate_specific_low_look_target(self):
        body = FakeInteractionBody(
            entities=[],
            block_states={(2, 64, 0): ("minecraft:oak_fence_gate", "SOLID", {"open": "false", "facing": "east"})},
            states=[state_at((0, 64, 0))],
        )
        use = FakeUseTransaction(ToolResult(success=True, reason="completed", can_retry=False, metrics={}))
        runtime = InteractionTransactions(body, use=use)

        result = runtime.open_openable(pos=(2, 64, 0))

        self.assertTrue(result.success)
        self.assertEqual(use.calls[0]["look_target"], (2.5, 64.2, 0.5))

    def test_close_openable_reports_already_closed(self):
        body = FakeInteractionBody(
            entities=[],
            block_states={
                (2, 64, 0): (
                    "minecraft:oak_door",
                    "SOLID",
                    {"open": "false", "facing": "east", "hinge": "left"},
                )
            },
            states=[state_at((0, 64, 0))],
        )
        use = FakeUseTransaction(
            ToolResult(
                success=True,
                reason="already_in_expected_state",
                can_retry=False,
                metrics={"target_after": {"properties": {"open": "false"}}},
            )
        )
        runtime = InteractionTransactions(body, use=use)

        result = runtime.close_openable(pos=(2, 64, 0))

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "already_closed")
        self.assertEqual(use.calls, [])

    def test_close_openable_uses_empty_hand_and_reports_closed(self):
        body = FakeInteractionBody(
            entities=[],
            block_states={
                (2, 64, 0): (
                    "minecraft:oak_door",
                    "SOLID",
                    {"open": "true", "facing": "east", "hinge": "left"},
                )
            },
            states=[state_at((0, 64, 0))],
        )
        use = FakeUseTransaction(
            ToolResult(
                success=True,
                reason="completed",
                can_retry=False,
                metrics={
                    "target_before": {"properties": {"open": "true"}},
                    "target_after": {"properties": {"open": "false"}},
                    "empty_hand": True,
                },
            )
        )
        runtime = InteractionTransactions(body, use=use)

        result = runtime.close_openable(pos=(2, 64, 0))

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "closed")
        self.assertTrue(result.metrics["empty_hand"])
        self.assertEqual(use.calls[0]["item"], None)
        self.assertEqual(use.calls[0]["expected_block_types"], ("oak_door",))
        self.assertEqual(use.calls[0]["expected_properties"]["open"], "false")
        self.assertEqual(use.calls[0]["look_target"], (2.5, 64.5, 0.1))
        self.assertIsNone(use.calls[0]["navigation_arrival_radius"])

    def test_till_farmland_reports_already_tilled(self):
        body = FakeInteractionBody(
            entities=[],
            block_states={(2, 64, 0): ("minecraft:farmland", "SOLID", {"moisture": "7"})},
            states=[state_at((0, 64, 0))],
        )
        runtime = InteractionTransactions(body)

        result = runtime.till_farmland(hoe_item="minecraft:diamond_hoe", pos=(2, 64, 0))

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "already_tilled")

    def test_till_farmland_uses_hoe_and_reports_tilled(self):
        body = FakeInteractionBody(
            entities=[],
            block_states={(2, 64, 0): ("minecraft:dirt", "SOLID")},
            states=[state_at((0, 64, 0))],
        )
        use = FakeUseTransaction(
            ToolResult(
                success=True,
                reason="completed",
                can_retry=False,
                metrics={
                    "target_before": {"type": "dirt"},
                    "target_after": {"type": "farmland", "properties": {"moisture": "0"}},
                },
            )
        )
        runtime = InteractionTransactions(body, use=use)

        result = runtime.till_farmland(hoe_item="minecraft:diamond_hoe", pos=(2, 64, 0))

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "tilled")
        self.assertEqual(use.calls[0]["item"], "minecraft:diamond_hoe")
        self.assertEqual(use.calls[0]["expected_block_types"], ("farmland",))

    def test_till_farmland_respects_governance_protection(self):
        body = FakeInteractionBody(
            entities=[],
            block_states={(2, 64, 0): ("minecraft:dirt", "SOLID")},
            states=[state_at((0, 64, 0))],
        )
        policy = GovernancePolicy()
        use = FakeUseTransaction(ToolResult(success=True, reason="completed", can_retry=False))
        runtime = InteractionTransactions(body, use=use, governance=policy)

        result = runtime.till_farmland(hoe_item="minecraft:diamond_hoe", pos=(2, 64, 0))

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "till_denied")
        self.assertEqual(result.metrics["legality"]["reason"], "unknown_provenance")
        self.assertEqual(use.calls, [])

    def test_sow_crop_rejects_non_farmland_target(self):
        body = FakeInteractionBody(
            entities=[],
            block_states={(2, 64, 0): ("minecraft:dirt", "SOLID")},
            states=[state_at((0, 64, 0))],
        )
        runtime = InteractionTransactions(body)

        result = runtime.sow_crop(seed_item="minecraft:wheat_seeds", farmland_pos=(2, 64, 0))

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "sow_target_not_farmland")

    def test_sow_crop_uses_observe_pos_above_farmland_and_reports_sown(self):
        body = FakeInteractionBody(
            entities=[],
            block_states={
                (2, 64, 0): ("minecraft:farmland", "SOLID", {"moisture": "7"}),
                (2, 65, 0): ("minecraft:air", "CLEAR"),
            },
            states=[state_at((0, 64, 0))],
        )
        use = UseTransactions(body)
        use._prepare_use_item = lambda item, timeout_s=8.0: ToolResult(  # type: ignore[method-assign]
            success=True, reason="completed", can_retry=False, metrics={"slot": 0, "item": item}
        )
        use._sow_crop_on_farmland = lambda **kwargs: ToolResult(  # type: ignore[method-assign]
            success=True,
            reason="completed",
            can_retry=False,
            metrics={
                "target_before": {"type": "farmland", "properties": {"moisture": "7"}},
                "target_after": {"type": "farmland", "properties": {"moisture": "7"}},
                "observed_before": {"type": "air", "properties": {}},
                "observed_after": {"type": "wheat", "properties": {"age": "0"}},
                "target": list(kwargs["pos"]),
                "observe_pos": list(kwargs["observe_pos"]),
            },
        )
        runtime = InteractionTransactions(body, use=use)

        result = runtime.sow_crop(seed_item="minecraft:wheat_seeds", farmland_pos=(2, 64, 0))

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "sown")
        self.assertEqual(result.metrics["target"], {"pos": [2, 64, 0], "type": "farmland", "properties": {"moisture": "7"}})
        self.assertEqual(result.metrics["observe_pos"], [2, 65, 0])

    def test_sow_crop_respects_governance_protection(self):
        body = FakeInteractionBody(
            entities=[],
            block_states={
                (2, 64, 0): ("minecraft:farmland", "SOLID", {"moisture": "7"}),
                (2, 65, 0): ("minecraft:air", "CLEAR"),
            },
            states=[state_at((0, 64, 0))],
        )
        policy = GovernancePolicy()
        use = UseTransactions(body)
        runtime = InteractionTransactions(body, use=use, governance=policy)

        result = runtime.sow_crop(seed_item="minecraft:wheat_seeds", farmland_pos=(2, 64, 0))

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "sow_denied")
        self.assertEqual(result.metrics["legality"]["reason"], "unknown_provenance")

    def test_activate_switch_reports_not_found(self):
        body = FakeInteractionBody(entities=[], block_states={}, states=[state_at((0, 64, 0))])
        runtime = InteractionTransactions(body)

        result = runtime.activate_switch(search_radius=8)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "switch_not_found")

    def test_interaction_stand_points_include_floor_mounted_target_neighbors(self):
        body = FakeInteractionBody(
            entities=[],
            block_states={
                (2, 65, 0): ("minecraft:lever", "SOLID", {"face": "floor", "powered": "false"}),
            },
            states=[state_at((0, 64, 0))],
        )

        stands = interaction_stand_points(body, (2, 65, 0))

        self.assertNotIsInstance(stands, ToolResult)
        self.assertIn((1, 64, 0), stands)

    def test_interaction_stand_points_allow_head_cell_to_be_the_target_block(self):
        body = FakeInteractionBody(
            entities=[],
            block_states={
                (2, 64, 0): ("minecraft:farmland", "SOLID", {"moisture": "7"}),
                (2, 65, 0): ("minecraft:wheat", "SOLID", {"age": "0"}),
            },
            states=[state_at((1, 64, 0))],
        )

        stands = interaction_stand_points(body, (2, 64, 0))

        self.assertNotIsInstance(stands, ToolResult)
        self.assertIn((1, 64, 0), stands)

    def test_ensure_interaction_range_accepts_current_valid_stand_without_navigation(self):
        body = FakeInteractionBody(
            entities=[],
            block_states={
                (2, 64, 0): ("minecraft:farmland", "SOLID", {"moisture": "7"}),
                (2, 65, 0): ("minecraft:wheat", "SOLID", {"age": "0"}),
                (1, 64, 0): ("minecraft:air", "CLEAR"),
                (1, 65, 0): ("minecraft:air", "CLEAR"),
                (1, 63, 0): ("minecraft:stone", "SOLID"),
            },
            states=[state_at((1.2, 64, 0.2))],
        )

        result = ensure_interaction_range(
            body,
            navigator=None,
            target=(2, 64, 0),
            timeout_s=1.0,
            missing_reason="use_navigation_missing",
            failure_prefix="use_navigation_failed",
            no_stand_reason="use_no_stand_point",
            navigation_arrival_radius=0.25,
        )

        self.assertIsInstance(result, dict)
        self.assertFalse(result["navigated"])

    def test_activate_switch_reports_already_powered(self):
        body = FakeInteractionBody(
            entities=[],
            block_states={(2, 64, 0): ("minecraft:lever", "SOLID", {"powered": "true", "face": "wall"})},
            states=[state_at((0, 64, 0))],
        )
        use = FakeUseTransaction(
            ToolResult(
                success=True,
                reason="already_in_expected_state",
                can_retry=False,
                metrics={"target_after": {"properties": {"powered": "true"}}},
            )
        )
        runtime = InteractionTransactions(body, use=use)

        result = runtime.activate_switch(pos=(2, 64, 0))

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "already_powered")
        self.assertEqual(use.calls, [])

    def test_activate_switch_uses_empty_hand_and_reports_powered(self):
        body = FakeInteractionBody(
            entities=[],
            block_states={(2, 64, 0): ("minecraft:stone_button", "SOLID", {"powered": "false", "face": "wall"})},
            states=[state_at((0, 64, 0))],
        )
        use = FakeUseTransaction(
            ToolResult(
                success=True,
                reason="completed",
                can_retry=False,
                metrics={
                    "target_before": {"properties": {"powered": "false"}},
                    "target_after": {"properties": {"powered": "true"}},
                    "empty_hand": True,
                },
            )
        )
        runtime = InteractionTransactions(body, use=use)

        result = runtime.activate_switch(pos=(2, 64, 0))

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "powered")
        self.assertTrue(result.metrics["empty_hand"])
        self.assertEqual(use.calls[0]["expected_block_types"], ("stone_button",))
        self.assertEqual(use.calls[0]["expected_properties"]["powered"], "true")
        self.assertEqual(use.calls[0]["look_target"], (2.5, 64.5, 0.5))
        self.assertEqual(use.calls[0]["navigation_arrival_radius"], 0.25)

    def test_activate_switch_respects_governance_protection(self):
        body = FakeInteractionBody(
            entities=[],
            block_states={(2, 64, 0): ("minecraft:lever", "SOLID", {"powered": "false", "face": "wall"})},
            states=[state_at((0, 64, 0))],
        )
        policy = GovernancePolicy()
        use = FakeUseTransaction(ToolResult(success=True, reason="completed", can_retry=False))
        runtime = InteractionTransactions(body, use=use, governance=policy)

        result = runtime.activate_switch(pos=(2, 64, 0))

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "switch_denied")
        self.assertEqual(result.metrics["legality"]["reason"], "unknown_provenance")
        self.assertEqual(use.calls, [])

    def test_deactivate_switch_reports_already_unpowered(self):
        body = FakeInteractionBody(
            entities=[],
            block_states={(2, 64, 0): ("minecraft:lever", "SOLID", {"powered": "false", "face": "wall"})},
            states=[state_at((0, 64, 0))],
        )
        use = FakeUseTransaction(
            ToolResult(
                success=True,
                reason="already_in_expected_state",
                can_retry=False,
                metrics={"target_after": {"properties": {"powered": "false"}}},
            )
        )
        runtime = InteractionTransactions(body, use=use)

        result = runtime.deactivate_switch(pos=(2, 64, 0))

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "already_unpowered")
        self.assertEqual(use.calls, [])

    def test_deactivate_switch_uses_empty_hand_and_reports_unpowered(self):
        body = FakeInteractionBody(
            entities=[],
            block_states={(2, 64, 0): ("minecraft:lever", "SOLID", {"powered": "true", "face": "wall"})},
            states=[state_at((0, 64, 0))],
        )
        use = FakeUseTransaction(
            ToolResult(
                success=True,
                reason="completed",
                can_retry=False,
                metrics={
                    "target_before": {"properties": {"powered": "true"}},
                    "target_after": {"properties": {"powered": "false"}},
                    "empty_hand": True,
                },
            )
        )
        runtime = InteractionTransactions(body, use=use)

        result = runtime.deactivate_switch(pos=(2, 64, 0))

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "unpowered")
        self.assertTrue(result.metrics["empty_hand"])
        self.assertEqual(use.calls[0]["expected_block_types"], ("lever",))
        self.assertEqual(use.calls[0]["expected_properties"]["powered"], "false")
        self.assertEqual(use.calls[0]["look_target"], (2.5, 64.5, 0.5))
        self.assertEqual(use.calls[0]["navigation_arrival_radius"], 0.25)

    def test_deactivate_switch_waits_for_button_release(self):
        body = FakeInteractionBody(
            entities=[],
            block_states={(2, 64, 0): ("minecraft:stone_button", "SOLID", {"powered": "true", "face": "wall"})},
            states=[state_at((0, 64, 0))],
        )
        runtime = InteractionTransactions(body)
        block_reads = iter(
            [
                ("minecraft:stone_button", "SOLID", {"powered": "true", "face": "wall"}),
                ("minecraft:stone_button", "SOLID", {"powered": "false", "face": "wall"}),
            ]
        )

        original_perceive = body.perceive

        def perceive(scope, params):
            if scope == "blockAt":
                body.block_states[(2, 64, 0)] = next(block_reads)
            return original_perceive(scope, params)

        body.perceive = perceive  # type: ignore[assignment]

        with patch("minebot.body.interaction.time.sleep", lambda _seconds: None):
            result = runtime.deactivate_switch(pos=(2, 64, 0), release_timeout_s=0.2, release_poll_s=0.0)

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "released")
        self.assertTrue(result.metrics["waited_for_release"])
        self.assertEqual(result.metrics["target_after"]["properties"]["powered"], "false")

    def test_deactivate_switch_reports_button_release_timeout(self):
        body = FakeInteractionBody(
            entities=[],
            block_states={(2, 64, 0): ("minecraft:stone_button", "SOLID", {"powered": "true", "face": "wall"})},
            states=[state_at((0, 64, 0))],
        )
        runtime = InteractionTransactions(body)

        with patch("minebot.body.interaction.time.sleep", lambda _seconds: None):
            result = runtime.deactivate_switch(pos=(2, 64, 0), release_timeout_s=0.01, release_poll_s=0.0)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "switch_release_timeout")
        self.assertTrue(result.can_retry)
        self.assertTrue(result.metrics["waited_for_release"])

    def test_deactivate_switch_respects_governance_protection(self):
        body = FakeInteractionBody(
            entities=[],
            block_states={(2, 64, 0): ("minecraft:lever", "SOLID", {"powered": "true", "face": "wall"})},
            states=[state_at((0, 64, 0))],
        )
        policy = GovernancePolicy()
        use = FakeUseTransaction(ToolResult(success=True, reason="completed", can_retry=False))
        runtime = InteractionTransactions(body, use=use, governance=policy)

        result = runtime.deactivate_switch(pos=(2, 64, 0))

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "switch_denied")
        self.assertEqual(result.metrics["legality"]["reason"], "unknown_provenance")
        self.assertEqual(use.calls, [])

    def test_harvest_and_resow_requires_farmland(self):
        body = FakeInteractionBody(
            entities=[],
            block_states={
                (2, 64, 0): ("minecraft:dirt", "SOLID"),
                (2, 65, 0): ("minecraft:wheat", "SOLID", {"age": "7"}),
            },
            states=[state_at((0, 64, 0))],
        )
        work = FakeWorkTransaction(ToolResult(success=True, reason="collected", can_retry=False))
        runtime = InteractionTransactions(body, work=work)

        result = runtime.harvest_and_resow(farmland_pos=(2, 64, 0))

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "harvest_target_not_farmland")
        self.assertEqual(work.calls, [])

    def test_harvest_and_resow_requires_mature_crop(self):
        body = FakeInteractionBody(
            entities=[],
            block_states={
                (2, 64, 0): ("minecraft:farmland", "SOLID", {"moisture": "7"}),
                (2, 65, 0): ("minecraft:wheat", "SOLID", {"age": "3"}),
            },
            states=[state_at((0, 64, 0))],
        )
        work = FakeWorkTransaction(ToolResult(success=True, reason="collected", can_retry=False))
        runtime = InteractionTransactions(body, work=work)

        result = runtime.harvest_and_resow(farmland_pos=(2, 64, 0))

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "harvest_crop_not_mature")
        self.assertEqual(result.metrics["age"], 3)
        self.assertEqual(result.metrics["required_age"], 7)
        self.assertEqual(work.calls, [])

    def test_harvest_and_resow_harvests_then_resows(self):
        body = FakeInteractionBody(
            entities=[],
            block_states={
                (2, 64, 0): ("minecraft:farmland", "SOLID", {"moisture": "7"}),
                (2, 65, 0): ("minecraft:wheat", "SOLID", {"age": "7"}),
            },
            states=[state_at((0, 64, 0))],
        )
        work = FakeWorkTransaction(
            ToolResult(
                success=True,
                reason="collected",
                can_retry=False,
                metrics={"collected_total": 2, "expected_drops": ["wheat", "wheat_seeds"]},
            )
        )
        use = FakeUseTransaction(
            ToolResult(
                success=True,
                reason="completed",
                can_retry=False,
                metrics={
                    "target_before": {"type": "farmland", "properties": {"moisture": "7"}},
                    "target_after": {"type": "farmland", "properties": {"moisture": "7"}},
                    "observed_before": {"type": "air", "properties": {}},
                    "observed_after": {"type": "wheat", "properties": {"age": "0"}},
                },
            )
        )
        runtime = InteractionTransactions(body, use=use, work=work)

        result = runtime.harvest_and_resow(farmland_pos=(2, 64, 0))

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "harvested_and_resown")
        self.assertEqual(work.calls[0]["pos"], (2, 65, 0))
        self.assertEqual(work.calls[0]["expected_drops"], ("wheat", "wheat_seeds"))
        self.assertEqual(use.calls[0]["phase"], "prepare")
        self.assertEqual(use.calls[0]["item"], "wheat_seeds")
        self.assertEqual(use.calls[1]["phase"], "sow")
        self.assertEqual(use.calls[1]["seed_item"], "wheat_seeds")
        self.assertEqual(use.calls[1]["observe_pos"], (2, 65, 0))
        self.assertEqual(result.metrics["harvest"]["reason"], "collected")
        self.assertEqual(result.metrics["resow"]["reason"], "sown")

    def test_harvest_and_resow_respects_governance_protection(self):
        body = FakeInteractionBody(
            entities=[],
            block_states={
                (2, 64, 0): ("minecraft:farmland", "SOLID", {"moisture": "7"}),
                (2, 65, 0): ("minecraft:wheat", "SOLID", {"age": "7"}),
            },
            states=[state_at((0, 64, 0))],
        )
        policy = GovernancePolicy()
        work = FakeWorkTransaction(ToolResult(success=True, reason="collected", can_retry=False))
        runtime = InteractionTransactions(body, work=work, governance=policy)

        result = runtime.harvest_and_resow(farmland_pos=(2, 64, 0))

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "harvest_denied")
        self.assertEqual(result.metrics["legality"]["reason"], "unknown_provenance")
        self.assertEqual(work.calls, [])

    def test_harvest_and_resow_surfaces_resow_failure_truthfully(self):
        body = FakeInteractionBody(
            entities=[],
            block_states={
                (2, 64, 0): ("minecraft:farmland", "SOLID", {"moisture": "7"}),
                (2, 65, 0): ("minecraft:carrots", "SOLID", {"age": "7"}),
            },
            states=[state_at((0, 64, 0))],
        )
        work = FakeWorkTransaction(
            ToolResult(success=True, reason="collected", can_retry=False, metrics={"collected_total": 1})
        )
        use = FakeUseTransaction(
            ToolResult(
                success=False,
                reason="targeted_use_no_effect",
                can_retry=True,
                metrics={"observed_after": {"type": "air", "properties": {}}},
            )
        )
        runtime = InteractionTransactions(body, use=use, work=work)

        result = runtime.harvest_and_resow(farmland_pos=(2, 64, 0))

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "resow_failed:targeted_use_no_effect")
        self.assertEqual(work.calls[0]["expected_drops"], ("carrot",))
        self.assertEqual(use.calls[0]["item"], "carrot")
        self.assertEqual(use.calls[1]["seed_item"], "carrot")

    def test_give_player_reports_receiver_not_found(self):
        body = FakeInteractionBody(entities=[], block_states={}, states=[state_at((0, 64, 0))])
        inventory = FakeInventoryTransaction(ToolResult(success=True, reason="completed", can_retry=False))
        runtime = InteractionTransactions(body, inventory=inventory)

        result = runtime.give_player(receiver_name="Receiver", item="minecraft:diamond", count=1)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "receiver_not_found")
        self.assertEqual(inventory.calls, [])

    def test_give_player_requires_navigation_when_receiver_out_of_range(self):
        body = FakeInteractionBody(
            entities=[player_entity(pos=(8.0, 64.0, 0.0), dist2=64.0)],
            block_states={
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
            },
            states=[state_at((0, 64, 0)), state_at((0, 64, 0))],
        )
        inventory = FakeInventoryTransaction(ToolResult(success=True, reason="completed", can_retry=False))
        runtime = InteractionTransactions(body, inventory=inventory)

        result = runtime.give_player(receiver_name="Receiver", item="minecraft:diamond", count=1)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "receiver_navigation_missing")
        self.assertEqual(inventory.calls, [])

    def test_give_player_reports_unconfirmed_pickup_when_no_receipt_arrives(self):
        discard = ToolResult(success=True, reason="completed", can_retry=False, metrics={"dropped_count": 1})
        body = FakeInteractionBody(
            entities=[player_entity(pos=(2.0, 64.0, 0.0), dist2=4.0)],
            block_states={},
            states=[state_at((0, 64, 0)), state_at((0, 64, 0)), state_at((0, 64, 0))],
            events=[[], []],
        )
        inventory = FakeInventoryTransaction(discard)
        runtime = InteractionTransactions(body, inventory=inventory)

        result = runtime.give_player(
            receiver_name="Receiver",
            item="minecraft:diamond",
            count=1,
            pickup_timeout_s=0.01,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "receiver_pickup_unconfirmed")
        self.assertEqual([action.name for action in body.actions], ["lookAt", "handoffItem"])
        self.assertEqual(body.actions[1].params["item"], "minecraft:diamond")
        self.assertGreaterEqual(body.poll_calls, 1)

    def test_give_player_succeeds_with_matching_pickup_receipt(self):
        discard = ToolResult(success=True, reason="completed", can_retry=False, metrics={"dropped_count": 2})
        body = FakeInteractionBody(
            entities=[player_entity(pos=(2.0, 64.0, 0.0), dist2=4.0)],
            block_states={},
            states=[state_at((0, 64, 0)), state_at((0, 64, 0)), state_at((0, 64, 0))],
            events=[
                [],
                [
                    Event(
                        seq=4,
                        tick=40,
                        bot="Receiver",
                        name="itemPickup",
                        data={"player": "Receiver", "item": "minecraft:diamond", "count": 2, "stack": {"item": "minecraft:diamond", "count": 2}},
                    )
                ],
            ],
        )
        inventory = FakeInventoryTransaction(discard)
        runtime = InteractionTransactions(body, inventory=inventory)

        result = runtime.give_player(
            receiver_name="Receiver",
            item="minecraft:diamond",
            count=2,
            pickup_timeout_s=0.05,
        )

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "completed")
        self.assertEqual([action.name for action in body.actions], ["lookAt", "handoffItem"])
        receipt = result.metrics["pickup_receipt"]
        self.assertEqual(receipt["player"], "Receiver")
        self.assertEqual(receipt["count"], 2)

    def test_give_player_micro_moves_when_receiver_is_near_but_outside_handoff_band(self):
        discard = ToolResult(success=True, reason="completed", can_retry=False, metrics={"dropped_count": 2})
        body = FakeInteractionBody(
            entities=[
                player_entity(pos=(4.0, 64.0, 0.0), dist2=16.0),
                player_entity(pos=(4.0, 64.0, 0.0), dist2=6.25),
            ],
            block_states={
                (5, 64, 0): ("minecraft:air", "CLEAR"),
                (5, 65, 0): ("minecraft:air", "CLEAR"),
                (5, 63, 0): ("minecraft:stone", "SOLID"),
                (3, 64, 0): ("minecraft:air", "CLEAR"),
                (3, 65, 0): ("minecraft:air", "CLEAR"),
                (3, 63, 0): ("minecraft:stone", "SOLID"),
                (4, 64, 1): ("minecraft:air", "CLEAR"),
                (4, 65, 1): ("minecraft:air", "CLEAR"),
                (4, 63, 1): ("minecraft:stone", "SOLID"),
                (4, 64, -1): ("minecraft:air", "CLEAR"),
                (4, 65, -1): ("minecraft:air", "CLEAR"),
                (4, 63, -1): ("minecraft:stone", "SOLID"),
                (4, 64, 0): ("minecraft:air", "CLEAR"),
                (4, 65, 0): ("minecraft:air", "CLEAR"),
                (4, 63, 0): ("minecraft:stone", "SOLID"),
            },
            states=[
                state_at((0, 64, 0)),
                state_at((0, 64, 0)),
                state_at((1.5, 64, 0)),
                state_at((1.5, 64, 0)),
            ],
            events=[
                [],
                [Event(seq=4, tick=40, bot="Receiver", name="itemPickup", data={"player": "Receiver", "item": "minecraft:diamond", "count": 2, "stack": {"item": "minecraft:diamond", "count": 2}})],
            ],
        )
        inventory = FakeInventoryTransaction(discard)
        navigator = FakeInteractionNavigator(
            [ToolResult(success=True, reason="arrived", can_retry=False, metrics={"selected_goal": [3, 64, 0]})]
        )
        runtime = InteractionTransactions(body, navigator=navigator, inventory=inventory)

        result = runtime.give_player(
            receiver_name="Receiver",
            item="minecraft:diamond",
            count=2,
            pickup_timeout_s=0.05,
        )

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "completed")
        self.assertEqual([action.name for action in body.actions], ["lookAt", "handoffItem"])
        self.assertEqual(len(navigator.calls), 1)
        self.assertIsInstance(navigator.calls[0][0], GoalComposite)
        self.assertTrue(result.metrics["approach"]["navigated"])
        self.assertGreaterEqual(result.metrics["approach"]["final_distance"], 1.25)
        self.assertLessEqual(result.metrics["approach"]["final_distance"], 3.0)

    def test_give_player_navigates_then_looks_then_discards(self):
        discard = ToolResult(success=True, reason="completed", can_retry=False, metrics={"dropped_count": 1})
        body = FakeInteractionBody(
            entities=[player_entity(pos=(8.0, 64.0, 0.0), dist2=64.0), player_entity(pos=(8.0, 64.0, 0.0), dist2=1.0)],
            block_states={
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
                (8, 64, 0): ("minecraft:air", "CLEAR"),
                (8, 65, 0): ("minecraft:air", "CLEAR"),
                (8, 63, 0): ("minecraft:stone", "SOLID"),
            },
            states=[state_at((0, 64, 0)), state_at((0, 64, 0)), state_at((6.55, 64, 0)), state_at((6.55, 64, 0))],
            events=[[], [Event(seq=2, tick=22, bot="Receiver", name="itemPickup", data={"player": "Receiver", "item": "minecraft:diamond", "count": 1, "stack": {"item": "minecraft:diamond", "count": 1}})]],
        )
        inventory = FakeInventoryTransaction(discard)
        navigator = FakeInteractionNavigator([ToolResult(success=True, reason="arrived", can_retry=False)])
        runtime = InteractionTransactions(body, navigator=navigator, inventory=inventory)

        result = runtime.give_player(
            receiver_name="Receiver",
            item="minecraft:diamond",
            count=1,
            pickup_timeout_s=0.05,
        )

        self.assertTrue(result.success)
        self.assertEqual(len(navigator.calls), 1)
        self.assertEqual(body.actions[0].name, "lookAt")
        self.assertEqual(body.actions[1].name, "handoffItem")
        self.assertEqual(body.actions[1].params["count"], 1)


if __name__ == "__main__":
    unittest.main()
