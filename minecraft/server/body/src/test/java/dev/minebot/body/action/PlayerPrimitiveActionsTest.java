package dev.minebot.body.action;

import dev.minebot.body.control.FakePlayerActionOwner;
import dev.minebot.body.control.OwnerPriority;
import dev.minebot.body.event.BotEventStream;
import org.junit.jupiter.api.Test;

import java.util.Arrays;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

final class PlayerPrimitiveActionsTest {
    private static final class FakePlayer implements PlayerPrimitiveActions.PlayerAccess {
        final String[] items = new String[41];
        final int[] counts = new int[41];
        int selected;
        boolean present = true;
        PlayerPrimitiveActions.Position position = new PlayerPrimitiveActions.Position(0, 64, 0);
        PlayerPrimitiveActions.Position eye = new PlayerPrimitiveActions.Position(0, 65.62, 0);
        PlayerPrimitiveActions.Position look = new PlayerPrimitiveActions.Position(0, 0, 1);
        float yaw;
        float pitch;

        void put(int slot, String item, int count) {
            items[slot] = item;
            counts[slot] = count;
        }

        @Override public boolean present() { return present; }
        @Override public int inventorySize() { return items.length; }
        @Override public String itemIdAt(int slot) { return items[slot]; }
        @Override public int itemCountAt(int slot) { return counts[slot]; }
        @Override public boolean slotEmpty(int slot) { return items[slot] == null || counts[slot] <= 0; }
        @Override public int selectedHotbarSlot() { return selected; }
        @Override public String selectedItemId() { return itemIdAt(selected); }
        @Override public PlayerPrimitiveActions.Position position() { return position; }
        @Override public PlayerPrimitiveActions.Position eyePosition() { return eye; }
        @Override public PlayerPrimitiveActions.Position lookDirection() { return look; }
        @Override public float yaw() { return yaw; }
        @Override public float pitch() { return pitch; }

        @Override
        public void moveWholeStack(int fromSlot, int toSlot) {
            if (!slotEmpty(toSlot)) {
                throw new IllegalStateException("occupied destination");
            }
            items[toSlot] = items[fromSlot];
            counts[toSlot] = counts[fromSlot];
            items[fromSlot] = null;
            counts[fromSlot] = 0;
        }

        @Override public boolean sameStack(int firstSlot, int secondSlot) {
            return items[firstSlot] != null && items[firstSlot].equals(items[secondSlot]);
        }
        @Override public int maxStackSizeAt(int slot) { return 64; }
        @Override public void moveItems(int fromSlot, int toSlot, int count) {
            if (slotEmpty(toSlot)) {
                items[toSlot] = items[fromSlot];
            }
            counts[toSlot] += count;
            counts[fromSlot] -= count;
            if (counts[fromSlot] == 0) {
                items[fromSlot] = null;
            }
        }

        @Override
        public String inventoryFingerprint() {
            return Arrays.toString(items) + Arrays.toString(counts);
        }
    }

    private static final class FakeControls implements PlayerPrimitiveActions.Controls, BotControls {
        final FakePlayer player;
        int onceUses;
        int continuousUses;
        int clears;

        FakeControls(FakePlayer player) {
            this.player = player;
        }

        @Override public void selectHotbar(String botName, int slot) { player.selected = slot; }

        @Override
        public void lookAt(String botName, double x, double y, double z) {
            double dx = x - player.eye.x();
            double dy = y - player.eye.y();
            double dz = z - player.eye.z();
            double length = Math.sqrt(dx * dx + dy * dy + dz * dz);
            player.look = new PlayerPrimitiveActions.Position(dx / length, dy / length, dz / length);
        }

        @Override public void useOnce(String botName) { onceUses++; }
        @Override public void useContinuous(String botName) { continuousUses++; }
        @Override public void dropOne(String botName) { player.counts[player.selected]--; }
        @Override public void dropStack(String botName) {
            player.counts[player.selected] = 0;
            player.items[player.selected] = null;
        }
        @Override public void clearAll(String botName) { clears++; }
    }

    @Test
    void selectsExistingHotbarItemWithoutMovingInventory() {
        FakePlayer player = new FakePlayer();
        player.put(4, "minecraft:bread", 3);
        FakeControls controls = new FakeControls(player);

        var outcome = PlayerPrimitiveActions.selectItem("Bot", "minecraft:bread", player, controls);

        assertTrue(outcome.success());
        assertEquals("completed", outcome.reason());
        assertEquals(4, player.selected);
        assertEquals("minecraft:bread", player.items[4]);
    }

    @Test
    void stagesCarryItemAndPreservesDisplacedWholeStack() {
        FakePlayer player = new FakePlayer();
        for (int slot = 0; slot < 9; slot++) {
            player.put(slot, "minecraft:stone", slot + 1);
        }
        player.put(12, "minecraft:bread", 3);
        FakeControls controls = new FakeControls(player);

        var outcome = PlayerPrimitiveActions.selectItem("Bot", "minecraft:bread", player, controls);

        assertTrue(outcome.success());
        assertEquals("moved_to_hotbar", outcome.reason());
        assertEquals("minecraft:bread", player.items[0]);
        assertEquals(3, player.counts[0]);
        assertEquals("minecraft:stone", player.items[9]);
        assertEquals(1, player.counts[9]);
        assertTrue(player.slotEmpty(12));
    }

