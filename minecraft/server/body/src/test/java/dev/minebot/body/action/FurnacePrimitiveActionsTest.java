package dev.minebot.body.action;

import dev.minebot.body.control.FakePlayerActionOwner;
import dev.minebot.body.control.OwnerPriority;
import dev.minebot.body.event.BotEventStream;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.List;
import java.util.Objects;
import java.util.concurrent.atomic.AtomicInteger;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

final class FurnacePrimitiveActionsTest {
    private static final class FakeInventory implements ContainerPrimitiveActions.InventoryAccess {
        final String[] items;
        final String[] components;
        final int[] counts;
        final boolean[] accepts;

        FakeInventory(int size) {
            items = new String[size];
            components = new String[size];
            counts = new int[size];
            accepts = new boolean[size];
            java.util.Arrays.fill(accepts, true);
        }

        void put(int slot, String item, int count, String component) {
            items[slot] = item;
            counts[slot] = count;
            components[slot] = component;
        }

        @Override public int size() { return items.length; }
        @Override public boolean slotEmpty(int slot) { return items[slot] == null || counts[slot] <= 0; }
        @Override public String itemIdAt(int slot) { return items[slot]; }
        @Override public int itemCountAt(int slot) { return counts[slot]; }
        @Override public int maxStackSizeAt(int slot) { return 64; }
        @Override public int destinationMaxStackSize(int slot, ContainerPrimitiveActions.InventoryAccess source, int sourceSlot) { return 64; }

        @Override
        public boolean sameStack(int slot, ContainerPrimitiveActions.InventoryAccess other, int otherSlot) {
            if (!(other instanceof FakeInventory target)) {
                return false;
            }
            return Objects.equals(items[slot], target.items[otherSlot])
                && Objects.equals(components[slot], target.components[otherSlot]);
        }

        @Override
        public boolean canMoveTo(
            int sourceSlot,
            ContainerPrimitiveActions.InventoryAccess destination,
            int destinationSlot
        ) {
            return destination instanceof FakeInventory target && target.accepts[destinationSlot];
        }

        @Override
        public void moveItemsTo(
            int sourceSlot,
            ContainerPrimitiveActions.InventoryAccess destination,
            int destinationSlot,
            int count
        ) {
            FakeInventory target = (FakeInventory) destination;
            if (target.slotEmpty(destinationSlot)) {
                target.items[destinationSlot] = items[sourceSlot];
                target.components[destinationSlot] = components[sourceSlot];
            }
            target.counts[destinationSlot] += count;
            counts[sourceSlot] -= count;
            if (counts[sourceSlot] == 0) {
                items[sourceSlot] = null;
                components[sourceSlot] = null;
            }
        }
    }

    @Test
    void namedFuelDepositUsesTheRealFuelSlot() {
        FakeInventory furnace = new FakeInventory(3);
        FakeInventory bot = new FakeInventory(46);
        bot.put(7, "minecraft:coal", 3, "plain");

        var outcome = FurnacePrimitiveActions.transfer(
            target("minecraft:furnace", furnace, bot),
            "bot_to_furnace",
            "fuel",
            7,
            2,
            64
        );

        assertTrue(outcome.success());
        assertEquals(2, furnace.counts[1]);
        assertEquals(1, bot.counts[7]);
        assertEquals("fuel", outcome.facts().get("furnace_slot").getAsString());
        assertEquals(2, outcome.facts().get("count").getAsInt());
        assertEquals(2, outcome.facts().getAsJsonObject("furnace_after").get("count").getAsInt());
    }

    @Test
    void outputWithdrawalPreservesTheOriginalStackComponents() {
        FakeInventory furnace = new FakeInventory(3);
        FakeInventory bot = new FakeInventory(46);
        furnace.put(2, "minecraft:iron_ingot", 2, "custom-name");

        var outcome = FurnacePrimitiveActions.transfer(
            target("minecraft:furnace", furnace, bot),
            "furnace_to_bot",
            "output",
            4,
            1,
            64
        );

        assertTrue(outcome.success());
        assertEquals(1, furnace.counts[2]);
        assertEquals(1, bot.counts[4]);
        assertEquals("custom-name", bot.components[4]);
    }

