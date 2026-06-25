import unittest
from types import SimpleNamespace

from minebot.body import NavigationRunConfig, NavigationTransactions
from minebot.body.navigation import make_block_at_prism_world_update
from minebot.body.world_read import read_block_cells_tiled
from minebot.game.governance import GovernancePolicy, Region
from minebot.contract import Action, BodyState, BreakContext, Event, PerceptionResult, Result
from minebot.game.navigation import (
    GoalAvoid,
    GoalNear,
    GoalXZ,
    GridCell,
    GridWorld,
    MoveKind,
    NavigationCostModel,
    NavigationSegment,
    PathResult,
    PathStep,
    RecheckResult,
    SegmentedNavigator,
)


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


def grid(width, depth=1, y=64):
    return {(x, y, z): GridCell() for x in range(width) for z in range(depth)}


def corridor(x_min, x_max, y=64):
    return {
        (x, y + dy, z): GridCell()
        for x in range(x_min, x_max + 1)
        for dy in (-1, 0, 1)
        for z in (-1, 0, 1)
    }


class FakeBody:
    bot_name = "Bot1"

    def __init__(self, states, *, accept=True, terminal_success=True, terminal_reasons=None, blocks=None):
        self.states = list(states)
        self.accept = accept
        self.terminal_success = terminal_success
        self.terminal_reasons = list(terminal_reasons or [])
        self.actions: list[Action] = []
        self.await_timeouts: list[float] = []
        self.blocks = dict(blocks or {})

    def get_state(self):
        if not self.states:
            return state_at((0, 64, 0))
        if len(self.states) == 1:
            return self.states[0]
        return self.states.pop(0)

    def perceive(self, scope: str, params: dict[str, object]) -> PerceptionResult:
        if scope != "blockAt":
            raise AssertionError(f"unexpected scope {scope}")
        pos = (int(params["x"]), int(params["y"]), int(params["z"]))
        raw = self.blocks.get(pos, ("air", "CLEAR"))
        if len(raw) == 2:
            block_type, state = raw
            properties = {}
        else:
            block_type, state, properties = raw
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

    def execute(self, action: Action) -> Result:
        self.actions.append(action)
        if action.name == "mineBlock":
            target = tuple(action.params["target"])
            self.blocks[target] = ("air", "CLEAR")
        elif action.name == "placeBlock":
            target = tuple(action.params["target"])
            block_type = str(action.params["block_type"])
            self.blocks[target] = (block_type, "SOLID")
        elif action.name == "useItem":
            for pos, raw in list(self.blocks.items()):
                if len(raw) == 2:
                    block_type, state = raw
                    properties = {}
                else:
                    block_type, state, properties = raw
                normalized = str(block_type).removeprefix("minecraft:")
                if normalized.endswith(("_fence_gate", "_door", "_trapdoor")) and state == "SOLID":
                    updated = dict(properties)
                    updated["open"] = "true"
                    self.blocks[pos] = (block_type, state, updated)
        return Result(
            id=action.id,
            bot="Bot1",
            type="result",
            ok=self.accept,
            accepted=self.accept,
            complete=True,
            data={"action": action.name},
            error=None if self.accept else "rejected",
        )

    def await_action_terminal(self, action_id: str, timeout_s: float = 15.0) -> Event:
        self.await_timeouts.append(timeout_s)
        action = next(action for action in self.actions if action.id == action_id)
        reason = self.terminal_reasons.pop(0) if self.terminal_reasons else None
        default_reason = "completed" if action.name in {"mineBlock", "placeBlock"} else ("arrived" if self.terminal_success else "stuck")
        stopped_reason = reason or default_reason
        arrived = stopped_reason == "arrived"
        success = stopped_reason in {"arrived", "completed"}
        event_name = {
            "moveTo": "moveDone",
            "mineBlock": "mineDone",
            "placeBlock": "placeDone",
            "useItem": "useDone",
        }.get(action.name, "moveDone")
        data = {"action_id": action_id, "stopped_reason": stopped_reason}
        if action.name == "moveTo":
            data["arrived"] = success
        elif action.name == "useItem":
            data["success"] = success
        else:
            data["success"] = success
        return Event(
            seq=len(self.actions),
            tick=10,
            bot="Bot1",
            name=event_name,
            data=data,
        )


class FakeNavigator:
    def __init__(self, segments, *, world=None, costs=None):
        self.segments = list(segments)
        self.calls = []
        self.world = world
        self.costs = costs

    def next_segment(self, start, goal, **kwargs):
        self.calls.append((start, goal, kwargs))
        if len(self.segments) == 1:
            return self.segments[0]
        return self.segments.pop(0)


class FakeWork:
    def __init__(self, success=True, reason="completed"):
        self.success = success
        self.reason = reason
        self.mine_calls = []
        self.place_calls = []
        self.dig_up_calls = []
        self.dig_down_calls = []

    def mine_block(self, pos, *, context, timeout_s=30.0):
        self.mine_calls.append((pos, context, timeout_s))
        from minebot.contract import ToolResult

        return ToolResult(success=self.success, reason=self.reason, can_retry=not self.success)

    def place_block(
        self,
        pos,
        block_type,
        *,
        face=None,
        context,
        purpose="scaffold",
        timeout_s=30.0,
    ):
        self.place_calls.append((pos, block_type, face, context, purpose, timeout_s))
        from minebot.contract import ToolResult

        return ToolResult(
            success=self.success,
            reason=self.reason,
            can_retry=not self.success,
            metrics={"target": list(pos), "block_type": block_type},
        )

    def dig_up_one(
        self,
        *,
        current_pos=None,
        context=BreakContext.DIRECT,
        timeout_s=30.0,
    ):
        self.dig_up_calls.append((current_pos, context, timeout_s))
        from minebot.contract import ToolResult

        return ToolResult(
            success=self.success,
            reason="dig_up_step_completed" if self.success else self.reason,
            can_retry=not self.success,
            metrics={"origin": list(current_pos) if current_pos is not None else None},
        )

    def dig_down_one(
        self,
        *,
        current_pos=None,
        context=BreakContext.DIRECT,
        timeout_s=30.0,
    ):
        self.dig_down_calls.append((current_pos, context, timeout_s))
        from minebot.contract import ToolResult

        return ToolResult(
            success=self.success,
            reason="dig_down_already_open" if self.success else self.reason,
            can_retry=not self.success,
            metrics={"dig_down": {"origin": list(current_pos) if current_pos is not None else None}},
        )

    def dig_down_to_y(
        self,
        target_y,
        *,
        current_pos=None,
        context=BreakContext.DIRECT,
        max_steps=None,
        dig_timeout_s=30.0,
        move_timeout_s=15.0,
    ):
        self.dig_down_calls.append((target_y, current_pos, context, max_steps, dig_timeout_s, move_timeout_s))
        from minebot.contract import ToolResult

        return ToolResult(
            success=self.success,
            reason="dig_down_target_reached" if self.success else self.reason,
            can_retry=not self.success,
            metrics={"origin": list(current_pos) if current_pos is not None else None, "target_y": target_y},
        )


