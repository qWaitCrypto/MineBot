"""Break/place legality for the Body layer."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
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


# Ground cover a player breaks in passing; wheat seeds come only from grass.
# Missing here, a world covered in grass tufts had no seeds at all and the
# Agent's wheat field could not start (life-stage b2).
NATURAL_GRASS_TYPES = frozenset({"short_grass", "tall_grass", "fern", "large_fern"})

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

NATURAL_LEAF_TYPES = frozenset(
    {
        "oak_leaves",
        "spruce_leaves",
        "birch_leaves",
        "jungle_leaves",
        "acacia_leaves",
        "dark_oak_leaves",
        "mangrove_leaves",
        "cherry_leaves",
        "azalea_leaves",
        "flowering_azalea_leaves",
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
        # The rest of the ground a player digs through. Missing, a dig_up
        # through andesite and a collect of diorite were refused as "not
        # natural" (easy run-18, 05:42 and 06:11; body-tool-boundaries §13).
        "andesite",
        "diorite",
        "granite",
        "tuff",
        "calcite",
        "dripstone_block",
        "smooth_basalt",
        "coarse_dirt",
        "rooted_dirt",
        "podzol",
        "mycelium",
        "mud",
        "moss_block",
        "red_sand",
        "terracotta",
        "snow_block",
        "soul_sand",
        "soul_soil",
        "magma_block",
        "nether_gold_ore",
        "nether_quartz_ore",
    }
) | NATURAL_FLOWER_TYPES | NATURAL_LEAF_TYPES | NATURAL_GRASS_TYPES

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
) | NATURAL_FLOWER_TYPES | NATURAL_GRASS_TYPES

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

# Not natural block types, yet the world makes them (the cages around End
# crystals). Broken whenever the Agent names one as the target.
NAMED_TARGET_BREAKABLE = frozenset({"iron_bars"})

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

# Where the Body is when nothing says otherwise (a fresh client, other bodies).
DEFAULT_DIMENSION = "minecraft:overworld"

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


# Blocks that keep items: opening one is storing into it or taking from it.
ITEM_HOLDING_TYPES = frozenset({
    "chest", "trapped_chest", "barrel", "ender_chest", "furnace", "blast_furnace", "smoker", "hopper",
    "dispenser", "dropper", "brewing_stand",
})


def holds_items(block_type: str) -> bool:
    name = normalize_block_type(block_type)
    return name in ITEM_HOLDING_TYPES or name.endswith("shulker_box")


@dataclass(frozen=True)
class Ownership:
    """Whose a block is: the Bot's own ledger, or what the Agent wrote in its life profile.

    vision-and-home-20261028.md §7.7 item 7: ``mine`` is the ledger, ``mine.containers``,
    ``mine.areas`` and a belonging the Agent wrote under its own name; ``shared`` and ``player``
    are the belongings players told the Agent about.
    """

    whose: str
    source: str
    owner: str | None = None
    said_by: str | None = None
    said_at: str | None = None

    def sentence(self) -> str:
        whose = "shared" if self.whose == "shared" else f"{self.owner}'s"
        when = f" at {self.said_at}" if self.said_at else ""
        return f"This is {whose} ({self.said_by} said so{when})."

    def details(self) -> dict[str, object]:
        out: dict[str, object] = {"whose": self.whose, "source": self.source}
        if self.source == "belongings":
            out.update(owner=self.owner, said_by=self.said_by, said_at=self.said_at, said=self.sentence())
        return out


@dataclass(frozen=True)
class _Claim:
    dimension: str
    ownership: Ownership
    pos: Position | None = None
    region: Region | None = None

    def covers(self, dimension: str, pos: Position) -> bool:
        if self.dimension != dimension:
            return False
        return self.pos == pos if self.pos is not None else self.region is not None and self.region.contains(pos)


def ownership_claims(profile: Mapping[str, object] | None, bot_name: str | None) -> tuple[_Claim, ...]:
    """The profile's claims in the order they are looked up.

    What a player said about one block comes first, then what they said about an area (the
    smallest first), then the profile's own ``mine``. The ledger is looked up between the first
    two (``GovernancePolicy.ownership``), so a scaffold the Bot placed in a shared area stays its own.
    """
    if not isinstance(profile, Mapping):
        return ()
    blocks: list[_Claim] = []
    areas: list[tuple[int, _Claim]] = []
    for row in profile.get("belongings") or ():
        if not isinstance(row, Mapping):
            continue
        owner = str(row.get("owner") or "").strip()
        if not owner:
            continue
        whose = ("shared" if owner.lower() == "shared" else
                 "mine" if bot_name and owner.lower() == bot_name.lower() else "player")
        ownership = Ownership(whose, "belongings", owner=owner, said_by=str(row.get("said_by") or "") or None,
                              said_at=str(row.get("said_at") or "") or None)
        dimension = _dimension_id(row.get("dimension"))
        pos, region = _cell(row.get("pos")), _area(row.get("area"), owner)
        if pos is not None:
            blocks.append(_Claim(dimension, ownership, pos=pos))
        elif region is not None:
            areas.append((_volume(region), _Claim(dimension, ownership, region=region)))
    mine: list[_Claim] = []
    own = profile.get("mine") if isinstance(profile.get("mine"), Mapping) else {}
    for point in own.get("containers") or ():
        cell = _cell(point)
        if cell is not None:
            mine.append(_Claim(DEFAULT_DIMENSION, Ownership("mine", "mine.containers"), pos=cell))
    for row in own.get("areas") or ():
        region = _area(row, str(row.get("name") or "")) if isinstance(row, Mapping) else None
        if region is not None:
            mine.append(_Claim(_dimension_id(row.get("dimension")), Ownership("mine", "mine.areas"), region=region))
    return (*blocks, *(claim for _, claim in sorted(areas, key=lambda item: item[0])), *mine)


def _dimension_id(value: object) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return DEFAULT_DIMENSION
    if ":" in text:
        return text
    return "minecraft:" + {"nether": "the_nether", "end": "the_end"}.get(text, text)


def _cell(value: object) -> Position | None:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    try:
        return tuple(int(math.floor(float(axis))) for axis in value)  # type: ignore[return-value]
    except (TypeError, ValueError):
        return None


def _area(value: object, name: str) -> Region | None:
    if not isinstance(value, Mapping):
        return None
    start, end = _cell(value.get("from")), _cell(value.get("to"))
    if start is None or end is None:
        return None
    return Region(name, tuple(map(min, start, end)), tuple(map(max, start, end)))  # type: ignore[arg-type]


def _volume(region: Region) -> int:
    return math.prod(high - low + 1 for low, high in zip(region.min_pos, region.max_pos))


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
    epoch: str
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
    epoch: str
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
        dimension: Callable[[], str | None] | None = None,
        ownership: Callable[[], Mapping[str, object] | None] | None = None,
        confirmation_ttl_s: float = GOVERNANCE_CONFIRMATION_TTL_S,
        confirmation_radius: int = GOVERNANCE_CONFIRMATION_RADIUS,
        confirmation_vertical_radius: int = GOVERNANCE_CONFIRMATION_VERTICAL_RADIUS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.natural_regions = list(natural_regions)
        self.protected_regions = list(protected_regions)
        # Own blocks per dimension: the same coordinates in the Nether are a
        # different block (pro plan P4). ``dimension`` reads where the Body is.
        self._dimension = dimension
        self._placements: dict[str, dict[Position, BotPlacement]] = {
            DEFAULT_DIMENSION: {entry.pos: entry for entry in bot_placements}}
        # Whose things are whose, as the Agent wrote it: ``ownership`` returns the
        # life profile as last read (vision-and-home-20261028.md §7.7 item 7).
        self._ownership = ownership
        self._claims: tuple[object, tuple[_Claim, ...]] = (None, ())
        self.structure_risk_assessor = structure_risk_assessor
        self.require_structure_assessment = require_structure_assessment
        self.bot_name = bot_name
        self._confirmation_ttl_s = max(1.0, float(confirmation_ttl_s))
        self._confirmation_radius = max(1, int(confirmation_radius))
        self._confirmation_vertical_radius = max(0, int(confirmation_vertical_radius))
        self._clock = clock
        self._confirmation_lock = RLock()
        self._confirmation_scope: Callable[[], object] = lambda: None
        self._confirmation_epoch = uuid4().hex
        self._pending_challenges: dict[str, _GovernanceChallenge] = {}
        self._active_permits: dict[str, _GovernancePermit] = {}

    def begin_confirmation_epoch(self, scope: Callable[[], object] | None = None) -> None:
        """Bind review state to a task/runtime scope and discard older authority."""

        with self._confirmation_lock:
            self._pending_challenges.clear()
            self._active_permits.clear()
            self._confirmation_scope = scope or (lambda: None)
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
                epoch=challenge.epoch,
                purpose=normalized_purpose,
            )
            self._active_permits[permit_id] = permit
            return {
                "confirmed": True,
                "reason": "governance_scope_confirmed",
                "challenge_id": challenge.challenge_id,
                "permit": self._permit_payload(permit, now),
            }

    @property
    def bot_placements(self) -> dict[Position, BotPlacement]:
        """The Bot's own blocks in the dimension the Body is in now."""
        return self._placements.setdefault(self._current_dimension(), {})

    def all_bot_placements(self) -> dict[str, dict[Position, BotPlacement]]:
        """The Bot's own blocks in every dimension, by dimension."""
        return {dimension: dict(entries) for dimension, entries in self._placements.items()}

    def attach_ownership(self, profile: Callable[[], Mapping[str, object] | None]) -> None:
        """Read whose things are whose from the life profile ``profile`` returns."""
        self._ownership = profile

    def ownership(self, pos: Position) -> Ownership | None:
        """Whose the block at ``pos`` is, in the dimension the Body is in; None when nobody said."""
        dimension = self._current_dimension()
        claims = self._current_claims()
        said_blocks = sum(1 for claim in claims if claim.pos is not None and claim.ownership.source == "belongings")
        for claim in claims[:said_blocks]:
            if claim.covers(dimension, pos):
                return claim.ownership
        if pos in self._placements.get(dimension, {}):
            return Ownership("mine", "placed")
        for claim in claims[said_blocks:]:
            if claim.covers(dimension, pos):
                return claim.ownership
        return None

    def _current_claims(self) -> tuple[_Claim, ...]:
        try:
            profile = self._ownership() if self._ownership is not None else None
        except Exception:
            return self._claims[1]
        if profile is not self._claims[0]:
            self._claims = (profile, ownership_claims(profile, self.bot_name))
        return self._claims[1]

    def _current_dimension(self) -> str:
        try:
            current = self._dimension() if self._dimension is not None else None
        except Exception:
            current = None
        return current if isinstance(current, str) and current else DEFAULT_DIMENSION

    def dismiss_action_confirmations(self, action_id: str) -> None:
        """Discard review prompts made irrelevant by an action succeeding elsewhere."""

        with self._confirmation_lock:
            for challenge_id, challenge in list(self._pending_challenges.items()):
                if action_id not in challenge.action_ids:
                    continue
                challenge.action_ids.discard(action_id)
                if not challenge.action_ids:
                    self._pending_challenges.pop(challenge_id, None)

    def record_bot_placement(
        self, pos: Position, block_type: str, purpose: str, bot: str, *, dimension: str | None = None,
    ) -> None:
        placements = self._placements.setdefault(dimension or self._current_dimension(), {})
        placements[pos] = BotPlacement(
            pos=pos,
            block_type=normalize_block_type(block_type),
            purpose=purpose,
            bot=bot,
        )

    def forget_bot_placement(self, pos: Position, *, dimension: str | None = None) -> None:
        """The block is no longer the Bot's: it was broken or turned into something else."""
        self._placements.get(dimension or self._current_dimension(), {}).pop(pos, None)

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

        owned = self.ownership(pos)
        if owned is not None and owned.whose in ("player", "shared"):
            return LegalityDecision(
                allowed=False,
                reason="player_owned" if owned.whose == "player" else "shared_not_broken",
                protected=True,
                details=owned.details(),
            )

        bot_entry = self.bot_placements.get(pos)
        if bot_entry is not None and bot_entry.block_type == block_type:
            # The Body's own block is the Body's to take down, on the way or on
            # purpose, as a player breaks out of the box they built for the
            # night. run-45 15:43-16:07: walls placed as "shelter" were not
            # "temporary", so every walk, dig_up and go_to_surface was kept
            # from them and the Body sat 24 minutes in its own flooded box
            # (project-review-20261110 §9.16, user: the permission layer goes).
            return LegalityDecision(allowed=True, reason="allowed_bot_owned", bot_owned=True)
        # A ledger row whose block has changed is no longer the Body's block;
        # what stands there now is judged as any other block below.

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

        if (block_type in NAMED_TARGET_BREAKABLE
                and (context == BreakContext.DIRECT or (context == BreakContext.COLLECT and explicit_target))):
            # World-made but not a natural block type: the iron bars caging an
            # End crystal. Refused as not natural, the Agent left the last
            # crystal (end run-33, 19:54); allowed only inside its own
            # mine.areas, it registered none, was refused and wrote "Cage bars
            # cannot be broken at all" (end run-35, 13:14-13:20). Allowed as the
            # named target anywhere; places that are a player's or protected
            # are refused above.
            return LegalityDecision(allowed=True, reason="allowed_named_target", natural_region=region_name,
                                    details=owned.details() if owned is not None else {})

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
        owned = self.ownership(pos)
        if owned is not None and owned.whose == "player" and holds_items(block_type):
            return LegalityDecision(
                allowed=False,
                reason="player_owned",
                protected=True,
                details={**owned.details(), "context": context.value},
            )
        if owned is not None and owned.whose in ("mine", "shared"):
            return LegalityDecision(
                allowed=True,
                reason="allowed_own" if owned.whose == "mine" else "allowed_shared",
                bot_owned=owned.source == "placed",
                natural_region=natural_region.name if natural_region is not None else None,
                details={**owned.details(), "context": context.value},
            )
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
            epoch = self._current_confirmation_epoch()
            for challenge in self._pending_challenges.values():
                if (
                    challenge.epoch == epoch
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
                epoch=epoch,
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
            epoch = self._current_confirmation_epoch()
            for permit_id, permit in list(self._active_permits.items()):
                if (
                    permit.epoch != epoch
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
        epoch = self._current_confirmation_epoch()
        self._pending_challenges = {
            challenge_id: challenge
            for challenge_id, challenge in self._pending_challenges.items()
            if challenge.expires_at > now and challenge.epoch == epoch
        }
        self._active_permits = {
            permit_id: permit
            for permit_id, permit in self._active_permits.items()
            if permit.expires_at > now and permit.epoch == epoch
        }

    def _current_confirmation_epoch(self) -> str:
        try:
            scope = self._confirmation_scope()
        except Exception:
            scope = None
        return f"{self._confirmation_epoch}:{scope!s}"

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
