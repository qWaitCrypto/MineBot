package dev.minebot.body.nav;

import dev.minebot.body.nav.AStarPathfinder.Outcome;
import dev.minebot.body.nav.AStarPathfinder.Result;
import dev.minebot.body.nav.AStarPathfinder.Waypoint;
import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

final class AStarPathfinderTest {
    private static final int FLOOR = 63;
    private static final int STAND = FLOOR + 1;

    private static Result solve(WorldView world, Goal goal, int sx, int sz) {
        return solveAt(world, goal, sx, STAND, sz);
    }

    private static Result solveAt(WorldView world, Goal goal, int sx, int sy, int sz) {
        AStarPathfinder pathfinder = new AStarPathfinder(world, goal, sx, sy, sz);
        Result result;
        do {
            result = pathfinder.step(10_000);
        } while (result.outcome() == Outcome.IN_PROGRESS);
        return result;
    }

    private static void assertContiguous(List<Waypoint> path) {
        for (int i = 1; i < path.size(); i++) {
            Waypoint a = path.get(i - 1);
            Waypoint b = path.get(i);
            assertTrue(Math.abs(a.x() - b.x()) <= 1 && Math.abs(a.z() - b.z()) <= 1, "steps move one column");
            assertTrue(b.y() - a.y() <= 1 && a.y() - b.y() <= MoveGenerator.MAX_FALL_BLOCKS, "vertical steps bounded");
        }
    }

    @Test
    void flatWorldReachesANearGoal() {
        Result result = solve(new FakeWorld(FLOOR), new Goal.Near(20, STAND, 0, 1.5), 0, 0);

        assertEquals(Outcome.COMPLETE, result.outcome());
        Waypoint last = result.path().get(result.path().size() - 1);
        assertTrue(new Goal.Near(20, STAND, 0, 1.5).isSatisfied(last.x(), last.y(), last.z()));
        assertContiguous(result.path());
        assertEquals(0, result.unloadedTouches());
    }

    @Test
    void wallsForceThePathThroughTheDoorway() {
        FakeWorld world = new FakeWorld(FLOOR);
        // A wall across z from -6..6 at x=5, with a doorway at z=4.
        for (int z = -6; z <= 6; z++) {
            if (z != 4) {
                world.wall(5, z, STAND + 2);
            }
        }
        Result result = solve(world, new Goal.Near(10, STAND, 0, 0.5), 0, 0);

        assertEquals(Outcome.COMPLETE, result.outcome());
        boolean throughDoorway = result.path().stream().anyMatch(w -> w.x() == 5 && w.z() == 4);
        boolean aroundWall = result.path().stream().anyMatch(w -> w.x() == 5 && Math.abs(w.z()) > 6);
        assertTrue(throughDoorway || aroundWall, "path must pass the doorway or walk around the wall");
        assertContiguous(result.path());
    }

    @Test
    void stepUpAndFallTerrainIsWalkable() {
        FakeWorld world = new FakeWorld(FLOOR);
        // A one-block-high ridge at x=3..4 across the route.
        for (int z = -8; z <= 8; z++) {
            world.set(3, STAND, z, WorldView.NodeKind.SOLID);
            world.set(4, STAND, z, WorldView.NodeKind.SOLID);
        }
        Result result = solve(world, new Goal.Near(8, STAND, 0, 0.5), 0, 0);

        assertEquals(Outcome.COMPLETE, result.outcome());
        assertTrue(result.path().stream().anyMatch(w -> w.y() == STAND + 1), "the ridge is climbed");
        assertContiguous(result.path());
    }

