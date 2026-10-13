package dev.minebot.client.body;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.junit.jupiter.api.Assertions.assertEquals;

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

    @Test
    void routesAroundACeilingToTheNearestOpenWaterColumn() {
        var space = new SurfaceSwimProgress.Space() {
            public boolean water(int x, int y, int z) {
                return Math.abs(x) <= 3 && Math.abs(z) <= 3 && y >= 60 && y <= 65;
            }
            public boolean breathable(int x, int y, int z) {
                // A roof covers the central 3x3 columns; no solid standing point.
                return y == 66 && Math.max(Math.abs(x), Math.abs(z)) > 1;
            }
        };
        var targets = SurfaceSwimProgress.routeTargets(space,
            new SurfaceSwimProgress.Cell(0, 60, 0), 0, 320, 8, 1);
        assertEquals(16, targets.size());
        assertTrue(targets.stream().allMatch(p -> p.y() == 64
            && Math.max(Math.abs(p.x()), Math.abs(p.z())) == 2));
    }

    @Test
    void closedUnloadedOrOutOfRangeWaterDoesNotBecomeATarget() {
        var closed = new SurfaceSwimProgress.Space() {
            public boolean water(int x, int y, int z) { return y < 320; }
            public boolean breathable(int x, int y, int z) { return y >= 320; }
        };
        assertTrue(SurfaceSwimProgress.routeTargets(closed,
            new SurfaceSwimProgress.Cell(0, 315, 0), 0, 320, 4, 1).isEmpty());
        var unloaded = new SurfaceSwimProgress.Space() {
            public boolean water(int x, int y, int z) { return false; }
            public boolean breathable(int x, int y, int z) { return true; }
        };
        assertTrue(SurfaceSwimProgress.routeTargets(unloaded,
            new SurfaceSwimProgress.Cell(0, 60, 0), 0, 320, 4, 1).isEmpty());
    }

    @Test
    void unsuccessfulNearGoalsCanBeReplacedByAFurtherRing() {
        var space = new SurfaceSwimProgress.Space() {
            public boolean water(int x, int y, int z) { return y == 60 || y == 61; }
            public boolean breathable(int x, int y, int z) { return y == 62; }
        };
        var targets = SurfaceSwimProgress.routeTargets(space,
            new SurfaceSwimProgress.Cell(0, 60, 0), 0, 320, 4, 2);
        assertEquals(16, targets.size());
        assertTrue(targets.stream().allMatch(p -> Math.max(Math.abs(p.x()), Math.abs(p.z())) == 2));
    }
}
