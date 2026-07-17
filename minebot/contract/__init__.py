"""Neutral Body/Agent contract types.

This package is intentionally type/data only.  Agent-side code and Body-side
transport/transactions can import these names without crossing the
`brain/ -> game/` boundary.
"""

from .governance import (
    BotPlacement,
    BreakContext,
    InteractionContext,
    LegalityDecision,
    PlaceContext,
    Position,
    Region,
    StructureRiskAssessment,
    StructureRiskLevel,
)
from .harvest import (
    MIN_PICKAXE_TIER,
    PICKAXE_BY_TIER,
    TOOL_TIER_ORDER,
    best_owned_pickaxe,
    required_pickaxe_tier,
    tier_satisfies,
)
from .messages import (
    Action,
    BodyState,
    CANDIDATE_SKIP_PREFIXES,
    CANDIDATE_SKIP_REASONS,
    Event,
    InventorySlot,
    JsonObject,
    PerceptionResult,
    Result,
    ToolResult,
    is_candidate_skip,
    perception_next_cursor,
)
from .body_iface import Body
from .progress import (
    FAILURE_STORM_LIMIT,
    LocalProgressController,
    ProgressAbort,
    ProgressController,
    ProgressFacts,
    STAGNATION_LIMIT,
    STALL_LIMIT,
)
from .results import terminal_event_to_tool_result

__all__ = [
    "Action",
    "Body",
    "BodyState",
    "BotPlacement",
    "BreakContext",
    "CANDIDATE_SKIP_PREFIXES",
    "CANDIDATE_SKIP_REASONS",
    "InteractionContext",
    "Event",
    "InventorySlot",
    "JsonObject",
    "LegalityDecision",
    "MIN_PICKAXE_TIER",
    "PerceptionResult",
    "perception_next_cursor",
    "PlaceContext",
    "PICKAXE_BY_TIER",
    "Position",
    "ProgressAbort",
    "ProgressController",
    "ProgressFacts",
    "LocalProgressController",
    "FAILURE_STORM_LIMIT",
    "Region",
    "Result",
    "ToolResult",
    "STAGNATION_LIMIT",
    "STALL_LIMIT",
    "StructureRiskAssessment",
    "StructureRiskLevel",
    "TOOL_TIER_ORDER",
    "best_owned_pickaxe",
    "is_candidate_skip",
    "required_pickaxe_tier",
    "tier_satisfies",
    "terminal_event_to_tool_result",
]
