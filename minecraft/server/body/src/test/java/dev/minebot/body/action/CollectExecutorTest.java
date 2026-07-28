package dev.minebot.body.action;

import com.google.gson.JsonObject;
import dev.minebot.body.control.FakePlayerActionOwner;
import dev.minebot.body.control.OwnerPriority;
import dev.minebot.body.event.BotEventStream;
import dev.minebot.body.nav.FakeWorld;
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
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

final class CollectExecutorTest {
    private static final int FLOOR = 63;
    private static final int STAND = FLOOR + 1;
    private static final String LOG = "minecraft:oak_log";

    /** Fake body: walks toward the look target; records every control call. */
    private static final class FakeBody
        implements MovementControls, ExactBlockBreaker, BotControls {
        double x = 0.5;
        double y = STAND;
        double z = 0.5;
        double lookX;
        double lookZ;
        boolean moving;
        boolean attacking;
        int breakX;
        int breakY;
        int breakZ;
        final List<String> log = new ArrayList<>();
        final List<String> breakTargets = new ArrayList<>();

        @Override
        public void lookAt(String bot, double tx, double ty, double tz) {
            lookX = tx;
            lookZ = tz;
        }

        @Override
        public void moveForward(String bot) {
            moving = true;
            log.add("forward");
        }

        @Override
        public void stopMovement(String bot) {
            moving = false;
            log.add("stop");
        }

        @Override
        public void jumpOnce(String bot) {
            log.add("jump");
        }

        @Override public void jumpContinuous(String bot) { log.add("jumpContinuous"); }

        @Override
        public void sprint(String bot) {
            log.add("sprint");
        }

        @Override
        public Outcome begin(String bot, int x, int y, int z, String blockId, int tick) {
            breakX = x;
            breakY = y;
            breakZ = z;
            attacking = true;
            breakTargets.add(x + "," + y + "," + z + ":" + blockId);
            log.add("exactBreak");
            return Outcome.working();
        }

        @Override public Outcome tick(String bot, int tick) { return Outcome.working(); }
        @Override public void abort(String bot) { attacking = false; }

        @Override
        public void clearAll(String bot) {
            moving = false;
            attacking = false;
            log.add("clearAll");
        }

        void physicsTick() {
            if (!moving) {
                return;
            }
            double dx = lookX - x;
            double dz = lookZ - z;
            double distance = Math.sqrt(dx * dx + dz * dz);
            double step = Math.min(0.25, distance);
            if (distance > 1e-6) {
                x += dx / distance * step;
                z += dz / distance * step;
            }
        }
    }

    private static final class Harness {
        final FakeBody body = new FakeBody();
        final Map<String, String> blockStates = new HashMap<>();
        final Map<String, Integer> itemCounts = new HashMap<>();
        final List<CollectExecutor.Drop> drops = new ArrayList<>();
        final List<MutationGate.Proposal> proposals = new ArrayList<>();
        final MutationGate gate = new MutationGate();
        final ActionRegistry registry = new ActionRegistry();
        final BotEventStream events = new BotEventStream();
        final ActionRuntime runtime = new ActionRuntime(new FakePlayerActionOwner(), body, registry, events);
        int miningTicks;
        boolean dropPresent;

        static String key(int x, int y, int z) {
            return x + "," + y + "," + z;
        }

        CollectExecutor build(WorldView world, List<CollectExecutor.Candidate> candidates, int timeoutTicks) {
            runtime.submit("Bot", "collect-1", "COLLECT_BLOCK", OwnerPriority.ACTION, 0);
            CollectExecutor executor = new CollectExecutor(
                "Bot",
                "collect-1",
                candidates,
                Map.of(LOG, LOG),
                new JsonObject(),
                world,
                body,
                body,
                body,
                (x, y, z) -> blockStates.getOrDefault(key(x, y, z), "minecraft:air"),
                itemId -> itemCounts.getOrDefault(itemId, 0),
                (itemId, x, y, z, radius) -> drops.stream()
                    .filter(drop -> drop.itemId().equals(itemId))
                    .toList(),
                gate,
                proposals::add,
                bot -> new NavigateExecutor.PositionSource.Position(body.x, body.y, body.z),
                (bot, tick, name, actionId, data) -> events.emit(bot, tick, name, actionId, data),
                runtime,
                timeoutTicks
            );
            runtime.attachExecutor("collect-1", executor);
            return executor;
        }

        JsonObject runUntilTerminal(int maxTicks, Runnable perTick) {
            for (int tick = 1; tick <= maxTicks; tick++) {
                runtime.tick(tick);
                body.physicsTick();
                perTick.run();
                ActionRegistry.ActionStatus status = registry.status("collect-1");
                if (status.state() == ActionRegistry.State.TERMINAL) {
                    return status.terminal();
                }
            }
            throw new AssertionError("no terminal within " + maxTicks + " ticks");
        }
    }

