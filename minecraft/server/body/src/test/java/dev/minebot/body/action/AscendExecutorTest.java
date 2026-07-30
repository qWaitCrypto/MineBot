package dev.minebot.body.action;

import com.google.gson.JsonObject;
import dev.minebot.body.control.FakePlayerActionOwner;
import dev.minebot.body.control.OwnerPriority;
import dev.minebot.body.event.BotEventStream;
import dev.minebot.body.nav.Goal;
import dev.minebot.body.nav.MovementControls;
import dev.minebot.body.nav.NavigateExecutor;
import dev.minebot.body.nav.WorldView;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

final class AscendExecutorTest {
    private static final String STONE = "minecraft:stone";
    private static final String AIR = "minecraft:air";
    private static final String COBBLESTONE = "minecraft:cobblestone";

    /** Fake physics supports both a cleared stair and jump-place pillar. */
    private static final class FakeBody implements MovementControls, ExactBlockBreaker, BotControls,
                                                    AscendExecutor.PillarAccess {
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
        boolean overshootCentering;
        int breakX;
        int breakY;
        int breakZ;
        String scaffoldItem = COBBLESTONE;
        int scaffoldCount = 8;
        World world;
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
        @Override public void stopMovement(String b) { moving = false; log.add("stop"); }
        @Override public void sprint(String b) { }
        @Override public void jumpOnce(String b) { wantJump = true; log.add("jump"); }
        @Override public void jumpContinuous(String b) { log.add("jumpContinuous"); }
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
        @Override public AscendExecutor.ScaffoldSelection selectScaffold(
            String b,
            List<String> candidates
        ) {
            if (scaffoldCount <= 0 || !candidates.contains(scaffoldItem)) {
                return new AscendExecutor.ScaffoldSelection(
                    false, null, 0, "pillar_no_scaffold_available"
                );
            }
            log.add("selectScaffold");
            return new AscendExecutor.ScaffoldSelection(true, scaffoldItem, scaffoldCount, "completed");
        }
        @Override public String selectedItemId() { return scaffoldCount > 0 ? scaffoldItem : null; }
        @Override public int selectedItemCount() { return scaffoldCount; }
        @Override public void useOnce(String b) {
            log.add("useOnce");
            int targetX = (int) Math.floor(lookX);
            int targetY = (int) Math.floor(lookY + 0.25);
            int targetZ = (int) Math.floor(lookZ);
            if (world != null && scaffoldCount > 0 && y > targetY + 0.15) {
                world.set(targetX, targetY, targetZ, scaffoldItem);
                scaffoldCount--;
                y = targetY + 1.0;
            }
        }
        @Override public void sneak(String b) { log.add("sneak"); }

        void physics() {
            if (moving && wantJump) {
                x = partialClimb ? Math.floor(lookX) - 0.2 : Math.floor(lookX) + 0.5;
                y = Math.floor(lookY);
                z = Math.floor(lookZ) + 0.5;
                wantJump = false;
            } else if (moving && partialClimb) {
                // Preserve the failed edge contact until the controller times out.
            } else if (moving) {
                double dx = lookX - x;
                double dz = lookZ - z;
                double distance = Math.hypot(dx, dz);
                if (distance > 1.0e-6) {
                    double step = overshootCentering ? 0.16 : Math.min(0.25, distance);
                    x += dx / distance * step;
                    z += dz / distance * step;
                }
            } else if (wantJump) {
                y = Math.floor(y) + 0.42;
                wantJump = false;
            }
        }
    }

    private static final class World implements WorldView {
        final Map<String, String> blocks = new HashMap<>();
        final Set<String> surfaces = new HashSet<>();
        boolean sky;

        static String key(int x, int y, int z) {
            return x + "," + y + "," + z;
        }

        String at(int x, int y, int z) {
            return blocks.getOrDefault(key(x, y, z), STONE);
        }

        void set(int x, int y, int z, String id) {
            blocks.put(key(x, y, z), id);
        }

        void addSurface(int x, int y, int z) {
            surfaces.add(key(x, y, z));
        }

        boolean skyAbove(int x, int y, int z) {
            return sky || surfaces.contains(key(x, y, z));
        }

