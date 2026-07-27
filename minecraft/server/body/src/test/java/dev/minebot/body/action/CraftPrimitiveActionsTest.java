package dev.minebot.body.action;

import net.minecraft.core.registries.Registries;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.core.component.DataComponentMap;
import net.minecraft.core.component.DataComponents;
import net.minecraft.resources.Identifier;
import net.minecraft.resources.ResourceKey;
import net.minecraft.server.Bootstrap;
import net.minecraft.SharedConstants;
import net.minecraft.world.SimpleContainer;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.item.ItemStackTemplate;
import net.minecraft.world.item.Items;
import net.minecraft.world.item.crafting.CraftingBookCategory;
import net.minecraft.world.item.crafting.CraftingRecipe;
import net.minecraft.world.item.crafting.Ingredient;
import net.minecraft.world.item.crafting.Recipe;
import net.minecraft.world.item.crafting.RecipeHolder;
import net.minecraft.world.item.crafting.ShapelessRecipe;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.BeforeAll;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

final class CraftPrimitiveActionsTest {
    @BeforeAll
    static void bootstrapMinecraftRegistries() {
        SharedConstants.tryDetectVersion();
        Bootstrap.bootStrap();
        BuiltInRegistries.ITEM.listElements().forEach(
            holder -> holder.bindComponents(
                DataComponentMap.builder().set(DataComponents.MAX_STACK_SIZE, 64).build()
            )
        );
    }

    @Test
    void liveRecipeConsumesExactInputAndProducesObservedOutput() {
        SimpleContainer inventory = new SimpleContainer(46);
        inventory.setItem(0, new ItemStack(Items.OAK_LOG, 2));
        CraftPrimitiveActions.Request request = request(
            List.of(new CraftPrimitiveActions.Input(0, "minecraft:oak_log", 2)),
            new CraftPrimitiveActions.Output(1, "minecraft:oak_planks", 8),
            List.of()
        );

        var outcome = CraftPrimitiveActions.craft(
            request,
            inventory,
            List.of(planksRecipe()),
            null
        );

        assertTrue(outcome.success(), outcome.facts().toString());
        assertEquals("completed", outcome.reason());
        assertTrue(inventory.getItem(0).isEmpty());
        assertEquals(Items.OAK_PLANKS, inventory.getItem(1).getItem());
        assertEquals(8, inventory.getItem(1).getCount());
        assertEquals(2, outcome.facts().get("crafts").getAsInt());
    }

    @Test
    void forgedOutputIsRejectedWithoutChangingInventory() {
        SimpleContainer inventory = new SimpleContainer(46);
        inventory.setItem(0, new ItemStack(Items.OAK_LOG, 1));
        CraftPrimitiveActions.Request request = request(
            List.of(new CraftPrimitiveActions.Input(0, "minecraft:oak_log", 1)),
            new CraftPrimitiveActions.Output(1, "minecraft:diamond", 64),
            List.of()
        );

        var outcome = CraftPrimitiveActions.craft(
            request,
            inventory,
            List.of(planksRecipe()),
            null
        );

        assertFalse(outcome.success());
        assertEquals("recipe_mismatch", outcome.reason());
        assertEquals(1, inventory.getItem(0).getCount());
        assertTrue(inventory.getItem(1).isEmpty());
    }

    @Test
    void serverRecipeRemainderMustMatchBeforeAnyMutation() {
        SimpleContainer inventory = new SimpleContainer(46);
        inventory.setItem(0, new ItemStack(Items.MILK_BUCKET, 1));
        RecipeHolder<?> recipe = shapeless(
            "test:bucket_remainder",
            new ItemStackTemplate(Items.CAKE, 1),
            Ingredient.of(Items.MILK_BUCKET)
        );
        CraftPrimitiveActions.Request valid = request(
            List.of(new CraftPrimitiveActions.Input(0, "minecraft:milk_bucket", 1)),
            new CraftPrimitiveActions.Output(1, "minecraft:cake", 1),
            List.of(new CraftPrimitiveActions.Remainder(0, "minecraft:bucket", 1))
        );

        var outcome = CraftPrimitiveActions.craft(valid, inventory, List.of(recipe), null);

        assertTrue(outcome.success(), outcome.facts().toString());
        assertEquals(Items.BUCKET, inventory.getItem(0).getItem());
        assertEquals(Items.CAKE, inventory.getItem(1).getItem());

        SimpleContainer rejectedInventory = new SimpleContainer(46);
        rejectedInventory.setItem(0, new ItemStack(Items.MILK_BUCKET, 1));
        CraftPrimitiveActions.Request forgedRemainder = request(
            List.of(new CraftPrimitiveActions.Input(0, "minecraft:milk_bucket", 1)),
            new CraftPrimitiveActions.Output(1, "minecraft:cake", 1),
            List.of(new CraftPrimitiveActions.Remainder(0, "minecraft:diamond", 1))
        );

        var rejected = CraftPrimitiveActions.craft(
            forgedRemainder, rejectedInventory, List.of(recipe), null
        );

        assertFalse(rejected.success());
        assertEquals("recipe_mismatch", rejected.reason());
        assertEquals(Items.MILK_BUCKET, rejectedInventory.getItem(0).getItem());
        assertTrue(rejectedInventory.getItem(1).isEmpty());
    }

    private static CraftPrimitiveActions.Request request(
        List<CraftPrimitiveActions.Input> inputs,
        CraftPrimitiveActions.Output output,
        List<CraftPrimitiveActions.Remainder> remainders
    ) {
        return new CraftPrimitiveActions.Request(inputs, output, remainders, 64);
    }

    private static RecipeHolder<?> planksRecipe() {
        return shapeless(
            "minecraft:oak_planks",
            new ItemStackTemplate(Items.OAK_PLANKS, 4),
            Ingredient.of(Items.OAK_LOG)
        );
    }

    private static RecipeHolder<CraftingRecipe> shapeless(
        String id,
        ItemStackTemplate output,
        Ingredient... ingredients
    ) {
        ShapelessRecipe recipe = new ShapelessRecipe(
            new Recipe.CommonInfo(true),
            new CraftingRecipe.CraftingBookInfo(CraftingBookCategory.MISC, ""),
            output,
            List.of(ingredients)
        );
        return new RecipeHolder<>(
            ResourceKey.create(Registries.RECIPE, Identifier.parse(id)),
            recipe
        );
    }
}
