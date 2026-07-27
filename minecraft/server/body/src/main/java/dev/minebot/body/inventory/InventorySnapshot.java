package dev.minebot.body.inventory;

import java.util.ArrayList;
import java.util.List;

/** Stable 46-slot inventory view shared with the neutral Body contract. */
public final class InventorySnapshot {
    public static final int TOTAL_SLOTS = 46;

    private InventorySnapshot() {}

    public static Page page(List<StackValue> backingSlots, int requestedStart, int requestedLimit) {
        int start = Math.max(0, Math.min(TOTAL_SLOTS - 1, requestedStart));
        int limit = Math.max(1, Math.min(TOTAL_SLOTS, requestedLimit));
        int end = Math.min(TOTAL_SLOTS, start + limit);
        List<Slot> slots = new ArrayList<>(end - start);
        for (int slot = start; slot < end; slot++) {
            StackValue value = slot < backingSlots.size() ? backingSlots.get(slot) : StackValue.emptySlot();
            boolean empty = value == null || value.empty();
            slots.add(new Slot(
                slot,
                slotType(slot),
                slotLabel(slot),
                empty,
                empty ? null : value.item(),
                empty ? 0 : value.count(),
                empty ? null : value.stackRaw()
            ));
        }
        return new Page(start, limit, end >= TOTAL_SLOTS ? null : end, TOTAL_SLOTS, List.copyOf(slots));
    }

    public static String slotType(int slot) {
        if (slot <= 8) {
            return "hotbar";
        }
        if (slot <= 35) {
            return "inventory";
        }
        if (slot <= 39) {
            return "armor";
        }
        if (slot == 40) {
            return "offhand";
        }
        return "aux";
    }

    public static String slotLabel(int slot) {
        if (slot <= 8) {
            return "hotbar." + slot;
        }
        if (slot <= 35) {
            return "inventory." + (slot - 9);
        }
        return switch (slot) {
            case 36 -> "armor.feet";
            case 37 -> "armor.legs";
            case 38 -> "armor.chest";
            case 39 -> "armor.head";
            case 40 -> "offhand";
            default -> "aux." + (slot - 41);
        };
    }

    public record StackValue(String item, int count, String stackRaw) {
        public static StackValue emptySlot() {
            return new StackValue(null, 0, null);
        }

        public boolean empty() {
            return item == null || count <= 0;
        }
    }

    public record Slot(
        int slot,
        String slotType,
        String slotLabel,
        boolean empty,
        String item,
        int count,
        String stackRaw
    ) {}

    public record Page(int start, int limit, Integer nextStart, int totalSlots, List<Slot> slots) {}
}
