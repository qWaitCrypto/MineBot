package dev.minebot.body.action;

import dev.minebot.body.control.FakePlayerActionOwner;
import dev.minebot.body.control.OwnerPriority;
import dev.minebot.body.event.BotEventStream;
import dev.minebot.body.nav.MovementControls;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

final class BlockPrimitiveActionsTest {
    private static final class FakeWorld implements BlockPrimitiveActions.WorldAccess {
        boolean present = true;
        String targetBlock = "minecraft:stone";
        boolean replaceable;
        boolean collision;
        String selectedItem = "minecraft:cobblestone";
        int selectedCount = 3;
        boolean supportAvailable = true;
        final Set<String> supportCells = new HashSet<>();
        PlayerPrimitiveActions.Position position = new PlayerPrimitiveActions.Position(0.5, 64.0, 0.5);

        @Override public boolean present() { return present; }
        @Override public String blockIdAt(int x, int y, int z) { return targetBlock; }
        @Override public boolean canReplaceAt(int x, int y, int z, boolean replaceLiquid) { return replaceable; }
        @Override public boolean playerIntersects(int x, int y, int z) { return collision; }
        @Override public boolean canPlaceAgainst(int x, int y, int z) {
            return supportCells.isEmpty() ? supportAvailable : supportCells.contains(x + ":" + y + ":" + z);
        }
        @Override public String selectedItemId() { return selectedItem; }
        @Override public int selectedItemCount() { return selectedCount; }
        @Override public PlayerPrimitiveActions.Position position() { return position; }
    }

    private static final class FakeControls
        implements MovementControls, PlayerPrimitiveActions.Controls, BotControls {
        final FakeWorld world;
        int looks;
        int uses;
        int jumps;
        int clears;
        boolean decrementOnUse = true;
        boolean placeAtTarget = true;
        boolean gainOnJump = true;

        FakeControls(FakeWorld world) {
            this.world = world;
        }

        @Override public void moveForward(String botName) {}
        @Override public void stopMovement(String botName) {}
        @Override public void sprint(String botName) {}
        @Override public void selectHotbar(String botName, int slot) {}
        @Override public void useContinuous(String botName) {}
        @Override public void dropOne(String botName) {}
        @Override public void dropStack(String botName) {}

        @Override
        public void lookAt(String botName, double x, double y, double z) {
            looks++;
        }

        @Override
        public void useOnce(String botName) {
            uses++;
            if (placeAtTarget) {
                world.targetBlock = world.selectedItem;
                world.replaceable = false;
            }
            if (decrementOnUse) {
                world.selectedCount--;
            }
        }

        @Override
        public void jumpOnce(String botName) {
            jumps++;
            if (gainOnJump) {
                world.position = new PlayerPrimitiveActions.Position(
                    world.position.x(), world.position.y() + 1.05, world.position.z()
                );
            }
        }

        @Override public void jumpContinuous(String botName) { }

        @Override
        public void clearAll(String botName) {
            clears++;
        }
    }

    private static final class FakeBreaker implements ExactBlockBreaker {
        final FakeWorld world;
        int begins;
        int ticks;
        int aborts;

        FakeBreaker(FakeWorld world) {
            this.world = world;
        }

        @Override
        public Outcome begin(String botName, int x, int y, int z, String expectedBlockId, int serverTick) {
            begins++;
            return Outcome.working();
        }

        @Override
        public Outcome tick(String botName, int serverTick) {
            ticks++;
            world.targetBlock = "minecraft:air";
            return Outcome.complete();
        }

        @Override
        public void abort(String botName) {
            aborts++;
        }
    }

