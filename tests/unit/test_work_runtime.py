import unittest

from minebot.body import BlockWork
from minebot.body.block_work import _scan_place_here_candidates, _surface_lateral_columns
from minebot.game.governance import BreakContext, GovernancePolicy, PlaceContext, Region
from minebot.contract import (
    Action,
    BodyState,
    Event,
    PerceptionResult,
    Result,
    StructureRiskAssessment,
    StructureRiskLevel,
    ToolResult,
)
from minebot.game.navigation import GoalComposite, NavigationGoal
from tests.unit._body_batch_helper import batch_block_cells_from_blockat


def slot(slot_id: int, item: str | None = None, count: int = 0) -> dict[str, object]:
    return {"slot": slot_id, "item": item, "count": count, "empty": item is None}


def inventory_page(slots: list[dict[str, object]], *, next_start: int | None = None) -> PerceptionResult:
    data: dict[str, object] = {"slots": slots}
    if next_start is not None:
        data["nextStart"] = next_start
    return PerceptionResult(
        bot="Bot1",
        scope="inventory",
        type="perception",
        ok=True,
        complete=next_start is None,
        data=data,
    )


def inv_page_with_pickaxe(
    slots: list[dict[str, object]],
    pickaxe: str = "minecraft:iron_pickaxe",
    *,
    next_start: int | None = None,
) -> PerceptionResult:
    return inventory_page([slot(0, pickaxe, 1), *slots], next_start=next_start)


def goal_position(goal) -> tuple[int, int, int]:
    if isinstance(goal, GoalComposite):
        return goal.goals[0].representative((0, 0, 0))
    if isinstance(goal, NavigationGoal):
        return goal.representative((0, 0, 0))
    return (int(goal[0]), int(goal[1]), int(goal[2]))


class FakeBody:
    bot_name = "Bot1"

    def __init__(
        self,
        perception: PerceptionResult | None = None,
        terminal: Event | None = None,
        blocks: dict[tuple[int, int, int], tuple[str, str]] | None = None,
        inventory_pages: list[PerceptionResult] | None = None,
        find_blocks: list[dict[str, object]] | None = None,
        find_block_pages: list[PerceptionResult] | None = None,
        find_blocks_complete: bool = True,
        find_blocks_uncertainty: list[dict[str, object]] | None = None,
    ):
        self.perception = perception or PerceptionResult(
            bot="Bot1",
            scope="blockAt",
            type="perception",
            ok=True,
            complete=True,
            data={"x": 0, "y": 64, "z": 0, "type": "stone", "state": "SOLID"},
        )
        self.terminal = terminal or Event(
            seq=1,
            tick=10,
            bot="Bot1",
            name="mineDone",
            data={"action_id": "placeholder", "success": True, "block_gone": True},
        )
        self.actions: list[Action] = []
        self.perceptions: list[tuple[str, dict[str, object]]] = []
        self.blocks = blocks
        self.inventory_pages = list(inventory_pages or [])
        self.find_blocks = list(find_blocks or [])
        self.find_block_pages = list(find_block_pages or [])
        self.find_blocks_complete = find_blocks_complete
        self.find_blocks_uncertainty = list(find_blocks_uncertainty or [])
        self.state_pos = (
            float(self.perception.data.get("x", 0)),
            float(self.perception.data.get("y", 64)),
            float(self.perception.data.get("z", 0)),
        )

    def perceive(self, scope: str, params: dict[str, object]) -> PerceptionResult:
        self.perceptions.append((scope, params))
        if scope == "inventory":
            if not self.inventory_pages:
                return PerceptionResult(
                    bot="Bot1",
                    scope="inventory",
                    type="perception",
                    ok=True,
                    complete=True,
                    data={"slots": []},
                )
            return self.inventory_pages.pop(0)
        if scope == "findBlocks":
            if self.find_block_pages:
                return self.find_block_pages.pop(0)
            return PerceptionResult(
                bot="Bot1",
                scope="findBlocks",
                type="perception",
                ok=True,
                complete=self.find_blocks_complete,
                data={"blocks": list(self.find_blocks)},
                uncertainty=self.find_blocks_uncertainty,
            )
        if self.blocks is not None and scope == "blockCells":
            return batch_block_cells_from_blockat(self, params)
        if self.blocks is not None and scope == "surfaceColumns":
            columns = params.get("columns") or []
            start = int(params.get("start") or 0)
            limit = int(params.get("limit") or 64)
            page = columns[start : start + limit]
            facts = []
            for column in page:
                x, z = int(column[0]), int(column[1])
                occupied = [
                    y
                    for (bx, y, bz), (_block_type, state) in self.blocks.items()
                    if bx == x and bz == z and state != "CLEAR"
                ]
                feet_y = max(occupied) + 1 if occupied else 0
                feet_type, feet_state = self.blocks.get((x, feet_y, z), ("air", "CLEAR"))
                head_type, head_state = self.blocks.get((x, feet_y + 1, z), ("air", "CLEAR"))
                support_type, support_state = self.blocks.get((x, feet_y - 1, z), ("air", "CLEAR"))
                facts.append(
                    {
                        "x": x,
                        "z": z,
                        "feetY": feet_y,
                        "feetType": feet_type,
                        "feetState": feet_state,
                        "headType": head_type,
                        "headState": head_state,
                        "supportType": support_type,
                        "supportState": support_state,
                    }
                )
            next_start = start + len(page)
            nxt = None if next_start >= len(columns) else next_start
            return PerceptionResult(
                bot="Bot1",
                scope="surfaceColumns",
                type="perception",
                ok=True,
                complete=nxt is None,
                data={"columns": facts, "nextStart": nxt},
                uncertainty=[] if nxt is None else [{"reason": "limit_exceeded"}],
            )
        if self.blocks is not None and scope == "blockAt":
            pos = (int(params["x"]), int(params["y"]), int(params["z"]))
            block_type, state = self.blocks.get(pos, ("air", "CLEAR"))
            return PerceptionResult(
                bot="Bot1",
                scope="blockAt",
                type="perception",
                ok=True,
                complete=True,
                data={"x": pos[0], "y": pos[1], "z": pos[2], "type": block_type, "state": state},
            )
        return self.perception

    def execute(self, action: Action) -> Result:
        self.actions.append(action)
        if self.blocks is not None:
            if action.name == "placeBlock":
                target = tuple(action.params["target"])
                self.blocks[target] = (str(action.params["block_type"]), "SOLID")
                self.terminal = Event(
                    seq=self.terminal.seq,
                    tick=self.terminal.tick,
                    bot=self.terminal.bot,
                    name="placeDone",
                    data={
                        "action_id": action.id,
                        "success": True,
                        "block_at_target": action.params["block_type"],
                    },
                )
            elif action.name == "mineBlock":
                target = tuple(action.params["target"])
                self.blocks[target] = ("air", "CLEAR")
                self.terminal = Event(
                    seq=self.terminal.seq,
                    tick=self.terminal.tick,
                    bot=self.terminal.bot,
                    name="mineDone",
                    data={"action_id": action.id, "success": True, "block_gone": True},
                )
                return Result(
                    id=action.id,
                    bot="Bot1",
                    type="result",
                    ok=True,
                    accepted=True,
                    complete=True,
                    data={"action": action.name},
                )
            elif action.name == "selectItem":
                self.terminal = Event(
                    seq=self.terminal.seq,
                    tick=self.terminal.tick,
                    bot=self.terminal.bot,
                    name="selectItemDone",
                    data={
                        "action_id": action.id,
                        "success": True,
                        "item": action.params["item"],
                        "slot": 0,
                        "count": 1,
                        "stopped_reason": "completed",
                    },
                )
                return Result(
                    id=action.id,
                    bot="Bot1",
                    type="result",
                    ok=True,
                    accepted=True,
                    complete=True,
                    data={"action": action.name},
                )
            elif action.name == "moveTo":
                target = tuple(action.params["target"])
                self.state_pos = (float(target[0]), float(target[1]), float(target[2]))
                self.terminal = Event(
                    seq=self.terminal.seq,
                    tick=self.terminal.tick,
                    bot=self.terminal.bot,
                    name="moveDone",
                    data={
                        "action_id": action.id,
                        "arrived": True,
                        "final_pos": list(self.state_pos),
                        "target": list(target),
                        "stopped_reason": "arrived",
                    },
                )
                return Result(
                    id=action.id,
                    bot="Bot1",
                    type="result",
                    ok=True,
                    accepted=True,
                    complete=True,
                    data={"action": action.name},
                )
            elif action.name == "jump":
                self.terminal = Event(
                    seq=self.terminal.seq,
                    tick=self.terminal.tick,
                    bot=self.terminal.bot,
                    name="jumpDone",
                    data={
                        "action_id": action.id,
                        "success": True,
                        "final_pos": list(self.state_pos),
                        "stopped_reason": "completed",
                    },
                )
                return Result(
                    id=action.id,
                    bot="Bot1",
                    type="result",
                    ok=True,
                    accepted=True,
                    complete=True,
                    data={"action": action.name},
                )
            return Result(
                id=action.id,
                bot="Bot1",
                type="result",
                ok=True,
                accepted=True,
                complete=True,
                data={"action": action.name},
            )
        if action.name == "selectItem":
            self.terminal = Event(
                seq=self.terminal.seq,
                tick=self.terminal.tick,
                bot=self.terminal.bot,
                name="selectItemDone",
                data={
                    "action_id": action.id,
                    "success": True,
                    "item": action.params["item"],
                    "slot": 0,
                    "count": 1,
                    "stopped_reason": "completed",
                },
            )
            return Result(
                id=action.id,
                bot="Bot1",
                type="result",
                ok=True,
                accepted=True,
                complete=True,
                data={"action": action.name},
            )
        self.terminal = Event(
            seq=self.terminal.seq,
            tick=self.terminal.tick,
            bot=self.terminal.bot,
            name=self.terminal.name,
            data={**self.terminal.data, "action_id": action.id},
        )
        return Result(
            id=action.id,
            bot="Bot1",
            type="result",
            ok=True,
            accepted=True,
            complete=True,
            data={"action": action.name},
        )

    def get_state(self) -> BodyState:
        return BodyState(
            bot="Bot1",
            pos=self.state_pos,
            yaw=None,
            pitch=None,
            health=20.0,
            food=20,
            oxygen=None,
            inventory_raw="[]",
            inventory_hash="hash",
            effects=None,
            time=0,
            weather=None,
            dimension="overworld",
            complete=True,
        )

    def poll_events(self) -> list[Event]:
        return []

    def await_action_terminal(self, action_id: str, timeout_s: float = 15.0) -> Event:
        if self.terminal.data.get("action_id") != action_id:
            raise AssertionError("terminal action id mismatch")
        return self.terminal


class FakeNavigator:
    def __init__(self, result: bool = True, reason: str = "arrived") -> None:
        self.result = result
        self.reason = reason
        self.calls: list[tuple[object, dict[str, object]]] = []
        self.body: FakeBody | None = None

    def navigate_to(self, goal, **kwargs):
        self.calls.append((goal, kwargs))
        selected = goal_position(goal)
        if self.result and self.body is not None:
            self.body.state_pos = (float(selected[0]), float(selected[1]), float(selected[2]))
        return ToolResult(
            success=self.result,
            reason=self.reason,
            can_retry=not self.result,
            metrics={"goal": list(selected), "selected_goal": list(selected), "kwargs": kwargs},
        )


class FallingBody(FakeBody):
    def get_state(self) -> BodyState:
        if self.blocks is not None:
            below = (
                int(self.state_pos[0]),
                int(self.state_pos[1]) - 1,
                int(self.state_pos[2]),
            )
            if self.blocks.get(below, ("air", "CLEAR"))[1] == "CLEAR":
                self.state_pos = (self.state_pos[0], self.state_pos[1] - 1.0, self.state_pos[2])
        return super().get_state()


