package dev.minebot.body.action;

import com.google.gson.JsonObject;
import dev.minebot.body.control.FakePlayerActionOwner;
import dev.minebot.body.control.OwnerPriority;
import dev.minebot.body.event.BotEventStream;
import dev.minebot.body.nav.MovementControls;
import dev.minebot.body.nav.NavigateExecutor;
import dev.minebot.body.nav.WorldView;
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

    /** Fake physics only climbs after forward+jump targets a cleared stair. */
    private static final class FakeBody implements MovementControls, ExactBlockBreaker, BotControls {
        double x = 0.5;
        double y;
        double z = 0.5;
        double lookX;
        double lookY;
        double lookZ;
        boolean attacking;
        boolean moving;
        boolean wantJump;
        boolean partialClimb;
        int breakX;
        int breakY;
        int breakZ;
        final List<String> log = new ArrayList<>();
        final List<String> breakTargets = new ArrayList<>();

        FakeBody(double y) {
            this.y = y;
        }

        @Override public void lookAt(String b, double x, double yy, double z) {
            lookX = x;
            lookY = yy;
            lookZ = z;
        }
        @Override public void moveForward(String b) { moving = true; log.add("forward"); }
        @Override public void stopMovement(String b) { moving = false; }
        @Override public void sprint(String b) { }
        @Override public void jumpOnce(String b) { wantJump = true; log.add("jump"); }
        @Override public Outcome begin(String b, int x, int y, int z, String blockId, int tick) {
            breakX = x;
            breakY = y;
            breakZ = z;
            attacking = true;
            breakTargets.add(x + "," + y + "," + z + ":" + blockId);
            log.add("exactBreak");
            return Outcome.working();
        }
        @Override public Outcome tick(String b, int tick) { return Outcome.working(); }
        @Override public void abort(String b) { attacking = false; }
        @Override public void clearAll(String b) {
            attacking = false;
            moving = false;
            wantJump = false;
            log.add("clearAll");
        }

        void physics() {
            if (moving && wantJump) {
                x = partialClimb ? Math.floor(lookX) - 0.2 : Math.floor(lookX) + 0.5;
                y = Math.floor(lookY);
                z = Math.floor(lookZ) + 0.5;
                wantJump = false;
            }
        }
    }

    private static final class World implements WorldView {
        final Map<String, String> blocks = new HashMap<>();
        boolean sky;

        String at(int x, int y, int z) {
            return blocks.getOrDefault(x + "," + y + "," + z, STONE);
        }

        void set(int x, int y, int z, String id) {
            blocks.put(x + "," + y + "," + z, id);
        }

        @Override
        public NodeKind kindAt(int x, int y, int z) {
            String block = at(x, y, z);
            if (block.equals(AIR)) {
                return NodeKind.PASSABLE;
            }
            if (block.equals("minecraft:lava") || block.equals("minecraft:water")) {
                return NodeKind.HAZARD;
            }
            return NodeKind.SOLID;
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
                id -> id.equals("minecraft:lava") || id.equals("minecraft:water"),
                gate,
                proposals::add,
                b -> new NavigateExecutor.PositionSource.Position(body.x, body.y, body.z),
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

    private static World enclosedStart() {
        World world = new World();
        world.set(0, 60, 0, AIR);
        world.set(0, 61, 0, AIR);
        return world;
    }

    @Test
    void carvesAndWalksUpARealStairUnderGovernance() {
        World world = enclosedStart();
        FakeBody body = new FakeBody(60);
        Harness h = Harness.create(world, body, 70, 4_000);

        JsonObject terminal = h.run(3_000, () -> {
            for (var proposal : h.proposals) {
                h.gate.verdict(proposal.proposalId(), true, "natural_terrain");
            }
            if (h.body.attacking) {
                h.world.set(h.body.breakX, h.body.breakY, h.body.breakZ, AIR);
            }
            if (h.body.y >= 61) {
                h.world.sky = true;
            }
        });

        assertEquals(
            "completed",
            terminal.get("classification").getAsString(),
            terminal + " controls=" + body.log
        );
        assertEquals("surface_reached", terminal.get("reason").getAsString());
        assertEquals(1, terminal.get("ascend_steps").getAsInt());
        assertEquals(3, terminal.get("break_steps").getAsInt());
        assertTrue(body.log.contains("forward"));
        assertTrue(body.log.contains("jump"));
        assertEquals(3, body.breakTargets.size());
        for (int i = 0; i < h.proposals.size(); i++) {
            MutationGate.Proposal proposal = h.proposals.get(i);
            assertEquals(
                proposal.x() + "," + proposal.y() + "," + proposal.z() + ":" + proposal.blockId(),
                body.breakTargets.get(i)
            );
        }
    }

    @Test
    void deniedStairBreakNeverMoves() {
        World world = enclosedStart();
        FakeBody body = new FakeBody(60);
        Harness h = Harness.create(world, body, 70, 2_000);

        JsonObject terminal = h.run(1_500, () -> {
            for (var proposal : h.proposals) {
                h.gate.verdict(proposal.proposalId(), false, "protected_region");
            }
        });

        assertEquals("failed", terminal.get("classification").getAsString());
        assertTrue(terminal.get("reason").getAsString().startsWith("governance_denied"));
        assertEquals(60.0, body.y);
        assertFalse(body.log.contains("forward"));
    }

    @Test
    void hazardInEveryStairDirectionIsUnsafeAndUntouched() {
        World world = enclosedStart();
        for (int[] direction : new int[][] {{1, 0}, {0, 1}, {-1, 0}, {0, -1}}) {
            world.set(direction[0], 61, direction[1], "minecraft:lava");
        }
        FakeBody body = new FakeBody(60);
        Harness h = Harness.create(world, body, 70, 2_000);

        JsonObject terminal = h.run(50, () -> { });

        assertEquals("unsafe", terminal.get("classification").getAsString());
        assertEquals("hazard_blocks_stair_route", terminal.get("reason").getAsString());
        assertTrue(h.proposals.isEmpty());
        assertFalse(body.log.contains("exactBreak"));
    }

    @Test
    void alreadyAtSurfaceCompletesImmediately() {
        World world = enclosedStart();
        world.sky = true;
        FakeBody body = new FakeBody(70);
        Harness h = Harness.create(world, body, 70, 500);

        JsonObject terminal = h.run(10, () -> { });

        assertEquals("completed", terminal.get("classification").getAsString());
        assertEquals(0, terminal.get("ascend_steps").getAsInt());
    }

    @Test
    void missingVerdictFailsClosed() {
        World world = enclosedStart();
        FakeBody body = new FakeBody(60);
        Harness h = Harness.create(world, body, 70, 2_000);

        JsonObject terminal = h.run(1_500, () -> { });

        assertEquals("failed", terminal.get("classification").getAsString());
        assertEquals("governance_verdict_timeout", terminal.get("reason").getAsString());
        assertFalse(body.log.contains("exactBreak"));
        assertFalse(body.log.contains("forward"));
    }

    @Test
    void airborneEdgeContactDoesNotCountAsStandingOnTheNextStep() {
        World world = enclosedStart();
        FakeBody body = new FakeBody(60);
        body.partialClimb = true;
        Harness h = Harness.create(world, body, 70, 2_000);

        JsonObject terminal = h.run(1_500, () -> {
            for (var proposal : h.proposals) {
                h.gate.verdict(proposal.proposalId(), true, "natural_terrain");
            }
            if (h.body.attacking) {
                h.world.set(h.body.breakX, h.body.breakY, h.body.breakZ, AIR);
            }
        });

        assertEquals("failed", terminal.get("classification").getAsString());
        assertEquals("stair_step_timeout", terminal.get("reason").getAsString());
        assertEquals(0, terminal.get("ascend_steps").getAsInt());
    }
}
