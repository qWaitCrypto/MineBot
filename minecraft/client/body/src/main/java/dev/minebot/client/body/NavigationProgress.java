package dev.minebot.client.body;

/** Counts repeated execution failures, never mere waiting or path recalculation. */
final class NavigationProgress {
    private boolean positioned;
    private double x, y, z;
    private String failedStep = "";
    private int repeatedFailures;
    private long lastProgressMs;

    void observePosition(double nextX, double nextY, double nextZ, long elapsedMs) {
        double distanceSquared = (nextX - x) * (nextX - x) + (nextY - y) * (nextY - y)
            + (nextZ - z) * (nextZ - z);
        if (!positioned || distanceSquared >= 0.0625) {
            x = nextX;
            y = nextY;
            z = nextZ;
            positioned = true;
            progressed(elapsedMs);
        }
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
