"""Authoritative local-voxel evidence for player-structure risk."""

from __future__ import annotations

from collections import Counter

from minebot.body.world_read import read_block_facts
from minebot.contract import (
    Body,
    BreakContext,
    Position,
    StructureRiskAssessment,
    StructureRiskLevel,
)
from minebot.game.governance import STRONGLY_PROTECTED_TYPES, normalize_block_type


_CLEAR_TYPES = frozenset({"air", "cave_air", "void_air", "water", "lava"})
_ROOT_TYPES = frozenset({"dirt", "grass_block", "podzol", "coarse_dirt", "rooted_dirt", "mud"})
_ORE_SUFFIXES = ("_ore",)
_MANUFACTURED_EXACT = frozenset(
    {
        "bookshelf",
        "ladder",
        "rail",
        "powered_rail",
        "detector_rail",
        "activator_rail",
        "chain",
        "scaffolding",
        "hay_block",
        "target",
    }
)
_MANUFACTURED_SUFFIXES = (
    "_planks",
    "_stairs",
    "_slab",
    "_fence",
    "_fence_gate",
    "_wall",
    "_door",
    "_trapdoor",
    "_button",
    "_pressure_plate",
    "_sign",
    "_hanging_sign",
    "_wool",
    "_carpet",
    "_concrete",
    "_concrete_powder",
    "_glazed_terracotta",
)


class VoxelStructureRiskAssessor:
    """Classify one proposed break from a bounded authoritative voxel window."""

    def __init__(self, body: Body) -> None:
        self.body = body

    def assess(
        self,
        pos: Position,
        block_type: str,
        context: BreakContext,
    ) -> StructureRiskAssessment:
        positions = _sample_positions(pos)
        normalized_target = normalize_block_type(block_type)
        try:
            facts = read_block_facts(
                self.body,
                positions,
                failure_label="structure_risk",
            )
        except (KeyError, TypeError, ValueError) as exc:
            return StructureRiskAssessment(
                pos=pos,
                block_type=normalized_target,
                level=StructureRiskLevel.AMBIGUOUS,
                score=1.0,
                complete=False,
                sampled_cells=0,
                signals=(f"world_read_failed:{type(exc).__name__}",),
            )

        block_types = {
            sample_pos: normalize_block_type(str(fact.data.get("type") or "unknown"))
            for sample_pos, fact in facts.items()
        }
        observed_target = block_types.get(pos, "unknown")
        if observed_target != normalized_target:
            return StructureRiskAssessment(
                pos=pos,
                block_type=normalized_target,
                level=StructureRiskLevel.AMBIGUOUS,
                score=1.0,
                complete=False,
                sampled_cells=len(block_types),
                signals=(f"target_changed:{observed_target}",),
            )
        return _classify_voxels(pos, normalized_target, BreakContext(context), block_types)


def _sample_positions(pos: Position) -> tuple[Position, ...]:
    x, y, z = pos
    return tuple(
        (x + dx, y + dy, z + dz)
        for dy in range(-3, 4)
        for dx in range(-1, 2)
        for dz in range(-1, 2)
    )


def _classify_voxels(
    pos: Position,
    block_type: str,
    context: BreakContext,
    cells: dict[Position, str],
) -> StructureRiskAssessment:
    signals: list[str] = []
    score = 0.0
    nearby = {sample_pos: value for sample_pos, value in cells.items() if sample_pos != pos}
    manufactured = [value for value in nearby.values() if _is_manufactured(value)]
    protected = [value for value in nearby.values() if value in STRONGLY_PROTECTED_TYPES]
    if protected:
        score += 0.8
        signals.append(f"functional_blocks:{len(protected)}")
    elif manufactured:
        score += min(0.85, 0.65 + 0.04 * len(manufactured))
        signals.append(f"manufactured_blocks:{len(manufactured)}")

    if block_type.endswith("_log"):
        score += _log_risk(pos, block_type, cells, signals)
    elif block_type.endswith(_ORE_SUFFIXES):
        score -= 0.35
        signals.append("ore_natural_prior")
    else:
        score += _regular_surface_risk(pos, block_type, cells, signals)

    if context in {
        BreakContext.TRAVEL,
        BreakContext.COLLECT_APPROACH,
        BreakContext.RECOVERY,
    }:
        score += 0.1
        signals.append(f"conservative_context:{context.value}")
    elif context is BreakContext.COLLECT:
        score -= 0.1
        signals.append("collect_target_prior")

    score = round(max(0.0, min(1.0, score)), 3)
    if score >= 0.65:
        level = StructureRiskLevel.HIGH
    elif score >= 0.3:
        level = StructureRiskLevel.AMBIGUOUS
    else:
        level = StructureRiskLevel.LOW
    palette = Counter(value for value in cells.values() if value not in _CLEAR_TYPES)
    signals.append(f"solid_palette:{len(palette)}")
    return StructureRiskAssessment(
        pos=pos,
        block_type=block_type,
        level=level,
        score=score,
        complete=True,
        sampled_cells=len(cells),
        signals=tuple(signals),
    )


