package dev.minebot.body.action;

import dev.minebot.body.control.FakePlayerActionOwner;
import dev.minebot.body.control.OwnerPriority;
import dev.minebot.body.event.BotEventStream;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;

final class SpecialUseActionsTest {
    private static final class FakeWorld implements SpecialUseActions.WorldAccess {
        boolean present = true;
        String target = "minecraft:air";
        String observed = "minecraft:air";
        String selected = "minecraft:flint_and_steel";
        int selectedCount = 1;
        int substitutes;
        PlayerPrimitiveActions.Position position = new PlayerPrimitiveActions.Position(0.5, 64, 0.5);

        @Override public boolean present() { return present; }
        @Override public String blockIdAt(int x, int y, int z) {
            return y == 64 ? target : observed;
        }
        @Override public String selectedItemId() { return selected; }
        @Override public int selectedItemCount() { return selectedCount; }
        @Override public PlayerPrimitiveActions.Position position() {
            return position;
        }
        @Override public boolean substituteFire(int x, int y, int z) {
            substitutes++;
            target = "minecraft:fire";
            return true;
        }
        @Override public boolean substituteCrop(int x, int y, int z, String crop, String seed) {
            substitutes++;
            observed = crop;
            selectedCount--;
            return true;
        }
    }

    private static final class FakeControls implements PlayerPrimitiveActions.Controls, BotControls {
        final FakeWorld world;
        boolean physicalEffect = true;
        int uses;
        int clears;

        FakeControls(FakeWorld world) {
            this.world = world;
        }

        @Override public void selectHotbar(String botName, int slot) {}
        @Override public void lookAt(String botName, double x, double y, double z) {}
        @Override public void useContinuous(String botName) {}
        @Override public void dropOne(String botName) {}
        @Override public void dropStack(String botName) {}
        @Override public void clearAll(String botName) { clears++; }
        @Override public void useOnce(String botName) {
            uses++;
            if (!physicalEffect) {
                return;
            }
            if ("minecraft:flint_and_steel".equals(world.selected)) {
                world.target = "minecraft:fire";
            } else {
                world.observed = "minecraft:wheat";
                world.selectedCount--;
            }
        }
    }

    @Test
    void governedPhysicalSowRequiresCropAndSeedDelta() {
        FakeWorld world = new FakeWorld();
        world.target = "minecraft:farmland";
        world.selected = "minecraft:wheat_seeds";
        world.selectedCount = 3;
        FakeControls controls = new FakeControls(world);
        MutationGate gate = new MutationGate();
        List<MutationGate.Proposal> proposals = new ArrayList<>();
        ActionRegistry registry = new ActionRegistry();
        ActionRuntime runtime = runtime(controls, registry);
        runtime.submit("Bot", "sow-1", "PLAYER_ACTION:sowCrop", OwnerPriority.ACTION, 10);
        runtime.attachExecutor("sow-1", new SpecialUseActions.Executor(
            "Bot", "sow-1", SpecialUseActions.Mode.SOW,
            1, 64, 0, "minecraft:wheat", "minecraft:wheat_seeds", "farm",
            true, 20, world, controls, gate, proposals::add, runtime
        ));

        runtime.tick(10);
        assertEquals("interact", proposals.getFirst().mutationKind());
        assertEquals("farm", proposals.getFirst().context());
        assertEquals(0, controls.uses);
        gate.verdict(proposals.getFirst().proposalId(), true, "allowed_interaction");
        runtime.tick(11);
        runtime.tick(12);

        var terminal = registry.status("sow-1").terminal();
        assertEquals("completed", terminal.get("classification").getAsString());
        assertEquals("minecraft:wheat", terminal.get("block_after").getAsString());
        assertEquals(3, terminal.get("item_count_before").getAsInt());
        assertEquals(2, terminal.get("item_count_after").getAsInt());
        assertEquals("physical", terminal.get("method").getAsString());
    }

