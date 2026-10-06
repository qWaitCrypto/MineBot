from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from minebot.body.navigation import (
    NavigationRunConfig,
    SERVER_GOAL_SET_LIMIT,
    aquatic_navigation_config,
    governed_mobility_navigation_config,
)
from minebot.body.objective_navigation import ObjectiveNavigationTransactions
from minebot.contract import Action, BodyState, BreakContext, Event, PerceptionResult, Result
from minebot.game.navigation import GoalComposite, GoalNear, GoalYLevel


@dataclass
class ObjectiveBody:
    final_pos: tuple[float, float, float] = (19.55, 70.0, -17.45)
    failure: str | None = None
    bot_name: str = "Bot"
    actions: list[Action] = field(default_factory=list)

    def execute(self, action: Action) -> Result:
        self.actions.append(action)
        success = self.failure is None
        return Result(
            id=action.id,
            bot=self.bot_name,
            type="result",
            ok=success,
            accepted=True,
            complete=True,
            data={
                "final_x": self.final_pos[0],
                "final_y": self.final_pos[1],
                "final_z": self.final_pos[2],
            },
            error=self.failure,
        )


@dataclass
class SegmentingObjectiveBody:
    outcomes: list[str | None]
    bot_name: str = "Bot"
    actions: list[Action] = field(default_factory=list)

    def execute(self, action: Action) -> Result:
        self.actions.append(action)
        outcome = self.outcomes.pop(0)
        return Result(
            id=action.id,
            bot=self.bot_name,
            type="result",
            ok=outcome is None,
            accepted=True,
            complete=True,
            data={"final_x": 4.5, "final_y": 64.0, "final_z": 0.5},
            error=outcome,
        )


def _state(*, owner=None, hazard=None, pos=(0.5, 64.0, 0.5)) -> BodyState:
    return BodyState(
        bot="Bot",
        pos=pos,
        yaw=0.0,
        pitch=0.0,
        health=20.0,
        food=20,
        oxygen=300,
        inventory_raw="",
        inventory_hash="empty",
        effects=[],
        time=0,
        weather="clear",
        dimension="minecraft:overworld",
        complete=True,
        body_owner=owner,
        pending_action_count=1 if owner else 0,
        hazard_unresolved=hazard,
    )


class PreemptingObjectiveBody:
    bot_name = "Bot"
    last_seq = 0

    def __init__(self) -> None:
        self.actions: list[Action] = []
        self.event_log: list[Event] = []

    def get_state(self) -> BodyState:
        return _state()

    def poll_events(self) -> list[Event]:
        return list(self.event_log)

    def execute(self, action: Action) -> Result:
        self.actions.append(action)
        if len(self.actions) == 1:
            self.event_log.append(Event(
                seq=1,
                tick=10,
                bot="Bot",
                name="reflexCompleted",
                data={
                    "kind": "lava",
                    "escaped_hazard": True,
                    "final_is_dry_stand": True,
                },
            ))
            return Result(
                id=action.id,
                bot="Bot",
                type="result",
                ok=False,
                accepted=True,
                complete=True,
                error="preempted",
            )
        return Result(
            id=action.id,
            bot="Bot",
            type="result",
            ok=True,
            accepted=True,
            complete=True,
            data={"final_x": 8.5, "final_y": 64.0, "final_z": 0.5},
        )


class RecoveringObjectiveBody:
    bot_name = "Bot"

    def __init__(self) -> None:
        self.actions: list[Action] = []
        self.hazard = {
            "kind": "lava",
            "pos": [0.5, 64.0, 0.5],
            "tick": 7,
            "recovery_target": [4.5, 64.0, 0.5],
        }

    def get_state(self) -> BodyState:
        return _state(hazard=self.hazard)

    def execute(self, action: Action) -> Result:
        self.actions.append(action)
        recovery = bool(action.params.get("survival_recovery"))
        if recovery:
            self.hazard = None
            final_x = 4.5
        else:
            final_x = 8.5
        return Result(
            id=action.id,
            bot="Bot",
            type="result",
            ok=True,
            accepted=True,
            complete=True,
            data={"final_x": final_x, "final_y": 64.0, "final_z": 0.5},
        )


class MovingObjectiveBody:
    bot_name = "Bot"

    def __init__(self) -> None:
        self.pos = (0.5, 64.0, 0.5)
        self.actions: list[Action] = []

    def get_state(self) -> BodyState:
        return _state(pos=self.pos)

    def execute(self, action: Action) -> Result:
        self.actions.append(action)
        goal = action.params["goal"]
        selected = goal["goals"][0] if goal["kind"] == "composite" else goal
        self.pos = (
            float(selected["x"]) + 0.5,
            float(selected["y"]),
            float(selected["z"]) + 0.5,
        )
        return Result(
            id=action.id,
            bot=self.bot_name,
            type="result",
            ok=True,
            accepted=True,
            complete=True,
            data={
                "final_x": self.pos[0],
                "final_y": self.pos[1],
                "final_z": self.pos[2],
            },
        )


