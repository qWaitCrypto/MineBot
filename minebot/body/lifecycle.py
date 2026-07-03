"""Body transactions for bounded death/respawn recovery."""

from __future__ import annotations

from math import dist
from time import monotonic, sleep

from minebot.contract import Body, Event, Result, ToolResult


Position = tuple[int, int, int]


class LifecycleTransactions:
    """Bounded Body-side lifecycle recovery helpers.

    This is intentionally narrow: it only re-acquires a missing body at a
    requested position and verifies the authoritative recovery facts. Broader
    watchdog/autoresume policy stays above this transaction.
    """

    def __init__(self, body: Body):
        self.body = body

    def recover_after_death(
        self,
        *,
        respawn_pos: Position | None,
        yaw: float | None = None,
        pitch: float | None = None,
        dimension: str | None = None,
        gamemode: str | None = None,
        spawn_timeout_s: float = 10.0,
        respawn_event_timeout_s: float = 6.0,
        arrival_tolerance: float = 1.0,
    ) -> ToolResult:
        state_before, state_before_errors, state_before_exc = _state_recheck_with_retry(self.body)
        if state_before is None:
            if state_before_exc is not None:
                raise state_before_exc
            raise RuntimeError("state recheck failed")
        metrics: dict[str, object] = {
            "respawn_pos": _pos_payload(respawn_pos),
            "state_before_missing": state_before.missing,
            "state_before_pos": list(state_before.pos),
        }
        if state_before_errors:
            metrics["state_before_recheck_errors"] = state_before_errors
        if not state_before.missing:
            return ToolResult(
                success=False,
                reason="body_not_missing",
                can_retry=False,
                metrics=metrics,
            )

        try:
            spawn = self.body.spawn(
                respawn_pos,
                yaw=yaw,
                pitch=pitch,
                dimension=dimension,
                gamemode=gamemode,
                emit_respawned=True,
                timeout_s=spawn_timeout_s,
            )
        except Exception as exc:
            recovered = self._recover_from_transport_recheck(
                exc,
                metrics,
                respawn_pos=respawn_pos,
                arrival_tolerance=arrival_tolerance,
                phase="spawn",
            )
            if recovered is not None:
                return recovered
            raise
        metrics["spawn"] = _result_metrics(spawn)
        if not (spawn.ok and spawn.accepted):
            return ToolResult(
                success=False,
                reason=f"respawn_failed:{spawn.error or 'spawn_failed'}",
                can_retry=True,
                metrics=metrics,
            )

        try:
            respawned = _wait_for_event(self.body, "respawned", timeout_s=respawn_event_timeout_s)
        except Exception as exc:
            recovered = self._recover_from_transport_recheck(
                exc,
                metrics,
                respawn_pos=respawn_pos,
                arrival_tolerance=arrival_tolerance,
                phase="wait_respawn_event",
            )
            if recovered is not None:
                return recovered
            raise
        if respawned is None:
            final_state = self.body.get_state()
            metrics["state_after"] = _state_metrics(final_state)
            if not final_state.missing:
                metrics["respawn_event"] = "missing_but_state_recovered"
                if respawn_pos is None:
                    return ToolResult(success=True, reason="completed", can_retry=False, metrics=metrics)
                state_distance = _state_distance(final_state, respawn_pos)
                metrics["state_distance"] = state_distance
                metrics["arrival_tolerance"] = arrival_tolerance
                if state_distance <= arrival_tolerance:
                    return ToolResult(success=True, reason="completed", can_retry=False, metrics=metrics)
                return ToolResult(
                    success=False,
                    reason="respawn_position_mismatch",
                    can_retry=True,
                    metrics=metrics,
                )
            return ToolResult(
                success=False,
                reason="respawn_event_missing",
                can_retry=final_state.missing,
                metrics=metrics,
            )

        final_state, final_state_errors, final_state_exc = _state_recheck_with_retry(self.body)
        if final_state is None:
            if final_state_exc is not None:
                raise final_state_exc
            raise RuntimeError("state recheck failed")
        if final_state_errors:
            metrics["state_after_recheck_errors"] = final_state_errors
        metrics["respawned_event"] = dict(respawned.data)
        metrics["state_after"] = _state_metrics(final_state)
        final_pos = tuple(respawned.data.get("final_pos") or ())
        if final_state.missing:
            return ToolResult(
                success=False,
                reason="respawn_missing_after_reacquire",
                can_retry=True,
                metrics=metrics,
            )
        if len(final_pos) != 3:
            if respawn_pos is None:
                return ToolResult(success=True, reason="completed", can_retry=False, metrics=metrics)
            return ToolResult(False, "respawn_event_invalid", True, metrics=metrics)
        if respawn_pos is None:
            return ToolResult(success=True, reason="completed", can_retry=False, metrics=metrics)
        target = (float(respawn_pos[0]), float(respawn_pos[1]), float(respawn_pos[2]))
        event_distance = dist((float(final_pos[0]), float(final_pos[1]), float(final_pos[2])), target)
        state_distance = _state_distance(final_state, respawn_pos)
        metrics["event_distance"] = event_distance
        metrics["state_distance"] = state_distance
        metrics["arrival_tolerance"] = arrival_tolerance
        validated_by: list[str] = []
        if event_distance <= arrival_tolerance:
            validated_by.append("respawned_event")
        if state_distance <= arrival_tolerance:
            validated_by.append("state_after")
        metrics["validated_by"] = validated_by
        if not validated_by:
            return ToolResult(
                success=False,
                reason="respawn_position_mismatch",
                can_retry=True,
                metrics=metrics,
            )
        return ToolResult(success=True, reason="completed", can_retry=False, metrics=metrics)

    def _recover_from_transport_recheck(
        self,
        exc: Exception,
        metrics: dict[str, object],
        *,
        respawn_pos: Position | None,
        arrival_tolerance: float,
        phase: str,
    ) -> ToolResult | None:
        if not _is_recheckable_transport_error(exc):
            return None
        recheck: dict[str, object] = {
            "phase": phase,
            "error_type": type(exc).__name__,
            "message": str(exc),
        }
        metrics["transport_recheck"] = recheck
        final_state, errors, _state_exc = _state_recheck_with_retry(self.body)
        if errors:
            recheck["state_errors"] = errors
        if final_state is None:
            last_error = errors[-1] if errors else {}
            recheck["state_error_type"] = last_error.get("error_type")
            recheck["state_error"] = last_error.get("message")
            return None
        metrics["state_after"] = _state_metrics(final_state)
        if final_state.missing:
            recheck["result"] = "body_still_missing"
            return None
        if respawn_pos is None:
            recheck["result"] = "state_recovered"
            metrics["respawn_event"] = "transport_error_but_state_recovered"
            return ToolResult(success=True, reason="completed_after_transport_recheck", can_retry=False, metrics=metrics)
        state_distance = _state_distance(final_state, respawn_pos)
        metrics["state_distance"] = state_distance
        metrics["arrival_tolerance"] = arrival_tolerance
        if state_distance > arrival_tolerance:
            recheck["result"] = "position_mismatch"
            return None
        recheck["result"] = "state_recovered"
        metrics["respawn_event"] = "transport_error_but_state_recovered"
        return ToolResult(success=True, reason="completed_after_transport_recheck", can_retry=False, metrics=metrics)


