package dev.minebot.body.nav;

import net.minecraft.SharedConstants;
import net.minecraft.core.BlockPos;
import net.minecraft.core.Direction;
import net.minecraft.server.Bootstrap;
import net.minecraft.world.level.EmptyBlockGetter;
import net.minecraft.world.level.block.Blocks;
import net.minecraft.world.level.block.state.BlockState;
import net.minecraft.world.level.block.state.properties.BlockStateProperties;
import net.minecraft.world.level.block.state.properties.SlabType;
import net.minecraft.world.phys.AABB;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/** Native collision measurements, with diagnostic comparisons to the current graph. */
final class NavigationGeometryProbeTest {
    private record Surface(String name, BlockState block, double height) {}

    @BeforeAll
    static void bootstrapMinecraft() {
        SharedConstants.tryDetectVersion();
        Bootstrap.bootStrap();
    }

    @Test
    void worldFactsKeepVoxelIdentityAndOptionalPhysicalStandingTruthSeparate() {
        var world = MinecraftWorldViewTest.nativeWorld(Map.of(
            new BlockPos(0, 63, 0), Blocks.STONE_SLAB.defaultBlockState()
        ));
        var fact = new dev.minebot.body.perception.BlockSnapshot.Fact(
            0, 63, 0, "minecraft:stone_slab", "SOLID", Map.of("type", "bottom"));
        var encoded = dev.minebot.body.perception.WorldReadService.factJson(fact, world);
        assertEquals(63, encoded.get("y").getAsInt());
        assertEquals("SOLID", encoded.get("state").getAsString());
        assertEquals(63.5, encoded.get("standing_y").getAsDouble());
        assertFalse(dev.minebot.body.perception.WorldReadService.factJson(fact).has("standing_y"));
        var unsupported = new dev.minebot.body.perception.BlockSnapshot.Fact(
            0, 64, 0, "minecraft:air", "CLEAR", Map.of());
        assertTrue(dev.minebot.body.perception.WorldReadService.factJson(unsupported, world)
            .get("standing_y").isJsonNull());
    }

    @Test
    void lowSurfaceRoutesAndReturnEdgesUseThePhysicalStandingPose() {
        for (var block : List.of(Blocks.STONE_SLAB, Blocks.WHITE_CARPET, Blocks.OAK_TRAPDOOR)) {
            var world = MinecraftWorldViewTest.nativeWorld(Map.of(
                new BlockPos(0, 63, 0), Blocks.STONE.defaultBlockState(),
                new BlockPos(1, 63, 0), block.defaultBlockState()
            ));
            double height = world.standingHeight(1, 63, 0);
            assertTrue(Double.isFinite(height));
            var moves = new MoveGenerator(world);
            var edge = moves.movesFrom(0, 64, 0).moves().stream()
                .filter(move -> move.x() == 1 && move.z() == 0).findFirst().orElseThrow();
            assertEquals(63, edge.y(), block.toString());
            assertTrue(moves.hasDirectReturnMove(0, 64, 0, edge), block.toString());
            var search = new AStarPathfinder(world, new Goal.Stand(1, 63, 0), 0, 64, 0);
            var route = search.step(100);
            assertEquals(AStarPathfinder.Outcome.COMPLETE, route.outcome(), block.toString());
            assertEquals(height, route.path().getLast().feetY(), 1.0e-7);
            var follower = new PathFollower(route.path());
            follower.tick(0.5, 64, 0.5);
            assertEquals(PathFollower.State.ARRIVED, follower.tick(1.5, height, 0.5).state());
        }
    }

