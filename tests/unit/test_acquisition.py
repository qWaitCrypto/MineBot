import unittest

from minebot.brain.acquisition import AcquisitionError, RecipeVariant, resolve_acquisition


def recipe_lookup(item: str) -> list[RecipeVariant] | None:
    recipes = {
        "oak_planks": [
            RecipeVariant(
                output_item="oak_planks",
                output_count=4,
                ingredient_groups=(("oak_log",),),
            )
        ],
        "stick": [
            RecipeVariant(
                output_item="stick",
                output_count=4,
                ingredient_groups=(("oak_planks", "spruce_planks", "birch_planks"), ("oak_planks", "spruce_planks", "birch_planks")),
            )
        ],
        "crafting_table": [
            RecipeVariant(
                output_item="crafting_table",
                output_count=1,
                ingredient_groups=(("oak_planks",), ("oak_planks",), ("oak_planks",), ("oak_planks",)),
            )
        ],
        "wooden_pickaxe": [
            RecipeVariant(
                output_item="wooden_pickaxe",
                output_count=1,
                ingredient_groups=(("oak_planks",), ("oak_planks",), ("oak_planks",), ("stick",), ("stick",)),
                requires_table=True,
            )
        ],
        "stone_pickaxe": [
            RecipeVariant(
                output_item="stone_pickaxe",
                output_count=1,
                ingredient_groups=(("cobblestone",), ("cobblestone",), ("cobblestone",), ("stick",), ("stick",)),
                requires_table=True,
            )
        ],
        "iron_pickaxe": [
            RecipeVariant(
                output_item="iron_pickaxe",
                output_count=1,
                ingredient_groups=(("iron_ingot",), ("iron_ingot",), ("iron_ingot",), ("stick",), ("stick",)),
                requires_table=True,
            )
        ],
        "furnace": [
            RecipeVariant(
                output_item="furnace",
                output_count=1,
                ingredient_groups=tuple(("cobblestone",) for _ in range(8)),
                requires_table=True,
            )
        ],
    }
    return recipes.get(item)


