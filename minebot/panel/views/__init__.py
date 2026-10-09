"""Page registry and the shared JSON detail overlay."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from minebot.panel import theme
from minebot.panel.canvas import Canvas, wrap
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
    return canvas


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


__all__ = ["DRAW", "PAGES", "detail_lines", "render"]