    @Test
    void governedMineWaitsForAllowAndCompletesOnlyAfterWorldChange() {
        FakeWorld world = new FakeWorld();
        FakeControls controls = new FakeControls(world);
        FakeBreaker breaker = new FakeBreaker(world);
        MutationGate gate = new MutationGate();
        List<MutationGate.Proposal> proposals = new ArrayList<>();
        ActionRegistry registry = new ActionRegistry();
        ActionRuntime runtime = runtime(controls, registry);
        runtime.submit("Bot", "mine-1", "PLAYER_ACTION:mineBlock", OwnerPriority.ACTION, 10);
        runtime.attachExecutor("mine-1", new BlockPrimitiveActions.MineExecutor(
            "Bot", "mine-1", 1, 64, 0, "minecraft:stone", "direct", 20,
            world, controls, breaker, gate, proposals::add, runtime
        ));

        runtime.tick(10);
        assertEquals(1, proposals.size());
        assertEquals(0, breaker.begins);
        assertEquals("break", proposals.getFirst().mutationKind());
        assertEquals("direct", proposals.getFirst().context());

        gate.verdict(proposals.getFirst().proposalId(), true, "allowed_natural");
        runtime.tick(11);
        assertEquals(1, breaker.begins);
        assertEquals("minecraft:stone", world.targetBlock);
        runtime.tick(12);

        var terminal = registry.status("mine-1").terminal();
        assertEquals("completed", terminal.get("classification").getAsString());
        assertEquals("minecraft:air", terminal.get("block_after").getAsString());
        assertTrue(terminal.get("success").getAsBoolean());
    }

    @Test
    void deniedMineNeverStartsTheBreaker() {
        FakeWorld world = new FakeWorld();
        FakeControls controls = new FakeControls(world);
        FakeBreaker breaker = new FakeBreaker(world);
        MutationGate gate = new MutationGate();
        List<MutationGate.Proposal> proposals = new ArrayList<>();
        ActionRegistry registry = new ActionRegistry();
        ActionRuntime runtime = runtime(controls, registry);
        runtime.submit("Bot", "mine-denied", "PLAYER_ACTION:mineBlock", OwnerPriority.ACTION, 20);
        runtime.attachExecutor("mine-denied", new BlockPrimitiveActions.MineExecutor(
            "Bot", "mine-denied", 1, 64, 0, "minecraft:stone", "direct", 20,
            world, controls, breaker, gate, proposals::add, runtime
        ));

        runtime.tick(20);
        gate.verdict(proposals.getFirst().proposalId(), false, "protected_region");
        runtime.tick(21);

        var terminal = registry.status("mine-denied").terminal();
        assertEquals("unsafe", terminal.get("classification").getAsString());
        assertEquals("governance_denied:protected_region", terminal.get("reason").getAsString());
        assertEquals(0, breaker.begins);
        assertEquals("minecraft:stone", world.targetBlock);
    }

    @Test
    void governedPlaceRequiresWorldAndInventoryDelta() {
        FakeWorld world = new FakeWorld();
        world.targetBlock = "minecraft:air";
        world.replaceable = true;
        FakeControls controls = new FakeControls(world);
        MutationGate gate = new MutationGate();
        List<MutationGate.Proposal> proposals = new ArrayList<>();
        ActionRegistry registry = new ActionRegistry();
        ActionRuntime runtime = runtime(controls, registry);
        runtime.submit("Bot", "place-1", "PLAYER_ACTION:placeBlock", OwnerPriority.ACTION, 30);
        runtime.attachExecutor("place-1", new BlockPrimitiveActions.PlaceExecutor(
            "Bot", "place-1", 1, 64, 0, "minecraft:cobblestone", "up", "work", false, 20,
            world, controls, gate, proposals::add, runtime
        ));

        runtime.tick(30);
        assertEquals("place", proposals.getFirst().mutationKind());
        assertEquals("work", proposals.getFirst().context());
        assertEquals(0, controls.uses);
        gate.verdict(proposals.getFirst().proposalId(), true, "allowed_place");
        runtime.tick(31);
        runtime.tick(32);

        var terminal = registry.status("place-1").terminal();
        assertEquals("completed", terminal.get("classification").getAsString());
        assertEquals("minecraft:cobblestone", terminal.get("block_after").getAsString());
        assertEquals(3, terminal.get("item_count_before").getAsInt());
        assertEquals(2, terminal.get("item_count_after").getAsInt());
        assertEquals(1, controls.uses);
        assertEquals(1, controls.looks);
    }

