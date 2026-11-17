"""Minecraft harvest-tier facts shared by Brain and Body code."""

from __future__ import annotations

TOOL_TIER_ORDER = ("wooden", "stone", "iron", "diamond", "netherite")

PICKAXE_BY_TIER = {
    "wooden": "wooden_pickaxe",
    "stone": "stone_pickaxe",
    "iron": "iron_pickaxe",
    "diamond": "diamond_pickaxe",
    "netherite": "netherite_pickaxe",
}

MIN_PICKAXE_TIER = {
    "stone": "wooden",
    "cobblestone": "wooden",
    "deepslate": "wooden",
    "cobbled_deepslate": "wooden",
    "coal_ore": "wooden",
    "deepslate_coal_ore": "wooden",
    "andesite": "wooden",
    "diorite": "wooden",
    "granite": "wooden",
    "tuff": "wooden",
    "blackstone": "wooden",
    "netherrack": "wooden",
    "sandstone": "wooden",
    "furnace": "wooden",
    "blast_furnace": "wooden",
    "smoker": "wooden",
    "iron_ore": "stone",
    "deepslate_iron_ore": "stone",
    "copper_ore": "stone",
    "deepslate_copper_ore": "stone",
    "lapis_ore": "stone",
    "deepslate_lapis_ore": "stone",
    "raw_iron_block": "stone",
    "raw_copper_block": "stone",
    "diamond_ore": "iron",
    "deepslate_diamond_ore": "iron",
    "gold_ore": "iron",
    "deepslate_gold_ore": "iron",
    "nether_gold_ore": "iron",
    "redstone_ore": "iron",
    "deepslate_redstone_ore": "iron",
    "emerald_ore": "iron",
    "deepslate_emerald_ore": "iron",
    "raw_gold_block": "iron",
    "obsidian": "diamond",
    "crying_obsidian": "diamond",
    "ancient_debris": "diamond",
}

_PICKAXE_TIER_BY_ITEM = {
    "wooden_pickaxe": "wooden",
    "golden_pickaxe": "wooden",
    "stone_pickaxe": "stone",
    "iron_pickaxe": "iron",
    "diamond_pickaxe": "diamond",
    "netherite_pickaxe": "netherite",
}

_TIER_RANK = {tier: index for index, tier in enumerate(TOOL_TIER_ORDER)}


def required_pickaxe_tier(block_type: str) -> str | None:
    return MIN_PICKAXE_TIER.get(_plain_name(block_type))


def best_owned_pickaxe(counts: dict[str, int]) -> tuple[str, str] | None:
    best: tuple[str, str] | None = None
    for raw_item, raw_count in counts.items():
        try:
            count = int(raw_count or 0)
        except (TypeError, ValueError):
            continue
        if count <= 0:
            continue
        item = _plain_name(str(raw_item))
        tier = _PICKAXE_TIER_BY_ITEM.get(item)
        if tier is None:
            continue
        if best is None or _TIER_RANK[tier] > _TIER_RANK[best[1]]:
            best = (item, tier)
    return best


def tier_satisfies(owned_tier: str | None, required_tier: str) -> bool:
    if owned_tier is None:
        return False
    owned = _normalize_tier(owned_tier)
    required = _normalize_tier(required_tier)
    if owned is None or required is None:
        return False
    return _TIER_RANK[owned] >= _TIER_RANK[required]


def _normalize_tier(tier: str) -> str | None:
    lowered = str(tier).removeprefix("minecraft:").strip().lower()
    if lowered == "golden":
        return "wooden"
    return lowered if lowered in _TIER_RANK else None


def _plain_name(item: str) -> str:
    return str(item).removeprefix("minecraft:").strip().lower()