    private static FakeWorld worldWithTrunk(int treeX) {
        FakeWorld world = new FakeWorld(FLOOR);
        for (int y = STAND; y <= STAND + 3; y++) {
            world.set(treeX, y, 0, WorldView.NodeKind.SOLID);
        }
        return world;
    }

    @Test
    void governedMineWithPickupTruthCompletes() {
        int treeX = 6;
        Harness harness = new Harness();
        harness.blockStates.put(Harness.key(treeX, STAND, 0), LOG);
        FakeWorld world = worldWithTrunk(treeX);
        double dropX = treeX - 0.25;
        double dropZ = 3.25;
        harness.build(world, List.of(new CollectExecutor.Candidate(treeX, STAND, 0, LOG)), 2_400);

        JsonObject terminal = harness.runUntilTerminal(2_000, () -> {
            // Governance allows as soon as the proposal arrives.
            for (MutationGate.Proposal proposal : harness.proposals) {
                harness.gate.verdict(proposal.proposalId(), true, "natural_terrain");
            }
            // Physics: the block breaks after 20 attack ticks. Inventory only
            // changes once the fake player's body actually reaches the drop.
            if (harness.body.attacking) {
                harness.miningTicks++;
                if (harness.miningTicks == 20) {
                    harness.blockStates.put(Harness.key(treeX, STAND, 0), "minecraft:air");
                    world.set(treeX, STAND, 0, WorldView.NodeKind.PASSABLE);
                    harness.dropPresent = true;
                    harness.drops.add(new CollectExecutor.Drop(
                        "mined-drop", LOG, 1, dropX, STAND, dropZ
                    ));
                }
            }
            if (harness.dropPresent && pickupDistance(harness.body, dropX, dropZ) <= 1.05) {
                harness.itemCounts.put(LOG, 1);
                harness.dropPresent = false;
                harness.drops.clear();
            }
        });

        assertEquals("completed", terminal.get("classification").getAsString());
        assertEquals("collected", terminal.get("reason").getAsString());
        JsonObject delta = terminal.getAsJsonObject("inventory_delta");
        assertEquals(LOG, delta.get("item_id").getAsString());
        assertEquals(0, delta.get("before").getAsInt());
        assertEquals(1, delta.get("after").getAsInt());
        assertEquals(1, harness.proposals.size(), "exactly one proposal for one mine");
        assertEquals(
            List.of(treeX + "," + STAND + ",0:" + LOG),
            harness.body.breakTargets
        );
        assertTrue(
            pickupDistance(harness.body, dropX, dropZ) <= 1.05,
            "pickup must physically reach the drop, x=" + harness.body.x
        );
    }

    @Test
    void navigationSelectsFromTheWholeCandidateDomainInsteadOfListOrder() {
        int listedFirstX = 20;
        int reachableFirstX = 6;
        Harness harness = new Harness();
        harness.blockStates.put(Harness.key(listedFirstX, STAND, 0), LOG);
        harness.blockStates.put(Harness.key(reachableFirstX, STAND, 0), LOG);
        FakeWorld world = worldWithTrunk(listedFirstX);
        for (int y = STAND; y <= STAND + 3; y++) {
            world.set(reachableFirstX, y, 0, WorldView.NodeKind.SOLID);
        }
        harness.build(
            world,
            List.of(
                new CollectExecutor.Candidate(listedFirstX, STAND, 0, LOG),
                new CollectExecutor.Candidate(reachableFirstX, STAND, 0, LOG)
            ),
            600
        );

        for (int tick = 1; tick <= 300 && harness.proposals.isEmpty(); tick++) {
            harness.runtime.tick(tick);
            harness.body.physicsTick();
        }

        assertFalse(harness.proposals.isEmpty(), "the reachable candidate should produce a proposal");
        MutationGate.Proposal proposal = harness.proposals.get(0);
        assertEquals(reachableFirstX, proposal.x());
        assertEquals(STAND, proposal.y());
        assertEquals(0, proposal.z());
    }

    @Test
    void navigationKeepsReachableBlocksFromTheSameVerticalColumn() {
        int treeX = 6;
        int reachableY = STAND;
        int highY = STAND + 4;
        Harness harness = new Harness();
        harness.blockStates.put(Harness.key(treeX, highY, 0), LOG);
        harness.blockStates.put(Harness.key(treeX, reachableY, 0), LOG);
        FakeWorld world = new FakeWorld(FLOOR);
        for (int y = reachableY; y <= highY; y++) {
            world.set(treeX, y, 0, WorldView.NodeKind.SOLID);
        }
        harness.build(
            world,
            List.of(
                new CollectExecutor.Candidate(treeX, highY, 0, LOG),
                new CollectExecutor.Candidate(treeX, reachableY, 0, LOG)
            ),
            600
        );

        for (int tick = 1; tick <= 300 && harness.proposals.isEmpty(); tick++) {
            harness.runtime.tick(tick);
            harness.body.physicsTick();
        }

        assertFalse(harness.proposals.isEmpty(), "the lower log should remain a route candidate");
        MutationGate.Proposal proposal = harness.proposals.get(0);
        assertEquals(treeX, proposal.x());
        assertEquals(reachableY, proposal.y());
        assertEquals(0, proposal.z());
    }

