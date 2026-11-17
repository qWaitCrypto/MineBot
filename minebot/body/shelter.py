"""Explicit, stationary shelter actions executed and verified by the client."""

from __future__ import annotations

from math import floor, isfinite

from minebot.contract import (
    Action, Body, BreakContext, PlaceContext, ToolResult, action_with_timeout,
    action_wait_timeout_s, body_rejection_to_tool_result, terminal_event_to_tool_result,
)
from minebot.game.governance import GovernancePolicy


class ShelterTransactions:
    def __init__(self, body: Body, governance: GovernancePolicy) -> None:
        self.body = body
        self.governance = governance

    def shelter_in_place(
        self, *, action: str = "seal", side: str | None = None,
        keep_open: bool = False, timeout_s: float = 60.0,
    ) -> ToolResult:
        if action not in {"seal", "look_out"}:
            raise ValueError("action must be seal or look_out")
        if side is not None and side not in {"north", "east", "south", "west"}:
            raise ValueError("side must be north, east, south or west")
        if type(keep_open) is not bool:
            raise ValueError("keep_open must be a boolean")
        if action == "seal" and (side is not None or keep_open):
            raise ValueError("side and keep_open apply only to look_out")
        if (type(timeout_s) not in (int, float) or not isfinite(timeout_s)
                or not 0 < timeout_s <= 120):
            raise ValueError("timeout_s must be > 0 and <= 120")
        state = self.body.get_state()
        center = tuple(floor(v) for v in state.pos)
        x, y, z = center
        # Include the optional foundations and the rim that supports the roof.
        footprint = [(x, y - 1, z), (x, y + 2, z), (x, y + 2, z - 1)]
        footprint += [(x + dx, y + dy, z + dz)
                      for dx, dz in ((0, -1), (1, 0), (0, 1), (-1, 0))
                      for dy in (-1, 0, 1)]
        items = list(state.inventory_counts or {})
        allowed = [{"pos": list(pos), "items": [item for item in items if self.governance.can_place(
            pos, item, PlaceContext.WORK, self.body.bot_name).allowed]} for pos in footprint]
        owned = [{"pos": list(pos), "block_type": entry.block_type}
                 for pos, entry in self.governance.bot_placements.items()
                 if pos in footprint and self.governance.can_break(
                     pos, entry.block_type, BreakContext.BOT_CLEANUP, bot=self.body.bot_name).allowed]
        params = {"action": action, "center": list(center), "keep_open": keep_open,
                  "allowed_placements": allowed, "owned_blocks": owned}
        if side is not None:
            params["side"] = side
        native = action_with_timeout(Action.create("shelterInPlace", params), timeout_s)
        rejected = body_rejection_to_tool_result(self.body.execute(native))
        if rejected is not None:
            return rejected
        terminal = self.body.await_action_terminal(native.id, timeout_s=action_wait_timeout_s(native))
        result = terminal_event_to_tool_result(terminal)
        metrics = {**(result.metrics or {}), "purpose": "shelter"}
        return ToolResult(result.success, result.reason, result.can_retry, metrics=metrics)


def shelter_summary(reason: str, metrics: dict[str, object]) -> dict[str, object]:
    """Expose physical facts without remedy text or a safety verdict."""
    keys = ("center", "open_cells", "blocked_cells", "blocks_needed", "blocks_held",
            "position", "seen", "port", "port_closed", "attacks", "message")
    return {key: metrics[key] for key in keys if key in metrics}
