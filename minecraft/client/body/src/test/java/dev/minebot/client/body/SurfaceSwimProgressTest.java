package dev.minebot.client.body;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class SurfaceSwimProgressTest {
    @Test
    void requiresHeadOutAndRecoveringAir() {
        assertFalse(SurfaceSwimProgress.completed(false, 100, 120, 300));
        assertFalse(SurfaceSwimProgress.completed(true, 100, 100, 300));
        assertTrue(SurfaceSwimProgress.completed(true, 100, 101, 300));
    }

    @Test
    void headOutWithFullAirIsAlreadyAtTheSurface() {
        assertTrue(SurfaceSwimProgress.completed(true, 300, 300, 300));
        assertFalse(SurfaceSwimProgress.completed(false, 300, 300, 300));
    }

    @Test
    void notRisingForTwoSecondsMeansTheWayUpIsClosed() {
        assertFalse(SurfaceSwimProgress.blocked(39));
        assertTrue(SurfaceSwimProgress.blocked(40));
    }

    @Test
    void rejectsLavaUnloadedAndOutOfBoundsTargets() {
        assertFalse(SurfaceSwimProgress.allowed(true, true, 64, 0, 320));
        assertFalse(SurfaceSwimProgress.allowed(false, false, 64, 0, 320));
        assertFalse(SurfaceSwimProgress.allowed(false, true, -1, 0, 320));
        assertFalse(SurfaceSwimProgress.allowed(false, true, 319, 0, 320));
        assertTrue(SurfaceSwimProgress.allowed(false, true, 64, 0, 320));
    }
}
