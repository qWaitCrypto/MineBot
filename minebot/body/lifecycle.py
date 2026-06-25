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
        respawn_pos: Position,
        yaw: float | None = None,
        pitch: float | None = None,
        dimension: str | None = None,
        gamemode: str | None = None,
        spawn_timeout_s: float = 10.0,
        respawn_event_timeout_s: float = 6.0,
        arrival_tolerance: float = 1.0,
    ) -> ToolResult:
        state_before = self.body.get_state()
        metrics: dict[str, object] = {
            "respawn_pos": list(respawn_pos),
            "state_before_missing": state_before.missing,
            "state_before_pos": list(state_before.pos),
        }
        if not state_before.missing:
            return ToolResult(
                success=False,
                reason="body_not_missing",
                can_retry=False,
                metrics=metrics,
            )

        spawn = self.body.spawn(
            respawn_pos,
            yaw=yaw,
            pitch=pitch,
            dimension=dimension,
            gamemode=gamemode,
            emit_respawned=True,
            timeout_s=spawn_timeout_s,
        )
        metrics["spawn"] = _result_metrics(spawn)
        if not (spawn.ok and spawn.accepted):
            return ToolResult(
                success=False,
                reason=f"respawn_failed:{spawn.error or 'spawn_failed'}",
                can_retry=True,
                metrics=metrics,
            )

        respawned = _wait_for_event(self.body, "respawned", timeout_s=respawn_event_timeout_s)
        if respawned is None:
            final_state = self.body.get_state()
            metrics["state_after"] = _state_metrics(final_state)
            return ToolResult(
                success=False,
                reason="respawn_event_missing",
                can_retry=final_state.missing,
                metrics=metrics,
            )

        final_state = self.body.get_state()
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
            return ToolResult(
                success=False,
                reason="respawn_event_invalid",
                can_retry=True,
                metrics=metrics,
            )
        target = (float(respawn_pos[0]), float(respawn_pos[1]), float(respawn_pos[2]))
        event_distance = dist((float(final_pos[0]), float(final_pos[1]), float(final_pos[2])), target)
        state_distance = dist(final_state.pos, target)
        metrics["event_distance"] = event_distance
        metrics["state_distance"] = state_distance
        metrics["arrival_tolerance"] = arrival_tolerance
        if event_distance > arrival_tolerance or state_distance > arrival_tolerance:
            return ToolResult(
                success=False,
                reason="respawn_position_mismatch",
                can_retry=True,
                metrics=metrics,
            )
        return ToolResult(success=True, reason="completed", can_retry=False, metrics=metrics)


def _wait_for_event(body: Body, name: str, *, timeout_s: float) -> Event | None:
    deadline = monotonic() + timeout_s
    while monotonic() < deadline:
        for event in body.poll_events():
            if event.name == name:
                return event
        sleep(0.05)
    return None


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
