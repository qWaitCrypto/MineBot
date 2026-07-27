package dev.minebot.body.action;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import it.unimi.dsi.fastutil.ints.IntList;
import net.minecraft.core.NonNullList;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.world.Container;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.item.crafting.CraftingInput;
import net.minecraft.world.item.crafting.CraftingRecipe;
import net.minecraft.world.item.crafting.Ingredient;
import net.minecraft.world.item.crafting.PlacementInfo;
import net.minecraft.world.item.crafting.RecipeHolder;
import net.minecraft.world.item.crafting.ShapedRecipe;
import net.minecraft.world.item.crafting.display.RecipeDisplay;
import net.minecraft.world.item.crafting.display.ShapedCraftingRecipeDisplay;
import net.minecraft.world.level.Level;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collection;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.function.Supplier;

/** Explicit-slot crafting that accepts only a recipe proven by the live server recipe set. */
public final class CraftPrimitiveActions {
    private CraftPrimitiveActions() {}

    public record Input(int slot, String item, int count) {}
    public record Output(int slot, String item, int count) {}
    public record Remainder(int slot, String item, int count) {}
    public record Request(List<Input> inputs, Output output, List<Remainder> remainders, int maxStack) {}

    public record Outcome(boolean success, String reason, JsonObject facts) {
        public String classification() {
            return success ? ActionRuntime.CLASS_COMPLETED : ActionRuntime.CLASS_FAILED;
        }
    }

    private record IngredientSlot(int gridIndex, Ingredient ingredient) {}
    private record Layout(int width, int height, List<IngredientSlot> slots) {}
    private record Requirement(int craft, int ingredientIndex, Ingredient ingredient, List<Integer> candidates) {}
    private record Validated(
        String recipeId,
        int crafts,
        ItemStack output,
        Map<Integer, ItemStack> remainders
    ) {}

    public static Outcome craft(
        Request request,
        Container inventory,
        Collection<RecipeHolder<?>> recipes,
        Level level
    ) {
        JsonObject facts = baseFacts(request, inventory);
        String invalid = validateRequest(request, inventory);
        if (invalid != null) {
            return failure(invalid, facts);
        }

        Validated validated = validateAgainstRecipes(request, inventory, recipes, level);
        if (validated == null) {
            return failure("recipe_mismatch", facts);
        }

        ItemStack destination = inventory.getItem(request.output().slot());
        if (!destination.isEmpty() && !ItemStack.isSameItemSameComponents(destination, validated.output())) {
            return failure("destination_occupied", facts);
        }
        int stackLimit = Math.min(
            Math.max(1, request.maxStack()),
            Math.min(validated.output().getMaxStackSize(), inventory.getMaxStackSize(validated.output()))
        );
        if (destination.getCount() + request.output().count() > stackLimit) {
            return failure("destination_full", facts);
        }

        Map<Integer, ItemStack> afterInputs = new LinkedHashMap<>();
        for (Input input : request.inputs()) {
            ItemStack after = inventory.getItem(input.slot()).copy();
            after.shrink(input.count());
            ItemStack remainder = validated.remainders().get(input.slot());
            if (remainder != null) {
                if (!after.isEmpty()) {
                    return failure("invalid_remainder", facts);
                }
                after = remainder.copy();
            }
            afterInputs.put(input.slot(), after);
        }

        for (Map.Entry<Integer, ItemStack> entry : afterInputs.entrySet()) {
            inventory.setItem(entry.getKey(), entry.getValue());
        }
        ItemStack afterOutput;
        if (destination.isEmpty()) {
            afterOutput = validated.output().copyWithCount(request.output().count());
        } else {
            afterOutput = destination.copy();
            afterOutput.grow(request.output().count());
        }
        inventory.setItem(request.output().slot(), afterOutput);
        inventory.setChanged();

        facts.addProperty("recipe_id", validated.recipeId());
        facts.addProperty("crafts", validated.crafts());
        facts.add("inputs_after", inputFacts(request.inputs(), inventory));
        facts.add("output_after", slotFact(inventory, request.output().slot()));
        facts.addProperty("success", true);
        facts.addProperty("reason", "completed");
        facts.addProperty("stopped_reason", "completed");
        return new Outcome(true, "completed", facts);
    }