    @Test
    void deniedPlaceDoesNotUseTheSelectedBlock() {
        FakeWorld world = new FakeWorld();
        world.targetBlock = "minecraft:air";
        world.replaceable = true;
        FakeControls controls = new FakeControls(world);
        MutationGate gate = new MutationGate();
        List<MutationGate.Proposal> proposals = new ArrayList<>();
        ActionRegistry registry = new ActionRegistry();
        ActionRuntime runtime = runtime(controls, registry);
        runtime.submit("Bot", "place-denied", "PLAYER_ACTION:placeBlock", OwnerPriority.ACTION, 40);
        runtime.attachExecutor("place-denied", new BlockPrimitiveActions.PlaceExecutor(
            "Bot", "place-denied", 1, 64, 0, "minecraft:cobblestone", "up", "work", false, 20,
            world, controls, gate, proposals::add, runtime
        ));

        runtime.tick(40);
        gate.verdict(proposals.getFirst().proposalId(), false, "protected_region");
        runtime.tick(41);

        assertEquals(0, controls.uses);
        assertEquals(3, world.selectedCount);
        assertEquals("minecraft:air", world.targetBlock);
        assertFalse(registry.status("place-denied").terminal().get("success").getAsBoolean());
    }

    @Test
    void placeRejectsWorldChangeWithoutSelectedInventoryDelta() {
        FakeWorld world = new FakeWorld();
        world.targetBlock = "minecraft:air";
        world.replaceable = true;
        FakeControls controls = new FakeControls(world);
        controls.decrementOnUse = false;
        MutationGate gate = new MutationGate();
        List<MutationGate.Proposal> proposals = new ArrayList<>();
        ActionRegistry registry = new ActionRegistry();
        ActionRuntime runtime = runtime(controls, registry);
        runtime.submit("Bot", "place-no-delta", "PLAYER_ACTION:placeBlock", OwnerPriority.ACTION, 45);
        runtime.attachExecutor("place-no-delta", new BlockPrimitiveActions.PlaceExecutor(
            "Bot", "place-no-delta", 1, 64, 0, "minecraft:cobblestone", "up", "work", false, 20,
            world, controls, gate, proposals::add, runtime
        ));

        runtime.tick(45);
        gate.verdict(proposals.getFirst().proposalId(), true, "allowed_place");
        runtime.tick(46);
        runtime.tick(47);

        var terminal = registry.status("place-no-delta").terminal();
        assertEquals("failed", terminal.get("classification").getAsString());
        assertEquals("placement_inventory_delta_missing", terminal.get("reason").getAsString());
    }

    @Test
    void autoFaceUsesAnActuallySupportedNeighbor() {
        FakeWorld world = new FakeWorld();
        world.targetBlock = "minecraft:air";
        world.replaceable = true;
        world.supportCells.add("0:64:0");
        FakeControls controls = new FakeControls(world);
        MutationGate gate = new MutationGate();
        List<MutationGate.Proposal> proposals = new ArrayList<>();
        ActionRegistry registry = new ActionRegistry();
        ActionRuntime runtime = runtime(controls, registry);
        runtime.submit("Bot", "place-auto", "PLAYER_ACTION:placeBlock", OwnerPriority.ACTION, 48);
        runtime.attachExecutor("place-auto", new BlockPrimitiveActions.PlaceExecutor(
            "Bot", "place-auto", 1, 64, 0, "minecraft:cobblestone", "auto", "work", false, 20,
            world, controls, gate, proposals::add, runtime
        ));

        runtime.tick(48);
        gate.verdict(proposals.getFirst().proposalId(), true, "allowed_place");
        runtime.tick(49);
        runtime.tick(50);

        var terminal = registry.status("place-auto").terminal();
        assertEquals("completed", terminal.get("classification").getAsString());
        assertEquals("east", terminal.get("face").getAsString());
    }

    @Test
    void placeFailsBeforeUseWhenNoSupportingFaceExists() {
        FakeWorld world = new FakeWorld();
        world.targetBlock = "minecraft:air";
        world.replaceable = true;
        world.supportAvailable = false;
        FakeControls controls = new FakeControls(world);
        ActionRegistry registry = new ActionRegistry();
        ActionRuntime runtime = runtime(controls, registry);
        runtime.submit("Bot", "place-no-support", "PLAYER_ACTION:placeBlock", OwnerPriority.ACTION, 51);
        runtime.attachExecutor("place-no-support", new BlockPrimitiveActions.PlaceExecutor(
            "Bot", "place-no-support", 1, 64, 0, "minecraft:cobblestone", "auto", "work", false, 20,
            world, controls, new MutationGate(), proposal -> {}, runtime
        ));

        runtime.tick(51);

        var terminal = registry.status("place-no-support").terminal();
        assertEquals("failed", terminal.get("classification").getAsString());
        assertEquals("placement_support_missing", terminal.get("reason").getAsString());
        assertEquals(0, controls.uses);
    }

