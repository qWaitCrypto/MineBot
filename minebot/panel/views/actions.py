"""Page 3: the tool waterfall, per-tool statistics and failures."""

from __future__ import annotations

from minebot.panel import theme
from minebot.panel.canvas import Canvas, fit, pad
from minebot.panel.model import Call, percentile
from minebot.panel.views.common import Frame, draw_chrome, seconds, since, spinner

GROUP_LAYER = {"sense": "body", "move": "baritone", "work": "world"}


def visible_calls(frame: Frame) -> list[Call]:
    return [call for call in (frame.state.calls[key] for key in frame.state.order)
            if call.invoke_ts is not None and call.invoke_ts <= frame.now]


def draw_call_rows(canvas: Canvas, frame: Frame, x: int, y: int, width: int, rows: int, *, compact: bool = False,
                   selected: int | None = None) -> None:
    calls = visible_calls(frame)
    if not calls:
        canvas.text(x, y, "暂无工具调用", theme.DIM)
        return
    if selected is None:
        window = calls[-rows:]
    else:
        start = max(0, min(selected - rows // 2, len(calls) - rows))
        window = calls[start:start + rows]
    now = frame.now
    span_start = min(call.model_ts or call.invoke_ts or now for call in window)
    span = max(1.0, now - span_start)
    bar_width = 0 if compact else max(16, width - 78)
    for index, call in enumerate(window):
        row = y + index
        absolute = calls.index(call)
        highlight = selected is not None and absolute == selected
        running = call.result_ts is None or call.result_ts > now
        ok = call.success is not False
        icon, icon_style = (spinner(frame.anim), theme.layer("body", bold=True)) if running else (
            ("✓", theme.OK) if ok else ("✗", theme.FAIL))
        base = theme.on("body") if highlight else None
        if highlight:
            canvas.fill(x, row, width, 1, " ", base)
        used = 0
        if not compact:
            used += canvas.text(x, row, since(frame.state, call.invoke_ts or now) + " ", base or theme.MUTED)
        used += canvas.text(x + used, row, icon + " ", base or icon_style)
        used += canvas.text(x + used, row, pad(call.tool, 18 if not compact else 16) + " ",
                            base or theme.layer(GROUP_LAYER.get(call.group, "body"), bold=running))
        if not compact:
            used += canvas.text(x + used, row, pad(call.args or "{}", 26) + " ", base or theme.MUTED)
            # waterfall: model → invoke (grey), invoke → result (layer), result → delivered (dim)
            def col(ts: float | None) -> int:
                if ts is None:
                    return bar_width
                return int((ts - span_start) / span * (bar_width - 1))
            start_model, start_invoke = col(call.model_ts or call.invoke_ts), col(call.invoke_ts)
            end_result = col(call.result_ts if not running else now)
            end_delivered = col(call.delivered_ts if call.delivered_ts and call.delivered_ts <= now else None)
            for column in range(bar_width):
                cx = x + used + column
                if start_model <= column < start_invoke:
                    canvas.put(cx, row, "─", theme.DIM)
                elif start_invoke <= column <= max(start_invoke, end_result):
                    style = theme.layer(GROUP_LAYER.get(call.group, "body"), bold=True) if ok else theme.FAIL
                    canvas.put(cx, row, "█" if running or column < end_result else "▌", style)
                elif end_result < column <= end_delivered < bar_width:
                    canvas.put(cx, row, "·", theme.DIM)
            used += bar_width + 1
        duration = (now - (call.invoke_ts or now)) if running else call.duration
        used += canvas.text(x + used, row, pad(seconds(duration), 6), base or theme.TEXT)
        detail = "运行中" if running else (call.reason or "—")
        if call.can_retry and not running:
            detail += " · canRetry"
        if call.edits:
            detail += f" · 改世界 {len(call.edits)}"
        if call.group == "move" and call.inventory_delta:
            detail += " · ⚠背包变化"
        canvas.text(x + used, row, detail, base or (theme.TEXT if ok else theme.FAIL), width - used)


def draw(canvas: Canvas, frame: Frame) -> None:
    _, top, width, height = draw_chrome(canvas, frame)
    state = frame.state
    calls = visible_calls(frame)
    selected = frame.selection.get("actions")
    if selected is not None:
        selected = max(0, min(selected, len(calls) - 1)) if calls else None
    stats_width = 44 if width >= 150 else 0
    list_width = width - stats_width - 4 if stats_width else width - 4
    rows = height - 2
    canvas.box(1, top, width - stats_width - 2 if stats_width else width - 2, height, theme.layer("body", dim=True),
               title=[("行动 · 瀑布", theme.layer("body", bold=True)),
                      ("  ─ 模型发出  █ 执行  · 回送模型", theme.MUTED)])
    draw_call_rows(canvas, frame, 3, top + 1, list_width - 2, rows, selected=selected)
    if not stats_width:
        return
    x = width - stats_width - 1
    canvas.box(x, top, stats_width, height // 2, theme.layer("body", dim=True),
               title=[("按工具统计", theme.layer("body", bold=True))])
    canvas.text(x + 2, top + 1, pad("工具", 20) + pad("次", 4) + pad("成功", 6) + pad("p50", 6) + "p95", theme.MUTED)
    ranked = sorted(state.stats.items(), key=lambda item: -item[1].count)
    for index, (tool, stats) in enumerate(ranked[: height // 2 - 3]):
        row = top + 2 + index
        rate = stats.ok / stats.count if stats.count else 0
        canvas.text(x + 2, row, pad(tool, 19), theme.TEXT)
        canvas.text(x + 22, row, pad(str(stats.count), 4), theme.BRIGHT)
        canvas.text(x + 26, row, pad(f"{rate * 100:.0f}%", 6), theme.OK if rate >= 0.9 else theme.WARN)
        canvas.text(x + 32, row, pad(seconds(percentile(stats.durations, 0.5)), 6), theme.MUTED)
        canvas.text(x + 38, row, seconds(percentile(stats.durations, 0.95)), theme.MUTED, stats_width - 39)
    fy = top + height // 2
    fh = height - height // 2
    canvas.box(x, fy, stats_width, fh, theme.tone("fail"), title=[("失败与原因", theme.tone("fail", bold=True))])
    failures = [call for call in calls if call.success is False and call.result_ts and call.result_ts <= frame.now]
    for index, call in enumerate(reversed(failures[-(fh - 2):])):
        row = fy + 1 + index
        used = canvas.text(x + 2, row, since(state, call.result_ts or 0) + " ", theme.MUTED)
        used += canvas.text(x + 2 + used, row, pad(call.tool, 12) + " ", theme.TEXT)
        canvas.text(x + 2 + used, row, fit(call.reason, stats_width - used - 4), theme.FAIL)
    if not failures:
        canvas.text(x + 2, fy + 1, "无", theme.DIM)
