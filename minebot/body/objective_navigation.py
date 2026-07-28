"""Navigation adapter for Body providers that own the whole objective."""

from __future__ import annotations

from dataclasses import replace
from math import floor
from time import monotonic, sleep

from minebot.body.navigation import (
    NavigationRunConfig,
    NavigationTransactions,
    SERVER_GOAL_SET_LIMIT,
)
from minebot.contract import Action, Body, BreakContext, Position, Result, ToolResult
from minebot.game.navigation import (
    GoalBlock,
    GoalComposite,
    GoalLike,
    GoalNear,
    GoalXZ,
    NavigationGoal,
    normalize_goal,
)


class ObjectiveNavigationTransactions(NavigationTransactions):
    """Translate neutral navigation goals into one provider-owned objective."""

    def __init__(self, body: Body) -> None:
        super().__init__(body)

    def navigate_to(
        self,
        goal: GoalLike,
        *,
        break_context: BreakContext | str = BreakContext.TRAVEL,
        config: NavigationRunConfig | None = None,
        timeout_s: float | None = None,
        arrival_radius: float | None = None,
        mutation_blacklist: set[Position] | None = None,
    ) -> ToolResult:
        del break_context, mutation_blacklist
        normalized = normalize_goal(goal)
        try:
            payload = _objective_goal_payload(normalized, arrival_radius=arrival_radius)
        except ValueError as error:
            return ToolResult(
                success=False,
                reason="capability_unavailable:navigate_goal",
                can_retry=False,
                metrics={"goal": normalized.payload(), "error": str(error)},
            )

        cfg = config or NavigationRunConfig()
        effective_timeout_s = (
            float(timeout_s)
            if timeout_s is not None
            else float(cfg.segment_timeout_s) * max(1, int(cfg.max_segments))
        )
        if effective_timeout_s <= 0:
            return ToolResult(False, "invalid_navigation_timeout", False)

        survival = self._prepare_survival(cfg)
        if survival is not None:
            return survival

        final_reach_distance = (
            min(0.1, max(0.05, float(arrival_radius)))
            if arrival_radius is not None
            else None
        )
        action_params: dict[str, object] = {
            "goal": payload,
            "timeout_ticks": max(20, min(12_000, int(effective_timeout_s * 20))),
            "survival_recovery": bool(cfg.survival_recovery),
        }
        if final_reach_distance is not None:
            action_params["final_reach_distance"] = final_reach_distance
        reflex_handoffs: list[dict[str, object]] = []
        deadline = monotonic() + effective_timeout_s
        result: Result | None = None
        max_attempts = max(1, min(8, int(cfg.max_segments)))
        for resume_index in range(max_attempts):
            remaining_s = (
                effective_timeout_s
                if resume_index == 0
                else max(0.05, deadline - monotonic())
            )
            action_params["timeout_ticks"] = max(
                20, min(12_000, int(remaining_s * 20))
            )
            action = Action.create("navigate", dict(action_params))
            after_seq = int(getattr(self.body, "last_seq", 0) or 0)
            try:
                result = self.body.execute(action)
            except Exception as error:
                return ToolResult(
                    False,
                    "body_unavailable",
                    True,
                    metrics={"error": type(error).__name__, "goal": normalized.payload()},
                )
            if str(result.error or "") != "preempted":
                break
            reflex = _wait_for_reflex_completion(
                self.body,
                after_seq=after_seq,
                timeout_s=min(6.0, max(1.0, remaining_s)),
            )
            if reflex is None:
                break
            handoff = {"event": reflex.name, "seq": reflex.seq, "data": dict(reflex.data)}
            reflex_handoffs.append(handoff)
            escaped = reflex.name == "reflexCompleted" and (
                reflex.data.get("escaped_hazard") is True
                and reflex.data.get("final_is_dry_stand") is True
            )
            if not escaped:
                return ToolResult(
                    False,
                    "reflex_failed",
                    True,
                    metrics={
                        "navigation_goal": normalized.payload(),
                        "reflex_handoffs": reflex_handoffs,
                    },
                )
            if resume_index >= max_attempts - 1 or monotonic() >= deadline:
                break

        assert result is not None

        success = bool(result.ok and result.accepted and result.complete)
        metrics = {
            **dict(result.data),
            "navigation_goal": normalized.payload(),
            "provider_action": "navigate",
        }
        if reflex_handoffs:
            metrics["reflex_handoffs"] = reflex_handoffs
        selected = _selected_goal_from_terminal(normalized, metrics)
        if selected is not None:
            metrics["selected_goal"] = list(selected)
            if final_reach_distance is not None:
                metrics["provider_centered"] = _terminal_within_center_radius(
                    metrics,
                    selected,
                    final_reach_distance,
                )
        reason = "arrived" if success else str(result.error or "body_rejected")
        return ToolResult(success, reason, not success, metrics=metrics)

    def follow_entity(
        self,
        target_spec: str,
        *,
        keep_distance: float = 3.0,
        timeout_s: float = 30.0,
        config: NavigationRunConfig | None = None,
    ) -> ToolResult:
        if not target_spec:
            raise ValueError("target_spec must be a non-empty name/uuid")
        if timeout_s <= 0:
            raise ValueError("timeout_s must be > 0")
        if keep_distance < 0:
            raise ValueError("keep_distance must be >= 0")

        cfg = config or NavigationRunConfig()
        survival = self._prepare_survival(cfg)
        if survival is not None:
            return survival
        action = Action.create(
            "followEntity",
            {
                "target_spec": target_spec,
                "keep_radius": keep_distance,
                "replan_distance": 2.0,
                "acquire_radius": 32,
                "timeout_ticks": max(20, min(12_000, int(timeout_s * 20))),
            },
        )
        try:
            accepted = self.body.execute(action)
        except Exception as error:
            return ToolResult(
                False,
                "body_unavailable",
                True,
                metrics={"error": type(error).__name__, "target_spec": target_spec},
            )
        if not (accepted.ok and accepted.accepted):
            return ToolResult(
                False,
                str(accepted.error or "body_rejected"),
                True,
                metrics={"target_spec": target_spec},
            )
        terminal = self.body.await_action_terminal(
            action.id,
            timeout_s=timeout_s + 5.0,
            terminal_events={"followDone", "death", "respawned"},
        )
        metrics = {
            **dict(terminal.data),
            "target_spec": target_spec,
            "keep_distance": keep_distance,
            "event": terminal.name,
            "provider_action": "followEntity",
        }
        success = bool(terminal.data.get("success"))
        reason = str(
            terminal.data.get("reason")
            or terminal.data.get("stopped_reason")
            or "unknown"
        )
        return ToolResult(
            success,
            reason,
            not success and reason in {"timeout", "target_lost", "stuck", "no_path"},
            metrics=metrics,
        )

    def _prepare_survival(self, cfg: NavigationRunConfig) -> ToolResult | None:
        get_state = getattr(self.body, "get_state", None)
        if not callable(get_state):
            return None
        try:
            state = get_state()
        except Exception as error:
            return ToolResult(
                False,
                "body_unavailable",
                True,
                metrics={"error": type(error).__name__, "phase": "survival_preflight"},
            )

        if state.body_owner in _SURVIVAL_REFLEX_OWNERS:
            completion = _wait_for_reflex_completion(
                self.body,
                after_seq=int(getattr(self.body, "last_seq", 0) or 0),
                timeout_s=min(5.0, max(0.25, float(cfg.segment_timeout_s) / 3.0)),
            )
            if completion is None:
                return ToolResult(
                    False,
                    "survival_reflex_active",
                    True,
                    metrics={"owner": state.body_owner},
                )
            state = get_state()

        hazard = state.hazard_unresolved
        if hazard is None or cfg.survival_recovery:
            if hazard is None:
                self._last_survival_recovery_signature = None
            return None

        signature = _hazard_signature(hazard)
        if signature == self._last_survival_recovery_signature:
            return ToolResult(
                False,
                "survival_hazard_unresolved",
                False,
                metrics={
                    "hazard_unresolved": dict(hazard),
                    "recovery": "unchanged_hazard_signature",
                },
            )
        self._last_survival_recovery_signature = signature
        raw_target = hazard.get("recovery_target")
        if not isinstance(raw_target, (list, tuple)) or len(raw_target) != 3:
            return ToolResult(
                False,
                "survival_hazard_unresolved",
                False,
                metrics={
                    "hazard_unresolved": dict(hazard),
                    "recovery": "target_unavailable",
                },
            )
        try:
            target = tuple(floor(float(value)) for value in raw_target)
        except (TypeError, ValueError):
            return ToolResult(
                False,
                "survival_hazard_unresolved",
                False,
                metrics={
                    "hazard_unresolved": dict(hazard),
                    "recovery": "target_invalid",
                },
            )

        recovery_cfg = replace(
            cfg,
            survival_recovery=True,
            max_segments=max(1, min(cfg.max_segments, 8)),
            segment_timeout_s=max(1.0, min(cfg.segment_timeout_s, 15.0)),
        )
        recovery = self.navigate_to(
            GoalNear(target, radius=0),
            config=recovery_cfg,
            timeout_s=min(15.0, recovery_cfg.segment_timeout_s),
            arrival_radius=0.25,
        )
        after = get_state()
        if recovery.success and after.hazard_unresolved is None:
            self._last_survival_recovery_signature = None
            return None
        return ToolResult(
            False,
            "survival_hazard_unresolved",
            False,
            metrics={
                "hazard_unresolved": dict(hazard),
                "recovery": recovery.to_payload(),
                "hazard_unresolved_after": (
                    dict(after.hazard_unresolved)
                    if after.hazard_unresolved is not None
                    else None
                ),
            },
        )


