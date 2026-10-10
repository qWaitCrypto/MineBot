"""One admission check for native Body processes that change the world.

A native process (Baritone mining, building, clearing, farming) runs to its
own terminal on the client, so the per-block gates in ``BlockWork`` never see
its blocks. What can be decided before it starts is decided here, once, for
every such process:

- the harvest tier the target blocks need is owned;
- a declared work volume lies inside a natural work region.

What cannot be decided up front is not pretended: the client chooses the
individual blocks, so "this block was placed by a player" is not covered here.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from minebot.contract import Body, Position, ToolResult
from minebot.contract.harvest import (
    PICKAXE_BY_TIER,
    TOOL_TIER_ORDER,
    best_owned_pickaxe,
    required_pickaxe_tier,
    tier_satisfies,
)


def required_tier_for(block_types: Iterable[str]) -> str | None:
    """Highest pickaxe tier any of the block types needs, or None."""
    tiers = [tier for tier in (required_pickaxe_tier(block) for block in block_types) if tier]
    return max(tiers, key=TOOL_TIER_ORDER.index) if tiers else None


def harvest_tool_denial(body: Body, block_types: Iterable[str]) -> ToolResult | None:
    """None when the owned tools can harvest every block type; else the refusal."""
    blocks = tuple(block_types)
    required = required_tier_for(blocks)
    if required is None:
        return None
    counts = dict(body.get_state().inventory_counts or {})
    best = best_owned_pickaxe(counts)
    if best is not None and tier_satisfies(best[1], required):
        return None
    return ToolResult(
        success=False,
        reason="missing_required_tool",
        can_retry=False,
        next_suggestion=f"craft a {PICKAXE_BY_TIER[required]} or better first",
        metrics={
            "block_types": list(blocks),
            "required_tier": required,
            "best_owned": None if best is None else {"item": best[0], "tier": best[1]},
        },
    )


def work_volume_denial(
    natural_regions: Sequence[object], low: Position, high: Position
) -> ToolResult | None:
    """None when the cuboid lies inside one natural region; else the refusal.

    No configured region means no region governance, as for per-block work.
    """
    if not natural_regions:
        return None
    lo = tuple(min(low[i], high[i]) for i in range(3))
    hi = tuple(max(low[i], high[i]) for i in range(3))
    if any(region.contains(lo) and region.contains(hi) for region in natural_regions):
        return None
    return ToolResult(
        success=False,
        reason="outside_natural_region",
        can_retry=False,
        next_suggestion="choose a work volume inside the natural work region",
        metrics={"low": list(lo), "high": list(hi)},
    )


__all__ = ["harvest_tool_denial", "required_tier_for", "work_volume_denial"]
