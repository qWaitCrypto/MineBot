import unittest
from types import SimpleNamespace

from minebot.body import NavigationRunConfig, NavigationTransactions
from minebot.body.world_read import read_block_cells_tiled, refresh_grid_world_around
from minebot.game.governance import GovernancePolicy, Region
from minebot.contract import Action, BodyState, BreakContext, Event, PerceptionResult, Result
from minebot.game.navigation import (
    GoalComposite,
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

    def __init__(self, states, *, accept=True, terminal_success=True, terminal_reasons=None, blocks=None, poll_events=None):
        self.states = list(states)
        self.accept = accept
        self.terminal_success = terminal_success
        self.terminal_reasons = list(terminal_reasons or [])
        self.poll_event_batches = [list(batch) for batch in (poll_events or [])]
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
            data["move_ticks"] = 37
            data["move_min_dist"] = 4.25
            data["move_stuck_ticks"] = 12
            data["move_deviation"] = 0.5
            data["move_waypoint_index"] = 2
            data["move_waypoint_count"] = 5
            data["move_current_waypoint"] = [2.5, 64.0, 0.5]
            data["movement_counts"] = {
                "walk": 5,
                "diagonal": 0,
                "ascend": 0,
                "descend": 0,
                "swim": 0,
                "fall": 0,
            }
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

    def poll_events(self) -> list[Event]:
        if not self.poll_event_batches:
            return []
        return self.poll_event_batches.pop(0)


class InventoryNavigationBody(FakeBody):
    def __init__(self, states, inventory_pages, **kwargs):
        super().__init__(states, **kwargs)
        self.inventory_pages = list(inventory_pages)

    def perceive(self, scope: str, params: dict[str, object]) -> PerceptionResult:
        if scope == "inventory":
            self.perceptions.append((scope, dict(params)))
            if not self.inventory_pages:
                raise AssertionError("unexpected inventory page")
            return self.inventory_pages.pop(0)
        return super().perceive(scope, params)


class MutationNavigationBody(InventoryNavigationBody):
    def __init__(self, states, inventory_pages, navigation_events, **kwargs):
        super().__init__(states, inventory_pages, **kwargs)
        self.navigation_events = list(navigation_events)

    def await_action_terminal(self, action_id: str, timeout_s: float = 15.0, **kwargs) -> Event:
        self.await_timeouts.append(timeout_s)
        if not self.navigation_events:
            raise AssertionError("unexpected navigation await")
        name, raw_data = self.navigation_events.pop(0)
        return Event(
            seq=len(self.actions) + 1,
            tick=10,
            bot="Bot1",
            name=name,
            data={"action_id": action_id, **dict(raw_data)},
        )


def inventory_page(slots, *, complete: bool, next_start=None, ok: bool = True):
    return PerceptionResult(
        bot="Bot1",
        scope="inventory",
        type="perception",
        ok=ok,
        complete=complete,
        data={"slots": list(slots), "nextStart": next_start},
        uncertainty=[] if complete else [{"reason": "page_limit"}],
        next=None if next_start is None else str(next_start),
        error=None if ok else "inventory_failed",
    )


def inventory_slot(index, item=None, count=0):
    return {
        "slot": index,
        "empty": item is None or count <= 0,
        "item": item,
        "count": count,
    }


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
        self.assertEqual(result.metrics["chosen_goal"], [-4, 64, -4])
        self.assertGreater(result.metrics["final_distance"], result.metrics["initial_distance"])
        self.assertEqual(len(body.actions), 1)
        self.assertEqual(body.actions[0].name, "navigateTo")
        self.assertFalse(body.actions[0].params["allow_break"])
        self.assertEqual(body.actions[0].params["break_budget"], 0)
        self.assertFalse(body.actions[0].params["allow_place"])
        self.assertEqual(body.actions[0].params["place_budget"], 0)
        self.assertFalse(body.actions[0].params["allow_pillar"])
        self.assertEqual(body.actions[0].params["pillar_budget"], 0)
        self.assertFalse(body.actions[0].params["allow_downward"])
        self.assertEqual(body.actions[0].params["downward_budget"], 0)

    def test_move_away_rejects_a_goal_domain_larger_than_the_server_contract(self):
        runtime = NavigationTransactions(FakeBody([state_at((0, 64, 0))]))

        with self.assertRaisesRegex(ValueError, "max_candidates must be <= 32"):
            runtime.move_away((0.0, 64.0, 0.0), max_candidates=33)

    def test_move_away_wraps_last_navigation_failure(self):
        nav = FakeNavigator([_segment("blocked", None, success=False, reason="no_path")])
        body = FakeBody([state_at((0, 64, 0)), state_at((0, 64, 0)), state_at((0, 64, 0))],
                        terminal_reasons=["no_path"])
        runtime = NavigationTransactions(body, nav)

        result = runtime.move_away(
            (0.0, 64.0, 0.0),
            min_distance=3.0,
            candidate_radii=(4,),
            max_candidates=1,
            config=NavigationRunConfig(recovery_attempts=0),
        )

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
        self.assertEqual(body.actions[0].params["goal_radius"], 0)
        self.assertEqual(
            {
                key: body.actions[0].params[key]
                for key in (
                    "allow_diagonal",
                    "allow_ascend",
                    "allow_descend",
                    "allow_swim",
                    "max_fall_depth",
                    "max_water_drop_depth",
                    "recheck_lookahead",
                )
            },
            {
                "allow_diagonal": True,
                "allow_ascend": True,
                "allow_descend": True,
                "allow_swim": True,
                "max_fall_depth": 3,
                "max_water_drop_depth": 32,
                "recheck_lookahead": 5,
            },
        )

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
        self.assertEqual(action.params["goal_radius"], 2)

    def test_navigate_to_preserves_composite_goal_set_and_server_selection(self):
        class SelectedGoalBody(FakeBody):
            def await_action_terminal(self, action_id: str, timeout_s: float = 15.0, **kwargs) -> Event:
                terminal = super().await_action_terminal(action_id, timeout_s=timeout_s, **kwargs)
                return Event(
                    seq=terminal.seq,
                    tick=terminal.tick,
                    bot=terminal.bot,
                    name=terminal.name,
                    data={**terminal.data, "selected_goal": [9, 64, 0]},
                )

        body = SelectedGoalBody([state_at((0, 64, 0))])
        runtime = NavigationTransactions(body, FakeNavigator([]))
        goal = GoalComposite(
            (
                GoalNear((2, 64, 0), radius=1),
                GoalNear((9, 64, 0), radius=2),
            )
        )

        result = runtime.navigate_to(goal)

        self.assertTrue(result.success)
        self.assertEqual(body.actions[0].params["target"], [2, 64, 0])
        self.assertEqual(
            body.actions[0].params["goals"],
            [[2, 64, 0, 1], [9, 64, 0, 2]],
        )
        self.assertEqual(result.metrics["goal"], [9, 64, 0])
        self.assertEqual(result.metrics["selected_goal"], [9, 64, 0])
        self.assertEqual(result.metrics["goal_count"], 2)
        self.assertTrue(result.metrics["goal_set_preserved"])

    def test_navigate_to_treats_arrived_reason_as_terminal_truth(self):
        class ArrivedReasonBody(FakeBody):
            def await_action_terminal(self, action_id: str, timeout_s: float = 15.0, **kwargs) -> Event:
                terminal = super().await_action_terminal(action_id, timeout_s=timeout_s, **kwargs)
                data = dict(terminal.data)
                data["arrived"] = False
                data["reason"] = "arrived"
                data["nav_reason"] = "arrived"
                data["goal_dist"] = 1.42
                return Event(
                    seq=terminal.seq,
                    tick=terminal.tick,
                    bot=terminal.bot,
                    name=terminal.name,
                    data=data,
                )

        nav = FakeNavigator([_segment("arrived", (5, 64, 0), success=True, reason="arrived")])
        body = ArrivedReasonBody([state_at((0, 64, 0))], terminal_reasons=["arrived"])
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to(GoalNear((5, 64, 0), radius=3))

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "arrived")
        self.assertTrue(result.metrics["segments"][0]["success"])

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

    def test_navigate_to_replans_after_live_world_change(self):
        body = FakeBody(
            [state_at((0, 64, 0)), state_at((1, 64, 0)), state_at((1, 64, 0))],
            terminal_reasons=["world_changed", "arrived"],
        )
        runtime = NavigationTransactions(body, FakeNavigator([]))

        result = runtime.navigate_to((8, 64, 0), config=NavigationRunConfig(max_segments=3))

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "arrived")
        self.assertEqual([action.name for action in body.actions], ["navigateTo", "navigateTo"])
        self.assertEqual(result.metrics["segments"][0]["terminal_reason"], "world_changed")

    def test_navigate_to_sends_configured_capability_snapshot(self):
        body = FakeBody([state_at((0, 64, 0))])
        runtime = NavigationTransactions(body, FakeNavigator([]))
        config = NavigationRunConfig(
            allow_diagonal=False,
            allow_ascend=False,
            allow_descend=False,
            allow_swim=False,
            max_safe_fall_depth=1,
            max_water_drop_depth=12,
            recheck_lookahead=2,
        )

        result = runtime.navigate_to((3, 64, 0), config=config)

        self.assertTrue(result.success)
        action = body.actions[0]
        self.assertFalse(action.params["allow_diagonal"])
        self.assertFalse(action.params["allow_ascend"])
        self.assertFalse(action.params["allow_descend"])
        self.assertFalse(action.params["allow_swim"])
        self.assertEqual(action.params["max_fall_depth"], 1)
        self.assertEqual(action.params["max_water_drop_depth"], 12)
        self.assertEqual(action.params["recheck_lookahead"], 2)
        self.assertTrue(action.params["allow_break"])
        self.assertEqual(action.params["break_budget"], 8)
        self.assertEqual(action.params["break_timeout_ticks"], 300)
        self.assertFalse(action.params["allow_pillar"])
        self.assertEqual(action.params["pillar_budget"], 8)
        self.assertTrue(action.params["allow_downward"])
        self.assertEqual(action.params["downward_budget"], 8)
        self.assertTrue(action.params["allow_open"])
        self.assertEqual(action.params["open_budget"], 8)

    def test_navigate_to_reads_paginated_inventory_before_enabling_bridge(self):
        body = InventoryNavigationBody(
            [state_at((0, 64, 0))],
            [
                inventory_page([inventory_slot(0, "minecraft:apple", 2)], complete=False, next_start=12),
                inventory_page(
                    [
                        inventory_slot(12, "minecraft:cobblestone", 7),
                        inventory_slot(13, "minecraft:diamond_pickaxe", 1),
                        inventory_slot(14, "minecraft:iron_axe", 1),
                        inventory_slot(15, "minecraft:stone_shovel", 1),
                    ],
                    complete=True,
                ),
            ],
        )
        runtime = NavigationTransactions(body, FakeNavigator([]))

        result = runtime.navigate_to((3, 64, 0))

        self.assertTrue(result.success)
        self.assertEqual(
            [params for scope, params in body.perceptions if scope == "inventory"],
            [{"start": 0, "limit": 12}, {"start": 12, "limit": 12}],
        )
        action = body.actions[0]
        self.assertTrue(action.params["allow_place"])
        self.assertEqual(action.params["scaffold_item"], "cobblestone")
        self.assertEqual(action.params["scaffold_count"], 7)
        self.assertEqual(action.params["place_budget"], 8)
        self.assertEqual(action.params["break_pickaxe"], "diamond_pickaxe")
        self.assertEqual(action.params["break_axe"], "iron_axe")
        self.assertEqual(action.params["break_shovel"], "stone_shovel")

    def test_navigate_to_disables_bridge_for_incomplete_inventory_without_cursor(self):
        body = InventoryNavigationBody(
            [state_at((0, 64, 0))],
            [inventory_page([inventory_slot(0, "minecraft:cobblestone", 7)], complete=False)],
        )
        runtime = NavigationTransactions(body, FakeNavigator([]))

        result = runtime.navigate_to((3, 64, 0))

        self.assertTrue(result.success)
        action = body.actions[0]
        self.assertFalse(action.params["allow_place"])
        self.assertIsNone(action.params["scaffold_item"])
        self.assertEqual(action.params["scaffold_count"], 0)

    def test_navigate_to_disables_bridge_without_scaffold_or_budget(self):
        no_scaffold = InventoryNavigationBody(
            [state_at((0, 64, 0))],
            [inventory_page([inventory_slot(0, "minecraft:apple", 2)], complete=True)],
        )
        runtime = NavigationTransactions(no_scaffold, FakeNavigator([]))

        result = runtime.navigate_to((3, 64, 0))

        self.assertTrue(result.success)
        self.assertFalse(no_scaffold.actions[0].params["allow_place"])

        no_budget = InventoryNavigationBody([state_at((0, 64, 0))], [])
        runtime = NavigationTransactions(no_budget, FakeNavigator([]))
        result = runtime.navigate_to(
            (3, 64, 0),
            config=NavigationRunConfig(
                allow_break=False,
                max_place_steps=0,
                allow_pillar=False,
                allow_downward=False,
            ),
        )

        self.assertTrue(result.success)
        self.assertFalse(no_budget.actions[0].params["allow_place"])
        self.assertEqual(no_budget.actions[0].params["place_budget"], 0)
        self.assertEqual(no_budget.perceptions, [])

    def test_navigate_to_can_enable_pillar_while_bridge_is_disabled(self):
        body = InventoryNavigationBody(
            [state_at((0, 64, 0))],
            [inventory_page([inventory_slot(0, "minecraft:cobblestone", 4)], complete=True)],
        )
        runtime = NavigationTransactions(body, FakeNavigator([]))

        result = runtime.navigate_to(
            (0, 65, 0),
            config=NavigationRunConfig(allow_place=False, allow_pillar=True, max_pillar_steps=2),
        )

        self.assertTrue(result.success)
        action = body.actions[0]
        self.assertFalse(action.params["allow_place"])
        self.assertTrue(action.params["allow_pillar"])
        self.assertEqual(action.params["scaffold_item"], "cobblestone")
        self.assertEqual(action.params["scaffold_count"], 4)
        self.assertEqual(action.params["pillar_budget"], 2)

    def test_navigate_to_can_enable_downward_without_other_terrain_mutations(self):
        body = InventoryNavigationBody(
            [state_at((0, 64, 0))],
            [inventory_page([inventory_slot(0, "minecraft:diamond_pickaxe", 1)], complete=True)],
        )
        runtime = NavigationTransactions(body, FakeNavigator([]))

        result = runtime.navigate_to(
            (0, 63, 0),
            config=NavigationRunConfig(
                allow_break=False,
                allow_place=False,
                allow_pillar=False,
                allow_downward=True,
                max_downward_steps=2,
            ),
        )

        self.assertTrue(result.success)
        action = body.actions[0]
        self.assertFalse(action.params["allow_break"])
        self.assertTrue(action.params["allow_downward"])
        self.assertEqual(action.params["downward_budget"], 2)
        self.assertEqual(action.params["break_pickaxe"], "diamond_pickaxe")

    def test_navigate_to_authorizes_verified_bridge_and_records_placement(self):
        bridge_pos = (1, 63, 0)
        body = MutationNavigationBody(
            [state_at((0, 64, 0)), state_at((1, 64, 0))],
            [inventory_page([inventory_slot(0, "minecraft:cobblestone", 3)], complete=True)],
            [
                (
                    "navigateMutationProposed",
                    {
                        "proposal_id": "proposal-1",
                        "kind": "place",
                        "pos": list(bridge_pos),
                        "source": [0, 64, 0],
                        "block_type": "cobblestone",
                        "before_type": "air",
                        "purpose": "bridge",
                    },
                ),
                (
                    "navigateMutationDone",
                    {
                        "proposal_id": "proposal-1",
                        "kind": "place",
                        "pos": list(bridge_pos),
                        "block_type": "cobblestone",
                        "success": True,
                        "reason": "placed",
                        "block_now": "cobblestone",
                    },
                ),
                ("navigateDone", {"reason": "world_changed", "nav_reason": "world_changed"}),
                ("navigateDone", {"reason": "arrived", "nav_reason": "arrived", "arrived": True}),
            ],
        )
        policy = GovernancePolicy(
            natural_regions=[Region("bridge-lane", (-4, 0, -4), (8, 100, 4))]
        )
        runtime = NavigationTransactions.server_side(body, policy)

        result = runtime.navigate_to(
            (4, 64, 0),
            config=NavigationRunConfig(max_segments=3, max_place_steps=2),
        )

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(result.reason, "arrived")
        self.assertEqual(
            [action.name for action in body.actions],
            ["navigateTo", "navigationMutationDecision", "navigateTo"],
        )
        decision = body.actions[1]
        self.assertTrue(decision.params["authorized"])
        self.assertEqual(decision.params["reason"], "allowed_place")
        self.assertEqual(decision.params["pos"], list(bridge_pos))
        self.assertEqual(body.actions[2].params["scaffold_count"], 2)
        self.assertEqual(body.actions[2].params["place_budget"], 1)
        placement = policy.bot_placements[bridge_pos]
        self.assertEqual(placement.block_type, "cobblestone")
        self.assertEqual(placement.purpose, "bridge")
        self.assertEqual(placement.bot, "Bot1")
        mutation_events = result.metrics["segments"][0]["diagnostics"]["mutation_events"]
        self.assertEqual(
            [event["event"] for event in mutation_events],
            ["navigateMutationProposed", "navigateMutationDone"],
        )

    def test_navigate_to_authorizes_governed_headroom_break_and_decrements_budget(self):
        break_pos = (1, 65, 0)
        body = MutationNavigationBody(
            [state_at((0, 64, 0)), state_at((0, 64, 0))],
            [inventory_page([], complete=True)],
            [
                (
                    "navigateMutationProposed",
                    {
                        "proposal_id": "proposal-break-1",
                        "kind": "break",
                        "pos": list(break_pos),
                        "source": [0, 64, 0],
                        "block_type": "stone",
                        "before_type": "stone",
                        "purpose": "headroom",
                    },
                ),
                (
                    "navigateMutationDone",
                    {
                        "proposal_id": "proposal-break-1",
                        "kind": "break",
                        "pos": list(break_pos),
                        "block_type": "stone",
                        "success": True,
                        "reason": "broken",
                        "block_now": "air",
                        "decision_reason": "allowed_natural",
                    },
                ),
                ("navigateDone", {"reason": "world_changed", "nav_reason": "world_changed"}),
                ("navigateDone", {"reason": "arrived", "nav_reason": "arrived", "arrived": True}),
            ],
            blocks={break_pos: ("stone", "SOLID")},
        )
        policy = GovernancePolicy(
            natural_regions=[Region("natural-corridor", (-4, 0, -4), (8, 100, 4))]
        )
        runtime = NavigationTransactions.server_side(body, policy)

        result = runtime.navigate_to(
            (4, 64, 0),
            break_context=BreakContext.TRAVEL,
            config=NavigationRunConfig(max_segments=3, max_break_steps=2),
        )

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(
            [action.name for action in body.actions],
            ["navigateTo", "navigationMutationDecision", "navigateTo"],
        )
        decision = body.actions[1]
        self.assertTrue(decision.params["authorized"])
        self.assertEqual(decision.params["reason"], "allowed_natural")
        self.assertEqual(decision.params["kind"], "break")
        self.assertEqual(body.actions[0].params["break_budget"], 2)
        self.assertEqual(body.actions[2].params["break_budget"], 1)
        self.assertIn(("blockAt", {"x": 1, "y": 65, "z": 0}), body.perceptions)

    def test_navigate_to_authorizes_pillar_and_records_scaffold_ledger(self):
        pillar_pos = (0, 64, 0)
        body = MutationNavigationBody(
            [state_at((0, 64, 0)), state_at((0, 65, 0))],
            [inventory_page([inventory_slot(0, "minecraft:cobblestone", 3)], complete=True)],
            [
                (
                    "navigateMutationProposed",
                    {
                        "proposal_id": "proposal-pillar-1",
                        "kind": "pillar",
                        "pos": list(pillar_pos),
                        "source": list(pillar_pos),
                        "block_type": "cobblestone",
                        "before_type": "air",
                        "purpose": "pillar",
                    },
                ),
                (
                    "navigateMutationDone",
                    {
                        "proposal_id": "proposal-pillar-1",
                        "kind": "pillar",
                        "pos": list(pillar_pos),
                        "block_type": "cobblestone",
                        "success": True,
                        "reason": "pillared",
                        "block_now": "cobblestone",
                        "decision_reason": "allowed_place",
                    },
                ),
                ("navigateDone", {"reason": "world_changed", "nav_reason": "world_changed"}),
                ("navigateDone", {"reason": "arrived", "nav_reason": "arrived", "arrived": True}),
            ],
        )
        policy = GovernancePolicy(
            natural_regions=[Region("pillar-column", (-2, 0, -2), (2, 100, 2))]
        )
        runtime = NavigationTransactions.server_side(body, policy)

        result = runtime.navigate_to(
            (0, 65, 0),
            config=NavigationRunConfig(max_segments=3, max_pillar_steps=2, max_place_steps=2),
        )

        self.assertTrue(result.success, result.to_payload())
        decision = body.actions[1]
        self.assertTrue(decision.params["authorized"])
        self.assertEqual(decision.params["kind"], "pillar")
        self.assertEqual(decision.params["reason"], "allowed_place")
        self.assertEqual(body.actions[0].params["pillar_budget"], 2)
        self.assertEqual(body.actions[2].params["pillar_budget"], 1)
        self.assertEqual(body.actions[2].params["place_budget"], 2)
        self.assertEqual(body.actions[2].params["scaffold_count"], 2)
        placement = policy.bot_placements[pillar_pos]
        self.assertEqual(placement.purpose, "pillar")
        self.assertEqual(placement.block_type, "cobblestone")

    def test_navigate_to_authorizes_downward_and_decrements_its_budget(self):
        floor_pos = (0, 63, 0)
        body = MutationNavigationBody(
            [state_at((0, 64, 0)), state_at((0, 63, 0))],
            [inventory_page([inventory_slot(0, "minecraft:diamond_pickaxe", 1)], complete=True)],
            [
                (
                    "navigateMutationProposed",
                    {
                        "proposal_id": "proposal-downward-1",
                        "kind": "downward",
                        "pos": list(floor_pos),
                        "source": [0, 64, 0],
                        "block_type": "stone",
                        "before_type": "stone",
                        "purpose": "downward",
                        "tool_item": "diamond_pickaxe",
                    },
                ),
                (
                    "navigateMutationDone",
                    {
                        "proposal_id": "proposal-downward-1",
                        "kind": "downward",
                        "pos": list(floor_pos),
                        "block_type": "stone",
                        "success": True,
                        "reason": "descended",
                        "block_now": "air",
                        "decision_reason": "allowed_natural",
                    },
                ),
                ("navigateDone", {"reason": "world_changed", "nav_reason": "world_changed"}),
                ("navigateDone", {"reason": "arrived", "nav_reason": "arrived", "arrived": True}),
            ],
            blocks={floor_pos: ("stone", "SOLID")},
        )
        policy = GovernancePolicy(
            natural_regions=[Region("downward-column", (-2, 0, -2), (2, 100, 2))]
        )
        runtime = NavigationTransactions.server_side(body, policy)

        result = runtime.navigate_to(
            (0, 63, 0),
            break_context=BreakContext.TRAVEL,
            config=NavigationRunConfig(
                max_segments=3,
                allow_break=False,
                allow_place=False,
                allow_pillar=False,
                max_downward_steps=2,
            ),
        )

        self.assertTrue(result.success, result.to_payload())
        decision = body.actions[1]
        self.assertTrue(decision.params["authorized"])
        self.assertEqual(decision.params["kind"], "downward")
        self.assertEqual(decision.params["reason"], "allowed_natural")
        self.assertEqual(body.actions[0].params["downward_budget"], 2)
        self.assertEqual(body.actions[2].params["downward_budget"], 1)
        self.assertEqual(body.actions[2].params["break_budget"], 8)
        self.assertIn(("blockAt", {"x": 0, "y": 63, "z": 0}), body.perceptions)

    def test_navigate_to_authorizes_openable_and_decrements_open_budget(self):
        door_pos = (1, 64, 0)
        body = MutationNavigationBody(
            [state_at((0, 64, 0)), state_at((0, 64, 0))],
            [],
            [
                (
                    "navigateMutationProposed",
                    {
                        "proposal_id": "proposal-open-1",
                        "kind": "open",
                        "pos": list(door_pos),
                        "source": [0, 64, 0],
                        "block_type": "oak_door",
                        "before_type": "oak_door",
                        "purpose": "open",
                    },
                ),
                (
                    "navigateMutationDone",
                    {
                        "proposal_id": "proposal-open-1",
                        "kind": "open",
                        "pos": list(door_pos),
                        "block_type": "oak_door",
                        "success": True,
                        "reason": "opened",
                        "block_now": "oak_door",
                        "decision_reason": "allowed_interaction",
                    },
                ),
                ("navigateDone", {"reason": "world_changed", "nav_reason": "world_changed"}),
                ("navigateDone", {"reason": "arrived", "nav_reason": "arrived", "arrived": True}),
            ],
            blocks={door_pos: ("oak_door", "SOLID", {"open": "false", "half": "lower"})},
        )
        policy = GovernancePolicy(
            natural_regions=[Region("door-corridor", (-2, 0, -2), (8, 100, 2))]
        )
        runtime = NavigationTransactions.server_side(body, policy)

        result = runtime.navigate_to(
            (4, 64, 0),
            config=NavigationRunConfig(
                max_segments=3,
                allow_break=False,
                allow_place=False,
                allow_pillar=False,
                allow_downward=False,
                max_open_steps=2,
            ),
        )

        self.assertTrue(result.success, result.to_payload())
        decision = body.actions[1]
        self.assertTrue(decision.params["authorized"])
        self.assertEqual(decision.params["kind"], "open")
        self.assertEqual(decision.params["reason"], "allowed_interaction")
        self.assertEqual(body.actions[0].params["open_budget"], 2)
        self.assertEqual(body.actions[2].params["open_budget"], 1)
        self.assertIn(("blockAt", {"x": 1, "y": 64, "z": 0}), body.perceptions)

    def test_navigate_to_denies_protected_headroom_break_and_preserves_world_fact(self):
        break_pos = (1, 65, 0)
        body = MutationNavigationBody(
            [state_at((0, 64, 0)), state_at((0, 64, 0))],
            [inventory_page([], complete=True)],
            [
                (
                    "navigateMutationProposed",
                    {
                        "proposal_id": "proposal-break-denied",
                        "kind": "break",
                        "pos": list(break_pos),
                        "source": [0, 64, 0],
                        "block_type": "stone",
                        "before_type": "stone",
                        "purpose": "headroom",
                    },
                ),
                (
                    "navigateMutationDone",
                    {
                        "proposal_id": "proposal-break-denied",
                        "kind": "break",
                        "pos": list(break_pos),
                        "block_type": "stone",
                        "success": False,
                        "reason": "mutation_denied",
                        "block_now": "stone",
                        "decision_reason": "protected_region",
                    },
                ),
                ("navigateDone", {"reason": "mutation_denied", "nav_reason": "mutation_denied"}),
                (
                    "navigateDone",
                    {
                        "reason": "arrived",
                        "nav_reason": "arrived",
                        "arrived": True,
                        "selected_goal": [-4, 64, 0],
                    },
                ),
            ],
            blocks={break_pos: ("stone", "SOLID")},
        )
        policy = GovernancePolicy(
            natural_regions=[Region("natural-corridor", (-8, 0, -4), (8, 100, 4))],
            protected_regions=[Region("player-wall", break_pos, break_pos)],
        )
        runtime = NavigationTransactions.server_side(body, policy)
        goal = GoalComposite((GoalNear((4, 64, 0), radius=0), GoalNear((-4, 64, 0), radius=0)))

        result = runtime.navigate_to(
            goal,
            break_context=BreakContext.TRAVEL,
            config=NavigationRunConfig(max_segments=3, max_break_steps=2),
        )

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(result.metrics["selected_goal"], [-4, 64, 0])
        decision = body.actions[1]
        self.assertFalse(decision.params["authorized"])
        self.assertEqual(decision.params["reason"], "protected_region")
        self.assertEqual(body.blocks[break_pos], ("stone", "SOLID"))
        self.assertEqual(body.actions[2].params["denied_mutations"], [list(break_pos)])
        self.assertEqual(body.actions[2].params["break_budget"], 2)

    def test_navigate_to_reports_governance_denial_domain_instead_of_segment_budget(self):
        first = (1, 64, 0)
        second = (0, 63, 0)
        capability_snapshot = {
            "allow_break": True,
            "allow_place": False,
            "allow_pillar": False,
            "allow_downward": True,
            "scaffold_item": None,
            "scaffold_count": 0,
        }
        body = MutationNavigationBody(
            [state_at((0, 64, 0))],
            [inventory_page([], complete=True)],
            [
                (
                    "navigateMutationProposed",
                    {
                        "proposal_id": "proposal-break",
                        "kind": "break",
                        "pos": list(first),
                        "source": [0, 64, 0],
                        "block_type": "stone",
                        "before_type": "stone",
                        "purpose": "path",
                    },
                ),
                (
                    "navigateMutationDone",
                    {
                        "proposal_id": "proposal-break",
                        "kind": "break",
                        "pos": list(first),
                        "block_type": "stone",
                        "success": False,
                        "reason": "mutation_denied",
                        "decision_reason": "structure_risk_unknown",
                    },
                ),
                (
                    "navigateDone",
                    {
                        "reason": "mutation_denied",
                        "nav_reason": "mutation_denied",
                        "final_pos": [0.5, 64.0, 0.5],
                        "selected_goal": [4, 72, 0],
                        "movement_counts": {"walk": 3, "break": 1},
                        "capability_snapshot": capability_snapshot,
                    },
                ),
                (
                    "navigateMutationProposed",
                    {
                        "proposal_id": "proposal-downward",
                        "kind": "downward",
                        "pos": list(second),
                        "source": [0, 64, 0],
                        "block_type": "stone",
                        "before_type": "stone",
                        "purpose": "downward",
                    },
                ),
                (
                    "navigateMutationDone",
                    {
                        "proposal_id": "proposal-downward",
                        "kind": "downward",
                        "pos": list(second),
                        "block_type": "stone",
                        "success": False,
                        "reason": "mutation_denied",
                        "decision_reason": "structure_risk_unknown",
                    },
                ),
                (
                    "navigateDone",
                    {
                        "reason": "mutation_denied",
                        "nav_reason": "mutation_denied",
                        "final_pos": [0.5, 64.0, 0.5],
                        "selected_goal": [4, 72, 0],
                        "movement_counts": {"downward": 1},
                        "capability_snapshot": capability_snapshot,
                    },
                ),
            ],
            blocks={first: ("stone", "SOLID"), second: ("stone", "SOLID")},
        )
        runtime = NavigationTransactions.server_side(body, GovernancePolicy())

        result = runtime.navigate_to(
            GoalNear((4, 72, 0), radius=2),
            config=NavigationRunConfig(max_segments=2),
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "protected_or_denied")
        self.assertTrue(result.can_retry)
        self.assertEqual(result.metrics["denied_mutation_count"], 2)
        self.assertEqual(result.metrics["governance_blockers"], {"structure_risk_unknown": 2})
        self.assertEqual(
            result.metrics["mutation_blockers"],
            {
                "break:stone:structure_risk_unknown": 1,
                "downward:stone:structure_risk_unknown": 1,
            },
        )
        self.assertEqual(result.metrics["movement_counts"], {"walk": 3, "break": 1, "downward": 1})
        self.assertEqual(result.metrics["final_pos"], [0.5, 64.0, 0.5])
        self.assertEqual(result.metrics["selected_goal"], [4, 72, 0])
        self.assertEqual(result.metrics["capability_snapshot"], capability_snapshot)

    def test_navigate_to_reuses_caller_owned_mutation_blacklist_across_calls(self):
        denied_pos = (1, 64, 0)
        body = MutationNavigationBody(
            [state_at((0, 64, 0))],
            [inventory_page([], complete=True)],
            [
                (
                    "navigateMutationProposed",
                    {
                        "proposal_id": "proposal-denied",
                        "kind": "break",
                        "pos": list(denied_pos),
                        "source": [0, 64, 0],
                        "block_type": "stone",
                        "before_type": "stone",
                        "purpose": "path",
                    },
                ),
                (
                    "navigateMutationDone",
                    {
                        "proposal_id": "proposal-denied",
                        "kind": "break",
                        "pos": list(denied_pos),
                        "block_type": "stone",
                        "success": False,
                        "reason": "mutation_denied",
                        "decision_reason": "structure_risk_unknown",
                    },
                ),
                (
                    "navigateDone",
                    {
                        "reason": "mutation_denied",
                        "nav_reason": "mutation_denied",
                    },
                ),
                (
                    "navigateDone",
                    {
                        "reason": "arrived",
                        "nav_reason": "arrived",
                        "arrived": True,
                        "selected_goal": [4, 64, 0],
                    },
                ),
            ],
            blocks={denied_pos: ("stone", "SOLID")},
        )
        runtime = NavigationTransactions.server_side(body, GovernancePolicy())
        mutation_blacklist = set()

        first = runtime.navigate_to(
            GoalNear((4, 64, 0), radius=0),
            config=NavigationRunConfig(max_segments=1),
            mutation_blacklist=mutation_blacklist,
        )
        second = runtime.navigate_to(
            GoalNear((4, 64, 0), radius=0),
            config=NavigationRunConfig(max_segments=1),
            mutation_blacklist=mutation_blacklist,
        )

        self.assertEqual(first.reason, "protected_or_denied")
        self.assertTrue(second.success, second.to_payload())
        self.assertEqual(mutation_blacklist, {denied_pos})
        navigate_actions = [action for action in body.actions if action.name == "navigateTo"]
        self.assertEqual(navigate_actions[0].params["denied_mutations"], [])
        self.assertEqual(navigate_actions[1].params["denied_mutations"], [list(denied_pos)])

    def test_navigate_to_blacklists_governance_denied_bridge_and_keeps_goal_set(self):
        denied_pos = (1, 63, 0)
        body = MutationNavigationBody(
            [state_at((0, 64, 0)), state_at((0, 64, 0))],
            [inventory_page([inventory_slot(0, "minecraft:cobblestone", 3)], complete=True)],
            [
                (
                    "navigateMutationProposed",
                    {
                        "proposal_id": "proposal-1",
                        "kind": "place",
                        "pos": list(denied_pos),
                        "source": [0, 64, 0],
                        "block_type": "cobblestone",
                        "before_type": "air",
                        "purpose": "bridge",
                    },
                ),
                (
                    "navigateMutationDone",
                    {
                        "proposal_id": "proposal-1",
                        "kind": "place",
                        "pos": list(denied_pos),
                        "block_type": "cobblestone",
                        "success": False,
                        "reason": "mutation_denied",
                        "block_now": "air",
                    },
                ),
                ("navigateDone", {"reason": "mutation_denied", "nav_reason": "mutation_denied"}),
                (
                    "navigateDone",
                    {
                        "reason": "arrived",
                        "nav_reason": "arrived",
                        "arrived": True,
                        "selected_goal": [8, 64, 0],
                    },
                ),
            ],
        )
        policy = GovernancePolicy(
            natural_regions=[Region("work", (-4, 0, -4), (12, 100, 4))],
            protected_regions=[Region("protected", denied_pos, denied_pos)],
        )
        runtime = NavigationTransactions.server_side(body, policy)
        goal = GoalComposite((GoalNear((3, 64, 0), radius=1), GoalNear((8, 64, 0), radius=1)))

        result = runtime.navigate_to(
            goal,
            config=NavigationRunConfig(max_segments=3, max_place_steps=2),
        )

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(result.metrics["selected_goal"], [8, 64, 0])
        decision = body.actions[1]
        self.assertFalse(decision.params["authorized"])
        self.assertEqual(decision.params["reason"], "protected_region")
        self.assertNotIn(denied_pos, policy.bot_placements)
        first, second = body.actions[0], body.actions[2]
        self.assertEqual(first.params["goals"], second.params["goals"])
        self.assertEqual(second.params["denied_mutations"], [list(denied_pos)])
        self.assertEqual(second.params["scaffold_count"], 3)
        self.assertEqual(second.params["place_budget"], 2)

    def test_navigate_to_rejects_invalid_snapshot_bounds(self):
        runtime = NavigationTransactions(FakeBody([state_at((0, 64, 0))]), FakeNavigator([]))

        with self.assertRaises(ValueError):
            runtime.navigate_to((3, 64, 0), config=NavigationRunConfig(max_safe_fall_depth=4))
        with self.assertRaises(ValueError):
            runtime.navigate_to((3, 64, 0), config=NavigationRunConfig(max_water_drop_depth=65))
        with self.assertRaises(ValueError):
            runtime.navigate_to((3, 64, 0), config=NavigationRunConfig(recheck_lookahead=-1))
        with self.assertRaises(ValueError):
            runtime.navigate_to((3, 64, 0), config=NavigationRunConfig(max_break_steps=-1))
        with self.assertRaises(ValueError):
            runtime.navigate_to((3, 64, 0), config=NavigationRunConfig(max_place_steps=-1))
        with self.assertRaises(ValueError):
            runtime.navigate_to((3, 64, 0), config=NavigationRunConfig(max_pillar_steps=-1))
        with self.assertRaises(ValueError):
            runtime.navigate_to((3, 64, 0), config=NavigationRunConfig(max_downward_steps=-1))
        with self.assertRaises(ValueError):
            runtime.navigate_to((3, 64, 0), config=NavigationRunConfig(max_open_steps=-1))

    def test_navigate_to_returns_failure_on_stuck(self):
        nav = FakeNavigator([_segment("arrived", (10, 64, 0), success=True, reason="arrived")])
        body = FakeBody([state_at((0, 64, 0))], terminal_reasons=["stuck"])
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to((10, 64, 0), config=NavigationRunConfig(recovery_attempts=0))

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

        result = runtime.navigate_to((10, 64, 0), config=NavigationRunConfig(recovery_attempts=0))

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
        self.assertEqual(result.reason, "partial_segment_budget_exhausted")
        self.assertEqual(len(body.actions), 3)

    def test_navigate_to_default_budget_allows_long_partial_progress_chain(self):
        nav = FakeNavigator([_segment("arrived", (50, 64, 0), success=True, reason="arrived")])
        body = FakeBody(
            [state_at((idx, 64, 0)) for idx in range(12)],
            terminal_reasons=["partial"] * 10 + ["arrived"],
        )
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to((50, 64, 0))

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "arrived")
        self.assertEqual(len(body.actions), 11)

    def test_navigate_to_respects_partial_segment_budget(self):
        nav = FakeNavigator([_segment("arrived", (50, 64, 0), success=True, reason="arrived")])
        body = FakeBody(
            [state_at((idx, 64, 0)) for idx in range(5)],
            terminal_reasons=["partial"] * 4,
        )
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to((50, 64, 0), config=NavigationRunConfig(max_segments=8, max_partial_segments=3))

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "partial_segment_budget_exhausted")
        self.assertEqual(result.metrics["partial_segments"], 3)

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

    def test_navigate_to_waits_for_reflex_completed_after_body_preempt(self):
        nav = FakeNavigator([])
        body = FakeBody(
            [
                state_at((0, 64, 0)),
                state_at((1, 64, 0)),
                state_at((1, 64, 0)),
                state_at((2, 64, 0)),
            ],
            terminal_reasons=["preempted", "arrived"],
            poll_events=[
                [Event(seq=10, tick=20, bot="Bot1", name="reflexCompleted", data={"kind": "water"})],
            ],
        )
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to((2, 64, 0), config=NavigationRunConfig(max_segments=4))

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "arrived")
        self.assertEqual(len(body.actions), 2)

    def test_navigate_to_waits_for_reflex_completed_after_owner_preempted_event(self):
        nav = FakeNavigator([])

        class OwnerPreemptBody(FakeBody):
            def await_action_terminal(self, action_id: str, timeout_s: float = 15.0, **kwargs) -> Event:
                if len(self.actions) == 1:
                    return Event(
                        seq=10,
                        tick=20,
                        bot="Bot1",
                        name="ownerPreempted",
                        data={"previous_owner": "moveTo", "new_owner": "waterReflex"},
                    )
                return super().await_action_terminal(action_id, timeout_s=timeout_s, **kwargs)

        body = OwnerPreemptBody(
            [
                state_at((0, 64, 0)),
                state_at((1, 64, 0)),
                state_at((1, 64, 0)),
                state_at((2, 64, 0)),
            ],
            terminal_reasons=["arrived"],
            poll_events=[
                [Event(seq=11, tick=21, bot="Bot1", name="reflexCompleted", data={"kind": "water"})],
            ],
        )
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to((2, 64, 0), config=NavigationRunConfig(max_segments=4))

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "arrived")
        self.assertEqual(len(body.actions), 2)

    def test_navigate_to_fails_when_reflex_completed_without_escape(self):
        nav = FakeNavigator([])
        body = FakeBody(
            [
                state_at((0, 64, 0)),
                state_at((1, 63, 0)),
            ],
            terminal_reasons=["preempted", "arrived"],
            poll_events=[
                [
                    Event(
                        seq=10,
                        tick=20,
                        bot="Bot1",
                        name="reflexCompleted",
                        data={
                            "kind": "water",
                            "escaped_hazard": False,
                            "target_is_dry_stand": False,
                            "final_is_dry_stand": False,
                        },
                    )
                ],
            ],
        )
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to((2, 64, 0), config=NavigationRunConfig(max_segments=4))

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "water_egress_failed")
        self.assertEqual(result.metrics["paused"], False)
        self.assertEqual(result.metrics["reflex_handoff"], "reflex_failed")
        self.assertEqual(result.metrics["reflex"]["final_is_dry_stand"], False)
        self.assertEqual(len(body.actions), 1)

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

    def test_navigate_to_segment_diagnostics_include_server_move_execution_fields(self):
        nav = FakeNavigator([])
        body = FakeBody([state_at((0, 64, 0))], terminal_reasons=["stuck"])
        runtime = NavigationTransactions(body, nav)

        result = runtime.navigate_to((3, 64, 0), config=NavigationRunConfig(max_segments=1))

        self.assertFalse(result.success)
        diagnostics = result.metrics["segments"][0]["diagnostics"]
        self.assertEqual(diagnostics["move_ticks"], 37)
        self.assertEqual(diagnostics["move_min_dist"], 4.25)
        self.assertEqual(diagnostics["move_stuck_ticks"], 12)
        self.assertEqual(diagnostics["move_deviation"], 0.5)
        self.assertEqual(diagnostics["move_waypoint_index"], 2)
        self.assertEqual(diagnostics["move_waypoint_count"], 5)
        self.assertEqual(diagnostics["move_current_waypoint"], [2.5, 64.0, 0.5])
        self.assertEqual(diagnostics["movement_counts"]["walk"], 5)

    def test_follow_entity_sends_follow_action_and_returns_arrived(self):
        nav = FakeNavigator([])
        body = FakeBody([state_at((0, 64, 0))], terminal_reasons=["arrived"])
        runtime = NavigationTransactions(body, nav)

        result = runtime.follow_entity(
            "TargetPlayer",
            keep_distance=3.0,
            timeout_s=5.0,
            config=NavigationRunConfig(
                server_grid_radius=48,
                server_max_expand=1800,
                allow_diagonal=False,
                allow_ascend=False,
                allow_descend=True,
                allow_swim=False,
                max_safe_fall_depth=2,
                max_water_drop_depth=10,
            ),
        )

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "arrived")
        self.assertEqual(len(body.actions), 1)
        self.assertEqual(body.actions[0].name, "followEntity")
        self.assertEqual(body.actions[0].params["target_spec"], "TargetPlayer")
        self.assertEqual(body.actions[0].params["keep_radius"], 3.0)
        self.assertEqual(body.actions[0].params["grid_radius"], 48)
        self.assertEqual(body.actions[0].params["max_expand"], 1800)
        self.assertFalse(body.actions[0].params["allow_diagonal"])
        self.assertFalse(body.actions[0].params["allow_ascend"])
        self.assertTrue(body.actions[0].params["allow_descend"])
        self.assertFalse(body.actions[0].params["allow_swim"])
        self.assertEqual(body.actions[0].params["max_fall_depth"], 2)
        self.assertEqual(body.actions[0].params["max_water_drop_depth"], 10)

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
        self.assertEqual(action.params["min_partial_progress"], 5)
        self.assertEqual(action.params["partial_replans"], 7)
        self.assertEqual(action.params["segment_index"], 0)
        self.assertEqual(action.params["goal_radius"], 0)

    def test_navigate_to_passes_configured_min_partial_progress_to_body(self):
        body = FakeBody(
            [state_at((0, 64, 0)), state_at((1, 64, -1))],
            terminal_success=True,
        )
        policy = GovernancePolicy(natural_regions=[Region("work", (-128, 0, -128), (128, 160, 128))])
        runtime = NavigationTransactions.server_side(body, policy)

        result = runtime.navigate_to(
            (64, 70, -64),
            config=NavigationRunConfig(max_segments=8, min_partial_progress=9),
        )

        self.assertEqual(result.reason, "arrived")
        action = next(action for action in body.actions if action.name == "navigateTo")
        self.assertEqual(action.params["min_partial_progress"], 9)

    def test_world_refresh_uses_block_cells_batches_not_per_cell_block_at(self):
        body = FakeBody([state_at((0, 64, 0))], blocks={(0, 63, 0): ("stone", "SOLID")})
        world = GridWorld({})

        refresh_grid_world_around(body, world, (0, 64, 0), h_radius=1, y_below=1, y_above=1)

        scopes = [scope for scope, _params in body.perceptions]
        self.assertIn("blockCells", scopes)
        self.assertNotIn("blockAt", scopes)
        self.assertFalse(world.cell_at((0, 63, 0)).walkable)

    def test_navigate_to_can_disable_recovery_and_return_stuck(self):
        body = FakeBody([state_at((0, 64, 0))], terminal_reasons=["stuck"])
        runtime = NavigationTransactions.server_side(body, GovernancePolicy())

        result = runtime.navigate_to(
            (3, 64, 0),
            config=NavigationRunConfig(max_segments=4, recovery_attempts=0),
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "stuck")
        self.assertEqual([action.name for action in body.actions], ["navigateTo"])

    def test_navigate_to_recovery_uses_goal_domain_then_resumes_original_goal(self):
        class RecoveryBody(FakeBody):
            def __init__(self):
                super().__init__([state_at((0, 64, 0))], terminal_reasons=["stuck", "arrived", "arrived"])
                self.current = state_at((0, 64, 0))

            def get_state(self):
                return self.current

            def await_action_terminal(self, action_id: str, timeout_s: float = 15.0, **kwargs) -> Event:
                terminal = super().await_action_terminal(action_id, timeout_s=timeout_s, **kwargs)
                action = next(action for action in self.actions if action.id == action_id)
                if terminal.data.get("arrived"):
                    self.current = state_at(tuple(action.params["target"]))
                return terminal

        body = RecoveryBody()
        runtime = NavigationTransactions.server_side(body, GovernancePolicy())

        result = runtime.navigate_to(
            (3, 64, 0),
            config=NavigationRunConfig(max_segments=4, recovery_attempts=1),
        )

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(result.reason, "arrived")
        self.assertEqual([action.name for action in body.actions], ["navigateTo", "navigateTo", "navigateTo"])
        self.assertEqual(body.actions[0].params["target"], [3, 64, 0])
        self.assertGreater(len(body.actions[1].params["goals"]), 1)
        self.assertEqual(body.actions[2].params["target"], [3, 64, 0])
        recovery = result.metrics["segments"][0]["diagnostics"]["recovery"]
        self.assertEqual(recovery["reason"], "arrived")
        self.assertEqual(recovery["metrics"]["original_reason"], "stuck")

    def test_navigate_to_returns_typed_recovery_exhaustion(self):
        body = FakeBody([state_at((0, 64, 0))], terminal_reasons=["stuck", "no_path"])
        runtime = NavigationTransactions.server_side(body, GovernancePolicy())

        result = runtime.navigate_to(
            (3, 64, 0),
            config=NavigationRunConfig(max_segments=4, recovery_attempts=1),
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "recovery_exhausted:stuck")
        self.assertEqual([action.name for action in body.actions], ["navigateTo", "navigateTo"])
        self.assertEqual(result.metrics["recovery_attempts"][0]["reason"], "no_path")



if __name__ == "__main__":
    unittest.main()
