"""Shared authoritative world-read helpers for Body transactions."""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic

from minebot.contract import Body, PerceptionResult, Position
from minebot.game.navigation import GridCell


@dataclass(frozen=True)
class BlockCellRead:
    cells: dict[Position, GridCell]
    diagnostics: dict[str, object]


def read_block_cells_tiled(
    body: Body,
    positions: tuple[Position, ...],
    *,
    tile_width: int = 4,
    tile_depth: int = 4,
    max_tiles: int = 16,
    failure_label: str = "read",
) -> BlockCellRead:
    """Read authoritative `blockAt` facts over tiled positions.

    This is a reusable Body-side world-read substrate: callers choose the
    positions and consume the returned cells/diagnostics, while the read path
    itself owns tiling, bounded budgets, truthful perception failure, and
    per-tile metrics.
    """

    if tile_width < 1:
        raise ValueError("tile_width must be >= 1")
    if tile_depth < 1:
        raise ValueError("tile_depth must be >= 1")
    if max_tiles < 1:
        raise ValueError("max_tiles must be >= 1")

    tiles = block_read_tiles(
        positions,
        tile_width=tile_width,
        tile_depth=tile_depth,
    )
    if len(tiles) > max_tiles:
        raise ValueError(f"authoritative block read exceeds max_tiles: {len(tiles)} > {max_tiles}")

    started = monotonic()
    read_cells: dict[Position, GridCell] = {}
    clear_count = 0
    solid_count = 0
    liquid_count = 0
    tile_summaries: list[dict[str, object]] = []

    for tile_index, tile in enumerate(tiles):
        tile_started = monotonic()
        tile_clear = 0
        tile_solid = 0
        tile_liquid = 0
        for pos in tile:
            perception = body.perceive("blockAt", {"x": pos[0], "y": pos[1], "z": pos[2]})
            failure = block_perception_failure(perception, pos, label=failure_label)
            if failure is not None:
                raise ValueError(failure)
            cell = grid_cell_from_block_perception(perception)
            read_cells[pos] = cell
            if cell.liquid:
                liquid_count += 1
                tile_liquid += 1
            elif cell.walkable:
                clear_count += 1
                tile_clear += 1
            else:
                solid_count += 1
                tile_solid += 1
        tile_summaries.append(
            {
                "index": tile_index,
                "cells": len(tile),
                "clear_cells": tile_clear,
                "solid_cells": tile_solid,
                "liquid_cells": tile_liquid,
                "elapsed_ms": round((monotonic() - tile_started) * 1000.0, 3),
                "bounds": positions_bounds(tile),
            }
        )

    diagnostics = {
        "complete": True,
        "refreshed_cells": len(positions),
        "clear_cells": clear_count,
        "solid_cells": solid_count,
        "liquid_cells": liquid_count,
        "tile_width": tile_width,
        "tile_depth": tile_depth,
        "max_tiles": max_tiles,
        "tile_count": len(tiles),
        "tiles": tile_summaries,
        "elapsed_ms": round((monotonic() - started) * 1000.0, 3),
    }
    return BlockCellRead(cells=read_cells, diagnostics=diagnostics)


def block_read_tiles(
    positions: tuple[Position, ...],
    *,
    tile_width: int,
    tile_depth: int,
) -> tuple[tuple[Position, ...], ...]:
    buckets: dict[tuple[int, int], list[Position]] = {}
    for pos in positions:
        key = (pos[0] // tile_width, pos[2] // tile_depth)
        buckets.setdefault(key, []).append(pos)
    ordered = sorted(buckets.items(), key=lambda item: (item[0][0], item[0][1]))
    return tuple(tuple(tile_positions) for _key, tile_positions in ordered)


def positions_bounds(positions: tuple[Position, ...]) -> dict[str, object]:
    xs = [pos[0] for pos in positions]
    ys = [pos[1] for pos in positions]
    zs = [pos[2] for pos in positions]
    return {
        "x": [min(xs), max(xs)],
        "y": [min(ys), max(ys)],
        "z": [min(zs), max(zs)],
    }


def block_perception_failure(
    perception: PerceptionResult,
    pos: Position,
    *,
    label: str = "read",
) -> str | None:
    if perception.ok and getattr(perception, "complete", False):
        return None
    return (
        f"blockAt {label} failed at {list(pos)}: ok={getattr(perception, 'ok', None)} "
        f"complete={getattr(perception, 'complete', None)} error={getattr(perception, 'error', None)} "
        f"uncertainty={getattr(perception, 'uncertainty', None)}"
    )


def grid_cell_from_block_perception(perception: PerceptionResult) -> GridCell:
    state = str((perception.data or {}).get("state") or "")
    block_type = str((perception.data or {}).get("type") or "air")
    properties = {str(key): str(value).lower() for key, value in dict((perception.data or {}).get("properties") or {}).items()}
    if state == "LIQUID":
        return GridCell(block_type=block_type, walkable=True, liquid=True)
    if state == "SOLID":
        normalized = block_type.removeprefix("minecraft:")
        if _is_walkthrough_openable(normalized, properties):
            return GridCell(
                block_type=block_type,
                walkable=True,
                headroom_block=block_type if normalized.endswith("_door") else None,
            )
        return GridCell(block_type=block_type, walkable=False)
    return GridCell(block_type=block_type, walkable=True)


def _is_walkthrough_openable(block_type: str, properties: dict[str, str]) -> bool:
    if properties.get("open") != "true":
        return False
    return (
        block_type.endswith("_door")
        or block_type.endswith("_trapdoor")
        or block_type.endswith("_fence_gate")
    )
