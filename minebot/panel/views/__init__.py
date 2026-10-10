"""Page registry and the shared JSON detail overlay."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from rich.cells import cell_len

from minebot.panel import theme
from minebot.panel.canvas import Canvas, fit, pad, wrap
from minebot.panel.views import actions, baritone, body, decision, events, overview, reflex
from minebot.panel.views.common import PAGES, Frame

DRAW: dict[str, Callable[[Canvas, Frame], None]] = {
    "overview": overview.draw,
    "decision": decision.draw,
    "actions": actions.draw,
    "body": body.draw,
    "baritone": baritone.draw,
    "reflex": reflex.draw,
    "events": events.draw,
}


def render(frame: Frame, width: int, height: int, detail: Any = None, detail_scroll: int = 0) -> Canvas:
    canvas = Canvas(width, height)
    DRAW[PAGES[frame.page][0]](canvas, frame)
    if detail is not None:
        draw_detail(canvas, detail, detail_scroll)
    if frame.flash is not None:
        draw_flash(canvas, *frame.flash)
    return canvas


def draw_flash(canvas: Canvas, tone: str, text: str) -> None:
    """A one-line toast above the status line, right-aligned."""
    label = fit(f" {text} ", max(10, canvas.width - 8))
    width = cell_len(label) + 2
    x = max(1, canvas.width - width - 2)
    y = max(0, canvas.height - 4)
    canvas.box(x, y, width, 3, theme.tone(tone, bold=True))
    canvas.text(x + 1, y + 1, label, theme.tone(tone, bold=True))


HELP_SECTIONS: list[tuple[str, list[tuple[str, str]]]] = [
    ("浏览", [("1–7  ←→ tab", "切换页面"), ("↑↓  pgup pgdn", "选择 / 滚动"), ("enter", "展开原始记录"),
             ("esc", "关闭覆盖层"), ("f", "跟随最新"), ("/", "筛选事件"), ("[ ]", "上一个 / 下一个失败"),
             ("u", "下一条人工消息")]),
    ("控制", [(": t", "对机器人说话；/goal 换目标"), ("p", "暂停 / 继续"), ("! !", "取消当前动作（1.5 秒内按两次）"),
             ("ctrl+d", "分离面板，机器人继续运行"), ("q", "退出（先确认）")]),
    ("回放", [("space", "播放 / 暂停"), ("+ −", "速度"), (", .", "后退 / 前进 50 条"), ("home end", "开头 / 结尾")]),
    ("外观", [("T", "切换主题：深色 · 浅色 · 高对比 · 无色"), ("N", "通知历史"), ("?", "这张帮助")]),
]
SYMBOLS = [("●", "活跃"), ("○", "空闲"), ("◐", "进行中"), ("✓", "成功"), ("✗", "失败"), ("⚠", "越界 / 警告"), ("❚❚", "已暂停")]


def draw_help(canvas: Canvas, *, controllable: bool, replay: bool) -> None:
    width = min(canvas.width - 4, 92)
    sections = [(title, rows) for title, rows in HELP_SECTIONS
                if (title != "控制" or controllable) and (title != "回放" or replay)]
    left = sections[: (len(sections) + 1) // 2]
    right = sections[(len(sections) + 1) // 2:]
    column_rows = max(sum(len(rows) + 2 for _, rows in side) for side in (left, right or left))
    height = min(canvas.height - 2, column_rows + 6)
    x, y = (canvas.width - width) // 2, max(1, (canvas.height - height) // 2)
    canvas.fill(x, y, width, height, " ")
    canvas.box(x, y, width, height, theme.layer("operator", bold=True),
               title=[("快捷键", theme.layer("operator", bold=True)), ("  esc 关闭", theme.MUTED)])
    half = (width - 4) // 2
    for column, side in enumerate((left, right)):
        row = y + 1
        for title, rows in side:
            canvas.text(x + 2 + column * half, row, title, theme.layer("agent", bold=True))
            row += 1
            for keys, label in rows:
                if row >= y + height - 3:
                    break
                canvas.text(x + 3 + column * half, row, pad(keys, 14), theme.BRIGHT)
                canvas.text(x + 18 + column * half, row, label, theme.TEXT, half - 18)
                row += 1
            row += 1
    legend = []
    for symbol, label in SYMBOLS:
        legend += [(symbol, theme.BRIGHT), (" " + label + "   ", theme.MUTED)]
    canvas.runs(x + 2, y + height - 2, legend, width - 4)


def draw_too_small(frame: Frame, width: int, height: int, minimum: tuple[int, int]) -> Canvas:
    """Below the minimum size: one line of what the bot is doing, plus how big to go."""
    canvas = Canvas(width, height)
    state = frame.state
    canvas.runs(0, 0, [("MINEBOT ", theme.BRIGHT), (state.bot or "", theme.layer("operator", bold=True))], width)
    last = state.log[-1] if state.log else None
    doing = "会话已结束" if state.terminal else f"{last.label} {last.detail}" if last else "等待事件"
    if height > 1:
        canvas.text(0, 1, fit(f"▶ {doing}", width), theme.layer("agent"))
    if frame.control is not None and height > 2:
        canvas.text(0, 2, fit("❚❚ 已暂停" if frame.control == "已暂停" else "● 可控制", width),
                    theme.WARN if frame.control == "已暂停" else theme.OK)
    if frame.flash is not None and height > 3:
        canvas.text(0, 3, fit(frame.flash[1], width), theme.tone(frame.flash[0], bold=True))
    if height > 4:
        canvas.text(0, height - 1, fit(f"放大终端看完整面板（至少 {minimum[0]}×{minimum[1]}，现在 {width}×{height}）", width),
                    theme.MUTED)
    return canvas


def _notice_style(tone: str, *, bold: bool = False) -> Any:
    if tone == "agent":
        return theme.layer("agent", bold=bold)
    return theme.tone(tone, bold=bold)


def draw_notices(canvas: Canvas, notices: list[Any], unread: int) -> None:
    """Newest notices stacked at the top right, under the tabs."""
    if not notices:
        return
    width = min(56, max(24, canvas.width // 3))
    x = canvas.width - width - 2
    y = 5
    for notice in reversed(notices):
        canvas.fill(x, y, width, 3, " ")
        canvas.box(x, y, width, 3, _notice_style(notice.tone))
        canvas.text(x + 2, y + 1, fit(notice.label(), width - 4), _notice_style(notice.tone, bold=True))
        y += 3
    if unread > len(notices):
        canvas.text(x + width - 16, y, fit(f" N 通知 {unread} 条 ", 16), theme.MUTED)


def draw_notice_history(canvas: Canvas, notices: list[Any]) -> None:
    width = min(canvas.width - 4, 110)
    height = canvas.height - 6
    x, y = (canvas.width - width) // 2, 3
    canvas.fill(x, y, width, height, " ")
    canvas.box(x, y, width, height, theme.layer("operator", bold=True),
               title=[("通知", theme.layer("operator", bold=True)), (f"  {len(notices)} 条  esc 关闭", theme.MUTED)])
    if not notices:
        canvas.text(x + 2, y + 1, "还没有通知：工具失败、掉血、越界改动、模型报错和 Agent 说的话会出现在这里",
                    theme.MUTED, width - 4)
        return
    origin = notices[0].ts
    for row, notice in enumerate(reversed(notices[-(height - 2):])):
        offset = int(notice.ts - origin)
        stamp = f"+{offset // 3600:02d}:{offset % 3600 // 60:02d}:{offset % 60:02d}"
        canvas.text(x + 2, y + 1 + row, stamp, theme.MUTED)
        canvas.text(x + 13, y + 1 + row, fit(notice.label(), width - 15), _notice_style(notice.tone))


def detail_lines(detail: Any, width: int) -> list[str]:
    text = json.dumps(detail, ensure_ascii=False, indent=2, default=str)
    lines: list[str] = []
    for raw in text.splitlines():
        indent = len(raw) - len(raw.lstrip(" "))
        wrapped = wrap(raw.strip(), max(10, width - indent), 1000) or [""]
        lines += [" " * indent + line if index == 0 else " " * (indent + 2) + line for index, line in enumerate(wrapped)]
    return lines


def draw_detail(canvas: Canvas, detail: Any, scroll: int) -> None:
    width = min(canvas.width - 4, 140)
    height = canvas.height - 6
    x = (canvas.width - width) // 2
    y = 3
    canvas.box(x, y, width, height, theme.layer("operator", bold=True),
               title=[("原始记录", theme.layer("operator", bold=True)), ("  ↑↓ 滚动  esc 关闭", theme.MUTED)])
    lines = detail_lines(detail, width - 4)
    scroll = max(0, min(scroll, max(0, len(lines) - (height - 2))))
    for index, line in enumerate(lines[scroll:scroll + height - 2]):
        style = theme.layer("agent") if line.strip().startswith('"') and '":' in line else theme.TEXT
        canvas.text(x + 2, y + 1 + index, line, style, width - 4)
    if len(lines) > height - 2:
        canvas.text(x + width - 12, y + height - 1, f" {scroll + 1}/{len(lines)} ", theme.MUTED)


__all__ = ["DRAW", "PAGES", "detail_lines", "draw_flash", "draw_help", "draw_notice_history", "draw_notices", "draw_too_small", "render"]