def _objective_goal_payload(
    goal: NavigationGoal,
    *,
    arrival_radius: float | None,
) -> dict[str, object]:
    if isinstance(goal, GoalBlock):
        return _near_payload(goal.pos, _exact_cell_range(arrival_radius))
    if isinstance(goal, GoalNear):
        radius = float(goal.radius) if goal.radius > 0 else _exact_cell_range(arrival_radius)
        return _near_payload(goal.pos, radius)
    if isinstance(goal, GoalXZ):
        return {"kind": "xz", "x": int(goal.x), "z": int(goal.z)}
    if isinstance(goal, GoalComposite):
        if goal.mode != "any":
            raise ValueError("Java objective navigation supports only any-composite goals")
        if len(goal.goals) > SERVER_GOAL_SET_LIMIT:
            raise ValueError(
                f"composite goal exceeds the shared {SERVER_GOAL_SET_LIMIT}-member limit"
            )
        return {
            "kind": "composite",
            "goals": [
                _objective_goal_payload(child, arrival_radius=arrival_radius)
                for child in goal.goals
            ],
        }
    raise ValueError(f"unsupported goal kind: {goal.kind.value}")


def _near_payload(pos: Position, radius: float) -> dict[str, object]:
    return {
        "kind": "near",
        "x": int(pos[0]),
        "y": int(pos[1]),
        "z": int(pos[2]),
        "range": max(0.5, float(radius)),
    }


