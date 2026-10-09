"""Page 6: reflex layer, body control, supervision, stops and the task outcome."""

from __future__ import annotations

from minebot.panel import theme
from minebot.panel.canvas import Canvas, pad, wrap
from minebot.panel.views.common import Frame, draw_chrome, seconds, since

CHAIN = (("用户停止", "立即"), ("L0 本能", "tick"), ("L1 威胁", "百毫秒~秒"), ("L2 工作", "秒~小时"), ("L3 生活", "秒~天"))
KIND_LABEL = {"supervision": "监督", "blocked": "阻塞", "handoff": "交接", "cancel": "停止确认", "interrupt": "中断"}


def draw(canvas: Canvas, frame: Frame) -> None:
    _, top, width, height = draw_chrome(canvas, frame)
    state, now = frame.state, frame.now
    holding = any(call.mutating or call.group == "move" for call in state.running_calls())
    canvas.box(1, top, width - 2, 6, theme.layer("reflex", dim=True),
               title=[("优先级链", theme.layer("reflex", bold=True)), ("  · 持有者由事实推导；控制权事件未接入（切片 2）", theme.MUTED)])
    cell = (width - 6) // len(CHAIN)
    for index, (label, scale) in enumerate(CHAIN):
        cx = 3 + index * cell
        active = holding and index == 3
        canvas.box(cx, top + 1, cell - 3, 4, theme.layer("reflex", bold=True) if active else theme.DIM)
        canvas.center(cx, top + 2, cell - 3, label, theme.on("reflex") if active else theme.layer("reflex", dim=True))
        canvas.center(cx, top + 3, cell - 3, scale, theme.MUTED)
        if index + 1 < len(CHAIN):
            canvas.text(cx + cell - 3, top + 2, " ›", theme.DIM)
    half = width // 2
    y = top + 6
    h = height - 6
    canvas.box(1, y, half - 1, h // 2, theme.layer("reflex", dim=True), title=[("控制权", theme.layer("reflex", bold=True))])
    running = state.running_calls()
    rows = [("holder", "agent_call" if holding else "none"), ("claim_id", running[-1].id if holding else "—"),
            ("generation", "未接入"), ("stop_state", "running" if holding else "—"),
            ("since", since(state, running[-1].invoke_ts or now) if holding else "—")]
    for index, (key, value) in enumerate(rows):
        canvas.runs(3, y + 1 + index, [(pad(key, 12), theme.MUTED), (str(value), theme.TEXT if value != "未接入" else theme.DIM)],
                    half - 5)
    sy = y + h // 2
    canvas.box(1, sy, half - 1, h - h // 2, theme.layer("reflex", dim=True), title=[("Decision 通道 · 监督", theme.layer("reflex", bold=True))])
    supervision = state.supervision
    if supervision:
        canvas.runs(3, sy + 1, [("状态 ", theme.MUTED), (str(supervision.get("status")), theme.TEXT),
                                ("  选择 ", theme.MUTED), (str(supervision.get("choice") or "—"), theme.layer("reflex", bold=True)),
                                ("  耗时 ", theme.MUTED), (seconds(supervision.get("elapsed_s")), theme.TEXT)], half - 5)
        canvas.runs(3, sy + 2, [("读取 ", theme.MUTED), (str(supervision.get("reads", "—")), theme.TEXT),
                                ("  提问 ", theme.MUTED), (str(supervision.get("questions", "—")), theme.TEXT)], half - 5)
    else:
        canvas.text(3, sy + 1, "本次会话没有监督请求", theme.DIM)
    entries = [entry for entry in state.reflex if entry["ts"] <= now]
    canvas.box(half + 1, y, width - half - 2, h // 2 + 2, theme.layer("reflex", dim=True),
               title=[("阻塞 · 交接 · 停止", theme.layer("reflex", bold=True))])
    for index, entry in enumerate(reversed(entries[-(h // 2):])):
        canvas.runs(half + 3, y + 1 + index, [(since(state, entry["ts"]) + " ", theme.MUTED),
                                              (pad(KIND_LABEL.get(entry["kind"], entry["kind"]), 9), theme.layer("reflex")),
                                              (str(entry.get("detail") or ""), theme.TEXT)], width - half - 6)
    if not entries:
        canvas.text(half + 3, y + 1, "无", theme.DIM)
    oy = y + h // 2 + 2
    canvas.box(half + 1, oy, width - half - 2, height - (oy - top), theme.layer("agent", dim=True),
               title=[("任务结论", theme.layer("agent", bold=True))])
    outcome = state.outcome
    if outcome and outcome.get("ts", 0) <= now:
        status = str(outcome.get("status"))
        canvas.runs(half + 3, oy + 1, [("状态 ", theme.MUTED),
                                       (status, theme.tone("ok" if status == "completed" else "warn", bold=True))])
        lines = wrap(str(outcome.get("summary") or ""), width - half - 8, max(1, height - (oy - top) - 4))
        for index, line in enumerate(lines):
            canvas.text(half + 3, oy + 2 + index, line, theme.TEXT)
    else:
        canvas.text(half + 3, oy + 1, "尚无", theme.DIM)
