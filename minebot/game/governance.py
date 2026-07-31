"""Break/place legality for the Body layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock
import time
from typing import Callable, Iterable, Mapping, Protocol
from uuid import uuid4

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


NATURAL_FLOWER_TYPES = frozenset(
    {
        "dandelion",
        "poppy",
        "blue_orchid",
        "allium",
        "azure_bluet",
        "red_tulip",
        "orange_tulip",
        "white_tulip",
        "pink_tulip",
        "oxeye_daisy",
        "cornflower",
        "lily_of_the_valley",
        "sunflower",
        "lilac",
        "rose_bush",
        "peony",
        "torchflower",
        "pitcher_plant",
        "open_eyeblossom",
        "closed_eyeblossom",
    }
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
) | NATURAL_FLOWER_TYPES

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
) | NATURAL_FLOWER_TYPES

# Recovery may clear ordinary terrain cover, even when that block can also be
# collected directly. Preserve route avoidance only for resources a player
# would reasonably route around and acquire deliberately.
RECOVERY_ROUTE_AVOID_TYPES = frozenset(
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
        "temporary_furnace",
        "temporary_furnace_auto_site",
        "temporary_light",
    }
)

GOVERNANCE_CONFIRMATION_TTL_S = 300.0
GOVERNANCE_CONFIRMATION_RADIUS = 8
GOVERNANCE_CONFIRMATION_VERTICAL_RADIUS = 4
_CONFIRMATION_MAX_UNIQUE_BLOCKS = {
    BreakContext.TRAVEL: 8,
    BreakContext.COLLECT: 12,
    BreakContext.COLLECT_APPROACH: 32,
    BreakContext.RECOVERY: 96,
    BreakContext.DIRECT: 8,
}
_CONFIRMATION_HORIZONTAL_RADIUS = {
    BreakContext.RECOVERY: 16,
}
_CONFIRMATION_VERTICAL_RADIUS = {
    BreakContext.COLLECT_APPROACH: 8,
    BreakContext.RECOVERY: 32,
}


def normalize_block_type(block_type: str) -> str:
    return block_type.removeprefix("minecraft:")


class StructureRiskAssessor(Protocol):
    def assess(
        self,
        pos: Position,
        block_type: str,
        context: BreakContext,
        *,
        bot_placements: Mapping[Position, BotPlacement] | None = None,
    ) -> StructureRiskAssessment: ...


@dataclass
class _GovernanceChallenge:
    challenge_id: str
    bot: str | None
    context: BreakContext
    block_types: tuple[str, ...]
    region: Region
    max_unique_blocks: int
    created_at: float
    expires_at: float
    generation: int
    evidence: dict[str, object]
    action_ids: set[str] = field(default_factory=set)


@dataclass
class _GovernancePermit:
    permit_id: str
    challenge_id: str
    bot: str | None
    context: BreakContext
    block_types: tuple[str, ...]
    region: Region
    max_unique_blocks: int
    expires_at: float
    generation: int
    purpose: str
    authorized_positions: set[Position] = field(default_factory=set)


class GovernancePolicy:
    def __init__(
        self,
        *,
        natural_regions: Iterable[Region] = (),
        protected_regions: Iterable[Region] = (),
        bot_placements: Iterable[BotPlacement] = (),
        structure_risk_assessor: StructureRiskAssessor | None = None,
        require_structure_assessment: bool = False,
        bot_name: str | None = None,
        confirmation_ttl_s: float = GOVERNANCE_CONFIRMATION_TTL_S,
        confirmation_radius: int = GOVERNANCE_CONFIRMATION_RADIUS,
        confirmation_vertical_radius: int = GOVERNANCE_CONFIRMATION_VERTICAL_RADIUS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.natural_regions = list(natural_regions)
        self.protected_regions = list(protected_regions)
        self.bot_placements: dict[Position, BotPlacement] = {entry.pos: entry for entry in bot_placements}
        self.structure_risk_assessor = structure_risk_assessor
        self.require_structure_assessment = require_structure_assessment
        self.bot_name = bot_name
        self._confirmation_ttl_s = max(1.0, float(confirmation_ttl_s))
        self._confirmation_radius = max(1, int(confirmation_radius))
        self._confirmation_vertical_radius = max(0, int(confirmation_vertical_radius))
        self._clock = clock
        self._confirmation_lock = RLock()
        self._confirmation_generation: Callable[[], int] = lambda: 0
        self._confirmation_epoch = uuid4().hex
        self._pending_challenges: dict[str, _GovernanceChallenge] = {}
        self._active_permits: dict[str, _GovernancePermit] = {}

    def begin_confirmation_epoch(self, generation: Callable[[], int] | None = None) -> None:
        """Invalidate review state when a task/runtime generation is rebuilt."""

        with self._confirmation_lock:
            self._pending_challenges.clear()
            self._active_permits.clear()
            self._confirmation_generation = generation or (lambda: 0)
            self._confirmation_epoch = uuid4().hex

    def invalidate_confirmation_state(self) -> None:
        """Drop pending authority after a body identity/lifecycle discontinuity."""

        with self._confirmation_lock:
            self._pending_challenges.clear()
            self._active_permits.clear()
            self._confirmation_epoch = uuid4().hex

    def pending_confirmations(self, *, action_id: str | None = None) -> list[dict[str, object]]:
        """Return bounded model-facing facts for currently actionable challenges."""

        with self._confirmation_lock:
            now = self._clock()
            self._prune_confirmation_state(now)
            challenges = [
                challenge
                for challenge in self._pending_challenges.values()
                if action_id is None or action_id in challenge.action_ids
            ]
            challenges.sort(key=lambda challenge: (challenge.created_at, challenge.challenge_id))
            return [self._challenge_payload(challenge, now) for challenge in challenges]

    def confirm_scope(self, challenge_id: str, purpose: str) -> dict[str, object]:
        """Turn one pending challenge into a bounded, rechecked break permit."""

        normalized_id = str(challenge_id).strip()
        normalized_purpose = " ".join(str(purpose).split())[:160]
        with self._confirmation_lock:
            now = self._clock()
            self._prune_confirmation_state(now)
            challenge = self._pending_challenges.pop(normalized_id, None)
            if challenge is None:
                return {
                    "confirmed": False,
                    "reason": "governance_challenge_not_pending",
                    "challenge_id": normalized_id,
                }
            if not normalized_purpose:
                self._pending_challenges[normalized_id] = challenge
                return {
                    "confirmed": False,
                    "reason": "governance_confirmation_purpose_required",
                    "challenge_id": normalized_id,
                }
            permit_id = f"gp-{uuid4().hex[:16]}"
            permit = _GovernancePermit(
                permit_id=permit_id,
                challenge_id=challenge.challenge_id,
                bot=challenge.bot,
                context=challenge.context,
                block_types=challenge.block_types,
                region=challenge.region,
                max_unique_blocks=challenge.max_unique_blocks,
                expires_at=challenge.expires_at,
                generation=challenge.generation,
                purpose=normalized_purpose,
            )
            self._active_permits[permit_id] = permit
            return {
                "confirmed": True,
                "reason": "governance_scope_confirmed",
                "challenge_id": challenge.challenge_id,
                "permit": self._permit_payload(permit, now),
            }

    def dismiss_action_confirmations(self, action_id: str) -> None:
        """Discard review prompts made irrelevant by an action succeeding elsewhere."""

        with self._confirmation_lock:
            for challenge_id, challenge in list(self._pending_challenges.items()):
                if action_id not in challenge.action_ids:
                    continue
                challenge.action_ids.discard(action_id)
                if not challenge.action_ids:
                    self._pending_challenges.pop(challenge_id, None)

    def record_bot_placement(self, pos: Position, block_type: str, purpose: str, bot: str) -> None:
        self.bot_placements[pos] = BotPlacement(
            pos=pos,
            block_type=normalize_block_type(block_type),
            purpose=purpose,
            bot=bot,
        )

    def can_break(
        self,
        pos: Position,
        block_type: str,
        context: BreakContext | str,
        *,
        explicit_target: bool = False,
        bot: str | None = None,
        action_id: str | None = None,
    ) -> LegalityDecision:
        """Authorize one break while keeping target and approach semantics separate.

        An explicit target-type allow-list can authorize a natural block that
        is not one of the default collect targets (for example stone that
        drops cobblestone).  That fact belongs to the final ``COLLECT``
        mutation and must not silently inherit ``COLLECT_APPROACH``'s more
        conservative structure-risk prior, which is reserved for clearing a
        route to a target.
        """
        context = BreakContext(context)
        block_type = normalize_block_type(block_type)
        actor = bot or self.bot_name
        if self.bot_name is not None and actor is not None and actor != self.bot_name:
            return LegalityDecision(
                allowed=False,
                reason="governance_bot_mismatch",
                protected=True,
                details={"expected_bot": self.bot_name, "observed_bot": actor},
            )
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

        if context == BreakContext.RECOVERY and block_type in RECOVERY_ROUTE_AVOID_TYPES:
            return LegalityDecision(allowed=False, reason="recovery_no_resource_break", natural_region=region_name)

        if context == BreakContext.COLLECT and block_type not in COLLECT_TARGETS and not explicit_target:
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
        if not assessment.complete:
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
        if assessment.level is StructureRiskLevel.AMBIGUOUS:
            permit = self._authorize_from_confirmed_scope(
                pos,
                block_type,
                context,
                bot=actor,
            )
            if permit is not None:
                return LegalityDecision(
                    allowed=True,
                    reason="allowed_governance_scope",
                    natural_region=region_name,
                    details={
                        "structure_risk": risk,
                        "governance_permit": permit,
                    },
                )
            challenge = self._pending_confirmation(
                pos,
                block_type,
                context,
                assessment,
                bot=actor,
                action_id=action_id,
            )
            return LegalityDecision(
                allowed=False,
                reason="governance_confirmation_required",
                protected=False,
                natural_region=region_name,
                details={
                    "structure_risk": risk,
                    "governance_confirmation": challenge,
                },
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

        if block_type in STRONGLY_PROTECTED_TYPES and context in {
            PlaceContext.TRAVEL,
            PlaceContext.RECOVERY,
        }:
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
            return self.structure_risk_assessor.assess(
                pos,
                block_type,
                context,
                bot_placements=self.bot_placements,
            )
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

    def _pending_confirmation(
        self,
        pos: Position,
        block_type: str,
        context: BreakContext,
        assessment: StructureRiskAssessment,
        *,
        bot: str | None,
        action_id: str | None,
    ) -> dict[str, object]:
        with self._confirmation_lock:
            now = self._clock()
            self._prune_confirmation_state(now)
            generation = self._current_confirmation_generation()
            for challenge in self._pending_challenges.values():
                if (
                    challenge.generation == generation
                    and challenge.bot == bot
                    and challenge.context is context
                    and block_type in challenge.block_types
                    and challenge.region.contains(pos)
                ):
                    if action_id:
                        challenge.action_ids.add(action_id)
                    return self._challenge_payload(challenge, now)

            challenge_id = f"gc-{uuid4().hex[:16]}"
            x, y, z = pos
            horizontal_radius = max(
                self._confirmation_radius,
                _CONFIRMATION_HORIZONTAL_RADIUS.get(context, 0),
            )
            vertical_radius = max(
                self._confirmation_vertical_radius,
                _CONFIRMATION_VERTICAL_RADIUS.get(context, 0),
            )
            region = Region(
                name=f"governance-confirmation:{challenge_id}",
                min_pos=(
                    x - horizontal_radius,
                    y - vertical_radius,
                    z - horizontal_radius,
                ),
                max_pos=(
                    x + horizontal_radius,
                    y + vertical_radius,
                    z + horizontal_radius,
                ),
            )
            challenge = _GovernanceChallenge(
                challenge_id=challenge_id,
                bot=bot,
                context=context,
                block_types=(block_type,),
                region=region,
                max_unique_blocks=_CONFIRMATION_MAX_UNIQUE_BLOCKS.get(context, 8),
                created_at=now,
                expires_at=now + self._confirmation_ttl_s,
                generation=generation,
                evidence=_structure_risk_payload(assessment),
                action_ids={action_id} if action_id else set(),
            )
            self._pending_challenges[challenge_id] = challenge
            return self._challenge_payload(challenge, now)

    def _authorize_from_confirmed_scope(
        self,
        pos: Position,
        block_type: str,
        context: BreakContext,
        *,
        bot: str | None,
    ) -> dict[str, object] | None:
        with self._confirmation_lock:
            now = self._clock()
            self._prune_confirmation_state(now)
            generation = self._current_confirmation_generation()
            for permit_id, permit in list(self._active_permits.items()):
                if (
                    permit.generation != generation
                    or permit.bot != bot
                    or permit.context is not context
                    or block_type not in permit.block_types
                    or not permit.region.contains(pos)
                ):
                    continue
                if (
                    pos not in permit.authorized_positions
                    and len(permit.authorized_positions) >= permit.max_unique_blocks
                ):
                    self._active_permits.pop(permit_id, None)
                    continue
                permit.authorized_positions.add(pos)
                return self._permit_payload(permit, now)
            return None

    def _prune_confirmation_state(self, now: float) -> None:
        generation = self._current_confirmation_generation()
        self._pending_challenges = {
            challenge_id: challenge
            for challenge_id, challenge in self._pending_challenges.items()
            if challenge.expires_at > now and challenge.generation == generation
        }
        self._active_permits = {
            permit_id: permit
            for permit_id, permit in self._active_permits.items()
            if permit.expires_at > now and permit.generation == generation
        }

    def _current_confirmation_generation(self) -> int:
        try:
            return int(self._confirmation_generation())
        except (TypeError, ValueError):
            return 0

    def _challenge_payload(self, challenge: _GovernanceChallenge, now: float) -> dict[str, object]:
        return {
            "challenge_id": challenge.challenge_id,
            "operation": "break",
            "bot": challenge.bot,
            "context": challenge.context.value,
            "block_types": list(challenge.block_types),
            "region": {
                "min": list(challenge.region.min_pos),
                "max": list(challenge.region.max_pos),
            },
            "max_unique_blocks": challenge.max_unique_blocks,
            "ttl_remaining_s": round(max(0.0, challenge.expires_at - now), 3),
            "evidence": dict(challenge.evidence),
        }

    def _permit_payload(self, permit: _GovernancePermit, now: float) -> dict[str, object]:
        return {
            "permit_id": permit.permit_id,
            "challenge_id": permit.challenge_id,
            "operation": "break",
            "bot": permit.bot,
            "context": permit.context.value,
            "block_types": list(permit.block_types),
            "region": {
                "min": list(permit.region.min_pos),
                "max": list(permit.region.max_pos),
            },
            "max_unique_blocks": permit.max_unique_blocks,
            "authorized_unique_blocks": len(permit.authorized_positions),
            "ttl_remaining_s": round(max(0.0, permit.expires_at - now), 3),
            "purpose": permit.purpose,
        }

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
