"""Shared frame context, chrome (title, legend, tabs, status line) and formatting."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from minebot.panel import theme
from rich.cells import cell_len

from minebot.panel.canvas import Canvas, fit, pad
from minebot.panel.model import Alert, PanelState, alerts, percentile

PAGES = (
    ("overview", "总览"),
    ("decision", "决策"),
    ("actions", "行动"),
    ("body", "身体·世界"),
    ("baritone", "寻路"),
    ("reflex", "反射"),
    ("events", "事件"),
)
SOURCE_LABELS = {
    "body.perception": "感知",
    "agent.context": "上下文",
    "body.block_work": "方块作业",
    "body.client": "客户端",
    "body.objective": "目标",
    "body.movement": "移动",
    "body.inventory": "背包",
    "body.combat": "战斗",
    "java_body": "Java",
    "client.environment": "环境",
}
GROUP_LABELS = {"sense": "感知 · 读取", "move": "移动 · 垂直", "work": "作业 · 改世界"}
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


@dataclass
class Frame:
    state: PanelState
    now: float
    anim: float
    live: bool
    title: str = ""
    mode: str = "LIVE"
    page: int = 0
    replay: tuple[int, int] | None = None
    selection: dict[str, int] = field(default_factory=dict)
    raw: list[dict[str, Any]] = field(default_factory=list)
    alerts: list[Alert] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.alerts:
            self.alerts = alerts(self.state, self.now, live=self.live)


def spinner(anim: float) -> str:
    return SPINNER[int(anim * 12) % len(SPINNER)]


def clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def since(state: PanelState, ts: float) -> str:
    return clock(ts - (state.first_ts or ts))


def seconds(value: float | None) -> str:
    if value is None:
        return "—"
    if value < 10:
        return f"{value:.1f}s"
    if value < 600:
        return f"{value:.0f}s"
    return f"{value / 60:.0f}m"


def tokens(value: float) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1000:
        return f"{value / 1000:.1f}k"
    return str(int(value))


def pos_text(pos: Any, digits: int = 1) -> str:
    if isinstance(pos, (list, tuple)) and len(pos) == 3:
        return " ".join(f"{float(value):.{digits}f}" for value in pos)
    return "—"


def block_pos(pos: Any) -> str:
    if isinstance(pos, (list, tuple)) and len(pos) == 3:
        return ",".join(str(int(math.floor(float(value)))) for value in pos)
    return "—"


def short_block(name: Any) -> str:
    return str(name or "").removeprefix("minecraft:").removeprefix("Block{minecraft:").rstrip("}")


def wait_tone(seconds_waited: float, warn: float = 15.0, fail: float = 45.0) -> str:
    return "fail" if seconds_waited >= fail else "warn" if seconds_waited >= warn else "ok"


def draw_chrome(canvas: Canvas, frame: Frame) -> tuple[int, int, int, int]:
    """Title, legend, tabs and the shell status line; returns the content rectangle."""
    state, width, height = frame.state, canvas.width, canvas.height
    origin = state.first_ts or frame.now
    # title
    left = [("MINEBOT", theme.BRIGHT), ("  ·  ", theme.MUTED), (state.bot or "—", theme.layer("operator", bold=True)),
            ("  ·  ", theme.MUTED), ("由意图到世界", theme.layer("baritone", bold=True))]
    canvas.runs(2, 0, left, width // 2)
    blink = int(frame.anim * 2) % 2 == 0
    if frame.live and state.terminal is None:
        badge = [("●" if blink else "○", theme.FAIL), (" LIVE ", theme.tone("fail", bold=True))]
    elif frame.live:
        badge = [("■", theme.MUTED), (" 已结束 ", theme.MUTED)]
    else:
        badge = [("▶" if frame.mode != "PAUSED" else "❚❚", theme.WARN), (f" {frame.mode} ", theme.tone("warn", bold=True))]
    right = [(fit(frame.title, max(10, width // 3)), theme.MUTED), ("   ", None), *badge,
             (clock(frame.now - origin), theme.BRIGHT)]
    used = sum(_cells(text) for text, _ in right)
    canvas.runs(max(0, width - used - 2), 0, right)
    canvas.hline(1, width - 2, 1, theme.DIM, "─")
    # legend
    legend = []
    for key in ("operator", "agent", "body", "baritone", "reflex", "world"):
        legend += [("■ ", theme.layer(key)), (theme.LAYER_NAMES[key], theme.layer(key, dim=True)), ("    ", None)]
    start = max(0, (width - sum(_cells(text) for text, _ in legend)) // 2)
    canvas.runs(start, 2, legend)
    # tabs and alerts
    x = 2
    for index, (_, label) in enumerate(PAGES):
        style = theme.on("agent") if index == frame.page else theme.MUTED
        x += canvas.text(x, 3, f" {index + 1} {label} ", style) + 1
    if frame.alerts:
        alert_text = "  ".join(f"▲ {alert.text}" for alert in frame.alerts)
        tone = frame.alerts[0].level
        canvas.text(x + 2, 3, fit(alert_text, max(0, width - x - 4)), theme.tone(tone, bold=True))
    else:
        canvas.text(x + 2, 3, "q 退出  ←→ 翻页  ↑↓ 选择  enter 展开  space 暂停  f 跟随", theme.DIM, width - x - 4)
    _status_line(canvas, frame, height - 1)
    return 0, 4, width, height - 5


def _status_line(canvas: Canvas, frame: Frame, y: int) -> None:
    state = frame.state
    calls = state.finished_calls()
    failed = sum(1 for call in calls if call.success is False)
    ttfb = percentile(list(state.ttfbs), 0.5)
    stats = [
        ("工具 ", theme.MUTED), (str(len(calls)), theme.layer("body", bold=True)),
        (" · ", theme.DIM), ("✓", theme.OK), (str(len(calls) - failed), theme.TEXT),
        (" ✗", theme.FAIL), (str(failed), theme.TEXT),
        (" · 人工 ", theme.MUTED), (str(len(state.operator)), theme.layer("operator")),
        (" · 请求 ", theme.MUTED), (str(state.request_total), theme.layer("agent")),
        (" · 首字节 p50 ", theme.MUTED), (seconds(ttfb), theme.TEXT),
        (" · tokens ", theme.MUTED), (tokens(state.usage["input"] + state.usage["output"]), theme.TEXT),
        (" · 告警 ", theme.MUTED), (str(len(frame.alerts)), theme.WARN if frame.alerts else theme.TEXT),
    ]
    if frame.replay is not None:
        position, total = frame.replay
        prompt = [("~/minebot ", theme.layer("agent")), ("$ ", theme.TEXT), ("replay ", theme.layer("operator"))]
        used = canvas.runs(1, y, prompt)
        bar_width = max(10, min(40, canvas.width // 4))
        canvas.bar(1 + used, y, bar_width, position / max(1, total), theme.layer("operator"), theme.DIM, "─")
        used += bar_width + 1
        used += canvas.text(1 + used, y, f"{position}/{total}  ", theme.MUTED)
        canvas.runs(1 + used, y, stats)
    else:
        prompt = [("~/minebot ", theme.layer("agent")), ("$ ", theme.TEXT), ("panel ", theme.layer("operator")),
                  ("--follow  ", theme.MUTED)]
        used = canvas.runs(1, y, prompt)
        canvas.runs(1 + used, y, stats)


def _cells(text: str) -> int:
    return cell_len(text)


def log_rows(canvas: Canvas, frame: Frame, x: int, y: int, width: int, rows: int, *, title: str = "session log") -> None:
    """The scrolling session log: newest line brightest, older lines sink."""
    state = frame.state
    canvas.hline(x, x + width - 1, y, theme.DIM, "─")
    canvas.runs(x + 1, y, [("┤ ", theme.DIM), (title, theme.MUTED), (" ├", theme.DIM)])
    lines = [line for line in state.log if line.ts <= frame.now + 1e-6][-rows:]
    for index, line in enumerate(lines):
        row = y + 1 + index
        age = len(lines) - 1 - index
        layer_style = theme.fade(line.layer, age, max(4, rows))
        detail_style = theme.fade(line.tone or "text", age, max(4, rows)) if line.tone else theme.fade("session", age, rows)
        if age == 0:
            layer_style = theme.layer(line.layer, bold=True)
            detail_style = theme.tone(line.tone or "bright")
        used = canvas.text(x + 1, row, since(state, line.ts), theme.fade("session", age, rows))
        used += 2
        used += canvas.text(x + 1 + used, row, pad(line.layer, 9), layer_style)
        used += canvas.text(x + 1 + used, row, pad(line.label, 18) + " ", layer_style)
        canvas.text(x + 1 + used, row, line.detail, detail_style, width - used - 2)