def _log_risk(
    pos: Position,
    block_type: str,
    cells: dict[Position, str],
    signals: list[str],
) -> float:
    x, y, z = pos
    vertical = sum(cells.get((x, y + dy, z)) == block_type for dy in (-2, -1, 1, 2))
    horizontal = sum(
        cells.get((x + dx, y, z + dz)) == block_type
        for dx, dz in ((-1, 0), (1, 0), (0, -1), (0, 1))
    )
    rooted = any(cells.get((x, y + dy, z)) in _ROOT_TYPES for dy in (-3, -2, -1))
    leaf_count = sum(value.endswith("_leaves") for value in cells.values())
    risk = 0.0
    if vertical and rooted:
        risk -= 0.45
        signals.append("rooted_vertical_log")
    elif vertical >= 2:
        risk -= 0.2
        signals.append("vertical_log_column")
    if leaf_count:
        risk -= 0.35
        signals.append(f"nearby_leaves:{leaf_count}")
    if horizontal > vertical:
        risk += 0.35
        signals.append("horizontal_log_pattern")
    if not vertical and not rooted and not leaf_count:
        risk += 0.35
        signals.append("log_without_tree_evidence")
    return risk


def _regular_surface_risk(
    pos: Position,
    block_type: str,
    cells: dict[Position, str],
    signals: list[str],
) -> float:
    x, y, z = pos
    vertical_column = sum(cells.get((x, y + dy, z)) == block_type for dy in range(-3, 4))
    horizontal_same = sum(
        cells.get((x + dx, y, z + dz)) == block_type
        for dx, dz in ((-1, 0), (1, 0), (0, -1), (0, 1))
    )
    clear_sides = sum(
        cells.get((x + dx, y, z + dz)) in _CLEAR_TYPES
        for dx, dz in ((-1, 0), (1, 0), (0, -1), (0, 1))
    )
    if vertical_column >= 4 and horizontal_same == 0 and clear_sides >= 3:
        signals.append("isolated_vertical_column")
        return 0.55

    clear_west = cells.get((x - 1, y, z)) in _CLEAR_TYPES
    clear_east = cells.get((x + 1, y, z)) in _CLEAR_TYPES
    clear_north = cells.get((x, y, z - 1)) in _CLEAR_TYPES
    clear_south = cells.get((x, y, z + 1)) in _CLEAR_TYPES
    x_plane = sum(
        cells.get((x, y + dy, z + dz)) == block_type
        for dy in range(-3, 4)
        for dz in range(-1, 2)
    )
    z_plane = sum(
        cells.get((x + dx, y + dy, z)) == block_type
        for dy in range(-3, 4)
        for dx in range(-1, 2)
    )
    if ((clear_west or clear_east) and x_plane >= 12) or (
        (clear_north or clear_south) and z_plane >= 12
    ):
        signals.append("exposed_regular_plane")
        return 0.35
    mirrored = sum(
        cells.get((x - dx, y + dy, z - dz))
        == cells.get((x + dx, y + dy, z + dz))
        not in _CLEAR_TYPES
        for dx, dz in ((1, 0), (0, 1), (1, 1), (1, -1))
        for dy in (-1, 0, 1)
    )
    if mirrored >= 8:
        signals.append("local_axis_symmetry")
        return 0.2
    return 0.0


def _is_manufactured(block_type: str) -> bool:
    return (
        block_type in STRONGLY_PROTECTED_TYPES
        or block_type in _MANUFACTURED_EXACT
        or block_type.endswith(_MANUFACTURED_SUFFIXES)
    )


__all__ = ["VoxelStructureRiskAssessor"]