    @Test
    void enclosedTargetIsAnHonestNoPath() {
        FakeWorld world = new FakeWorld(FLOOR);
        for (int x = 8; x <= 12; x++) {
            for (int z = -2; z <= 2; z++) {
                boolean border = x == 8 || x == 12 || z == -2 || z == 2;
                if (border) {
                    world.wall(x, z, STAND + 3);
                }
            }
        }
        Result result = solve(world, new Goal.Near(10, STAND, 0, 0.5), 0, 0);

        // The searcher explores the whole reachable region; with an infinite
        // flat plain that would never exhaust, so bound it with walls too far
        // to matter is impractical — instead the node cap produces the honest
        // bounded outcome.
        assertTrue(
            result.outcome() == Outcome.NO_PATH || result.outcome() == Outcome.PARTIAL,
            "an enclosed target can never be COMPLETE"
        );
    }

    @Test
    void lavaIsNeverWalkedOn() {
        FakeWorld world = new FakeWorld(FLOOR);
        // A lava channel across the route: floor and standing cell are HAZARD.
        for (int z = -32; z <= 32; z++) {
            world.set(5, FLOOR, z, WorldView.NodeKind.HAZARD);
            world.set(5, STAND, z, WorldView.NodeKind.HAZARD);
        }
        Result result = solve(world, new Goal.Near(10, STAND, 0, 0.5), 0, 0);

        result.path().forEach(w -> assertTrue(w.x() != 5 || Math.abs(w.z()) > 32, "no waypoint stands in lava"));
    }

    @Test
    void aWaterChannelIsSwumAcrossToReachTheGoal() {
        FakeWorld world = new FakeWorld(FLOOR);
        // A 3-wide water channel the route must cross (feet + head are water).
        for (int x = 4; x <= 6; x++) {
            for (int z = -20; z <= 20; z++) {
                world.set(x, STAND, z, WorldView.NodeKind.LIQUID);
                world.set(x, STAND + 1, z, WorldView.NodeKind.LIQUID);
            }
        }
        Result result = solve(world, new Goal.Near(10, STAND, 0, 0.5), 0, 0);

        assertEquals(Outcome.COMPLETE, result.outcome());
        assertTrue(result.path().stream().anyMatch(w -> w.x() >= 4 && w.x() <= 6), "the path swims through the channel");
        assertContiguous(result.path());
    }

    @Test
    void aWaterColumnIsClimbedToTheSurface() {
        // Vertical escape through water: the bot starts at the bottom of a
        // water shaft and must swim up and climb out onto the surface.
        FakeWorld world = new FakeWorld(FLOOR);
        int bottom = FLOOR - 6;
        // Walls forming a shaft at (0,0) from bottom up to STAND, water inside.
        for (int y = bottom; y <= STAND; y++) {
            world.set(0, y, 0, WorldView.NodeKind.LIQUID);
            for (int[] wall : new int[][] {{1, 0}, {-1, 0}, {0, 1}, {0, -1}}) {
                world.set(wall[0], y, wall[1], WorldView.NodeKind.SOLID);
            }
        }
        // Open the top so the surface at STAND has land to step onto, with a
        // walkable plain beyond it.
        for (int x = 1; x <= 6; x++) {
            world.set(x, STAND, 0, WorldView.NodeKind.SOLID);
        }

        Result result = solveAt(world, new Goal.Near(4, STAND + 1, 0, 0.5), 0, bottom, 0);

        assertEquals(Outcome.COMPLETE, result.outcome());
        assertTrue(result.path().get(0).y() < STAND, "starts below the surface");
        assertTrue(result.path().stream().anyMatch(w -> w.y() >= STAND + 1), "rises out of the shaft");
    }

    @Test
    void unloadedGoalRegionIsBoundedAndHonest() {
        FakeWorld world = new FakeWorld(FLOOR);
        for (int chunkX = 1; chunkX <= 12; chunkX++) {
            for (int chunkZ = -12; chunkZ <= 12; chunkZ++) {
                world.unloadChunk(chunkX, chunkZ);
            }
        }
        Result result = solve(world, new Goal.Near(120, STAND, 0, 0.5), 4, 0);

        assertTrue(result.outcome() == Outcome.NO_PATH || result.outcome() == Outcome.PARTIAL);
        assertTrue(result.unloadedTouches() > 0, "the boundary must be counted, never invented");
    }

