"""Character-grid canvas: boxes, orthogonal wires, moving dots, bars and braille.

Cell widths come from ``rich.cells`` so CJK text lines up with box borders.
The canvas renders to a ``rich.text.Text`` that a Textual widget displays.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from rich.cells import cell_len, get_character_cell_size
from rich.style import Style
from rich.text import Text

Point = tuple[int, int]

BOX_SOLID = "┌┐└┘─│"
BOX_ROUND = "╭╮╰╯─│"
BOX_DASHED = "┌┐└┘╌╎"
BOX_HEAVY = "┏┓┗┛━┃"
SPARK = "▁▂▃▄▅▆▇█"
EIGHTHS = " ▏▎▍▌▋▊▉█"
_WIDE_FILLER = ""


class Canvas:
    def __init__(self, width: int, height: int, background: Style | None = None) -> None:
        self.width = max(1, width)
        self.height = max(1, height)
        self.chars = [[" "] * self.width for _ in range(self.height)]
        self.styles: list[list[Style | None]] = [[background] * self.width for _ in range(self.height)]

    # -------------------------------------------------------------- primitives
    def inside(self, x: int, y: int) -> bool:
        return 0 <= x < self.width and 0 <= y < self.height

    def put(self, x: int, y: int, char: str, style: Style | str | None = None) -> None:
        if not self.inside(x, y):
            return
        self._clear_wide(x, y)
        self.chars[y][x] = char
        self.styles[y][x] = _style(style)

    def _clear_wide(self, x: int, y: int) -> None:
        row = self.chars[y]
        if row[x] == _WIDE_FILLER and x > 0:
            row[x - 1] = " "
        if x + 1 < self.width and row[x + 1] == _WIDE_FILLER:
            row[x + 1] = " "

    def text(self, x: int, y: int, value: str, style: Style | str | None = None,
             max_width: int | None = None) -> int:
        """Write ``value`` at (x, y); returns the cells used.  Truncates with an ellipsis."""
        if not 0 <= y < self.height:
            return 0
        limit = self.width - x if max_width is None else min(max_width, self.width - x)
        if limit <= 0:
            return 0
        value = fit(value, limit)
        used = 0
        resolved = _style(style)
        for char in value:
            size = get_character_cell_size(char)
            if size == 0:
                continue
            cx = x + used
            if cx < 0:
                used += size
                continue
            if cx + size > self.width:
                break
            self._clear_wide(cx, y)
            self.chars[y][cx] = char
            self.styles[y][cx] = resolved
            if size == 2:
                if cx + 1 < self.width:
                    self._clear_wide(cx + 1, y)
                    self.chars[y][cx + 1] = _WIDE_FILLER
                    self.styles[y][cx + 1] = resolved
            used += size
        return used

    def runs(self, x: int, y: int, parts: Iterable[tuple[str, Style | str | None]], max_width: int | None = None) -> int:
        """Write consecutive styled fragments; returns the cells used."""
        limit = self.width - x if max_width is None else max_width
        used = 0
        for value, style in parts:
            if used >= limit:
                break
            used += self.text(x + used, y, value, style, limit - used)
        return used

    def center(self, x: int, y: int, width: int, value: str, style: Style | str | None = None) -> None:
        value = fit(value, width)
        self.text(x + max(0, (width - cell_len(value)) // 2), y, value, style)

    def fill(self, x: int, y: int, width: int, height: int, char: str = " ", style: Style | str | None = None) -> None:
        for row in range(y, y + height):
            for col in range(x, x + width):
                self.put(col, row, char, style)

    def box(self, x: int, y: int, width: int, height: int, style: Style | str | None = None,
            title: str | Sequence[tuple[str, Style | str | None]] | None = None,
            title_style: Style | str | None = None, chars: str = BOX_ROUND, clear: bool = True) -> None:
        if width < 2 or height < 2:
            return
        tl, tr, bl, br, horizontal, vertical = chars
        if clear:
            self.fill(x + 1, y + 1, width - 2, height - 2)
        for col in range(x + 1, x + width - 1):
            self.put(col, y, horizontal, style)
            self.put(col, y + height - 1, horizontal, style)
        for row in range(y + 1, y + height - 1):
            self.put(x, row, vertical, style)
            self.put(x + width - 1, row, vertical, style)
        self.put(x, y, tl, style)
        self.put(x + width - 1, y, tr, style)
        self.put(x, y + height - 1, bl, style)
        self.put(x + width - 1, y + height - 1, br, style)
        if title:
            parts = [(title, title_style or style)] if isinstance(title, str) else list(title)
            self.put(x + 1, y, horizontal, style)
            used = self.runs(x + 2, y, [(" ", None), *parts, (" ", None)], width - 4)
            del used

    def hline(self, x1: int, x2: int, y: int, style: Style | str | None = None, char: str = "─") -> None:
        for col in range(min(x1, x2), max(x1, x2) + 1):
            self.put(col, y, char, style)

    def vline(self, x: int, y1: int, y2: int, style: Style | str | None = None, char: str = "│") -> None:
        for row in range(min(y1, y2), max(y1, y2) + 1):
            self.put(x, row, char, style)

    def wire(self, points: Sequence[Point], style: Style | str | None = None, dashed: bool = False,
             arrow: str | None = None) -> list[Point]:
        """Draw an orthogonal polyline and return its cells in travel order."""
        cells = route(points)
        horizontal, vertical = ("╌", "╎") if dashed else ("─", "│")
        for index, (cx, cy) in enumerate(cells):
            previous = cells[index - 1] if index else None
            following = cells[index + 1] if index + 1 < len(cells) else None
            self.put(cx, cy, _wire_char((cx, cy), previous, following, horizontal, vertical), style)
        if arrow and cells:
            end = cells[-1]
            before = cells[-2] if len(cells) > 1 else end
            head = {(1, 0): "▶", (-1, 0): "◀", (0, 1): "▼", (0, -1): "▲"}.get(
                (_sign(end[0] - before[0]), _sign(end[1] - before[1])), arrow)
            self.put(end[0], end[1], head, style)
        return cells

    def dot(self, cells: Sequence[Point], progress: float, style: Style | str | None, trail_style: Style | str | None,
            trail: int = 3, head: str = "●") -> None:
        """A packet at ``progress`` (0..1) along ``cells`` with a fading trail behind it."""
        if not cells:
            return
        position = int(progress * (len(cells) - 1) + 0.5)
        position = max(0, min(len(cells) - 1, position))
        for offset, char in zip(range(trail, 0, -1), "·•∙"[-trail:] if trail <= 3 else "·" * trail):
            index = position - offset
            if 0 <= index < len(cells):
                self.put(cells[index][0], cells[index][1], char, trail_style)
        self.put(cells[position][0], cells[position][1], head, style)

    def bar(self, x: int, y: int, width: int, fraction: float, fill: Style | str | None,
            empty: Style | str | None = None, empty_char: str = "░") -> None:
        fraction = max(0.0, min(1.0, fraction))
        exact = fraction * width
        full = int(exact)
        for col in range(width):
            if col < full:
                self.put(x + col, y, "█", fill)
            elif col == full and exact - full > 0.0625:
                self.put(x + col, y, EIGHTHS[int((exact - full) * 8)], fill)
            else:
                self.put(x + col, y, empty_char, empty)

    def sparkline(self, x: int, y: int, width: int, values: Sequence[float | None], style: Style | str | None,
                  low: float | None = None, high: float | None = None) -> None:
        values = list(values)[-width:]
        present = [value for value in values if value is not None]
        if not present:
            return
        low = min(present) if low is None else low
        high = max(present) if high is None else high
        span = (high - low) or 1.0
        start = x + width - len(values)
        for index, value in enumerate(values):
            if value is None:
                continue
            level = int((value - low) / span * (len(SPARK) - 1) + 0.5)
            self.put(start + index, y, SPARK[max(0, min(len(SPARK) - 1, level))], style)

    def to_text(self) -> Text:
        text = Text(no_wrap=True, overflow="crop", end="")
        for row_index in range(self.height):
            chars, styles = self.chars[row_index], self.styles[row_index]
            run_style, run = styles[0] if styles else None, []
            for char, style in zip(chars, styles):
                if char == _WIDE_FILLER:
                    continue
                if style is not run_style and style != run_style:
                    if run:
                        text.append("".join(run), run_style)
                    run, run_style = [], style
                run.append(char)
            if run:
                text.append("".join(run), run_style)
            if row_index + 1 < self.height:
                text.append("\n")
        return text

    def plain_rows(self) -> list[str]:
        return ["".join(char for char in row if char != _WIDE_FILLER) for row in self.chars]


class Braille:
    """A 2x4-dots-per-cell bitmap that is blitted onto a canvas region."""

    _BITS = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))

    def __init__(self, columns: int, rows: int) -> None:
        self.columns, self.rows = columns, rows
        self.cells = [[0] * columns for _ in range(rows)]
        self.colors: list[list[Style | None]] = [[None] * columns for _ in range(rows)]
        self.priority = [[-1] * columns for _ in range(rows)]

    @property
    def pixel_width(self) -> int:
        return self.columns * 2

    @property
    def pixel_height(self) -> int:
        return self.rows * 4

    def set(self, px: int, py: int, style: Style | str | None = None, priority: int = 0) -> None:
        if not (0 <= px < self.pixel_width and 0 <= py < self.pixel_height):
            return
        cx, cy = px // 2, py // 4
        self.cells[cy][cx] |= self._BITS[py % 4][px % 2]
        if priority >= self.priority[cy][cx]:
            self.priority[cy][cx] = priority
            self.colors[cy][cx] = _style(style)

    def blit(self, canvas: Canvas, x: int, y: int) -> None:
        for row in range(self.rows):
            for col in range(self.columns):
                if self.cells[row][col]:
                    canvas.put(x + col, y + row, chr(0x2800 + self.cells[row][col]), self.colors[row][col])


# ------------------------------------------------------------------ helpers

def fit(value: str, width: int) -> str:
    """Truncate ``value`` to ``width`` cells, ending with an ellipsis when cut."""
    value = str(value).replace("\n", " ")
    if width <= 0:
        return ""
    if cell_len(value) <= width:
        return value
    out, used = [], 0
    for char in value:
        size = get_character_cell_size(char)
        if used + size > width - 1:
            break
        out.append(char)
        used += size
    return "".join(out) + "…"


def pad(value: str, width: int) -> str:
    """Fit then right-pad to exactly ``width`` cells."""
    value = fit(value, width)
    return value + " " * max(0, width - cell_len(value))


def wrap(value: str, width: int, lines: int) -> list[str]:
    """Greedy cell-width wrap to at most ``lines`` lines; a cut last line ends with an ellipsis."""
    value = " ".join(str(value).split())
    result: list[str] = []
    current: list[str] = []
    used = 0
    for char in value:
        size = get_character_cell_size(char)
        if used + size > width:
            carry: list[str] = []
            if char.isascii() and char.isalnum() and " " in current[-16:]:
                cut = len(current) - 1 - current[::-1].index(" ")
                current, carry = current[:cut], current[cut + 1:]
            result.append("".join(current))
            if len(result) == lines:
                result[-1] = fit(result[-1] + "…", width)
                return result
            current, used = carry, cell_len("".join(carry))
        current.append(char)
        used += size
    if current:
        result.append("".join(current))
    return result


def route(points: Sequence[Point]) -> list[Point]:
    cells: list[Point] = []
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        steps = max(abs(x2 - x1), abs(y2 - y1))
        dx, dy = _sign(x2 - x1), _sign(y2 - y1)
        for step in range(steps + 1):
            cell = (x1 + dx * step, y1 + dy * step)
            if not cells or cells[-1] != cell:
                cells.append(cell)
    if len(points) == 1:
        cells.append(points[0])
    return cells


def _wire_char(cell: Point, previous: Point | None, following: Point | None, horizontal: str, vertical: str) -> str:
    directions = set()
    for other in (previous, following):
        if other is not None:
            directions.add((_sign(other[0] - cell[0]), _sign(other[1] - cell[1])))
    if directions <= {(-1, 0), (1, 0)}:
        return horizontal
    if directions <= {(0, -1), (0, 1)}:
        return vertical
    corners = {
        frozenset({(1, 0), (0, 1)}): "┌",
        frozenset({(-1, 0), (0, 1)}): "┐",
        frozenset({(1, 0), (0, -1)}): "└",
        frozenset({(-1, 0), (0, -1)}): "┘",
    }
    return corners.get(frozenset(directions), horizontal)


def _sign(value: int) -> int:
    return (value > 0) - (value < 0)


_STYLE_CACHE: dict[str, Style] = {}


def _style(style: Style | str | None) -> Style | None:
    if style is None or isinstance(style, Style):
        return style
    cached = _STYLE_CACHE.get(style)
    if cached is None:
        cached = _STYLE_CACHE[style] = Style.parse(style)
    return cached


__all__ = ["BOX_DASHED", "BOX_HEAVY", "BOX_ROUND", "BOX_SOLID", "Braille", "Canvas", "fit", "pad", "route", "wrap"]
