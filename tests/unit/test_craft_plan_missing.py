from minebot.body.inventory import CraftRecipeVariant, _choose_craft_plan
from minebot.contract import InventorySlot

PLANKS = ("oak_planks", "spruce_planks", "birch_planks")
PICKAXE = CraftRecipeVariant(
    output_item="wooden_pickaxe", output_count=1, recipe_kind="shaped", width=3, height=3,
    ingredient_groups=(PLANKS, PLANKS, PLANKS, None, ("stick",), None, None, ("stick",), None),
    requires_table=True,
)


def _slot(index, item, count):
    return InventorySlot(slot=index, item=f"minecraft:{item}", count=count, empty=False)


def test_short_craft_says_what_is_missing_and_how_much():
    # Villa run 2026-10-15: 2 planks, 5 sticks and 10 logs; the result only
    # said "gather the missing ingredients" and the Agent spent minutes on it.
    slots = [_slot(0, "oak_planks", 2), _slot(1, "stick", 5), _slot(2, "oak_log", 10)]
    result = _choose_craft_plan(slots, item="wooden_pickaxe", count=1, output_slot=None, variants=[PICKAXE])
    assert result.reason == "craft_plan_not_available"
    assert result.metrics["missing"] == [{"any_of": list(PLANKS), "need": 3, "have": 2}]
    assert result.next_suggestion == (
        "To craft 1 wooden_pickaxe the inventory still lacks: "
        "1 more oak_planks or another of 3 kinds (needs 3, has 2)."
    )


def test_enough_ingredients_still_plans():
    slots = [_slot(0, "birch_planks", 3), _slot(1, "stick", 2)]
    plan = _choose_craft_plan(slots, item="wooden_pickaxe", count=1, output_slot=None, variants=[PICKAXE])
    assert plan.item == "minecraft:wooden_pickaxe" and plan.crafted_count == 1
