"""Pure governance contract types shared across layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


Position = tuple[int, int, int]


class BreakContext(StrEnum):
    PATH = "path"
    TRAVEL = "travel"
    COLLECT = "collect"
    FARM = "farm"
    RECOVERY = "recovery"
    DIRECT = "direct"
    BOT_CLEANUP = "bot_cleanup"


class PlaceContext(StrEnum):
    TRAVEL = "travel"
    WORK = "work"
    FARM = "farm"
    RECOVERY = "recovery"
    DIRECT = "direct"


class InteractionContext(StrEnum):
    ACTIVATE = "activate"
    SLEEP = "sleep"
    FARM = "farm"


@dataclass(frozen=True)
class Region:
    """Inclusive axis-aligned region."""

    name: str
    min_pos: Position
    max_pos: Position

    def contains(self, pos: Position) -> bool:
        return all(self.min_pos[i] <= pos[i] <= self.max_pos[i] for i in range(3))


@dataclass(frozen=True)
class BotPlacement:
    pos: Position
    block_type: str
    purpose: str
    bot: str


@dataclass(frozen=True)
class LegalityDecision:
    allowed: bool
    reason: str
    protected: bool = False
    bot_owned: bool = False
    natural_region: str | None = None
    details: dict[str, object] = field(default_factory=dict)
