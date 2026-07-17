"""Break/place legality for the Body layer."""

from __future__ import annotations

from typing import Iterable, Protocol

from minebot.contract.governance import (
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
        "dirt",
        "grass_block",
        "gravel",
        "sand",
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

UNSAFE_STAND_SUPPORT_TYPES = frozenset(
    {
        "cactus",
        "campfire",
        "fire",
        "lava",
        "magma_block",
        "powder_snow",
        "soul_campfire",
        "soul_fire",
        "sweet_berry_bush",
        "wither_rose",
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


class StructureRiskAssessor(Protocol):
    def assess(
        self,
        pos: Position,
        block_type: str,
        context: BreakContext,
    ) -> StructureRiskAssessment: ...


class GovernancePolicy:
    def __init__(
        self,
        *,
        natural_regions: Iterable[Region] = (),
        protected_regions: Iterable[Region] = (),
        bot_placements: Iterable[BotPlacement] = (),
        structure_risk_assessor: StructureRiskAssessor | None = None,
        require_structure_assessment: bool = False,
    ) -> None:
        self.natural_regions = list(natural_regions)
        self.protected_regions = list(protected_regions)
        self.bot_placements: dict[Position, BotPlacement] = {entry.pos: entry for entry in bot_placements}
        self.structure_risk_assessor = structure_risk_assessor
        self.require_structure_assessment = require_structure_assessment

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
        region_name = natural_region.name if natural_region is not None else None
        if context == BreakContext.FARM and block_type in FARM_TARGETS:
            # Crops are player-planted by default; harvesting still requires a
            # declared natural region (unchanged, conservative).
            if natural_region is None:
                return LegalityDecision(allowed=False, reason="unknown_provenance", protected=True)
            return LegalityDecision(allowed=True, reason="allowed_natural_farm", natural_region=natural_region.name)

        if block_type in STRONGLY_PROTECTED_TYPES:
            return LegalityDecision(allowed=False, reason="protected_type", protected=True)

        if natural_region is None and context == BreakContext.TRAVEL:
            # Conservative pathing: the navigator does not tunnel through
            # undeclared terrain. Resource/explicit breaks (COLLECT /
            # COLLECT_APPROACH / DIRECT / BOT_CLEANUP) establish provenance by
            # block TYPE below, so a declared region is not required to mine a
            # known-natural target or to clear a path to one.
            return LegalityDecision(allowed=False, reason="unknown_provenance", protected=True)

        if block_type not in NATURAL_BREAKABLE:
            return LegalityDecision(
                allowed=False,
                reason="not_natural_breakable",
                protected=True,
                natural_region=region_name,
            )

        if context == BreakContext.PATH:
            return LegalityDecision(allowed=False, reason="path_no_terrain_break", natural_region=region_name)

        if context == BreakContext.RECOVERY and block_type in COLLECT_TARGETS:
            return LegalityDecision(allowed=False, reason="recovery_no_resource_break", natural_region=region_name)

        if context == BreakContext.COLLECT and block_type not in COLLECT_TARGETS:
            return LegalityDecision(allowed=False, reason="collect_target_required", natural_region=region_name)

        if context == BreakContext.BOT_CLEANUP:
            return LegalityDecision(allowed=False, reason="not_bot_owned", protected=True, natural_region=region_name)

        assessment = self._assess_structure_risk(pos, block_type, context)
        if assessment is None:
            if self.require_structure_assessment:
                return LegalityDecision(
                    allowed=False,
                    reason="structure_assessment_required",
                    protected=True,
                    natural_region=region_name,
                )
            return LegalityDecision(allowed=True, reason="allowed_natural", natural_region=region_name)
        risk = _structure_risk_payload(assessment)
        if not assessment.complete or assessment.level is StructureRiskLevel.AMBIGUOUS:
            return LegalityDecision(
                allowed=False,
                reason="structure_risk_unknown",
                protected=True,
                natural_region=region_name,
                details={"structure_risk": risk},
            )
        if assessment.level is StructureRiskLevel.HIGH:
            return LegalityDecision(
                allowed=False,
                reason="player_structure_risk",
                protected=True,
                natural_region=region_name,
                details={"structure_risk": risk},
            )
        return LegalityDecision(
            allowed=True,
            reason="allowed_natural",
            natural_region=region_name,
            details={"structure_risk": risk},
        )

    def can_stand(self, pos: Position, support_type: str) -> LegalityDecision:
        """Authorize a non-mutating stand goal without granting mutation rights."""

        support_type = normalize_block_type(support_type)
        protected_region = self._region_containing(self.protected_regions, pos)
        if protected_region is not None:
            return LegalityDecision(
                allowed=False,
                reason="protected_region",
                protected=True,
                details={"region": protected_region.name, "operation": "stand"},
            )

        bot_entry = self.bot_placements.get(pos)
        if bot_entry is not None:
            if bot_entry.block_type != support_type:
                return LegalityDecision(
                    allowed=False,
                    reason="bot_ledger_type_mismatch",
                    protected=True,
                    bot_owned=True,
                    details={"ledger_type": bot_entry.block_type, "observed_type": support_type},
                )
            return LegalityDecision(
                allowed=True,
                reason="allowed_bot_owned",
                bot_owned=True,
                details={"operation": "stand"},
            )

        if support_type in STRONGLY_PROTECTED_TYPES:
            return LegalityDecision(
                allowed=False,
                reason="protected_support_type",
                protected=True,
                details={"operation": "stand"},
            )
        if support_type in UNSAFE_STAND_SUPPORT_TYPES:
            return LegalityDecision(
                allowed=False,
                reason="unsafe_support_type",
                protected=True,
                details={"operation": "stand"},
            )

        natural_region = self._region_containing(self.natural_regions, pos)
        if natural_region is None:
            return LegalityDecision(
                allowed=False,
                reason="unknown_provenance",
                protected=True,
                details={"operation": "stand"},
            )
        return LegalityDecision(
            allowed=True,
            reason="allowed_stand",
            natural_region=natural_region.name,
            details={"operation": "stand"},
        )

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

    def _assess_structure_risk(
        self,
        pos: Position,
        block_type: str,
        context: BreakContext,
    ) -> StructureRiskAssessment | None:
        if self.structure_risk_assessor is None:
            return None
        try:
            return self.structure_risk_assessor.assess(pos, block_type, context)
        except Exception as exc:
            return StructureRiskAssessment(
                pos=pos,
                block_type=block_type,
                level=StructureRiskLevel.AMBIGUOUS,
                score=1.0,
                complete=False,
                sampled_cells=0,
                signals=(f"assessor_failed:{type(exc).__name__}",),
            )

    @staticmethod
    def _region_containing(regions: Iterable[Region], pos: Position) -> Region | None:
        for region in regions:
            if region.contains(pos):
                return region
        return None


def _structure_risk_payload(assessment: StructureRiskAssessment) -> dict[str, object]:
    return {
        "level": assessment.level.value,
        "score": assessment.score,
        "complete": assessment.complete,
        "sampled_cells": assessment.sampled_cells,
        "signals": list(assessment.signals),
        "source": assessment.source,
    }
