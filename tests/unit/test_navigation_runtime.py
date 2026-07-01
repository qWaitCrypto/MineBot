import unittest
from types import SimpleNamespace

from minebot.body import NavigationRunConfig, NavigationTransactions
from minebot.body.navigation import make_block_at_prism_world_update
from minebot.body.world_read import read_block_cells_tiled, refresh_grid_world_around
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
        self.perceptions: list[tuple[str, dict[str, object]]] = []

    def get_state(self):
        if not self.states:
            return state_at((0, 64, 0))
        if len(self.states) == 1:
            return self.states[0]
        return self.states.pop(0)

    def perceive(self, scope: str, params: dict[str, object]) -> PerceptionResult:
        self.perceptions.append((scope, dict(params)))
        if scope == "blockCells":
            cells = params.get("cells") or []
            start = int(params.get("start") or 0)
            limit = int(params.get("limit") or 64)
            page = cells[start : start + limit]
            facts = []
            for raw_pos in page:
                pos = (int(raw_pos[0]), int(raw_pos[1]), int(raw_pos[2]))
                raw = self.blocks.get(pos, ("air", "CLEAR"))
                if len(raw) == 2:
                    block_type, state = raw
                    properties = {}
                else:
                    block_type, state, properties = raw
                facts.append(
                    {
                        "x": pos[0],
                        "y": pos[1],
                        "z": pos[2],
                        "type": block_type,
                        "state": state,
                        "properties": properties,
                    }
                )
            next_index = start + len(page)
            nxt = None if next_index >= len(cells) else next_index
            return PerceptionResult(
                bot="Bot1",
                scope="blockCells",
                type="perception",
                ok=True,
                complete=nxt is None,
                data={"cells": facts, "next": nxt, "count": len(facts), "total": len(cells)},
                uncertainty=[] if nxt is None else [{"reason": "limit_exceeded"}],
                next=None,
                error=None,
            )
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

    def await_action_terminal(self, action_id: str, timeout_s: float = 15.0, **kwargs) -> Event:
        self.await_timeouts.append(timeout_s)
        action = next(action for action in self.actions if action.id == action_id)
        reason = self.terminal_reasons.pop(0) if self.terminal_reasons else None
        if reason in {"death", "bodyMissing", "respawned"}:
            return Event(
                seq=len(self.actions),
                tick=10,
                bot="Bot1",
                name=reason,
                data={"pos": [0, 59, 0], "inventory_hash": "dead"},
            )
        default_reason = "completed" if action.name in {"mineBlock", "placeBlock"} else ("arrived" if self.terminal_success else "stuck")
        stopped_reason = reason or default_reason
        arrived = stopped_reason == "arrived"
        success = stopped_reason in {"arrived", "completed"}
        event_name = {
            "moveTo": "moveDone",
            "navigateTo": "navigateDone",
            "followEntity": "followDone",
            "mineBlock": "mineDone",
            "placeBlock": "placeDone",
            "useItem": "useDone",
        }.get(action.name, "moveDone")
        data = {"action_id": action_id, "stopped_reason": stopped_reason}
        if action.name == "navigateTo":
            data["arrived"] = arrived
            data["reason"] = stopped_reason
            data["nav_reason"] = stopped_reason
            data["goal_dist"] = 0.0 if arrived else 10.0
            data["expanded"] = 50
            data["waypoints"] = 5
        elif action.name == "followEntity":
            data["arrived"] = stopped_reason == "arrived"
            data["reason"] = stopped_reason
        elif action.name == "moveTo":
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
    def __init__(self, success=True, reason="completed", *, body: FakeBody | None = None):
        self.success = success
        self.reason = reason
        self.body = body
        self.mine_calls = []
        self.place_calls = []
        self.dig_up_calls = []
        self.dig_down_calls = []

    def mine_block(self, pos, *, context, timeout_s=30.0):
        self.mine_calls.append((pos, context, timeout_s))
        from minebot.contract import ToolResult

        if self.success and self.body is not None:
            self.body.blocks[pos] = ("air", "CLEAR")
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

        if self.success and self.body is not None:
            self.body.blocks[pos] = (block_type, "SOLID")
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

        if self.success and self.body is not None and current_pos is not None:
            self.body.blocks[current_pos] = ("minecraft:cobblestone", "SOLID")
            self.body.blocks[(current_pos[0], current_pos[1] + 1, current_pos[2])] = ("air", "CLEAR")
            self.body.blocks[(current_pos[0], current_pos[1] + 2, current_pos[2])] = ("air", "CLEAR")
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

        if self.success and self.body is not None and current_pos is not None:
            self.body.blocks[(current_pos[0], current_pos[1] - 1, current_pos[2])] = ("air", "CLEAR")
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

        if self.success and self.body is not None and current_pos is not None:
            self.body.blocks[(current_pos[0], target_y, current_pos[2])] = ("air", "CLEAR")
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
        self.assertEqual(body.actions[0].name, "navigateTo")

    def test_move_away_wraps_last_navigation_failure(self):
        nav = FakeNavigator([_segment("blocked", None, success=False, reason="no_path")])
        body = FakeBody([state_at((0, 64, 0)), state_at((0, 64, 0)), state_at((0, 64, 0))],
                        terminal_reasons=["no_path"])
        runtime = NavigationTransactions(body, nav)

        result = runtime.move_away((0.0, 64.0, 0.0), min_distance=3.0, candidate_radii=(4,), max_candidates=1)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "move_away_failed:no_path")
        self.assertTrue(result.can_retry)

    def test_move_away_uses_candidate_selection_instead_of_local_world_cells(self):
        nav = FakeNavigator([_segment("arrived", (4, 64, 4), success=True, reason="arrived")])
        body = FakeBody([state_at((0, 64, 0)), state_at((0, 64, 0)), state_at((4, 64, 4))])
        runtime = NavigationTransactions(body, nav)

        result = runtime.move_away((0.0, 64.0, 0.0), min_distance=4.0, candidate_radii=(4,), max_candidates=4)

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "moved_away")
        self.assertEqual(result.metrics["chosen_goal"], [4, 64, 4])
        self.assertEqual(body.actions[0].name, "navigateTo")

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
                state_at((0, 64, 0)),
                state_at((4, 64, 0)),
                state_at((4, 64, 0)),
                state_at((4, 64, 0)),
                state_at((4, 64, 0)),
                state_at((4, 64, 0)),
                state_at((8, 64, 0)),
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
        self.assertEqual(len(body.actions), 2)

    def test_navigate_to_sends_navigate_to_action_and_returns_arrived(self):
        nav = FakeNavigator([_segment("arrived", (3, 64, 0), success=True, reason="arrived")])
        body = FakeBody([state_at((0, 64, 0))])
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to((3, 64, 0), config=NavigationRunConfig(max_segments=2))

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "arrived")
        self.assertEqual(len(body.actions), 1)
        self.assertEqual(body.actions[0].name, "navigateTo")
        self.assertEqual(body.actions[0].params["target"], [3, 64, 0])

    def test_navigate_to_accepts_typed_goal(self):
        nav = FakeNavigator([_segment("arrived", (5, 64, 0), success=True, reason="arrived")])
        body = FakeBody([state_at((0, 64, 0))])
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to(GoalNear((5, 64, 0), radius=2))

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "arrived")
        action = body.actions[0]
        self.assertEqual(action.name, "navigateTo")
        self.assertEqual(action.params["target"], [5, 64, 0])

    def test_navigate_to_can_send_precise_arrival_radius(self):
        nav = FakeNavigator([_segment("arrived", (3, 64, 0), success=True, reason="arrived")])
        body = FakeBody([state_at((0, 64, 0))])
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to((3, 64, 0), arrival_radius=0.25)

        self.assertTrue(result.success)
        self.assertEqual(body.actions[0].params["arrival_radius"], 0.25)

    def test_navigate_to_continues_after_partial_segment(self):
        nav = FakeNavigator([_segment("arrived", (10, 64, 0), success=True, reason="arrived")])
        body = FakeBody(
            [state_at((0, 64, 0)), state_at((5, 64, 0)), state_at((5, 64, 0))],
            terminal_reasons=["partial", "arrived"],
        )
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to((10, 64, 0), config=NavigationRunConfig(max_segments=4))

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "arrived")
        self.assertEqual(len(body.actions), 2)

    def test_navigate_to_returns_failure_on_stuck(self):
        nav = FakeNavigator([_segment("arrived", (10, 64, 0), success=True, reason="arrived")])
        body = FakeBody([state_at((0, 64, 0))], terminal_reasons=["stuck"])
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to((10, 64, 0))

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "stuck")
        self.assertTrue(result.can_retry)

    def test_navigate_to_surfaces_global_death_event_as_body_fact(self):
        nav = FakeNavigator([_segment("arrived", (10, 64, 0), success=True, reason="arrived")])
        body = FakeBody([state_at((0, 64, 0))], terminal_reasons=["death"])
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to((10, 64, 0))

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "death")
        self.assertEqual(result.metrics["segments"][0]["diagnostics"]["event"], "death")

    def test_navigate_to_returns_failure_on_no_path(self):
        nav = FakeNavigator([_segment("blocked", None, success=False, reason="no_path")])
        body = FakeBody([state_at((0, 64, 0))], terminal_reasons=["no_path"])
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to((10, 64, 0))

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "no_path")

    def test_navigate_to_returns_failure_on_body_rejection(self):
        nav = FakeNavigator([_segment("arrived", (3, 64, 0), success=True, reason="arrived")])
        body = FakeBody([state_at((0, 64, 0))], accept=False)
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to((3, 64, 0))

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "body_rejected")

    def test_navigate_to_respects_segment_budget(self):
        nav = FakeNavigator([_segment("arrived", (30, 64, 0), success=True, reason="arrived")])
        body = FakeBody(
            [state_at((0, 64, 0)), state_at((5, 64, 0)), state_at((10, 64, 0)),
             state_at((15, 64, 0))],
            terminal_reasons=["partial", "partial", "partial"],
        )
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to((30, 64, 0), config=NavigationRunConfig(max_segments=3))

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "segment_budget_exhausted")
        self.assertEqual(len(body.actions), 3)

    def test_navigate_to_accepts_timeout_s(self):
        nav = FakeNavigator([_segment("arrived", (3, 64, 0), success=True, reason="arrived")])
        body = FakeBody([state_at((0, 64, 0))])
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to((3, 64, 0), timeout_s=5.0)

        self.assertTrue(result.success)
        action = body.actions[0]
        self.assertEqual(action.params["timeout_ticks"], 300)

    def test_navigate_to_spreads_long_timeout_across_segments(self):
        nav = FakeNavigator([_segment("arrived", (3, 64, 0), success=True, reason="arrived")])
        body = FakeBody([state_at((0, 64, 0))])
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to((3, 64, 0), timeout_s=80.0, config=NavigationRunConfig(max_segments=4))

        self.assertTrue(result.success)
        action = body.actions[0]
        self.assertEqual(action.params["timeout_ticks"], 400)

    def test_navigate_to_rejects_non_positive_timeout_s(self):
        nav = FakeNavigator([])
        body = FakeBody([state_at((0, 64, 0))])
        runtime = NavigationTransactions(body, nav)

        with self.assertRaises(ValueError):
            runtime.navigate_to((3, 64, 0), timeout_s=-1.0)

    def test_navigate_to_returns_preempted_when_generation_stale(self):
        nav = FakeNavigator([_segment("arrived", (3, 64, 0), success=True, reason="arrived")])
        from minebot.brain.progress import ProgressAuthority
        progress = ProgressAuthority()

        class PreemptingBody(FakeBody):
            def await_action_terminal(self, action_id, **kwargs):
                progress.invalidate_generation("test_preempt")
                return super().await_action_terminal(action_id, **kwargs)

        body = PreemptingBody(
            [state_at((0, 64, 0)), state_at((2, 64, 0)), state_at((2, 64, 0))],
            terminal_reasons=["partial"],
        )
        runtime = NavigationTransactions(body, nav, progress=progress)

        result = runtime.navigate_to((3, 64, 0), config=NavigationRunConfig(max_segments=4))

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "preempted")

    def test_navigate_to_does_not_invalidate_outer_generation_when_shared(self):
        nav = FakeNavigator([_segment("arrived", (3, 64, 0), success=True, reason="arrived")])
        from minebot.brain.progress import ProgressAuthority
        progress = ProgressAuthority()
        outer_generation = progress.next_generation()
        body = FakeBody([state_at((0, 64, 0)), state_at((3, 64, 0))], terminal_reasons=["arrived"])
        runtime = NavigationTransactions(body, nav, progress=progress)

        result = runtime.navigate_to((3, 64, 0), config=NavigationRunConfig(max_segments=4))

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "arrived")
        self.assertTrue(progress.generation_current(outer_generation))

    def test_navigate_to_metrics_include_segments(self):
        nav = FakeNavigator([_segment("arrived", (3, 64, 0), success=True, reason="arrived")])
        body = FakeBody([state_at((0, 64, 0))])
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to((3, 64, 0))

        self.assertIsNotNone(result.metrics)
        segments = result.metrics.get("segments")
        self.assertIsInstance(segments, list)
        self.assertEqual(len(segments), 1)

    def test_follow_entity_sends_follow_action_and_returns_arrived(self):
        nav = FakeNavigator([])
        body = FakeBody([state_at((0, 64, 0))], terminal_reasons=["arrived"])
        runtime = NavigationTransactions(body, nav)

        result = runtime.follow_entity("TargetPlayer", keep_distance=3.0, timeout_s=5.0)

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "arrived")
        self.assertEqual(len(body.actions), 1)
        self.assertEqual(body.actions[0].name, "followEntity")
        self.assertEqual(body.actions[0].params["target_spec"], "TargetPlayer")
        self.assertEqual(body.actions[0].params["keep_radius"], 3.0)

    def test_follow_entity_returns_target_lost(self):
        nav = FakeNavigator([])
        body = FakeBody([state_at((0, 64, 0))], terminal_reasons=["target_lost"])
        runtime = NavigationTransactions(body, nav)

        result = runtime.follow_entity("Ghost", timeout_s=5.0)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "target_lost")
        self.assertTrue(result.can_retry)

    def test_follow_entity_returns_timeout(self):
        nav = FakeNavigator([])
        body = FakeBody([state_at((0, 64, 0))], terminal_reasons=["timeout"])
        runtime = NavigationTransactions(body, nav)

        result = runtime.follow_entity("Runner", timeout_s=5.0)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "timeout")

    def test_follow_entity_returns_body_rejected(self):
        nav = FakeNavigator([])
        body = FakeBody([state_at((0, 64, 0))], accept=False)
        runtime = NavigationTransactions(body, nav)

        result = runtime.follow_entity("Someone", timeout_s=5.0)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "body_rejected")

    def test_follow_entity_rejects_empty_target_and_bad_timeout(self):
        nav = FakeNavigator([])
        body = FakeBody([state_at((0, 64, 0))])
        runtime = NavigationTransactions(body, nav)

        with self.assertRaises(ValueError):
            runtime.follow_entity("", timeout_s=5.0)
        with self.assertRaises(ValueError):
            runtime.follow_entity("X", timeout_s=0)
        with self.assertRaises(ValueError):
            runtime.follow_entity("X", keep_distance=-1, timeout_s=5.0)

    def test_follow_entity_metrics_carry_target_spec(self):
        nav = FakeNavigator([])
        body = FakeBody([state_at((0, 64, 0))], terminal_reasons=["arrived"])
        runtime = NavigationTransactions(body, nav)

        result = runtime.follow_entity("TargetPlayer", timeout_s=5.0)

        self.assertEqual(result.metrics["target_spec"], "TargetPlayer")
        self.assertEqual(result.metrics["event"], "followDone")


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