    private static String validateRequest(Request request, Container inventory) {
        if (request == null || request.inputs() == null || request.inputs().isEmpty()
            || request.output() == null || request.remainders() == null) {
            return "invalid_recipe";
        }
        if (request.output().slot() < 0 || request.output().slot() >= inventory.getContainerSize()
            || request.output().count() <= 0 || request.output().item() == null) {
            return "invalid_recipe";
        }
        Set<Integer> inputSlots = new HashSet<>();
        for (Input input : request.inputs()) {
            if (input.slot() < 0 || input.slot() >= inventory.getContainerSize()
                || input.count() <= 0 || input.item() == null || !inputSlots.add(input.slot())) {
                return "invalid_recipe";
            }
            ItemStack actual = inventory.getItem(input.slot());
            if (actual.isEmpty() || !itemId(actual).equals(input.item()) || actual.getCount() < input.count()) {
                return "missing_inputs";
            }
        }
        if (inputSlots.contains(request.output().slot())) {
            return "invalid_slot_overlap";
        }
        Set<Integer> remainderSlots = new HashSet<>();
        for (Remainder remainder : request.remainders()) {
            if (!inputSlots.contains(remainder.slot()) || remainder.count() <= 0
                || remainder.item() == null || !remainderSlots.add(remainder.slot())) {
                return "invalid_remainder";
            }
        }
        return null;
    }

    private static Validated validateAgainstRecipes(
        Request request,
        Container inventory,
        Collection<RecipeHolder<?>> recipes,
        Level level
    ) {
        List<RecipeHolder<?>> ordered = new ArrayList<>(recipes);
        ordered.sort(Comparator.comparing(holder -> holder.id().identifier().toString()));
        for (RecipeHolder<?> holder : ordered) {
            if (!(holder.value() instanceof CraftingRecipe recipe)) {
                continue;
            }
            Validated validated = validateRecipe(holder, recipe, request, inventory, level);
            if (validated != null) {
                return validated;
            }
        }
        return null;
    }

    private static Validated validateRecipe(
        RecipeHolder<?> holder,
        CraftingRecipe recipe,
        Request request,
        Container inventory,
        Level level
    ) {
        Layout layout = layout(recipe);
        if (layout == null || layout.slots().isEmpty()) {
            return null;
        }
        int totalInputs = request.inputs().stream().mapToInt(Input::count).sum();
        if (totalInputs % layout.slots().size() != 0) {
            return null;
        }
        int crafts = totalInputs / layout.slots().size();
        if (crafts <= 0 || crafts > 99) {
            return null;
        }

        List<ItemStack> sources = request.inputs().stream()
            .map(input -> inventory.getItem(input.slot()).copy())
            .toList();
        int[] remaining = request.inputs().stream().mapToInt(Input::count).toArray();
        int[][] assigned = new int[crafts][layout.slots().size()];
        for (int[] row : assigned) {
            Arrays.fill(row, -1);
        }
        List<Requirement> requirements = new ArrayList<>();
        for (int craft = 0; craft < crafts; craft++) {
            for (int ingredientIndex = 0; ingredientIndex < layout.slots().size(); ingredientIndex++) {
                Ingredient ingredient = layout.slots().get(ingredientIndex).ingredient();
                List<Integer> candidates = new ArrayList<>();
                for (int source = 0; source < sources.size(); source++) {
                    if (ingredient.test(sources.get(source))) {
                        candidates.add(source);
                    }
                }
                if (candidates.isEmpty()) {
                    return null;
                }
                requirements.add(new Requirement(craft, ingredientIndex, ingredient, List.copyOf(candidates)));
            }
        }
        requirements.sort(Comparator.comparingInt(requirement -> requirement.candidates().size()));
        if (!assign(requirements, 0, remaining, assigned)) {
            return null;
        }

        ItemStack combinedOutput = ItemStack.EMPTY;
        Map<Integer, ItemStack> actualRemainders = new HashMap<>();
        for (int craft = 0; craft < crafts; craft++) {
            List<ItemStack> grid = new ArrayList<>();
            for (int index = 0; index < layout.width() * layout.height(); index++) {
                grid.add(ItemStack.EMPTY);
            }
            int[] sourceByGrid = new int[grid.size()];
            Arrays.fill(sourceByGrid, -1);
            for (int ingredientIndex = 0; ingredientIndex < layout.slots().size(); ingredientIndex++) {
                int sourceIndex = assigned[craft][ingredientIndex];
                int gridIndex = layout.slots().get(ingredientIndex).gridIndex();
                grid.set(gridIndex, sources.get(sourceIndex).copyWithCount(1));
                sourceByGrid[gridIndex] = sourceIndex;
            }
            CraftingInput input = CraftingInput.of(layout.width(), layout.height(), grid);
            if (!recipe.matches(input, level)) {
                return null;
            }
            ItemStack output = recipe.assemble(input);
            if (output.isEmpty() || !itemId(output).equals(request.output().item())) {
                return null;
            }
            if (combinedOutput.isEmpty()) {
                combinedOutput = output.copy();
            } else if (!ItemStack.isSameItemSameComponents(combinedOutput, output)) {
                return null;
            } else {
                combinedOutput.grow(output.getCount());
            }
            NonNullList<ItemStack> remainders = recipe.getRemainingItems(input);
            for (int gridIndex = 0; gridIndex < remainders.size(); gridIndex++) {
                ItemStack remainder = remainders.get(gridIndex);
                if (remainder.isEmpty()) {
                    continue;
                }
                int sourceIndex = sourceByGrid[gridIndex];
                if (sourceIndex < 0) {
                    return null;
                }
                int inventorySlot = request.inputs().get(sourceIndex).slot();
                ItemStack existing = actualRemainders.get(inventorySlot);
                if (existing == null) {
                    actualRemainders.put(inventorySlot, remainder.copy());
                } else if (ItemStack.isSameItemSameComponents(existing, remainder)) {
                    existing.grow(remainder.getCount());
                } else {
                    return null;
                }
            }
        }
        if (combinedOutput.getCount() != request.output().count()) {
            return null;
        }
        if (!remaindersMatch(request.remainders(), actualRemainders)) {
            return null;
        }
        return new Validated(
            holder.id().identifier().toString(),
            crafts,
            combinedOutput.copyWithCount(1),
            Map.copyOf(actualRemainders)
        );
    }

