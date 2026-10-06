"""Resolve one combat target and dispatch melee/ranged work through Body."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from minebot.contract import (
    Action,
    Body,
    LocalProgressController,
    ProgressAbort,
    ProgressController,
    ToolResult,
    body_rejection_to_tool_result,
)


def _pos(state) -> tuple[int, int, int]:
    p = state.pos
    return (int(p[0]), int(p[1]), int(p[2]))


def _result(success: bool, reason: str, can_retry: bool, pos, extra: dict[str, object] | None = None) -> ToolResult:
    metrics: dict[str, object] = {"pos": list(pos)}
    if extra:
        metrics.update(extra)
    return ToolResult(success=success, reason=reason, can_retry=can_retry, metrics=metrics)


@dataclass(frozen=True)
class ResolvedEntityTarget:
    """One Agent-selected physical entity target for a Body objective."""

    requested_target: str
    target_uuid: str
    entity_type: str | None
    name: str | None
    pos: tuple[float, float, float] | None

    def evidence(self) -> dict[str, object]:
        return {
            "requested_target": self.requested_target,
            "target_uuid": self.target_uuid,
            "target_type": self.entity_type,
            "target_name": self.name,
            "target_pos": None if self.pos is None else list(self.pos),
        }


def resolve_entity_target(
    body: Body,
    target_spec: str,
    *,
    radius: int = 32,
    limit: int = 64,
) -> ResolvedEntityTarget | ToolResult:
    """Resolve a model-facing entity selector to one UUID in Python.

    Java receives only the selected UUID and may refresh that entity's live
    position.  It never receives a selector that could authorize substitution.
    """
    requested = str(target_spec).strip()
    if not requested:
        raise ValueError("target_spec must be a non-empty name/uuid/spec")

    if requested == "nearest_hostile":
        perceived = find_hostiles(body, radius=radius, limit=limit)
        if not perceived.success:
            return perceived
        raw_entities = list((perceived.metrics or {}).get("hostiles") or ())
        scope = "nearbyHostiles"
    else:
        perception = body.perceive(
            "nearbyEntities",
            {"radius": int(radius), "limit": int(limit)},
        )
        if not perception.ok:
            return ToolResult(
                success=False,
                reason="entity_target_perception_failed",
                can_retry=True,
                metrics={
                    "requested_target": requested,
                    "scope": "nearbyEntities",
                    "error": perception.error,
                    "uncertainty": list(perception.uncertainty or ()),
                },
            )
        raw_entities = list(perception.data.get("entities") or ())
        scope = "nearbyEntities"

    matches: list[dict[str, object]] = []
    normalized_requested_type = requested.removeprefix("minecraft:").casefold()
    for raw in raw_entities:
        if not isinstance(raw, dict):
            continue
        target_uuid = str(raw.get("id") or "").strip()
        if not target_uuid:
            continue
        if requested != "nearest_hostile":
            entity_type = str(raw.get("type") or "")
            normalized_type = entity_type.removeprefix("minecraft:").casefold()
            name = str(raw.get("name") or "")
            if not (
                target_uuid == requested
                or name == requested
                or normalized_type == normalized_requested_type
            ):
                continue
        matches.append(raw)

    if not matches:
        return ToolResult(
            success=False,
            reason="target_not_found",
            can_retry=True,
            metrics={
                "requested_target": requested,
                "scope": scope,
                "search_radius": int(radius),
                "entities_observed": len(raw_entities),
            },
        )

    selected = min(
        matches,
        key=lambda item: (
            (
                float(item["dist2"])
                if item.get("dist2") is not None
                else float("inf")
            ),
            str(item.get("id") or ""),
        ),
    )
    raw_pos = selected.get("pos")
    pos = None
    if isinstance(raw_pos, (list, tuple)) and len(raw_pos) == 3:
        try:
            pos = (float(raw_pos[0]), float(raw_pos[1]), float(raw_pos[2]))
        except (TypeError, ValueError):
            pos = None
    try:
        target_uuid = str(UUID(str(selected["id"])))
    except (TypeError, ValueError, AttributeError):
        return ToolResult(
            success=False,
            reason="target_identity_invalid",
            can_retry=False,
            metrics={
                "requested_target": requested,
                "scope": scope,
                "observed_target_id": selected.get("id"),
            },
        )
    return ResolvedEntityTarget(
        requested_target=requested,
        target_uuid=target_uuid,
        entity_type=(str(selected["type"]) if selected.get("type") is not None else None),
        name=(str(selected["name"]) if selected.get("name") is not None else None),
        pos=pos,
    )


def entity_target_terminal_mismatch(
    resolved: ResolvedEntityTarget,
    terminal_data: dict[str, object],
) -> ToolResult | None:
    observed = terminal_data.get("target_id")
    if observed is None or str(observed) == resolved.target_uuid:
        return None
    return ToolResult(
        success=False,
        reason="target_identity_mismatch",
        can_retry=False,
        metrics={
            **resolved.evidence(),
            "observed_target_uuid": str(observed),
        },
    )


class CombatTransactions:
    """Executes combat objectives through the Body combat controller."""

    def __init__(self, body: Body, *, progress: ProgressController | None = None) -> None:
        self.body = body
        self.progress = progress or LocalProgressController()

    def ranged_attack(
        self,
        target_spec: str,
        *,
        weapon: str = "bow",
        shots: int = 1,
        max_range: float = 24.0,
        timeout_s: float = 20.0,
    ) -> ToolResult:
        """Fire a bounded bow/crossbow action at one resolved entity UUID."""
        from minebot.body.inventory import _dispatch

        weapon = weapon.removeprefix("minecraft:")
        if weapon not in {"bow", "crossbow"} or not 1 <= shots <= 64 or not 2 <= max_range <= 32 or not 0 < timeout_s <= 300:
            raise ValueError("provide a bow/crossbow, 1–64 shots, range 2–32, and timeout 0–300 seconds")
        generation = self.progress.current_generation()
        start = _pos(self.body.get_state())
        if not self.progress.generation_current(generation):
            return _result(False, "preempted", True, start)
        try:
            self.progress.require_can_continue(f"ranged_attack:{target_spec}")
        except ProgressAbort as exc:
            return _result(False, "progress_yielded", True, start, {"error": str(exc)})
        resolved = resolve_entity_target(self.body, target_spec, radius=32)
        if isinstance(resolved, ToolResult):
            return resolved
        result = _dispatch(
            self.body,
            "rangedAttack",
            {"target_uuid": resolved.target_uuid, "weapon": weapon, "expected_shots": shots,
             "max_range": max_range, "timeout_ticks": max(20, int(timeout_s * 20))},
            timeout_s=timeout_s + 5.0,
        )
        if not self.progress.generation_current(generation):
            return _result(False, "preempted", True, start, resolved.evidence())
        metrics = dict(result.metrics or {})
        mismatch = entity_target_terminal_mismatch(resolved, metrics)
        if mismatch is not None:
            return mismatch
        self.progress.note_step(
            ("ranged.tick", start, resolved.target_uuid, result.reason),
            success=result.success,
            fingerprint=self.progress.fingerprint(self.body.get_state()),
            neutral=result.reason in {"timeout", "target_lost"},
        )
        return _result(result.success, result.reason, result.can_retry, start,
                       {**resolved.evidence(), **metrics, "weapon": weapon, "requested_shots": shots})

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

        The Body approaches, swings on cooldown when within ``attack_range``
        and line-of-sight is clear,
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

        generation = self.progress.current_generation()
        if not self.progress.generation_current(generation):
            return _result(False, "preempted", True, _pos(self.body.get_state()), {"generation_current": False, "target_spec": target_spec})
        try:
            self.progress.require_can_continue(f"engage_entity:{target_spec}")
        except ProgressAbort as exc:
            return _result(False, "progress_yielded", True, _pos(self.body.get_state()), {
                "error": str(exc), "target_spec": target_spec,
                "progress_stop_reason": exc.stop_reason.value,
            })

        resolved = resolve_entity_target(self.body, target_spec, radius=32)
        if isinstance(resolved, ToolResult):
            return resolved
        target_uuid = resolved.target_uuid

        action = Action.create(
            "engageEntity",
            {
                "target_uuid": target_uuid,
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
            rejected = body_rejection_to_tool_result(
                result,
                {"error": result.error, **resolved.evidence()},
            )
            if rejected is not None:
                return ToolResult(
                    success=rejected.success,
                    reason=rejected.reason,
                    can_retry=rejected.can_retry,
                    next_suggestion=rejected.next_suggestion,
                    metrics={"pos": list(start), **dict(rejected.metrics or {})},
                )
            return _result(
                False,
                "body_rejected",
                True,
                start,
                {"error": result.error, **resolved.evidence()},
            )

        terminal = self.body.await_action_terminal(
            action.id,
            timeout_s=timeout_s + 5.0,
            terminal_events={"engageDone", "death", "respawned"},
        )
        if not self.progress.generation_current(generation):
            return _result(False, "preempted", True, start, {"generation_current": False, **resolved.evidence()})

        td = terminal.data
        mismatch = entity_target_terminal_mismatch(resolved, td)
        if mismatch is not None:
            return mismatch
        success = bool(td.get("success", False))
        reason = str(td.get("reason") or "unknown")
        self.progress.note_step(
            ("engage.tick", start, target_uuid, reason),
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
                **resolved.evidence(),
                "attack_range": attack_range,
                "action_id": action.id,
                "event": terminal.name,
                "attacks": td.get("attacks"),
                "target_id": td.get("target_id"),
                "target_health": td.get("target_health"),
                "target_initial_health": td.get("target_initial_health"),
                "damage_observed": td.get("damage_observed"),
                "persistent_target": td.get("persistent_target"),
                "cooldown_ticks": td.get("cooldown_ticks"),
                "min_attack_interval_ticks": td.get("min_attack_interval_ticks"),
                "max_attack_interval_ticks": td.get("max_attack_interval_ticks"),
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


__all__ = [
    "CombatTransactions",
    "ResolvedEntityTarget",
    "entity_target_terminal_mismatch",
    "find_hostiles",
    "resolve_entity_target",
]