class WorldRefreshTests(unittest.TestCase):
    def test_refresh_populates_grid_world_from_live_terrain(self):
        blocks = {
            (0, 63, 0): ("stone", "SOLID"),
            (1, 64, 0): ("water", "LIQUID"),
            # everything else defaults to ("air", "CLEAR") in FakeBody.perceive
        }
        body = FakeBody([state_at((0, 64, 0))], blocks=blocks)
        world = GridWorld({})

        diag = refresh_grid_world_around(body, world, (0, 64, 0), h_radius=1, y_below=1, y_above=1)

        # 3x3x3 window = 27 cells read into a previously-empty world.
        self.assertEqual(diag["refreshed_cells"], 27)
        self.assertEqual(diag["world_cells"], 27)
        floor = world.cell_at((0, 63, 0))
        self.assertIsNotNone(floor)
        self.assertFalse(floor.walkable)  # stone is a solid wall
        liquid = world.cell_at((1, 64, 0))
        self.assertTrue(liquid.liquid)
        air = world.cell_at((0, 64, 0))
        self.assertTrue(air.walkable)  # air is walkable

    def test_refresh_accumulates_across_calls_without_clearing(self):
        body = FakeBody([state_at((0, 64, 0)), state_at((5, 64, 0))], blocks={})
        world = GridWorld({})

        refresh_grid_world_around(body, world, (0, 64, 0), h_radius=1, y_below=0, y_above=0)
        first_cells = len(world.cells)
        refresh_grid_world_around(body, world, (5, 64, 0), h_radius=1, y_below=0, y_above=0)

        # Both windows are retained (no clear); the first window's cells survive.
        self.assertGreater(len(world.cells), first_cells)
        self.assertIsNotNone(world.cell_at((0, 64, 0)))
        self.assertIsNotNone(world.cell_at((5, 64, 0)))

    def test_refresh_propagates_incomplete_perception_as_failure(self):
        class IncompleteBody(FakeBody):
            def perceive(self, scope, params):
                result = super().perceive(scope, params)
                return PerceptionResult(
                    bot=result.bot,
                    scope=result.scope,
                    type=result.type,
                    ok=False,
                    complete=False,
                    data=result.data,
                    uncertainty=[{"reason": "unloaded"}],
                    next=None,
                    error="unloaded",
                )

        body = IncompleteBody([state_at((0, 64, 0))], blocks={})
        world = GridWorld({})

        with self.assertRaises(ValueError):
            refresh_grid_world_around(body, world, (0, 64, 0), h_radius=0, y_below=0, y_above=0)
        # No invented cells were seeded into the planner world on failure.
        self.assertEqual(len(world.cells), 0)