def _wait_for_event(body: Body, name: str, *, timeout_s: float) -> Event | None:
    deadline = monotonic() + timeout_s
    while monotonic() < deadline:
        for event in body.poll_events():
            if event.name == name:
                return event
        sleep(0.05)
    return None


def _is_recheckable_transport_error(exc: Exception) -> bool:
    if isinstance(exc, (OSError, TimeoutError)):
        return True
    error_type = type(exc).__name__
    if error_type in {"BodyProtocolError", "EnvelopeError", "RconError", "TruncatedPayloadError", "IncompletePayloadError"}:
        return True
    return "RCON" in str(exc)


def _state_recheck_with_retry(body: Body, *, attempts: int = 5, delay_s: float = 0.2):
    errors: list[dict[str, object]] = []
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return body.get_state(), errors, None
        except Exception as exc:
            if not _is_recheckable_transport_error(exc):
                raise
            last_exc = exc
            errors.append({"attempt": attempt, "error_type": type(exc).__name__, "message": str(exc)})
            if attempt < attempts:
                sleep(delay_s)
    return None, errors, last_exc


def _state_distance(state, target_pos: Position) -> float:
    target = (float(target_pos[0]), float(target_pos[1]), float(target_pos[2]))
    return dist(state.pos, target)


def _pos_payload(pos: Position | None) -> list[int] | None:
    return None if pos is None else list(pos)


def _result_metrics(result: Result) -> dict[str, object]:
    return {
        "ok": result.ok,
        "accepted": result.accepted,
        "complete": result.complete,
        "error": result.error,
        "data": dict(result.data),
    }


def _state_metrics(state) -> dict[str, object]:
    return {
        "missing": state.missing,
        "pos": list(state.pos),
        "dimension": state.dimension,
        "health": state.health,
        "food": state.food,
    }