    private static boolean assign(
        List<Requirement> requirements,
        int index,
        int[] remaining,
        int[][] assigned
    ) {
        if (index >= requirements.size()) {
            return Arrays.stream(remaining).allMatch(value -> value == 0);
        }
        Requirement requirement = requirements.get(index);
        List<Integer> candidates = new ArrayList<>(requirement.candidates());
        candidates.sort((left, right) -> Integer.compare(remaining[right], remaining[left]));
        for (int source : candidates) {
            if (remaining[source] <= 0) {
                continue;
            }
            remaining[source]--;
            assigned[requirement.craft()][requirement.ingredientIndex()] = source;
            if (assign(requirements, index + 1, remaining, assigned)) {
                return true;
            }
            assigned[requirement.craft()][requirement.ingredientIndex()] = -1;
            remaining[source]++;
        }
        return false;
    }

    private static Layout layout(CraftingRecipe recipe) {
        if (recipe instanceof ShapedRecipe shaped) {
            List<IngredientSlot> slots = new ArrayList<>();
            List<java.util.Optional<Ingredient>> ingredients = shaped.getIngredients();
            for (int index = 0; index < ingredients.size(); index++) {
                int gridIndex = index;
                ingredients.get(index).ifPresent(ingredient -> slots.add(new IngredientSlot(gridIndex, ingredient)));
            }
            return new Layout(shaped.getWidth(), shaped.getHeight(), List.copyOf(slots));
        }

        PlacementInfo placement = recipe.placementInfo();
        if (placement.isImpossibleToPlace() || placement.ingredients().isEmpty()) {
            return null;
        }
        for (RecipeDisplay display : recipe.display()) {
            if (!(display instanceof ShapedCraftingRecipeDisplay shapedDisplay)) {
                continue;
            }
            IntList mapping = placement.slotsToIngredientIndex();
            if (mapping.size() != shapedDisplay.width() * shapedDisplay.height()) {
                continue;
            }
            List<IngredientSlot> slots = new ArrayList<>();
            for (int gridIndex = 0; gridIndex < mapping.size(); gridIndex++) {
                int ingredientIndex = mapping.getInt(gridIndex);
                if (ingredientIndex >= 0 && ingredientIndex < placement.ingredients().size()) {
                    slots.add(new IngredientSlot(gridIndex, placement.ingredients().get(ingredientIndex)));
                }
            }
            return new Layout(shapedDisplay.width(), shapedDisplay.height(), List.copyOf(slots));
        }

        int count = placement.ingredients().size();
        int width = count == 1 ? 1 : count <= 4 ? 2 : 3;
        int height = Math.max(1, (count + width - 1) / width);
        List<IngredientSlot> slots = new ArrayList<>();
        for (int index = 0; index < count; index++) {
            slots.add(new IngredientSlot(index, placement.ingredients().get(index)));
        }
        return new Layout(width, height, List.copyOf(slots));
    }

