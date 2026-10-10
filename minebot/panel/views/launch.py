"""The launch screen: one box per step on a wire, packets on the step being worked on."""

from __future__ import annotations

import time
from typing import Any

from rich.cells import cell_len

from minebot.panel import theme
from minebot.panel.canvas import BOX_HEAVY, BOX_ROUND, Canvas, fit, wrap
from minebot.panel.views.common import clock, seconds, spinner

ICONS = {"ok": "✓", "warn": "⚠", "fail": "✗", "muted": "·", "running": "◐", "pending": "○"}
STATUS_TONE = {"ok": "ok", "warn": "warn", "fail": "fail"}


def _step_style(status: str, *, bold: bool = False) -> Any:
    if status == "running":
        return theme.layer("agent", bold=True)
    if status in STATUS_TONE:
        return theme.tone(STATUS_TONE[status], bold=bold)
    return theme.DIM


def _line_style(tone: str) -> Any:
    return {"ok": theme.OK, "warn": theme.WARN, "fail": theme.FAIL}.get(tone, theme.MUTED)


def draw(canvas: Canvas, launcher: Any, anim: float, *, log: tuple[str, list[str], int] | None = None) -> None:
    width, height = canvas.width, canvas.height
    now = time.monotonic()
    steps = launcher.steps
    # title
    canvas.runs(2, 0, [("MINEBOT", theme.BRIGHT), ("  ▸  ", theme.MUTED), ("启动", theme.layer("agent", bold=True))])
    who = f"{launcher.bot or '—'}@{launcher.server or '—'}"
    right = [(who, theme.layer("operator")), ("   ", None), (clock(time.time() - launcher.started_at), theme.BRIGHT)]
    canvas.runs(max(0, width - sum(cell_len(text) for text, _ in right) - 2), 0, right)
    canvas.hline(1, width - 2, 1, theme.DIM)
    # pipeline
    tail = 9 if width >= 110 else 0
    gap = 4 if width >= 110 else 2
    box_w = max(10, (width - 4 - tail - gap * (len(steps) - 1)) // len(steps))
    top = 3
    xs = [2 + index * (box_w + gap) for index in range(len(steps))]
    for index, step in enumerate(steps):
        x = xs[index]
        style = _step_style(step.status)
        chars = BOX_HEAVY if step.status in ("running", "fail") else BOX_ROUND
        canvas.box(x, top, box_w, 4, style, title=[(step.label, _step_style(step.status, bold=True))], chars=chars)
        icon = spinner(anim) if step.status == "running" else ICONS.get(step.status, "○")
        summary = step.summary or {"pending": "等待", "running": "进行中"}.get(step.status, "")
        canvas.runs(x + 2, top + 1, [(icon + " ", style), (fit(summary, box_w - 6), theme.TEXT if step.status != "pending" else theme.DIM)])
        elapsed = step.elapsed(now)
        if elapsed is not None:
            canvas.text(x + 2, top + 2, seconds(elapsed), theme.MUTED if step.status != "running" else theme.BRIGHT)
        if index + 1 < len(steps):
            following = steps[index + 1]
            cells = canvas.wire([(x + box_w, top + 1), (xs[index + 1] - 1, top + 1)],
                                theme.OK if following.status in ("ok", "warn") else
                                theme.layer("agent") if following.status == "running" else theme.DIM,
                                dashed=following.status == "pending", arrow="▶")
            if following.status == "running":
                canvas.dot(cells, (anim * 1.2) % 1.0, theme.layer("agent", bold=True), theme.layer("agent", dim=True))
    if tail:
        done = launcher.phase == "ready"
        x = xs[-1] + box_w
        cells = canvas.wire([(x, top + 1), (x + 3, top + 1)], theme.OK if done else theme.DIM, dashed=not done, arrow="▶")
        del cells
        canvas.text(x + 5, top + 1, "面板", theme.tone("ok", bold=True) if done else theme.DIM)
    # overall progress
    finished = sum(1 for step in steps if step.status in ("ok", "warn"))
    bar_y = top + 5
    canvas.bar(2, bar_y, width - 16, finished / len(steps), theme.OK if launcher.phase != "failed" else theme.FAIL, theme.DIM, "─")
    canvas.text(width - 12, bar_y, f"{finished}/{len(steps)}", theme.BRIGHT)
    # detail of the step being worked on (or the last one)
    body_y = bar_y + 2
    body_h = height - body_y - 2
    if log is not None:
        _draw_log(canvas, 2, body_y, width - 4, body_h, *log)
    elif launcher.phase in ("stopping", "stopped"):
        _draw_messages(canvas, launcher, 2, body_y, width - 4, body_h, anim)
    else:
        step = launcher.current or next((step for step in reversed(steps) if step.status != "pending"), steps[0])
        _draw_step(canvas, launcher, step, 2, body_y, width - 4, body_h, anim)
    _status_line(canvas, launcher, height - 1)


def _draw_step(canvas: Canvas, launcher: Any, step: Any, x: int, y: int, width: int, height: int, anim: float) -> None:
    failed = step.status == "fail"
    card_h = 0
    if failed:
        error_lines = wrap(step.error, width - 6, 3) or ["失败"]
        hint_lines = wrap(step.hint, width - 6, 2) if step.hint else []
        card_h = len(error_lines) + len(hint_lines) + 4
        cy = y + height - card_h
        canvas.box(x, cy, width, card_h, theme.tone("fail", bold=True), chars=BOX_HEAVY,
                   title=[(f"✗ {step.label}", theme.tone("fail", bold=True))])
        row = cy + 1
        for line in error_lines:
            canvas.text(x + 2, row, line, theme.BRIGHT)
            row += 1
        for line in hint_lines:
            canvas.runs(x + 2, row, [("→ ", theme.WARN), (line, theme.TEXT)])
            row += 1
        canvas.runs(x + 2, cy + card_h - 2, [("r", theme.BRIGHT), (" 重试这一步    ", theme.MUTED), ("l", theme.BRIGHT),
                                              (" 看日志    ", theme.MUTED), ("q", theme.BRIGHT), (" 退出", theme.MUTED)])
    box_h = height - card_h
    if box_h < 3:
        return
    canvas.box(x, y, width, box_h, theme.layer("agent", dim=True) if not failed else theme.DIM,
               title=[(step.label, theme.layer("agent", bold=True)), ("  " + _step_hint(step.key), theme.MUTED)])
    row = y + 1
    output = list(launcher.output) if step.status == "running" or failed else []
    room = box_h - 2 - (len(output) + 1 if output else 0)
    for tone, text in step.lines[-max(1, room):]:
        canvas.runs(x + 2, row, [(ICONS.get(tone, "·") + " ", _line_style(tone)), (fit(text, width - 6), theme.TEXT)])
        row += 1
    if step.status == "running":
        canvas.runs(x + 2, row, [(spinner(anim) + " ", theme.layer("agent", bold=True)),
                                 (_waiting(step.key), theme.layer("agent")),
                                 (f"  {seconds(step.elapsed())}", theme.MUTED)])
        row += 1
    if output:
        start = y + box_h - 1 - len(output)
        for index, line in enumerate(output):
            age = len(output) - 1 - index
            canvas.runs(x + 2, start + index, [("│ ", theme.DIM), (fit(line, width - 6), theme.fade("session", age, 6))])


def _step_hint(key: str) -> str:
    return {
        "env": "运行配置 · 模型 · 身体 · 客户端",
        "account": "机器人的游戏账号",
        "server": "服务器状态 · 版本 · 世界权限",
        "client": "带 MineBot mod 的游戏客户端",
        "world": "角色出生并站稳",
        "agent": "Agent 会话进程",
    }.get(key, "")


def _waiting(key: str) -> str:
    return {
        "env": "检查配置",
        "server": "查询服务器",
        "client": "等待客户端 IPC 就绪",
        "world": "等待角色进入世界",
        "agent": "等待 Agent 就绪",
    }.get(key, "处理中")


def _draw_messages(canvas: Canvas, launcher: Any, x: int, y: int, width: int, height: int, anim: float) -> None:
    stopping = launcher.phase == "stopping"
    canvas.box(x, y, width, height, theme.layer("reflex", dim=True),
               title=[("正在收尾" if stopping else "已停止", theme.layer("reflex", bold=True))])
    row = y + 1
    for _, tone, text in list(launcher.messages)[-(height - 3):]:
        canvas.runs(x + 2, row, [(ICONS.get(tone, "·") + " ", _line_style(tone)), (fit(text, width - 6), theme.TEXT)])
        row += 1
    if stopping:
        canvas.runs(x + 2, row, [(spinner(anim) + " ", theme.layer("reflex", bold=True)), ("请稍候，不要关闭终端", theme.MUTED)])


def _draw_log(canvas: Canvas, x: int, y: int, width: int, height: int, title: str, lines: list[str], scroll: int) -> None:
    canvas.box(x, y, width, height, theme.layer("operator", bold=True),
               title=[(title, theme.layer("operator", bold=True)), ("  ↑↓ 滚动  tab 换文件  esc 关闭", theme.MUTED)])
    rows = height - 2
    end = max(0, len(lines) - scroll)
    for index, line in enumerate(lines[max(0, end - rows):end]):
        style = theme.FAIL if any(marker in line for marker in ("ERROR", "Exception", "FAILURE")) else theme.TEXT
        canvas.text(x + 2, y + 1 + index, fit(line, width - 4), style)


def _status_line(canvas: Canvas, launcher: Any, y: int) -> None:
    prompt = [("~/minebot ", theme.layer("agent")), ("$ ", theme.TEXT), ("minebot ", theme.layer("operator"))]
    used = canvas.runs(1, y, prompt)
    keys = "esc 取消启动  l 看日志" if launcher.phase == "launching" else "r 重试  l 看日志  q 退出" if launcher.phase == "failed" else ""
    used += canvas.text(1 + used, y, fit(str(launcher.run_dir), max(0, canvas.width - used - cell_len(keys) - 6)), theme.MUTED)
    canvas.text(max(used + 3, canvas.width - cell_len(keys) - 2), y, keys, theme.DIM)


__all__ = ["draw"]
