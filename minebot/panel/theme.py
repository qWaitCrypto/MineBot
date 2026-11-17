"""Panel palette: one colour per runtime layer, plus status tones.

Views always paint with the dark palette below. :func:`themed` maps a finished style
to the active theme (``dark``, ``light``, ``contrast`` or ``mono``) when the canvas is
turned into text, so a theme is one table here rather than a branch in every view.
``NO_COLOR`` selects ``mono``: no colours, state carried by bold, reverse and symbols.
"""

from __future__ import annotations

import os
from functools import lru_cache

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


# ------------------------------------------------------------------ themes

THEMES = ("dark", "light", "contrast", "mono")
_LIGHT = {
    BACKGROUND: "#f6f8fa",
    "#5fd7d7": "#0b7a83", "#87d787": "#2f7d32", "#5fafff": "#1f5fbf", "#8787ff": "#4b4bc4",
    "#af87ff": "#7340c4", "#ffaf5f": "#b35900", "#d7af5f": "#8a6400", "#8a96a3": "#57606a",
    "#5fd75f": "#1a7f37", "#ffd75f": "#9a6700", "#ff5f5f": "#cf222e", "#6c7a89": "#57606a",
    "#3b4652": "#8c959f", "#c9d1d9": "#24292f", "#f0f6fc": "#0b0f14",
}
_CONTRAST = {
    BACKGROUND: "#000000",
    "#6c7a89": "#c0c0c0", "#3b4652": "#8a8a8a", "#c9d1d9": "#ffffff", "#8a96a3": "#d0d0d0",
}
_active = "dark"


def default_theme(environ: dict[str, str] | None = None) -> str:
    environment = os.environ if environ is None else environ
    if environment.get("NO_COLOR"):
        return "mono"
    chosen = environment.get("MINEBOT_THEME", "").strip().lower()
    return chosen if chosen in THEMES else "dark"


def set_theme(name: str) -> str:
    global _active
    _active = name if name in THEMES else "dark"
    themed.cache_clear()
    return _active


def active_theme() -> str:
    return _active


def background() -> str:
    if _active == "light":
        return _LIGHT[BACKGROUND]
    if _active == "contrast":
        return _CONTRAST[BACKGROUND]
    return BACKGROUND


@lru_cache(maxsize=4096)
def themed(style: Style | None) -> Style | None:
    """``style`` as the active theme paints it."""
    if style is None or _active == "dark":
        return style
    if _active == "mono":
        dim = style.color is not None and style.color.triplet is not None and \
            style.color.triplet.hex in (TONE["dim"], TONE["muted"])
        return Style(bold=style.bold, dim=dim or None, reverse=True if style.bgcolor is not None else None)
    table = _LIGHT if _active == "light" else _CONTRAST
    color = style.color.triplet.hex if style.color is not None and style.color.triplet is not None else None
    bgcolor = style.bgcolor.triplet.hex if style.bgcolor is not None and style.bgcolor.triplet is not None else None
    new_color = (table.get(color) or _mapped(color, table)) if color else None
    new_bg = (table.get(bgcolor) or _mapped(bgcolor, table)) if bgcolor else None
    if _active == "light" and new_bg is not None:
        new_bg = blend(new_bg, table[BACKGROUND], 0.75) if bgcolor not in table else new_bg
    return style + Style(color=new_color, bgcolor=new_bg)


def _mapped(color: str, table: dict[str, str]) -> str:
    """Colours made by :func:`blend` (dimmed layers, faded log lines) aren't in the table."""
    base = min((*LAYER.values(), *TONE.values()), key=lambda candidate: _distance(candidate, color))
    if table is _CONTRAST:
        return table.get(base, base)  # high contrast: no fading, every colour at full strength
    # Re-derive in the light palette: the brightness distance from the dark background
    # becomes the distance from the light background.
    amount = _amount(color, base) * 0.7  # fades read weaker on a pale page, so fade less
    return blend(table.get(base, base), table[BACKGROUND], amount)


def _rgb(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def _distance(a: str, b: str) -> float:
    x, y = _rgb(a), _rgb(b)
    # Compare hue direction, not brightness: normalize both toward the background first.
    bg = _rgb(BACKGROUND)
    vx = [c - g for c, g in zip(x, bg)]
    vy = [c - g for c, g in zip(y, bg)]
    nx = sum(v * v for v in vx) ** 0.5 or 1.0
    ny = sum(v * v for v in vy) ** 0.5 or 1.0
    return sum((p / nx - q / ny) ** 2 for p, q in zip(vx, vy))


def _amount(color: str, base: str) -> float:
    bg = _rgb(BACKGROUND)
    full = sum((c - g) ** 2 for c, g in zip(_rgb(base), bg)) ** 0.5 or 1.0
    part = sum((c - g) ** 2 for c, g in zip(_rgb(color), bg)) ** 0.5
    return max(0.0, min(1.0, 1 - part / full))


set_theme(default_theme())
