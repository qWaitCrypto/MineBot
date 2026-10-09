"""Page 2: model requests, tokens, turn profile and what the Agent said."""

from __future__ import annotations

from rich.cells import cell_len

from minebot.panel import theme
from minebot.panel.canvas import Canvas, pad, wrap
from minebot.panel.model import PHASE_LABELS, percentile
from minebot.panel.views.common import Frame, draw_chrome, seconds, since, spinner, tokens, wait_tone

PHASE_STYLE = {
    "prepare": theme.MUTED,
    "connect": theme.layer("body"),
    "send": theme.layer("client"),
    "wait": theme.WARN,
    "stream": theme.layer("agent", bold=True),
    "compact": theme.layer("baritone"),
    "failed": theme.FAIL,
}


def draw(canvas: Canvas, frame: Frame) -> None:
    _, top, width, height = draw_chrome(canvas, frame)
    state, now = frame.state, frame.now
    side = 46 if width >= 140 else 0
    main = width - side - 3
    gantt_height = max(8, height - 9)
    requests = [request for request in state.requests if request.started_ts <= now]
    box_width = width - side - 2 if side else width - 2
    canvas.box(1, top, box_width, gantt_height, theme.layer("agent", dim=True),
               title=[("模型请求 · 阶段甘特", theme.layer("agent", bold=True))])
    legend = [(f" █{PHASE_LABELS[name]}", style) for name, style in PHASE_STYLE.items()]
    legend_width = sum(cell_len(text) for text, _ in legend)
    canvas.runs(box_width - legend_width - 2, top, legend)
    rows = gantt_height - 2
    window = requests[-rows:]
    durations = [((request.ended_ts if request.ended_ts and request.ended_ts <= now else now) - request.started_ts)
                 for request in window]
    scale = max(durations or [1.0])
    bar_width = max(20, main - 46)
    for index, request in enumerate(window):
        row = top + 1 + index
        end = request.ended_ts if request.ended_ts and request.ended_ts <= now else now
        running = request.status == "running" or (request.ended_ts or 0) > now
        used = canvas.text(3, row, f"#{request.index:<4}", theme.MUTED)
        used += canvas.text(3 + used, row, since(state, request.started_ts) + " ", theme.MUTED)
        phases = [(phase, ts) for phase, ts in request.phases if ts <= now]
        for position, (phase, ts) in enumerate(phases):
            next_ts = phases[position + 1][1] if position + 1 < len(phases) else end
            start_col = int((ts - request.started_ts) / scale * bar_width)
            end_col = max(start_col + 1, int((next_ts - request.started_ts) / scale * bar_width))
            for column in range(start_col, min(end_col, bar_width)):
                canvas.put(3 + used + column, row, "█" if phase not in ("done",) else "▏",
                           PHASE_STYLE.get(phase, theme.MUTED))
        used += bar_width + 1
        status = (spinner(frame.anim) + " " + PHASE_LABELS.get(request.phase, request.phase)) if running else (
            "✓" if request.status == "completed" else "✗ " + request.error)
        used += canvas.text(3 + used, row, pad(seconds(end - request.started_ts), 6), theme.TEXT)
        used += canvas.text(3 + used, row, pad(f"ttfb {seconds(request.ttfb_s)}", 11), theme.MUTED)
        used += canvas.text(3 + used, row, pad(f"{tokens(request.input_tokens)}→{tokens(request.output_tokens)}", 12),
                            theme.MUTED)
        canvas.text(3 + used, row, status, theme.OK if request.status == "completed" else theme.FAIL if not running
                    else theme.layer("agent", bold=True), main - used - 2)
    # speech
    sy = top + gantt_height
    sh = height - gantt_height
    canvas.box(1, sy, box_width, sh, theme.layer("agent", dim=True), title=[("Agent 说", theme.layer("agent", bold=True))])
    row = sy + 1
    for ts, content in reversed([entry for entry in state.speech if entry[0] <= now]):
        for index, line in enumerate(wrap(content, main - 14, 2)):
            if row >= sy + sh - 1:
                break
            canvas.text(3, row, since(state, ts) if index == 0 else "", theme.MUTED)
            canvas.text(13, row, line, theme.TEXT if row == sy + 1 else theme.MUTED, main - 12)
            row += 1
        if row >= sy + sh - 1:
            break
    if not side:
        return
    x = width - side - 1
    _current(canvas, frame, x, top, side, 7)
    _usage(canvas, frame, x, top + 7, side, 9)
    _profile(canvas, frame, x, top + 16, side, height - 16)