        Goal surfaceGoal() {
            List<Goal> goals = new ArrayList<>();
            for (String value : surfaces) {
                String[] parts = value.split(",");
                goals.add(new Goal.Stand(
                    Integer.parseInt(parts[0]),
                    Integer.parseInt(parts[1]),
                    Integer.parseInt(parts[2])
                ));
            }
            if (goals.isEmpty()) {
                return null;
            }
            return goals.size() == 1 ? goals.getFirst() : new Goal.Composite(goals);
        }

        @Override
        public NodeKind kindAt(int x, int y, int z) {
            String block = at(x, y, z);
            if (block.equals(AIR)) {
                return NodeKind.PASSABLE;
            }
            if (block.equals("minecraft:lava")) {
                return NodeKind.HAZARD;
            }
            if (block.equals("minecraft:water")) {
                return NodeKind.LIQUID;
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

        static Harness create(World world, FakeBody body, int timeout) {
            ActionRegistry registry = new ActionRegistry();
            BotEventStream events = new BotEventStream();
            MutationGate gate = new MutationGate();
            ActionRuntime runtime = new ActionRuntime(new FakePlayerActionOwner(), body, registry, events);
            body.world = world;
            runtime.submit("Bot", "asc-1", "ASCEND", OwnerPriority.RECOVERY, 0);
            List<MutationGate.Proposal> proposals = new ArrayList<>();
            AscendExecutor executor = new AscendExecutor(
                "Bot", "asc-1",
                body, body, body, body,
                new AscendExecutor.BlockReader() {
                    public String blockIdAt(int x, int y, int z) { return world.at(x, y, z); }
                    public boolean skyAbove(int x, int y, int z) { return world.skyAbove(x, y, z); }
                },
                new AscendExecutor.SurfaceAccess() {
                    public boolean isSurfaceStand(int x, int y, int z) {
                        return world.skyAbove(x, y, z)
                            && new Goal.Stand(x, y, z).isSatisfied(world, x, y, z);
                    }
                    public Goal findSurfaceGoal(NavigateExecutor.PositionSource.Position position) {
                        return world.surfaceGoal();
                    }
                    public WorldView world() { return world; }
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

    private static World openPillarStart() {
        World world = enclosedStart();
        for (int y = 60; y <= 63; y++) {
            world.set(0, y, 0, AIR);
        }
        for (int[] direction : new int[][] {{1, 0}, {0, 1}, {-1, 0}, {0, -1}}) {
            world.set(direction[0], 60, direction[1], AIR);
        }
        return world;
    }

    private static World openPillarShaft(int topY) {
        World world = enclosedStart();
        for (int y = 60; y <= topY + 1; y++) {
            world.set(0, y, 0, AIR);
            for (int[] direction : DIRECTIONS) {
                world.set(direction[0], y, direction[1], AIR);
            }
        }
        return world;
    }

    private static final int[][] DIRECTIONS = {
        {1, 0}, {0, 1}, {-1, 0}, {0, -1}
    };

    @Test
    void carvesAndWalksUpARealStairUnderGovernance() {
        World world = enclosedStart();
        FakeBody body = new FakeBody(60);
        Harness h = Harness.create(world, body, 4_000);

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
        Harness h = Harness.create(world, body, 2_000);

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
        body.scaffoldCount = 0;
        Harness h = Harness.create(world, body, 2_000);

        JsonObject terminal = h.run(50, () -> { });

        assertEquals("unsafe", terminal.get("classification").getAsString());
        assertEquals("pillar_no_scaffold_available", terminal.get("reason").getAsString());
        assertEquals(
            "hazard_blocks_stair_route",
            terminal.get("pillar_fallback_from").getAsString()
        );
        assertTrue(h.proposals.isEmpty());
        assertFalse(body.log.contains("exactBreak"));
    }

    @Test
    void pillarsWhenNoStairRouteExistsAndVerifiesWorldHeightAndInventory() {
        World world = openPillarStart();
        FakeBody body = new FakeBody(60);
        body.scaffoldCount = 2;
        Harness h = Harness.create(world, body, 2_000);

        JsonObject terminal = h.run(500, () -> {
            for (var proposal : h.proposals) {
                h.gate.verdict(proposal.proposalId(), true, "natural_terrain");
            }
            if (h.body.y >= 61) {
                h.world.sky = true;
            }
        });

        assertEquals("completed", terminal.get("classification").getAsString(), terminal.toString());
        assertEquals("surface_reached", terminal.get("reason").getAsString());
        assertEquals(1, terminal.get("pillar_steps").getAsInt());
        assertEquals(1, terminal.get("ascend_steps").getAsInt());
        assertEquals(COBBLESTONE, world.at(0, 60, 0));
        assertEquals(61.0, body.y);
        assertEquals(1, body.scaffoldCount);
        assertTrue(body.log.contains("sneak"));
        assertTrue(body.log.contains("jump"));
        assertTrue(body.log.contains("useOnce"));
        assertEquals(1, h.proposals.size());
        MutationGate.Proposal proposal = h.proposals.get(0);
        assertEquals("place", proposal.mutationKind());
        assertEquals("recovery", proposal.context());
        assertEquals(60, proposal.y());
        assertEquals(1, terminal.getAsJsonArray("placed").size());
    }

    @Test
    void movesToNearbySafeColumnBeforePillaringFromHazardousColumn() {
        World world = openPillarStart();
        world.set(0, 60, 0, "minecraft:water");
        world.set(0, 61, 0, "minecraft:water");
        world.set(1, 60, 0, AIR);
        world.set(1, 61, 0, AIR);
        world.set(1, 62, 0, AIR);
        world.set(2, 60, 0, AIR);
        world.set(1, 60, 1, AIR);
        world.set(1, 60, -1, AIR);
        FakeBody body = new FakeBody(60);
        body.scaffoldCount = 2;
        Harness h = Harness.create(world, body, 2_000);

        JsonObject terminal = h.run(1_000, () -> {
            for (var proposal : h.proposals) {
                h.gate.verdict(proposal.proposalId(), true, "natural_terrain");
            }
            if (h.body.y >= 61) {
                h.world.sky = true;
            }
        });

        assertEquals("completed", terminal.get("classification").getAsString(), terminal.toString());
        assertEquals("surface_reached", terminal.get("reason").getAsString());
        assertTrue(terminal.get("recovery_staging_attempted").getAsBoolean());
        assertTrue(terminal.get("recovery_staging_used").getAsBoolean());
        assertTrue(terminal.get("recovery_staging_candidate_count").getAsInt() >= 1);
        assertEquals(1, terminal.get("pillar_steps").getAsInt());
        assertTrue(body.x >= 1.4, "the player must leave the hazardous source column: " + body.x);
        assertEquals("minecraft:water", world.at(0, 60, 0));
        assertEquals("minecraft:water", world.at(0, 61, 0));
        assertEquals(COBBLESTONE, world.at(1, 60, 0));
    }

    @Test
    void brakesAtCenterAndContinuesPillaringAcrossMultipleBlocks() {
        World world = openPillarShaft(64);
        FakeBody body = new FakeBody(60);
        body.x = 0.31;
        body.overshootCentering = true;
        body.scaffoldCount = 4;
        Harness h = Harness.create(world, body, 2_000);

        JsonObject terminal = h.run(1_500, () -> {
            for (var proposal : h.proposals) {
                h.gate.verdict(proposal.proposalId(), true, "natural_terrain");
            }
            if (h.body.y >= 64) {
                h.world.sky = true;
            }
        });

        assertEquals("completed", terminal.get("classification").getAsString(), terminal.toString());
        assertEquals("surface_reached", terminal.get("reason").getAsString());
        assertEquals(4, terminal.get("pillar_steps").getAsInt());
        assertEquals(64.0, body.y);
        assertEquals(0, body.scaffoldCount);
        assertTrue(body.log.contains("stop"), "centering must brake before stability is accepted");
    }

    @Test
    void ordinaryPlanksAreValidEmergencyScaffold() {
        World world = openPillarStart();
        FakeBody body = new FakeBody(60);
        body.scaffoldItem = "minecraft:spruce_planks";
        body.scaffoldCount = 1;
        Harness h = Harness.create(world, body, 2_000);

        JsonObject terminal = h.run(500, () -> {
            for (var proposal : h.proposals) {
                h.gate.verdict(proposal.proposalId(), true, "natural_terrain");
            }
            if (h.body.y >= 61) {
                h.world.sky = true;
            }
        });

        assertEquals("completed", terminal.get("classification").getAsString(), terminal.toString());
        assertEquals("surface_reached", terminal.get("reason").getAsString());
        assertEquals("minecraft:spruce_planks", world.at(0, 60, 0));
        assertEquals(0, body.scaffoldCount);
    }

    @Test
    void deniedPillarPlaceNeverJumpsOrMutates() {
        World world = openPillarStart();
        FakeBody body = new FakeBody(60);
        Harness h = Harness.create(world, body, 2_000);

        JsonObject terminal = h.run(200, () -> {
            for (var proposal : h.proposals) {
                h.gate.verdict(proposal.proposalId(), false, "protected_region");
            }
        });

        assertEquals("unsafe", terminal.get("classification").getAsString());
        assertTrue(terminal.get("reason").getAsString().startsWith("governance_denied"));
        assertEquals(AIR, world.at(0, 60, 0));
        assertEquals(60.0, body.y);
        assertFalse(body.log.contains("jump"));
        assertFalse(body.log.contains("useOnce"));
    }

    @Test
    void pillarWithoutScaffoldFailsBeforeMutationOrMovement() {
        World world = openPillarStart();
        FakeBody body = new FakeBody(60);
        body.scaffoldCount = 0;
        Harness h = Harness.create(world, body, 2_000);

        JsonObject terminal = h.run(20, () -> { });

        assertEquals("failed", terminal.get("classification").getAsString());
        assertEquals("pillar_no_scaffold_available", terminal.get("reason").getAsString());
        assertTrue(h.proposals.isEmpty());
        assertEquals(AIR, world.at(0, 60, 0));
        assertEquals(60.0, body.y);
        assertFalse(body.log.contains("jump"));
    }

    @Test
    void alreadyAtSurfaceCompletesImmediately() {
        World world = enclosedStart();
        world.set(0, 70, 0, AIR);
        world.set(0, 71, 0, AIR);
        world.sky = true;
        FakeBody body = new FakeBody(70);
        Harness h = Harness.create(world, body, 500);

        JsonObject terminal = h.run(10, () -> { });

        assertEquals("completed", terminal.get("classification").getAsString());
        assertEquals(0, terminal.get("ascend_steps").getAsInt());
    }

    @Test
    void visibleSkyWhileSubmergedIsNotASurfaceTerminal() {
        World world = enclosedStart();
        world.set(0, 60, 0, "minecraft:water");
        world.set(0, 61, 0, "minecraft:water");
        world.sky = true;
        FakeBody body = new FakeBody(60);
        body.scaffoldCount = 0;
        Harness h = Harness.create(world, body, 500);

        JsonObject terminal = h.run(1_000, () -> {
            for (var proposal : h.proposals) {
                h.gate.verdict(proposal.proposalId(), false, "submerged_route");
            }
        });

        assertEquals("unsafe", terminal.get("classification").getAsString(), terminal.toString());
        assertFalse("surface_reached".equals(terminal.get("reason").getAsString()));
    }

    @Test
    void walksToAReachableSurfaceExitBeforeConsideringMutation() {
        World world = enclosedStart();
        for (int x = 1; x <= 3; x++) {
            world.set(x, 60, 0, AIR);
            world.set(x, 61, 0, AIR);
        }
        world.addSurface(3, 60, 0);
        FakeBody body = new FakeBody(60);
        body.scaffoldCount = 0;
        Harness h = Harness.create(world, body, 500);

        JsonObject terminal = h.run(400, () -> { });

        assertEquals("completed", terminal.get("classification").getAsString(), terminal.toString());
        assertEquals("surface_reached", terminal.get("reason").getAsString());
        assertTrue(body.x >= 2.8, "the player must physically walk to the exit: " + body.x);
        assertTrue(h.proposals.isEmpty(), "a walkable exit must not mutate terrain");
        assertFalse(body.log.contains("exactBreak"));
    }

    @Test
    void invalidAuthoritativePositionFailsBeforeAnyControlOrMutation() {
        World world = enclosedStart();
        FakeBody body = new FakeBody(60);
        body.x = Double.NaN;
        Harness h = Harness.create(world, body, 500);

        JsonObject terminal = h.run(10, () -> { });

        assertEquals("failed", terminal.get("classification").getAsString());
        assertEquals("body_position_invalid", terminal.get("reason").getAsString());
        assertTrue(h.proposals.isEmpty());
        assertFalse(body.log.contains("jump"));
        assertFalse(body.log.contains("forward"));
    }

    @Test
    void missingVerdictFailsClosed() {
        World world = enclosedStart();
        FakeBody body = new FakeBody(60);
        Harness h = Harness.create(world, body, 2_000);

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
        Harness h = Harness.create(world, body, 2_000);

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
