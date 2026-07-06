"""Deterministic item acquisition planning for agent-layer composition."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from math import ceil
from typing import Callable, Literal

from minebot.contract.harvest import PICKAXE_BY_TIER, best_owned_pickaxe, required_pickaxe_tier, tier_satisfies


@dataclass(frozen=True)
class RecipeVariant:
    output_item: str
    output_count: int
    ingredient_groups: tuple[tuple[str, ...], ...]
    requires_table: bool = False
    recipe_kind: str = "crafting"


@dataclass(frozen=True)
class AcquisitionStep:
    kind: Literal["collect", "craft", "smelt", "equip"]
    item: str
    count: int
    detail: dict[str, object]


@dataclass(frozen=True)
class AcquisitionError:
    reason: str
    item: str
    count: int
    chain: tuple[str, ...]
    detail: dict[str, object]


RecipeLookup = Callable[[str], list[RecipeVariant] | None]

LOG_ITEMS = (
    "oak_log",
    "spruce_log",
    "birch_log",
    "jungle_log",
    "acacia_log",
    "dark_oak_log",
)
PLANK_ITEMS = (
    "oak_planks",
    "spruce_planks",
    "birch_planks",
    "jungle_planks",
    "acacia_planks",
    "dark_oak_planks",
)
PLANK_FROM_LOG = dict(zip(PLANK_ITEMS, LOG_ITEMS, strict=True))

FUEL_BURN_SECONDS = {
    "coal": 80.0,
    "charcoal": 80.0,
    "coal_block": 800.0,
    "oak_planks": 15.0,
    "spruce_planks": 15.0,
    "birch_planks": 15.0,
    "jungle_planks": 15.0,
    "acacia_planks": 15.0,
    "dark_oak_planks": 15.0,
    "stick": 5.0,
    "bamboo": 2.5,
    "dried_kelp_block": 200.0,
    "blaze_rod": 120.0,
    "oak_log": 15.0,
    "spruce_log": 15.0,
    "birch_log": 15.0,
    "jungle_log": 15.0,
    "acacia_log": 15.0,
    "dark_oak_log": 15.0,
    "lava_bucket": 1000.0,
}
FUEL_SELECTION_ORDER = (
    "coal",
    "charcoal",
    "coal_block",
    *PLANK_ITEMS,
    "stick",
    "bamboo",
    "dried_kelp_block",
    "blaze_rod",
    *LOG_ITEMS,
    "lava_bucket",
)


@dataclass(frozen=True)
class _CollectRoute:
    inventory_item: str
    collect_item: str
    block_types: tuple[str, ...]
    expected_drops: tuple[str, ...]


@dataclass(frozen=True)
class _SmeltRoute:
    input_item: str
    output_count: int = 1
    input_count: int = 1


COLLECT_ROUTES: dict[str, _CollectRoute] = {
    "log": _CollectRoute("oak_log", "oak_log", LOG_ITEMS, LOG_ITEMS),
    "logs": _CollectRoute("oak_log", "oak_log", LOG_ITEMS, LOG_ITEMS),
    "oak_log": _CollectRoute("oak_log", "oak_log", ("oak_log",), ("oak_log",)),
    "spruce_log": _CollectRoute("spruce_log", "spruce_log", ("spruce_log",), ("spruce_log",)),
    "birch_log": _CollectRoute("birch_log", "birch_log", ("birch_log",), ("birch_log",)),
    "jungle_log": _CollectRoute("jungle_log", "jungle_log", ("jungle_log",), ("jungle_log",)),
    "acacia_log": _CollectRoute("acacia_log", "acacia_log", ("acacia_log",), ("acacia_log",)),
    "dark_oak_log": _CollectRoute("dark_oak_log", "dark_oak_log", ("dark_oak_log",), ("dark_oak_log",)),
    "cobblestone": _CollectRoute("cobblestone", "cobblestone", ("stone", "cobblestone"), ("cobblestone",)),
    "coal": _CollectRoute("coal", "coal", ("coal_ore", "deepslate_coal_ore"), ("coal",)),
    "raw_iron": _CollectRoute("raw_iron", "raw_iron", ("iron_ore", "deepslate_iron_ore"), ("raw_iron",)),
    "raw_gold": _CollectRoute("raw_gold", "raw_gold", ("gold_ore", "deepslate_gold_ore"), ("raw_gold",)),
    "raw_copper": _CollectRoute("raw_copper", "raw_copper", ("copper_ore", "deepslate_copper_ore"), ("raw_copper",)),
    "diamond": _CollectRoute("diamond", "diamond", ("diamond_ore", "deepslate_diamond_ore"), ("diamond",)),
}

SMELT_ROUTES: dict[str, _SmeltRoute] = {
    "iron_ingot": _SmeltRoute("raw_iron"),
    "gold_ingot": _SmeltRoute("raw_gold"),
    "copper_ingot": _SmeltRoute("raw_copper"),
    "charcoal": _SmeltRoute("oak_log"),
    "stone": _SmeltRoute("cobblestone"),
}

SMELT_SECONDS_PER_ITEM = 10.0


def resolve_acquisition(
    item: str,
    count: int,
    counts: dict[str, int],
    recipe_lookup: RecipeLookup,
    *,
    max_depth: int = 6,
) -> list[AcquisitionStep] | AcquisitionError:
    planner = _Planner(counts, recipe_lookup, max_depth=max_depth)
    error = planner.resolve(_normalize_item(item), int(count), ())
    if error is not None:
        return error
    return _compact_steps(planner.steps)


class _Planner:
    def __init__(self, counts: dict[str, int], recipe_lookup: RecipeLookup, *, max_depth: int):
        self.counts = _normalize_counts(counts)
        self.recipe_lookup = recipe_lookup
        self.max_depth = max_depth
        self.steps: list[AcquisitionStep] = []

    def resolve(self, item: str, count: int, chain: tuple[str, ...]) -> AcquisitionError | None:
        item = _normalize_item(item)
        if count <= 0 or self.counts.get(item, 0) >= count:
            return None
        if len(chain) >= self.max_depth:
            return self._error("max_depth_exceeded", item, count, chain, {"max_depth": self.max_depth})
        if item in chain:
            return self._error("recipe_cycle", item, count, chain, {"cycle": (*chain, item)})

        route = COLLECT_ROUTES.get(item)
        if route is not None:
            return self._resolve_collect(item, count, route, chain)

        smelt = SMELT_ROUTES.get(item)
        if smelt is not None:
            return self._resolve_smelt(item, count, smelt, chain)

        variants = self.recipe_lookup(item)
        if not variants:
            return self._error("unplannable", item, count, chain, {"missing_recipe": item})
        return self._resolve_craft(item, count, variants, chain)

    def _resolve_collect(
        self,
        item: str,
        count: int,
        route: _CollectRoute,
        chain: tuple[str, ...],
    ) -> AcquisitionError | None:
        missing = max(0, count - self.counts.get(route.inventory_item, 0))
        required_tier = _strongest_required_tier(route.block_types)
        if required_tier is not None:
            best = best_owned_pickaxe(self.counts)
            if best is None or not tier_satisfies(best[1], required_tier):
                tool_item = PICKAXE_BY_TIER[required_tier]
                before_tool_count = self.counts.get(tool_item, 0)
                error = self.resolve(tool_item, 1, (*chain, item))
                if error is not None:
                    return error
                best = best_owned_pickaxe(self.counts)
                if best is None or not tier_satisfies(best[1], required_tier):
                    return self._error(
                        "required_tool_unavailable",
                        item,
                        count,
                        chain,
                        {"required_tier": required_tier, "tool_item": tool_item, "best_owned": best},
                    )
                if before_tool_count <= 0 and self.counts.get(tool_item, 0) > 0:
                    self._add_step(
                        "equip",
                        tool_item,
                        1,
                        {"required_tier": required_tier, "for_item": item},
                    )
        if missing <= 0:
            return None
        self._add_step(
            "collect",
            route.collect_item,
            missing,
            {
                "inventory_item": route.inventory_item,
                "block_types": list(route.block_types),
                "expected_drops": list(route.expected_drops),
                "required_tier": required_tier,
            },
        )
        self.counts[route.inventory_item] = self.counts.get(route.inventory_item, 0) + missing
        return None

    def _resolve_smelt(
        self,
        item: str,
        count: int,
        route: _SmeltRoute,
        chain: tuple[str, ...],
    ) -> AcquisitionError | None:
        missing = max(0, count - self.counts.get(item, 0))
        if missing <= 0:
            return None
        crafts = ceil(missing / route.output_count)
        input_needed = crafts * route.input_count
        error = self.resolve(route.input_item, self.counts.get(route.input_item, 0) + input_needed, (*chain, item))
        if error is not None:
            return error
        if self.counts.get("furnace", 0) < 1:
            error = self.resolve("furnace", 1, (*chain, item))
            if error is not None:
                return error

        seconds_needed = crafts * SMELT_SECONDS_PER_ITEM
        fuel = _select_fuel(self.counts, seconds_needed)
        if fuel is None:
            planks_needed = ceil(seconds_needed / FUEL_BURN_SECONDS["oak_planks"])
            error = self.resolve("oak_planks", self.counts.get("oak_planks", 0) + planks_needed, (*chain, item))
            if error is not None:
                return error
            fuel = _select_fuel(self.counts, seconds_needed)
        if fuel is None:
            return self._error(
                "fuel_unavailable",
                item,
                count,
                chain,
                {"seconds_needed": seconds_needed, "usable_fuels": _usable_fuels(self.counts)},
            )

        fuel_item, fuel_count = fuel
        output_count = crafts * route.output_count
        self._add_step(
            "smelt",
            item,
            output_count,
            {
                "input_item": route.input_item,
                "input_count": input_needed,
                "fuel_item": fuel_item,
                "fuel_count": fuel_count,
                "seconds_needed": seconds_needed,
            },
        )
        self.counts[route.input_item] = max(0, self.counts.get(route.input_item, 0) - input_needed)
        self.counts[fuel_item] = max(0, self.counts.get(fuel_item, 0) - fuel_count)
        self.counts[item] = self.counts.get(item, 0) + output_count
        return None

    def _resolve_craft(
        self,
        item: str,
        count: int,
        variants: list[RecipeVariant],
        chain: tuple[str, ...],
    ) -> AcquisitionError | None:
        missing = max(0, count - self.counts.get(item, 0))
        if missing <= 0:
            return None
        variant = _choose_variant(item, missing, variants, self.counts)
        if variant is None:
            return self._error("recipe_not_found", item, count, chain, {"variant_count": len(variants)})

        if variant.requires_table and item != "crafting_table" and self.counts.get("crafting_table", 0) < 1:
            error = self.resolve("crafting_table", 1, (*chain, item))
            if error is not None:
                return error

        crafts = ceil(missing / variant.output_count)
        required = _select_requirements(variant, crafts, self.counts)
        for _ in range(self.max_depth + 1):
            error = self._resolve_requirements(required, chain, item)
            if error is not None:
                return error
            remaining = _missing_requirements(required, self.counts)
            if not remaining:
                break
            required = tuple(remaining.items())
        else:
            return self._error(
                "ingredient_resolution_failed",
                item,
                count,
                chain,
                {"requirements": dict(required), "counts": dict(self.counts)},
            )

        crafted_count = crafts * variant.output_count
        self._add_step(
            "craft",
            item,
            crafted_count,
            {
                "recipe_kind": variant.recipe_kind,
                "requires_table": variant.requires_table,
                "output_count": variant.output_count,
                "ingredients": dict(required),
            },
        )
        for ingredient, ingredient_count in required:
            self.counts[ingredient] = max(0, self.counts.get(ingredient, 0) - ingredient_count)
        self.counts[item] = self.counts.get(item, 0) + crafted_count
        return None

    def _resolve_requirements(
        self,
        required: tuple[tuple[str, int], ...],
        chain: tuple[str, ...],
        item: str,
    ) -> AcquisitionError | None:
        for ingredient, ingredient_count in sorted(required, key=lambda entry: _ingredient_priority(entry[0])):
            error = self.resolve(ingredient, ingredient_count, (*chain, item))
            if error is not None:
                return error
        return None

    def _add_step(
        self,
        kind: Literal["collect", "craft", "smelt", "equip"],
        item: str,
        count: int,
        detail: dict[str, object],
    ) -> None:
        self.steps.append(AcquisitionStep(kind=kind, item=item, count=count, detail=detail))

    def _error(
        self,
        reason: str,
        item: str,
        count: int,
        chain: tuple[str, ...],
        detail: dict[str, object],
    ) -> AcquisitionError:
        return AcquisitionError(reason=reason, item=item, count=count, chain=chain, detail=detail)


def _choose_variant(
    item: str,
    missing: int,
    variants: list[RecipeVariant],
    counts: dict[str, int],
) -> RecipeVariant | None:
    normalized = _normalize_item(item)
    candidates = [
        variant
        for variant in variants
        if _normalize_item(variant.output_item) == normalized and variant.output_count > 0
    ]
    if not candidates:
        return None

    def score(variant: RecipeVariant) -> tuple[int, int, int]:
        crafts = ceil(missing / variant.output_count)
        requirements = _select_requirements(variant, crafts, counts)
        deficit = sum(max(0, required - counts.get(ingredient, 0)) for ingredient, required in requirements)
        return (deficit, int(variant.requires_table), len(variant.ingredient_groups))

    return sorted(candidates, key=score)[0]


def _select_requirements(
    variant: RecipeVariant,
    crafts: int,
    counts: dict[str, int],
) -> tuple[tuple[str, int], ...]:
    selected: dict[str, int] = {}
    for group in variant.ingredient_groups:
        normalized_group = tuple(_normalize_item(option) for option in group if option)
        if not normalized_group:
            continue
        option = sorted(
            normalized_group,
            key=lambda candidate: (
                0 if counts.get(candidate, 0) > selected.get(candidate, 0) else 1,
                _item_preference(candidate),
                candidate,
            ),
        )[0]
        selected[option] = selected.get(option, 0) + crafts
    return tuple(sorted(selected.items()))


def _missing_requirements(
    required: tuple[tuple[str, int], ...],
    counts: dict[str, int],
) -> dict[str, int]:
    return {item: count for item, count in required if counts.get(item, 0) < count}


def _ingredient_priority(item: str) -> tuple[int, str]:
    if item == "stick":
        return (0, item)
    if item in PLANK_ITEMS:
        return (2, item)
    return (1, item)


def _item_preference(item: str) -> int:
    if item in PLANK_ITEMS:
        return PLANK_ITEMS.index(item)
    if item in LOG_ITEMS:
        return LOG_ITEMS.index(item)
    return len(PLANK_ITEMS) + len(LOG_ITEMS)


def _strongest_required_tier(block_types: tuple[str, ...]) -> str | None:
    strongest: str | None = None
    for block_type in block_types:
        required = required_pickaxe_tier(block_type)
        if required is None:
            continue
        if strongest is None or not tier_satisfies(strongest, required):
            strongest = required
    return strongest


def _select_fuel(counts: dict[str, int], seconds_needed: float) -> tuple[str, int] | None:
    if seconds_needed <= 0:
        return None
    for fuel in FUEL_SELECTION_ORDER:
        seconds_per_item = FUEL_BURN_SECONDS[fuel]
        fuel_count = ceil(seconds_needed / seconds_per_item)
        if counts.get(fuel, 0) >= fuel_count:
            return fuel, max(1, fuel_count)
    return None


def _usable_fuels(counts: dict[str, int]) -> list[dict[str, object]]:
    fuels: list[dict[str, object]] = []
    for fuel in FUEL_SELECTION_ORDER:
        count = counts.get(fuel, 0)
        if count > 0:
            fuels.append({"item": fuel, "count": count, "seconds_per_item": FUEL_BURN_SECONDS[fuel]})
    return fuels


def _compact_steps(steps: list[AcquisitionStep]) -> list[AcquisitionStep]:
    merged: OrderedDict[tuple[object, ...], AcquisitionStep] = OrderedDict()
    for step in steps:
        key = _step_key(step)
        existing = merged.get(key)
        if existing is None:
            merged[key] = step
            continue
        merged[key] = _merge_step(existing, step)
    return list(merged.values())


def _step_key(step: AcquisitionStep) -> tuple[object, ...]:
    if step.kind == "collect":
        return (step.kind, step.item, tuple(step.detail.get("expected_drops") or ()))
    if step.kind == "craft":
        return (step.kind, step.item, step.detail.get("recipe_kind"), step.detail.get("requires_table"))
    if step.kind == "smelt":
        return (step.kind, step.item, step.detail.get("input_item"), step.detail.get("fuel_item"))
    return (step.kind, step.item)


def _merge_step(left: AcquisitionStep, right: AcquisitionStep) -> AcquisitionStep:
    detail = dict(left.detail)
    if left.kind == "smelt":
        detail["input_count"] = int(detail.get("input_count") or 0) + int(right.detail.get("input_count") or 0)
        detail["fuel_count"] = int(detail.get("fuel_count") or 0) + int(right.detail.get("fuel_count") or 0)
        detail["seconds_needed"] = float(detail.get("seconds_needed") or 0.0) + float(right.detail.get("seconds_needed") or 0.0)
    return AcquisitionStep(left.kind, left.item, left.count + right.count, detail)


def _normalize_counts(counts: dict[str, int]) -> dict[str, int]:
    normalized: dict[str, int] = {}
    for raw_item, raw_count in counts.items():
        item = _normalize_item(raw_item)
        try:
            count = int(raw_count or 0)
        except (TypeError, ValueError):
            continue
        if item and count > 0:
            normalized[item] = normalized.get(item, 0) + count
    return normalized


def _normalize_item(item: object) -> str:
    return str(item).removeprefix("minecraft:").strip().lower().replace(" ", "_")
