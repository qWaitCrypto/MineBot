"""Shared authoritative world-read helpers for Body transactions."""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic

from minebot.contract import Body, PerceptionResult, Position, perception_next_cursor
from minebot.game.navigation import GridCell, GridWorld


@dataclass(frozen=True)
class BlockCellRead:
    cells: dict[Position, GridCell]
    diagnostics: dict[str, object]


def read_block_facts(
    body: Body,
    positions: tuple[Position, ...],
    *,
    page_size: int = 64,
    failure_label: str = "read",
) -> dict[Position, PerceptionResult]:
    """Read authoritative ``blockAt`` facts for many positions in one pass.

    Replaces the per-cell ``body.perceive("blockAt", ...)`` loop: bounded
    ``blockCells`` perceptions return the type/state/properties of every
    requested cell. The Python side chunks the outbound request so one large
    position list cannot exceed RCON command limits; the Scarpet side may still
    paginate each chunk on its response-char budget. Each result is wrapped as a
    ``blockAt``-shaped ``PerceptionResult`` so existing predicates consume it
    unchanged.

    Honesty: raises ``ValueError`` on any incomplete perception (matching
    ``read_block_cells_tiled``) rather than seeding callers with invented cells.
    """

    if page_size < 1:
        raise ValueError("page_size must be >= 1")

    cells = [[int(p[0]), int(p[1]), int(p[2])] for p in positions]
    requested = {(int(p[0]), int(p[1]), int(p[2])) for p in positions}
    bot_name = getattr(body, "bot_name", "")
    facts: dict[Position, PerceptionResult] = {}
    offset = 0
    while offset < len(cells):
        request_cells = cells[offset : offset + page_size]
        start = 0
        while start is not None:
            perception = body.perceive(
                "blockCells",
                {"cells": request_cells, "start": start, "limit": page_size},
            )
            absolute_start = offset + start
            if not perception.ok:
                raise ValueError(
                    f"blockCells {failure_label} failed at start={absolute_start}: "
                    f"ok={getattr(perception, 'ok', None)} "
                    f"complete={getattr(perception, 'complete', None)} "
                    f"error={getattr(perception, 'error', None)}"
                )
            response_cells = perception.data.get("cells") or []
            if not isinstance(response_cells, list):
                raise ValueError(f"blockCells {failure_label} returned malformed cells at start={absolute_start}")
            for cell in perception.data.get("cells") or []:
                pos = (int(cell["x"]), int(cell["y"]), int(cell["z"]))
                if pos not in requested:
                    raise ValueError(
                        f"blockCells {failure_label} returned unexpected cell {list(pos)} at start={absolute_start}"
                    )
                facts[pos] = PerceptionResult(
                    bot=bot_name,
                    scope="blockAt",
                    type="perception",
                    ok=True,
                    complete=True,
                    data=dict(cell),
                    uncertainty=[],
                    next=None,
                    error=None,
                )
            nxt = perception_next_cursor(perception, "next", "nextStart")
            if nxt is None:
                if not perception.complete:
                    raise ValueError(f"blockCells {failure_label} incomplete without next at start={absolute_start}")
                start = None
            else:
                next_start = int(nxt)
                if next_start <= start:
                    raise ValueError(
                        f"blockCells {failure_label} did not advance next cursor: start={absolute_start} next={offset + next_start}"
                    )
                if next_start > len(request_cells):
                    raise ValueError(
                        f"blockCells {failure_label} next cursor exceeds request length: "
                        f"next={offset + next_start} total={len(cells)}"
                    )
                start = next_start
        offset += len(request_cells)
    missing = requested.difference(facts)
    if missing:
        sample = sorted(missing)[:5]
        raise ValueError(
            f"blockCells {failure_label} returned {len(facts)}/{len(requested)} requested cells; "
            f"missing={ [list(pos) for pos in sample] }"
        )
    return facts


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
        facts = read_block_facts(body, tile, failure_label=failure_label)
        for pos in tile:
            perception = facts[pos]
            failure = block_perception_failure(perception, pos, label=failure_label)
            if failure is not None:
                raise ValueError(failure)
            cell = grid_cell_from_block_perception(perception)
            if _cell_requires_support(pos, cell, facts):
                cell = GridCell(
                    block_type=cell.block_type,
                    walkable=cell.walkable,
                    liquid=cell.liquid,
                    fall_depth=cell.fall_depth,
                    requires_support=True,
                    headroom_block=cell.headroom_block,
                )
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


def refresh_grid_world_around(
    body: Body,
    world: GridWorld,
    center: Position,
    *,
    h_radius: int = 4,
    y_below: int = 3,
    y_above: int = 3,
    max_tiles: int = 32,
    failure_label: str = "nav_refresh",
) -> dict[str, object]:
    """Read the bot's local terrain and fold it into the planner's GridWorld.

    This is the seam Baritone calls a per-segment local re-read: before each
    navigation segment, the live world around ``center`` (the bot's block feet)
    is read authoritatively and merged into ``world.cells`` so the planner never
    searches over a stale or placeholder grid. Cells accumulate (we never clear
    previously-read terrain), so revisited windows are cheap.

    Honesty: the underlying ``read_block_cells_tiled`` raises on any incomplete
    perception, so a failed refresh propagates as an exception rather than
    seeding the planner with invented cells. The caller converts that into an
    honest navigation terminal, never a silent guess.
    """

    if h_radius < 0:
        raise ValueError("h_radius must be >= 0")
    if y_below < 0:
        raise ValueError("y_below must be >= 0")
    if y_above < 0:
        raise ValueError("y_above must be >= 0")

    cx, cy, cz = int(center[0]), int(center[1]), int(center[2])
    positions: list[Position] = []
    for x in range(cx - h_radius, cx + h_radius + 1):
        for y in range(cy - y_below, cy + y_above + 1):
            for z in range(cz - h_radius, cz + h_radius + 1):
                positions.append((x, y, z))

    read = read_block_cells_tiled(
        body,
        tuple(positions),
        max_tiles=max_tiles,
        failure_label=failure_label,
    )
    world.cells.update(read.cells)
    diagnostics = dict(read.diagnostics)
    diagnostics.update(
        {
            "center": [cx, cy, cz],
            "h_radius": h_radius,
            "y_below": y_below,
            "y_above": y_above,
            "world_cells": len(world.cells),
        }
    )
    return diagnostics


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


def _cell_requires_support(
    pos: Position,
    cell: GridCell,
    facts: dict[Position, PerceptionResult],
) -> bool:
    if not cell.walkable or cell.liquid:
        return False
    below = facts.get((pos[0], pos[1] - 1, pos[2]))
    if below is None:
        return False
    below_cell = grid_cell_from_block_perception(below)
    return below_cell.walkable or below_cell.liquid


def _is_walkthrough_openable(block_type: str, properties: dict[str, str]) -> bool:
    if properties.get("open") != "true":
        return False
    return (
        block_type.endswith("_door")
        or block_type.endswith("_trapdoor")
        or block_type.endswith("_fence_gate")
    )
