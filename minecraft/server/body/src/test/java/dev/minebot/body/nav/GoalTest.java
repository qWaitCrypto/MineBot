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