    @Test
    void interactGoalUnifiesStandSelectionWithReachability() {
        // The structural fix for the Scarpet-era failure family: a tree trunk
        // whose east side is fenced off. The only legal stands are west; the
        // planner must end at one of them without any separate stand pre-pass.
        FakeWorld world = new FakeWorld(FLOOR);
        int treeX = 12;
        int treeZ = 0;
        for (int y = STAND; y <= STAND + 4; y++) {
            world.set(treeX, y, treeZ, WorldView.NodeKind.SOLID);
        }
        // A tall fence isolating everything east of the trunk plus the near columns.
        for (int z = -10; z <= 10; z++) {
            world.wall(treeX - 1, z, STAND + 4);
        }
        world.set(treeX - 1, STAND, treeZ, WorldView.NodeKind.SOLID);
        world.set(treeX - 1, STAND + 1, treeZ, WorldView.NodeKind.SOLID);

        Goal goal = new Goal.Interact(treeX, STAND + 1, treeZ, 4.5);
        Result result = solve(world, goal, 0, 0);

        assertEquals(Outcome.COMPLETE, result.outcome());
        Waypoint stand = result.path().get(result.path().size() - 1);
        assertTrue(goal.isSatisfied(stand.x(), stand.y(), stand.z()), "the terminal node is a legal stand");
        assertTrue(stand.x() < treeX - 1, "the stand is on the reachable side of the fence");
        assertContiguous(result.path());
    }

    @Test
    void compositeGoalPicksTheReachableMember() {
        FakeWorld world = new FakeWorld(FLOOR);
        // Box in the near member entirely.
        for (int x = 6; x <= 10; x++) {
            for (int z = -2; z <= 2; z++) {
                if (x == 6 || x == 10 || z == -2 || z == 2) {
                    world.wall(x, z, STAND + 3);
                }
            }
        }
        Goal goal = new Goal.Composite(List.of(
            new Goal.Near(8, STAND, 0, 0.5),
            new Goal.Near(0, STAND, 15, 0.5)
        ));
        Result result = solve(world, goal, 0, 0);

        assertEquals(Outcome.COMPLETE, result.outcome());
        Waypoint last = result.path().get(result.path().size() - 1);
        assertTrue(new Goal.Near(0, STAND, 15, 0.5).isSatisfied(last.x(), last.y(), last.z()));
    }

    @Test
    void nodeBudgetYieldsPartialProgressTowardTheGoal() {
        AStarPathfinder pathfinder = new AStarPathfinder(
            new FakeWorld(FLOOR),
            new Goal.Near(4_000, STAND, 0, 0.5),
            0,
            STAND,
            0,
            3_000,
            AStarPathfinder.DEFAULT_UNLOADED_TOUCH_CAP
        );
        Result result;
        do {
            result = pathfinder.step(1_000);
        } while (result.outcome() == Outcome.IN_PROGRESS);

        assertEquals(Outcome.PARTIAL, result.outcome());
        assertEquals("node_budget", result.reason());
        Waypoint last = result.path().get(result.path().size() - 1);
        assertTrue(last.x() >= AStarPathfinder.MIN_PARTIAL_PROGRESS_BLOCKS, "partial path moves toward the goal");
    }

    @Test
    void planningIsResumableAcrossTickSlices() {
        AStarPathfinder pathfinder = new AStarPathfinder(new FakeWorld(FLOOR), new Goal.Near(30, STAND, 0, 0.5), 0, STAND, 0);
        int slices = 0;
        Result result;
        do {
            result = pathfinder.step(50);
            slices++;
        } while (result.outcome() == Outcome.IN_PROGRESS);

        assertEquals(Outcome.COMPLETE, result.outcome());
        assertTrue(slices > 1, "a small slice budget must span multiple steps");
        // A finished search is stable on further calls.
        assertEquals(Outcome.COMPLETE, pathfinder.step(50).outcome());
    }
}