def _current(canvas: Canvas, frame: Frame, x: int, y: int, w: int, h: int) -> None:
    state, now = frame.state, frame.now
    request = state.request
    canvas.box(x, y, w, h, theme.layer("agent", dim=True), title=[("当前请求", theme.layer("agent", bold=True))])
    if request is None or request.started_ts > now:
        canvas.text(x + 2, y + 1, "—", theme.DIM)
        return
    running = request.status == "running"
    waited = now - request.phase_ts
    canvas.runs(x + 2, y + 1, [(f"#{request.index} ", theme.BRIGHT),
                               ((spinner(frame.anim) + " ") if running else "", theme.layer("agent", bold=True)),
                               (PHASE_LABELS.get(request.phase, request.phase), theme.layer("agent", bold=True)),
                               (f"  本阶段 {seconds(waited)}" if running else "", theme.tone(wait_tone(waited)))], w - 4)
    canvas.runs(x + 2, y + 2, [("总计 ", theme.MUTED), (seconds((request.ended_ts or now) - request.started_ts), theme.TEXT),
                               ("  attempt ", theme.MUTED), (str(request.attempt), theme.TEXT),
                               ("  key ", theme.MUTED), (request.key or "—", theme.DIM)], w - 4)
    canvas.runs(x + 2, y + 3, [("首字节 ", theme.MUTED), (seconds(request.ttfb_s), theme.TEXT),
                               ("  收到 ", theme.MUTED), (f"{request.received_bytes // 1024}KB", theme.TEXT),
                               ("  空闲 ", theme.MUTED), (seconds(request.byte_idle_s), theme.TEXT)], w - 4)
    canvas.runs(x + 2, y + 4, [("模型 ", theme.MUTED), (request.model or state.model or "—", theme.layer("agent"))], w - 4)
    if request.error:
        canvas.text(x + 2, y + 5, request.error, theme.FAIL, w - 4)


def _usage(canvas: Canvas, frame: Frame, x: int, y: int, w: int, h: int) -> None:
    state = frame.state
    canvas.box(x, y, w, h, theme.layer("agent", dim=True), title=[("Token", theme.layer("agent", bold=True))])
    usage = state.usage
    canvas.runs(x + 2, y + 1, [("入 ", theme.MUTED), (tokens(usage["input"]), theme.BRIGHT),
                               ("  缓存 ", theme.MUTED), (tokens(usage["cached"]), theme.TEXT),
                               ("  出 ", theme.MUTED), (tokens(usage["output"]), theme.TEXT),
                               ("  推理 ", theme.MUTED), (tokens(usage["reasoning"]), theme.TEXT)], w - 4)
    ratio = state.cache_ratio or 0.0
    used = canvas.text(x + 2, y + 2, "缓存命中 ", theme.MUTED)
    canvas.bar(x + 2 + used, y + 2, w - 16, ratio, theme.layer("agent"), theme.DIM)
    canvas.text(x + w - 6, y + 2, f"{ratio * 100:.0f}%", theme.TEXT)
    canvas.runs(x + 2, y + 3, [("上下文 ", theme.MUTED), (tokens(state.context_tokens), theme.TEXT),
                               ("  压缩 ", theme.MUTED), (str(state.compactions), theme.TEXT),
                               ("  失败 ", theme.MUTED), (str(state.request_failed), theme.FAIL if state.request_failed else theme.TEXT)], w - 4)
    inputs = [request.input_tokens or None for request in state.requests if request.started_ts <= frame.now]
    canvas.text(x + 2, y + 4, "每次输入", theme.MUTED)
    canvas.sparkline(x + 2, y + 5, w - 4, inputs, theme.layer("agent"))
    ttfbs = [request.ttfb_s for request in state.requests if request.started_ts <= frame.now]
    p50, p95 = percentile([value for value in ttfbs if value is not None], 0.5), percentile(
        [value for value in ttfbs if value is not None], 0.95)
    canvas.runs(x + 2, y + 6, [("首字节 p50 ", theme.MUTED), (seconds(p50), theme.TEXT), ("  p95 ", theme.MUTED),
                               (seconds(p95), theme.TEXT)])
    canvas.sparkline(x + 2, y + 7, w - 4, ttfbs, theme.WARN)


def _profile(canvas: Canvas, frame: Frame, x: int, y: int, w: int, h: int) -> None:
    state = frame.state
    canvas.box(x, y, w, h, theme.layer("agent", dim=True), title=[("回合画像 · 决策依据", theme.layer("agent", bold=True))])
    row = y + 1
    for key, label in (("model_route", "路由"), ("route_model", "模型"), ("effort", "effort"), ("situational", "情境"),
                       ("relationship", "关系"), ("intent_kind", "意图"), ("lifecycle", "生命周期"),
                       ("tool_focus", "工具焦点"), ("policy_tags", "策略"), ("context_frame", "框架")):
        value = state.profile.get(key)
        if value is None or row >= y + h - 1:
            continue
        text = ",".join(map(str, value)) if isinstance(value, list) else str(value)
        canvas.runs(x + 2, row, [(pad(label, 9), theme.MUTED), (text, theme.TEXT)], w - 4)
        row += 1
    decision = state.decision
    situation = decision.get("situation") if isinstance(decision.get("situation"), dict) else None
    if situation and row < y + h - 2:
        canvas.text(x + 2, row, "最近工具决策情境", theme.layer("agent"))
        row += 1
        for key, value in situation.items():
            if row >= y + h - 1:
                break
            canvas.runs(x + 2, row, [(pad(str(key), 14), theme.MUTED), (str(value), theme.TEXT)], w - 4)
            row += 1
