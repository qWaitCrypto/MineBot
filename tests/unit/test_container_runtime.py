import unittest

from minebot.body import ContainerTransactions
from minebot.contract import Action, BodyState, Event, PerceptionResult, Result, ToolResult
from minebot.game.governance import GovernancePolicy, Region


class FakeContainerBody:
    bot_name = "Bot1"

    def __init__(self, container_pages, inventory_pages, *, accepted: bool = True, block_states=None):
        self.container_pages = list(container_pages)
        self.inventory_pages = list(inventory_pages)
        self.accepted = accepted
        self.block_states = dict(block_states or {(1, 59, 0): ("minecraft:chest", "SOLID")})
        self.actions: list[Action] = []
        self.perceptions: list[tuple[str, dict[str, object]]] = []

    def perceive(self, scope: str, params: dict[str, object]) -> PerceptionResult:
        self.perceptions.append((scope, params))
        if scope == "container":
            return self.container_pages.pop(0)
        if scope == "inventory":
            return self.inventory_pages.pop(0)
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
        return Event(
            seq=len(self.actions),
            tick=20,
            bot="Bot1",
            name="containerDone",
            data={
                "action_id": action_id,
                "success": True,
                "stopped_reason": "completed",
                "count": action.params["count"],
            },
        )


def perception(scope, slots, *, complete=True, next_start=None, ok=True):
    return PerceptionResult(
        bot="Bot1",
        scope=scope,
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


class FakeNearestContainerBody(FakeContainerBody):
    def __init__(self, container_pages, inventory_pages, *, found_blocks, block_states, states, accepted: bool = True):
        super().__init__(container_pages, inventory_pages, accepted=accepted)
        self.found_blocks = list(found_blocks)
        self.block_states = dict(block_states)
        self.states = list(states)

    def get_state(self) -> BodyState:
        if len(self.states) == 1:
            return self.states[0]
        return self.states.pop(0)

    def perceive(self, scope: str, params: dict[str, object]) -> PerceptionResult:
        self.perceptions.append((scope, params))
        if scope == "findBlocks":
            return PerceptionResult(
                bot="Bot1",
                scope="findBlocks",
                type="perception",
                ok=True,
                complete=True,
                data={"blocks": list(self.found_blocks)},
                uncertainty=[],
                next=None,
                error=None,
            )
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
        return super().perceive(scope, params)


class ContainerRuntimeTests(unittest.TestCase):
    def test_transfer_from_container_selects_slots_by_item_and_count(self):
        body = FakeContainerBody(
            container_pages=[
                perception(
                    "container",
                    [
                        slot(0, "minecraft:cobblestone", 8),
                        slot(1, "minecraft:diamond", 3),
                        slot(2, "minecraft:diamond", 5),
                    ],
                )
            ],
            inventory_pages=[
                perception("inventory", [slot(0), slot(1), slot(2, "minecraft:diamond", 62)])
            ],
        )
        runtime = ContainerTransactions(body)

        result = runtime.transfer_item(
            (1, 59, 0),
            item="minecraft:diamond",
            count=4,
            direction="container_to_bot",
            total_slots=3,
        )

        self.assertTrue(result.success)
        self.assertEqual(result.metrics["moved_count"], 4)
        self.assertEqual(len(body.actions), 2)
        self.assertEqual(body.actions[0].params["container_slot"], 1)
        self.assertEqual(body.actions[0].params["bot_slot"], 0)
        self.assertEqual(body.actions[0].params["count"], 3)
        self.assertEqual(body.actions[1].params["container_slot"], 2)
        self.assertEqual(body.actions[1].params["bot_slot"], 0)
        self.assertEqual(body.actions[1].params["count"], 1)

    def test_transfer_to_container_merges_then_uses_empty_slot(self):
        body = FakeContainerBody(
            container_pages=[
                perception(
                    "container",
                    [slot(0, "minecraft:oak_log", 63), slot(1), slot(2, "minecraft:stone", 10)],
                )
            ],
            inventory_pages=[
                perception("inventory", [slot(0, "minecraft:oak_log", 4), slot(1), slot(2)])
            ],
        )
        runtime = ContainerTransactions(body)

        result = runtime.transfer_item(
            (1, 59, 0),
            item="oak_log",
            count=4,
            direction="bot_to_container",
            total_slots=3,
        )

        self.assertTrue(result.success)
        self.assertEqual(len(body.actions), 2)
        self.assertEqual(body.actions[0].params["bot_slot"], 0)
        self.assertEqual(body.actions[0].params["container_slot"], 0)
        self.assertEqual(body.actions[0].params["count"], 1)
        self.assertEqual(body.actions[1].params["bot_slot"], 0)
        self.assertEqual(body.actions[1].params["container_slot"], 1)
        self.assertEqual(body.actions[1].params["count"], 3)

    def test_reports_destination_full_without_executing(self):
        body = FakeContainerBody(
            container_pages=[
                perception("container", [slot(0, "minecraft:diamond", 2)])
            ],
            inventory_pages=[
                perception("inventory", [slot(0, "minecraft:dirt", 64)])
            ],
        )
        runtime = ContainerTransactions(body)

        result = runtime.transfer_item(
            (1, 59, 0),
            item="minecraft:diamond",
            count=1,
            direction="container_to_bot",
            total_slots=1,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "destination_full")
        self.assertEqual(body.actions, [])
        self.assertEqual(result.metrics["available_count"], 2)

    def test_reports_missing_item_without_executing(self):
        body = FakeContainerBody(
            container_pages=[perception("container", [slot(0, "minecraft:stone", 2)])],
            inventory_pages=[perception("inventory", [slot(0)])],
        )
        runtime = ContainerTransactions(body)

        result = runtime.transfer_item(
            (1, 59, 0),
            item="minecraft:diamond",
            count=1,
            direction="container_to_bot",
            total_slots=1,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "item_not_available")
        self.assertEqual(body.actions, [])

    def test_refuses_incomplete_perception(self):
        body = FakeContainerBody(
            container_pages=[
                PerceptionResult(
                    bot="Bot1",
                    scope="container",
                    type="perception",
                    ok=True,
                    complete=False,
                    data={
                        "start": 0,
                        "limit": 1,
                        "nextStart": None,
                        "totalSlots": 1,
                        "slots": [slot(0, "minecraft:diamond", 2)],
                    },
                    uncertainty=[{"reason": "truncated"}],
                    next=None,
                )
            ],
            inventory_pages=[],
        )
        runtime = ContainerTransactions(body)

        result = runtime.transfer_item(
            (1, 59, 0),
            item="minecraft:diamond",
            count=1,
            direction="container_to_bot",
            total_slots=1,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "perception_failed")
        self.assertEqual(body.actions, [])

    def test_refuses_missing_body_perception(self):
        body = FakeContainerBody(
            container_pages=[
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
                )
            ],
            inventory_pages=[],
        )
        runtime = ContainerTransactions(body)

        result = runtime.transfer_item(
            (1, 59, 0),
            item="minecraft:diamond",
            count=1,
            direction="container_to_bot",
            total_slots=1,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "perception_failed")
        self.assertEqual(body.actions, [])

    def test_reports_body_rejection_with_plan_facts(self):
        body = FakeContainerBody(
            container_pages=[perception("container", [slot(0, "minecraft:diamond", 2)])],
            inventory_pages=[perception("inventory", [slot(0)])],
            accepted=False,
        )
        runtime = ContainerTransactions(body)

        result = runtime.transfer_item(
            (1, 59, 0),
            item="minecraft:diamond",
            count=1,
            direction="container_to_bot",
            total_slots=1,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "body_rejected")
        self.assertEqual(result.metrics["planned_count"], 1)
        self.assertEqual(len(body.actions), 1)

    def test_transfer_item_respects_governance_protection(self):
        body = FakeContainerBody(
            container_pages=[perception("container", [slot(0, "minecraft:diamond", 2)])],
            inventory_pages=[perception("inventory", [slot(0)])],
        )
        runtime = ContainerTransactions(body, governance=GovernancePolicy())

        result = runtime.transfer_item(
            (1, 59, 0),
            item="minecraft:diamond",
            count=1,
            direction="container_to_bot",
            total_slots=1,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "container_denied")
        self.assertEqual(result.metrics["legality"]["reason"], "unknown_provenance")
        self.assertEqual(body.actions, [])

    def test_transfer_item_rejects_wrong_block_type(self):
        body = FakeContainerBody(
            container_pages=[perception("container", [slot(0, "minecraft:diamond", 2)])],
            inventory_pages=[perception("inventory", [slot(0)])],
            block_states={(1, 59, 0): ("minecraft:furnace", "SOLID")},
        )
        runtime = ContainerTransactions(body)

        result = runtime.transfer_item(
            (1, 59, 0),
            item="minecraft:diamond",
            count=1,
            direction="container_to_bot",
            total_slots=1,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "container_wrong_type")
        self.assertEqual(body.actions, [])

    def test_transfer_nearest_container_reports_not_found(self):
        body = FakeNearestContainerBody(
            container_pages=[],
            inventory_pages=[],
            found_blocks=[],
            block_states={},
            states=[state_at((0, 64, 0))],
        )
        runtime = ContainerTransactions(body)

        result = runtime.transfer_nearest_container(
            item="diamond",
            count=1,
            direction="container_to_bot",
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "container_not_found")
        self.assertEqual(body.perceptions[0][0], "findBlocks")

    def test_transfer_nearest_container_requires_navigation_when_out_of_range(self):
        body = FakeNearestContainerBody(
            container_pages=[],
            inventory_pages=[],
            found_blocks=[{"x": 8, "y": 64, "z": 0, "type": "minecraft:chest"}],
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
            states=[state_at((0, 64, 0))],
        )
        runtime = ContainerTransactions(body)

        result = runtime.transfer_nearest_container(
            item="diamond",
            count=1,
            direction="container_to_bot",
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "container_navigation_missing")
        self.assertEqual(result.metrics["attempted_targets"][0]["container_target"], [8, 64, 0])

    def test_transfer_nearest_container_reports_navigation_failure(self):
        body = FakeNearestContainerBody(
            container_pages=[],
            inventory_pages=[],
            found_blocks=[{"x": 8, "y": 64, "z": 0, "type": "minecraft:chest"}],
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
            states=[state_at((0, 64, 0))],
        )
        navigator = FakeInteractionNavigator(
            [
                ToolResult(success=False, reason="blocked", can_retry=True),
                ToolResult(success=False, reason="blocked", can_retry=True),
                ToolResult(success=False, reason="blocked", can_retry=True),
                ToolResult(success=False, reason="blocked", can_retry=True),
            ]
        )
        runtime = ContainerTransactions(body, navigator=navigator)

        result = runtime.transfer_nearest_container(
            item="diamond",
            count=1,
            direction="container_to_bot",
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "container_navigation_failed:blocked")
        self.assertEqual(len(navigator.calls), 4)

    def test_transfer_nearest_container_navigates_then_transfers(self):
        body = FakeNearestContainerBody(
            container_pages=[perception("container", [slot(0, "minecraft:diamond", 2)])],
            inventory_pages=[perception("inventory", [slot(0)])],
            found_blocks=[{"x": 8, "y": 64, "z": 0, "type": "minecraft:chest"}],
            block_states={
                (8, 64, 0): ("minecraft:chest", "SOLID"),
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
            states=[state_at((0, 64, 0)), state_at((0, 64, 0)), state_at((7, 64, 0))],
        )
        navigator = FakeInteractionNavigator([ToolResult(success=True, reason="arrived", can_retry=False)])
        runtime = ContainerTransactions(body, navigator=navigator)

        result = runtime.transfer_nearest_container(
            item="diamond",
            count=1,
            direction="container_to_bot",
        )

        self.assertTrue(result.success)
        self.assertEqual(result.metrics["container_target"], [8, 64, 0])
        self.assertTrue(result.metrics["approach"]["navigated"])
        self.assertEqual(len(body.actions), 1)

    def test_transfer_nearest_container_respects_governance_protection(self):
        body = FakeNearestContainerBody(
            container_pages=[perception("container", [slot(0, "minecraft:diamond", 2)])],
            inventory_pages=[perception("inventory", [slot(0)])],
            found_blocks=[{"x": 8, "y": 64, "z": 0, "type": "minecraft:chest"}],
            block_states={
                (8, 64, 0): ("minecraft:chest", "SOLID"),
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
            states=[state_at((0, 64, 0))],
        )
        policy = GovernancePolicy(protected_regions=[Region("base", (8, 60, 0), (8, 70, 0))])
        navigator = FakeInteractionNavigator([ToolResult(success=True, reason="arrived", can_retry=False)])
        runtime = ContainerTransactions(body, navigator=navigator, governance=policy)

        result = runtime.transfer_nearest_container(
            item="diamond",
            count=1,
            direction="container_to_bot",
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "container_denied")
        self.assertEqual(result.metrics["legality"]["reason"], "protected_region")
        self.assertEqual(navigator.calls, [])
        self.assertEqual(body.actions, [])


if __name__ == "__main__":
    unittest.main()
