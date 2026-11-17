"""Counter motion: a number that changes glides from its old value to the new one.

Views are pure functions of a frame, so the remembered old values live here, owned by
the app and handed to views through :attr:`Frame.tweens`. Only live panels animate;
replay jumps and the first sighting of a counter show the value at once.
"""

from __future__ import annotations

from dataclasses import dataclass

DURATION_S = 0.3
MARK_S = 2.0  # how long the ▲/▼ after a change stays visible


@dataclass
class _Tween:
    start: float
    end: float
    began: float
    changed: float


class Tweens:
    def __init__(self) -> None:
        self._items: dict[str, _Tween] = {}

    def value(self, key: str, target: float, now: float) -> tuple[float, int]:
        """(value to draw, direction of the last change: 1 up, -1 down, 0 none recently)."""
        item = self._items.get(key)
        if item is None:
            self._items[key] = _Tween(target, target, now, -MARK_S)
            return target, 0
        if target != item.end:
            item.start, item.end, item.began, item.changed = self._at(item, now), target, now, now
        shown = self._at(item, now)
        direction = 0
        if now - item.changed < MARK_S and item.end != item.start:
            direction = 1 if item.end > item.start else -1
        return shown, direction

    @staticmethod
    def _at(item: _Tween, now: float) -> float:
        progress = min(1.0, max(0.0, (now - item.began) / DURATION_S))
        eased = 1 - (1 - progress) ** 3
        return item.start + (item.end - item.start) * eased


__all__ = ["DURATION_S", "MARK_S", "Tweens"]