class WorldRefreshNavigationIntegrationTests(unittest.TestCase):
    def test_navigate_to_uses_server_side_pathfinding(self):
        body = FakeBody(
            [state_at((0, 64, 0)), state_at((1, 64, -1))],
            terminal_success=True,
        )
        policy = GovernancePolicy(natural_regions=[Region("work", (-10, 0, -10), (20, 100, 10))])
        world = GridWorld({})
        navigator = SegmentedNavigator(world, NavigationCostModel(policy))
        runtime = NavigationTransactions(body, navigator)

        result = runtime.navigate_to(
            (1, 64, -1),
            config=NavigationRunConfig(max_segments=4),
        )

        self.assertTrue(any(action.name == "navigateTo" for action in body.actions))
        self.assertEqual(result.reason, "arrived")

    def test_navigate_to_uses_real_terrain_server_side_budget(self):
        body = FakeBody(
            [state_at((0, 64, 0)), state_at((1, 64, -1))],
            terminal_success=True,
        )
        policy = GovernancePolicy(natural_regions=[Region("work", (-128, 0, -128), (128, 160, 128))])
        runtime = NavigationTransactions.server_side(body, policy)

        result = runtime.navigate_to(
            (64, 70, -64),
            timeout_s=80.0,
            config=NavigationRunConfig(max_segments=8),
        )

        self.assertEqual(result.reason, "arrived")
        action = next(action for action in body.actions if action.name == "navigateTo")
        self.assertEqual(action.params["grid_radius"], 64)
        self.assertEqual(action.params["max_expand"], 2500)
        self.assertEqual(action.params["no_progress_ticks"], 120)
        self.assertEqual(action.params["timeout_ticks"], 300)

    def test_world_refresh_uses_block_cells_batches_not_per_cell_block_at(self):
        body = FakeBody([state_at((0, 64, 0))], blocks={(0, 63, 0): ("stone", "SOLID")})
        world = GridWorld({})

        refresh_grid_world_around(body, world, (0, 64, 0), h_radius=1, y_below=1, y_above=1)

        scopes = [scope for scope, _params in body.perceptions]
        self.assertIn("blockCells", scopes)
        self.assertNotIn("blockAt", scopes)
        self.assertFalse(world.cell_at((0, 63, 0)).walkable)

    def test_navigate_to_does_not_use_local_terrain_fallback_by_default(self):
        body = FakeBody(
            [
                state_at((0, 64, 0)),
                state_at((0, 64, 0)),
            ],
            terminal_reasons=["stuck"],
        )
        policy = GovernancePolicy(natural_regions=[Region("work", (-10, 0, -10), (10, 100, 10))])
        navigator = SegmentedNavigator(GridWorld({}), NavigationCostModel(policy))
        work = FakeWork(success=True, body=body)
        runtime = NavigationTransactions(body, navigator, work=work)

        result = runtime.navigate_to(
            (0, 65, 0),
            break_context=BreakContext.RECOVERY,
            config=NavigationRunConfig(max_segments=4, min_partial_progress=1),
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "stuck")
        self.assertEqual([action.name for action in body.actions], ["navigateTo"])
        self.assertEqual(work.place_calls, [])
        self.assertEqual(work.dig_up_calls, [])
        self.assertNotIn("blockCells", [scope for scope, _params in body.perceptions])

    def test_collect_approach_can_opt_in_to_terrain_fallback_after_scarpet_stuck(self):
        body = FakeBody(
            [
                state_at((0, 64, 0)),
                state_at((0, 64, 0)),
                state_at((0, 64, 0)),
                state_at((0, 64, 0)),
                state_at((0, 64, 0)),
                state_at((0, 65, 0)),
            ],
            terminal_reasons=["stuck"],
        )
        policy = GovernancePolicy(natural_regions=[Region("work", (-10, 0, -10), (10, 100, 10))])
        navigator = SegmentedNavigator(GridWorld({}), NavigationCostModel(policy))
        work = FakeWork(success=True, body=body)
        runtime = NavigationTransactions(body, navigator, work=work)

        result = runtime.navigate_to(
            (0, 65, 0),
            break_context=BreakContext.COLLECT_APPROACH,
            config=NavigationRunConfig(
                max_segments=4,
                min_partial_progress=1,
                allow_local_terrain_fallback=True,
            ),
        )

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "arrived")
        self.assertEqual(body.actions[0].name, "navigateTo")
        self.assertEqual(work.place_calls, [((0, 64, 0), "minecraft:cobblestone", None, BreakContext.TRAVEL, "scaffold", 15.0)])
        self.assertEqual(work.dig_up_calls, [((0, 64, 0), BreakContext.COLLECT_APPROACH, 15.0)])
        self.assertTrue(any(scope == "blockCells" for scope, _params in body.perceptions))
        self.assertNotIn("blockAt", [scope for scope, _params in body.perceptions])
        segments = result.metrics["segments"]
        self.assertTrue(any(item["status"] == "terrain_place" for item in segments))
        self.assertTrue(any(item["status"] == "terrain_pillar" for item in segments))
        self.assertEqual(result.metrics["terrain_fallback_original_reason"], "stuck")

    def test_collect_approach_fallback_moves_to_prefix_before_terrain_action(self):
        class MovingFakeBody(FakeBody):
            def await_action_terminal(self, action_id: str, timeout_s: float = 15.0, **kwargs) -> Event:
                terminal = super().await_action_terminal(action_id, timeout_s=timeout_s, **kwargs)
                action = next(action for action in self.actions if action.id == action_id)
                if action.name == "moveTo" and terminal.data.get("arrived"):
                    self.states.insert(0, state_at(tuple(action.params["target"])))
                return terminal

        body = MovingFakeBody(
            [
                state_at((0, 64, 0)),
                state_at((0, 64, 0)),
                state_at((0, 64, 0)),
            ],
            terminal_reasons=["stuck", "arrived"],
            blocks={
                (0, 63, 0): ("stone", "SOLID"),
                (1, 63, 0): ("stone", "SOLID"),
                (2, 63, 0): ("stone", "SOLID"),
                (3, 63, 0): ("stone", "SOLID"),
                (3, 64, 0): ("stone", "SOLID"),
            },
        )
        policy = GovernancePolicy(natural_regions=[Region("work", (-10, 0, -10), (10, 100, 10))])
        navigator = SegmentedNavigator(GridWorld({}), NavigationCostModel(policy))
        work = FakeWork(success=True, body=body)
        runtime = NavigationTransactions(body, navigator, work=work)

        result = runtime.navigate_to(
            (3, 64, 0),
            break_context=BreakContext.COLLECT_APPROACH,
            config=NavigationRunConfig(
                max_segments=5,
                min_partial_progress=1,
                allow_local_terrain_fallback=True,
            ),
        )

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "arrived")
        self.assertEqual([action.name for action in body.actions[:2]], ["navigateTo", "moveTo"])
        self.assertEqual(body.actions[1].params["target"], [2, 64, 0])
        self.assertEqual(work.mine_calls, [((3, 64, 0), BreakContext.COLLECT_APPROACH, 15.0)])
        segments = result.metrics["segments"]
        self.assertTrue(any(item["status"] == "advanced" for item in segments))
        self.assertTrue(any(item["status"] == "terrain_break" for item in segments))


if __name__ == "__main__":
    unittest.main()
