from __future__ import annotations

from dataclasses import dataclass, field

from minebot.body.navigation import NavigationRunConfig, SERVER_GOAL_SET_LIMIT
from minebot.body.objective_navigation import ObjectiveNavigationTransactions
from minebot.contract import Action, BodyState, Event, Result
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
                "target_id": "target-1",
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


def test_survival_recovery_marker_reaches_the_provider_owned_objective() -> None:
    body = ObjectiveBody(final_pos=(2.5, 64.0, 0.5))

    ObjectiveNavigationTransactions(body).navigate_to(
        (2, 64, 0),
        config=NavigationRunConfig(survival_recovery=True),
    )

    assert body.actions[0].params["survival_recovery"] is True


def test_successful_reflex_resumes_the_same_provider_owned_goal_once() -> None:
    body = PreemptingObjectiveBody()

    result = ObjectiveNavigationTransactions(body).navigate_to(
        (8, 64, 0),
        timeout_s=10.0,
    )

    assert result.success is True
    assert len(body.actions) == 2
    assert body.actions[0].params["goal"] == body.actions[1].params["goal"]
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
    assert result.metrics["target_id"] == "target-1"
    assert len(body.actions) == 1
    action = body.actions[0]
    assert action.name == "followEntity"
    assert action.params["target_spec"] == "Guide"
    assert action.params["timeout_ticks"] == 200
