"""Body transaction combat runtime (engage + find_hostiles).

Mirrors ``NavigationTransactions.follow_entity``: the brain emits one intent
("engage X" or "engage nearest_hostile") and the Body owns the fight server-side
(approach via Scarpet navigateTo + run_move_tick, swing on cooldown when in
range with line-of-sight, disengage on low health, kill-verify). Python is the
transaction glue: dispatch the action, await ``engageDone`` terminal truth,
record progress.
"""

from __future__ import annotations

from minebot.contract import (
    Action,
    Body,
    LocalProgressController,
    ProgressAbort,
    ProgressController,
    ToolResult,
)


def _pos(state) -> tuple[int, int, int]:
    p = state.pos
    return (int(p[0]), int(p[1]), int(p[2]))


def _result(success: bool, reason: str, can_retry: bool, pos, extra: dict[str, object] | None = None) -> ToolResult:
    metrics: dict[str, object] = {"pos": list(pos)}
    if extra:
        metrics.update(extra)
    return ToolResult(success=success, reason=reason, can_retry=can_retry, metrics=metrics)


class CombatTransactions:
    """Executes combat objectives through the Body combat controller."""

    def __init__(self, body: Body, *, progress: ProgressController | None = None) -> None:
        self.body = body
        self.progress = progress or LocalProgressController()

    def engage_entity(
        self,
        target_spec: str,
        *,
        attack_range: float = 2.0,
        cooldown_ticks: int = 10,
        timeout_s: float = 20.0,
        disengage_health: float = 6.0,
    ) -> ToolResult:
        """Engage and fight a target (name/type/uuid or 'nearest_hostile').

        The Body approaches via server-side A* + ``run_move_tick``, swings on
        cooldown when within ``attack_range`` AND line-of-sight is clear,
        disengages when bot health drops to ``disengage_health``, and
        kill-verifies via target health <= 0. Returns on ``engageDone``.
        """
        if not target_spec:
            raise ValueError("target_spec must be a non-empty name/uuid/spec")
        if timeout_s <= 0:
            raise ValueError("timeout_s must be > 0")
        if attack_range < 1.2:
            raise ValueError("attack_range must be >= 1.2")
        if disengage_health < 0:
            raise ValueError("disengage_health must be >= 0")

        generation = self.progress.next_generation()
        if not self.progress.generation_current(generation):
            return _result(False, "preempted", True, _pos(self.body.get_state()), {"generation_current": False, "target_spec": target_spec})
        try:
            self.progress.require_can_continue(f"engage_entity:{target_spec}")
        except ProgressAbort as exc:
            return _result(False, "progress_yielded", True, _pos(self.body.get_state()), {"error": str(exc), "target_spec": target_spec})

        action = Action.create(
            "engageEntity",
            {
                "target_spec": target_spec,
                "attack_range": attack_range,
                "cooldown_ticks": cooldown_ticks,
                "acquire_radius": 32,
                "grid_radius": 32,
                "max_expand": 200,
                "timeout_ticks": max(20, int(timeout_s * 20)),
                "disengage_health": disengage_health,
            },
        )
        result = self.body.execute(action)
        start = _pos(self.body.get_state())
        if not (result.ok and result.accepted):
            return _result(False, "body_rejected", True, start, {"error": result.error, "target_spec": target_spec})

        terminal = self.body.await_action_terminal(
            action.id,
            timeout_s=timeout_s + 5.0,
            terminal_events={"engageDone", "death", "respawned"},
        )
        td = terminal.data
        success = bool(td.get("success", False))
        reason = str(td.get("reason") or "unknown")
        self.progress.note_step(
            ("engage.tick", start, target_spec, reason),
            success=success,
            fingerprint=self.progress.fingerprint(self.body.get_state()),
            neutral=reason in ("timeout", "target_lost", "disengaged_low_health"),
        )
        return _result(
            success,
            reason,
            reason in ("timeout", "target_lost", "disengaged_low_health"),
            start,
            {
                "target_spec": target_spec,
                "attack_range": attack_range,
                "event": terminal.name,
                "attacks": td.get("attacks"),
                "target_health": td.get("target_health"),
            },
        )


def find_hostiles(body: Body, *, radius: int = 16, limit: int = 16) -> ToolResult:
    """Perceive nearby hostile mobs via the ``nearbyHostiles`` scope, nearest-first."""
    perception = body.perceive("nearbyHostiles", {"radius": int(radius), "limit": int(limit)})
    if not perception.ok:
        return ToolResult(
            success=False,
            reason="perception_failed",
            can_retry=False,
            metrics={"error": perception.error, "scope": "nearbyHostiles"},
        )
    entities = perception.data.get("entities") or []
    return ToolResult(
        success=True,
        reason="hostiles_found",
        can_retry=False,
        metrics={"radius": int(radius), "count": perception.data.get("count"), "hostiles": entities},
    )


__all__ = ["CombatTransactions", "find_hostiles"]
