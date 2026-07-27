package dev.minebot.body.action;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;

/**
 * Ordinary player primitives shared by the Python data-plane transactions.
 * Selection preserves whole stacks, look completion is observed from the
 * player's view vector, and use completes only after a server tick observes
 * either the declared item changing or the bounded empty-hand gesture ending.
 */
public final class PlayerPrimitiveActions {
    public static final int HOTBAR_SIZE = 9;
    public static final int CARRY_END_EXCLUSIVE = 36;
    public static final int MAX_USE_TICKS = 200;
    private static final double LOOK_ALIGNMENT_MIN = Math.cos(Math.toRadians(2.0));

    private PlayerPrimitiveActions() {}

    public record Position(double x, double y, double z) {
        JsonArray toJson() {
            JsonArray array = new JsonArray();
            array.add(x);
            array.add(y);
            array.add(z);
            return array;
        }
    }

    public record Outcome(boolean success, String reason, JsonObject facts) {
        public String classification() {
            return success ? ActionRuntime.CLASS_COMPLETED : ActionRuntime.CLASS_FAILED;
        }
    }

    /** Server-authoritative player facts and full-stack inventory mutation. */
    public interface PlayerAccess {
        boolean present();
        int inventorySize();
        String itemIdAt(int slot);
        int itemCountAt(int slot);
        boolean slotEmpty(int slot);
        void moveWholeStack(int fromSlot, int toSlot);
        int selectedHotbarSlot();
        String selectedItemId();
        String inventoryFingerprint();
        Position position();
        Position eyePosition();
        Position lookDirection();
        float yaw();
        float pitch();
    }

    /** Physical controls; production uses only Carpet's public /player surface. */
    public interface Controls {
        void selectHotbar(String botName, int slot);
        void lookAt(String botName, double x, double y, double z);
        void useOnce(String botName);
        void useContinuous(String botName);
    }

    public static Outcome selectItem(
        String botName,
        String itemId,
        PlayerAccess player,
        Controls controls
    ) {
        int found = findItem(player, 0, Math.min(HOTBAR_SIZE, player.inventorySize()), itemId);
        boolean moved = false;
        if (found < 0) {
            found = findItem(
                player,
                HOTBAR_SIZE,
                Math.min(CARRY_END_EXCLUSIVE, player.inventorySize()),
                itemId
            );
            if (found < 0) {
                return outcome(false, "not_in_inventory", itemId, -1, 0, false);
            }

            int hotbarSlot = findEmpty(player, 0, Math.min(HOTBAR_SIZE, player.inventorySize()));
            if (hotbarSlot < 0) {
                int carrySlot = findEmpty(
                    player,
                    HOTBAR_SIZE,
                    Math.min(CARRY_END_EXCLUSIVE, player.inventorySize())
                );
                if (carrySlot < 0) {
                    return outcome(false, "hotbar_full", itemId, -1, player.itemCountAt(found), false);
                }
                hotbarSlot = 0;
                player.moveWholeStack(hotbarSlot, carrySlot);
            }
            player.moveWholeStack(found, hotbarSlot);
            found = hotbarSlot;
            moved = true;
        }

        controls.selectHotbar(botName, found);
        String selected = player.selectedItemId();
        if (player.selectedHotbarSlot() != found || !itemId.equals(selected)) {
            return outcome(false, "selection_verification_failed", itemId, found, player.itemCountAt(found), moved);
        }
        return outcome(
            true,
            moved ? "moved_to_hotbar" : "completed",
            itemId,
            found,
            player.itemCountAt(found),
            moved
        );
    }

    public static Outcome lookAt(
        String botName,
        Position target,
        PlayerAccess player,
        Controls controls
    ) {
        Position eye = player.eyePosition();
        double dx = target.x() - eye.x();
        double dy = target.y() - eye.y();
        double dz = target.z() - eye.z();
        double targetLength = Math.sqrt(dx * dx + dy * dy + dz * dz);
        if (!Double.isFinite(targetLength) || targetLength < 1.0e-6) {
            return lookOutcome(false, "invalid_target", target, player, 0.0);
        }

        controls.lookAt(botName, target.x(), target.y(), target.z());
        Position look = player.lookDirection();
        double lookLength = Math.sqrt(look.x() * look.x() + look.y() * look.y() + look.z() * look.z());
        double alignment = lookLength < 1.0e-6
            ? 0.0
            : (look.x() * dx + look.y() * dy + look.z() * dz) / (lookLength * targetLength);
        boolean aimed = Double.isFinite(alignment) && alignment >= LOOK_ALIGNMENT_MIN;
        return lookOutcome(aimed, aimed ? "completed" : "look_verification_failed", target, player, alignment);
    }

    public static Outcome stop() {
        return new Outcome(true, "completed", baseFacts(true, "completed"));
    }

    private static Outcome lookOutcome(
        boolean success,
        String reason,
        Position target,
        PlayerAccess player,
        double alignment
    ) {
        JsonObject facts = baseFacts(success, reason);
        facts.add("target", target.toJson());
        facts.addProperty("yaw", player.yaw());
        facts.addProperty("pitch", player.pitch());
        facts.addProperty("alignment", alignment);
        return new Outcome(success, reason, facts);
    }

    private static Outcome outcome(
        boolean success,
        String reason,
        String item,
        int slot,
        int count,
        boolean moved
    ) {
        JsonObject facts = baseFacts(success, reason);
        facts.addProperty("item", item);
        facts.addProperty("slot", slot);
        facts.addProperty("count", count);
        facts.addProperty("moved_to_hotbar", moved);
        return new Outcome(success, reason, facts);
    }

