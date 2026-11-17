"""Page 7: raw trace records, filterable; the place to reconcile any panel claim."""

from __future__ import annotations

import json
from typing import Any

from minebot.panel import theme
from minebot.panel.canvas import Canvas, pad
from minebot.panel.views.common import Frame, draw_chrome, since

FILTERS = (
    ("全部", None),
    ("工具", ("responses_tool_started", "responses_tool_output", "tool_decision_context", "tool_invoke", "tool_result",
              "tool_raw_result")),
    ("模型", ("responses_",)),
    ("身体", ("body_", "world_edit", "direct_call_progress")),
    ("反射", ("direct_", "execution_cancelled", "task_outcome", "session_command_body_interrupt")),
    ("人", ("user_message", "assistant_message")),
)
LAYER_OF = (
    ("responses_", "agent"), ("assistant_", "agent"), ("turn_", "agent"), ("tool_", "body"), ("body_", "body"),
    ("world_", "world"), ("direct_", "reflex"), ("execution_", "reflex"), ("user_", "operator"), ("session", "session"),
)


def matches(record: dict[str, Any], index: int) -> bool:
    patterns = FILTERS[index][1]
    if patterns is None:
        return True
    event = str(record.get("event") or "")
    return any(event == pattern or (pattern.endswith("_") and event.startswith(pattern)) for pattern in patterns)


def visible(frame: Frame) -> list[dict[str, Any]]:
    index = frame.selection.get("filter", 0)
    return [record for record in frame.raw if matches(record, index)]


def summary(record: dict[str, Any]) -> str:
    skip = {"seq", "ts", "session_id", "event", "full_result", "model_result", "native_sample", "usage",
            "last_known_body_state", "recent_tool_results", "recent_session_messages", "request", "response"}
    parts = []
    for key, value in record.items():
        if key in skip:
            continue
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        parts.append(f"{key}={text}")
    return " ".join(parts)


def draw(canvas: Canvas, frame: Frame) -> None:
    _, top, width, height = draw_chrome(canvas, frame)
    records = visible(frame)
    filter_index = frame.selection.get("filter", 0)
    title = [("原始事件", theme.layer("session", bold=True)), (f" · {len(records)}", theme.MUTED), ("   / 切换过滤：", theme.DIM)]
    for index, (label, _) in enumerate(FILTERS):
        title.append((f" {label} ", theme.on("session") if index == filter_index else theme.DIM))
    canvas.box(1, top, width - 2, height, theme.layer("session", dim=True), title=title)
    rows = height - 2
    selected = frame.selection.get("events")
    if selected is None or not records:
        window_start = max(0, len(records) - rows)
    else:
        selected = max(0, min(selected, len(records) - 1))
        window_start = max(0, min(selected - rows // 2, len(records) - rows))
    origin = frame.state.first_ts or 0
    for offset, record in enumerate(records[window_start:window_start + rows]):
        row = top + 1 + offset
        index = window_start + offset
        event = str(record.get("event") or "?")
        layer = next((name for prefix, name in LAYER_OF if event.startswith(prefix)), "session")
        highlight = selected is not None and index == selected
        if highlight:
            canvas.fill(2, row, width - 4, 1, " ", theme.on(layer))
        base = theme.on(layer) if highlight else None
        used = canvas.text(3, row, pad(str(record.get("seq", "")), 6), base or theme.DIM)
        ts = float(record.get("ts") or origin)
        used += canvas.text(3 + used, row, since(frame.state, ts) + "  ", base or theme.MUTED)
        used += canvas.text(3 + used, row, pad(event, 28), base or theme.layer(layer))
        canvas.text(3 + used, row, summary(record), base or theme.TEXT, width - used - 6)
