package dev.minebot.client.body;

import java.util.HashSet;
import java.util.Set;

/** Counts repeated execution failures, never mere waiting or path recalculation. */
final class NavigationProgress {
    private final Set<Long> visitedCells = new HashSet<>();
    private String failedStep = "";
    private int repeatedFailures;
    private long lastProgressMs;

    /**
     * Reaching a block the Body has not stood in during this run is progress;
     * moving inside cells it already knows is not. Bobbing in water between
     * two cells reset the failure count on every rise, so a clear run in a
     * flooded pit broke nothing for 5 minutes and never stopped by itself
     * (house run 2026-10-18).
     */
    void observePosition(double nextX, double nextY, double nextZ, long elapsedMs) {
        long cell = cellKey((int) Math.floor(nextX), (int) Math.floor(nextY), (int) Math.floor(nextZ));
        if (visitedCells.add(cell)) {
            progressed(elapsedMs);
        }
    }

    private static long cellKey(int x, int y, int z) {
        return ((long) (x & 0x3FFFFFF) << 38) | ((long) (z & 0x3FFFFFF) << 12) | (y & 0xFFF);
    }

    void progressed(long elapsedMs) {
        lastProgressMs = elapsedMs;
        failedStep = "";
        repeatedFailures = 0;
    }

    boolean executionFailed(String step) {
        repeatedFailures = step.equals(failedStep) ? repeatedFailures + 1 : 1;
        failedStep = step;
        return repeatedFailures >= 2;
    }

    int repeatedFailures() { return repeatedFailures; }
    long idleMs(long elapsedMs) { return Math.max(0, elapsedMs - lastProgressMs); }
}
