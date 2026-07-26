package dev.minebot.body.action;

import com.google.gson.JsonObject;
import dev.minebot.body.control.FakePlayerActionOwner;
import dev.minebot.body.control.OwnerPriority;
import dev.minebot.body.event.BotEventStream;
import dev.minebot.body.nav.MovementControls;
import dev.minebot.body.nav.NavigateExecutor;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

final class AscendExecutorTest {
    private static final String STONE = "minecraft:stone";
    private static final String AIR = "minecraft:air";

    /** Fake body: rises one block each tick while trying to jump. */
    private static final class FakeBody implements MovementControls, CollectExecutor.MiningControls, BotControls {
        double y;
        boolean attacking;
        boolean wantRise;
        final List<String> log = new ArrayList<>();

        FakeBody(double y) {
            this.y = y;
        }

        @Override public void lookAt(String b, double x, double yy, double z) { }
        @Override public void moveForward(String b) { }
        @Override public void stopMovement(String b) { }
        @Override public void sprint(String b) { }
        @Override public void jumpOnce(String b) { wantRise = true; log.add("jump"); }
        @Override public void attackContinuous(String b) { attacking = true; log.add("attack"); }
        @Override public void clearAll(String b) { attacking = false; log.add("clearAll"); }

        void physics() {
            if (wantRise) {
                y += 1.0;
                wantRise = false;
            }
        }
    }

    private static final class World {
        final Map<String, String> blocks = new HashMap<>();
        boolean sky;

        String at(int x, int y, int z) {
            return blocks.getOrDefault(x + "," + y + "," + z, AIR);
        }

        void set(int x, int y, int z, String id) {
            blocks.put(x + "," + y + "," + z, id);
        }
    }

    private static final class Harness {
        final ActionRuntime runtime;
        final ActionRegistry registry;
        final FakeBody body;
        final MutationGate gate;
        final World world;
        final List<MutationGate.Proposal> proposals;

        private Harness(ActionRuntime runtime, ActionRegistry registry, FakeBody body, MutationGate gate,
                        World world, List<MutationGate.Proposal> proposals) {
            this.runtime = runtime;
            this.registry = registry;
            this.body = body;
            this.gate = gate;
            this.world = world;
            this.proposals = proposals;
        }

        static Harness create(World world, FakeBody body, int targetY, int timeout) {
            ActionRegistry registry = new ActionRegistry();
            BotEventStream events = new BotEventStream();
            MutationGate gate = new MutationGate();
            ActionRuntime runtime = new ActionRuntime(new FakePlayerActionOwner(), body, registry, events);
            runtime.submit("Bot", "asc-1", "ASCEND", OwnerPriority.RECOVERY, 0);
            List<MutationGate.Proposal> proposals = new ArrayList<>();
            AscendExecutor executor = new AscendExecutor(
                "Bot", "asc-1", targetY,
                body, body, body,
                new AscendExecutor.BlockReader() {
                    public String blockIdAt(int x, int y, int z) { return world.at(x, y, z); }
                    public boolean skyAbove(int x, int y, int z) { return world.sky; }
                },
                id -> id.equals("minecraft:lava") || id.equals("minecraft:fire"),
                gate,
                proposals::add,
                b -> new NavigateExecutor.PositionSource.Position(0.5, body.y, 0.5),
                (b, tick, name, aid, data) -> events.emit(b, tick, name, aid, data),
                runtime, timeout
            );
            runtime.attachExecutor("asc-1", executor);
            return new Harness(runtime, registry, body, gate, world, proposals);
        }

        JsonObject run(int maxTicks, Runnable perTick) {
            for (int tick = 1; tick <= maxTicks; tick++) {
                runtime.tick(tick);
                body.physics();
                perTick.run();
                var status = registry.status("asc-1");
                if (status.state() == ActionRegistry.State.TERMINAL) {
                    return status.terminal();
                }
            }
            throw new AssertionError("no terminal in " + maxTicks + " ticks");
        }
    }

    @Test
    void digsUpThroughStoneToTheSurfaceUnderGovernance() {
        World world = new World();
        FakeBody body = new FakeBody(60);
        // A stone ceiling at feet+2 for the first two rises, then open sky.
        world.set(0, 62, 0, STONE);
        Harness h = Harness.create(world, body, 66, 4_000);

        JsonObject terminal = h.run(3_000, () -> {
            // Governance allows; the block breaks two ticks after attack starts.
            for (var p : h.proposals) {
                h.gate.verdict(p.proposalId(), true, "natural_terrain");
            }
            if (h.body.attacking) {
                int cy = 62;
                // Clear whichever ceiling was proposed most recently.
                for (var p : h.proposals) {
                    h.world.set(p.x(), p.y(), p.z(), AIR);
                }
            }
            // After rising twice, expose sky so it completes.
            if (h.body.y >= 62) {
                h.world.sky = true;
            }
        });

        assertEquals("completed", terminal.get("classification").getAsString());
        assertEquals("surface_reached", terminal.get("reason").getAsString());
        assertTrue(terminal.get("dig_steps").getAsInt() >= 1);
        assertTrue(h.body.log.contains("attack"));
    }

    @Test
    void deniedCeilingBreakNeverDigs() {
        World world = new World();
        FakeBody body = new FakeBody(60);
        world.set(0, 62, 0, STONE);
        Harness h = Harness.create(world, body, 66, 2_000);

        JsonObject terminal = h.run(1_500, () -> {
            for (var p : h.proposals) {
                h.gate.verdict(p.proposalId(), false, "protected_region");
            }
        });

        assertEquals("failed", terminal.get("classification").getAsString());
        assertTrue(terminal.get("reason").getAsString().startsWith("governance_denied"));
        assertEquals(STONE, world.at(0, 62, 0), "a denied ceiling is untouched");
        assertFalse(body.log.contains("attack"));
    }

    @Test
    void hazardAboveIsUnsafeNeverDug() {
        World world = new World();
        FakeBody body = new FakeBody(60);
        world.set(0, 62, 0, "minecraft:lava");
        Harness h = Harness.create(world, body, 66, 2_000);

        JsonObject terminal = h.run(50, () -> { });

        assertEquals("unsafe", terminal.get("classification").getAsString());
        assertTrue(terminal.get("reason").getAsString().startsWith("hazard_above"));
        assertTrue(h.proposals.isEmpty(), "no proposal for a hazard ceiling");
    }

    @Test
    void alreadyAtSurfaceCompletesImmediately() {
        World world = new World();
        world.sky = true;
        FakeBody body = new FakeBody(70);
        Harness h = Harness.create(world, body, 66, 500);

        JsonObject terminal = h.run(10, () -> { });

        assertEquals("completed", terminal.get("classification").getAsString());
        assertEquals(0, terminal.get("dig_steps").getAsInt());
    }

    @Test
    void missingVerdictFailsClosed() {
        World world = new World();
        FakeBody body = new FakeBody(60);
        world.set(0, 62, 0, STONE);
        Harness h = Harness.create(world, body, 66, 2_000);

        JsonObject terminal = h.run(1_500, () -> { });

        assertEquals("failed", terminal.get("classification").getAsString());
        assertEquals("governance_verdict_timeout", terminal.get("reason").getAsString());
        assertFalse(body.log.contains("attack"));
    }
}
