"""Navigation adapter for Body providers that own the whole objective."""

from __future__ import annotations

from dataclasses import replace
from collections.abc import Mapping
from math import ceil, floor
from time import monotonic, sleep
from typing import Callable

from minebot.contract import action_with_timeout, action_wait_timeout_s
from minebot.body.combat import entity_target_terminal_mismatch, resolve_entity_target
from minebot.body.navigation import (
    NavigationRunConfig,
    NavigationTransactions,
    SERVER_GOAL_SET_LIMIT,
)
from minebot.contract import Action, Body, BreakContext, Position, ToolResult
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
        continuation_check: Callable[[], str | None] | None = None,
    ) -> ToolResult:
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

        def continuation_yield(
            reason: str,
            *,
            completed_segments: int,
            previous: ToolResult | None = None,
        ) -> ToolResult:
            prior_metrics = dict(previous.metrics or {}) if previous is not None else {}
            metrics = {
                **prior_metrics,
                "navigation_goal": normalized.payload(),
                "provider_action": "navigate",
                "continuation_yield": {
                    "reason": reason,
                    "completed_segments": completed_segments,
                },
            }
            return ToolResult(False, reason, True, metrics=metrics)

        if continuation_check is None:
            return self._execute_objective(
                normalized,
                payload,
                cfg=cfg,
                break_context=break_context,
                timeout_s=effective_timeout_s,
                arrival_radius=arrival_radius,
                mutation_blacklist=mutation_blacklist,
            )

        # A Java objective owns the fine-grained path.  For callers that must
        # regain the decision lane, issue the same objective in finite time
        # slices and inspect only after each completed provider action. Governed
        # slices share one authority: only a consistent server mutation receipt
        # permits continuation, with the remaining allowance (never a refill).
        remaining_timeout_s = effective_timeout_s
        deadline = monotonic() + effective_timeout_s
        completed_segments = 0
        governed_break = bool(
            cfg.aquatic_traversal and cfg.allow_break and cfg.max_break_steps > 0
        )
        max_segments = max(1, ceil(effective_timeout_s / max(0.05, cfg.segment_timeout_s)))
        previous: ToolResult | None = None
        remaining_breaks = cfg.max_break_steps
        mutations_completed = 0
        slice_results: list[dict[str, object]] = []
        while remaining_timeout_s > 0:
            continuation_reason = continuation_check()
            if continuation_reason is not None:
                return continuation_yield(
                    continuation_reason,
                    completed_segments=completed_segments,
                    previous=previous,
                )
            segment_timeout_s = min(remaining_timeout_s, cfg.segment_timeout_s, deadline - monotonic())
            if segment_timeout_s <= 0 and previous is not None:
                return previous
            previous = self._execute_objective(
                normalized,
                payload,
                cfg=replace(cfg, max_break_steps=remaining_breaks) if governed_break else cfg,
                break_context=break_context,
                timeout_s=segment_timeout_s,
                arrival_radius=arrival_radius,
                mutation_blacklist=mutation_blacklist,
            )
            completed_segments += 1
            remaining_timeout_s -= segment_timeout_s
            progress_receipt = False
            if governed_break:
                metrics = dict(previous.metrics or {})
                used = metrics.get("mutations_completed")
                left = metrics.get("budget_remaining")
                valid_receipt = (
                    type(used) is int and type(left) is int
                    and 0 <= used <= remaining_breaks
                    and left == remaining_breaks - used
                )
                slice_results.append(previous.to_payload())
                if valid_receipt:
                    mutations_completed += used
                    remaining_breaks = left
                    progress_receipt = used > 0 and left > 0
                metrics.update(
                    mutations_completed=mutations_completed,
                    budget_remaining=remaining_breaks,
                    navigation_slices=list(slice_results),
                    mutation_receipt_valid=valid_receipt,
                )
                previous = replace(previous, metrics=metrics)
            if previous.success or previous.reason != "timeout_ticks_exhausted":
                return previous
            if (governed_break and not progress_receipt) or completed_segments >= max_segments:
                return previous

        assert previous is not None
        return previous

    def _execute_objective(
        self,
        normalized: NavigationGoal,
        payload: dict[str, object],
        *,
        cfg: NavigationRunConfig,
        break_context: BreakContext | str,
        timeout_s: float,
        arrival_radius: float | None,
        mutation_blacklist: set[Position] | None,
    ) -> ToolResult:

        final_reach_distance = (
            min(0.1, max(0.05, float(arrival_radius)))
            if arrival_radius is not None
            else None
        )
        action_params: dict[str, object] = {
            "goal": payload,
            "timeout_ticks": max(20, min(12_000, int(timeout_s * 20))),
            "survival_recovery": bool(cfg.survival_recovery),
            # Keep the provider-owned objective, but do not discard the
            # caller's finite navigation profile at the Python/Java seam.
            # Java otherwise falls back to its defaults (including swimming
            # and the full search cap), making dry-first and load-limited
            # callers only nominally different.
            "grid_radius": max(0, min(256, int(cfg.server_grid_radius))),
            "max_expand": max(1, min(50_000, int(cfg.server_max_expand))),
            "allow_diagonal": bool(cfg.allow_diagonal),
            "allow_ascend": bool(cfg.allow_ascend),
            "allow_descend": bool(cfg.allow_descend),
            "allow_swim": bool(cfg.allow_swim),
            "max_fall_depth": max(0, min(32, int(cfg.max_safe_fall_depth))),
            "max_water_drop_depth": max(0, min(64, int(cfg.max_water_drop_depth))),
        }
        # The Java provider keeps ordinary NAVIGATE pure movement.  Terrain
        # mutation is available only on the explicit governed-mobility
        # escalation profile: pure aquatic traversal also sets
        # ``aquatic_traversal`` but clears ``allow_break``.
        governed_break = bool(
            cfg.aquatic_traversal
            and cfg.allow_break
            and cfg.max_break_steps > 0
        )
        if governed_break:
            action_params.update({
                "allow_break": True,
                "break_budget": int(cfg.max_break_steps),
                "break_context": BreakContext(break_context).value,
                "denied_mutations": [
                    list(pos) for pos in sorted(mutation_blacklist or set())
                ],
            })
        if final_reach_distance is not None:
            action_params["final_reach_distance"] = final_reach_distance
        action = Action.create("navigate", dict(action_params))
        action = action_with_timeout(action, timeout_s)
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
        if result.ok and result.accepted and not result.complete:
            await_terminal = getattr(self.body, "await_action_terminal", None)
            if not callable(await_terminal):
                return ToolResult(
                    False,
                    "body_unavailable",
                    True,
                    metrics={
                        "error": "async_body_missing_terminal_wait",
                        "goal": normalized.payload(),
                    },
                )
            try:
                terminal = await_terminal(action.id, timeout_s=action_wait_timeout_s(action))
            except Exception as error:
                return ToolResult(
                    False,
                    "body_unavailable",
                    True,
                    metrics={
                        "error": type(error).__name__,
                        "goal": normalized.payload(),
                    },
                )
            terminal_data = dict(terminal.data)
            terminal_success = bool(terminal_data.get("success"))
            result = replace(
                result,
                ok=terminal_success,
                complete=True,
                data=terminal_data,
                error=None if terminal_success else str(
                    terminal_data.get("reason") or "body_rejected"
                ),
            )
        reflex_handoffs: list[dict[str, object]] = []
        if str(result.error or "") == "preempted":
            reflex = _wait_for_reflex_completion(
                self.body,
                after_seq=after_seq,
                timeout_s=min(6.0, max(1.0, timeout_s)),
            )
            if reflex is not None:
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
                safe_state = self.body.get_state()
                return ToolResult(
                    False,
                    "survival_reflex_recovered",
                    True,
                    metrics={
                        **dict(result.data),
                        "navigation_goal": normalized.payload(),
                        "provider_action": "navigate",
                        "reflex_handoffs": reflex_handoffs,
                        "reflex_handoff": "route_invalidated",
                        "safe_pos": list(safe_state.pos),
                    },
                )

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
        capabilities = getattr(self.body, "capabilities", None)
        direct_follow = isinstance(capabilities, Mapping) and "follow_entity" in capabilities
        resolved = None
        if not direct_follow:
            resolved = resolve_entity_target(self.body, target_spec, radius=32)
            if isinstance(resolved, ToolResult):
                return resolved
        action = Action.create(
            "followEntity",
            {
                "target": target_spec,
                "keep_radius": keep_distance,
                "replan_distance": 2.0,
                "acquire_radius": 32,
                "timeout_ticks": max(20, min(12_000, int(timeout_s * 20))),
            },
        )
        if resolved is not None:
            action.params["target_uuid"] = resolved.target_uuid
        try:
            action = action_with_timeout(action, timeout_s)
            accepted = self.body.execute(action)
        except Exception as error:
            return ToolResult(
                False,
                "body_unavailable",
                True,
                metrics={
                    "error": type(error).__name__,
                    **({} if resolved is None else resolved.evidence()),
                    "requested_target": target_spec,
                },
            )
        if not (accepted.ok and accepted.accepted):
            return ToolResult(
                False,
                str(accepted.error or "body_rejected"),
                True,
                metrics=(
                    {"requested_target": target_spec}
                    if resolved is None
                    else resolved.evidence()
                ),
            )
        terminal = self.body.await_action_terminal(
            action.id,
            timeout_s=action_wait_timeout_s(action),
            terminal_events={"followDone", "death", "respawned"},
        )
        if resolved is not None:
            mismatch = entity_target_terminal_mismatch(resolved, terminal.data)
            if mismatch is not None:
                return mismatch
        metrics = {
            **dict(terminal.data),
            **(
                {"requested_target": target_spec}
                if resolved is None
                else resolved.evidence()
            ),
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
