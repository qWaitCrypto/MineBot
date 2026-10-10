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
    control: str | None = None  # None: read-only; otherwise the control state label
    connection: dict[str, Any] | None = None  # launcher's view of the server connection, when it has one
    tweens: Any = None  # minebot.panel.motion.Tweens on a live panel; None draws values as they are
    identity: dict[str, Any] | None = None  # launcher facts for the title: server, version, disposable
    flash: tuple[str, str] | None = None  # (tone, text)

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


STATUS_TONE = {"思考中": "agent", "行动中": "body", "已暂停": "warn", "等待指令": "muted", "已结束": "muted",
               "请求失败": "fail", "已断线": "fail", "判断中": "reflex"}


def agent_status(frame: Frame) -> tuple[str, str]:
    """(label, detail) for what the Agent is doing right now, from the trace alone."""
    state = frame.state
    if state.terminal is not None:
        return "已结束", str(state.terminal.get("status") or "")
    if frame.connection and frame.connection.get("state") == "lost":
        lost_for = frame.now - float(frame.connection.get("since") or frame.now)
        return "已断线", f"{frame.connection.get('reason') or ''} {seconds(lost_for)}".strip()
    if frame.control == "已暂停":
        return "已暂停", "p 继续"
    running = state.running_calls()
    supervision = state.supervision
    if running and supervision.get("status") == "asking":
        # The runtime asked the model whether the running tool is stuck; nothing waits on the operator.
        return "判断中", f"{running[-1].tool} 是否卡住 {seconds(frame.now - float(supervision.get('ts') or frame.now))}"
    if running:
        call = running[-1]
        return "行动中", f"{call.tool} {seconds(frame.now - (call.invoke_ts or frame.now))}"
    request = state.request
    if request is not None and request.status == "running":
        return "思考中", f"第 {state.request_total} 次请求 {seconds(frame.now - request.started_ts)}"
    if request is not None and request.status == "failed" and frame.now - (request.ended_ts or 0) < 30:
        return "请求失败", (request.error or "")[:30]
    return "等待指令", ""


def _status_badge(frame: Frame) -> list[tuple[str, Any]]:
    label, detail = agent_status(frame)
    key = STATUS_TONE.get(label, "muted")
    style = theme.layer(key, bold=True) if key in theme.LAYER else theme.tone(key, bold=True)
    if label == "思考中" and frame.live:
        # Slow breathing, the only motion while nothing else happens.
        phase = (math.sin(frame.anim * math.pi) + 1) / 2
        style = theme.layer("agent", bold=True) if phase > 0.35 else theme.layer("agent", dim=True)
    icon = {"思考中": "◐", "行动中": "▶", "已暂停": "❚❚", "已结束": "■", "请求失败": "✗", "已断线": "⚠", "判断中": "◐"}.get(label, "○")
    parts: list[tuple[str, Any]] = [(f"{icon} {label}", style)]
    if detail:
        parts.append((f"  {detail}", theme.MUTED))
    return parts


def _identity(frame: Frame, width: int) -> list[tuple[str, Any]]:
    """The left title segment, shortened to fit: the version goes first, then the server;
    whether the bot may change the world stays visible the whole session."""
    bot = [("MINEBOT", theme.BRIGHT), ("  ·  ", theme.MUTED), (frame.state.bot or "—", theme.layer("operator", bold=True))]
    identity = frame.identity
    if identity is None:
        return bot + ([("  ·  ", theme.MUTED), ("由意图到世界", theme.layer("baritone", bold=True))] if width >= 160 else [])
    world: list[tuple[str, Any]] = []
    if identity.get("disposable") is not None:
        world = [("  ·  ", theme.MUTED),
                 ("世界:可改动", theme.WARN) if identity["disposable"] else ("世界:保护", theme.OK)]
    server = [(f"@{identity['server']}", theme.layer("operator"))] if identity.get("server") else []
    version = [("  ·  ", theme.MUTED), (str(identity["version"]), theme.MUTED)] if identity.get("version") else []
    budget = width // 2 - 2
    for candidate in (bot + server + version + world, bot + server + world, bot + world):
        if sum(_cells(text) for text, _ in candidate) <= budget:
            return candidate
    return bot + world


