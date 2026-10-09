"""Panel palette: one colour per runtime layer, plus status tones."""

from __future__ import annotations

from rich.style import Style

BACKGROUND = "#0b0f14"
LAYER = {
    "operator": "#5fd7d7",
    "agent": "#87d787",
    "body": "#5fafff",
    "client": "#8787ff",
    "baritone": "#af87ff",
    "reflex": "#ffaf5f",
    "world": "#d7af5f",
    "session": "#8a96a3",
}
LAYER_NAMES = {
    "operator": "操作者",
    "agent": "Agent",
    "body": "Body",
    "client": "ClientBody",
    "baritone": "Baritone",
    "reflex": "反射",
    "world": "世界",
    "session": "会话",
}
TONE = {
    "ok": "#5fd75f",
    "warn": "#ffd75f",
    "fail": "#ff5f5f",
    "muted": "#6c7a89",
    "dim": "#3b4652",
    "text": "#c9d1d9",
    "bright": "#f0f6fc",
}


def layer(name: str, *, bold: bool = False, dim: bool = False) -> Style:
    color = LAYER.get(name, TONE["text"])
    if dim:
        return Style(color=blend(color, BACKGROUND, 0.55))
    return Style(color=color, bold=bold)


def tone(name: str, *, bold: bool = False) -> Style:
    return Style(color=TONE.get(name, TONE["text"]), bold=bold)


def on(name: str, color: str | None = None) -> Style:
    """Highlight row: layer colour on a tinted background."""
    base = LAYER.get(name, TONE.get(name, TONE["text"]))
    return Style(color=color or TONE["bright"], bgcolor=blend(base, BACKGROUND, 0.78))


def blend(color: str, other: str, amount: float) -> str:
    """Mix ``color`` toward ``other`` by ``amount`` (0 keeps ``color``)."""
    a = [int(color[index:index + 2], 16) for index in (1, 3, 5)]
    b = [int(other[index:index + 2], 16) for index in (1, 3, 5)]
    mixed = [round(x + (y - x) * amount) for x, y in zip(a, b)]
    return "#" + "".join(f"{value:02x}" for value in mixed)


def fade(name: str, age: float, horizon: float) -> Style:
    """Newest is brightest; older log lines sink toward the background."""
    amount = 0.0 if age <= 0 else min(0.65, age / horizon * 0.65)
    return Style(color=blend(LAYER.get(name, TONE.get(name, TONE["text"])), BACKGROUND, amount))


TEXT = tone("text")
MUTED = tone("muted")
DIM = tone("dim")
BRIGHT = tone("bright", bold=True)
OK = tone("ok")
WARN = tone("warn")
FAIL = tone("fail")
