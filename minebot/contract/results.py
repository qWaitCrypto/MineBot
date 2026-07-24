"""Shared terminal event conversion helpers."""

from __future__ import annotations

from .messages import Event, Result, ToolResult


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


def body_rejection_to_tool_result(
    result: Result,
    metrics: dict[str, object] | None = None,
) -> ToolResult | None:
    """Preserve typed server safety rejections at the Body tool boundary.

    Scarpet may reject an ordinary action while a survival hazard remains
    unresolved.  Collapsing that fact into a generic retryable rejection makes
    the Agent repeat the same unsafe request.  All other acceptance failures
    retain the historical ``body_rejected`` semantics.
    """

    if result.ok and result.accepted:
        return None
    merged = dict(metrics or {})
    merged["accepted"] = {
        "ok": result.ok,
        "accepted": result.accepted,
        "error": result.error,
        "data": result.data,
    }
    if result.error == "hazard_unresolved":
        return ToolResult(
            success=False,
            reason="survival_hazard_unresolved",
            can_retry=False,
            next_suggestion="run the Body-owned survival recovery transaction before ordinary actions",
            metrics=merged,
        )
    return ToolResult(success=False, reason="body_rejected", can_retry=True, metrics=merged)
