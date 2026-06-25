"""Break/place legality for the Body layer.

This module is the shared governance path required by the canonical docs.  It
is intentionally conservative: unknown provenance is protected until a natural
work region, bot placement ledger entry, or explicit future override says
otherwise.
"""

from __future__ import annotations

from typing import Iterable

from minebot.contract.governance import (
    BotPlacement,
    BreakContext,
    InteractionContext,
    LegalityDecision,
    PlaceContext,
    Position,
    Region,
)


NATURAL_BREAKABLE = frozenset(
    {
        "stone",
        "deepslate",
        "dirt",
        "grass_block",
        "gravel",
        "sand",
        "sandstone",
        "clay",
        "netherrack",
        "basalt",
        "blackstone",
        "end_stone",
        "obsidian",
        "cobblestone",
        "oak_log",
        "spruce_log",
        "birch_log",
        "jungle_log",
        "acacia_log",
        "dark_oak_log",
        "mangrove_log",
        "cherry_log",
        "coal_ore",
        "iron_ore",
        "copper_ore",
        "gold_ore",
        "redstone_ore",
        "lapis_ore",
        "diamond_ore",
        "emerald_ore",
        "deepslate_coal_ore",
        "deepslate_iron_ore",
        "deepslate_copper_ore",
        "deepslate_gold_ore",
        "deepslate_redstone_ore",
        "deepslate_lapis_ore",
        "deepslate_diamond_ore",
        "deepslate_emerald_ore",
    }
)

COLLECT_TARGETS = frozenset(
    {
        "oak_log",
        "spruce_log",
        "birch_log",
        "jungle_log",
        "acacia_log",
        "dark_oak_log",
        "mangrove_log",
        "cherry_log",
        "coal_ore",
        "iron_ore",
        "copper_ore",
        "gold_ore",
        "redstone_ore",
        "lapis_ore",
        "diamond_ore",
        "emerald_ore",
        "deepslate_coal_ore",
        "deepslate_iron_ore",
        "deepslate_copper_ore",
        "deepslate_gold_ore",
        "deepslate_redstone_ore",
        "deepslate_lapis_ore",
        "deepslate_diamond_ore",
        "deepslate_emerald_ore",
    }
)

FARM_TARGETS = frozenset(
    {
        "wheat",
        "carrots",
        "potatoes",
        "beetroots",
    }
)

STRONGLY_PROTECTED_TYPES = frozenset(
    {
        "chest",
        "trapped_chest",
        "barrel",
        "furnace",
        "blast_furnace",
        "smoker",
        "crafting_table",
        "bed",
        "white_bed",
        "orange_bed",
        "magenta_bed",
        "light_blue_bed",
        "yellow_bed",
        "lime_bed",
        "pink_bed",
        "gray_bed",
        "light_gray_bed",
        "cyan_bed",
        "purple_bed",
        "blue_bed",
        "brown_bed",
        "green_bed",
        "red_bed",
        "black_bed",
        "oak_door",
        "spruce_door",
        "birch_door",
        "jungle_door",
        "acacia_door",
        "dark_oak_door",
        "mangrove_door",
        "cherry_door",
        "iron_door",
        "lever",
        "redstone",
        "redstone_wire",
        "redstone_torch",
        "repeater",
        "comparator",
        "hopper",
        "dispenser",
        "dropper",
        "piston",
        "sticky_piston",
        "farmland",
        "wheat",
        "carrots",
        "potatoes",
        "beetroots",
        "sign",
        "oak_sign",
        "spruce_sign",
        "birch_sign",
        "jungle_sign",
        "acacia_sign",
        "dark_oak_sign",
        "mangrove_sign",
        "cherry_sign",
        "glass",
        "glass_pane",
        "bricks",
        "stone_bricks",
        "polished_blackstone_bricks",
        "quartz_block",
        "lantern",
        "torch",
    }
)

TEMPORARY_BOT_PURPOSES = frozenset(
    {
        "scaffold",
        "seal",
        "bridge",
        "pillar",
        "workstation",
        "temporary_light",
    }
)


def normalize_block_type(block_type: str) -> str:
    return block_type.removeprefix("minecraft:")


