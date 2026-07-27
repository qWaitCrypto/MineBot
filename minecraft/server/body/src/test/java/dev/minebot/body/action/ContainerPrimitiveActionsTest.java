package dev.minebot.body.action;

import dev.minebot.body.control.FakePlayerActionOwner;
import dev.minebot.body.control.OwnerPriority;
import dev.minebot.body.event.BotEventStream;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

final class ContainerPrimitiveActionsTest {
    private static final class FakeInventory implements ContainerPrimitiveActions.InventoryAccess {
        final String[] items;
        final String[] components;
        final int[] counts;
        boolean accepts = true;

        FakeInventory(int size) {
            items = new String[size];
            components = new String[size];
            counts = new int[size];
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
            return java.util.Objects.equals(items[slot], target.items[otherSlot])
                && java.util.Objects.equals(components[slot], target.components[otherSlot]);
        }

        @Override
        public boolean canMoveTo(int sourceSlot, ContainerPrimitiveActions.InventoryAccess destination, int destinationSlot) {
            return destination instanceof FakeInventory target && target.accepts;
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
    void transferPreservesComponentsAndObservedCounts() {
        FakeInventory chest = new FakeInventory(27);
        FakeInventory bot = new FakeInventory(46);
        chest.put(0, "minecraft:diamond_helmet", 1, "damage=7");

        var outcome = ContainerPrimitiveActions.transfer(
            target("minecraft:chest", chest, bot), "container_to_bot", 0, 18, 1, 64
        );

        assertTrue(outcome.success());
        assertEquals("completed", outcome.reason());
        assertEquals(0, chest.counts[0]);
        assertEquals(1, bot.counts[18]);
        assertEquals("damage=7", bot.components[18]);
        assertEquals(1, outcome.facts().get("count").getAsInt());
    }

    @Test
    void incompatibleDestinationDoesNotMutateEitherInventory() {
        FakeInventory chest = new FakeInventory(27);
        FakeInventory bot = new FakeInventory(46);
        chest.put(0, "minecraft:diamond", 3, "plain");
        bot.put(0, "minecraft:cobblestone", 4, "plain");

        var outcome = ContainerPrimitiveActions.transfer(
            target("minecraft:chest", chest, bot), "container_to_bot", 0, 0, 2, 64
        );

        assertFalse(outcome.success());
        assertEquals("destination_occupied", outcome.reason());
        assertEquals(3, chest.counts[0]);
        assertEquals(4, bot.counts[0]);
    }

    @Test
    void executorMovesOnlyAfterOpenGovernanceAndFreshTargetRead() {
        FakeInventory chest = new FakeInventory(27);
        FakeInventory bot = new FakeInventory(46);
        chest.put(0, "minecraft:diamond", 3, "plain");
        MutationGate gate = new MutationGate();
        ActionRegistry registry = new ActionRegistry();
        BotEventStream events = new BotEventStream();
        ActionRuntime runtime = new ActionRuntime(new FakePlayerActionOwner(), ignored -> {}, registry, events);
        List<MutationGate.Proposal> proposals = new ArrayList<>();
        AtomicInteger reads = new AtomicInteger();
        runtime.submit("Bot", "container-1", "CONTAINER_TRANSFER", OwnerPriority.ACTION, 0);
        runtime.attachExecutor("container-1", new ContainerPrimitiveActions.Executor(
            "Bot", "container-1", 1, 64, 0,
            "container_to_bot", 0, 1, 2, 64,
            () -> {
                reads.incrementAndGet();
                return target("minecraft:chest", chest, bot);
            },
            gate, proposals::add, events, runtime
        ));

        runtime.tick(1);
        assertEquals(1, proposals.size());
        assertEquals("open", proposals.getFirst().mutationKind());
        assertEquals(3, chest.counts[0]);
        assertEquals(0, bot.counts[1]);
        gate.verdict(proposals.getFirst().proposalId(), true, "allowed_interaction");
        runtime.tick(2);

        assertEquals(2, reads.get(), "the container is read again after the verdict");
        assertEquals(1, chest.counts[0]);
        assertEquals(2, bot.counts[1]);
        assertEquals("completed", registry.status("container-1").terminal().get("classification").getAsString());
    }

    @Test
    void deniedOpenLeavesBothInventoriesUnchanged() {
        FakeInventory chest = new FakeInventory(27);
        FakeInventory bot = new FakeInventory(46);
        chest.put(0, "minecraft:diamond", 3, "plain");
        MutationGate gate = new MutationGate();
        ActionRegistry registry = new ActionRegistry();
        BotEventStream events = new BotEventStream();
        ActionRuntime runtime = new ActionRuntime(new FakePlayerActionOwner(), ignored -> {}, registry, events);
        List<MutationGate.Proposal> proposals = new ArrayList<>();
        runtime.submit("Bot", "container-2", "CONTAINER_TRANSFER", OwnerPriority.ACTION, 0);
        runtime.attachExecutor("container-2", new ContainerPrimitiveActions.Executor(
            "Bot", "container-2", 1, 64, 0,
            "container_to_bot", 0, 1, 2, 64,
            () -> target("minecraft:chest", chest, bot),
            gate, proposals::add, events, runtime
        ));

        runtime.tick(1);
        gate.verdict(proposals.getFirst().proposalId(), false, "protected_region");
        runtime.tick(2);

        assertEquals(3, chest.counts[0]);
        assertEquals(0, bot.counts[1]);
        var terminal = registry.status("container-2").terminal();
        assertEquals("failed", terminal.get("classification").getAsString());
        assertEquals("governance_denied:protected_region", terminal.get("reason").getAsString());
    }

    @Test
    void changedTargetAfterApprovalLeavesBothInventoriesUnchanged() {
        FakeInventory chest = new FakeInventory(27);
        FakeInventory bot = new FakeInventory(46);
        chest.put(0, "minecraft:diamond", 3, "plain");
        MutationGate gate = new MutationGate();
        ActionRegistry registry = new ActionRegistry();
        BotEventStream events = new BotEventStream();
        ActionRuntime runtime = new ActionRuntime(new FakePlayerActionOwner(), ignored -> {}, registry, events);
        List<MutationGate.Proposal> proposals = new ArrayList<>();
        AtomicInteger reads = new AtomicInteger();
        runtime.submit("Bot", "container-3", "CONTAINER_TRANSFER", OwnerPriority.ACTION, 0);
        runtime.attachExecutor("container-3", new ContainerPrimitiveActions.Executor(
            "Bot", "container-3", 1, 64, 0,
            "container_to_bot", 0, 1, 2, 64,
            () -> target(reads.incrementAndGet() == 1 ? "minecraft:chest" : "minecraft:barrel", chest, bot),
            gate, proposals::add, events, runtime
        ));

        runtime.tick(1);
        gate.verdict(proposals.getFirst().proposalId(), true, "allowed_interaction");
        runtime.tick(2);

        assertEquals(3, chest.counts[0]);
        assertEquals(0, bot.counts[1]);
        var terminal = registry.status("container-3").terminal();
        assertEquals("failed", terminal.get("classification").getAsString());
        assertEquals("target_changed", terminal.get("reason").getAsString());
    }

    private static ContainerPrimitiveActions.Target target(String block, FakeInventory chest, FakeInventory bot) {
        return new ContainerPrimitiveActions.Target(block, chest, bot, null);
    }
}