def _exact_cell_range(arrival_radius: float | None) -> float:
    return max(0.5, float(arrival_radius or 0.5))


def _selected_goal_from_terminal(
    goal: NavigationGoal,
    metrics: dict[str, object],
) -> Position | None:
    final = _terminal_block_position(metrics)
    if final is None:
        return None
    metrics["final_pos"] = list(final)
    if isinstance(goal, GoalComposite):
        for child in goal.goals:
            if child.is_satisfied(final):
                return child.representative(final)
        return None
    if goal.is_satisfied(final):
        return goal.representative(final)
    return None


def _terminal_block_position(metrics: dict[str, object]) -> Position | None:
    if not all(key in metrics for key in ("final_x", "final_y", "final_z")):
        return None
    try:
        x = floor(float(metrics["final_x"]))
        raw_y = float(metrics["final_y"])
        nearest_y = round(raw_y)
        y = nearest_y if abs(raw_y - nearest_y) <= 1.0e-4 else floor(raw_y)
        z = floor(float(metrics["final_z"]))
    except (TypeError, ValueError):
        return None
    return (x, y, z)


def _terminal_within_center_radius(
    metrics: dict[str, object],
    stand: Position,
    radius: float,
) -> bool:
    try:
        dx = float(metrics["final_x"]) - (stand[0] + 0.5)
        dz = float(metrics["final_z"]) - (stand[2] + 0.5)
    except (KeyError, TypeError, ValueError):
        return False
    return dx * dx + dz * dz <= (radius + 1.0e-6) ** 2


_SURVIVAL_REFLEX_OWNERS = frozenset({
    "lavaReflex",
    "fireReflex",
    "waterReflex",
})


def _wait_for_reflex_completion(body: Body, *, after_seq: int, timeout_s: float):
    def matching(events):
        for event in events:
            if int(getattr(event, "seq", 0) or 0) <= after_seq:
                continue
            if event.name in {"reflexCompleted", "death", "bodyMissing", "respawned"}:
                return event
        return None

    buffered = matching(getattr(body, "event_log", ()))
    if buffered is not None:
        return buffered
    poll_events = getattr(body, "poll_events", None)
    if not callable(poll_events):
        return None
    deadline = monotonic() + max(0.0, timeout_s)
    while monotonic() < deadline:
        found = matching(poll_events())
        if found is not None:
            return found
        sleep(0.05)
    return None


def _hazard_signature(hazard: dict[str, object]) -> tuple[object, ...]:
    raw_pos = hazard.get("pos")
    raw_target = hazard.get("recovery_target")
    pos = tuple(raw_pos) if isinstance(raw_pos, (list, tuple)) else raw_pos
    target = tuple(raw_target) if isinstance(raw_target, (list, tuple)) else raw_target
    return (hazard.get("kind"), pos, target, hazard.get("tick"))


__all__ = ["ObjectiveNavigationTransactions"]
