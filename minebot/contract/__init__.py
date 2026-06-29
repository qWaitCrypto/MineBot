"""Neutral Body/Agent contract types.

This package is intentionally type/data only.  Agent-side code and Body-side
transport/transactions can import these names without crossing the
`brain/ -> game/` boundary.
"""

from .governance import BotPlacement, BreakContext, InteractionContext, LegalityDecision, PlaceContext, Position, Region
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
    "PerceptionResult",
    "PlaceContext",
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
    "is_candidate_skip",
    "terminal_event_to_tool_result",
]