class FollowingObjectiveBody:
    bot_name = "Bot"

    def __init__(self) -> None:
        self.actions: list[Action] = []

    def get_state(self) -> BodyState:
        return _state()

    def perceive(self, scope: str, params: dict[str, object]) -> PerceptionResult:
        assert scope == "nearbyEntities"
        return PerceptionResult(
            bot=self.bot_name,
            scope=scope,
            type="perception",
            ok=True,
            complete=True,
            data={
                "count": 1,
                "entities": [{
                    "id": "22222222-2222-4222-8222-222222222222",
                    "type": "minecraft:player",
                    "name": "Guide",
                    "pos": [3.0, 64.0, 0.0],
                    "health": 20.0,
                    "dist2": 9.0,
                }],
            },
            uncertainty=[],
        )

    def execute(self, action: Action) -> Result:
        self.actions.append(action)
        return Result(
            id=action.id,
            bot=self.bot_name,
            type="result",
            ok=True,
            accepted=True,
            complete=False,
        )

    def await_action_terminal(self, action_id: str, **_kwargs) -> Event:
        return Event(
            seq=1,
            tick=20,
            bot=self.bot_name,
            name="followDone",
            data={
                "action_id": action_id,
                "success": True,
                "reason": "arrived",
                "target_id": "22222222-2222-4222-8222-222222222222",
                "final_distance": 2.5,
            },
        )


def test_complete_stand_domain_is_sent_in_one_provider_owned_objective() -> None:
    body = ObjectiveBody()
    navigator = ObjectiveNavigationTransactions(body)
    stands = tuple((index, 70, 1 - index) for index in range(SERVER_GOAL_SET_LIMIT))

    result = navigator.navigate_to(
        GoalComposite(tuple(GoalNear(stand, radius=0) for stand in stands)),
        timeout_s=15.0,
        arrival_radius=0.25,
    )

    assert result.success is True
    assert result.reason == "arrived"
    assert result.metrics["selected_goal"] == [19, 70, -18]
    assert result.metrics["final_pos"] == [19, 70, -18]
    assert len(body.actions) == 1
    action = body.actions[0]
    assert action.name == "navigate"
    assert action.params["timeout_ticks"] == 300
    assert action.params["final_reach_distance"] == 0.1
    assert action.params["survival_recovery"] is False
    assert len(action.params["goal"]["goals"]) == SERVER_GOAL_SET_LIMIT
    assert all(goal["range"] == 0.5 for goal in action.params["goal"]["goals"])
    assert result.metrics["provider_centered"] is True


def test_terminal_position_selects_the_arrived_candidate_not_the_first_candidate() -> None:
    body = ObjectiveBody(final_pos=(8.5, 64.0, 4.5))
    navigator = ObjectiveNavigationTransactions(body)
    candidates = ((1, 64, 1), (8, 64, 4), (12, 64, 9))

    result = navigator.navigate_to(
        GoalComposite(tuple(GoalNear(candidate, radius=0) for candidate in candidates))
    )

    assert result.metrics["selected_goal"] == [8, 64, 4]


def test_provider_failure_is_not_retried_or_relabelled() -> None:
    body = ObjectiveBody(failure="no_path")

    result = ObjectiveNavigationTransactions(body).navigate_to((99, 64, 0))

    assert result.success is False
    assert result.reason == "no_path"
    assert result.can_retry is True
    assert len(body.actions) == 1


def test_unsupported_goal_returns_a_typed_capability_gap_without_dispatch() -> None:
    body = ObjectiveBody()

    result = ObjectiveNavigationTransactions(body).navigate_to(GoalYLevel(90))

    assert result.success is False
    assert result.reason == "capability_unavailable:navigate_goal"
    assert result.can_retry is False
    assert body.actions == []


def test_configured_segment_envelope_becomes_one_objective_timeout() -> None:
    body = ObjectiveBody(final_pos=(2.5, 64.0, 0.5))

    ObjectiveNavigationTransactions(body).navigate_to(
        (2, 64, 0),
        config=NavigationRunConfig(max_segments=5, segment_timeout_s=12.0),
    )

    assert body.actions[0].params["timeout_ticks"] == 1_200
    assert body.actions[0].params["grid_radius"] == 64
    assert body.actions[0].params["max_expand"] == 2_500
    assert body.actions[0].params["allow_swim"] is True
    assert body.actions[0].params["allow_ascend"] is True
    assert body.actions[0].params["allow_descend"] is True
    assert body.actions[0].params["max_fall_depth"] == 3
    assert body.actions[0].params["max_water_drop_depth"] == 32


