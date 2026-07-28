package dev.minebot.body.nav;

import static org.junit.jupiter.api.Assertions.assertDoesNotThrow;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

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

    private static List<Goal> goals(int count) {
        List<Goal> goals = new ArrayList<>();
        for (int index = 0; index < count; index++) {
            goals.add(new Goal.Near(index, 64, -index, 0.5));
        }
        return goals;
    }
}