class EdgeSupportedFallingBody(FakeBody):
    def get_state(self) -> BodyState:
        if self.blocks is not None:
            feet = (
                int(self.state_pos[0] // 1),
                int(self.state_pos[1] // 1),
                int(self.state_pos[2] // 1),
            )
            below = (feet[0], feet[1] - 1, feet[2])
            within_shaft = (
                feet[0] + 0.32 <= self.state_pos[0] <= feet[0] + 0.68
                and feet[2] + 0.32 <= self.state_pos[2] <= feet[2] + 0.68
            )
            if within_shaft and self.blocks.get(below, ("air", "CLEAR"))[1] == "CLEAR":
                self.state_pos = (self.state_pos[0], self.state_pos[1] - 1.0, self.state_pos[2])
        return super().get_state()


class FallProbeBatchFailureBody(FallingBody):
    def perceive(self, scope: str, params: dict[str, object]) -> PerceptionResult:
        self.perceptions.append((scope, params))
        if scope == "blockCells":
            return PerceptionResult(
                bot="Bot1",
                scope="blockCells",
                type="perception",
                ok=False,
                complete=False,
                data={"cells": []},
                uncertainty=[{"reason": "synthetic_deeper_cell_failure"}],
                next=None,
                error="synthetic_deeper_cell_failure",
            )
        if scope == "blockAt":
            pos = (int(params["x"]), int(params["y"]), int(params["z"]))
            block_type, state = self.blocks.get(pos, ("air", "CLEAR"))
            return PerceptionResult(
                bot="Bot1",
                scope="blockAt",
                type="perception",
                ok=True,
                complete=True,
                data={"x": pos[0], "y": pos[1], "z": pos[2], "type": block_type, "state": state},
            )
        return super().perceive(scope, params)


class ScopeBatchFailureBody(FakeBody):
    def __init__(self, *args, failed_scope: str, **kwargs):
        super().__init__(*args, **kwargs)
        self.failed_scope = failed_scope

    def perceive(self, scope: str, params: dict[str, object]) -> PerceptionResult:
        if scope == "blockCells":
            cells = params.get("cells") or []
            if cells:
                sample_y = int(cells[0][1])
                label = self._batch_label(sample_y, len(cells))
                if label == self.failed_scope:
                    self.perceptions.append((scope, params))
                    return PerceptionResult(
                        bot="Bot1",
                        scope="blockCells",
                        type="perception",
                        ok=False,
                        complete=False,
                        data={"cells": []},
                        uncertainty=[{"reason": f"synthetic_{label}_failure"}],
                        next=None,
                        error=f"synthetic_{label}_failure",
                    )
        return super().perceive(scope, params)

    @staticmethod
    def _batch_label(sample_y: int, count: int) -> str:
        if count == 3:
            return "surface_candidate"
        if count > 3 and sample_y == 65:
            return "surface_column"
        if count > 1 and sample_y >= 66:
            return "sky_exposure"
        return "other"


class BlockWorkTests(unittest.TestCase):
    def test_surface_lateral_sampling_keeps_outer_ring_four_block_coverage(self):
        columns = _surface_lateral_columns((0, 64, 0), ((16, 8), (24, 16), (32, 64)))

        self.assertEqual(len(columns), 88)
        sampled = {(entry["x"], entry["z"]) for entry in columns}
        self.assertIn((-28, -32), sampled)
        self.assertIn((32, 28), sampled)

    def test_mine_block_approach_uses_feet_y_above_target_floor(self):
        blocks = {
            (0, 64, 0): ("minecraft:stone", "SOLID"),
            (1, 65, 0): ("minecraft:air", "CLEAR"),
            (1, 66, 0): ("minecraft:air", "CLEAR"),
            (1, 64, 0): ("minecraft:grass_block", "SOLID"),
        }
        body = FakeBody(blocks=blocks)
        body.state_pos = (4.5, 65.0, 0.5)
        navigator = FakeNavigator()
        navigator.body = body
        work = BlockWork(
            body,
            GovernancePolicy(natural_regions=[Region("mine", (-10, 0, -10), (10, 100, 10))]),
            navigator=navigator,
        )

        result = work.mine_block((0, 64, 0), context=BreakContext.TRAVEL)

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual([action.name for action in body.actions], ["mineBlock"])
        self.assertEqual(len(navigator.calls), 1)
        self.assertIsInstance(navigator.calls[0][0], GoalComposite)
        self.assertEqual(result.metrics["mine_approach"]["selected_goal"][1], 65)

    def test_search_for_block_requires_filter(self):
        body = FakeBody()
        work = BlockWork(body, GovernancePolicy(natural_regions=[Region("search", (-10, 0, -10), (10, 100, 10))]))

        result = work.search_for_block(block_types=())

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "search_block_filter_missing")

    def test_search_for_block_reports_not_found(self):
        body = FakeBody(find_blocks=[])
        work = BlockWork(body, GovernancePolicy(natural_regions=[Region("search", (-10, 0, -10), (10, 100, 10))]))

        result = work.search_for_block(block_types=("oak_log",), search_radius=8)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "search_block_not_found")

    def test_search_for_block_returns_distant_candidates_without_navigation(self):
        blocks = {
            (8, 64, 0): ("minecraft:oak_log", "SOLID"),
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
        body = FakeBody(
            blocks=blocks,
            find_blocks=[{"x": 8, "y": 64, "z": 0, "type": "minecraft:oak_log"}],
        )
        work = BlockWork(body, GovernancePolicy(natural_regions=[Region("search", (-20, 0, -20), (20, 100, 20))]))

        result = work.search_for_block(block_types=("oak_log",), search_radius=12)

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(result.reason, "block_candidates_found")
        self.assertEqual(result.metrics["target"]["pos"], [8, 64, 0])
        self.assertEqual(result.metrics["candidates"][0]["pos"], [8, 64, 0])
        self.assertFalse(result.metrics["range_verified"])
        self.assertFalse(result.metrics["line_of_sight_verified"])
        self.assertEqual(result.metrics["interaction_readiness"], "unknown")
        self.assertIn("collect_resource", result.next_suggestion)
        self.assertIn("get_to_block", result.next_suggestion)
        self.assertEqual(body.actions, [])

    def test_search_for_block_in_range_does_not_claim_interaction_readiness(self):
        body = FakeBody(
            find_blocks=[{"x": 3, "y": 64, "z": 0, "type": "minecraft:oak_log"}],
        )
        work = BlockWork(
            body,
            GovernancePolicy(natural_regions=[Region("search", (-20, 0, -20), (20, 100, 20))]),
        )

        result = work.search_for_block(block_types=("oak_log",), search_radius=12)

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(result.reason, "block_in_range")
        self.assertTrue(result.metrics["range_verified"])
        self.assertFalse(result.metrics["line_of_sight_verified"])
        self.assertEqual(result.metrics["interaction_readiness"], "unknown")
        self.assertIn("Distance alone is not interaction truth", result.next_suggestion)
        self.assertEqual(body.actions, [])

    def test_search_for_block_does_not_navigate_even_when_navigator_is_available(self):
        blocks = {
            (8, 64, 0): ("minecraft:oak_log", "SOLID"),
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
        body = FakeBody(
            blocks=blocks,
            find_blocks=[{"x": 8, "y": 64, "z": 0, "type": "minecraft:oak_log"}],
        )
        navigator = FakeNavigator()
        navigator.body = body
        work = BlockWork(
            body,
            GovernancePolicy(natural_regions=[Region("search", (-20, 0, -20), (20, 100, 20))]),
            navigator=navigator,
        )

        result = work.search_for_block(block_types=("oak_log",), search_radius=12)

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(result.reason, "block_candidates_found")
        self.assertEqual(result.metrics["target"]["type"], "oak_log")
        self.assertGreater(result.metrics["final_distance"], 4.5)
        self.assertEqual(navigator.calls, [])

    def test_search_for_block_accepts_truncated_find_blocks_when_candidates_exist(self):
        blocks = {
            (4, 64, 0): ("minecraft:oak_log", "SOLID"),
            (5, 64, 0): ("minecraft:air", "CLEAR"),
            (5, 65, 0): ("minecraft:air", "CLEAR"),
            (5, 63, 0): ("minecraft:stone", "SOLID"),
        }
        body = FakeBody(
            blocks=blocks,
            find_blocks=[{"x": 4, "y": 64, "z": 0, "type": "minecraft:oak_log"}],
            find_blocks_complete=False,
            find_blocks_uncertainty=[{"reason": "limit_exceeded"}],
        )
        navigator = FakeNavigator()
        navigator.body = body
        work = BlockWork(
            body,
            GovernancePolicy(natural_regions=[Region("search", (-20, 0, -20), (20, 100, 20))]),
            navigator=navigator,
        )

        result = work.search_for_block(block_types=("oak_log",), search_radius=12)

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(result.reason, "block_candidates_found")
        self.assertTrue(result.metrics["truncated"])
        self.assertEqual(result.metrics["uncertainty"], [{"reason": "limit_exceeded"}])

    def test_search_for_block_returns_all_candidates_without_candidate_navigation(self):
        blocks = {
            (4, 64, 0): ("minecraft:oak_log", "SOLID"),
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
            (8, 64, 0): ("minecraft:oak_log", "SOLID"),
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
        body = FakeBody(
            blocks=blocks,
            find_blocks=[
                {"x": 4, "y": 64, "z": 0, "type": "minecraft:oak_log"},
                {"x": 8, "y": 64, "z": 0, "type": "minecraft:oak_log"},
            ],
        )

        class FirstCandidateFailsNavigator(FakeNavigator):
            def navigate_to(self, goal, **kwargs):
                self.calls.append((goal, kwargs))
                if goal[0] <= 5:
                    return ToolResult(success=False, reason="navigation_blocked:no_path", can_retry=True)
                if self.body is not None:
                    selected = goal_position(goal)
                    self.body.state_pos = (float(selected[0]), float(selected[1]), float(selected[2]))
                return ToolResult(success=True, reason="arrived", can_retry=False, metrics={"goal": list(goal)})

        navigator = FirstCandidateFailsNavigator()
        navigator.body = body
        work = BlockWork(
            body,
            GovernancePolicy(natural_regions=[Region("search", (-20, 0, -20), (20, 100, 20))]),
            navigator=navigator,
        )

        result = work.search_for_block(block_types=("oak_log",), search_radius=12)

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(result.reason, "block_candidates_found")
        self.assertEqual(result.metrics["target"]["pos"], [4, 64, 0])
        self.assertEqual([item["pos"] for item in result.metrics["candidates"]], [[4, 64, 0], [8, 64, 0]])
        self.assertEqual(navigator.calls, [])

    def test_search_for_block_read_only_does_not_feed_failure_storm(self):
        from minebot.brain.progress import ProgressAuthority

        blocks: dict[tuple[int, int, int], tuple[str, str]] = {}
        find_blocks = []
        for x in (4, 8, 12, 16, 20):
            blocks[(x, 64, 0)] = ("minecraft:oak_log", "SOLID")
            find_blocks.append({"x": x, "y": 64, "z": 0, "type": "minecraft:oak_log"})
            for point in ((x + 1, 64, 0), (x - 1, 64, 0), (x, 64, 1), (x, 64, -1)):
                blocks[point] = ("minecraft:air", "CLEAR")
                blocks[(point[0], point[1] + 1, point[2])] = ("minecraft:air", "CLEAR")
                blocks[(point[0], point[1] - 1, point[2])] = ("minecraft:stone", "SOLID")
        body = FakeBody(blocks=blocks, find_blocks=find_blocks)
        progress = ProgressAuthority()

        class AlwaysStuckNavigator:
            def __init__(self):
                self.calls = []

            def navigate_to(self, goal, **kwargs):
                self.calls.append((goal, kwargs))
                return ToolResult(False, "stuck", True, metrics={"goal": list(goal_position(goal))})

        navigator = AlwaysStuckNavigator()
        work = BlockWork(
            body,
            GovernancePolicy(natural_regions=[Region("search", (-32, 0, -32), (32, 100, 32))]),
            navigator=navigator,
        )

        result = work.search_for_block(block_types=("oak_log",), search_radius=24, find_limit=5)

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "block_candidates_found")
        self.assertEqual(navigator.calls, [])
        self.assertEqual(progress.failure_steps, 0)
        progress.require_can_continue("next independent move")

    def test_search_for_block_does_not_chase_extra_find_blocks_pages_by_default(self):
        blocks = {
            (4, 64, 0): ("minecraft:oak_log", "SOLID"),
            (5, 64, 0): ("minecraft:air", "CLEAR"),
            (5, 65, 0): ("minecraft:air", "CLEAR"),
            (5, 63, 0): ("minecraft:stone", "SOLID"),
            (8, 64, 0): ("minecraft:oak_log", "SOLID"),
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
        pages = [
            PerceptionResult(
                "Bot1",
                "findBlocks",
                "perception",
                True,
                False,
                {
                    "blocks": [{"x": 4, "y": 64, "z": 0, "type": "minecraft:oak_log"}],
                    "totalMatches": 2,
                },
                uncertainty=[{"reason": "page_limit"}],
                next="1",
            ),
            PerceptionResult(
                "Bot1",
                "findBlocks",
                "perception",
                True,
                True,
                {
                    "blocks": [{"x": 8, "y": 64, "z": 0, "type": "minecraft:oak_log"}],
                    "nextStart": None,
                    "totalMatches": 2,
                },
                uncertainty=[],
            ),
            PerceptionResult("Bot1", "findBlocks", "perception", True, True, {"blocks": [], "totalMatches": 2}),
        ]
        body = FakeBody(blocks=blocks, find_block_pages=pages)

        class FirstCandidateFailsNavigator(FakeNavigator):
            def navigate_to(self, goal, **kwargs):
                self.calls.append((goal, kwargs))
                if goal[0] <= 5:
                    return ToolResult(success=False, reason="navigation_blocked:no_path", can_retry=True)
                if self.body is not None:
                    selected = goal_position(goal)
                    self.body.state_pos = (float(selected[0]), float(selected[1]), float(selected[2]))
                return ToolResult(success=True, reason="arrived", can_retry=False, metrics={"goal": list(goal)})

        navigator = FirstCandidateFailsNavigator()
        navigator.body = body
        work = BlockWork(
            body,
            GovernancePolicy(natural_regions=[Region("search", (-20, 0, -20), (20, 100, 20))]),
            navigator=navigator,
        )

        result = work.search_for_block(block_types=("oak_log",), search_radius=12, find_limit=1)

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(result.reason, "block_candidates_found")
        find_calls = [params for scope, params in body.perceptions if scope == "findBlocks"]
        self.assertEqual(len(find_calls), 1)
        self.assertEqual(find_calls[0]["start"], 0)
        self.assertEqual(find_calls[0]["y_radius"], 6)
        self.assertEqual(result.metrics["pages_read"], 1)
        self.assertEqual(result.metrics["total_matches"], 2)
        self.assertTrue(result.metrics["truncated"])

    def test_search_for_block_can_chase_bounded_extra_find_blocks_pages_when_requested(self):
        blocks = {
            (4, 64, 0): ("minecraft:oak_log", "SOLID"),
            (8, 64, 0): ("minecraft:oak_log", "SOLID"),
        }
        pages = [
            PerceptionResult(
                "Bot1",
                "findBlocks",
                "perception",
                True,
                False,
                {
                    "blocks": [{"x": 4, "y": 64, "z": 0, "type": "minecraft:oak_log"}],
                    "totalMatches": 2,
                },
                uncertainty=[],
                next="1",
            ),
            PerceptionResult(
                "Bot1",
                "findBlocks",
                "perception",
                True,
                True,
                {
                    "blocks": [{"x": 8, "y": 64, "z": 0, "type": "minecraft:oak_log"}],
                    "totalMatches": 2,
                },
            ),
        ]
        body = FakeBody(blocks=blocks, find_block_pages=pages)
        work = BlockWork(
            body,
            GovernancePolicy(natural_regions=[Region("search", (-20, 0, -20), (20, 100, 20))]),
        )

        result = work.search_for_block(block_types=("oak_log",), search_radius=12, find_limit=1, max_pages=2)

        self.assertTrue(result.success, result.to_payload())
        find_calls = [params for scope, params in body.perceptions if scope == "findBlocks"]
        self.assertEqual([call["start"] for call in find_calls], [0, 1])
        self.assertEqual(result.metrics["pages_read"], 2)
        self.assertFalse(result.metrics["truncated"])
        self.assertEqual([item["pos"] for item in result.metrics["candidates"]], [[4, 64, 0], [8, 64, 0]])

    def test_search_for_block_caps_find_blocks_vertical_window_for_large_radius(self):
        body = FakeBody(
            blocks={(4, 64, 0): ("minecraft:oak_log", "SOLID")},
            find_blocks=[{"x": 4, "y": 64, "z": 0, "type": "minecraft:oak_log"}],
        )
        work = BlockWork(
            body,
            GovernancePolicy(natural_regions=[Region("search", (-100, 0, -100), (100, 140, 100))]),
        )

        result = work.search_for_block(block_types=("oak_log",), search_radius=64)

        self.assertTrue(result.success, result.to_payload())
        find_calls = [params for scope, params in body.perceptions if scope == "findBlocks"]
        self.assertEqual(find_calls[0]["radius"], 64)
        self.assertEqual(find_calls[0]["y_radius"], 16)

    def test_search_for_block_does_not_refresh_or_report_target_lost_by_moving(self):
        blocks = {
            (8, 64, 0): ("minecraft:oak_log", "SOLID"),
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
        body = FakeBody(
            blocks=blocks,
            find_blocks=[{"x": 8, "y": 64, "z": 0, "type": "minecraft:oak_log"}],
        )
        class VanishingTargetNavigator(FakeNavigator):
            def navigate_to(self, goal, **kwargs):
                result = super().navigate_to(goal, **kwargs)
                body.blocks[(8, 64, 0)] = ("minecraft:air", "CLEAR")
                return result

        navigator = VanishingTargetNavigator()
        navigator.body = body
        work = BlockWork(
            body,
            GovernancePolicy(natural_regions=[Region("search", (-20, 0, -20), (20, 100, 20))]),
            navigator=navigator,
        )

        result = work.search_for_block(block_types=("oak_log",), search_radius=12)

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "block_candidates_found")
        self.assertEqual(navigator.calls, [])

    def test_mine_block_denies_unknown_provenance_without_executing_action(self):
        body = FakeBody(
            PerceptionResult(
                bot="Bot1",
                scope="blockAt",
                type="perception",
                ok=True,
                complete=True,
                data={"x": 100, "y": 64, "z": 100, "type": "stone", "state": "SOLID"},
            )
        )
        runtime = BlockWork(body, GovernancePolicy())

        result = runtime.mine_block((100, 64, 100), context=BreakContext.TRAVEL)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "break_denied:unknown_provenance")
        self.assertEqual(body.actions, [])
        self.assertEqual(result.metrics["legality"]["allowed"], False)

    def test_mine_block_executes_when_governance_allows_collect_target(self):
        body = FakeBody(
            PerceptionResult(
                bot="Bot1",
                scope="blockAt",
                type="perception",
                ok=True,
                complete=True,
                data={"x": 0, "y": 64, "z": 0, "type": "diamond_ore", "state": "SOLID"},
            )
        )
        policy = GovernancePolicy(natural_regions=[Region("mine", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy)

        result = runtime.mine_block((0, 64, 0), context=BreakContext.COLLECT, timeout_s=1.0)

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(len(body.actions), 1)
        self.assertEqual(body.actions[0].name, "mineBlock")
        self.assertEqual(body.actions[0].params["target"], [0, 64, 0])
        self.assertEqual(body.actions[0].params["context"], "collect")
        self.assertEqual(body.actions[0].params["timeout_ticks"], 20)
        self.assertEqual(body.actions[0].params["legality"]["reason"], "allowed_natural")

    def test_mine_block_approaches_target_before_mining_when_out_of_reach(self):
        body = FakeBody(
            blocks={(0, 64, 6): ("stone", "SOLID")},
        )
        body.state_pos = (0.5, 65.0, 0.5)
        settled: list[float] = []
        policy = GovernancePolicy(natural_regions=[Region("mine", (-10, 0, -10), (10, 100, 10))])
        navigator = FakeNavigator()
        navigator.body = body
        runtime = BlockWork(body, policy, navigator=navigator, settle=settled.append)

        result = runtime.mine_block((0, 64, 6), context=BreakContext.TRAVEL, timeout_s=1.0)

        self.assertTrue(result.success)
        self.assertEqual([action.name for action in body.actions], ["mineBlock"])
        self.assertEqual(len(navigator.calls), 1)
        self.assertIsInstance(navigator.calls[0][0], GoalComposite)
        self.assertEqual(result.metrics["mine_approach"]["selected_goal"], [0, 65, 5])
        self.assertEqual(settled, [0.3])

    def test_mine_block_collect_does_not_over_approach_inside_interaction_range(self):
        blocks = {
            (0, 66, 2): ("spruce_log", "SOLID"),
        }
        body = FakeBody(
            blocks=blocks,
            inventory_pages=[
                inventory_page([slot(9, "minecraft:spruce_log", 0)]),
                inventory_page([slot(9, "minecraft:spruce_log", 1)]),
            ],
        )
        body.state_pos = (0.5, 64.0, 0.5)
        policy = GovernancePolicy(natural_regions=[Region("mine", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy)

        result = runtime.mine_block_collect((0, 66, 2), context=BreakContext.COLLECT, timeout_s=1.0)

        self.assertTrue(result.success, result)
        self.assertEqual([action.name for action in body.actions], ["mineBlock"])
        self.assertNotIn("mine_approach", result.metrics["mine_result"]["metrics"])

    def test_mine_block_collect_classifies_unreachable_approach_as_candidate_skip(self):
        from minebot.contract import is_candidate_skip

        body = FakeBody(blocks={(0, 64, 5): ("dirt", "SOLID")})
        body.state_pos = (0.5, 65.0, 0.5)
        policy = GovernancePolicy(natural_regions=[Region("mine", (-10, 0, -10), (10, 100, 10))])
        navigator = FakeNavigator(result=False, reason="stuck")
        runtime = BlockWork(body, policy, navigator=navigator)

        result = runtime.mine_block_collect((0, 64, 5), context=BreakContext.COLLECT, timeout_s=1.0)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "mine_approach_failed:stuck")
        self.assertTrue(is_candidate_skip(result.reason))
        self.assertEqual(len(navigator.calls), 1)
        self.assertIsInstance(navigator.calls[0][0], GoalComposite)
        self.assertNotIn("mineBlock", [action.name for action in body.actions])

    def test_mine_block_collect_uses_governed_goal_set_break_edge_for_buried_stand(self):
        class GovernedBreakNavigator(FakeNavigator):
            def navigate_to(self, goal, **kwargs):
                self.calls.append((goal, kwargs))
                selected = goal_position(goal)
                self.body.blocks[selected] = ("air", "CLEAR")
                self.body.state_pos = (float(selected[0]), float(selected[1]), float(selected[2]))
                return ToolResult(
                    success=True,
                    reason="arrived",
                    can_retry=False,
                    metrics={
                        "goal": list(selected),
                        "selected_goal": list(selected),
                        "movement_counts": {"break": 1},
                    },
                )

        body = FakeBody(
            blocks={
                (0, 64, 5): ("dirt", "SOLID"),
                (0, 65, 4): ("stone", "SOLID"),
            },
            inventory_pages=[
                inventory_page([slot(9, "minecraft:dirt", 0)]),
                inventory_page([slot(9, "minecraft:dirt", 1)]),
            ],
        )
        body.state_pos = (0.5, 65.0, 0.5)
        navigator = GovernedBreakNavigator()
        navigator.body = body
        policy = GovernancePolicy(natural_regions=[Region("mine", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy, navigator=navigator)

        result = runtime.mine_block_collect((0, 64, 5), context=BreakContext.COLLECT, timeout_s=1.0)

        self.assertEqual(len(navigator.calls), 1)
        self.assertIsInstance(navigator.calls[0][0], GoalComposite)
        nav_kwargs = navigator.calls[-1][1]
        self.assertEqual(nav_kwargs["break_context"], BreakContext.COLLECT_APPROACH)
        nav_config = nav_kwargs["config"]
        self.assertEqual(nav_config.max_break_steps, BlockWork.MINE_APPROACH_MAX_BREAK_STEPS)
        self.assertEqual(body.blocks[(0, 65, 4)], ("air", "CLEAR"))
        mine_actions = [action for action in body.actions if action.name == "mineBlock"]
        self.assertEqual([action.params["target"] for action in mine_actions], [[0, 64, 5]])
        mine_result = result.metrics["mine_result"]["metrics"]["mine_approach"]
        self.assertEqual(mine_result["selected_goal"], [0, 65, 4])
        self.assertEqual(mine_result["navigation_result"]["metrics"]["movement_counts"]["break"], 1)
        self.assertTrue(result.success, result)
        self.assertEqual(result.reason, "collected")

    def test_mine_block_collect_skips_leaf_blocked_stand_candidate(self):
        class AlternateStandNavigator(FakeNavigator):
            def navigate_to(self, goal, **kwargs):
                self.calls.append((goal, kwargs))
                selected = (-1, 65, 5)
                self.body.state_pos = (float(selected[0]), float(selected[1]), float(selected[2]))
                return ToolResult(
                    success=True,
                    reason="arrived",
                    can_retry=False,
                    metrics={"goal": list(selected), "selected_goal": list(selected)},
                )

        body = FakeBody(
            blocks={
                (0, 64, 5): ("dirt", "SOLID"),
                (0, 65, 4): ("spruce_leaves", "SOLID"),
                (0, 66, 4): ("air", "CLEAR"),
                (0, 64, 4): ("stone", "SOLID"),
                (0, 65, 6): ("stone", "SOLID"),
                (0, 66, 6): ("air", "CLEAR"),
                (0, 64, 6): ("stone", "SOLID"),
                (-1, 65, 5): ("stone", "SOLID"),
                (-1, 66, 5): ("air", "CLEAR"),
                (-1, 64, 5): ("stone", "SOLID"),
                (1, 65, 5): ("stone", "SOLID"),
                (1, 66, 5): ("air", "CLEAR"),
                (1, 64, 5): ("stone", "SOLID"),
            },
            inventory_pages=[
                inventory_page([slot(9, "minecraft:dirt", 0)]),
                inventory_page([slot(9, "minecraft:dirt", 1)]),
            ],
        )
        body.state_pos = (0.5, 65.0, 0.5)
        navigator = AlternateStandNavigator()
        navigator.body = body
        policy = GovernancePolicy(natural_regions=[Region("mine", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy, navigator=navigator)

        result = runtime.mine_block_collect((0, 64, 5), context=BreakContext.COLLECT, timeout_s=1.0)

        self.assertTrue(result.success, result)
        approach = result.metrics["mine_result"]["metrics"]["mine_approach"]
        self.assertEqual(len(navigator.calls), 1)
        self.assertIsInstance(navigator.calls[0][0], GoalComposite)
        goals = [child.pos for child in navigator.calls[0][0].goals]
        self.assertIn((0, 65, 4), goals)
        self.assertIn((-1, 65, 5), goals)
        self.assertEqual(approach["selected_goal"], [-1, 65, 5])
        self.assertEqual(body.blocks[(0, 65, 4)], ("spruce_leaves", "SOLID"))
        cleared_targets = [
            action.params["target"]
            for action in body.actions
            if action.name == "mineBlock" and action.params.get("context") == BreakContext.COLLECT_APPROACH.value
        ]
        self.assertEqual(cleared_targets, [])

    def test_mine_block_collect_disables_local_terrain_fallback_for_goal_set_navigation(self):
        class StuckThenNavBody(FakeBody):
            def execute(self, action: Action) -> Result:
                result = super().execute(action)
                if action.name == "moveTo":
                    self.terminal = Event(
                        seq=self.terminal.seq,
                        tick=self.terminal.tick,
                        bot=self.terminal.bot,
                        name="moveDone",
                        data={
                            "action_id": action.id,
                            "arrived": False,
                            "final_pos": list(self.state_pos),
                            "target": list(action.params["target"]),
                            "stopped_reason": "stuck",
                        },
                    )
                return result

        body = StuckThenNavBody(
            blocks={
                (0, 64, 5): ("oak_log", "SOLID"),
                (0, 65, 4): ("air", "CLEAR"),
            },
            inventory_pages=[
                inventory_page([slot(9, "minecraft:oak_log", 0)]),
                inventory_page([slot(9, "minecraft:oak_log", 0)]),
            ],
        )
        body.state_pos = (0.5, 65.0, 0.5)
        navigator = FakeNavigator(result=False, reason="no_path")
        navigator.body = body
        policy = GovernancePolicy(natural_regions=[Region("mine", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy, navigator=navigator)

        result = runtime.mine_block_collect((0, 64, 5), context=BreakContext.COLLECT, timeout_s=1.0)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "mine_approach_failed:no_path")
        self.assertTrue(navigator.calls)
        nav_config = navigator.calls[-1][1]["config"]



    def test_mine_block_collect_goal_set_failure_is_not_progress_neutral(self):
        body = FakeBody(
            blocks={
                (0, 64, 5): ("dirt", "SOLID"),
                (0, 65, 4): ("air", "CLEAR"),
            },
        )
        body.state_pos = (0.5, 65.0, 0.5)
        navigator = FakeNavigator(result=False, reason="stuck")
        policy = GovernancePolicy(natural_regions=[Region("mine", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy, navigator=navigator)

        result = runtime.mine_block_collect((0, 64, 5), context=BreakContext.COLLECT, timeout_s=1.0)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "mine_approach_failed:stuck")
        self.assertEqual(len(navigator.calls), 1)

    def test_mine_block_approach_uses_feet_level_stand_for_headroom_target(self):
        body = FakeBody(
            blocks={
                (2, 65, 0): ("dirt", "SOLID"),
                (2, 64, -1): ("air", "CLEAR"),
                (2, 63, -1): ("stone", "SOLID"),
                (2, 65, -1): ("air", "CLEAR"),
            },
        )
        body.state_pos = (0.5, 64.0, 0.5)
        policy = GovernancePolicy(natural_regions=[Region("work", (-10, 0, -10), (10, 100, 10))])
        navigator = FakeNavigator()
        navigator.body = body
        runtime = BlockWork(body, policy, navigator=navigator)

        result = runtime.mine_block((2, 65, 0), context=BreakContext.DIRECT)

        self.assertTrue(result.success)
        self.assertEqual([action.name for action in body.actions], ["mineBlock"])
        self.assertEqual(result.metrics["mine_approach"]["selected_goal"], [2, 64, -1])

    def test_mine_block_does_not_execute_when_perception_incomplete(self):
        body = FakeBody(
            PerceptionResult(
                bot="Bot1",
                scope="blockAt",
                type="perception",
                ok=True,
                complete=False,
                data={},
                uncertainty=[{"reason": "limit_exceeded"}],
            )
        )
        runtime = BlockWork(body, GovernancePolicy())

        result = runtime.mine_block((0, 64, 0))

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "perception_failed")
        self.assertEqual(body.actions, [])

    def test_mine_block_dry_mines_ore_without_liquid_faces(self):
        blocks = {
            (0, 64, 0): ("diamond_ore", "SOLID"),
        }
        body = FakeBody(blocks=blocks)
        policy = GovernancePolicy(natural_regions=[Region("mine", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy)

        result = runtime.mine_block_dry((0, 64, 0), timeout_s=1.0)

        self.assertTrue(result.success)
        self.assertEqual([action.name for action in body.actions], ["mineBlock"])
        self.assertEqual(body.actions[0].params["context"], "collect")
        self.assertEqual(result.metrics["dry_mining"]["initial_liquid_faces"], 0)

    def test_mine_block_dry_refuses_too_many_liquid_faces_without_mutation(self):
        blocks = {
            (0, 64, 0): ("diamond_ore", "SOLID"),
            (1, 64, 0): ("water", "LIQUID"),
            (-1, 64, 0): ("lava", "LIQUID"),
            (0, 65, 0): ("water", "LIQUID"),
        }
        body = FakeBody(blocks=blocks)
        policy = GovernancePolicy(natural_regions=[Region("mine", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy)

        result = runtime.mine_block_dry((0, 64, 0), max_seal_faces=2)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "dry_mining_too_many_liquid_faces")
        self.assertEqual(body.actions, [])
        self.assertEqual(len(result.metrics["liquid_faces"]), 3)

    def test_mine_block_dry_batches_liquid_face_scan(self):
        class NativeBatchBody(FakeBody):
            def perceive(self, scope: str, params: dict[str, object]) -> PerceptionResult:
                if scope == "blockCells":
                    self.perceptions.append((scope, params))
                    facts = []
                    for c in params.get("cells") or []:
                        pos = (int(c[0]), int(c[1]), int(c[2]))
                        block_type, state = self.blocks.get(pos, ("air", "CLEAR"))
                        facts.append(
                            {
                                "x": pos[0],
                                "y": pos[1],
                                "z": pos[2],
                                "type": block_type,
                                "state": state,
                                "properties": {},
                            }
                        )
                    return PerceptionResult(
                        bot="Bot1",
                        scope="blockCells",
                        type="perception",
                        ok=True,
                        complete=True,
                        data={"count": len(facts), "total": len(facts), "next": None, "cells": facts},
                    )
                return super().perceive(scope, params)

        blocks = {
            (0, 64, 0): ("diamond_ore", "SOLID"),
            (1, 64, 0): ("water", "LIQUID"),
        }
        body = NativeBatchBody(blocks=blocks)
        policy = GovernancePolicy(natural_regions=[Region("mine", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy)

        result = runtime.mine_block_dry((0, 64, 0), max_seal_faces=0)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "dry_mining_too_many_liquid_faces")
        self.assertEqual(result.metrics["liquid_faces"], [[1, 64, 0]])
        block_cells_reads = [params for scope, params in body.perceptions if scope == "blockCells"]
        self.assertGreaterEqual(len(block_cells_reads), 2)
        self.assertCountEqual(
            block_cells_reads[0]["cells"],
            [[0, 64, 0], [0, 65, 0], [0, 63, 0], [1, 64, 0], [-1, 64, 0], [0, 64, 1], [0, 64, -1]],
        )
        self.assertCountEqual(
            block_cells_reads[1]["cells"],
            [[1, 64, 0], [-1, 64, 0], [0, 64, 1], [0, 64, -1], [0, 65, 0], [0, 63, 0]],
        )
        self.assertEqual(
            [params for scope, params in body.perceptions if scope == "blockAt"],
            [{"x": 0, "y": 64, "z": 0}],
        )

    def test_mine_block_dry_seals_liquid_face_then_mines(self):
        blocks = {
            (0, 64, 0): ("diamond_ore", "SOLID"),
            (1, 64, 0): ("water", "LIQUID"),
        }
        settled: list[float] = []
        body = FakeBody(blocks=blocks, inventory_pages=[inventory_page([slot(9, "minecraft:cobblestone", 3)])])
        policy = GovernancePolicy(natural_regions=[Region("mine", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy, settle=settled.append)

        result = runtime.mine_block_dry((0, 64, 0), seal_blocks=("cobblestone",), timeout_s=1.0)

        self.assertTrue(result.success)
        self.assertEqual([action.name for action in body.actions], ["placeBlock", "mineBlock"])
        self.assertEqual(body.actions[0].params["target"], [1, 64, 0])
        self.assertEqual(body.actions[0].params["purpose"], "seal")
        self.assertEqual(body.actions[0].params["replace_liquid"], True)
        self.assertEqual(body.actions[1].params["target"], [0, 64, 0])
        self.assertEqual(settled, [0.2])
        self.assertEqual(result.metrics["dry_mining"]["initial_liquid_faces"], 1)
        self.assertEqual(result.metrics["dry_mining"]["sealed_faces"][0]["pos"], [1, 64, 0])
        self.assertEqual(result.metrics["dry_mining"]["seal_inventory_counts"]["cobblestone"], 3)
        cleanup = policy.can_break((1, 64, 0), "cobblestone", BreakContext.BOT_CLEANUP)
        self.assertTrue(cleanup.allowed)

    def test_mine_block_dry_approaches_seal_face_before_placing_when_enabled(self):
        blocks = {
            (0, 64, 0): ("diamond_ore", "SOLID"),
            (1, 64, 0): ("water", "LIQUID"),
        }
        body = FakeBody(blocks=blocks, inventory_pages=[inventory_page([slot(9, "minecraft:cobblestone", 3)])])
        navigator = FakeNavigator()
        policy = GovernancePolicy(natural_regions=[Region("mine", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy, navigator=navigator)

        result = runtime.mine_block_dry(
            (0, 64, 0),
            seal_blocks=("cobblestone",),
            approach_seal_faces=True,
            timeout_s=1.0,
        )

        self.assertTrue(result.success)
        self.assertEqual(navigator.calls, [((1, 64, 0), {"break_context": BreakContext.TRAVEL})])
        self.assertEqual([action.name for action in body.actions], ["placeBlock", "mineBlock"])

    def test_mine_block_dry_reports_seal_approach_failure_without_placing(self):
        blocks = {
            (0, 64, 0): ("diamond_ore", "SOLID"),
            (1, 64, 0): ("water", "LIQUID"),
        }
        body = FakeBody(blocks=blocks, inventory_pages=[inventory_page([slot(9, "minecraft:cobblestone", 3)])])
        navigator = FakeNavigator(result=False, reason="navigation_blocked:unloaded")
        policy = GovernancePolicy(natural_regions=[Region("mine", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy, navigator=navigator)

        result = runtime.mine_block_dry(
            (0, 64, 0),
            seal_blocks=("cobblestone",),
            approach_seal_faces=True,
            timeout_s=1.0,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "dry_mining_seal_approach_failed")
        self.assertEqual(body.actions, [])
        self.assertEqual(result.metrics["dry_mining"]["failed_face"], [1, 64, 0])
        self.assertEqual(result.metrics["navigation"]["reason"], "navigation_blocked:unloaded")

    def test_mine_block_dry_reports_missing_seal_approach_runtime_when_enabled(self):
        blocks = {
            (0, 64, 0): ("diamond_ore", "SOLID"),
            (1, 64, 0): ("water", "LIQUID"),
        }
        body = FakeBody(blocks=blocks, inventory_pages=[inventory_page([slot(9, "minecraft:cobblestone", 3)])])
        policy = GovernancePolicy(natural_regions=[Region("mine", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy)

        result = runtime.mine_block_dry(
            (0, 64, 0),
            seal_blocks=("cobblestone",),
            approach_seal_faces=True,
            timeout_s=1.0,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "dry_mining_seal_approach_runtime_missing")
        self.assertEqual(body.actions, [])

    def test_mine_block_dry_uses_inventory_available_seal_candidate(self):
        blocks = {
            (0, 64, 0): ("diamond_ore", "SOLID"),
            (1, 64, 0): ("water", "LIQUID"),
        }
        body = FakeBody(
            blocks=blocks,
            inventory_pages=[
                inventory_page([slot(9, "minecraft:dirt", 2)], next_start=46),
                inventory_page([slot(46, "minecraft:netherrack", 1)]),
            ],
        )
        policy = GovernancePolicy(natural_regions=[Region("mine", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy)

        result = runtime.mine_block_dry(
            (0, 64, 0),
            seal_blocks=("cobblestone", "dirt", "netherrack"),
            timeout_s=1.0,
        )

        self.assertTrue(result.success)
        self.assertEqual(body.actions[0].params["block_type"], "dirt")
        self.assertEqual(result.metrics["dry_mining"]["seal_candidates"], ["dirt", "netherrack"])
        self.assertEqual(result.metrics["dry_mining"]["seal_inventory_counts"]["dirt"], 2)

    def test_mine_block_dry_refuses_when_no_inventory_seal_block_is_available(self):
        blocks = {
            (0, 64, 0): ("diamond_ore", "SOLID"),
            (1, 64, 0): ("water", "LIQUID"),
        }
        body = FakeBody(blocks=blocks, inventory_pages=[inventory_page([slot(9, "minecraft:torch", 4)])])
        policy = GovernancePolicy(natural_regions=[Region("mine", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy)

        result = runtime.mine_block_dry((0, 64, 0), seal_blocks=("cobblestone", "dirt"), timeout_s=1.0)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "dry_mining_no_seal_blocks_available")
        self.assertEqual(body.actions, [])
        self.assertEqual(result.metrics["inventory_counts"], {"torch": 4})

    def test_mine_block_dry_reports_when_seal_does_not_make_target_dry(self):
        class LeakyBody(FakeBody):
            def execute(self, action: Action) -> Result:
                result = super().execute(action)
                if action.name == "placeBlock":
                    self.blocks[tuple(action.params["target"])] = ("water", "LIQUID")
                return result

        blocks = {
            (0, 64, 0): ("diamond_ore", "SOLID"),
            (1, 64, 0): ("water", "LIQUID"),
        }
        body = LeakyBody(blocks=blocks, inventory_pages=[inventory_page([slot(9, "minecraft:cobblestone", 3)])])
        policy = GovernancePolicy(natural_regions=[Region("mine", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy)

        result = runtime.mine_block_dry((0, 64, 0), seal_blocks=("cobblestone",), timeout_s=1.0)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "dry_mining_still_liquid_adjacent")
        self.assertEqual([action.name for action in body.actions], ["placeBlock"])
        self.assertEqual(result.metrics["remaining_liquid_contact"], [[1, 64, 0]])

    def test_mine_block_collect_succeeds_only_when_expected_drop_count_increases(self):
        blocks = {
            (0, 64, 0): ("diamond_ore", "SOLID"),
        }
        body = FakeBody(
            blocks=blocks,
            inventory_pages=[
                inv_page_with_pickaxe([slot(9, "minecraft:diamond", 0)]),
                inv_page_with_pickaxe([slot(9, "minecraft:diamond", 0)]),
                inv_page_with_pickaxe([slot(9, "minecraft:diamond", 1)]),
            ],
        )
        policy = GovernancePolicy(natural_regions=[Region("mine", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy)

        result = runtime.mine_block_collect((0, 64, 0), timeout_s=1.0)

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "collected")
        self.assertEqual([action.name for action in body.actions], ["selectItem", "mineBlock"])
        self.assertEqual(body.actions[0].params["item"], "iron_pickaxe")
        self.assertEqual(result.metrics["expected_drops"], ["diamond"])
        self.assertEqual(result.metrics["deltas"], {"diamond": 1})
        self.assertEqual(result.metrics["collected_total"], 1)
        self.assertEqual(result.metrics["mine_result"]["reason"], "mineDone")
        self.assertEqual(result.metrics["tool_gate"]["required_tier"], "iron")

    def test_mine_block_collect_refuses_missing_required_tool_before_breaking(self):
        blocks = {
            (0, 64, 0): ("diamond_ore", "SOLID"),
        }
        body = FakeBody(
            blocks=blocks,
            inventory_pages=[
                inventory_page([slot(9, "minecraft:stone_pickaxe", 1)]),
            ],
        )
        policy = GovernancePolicy(natural_regions=[Region("mine", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy)

        result = runtime.mine_block_collect((0, 64, 0), timeout_s=1.0)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "missing_required_tool")
        self.assertFalse(result.can_retry)
        self.assertEqual(body.actions, [])
        self.assertEqual(blocks[(0, 64, 0)], ("diamond_ore", "SOLID"))
        self.assertEqual(result.metrics["block_type"], "diamond_ore")
        self.assertEqual(result.metrics["required_tier"], "iron")
        self.assertEqual(result.metrics["best_owned"], {"item": "stone_pickaxe", "tier": "stone"})

    def test_mine_block_collect_reports_no_inventory_delta_after_successful_mine(self):
        blocks = {
            (0, 64, 0): ("diamond_ore", "SOLID"),
        }
        body = FakeBody(
            blocks=blocks,
            inventory_pages=[
                inv_page_with_pickaxe([slot(9, "minecraft:diamond", 0)]),
                inv_page_with_pickaxe([slot(9, "minecraft:diamond", 0)]),
                inv_page_with_pickaxe([slot(9, "minecraft:diamond", 0)]),
            ],
        )
        policy = GovernancePolicy(natural_regions=[Region("mine", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy, settle=lambda _s: None)

        result = runtime.mine_block_collect((0, 64, 0), pickup_timeout_s=0.05, timeout_s=1.0)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "collect_no_inventory_delta")
        self.assertTrue(result.can_retry)
        self.assertEqual([action.name for action in body.actions], ["selectItem", "mineBlock"])
        self.assertEqual(result.metrics["expected_drops"], ["diamond"])
        self.assertEqual(result.metrics["deltas"], {"diamond": 0})
        # No navigator wired in, so the assist walk must not have fired.
        self.assertTrue(result.metrics["pickup_assist"]["waited"])
        self.assertFalse(result.metrics["pickup_assist"]["moved"])
        self.assertEqual(result.metrics["pickup_assist"]["plans"], [])

    def test_mine_block_collect_walks_onto_drop_cell_when_pickup_lags(self):
        # The pickup root cause: after mining, the drop often isn't collected
        # yet (vanilla ~0.5s pickup delay + the bot may mine from just outside
        # the ~1-block auto-pickup range). The fix walks onto `pos` — the air
        # cell the drop rests in — then waits again. This test pins that the
        # walk target is `pos` (NOT `pos-1`, which is the SOLID floor and can
        # never be stood in), uses TRAVEL (pure reposition, no digging), and
        # that a delta appearing only AFTER the walk still counts as collected.
        class LagThenCollectNavigator:
            def __init__(self, body: FakeBody) -> None:
                self.body = body
                self.calls: list[tuple] = []

            def navigate_to(self, goal, **kwargs):
                self.calls.append((goal, kwargs))
                selected = goal_position(goal)
                if isinstance(goal, GoalComposite):
                    self.body.state_pos = (float(selected[0]), float(selected[1]), float(selected[2]))
                    if not kwargs["config"].allow_break:
                        self.body.inventory_pages.append(
                            inventory_page([slot(9, "minecraft:dirt", 1)])
                        )
                    return ToolResult(
                        success=True,
                        reason="arrived",
                        can_retry=False,
                        metrics={"goal": list(selected), "selected_goal": list(selected)},
                    )
                # Walking onto the drop brings it into the pickup box; the next
                # inventory read reflects the pickup.
                self.body.inventory_pages.append(
                    inventory_page([slot(9, "minecraft:dirt", 1)])
                )
                return ToolResult(
                    success=True,
                    reason="arrived",
                    can_retry=False,
                    metrics={"goal": list(selected), "selected_goal": list(selected)},
                )

        body = FakeBody(
            blocks={(0, 64, 5): ("dirt", "SOLID")},
            inventory_pages=[
                inventory_page([slot(9, "minecraft:dirt", 0)]),  # before
                inventory_page([slot(9, "minecraft:dirt", 0)]),  # first poll: still lagging
            ],
        )
        body.state_pos = (0.5, 65.0, 0.5)
        navigator = LagThenCollectNavigator(body)
        policy = GovernancePolicy(natural_regions=[Region("mine", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy, navigator=navigator, settle=lambda _s: None)

        result = runtime.mine_block_collect((0, 64, 5), pickup_timeout_s=0.05, timeout_s=1.0)

        # The drop appeared only after the walk, and was collected.
        self.assertTrue(result.success, result)
        self.assertEqual(result.reason, "collected")
        self.assertEqual(result.metrics["deltas"], {"dirt": 1})
        # Walked into the drop cell center, not pos-1 (the solid floor), with a
        # tight arrival radius so pickup range is actually reached.
        self.assertTrue(navigator.calls, "pickup-assist walk did not fire")
        goal, nav_kwargs = navigator.calls[-1]
        self.assertIsInstance(goal, GoalComposite)
        self.assertIn((0, 64, 5), {child.pos for child in goal.goals})
        self.assertEqual(nav_kwargs["break_context"], BreakContext.TRAVEL)
        self.assertEqual(nav_kwargs["arrival_radius"], 0.25)
        self.assertFalse(nav_kwargs["config"].allow_break)
        self.assertFalse(nav_kwargs["config"].allow_place)
        assist = result.metrics["pickup_assist"]
        self.assertTrue(assist["moved"])
        self.assertTrue(assist["waited"])

    def test_mine_block_collect_walks_to_drop_entity_position_not_mined_cell(self):
        # pickup-B: a log mined at trunk height drops an item that FALLS away
        # from the mined cell `pos`. The assist must read nearbyEntities and walk
        # to the drop ENTITY's actual position, not `pos`. This is the case the
        # §8 walk-to-pos could not cover (it only works when the drop stays put).
        # Beats Mindcraft's pickupNearbyItems, which gives up if the first item
        # is unreachable; here we walk to the real drop and collect it.
        class DropEntityBody(FakeBody):
            def __init__(self, *a, item_pos, **kw):
                super().__init__(*a, **kw)
                self._item_pos = item_pos

            def perceive(self, scope, params):
                if scope == "nearbyEntities":
                    return PerceptionResult(
                        bot="Bot1",
                        scope="nearbyEntities",
                        type="perception",
                        ok=True,
                        complete=True,
                        data={"entities": [
                            {"id": "e1", "type": "item", "name": "Dirt",
                             "pos": list(self._item_pos), "health": None, "dist2": 1.0}
                        ]},
                    )
                return super().perceive(scope, params)

        class RecordingNavigator:
            def __init__(self, body):
                self.body = body
                self.calls = []

            def navigate_to(self, goal, **kwargs):
                self.calls.append((goal, kwargs))
                selected = goal_position(goal)
                if isinstance(goal, GoalComposite):
                    self.body.state_pos = (float(selected[0]), float(selected[1]), float(selected[2]))
                    if not kwargs["config"].allow_break:
                        self.body.inventory_pages.append(
                            inventory_page([slot(9, "minecraft:dirt", 1)])
                        )
                    return ToolResult(
                        success=True,
                        reason="arrived",
                        can_retry=False,
                        metrics={"goal": list(selected), "selected_goal": list(selected)},
                    )
                # Arriving at the drop entity puts it in the pickup box.
                self.body.inventory_pages.append(
                    inventory_page([slot(9, "minecraft:dirt", 1)])
                )
                return ToolResult(
                    success=True,
                    reason="arrived",
                    can_retry=False,
                    metrics={"goal": list(selected), "selected_goal": list(selected)},
                )

        # Mined cell at (0,64,5); the drop fell to (2,64,5) — a different cell.
        body = DropEntityBody(
            blocks={(0, 64, 5): ("dirt", "SOLID")},
            item_pos=(2.0, 64.0, 5.0),
            inventory_pages=[
                inventory_page([slot(9, "minecraft:dirt", 0)]),  # before
                inventory_page([slot(9, "minecraft:dirt", 0)]),  # first poll: lagging
            ],
        )
        body.state_pos = (0.5, 65.0, 0.5)
        navigator = RecordingNavigator(body)
        policy = GovernancePolicy(natural_regions=[Region("mine", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy, navigator=navigator, settle=lambda _s: None)

        result = runtime.mine_block_collect((0, 64, 5), pickup_timeout_s=0.05, timeout_s=1.0)

        self.assertTrue(result.success, result)
        self.assertEqual(result.reason, "collected")
        self.assertEqual(result.metrics["deltas"], {"dirt": 1})
        # Walked to the drop ENTITY's position, not the mined cell.
        self.assertTrue(navigator.calls, "pickup-assist walk did not fire")
        goal, nav_kwargs = navigator.calls[-1]
        self.assertIsInstance(goal, GoalComposite)
        self.assertEqual(goal.goals[0].pos, (2, 64, 5))
        self.assertEqual(tuple(child.pos for child in goal.goals), ((2, 64, 5),))
        self.assertNotEqual(goal.goals[0].pos, (0, 64, 5))
        self.assertEqual(nav_kwargs["break_context"], BreakContext.TRAVEL)
        self.assertEqual(nav_kwargs["arrival_radius"], 0.25)
        assist = result.metrics["pickup_assist"]
        self.assertEqual(assist["scans"][0]["candidate_count"], 1)
        self.assertTrue(assist["moved"])

    def test_mine_block_collect_uses_ore_drop_mapping_for_raw_resource(self):
        blocks = {
            (0, 64, 0): ("iron_ore", "SOLID"),
        }
        body = FakeBody(
            blocks=blocks,
            inventory_pages=[
                inv_page_with_pickaxe([slot(9, "minecraft:raw_iron", 2)], "minecraft:stone_pickaxe"),
                inv_page_with_pickaxe([slot(9, "minecraft:raw_iron", 2)], "minecraft:stone_pickaxe"),
                inv_page_with_pickaxe([slot(9, "minecraft:raw_iron", 3)], "minecraft:stone_pickaxe"),
            ],
        )
        policy = GovernancePolicy(natural_regions=[Region("mine", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy)

        result = runtime.mine_block_collect((0, 64, 0), timeout_s=1.0)

        self.assertTrue(result.success)
        self.assertEqual([action.name for action in body.actions], ["selectItem", "mineBlock"])
        self.assertEqual(body.actions[0].params["item"], "stone_pickaxe")
        self.assertEqual(result.metrics["expected_drops"], ["raw_iron"])
        self.assertEqual(result.metrics["deltas"], {"raw_iron": 1})

    def test_mine_block_collect_allows_explicit_target_block_to_drop_different_item(self):
        blocks = {
            (0, 64, 0): ("stone", "SOLID"),
        }
        body = FakeBody(
            blocks=blocks,
            inventory_pages=[
                inv_page_with_pickaxe([slot(9, "minecraft:cobblestone", 0)], "minecraft:wooden_pickaxe"),
                inv_page_with_pickaxe([slot(9, "minecraft:cobblestone", 0)], "minecraft:wooden_pickaxe"),
                inv_page_with_pickaxe([slot(9, "minecraft:cobblestone", 1)], "minecraft:wooden_pickaxe"),
            ],
        )
        policy = GovernancePolicy(natural_regions=[Region("mine", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy)

        result = runtime.mine_block_collect(
            (0, 64, 0),
            expected_drops=("minecraft:cobblestone",),
            target_block_types=("minecraft:stone", "minecraft:cobblestone"),
            timeout_s=1.0,
        )

        self.assertTrue(result.success)
        self.assertEqual([action.name for action in body.actions], ["selectItem", "mineBlock"])
        self.assertEqual(body.actions[0].params["item"], "wooden_pickaxe")
        self.assertEqual(result.metrics["block_type"], "stone")
        self.assertEqual(result.metrics["target_block_types"], ["stone", "cobblestone"])
        self.assertEqual(result.metrics["expected_drops"], ["cobblestone"])
        self.assertEqual(result.metrics["deltas"], {"cobblestone": 1})

    def test_mine_block_collect_refuses_explicit_target_block_mismatch_before_breaking(self):
        blocks = {
            (0, 64, 0): ("stone", "SOLID"),
        }
        body = FakeBody(
            blocks=blocks,
            inventory_pages=[inv_page_with_pickaxe([slot(9, "minecraft:diamond", 0)])],
        )
        policy = GovernancePolicy(natural_regions=[Region("mine", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy)

        result = runtime.mine_block_collect(
            (0, 64, 0),
            expected_drops=("minecraft:diamond",),
            target_block_types=("minecraft:diamond_ore",),
            timeout_s=1.0,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "break_denied:collect_target_required")
        self.assertEqual(body.actions, [])
        self.assertEqual(blocks[(0, 64, 0)], ("stone", "SOLID"))
        self.assertEqual(result.metrics["block_type"], "stone")
        self.assertEqual(result.metrics["target_block_types"], ["diamond_ore"])

    def test_mine_block_collect_can_use_dry_mining_before_inventory_delta_check(self):
        blocks = {
            (0, 64, 0): ("diamond_ore", "SOLID"),
            (1, 64, 0): ("water", "LIQUID"),
        }
        body = FakeBody(
            blocks=blocks,
            inventory_pages=[
                inv_page_with_pickaxe([slot(9, "minecraft:diamond", 0), slot(10, "minecraft:cobblestone", 3)]),
                inv_page_with_pickaxe([slot(9, "minecraft:diamond", 0), slot(10, "minecraft:cobblestone", 3)]),
                inv_page_with_pickaxe([slot(9, "minecraft:diamond", 0), slot(10, "minecraft:cobblestone", 3)]),
                inv_page_with_pickaxe([slot(9, "minecraft:diamond", 1), slot(10, "minecraft:cobblestone", 2)]),
            ],
        )
        policy = GovernancePolicy(natural_regions=[Region("mine", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy)

        result = runtime.mine_block_collect(
            (0, 64, 0),
            dry=True,
            expected_drops=("minecraft:diamond",),
            timeout_s=1.0,
        )

        self.assertTrue(result.success)
        self.assertEqual([action.name for action in body.actions], ["selectItem", "placeBlock", "mineBlock"])
        self.assertEqual(body.actions[1].params["purpose"], "seal")
        self.assertEqual(result.metrics["deltas"], {"diamond": 1})
        self.assertEqual(result.metrics["mine_result"]["metrics"]["dry_mining"]["initial_liquid_faces"], 1)

    def test_dig_down_one_refuses_liquid_start_without_mutation(self):
        blocks = {
            (0, 64, 0): ("water", "LIQUID"),
            (0, 65, 0): ("air", "CLEAR"),
            (0, 63, 0): ("stone", "SOLID"),
        }
        body = FallingBody(blocks=blocks)
        policy = GovernancePolicy(natural_regions=[Region("shaft", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy, settle=lambda _seconds: None)

        result = runtime.dig_down_one(current_pos=(0, 64, 0), timeout_s=1.0)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "dig_down_start_liquid")
        self.assertEqual(body.actions, [])
        self.assertEqual(result.metrics["liquid_part"], "feet")

    def test_dig_down_one_refuses_liquid_target_without_mutation(self):
        blocks = {
            (0, 64, 0): ("air", "CLEAR"),
            (0, 65, 0): ("air", "CLEAR"),
            (0, 63, 0): ("lava", "LIQUID"),
            (0, 62, 0): ("stone", "SOLID"),
        }
        body = FallingBody(blocks=blocks)
        policy = GovernancePolicy(natural_regions=[Region("shaft", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy, settle=lambda _seconds: None)

        result = runtime.dig_down_one(current_pos=(0, 64, 0), timeout_s=1.0)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "dig_down_target_liquid")
        self.assertEqual(body.actions, [])
        self.assertEqual(result.metrics["target"], [0, 63, 0])

    def test_dig_down_one_refuses_excessive_fall_without_mutation(self):
        blocks = {
            (0, 64, 0): ("air", "CLEAR"),
            (0, 65, 0): ("air", "CLEAR"),
            (0, 63, 0): ("stone", "SOLID"),
            (0, 62, 0): ("air", "CLEAR"),
            (0, 61, 0): ("air", "CLEAR"),
            (0, 60, 0): ("stone", "SOLID"),
        }
        body = FallingBody(blocks=blocks)
        policy = GovernancePolicy(natural_regions=[Region("shaft", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy, settle=lambda _seconds: None)

        result = runtime.dig_down_one(current_pos=(0, 64, 0), max_clear_fall=2, timeout_s=1.0)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "dig_down_fall_risk")
        self.assertEqual(body.actions, [])
        self.assertEqual(result.metrics["fall_clearance"], 3)
        self.assertEqual(result.metrics["max_clear_fall"], 2)

    def test_dig_down_one_refuses_protected_region_before_mutation(self):
        # Under type-based provenance a natural stone floor is now diggable, so
        # the red-line refusal-before-mutation property is pinned via an explicit
        # protected_region instead of unknown provenance.
        blocks = {
            (50, 64, 50): ("air", "CLEAR"),
            (50, 65, 50): ("air", "CLEAR"),
            (50, 63, 50): ("stone", "SOLID"),
            (50, 62, 50): ("stone", "SOLID"),
        }
        body = FakeBody(blocks=blocks)
        runtime = BlockWork(
            body,
            GovernancePolicy(protected_regions=[Region("base", (40, 0, 40), (60, 100, 60))]),
        )

        result = runtime.dig_down_one(current_pos=(50, 64, 50), timeout_s=1.0)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "dig_down_denied:protected_region")
        self.assertEqual(body.actions, [])
        self.assertEqual(result.metrics["legality"]["protected"], True)

    def test_dig_down_one_mines_floor_after_safety_checks(self):
        blocks = {
            (0, 64, 0): ("air", "CLEAR"),
            (0, 65, 0): ("air", "CLEAR"),
            (0, 63, 0): ("stone", "SOLID"),
            (0, 62, 0): ("stone", "SOLID"),
        }
        body = FallingBody(blocks=blocks)
        policy = GovernancePolicy(natural_regions=[Region("shaft", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy, settle=lambda _seconds: None)

        result = runtime.dig_down_one(current_pos=(0, 64, 0), timeout_s=1.0)

        self.assertTrue(result.success)
        self.assertEqual([action.name for action in body.actions], ["mineBlock"])
        self.assertEqual(body.actions[0].params["target"], [0, 63, 0])
        self.assertEqual(result.metrics["dig_down"]["fall_clearance"], 1)
        self.assertEqual(result.metrics["dig_down"]["first_support"], [0, 62, 0])
        self.assertTrue(result.metrics["dig_down"]["safe_to_continue"])

    def test_dig_down_one_batches_fall_probe_cells(self):
        blocks = {
            (0, 64, 0): ("air", "CLEAR"),
            (0, 65, 0): ("air", "CLEAR"),
            (0, 63, 0): ("stone", "SOLID"),
            (0, 62, 0): ("air", "CLEAR"),
            (0, 61, 0): ("stone", "SOLID"),
        }
        body = FallingBody(blocks=blocks)
        policy = GovernancePolicy(natural_regions=[Region("shaft", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy, settle=lambda _seconds: None)

        result = runtime.dig_down_one(current_pos=(0, 64, 0), max_clear_fall=2, timeout_s=1.0)

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(result.metrics["dig_down"]["fall_clearance"], 2)
        self.assertEqual(result.metrics["dig_down"]["first_support"], [0, 61, 0])
        block_cells_reads = [params for scope, params in body.perceptions if scope == "blockCells"]
        self.assertEqual(len(block_cells_reads), 1)
        self.assertEqual(block_cells_reads[0]["cells"], [[0, 62, 0], [0, 61, 0]])

    def test_dig_down_one_fall_probe_batch_failure_preserves_short_circuit(self):
        blocks = {
            (0, 64, 0): ("air", "CLEAR"),
            (0, 65, 0): ("air", "CLEAR"),
            (0, 63, 0): ("stone", "SOLID"),
            (0, 62, 0): ("stone", "SOLID"),
            (0, 61, 0): ("air", "CLEAR"),
        }
        body = FallProbeBatchFailureBody(blocks=blocks)
        policy = GovernancePolicy(natural_regions=[Region("shaft", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy, settle=lambda _seconds: None)

        result = runtime.dig_down_one(current_pos=(0, 64, 0), max_clear_fall=2, timeout_s=1.0)

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(result.metrics["dig_down"]["fall_clearance"], 1)
        self.assertEqual(result.metrics["dig_down"]["first_support"], [0, 62, 0])
        self.assertEqual(
            [params for scope, params in body.perceptions if scope == "blockCells"],
            [{"cells": [[0, 62, 0], [0, 61, 0]], "start": 0, "limit": 64}],
        )
        self.assertEqual(
            [params for scope, params in body.perceptions if scope == "blockAt"],
            [
                {"x": 0, "y": 64, "z": 0},
                {"x": 0, "y": 65, "z": 0},
                {"x": 0, "y": 63, "z": 0},
                {"x": 0, "y": 62, "z": 0},
                {"x": 0, "y": 63, "z": 0},
            ],
        )
        self.assertNotIn(
            {"x": 0, "y": 61, "z": 0},
            [params for scope, params in body.perceptions if scope == "blockAt"],
        )

    def test_dig_down_one_reports_already_open_with_support_truth(self):
        blocks = {
            (0, 64, 0): ("air", "CLEAR"),
            (0, 65, 0): ("air", "CLEAR"),
            (0, 63, 0): ("air", "CLEAR"),
            (0, 62, 0): ("stone", "SOLID"),
        }
        body = FallingBody(blocks=blocks)
        policy = GovernancePolicy(natural_regions=[Region("shaft", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy, settle=lambda _seconds: None)

        result = runtime.dig_down_one(current_pos=(0, 64, 0), timeout_s=1.0)

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "dig_down_already_open")
        self.assertEqual(body.actions, [])
        self.assertEqual(result.metrics["dig_down"]["fall_clearance"], 1)
        self.assertEqual(result.metrics["dig_down"]["first_support"], [0, 62, 0])

    def test_dig_down_one_uses_floor_for_negative_state_coordinates(self):
        class StateBody(FakeBody):
            def get_state(self):
                return BodyState(
                    bot="Bot1",
                    pos=(-0.2, 64.0, -0.2),
                    yaw=None,
                    pitch=None,
                    health=20.0,
                    food=20,
                    oxygen=None,
                    inventory_raw="[]",
                    inventory_hash="hash",
                    effects=None,
                    time=0,
                    weather=None,
                    dimension="overworld",
                    complete=True,
                )

        blocks = {
            (-1, 64, -1): ("air", "CLEAR"),
            (-1, 65, -1): ("air", "CLEAR"),
            (-1, 63, -1): ("stone", "SOLID"),
            (-1, 62, -1): ("stone", "SOLID"),
        }
        body = StateBody(blocks=blocks)
        policy = GovernancePolicy(natural_regions=[Region("shaft", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy)

        result = runtime.dig_down_one(timeout_s=1.0)

        self.assertTrue(result.success)
        self.assertEqual(result.metrics["dig_down"]["origin"], [-1, 64, -1])
        self.assertEqual(body.actions[0].params["target"], [-1, 63, -1])

    def test_dig_down_to_y_reaches_target_via_repeated_open_and_descend(self):
        blocks = {
            (0, 64, 0): ("air", "CLEAR"),
            (0, 65, 0): ("air", "CLEAR"),
            (0, 63, 0): ("stone", "SOLID"),
            (0, 62, 0): ("stone", "SOLID"),
            (0, 61, 0): ("stone", "SOLID"),
        }
        body = FallingBody(blocks=blocks)
        policy = GovernancePolicy(natural_regions=[Region("shaft", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy, settle=lambda _seconds: None)

        result = runtime.dig_down_to_y(62, current_pos=(0, 64, 0), dig_timeout_s=1.0, move_timeout_s=1.0)

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "dig_down_target_reached")
        self.assertEqual(result.metrics["final_pos"], [0, 62, 0])
        self.assertEqual(result.metrics["steps_completed"], 2)
        self.assertEqual([action.name for action in body.actions], ["mineBlock", "mineBlock"])
        self.assertEqual([step["kind"] for step in result.metrics["steps"]], ["open", "descent", "open", "descent"])

    def test_dig_down_to_y_centers_off_edge_before_waiting_for_descent(self):
        blocks = {
            (0, 64, 0): ("air", "CLEAR"),
            (0, 65, 0): ("air", "CLEAR"),
            (0, 63, 0): ("stone", "SOLID"),
            (0, 62, 0): ("stone", "SOLID"),
        }
        body = EdgeSupportedFallingBody(blocks=blocks)
        body.state_pos = (0.98, 64.0, 0.5)
        policy = GovernancePolicy(natural_regions=[Region("shaft", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy, settle=lambda _seconds: None)

        result = runtime.dig_down_to_y(63, current_pos=(0, 64, 0), dig_timeout_s=1.0, move_timeout_s=1.0)

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "dig_down_target_reached")
        self.assertEqual([action.name for action in body.actions], ["mineBlock", "moveTo"])
        self.assertEqual(body.actions[1].params["target"], [0.5, 64, 0.5])
        self.assertEqual(body.actions[1].params["arrival_radius"], 0.1)
        self.assertEqual([step["kind"] for step in result.metrics["steps"]], ["open", "alignment", "descent"])
        self.assertEqual(result.metrics["steps"][1]["reason"], "dig_down_aligned")
        self.assertEqual(result.metrics["final_pos"], [0, 63, 0])

    def test_dig_down_to_y_reports_typed_alignment_failure(self):
        class BlockedCenterBody(EdgeSupportedFallingBody):
            def execute(self, action: Action) -> Result:
                accepted = super().execute(action)
                if action.name == "moveTo":
                    self.state_pos = (0.98, 64.0, 0.5)
                    self.terminal = Event(
                        seq=self.terminal.seq,
                        tick=self.terminal.tick,
                        bot=self.terminal.bot,
                        name="moveDone",
                        data={
                            "action_id": action.id,
                            "arrived": False,
                            "final_pos": list(self.state_pos),
                            "target": list(action.params["target"]),
                            "stopped_reason": "blocked",
                        },
                    )
                return accepted

        blocks = {
            (0, 64, 0): ("air", "CLEAR"),
            (0, 65, 0): ("air", "CLEAR"),
            (0, 63, 0): ("stone", "SOLID"),
            (0, 62, 0): ("stone", "SOLID"),
        }
        body = BlockedCenterBody(blocks=blocks)
        body.state_pos = (0.98, 64.0, 0.5)
        policy = GovernancePolicy(natural_regions=[Region("shaft", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy, settle=lambda _seconds: None)

        result = runtime.dig_down_to_y(63, current_pos=(0, 64, 0), dig_timeout_s=1.0, move_timeout_s=1.0)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "dig_down_alignment_failed:blocked")
        self.assertEqual([action.name for action in body.actions], ["mineBlock", "moveTo"])
        self.assertEqual(result.metrics["steps_completed"], 0)
        self.assertEqual(result.metrics["steps"][-1]["kind"], "alignment")

    def test_dig_down_to_y_accepts_authoritative_descent_when_center_move_stops_stuck(self):
        class GravityWinsBody(EdgeSupportedFallingBody):
            def execute(self, action: Action) -> Result:
                accepted = super().execute(action)
                if action.name == "moveTo":
                    self.state_pos = (0.58, 63.79, 0.5)
                    self.terminal = Event(
                        seq=self.terminal.seq,
                        tick=self.terminal.tick,
                        bot=self.terminal.bot,
                        name="moveDone",
                        data={
                            "action_id": action.id,
                            "arrived": False,
                            "final_pos": list(self.state_pos),
                            "target": list(action.params["target"]),
                            "stopped_reason": "stuck",
                        },
                    )
                return accepted

        blocks = {
            (0, 64, 0): ("air", "CLEAR"),
            (0, 65, 0): ("air", "CLEAR"),
            (0, 63, 0): ("stone", "SOLID"),
            (0, 62, 0): ("stone", "SOLID"),
        }
        body = GravityWinsBody(blocks=blocks)
        body.state_pos = (0.98, 64.0, 0.5)
        policy = GovernancePolicy(natural_regions=[Region("shaft", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy, settle=lambda _seconds: None)

        result = runtime.dig_down_to_y(63, current_pos=(0, 64, 0), dig_timeout_s=1.0, move_timeout_s=1.0)

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "dig_down_target_reached")
        self.assertEqual(result.metrics["steps"][1]["reason"], "dig_down_aligned")
        self.assertEqual(result.metrics["steps"][1]["metrics"]["movement"]["reason"], "stuck")
        self.assertTrue(result.metrics["steps"][1]["metrics"]["already_descended"])

    def test_dig_down_to_y_stops_on_second_step_fall_risk(self):
        blocks = {
            (0, 64, 0): ("air", "CLEAR"),
            (0, 65, 0): ("air", "CLEAR"),
            (0, 63, 0): ("stone", "SOLID"),
            (0, 62, 0): ("stone", "SOLID"),
            (0, 61, 0): ("air", "CLEAR"),
            (0, 60, 0): ("air", "CLEAR"),
            (0, 59, 0): ("stone", "SOLID"),
        }
        body = FallingBody(blocks=blocks)
        policy = GovernancePolicy(natural_regions=[Region("shaft", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy, settle=lambda _seconds: None)

        result = runtime.dig_down_to_y(60, current_pos=(0, 64, 0), max_clear_fall=2, dig_timeout_s=1.0, move_timeout_s=1.0)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "dig_down_fall_risk")
        self.assertEqual(result.metrics["final_pos"], [0, 63, 0])
        self.assertEqual(result.metrics["steps_completed"], 1)
        self.assertEqual(result.metrics["steps"][-1]["reason"], "dig_down_fall_risk")

    def test_dig_down_to_y_honors_step_budget(self):
        blocks = {
            (0, 64, 0): ("air", "CLEAR"),
            (0, 65, 0): ("air", "CLEAR"),
            (0, 63, 0): ("stone", "SOLID"),
            (0, 62, 0): ("stone", "SOLID"),
            (0, 61, 0): ("stone", "SOLID"),
        }
        body = FallingBody(blocks=blocks)
        policy = GovernancePolicy(natural_regions=[Region("shaft", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy, settle=lambda _seconds: None)

        result = runtime.dig_down_to_y(61, current_pos=(0, 64, 0), max_steps=1, dig_timeout_s=1.0, move_timeout_s=1.0)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "dig_down_step_budget_exhausted")
        self.assertEqual(result.metrics["final_pos"], [0, 63, 0])
        self.assertEqual(result.metrics["step_budget"], 1)
        self.assertEqual(result.metrics["steps_completed"], 1)

    def test_dig_down_to_y_short_circuits_when_already_at_or_below_target(self):
        body = FakeBody(blocks={(0, 62, 0): ("air", "CLEAR")})
        runtime = BlockWork(body, GovernancePolicy())

        result = runtime.dig_down_to_y(63, current_pos=(0, 62, 0), dig_timeout_s=1.0, move_timeout_s=1.0)

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "dig_down_target_reached")
        self.assertEqual(result.metrics["steps_completed"], 0)
        self.assertEqual(body.actions, [])

    def test_dig_up_one_refuses_when_no_scaffold_block_is_available(self):
        blocks = {
            (0, 64, 0): ("air", "CLEAR"),
            (0, 65, 0): ("air", "CLEAR"),
            (0, 66, 0): ("air", "CLEAR"),
        }
        body = FakeBody(blocks=blocks, inventory_pages=[inventory_page([slot(9, "minecraft:torch", 4)])])
        policy = GovernancePolicy(natural_regions=[Region("shaft", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy)

        result = runtime.dig_up_one(current_pos=(0, 64, 0), timeout_s=1.0)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "dig_up_no_scaffold_available")
        self.assertEqual(body.actions, [])

    def test_dig_up_one_refuses_liquid_above_without_mutation(self):
        blocks = {
            (0, 64, 0): ("air", "CLEAR"),
            (0, 65, 0): ("water", "LIQUID"),
            (0, 66, 0): ("air", "CLEAR"),
        }
        body = FakeBody(blocks=blocks, inventory_pages=[inventory_page([slot(9, "minecraft:cobblestone", 4)])])
        policy = GovernancePolicy(natural_regions=[Region("shaft", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy)

        result = runtime.dig_up_one(current_pos=(0, 64, 0), timeout_s=1.0)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "dig_up_liquid_above")
        self.assertEqual(body.actions, [])
        self.assertEqual(result.metrics["phase"], "head")

    def test_dig_up_one_clears_headroom_places_pillar_and_requires_height_gain(self):
        class RisingBody(FakeBody):
            def execute(self, action: Action) -> Result:
                result = super().execute(action)
                if action.name == "jump":
                    self.state_pos = (self.state_pos[0], self.state_pos[1] + 1.0, self.state_pos[2])
                    self.terminal = Event(
                        seq=self.terminal.seq,
                        tick=self.terminal.tick,
                        bot=self.terminal.bot,
                        name="jumpDone",
                        data={
                            "action_id": action.id,
                            "success": True,
                            "final_pos": list(self.state_pos),
                            "stopped_reason": "completed",
                        },
                    )
                return result

        blocks = {
            (0, 64, 0): ("air", "CLEAR"),
            (0, 65, 0): ("stone", "SOLID"),
            (0, 66, 0): ("stone", "SOLID"),
        }
        body = RisingBody(blocks=blocks, inventory_pages=[inventory_page([slot(9, "minecraft:cobblestone", 4)])])
        policy = GovernancePolicy(natural_regions=[Region("shaft", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy)

        result = runtime.dig_up_one(current_pos=(0, 64, 0), timeout_s=1.0)

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "dig_up_step_completed")
        self.assertEqual([action.name for action in body.actions], ["mineBlock", "mineBlock", "jump", "placeBlock"])
        self.assertEqual(result.metrics["gained_y"], 1.0)
        self.assertEqual(result.metrics["scaffold_block"], "cobblestone")
        self.assertEqual(result.metrics["final_pos"], [0, 65, 0])
        cleanup = policy.can_break((0, 64, 0), "cobblestone", BreakContext.BOT_CLEANUP)
        self.assertTrue(cleanup.allowed)

    def test_dig_up_one_fails_when_jump_does_not_gain_height(self):
        blocks = {
            (0, 64, 0): ("air", "CLEAR"),
            (0, 65, 0): ("air", "CLEAR"),
            (0, 66, 0): ("air", "CLEAR"),
        }
        body = FakeBody(blocks=blocks, inventory_pages=[inventory_page([slot(9, "minecraft:cobblestone", 4)])])
        policy = GovernancePolicy(natural_regions=[Region("shaft", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy)

        result = runtime.dig_up_one(current_pos=(0, 64, 0), timeout_s=1.0)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "dig_up_no_height_gain")
        self.assertEqual([action.name for action in body.actions], ["jump", "placeBlock"])

    def test_dig_up_to_y_reaches_target_via_repeated_pillar_steps(self):
        class RisingBody(FakeBody):
            def execute(self, action: Action) -> Result:
                result = super().execute(action)
                if action.name == "jump":
                    self.state_pos = (self.state_pos[0], self.state_pos[1] + 1.0, self.state_pos[2])
                    self.terminal = Event(
                        seq=self.terminal.seq,
                        tick=self.terminal.tick,
                        bot=self.terminal.bot,
                        name="jumpDone",
                        data={
                            "action_id": action.id,
                            "success": True,
                            "final_pos": list(self.state_pos),
                            "stopped_reason": "completed",
                        },
                    )
                return result

        blocks = {
            (0, 64, 0): ("air", "CLEAR"),
            (0, 65, 0): ("stone", "SOLID"),
            (0, 66, 0): ("stone", "SOLID"),
            (0, 67, 0): ("stone", "SOLID"),
        }
        body = RisingBody(
            blocks=blocks,
            inventory_pages=[
                inventory_page([slot(9, "minecraft:cobblestone", 8)]),
                inventory_page([slot(9, "minecraft:cobblestone", 7)]),
            ],
        )
        policy = GovernancePolicy(natural_regions=[Region("shaft", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy)

        result = runtime.dig_up_to_y(66, current_pos=(0, 64, 0), timeout_s=1.0)

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "dig_up_target_reached")
        self.assertEqual(result.metrics["final_pos"], [0, 66, 0])
        self.assertEqual(result.metrics["steps_completed"], 2)
        self.assertEqual(
            [action.name for action in body.actions],
            ["mineBlock", "mineBlock", "jump", "placeBlock", "mineBlock", "jump", "placeBlock"],
        )

    def test_dig_up_to_y_stops_when_second_step_has_no_height_gain(self):
        class OneRiseBody(FakeBody):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.jump_count = 0

            def execute(self, action: Action) -> Result:
                result = super().execute(action)
                if action.name == "jump":
                    self.jump_count += 1
                    if self.jump_count == 1:
                        self.state_pos = (self.state_pos[0], self.state_pos[1] + 1.0, self.state_pos[2])
                    self.terminal = Event(
                        seq=self.terminal.seq,
                        tick=self.terminal.tick,
                        bot=self.terminal.bot,
                        name="jumpDone",
                        data={
                            "action_id": action.id,
                            "success": True,
                            "final_pos": list(self.state_pos),
                            "stopped_reason": "completed",
                        },
                    )
                return result

        blocks = {
            (0, 64, 0): ("air", "CLEAR"),
            (0, 65, 0): ("air", "CLEAR"),
            (0, 66, 0): ("air", "CLEAR"),
            (0, 67, 0): ("air", "CLEAR"),
        }
        body = OneRiseBody(
            blocks=blocks,
            inventory_pages=[
                inventory_page([slot(9, "minecraft:cobblestone", 8)]),
                inventory_page([slot(9, "minecraft:cobblestone", 7)]),
            ],
        )
        policy = GovernancePolicy(natural_regions=[Region("shaft", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy)

        result = runtime.dig_up_to_y(66, current_pos=(0, 64, 0), timeout_s=1.0)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "dig_up_no_height_gain")
        self.assertEqual(result.metrics["final_pos"], [0, 65, 0])
        self.assertEqual(result.metrics["steps_completed"], 1)

    def test_dig_up_to_y_honors_step_budget(self):
        class RisingBody(FakeBody):
            def execute(self, action: Action) -> Result:
                result = super().execute(action)
                if action.name == "jump":
                    self.state_pos = (self.state_pos[0], self.state_pos[1] + 1.0, self.state_pos[2])
                    self.terminal = Event(
                        seq=self.terminal.seq,
                        tick=self.terminal.tick,
                        bot=self.terminal.bot,
                        name="jumpDone",
                        data={
                            "action_id": action.id,
                            "success": True,
                            "final_pos": list(self.state_pos),
                            "stopped_reason": "completed",
                        },
                    )
                return result

        blocks = {
            (0, 64, 0): ("air", "CLEAR"),
            (0, 65, 0): ("air", "CLEAR"),
            (0, 66, 0): ("air", "CLEAR"),
        }
        body = RisingBody(blocks=blocks, inventory_pages=[inventory_page([slot(9, "minecraft:cobblestone", 8)])])
        policy = GovernancePolicy(natural_regions=[Region("shaft", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy)

        result = runtime.dig_up_to_y(66, current_pos=(0, 64, 0), max_steps=1, timeout_s=1.0)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "dig_up_step_budget_exhausted")
        self.assertEqual(result.metrics["final_pos"], [0, 65, 0])
        self.assertEqual(result.metrics["steps_completed"], 1)
        self.assertEqual(result.metrics["step_budget"], 1)

    def test_dig_up_to_y_short_circuits_when_already_at_or_above_target(self):
        body = FakeBody(blocks={(0, 66, 0): ("air", "CLEAR")})
        runtime = BlockWork(body, GovernancePolicy())

        result = runtime.dig_up_to_y(65, current_pos=(0, 66, 0), timeout_s=1.0)

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "dig_up_target_reached")
        self.assertEqual(result.metrics["steps_completed"], 0)
        self.assertEqual(body.actions, [])

    def test_go_to_surface_reaches_adjacent_sky_exposed_natural_surface(self):
        class RisingBody(FakeBody):
            def execute(self, action: Action) -> Result:
                result = super().execute(action)
                if action.name == "jump":
                    self.state_pos = (self.state_pos[0], self.state_pos[1] + 1.0, self.state_pos[2])
                    self.terminal = Event(
                        seq=self.terminal.seq,
                        tick=self.terminal.tick,
                        bot=self.terminal.bot,
                        name="jumpDone",
                        data={
                            "action_id": action.id,
                            "success": True,
                            "final_pos": list(self.state_pos),
                            "stopped_reason": "completed",
                        },
                    )
                return result

        class MovingNavigator(FakeNavigator):
            def __init__(self, body):
                super().__init__(result=True, reason="arrived")
                self.body = body

            def navigate_to(self, goal, **kwargs):
                result = super().navigate_to(goal, **kwargs)
                selected = goal_position(goal)
                self.body.state_pos = (float(selected[0]), float(selected[1]), float(selected[2]))
                return result

        blocks = {
            (0, 64, 0): ("air", "CLEAR"),
            (0, 65, 0): ("air", "CLEAR"),
            (0, 66, 0): ("air", "CLEAR"),
            (1, 64, 0): ("stone", "SOLID"),
            (1, 65, 0): ("air", "CLEAR"),
            (1, 66, 0): ("air", "CLEAR"),
        }
        body = RisingBody(blocks=blocks, inventory_pages=[inventory_page([slot(9, "minecraft:cobblestone", 8)])])
        body.state_pos = (0.5, 64.0, 0.5)
        policy = GovernancePolicy(natural_regions=[Region("surface", (-10, 0, -10), (10, 120, 10))])
        navigator = MovingNavigator(body)
        runtime = BlockWork(body, policy, navigator=navigator)

        result = runtime.go_to_surface(timeout_s=1.0, surface_scan_height=4, world_top_y=70)

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(result.reason, "surface_reached")
        self.assertEqual(result.metrics["target_surface"], [1, 65, 0])
        self.assertEqual(result.metrics["final_pos"], [1, 65, 0])
        self.assertEqual(result.metrics["terminal_surface"]["candidate"], True)
        self.assertEqual(len(navigator.calls), 1)
        self.assertIsInstance(navigator.calls[0][0], GoalComposite)
        self.assertEqual(navigator.calls[0][1]["arrival_radius"], 0.25)
        nav_config = navigator.calls[0][1]["config"]
        self.assertTrue(nav_config.allow_break)
        self.assertTrue(nav_config.allow_pillar)
        self.assertFalse(nav_config.allow_downward)
        self.assertEqual(body.actions, [])

    def test_go_to_surface_requires_verified_surface_feet_after_navigation(self):
        class MissNavigator(FakeNavigator):
            def __init__(self, body):
                super().__init__(result=True, reason="arrived")
                self.body = body

            def navigate_to(self, goal, **kwargs):
                self.calls.append((goal, kwargs))
                self.body.state_pos = (0.5, 65.0, 0.5)
                selected = goal_position(goal)
                return ToolResult(
                    success=True,
                    reason="arrived",
                    can_retry=False,
                    metrics={"goal": list(selected), "selected_goal": list(selected), "kwargs": kwargs},
                )

        blocks = {
            (0, 64, 0): ("air", "CLEAR"),
            (0, 65, 0): ("air", "CLEAR"),
            (0, 66, 0): ("air", "CLEAR"),
            (1, 64, 0): ("stone", "SOLID"),
            (1, 65, 0): ("air", "CLEAR"),
            (1, 66, 0): ("air", "CLEAR"),
            (2, 64, 0): ("stone", "SOLID"),
            (2, 65, 0): ("air", "CLEAR"),
            (2, 66, 0): ("air", "CLEAR"),
        }
        body = FakeBody(blocks=blocks)
        body.state_pos = (0.5, 65.0, 0.5)
        policy = GovernancePolicy(natural_regions=[Region("surface", (-10, 0, -10), (10, 120, 10))])
        navigator = MissNavigator(body)
        runtime = BlockWork(body, policy, navigator=navigator)

        result = runtime.go_to_surface(timeout_s=1.0, surface_scan_height=4, world_top_y=70)

        self.assertFalse(result.success, result.to_payload())
        self.assertEqual(result.reason, "surface_verification_failed")
        self.assertEqual(result.metrics["final_pos"], [0, 65, 0])
        self.assertEqual(navigator.calls[0][1]["arrival_radius"], 0.25)
        self.assertEqual(len(navigator.calls), 1)

    def test_go_to_surface_can_use_same_level_alternate_surface(self):
        class MovingNavigator(FakeNavigator):
            def __init__(self, body):
                super().__init__(result=True, reason="arrived")
                self.body = body

            def navigate_to(self, goal, **kwargs):
                result = super().navigate_to(goal, **kwargs)
                selected = goal_position(goal)
                self.body.state_pos = (float(selected[0]), float(selected[1]), float(selected[2]))
                return result

        blocks = {
            (0, 64, 0): ("air", "CLEAR"),
            (0, 65, 0): ("stone", "SOLID"),
            (0, 66, 0): ("stone", "SOLID"),
            (1, 63, 0): ("stone", "SOLID"),
            (1, 64, 0): ("air", "CLEAR"),
            (1, 65, 0): ("air", "CLEAR"),
            (1, 66, 0): ("air", "CLEAR"),
        }
        body = FakeBody(blocks=blocks)
        body.state_pos = (0.5, 64.0, 0.5)
        policy = GovernancePolicy(natural_regions=[Region("surface", (-10, 0, -10), (10, 120, 10))])
        navigator = MovingNavigator(body)
        runtime = BlockWork(body, policy, navigator=navigator)

        result = runtime.go_to_surface(timeout_s=1.0, surface_scan_height=0, world_top_y=70)

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(result.reason, "surface_reached")
        self.assertEqual(result.metrics["target_surface"], [1, 64, 0])
        self.assertEqual(result.metrics["final_pos"], [1, 64, 0])
        self.assertIsInstance(navigator.calls[0][0], GoalComposite)
        self.assertEqual(navigator.calls[0][1]["arrival_radius"], 0.25)

    def test_go_to_surface_stages_covered_water_egress_before_surface_navigation(self):
        class StagedNavigator(FakeNavigator):
            def __init__(self, body):
                super().__init__(result=True, reason="arrived")
                self.body = body

            def navigate_to(self, goal, **kwargs):
                result = super().navigate_to(goal, **kwargs)
                selected = goal_position(goal)
                self.body.state_pos = (float(selected[0]), float(selected[1]), float(selected[2]))
                return result

        blocks = {
            (0, 63, 0): ("stone", "SOLID"),
            (0, 64, 0): ("water", "LIQUID"),
            (0, 67, 0): ("stone", "SOLID"),
            (1, 64, 0): ("water", "LIQUID"),
            (2, 64, 0): ("stone", "SOLID"),
            (2, 67, 0): ("stone", "SOLID"),
            (3, 65, 0): ("stone", "SOLID"),
        }
        body = FakeBody(blocks=blocks)
        body.state_pos = (0.5, 64.0, 0.5)
        policy = GovernancePolicy(natural_regions=[Region("surface", (-20, 0, -20), (20, 120, 20))])
        navigator = StagedNavigator(body)
        runtime = BlockWork(body, policy, navigator=navigator)

        result = runtime.go_to_surface(
            timeout_s=1.0,
            surface_scan_height=2,
            world_top_y=70,
        )

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(result.reason, "surface_reached")
        self.assertEqual(result.metrics["origin"], [0, 64, 0])
        self.assertEqual(result.metrics["surface_origin"], [2, 65, 0])
        self.assertEqual(result.metrics["final_pos"], [3, 66, 0])
        self.assertEqual(len(navigator.calls), 2)
        egress_goal, egress_kwargs = navigator.calls[0]
        self.assertIsInstance(egress_goal, GoalComposite)
        self.assertIn((2, 65, 0), tuple(goal_position(goal) for goal in egress_goal.goals))
        egress_config = egress_kwargs["config"]
        self.assertFalse(egress_config.allow_break)
        self.assertFalse(egress_config.allow_place)
        self.assertFalse(egress_config.allow_pillar)
        self.assertFalse(egress_config.allow_downward)
        surface_goal, surface_kwargs = navigator.calls[1]
        self.assertIsInstance(surface_goal, GoalComposite)
        self.assertIn((3, 66, 0), tuple(goal_position(goal) for goal in surface_goal.goals))
        self.assertTrue(surface_kwargs["config"].allow_break)
        self.assertEqual(result.metrics["surface_egress"]["final_pos"], [2, 65, 0])
        self.assertTrue(result.metrics["surface_egress"]["terminal_stand"]["standable"])

    def test_go_to_surface_uses_sparse_lateral_surface_after_covered_water_egress(self):
        remote_surface = (2, 66, -32)

        class AmbiguousMutationRisk:
            def __init__(self):
                self.calls = []

            def assess(self, pos, block_type, context):
                self.calls.append((pos, block_type, context))
                return StructureRiskAssessment(
                    pos=pos,
                    block_type=block_type,
                    level=StructureRiskLevel.AMBIGUOUS,
                    score=0.35,
                    complete=True,
                    sampled_cells=63,
                    signals=("exposed_regular_plane",),
                )

        class LateralNavigator(FakeNavigator):
            def __init__(self, body):
                super().__init__(result=True, reason="arrived")
                self.body = body

            def navigate_to(self, goal, **kwargs):
                self.calls.append((goal, kwargs))
                goals = tuple(goal_position(candidate) for candidate in goal.goals)
                selected = goals[0] if len(self.calls) == 1 else remote_surface
                if selected not in goals:
                    raise AssertionError(f"expected lateral surface missing from {goals}")
                self.body.state_pos = tuple(float(value) for value in selected)
                return ToolResult(
                    success=True,
                    reason="arrived",
                    can_retry=False,
                    metrics={"goal": list(selected), "selected_goal": list(selected)},
                )

        blocks = {
            (0, 63, 0): ("stone", "SOLID"),
            (0, 64, 0): ("water", "LIQUID"),
            (0, 67, 0): ("stone", "SOLID"),
            (1, 64, 0): ("water", "LIQUID"),
            (2, 64, 0): ("stone", "SOLID"),
            (2, 67, 0): ("stone", "SOLID"),
            (3, 65, 0): ("stone", "SOLID"),
            (2, 65, -32): ("stone", "SOLID"),
            (18, 74, 0): ("stone", "SOLID"),
        }
        body = FakeBody(blocks=blocks)
        body.state_pos = (0.5, 64.0, 0.5)
        assessor = AmbiguousMutationRisk()
        policy = GovernancePolicy(
            natural_regions=[Region("surface", (-40, 0, -40), (40, 120, 40))],
            structure_risk_assessor=assessor,
            require_structure_assessment=True,
        )
        navigator = LateralNavigator(body)
        runtime = BlockWork(body, policy, navigator=navigator)

        result = runtime.go_to_surface(timeout_s=1.0, surface_scan_height=2, world_top_y=70)

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(result.metrics["final_pos"], list(remote_surface))
        self.assertEqual(result.metrics["surface_domain"]["selection"], "covered_water_lateral_surface")
        self.assertEqual(result.metrics["surface_lateral_domain"]["source"], "surfaceColumns")
        self.assertIn(
            remote_surface,
            tuple(goal_position(candidate) for candidate in navigator.calls[1][0].goals),
        )
        self.assertNotIn(
            (18, 75, 0),
            tuple(goal_position(candidate) for candidate in navigator.calls[1][0].goals),
        )
        self.assertEqual(result.metrics["surface_lateral_domain"]["selection_strategy"], "minimum_vertical_effort_tier")
        self.assertEqual(result.metrics["surface_lateral_domain"]["selected_vertical_effort"], 1)
        self.assertGreater(result.metrics["surface_lateral_domain"]["deferred_candidate_count"], 0)
        self.assertEqual(result.metrics["terminal_surface"]["candidate"], True)
        self.assertEqual(result.metrics["terminal_surface"]["support_legality"]["reason"], "allowed_stand")
        self.assertEqual(assessor.calls, [])

    def test_go_to_surface_excludes_constructible_candidates_without_scaffold(self):
        blocks = {
            (0, 63, 0): ("stone", "SOLID"),
            (0, 64, 0): ("air", "CLEAR"),
            (0, 65, 0): ("dirt", "SOLID"),
            (0, 66, 0): ("dirt", "SOLID"),
            (1, 63, 0): ("stone", "SOLID"),
            (1, 64, 0): ("air", "CLEAR"),
            (1, 65, 0): ("air", "CLEAR"),
            (1, 66, 0): ("dirt", "SOLID"),
        }
        body = FakeBody(blocks=blocks)
        body.state_pos = (0.5, 64.0, 0.5)
        policy = GovernancePolicy(natural_regions=[Region("surface", (-10, 0, -10), (10, 120, 10))])
        runtime = BlockWork(body, policy, navigator=FakeNavigator())

        result = runtime.go_to_surface(
            timeout_s=1.0,
            surface_scan_height=2,
            world_top_y=70,
        )

        self.assertFalse(result.success, result.to_payload())
        self.assertEqual(result.reason, "surface_not_found_in_column")
        self.assertFalse(result.metrics["surface_capability"]["constructible_pillar"])
        self.assertEqual(result.metrics["surface_domain"]["candidates"], [])
        self.assertFalse(result.metrics["surface_domain"]["constructible_allowed"])

    def test_go_to_surface_can_use_alternate_ascent_column(self):
        class RisingBody(FakeBody):
            def execute(self, action: Action) -> Result:
                result = super().execute(action)
                if action.name == "jump":
                    self.state_pos = (self.state_pos[0], self.state_pos[1] + 1.0, self.state_pos[2])
                    self.terminal = Event(
                        seq=self.terminal.seq,
                        tick=self.terminal.tick,
                        bot=self.terminal.bot,
                        name="jumpDone",
                        data={
                            "action_id": action.id,
                            "success": True,
                            "final_pos": list(self.state_pos),
                            "stopped_reason": "completed",
                        },
                    )
                return result

        class MovingNavigator(FakeNavigator):
            def __init__(self, body, policy):
                super().__init__(result=True, reason="arrived")
                self.body = body
                self.policy = policy

            def navigate_to(self, goal, **kwargs):
                result = super().navigate_to(goal, **kwargs)
                selected = goal_position(goal)
                support = (selected[0], selected[1] - 1, selected[2])
                self.body.blocks[support] = ("cobblestone", "SOLID")
                self.body.blocks[selected] = ("air", "CLEAR")
                self.body.blocks[(selected[0], selected[1] + 1, selected[2])] = ("air", "CLEAR")
                self.policy.record_bot_placement(support, "cobblestone", "pillar", self.body.bot_name)
                self.body.state_pos = (float(selected[0]), float(selected[1]), float(selected[2]))
                return result

        blocks = {
            (0, 64, 0): ("air", "CLEAR"),
            (0, 65, 0): ("stone", "SOLID"),
            (0, 66, 0): ("stone", "SOLID"),
            (1, 63, 0): ("stone", "SOLID"),
            (1, 64, 0): ("air", "CLEAR"),
            (1, 65, 0): ("air", "CLEAR"),
            (1, 66, 0): ("dirt", "SOLID"),
            (1, 67, 0): ("air", "CLEAR"),
            (2, 64, 0): ("stone", "SOLID"),
            (2, 65, 0): ("air", "CLEAR"),
            (2, 66, 0): ("air", "CLEAR"),
        }
        body = RisingBody(blocks=blocks, inventory_pages=[inventory_page([slot(9, "minecraft:cobblestone", 8)])])
        body.state_pos = (0.5, 64.0, 0.5)
        policy = GovernancePolicy(natural_regions=[Region("surface", (-10, 0, -10), (10, 120, 10))])
        navigator = MovingNavigator(body, policy)
        runtime = BlockWork(body, policy, navigator=navigator)

        result = runtime.go_to_surface(timeout_s=1.0, surface_scan_height=2, world_top_y=70)

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(result.reason, "surface_reached")
        self.assertEqual(result.metrics["target_surface"], [1, 65, 0])
        self.assertEqual(result.metrics["final_pos"], [1, 65, 0])
        self.assertEqual(result.metrics["terminal_surface"]["support_legality"]["reason"], "allowed_bot_owned")
        self.assertIsInstance(navigator.calls[0][0], GoalComposite)
        self.assertEqual(len(navigator.calls), 1)
        self.assertEqual(navigator.calls[0][1]["arrival_radius"], 0.25)

    def test_go_to_surface_can_route_to_wider_exit_after_ascent(self):
        class RisingBody(FakeBody):
            def execute(self, action: Action) -> Result:
                result = super().execute(action)
                if action.name == "jump":
                    self.state_pos = (self.state_pos[0], self.state_pos[1] + 1.0, self.state_pos[2])
                    self.terminal = Event(
                        seq=self.terminal.seq,
                        tick=self.terminal.tick,
                        bot=self.terminal.bot,
                        name="jumpDone",
                        data={
                            "action_id": action.id,
                            "success": True,
                            "final_pos": list(self.state_pos),
                            "stopped_reason": "completed",
                        },
                    )
                return result

        class MovingNavigator(FakeNavigator):
            def __init__(self, body):
                super().__init__(result=True, reason="arrived")
                self.body = body

            def navigate_to(self, goal, **kwargs):
                result = super().navigate_to(goal, **kwargs)
                selected = goal_position(goal)
                self.body.state_pos = (float(selected[0]), float(selected[1]), float(selected[2]))
                return result

        blocks = {
            (0, 64, 0): ("air", "CLEAR"),
            (0, 65, 0): ("air", "CLEAR"),
            (0, 66, 0): ("air", "CLEAR"),
            (1, 64, 0): ("stone", "SOLID"),
            (1, 65, 0): ("air", "CLEAR"),
            (1, 66, 0): ("air", "CLEAR"),
            (1, 67, 0): ("dirt", "SOLID"),
            (2, 64, 0): ("stone", "SOLID"),
            (2, 65, 0): ("air", "CLEAR"),
            (2, 66, 0): ("air", "CLEAR"),
        }
        body = RisingBody(blocks=blocks, inventory_pages=[inventory_page([slot(9, "minecraft:cobblestone", 8)])])
        body.state_pos = (0.5, 64.0, 0.5)
        policy = GovernancePolicy(natural_regions=[Region("surface", (-10, 0, -10), (10, 120, 10))])
        navigator = MovingNavigator(body)
        runtime = BlockWork(body, policy, navigator=navigator)

        result = runtime.go_to_surface(timeout_s=1.0, surface_scan_height=2, surface_scan_radius=2, world_top_y=70)

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(result.reason, "surface_reached")
        self.assertEqual(result.metrics["target_surface"], [2, 65, 0])
        self.assertEqual(result.metrics["final_pos"], [2, 65, 0])
        self.assertIsInstance(navigator.calls[0][0], GoalComposite)
        self.assertEqual(len(navigator.calls), 1)
        self.assertEqual(navigator.calls[0][1]["arrival_radius"], 0.25)

    def test_go_to_surface_can_use_shared_navigation_staircase_fallback(self):
        class MovingNavigator(FakeNavigator):
            def __init__(self, body):
                super().__init__(result=True, reason="arrived")
                self.body = body

            def navigate_to(self, goal, **kwargs):
                self.calls.append((goal, kwargs))
                selected = goal.goals[1].representative((0, 0, 0))
                self.body.state_pos = (float(selected[0]), float(selected[1]), float(selected[2]))
                return ToolResult(
                    success=True,
                    reason="arrived",
                    can_retry=False,
                    metrics={"goal": list(selected), "selected_goal": list(selected)},
                )

        blocks = {
            (0, 63, 0): ("stone", "SOLID"),
            (0, 64, 0): ("air", "CLEAR"),
            (0, 65, 0): ("air", "CLEAR"),
            (0, 66, 0): ("dirt", "SOLID"),
            (1, 63, 0): ("air", "CLEAR"),
            (1, 64, 0): ("stone", "SOLID"),
            (1, 65, 0): ("air", "CLEAR"),
            (1, 66, 0): ("air", "CLEAR"),
        }
        body = FakeBody(blocks=blocks, inventory_pages=[inventory_page([slot(9, "minecraft:cobblestone", 8)])])
        body.state_pos = (0.5, 64.0, 0.5)
        policy = GovernancePolicy(natural_regions=[Region("surface", (-10, 0, -10), (10, 120, 10))])
        navigator = MovingNavigator(body)
        runtime = BlockWork(body, policy, navigator=navigator)

        result = runtime.go_to_surface(
            timeout_s=1.0,
            surface_scan_height=2,
            world_top_y=70,
        )

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(result.reason, "surface_reached")
        self.assertEqual(result.metrics["target_surface"], [1, 65, 0])
        self.assertEqual(result.metrics["terminal_surface"]["candidate"], True)
        self.assertIsInstance(navigator.calls[0][0], GoalComposite)
        self.assertEqual(len(navigator.calls), 1)
        self.assertEqual(navigator.calls[0][1]["arrival_radius"], 0.25)
        self.assertEqual(body.actions, [])

    def test_go_to_surface_can_use_multi_step_shared_navigation_staircase_fallback(self):
        class MovingNavigator(FakeNavigator):
            def __init__(self, body):
                super().__init__(result=True, reason="arrived")
                self.body = body

            def navigate_to(self, goal, **kwargs):
                result = super().navigate_to(goal, **kwargs)
                selected = goal_position(goal)
                self.body.state_pos = (float(selected[0]), float(selected[1]), float(selected[2]))
                return result

        blocks = {
            (0, 63, 0): ("stone", "SOLID"),
            (0, 64, 0): ("air", "CLEAR"),
            (0, 65, 0): ("air", "CLEAR"),
            (0, 66, 0): ("chest", "SOLID"),
            (1, 64, 0): ("stone", "SOLID"),
            (1, 65, 0): ("air", "CLEAR"),
            (1, 66, 0): ("air", "CLEAR"),
            (1, 67, 0): ("chest", "SOLID"),
            (2, 65, 0): ("stone", "SOLID"),
            (2, 66, 0): ("air", "CLEAR"),
            (2, 67, 0): ("air", "CLEAR"),
        }
        body = FakeBody(blocks=blocks, inventory_pages=[inventory_page([slot(9, "minecraft:cobblestone", 8)])])
        body.state_pos = (0.5, 64.0, 0.5)
        policy = GovernancePolicy(natural_regions=[Region("surface", (-10, 0, -10), (10, 120, 10))])
        navigator = MovingNavigator(body)
        runtime = BlockWork(body, policy, navigator=navigator)

        result = runtime.go_to_surface(
            timeout_s=1.0,
            surface_scan_height=2,
            surface_scan_radius=2,
            world_top_y=70,
        )

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(result.reason, "surface_reached")
        self.assertEqual(result.metrics["target_surface"], [2, 66, 0])
        self.assertEqual(result.metrics["terminal_surface"]["candidate"], True)
        self.assertIsInstance(navigator.calls[0][0], GoalComposite)
        self.assertEqual(len(navigator.calls), 1)
        self.assertEqual(navigator.calls[0][1]["arrival_radius"], 0.25)
        self.assertEqual(body.actions, [])

    def test_go_to_surface_reports_navigation_missing_for_adjacent_surface_exit(self):
        class RisingBody(FakeBody):
            def execute(self, action: Action) -> Result:
                result = super().execute(action)
                if action.name == "jump":
                    self.state_pos = (self.state_pos[0], self.state_pos[1] + 1.0, self.state_pos[2])
                    self.terminal = Event(
                        seq=self.terminal.seq,
                        tick=self.terminal.tick,
                        bot=self.terminal.bot,
                        name="jumpDone",
                        data={
                            "action_id": action.id,
                            "success": True,
                            "final_pos": list(self.state_pos),
                            "stopped_reason": "completed",
                        },
                    )
                return result

        blocks = {
            (0, 64, 0): ("air", "CLEAR"),
            (0, 65, 0): ("air", "CLEAR"),
            (0, 66, 0): ("air", "CLEAR"),
            (1, 64, 0): ("stone", "SOLID"),
            (1, 65, 0): ("air", "CLEAR"),
            (1, 66, 0): ("air", "CLEAR"),
        }
        body = RisingBody(blocks=blocks, inventory_pages=[inventory_page([slot(9, "minecraft:cobblestone", 8)])])
        body.state_pos = (0.5, 64.0, 0.5)
        policy = GovernancePolicy(natural_regions=[Region("surface", (-10, 0, -10), (10, 120, 10))])
        runtime = BlockWork(body, policy)

        result = runtime.go_to_surface(timeout_s=1.0, surface_scan_height=4, world_top_y=70)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "surface_navigation_missing")
        self.assertEqual(result.metrics["surface_domain"]["candidates"][0]["feet_pos"], [1, 65, 0])
        self.assertEqual(body.state_pos, (0.5, 64.0, 0.5))

    def test_go_to_surface_reports_not_found_when_no_sky_exposed_natural_surface_exists(self):
        blocks = {
            (0, 64, 0): ("air", "CLEAR"),
            (0, 65, 0): ("stone", "SOLID"),
            (1, 64, 0): ("air", "CLEAR"),
            (1, 65, 0): ("air", "CLEAR"),
            (1, 66, 0): ("stone", "SOLID"),
            (-1, 64, 0): ("air", "CLEAR"),
            (-1, 65, 0): ("air", "CLEAR"),
            (-1, 66, 0): ("stone", "SOLID"),
            (0, 64, 1): ("air", "CLEAR"),
            (0, 65, 1): ("air", "CLEAR"),
            (0, 66, 1): ("stone", "SOLID"),
            (0, 64, -1): ("air", "CLEAR"),
            (0, 65, -1): ("air", "CLEAR"),
            (0, 66, -1): ("stone", "SOLID"),
        }
        body = FakeBody(blocks=blocks)
        body.state_pos = (0.5, 64.0, 0.5)
        policy = GovernancePolicy(natural_regions=[Region("surface", (-10, 0, -10), (10, 120, 10))])
        runtime = BlockWork(body, policy)

        result = runtime.go_to_surface(timeout_s=1.0, surface_scan_height=1, world_top_y=70)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "surface_not_found_in_column")
        self.assertEqual(result.metrics["origin"], [0, 64, 0])
        self.assertEqual(body.actions, [])

    def test_sky_exposed_batches_surface_window(self):
        class NativeBatchBody(FakeBody):
            def perceive(self, scope: str, params: dict[str, object]) -> PerceptionResult:
                self.perceptions.append((scope, params))
                if scope == "blockCells":
                    cells = params.get("cells") or []
                    facts = []
                    for c in cells:
                        pos = (int(c[0]), int(c[1]), int(c[2]))
                        block_type, state = self.blocks.get(pos, ("air", "CLEAR"))
                        facts.append(
                            {
                                "x": pos[0],
                                "y": pos[1],
                                "z": pos[2],
                                "type": block_type,
                                "state": state,
                                "properties": {},
                            }
                        )
                    return PerceptionResult(
                        bot="Bot1",
                        scope="blockCells",
                        type="perception",
                        ok=True,
                        complete=True,
                        data={"count": len(facts), "total": len(cells), "next": None, "cells": facts},
                    )
                return super().perceive(scope, params)

        blocks = {
            (0, 64, 0): ("air", "CLEAR"),
            (0, 65, 0): ("air", "CLEAR"),
            (0, 66, 0): ("air", "CLEAR"),
            (0, 67, 0): ("stone", "SOLID"),
        }
        body = NativeBatchBody(blocks=blocks)
        runtime = BlockWork(body, GovernancePolicy())

        sky = runtime.sky_exposed((0, 64, 0), world_top_y=70)

        self.assertEqual(sky["exposed"], False)
        self.assertEqual(sky["first_blocker"], {"pos": [0, 67, 0], "block_type": "stone", "block_state": "SOLID"})
        self.assertEqual(
            [params for scope, params in body.perceptions if scope == "blockCells"],
            [{"cells": [[0, 66, 0], [0, 67, 0], [0, 68, 0], [0, 69, 0], [0, 70, 0]], "start": 0, "limit": 64}],
        )
        self.assertEqual([params for scope, params in body.perceptions if scope == "blockAt"], [])

    def test_constructible_surface_column_batches_and_falls_back_on_batch_failure(self):
        blocks = {
            (0, 63, 0): ("stone", "SOLID"),
            (0, 64, 0): ("air", "CLEAR"),
            (0, 65, 0): ("air", "CLEAR"),
            (0, 66, 0): ("air", "CLEAR"),
            (0, 67, 0): ("air", "CLEAR"),
            (0, 68, 0): ("air", "CLEAR"),
        }
        body = ScopeBatchFailureBody(blocks=blocks, failed_scope="surface_column")
        runtime = BlockWork(body, GovernancePolicy(natural_regions=[Region("surface", (-10, 0, -10), (10, 120, 10))]))

        result = runtime._constructible_surface_column_at((0, 64, 0), target_y=67, world_top_y=70)

        self.assertIsInstance(result, dict)
        self.assertTrue(result["constructible"])
        self.assertEqual(result["reason"], "constructible")
        self.assertTrue(
            any(scope == "blockCells" for scope, _params in body.perceptions),
            body.perceptions,
        )
        self.assertIn(("blockAt", {"x": 0, "y": 65, "z": 0}), body.perceptions)

    def test_place_block_denies_unknown_region_without_executing_action(self):
        body = FakeBody(
            PerceptionResult(
                bot="Bot1",
                scope="blockAt",
                type="perception",
                ok=True,
                complete=True,
                data={"x": 100, "y": 64, "z": 100, "type": "air", "state": "CLEAR"},
            )
        )
        body.state_pos = (0.5, 64.0, 0.5)
        runtime = BlockWork(body, GovernancePolicy())

        result = runtime.place_block((100, 64, 100), "cobblestone", context=PlaceContext.TRAVEL)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "place_denied:unknown_provenance")
        self.assertEqual(body.actions, [])

    def test_place_block_executes_and_records_bot_ledger_on_success(self):
        body = FakeBody(
            perception=PerceptionResult(
                bot="Bot1",
                scope="blockAt",
                type="perception",
                ok=True,
                complete=True,
                data={"x": 1, "y": 64, "z": 0, "type": "air", "state": "CLEAR"},
            ),
            terminal=Event(
                seq=1,
                tick=10,
                bot="Bot1",
                name="placeDone",
                data={"action_id": "placeholder", "success": True, "block_at_target": "cobblestone"},
            )
        )
        body.state_pos = (0.5, 64.0, 0.5)
        policy = GovernancePolicy(natural_regions=[Region("work", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy)

        result = runtime.place_block((1, 64, 0), "minecraft:cobblestone", context=PlaceContext.WORK, purpose="bridge")

        self.assertTrue(result.success)
        self.assertEqual(len(body.actions), 1)
        self.assertEqual(body.actions[0].name, "placeBlock")
        self.assertEqual(body.actions[0].params["target"], [1, 64, 0])
        self.assertEqual(body.actions[0].params["purpose"], "bridge")
        self.assertEqual(body.actions[0].params["timeout_ticks"], 600)

        cleanup = policy.can_break((1, 64, 0), "cobblestone", BreakContext.BOT_CLEANUP)
        self.assertTrue(cleanup.allowed)
        self.assertTrue(cleanup.bot_owned)

    def test_place_block_does_not_execute_when_target_occupied(self):
        body = FakeBody(
            PerceptionResult(
                bot="Bot1",
                scope="blockAt",
                type="perception",
                ok=True,
                complete=True,
                data={"x": 1, "y": 64, "z": 0, "type": "stone", "state": "SOLID"},
            )
        )
        policy = GovernancePolicy(natural_regions=[Region("work", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy)

        result = runtime.place_block((1, 64, 0), "minecraft:cobblestone", context=PlaceContext.WORK)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "place_denied:target_occupied")
        self.assertEqual(body.actions, [])
        self.assertEqual(result.metrics["block_at_target"], "stone")

    def test_place_block_does_not_execute_when_perception_incomplete(self):
        body = FakeBody(
            PerceptionResult(
                bot="Bot1",
                scope="blockAt",
                type="perception",
                ok=True,
                complete=False,
                data={},
                uncertainty=[{"reason": "limit_exceeded"}],
            )
        )
        policy = GovernancePolicy(natural_regions=[Region("work", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy)

        result = runtime.place_block((1, 64, 0), "minecraft:cobblestone", context=PlaceContext.WORK)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "perception_failed")
        self.assertEqual(body.actions, [])

    def test_place_block_denies_body_head_collision_without_executing_action(self):
        body = FakeBody(
            blocks={(0, 65, 0): ("air", "CLEAR")},
        )
        body.state_pos = (0.5, 64.0, 0.5)
        policy = GovernancePolicy(natural_regions=[Region("work", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy)

        result = runtime.place_block((0, 65, 0), "minecraft:cobblestone", context=PlaceContext.WORK)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "place_denied:body_collision")
        self.assertEqual(result.metrics["collision_part"], "head")
        self.assertEqual(body.actions, [])

    def test_place_block_allows_pillar_placement_at_body_feet(self):
        body = FakeBody(
            blocks={(0, 64, 0): ("air", "CLEAR")},
        )
        body.state_pos = (0.5, 64.0, 0.5)
        policy = GovernancePolicy(natural_regions=[Region("work", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy)

        result = runtime.place_block(
            (0, 64, 0),
            "minecraft:cobblestone",
            context=PlaceContext.WORK,
            purpose="pillar",
        )

        self.assertTrue(result.success)
        self.assertEqual(len(body.actions), 1)
        self.assertEqual(body.actions[0].name, "placeBlock")

    def test_place_here_chooses_nearest_supported_clear_spot(self):
        body = FakeBody(
            blocks={
                (0, 63, 0): ("stone", "SOLID"),
                (0, 64, 1): ("air", "CLEAR"),
                (0, 63, 1): ("stone", "SOLID"),
                (1, 64, 0): ("air", "CLEAR"),
                (1, 63, 0): ("stone", "SOLID"),
            },
        )
        body.state_pos = (0.5, 64.0, 0.5)
        policy = GovernancePolicy(natural_regions=[Region("work", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy)

        result = runtime.place_here("minecraft:cobblestone", radius=1, context=PlaceContext.WORK, purpose="bridge")

        self.assertTrue(result.success)
        self.assertIn(result.metrics["place_here"]["chosen_target"], ([0, 64, 1], [1, 64, 0]))
        self.assertEqual(body.actions[-1].name, "placeBlock")
        self.assertEqual(body.actions[-1].params["face"], "up")

    def test_place_here_reports_no_supported_spot(self):
        body = FakeBody(
            blocks={
                (0, 63, 0): ("stone", "SOLID"),
                (0, 64, 1): ("air", "CLEAR"),
                (0, 63, 1): ("water", "LIQUID"),
                (1, 64, 0): ("air", "CLEAR"),
                (1, 63, 0): ("water", "LIQUID"),
            },
        )
        body.state_pos = (0.5, 64.0, 0.5)
        policy = GovernancePolicy(natural_regions=[Region("work", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy)

        result = runtime.place_here("minecraft:cobblestone", radius=1, context=PlaceContext.WORK)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "place_here_no_supported_spot")
        self.assertTrue(result.metrics["scan"]["vertical_fallback"])
        self.assertGreater(result.metrics["scan"]["rejection_counts"]["support_liquid"], 0)
        self.assertEqual(body.actions, [])

    def test_place_here_finds_higher_natural_surface_when_same_level_has_no_support(self):
        body = FakeBody(
            blocks={
                (0, 63, 0): ("stone", "SOLID"),
                (1, 66, 0): ("grass_block", "SOLID"),
                (2, 66, 0): ("grass_block", "SOLID"),
            },
        )
        body.state_pos = (0.5, 64.0, 0.5)
        navigator = FakeNavigator()
        navigator.body = body
        runtime = BlockWork(
            body,
            GovernancePolicy(natural_regions=[Region("work", (-10, 0, -10), (10, 100, 10))]),
            navigator=navigator,
        )

        result = runtime.place_here(
            "minecraft:crafting_table",
            radius=1,
            context=PlaceContext.DIRECT,
            purpose="workstation",
        )

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(result.metrics["place_here"]["chosen_target"], [1, 67, 0])
        self.assertEqual(result.metrics["place_here"]["scan"]["vertical_fallback"], True)
        self.assertEqual(result.metrics["place_here"]["candidates"][0]["vertical_delta"], 3)
        self.assertEqual(body.actions[-1].params["target"], [1, 67, 0])

    def test_place_here_finds_lower_natural_surface_when_same_level_has_no_support(self):
        body = FakeBody(
            blocks={
                (0, 63, 0): ("stone", "SOLID"),
                (1, 61, 0): ("grass_block", "SOLID"),
                (2, 61, 0): ("grass_block", "SOLID"),
            },
        )
        body.state_pos = (0.5, 64.0, 0.5)
        navigator = FakeNavigator()
        navigator.body = body
        runtime = BlockWork(
            body,
            GovernancePolicy(natural_regions=[Region("work", (-10, 0, -10), (10, 100, 10))]),
            navigator=navigator,
        )

        result = runtime.place_here("minecraft:cobblestone", radius=1, context=PlaceContext.WORK)

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(result.metrics["place_here"]["chosen_target"], [1, 62, 0])
        self.assertEqual(result.metrics["place_here"]["candidates"][0]["vertical_delta"], -2)
        self.assertEqual(body.actions[-1].params["target"], [1, 62, 0])

    def test_place_here_rejects_occupied_vertical_surface(self):
        body = FakeBody(
            blocks={
                (0, 63, 0): ("stone", "SOLID"),
                (1, 67, 0): ("grass_block", "SOLID"),
                (1, 68, 0): ("stone", "SOLID"),
            },
        )
        body.state_pos = (0.5, 64.0, 0.5)
        runtime = BlockWork(
            body,
            GovernancePolicy(natural_regions=[Region("work", (-10, 0, -10), (10, 100, 10))]),
        )

        result = runtime.place_here("minecraft:cobblestone", radius=1, context=PlaceContext.WORK)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "place_here_no_supported_spot")
        self.assertGreater(result.metrics["scan"]["rejection_counts"]["target_not_clear"], 0)
        self.assertEqual(body.actions, [])

    def test_place_here_preserves_governance_for_vertical_surface(self):
        body = FakeBody(
            blocks={
                (0, 63, 0): ("stone", "SOLID"),
                (0, 65, 0): ("stone", "SOLID"),
                (1, 66, 0): ("grass_block", "SOLID"),
            },
        )
        body.state_pos = (0.5, 64.0, 0.5)
        navigator = FakeNavigator()
        navigator.body = body
        runtime = BlockWork(
            body,
            GovernancePolicy(
                natural_regions=[Region("work", (-10, 0, -10), (10, 100, 10))],
                protected_regions=[Region("protected", (1, 67, 0), (1, 67, 0))],
            ),
            navigator=navigator,
        )

        result = runtime.place_here(
            "minecraft:crafting_table",
            radius=1,
            context=PlaceContext.DIRECT,
            purpose="workstation",
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "place_here_no_placeable_spot")
        self.assertEqual(result.metrics["attempts"][0]["result"]["reason"], "place_denied:protected_region")
        self.assertEqual(body.actions, [])

    def test_place_here_vertical_scan_has_hard_column_and_candidate_budgets(self):
        blocks = {(0, 63, 0): ("stone", "SOLID")}
        for dz in range(-6, 7):
            for dx in range(-6, 7):
                if dx == 0 and dz == 0:
                    continue
                blocks[(dx, 64, dz)] = ("stone", "SOLID")
                blocks[(dx, 66, dz)] = ("stone", "SOLID")
        body = FakeBody(blocks=blocks)
        body.state_pos = (0.5, 64.0, 0.5)

        scan = _scan_place_here_candidates(
            body,
            (0, 64, 0),
            6,
            vertical_radius=4,
            column_limit=32,
            candidate_limit=32,
        )

        self.assertFalse(isinstance(scan, ToolResult))
        self.assertEqual(scan.diagnostics["columns_total"], 168)
        self.assertEqual(scan.diagnostics["columns_scanned"], 32)
        self.assertFalse(scan.diagnostics["columns_complete"])
        self.assertEqual(scan.diagnostics["supported_total"], 64)
        self.assertEqual(scan.diagnostics["candidate_count"], 32)
        self.assertFalse(scan.diagnostics["candidates_complete"])
        self.assertEqual(len(scan.candidates), 32)

    def test_place_here_skips_denied_candidate_and_uses_next_supported_spot(self):
        body = FakeBody(
            blocks={
                (0, 63, 0): ("stone", "SOLID"),
                (0, 64, 1): ("air", "CLEAR"),
                (0, 63, 1): ("stone", "SOLID"),
                (-1, 64, 0): ("air", "CLEAR"),
                (-1, 63, 0): ("stone", "SOLID"),
            },
        )
        body.state_pos = (0.5, 64.0, 0.5)
        policy = GovernancePolicy(
            natural_regions=[Region("work", (-10, 0, -10), (10, 100, 10))],
            protected_regions=[Region("deny-first", (-1, 64, 0), (-1, 64, 0))],
        )
        runtime = BlockWork(body, policy)

        result = runtime.place_here("minecraft:cobblestone", radius=1, context=PlaceContext.WORK)

        self.assertTrue(result.success)
        self.assertEqual(result.metrics["place_here"]["attempts"][0]["result"]["reason"], "place_denied:protected_region")
        self.assertEqual(result.metrics["place_here"]["chosen_target"], [0, 64, 1])
        self.assertEqual(body.actions[-1].params["target"], [0, 64, 1])

    def test_place_here_skips_physical_timeout_candidate_and_uses_next_supported_spot(self):
        class TimeoutFirstPlaceBody(FakeBody):
            def execute(self, action):
                result = super().execute(action)
                if action.name == "placeBlock" and len([item for item in self.actions if item.name == "placeBlock"]) == 1:
                    target = tuple(action.params["target"])
                    self.blocks[target] = ("air", "CLEAR")
                    self.terminal = Event(
                        seq=self.terminal.seq,
                        tick=self.terminal.tick,
                        bot=self.terminal.bot,
                        name="placeDone",
                        data={
                            "action_id": action.id,
                            "success": False,
                            "block_at_target": "air",
                            "stopped_reason": "timeout",
                        },
                    )
                return result

        body = TimeoutFirstPlaceBody(
            blocks={
                (0, 63, 0): ("stone", "SOLID"),
                (0, 64, 1): ("air", "CLEAR"),
                (0, 63, 1): ("stone", "SOLID"),
                (1, 64, 0): ("air", "CLEAR"),
                (1, 63, 0): ("stone", "SOLID"),
            },
        )
        body.state_pos = (0.5, 64.0, 0.5)
        policy = GovernancePolicy(natural_regions=[Region("work", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy)

        result = runtime.place_here("minecraft:cobblestone", radius=1, context=PlaceContext.WORK)

        self.assertTrue(result.success)
        place_actions = [action for action in body.actions if action.name == "placeBlock"]
        self.assertEqual(len(place_actions), 2)
        self.assertEqual(result.metrics["place_here"]["attempts"][0]["result"]["reason"], "timeout")
        self.assertEqual(result.metrics["place_here"]["chosen_target"], place_actions[-1].params["target"])
        self.assertNotEqual(place_actions[0].params["target"], place_actions[-1].params["target"])

    def test_place_here_reports_navigation_missing_when_only_remote_stand_point_exists(self):
        body = FakeBody(
            blocks={
                (0, 63, 0): ("air", "CLEAR"),
                (1, 64, 0): ("air", "CLEAR"),
                (1, 63, 0): ("stone", "SOLID"),
                (2, 64, 0): ("air", "CLEAR"),
                (2, 65, 0): ("air", "CLEAR"),
                (2, 63, 0): ("stone", "SOLID"),
            },
        )
        body.state_pos = (0.5, 64.0, 0.5)
        policy = GovernancePolicy(natural_regions=[Region("work", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy)

        result = runtime.place_here("minecraft:cobblestone", radius=1, context=PlaceContext.WORK)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "place_here_navigation_missing")
        self.assertEqual(body.actions, [])

    def test_place_here_requires_verified_stand_feet_after_navigation(self):
        class MissThenHitNavigator(FakeNavigator):
            def __init__(self, body):
                super().__init__(result=True, reason="arrived")
                self.body = body

            def navigate_to(self, goal, **kwargs):
                self.calls.append((goal, kwargs))
                if len(self.calls) == 1:
                    self.body.state_pos = (0.5, 64.0, 0.5)
                else:
                    selected = goal_position(goal)
                    self.body.state_pos = (float(selected[0]), float(selected[1]), float(selected[2]))
                return ToolResult(success=True, reason="arrived", can_retry=False, metrics={"goal": list(goal), "kwargs": kwargs})

        body = FakeBody(
            blocks={
                (0, 63, 0): ("air", "CLEAR"),
                (1, 64, 0): ("air", "CLEAR"),
                (1, 63, 0): ("stone", "SOLID"),
                (2, 64, 0): ("air", "CLEAR"),
                (2, 65, 0): ("air", "CLEAR"),
                (2, 63, 0): ("stone", "SOLID"),
                (1, 64, 1): ("air", "CLEAR"),
                (1, 65, 1): ("air", "CLEAR"),
                (1, 63, 1): ("stone", "SOLID"),
                (1, 64, -1): ("air", "CLEAR"),
                (1, 65, -1): ("air", "CLEAR"),
                (1, 63, -1): ("stone", "SOLID"),
            },
        )
        body.state_pos = (0.5, 64.0, 0.5)
        policy = GovernancePolicy(natural_regions=[Region("work", (-10, 0, -10), (10, 100, 10))])
        navigator = MissThenHitNavigator(body)
        runtime = BlockWork(body, policy, navigator=navigator)

        result = runtime.place_here("minecraft:cobblestone", radius=1, context=PlaceContext.WORK)

        self.assertTrue(result.success)
        approach = result.metrics["place_here"]["attempts"][0]["approach"]
        self.assertEqual(approach["attempts"][0]["reason"], "stand_point_missed")
        self.assertEqual(approach["attempts"][0]["final_feet"], [0, 64, 0])
        self.assertEqual(len(navigator.calls), 2)
        self.assertEqual(navigator.calls[0][1]["arrival_radius"], 0.25)
        self.assertEqual(navigator.calls[1][1]["arrival_radius"], 0.25)
        self.assertEqual(approach["stand_target"], list(navigator.calls[1][0]))

    def test_place_here_recovers_headroom_by_mining_one_adjacent_head_block(self):
        class MovingNavigator(FakeNavigator):
            def __init__(self, body):
                super().__init__(result=True, reason="arrived")
                self.body = body

            def navigate_to(self, goal, **kwargs):
                result = super().navigate_to(goal, **kwargs)
                selected = goal_position(goal)
                self.body.state_pos = (float(selected[0]), float(selected[1]), float(selected[2]))
                return result

        body = FakeBody(
            blocks={
                (0, 63, 0): ("air", "CLEAR"),
                (1, 64, 0): ("air", "CLEAR"),
                (1, 63, 0): ("stone", "SOLID"),
                (2, 64, 0): ("air", "CLEAR"),
                (2, 63, 0): ("stone", "SOLID"),
                (2, 65, 0): ("stone", "SOLID"),
                (1, 64, 1): ("air", "CLEAR"),
                (1, 63, 1): ("air", "CLEAR"),
                (1, 64, -1): ("air", "CLEAR"),
                (1, 63, -1): ("air", "CLEAR"),
            },
        )
        body.state_pos = (0.5, 64.0, 0.5)
        policy = GovernancePolicy(natural_regions=[Region("work", (-10, 0, -10), (10, 100, 10))])
        navigator = MovingNavigator(body)
        runtime = BlockWork(body, policy, navigator=navigator)

        result = runtime.place_here("minecraft:cobblestone", radius=1, context=PlaceContext.WORK)

        self.assertTrue(result.success)
        self.assertEqual([action.name for action in body.actions], ["mineBlock", "placeBlock"])
        self.assertEqual(body.actions[0].params["target"], [2, 65, 0])
        self.assertTrue(any(isinstance(goal, GoalComposite) for goal, _kwargs in navigator.calls))
        self.assertIn(
            ((2, 64, 0), {"timeout_s": 30.0, "break_context": BreakContext.TRAVEL, "arrival_radius": 0.25}),
            navigator.calls,
        )
        self.assertEqual(body.actions[-1].name, "placeBlock")
        self.assertTrue(result.metrics["place_here"]["headroom_recovery"]["recovered"])

    def test_place_here_recovers_stand_position_by_mining_one_adjacent_block(self):
        class MovingNavigator(FakeNavigator):
            def __init__(self, body):
                super().__init__(result=True, reason="arrived")
                self.body = body

            def navigate_to(self, goal, **kwargs):
                result = super().navigate_to(goal, **kwargs)
                selected = goal_position(goal)
                self.body.state_pos = (float(selected[0]), float(selected[1]), float(selected[2]))
                return result

        body = FakeBody(
            blocks={
                (0, 63, 0): ("air", "CLEAR"),
                (1, 64, 0): ("air", "CLEAR"),
                (1, 63, 0): ("stone", "SOLID"),
                (2, 64, 0): ("stone", "SOLID"),
                (2, 65, 0): ("air", "CLEAR"),
                (2, 63, 0): ("stone", "SOLID"),
                (1, 64, 1): ("air", "CLEAR"),
                (1, 63, 1): ("air", "CLEAR"),
                (1, 64, -1): ("air", "CLEAR"),
                (1, 63, -1): ("air", "CLEAR"),
            },
        )
        body.state_pos = (0.5, 64.0, 0.5)
        policy = GovernancePolicy(natural_regions=[Region("work", (-10, 0, -10), (10, 100, 10))])
        navigator = MovingNavigator(body)
        runtime = BlockWork(body, policy, navigator=navigator)

        result = runtime.place_here("minecraft:cobblestone", radius=1, context=PlaceContext.WORK)

        self.assertTrue(result.success)
        self.assertEqual(body.actions[0].name, "mineBlock")
        self.assertEqual(body.actions[0].params["target"], [2, 64, 0])
        self.assertEqual(body.actions[-1].name, "placeBlock")
        self.assertIn(
            ((2, 64, 0), {"timeout_s": 30.0, "break_context": BreakContext.TRAVEL, "arrival_radius": 0.25}),
            navigator.calls,
        )
        self.assertTrue(result.metrics["place_here"]["stand_position_recovery"]["recovered"])
        self.assertEqual(result.metrics["place_here"]["stand_position_recovery"]["stand_pos"], [2, 64, 0])

    def test_place_here_reports_no_stand_point_when_headroom_cannot_be_cleared_legally(self):
        body = FakeBody(
            blocks={
                (0, 63, 0): ("air", "CLEAR"),
                (1, 64, 0): ("air", "CLEAR"),
                (1, 63, 0): ("stone", "SOLID"),
                (2, 64, 0): ("air", "CLEAR"),
                (2, 63, 0): ("stone", "SOLID"),
                (2, 65, 0): ("chest", "SOLID"),
                (1, 64, 1): ("air", "CLEAR"),
                (1, 63, 1): ("air", "CLEAR"),
                (1, 64, -1): ("air", "CLEAR"),
                (1, 63, -1): ("air", "CLEAR"),
            },
        )
        body.state_pos = (0.5, 64.0, 0.5)
        policy = GovernancePolicy(natural_regions=[Region("work", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy)

        result = runtime.place_here("minecraft:cobblestone", radius=1, context=PlaceContext.WORK)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "place_here_no_stand_point")
        self.assertFalse(result.metrics["headroom_recovery"]["recovered"])
        self.assertEqual(body.actions, [])

    def test_place_here_does_not_recover_stand_position_when_stand_block_is_illegal(self):
        body = FakeBody(
            blocks={
                (0, 63, 0): ("air", "CLEAR"),
                (1, 64, 0): ("air", "CLEAR"),
                (1, 63, 0): ("stone", "SOLID"),
                (2, 64, 0): ("chest", "SOLID"),
                (2, 65, 0): ("air", "CLEAR"),
                (2, 63, 0): ("stone", "SOLID"),
                (1, 64, 1): ("air", "CLEAR"),
                (1, 63, 1): ("air", "CLEAR"),
                (1, 64, -1): ("air", "CLEAR"),
                (1, 63, -1): ("air", "CLEAR"),
            },
        )
        body.state_pos = (0.5, 64.0, 0.5)
        policy = GovernancePolicy(natural_regions=[Region("work", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy)

        result = runtime.place_here("minecraft:cobblestone", radius=1, context=PlaceContext.WORK)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "place_here_no_stand_point")
        self.assertFalse(result.metrics["stand_position_recovery"]["recovered"])
        self.assertEqual(body.actions, [])

    def test_place_here_can_create_stand_point_by_clearing_stand_then_head(self):
        class MovingNavigator(FakeNavigator):
            def __init__(self, body):
                super().__init__(result=True, reason="arrived")
                self.body = body

            def navigate_to(self, goal, **kwargs):
                result = super().navigate_to(goal, **kwargs)
                selected = goal_position(goal)
                self.body.state_pos = (float(selected[0]), float(selected[1]), float(selected[2]))
                return result

        body = FakeBody(
            blocks={
                (0, 63, 0): ("air", "CLEAR"),
                (1, 64, 0): ("air", "CLEAR"),
                (1, 63, 0): ("stone", "SOLID"),
                (2, 64, 0): ("dirt", "SOLID"),
                (2, 65, 0): ("dirt", "SOLID"),
                (2, 63, 0): ("stone", "SOLID"),
                (1, 64, 1): ("air", "CLEAR"),
                (1, 63, 1): ("air", "CLEAR"),
                (1, 64, -1): ("air", "CLEAR"),
                (1, 63, -1): ("air", "CLEAR"),
            },
        )
        body.state_pos = (0.5, 64.0, 0.5)
        policy = GovernancePolicy(natural_regions=[Region("work", (-10, 0, -10), (10, 100, 10))])
        navigator = MovingNavigator(body)
        runtime = BlockWork(body, policy, navigator=navigator)

        result = runtime.place_here("minecraft:cobblestone", radius=1, context=PlaceContext.WORK)

        self.assertTrue(result.success)
        place_here = result.metrics["place_here"]
        self.assertTrue(place_here["stand_position_recovery"]["recovered"])
        self.assertTrue(place_here["headroom_recovery"]["recovered"])
        self.assertEqual(place_here["stand_position_recovery"]["stand_pos"], [2, 64, 0])
        self.assertEqual(place_here["headroom_recovery"]["head_pos"], [2, 65, 0])
        self.assertEqual(body.actions[0].name, "mineBlock")
        self.assertEqual(body.actions[0].params["target"], [2, 64, 0])
        self.assertEqual(body.actions[1].name, "mineBlock")
        self.assertEqual(body.actions[1].params["target"], [2, 65, 0])
        self.assertEqual(body.actions[-1].name, "placeBlock")
        self.assertTrue(any(isinstance(goal, GoalComposite) for goal, _kwargs in navigator.calls))
        self.assertIn(
            ((2, 64, 0), {"timeout_s": 30.0, "break_context": BreakContext.TRAVEL, "arrival_radius": 0.25}),
            navigator.calls,
        )

    def test_place_here_refuses_stand_point_creation_when_both_blockers_are_illegal(self):
        body = FakeBody(
            blocks={
                (0, 63, 0): ("air", "CLEAR"),
                (1, 64, 0): ("air", "CLEAR"),
                (1, 63, 0): ("stone", "SOLID"),
                (2, 64, 0): ("chest", "SOLID"),
                (2, 65, 0): ("chest", "SOLID"),
                (2, 63, 0): ("stone", "SOLID"),
                (1, 64, 1): ("air", "CLEAR"),
                (1, 63, 1): ("air", "CLEAR"),
                (1, 64, -1): ("air", "CLEAR"),
                (1, 63, -1): ("air", "CLEAR"),
            },
        )
        body.state_pos = (0.5, 64.0, 0.5)
        policy = GovernancePolicy(natural_regions=[Region("work", (-10, 0, -10), (10, 100, 10))])
        runtime = BlockWork(body, policy)

        result = runtime.place_here("minecraft:cobblestone", radius=1, context=PlaceContext.WORK)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "place_here_no_stand_point")
        self.assertFalse(result.metrics["stand_position_recovery"]["recovered"])
        self.assertFalse(result.metrics["headroom_recovery"]["recovered"])
        self.assertEqual(body.actions, [])


if __name__ == "__main__":
    unittest.main()