class NavigationRuntimeTests(unittest.TestCase):
    def test_move_away_short_circuits_when_already_safe(self):
        nav = FakeNavigator([_segment("arrived", (2, 64, 0), success=True, reason="arrived")])
        body = FakeBody([state_at((10, 64, 0))])
        runtime = NavigationTransactions(body, nav)

        result = runtime.move_away((0.0, 64.0, 0.0), min_distance=6.0)

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "already_safe")
        self.assertEqual(body.actions, [])
        self.assertEqual(result.metrics["attempts"], [])

    def test_move_away_reports_no_candidate_when_radius_cannot_clear_band(self):
        nav = FakeNavigator([_segment("arrived", (2, 64, 0), success=True, reason="arrived")])
        body = FakeBody([state_at((0, 64, 0))])
        runtime = NavigationTransactions(body, nav)

        result = runtime.move_away((0.0, 64.0, 0.0), min_distance=100.0, candidate_radii=(1,), max_candidates=4)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "move_away_no_candidate")
        self.assertEqual(body.actions, [])

    def test_move_away_navigates_to_farther_candidate_and_verifies_distance_gain(self):
        nav = FakeNavigator([_segment("arrived", (-4, 64, -4), success=True, reason="arrived")])
        body = FakeBody([state_at((0, 64, 0)), state_at((0, 64, 0)), state_at((-4, 64, -4))])
        runtime = NavigationTransactions(body, nav)

        result = runtime.move_away((0.0, 64.0, 0.0), min_distance=3.0, candidate_radii=(4,), max_candidates=4)

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "moved_away")
        self.assertEqual(result.metrics["chosen_goal"], [4, 64, 4])
        self.assertGreater(result.metrics["final_distance"], result.metrics["initial_distance"])
        self.assertEqual(len(body.actions), 1)
        self.assertEqual(body.actions[0].name, "moveTo")
        self.assertEqual(result.metrics["navigation_goal"]["kind"], "avoid")
        self.assertEqual(result.metrics["navigation_goal"]["fallback"]["kind"], "block")
        self.assertIsInstance(nav.calls[0][1], GoalAvoid)

    def test_move_away_wraps_last_navigation_failure(self):
        nav = FakeNavigator([_segment("blocked", None, success=False, reason="no_path")])
        body = FakeBody([state_at((0, 64, 0)), state_at((0, 64, 0)), state_at((0, 64, 0))])
        runtime = NavigationTransactions(body, nav)

        result = runtime.move_away((0.0, 64.0, 0.0), min_distance=3.0, candidate_radii=(4,), max_candidates=1)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "move_away_failed:navigation_blocked:no_path")
        self.assertTrue(result.can_retry)

    def test_move_away_reports_no_candidate_from_local_world_for_avoid_goal(self):
        cells = {
            (0, 64, 0): GridCell(),
            (1, 64, 0): GridCell(),
            (0, 64, 1): GridCell(),
            (1, 64, 1): GridCell(),
        }
        nav = _navigator(cells)
        body = FakeBody([state_at((0, 64, 0))])
        runtime = NavigationTransactions(body, nav)

        result = runtime.move_away((0.0, 64.0, 0.0), min_distance=4.0, candidate_radii=(4,), max_candidates=4)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "move_away_no_candidate")
        self.assertEqual(body.actions, [])

    def test_move_away_rechecks_moving_hazard_and_can_escape_again(self):
        nav = FakeNavigator(
            [
                _segment("arrived", (4, 64, 0), success=True, reason="arrived"),
                _segment("arrived", (8, 64, 0), success=True, reason="arrived"),
            ]
        )
        body = FakeBody(
            [
                state_at((0, 64, 0)),
                state_at((0, 64, 0)),
                state_at((4, 64, 0)),
                state_at((4, 64, 0)),
                state_at((4, 64, 0)),
                state_at((8, 64, 0)),
                state_at((8, 64, 0)),
            ]
        )
        runtime = NavigationTransactions(body, nav)
        danger_positions = iter(((0.0, 64.0, 0.0), (2.0, 64.0, 0.0)))

        def refresh_danger():
            try:
                return next(danger_positions)
            except StopIteration:
                return (2.0, 64.0, 0.0)

        result = runtime.move_away(
            (0.0, 64.0, 0.0),
            min_distance=4.0,
            maintenance_checks=2,
            danger_refresh=refresh_danger,
            candidate_radii=(4,),
            max_candidates=4,
        )

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "moved_away")
        self.assertEqual(result.metrics["maintenance_checks"], 2)
        self.assertEqual(result.metrics["attempts"][0]["result"]["reason"], "arrived")
        self.assertEqual(result.metrics["attempts"][1]["result"]["reason"], "arrived")
        self.assertGreaterEqual(result.metrics["final_distance"], 4.0)
        self.assertEqual(len(body.actions), 2)

    def test_navigate_to_sends_move_to_and_returns_arrived(self):
        cells = grid(4)
        nav = _navigator(cells)
        body = FakeBody([state_at((0, 64, 0))])
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to((3, 64, 0), config=NavigationRunConfig(max_segments=2))

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "arrived")
        self.assertEqual(len(body.actions), 1)
        self.assertEqual(body.actions[0].name, "moveTo")
        self.assertEqual(body.actions[0].params["target"], [3, 64, 0])
        self.assertEqual(body.actions[0].params["waypoints"], [[1, 64, 0], [2, 64, 0], [3, 64, 0]])
        self.assertEqual(body.actions[0].params["final_goal"], [3, 64, 0])
        self.assertEqual(body.actions[0].params["path_steps"], 3)
        self.assertEqual(body.actions[0].params["path_moves"], ["walk", "walk", "walk"])

    def test_navigate_to_accepts_typed_goal_and_preserves_goal_payload(self):
        cells = grid(8)
        nav = _navigator(cells)
        body = FakeBody([state_at((0, 64, 0))])
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to(GoalNear((6, 64, 0), radius=2), config=NavigationRunConfig(max_segments=2))

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "arrived")
        self.assertEqual(body.actions[0].params["target"], [4, 64, 0])
        self.assertEqual(body.actions[0].params["final_goal"], [6, 64, 0])
        self.assertEqual(body.actions[0].params["navigation_goal"], {"kind": "near", "pos": [6, 64, 0], "radius": 2})
        self.assertEqual(result.metrics["goal"], [6, 64, 0])
        self.assertEqual(result.metrics["navigation_goal"]["kind"], "near")

    def test_navigate_to_can_send_precise_arrival_radius(self):
        cells = grid(4)
        nav = _navigator(cells)
        body = FakeBody([state_at((0, 64, 0))])
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to((3, 64, 0), config=NavigationRunConfig(max_segments=2), arrival_radius=0.25)

        self.assertTrue(result.success)
        self.assertEqual(body.actions[0].params["arrival_radius"], 0.25)

    def test_navigate_to_typed_xz_goal_passes_goal_object_to_navigator(self):
        nav = FakeNavigator([_segment("arrived", (3, 64, 0), success=True, reason="arrived")])
        body = FakeBody([state_at((0, 64, 0))])
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to(GoalXZ(3, 0), config=NavigationRunConfig(max_segments=1))

        self.assertTrue(result.success)
        self.assertEqual(body.actions[0].params["navigation_goal"], {"kind": "xz", "x": 3, "z": 0})
        self.assertEqual(nav.calls[0][1].payload(), {"kind": "xz", "x": 3, "z": 0})

    def test_navigate_to_preserves_vertical_water_and_fall_waypoint_moves(self):
        path = (
            PathStep(
                pos=(0, 65, 0),
                move=MoveKind.ASCEND,
                cost=2.0,
                reason="ascend",
                safe_to_cancel=False,
                cancel_policy="settle_on_support",
            ),
            PathStep(
                pos=(1, 65, 0),
                move=MoveKind.SWIM,
                cost=3.0,
                reason="swim",
                block_type="water",
                safe_to_cancel=False,
                cancel_policy="surface_or_stable_water",
            ),
            PathStep(
                pos=(1, 65, 1),
                move=MoveKind.DIAGONAL,
                cost=1.4,
                reason="diagonal",
            ),
            PathStep(
                pos=(1, 64, 0),
                move=MoveKind.FALL,
                cost=6.0,
                reason="fall",
                safe_to_cancel=False,
                cancel_policy="land_first",
                fall_depth=3,
            ),
        )
        nav = FakeNavigator([_segment("arrived", (1, 64, 0), success=True, reason="arrived", path=path)])
        body = FakeBody([state_at((0, 64, 0))])
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to((1, 64, 0), config=NavigationRunConfig(max_segments=1))

        self.assertTrue(result.success)
        self.assertEqual(body.actions[0].params["target"], [1, 62, 0])
        self.assertEqual(body.actions[0].params["planned_target"], [1, 64, 0])
        self.assertEqual(body.actions[0].params["waypoints"], [[0, 65, 0], [1, 65, 0], [1, 65, 1], [1, 62, 0]])
        self.assertEqual(body.actions[0].params["path_moves"], ["ascend", "swim", "diagonal", "fall"])
        self.assertEqual(body.actions[0].params["path_fall_depths"], [0, 0, 0, 3])
        self.assertEqual(body.actions[0].params["movement_cancel"]["unsafe_count"], 3)
        self.assertFalse(body.actions[0].params["movement_cancel"]["safe_to_cancel"])
        self.assertEqual(
            [step["policy"] for step in body.actions[0].params["movement_cancel"]["unsafe_steps"]],
            ["settle_on_support", "surface_or_stable_water", "land_first"],
        )
        segment = result.metrics["segments"][0]["diagnostics"]["segment"]
        self.assertEqual(segment["path_fall_depths"], [0, 0, 0, 3])
        self.assertEqual(segment["movement_waypoints"], [[0, 65, 0], [1, 65, 0], [1, 65, 1], [1, 62, 0]])
        self.assertFalse(segment["movement_cancel"]["safe_to_cancel"])
        self.assertEqual(segment["movement_cancel"]["unsafe_count"], 3)

    def test_navigate_to_continues_after_partial_segment(self):
        nav = FakeNavigator(
            [
                _segment("advanced", (2, 64, 0), success=False, reason="partial"),
                _segment("arrived", (7, 64, 0), success=True, reason="arrived"),
            ]
        )
        body = FakeBody([state_at((0, 64, 0)), state_at((2, 64, 0))])
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to(
            (7, 64, 0),
            config=NavigationRunConfig(max_segments=2, min_partial_progress=2),
        )

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "arrived")
        self.assertEqual([action.params["target"] for action in body.actions], [[2, 64, 0], [7, 64, 0]])
        self.assertEqual([action.params["waypoints"] for action in body.actions], [[[2, 64, 0]], [[7, 64, 0]]])
        self.assertEqual(result.metrics["segment_count"], 2)
        self.assertEqual([call[0] for call in nav.calls], [(0, 64, 0), (2, 64, 0)])

    def test_navigate_to_executes_open_step_before_walk(self):
        path = (
            PathStep(
                pos=(1, 64, 0),
                move=MoveKind.OPEN,
                cost=1.0,
                reason="open_allowed",
                block_type="oak_fence_gate",
                safe_to_cancel=False,
                cancel_policy="finish_or_abort_controller",
                interaction_target=(1, 64, 0),
                open_expected_properties={"open": "true"},
            ),
            PathStep(
                pos=(1, 64, 0),
                move=MoveKind.WALK,
                cost=1.0,
                reason="walk",
            ),
            PathStep(
                pos=(2, 64, 0),
                move=MoveKind.WALK,
                cost=1.0,
                reason="walk",
            ),
        )
        nav = FakeNavigator([_segment("arrived", (2, 64, 0), success=True, reason="arrived", path=path)])
        body = FakeBody(
            [state_at((0, 64, 0)), state_at((0, 64, 0))],
            blocks={(1, 64, 0): ("oak_fence_gate", "SOLID", {"open": "false"})},
        )
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to((2, 64, 0), config=NavigationRunConfig(max_segments=2))

        self.assertTrue(result.success)
        self.assertEqual([action.name for action in body.actions], ["lookAt", "useItem", "moveTo"])
        self.assertEqual(result.reason, "arrived")
        self.assertEqual(result.metrics["segments"][0]["status"], "terrain_open")
        self.assertEqual(result.metrics["segments"][1]["status"], "arrived")

    def test_navigate_to_refreshes_world_after_partial_segment_before_replanning(self):
        cells = corridor(0, 4)
        policy = GovernancePolicy(natural_regions=[Region("work", (-10, 0, -10), (20, 100, 10))])
        nav = SegmentedNavigator(GridWorld(cells), NavigationCostModel(policy))
        body = FakeBody([state_at((0, 64, 0)), state_at((3, 64, 0))])
        updates = []

        def refresh(navigator, segment):
            updates.append((segment.target, segment.status))
            navigator.world.cells.update(corridor(5, 8))
            return {"added_cells": 36, "from_segment": list(segment.target)}

        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to(
            (7, 64, 0),
            config=NavigationRunConfig(
                max_segments=2,
                min_partial_progress=2,
                unloaded_boundary_limit=40,
                partial_tail_trim=1,
                world_update=refresh,
            ),
        )

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "arrived")
        self.assertEqual(updates, [((3, 64, 0), "advanced")])
        self.assertEqual([action.params["target"] for action in body.actions], [[3, 64, 0], [7, 64, 0]])
        self.assertEqual(result.metrics["segment_count"], 2)
        first_segment = result.metrics["segments"][0]
        self.assertEqual(first_segment["diagnostics"]["segment"]["plan_reason"], "partial")
        self.assertEqual(first_segment["diagnostics"]["world_update"]["added_cells"], 36)
        self.assertEqual(first_segment["diagnostics"]["world_update"]["from_segment"], [3, 64, 0])
        self.assertEqual(result.metrics["segments"][1]["diagnostics"]["segment"]["plan_reason"], "arrived")

    def test_block_at_prism_world_update_refreshes_authoritative_cells(self):
        cells = corridor(0, 4)
        policy = GovernancePolicy(natural_regions=[Region("work", (-10, 0, -10), (20, 100, 10))])
        nav = SegmentedNavigator(GridWorld(cells), NavigationCostModel(policy))
        body = FakeBody(
            [state_at((0, 64, 0)), state_at((3, 64, 0))],
            blocks={
                **{(x, 63, z): ("stone", "SOLID") for x in range(3, 8) for z in (-1, 0, 1)},
                **{(x, 64, z): ("air", "CLEAR") for x in range(3, 8) for z in (-1, 0, 1)},
                **{(x, 65, z): ("air", "CLEAR") for x in range(3, 8) for z in (-1, 0, 1)},
            },
        )
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to(
            (7, 64, 0),
            config=NavigationRunConfig(
                max_segments=2,
                min_partial_progress=2,
                unloaded_boundary_limit=40,
                partial_tail_trim=1,
                world_update=make_block_at_prism_world_update(body, lateral_margin=1, y_offsets=(-1, 0, 1), max_cells=80),
            ),
        )

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "arrived")
        self.assertEqual([action.params["target"] for action in body.actions], [[3, 64, 0], [7, 64, 0]])
        update = result.metrics["segments"][0]["diagnostics"]["world_update"]
        self.assertEqual(update["source"], "authoritative_block_at_prism_refresh")
        self.assertEqual(update["segment_target"], [3, 64, 0])
        self.assertEqual(update["goal"], [7, 64, 0])
        self.assertEqual(update["refreshed_cells"], 63)
        self.assertEqual(update["added_cells"], 36)
        self.assertEqual(update["solid_cells"], 15)
        self.assertEqual(update["clear_cells"], 48)
        self.assertTrue(update["complete"])
        self.assertEqual(update["tile_count"], 6)
        self.assertEqual(update["tile_width"], 4)
        self.assertEqual(update["tile_depth"], 4)
        self.assertEqual(sum(tile["cells"] for tile in update["tiles"]), 63)
        self.assertEqual(nav.world.cell_at((7, 63, 0)).block_type, "stone")
        self.assertFalse(nav.world.cell_at((7, 63, 0)).walkable)
        self.assertTrue(nav.world.cell_at((7, 64, 0)).walkable)

    def test_block_at_prism_world_update_can_chain_bounded_forward_refreshes(self):
        cells = corridor(0, 4)
        policy = GovernancePolicy(natural_regions=[Region("work", (-10, 0, -10), (24, 100, 10))])
        nav = SegmentedNavigator(GridWorld(cells), NavigationCostModel(policy))
        body = FakeBody(
            [state_at((0, 64, 0)), state_at((3, 64, 0)), state_at((7, 64, 0)), state_at((11, 64, 0))],
            blocks={
                **{(x, 63, z): ("stone", "SOLID") for x in range(3, 15) for z in (-1, 0, 1)},
                **{(x, 64, z): ("air", "CLEAR") for x in range(3, 15) for z in (-1, 0, 1)},
                **{(x, 65, z): ("air", "CLEAR") for x in range(3, 15) for z in (-1, 0, 1)},
            },
        )
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to(
            (14, 64, 0),
            config=NavigationRunConfig(
                max_segments=4,
                min_partial_progress=2,
                unloaded_boundary_limit=40,
                partial_tail_trim=1,
                world_update=make_block_at_prism_world_update(
                    body,
                    lateral_margin=1,
                    y_offsets=(-1, 0, 1),
                    max_cells=64,
                    forward_axis_limit=4,
                ),
            ),
        )

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "arrived")
        self.assertEqual([action.params["target"] for action in body.actions], [[3, 64, 0], [7, 64, 0], [11, 64, 0], [14, 64, 0]])
        self.assertEqual(result.metrics["segment_count"], 4)
        updates = [segment["diagnostics"].get("world_update") for segment in result.metrics["segments"][:-1]]
        self.assertEqual(len(updates), 3)
        self.assertTrue(all(update is not None for update in updates))
        self.assertEqual([update["refresh_goal"] for update in updates], [[7, 64, 0], [11, 64, 0], [14, 64, 0]])
        self.assertTrue(all(update["forward_axis_limit"] == 4 for update in updates))
        self.assertTrue(all(update["refreshed_cells"] <= 63 for update in updates))
        self.assertEqual(result.metrics["segments"][-1]["diagnostics"]["segment"]["plan_reason"], "arrived")
        self.assertTrue(nav.world.cell_at((14, 63, 0)).block_type == "stone")
        self.assertTrue(nav.world.cell_at((14, 64, 0)).walkable)

    def test_block_at_prism_world_update_fails_when_tile_budget_is_exceeded(self):
        cells = corridor(0, 4)
        policy = GovernancePolicy(natural_regions=[Region("work", (-10, 0, -10), (20, 100, 10))])
        nav = SegmentedNavigator(GridWorld(cells), NavigationCostModel(policy))
        body = FakeBody([state_at((0, 64, 0)), state_at((3, 64, 0))])
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to(
            (7, 64, 0),
            config=NavigationRunConfig(
                max_segments=2,
                min_partial_progress=2,
                unloaded_boundary_limit=40,
                partial_tail_trim=1,
                world_update=make_block_at_prism_world_update(
                    body,
                    lateral_margin=1,
                    y_offsets=(-1, 0, 1),
                    max_cells=80,
                    tile_width=2,
                    tile_depth=2,
                    max_tiles=2,
                ),
            ),
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "world_update_failed")
        self.assertIn("exceeds max_tiles", result.metrics["error"])

    def test_block_at_prism_world_update_fails_when_perception_is_incomplete(self):
        cells = corridor(0, 4)
        policy = GovernancePolicy(natural_regions=[Region("work", (-10, 0, -10), (20, 100, 10))])
        nav = SegmentedNavigator(GridWorld(cells), NavigationCostModel(policy))
        body = FakeBody([state_at((0, 64, 0)), state_at((3, 64, 0))])

        def incomplete_block_at(scope: str, params: dict[str, object]) -> PerceptionResult:
            if scope == "blockAt" and int(params["x"]) == 5 and int(params["y"]) == 64 and int(params["z"]) == 0:
                return PerceptionResult(
                    bot="Bot1",
                    scope="blockAt",
                    type="perception",
                    ok=True,
                    complete=False,
                    data={"x": 5, "y": 64, "z": 0, "type": "air", "state": "CLEAR"},
                    uncertainty=[{"reason": "limit_exceeded"}],
                    next="limit",
                    error=None,
                )
            return FakeBody.perceive(body, scope, params)

        body.perceive = incomplete_block_at  # type: ignore[method-assign]
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to(
            (7, 64, 0),
            config=NavigationRunConfig(
                max_segments=2,
                min_partial_progress=2,
                unloaded_boundary_limit=40,
                partial_tail_trim=1,
                world_update=make_block_at_prism_world_update(body, lateral_margin=1, y_offsets=(0,), max_cells=32),
            ),
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "world_update_failed")
        self.assertTrue(result.can_retry)
        self.assertIn("blockAt refresh failed at [5, 64, 0]", result.metrics["error"])

    def test_read_block_cells_tiled_returns_cells_and_tile_diagnostics(self):
        body = FakeBody(
            [state_at((0, 64, 0))],
            blocks={
                (0, 64, 0): ("air", "CLEAR"),
                (1, 64, 0): ("stone", "SOLID"),
                (2, 64, 0): ("water", "LIQUID"),
                (4, 64, 0): ("air", "CLEAR"),
            },
        )

        read = read_block_cells_tiled(
            body,
            ((0, 64, 0), (1, 64, 0), (2, 64, 0), (4, 64, 0)),
            tile_width=2,
            tile_depth=2,
            max_tiles=3,
        )

        self.assertEqual(read.diagnostics["refreshed_cells"], 4)
        self.assertEqual(read.diagnostics["clear_cells"], 2)
        self.assertEqual(read.diagnostics["solid_cells"], 1)
        self.assertEqual(read.diagnostics["liquid_cells"], 1)
        self.assertTrue(read.diagnostics["complete"])
        self.assertEqual(read.diagnostics["tile_count"], 3)
        self.assertEqual(sum(tile["cells"] for tile in read.diagnostics["tiles"]), 4)
        self.assertTrue(read.cells[(0, 64, 0)].walkable)
        self.assertFalse(read.cells[(1, 64, 0)].walkable)
        self.assertTrue(read.cells[(2, 64, 0)].liquid)

    def test_block_at_prism_world_update_rejects_invalid_forward_axis_limit(self):
        with self.assertRaisesRegex(ValueError, "forward_axis_limit must be >= 1"):
            make_block_at_prism_world_update(FakeBody([state_at((0, 64, 0))]), forward_axis_limit=0)

    def test_read_block_cells_tiled_fails_with_labeled_perception_error(self):
        body = FakeBody([state_at((0, 64, 0))])

        def incomplete_block_at(scope: str, params: dict[str, object]) -> PerceptionResult:
            if scope == "blockAt":
                return PerceptionResult(
                    bot="Bot1",
                    scope="blockAt",
                    type="perception",
                    ok=True,
                    complete=False,
                    data={"x": 1, "y": 64, "z": 0, "type": "air", "state": "CLEAR"},
                    uncertainty=[{"reason": "limit_exceeded"}],
                    next="limit",
                    error=None,
                )
            return FakeBody.perceive(body, scope, params)

        body.perceive = incomplete_block_at  # type: ignore[method-assign]

        with self.assertRaisesRegex(ValueError, "blockAt scan failed at \\[1, 64, 0\\]"):
            read_block_cells_tiled(
                body,
                ((1, 64, 0),),
                failure_label="scan",
            )

    def test_navigate_to_returns_neutral_preempted_without_replanning(self):
        nav = FakeNavigator(
            [
                _segment("advanced", (2, 64, 0), success=False, reason="partial"),
                _segment("arrived", (7, 64, 0), success=True, reason="arrived"),
            ]
        )
        body = FakeBody([state_at((0, 64, 0)), state_at((1, 64, 0))], terminal_reasons=["preempted"])
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to(
            (7, 64, 0),
            config=NavigationRunConfig(max_segments=3, min_partial_progress=2),
        )

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "preempted")
        self.assertTrue(result.can_retry)
        self.assertEqual(len(body.actions), 1)
        self.assertEqual(len(nav.calls), 1)
        self.assertEqual(result.metrics["segments"][0]["terminal_reason"], "preempted")
        self.assertTrue(result.metrics["paused"])

    def test_navigate_to_reports_blocked_without_moving(self):
        cells = grid(3)
        cells[(1, 64, 0)] = GridCell(block_type="stone", walkable=False)
        nav = SegmentedNavigator(GridWorld(cells), NavigationCostModel(GovernancePolicy()))
        body = FakeBody([state_at((0, 64, 0))])
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to((2, 64, 0))

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "navigation_blocked:no_path")
        self.assertFalse(result.can_retry)
        self.assertEqual(body.actions, [])
        self.assertEqual(result.metrics["path_update"]["source"], "planner")
        self.assertEqual(result.metrics["path_update"]["category"], "protected_or_denied")
        self.assertEqual(result.metrics["path_update"]["blocked_reasons"]["break_denied:unknown_provenance"], 1)

    def test_navigate_to_classifies_unloaded_boundary_as_path_update(self):
        segment = NavigationSegment(
            status="blocked",
            target=None,
            plan=PathResult(
                success=False,
                reason="unloaded_boundary",
                expanded=1,
                diagnostics={
                    "blocked": [{"pos": [1, 64, 0], "reason": "unloaded"}],
                    "blocked_count": 1,
                    "unloaded_boundary_count": 1,
                    "unloaded_boundary_limit": 1,
                },
            ),
        )
        nav = FakeNavigator([segment])
        body = FakeBody([state_at((0, 64, 0))])
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to((10, 64, 0))

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "navigation_blocked:unloaded_boundary")
        self.assertEqual(result.metrics["path_update"]["category"], "unloaded_boundary")
        self.assertEqual(result.metrics["path_update"]["unloaded_boundary_count"], 1)

    def test_navigate_to_preserves_unloaded_boundary_when_partial_segment_budget_exhausts(self):
        segment = NavigationSegment(
            status="advanced",
            target=(2, 64, 0),
            plan=PathResult(
                success=False,
                reason="partial",
                path=(
                    PathStep(pos=(1, 64, 0), move=MoveKind.WALK, cost=1.0, reason="walk"),
                    PathStep(pos=(2, 64, 0), move=MoveKind.WALK, cost=1.0, reason="walk"),
                ),
                expanded=4,
                diagnostics={
                    "stop_reason": "unloaded_boundary",
                    "blocked": [{"pos": [4, 64, 0], "reason": "unloaded"}],
                    "blocked_count": 1,
                    "unloaded_boundary_count": 1,
                    "unloaded_boundary_limit": 40,
                    "original_partial_target": [3, 64, 0],
                    "partial_target": [2, 64, 0],
                    "tail_trimmed_steps": 1,
                    "tail_trim_reason": "unloaded_boundary",
                },
            ),
            recheck=RecheckResult(ok=True, reason="valid", checked=2),
        )
        nav = FakeNavigator([segment])
        body = FakeBody([state_at((0, 64, 0)), state_at((2, 64, 0))])
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to((10, 64, 0), config=NavigationRunConfig(max_segments=1))

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "segment_budget_exhausted")
        self.assertTrue(result.can_retry)
        self.assertEqual(result.metrics["path_update"]["source"], "budget")
        self.assertEqual(result.metrics["path_update"]["category"], "unloaded_boundary")
        self.assertEqual(result.metrics["path_update"]["unloaded_boundary_count"], 1)
        self.assertEqual(result.metrics["segments"][0]["diagnostics"]["segment"]["plan_diagnostics"]["tail_trimmed_steps"], 1)
        self.assertNotIn("planned_segment", result.metrics["segments"][0]["diagnostics"])

    def test_navigate_to_does_not_execute_when_recheck_requires_replan(self):
        nav = FakeNavigator(
            [
                _segment(
                    "replan_required",
                    None,
                    success=True,
                    reason="arrived",
                    recheck=RecheckResult(ok=False, reason="break_denied:protected_region", checked=1),
                )
            ]
        )
        body = FakeBody([state_at((0, 64, 0))])
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to((2, 64, 0))

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "navigation_replan_required:break_denied:protected_region")
        self.assertTrue(result.can_retry)
        self.assertEqual(body.actions, [])
        self.assertEqual(result.metrics["path_update"]["source"], "recheck")
        self.assertEqual(result.metrics["path_update"]["category"], "goal_changed_or_world_changed")
        self.assertEqual(result.metrics["path_update"]["recheck_reason"], "break_denied:protected_region")

    def test_navigate_to_rechecks_against_authoritative_world_before_dispatch(self):
        planned_world = GridWorld(grid(4))
        stale_cells = grid(4)
        stale_cells[(1, 65, 0)] = GridCell(block_type="stone", walkable=False)
        recheck_world = GridWorld(stale_cells)
        policy = GovernancePolicy(natural_regions=[Region("work", (-10, 0, -10), (20, 100, 10))])
        nav = SegmentedNavigator(planned_world, NavigationCostModel(policy))
        body = FakeBody([state_at((0, 64, 0))])
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to(
            (3, 64, 0),
            config=NavigationRunConfig(max_segments=1, recheck_world=recheck_world),
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "navigation_replan_required:headroom_blocked")
        self.assertTrue(result.can_retry)
        self.assertEqual(body.actions, [])
        self.assertEqual(result.metrics["path_update"]["source"], "recheck")
        self.assertEqual(result.metrics["path_update"]["category"], "goal_changed_or_world_changed")
        self.assertEqual(result.metrics["path_update"]["recheck_reason"], "headroom_blocked")
        segment = result.metrics["segments"][0]["diagnostics"]["segment"]
        self.assertEqual(segment["plan_reason"], "arrived")
        self.assertEqual(segment["recheck_reason"], "headroom_blocked")

    def test_navigate_to_rechecks_support_missing_authoritative_world_before_dispatch(self):
        planned_cells = grid(5)
        planned_cells[(2, 63, 0)] = GridCell(block_type="stone", walkable=False)
        planned_cells[(2, 64, 0)] = GridCell(requires_support=True)
        recheck_cells = dict(planned_cells)
        recheck_cells[(2, 63, 0)] = GridCell(block_type="air", walkable=True)
        planned_world = GridWorld(planned_cells)
        recheck_world = GridWorld(recheck_cells)
        policy = GovernancePolicy(natural_regions=[Region("work", (-10, 0, -10), (20, 100, 10))])
        nav = SegmentedNavigator(planned_world, NavigationCostModel(policy))
        body = FakeBody([state_at((0, 64, 0))])
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to(
            (4, 64, 0),
            config=NavigationRunConfig(max_segments=1, recheck_world=recheck_world),
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "navigation_replan_required:support_missing")
        self.assertTrue(result.can_retry)
        self.assertEqual(body.actions, [])
        self.assertEqual(result.metrics["path_update"]["source"], "recheck")
        self.assertEqual(result.metrics["path_update"]["category"], "goal_changed_or_world_changed")
        self.assertEqual(result.metrics["path_update"]["recheck_reason"], "support_missing")
        segment = result.metrics["segments"][0]["diagnostics"]["segment"]
        self.assertEqual(segment["plan_reason"], "arrived")
        self.assertEqual(segment["recheck_reason"], "support_missing")
        self.assertEqual(segment["path_moves"], ["walk", "walk", "walk", "walk"])

    def test_navigate_to_rechecks_diagonal_corner_headroom_before_dispatch(self):
        planned_cells = {
            (0, 64, 0): GridCell(),
            (1, 64, 0): GridCell(),
            (0, 64, 1): GridCell(),
            (1, 64, 1): GridCell(),
        }
        recheck_cells = dict(planned_cells)
        recheck_cells[(1, 64, 0)] = GridCell(headroom_block="stone")
        recheck_cells[(1, 65, 0)] = GridCell(block_type="stone", walkable=False)
        planned_world = GridWorld(planned_cells)
        recheck_world = GridWorld(recheck_cells)
        policy = GovernancePolicy(natural_regions=[Region("work", (-10, 0, -10), (20, 100, 10))])
        nav = SegmentedNavigator(planned_world, NavigationCostModel(policy))
        body = FakeBody([state_at((0, 64, 0))])
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to(
            (1, 64, 1),
            config=NavigationRunConfig(max_segments=1, recheck_world=recheck_world),
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "navigation_replan_required:diagonal_corner_headroom_blocked")
        self.assertTrue(result.can_retry)
        self.assertEqual(body.actions, [])
        self.assertEqual(result.metrics["path_update"]["source"], "recheck")
        self.assertEqual(result.metrics["path_update"]["category"], "goal_changed_or_world_changed")
        self.assertEqual(result.metrics["path_update"]["recheck_reason"], "diagonal_corner_headroom_blocked")
        segment = result.metrics["segments"][0]["diagnostics"]["segment"]
        self.assertEqual(segment["plan_reason"], "arrived")
        self.assertEqual(segment["recheck_reason"], "diagonal_corner_headroom_blocked")
        self.assertEqual(segment["path_moves"], ["diagonal"])

    def test_navigate_to_rechecks_fall_depth_before_dispatch(self):
        planned_cells = {
            (0, 64, 0): GridCell(),
            (0, 63, 0): GridCell(fall_depth=2),
        }
        recheck_cells = dict(planned_cells)
        recheck_cells[(0, 63, 0)] = GridCell(fall_depth=6)
        planned_world = GridWorld(planned_cells)
        recheck_world = GridWorld(recheck_cells)
        policy = GovernancePolicy(natural_regions=[Region("work", (-10, 0, -10), (20, 100, 10))])
        nav = SegmentedNavigator(planned_world, NavigationCostModel(policy))
        body = FakeBody([state_at((0, 64, 0))])
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to(
            (0, 63, 0),
            config=NavigationRunConfig(max_segments=1, recheck_world=recheck_world),
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "navigation_replan_required:fall_denied:unsafe_depth")
        self.assertTrue(result.can_retry)
        self.assertEqual(body.actions, [])
        self.assertEqual(result.metrics["path_update"]["source"], "recheck")
        self.assertEqual(result.metrics["path_update"]["category"], "goal_changed_or_world_changed")
        self.assertEqual(result.metrics["path_update"]["recheck_reason"], "fall_denied:unsafe_depth")
        segment = result.metrics["segments"][0]["diagnostics"]["segment"]
        self.assertEqual(segment["plan_reason"], "arrived")
        self.assertEqual(segment["recheck_reason"], "fall_denied:unsafe_depth")
        self.assertEqual(segment["path_moves"], ["fall"])
        self.assertEqual(segment["path_fall_depths"], [2])

    def test_navigate_to_rechecks_unloaded_authoritative_world_before_dispatch(self):
        planned_world = GridWorld(grid(4))
        recheck_cells = grid(4)
        del recheck_cells[(1, 64, 0)]
        recheck_world = GridWorld(recheck_cells)
        policy = GovernancePolicy(natural_regions=[Region("work", (-10, 0, -10), (20, 100, 10))])
        nav = SegmentedNavigator(planned_world, NavigationCostModel(policy))
        body = FakeBody([state_at((0, 64, 0))])
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to(
            (3, 64, 0),
            config=NavigationRunConfig(max_segments=1, recheck_world=recheck_world),
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "navigation_replan_required:unloaded")
        self.assertTrue(result.can_retry)
        self.assertEqual(body.actions, [])
        self.assertEqual(result.metrics["path_update"]["source"], "recheck")
        self.assertEqual(result.metrics["path_update"]["category"], "goal_changed_or_world_changed")
        self.assertEqual(result.metrics["path_update"]["recheck_reason"], "unloaded")
        segment = result.metrics["segments"][0]["diagnostics"]["segment"]
        self.assertEqual(segment["plan_reason"], "arrived")
        self.assertEqual(segment["recheck_reason"], "unloaded")
        self.assertEqual(result.metrics["path_update"]["recheck_checked"], 1)

    def test_navigate_to_rechecks_governance_costs_before_dispatch(self):
        cells = grid(3)
        cells[(1, 64, 0)] = GridCell(block_type="stone", walkable=False)
        planned_policy = GovernancePolicy(natural_regions=[Region("work", (-10, 0, -10), (20, 100, 10))])
        protected_policy = GovernancePolicy(
            natural_regions=[Region("work", (-10, 0, -10), (20, 100, 10))],
            protected_regions=[Region("new_build", (1, 0, 0), (1, 100, 0))],
        )
        nav = SegmentedNavigator(GridWorld(cells), NavigationCostModel(planned_policy))
        body = FakeBody([state_at((0, 64, 0))])
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to(
            (2, 64, 0),
            break_context=BreakContext.TRAVEL,
            config=NavigationRunConfig(max_segments=1, recheck_costs=NavigationCostModel(protected_policy)),
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "navigation_replan_required:break_denied:protected_region")
        self.assertTrue(result.can_retry)
        self.assertEqual(body.actions, [])
        self.assertEqual(result.metrics["path_update"]["source"], "recheck")
        self.assertEqual(result.metrics["path_update"]["category"], "goal_changed_or_world_changed")
        self.assertEqual(result.metrics["path_update"]["recheck_reason"], "break_denied:protected_region")
        segment = result.metrics["segments"][0]["diagnostics"]["segment"]
        self.assertEqual(segment["plan_reason"], "arrived")
        self.assertEqual(segment["path_moves"], ["break", "walk"])
        self.assertEqual(segment["recheck_reason"], "break_denied:protected_region")

    def test_navigate_to_reports_body_rejection(self):
        cells = grid(4)
        nav = _navigator(cells)
        body = FakeBody([state_at((0, 64, 0))], accept=False)
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to((3, 64, 0))

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "body_rejected")
        self.assertTrue(result.can_retry)
        self.assertEqual(len(body.actions), 1)

    def test_navigate_to_executes_break_step_between_move_segments(self):
        break_path = (
            PathStep(pos=(1, 64, 0), move=MoveKind.WALK, cost=1.0, reason="walk"),
            PathStep(pos=(2, 64, 0), move=MoveKind.BREAK, cost=6.0, reason="break_allowed:allowed_natural"),
        )
        nav = FakeNavigator(
            [
                _segment("advanced", (2, 64, 0), success=False, reason="partial", path=break_path),
                _segment("arrived", (4, 64, 0), success=True, reason="arrived"),
            ]
        )
        body = FakeBody([state_at((0, 64, 0)), state_at((1, 64, 0))], terminal_reasons=["arrived", "arrived"])
        work = FakeWork()
        runtime = NavigationTransactions(body, nav, work=work)

        result = runtime.navigate_to(
            (4, 64, 0),
            config=NavigationRunConfig(max_segments=3, min_partial_progress=2),
        )

        self.assertTrue(result.success)
        self.assertEqual([action.params["target"] for action in body.actions], [[1, 64, 0], [4, 64, 0]])
        self.assertEqual(body.actions[0].params["waypoints"], [[1, 64, 0]])
        self.assertEqual(body.actions[0].params["path_moves"], ["walk"])
        self.assertEqual(work.mine_calls[0][0], (2, 64, 0))
        self.assertEqual([call[0] for call in nav.calls], [(0, 64, 0), (1, 64, 0)])

    def test_navigate_to_auto_wires_break_runtime_from_segmented_navigator_governance(self):
        cells = grid(5)
        cells[(2, 64, 0)] = GridCell(block_type="stone", walkable=False)
        nav = _navigator(cells)
        body = FakeBody(
            [state_at((0, 64, 0)), state_at((2, 64, 0))],
            terminal_reasons=["arrived", "completed", "arrived"],
            blocks={(2, 64, 0): ("stone", "SOLID")},
        )
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to(
            (4, 64, 0),
            config=NavigationRunConfig(max_segments=3, min_partial_progress=2),
        )

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "arrived")
        self.assertEqual([action.name for action in body.actions], ["moveTo", "mineBlock", "moveTo"])
        self.assertEqual(body.actions[1].params["target"], [2, 64, 0])
        self.assertEqual(body.actions[1].params["context"], "travel")
        self.assertTrue(nav.world.cell_at((2, 64, 0)).walkable)
        self.assertEqual(nav.world.cell_at((2, 64, 0)).block_type, "air")

    def test_navigate_to_requires_work_runtime_for_break_step(self):
        path = (PathStep(pos=(2, 64, 0), move=MoveKind.BREAK, cost=6.0, reason="break_allowed:allowed_natural"),)
        nav = FakeNavigator([_segment("advanced", (2, 64, 0), success=False, reason="partial", path=path)])
        body = FakeBody([state_at((0, 64, 0))])
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to((4, 64, 0))

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "terrain_break_runtime_missing")
        self.assertEqual(body.actions, [])

    def test_navigate_to_refuses_break_before_mutation_when_break_budget_is_zero(self):
        path = (PathStep(pos=(2, 64, 0), move=MoveKind.BREAK, cost=6.0, reason="break_allowed:allowed_natural"),)
        nav = FakeNavigator([_segment("advanced", (2, 64, 0), success=False, reason="partial", path=path)])
        body = FakeBody([state_at((0, 64, 0))])
        work = FakeWork()
        runtime = NavigationTransactions(body, nav, work=work)

        result = runtime.navigate_to((4, 64, 0), config=NavigationRunConfig(max_break_steps=0))

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "navigation_break_budget_exhausted")
        self.assertFalse(result.can_retry)
        self.assertEqual(work.mine_calls, [])
        self.assertEqual(result.metrics["break_steps_used"], 0)
        self.assertEqual(result.metrics["max_break_steps"], 0)
        self.assertEqual(result.metrics["attempted_break"], [2, 64, 0])

    def test_navigate_to_stops_before_second_break_when_break_budget_is_exhausted(self):
        first = (PathStep(pos=(2, 64, 0), move=MoveKind.BREAK, cost=6.0, reason="break_allowed:allowed_natural"),)
        second = (PathStep(pos=(3, 64, 0), move=MoveKind.BREAK, cost=6.0, reason="break_allowed:allowed_natural"),)
        nav = FakeNavigator(
            [
                _segment("advanced", (2, 64, 0), success=False, reason="partial", path=first),
                _segment("advanced", (3, 64, 0), success=False, reason="partial", path=second),
            ]
        )
        body = FakeBody([state_at((0, 64, 0)), state_at((2, 64, 0))])
        work = FakeWork()
        runtime = NavigationTransactions(body, nav, work=work)

        result = runtime.navigate_to(
            (4, 64, 0),
            config=NavigationRunConfig(max_segments=3, max_break_steps=1),
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "navigation_break_budget_exhausted")
        self.assertEqual(work.mine_calls, [((2, 64, 0), BreakContext.TRAVEL, 15.0)])
        self.assertEqual(result.metrics["break_steps_used"], 1)
        self.assertEqual(result.metrics["max_break_steps"], 1)
        self.assertEqual(result.metrics["attempted_break"], [3, 64, 0])

    def test_navigate_to_rejects_negative_break_budget(self):
        nav = FakeNavigator([_segment("arrived", (2, 64, 0), success=True, reason="arrived")])
        body = FakeBody([state_at((0, 64, 0))])
        runtime = NavigationTransactions(body, nav)

        with self.assertRaises(ValueError):
            runtime.navigate_to((2, 64, 0), config=NavigationRunConfig(max_break_steps=-1))

    def test_navigate_to_stops_when_guard_target_distance_worsens(self):
        nav = FakeNavigator(
            [
                _segment("advanced", (-3, 64, 0), success=False, reason="partial"),
                _segment("arrived", (10, 64, 0), success=True, reason="arrived"),
            ]
        )
        body = FakeBody([state_at((0, 64, 0)), state_at((-3, 64, 0))])
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to(
            (10, 64, 0),
            config=NavigationRunConfig(
                max_segments=3,
                guard_target=(10, 64, 0),
                max_worse_distance=2.0,
            ),
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "navigation_guard_target_worsened")
        self.assertTrue(result.can_retry)
        self.assertEqual(len(body.actions), 1)
        self.assertEqual(len(nav.calls), 1)
        self.assertEqual(result.metrics["guard_target"], [10, 64, 0])
        self.assertEqual(result.metrics["best_guard_distance"], 10.0)
        self.assertEqual(result.metrics["current_guard_distance"], 13.0)
        self.assertEqual(result.metrics["max_worse_distance"], 2.0)

    def test_navigate_to_allows_guard_target_progress(self):
        nav = FakeNavigator(
            [
                _segment("advanced", (3, 64, 0), success=False, reason="partial"),
                _segment("arrived", (10, 64, 0), success=True, reason="arrived"),
            ]
        )
        body = FakeBody([state_at((0, 64, 0)), state_at((3, 64, 0))])
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to(
            (10, 64, 0),
            config=NavigationRunConfig(
                max_segments=3,
                guard_target=(10, 64, 0),
                max_worse_distance=2.0,
            ),
        )

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "arrived")
        self.assertEqual(len(body.actions), 2)
        self.assertEqual(len(nav.calls), 2)

    def test_navigate_to_rejects_negative_max_worse_distance(self):
        nav = FakeNavigator([_segment("arrived", (2, 64, 0), success=True, reason="arrived")])
        body = FakeBody([state_at((0, 64, 0))])
        runtime = NavigationTransactions(body, nav)

        with self.assertRaises(ValueError):
            runtime.navigate_to((2, 64, 0), config=NavigationRunConfig(max_worse_distance=-0.1))

    def test_navigate_to_executes_place_step_between_move_segments(self):
        place_path = (
            PathStep(pos=(1, 64, 0), move=MoveKind.WALK, cost=1.0, reason="walk"),
            PathStep(
                pos=(2, 64, 0),
                move=MoveKind.PLACE,
                cost=3.0,
                reason="place_allowed:minecraft:cobblestone",
                place_face="west",
            ),
        )
        nav = FakeNavigator(
            [
                _segment("advanced", (2, 64, 0), success=False, reason="partial", path=place_path),
                _segment("arrived", (4, 64, 0), success=True, reason="arrived"),
            ]
        )
        body = FakeBody([state_at((0, 64, 0)), state_at((1, 64, 0))], terminal_reasons=["arrived", "arrived"])
        work = FakeWork()
        runtime = NavigationTransactions(body, nav, work=work)

        result = runtime.navigate_to(
            (4, 64, 0),
            config=NavigationRunConfig(max_segments=3, min_partial_progress=2),
        )

        self.assertTrue(result.success)
        self.assertEqual([action.params["target"] for action in body.actions], [[1, 64, 0], [4, 64, 0]])
        self.assertEqual(body.actions[0].params["waypoints"], [[1, 64, 0]])
        self.assertEqual(body.actions[0].params["path_moves"], ["walk"])
        self.assertEqual(work.place_calls[0][0], (2, 64, 0))
        self.assertEqual(work.place_calls[0][1], "minecraft:cobblestone")
        self.assertEqual(work.place_calls[0][2], "west")
        self.assertEqual(work.place_calls[0][3], "travel")
        self.assertEqual(work.place_calls[0][4], "scaffold")
        self.assertEqual([call[0] for call in nav.calls], [(0, 64, 0), (1, 64, 0)])

    def test_navigate_to_auto_wires_place_runtime_from_segmented_navigator_governance(self):
        place_path = (
            PathStep(pos=(1, 64, 0), move=MoveKind.WALK, cost=1.0, reason="walk"),
            PathStep(
                pos=(2, 64, 0),
                move=MoveKind.PLACE,
                cost=3.0,
                reason="place_allowed:minecraft:cobblestone",
                place_face="west",
            ),
        )
        policy = GovernancePolicy(natural_regions=[Region("work", (-10, 0, -10), (20, 100, 10))])
        nav = FakeNavigator(
            [
                _segment("advanced", (2, 64, 0), success=False, reason="partial", path=place_path),
                _segment("arrived", (4, 64, 0), success=True, reason="arrived"),
            ]
        )
        nav.costs = SimpleNamespace(governance=policy)
        body = FakeBody(
            [state_at((0, 64, 0)), state_at((1, 64, 0))],
            terminal_reasons=["arrived", "completed", "arrived"],
            blocks={(2, 64, 0): ("air", "CLEAR")},
        )
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to(
            (4, 64, 0),
            config=NavigationRunConfig(max_segments=3, min_partial_progress=2),
        )

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "arrived")
        self.assertEqual([action.name for action in body.actions], ["moveTo", "placeBlock", "moveTo"])
        self.assertEqual(body.actions[1].params["target"], [2, 64, 0])
        self.assertEqual(body.actions[1].params["block_type"], "minecraft:cobblestone")
        self.assertEqual(body.actions[1].params["face"], "west")
        self.assertEqual(body.actions[1].params["context"], "travel")

    def test_navigate_to_requires_work_runtime_for_place_step(self):
        path = (PathStep(pos=(2, 64, 0), move=MoveKind.PLACE, cost=3.0, reason="place_allowed:minecraft:cobblestone"),)
        nav = FakeNavigator([_segment("advanced", (2, 64, 0), success=False, reason="partial", path=path)])
        body = FakeBody([state_at((0, 64, 0))])
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to((4, 64, 0))

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "terrain_place_runtime_missing")

    def test_navigate_to_executes_pillar_step_through_work_runtime(self):
        pillar_path = (
            PathStep(
                pos=(0, 65, 0),
                move=MoveKind.PILLAR,
                cost=NavigationCostModel.PILLAR_COST,
                reason="pillar",
                safe_to_cancel=False,
                cancel_policy="finish_or_abort_controller",
            ),
        )
        nav = FakeNavigator(
            [
                _segment("advanced", (0, 65, 0), success=False, reason="partial", path=pillar_path),
                _segment("arrived", (0, 65, 0), success=True, reason="arrived"),
            ]
        )
        body = FakeBody([state_at((0, 64, 0)), state_at((0, 65, 0))], terminal_reasons=["arrived"])
        work = FakeWork()
        runtime = NavigationTransactions(body, nav, work=work)

        result = runtime.navigate_to((0, 65, 0), config=NavigationRunConfig(max_segments=3, min_partial_progress=1))

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "arrived")
        self.assertEqual(work.dig_up_calls, [((0, 64, 0), BreakContext.TRAVEL, 15.0)])
        self.assertEqual([action.name for action in body.actions], ["moveTo"])
        first_segment = result.metrics["segments"][0]["diagnostics"]["segment"]
        self.assertEqual(first_segment["path_moves"], ["pillar"])
        self.assertEqual(first_segment["movement_waypoints"], [])

    def test_navigate_to_requires_work_runtime_for_pillar_step(self):
        path = (
            PathStep(
                pos=(0, 65, 0),
                move=MoveKind.PILLAR,
                cost=NavigationCostModel.PILLAR_COST,
                reason="pillar",
            ),
        )
        nav = FakeNavigator([_segment("advanced", (0, 65, 0), success=False, reason="partial", path=path)])
        body = FakeBody([state_at((0, 64, 0))])
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to((0, 65, 0))

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "terrain_pillar_runtime_missing")

    def test_navigate_to_executes_downward_step_through_work_runtime(self):
        downward_path = (
            PathStep(
                pos=(0, 63, 0),
                move=MoveKind.DOWNWARD,
                cost=NavigationCostModel.DOWNWARD_COST,
                reason="downward",
                safe_to_cancel=False,
                cancel_policy="finish_or_abort_controller",
            ),
        )
        nav = FakeNavigator(
            [
                _segment("advanced", (0, 63, 0), success=False, reason="partial", path=downward_path),
                _segment("arrived", (0, 63, 0), success=True, reason="arrived"),
            ]
        )
        body = FakeBody([state_at((0, 64, 0)), state_at((0, 63, 0))], terminal_reasons=["arrived"])
        work = FakeWork()
        runtime = NavigationTransactions(body, nav, work=work)

        result = runtime.navigate_to((0, 63, 0), config=NavigationRunConfig(max_segments=3, min_partial_progress=1))

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "arrived")
        self.assertEqual(work.dig_down_calls, [(63, (0, 64, 0), BreakContext.TRAVEL, 1, 15.0, 15.0)])
        self.assertEqual([action.name for action in body.actions], ["moveTo"])
        first_segment = result.metrics["segments"][0]["diagnostics"]["segment"]
        self.assertEqual(first_segment["path_moves"], ["downward"])
        self.assertEqual(first_segment["movement_waypoints"], [])

    def test_navigate_to_requires_work_runtime_for_downward_step(self):
        path = (
            PathStep(
                pos=(0, 63, 0),
                move=MoveKind.DOWNWARD,
                cost=NavigationCostModel.DOWNWARD_COST,
                reason="downward",
            ),
        )
        nav = FakeNavigator([_segment("advanced", (0, 63, 0), success=False, reason="partial", path=path)])
        body = FakeBody([state_at((0, 64, 0))])
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to((0, 63, 0))

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "terrain_downward_runtime_missing")
        self.assertEqual(body.actions, [])

    def test_navigate_to_replans_after_recoverable_stuck(self):
        nav = FakeNavigator(
            [
                _segment("advanced", (2, 64, 0), success=False, reason="partial"),
                _segment("arrived", (4, 64, 0), success=True, reason="arrived"),
            ]
        )
        body = FakeBody(
            [state_at((0, 64, 0)), state_at((1, 64, 0)), state_at((1, 64, 0))],
            terminal_reasons=["stuck", "arrived"],
        )
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to(
            (4, 64, 0),
            config=NavigationRunConfig(
                max_segments=3,
                recovery_attempts=1,
                recovery_detour_max_attempts=0,
                min_partial_progress=2,
                backtrack_cost_factor=0.5,
                unloaded_boundary_limit=9,
                partial_tail_trim=2,
            ),
        )

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "arrived")
        self.assertEqual(len(body.actions), 2)
        self.assertEqual([call[0] for call in nav.calls], [(0, 64, 0), (1, 64, 0)])
        self.assertEqual(nav.calls[0][2]["previous_segment"], ())
        self.assertEqual(nav.calls[1][2]["previous_segment"], ((2, 64, 0),))
        self.assertEqual(nav.calls[1][2]["backtrack_cost_factor"], 0.5)
        self.assertEqual(nav.calls[1][2]["unloaded_boundary_limit"], 9)
        self.assertEqual(nav.calls[1][2]["partial_tail_trim"], 2)
        self.assertEqual(result.metrics["segment_count"], 2)
        self.assertEqual(result.metrics["segments"][0]["terminal_reason"], "stuck")

    def test_navigate_to_stops_after_recovery_budget_exhausted(self):
        nav = FakeNavigator([_segment("advanced", (2, 64, 0), success=False, reason="partial")])
        body = FakeBody(
            [state_at((0, 64, 0)), state_at((0, 64, 0)), state_at((0, 64, 0))],
            terminal_reasons=["stuck", "stuck"],
        )
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to(
            (4, 64, 0),
            config=NavigationRunConfig(max_segments=3, recovery_attempts=1, recovery_detour_max_attempts=0, min_partial_progress=2),
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "stuck")
        self.assertTrue(result.can_retry)
        self.assertEqual(len(body.actions), 2)
        self.assertEqual(result.metrics["segment_count"], 2)

    def test_navigate_to_classifies_recoverable_timeout_path_update(self):
        nav = FakeNavigator([_segment("advanced", (2, 64, 0), success=False, reason="partial")])
        body = FakeBody(
            [state_at((0, 64, 0)), state_at((0, 64, 0)), state_at((0, 64, 0))],
            terminal_reasons=["timeout", "timeout"],
        )
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to(
            (4, 64, 0),
            config=NavigationRunConfig(max_segments=3, recovery_attempts=1, recovery_detour_max_attempts=0, min_partial_progress=2),
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "timeout")
        self.assertEqual(result.metrics["path_update"]["source"], "terminal")
        self.assertEqual(result.metrics["path_update"]["category"], "timeout")

    def test_navigate_to_attempts_local_recovery_detour_after_stuck(self):
        nav = FakeNavigator(
            [
                _segment("advanced", (2, 64, 0), success=False, reason="partial"),
                _segment("arrived", (4, 64, 0), success=True, reason="arrived"),
            ]
        )
        body = FakeBody(
            [
                state_at((0, 64, 0)),
                state_at((0, 64, 0)),
                state_at((0, 64, 0)),
                state_at((1, 64, 0)),
                state_at((1, 64, 0)),
            ],
            terminal_reasons=["stuck", "arrived", "arrived"],
        )
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to(
            (4, 64, 0),
            config=NavigationRunConfig(max_segments=3, recovery_attempts=1, min_partial_progress=2),
        )

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "arrived")
        self.assertEqual([action.params["segment_status"] for action in body.actions], ["advanced", "recovery_detour", "arrived"])
        self.assertEqual(body.actions[1].params["break_context"], "recovery")
        self.assertEqual(body.actions[1].params["target"], [1, 64, 0])
        self.assertEqual(result.metrics["segments"][1]["status"], "recovery_detour")
        self.assertEqual(result.metrics["segments"][1]["terminal_reason"], "arrived")
        self.assertTrue(result.metrics["segments"][1]["success"])
        self.assertTrue(result.metrics["segments"][1]["diagnostics"]["attempts"][0]["displaced"])

    def test_navigate_to_recovery_detour_requires_real_displacement(self):
        nav = FakeNavigator(
            [
                _segment("advanced", (2, 64, 0), success=False, reason="partial"),
                _segment("arrived", (4, 64, 0), success=True, reason="arrived"),
            ]
        )
        body = FakeBody(
            [
                state_at((0, 64, 0)),
                state_at((0, 64, 0)),
                state_at((0, 64, 0)),
                state_at((0, 64, 0)),
                state_at((0, 64, 0)),
            ],
            terminal_reasons=["stuck", "arrived", "arrived"],
        )
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to(
            (4, 64, 0),
            config=NavigationRunConfig(max_segments=3, recovery_attempts=1, min_partial_progress=2),
        )

        self.assertTrue(result.success)
        detour = result.metrics["segments"][1]
        self.assertEqual(detour["status"], "recovery_detour")
        self.assertEqual(detour["terminal_reason"], "no_displacement")
        self.assertFalse(detour["success"])
        self.assertEqual(detour["diagnostics"]["attempts"][0]["displacement"], 0.0)

    def test_navigate_to_recovery_clearance_mines_block_then_retries_same_detour(self):
        nav = FakeNavigator(
            [
                _segment("advanced", (2, 64, 0), success=False, reason="partial"),
                _segment("arrived", (4, 64, 0), success=True, reason="arrived"),
            ]
        )
        body = FakeBody(
            [
                state_at((0, 64, 0)),
                state_at((0, 64, 0)),
                state_at((0, 64, 0)),
                state_at((0, 64, 0)),
                state_at((1, 64, 0)),
                state_at((1, 64, 0)),
            ],
            terminal_reasons=["stuck", "arrived", "arrived", "arrived"],
        )
        work = FakeWork()
        runtime = NavigationTransactions(body, nav, work=work)

        result = runtime.navigate_to(
            (4, 64, 0),
            config=NavigationRunConfig(max_segments=3, recovery_attempts=1, min_partial_progress=2),
        )

        self.assertTrue(result.success)
        self.assertEqual([action.params["segment_status"] for action in body.actions], ["advanced", "recovery_detour", "recovery_detour_clearance", "arrived"])
        self.assertEqual(work.mine_calls, [((1, 64, 0), BreakContext.RECOVERY, 3.0)])
        detour = result.metrics["segments"][1]
        self.assertTrue(detour["success"])
        attempt = detour["diagnostics"]["attempts"][0]
        self.assertTrue(attempt["clearance"]["success"])
        self.assertEqual(attempt["clearance"]["target"], [1, 64, 0])
        self.assertEqual(attempt["clearance"]["retry"]["reason"], "arrived")
        self.assertTrue(attempt["displaced"])

    def test_navigate_to_recovery_clearance_runs_after_recoverable_detour_stuck(self):
        nav = FakeNavigator(
            [
                _segment("advanced", (2, 64, 0), success=False, reason="partial"),
                _segment("arrived", (4, 64, 0), success=True, reason="arrived"),
            ]
        )
        body = FakeBody(
            [
                state_at((0, 64, 0)),
                state_at((0, 64, 0)),
                state_at((0, 64, 0)),
                state_at((0, 64, 0)),
                state_at((1, 64, 0)),
                state_at((1, 64, 0)),
            ],
            terminal_reasons=["stuck", "stuck", "arrived", "arrived"],
        )
        work = FakeWork()
        runtime = NavigationTransactions(body, nav, work=work)

        result = runtime.navigate_to(
            (4, 64, 0),
            config=NavigationRunConfig(max_segments=3, recovery_attempts=1, min_partial_progress=2),
        )

        self.assertTrue(result.success)
        self.assertEqual([action.params["segment_status"] for action in body.actions], ["advanced", "recovery_detour", "recovery_detour_clearance", "arrived"])
        self.assertEqual(work.mine_calls, [((1, 64, 0), BreakContext.RECOVERY, 3.0)])
        detour = result.metrics["segments"][1]
        self.assertTrue(detour["success"])
        attempt = detour["diagnostics"]["attempts"][0]
        self.assertEqual(attempt["terminal_reason"], "stuck")
        self.assertFalse(attempt["terminal_success"])
        self.assertTrue(attempt["clearance"]["success"])
        self.assertEqual(attempt["clearance"]["retry"]["reason"], "arrived")
        self.assertTrue(attempt["displaced"])

    def test_navigate_to_recovery_clearance_success_uses_retry_terminal_reason(self):
        nav = FakeNavigator(
            [
                _segment("advanced", (2, 64, 0), success=False, reason="partial"),
                _segment("arrived", (4, 64, 0), success=True, reason="arrived"),
            ]
        )
        body = FakeBody(
            [
                state_at((0, 64, 0)),
                state_at((0, 64, 0)),
                state_at((0, 64, 0)),
                state_at((0, 64, 0)),
                state_at((2, 64, 0)),
                state_at((2, 64, 0)),
            ],
            terminal_reasons=["stuck", "stuck", "arrived", "arrived"],
        )
        work = FakeWork()
        runtime = NavigationTransactions(body, nav, work=work)

        result = runtime.navigate_to(
            (4, 64, 0),
            config=NavigationRunConfig(
                max_segments=3,
                recovery_attempts=1,
                recovery_detour_distances=(1,),
                recovery_min_displacement=1.5,
                min_partial_progress=2,
            ),
        )

        self.assertTrue(result.success)
        detour = result.metrics["segments"][1]
        self.assertTrue(detour["success"])
        self.assertEqual(detour["terminal_reason"], "arrived")
        attempt = detour["diagnostics"]["attempts"][0]
        self.assertEqual(attempt["terminal_reason"], "stuck")
        self.assertEqual(attempt["clearance"]["retry"]["reason"], "arrived")
        self.assertTrue(attempt["displaced"])

    def test_navigate_to_recovery_clearance_failure_is_reported_without_success(self):
        nav = FakeNavigator(
            [
                _segment("advanced", (2, 64, 0), success=False, reason="partial"),
                _segment("arrived", (4, 64, 0), success=True, reason="arrived"),
            ]
        )
        body = FakeBody(
            [
                state_at((0, 64, 0)),
                state_at((0, 64, 0)),
                state_at((0, 64, 0)),
                state_at((0, 64, 0)),
                state_at((0, 64, 0)),
            ],
            terminal_reasons=["stuck", "arrived", "arrived"],
        )
        work = FakeWork(success=False, reason="break_denied")
        runtime = NavigationTransactions(body, nav, work=work)

        result = runtime.navigate_to(
            (4, 64, 0),
            config=NavigationRunConfig(max_segments=3, recovery_attempts=1, min_partial_progress=2),
        )

        self.assertTrue(result.success)
        self.assertEqual([action.params["segment_status"] for action in body.actions], ["advanced", "recovery_detour", "arrived"])
        self.assertEqual(work.mine_calls, [((1, 64, 0), BreakContext.RECOVERY, 3.0)])
        detour = result.metrics["segments"][1]
        self.assertFalse(detour["success"])
        self.assertEqual(detour["terminal_reason"], "no_displacement")
        attempt = detour["diagnostics"]["attempts"][0]
        self.assertFalse(attempt["clearance"]["success"])
        self.assertEqual(attempt["clearance"]["reason"], "break_denied")

    def test_navigate_to_recovery_detour_is_bounded_to_configured_attempts(self):
        nav = FakeNavigator(
            [
                _segment("advanced", (2, 64, 0), success=False, reason="partial"),
                _segment("arrived", (4, 64, 0), success=True, reason="arrived"),
            ]
        )
        body = FakeBody(
            [
                state_at((0, 64, 0)),
                state_at((0, 64, 0)),
                state_at((0, 64, 0)),
                state_at((0, 64, 0)),
                state_at((0, 64, 0)),
            ],
            terminal_reasons=["stuck", "arrived", "arrived"],
        )
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to(
            (4, 64, 0),
            config=NavigationRunConfig(
                max_segments=3,
                recovery_attempts=1,
                recovery_detour_offsets=((1, 0), (-1, 0), (0, 1)),
                recovery_detour_max_attempts=1,
            ),
        )

        self.assertTrue(result.success)
        self.assertEqual([action.params["segment_status"] for action in body.actions], ["advanced", "recovery_detour", "arrived"])
        self.assertEqual(body.actions[1].params["target"], [1, 64, 0])
        self.assertEqual(len(result.metrics["segments"][1]["diagnostics"]["attempts"]), 1)

    def test_navigate_to_recovery_detour_tries_farther_distance_when_near_detour_fails(self):
        nav = FakeNavigator(
            [
                _segment("advanced", (2, 64, 0), success=False, reason="partial"),
                _segment("arrived", (4, 64, 0), success=True, reason="arrived"),
            ]
        )
        body = FakeBody(
            [
                state_at((0, 64, 0)),
                state_at((0, 64, 0)),
                state_at((0, 64, 0)),
                state_at((0, 64, 0)),
                state_at((2, 64, 0)),
                state_at((2, 64, 0)),
            ],
            terminal_reasons=["stuck", "arrived", "arrived", "arrived"],
        )
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to(
            (4, 64, 0),
            config=NavigationRunConfig(
                max_segments=3,
                recovery_attempts=1,
                recovery_detour_distances=(1, 2),
                recovery_detour_offsets=((1, 0), (-1, 0)),
                recovery_detour_max_attempts=2,
                min_partial_progress=2,
            ),
        )

        self.assertTrue(result.success)
        self.assertEqual([action.params["segment_status"] for action in body.actions], ["advanced", "recovery_detour", "recovery_detour", "arrived"])
        self.assertEqual(body.actions[1].params["target"], [1, 64, 0])
        self.assertEqual(body.actions[2].params["target"], [2, 64, 0])
        attempts = result.metrics["segments"][2]["diagnostics"]["attempts"]
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0]["detour_distance"], 1)
        self.assertEqual(attempts[0]["target"], [1, 64, 0])
        self.assertFalse(attempts[0]["displaced"])
        self.assertEqual(attempts[1]["detour_distance"], 2)
        self.assertEqual(attempts[1]["target"], [2, 64, 0])
        self.assertTrue(attempts[1]["displaced"])

    def test_navigate_to_recovery_detour_prefers_standable_support_step_candidate(self):
        policy = GovernancePolicy(natural_regions=[Region("nav", (-2, 0, -2), (6, 100, 2))])
        costs = NavigationCostModel(policy)
        world = GridWorld(
            {
                (0, 64, 0): GridCell(),
                (1, 64, 0): GridCell(block_type="stone", walkable=False),
                (1, 65, 0): GridCell(),
                (1, 66, 0): GridCell(),
                (4, 64, 0): GridCell(),
            }
        )
        body = FakeBody(
            [
                state_at((0, 64, 0)),
                state_at((0, 64, 0)),
                state_at((0, 64, 0)),
                state_at((1, 65, 0)),
                state_at((1, 65, 0)),
            ],
            terminal_reasons=["stuck", "arrived", "arrived"],
        )
        runtime = NavigationTransactions(
            body,
            FakeNavigator(
                [
                    _segment("advanced", (2, 64, 0), success=False, reason="partial"),
                    _segment("arrived", (4, 64, 0), success=True, reason="arrived"),
                ],
                world=world,
                costs=costs,
            ),
        )

        result = runtime.navigate_to(
            (4, 64, 0),
            config=NavigationRunConfig(
                max_segments=3,
                recovery_attempts=1,
                recovery_detour_distances=(1,),
                recovery_detour_offsets=((1, 0),),
                recovery_detour_y_offsets=(0, 1, -1),
                recovery_detour_max_attempts=1,
                min_partial_progress=2,
            ),
        )

        self.assertTrue(result.success)
        self.assertEqual(body.actions[1].params["target"], [1, 65, 0])
        self.assertEqual(body.actions[1].params["recovery_pulse"]["kind"], "single_waypoint_move")
        self.assertEqual(body.actions[1].params["recovery_pulse"]["timeout_s"], 3.0)
        attempt = result.metrics["segments"][1]["diagnostics"]["attempts"][0]
        self.assertEqual(attempt["target_y_offset"], 1)
        self.assertEqual(attempt["target_kind"], "support_step_up")
        self.assertEqual(attempt["pulse_kind"], "single_waypoint_move")

    def test_navigate_to_recovery_detour_prefers_standable_support_step_down_candidate(self):
        policy = GovernancePolicy(natural_regions=[Region("nav", (-2, 0, -2), (6, 100, 2))])
        costs = NavigationCostModel(policy)
        world = GridWorld(
            {
                (0, 64, 0): GridCell(),
                (1, 64, 0): GridCell(),
                (1, 63, 0): GridCell(),
                (1, 62, 0): GridCell(block_type="stone", walkable=False),
                (4, 64, 0): GridCell(),
            }
        )
        body = FakeBody(
            [
                state_at((0, 64, 0)),
                state_at((0, 64, 0)),
                state_at((0, 64, 0)),
                state_at((1, 63, 0)),
                state_at((1, 63, 0)),
            ],
            terminal_reasons=["stuck", "arrived", "arrived"],
        )
        runtime = NavigationTransactions(
            body,
            FakeNavigator(
                [
                    _segment("advanced", (2, 64, 0), success=False, reason="partial"),
                    _segment("arrived", (4, 64, 0), success=True, reason="arrived"),
                ],
                world=world,
                costs=costs,
            ),
        )

        result = runtime.navigate_to(
            (4, 64, 0),
            config=NavigationRunConfig(
                max_segments=3,
                recovery_attempts=1,
                recovery_detour_distances=(1,),
                recovery_detour_offsets=((1, 0),),
                recovery_detour_y_offsets=(0, -1, 1),
                recovery_detour_max_attempts=1,
                min_partial_progress=2,
            ),
        )

        self.assertTrue(result.success)
        self.assertEqual(body.actions[1].params["target"], [1, 63, 0])
        self.assertEqual(body.actions[1].params["recovery_pulse"]["kind"], "single_waypoint_move")
        attempt = result.metrics["segments"][1]["diagnostics"]["attempts"][0]
        self.assertEqual(attempt["target_y_offset"], -1)
        self.assertEqual(attempt["target_kind"], "support_step_down")
        self.assertEqual(attempt["pulse_kind"], "single_waypoint_move")

    def test_navigate_to_recovery_detour_uses_water_prep_candidate_without_clearance(self):
        policy = GovernancePolicy(natural_regions=[Region("nav", (-2, 0, -2), (6, 100, 2))])
        costs = NavigationCostModel(policy)
        world = GridWorld(
            {
                (0, 64, 0): GridCell(),
                (1, 64, 0): GridCell(block_type="water", liquid=True),
                (1, 65, 0): GridCell(),
                (4, 64, 0): GridCell(),
            }
        )
        body = FakeBody(
            [
                state_at((0, 64, 0)),
                state_at((0, 64, 0)),
                state_at((0, 64, 0)),
                state_at((1, 64, 0)),
                state_at((1, 64, 0)),
            ],
            terminal_reasons=["stuck", "arrived", "arrived"],
        )
        work = FakeWork()
        runtime = NavigationTransactions(
            body,
            FakeNavigator(
                [
                    _segment("advanced", (2, 64, 0), success=False, reason="partial"),
                    _segment("arrived", (4, 64, 0), success=True, reason="arrived"),
                ],
                world=world,
                costs=costs,
            ),
            work=work,
        )

        result = runtime.navigate_to(
            (4, 64, 0),
            config=NavigationRunConfig(
                max_segments=3,
                recovery_attempts=1,
                recovery_detour_distances=(1,),
                recovery_detour_offsets=((1, 0),),
                recovery_detour_y_offsets=(0,),
                recovery_detour_max_attempts=1,
                min_partial_progress=2,
            ),
        )

        self.assertTrue(result.success)
        self.assertEqual(body.actions[1].params["target"], [1, 64, 0])
        self.assertEqual(body.actions[1].params["path_moves"], ["swim"])
        self.assertEqual(body.actions[1].params["recovery_pulse"]["kind"], "single_waypoint_move")
        self.assertEqual(work.mine_calls, [])
        attempt = result.metrics["segments"][1]["diagnostics"]["attempts"][0]
        self.assertEqual(attempt["target_kind"], "water_prep")
        self.assertEqual(attempt["target_y_offset"], 0)
        self.assertEqual(attempt["pulse_kind"], "single_waypoint_move")
        self.assertTrue(attempt["displaced"])
        self.assertNotIn("clearance", attempt)

    def test_navigate_to_rejects_negative_recovery_detour_config(self):
        nav = FakeNavigator([_segment("arrived", (2, 64, 0), success=True, reason="arrived")])
        body = FakeBody([state_at((0, 64, 0))])
        runtime = NavigationTransactions(body, nav)

        with self.assertRaises(ValueError):
            runtime.navigate_to((2, 64, 0), config=NavigationRunConfig(recovery_detour_max_attempts=-1))
        with self.assertRaises(ValueError):
            runtime.navigate_to((2, 64, 0), config=NavigationRunConfig(recovery_min_displacement=-0.1))
        with self.assertRaises(ValueError):
            runtime.navigate_to((2, 64, 0), config=NavigationRunConfig(recovery_detour_timeout_s=0))
        with self.assertRaises(ValueError):
            runtime.navigate_to((2, 64, 0), config=NavigationRunConfig(recovery_detour_distances=(0, 1)))
        with self.assertRaises(ValueError):
            runtime.navigate_to((2, 64, 0), config=NavigationRunConfig(recovery_detour_y_offsets=(0, 0, 1)))

    def test_navigate_to_accepts_timeout_s_for_transaction_navigator_protocol(self):
        nav = FakeNavigator([_segment("arrived", (2, 64, 0), success=True, reason="arrived")])
        body = FakeBody([state_at((0, 64, 0)), state_at((2, 64, 0))])
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to((2, 64, 0), timeout_s=2.5)

        self.assertTrue(result.success)
        self.assertEqual(body.await_timeouts, [2.5])

    def test_navigate_to_rejects_non_positive_timeout_s(self):
        nav = FakeNavigator([_segment("arrived", (2, 64, 0), success=True, reason="arrived")])
        body = FakeBody([state_at((0, 64, 0))])
        runtime = NavigationTransactions(body, nav)

        with self.assertRaises(ValueError):
            runtime.navigate_to((2, 64, 0), timeout_s=0)

    def test_navigate_to_yields_on_repeated_no_progress(self):
        nav = FakeNavigator([_segment("advanced", (1, 64, 0), success=False, reason="partial")])
        body = FakeBody([state_at((0, 64, 0))], terminal_success=True)
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to(
            (3, 64, 0),
            config=NavigationRunConfig(max_segments=5, min_partial_progress=1),
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "progress_yielded")
        self.assertTrue(result.can_retry)
        self.assertEqual(len(body.actions), 3)


def _navigator(cells):
    policy = GovernancePolicy(natural_regions=[Region("work", (-10, 0, -10), (20, 100, 10))])
    return SegmentedNavigator(GridWorld(cells), NavigationCostModel(policy))


def _segment(status, target, *, success, reason, recheck=None, path=None):
    if path is None:
        path = ()
    if target is not None and not path:
        path = (PathStep(pos=target, move=MoveKind.WALK, cost=1.0, reason="walk"),)
    path_start = None
    if path:
        first = path[0]
        if first.move == MoveKind.DIAGONAL:
            path_start = [first.pos[0] - 1, first.pos[1], first.pos[2] - 1]
        else:
            path_start = list(path[0].pos)
    return NavigationSegment(
        status=status,
        target=target,
        plan=PathResult(
            success=success,
            reason=reason,
            path=tuple(path),
            cost=1.0,
            expanded=1,
            diagnostics={} if path_start is None else {"path_start": path_start},
        ),
        recheck=recheck or RecheckResult(ok=True, reason="valid", checked=len(path)),
    )


if __name__ == "__main__":
    unittest.main()