class GovernancePolicy:
    def __init__(
        self,
        *,
        natural_regions: Iterable[Region] = (),
        protected_regions: Iterable[Region] = (),
        bot_placements: Iterable[BotPlacement] = (),
    ) -> None:
        self.natural_regions = list(natural_regions)
        self.protected_regions = list(protected_regions)
        self.bot_placements: dict[Position, BotPlacement] = {entry.pos: entry for entry in bot_placements}

    def record_bot_placement(self, pos: Position, block_type: str, purpose: str, bot: str) -> None:
        self.bot_placements[pos] = BotPlacement(
            pos=pos,
            block_type=normalize_block_type(block_type),
            purpose=purpose,
            bot=bot,
        )

    def can_break(self, pos: Position, block_type: str, context: BreakContext | str) -> LegalityDecision:
        context = BreakContext(context)
        block_type = normalize_block_type(block_type)
        protected_region = self._region_containing(self.protected_regions, pos)
        if protected_region is not None:
            return LegalityDecision(
                allowed=False,
                reason="protected_region",
                protected=True,
                details={"region": protected_region.name},
            )

        bot_entry = self.bot_placements.get(pos)
        if bot_entry is not None:
            return self._can_break_bot_owned(bot_entry, block_type, context)

        natural_region = self._region_containing(self.natural_regions, pos)
        if context == BreakContext.FARM and block_type in FARM_TARGETS:
            if natural_region is None:
                return LegalityDecision(allowed=False, reason="unknown_provenance", protected=True)
            return LegalityDecision(allowed=True, reason="allowed_natural_farm", natural_region=natural_region.name)

        if block_type in STRONGLY_PROTECTED_TYPES:
            return LegalityDecision(allowed=False, reason="protected_type", protected=True)

        if natural_region is None:
            return LegalityDecision(allowed=False, reason="unknown_provenance", protected=True)

        if block_type not in NATURAL_BREAKABLE:
            return LegalityDecision(
                allowed=False,
                reason="not_natural_breakable",
                protected=True,
                natural_region=natural_region.name,
            )

        if context == BreakContext.PATH:
            return LegalityDecision(allowed=False, reason="path_no_terrain_break", natural_region=natural_region.name)

        if context == BreakContext.RECOVERY and block_type in COLLECT_TARGETS:
            return LegalityDecision(allowed=False, reason="recovery_no_resource_break", natural_region=natural_region.name)

        if context == BreakContext.COLLECT and block_type not in COLLECT_TARGETS:
            return LegalityDecision(allowed=False, reason="collect_target_required", natural_region=natural_region.name)

        if context == BreakContext.BOT_CLEANUP:
            return LegalityDecision(allowed=False, reason="not_bot_owned", protected=True, natural_region=natural_region.name)

        return LegalityDecision(allowed=True, reason="allowed_natural", natural_region=natural_region.name)

    def can_place(self, pos: Position, block_type: str, context: PlaceContext | str, bot: str) -> LegalityDecision:
        context = PlaceContext(context)
        block_type = normalize_block_type(block_type)
        protected_region = self._region_containing(self.protected_regions, pos)
        if protected_region is not None:
            return LegalityDecision(
                allowed=False,
                reason="protected_region",
                protected=True,
                details={"region": protected_region.name},
            )

        natural_region = self._region_containing(self.natural_regions, pos)
        if natural_region is None and context != PlaceContext.DIRECT:
            return LegalityDecision(allowed=False, reason="unknown_provenance", protected=True)

        if block_type in STRONGLY_PROTECTED_TYPES and context != PlaceContext.DIRECT:
            return LegalityDecision(allowed=False, reason="protected_type", protected=True)

        return LegalityDecision(
            allowed=True,
            reason="allowed_place",
            natural_region=natural_region.name if natural_region else None,
            details={"bot": bot, "context": context.value},
        )

    def can_interact(self, pos: Position, block_type: str, context: InteractionContext | str) -> LegalityDecision:
        context = InteractionContext(context)
        block_type = normalize_block_type(block_type)
        protected_region = self._region_containing(self.protected_regions, pos)
        if protected_region is not None:
            return LegalityDecision(
                allowed=False,
                reason="protected_region",
                protected=True,
                details={"region": protected_region.name, "context": context.value},
            )

        natural_region = self._region_containing(self.natural_regions, pos)
        if natural_region is None:
            return LegalityDecision(
                allowed=False,
                reason="unknown_provenance",
                protected=True,
                details={"context": context.value},
            )

        return LegalityDecision(
            allowed=True,
            reason="allowed_interaction",
            natural_region=natural_region.name,
            details={"context": context.value},
        )

    def _can_break_bot_owned(
        self, entry: BotPlacement, block_type: str, context: BreakContext
    ) -> LegalityDecision:
        if entry.block_type != block_type:
            return LegalityDecision(
                allowed=False,
                reason="bot_ledger_type_mismatch",
                protected=True,
                bot_owned=True,
                details={"ledger_type": entry.block_type, "observed_type": block_type},
            )
        if context != BreakContext.BOT_CLEANUP and entry.purpose not in TEMPORARY_BOT_PURPOSES:
            return LegalityDecision(allowed=False, reason="bot_owned_not_temporary", protected=True, bot_owned=True)
        return LegalityDecision(allowed=True, reason="allowed_bot_owned", bot_owned=True)

    @staticmethod
    def _region_containing(regions: Iterable[Region], pos: Position) -> Region | None:
        for region in regions:
            if region.contains(pos):
                return region
        return None