def draw_chrome(canvas: Canvas, frame: Frame) -> tuple[int, int, int, int]:
    """Title, legend, tabs and the shell status line; returns the content rectangle."""
    state, width, height = frame.state, canvas.width, canvas.height
    origin = state.first_ts or frame.now
    # title: identity on the left, what the Agent is doing in the middle, health and time on the right
    left = _identity(frame, width)
    canvas.runs(2, 0, left, width // 2)
    blink = int(frame.anim * 2) % 2 == 0
    if frame.live and state.terminal is None:
        badge = [("●" if blink else "○", theme.FAIL), (" LIVE ", theme.tone("fail", bold=True))]
    elif frame.live:
        badge = [("■", theme.MUTED), (" 已结束 ", theme.MUTED)]
    else:
        badge = [("▶" if frame.mode != "PAUSED" else "❚❚", theme.WARN), (f" {frame.mode} ", theme.tone("warn", bold=True))]
    right: list[tuple[str, Any]] = []
    if frame.identity is None:
        right += [(fit(frame.title, max(10, width // 3)), theme.MUTED), ("   ", None)]
    health, food = state.body.get("health"), state.body.get("food")
    if isinstance(health, (int, float)):
        right += [("♥", theme.FAIL), (f"{health:.0f}", theme.tone("fail" if health <= 6 else "text", bold=health <= 6))]
        if isinstance(food, (int, float)):
            right += [("  食", theme.MUTED), (f"{food:.0f}", theme.tone("warn" if food <= 6 else "text"))]
        right.append(("   ", None))
    right += [*badge, (clock(frame.now - origin), theme.BRIGHT)]
    used = sum(_cells(text) for text, _ in right)
    canvas.runs(max(0, width - used - 2), 0, right)
    if frame.live:
        left_used = 2 + sum(_cells(text) for text, _ in left)
        badge_parts = _status_badge(frame)
        room = width - used - 2 - left_used - 4
        badge_width = sum(_cells(text) for text, _ in badge_parts)
        if badge_width > room:
            badge_parts = badge_parts[:1]  # keep the state, drop the detail
            badge_width = _cells(badge_parts[0][0])
        if room >= badge_width:
            start = left_used + 2 + max(0, (room - badge_width) // 2)
            canvas.runs(start, 0, badge_parts)
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
    elif frame.control is not None:
        canvas.text(x + 2, 3, ": 说话  p 暂停/继续  !! 取消动作  q 退出  ←→ 翻页  enter 展开", theme.DIM, width - x - 4)
    else:
        canvas.text(x + 2, 3, "q 退出  ←→ 翻页  ↑↓ 选择  enter 展开  space 暂停  f 跟随", theme.DIM, width - x - 4)
    _status_line(canvas, frame, height - 1)
    return 0, 4, width, height - 5


def counter(frame: Frame, key: str, value: int, style: Any, *, up_good: bool = True) -> list[tuple[str, Any]]:
    """A count that glides to its new value on a live panel, with ▲/▼ for a couple of seconds."""
    if frame.tweens is None:
        return [(str(value), style)]
    shown, direction = frame.tweens.value(key, value, frame.anim)
    parts: list[tuple[str, Any]] = [(str(round(shown)), style)]
    if direction:
        good = (direction > 0) == up_good
        parts.append(("▲" if direction > 0 else "▼", theme.OK if good else theme.FAIL))
    return parts


def _status_line(canvas: Canvas, frame: Frame, y: int) -> None:
    state = frame.state
    calls = state.finished_calls()
    failed = sum(1 for call in calls if call.success is False)
    ttfb = percentile(list(state.ttfbs), 0.5)
    stats = [
        ("工具 ", theme.MUTED), *counter(frame, "calls", len(calls), theme.layer("body", bold=True)),
        (" · ", theme.DIM), ("✓", theme.OK), *counter(frame, "ok", len(calls) - failed, theme.TEXT),
        (" ✗", theme.FAIL), *counter(frame, "failed", failed, theme.TEXT, up_good=False),
        (" · 人工 ", theme.MUTED), *counter(frame, "operator", len(state.operator), theme.layer("operator")),
        (" · 请求 ", theme.MUTED), *counter(frame, "requests", state.request_total, theme.layer("agent")),
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
        if frame.control is not None:
            prompt = [("~/minebot ", theme.layer("agent")), ("$ ", theme.TEXT), ("minebot ", theme.layer("operator")),
                      (("❚❚ 已暂停  " if frame.control == "已暂停" else "● 可控制  "),
                       theme.WARN if frame.control == "已暂停" else theme.OK)]
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