def test_continuation_yields_after_one_completed_provider_segment() -> None:
    body = SegmentingObjectiveBody(["timeout_ticks_exhausted"])
    checks = iter((None, "resource_output_window_exhausted"))

    result = ObjectiveNavigationTransactions(body).navigate_to(
        (8, 64, 0),
        config=NavigationRunConfig(max_segments=4, segment_timeout_s=12.0),
        continuation_check=lambda: next(checks),
    )

    assert result.success is False
    assert result.reason == "resource_output_window_exhausted"
    assert result.can_retry is True
    assert len(body.actions) == 1
    assert body.actions[0].params["timeout_ticks"] == 240
    assert result.metrics["continuation_yield"] == {
        "reason": "resource_output_window_exhausted",
        "completed_segments": 1,
    }


def test_continuation_reissues_a_pure_objective_after_timeout_segment() -> None:
    body = SegmentingObjectiveBody(["timeout_ticks_exhausted", None])
    checks = iter((None, None))

    result = ObjectiveNavigationTransactions(body).navigate_to(
        (8, 64, 0),
        config=NavigationRunConfig(max_segments=2, segment_timeout_s=12.0),
        continuation_check=lambda: next(checks),
    )

    assert result.success is True
    assert result.reason == "arrived"
    assert len(body.actions) == 2
    assert [action.params["timeout_ticks"] for action in body.actions] == [240, 240]


def test_dry_first_profile_is_preserved_at_the_java_objective_seam() -> None:
    body = ObjectiveBody()

    ObjectiveNavigationTransactions(body).navigate_to(
        (2, 64, 0),
        config=NavigationRunConfig(
            server_grid_radius=24,
            server_max_expand=600,
            allow_diagonal=False,
            allow_ascend=False,
            allow_descend=True,
            allow_swim=False,
            max_safe_fall_depth=1,
            max_water_drop_depth=4,
        ),
    )

    params = body.actions[0].params
    assert params["grid_radius"] == 24
    assert params["max_expand"] == 600
    assert params["allow_diagonal"] is False
    assert params["allow_ascend"] is False
    assert params["allow_descend"] is True
    assert params["allow_swim"] is False
    assert params["max_fall_depth"] == 1
    assert params["max_water_drop_depth"] == 4
    assert "allow_break" not in params


@pytest.mark.parametrize("stop", [None, "resource_output_window_exhausted"])
def test_governed_timeout_preserves_authority_across_slices(stop) -> None:
    class ProgressBody(SegmentingObjectiveBody):
        def execute(self, action):
            result = super().execute(action)
            result.data.update(mutations_completed=1, budget_remaining=action.params["break_budget"] - 1)
            return result

    body = ProgressBody(["timeout_ticks_exhausted", None])
    checks = iter((None, stop))
    result = ObjectiveNavigationTransactions(body).navigate_to(
        (8, 64, 0),
        config=governed_mobility_navigation_config(
            NavigationRunConfig(max_break_steps=3, max_segments=2, segment_timeout_s=12)
        ),
        mutation_blacklist={(1, 64, 0)},
        continuation_check=lambda: next(checks),
    )
    assert result.reason == (stop or "arrived")
    assert [a.params["break_budget"] for a in body.actions] == ([3] if stop else [3, 2])
    assert result.metrics["mutations_completed"] == (1 if stop else 2)
    assert result.metrics["budget_remaining"] == (2 if stop else 1)
    assert all(a.params["denied_mutations"] == [[1, 64, 0]] for a in body.actions)
    assert all(a.params["goal"] == body.actions[0].params["goal"] for a in body.actions)


@pytest.mark.parametrize("facts", [
    {}, {"mutations_completed": 0, "budget_remaining": 3},
    {"mutations_completed": 1, "budget_remaining": 3},
    {"mutations_completed": 3, "budget_remaining": 0},
])
def test_governed_timeout_without_remaining_proven_authority_stops(facts) -> None:
    class ProgressBody(SegmentingObjectiveBody):
        def execute(self, action):
            result = super().execute(action)
            result.data.update(facts)
            return result

    body = ProgressBody(["timeout_ticks_exhausted"])
    result = ObjectiveNavigationTransactions(body).navigate_to(
        (8, 64, 0),
        config=governed_mobility_navigation_config(NavigationRunConfig(max_break_steps=3)),
        continuation_check=lambda: None,
    )
    assert result.reason == "timeout_ticks_exhausted"
    assert len(body.actions) == 1


