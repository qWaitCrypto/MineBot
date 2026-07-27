package dev.minebot.body.inventory;

import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

final class InventorySnapshotTest {
    @Test
    void preservesTheExistingSlotTypesAndLabels() {
        assertEquals("hotbar", InventorySnapshot.slotType(0));
        assertEquals("hotbar.8", InventorySnapshot.slotLabel(8));
        assertEquals("inventory", InventorySnapshot.slotType(9));
        assertEquals("inventory.26", InventorySnapshot.slotLabel(35));
        assertEquals("armor.feet", InventorySnapshot.slotLabel(36));
        assertEquals("armor.head", InventorySnapshot.slotLabel(39));
        assertEquals("offhand", InventorySnapshot.slotType(40));
        assertEquals("offhand", InventorySnapshot.slotLabel(40));
        assertEquals("aux.0", InventorySnapshot.slotLabel(41));
        assertEquals("aux.4", InventorySnapshot.slotLabel(45));
    }

    @Test
    void pagesAllFortySixLogicalSlotsAndPreservesMetadata() {
        List<InventorySnapshot.StackValue> backing = emptyBacking(43);
        backing.set(0, new InventorySnapshot.StackValue(
            "minecraft:stone", 3, "{\"id\":\"minecraft:stone\",\"count\":3}"
        ));
        backing.set(39, new InventorySnapshot.StackValue(
            "minecraft:diamond_helmet", 1, "{\"components\":{\"minecraft:damage\":7}}"
        ));

        InventorySnapshot.Page first = InventorySnapshot.page(backing, 0, 40);
        assertEquals(0, first.start());
        assertEquals(40, first.limit());
        assertEquals(40, first.nextStart());
        assertEquals(46, first.totalSlots());
        assertEquals(40, first.slots().size());
        assertFalse(first.slots().get(0).empty());
        assertEquals("minecraft:stone", first.slots().get(0).item());
        assertTrue(first.slots().get(39).stackRaw().contains("minecraft:damage"));

        InventorySnapshot.Page second = InventorySnapshot.page(backing, first.nextStart(), 40);
        assertNull(second.nextStart());
        assertEquals(6, second.slots().size());
        assertTrue(second.slots().get(3).empty());
        assertEquals("aux.2", second.slots().get(3).slotLabel());
        assertEquals("aux.4", second.slots().get(5).slotLabel());
    }

    @Test
    void clampsStartAndLimitLikeTheLegacyContract() {
        List<InventorySnapshot.StackValue> backing = emptyBacking(43);

        InventorySnapshot.Page low = InventorySnapshot.page(backing, -50, 0);
        assertEquals(0, low.start());
        assertEquals(1, low.limit());
        assertEquals(1, low.nextStart());

        InventorySnapshot.Page high = InventorySnapshot.page(backing, 100, 100);
        assertEquals(45, high.start());
        assertEquals(46, high.limit());
        assertNull(high.nextStart());
        assertEquals(1, high.slots().size());
        assertEquals(45, high.slots().get(0).slot());
    }

    private static List<InventorySnapshot.StackValue> emptyBacking(int size) {
        List<InventorySnapshot.StackValue> slots = new ArrayList<>();
        for (int index = 0; index < size; index++) {
            slots.add(InventorySnapshot.StackValue.emptySlot());
        }
        return slots;
    }
}
