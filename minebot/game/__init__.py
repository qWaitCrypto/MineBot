"""Game/body client primitives.

Legacy Scarpet/RCON exports stay available to explicit debug and archive
callers, but are loaded lazily so the Java-only production process does not
import either transport.
"""

from minebot.contract import (
    Action,
    BodyState,
    BotPlacement,
    BreakContext,
    Event,
    InteractionContext,
    InventorySlot,
    LegalityDecision,
    PerceptionResult,
    PlaceContext,
    Region,
    Result,
)
from minebot.game.governance import GovernancePolicy


def __getattr__(name: str):
    if name == "ScarpetBody":
        from minebot.game.body import ScarpetBody

        return ScarpetBody
    if name == "RconClient":
        from minebot.game.rcon import RconClient

        return RconClient
    raise AttributeError(name)

__all__ = [
    "Action",
    "BotPlacement",
    "BreakContext",
    "BodyState",
    "Event",
    "InventorySlot",
    "GovernancePolicy",
    "InteractionContext",
    "LegalityDecision",
    "PlaceContext",
    "PerceptionResult",
    "Region",
    "Result",
    "RconClient",
    "ScarpetBody",
]