    @Test
    void lowCeilingRouteRetainsNativeGeometryThroughTheRadiusLimitedWorld() {
        for (var block : List.of(Blocks.WHITE_CARPET, Blocks.STONE_SLAB)) {
            var world = MinecraftWorldViewTest.nativeWorld(Map.of(
                new BlockPos(0, 63, 0), block.defaultBlockState(),
                new BlockPos(1, 63, 0), block.defaultBlockState(),
                new BlockPos(0, 65, 0), Blocks.STONE.defaultBlockState(),
                new BlockPos(1, 65, 0), Blocks.STONE.defaultBlockState()
            ));
            var search = new AStarPathfinder(world, new Goal.Stand(1, 63, 0), 0, 63, 0,
                100, 50, true, false, 1, MoveGenerator.Policy.defaults());
            var result = search.step(100);
            assertEquals(block == Blocks.WHITE_CARPET ? AStarPathfinder.Outcome.COMPLETE
                : AStarPathfinder.Outcome.NO_PATH, result.outcome(), block.toString());
            if (result.outcome() == AStarPathfinder.Outcome.COMPLETE) {
                assertEquals(63.0625, result.path().getLast().feetY(), 1.0e-7);
            }
        }
    }

    @Test
    void physicalRiseAndFallLimitsCannotBeBypassedByVoxelRounding() {
        var tooHigh = MinecraftWorldViewTest.nativeWorld(Map.of(
            new BlockPos(0, 63, 0), Blocks.STONE.defaultBlockState(),
            new BlockPos(1, 64, 0), Blocks.OAK_FENCE.defaultBlockState()
        ));
        assertEquals(65.5, tooHigh.standingHeight(1, 65, 0));
        assertFalse(new MoveGenerator(tooHigh).movesFrom(0, 64, 0).moves().stream()
            .anyMatch(move -> move.x() == 1 && move.z() == 0));
        var drop = MinecraftWorldViewTest.nativeWorld(Map.of(
            new BlockPos(0, 63, 0), Blocks.STONE_SLAB.defaultBlockState(),
            new BlockPos(1, 61, 0), Blocks.STONE.defaultBlockState()
        ));
        for (int maxFall : List.of(1, 2)) {
            var policy = new MoveGenerator.Policy(true, true, true, true, maxFall, 20);
            assertEquals(maxFall == 2, new MoveGenerator(drop, policy).movesFrom(0, 63, 0).moves().stream()
                .anyMatch(move -> move.x() == 1 && move.y() == 62 && move.z() == 0));
        }
    }

    @Test
    void descendingKeepsWalkingUntilTheBodyClearsTheUpperSupport() {
        for (var surface : List.of(Blocks.STONE_SLAB, Blocks.WHITE_CARPET, Blocks.OAK_TRAPDOOR)) {
            var world = MinecraftWorldViewTest.nativeWorld(Map.of(
                new BlockPos(0, 63, 0), Blocks.STONE.defaultBlockState(),
                new BlockPos(1, 63, 0), surface.defaultBlockState(),
                new BlockPos(2, 63, 0), surface.defaultBlockState()
            ));
            double landingY = world.standingHeight(1, 63, 0);
            var route = new AStarPathfinder(world, new Goal.Stand(2, 63, 0), 0, 64, 0).step(100);
            assertEquals(AStarPathfinder.Outcome.COMPLETE, route.outcome());
            var follower = new PathFollower(route.path());
            assertTrue(follower.tick(0.5, 64, 0.5).forward());
            for (double x : new double[] {0.85, 1.05, 1.25}) {
                assertTrue(world.isStableDryPosition(x, 64, 0.5), "body still overlaps upper support");
                assertFalse(world.isStableDryPosition(x, landingY, 0.5), "upper support blocks descent");
                assertTrue(follower.tick(x, 64, 0.5).forward(),
                    surface + " must not stop before the body can fall at x=" + x);
            }
            assertFalse(world.isStableDryPosition(1.5, 64, 0.5));
            assertFalse(follower.tick(1.5, 64, 0.5).forward(), "release after clearing the ledge");
            assertEquals(2, follower.tick(1.5, landingY, 0.5).waypointIndex());
        }
    }