    @Test
    void fullHotbarAndCarryRefusesWithoutMutation() {
        FakePlayer player = new FakePlayer();
        for (int slot = 0; slot < 36; slot++) {
            player.put(slot, slot == 20 ? "minecraft:bread" : "minecraft:stone", 1);
        }
        String before = player.inventoryFingerprint();

        var outcome = PlayerPrimitiveActions.selectItem(
            "Bot", "minecraft:bread", player, new FakeControls(player)
        );

        assertFalse(outcome.success());
        assertEquals("hotbar_full", outcome.reason());
        assertEquals(before, player.inventoryFingerprint());
    }

    @Test
    void lookCompletesOnlyFromObservedAlignment() {
        FakePlayer player = new FakePlayer();
        FakeControls controls = new FakeControls(player);

        var outcome = PlayerPrimitiveActions.lookAt(
            "Bot", new PlayerPrimitiveActions.Position(5, 66, 5), player, controls
        );

        assertTrue(outcome.success());
        assertTrue(outcome.facts().get("alignment").getAsDouble() > 0.999);
    }

    @Test
    void useCompletesWhenTheSelectedItemActuallyChanges() {
        FakePlayer player = new FakePlayer();
        player.put(0, "minecraft:bread", 2);
        FakeControls controls = new FakeControls(player);
        ActionRegistry registry = new ActionRegistry();
        ActionRuntime runtime = runtime(controls, registry);
        runtime.submit("Bot", "use-1", "PLAYER_ACTION:useItem", OwnerPriority.ACTION, 10);
        var executor = new PlayerPrimitiveActions.UseExecutor(
            "Bot", "use-1", "continuous", "minecraft:bread", 80, player, controls, runtime
        );
        runtime.attachExecutor("use-1", executor);

        runtime.tick(11);
        player.counts[0] = 1;
        runtime.tick(12);

        var status = registry.status("use-1");
        assertEquals(ActionRegistry.State.TERMINAL, status.state());
        assertEquals("completed", status.terminal().get("classification").getAsString());
        assertEquals("completed", status.terminal().get("reason").getAsString());
        assertTrue(status.terminal().get("inventory_changed").getAsBoolean());
        assertEquals(1, controls.continuousUses);
        assertEquals(1, controls.clears);
    }

    @Test
    void declaredItemUseReportsNoEffectAfterItsBound() {
        FakePlayer player = new FakePlayer();
        player.put(0, "minecraft:bread", 2);
        FakeControls controls = new FakeControls(player);
        ActionRegistry registry = new ActionRegistry();
        ActionRuntime runtime = runtime(controls, registry);
        runtime.submit("Bot", "use-2", "PLAYER_ACTION:useItem", OwnerPriority.ACTION, 20);
        runtime.attachExecutor("use-2", new PlayerPrimitiveActions.UseExecutor(
            "Bot", "use-2", "once", "minecraft:bread", 2, player, controls, runtime
        ));

        runtime.tick(21);
        runtime.tick(22);
        runtime.tick(23);

        var terminal = registry.status("use-2").terminal();
        assertEquals("failed", terminal.get("classification").getAsString());
        assertEquals("no_effect", terminal.get("reason").getAsString());
        assertEquals(2, controls.onceUses);
    }

    @Test
    void moveItemSplitsAndMergesWithObservedCounts() {
        FakePlayer player = new FakePlayer();
        player.put(18, "minecraft:stone", 8);
        player.put(1, "minecraft:stone", 2);

        var split = PlayerPrimitiveActions.moveItem(18, 0, 3, 64, player);
        var merge = PlayerPrimitiveActions.moveItem(0, 1, 2, 64, player);

        assertTrue(split.success());
        assertEquals("partial", split.reason());
        assertEquals(3, split.facts().get("count").getAsInt());
        assertTrue(merge.success());
        assertEquals(1, player.counts[0]);
        assertEquals(4, player.counts[1]);
    }

    @Test
    void moveItemRefusesDifferentDestinationWithoutMutation() {
        FakePlayer player = new FakePlayer();
        player.put(18, "minecraft:diamond", 3);
        player.put(0, "minecraft:stone", 1);
        String before = player.inventoryFingerprint();

        var outcome = PlayerPrimitiveActions.moveItem(18, 0, 3, 64, player);

        assertFalse(outcome.success());
        assertEquals("destination_occupied", outcome.reason());
        assertEquals(before, player.inventoryFingerprint());
    }

    @Test
    void dropCompletesOnlyAfterTheHotbarCountDecreases() {
        FakePlayer player = new FakePlayer();
        player.put(3, "minecraft:diamond", 2);
        FakeControls controls = new FakeControls(player);
        ActionRegistry registry = new ActionRegistry();
        ActionRuntime runtime = runtime(controls, registry);
        runtime.submit("Bot", "drop-1", "PLAYER_ACTION:dropItem", OwnerPriority.ACTION, 30);
        runtime.attachExecutor("drop-1", new PlayerPrimitiveActions.DropExecutor(
            "Bot", "drop-1", 3, "one", player, controls, runtime
        ));

        runtime.tick(31);
        runtime.tick(32);

        var terminal = registry.status("drop-1").terminal();
        assertEquals("completed", terminal.get("classification").getAsString());
        assertEquals(2, terminal.get("count_before").getAsInt());
        assertEquals(1, terminal.get("count_after").getAsInt());
    }

    private static ActionRuntime runtime(BotControls controls, ActionRegistry registry) {
        return new ActionRuntime(
            new FakePlayerActionOwner(), controls, registry, new BotEventStream()
        );
    }
}
