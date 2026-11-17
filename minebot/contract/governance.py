"""Pure governance contract types shared across layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


Position = tuple[int, int, int]


class BreakContext(StrEnum):
    PATH = "path"
    TRAVEL = "travel"
    COLLECT = "collect"
    # COLLECT_APPROACH: "dig over to a collect target". Unlike COLLECT (which
    # only permits breaking the target TYPE), this permits breaking the natural
    # blocks ON THE WAY to a target so the navigator can clear a path to a buried
    # block — like TRAVEL's allowed_natural, but TYPE-gated without requiring a
    # declared region (TRAVEL refuses undeclared terrain). The red line is
    # unchanged: player functional blocks (STRONGLY_PROTECTED_TYPES),
    # protected_regions, and the bot ledger still deny. Bounded by the
    # navigator's max_break_steps so it cannot tunnel indefinitely.
    COLLECT_APPROACH = "collect_approach"
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


class StructureRiskLevel(StrEnum):
    LOW = "low"
    AMBIGUOUS = "ambiguous"
    HIGH = "high"


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
class StructureRiskAssessment:
    pos: Position
    block_type: str
    level: StructureRiskLevel
    score: float
    complete: bool
    sampled_cells: int
    signals: tuple[str, ...] = ()
    source: str = "voxel"


@dataclass(frozen=True)
class LegalityDecision:
    allowed: bool
    reason: str
    protected: bool = False
    bot_owned: bool = False
    natural_region: str | None = None
    details: dict[str, object] = field(default_factory=dict)