    @Test
    void descendingFractionalWaypointIsNotConsumedWhileStillAboveItsSupport() {
        var follower = new PathFollower(List.of(
            new AStarPathfinder.Waypoint(0, 64, 0, 64),
            new AStarPathfinder.Waypoint(1, 63, 0, 63.5),
            new AStarPathfinder.Waypoint(2, 63, 0, 63.5)
        ));
        follower.tick(0.5, 64, 0.5);
        var falling = follower.tick(1.5, 63.9, 0.5);
        assertEquals(1, falling.waypointIndex());
        assertFalse(falling.forward());
        assertFalse(falling.jump());
        assertEquals(2, follower.tick(1.5, 63.5, 0.5).waypointIndex());
    }

    @Test
    void pickupAndInteractionPredicatesUsePhysicalHeightWithoutChangingTargetAddresses() {
        var world = MinecraftWorldViewTest.nativeWorld(Map.of(
            new BlockPos(0, 63, 0), Blocks.STONE_SLAB.defaultBlockState(),
            new BlockPos(1, 64, 0), Blocks.STONE.defaultBlockState()
        ));
        assertTrue(new Goal.Stand(0, 63, 0).isSatisfied(world, 0, 63, 0));
        assertFalse(new Goal.Stand(0, 64, 0).isSatisfied(world, 0, 64, 0));
        assertFalse(new Goal.Near(0, 63, 0, 0.2).isSatisfied(world, 0, 63, 0));
        assertTrue(new Goal.Pickup(0.5, 65.5, 0.5).isSatisfied(world, 0, 63, 0));
        assertFalse(new Goal.Pickup(0.5, 62.8, 0.5).isSatisfied(world, 0, 63, 0));
        assertTrue(new Goal.Interact(1, 64, 0, 2).isSatisfied(world, 0, 63, 0));
    }

    @Test
    void measureNativeGeometrySearchWorkAtTheExistingTickNodeCap() {
        var states = new java.util.HashMap<BlockPos, BlockState>();
        for (int x = -40; x <= 40; x++) {
            for (int z = -40; z <= 40; z++) {
                states.put(new BlockPos(x, 63, z), Blocks.STONE.defaultBlockState());
            }
        }
        var world = MinecraftWorldViewTest.nativeWorld(states);
        var search = new AStarPathfinder(world, new Goal.XZ(100, 100), 0, 64, 0);
        long started = System.nanoTime();
        var result = search.step(NavigateExecutor.NODES_PER_TICK);
        double elapsedMs = (System.nanoTime() - started) / 1_000_000.0;
        assertEquals(AStarPathfinder.Outcome.IN_PROGRESS, result.outcome());
        assertEquals(NavigateExecutor.NODES_PER_TICK, result.expandedNodes());
        System.out.println("GEOMETRY_SEARCH_PROBE expanded=" + result.expandedNodes()
            + " elapsed_ms=" + elapsedMs);
        var sliced = new AStarPathfinder(world, new Goal.XZ(100, 100), 0, 64, 0);
        started = System.nanoTime();
        result = sliced.step(NavigateExecutor.NODES_PER_TICK, NavigateExecutor.PLANNING_NANOS_PER_TICK);
        elapsedMs = (System.nanoTime() - started) / 1_000_000.0;
        assertEquals(AStarPathfinder.Outcome.IN_PROGRESS, result.outcome());
        assertTrue(result.expandedNodes() > 0);
        System.out.println("GEOMETRY_TIMED_SLICE expanded=" + result.expandedNodes()
            + " elapsed_ms=" + elapsedMs);
    }

    @Test
    void nativeMovementRejectsAnObstructedSweepDespiteLegalEndpoints() {
        BlockState door = Blocks.OAK_TRAPDOOR.defaultBlockState()
            .setValue(BlockStateProperties.OPEN, true)
            .setValue(BlockStateProperties.HORIZONTAL_FACING, Direction.EAST);
        var world = MinecraftWorldViewTest.nativeWorld(Map.of(
            new BlockPos(0, 63, 0), Blocks.STONE.defaultBlockState(),
            new BlockPos(1, 63, 0), Blocks.STONE.defaultBlockState(),
            new BlockPos(2, 63, 0), Blocks.STONE.defaultBlockState(),
            new BlockPos(1, 64, 0), door
        ));
        var shape = door.getCollisionShape(EmptyBlockGetter.INSTANCE, BlockPos.ZERO);
        int start = shape.min(Direction.Axis.X) < 0.5 ? 0 : 1;
        assertEquals(64, world.standingHeight(start, 64, 0));
        assertEquals(64, world.standingHeight(start + 1, 64, 0));
        assertFalse(world.isDryTransitionClear(start, 64, 0, start + 1, 64, 0));
        assertFalse(new MoveGenerator(world).movesFrom(start, 64, 0).moves().stream()
            .anyMatch(move -> move.x() == start + 1 && move.y() == 64 && move.z() == 0));
    }