    @Test
    void igniteUsesSubstituteOnlyAfterPhysicalUseHasNoEffect() {
        FakeWorld world = new FakeWorld();
        FakeControls controls = new FakeControls(world);
        controls.physicalEffect = false;
        MutationGate gate = new MutationGate();
        List<MutationGate.Proposal> proposals = new ArrayList<>();
        ActionRegistry registry = new ActionRegistry();
        ActionRuntime runtime = runtime(controls, registry);
        runtime.submit("Bot", "ignite-1", "PLAYER_ACTION:igniteBlock", OwnerPriority.ACTION, 20);
        runtime.attachExecutor("ignite-1", new SpecialUseActions.Executor(
            "Bot", "ignite-1", SpecialUseActions.Mode.IGNITE,
            1, 64, 0, "minecraft:fire", "minecraft:flint_and_steel", "activate",
            true, 20, world, controls, gate, proposals::add, runtime
        ));

        runtime.tick(20);
        gate.verdict(proposals.getFirst().proposalId(), true, "allowed_interaction");
        runtime.tick(21);
        runtime.tick(22);
        assertEquals(ActionRegistry.State.RUNNING, registry.status("ignite-1").state());
        runtime.tick(23);

        var terminal = registry.status("ignite-1").terminal();
        assertEquals("completed", terminal.get("classification").getAsString());
        assertEquals("substitute", terminal.get("method").getAsString());
        assertEquals("minecraft:fire", terminal.get("block_after").getAsString());
        assertEquals(1, world.substitutes);
    }

    @Test
    void governanceDenialPreventsPhysicalAndSubstituteMutation() {
        FakeWorld world = new FakeWorld();
        FakeControls controls = new FakeControls(world);
        MutationGate gate = new MutationGate();
        List<MutationGate.Proposal> proposals = new ArrayList<>();
        ActionRegistry registry = new ActionRegistry();
        ActionRuntime runtime = runtime(controls, registry);
        runtime.submit("Bot", "ignite-denied", "PLAYER_ACTION:igniteBlock", OwnerPriority.ACTION, 30);
        runtime.attachExecutor("ignite-denied", new SpecialUseActions.Executor(
            "Bot", "ignite-denied", SpecialUseActions.Mode.IGNITE,
            1, 64, 0, "minecraft:fire", "minecraft:flint_and_steel", "activate",
            true, 20, world, controls, gate, proposals::add, runtime
        ));

        runtime.tick(30);
        gate.verdict(proposals.getFirst().proposalId(), false, "protected_region");
        runtime.tick(31);

        var terminal = registry.status("ignite-denied").terminal();
        assertEquals("unsafe", terminal.get("classification").getAsString());
        assertEquals("governance_denied:protected_region", terminal.get("reason").getAsString());
        assertEquals(0, controls.uses);
        assertEquals(0, world.substitutes);
        assertEquals("minecraft:air", world.target);
        assertFalse(terminal.get("success").getAsBoolean());
    }

    @Test
    void outOfRangeSpecialUseDoesNotProposeOrMutate() {
        FakeWorld world = new FakeWorld();
        world.position = new PlayerPrimitiveActions.Position(20.5, 64, 0.5);
        FakeControls controls = new FakeControls(world);
        MutationGate gate = new MutationGate();
        List<MutationGate.Proposal> proposals = new ArrayList<>();
        ActionRegistry registry = new ActionRegistry();
        ActionRuntime runtime = runtime(controls, registry);
        runtime.submit("Bot", "ignite-far", "PLAYER_ACTION:igniteBlock", OwnerPriority.ACTION, 40);
        runtime.attachExecutor("ignite-far", new SpecialUseActions.Executor(
            "Bot", "ignite-far", SpecialUseActions.Mode.IGNITE,
            1, 64, 0, "minecraft:fire", "minecraft:flint_and_steel", "activate",
            true, 20, world, controls, gate, proposals::add, runtime
        ));

        runtime.tick(40);

        var terminal = registry.status("ignite-far").terminal();
        assertEquals("target_out_of_range", terminal.get("reason").getAsString());
        assertEquals(0, proposals.size());
        assertEquals(0, controls.uses);
        assertEquals(0, world.substitutes);
        assertEquals("minecraft:air", world.target);
    }

    private static ActionRuntime runtime(BotControls controls, ActionRegistry registry) {
        return new ActionRuntime(
            new FakePlayerActionOwner(), controls, registry, new BotEventStream()
        );
    }
}