    @Test
    void executorMovesOnlyAfterOpenApprovalAndFreshFurnaceRead() {
        FakeInventory furnace = new FakeInventory(3);
        FakeInventory bot = new FakeInventory(46);
        bot.put(5, "minecraft:raw_iron", 2, "plain");
        MutationGate gate = new MutationGate();
        ActionRegistry registry = new ActionRegistry();
        BotEventStream events = new BotEventStream();
        ActionRuntime runtime = new ActionRuntime(new FakePlayerActionOwner(), ignored -> {}, registry, events);
        List<MutationGate.Proposal> proposals = new ArrayList<>();
        AtomicInteger reads = new AtomicInteger();
        runtime.submit("Bot", "furnace-1", "FURNACE_TRANSFER", OwnerPriority.ACTION, 0);
        runtime.attachExecutor("furnace-1", new FurnacePrimitiveActions.Executor(
            "Bot", "furnace-1", 1, 64, 0,
            "bot_to_furnace", "input", 5, 2, 64,
            () -> {
                reads.incrementAndGet();
                return target("minecraft:furnace", furnace, bot);
            },
            gate, proposals::add, events, runtime
        ));

        runtime.tick(1);
        assertEquals(1, proposals.size());
        assertEquals("open", proposals.getFirst().mutationKind());
        assertEquals("activate", proposals.getFirst().context());
        assertEquals(0, furnace.counts[0]);
        gate.verdict(proposals.getFirst().proposalId(), true, "allowed_interaction");
        runtime.tick(2);

        assertEquals(2, reads.get());
        assertEquals(2, furnace.counts[0]);
        assertEquals(0, bot.counts[5]);
        var terminal = registry.status("furnace-1").terminal();
        assertEquals("completed", terminal.get("classification").getAsString());
        assertEquals("input", terminal.get("furnace_slot").getAsString());
    }

    @Test
    void deniedOpenLeavesFurnaceAndPlayerInventoryUnchanged() {
        FakeInventory furnace = new FakeInventory(3);
        FakeInventory bot = new FakeInventory(46);
        bot.put(5, "minecraft:coal", 2, "plain");
        MutationGate gate = new MutationGate();
        ActionRegistry registry = new ActionRegistry();
        BotEventStream events = new BotEventStream();
        ActionRuntime runtime = new ActionRuntime(new FakePlayerActionOwner(), ignored -> {}, registry, events);
        List<MutationGate.Proposal> proposals = new ArrayList<>();
        runtime.submit("Bot", "furnace-2", "FURNACE_TRANSFER", OwnerPriority.ACTION, 0);
        runtime.attachExecutor("furnace-2", new FurnacePrimitiveActions.Executor(
            "Bot", "furnace-2", 1, 64, 0,
            "bot_to_furnace", "fuel", 5, 1, 64,
            () -> target("minecraft:furnace", furnace, bot),
            gate, proposals::add, events, runtime
        ));

        runtime.tick(1);
        gate.verdict(proposals.getFirst().proposalId(), false, "protected_region");
        runtime.tick(2);

        assertEquals(0, furnace.counts[1]);
        assertEquals(2, bot.counts[5]);
        var terminal = registry.status("furnace-2").terminal();
        assertEquals("failed", terminal.get("classification").getAsString());
        assertEquals("governance_denied:protected_region", terminal.get("reason").getAsString());
    }

    @Test
    void rejectedFurnaceSlotDoesNotMutateEitherInventory() {
        FakeInventory furnace = new FakeInventory(3);
        FakeInventory bot = new FakeInventory(46);
        bot.put(5, "minecraft:coal", 2, "plain");
        furnace.accepts[2] = false;

        var outcome = FurnacePrimitiveActions.transfer(
            target("minecraft:furnace", furnace, bot),
            "bot_to_furnace",
            "output",
            5,
            1,
            64
        );

        assertFalse(outcome.success());
        assertEquals("destination_rejected", outcome.reason());
        assertEquals(0, furnace.counts[2]);
        assertEquals(2, bot.counts[5]);
    }

    private static ContainerPrimitiveActions.Target target(
        String block,
        FakeInventory furnace,
        FakeInventory bot
    ) {
        return new ContainerPrimitiveActions.Target(block, furnace, bot, null);
    }
}