@pytest.mark.parametrize("reason", ["timeout_ticks_exhausted", "governance_denied", "body_missing"])
@pytest.mark.parametrize("max_segments", [2, 4])
def test_governed_slices_never_refill_or_retry_a_non_timeout(reason, max_segments) -> None:
    class ProgressBody(SegmentingObjectiveBody):
        def execute(self, action):
            result = super().execute(action)
            result.data.update(mutations_completed=1, budget_remaining=action.params["break_budget"] - 1)
            return result

    body = ProgressBody([reason] * 3)
    result = ObjectiveNavigationTransactions(body).navigate_to(
        (8, 64, 0),
        config=governed_mobility_navigation_config(NavigationRunConfig(
            max_break_steps=3, max_segments=max_segments,
        )),
        continuation_check=lambda: None,
    )
    assert result.reason == reason
    assert [a.params["break_budget"] for a in body.actions] == (
        [3, 2, 1][:max_segments] if reason == "timeout_ticks_exhausted" else [3]
    )


def test_governed_mobility_explicitly_carries_bounded_break_authority() -> None:
    body = ObjectiveBody()

    ObjectiveNavigationTransactions(body).navigate_to(
        (2, 64, 0),
        break_context=BreakContext.COLLECT_APPROACH,
        config=governed_mobility_navigation_config(
            NavigationRunConfig(max_break_steps=3)
        ),
        mutation_blacklist={(1, 64, 0)},
    )

    params = body.actions[0].params
    assert params["allow_break"] is True
    assert params["break_budget"] == 3
    assert params["break_context"] == "collect_approach"
    assert params["denied_mutations"] == [[1, 64, 0]]


def test_pure_aquatic_navigation_never_inherits_break_authority() -> None:
    body = ObjectiveBody()

    ObjectiveNavigationTransactions(body).navigate_to(
        (2, 64, 0),
        config=aquatic_navigation_config(NavigationRunConfig(max_break_steps=3)),
    )

    assert "allow_break" not in body.actions[0].params
    assert "break_budget" not in body.actions[0].params


def test_survival_recovery_marker_reaches_the_provider_owned_objective() -> None:
    body = ObjectiveBody(final_pos=(2.5, 64.0, 0.5))

    ObjectiveNavigationTransactions(body).navigate_to(
        (2, 64, 0),
        config=NavigationRunConfig(survival_recovery=True),
    )

    assert body.actions[0].params["survival_recovery"] is True


def test_successful_reflex_invalidates_the_provider_owned_route() -> None:
    body = PreemptingObjectiveBody()

    result = ObjectiveNavigationTransactions(body).navigate_to(
        (8, 64, 0),
        timeout_s=10.0,
    )

    assert result.success is False
    assert result.can_retry is True
    assert result.reason == "survival_reflex_recovered"
    assert len(body.actions) == 1
    assert result.metrics["reflex_handoff"] == "route_invalidated"
    assert result.metrics["reflex_handoffs"][0]["data"]["kind"] == "lava"


def test_unresolved_hazard_uses_only_the_provider_supplied_recovery_target() -> None:
    body = RecoveringObjectiveBody()

    result = ObjectiveNavigationTransactions(body).navigate_to(
        (8, 64, 0),
        timeout_s=10.0,
    )

    assert result.success is True
    assert len(body.actions) == 2
    recovery, original = body.actions
    assert recovery.params["survival_recovery"] is True
    assert recovery.params["goal"] == {
        "kind": "near",
        "x": 4,
        "y": 64,
        "z": 0,
        "range": 0.5,
    }
    assert original.params["survival_recovery"] is False


def test_move_away_uses_the_same_provider_owned_navigation_action() -> None:
    body = MovingObjectiveBody()

    result = ObjectiveNavigationTransactions(body).move_away(
        (0.5, 64.0, 0.5),
        min_distance=4.0,
        candidate_radii=(4, 6),
    )

    assert result.success is True
    assert result.reason == "moved_away"
    assert len(body.actions) == 1
    assert body.actions[0].name == "navigate"
    assert body.actions[0].params["goal"]["kind"] == "composite"


def test_follow_entity_uses_one_provider_owned_moving_target_action() -> None:
    body = FollowingObjectiveBody()

    result = ObjectiveNavigationTransactions(body).follow_entity(
        "Guide",
        keep_distance=3.0,
        timeout_s=10.0,
    )

    assert result.success is True
    assert result.reason == "arrived"
    assert result.metrics["target_id"] == "22222222-2222-4222-8222-222222222222"
    assert len(body.actions) == 1
    action = body.actions[0]
    assert action.name == "followEntity"
    assert action.params["target_uuid"] == "22222222-2222-4222-8222-222222222222"
    assert action.params["timeout_ticks"] == 200
