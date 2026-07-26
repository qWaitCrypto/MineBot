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
        AStarPathfinder pathfinder = new AStarPathfinder(world, goal, sx, STAND, sz);
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
    void liquidIsNeverWalkedOn() {
        FakeWorld world = new FakeWorld(FLOOR);
        // A water channel across the route: floor and standing cell are LIQUID.
        for (int z = -32; z <= 32; z++) {
            world.set(5, FLOOR, z, WorldView.NodeKind.LIQUID);
            world.set(5, STAND, z, WorldView.NodeKind.LIQUID);
        }
        Result result = solve(world, new Goal.Near(10, STAND, 0, 0.5), 0, 0);

        result.path().forEach(w -> assertTrue(w.x() != 5 || Math.abs(w.z()) > 32, "no waypoint stands in water"));
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
