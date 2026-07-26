"""Trace continuity between the Java Body and the frozen autonomy evaluator.

The autonomy-quality evaluator (`minebot/app/autonomy_quality.py`) is
trace-only and frozen. Most of its trace vocabulary is emitted by the runner
around any tool call — `tool_invoke`/`tool_result` wrap every RegisteredTool,
and periodic `body_state` sampling is a runner concern — so the Java Body
tools inherit that emission by construction, because they return the same
`ToolResult` contract as the Scarpet tools.

The one Java-specific contribution is turning a verified `collect_block`
inventory delta into an authoritative `body_events` progress sample the
evaluator scores as effective output. This module owns exactly that mapping so
the evaluator, its thresholds, and its coverage rules stay untouched.
"""

from __future__ import annotations

from minebot.contract import JsonObject, ToolResult


def body_progress_event(result: ToolResult, *, ts: float, seq: int) -> JsonObject | None:
    """Map a completed collect ToolResult's inventory delta to a body_events sample.

    Returns ``None`` when the result carries no positive authoritative delta —
    a failure, a denial, or a non-collect result never mints output. The
    emitted shape matches the evaluator's ``itemPickup`` progress ledger
    (`_authoritative_progress_events`): a `body_events` sample whose nested
    event carries `{item, count}` with a positive count.
    """
    if not result.success:
        return None
    metrics = result.metrics or {}
    delta = metrics.get("inventory_delta")
    if not isinstance(delta, dict):
        return None
    item = delta.get("item_id")
    before = _int(delta.get("before"))
    after = _int(delta.get("after"))
    count = after - before
    if not isinstance(item, str) or not item or count <= 0:
        return None
    return {
        "event": "body_events",
        "ts": ts,
        "seq": seq,
        "events": [
            {
                "name": "itemPickup",
                "seq": seq,
                "data": {"item": item, "count": count},
            }
        ],
    }


def _int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
