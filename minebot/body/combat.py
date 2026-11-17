"""Resolve one combat target and dispatch melee/ranged work through Body."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from minebot.contract import action_with_timeout, action_wait_timeout_s
from minebot.contract import (
    Action,
    Body,
    LocalProgressController,
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
    health: float | None

    def evidence(self) -> dict[str, object]:
        return {
            "requested_target": self.requested_target,
            "target_uuid": self.target_uuid,
            "target_type": self.entity_type,
            "target_name": self.name,
            "target_pos": None if self.pos is None else list(self.pos),
        }


# A named position matches an entity of the kind this close to it (it may have moved).
# A grazing cow walks out of 6 blocks in the seconds between STATE and the call:
# the hunt for one 8 blocks from where the Agent saw it was "not found" 8 times
# in 10 s (easy run-19, 06:38). End crystals stand 30+ blocks apart, so 16
# still tells one from another.
NEAR_SLACK = 16.0


NEAREST_HOSTILE_RADIUS = 16


def resolve_entity_target(
    body: Body,
    target_spec: str,
    *,
    radius: int = 32,
    limit: int = 64,
    near: tuple[float, float, float] | None = None,
    within: float | None = None,
) -> ResolvedEntityTarget | ToolResult:
    """Resolve a model-facing entity selector to one UUID in Python.

    ``within``: only a match this many blocks from the Body or nearer; when none
    is, the result says so and how far the nearest match is.

    Java receives only the selected UUID and may refresh that entity's live
    position.  It never receives a selector that could authorize substitution.
    """
    requested = str(target_spec).strip()
    if not requested:
        raise ValueError("target_spec must be a non-empty name/uuid/spec")

    if requested == "nearest_hostile":
        # "The nearest hostile" is one at hand, and never a creeper: an Agent
        # rule "fight a zombie within 4 blocks" sent the Body at 2 health after
        # a spider 34 blocks off (run-15 03:33:10, body-tool-boundaries §13).
        radius = min(int(radius), NEAREST_HOSTILE_RADIUS)
        perceived = find_hostiles(body, radius=radius, limit=limit)
        if not perceived.success:
            return perceived
        raw_entities = list((perceived.metrics or {}).get("hostiles") or ())
        scope = "nearbyHostiles"
    else:
        # Matched before the nearest are kept: in the End, endermen, arrows and
        # clouds filled the 64 nearest and the dragon 15 blocks off was "not
        # found" (end run-7, 22:27).
        perception = body.perceive(
            "nearbyEntities",
            {"radius": int(radius), "limit": int(limit), "match": requested},
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
        if requested == "nearest_hostile":
            if str(raw.get("type") or "").removeprefix("minecraft:") == "creeper":
                continue
        else:
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

    if within is not None:
        # run-36 27:00: "the skeleton within 4 blocks of the Body is dead" sent
        # the Body at 6.9 health after the nearest skeleton, 18 blocks off down
        # a dark tunnel; it died on the way (project-review-20261110 §9.16).
        def body_distance(item: dict[str, object]) -> float:
            value = item.get("dist2")
            return float(value) ** 0.5 if isinstance(value, (int, float)) and value >= 0 else float("inf")

        inside = [item for item in matches if body_distance(item) <= within]
        if not inside:
            nearest = min(matches, key=body_distance)
            distance = body_distance(nearest)
            return ToolResult(
                success=False,
                reason="none_within",
                can_retry=True,
                metrics={
                    "requested_target": requested,
                    # Said as a walk that ended short is: "the target is 18
                    # blocks away; the call allowed 4" (same words, same fields).
                    "target_distance": None if distance == float("inf") else round(distance, 1),
                    "max_range": within,
                    "nearest_of_kind_at": nearest.get("pos"),
                    "scope": scope,
                },
            )
        matches = inside

    def from_near(item: dict[str, object]) -> float:
        pos = item.get("pos")
        if not (isinstance(pos, (list, tuple)) and len(pos) == 3):
            return float("inf")
        return sum((float(a) - float(b)) ** 2 for a, b in zip(pos, near)) ** 0.5  # type: ignore[arg-type]

    if near is not None:
        # The Agent names a target by its kind and where it saw it, not by a
        # 36-character id: copied by hand, one was wrong (end baseline run-4).
        selected = min(matches, key=lambda item: (from_near(item), str(item.get("id") or "")))
        if from_near(selected) > NEAR_SLACK:
            seen = selected.get("pos")
            return ToolResult(
                success=False,
                reason="target_not_found",
                can_retry=True,
                metrics={
                    "requested_target": requested,
                    "near": list(near),
                    "nearest_of_kind_at": list(seen) if isinstance(seen, (list, tuple)) else None,
                    "scope": scope,
                    "search_radius": int(radius),
                },
            )
    else:
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
        health=(float(selected["health"]) if selected.get("health") is not None else None),
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
        near: tuple[float, float, float] | None = None,
        weapon: str = "bow",
        max_range: float = 24.0,
        timeout_s: float = 20.0,
    ) -> ToolResult:
        """Shoot one arrow at one resolved entity.

        One shot per action: standing to loose five to eight in a row the Body
        was killed (end baseline run-4); shooting again is the next choice.
        The action ends when the arrow is loosed (``arrow_released``); whether
        it hit arrives afterwards as the Body's ``last_shot`` fact. A target
        beyond ``max_range`` is refused with its distance, not walked to.
        """
        from minebot.body.inventory import _dispatch

        weapon = weapon.removeprefix("minecraft:")
        if weapon not in {"bow", "crossbow"} or not 2 <= max_range <= 64 or not 0 < timeout_s <= 300:
            raise ValueError("provide a bow/crossbow, range 2–64, and timeout 0–300 seconds")
        generation = self.progress.current_generation()
        start = _pos(self.body.get_state())
        if not self.progress.generation_current(generation):
            return _result(False, "preempted", True, start)
        # A full-charge arrow flies far beyond 32 blocks; a pillar crystal ~32.2
        # blocks away was target_not_found twice (end baseline run-1, 14:42).
        resolved = resolve_entity_target(self.body, target_spec, radius=64, near=near)
        if isinstance(resolved, ToolResult):
            return resolved
        result = _dispatch(
            self.body,
            "rangedAttack",
            {"target_uuid": resolved.target_uuid, "weapon": weapon, "expected_shots": 1,
             "max_range": max_range, "timeout_ticks": max(20, int(timeout_s * 20))},
            timeout_s=timeout_s + 5.0,
        )
        if not self.progress.generation_current(generation):
            return _result(False, "preempted", True, start, resolved.evidence())
        metrics = dict(result.metrics or {})
        mismatch = entity_target_terminal_mismatch(resolved, metrics)
        if mismatch is not None:
            return mismatch
        observed = self.body.perceive("nearbyEntities", {"radius": 64, "limit": 128})
        target_gone = not any(
            str(entity.get("id")) == resolved.target_uuid
            for entity in observed.data.get("entities") or ()
            if isinstance(entity, dict)
        ) if observed.ok else None
        metrics["target_gone"] = target_gone
        nonliving_target = resolved.health is None
        success = result.success or (nonliving_target and target_gone is True)
        reason = "completed" if success and not result.success else result.reason
        self.progress.note_step(
            ("ranged.tick", start, resolved.target_uuid, result.reason),
            success=success,
            fingerprint=self.progress.fingerprint(self.body.get_state()),
            neutral=result.reason in {"timeout", "target_lost"},
        )
        if success and reason == "arrow_released":
            return ToolResult(
                success=True, reason=reason, can_retry=False,
                next_suggestion=("The arrow is loosed. Whether it hit is reported afterwards as last_shot "
                                 "in the Body state (read_state)."),
                metrics={"pos": list(start), **resolved.evidence(), **metrics, "weapon": weapon},
            )
        return _result(success, reason, result.can_retry if not success else False, start,
                       {**resolved.evidence(), **metrics, "weapon": weapon})

    def engage_entity(
        self,
        target_spec: str,
        *,
        attack_range: float = 3.0,
        cooldown_ticks: int = 10,
        timeout_s: float = 20.0,
        disengage_health: float = 0.0,
        near: tuple[float, float, float] | None = None,
        within: float | None = None,
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
        if within is not None and within <= 0:
            raise ValueError("within must be > 0")

        generation = self.progress.current_generation()
        if not self.progress.generation_current(generation):
            return _result(False, "preempted", True, _pos(self.body.get_state()), {"generation_current": False, "target_spec": target_spec})

        # As far as the frames see mobs (64): an engage chosen on a mob 40 blocks
        # off ended "target_not_found" three times in a row (run-14 02:13:47).
        resolved = resolve_entity_target(self.body, target_spec, radius=64, near=near, within=within)
        if isinstance(resolved, ToolResult):
            return resolved
        target_uuid = resolved.target_uuid

        action = Action.create(
            "engageEntity",
            {
                "target_uuid": target_uuid,
                "attack_range": attack_range,
                "cooldown_ticks": cooldown_ticks,
                "acquire_radius": 64,
                "grid_radius": 32,
                "max_expand": 200,
                "timeout_ticks": max(20, int(timeout_s * 20)),
                "disengage_health": disengage_health,
            },
        )
        action = action_with_timeout(action, timeout_s)
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
            timeout_s=action_wait_timeout_s(action),
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
                **({"broken_item": td["broken_item"]} if isinstance(td.get("broken_item"), str) else {}),
                **{key: td[key] for key in ("target_distance", "max_range", "sight_blocked_by") if key in td},
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