    private static boolean remaindersMatch(
        List<Remainder> expected,
        Map<Integer, ItemStack> actual
    ) {
        if (expected.size() != actual.size()) {
            return false;
        }
        for (Remainder remainder : expected) {
            ItemStack stack = actual.get(remainder.slot());
            if (stack == null || !itemId(stack).equals(remainder.item()) || stack.getCount() != remainder.count()) {
                return false;
            }
        }
        return true;
    }

    private static JsonObject baseFacts(Request request, Container inventory) {
        JsonObject facts = new JsonObject();
        if (request == null) {
            return facts;
        }
        facts.addProperty("output_item", request.output() == null ? null : request.output().item());
        facts.addProperty("output_count", request.output() == null ? 0 : request.output().count());
        facts.addProperty("output_slot", request.output() == null ? -1 : request.output().slot());
        facts.add("inputs_before", inputFacts(request.inputs(), inventory));
        facts.add("output_before", slotFact(inventory, request.output() == null ? -1 : request.output().slot()));
        return facts;
    }

    private static JsonArray inputFacts(List<Input> inputs, Container inventory) {
        JsonArray facts = new JsonArray();
        if (inputs == null) {
            return facts;
        }
        for (Input input : inputs) {
            facts.add(slotFact(inventory, input.slot()));
        }
        return facts;
    }

    private static JsonObject slotFact(Container inventory, int slot) {
        JsonObject fact = new JsonObject();
        boolean valid = slot >= 0 && slot < inventory.getContainerSize();
        ItemStack stack = valid ? inventory.getItem(slot) : ItemStack.EMPTY;
        fact.addProperty("slot", slot);
        fact.addProperty("empty", !valid || stack.isEmpty());
        fact.addProperty("item", !valid || stack.isEmpty() ? null : itemId(stack));
        fact.addProperty("count", !valid || stack.isEmpty() ? 0 : stack.getCount());
        return fact;
    }

    private static Outcome failure(String reason, JsonObject facts) {
        facts.addProperty("success", false);
        facts.addProperty("reason", reason);
        facts.addProperty("stopped_reason", reason);
        return new Outcome(false, reason, facts);
    }

    private static String itemId(ItemStack stack) {
        return BuiltInRegistries.ITEM.getKey(stack.getItem()).toString();
    }

    /** One-tick deferral keeps the action acknowledgement ahead of terminal truth. */
    public static final class Executor implements ActionRuntime.TickExecutor {
        private final String bot;
        private final String actionId;
        private final Supplier<Outcome> operation;
        private final ActionRuntime runtime;
        private boolean ran;

        public Executor(String bot, String actionId, Supplier<Outcome> operation, ActionRuntime runtime) {
            this.bot = bot;
            this.actionId = actionId;
            this.operation = operation;
            this.runtime = runtime;
        }

        @Override
        public void tick(int serverTick) {
            if (ran) {
                return;
            }
            ran = true;
            if (runtime.cancelRequested(actionId)) {
                runtime.finish(bot, actionId, ActionRuntime.CLASS_CANCELED, failure("canceled", new JsonObject()).facts(), serverTick);
                return;
            }
            Outcome outcome;
            try {
                outcome = operation.get();
            } catch (RuntimeException error) {
                outcome = failure("craft_internal_error", new JsonObject());
            }
            runtime.finish(bot, actionId, outcome.classification(), outcome.facts(), serverTick);
        }
    }
}
