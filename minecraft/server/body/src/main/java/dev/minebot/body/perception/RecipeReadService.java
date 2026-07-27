package dev.minebot.body.perception;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonNull;
import com.google.gson.JsonObject;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.util.context.ContextMap;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.item.crafting.AbstractCookingRecipe;
import net.minecraft.world.item.crafting.CraftingRecipe;
import net.minecraft.world.item.crafting.RecipeHolder;
import net.minecraft.world.item.crafting.RecipeType;
import net.minecraft.world.item.crafting.display.FurnaceRecipeDisplay;
import net.minecraft.world.item.crafting.display.RecipeDisplay;
import net.minecraft.world.item.crafting.display.ShapedCraftingRecipeDisplay;
import net.minecraft.world.item.crafting.display.ShapelessCraftingRecipeDisplay;
import net.minecraft.world.item.crafting.display.SlotDisplay;
import net.minecraft.world.item.crafting.display.SlotDisplayContext;

import java.util.ArrayList;
import java.util.Collection;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/** Structured server recipe truth for the neutral recipeData perception. */
public final class RecipeReadService {
    private RecipeReadService() {}

    public static JsonArray read(
        Collection<RecipeHolder<?>> recipes,
        ServerLevel level,
        String itemId,
        String recipeType
    ) {
        ContextMap context = SlotDisplayContext.fromLevel(level);
        List<RecipeHolder<?>> ordered = new ArrayList<>(recipes);
        ordered.sort(Comparator.comparing(holder -> holder.id().identifier().toString()));
        Map<String, JsonObject> unique = new LinkedHashMap<>();
        for (RecipeHolder<?> holder : ordered) {
            if (recipeType.equals("crafting") && holder.value() instanceof CraftingRecipe crafting) {
                for (RecipeDisplay display : crafting.display()) {
                    JsonObject variant = craftingVariant(holder, display, context, itemId);
                    if (variant != null) {
                        unique.putIfAbsent(semanticKey(variant), variant);
                    }
                }
            } else if (recipeType.equals("smelting")
                && holder.value().getType() == RecipeType.SMELTING
                && holder.value() instanceof AbstractCookingRecipe cooking) {
                for (RecipeDisplay display : cooking.display()) {
                    JsonObject variant = cookingVariant(holder, display, context, itemId);
                    if (variant != null) {
                        unique.putIfAbsent(semanticKey(variant), variant);
                    }
                }
            }
        }
        JsonArray result = new JsonArray();
        unique.values().forEach(result::add);
        return result;
    }

    private static JsonObject craftingVariant(
        RecipeHolder<?> holder,
        RecipeDisplay display,
        ContextMap context,
        String requestedItem
    ) {
        List<SlotDisplay> ingredients;
        String kind;
        int width;
        int height;
        if (display instanceof ShapedCraftingRecipeDisplay shaped) {
            ingredients = shaped.ingredients();
            kind = "shaped";
            width = shaped.width();
            height = shaped.height();
        } else if (display instanceof ShapelessCraftingRecipeDisplay shapeless) {
            ingredients = shapeless.ingredients();
            kind = "shapeless";
            width = 0;
            height = 0;
        } else {
            return null;
        }
        ItemStack output = matchingOutput(display.result(), context, requestedItem);
        if (output == null) {
            return null;
        }
        JsonArray groups = ingredientGroups(ingredients, context);
        if (groups == null) {
            return null;
        }
        int ingredientCount = 0;
        for (JsonElement group : groups) {
            if (!group.isJsonNull()) {
                ingredientCount++;
            }
        }
        return variant(
            holder,
            output,
            kind,
            width,
            height,
            groups,
            (kind.equals("shaped") && (width > 2 || height > 2))
                || (kind.equals("shapeless") && ingredientCount > 4)
        );
    }

    private static JsonObject cookingVariant(
        RecipeHolder<?> holder,
        RecipeDisplay display,
        ContextMap context,
        String requestedItem
    ) {
        if (!(display instanceof FurnaceRecipeDisplay furnace)) {
            return null;
        }
        ItemStack output = matchingOutput(furnace.result(), context, requestedItem);
        if (output == null) {
            return null;
        }
        JsonArray groups = ingredientGroups(List.of(furnace.ingredient()), context);
        if (groups == null) {
            return null;
        }
        return variant(holder, output, "smelting", 1, 1, groups, false);
    }

    private static JsonObject variant(
        RecipeHolder<?> holder,
        ItemStack output,
        String kind,
        int width,
        int height,
        JsonArray groups,
        boolean requiresTable
    ) {
        JsonObject variant = new JsonObject();
        variant.addProperty("recipe_id", holder.id().identifier().toString());
        variant.addProperty("output_item", itemId(output));
        variant.addProperty("output_count", output.getCount());
        variant.addProperty("recipe_kind", kind);
        variant.addProperty("width", width);
        variant.addProperty("height", height);
        variant.add("ingredient_groups", groups);
        variant.addProperty("requires_table", requiresTable);
        return variant;
    }

    private static JsonArray ingredientGroups(List<SlotDisplay> displays, ContextMap context) {
        JsonArray groups = new JsonArray();
        for (SlotDisplay display : displays) {
            if (display instanceof SlotDisplay.Empty) {
                groups.add(JsonNull.INSTANCE);
                continue;
            }
            SlotDisplay source = display instanceof SlotDisplay.WithRemainder withRemainder
                ? withRemainder.input()
                : display;
            Set<String> items = new LinkedHashSet<>();
            for (ItemStack stack : source.resolveForStacks(context)) {
                if (!stack.isEmpty()) {
                    items.add(itemId(stack));
                }
            }
            if (items.isEmpty()) {
                return null;
            }
            JsonArray group = new JsonArray();
            items.forEach(group::add);
            groups.add(group);
        }
        return groups;
    }

    private static ItemStack matchingOutput(SlotDisplay display, ContextMap context, String requestedItem) {
        for (ItemStack stack : display.resolveForStacks(context)) {
            if (!stack.isEmpty() && itemId(stack).equals(requestedItem)) {
                return stack;
            }
        }
        return null;
    }

    private static String semanticKey(JsonObject variant) {
        return variant.get("output_item") + "|"
            + variant.get("output_count") + "|"
            + variant.get("recipe_kind") + "|"
            + variant.get("width") + "|"
            + variant.get("height") + "|"
            + variant.get("ingredient_groups");
    }

    private static String itemId(ItemStack stack) {
        return BuiltInRegistries.ITEM.getKey(stack.getItem()).toString();
    }
}