    @Test
    void consumedItemWithoutTargetEffectFailsAsTargetMissInsteadOfTimingOut() {
        FakeWorld world = new FakeWorld();
        world.targetBlock = "minecraft:air";
        world.replaceable = true;
        FakeControls controls = new FakeControls(world);
        controls.placeAtTarget = false;
        MutationGate gate = new MutationGate();
        List<MutationGate.Proposal> proposals = new ArrayList<>();
        ActionRegistry registry = new ActionRegistry();
        ActionRuntime runtime = runtime(controls, registry);
        runtime.submit("Bot", "place-missed", "PLAYER_ACTION:placeBlock", OwnerPriority.ACTION, 52);
        runtime.attachExecutor("place-missed", new BlockPrimitiveActions.PlaceExecutor(
            "Bot", "place-missed", 1, 64, 0, "minecraft:cobblestone", "up", "work", false, 40,
            world, controls, gate, proposals::add, runtime
        ));

        runtime.tick(52);
        gate.verdict(proposals.getFirst().proposalId(), true, "allowed_place");
        for (int tick = 53; tick <= 59; tick++) {
            runtime.tick(tick);
        }

        var terminal = registry.status("place-missed").terminal();
        assertEquals("failed", terminal.get("classification").getAsString());
        assertEquals("placement_target_missed", terminal.get("reason").getAsString());
        assertEquals(3, terminal.get("item_count_before").getAsInt());
        assertEquals(2, terminal.get("item_count_after").getAsInt());
    }

    @Test
    void jumpCompletesOnlyAfterObservedHeightGain() {
        FakeWorld world = new FakeWorld();
        FakeControls controls = new FakeControls(world);
        ActionRegistry registry = new ActionRegistry();
        ActionRuntime runtime = runtime(controls, registry);
        runtime.submit("Bot", "jump-1", "PLAYER_ACTION:jump", OwnerPriority.ACTION, 50);
        runtime.attachExecutor("jump-1", new BlockPrimitiveActions.JumpExecutor(
            "Bot", "jump-1", 5, world, controls, runtime
        ));

        runtime.tick(50);
        assertEquals(ActionRegistry.State.RUNNING, registry.status("jump-1").state());
        runtime.tick(51);

        var terminal = registry.status("jump-1").terminal();
        assertEquals("completed", terminal.get("classification").getAsString());
        assertTrue(terminal.get("gained_y").getAsDouble() >= 1.0);
        assertEquals(1, controls.jumps);
    }

    @Test
    void jumpWithoutObservedHeightGainFailsHonestly() {
        FakeWorld world = new FakeWorld();
        FakeControls controls = new FakeControls(world);
        controls.gainOnJump = false;
        ActionRegistry registry = new ActionRegistry();
        ActionRuntime runtime = runtime(controls, registry);
        runtime.submit("Bot", "jump-stuck", "PLAYER_ACTION:jump", OwnerPriority.ACTION, 60);
        runtime.attachExecutor("jump-stuck", new BlockPrimitiveActions.JumpExecutor(
            "Bot", "jump-stuck", 2, world, controls, runtime
        ));

        runtime.tick(60);
        runtime.tick(61);
        runtime.tick(62);
        runtime.tick(63);

        var terminal = registry.status("jump-stuck").terminal();
        assertEquals("failed", terminal.get("classification").getAsString());
        assertEquals("jump_no_height_gain", terminal.get("reason").getAsString());
        assertEquals(0.0, terminal.get("gained_y").getAsDouble());
    }

    private static ActionRuntime runtime(BotControls controls, ActionRegistry registry) {
        return new ActionRuntime(
            new FakePlayerActionOwner(), controls, registry, new BotEventStream()
        );
    }
}
