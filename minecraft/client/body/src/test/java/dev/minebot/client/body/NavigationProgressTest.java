package dev.minebot.client.body;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class NavigationProgressTest {
    private static final String STEP = "MovementAscend(45,55,655)(45,56,654)";

    @Test
    void bobbingInPlaceDoesNotClearTheFailureCount() {
        var progress = new NavigationProgress();
        progress.observePosition(45.7, 55.0, 655.3, 0);
        progress.observePosition(45.7, 56.5, 655.3, 500);
        assertFalse(progress.executionFailed(STEP));
        progress.observePosition(45.7, 55.0, 655.3, 1000);
        progress.observePosition(45.7, 56.2, 655.3, 1500);
        assertTrue(progress.executionFailed(STEP));
        assertEquals(1000, progress.idleMs(1500));
    }

    @Test
    void reachingANewCellIsProgress() {
        var progress = new NavigationProgress();
        progress.observePosition(45.7, 55.0, 655.3, 0);
        assertFalse(progress.executionFailed(STEP));
        progress.observePosition(46.2, 55.0, 655.3, 800);
        assertEquals(0, progress.repeatedFailures());
        assertFalse(progress.executionFailed(STEP));
    }

    @Test
    void negativeCoordinatesAreDistinctCells() {
        var progress = new NavigationProgress();
        progress.observePosition(-0.5, 64.0, -0.5, 0);
        assertFalse(progress.executionFailed(STEP));
        progress.observePosition(0.5, 64.0, 0.5, 100);
        assertEquals(0, progress.repeatedFailures());
    }
}