    @Test
    void denialNeverTouchesTheWorld() {
        int treeX = 6;
        Harness harness = new Harness();
        harness.blockStates.put(Harness.key(treeX, STAND, 0), LOG);
        harness.build(worldWithTrunk(treeX), List.of(new CollectExecutor.Candidate(treeX, STAND, 0, LOG)), 2_400);

        JsonObject terminal = harness.runUntilTerminal(2_000, () -> {
            for (MutationGate.Proposal proposal : harness.proposals) {
                harness.gate.verdict(proposal.proposalId(), false, "protected_region");
            }
        });

        assertEquals("failed", terminal.get("classification").getAsString());
        assertEquals("candidate_targets_exhausted", terminal.get("reason").getAsString());
        assertFalse(harness.body.log.contains("exactBreak"), "a denied mutation must never break");
        String failures = terminal.getAsJsonArray("attempt_failures").toString();
        assertTrue(failures.contains("governance_denied:protected_region"), failures);
        assertEquals(LOG, harness.blockStates.get(Harness.key(treeX, STAND, 0)), "the block is untouched");
    }

    @Test
    void missingVerdictFailsClosedWithoutMutation() {
        int treeX = 6;
        Harness harness = new Harness();
        harness.blockStates.put(Harness.key(treeX, STAND, 0), LOG);
        harness.build(worldWithTrunk(treeX), List.of(new CollectExecutor.Candidate(treeX, STAND, 0, LOG)), 2_400);

        JsonObject terminal = harness.runUntilTerminal(2_000, () -> {
        });

        assertEquals("failed", terminal.get("classification").getAsString());
        assertFalse(harness.body.log.contains("exactBreak"), "no verdict means no mutation");
        String failures = terminal.getAsJsonArray("attempt_failures").toString();
        assertTrue(failures.contains("governance_verdict_timeout"), failures);
    }

    @Test
    void missingDropIsPickupNotObservedNeverFakeSuccess() {
        int treeX = 6;
        Harness harness = new Harness();
        harness.blockStates.put(Harness.key(treeX, STAND, 0), LOG);
        FakeWorld world = worldWithTrunk(treeX);
        harness.build(world, List.of(new CollectExecutor.Candidate(treeX, STAND, 0, LOG)), 4_000);

        JsonObject terminal = harness.runUntilTerminal(3_500, () -> {
            for (MutationGate.Proposal proposal : harness.proposals) {
                harness.gate.verdict(proposal.proposalId(), true, "natural_terrain");
            }
            if (harness.body.attacking) {
                harness.miningTicks++;
                if (harness.miningTicks == 20) {
                    harness.blockStates.put(Harness.key(treeX, STAND, 0), "minecraft:air");
                    world.set(treeX, STAND, 0, WorldView.NodeKind.PASSABLE);
                }
            }
            // The drop never arrives in the inventory.
        });

        assertEquals("failed", terminal.get("classification").getAsString());
        String failures = terminal.getAsJsonArray("attempt_failures").toString();
        assertTrue(failures.contains("pickup_not_observed"), failures);
    }

    @Test
    void staleCandidateIsSkippedWithoutProposal() {
        Harness harness = new Harness();
        // The reader reports air where the candidate claims a log.
        harness.build(
            worldWithTrunk(6),
            List.of(new CollectExecutor.Candidate(6, STAND, 0, LOG)),
            1_200
        );

        JsonObject terminal = harness.runUntilTerminal(1_000, () -> {
        });

        assertEquals("failed", terminal.get("classification").getAsString());
        assertTrue(harness.proposals.isEmpty(), "no proposal for a vanished target");
        String failures = terminal.getAsJsonArray("attempt_failures").toString();
        assertTrue(failures.contains("target_changed"), failures);
    }

    @Test
    void emptyCandidateListIsTargetNotFound() {
        Harness harness = new Harness();
        harness.build(new FakeWorld(FLOOR), List.of(), 1_200);

        JsonObject terminal = harness.runUntilTerminal(10, () -> {
        });

        assertEquals("failed", terminal.get("classification").getAsString());
        assertEquals("target_not_found", terminal.get("reason").getAsString());
    }

    private static double pickupDistance(FakeBody body, double dropX, double dropZ) {
        return Math.hypot(body.x - dropX, body.z - dropZ);
    }
}