    @Test
    void aSolidFeetCellCanContainAFullStandingBodyBelowACeiling() {
        // A carpet and a bottom slab share the same coarse SOLID classification,
        // but only the carpet leaves standing-player headroom under this roof.
        for (Surface surface : List.of(
            new Surface("carpet", Blocks.WHITE_CARPET.defaultBlockState(), 0.0625),
            new Surface("bottom_slab", Blocks.STONE_SLAB.defaultBlockState(), 0.5)
        )) {
            var blocks = MinecraftWorldViewTest.blocks(Map.of(
                new BlockPos(0, 63, 0), surface.block(),
                new BlockPos(0, 65, 0), Blocks.STONE.defaultBlockState()
            ));
            double feet = 63 + surface.height();
            assertEquals(63, NavigateExecutor.PositionSource.Position.feetBlockY(feet));
            assertEquals(WorldView.NodeKind.SOLID, MinecraftWorldView.classify(
                surface.block(), blocks, new BlockPos(0, 63, 0)));
            assertEquals(surface.height() + 1.8 <= 2,
                MinecraftWorldView.hasStableCollisionSupport(blocks, 0.5, feet, 0.5),
                surface.name());
            assertFalse(MinecraftWorldView.hasStableCollisionSupport(blocks, 0.5, 64, 0.5),
                "the integer ceiling of support height is not a physical stand: " + surface.name());
        }
    }

    @Test
    void supportedClearEndpointsDoNotProveHorizontalTraversal() {
        BlockState door = Blocks.OAK_TRAPDOOR.defaultBlockState()
            .setValue(BlockStateProperties.OPEN, true)
            .setValue(BlockStateProperties.HORIZONTAL_FACING, Direction.EAST);
        BlockPos doorPos = new BlockPos(1, 64, 0);
        var blocks = MinecraftWorldViewTest.blocks(Map.of(
            new BlockPos(0, 63, 0), Blocks.STONE.defaultBlockState(),
            new BlockPos(1, 63, 0), Blocks.STONE.defaultBlockState(),
            new BlockPos(2, 63, 0), Blocks.STONE.defaultBlockState(),
            doorPos, door
        ));
        var obstacle = door.getCollisionShape(blocks, doorPos).move(1, 64, 0);
        // Select the adjacent column across the actual native trapdoor plane;
        // both centered endpoints are clear, regardless of facing convention.
        double startX = obstacle.min(Direction.Axis.X) < 1.5 ? 0.5 : 1.5;
        double endX = startX + 1;
        assertTrue(MinecraftWorldView.hasStableCollisionSupport(blocks, startX, 64, 0.5));
        assertTrue(MinecraftWorldView.hasStableCollisionSupport(blocks, endX, 64, 0.5));
        AABB body = new AABB(startX - 0.3, 64, 0.2, startX + 0.3, 65.8, 0.8);
        double permitted = obstacle.collide(Direction.Axis.X, body, endX - startX);
        assertTrue(permitted >= 0 && permitted < endX - startX,
            "endpoint occupancy must not substitute for an edge collision check");
        System.out.println("GEOMETRY_EDGE_PROBE clear_supported_endpoints=true requested_dx=1.0"
            + " native_permitted_dx=" + permitted);
    }

