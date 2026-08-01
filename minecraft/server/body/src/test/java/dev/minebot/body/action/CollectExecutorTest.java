package dev.minebot.body.action;

import com.google.gson.JsonObject;
import dev.minebot.body.control.FakePlayerActionOwner;
import dev.minebot.body.control.OwnerPriority;
import dev.minebot.body.event.BotEventStream;
import dev.minebot.body.nav.FakeWorld;
import dev.minebot.body.nav.Goal;
import dev.minebot.body.nav.MovementControls;
import dev.minebot.body.nav.NavigateExecutor;
import dev.minebot.body.nav.WorldView;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

final class CollectExecutorTest {
    private static final int FLOOR = 63;
    private static final int STAND = FLOOR + 1;
    private static final String LOG = "minecraft:oak_log";
    private static final String STONE = "minecraft:stone";
    private static final String COBBLESTONE = "minecraft:cobblestone";
    private static final String IRON_ORE = "minecraft:iron_ore";
    private static final String RAW_IRON = "minecraft:raw_iron";

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
        double maxZ = z;
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
                maxZ = Math.max(maxZ, z);
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
            return build(world, candidates, List.of(LOG), timeoutTicks);
        }

        CollectExecutor build(
            WorldView world,
            List<CollectExecutor.Candidate> candidates,
            List<String> expectedItemIds,
            int timeoutTicks
        ) {
            return build(world, candidates, expectedItemIds, timeoutTicks, 1, 8, 8);
        }

        CollectExecutor build(
            WorldView world,
            List<CollectExecutor.Candidate> candidates,
            List<String> expectedItemIds,
            int timeoutTicks,
            int targetCount,
            int candidateBudget,
            int mutationBudget
        ) {
            return build(
                world,
                candidates,
                expectedItemIds,
                timeoutTicks,
                targetCount,
                candidateBudget,
                mutationBudget,
                null
            );
        }

        CollectExecutor build(
            WorldView world,
            List<CollectExecutor.Candidate> candidates,
            List<String> expectedItemIds,
            int timeoutTicks,
            int targetCount,
            int candidateBudget,
            int mutationBudget,
            CollectExecutor.CandidateSource candidateSource
        ) {
            runtime.submit("Bot", "collect-1", "COLLECT_BLOCK", OwnerPriority.ACTION, 0);
            JsonObject searchFacts = new JsonObject();
            searchFacts.addProperty("matches", candidates.size());
            searchFacts.addProperty("coverage_complete", true);
            searchFacts.addProperty("unloaded_chunk_count", 0);
            searchFacts.addProperty("result_capped", false);
            CollectExecutor executor = new CollectExecutor(
                "Bot",
                "collect-1",
                candidates,
                expectedItemIds,
                searchFacts,
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
                timeoutTicks,
                targetCount,
                candidateBudget,
                mutationBudget,
                candidateSource
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

    private static FakeWorld sealedTunnelWorld(Harness harness, int targetX) {
        FakeWorld world = new FakeWorld(FLOOR);
        for (int x = -1; x <= targetX + 1; x++) {
            for (int y = STAND; y <= STAND + 2; y++) {
                for (int z = -1; z <= 1; z++) {
                    world.set(x, y, z, WorldView.NodeKind.SOLID);
                    harness.blockStates.put(Harness.key(x, y, z), "minecraft:stone");
                }
            }
        }
        world.set(0, STAND, 0, WorldView.NodeKind.PASSABLE);
        world.set(0, STAND + 1, 0, WorldView.NodeKind.PASSABLE);
        harness.blockStates.put(Harness.key(0, STAND, 0), "minecraft:air");
        harness.blockStates.put(Harness.key(0, STAND + 1, 0), "minecraft:air");
        world.set(targetX, STAND, 0, WorldView.NodeKind.SOLID);
        harness.blockStates.put(Harness.key(targetX, STAND, 0), LOG);
        for (int x = -8; x <= targetX + 8; x++) {
            for (int y = STAND - 4; y <= STAND + 5; y++) {
                for (int z = -8; z <= 8; z++) {
                    boolean intendedTunnel = z == 0 && y == STAND && x >= 0 && x < targetX;
                    if (!intendedTunnel) {
                        world.hazardBodyPosition(x, y, z);
                    }
                }
            }
        }
        return world;
    }

    @Test
    void governedMineWithPickupTruthCompletes() {
        int treeX = 6;
        Harness harness = new Harness();
        harness.blockStates.put(Harness.key(treeX, STAND, 0), IRON_ORE);
        FakeWorld world = worldWithTrunk(treeX);
        double dropX = treeX - 0.25;
        double dropZ = 3.25;
        harness.build(
            world,
            List.of(new CollectExecutor.Candidate(treeX, STAND, 0, IRON_ORE)),
            List.of(RAW_IRON),
            2_400
        );

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
                        "mined-drop", RAW_IRON, 1, dropX, STAND, dropZ
                    ));
                }
            }
            if (harness.dropPresent && insidePlayerTouchVolume(harness.body, dropX, STAND, dropZ)) {
                harness.itemCounts.put(RAW_IRON, 1);
                harness.dropPresent = false;
                harness.drops.clear();
            }
        });

        assertEquals(
            "completed",
            terminal.get("classification").getAsString(),
            terminal + ", body=" + harness.body.x + "," + harness.body.y + "," + harness.body.z
        );
        assertEquals("collected", terminal.get("reason").getAsString());
        JsonObject delta = terminal.getAsJsonObject("inventory_delta");
        assertEquals(RAW_IRON, delta.get("item_id").getAsString());
        assertEquals(0, delta.get("before").getAsInt());
        assertEquals(1, delta.get("after").getAsInt());
        assertEquals(1, harness.proposals.size(), "exactly one proposal for one mine");
        assertEquals(
            List.of(treeX + "," + STAND + ",0:" + IRON_ORE),
            harness.body.breakTargets
        );
        assertTrue(
            insidePlayerTouchVolume(harness.body, dropX, STAND, dropZ),
            "pickup must physically reach the drop, x=" + harness.body.x
        );
    }

    @Test
    void oneQuantityObjectiveMinesNearbyBlocksBeforeBatchPickup() {
        Harness harness = new Harness();
        FakeWorld world = new FakeWorld(FLOOR);
        List<CollectExecutor.Candidate> candidates = new ArrayList<>();
        for (int x : List.of(4, 6, 8)) {
            harness.blockStates.put(Harness.key(x, STAND, 0), LOG);
            world.set(x, STAND, 0, WorldView.NodeKind.SOLID);
            candidates.add(new CollectExecutor.Candidate(x, STAND, 0, LOG));
        }
        harness.build(world, candidates, List.of(LOG), 2_400, 3, 8, 8);
        Set<String> changed = new HashSet<>();
        double dropX = 0.5;
        double dropY = STAND;
        double dropZ = 4.5;
        int[] breaksBeforePickup = {-1};

        JsonObject terminal = harness.runUntilTerminal(2_000, () -> {
            for (MutationGate.Proposal proposal : harness.proposals) {
                harness.gate.verdict(proposal.proposalId(), true, "natural_terrain");
            }
            if (harness.body.attacking) {
                String key = Harness.key(harness.body.breakX, harness.body.breakY, harness.body.breakZ);
                if (changed.add(key)) {
                    harness.blockStates.put(key, "minecraft:air");
                    world.set(
                        harness.body.breakX,
                        harness.body.breakY,
                        harness.body.breakZ,
                        WorldView.NodeKind.PASSABLE
                    );
                    harness.drops.add(new CollectExecutor.Drop(
                        "batch-drop-" + changed.size(), LOG, 1, dropX, dropY, dropZ
                    ));
                }
            }
            if (!harness.drops.isEmpty()
                && insidePlayerTouchVolume(harness.body, dropX, dropY, dropZ)) {
                breaksBeforePickup[0] = changed.size();
                harness.itemCounts.put(LOG, harness.drops.size());
                harness.drops.clear();
            }
        });

        assertEquals("completed", terminal.get("classification").getAsString(), terminal.toString());
        assertEquals("collected", terminal.get("reason").getAsString());
        assertEquals(3, terminal.get("target_breaks").getAsInt());
        assertEquals(3, terminal.get("collected_count").getAsInt());
        assertEquals(3, breaksBeforePickup[0], "pickup should be batched after the nearby mining run");
        assertEquals(3, harness.body.breakTargets.size());
        assertEquals(3, harness.proposals.size());
    }

    @Test
    void vanishedPickupGoalDoesNotRetireLiveCandidatesFromTheCompositeDomain() {
        Harness harness = new Harness();
        FakeWorld world = new FakeWorld(FLOOR);
        List<CollectExecutor.Candidate> candidates = new ArrayList<>();
        for (int x : List.of(-40, -44)) {
            harness.blockStates.put(Harness.key(x, STAND, 0), IRON_ORE);
            world.set(x, STAND, 0, WorldView.NodeKind.SOLID);
            candidates.add(new CollectExecutor.Candidate(x, STAND, 0, IRON_ORE));
        }
        harness.drops.add(new CollectExecutor.Drop(
            "vanishing-drop", RAW_IRON, 1, 25.5, STAND, 0.5
        ));
        harness.build(
            world,
            candidates,
            List.of(RAW_IRON),
            4_000,
            2,
            8,
            8
        );
        Set<String> changed = new HashSet<>();
        int[] ticks = {0};

        JsonObject terminal = harness.runUntilTerminal(3_500, () -> {
            ticks[0]++;
            for (MutationGate.Proposal proposal : harness.proposals) {
                harness.gate.verdict(proposal.proposalId(), true, "natural_terrain");
            }
            if (ticks[0] == 5) {
                harness.drops.clear();
            }
            if (!harness.body.attacking) {
                return;
            }
            String key = Harness.key(
                harness.body.breakX, harness.body.breakY, harness.body.breakZ
            );
            if (!changed.add(key)) {
                return;
            }
            harness.blockStates.put(key, "minecraft:air");
            world.set(
                harness.body.breakX,
                harness.body.breakY,
                harness.body.breakZ,
                WorldView.NodeKind.PASSABLE
            );
            harness.itemCounts.put(RAW_IRON, changed.size());
        });

        assertEquals("completed", terminal.get("classification").getAsString(), terminal.toString());
        assertEquals(2, terminal.get("target_breaks").getAsInt(), terminal.toString());
        assertEquals(2, terminal.get("collected_count").getAsInt(), terminal.toString());
        assertEquals(2, harness.proposals.size(), terminal.toString());
        assertEquals(2, harness.body.breakTargets.size(), terminal.toString());
        assertTrue(
            terminal.getAsJsonArray("drop_losses").toString()
                .contains("drop_no_longer_observed"),
            terminal.toString()
        );
        assertFalse(
            terminal.getAsJsonArray("attempt_failures").toString()
                .contains("reached_stand_candidate_mismatch"),
            terminal.toString()
        );
        assertEquals("minecraft:air", harness.blockStates.get(Harness.key(-40, STAND, 0)));
        assertEquals("minecraft:air", harness.blockStates.get(Harness.key(-44, STAND, 0)));
    }

    @Test
    void persistentQuantityProcessRescansTheSameWayForWoodStoneAndIron() {
        record Material(String blockId, String itemId) {
        }
        for (Material material : List.of(
            new Material(LOG, LOG),
            new Material(STONE, COBBLESTONE),
            new Material(IRON_ORE, RAW_IRON)
        )) {
            Harness harness = new Harness();
            FakeWorld world = new FakeWorld(FLOOR);
            int targetX = -12;
            harness.blockStates.put(Harness.key(targetX, STAND, 0), material.blockId());
            world.set(targetX, STAND, 0, WorldView.NodeKind.SOLID);
            CollectExecutor.CandidateSource source = (x, _y, _z, _tick) -> {
                List<CollectExecutor.Candidate> discovered = new ArrayList<>();
                if (x < 0 && material.blockId().equals(
                    harness.blockStates.get(Harness.key(targetX, STAND, 0))
                )) {
                    discovered.add(new CollectExecutor.Candidate(targetX, STAND, 0, material.blockId()));
                }
                return new CollectExecutor.CandidateScan(discovered, true, 0, false);
            };
            harness.build(
                world,
                List.of(),
                List.of(material.itemId()),
                2_400,
                1,
                8,
                8,
                source
            );
            Set<String> changed = new HashSet<>();

            JsonObject terminal = harness.runUntilTerminal(2_000, () -> {
                for (MutationGate.Proposal proposal : harness.proposals) {
                    harness.gate.verdict(proposal.proposalId(), true, "natural_terrain");
                }
                if (!harness.body.attacking) {
                    return;
                }
                String key = Harness.key(
                    harness.body.breakX, harness.body.breakY, harness.body.breakZ
                );
                if (!changed.add(key)) {
                    return;
                }
                harness.blockStates.put(key, "minecraft:air");
                world.set(
                    harness.body.breakX,
                    harness.body.breakY,
                    harness.body.breakZ,
                    WorldView.NodeKind.PASSABLE
                );
                harness.itemCounts.put(material.itemId(), changed.size());
            });

            assertEquals("completed", terminal.get("classification").getAsString(), terminal.toString());
            assertEquals(1, terminal.get("target_breaks").getAsInt(), terminal.toString());
            assertEquals(1, terminal.get("collected_count").getAsInt(), terminal.toString());
            assertEquals(1, harness.proposals.size(), terminal.toString());
            JsonObject search = terminal.getAsJsonObject("search");
            assertEquals(1, search.get("discovered_after_start").getAsInt(), search.toString());
            assertTrue(search.get("scans").getAsInt() >= 2, search.toString());
            assertTrue(search.get("frontiers_reached").getAsInt() >= 1, search.toString());
            assertTrue(harness.body.x < 0, "the same action must move before its first candidate appears");
        }
    }

    @Test
    void unavailableFrontiersDoNotRescanAnUnchangedPosition() {
        Harness harness = new Harness();
        int[] scans = {0};
        CollectExecutor.CandidateSource source = (_x, _y, _z, _tick) -> {
            scans[0]++;
            return new CollectExecutor.CandidateScan(List.of(), true, 0, false);
        };
        harness.build(
            (_x, _y, _z) -> WorldView.NodeKind.HAZARD,
            List.of(),
            List.of(LOG),
            1_200,
            1,
            2,
            1,
            source
        );

        JsonObject terminal = harness.runUntilTerminal(20, () -> {
        });

        assertEquals("failed", terminal.get("classification").getAsString(), terminal.toString());
        assertEquals("target_not_found", terminal.get("reason").getAsString(), terminal.toString());
        assertEquals(0, scans[0], "no movement means the authoritative scan center did not change");
        JsonObject search = terminal.getAsJsonObject("search");
        assertEquals(1, search.get("scans").getAsInt(), search.toString());
        assertEquals(2, search.get("frontier_attempts").getAsInt(), search.toString());
        assertEquals(0, search.get("frontiers_reached").getAsInt(), search.toString());
    }

    @Test
    void objectiveBlacklistPreventsARescanFromReaddingADeniedCandidate() {
        int treeX = -12;
        Harness harness = new Harness();
        harness.blockStates.put(Harness.key(treeX, STAND, 0), LOG);
        CollectExecutor.Candidate candidate = new CollectExecutor.Candidate(treeX, STAND, 0, LOG);
        CollectExecutor.CandidateSource source = (x, _y, _z, _tick) ->
            new CollectExecutor.CandidateScan(x < 0 ? List.of(candidate) : List.of(), true, 0, false);
        harness.build(
            worldWithTrunk(treeX),
            List.of(),
            List.of(LOG),
            2_400,
            1,
            1,
            1,
            source
        );

        JsonObject terminal = harness.runUntilTerminal(2_000, () -> {
            for (MutationGate.Proposal proposal : harness.proposals) {
                harness.gate.verdict(proposal.proposalId(), false, "protected_region");
            }
        });

        assertEquals("failed", terminal.get("classification").getAsString(), terminal.toString());
        assertEquals("governance_denied:protected_region", terminal.get("reason").getAsString());
        assertEquals(1, harness.proposals.size(), "a denied coordinate must stay blacklisted");
        assertFalse(harness.body.log.contains("exactBreak"));
        assertTrue(terminal.getAsJsonObject("search").get("frontiers_reached").getAsInt() >= 1);
    }

    @Test
    void unsafeDropIsNotChasedAndTheObjectiveChoosesAnotherResource() {
        Harness harness = new Harness();
        FakeWorld world = new FakeWorld(FLOOR);
        for (int x : List.of(4, 8)) {
            harness.blockStates.put(Harness.key(x, STAND, 0), LOG);
            world.set(x, STAND, 0, WorldView.NodeKind.SOLID);
        }
        for (int x = 2; x <= 10; x++) {
            for (int y = STAND - 2; y <= STAND + 1; y++) {
                for (int z = 2; z <= 6; z++) {
                    world.hazardBodyPosition(x, y, z);
                }
            }
        }
        harness.build(
            world,
            List.of(
                new CollectExecutor.Candidate(4, STAND, 0, LOG),
                new CollectExecutor.Candidate(8, STAND, 0, LOG)
            ),
            List.of(LOG),
            2_400,
            1,
            8,
            8
        );
        Set<String> changed = new HashSet<>();

        JsonObject terminal = harness.runUntilTerminal(2_000, () -> {
            for (MutationGate.Proposal proposal : harness.proposals) {
                harness.gate.verdict(proposal.proposalId(), true, "natural_terrain");
            }
            if (!harness.body.attacking) {
                return;
            }
            String key = Harness.key(harness.body.breakX, harness.body.breakY, harness.body.breakZ);
            if (!changed.add(key)) {
                return;
            }
            harness.blockStates.put(key, "minecraft:air");
            world.set(
                harness.body.breakX,
                harness.body.breakY,
                harness.body.breakZ,
                WorldView.NodeKind.PASSABLE
            );
            if (changed.size() == 1) {
                harness.drops.add(new CollectExecutor.Drop(
                    "unsafe-drop", LOG, 1, harness.body.breakX + 0.5, STAND, 4.5
                ));
            } else {
                harness.itemCounts.put(LOG, 1);
            }
        });

        assertEquals("completed", terminal.get("classification").getAsString(), terminal.toString());
        assertEquals(2, terminal.get("target_breaks").getAsInt(), terminal.toString());
        assertTrue(terminal.getAsJsonArray("drop_losses").toString().contains("unsafe_pickup_domain"));
        assertTrue(harness.body.maxZ < 2.0, "the player must not walk toward the hazardous drop");
        assertEquals(2, harness.body.breakTargets.size());
    }

    @Test
    void usefulInventoryGainBelowTheQuantityGoalReturnsPartial() {
        Harness harness = new Harness();
        FakeWorld world = new FakeWorld(FLOOR);
        List<CollectExecutor.Candidate> candidates = new ArrayList<>();
        for (int x : List.of(4, 6)) {
            harness.blockStates.put(Harness.key(x, STAND, 0), LOG);
            world.set(x, STAND, 0, WorldView.NodeKind.SOLID);
            candidates.add(new CollectExecutor.Candidate(x, STAND, 0, LOG));
        }
        harness.build(world, candidates, List.of(LOG), 2_400, 3, 8, 8);
        Set<String> changed = new HashSet<>();

        JsonObject terminal = harness.runUntilTerminal(1_000, () -> {
            for (MutationGate.Proposal proposal : harness.proposals) {
                harness.gate.verdict(proposal.proposalId(), true, "natural_terrain");
            }
            if (!harness.body.attacking) {
                return;
            }
            String key = Harness.key(harness.body.breakX, harness.body.breakY, harness.body.breakZ);
            if (!changed.add(key)) {
                return;
            }
            harness.blockStates.put(key, "minecraft:air");
            world.set(
                harness.body.breakX,
                harness.body.breakY,
                harness.body.breakZ,
                WorldView.NodeKind.PASSABLE
            );
            harness.itemCounts.put(LOG, changed.size());
        });

        assertEquals("partial", terminal.get("classification").getAsString(), terminal.toString());
        assertEquals("partial", terminal.get("reason").getAsString());
        assertEquals(2, terminal.get("collected_count").getAsInt());
        assertEquals(1, terminal.get("remaining_count").getAsInt());
        assertEquals(2, terminal.get("target_breaks").getAsInt());
    }

    @Test
    void governedExcavationBuildsAPlayerSizedTunnelAndCollectsTheBuriedTarget() {
        int targetX = 4;
        Harness harness = new Harness();
        FakeWorld world = sealedTunnelWorld(harness, targetX);
        harness.build(
            world,
            List.of(new CollectExecutor.Candidate(targetX, STAND, 0, LOG)),
            2_400
        );
        Set<String> changed = new HashSet<>();
        double dropX = targetX + 0.25;
        double dropZ = 0.5;

        JsonObject terminal = harness.runUntilTerminal(2_000, () -> {
            for (MutationGate.Proposal proposal : harness.proposals) {
                harness.gate.verdict(proposal.proposalId(), true, "natural_terrain");
            }
            if (harness.body.attacking) {
                String key = Harness.key(
                    harness.body.breakX, harness.body.breakY, harness.body.breakZ
                );
                if (changed.add(key)) {
                    harness.blockStates.put(key, "minecraft:air");
                    world.set(
                        harness.body.breakX,
                        harness.body.breakY,
                        harness.body.breakZ,
                        WorldView.NodeKind.PASSABLE
                    );
                    if (harness.body.breakX == targetX
                        && harness.body.breakY == STAND
                        && harness.body.breakZ == 0) {
                        harness.dropPresent = true;
                        harness.drops.add(new CollectExecutor.Drop(
                            "buried-drop", LOG, 1, dropX, STAND, dropZ
                        ));
                    }
                }
            }
            if (harness.dropPresent
                && insidePlayerTouchVolume(harness.body, dropX, STAND, dropZ)) {
                harness.itemCounts.put(LOG, 1);
                harness.dropPresent = false;
                harness.drops.clear();
            }
        });

        assertEquals("completed", terminal.get("classification").getAsString(), terminal.toString());
        assertEquals("collected", terminal.get("reason").getAsString());
        assertTrue(terminal.get("approach_excavation_attempted").getAsBoolean());
        assertTrue(terminal.get("approach_excavation_used").getAsBoolean());
        assertEquals(6, terminal.get("approach_excavation_breaks").getAsInt());
        assertEquals(6, terminal.getAsJsonArray("approach_broken").size());
        assertEquals(7, harness.proposals.size(), "six corridor blocks plus the target");
        assertTrue(harness.proposals.subList(0, 6).stream().allMatch(
            proposal -> proposal.context().equals("collect_approach")
        ));
        assertEquals("collect", harness.proposals.getLast().context());
        assertEquals("minecraft:air", harness.blockStates.get(Harness.key(targetX, STAND, 0)));
        assertEquals(1, harness.itemCounts.get(LOG));
        assertTrue(harness.body.x >= 3.35, "player must physically enter the opened tunnel");
    }

    @Test
    void deniedExcavationBlockLeavesTheTunnelAndTargetUntouched() {
        int targetX = 4;
        Harness harness = new Harness();
        FakeWorld world = sealedTunnelWorld(harness, targetX);
        harness.build(
            world,
            List.of(new CollectExecutor.Candidate(targetX, STAND, 0, LOG)),
            2_400
        );

        JsonObject terminal = harness.runUntilTerminal(1_000, () -> {
            for (MutationGate.Proposal proposal : harness.proposals) {
                harness.gate.verdict(proposal.proposalId(), false, "player_structure");
            }
        });

        assertEquals("failed", terminal.get("classification").getAsString());
        assertTrue(terminal.get("reason").getAsString().contains("governance_denied:player_structure"));
        assertTrue(harness.body.breakTargets.isEmpty(), "denial must happen before physical mining");
        assertEquals(LOG, harness.blockStates.get(Harness.key(targetX, STAND, 0)));
        assertEquals("minecraft:stone", harness.blockStates.get(Harness.key(1, STAND, 0)));
        assertEquals(1, harness.proposals.size());
        assertEquals("collect_approach", harness.proposals.getFirst().context());
    }

    @Test
    void pickupUsesThePlayersTouchVolumeInsteadOfTheDropsFeetLevel() {
        int treeX = 6;
        int targetY = STAND + 1;
        double dropX = treeX + 0.5;
        double dropY = targetY;
        double dropZ = 0.5;
        Harness harness = new Harness();
        harness.body.y = STAND - 1;
        harness.blockStates.put(Harness.key(treeX, targetY, 0), LOG);
        FakeWorld world = new FakeWorld(FLOOR - 1);
        world.set(treeX, targetY - 1, 0, WorldView.NodeKind.SOLID);
        world.set(treeX, targetY, 0, WorldView.NodeKind.SOLID);
        harness.build(
            world,
            List.of(new CollectExecutor.Candidate(treeX, targetY, 0, LOG)),
            2_400
        );

        JsonObject terminal = harness.runUntilTerminal(2_000, () -> {
            for (MutationGate.Proposal proposal : harness.proposals) {
                harness.gate.verdict(proposal.proposalId(), true, "natural_terrain");
            }
            if (harness.body.attacking) {
                harness.miningTicks++;
                if (harness.miningTicks == 20) {
                    harness.blockStates.put(Harness.key(treeX, targetY, 0), "minecraft:air");
                    world.set(treeX, targetY, 0, WorldView.NodeKind.PASSABLE);
                    harness.dropPresent = true;
                    harness.drops.add(new CollectExecutor.Drop(
                        "raised-drop", LOG, 1, dropX, dropY, dropZ
                    ));
                }
            }
            if (harness.dropPresent && insidePlayerTouchVolume(harness.body, dropX, dropY, dropZ)) {
                harness.itemCounts.put(LOG, 1);
                harness.dropPresent = false;
                harness.drops.clear();
            }
        });

        assertEquals("completed", terminal.get("classification").getAsString());
        assertEquals("collected", terminal.get("reason").getAsString());
        assertEquals(1, harness.proposals.size(), "one requested item must require one mined block");
        assertEquals(
            List.of(treeX + "," + targetY + ",0:" + LOG),
            harness.body.breakTargets
        );
    }

    @Test
    void pickupTracksADropWhileItIsStillMoving() {
        int treeX = 6;
        double dropX = treeX - 0.25;
        double[] dropZ = {3.25};
        Harness harness = new Harness();
        harness.blockStates.put(Harness.key(treeX, STAND, 0), LOG);
        FakeWorld world = worldWithTrunk(treeX);
        harness.build(
            world,
            List.of(new CollectExecutor.Candidate(treeX, STAND, 0, LOG)),
            2_400
        );

        JsonObject terminal = harness.runUntilTerminal(2_000, () -> {
            for (MutationGate.Proposal proposal : harness.proposals) {
                harness.gate.verdict(proposal.proposalId(), true, "natural_terrain");
            }
            if (harness.body.attacking) {
                harness.miningTicks++;
                if (harness.miningTicks == 20) {
                    harness.blockStates.put(Harness.key(treeX, STAND, 0), "minecraft:air");
                    world.set(treeX, STAND, 0, WorldView.NodeKind.PASSABLE);
                    harness.dropPresent = true;
                }
            }
            if (harness.dropPresent) {
                dropZ[0] += 0.08;
                harness.drops.clear();
                harness.drops.add(new CollectExecutor.Drop(
                    "moving-drop", LOG, 1, dropX, STAND, dropZ[0]
                ));
                if (insidePlayerTouchVolume(harness.body, dropX, STAND, dropZ[0])) {
                    harness.itemCounts.put(LOG, 1);
                    harness.dropPresent = false;
                    harness.drops.clear();
                }
            }
        });

        assertEquals("completed", terminal.get("classification").getAsString(), terminal.toString());
        assertEquals("collected", terminal.get("reason").getAsString());
        assertEquals(1, harness.proposals.size(), "a moving drop must not trigger another mine");
        assertTrue(terminal.getAsJsonArray("attempt_failures").isEmpty(), terminal.toString());
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
    void navigationSkipsAnOccludedCandidateForAVisibleCandidate() {
        int occludedX = 3;
        int occludedY = STAND + 1;
        int occludedZ = 3;
        int visibleX = 8;
        Harness harness = new Harness();
        harness.blockStates.put(Harness.key(occludedX, occludedY, occludedZ), LOG);
        harness.blockStates.put(Harness.key(visibleX, STAND, 0), LOG);

        FakeWorld world = worldWithTrunk(visibleX);
        world.set(occludedX, occludedY, occludedZ, WorldView.NodeKind.SOLID);
        for (int x = occludedX - 1; x <= occludedX + 1; x++) {
            for (int y = occludedY - 1; y <= occludedY + 1; y++) {
                for (int z = occludedZ - 1; z <= occludedZ + 1; z++) {
                    if (x != occludedX || y != occludedY || z != occludedZ) {
                        world.set(x, y, z, WorldView.NodeKind.SOLID);
                    }
                }
            }
        }
        harness.build(
            world,
            List.of(
                new CollectExecutor.Candidate(occludedX, occludedY, occludedZ, LOG),
                new CollectExecutor.Candidate(visibleX, STAND, 0, LOG)
            ),
            1_200
        );

        for (int tick = 1; tick <= 600 && harness.proposals.isEmpty(); tick++) {
            harness.runtime.tick(tick);
            harness.body.physicsTick();
        }

        assertFalse(harness.proposals.isEmpty(), "the visible candidate should produce a proposal");
        MutationGate.Proposal proposal = harness.proposals.get(0);
        assertEquals(visibleX, proposal.x());
        assertEquals(STAND, proposal.y());
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
        assertEquals("governance_denied:protected_region", terminal.get("reason").getAsString());
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
        assertTrue(failures.contains("drop_not_observed_after_break"), failures);
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

    private static boolean insidePlayerTouchVolume(
        FakeBody body,
        double dropX,
        double dropY,
        double dropZ
    ) {
        return Math.abs(body.x - dropX) <= Goal.Pickup.HORIZONTAL_REACH
            && Math.abs(body.z - dropZ) <= Goal.Pickup.HORIZONTAL_REACH
            && dropY >= body.y - Goal.Pickup.BELOW_FEET_REACH
            && dropY <= body.y + Goal.Pickup.ABOVE_FEET_REACH;
    }
}