class AcquisitionResolverTests(unittest.TestCase):
    def test_empty_backpack_plans_iron_pickaxe_ladder(self):
        steps = resolve_acquisition("iron_pickaxe", 1, {}, recipe_lookup, max_depth=16)

        self.assertIsInstance(steps, list)
        sequence = [(step.kind, step.item) for step in steps]
        self.assertIn(("collect", "oak_log"), sequence)
        self.assertIn(("craft", "oak_planks"), sequence)
        self.assertIn(("craft", "stick"), sequence)
        self.assertIn(("craft", "crafting_table"), sequence)
        self.assertIn(("craft", "wooden_pickaxe"), sequence)
        self.assertIn(("equip", "wooden_pickaxe"), sequence)
        self.assertIn(("collect", "cobblestone"), sequence)
        self.assertIn(("craft", "stone_pickaxe"), sequence)
        self.assertIn(("equip", "stone_pickaxe"), sequence)
        self.assertIn(("collect", "raw_iron"), sequence)
        self.assertIn(("craft", "furnace"), sequence)
        self.assertIn(("smelt", "iron_ingot"), sequence)
        self.assertEqual(sequence[-1], ("craft", "iron_pickaxe"))

        smelt_step = next(step for step in steps if step.kind == "smelt" and step.item == "iron_ingot")
        self.assertEqual(smelt_step.count, 3)
        self.assertEqual(smelt_step.detail["input_item"], "raw_iron")
        self.assertEqual(smelt_step.detail["input_count"], 3)
        self.assertIn(smelt_step.detail["fuel_item"], {"oak_planks", "oak_log"})

    def test_merged_craft_steps_keep_ingredient_counts_precise(self):
        steps = resolve_acquisition("iron_pickaxe", 1, {}, recipe_lookup, max_depth=16)

        self.assertIsInstance(steps, list)
        planks = next(step for step in steps if step.kind == "craft" and step.item == "oak_planks")
        self.assertEqual(planks.count, 16)
        self.assertEqual(planks.detail["ingredients"], {"oak_log": 4})
        sticks = next(step for step in steps if step.kind == "craft" and step.item == "stick")
        self.assertEqual(sticks.count, 8)
        self.assertEqual(sticks.detail["ingredients"], {"oak_planks": 4})

    def test_empty_backpack_plans_diamond_after_iron_pickaxe(self):
        steps = resolve_acquisition("diamond", 3, {}, recipe_lookup, max_depth=18)

        self.assertIsInstance(steps, list)
        sequence = [(step.kind, step.item) for step in steps]
        self.assertIn(("craft", "iron_pickaxe"), sequence)
        self.assertIn(("equip", "iron_pickaxe"), sequence)
        self.assertEqual(sequence[-1], ("collect", "diamond"))
        diamond = steps[-1]
        self.assertEqual(diamond.count, 3)
        self.assertEqual(diamond.detail["required_tier"], "iron")

    def test_existing_pickaxe_prunes_lower_ladder(self):
        steps = resolve_acquisition(
            "diamond",
            2,
            {"iron_pickaxe": 1},
            recipe_lookup,
            max_depth=8,
        )

        self.assertIsInstance(steps, list)
        self.assertEqual([(step.kind, step.item) for step in steps], [("collect", "diamond")])

    def test_existing_stone_pickaxe_prunes_wooden_ladder_for_iron_pickaxe(self):
        steps = resolve_acquisition(
            "iron_pickaxe",
            1,
            {"stone_pickaxe": 1, "oak_planks": 3, "stick": 2, "crafting_table": 1},
            recipe_lookup,
            max_depth=12,
        )

        self.assertIsInstance(steps, list)
        sequence = [(step.kind, step.item) for step in steps]
        self.assertNotIn(("craft", "wooden_pickaxe"), sequence)
        self.assertIn(("collect", "raw_iron"), sequence)
        self.assertIn(("smelt", "iron_ingot"), sequence)
        self.assertEqual(sequence[-1], ("craft", "iron_pickaxe"))

    def test_live_recipe_groups_prefer_reachable_cobblestone_over_blackstone(self):
        def live_like_lookup(item: str) -> list[RecipeVariant] | None:
            if item == "stone_pickaxe":
                return [
                    RecipeVariant(
                        output_item="stone_pickaxe",
                        output_count=1,
                        ingredient_groups=(
                            ("cobblestone", "blackstone", "cobbled_deepslate"),
                            ("cobblestone", "blackstone", "cobbled_deepslate"),
                            ("cobblestone", "blackstone", "cobbled_deepslate"),
                            ("stick",),
                            ("stick",),
                        ),
                        requires_table=True,
                    )
                ]
            return recipe_lookup(item)

        steps = resolve_acquisition(
            "stone_pickaxe",
            1,
            {"wooden_pickaxe": 1, "crafting_table": 1, "stick": 2},
            live_like_lookup,
            max_depth=8,
        )

        self.assertIsInstance(steps, list)
        collect = next(step for step in steps if step.kind == "collect" and step.item == "cobblestone")
        self.assertEqual(collect.count, 3)
        self.assertNotIn(("collect", "blackstone"), [(step.kind, step.item) for step in steps])

    def test_existing_resources_prune_collection_and_smelt_inputs(self):
        steps = resolve_acquisition(
            "iron_pickaxe",
            1,
            {
                "stone_pickaxe": 1,
                "crafting_table": 1,
                "furnace": 1,
                "raw_iron": 3,
                "oak_planks": 5,
                "stick": 2,
            },
            recipe_lookup,
            max_depth=8,
        )

        self.assertIsInstance(steps, list)
        sequence = [(step.kind, step.item) for step in steps]
        self.assertNotIn(("collect", "raw_iron"), sequence)
        self.assertNotIn(("craft", "furnace"), sequence)
        self.assertEqual(sequence, [("smelt", "iron_ingot"), ("craft", "iron_pickaxe")])

    def test_smelt_bootstraps_plank_fuel_when_none_owned(self):
        steps = resolve_acquisition(
            "iron_ingot",
            1,
            {"raw_iron": 1, "furnace": 1},
            recipe_lookup,
            max_depth=3,
        )

        self.assertIsInstance(steps, list)
        self.assertEqual(
            [(step.kind, step.item) for step in steps],
            [("collect", "oak_log"), ("craft", "oak_planks"), ("smelt", "iron_ingot")],
        )
        smelt = steps[-1]
        self.assertEqual(smelt.detail["fuel_item"], "oak_planks")
        self.assertEqual(smelt.detail["fuel_count"], 1)

    def test_smelt_reports_unplannable_when_fuel_recipe_missing(self):
        result = resolve_acquisition(
            "iron_ingot",
            1,
            {"raw_iron": 1, "furnace": 1},
            lambda item: None,
            max_depth=4,
        )

        self.assertIsInstance(result, AcquisitionError)
        self.assertEqual(result.reason, "unplannable")
        self.assertEqual(result.item, "oak_planks")

    def test_recipe_cycle_returns_unplannable_error_with_chain(self):
        def cyclic_lookup(item: str) -> list[RecipeVariant] | None:
            if item == "loop_ingot":
                return [
                    RecipeVariant(
                        output_item="loop_ingot",
                        output_count=9,
                        ingredient_groups=(("loop_block",),),
                    )
                ]
            if item == "loop_block":
                return [
                    RecipeVariant(
                        output_item="loop_block",
                        output_count=1,
                        ingredient_groups=tuple(("loop_ingot",) for _ in range(9)),
                    )
                ]
            return None

        result = resolve_acquisition("loop_block", 1, {}, cyclic_lookup, max_depth=8)

        self.assertIsInstance(result, AcquisitionError)
        self.assertEqual(result.reason, "recipe_cycle")
        self.assertIn("loop_block", result.detail["cycle"])

    def test_route_priority_avoids_iron_ingot_recipe_cycle(self):
        def hostile_lookup(item: str) -> list[RecipeVariant] | None:
            if item == "iron_ingot":
                return [
                    RecipeVariant(
                        output_item="iron_ingot",
                        output_count=9,
                        ingredient_groups=(("iron_block",),),
                    )
                ]
            return recipe_lookup(item)

        steps = resolve_acquisition("iron_ingot", 3, {"stone_pickaxe": 1, "furnace": 1, "oak_planks": 2}, hostile_lookup, max_depth=8)

        self.assertIsInstance(steps, list)
        self.assertEqual([(step.kind, step.item) for step in steps], [("collect", "raw_iron"), ("smelt", "iron_ingot")])

    def test_unknown_item_reports_unplannable(self):
        result = resolve_acquisition("elytra", 1, {}, recipe_lookup, max_depth=6)

        self.assertIsInstance(result, AcquisitionError)
        self.assertEqual(result.reason, "unplannable")
        self.assertEqual(result.item, "elytra")

    def test_depth_limit_reports_error(self):
        result = resolve_acquisition("iron_pickaxe", 1, {}, recipe_lookup, max_depth=2)

        self.assertIsInstance(result, AcquisitionError)
        self.assertEqual(result.reason, "max_depth_exceeded")

    def test_already_satisfied_returns_empty_plan(self):
        result = resolve_acquisition("iron_pickaxe", 1, {"minecraft:iron_pickaxe": 1}, recipe_lookup)

        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