    @Test
    void measureNativeSupportAndReportPlannerFollowerAgreement() {
        List<Surface> surfaces = List.of(
            new Surface("full_block", Blocks.STONE.defaultBlockState(), 1.0),
            new Surface("bottom_slab", Blocks.STONE_SLAB.defaultBlockState(), 0.5),
            new Surface("top_slab", Blocks.STONE_SLAB.defaultBlockState()
                .setValue(BlockStateProperties.SLAB_TYPE, SlabType.TOP), 1.0),
            new Surface("stairs", Blocks.STONE_STAIRS.defaultBlockState(), 1.0),
            new Surface("carpet", Blocks.WHITE_CARPET.defaultBlockState(), 0.0625),
            new Surface("closed_trapdoor", Blocks.OAK_TRAPDOOR.defaultBlockState(), 0.1875),
            new Surface("fence", Blocks.OAK_FENCE.defaultBlockState(), 1.5),
            new Surface("wall", Blocks.COBBLESTONE_WALL.defaultBlockState(), 1.5)
        );
        for (Surface surface : surfaces) {
            var shape = surface.block().getCollisionShape(EmptyBlockGetter.INSTANCE, BlockPos.ZERO);
            // Resolve downward movement of a centered, standing player using
            // Minecraft's shape solver, rather than a hand-written top-face rule.
            AABB player = new AABB(0.2, 3.0, 0.2, 0.8, 4.8, 0.8);
            double feetHeight = 3.0 + shape.collide(Direction.Axis.Y, player, -3.0);
            assertEquals(surface.height(), feetHeight, 1.0e-7, surface.name());

            WorldView world = MinecraftWorldViewTest.nativeWorld(Map.of(
                new BlockPos(0, 63, 0), Blocks.STONE.defaultBlockState(),
                new BlockPos(1, 63, 0), surface.block()
            ));
            int goalY = NavigateExecutor.PositionSource.Position.feetBlockY(63 + feetHeight);
            var east = new MoveGenerator(world).movesFrom(0, 64, 0).moves().stream()
                .filter(move -> move.x() == 1 && move.z() == 0).findFirst();
            boolean followerArrived = false;
            if (east.isPresent()) {
                var move = east.get();
                var follower = new PathFollower(List.of(
                    new AStarPathfinder.Waypoint(1, move.y(), 0, world.standingHeight(1, move.y(), 0))
                ));
                followerArrived = follower.tick(1.5, 63.0 + feetHeight, 0.5).state()
                    == PathFollower.State.ARRIVED;
            }
            var approach = new ApproachController(
                "probe", "geometry", new Goal.Stand(1, goalY, 0), world,
                new MovementControls() {
                    public void lookAt(String bot, double x, double y, double z) {}
                    public void moveForward(String bot) {}
                    public void stopMovement(String bot) {}
                    public void jumpOnce(String bot) {}
                    public void jumpContinuous(String bot) {}
                    public void sprint(String bot) {}
                },
                (bot, tick, name, action, data) -> {}, 0
            );
            var outcome = approach.tick(0, 0.5, 64.0, 0.5);
            int observedTicks = 0;
            while (outcome.status() == ApproachController.Status.WORKING
                && observedTicks < 2 * PathFollower.STUCK_TICKS) {
                observedTicks++;
                outcome = approach.tick(observedTicks, 1.5, 63.0 + feetHeight, 0.5);
            }
            approach.halt();
            assertTrue(east.isPresent(), surface.name());
            assertEquals(goalY, east.get().y(), surface.name());
            assertEquals(ApproachController.Status.COMPLETED, outcome.status(),
                "actual supported position in the goal voxel must settle: " + surface.name());
            // These are diagnostic facts, not assertions that preserve today's
            // graph mismatch as desired behavior. Collision measurements above
            // remain valid when the graph/arrival representation is replaced.
            System.out.println("GEOMETRY_PROBE surface=" + surface.name()
                + " native_feet_y=" + (63.0 + feetHeight)
                + " planned_feet_y=" + east.map(MoveGenerator.Move::y).orElse(null)
                + " follower_arrived=" + followerArrived
                + " approach_status=" + outcome.status()
                + " approach_reason=" + outcome.reason()
                + " observed_ticks=" + observedTicks);
        }
    }
}