    private static JsonObject baseFacts(boolean success, String reason) {
        JsonObject facts = new JsonObject();
        facts.addProperty("success", success);
        facts.addProperty("reason", reason);
        facts.addProperty("stopped_reason", reason);
        return facts;
    }

    private static int findItem(PlayerAccess player, int start, int end, String itemId) {
        for (int slot = start; slot < end; slot++) {
            if (!player.slotEmpty(slot) && itemId.equals(player.itemIdAt(slot))) {
                return slot;
            }
        }
        return -1;
    }

    private static int findEmpty(PlayerAccess player, int start, int end) {
        for (int slot = start; slot < end; slot++) {
            if (player.slotEmpty(slot)) {
                return slot;
            }
        }
        return -1;
    }

    /** One-tick deferral keeps the acknowledgement ahead of the terminal event. */
    public static final class ImmediateExecutor implements ActionRuntime.TickExecutor {
        private final String botName;
        private final String actionId;
        private final java.util.function.Supplier<Outcome> operation;
        private final ActionRuntime runtime;
        private boolean ran;

        public ImmediateExecutor(
            String botName,
            String actionId,
            java.util.function.Supplier<Outcome> operation,
            ActionRuntime runtime
        ) {
            this.botName = botName;
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
                runtime.finish(botName, actionId, ActionRuntime.CLASS_CANCELED, baseFacts(false, "canceled"), serverTick);
                return;
            }
            Outcome outcome;
            try {
                outcome = operation.get();
            } catch (RuntimeException error) {
                outcome = new Outcome(
                    false,
                    "player_action_internal_error",
                    baseFacts(false, "player_action_internal_error")
                );
            }
            runtime.finish(botName, actionId, outcome.classification(), outcome.facts(), serverTick);
        }
    }

    /** Tick-bounded physical use with selected-item and inventory-change truth. */
    public static final class UseExecutor implements ActionRuntime.TickExecutor {
        private final String botName;
        private final String actionId;
        private final String mode;
        private final String expectedItem;
        private final int requestedTicks;
        private final PlayerAccess player;
        private final Controls controls;
        private final ActionRuntime runtime;
        private boolean started;
        private int startedTick;
        private String inventoryBefore;
        private Position positionBefore;

        public UseExecutor(
            String botName,
            String actionId,
            String mode,
            String expectedItem,
            int requestedTicks,
            PlayerAccess player,
            Controls controls,
            ActionRuntime runtime
        ) {
            this.botName = botName;
            this.actionId = actionId;
            this.mode = mode;
            this.expectedItem = expectedItem;
            this.requestedTicks = Math.max(1, Math.min(MAX_USE_TICKS, requestedTicks));
            this.player = player;
            this.controls = controls;
            this.runtime = runtime;
        }

        @Override
        public void tick(int serverTick) {
            try {
                tickObserved(serverTick);
            } catch (RuntimeException error) {
                finish(false, "player_action_internal_error", ActionRuntime.CLASS_FAILED, serverTick);
            }
        }

        private void tickObserved(int serverTick) {
            if (runtime.cancelRequested(actionId)) {
                finish(false, "canceled", ActionRuntime.CLASS_CANCELED, serverTick);
                return;
            }
            if (!player.present()) {
                finish(false, "body_missing", ActionRuntime.CLASS_FAILED, serverTick);
                return;
            }
            if (!started) {
                start(serverTick);
                return;
            }

            int elapsed = Math.max(1, serverTick - startedTick);
            boolean inventoryChanged = !inventoryBefore.equals(player.inventoryFingerprint());
            if (!"unknown".equals(expectedItem) && inventoryChanged) {
                finish(true, "completed", ActionRuntime.CLASS_COMPLETED, serverTick);
                return;
            }
            if (elapsed >= requestedTicks) {
                boolean success = "unknown".equals(expectedItem);
                finish(
                    success,
                    success ? "completed" : "no_effect",
                    success ? ActionRuntime.CLASS_COMPLETED : ActionRuntime.CLASS_FAILED,
                    serverTick
                );
                return;
            }
            if ("once".equals(mode)) {
                controls.useOnce(botName);
            }
        }

        private void start(int serverTick) {
            if (!"unknown".equals(expectedItem) && !expectedItem.equals(player.selectedItemId())) {
                finish(false, "selected_item_mismatch", ActionRuntime.CLASS_FAILED, serverTick);
                return;
            }
            started = true;
            startedTick = serverTick;
            inventoryBefore = player.inventoryFingerprint();
            positionBefore = player.position();
            if ("continuous".equals(mode)) {
                controls.useContinuous(botName);
            } else {
                controls.useOnce(botName);
            }
        }

        private void finish(boolean success, String reason, String classification, int serverTick) {
            Position finalPosition = player.position();
            JsonObject facts = baseFacts(success, reason);
            facts.addProperty("mode", mode);
            facts.addProperty("item", expectedItem);
            facts.addProperty("ticks", started ? Math.max(0, serverTick - startedTick) : 0);
            facts.addProperty(
                "inventory_changed",
                started && inventoryBefore != null && !inventoryBefore.equals(player.inventoryFingerprint())
            );
            facts.add("start_pos", (positionBefore == null ? finalPosition : positionBefore).toJson());
            facts.add("final_pos", finalPosition.toJson());
            runtime.finish(botName, actionId, classification, facts, serverTick);
        }
    }
}
