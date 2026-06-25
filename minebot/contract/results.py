"""Shared terminal event conversion helpers."""

from __future__ import annotations

from .messages import Event, ToolResult


def terminal_event_to_tool_result(event: Event) -> ToolResult:
    reason = str(event.data.get("stopped_reason") or event.name)
    if reason == "preempted" or event.name == "ownerPreempted":
        metrics = dict(event.data)
        metrics["paused"] = True
        return ToolResult(success=True, reason="preempted", can_retry=True, metrics=metrics)

    success = bool(
        event.data.get("arrived")
        or event.data.get("success")
        or event.data.get("completed")
        or reason in {"arrived", "completed"}
    )
    return ToolResult(
        success=success,
        reason=reason,
        can_retry=not success,
        metrics=dict(event.data),
    )
