package dev.minebot.body.nav;

import static org.junit.jupiter.api.Assertions.assertDoesNotThrow;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.ArrayList;
import java.util.List;
import org.junit.jupiter.api.Test;

final class GoalTest {
    @Test
    void distantCompositeStandsDoNotReadCollisionGeometry() {
        int[] reads = {0};
        WorldView world = new WorldView() {
            public NodeKind kindAt(int x, int y, int z) { return NodeKind.PASSABLE; }
            public double standingHeight(int x, int y, int z) { reads[0]++; return y + 0.5; }
        };
        List<Goal> targets = new ArrayList<>();
        for (int i = 0; i < 32; i++) {
            targets.add(new Goal.Near(100 + i, 64, 100, 0.5));
        }
        assertFalse(new Goal.Composite(targets).isSatisfied(world, 0, 64, 0));
        assertEquals(0, reads[0], "horizontal exclusion needs no standing/collision read");
        assertTrue(new Goal.Near(0, 64, 0, 0.5).isSatisfied(world, 0, 64, 0));
        assertEquals(1, reads[0], "nearby sub-voxel geometry still requires authoritative height");
        assertFalse(new Goal.Near(0, 64, 0, 0).isSatisfied(world, 0, 64, 0));
    }

    @Test
    void interactionReadinessRejectsNearbyOccludedBlocks() {
        FakeWorld world = new FakeWorld(63);
        Goal.Interact goal = new Goal.Interact(2, 64, 0, 4.5);
        assertTrue(goal.isWithinPhysicalReach(world, 0.5, 64, 0.5));
        world.wall(1, 0, 67);
        assertFalse(goal.isWithinPhysicalReach(world, 0.5, 64, 0.5));
        assertTrue(goal.isWithinPhysicalReach(world, 2.5, 64, 1.5));
    }

    @Test
    void interactionReadinessUsesActualPositionAtRangeBoundary() {
        FakeWorld world = new FakeWorld(63);
        Goal.Interact goal = new Goal.Interact(4, 64, 0, 4.5);
        assertTrue(goal.isWithinPhysicalReach(world, 0.5, 64, 0.5));
        assertFalse(goal.isWithinPhysicalReach(world, 0.0, 64, 0.5));
    }

    @Test
    void compositeLimitMatchesTheCompletePythonStandDomain() {
        assertEquals(32, Goal.MAX_COMPOSITE_MEMBERS);
        assertDoesNotThrow(() -> new Goal.Composite(goals(32)));
        assertThrows(IllegalArgumentException.class, () -> new Goal.Composite(goals(33)));
    }

    @Test
    void pickupPhysicalReachUsesExactPlayerPositionInsteadOfTheCellCenter() {
        int standY = 79;
        FakeWorld world = new FakeWorld(standY - 1);
        Goal.Pickup goal = new Goal.Pickup(-63.5, 80.5, -62.7);

        assertFalse(
            goal.isSatisfied(world, -64, standY, -62),
            "the conservative planning envelope excludes this cell center"
        );
        assertTrue(
            goal.isWithinPhysicalReach(-63.5, standY, -61.45),
            "the exact player position still overlaps the real pickup volume"
        );
    }

    private static List<Goal> goals(int count) {
        List<Goal> goals = new ArrayList<>();
        for (int index = 0; index < count; index++) {
            goals.add(new Goal.Near(index, 64, -index, 0.5));
        }
        return goals;
    }
}
