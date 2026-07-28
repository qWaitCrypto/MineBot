package dev.minebot.body.nav;

import dev.minebot.body.nav.AStarPathfinder.Waypoint;

import java.util.List;

/**
 * Pure per-tick waypoint following. Consumes the bot's observed position and
 * yields a directive: where to look, whether to hold forward, whether to
 * jump — or that the path is done, deviated, or stuck. One stuck detector
 * lives here and nowhere else (entropy rule).
 */
public final class PathFollower {
    public static final double WAYPOINT_REACH_DISTANCE = 0.7;
    public static final double DEVIATION_DISTANCE = 4.0;
    public static final int RESYNC_LOOKAHEAD = 3;
    public static final int STUCK_TICKS = 60;
    public static final double STUCK_MIN_PROGRESS = 0.05;

    public enum State {
        CONTINUE,
        ARRIVED,
        DEVIATED,
        STUCK
    }

    public record Directive(State state, Waypoint lookTarget, boolean forward, boolean jump, int waypointIndex) {
    }

    private final List<Waypoint> path;
    private final double finalWaypointReachDistance;
    private int index;
    private double bestProgress = -Double.MAX_VALUE;
    private int ticksWithoutProgress;

    public PathFollower(List<Waypoint> path) {
        this(path, WAYPOINT_REACH_DISTANCE);
    }

    public PathFollower(List<Waypoint> path, double finalWaypointReachDistance) {
        if (path.isEmpty()) {
            throw new IllegalArgumentException("path must not be empty");
        }
        if (!(finalWaypointReachDistance > 0.0)
            || finalWaypointReachDistance > WAYPOINT_REACH_DISTANCE) {
            throw new IllegalArgumentException(
                "final waypoint reach distance must be in (0, " + WAYPOINT_REACH_DISTANCE + "]"
            );
        }
        this.path = List.copyOf(path);
        this.finalWaypointReachDistance = finalWaypointReachDistance;
    }

    public Directive tick(double px, double py, double pz) {
        advanceThroughReachedWaypoints(px, py, pz);
        if (index >= path.size()) {
            return new Directive(State.ARRIVED, path.get(path.size() - 1), false, false, path.size() - 1);
        }
        if (!resync(px, py, pz)) {
            return new Directive(State.DEVIATED, path.get(index), false, false, index);
        }
        if (trackStuck(px, py, pz)) {
            return new Directive(State.STUCK, path.get(index), false, false, index);
        }
        Waypoint target = path.get(index);
        boolean jump = target.y() > Math.floor(py) + 0.1 && horizontalDistance(px, pz, target) < 1.1;
        return new Directive(State.CONTINUE, target, true, jump, index);
    }

    private void advanceThroughReachedWaypoints(double px, double py, double pz) {
        while (index < path.size() && reached(px, py, pz, path.get(index), reachDistance(index))) {
            index++;
            bestProgress = -Double.MAX_VALUE;
            ticksWithoutProgress = 0;
        }
    }

    /** After lag or drift, snap to the nearest of the next few waypoints. */
    private boolean resync(double px, double py, double pz) {
        int bestIndex = -1;
        double bestDistance = DEVIATION_DISTANCE;
        for (int i = index; i < Math.min(index + RESYNC_LOOKAHEAD + 1, path.size()); i++) {
            double distance = waypointDistance(px, py, pz, path.get(i));
            if (distance < bestDistance) {
                bestDistance = distance;
                bestIndex = i;
            }
        }
        if (bestIndex < 0) {
            // A graph edge can represent a compound physical move, such as
            // walking off a ledge and falling several blocks into water. The
            // player is still on that edge even when neither endpoint is
            // within the ordinary point-deviation radius.
            return index > 0
                && segmentDistance(px, py, pz, path.get(index - 1), path.get(index))
                    < DEVIATION_DISTANCE;
        }
        if (bestIndex > index) {
            index = bestIndex;
            bestProgress = -Double.MAX_VALUE;
            ticksWithoutProgress = 0;
        }
        return true;
    }

    private boolean trackStuck(double px, double py, double pz) {
        double progress = index * 1_000.0 - waypointDistance(px, py, pz, path.get(index));
        if (progress > bestProgress + STUCK_MIN_PROGRESS) {
            bestProgress = progress;
            ticksWithoutProgress = 0;
            return false;
        }
        ticksWithoutProgress++;
        return ticksWithoutProgress >= STUCK_TICKS;
    }

    private double reachDistance(int waypointIndex) {
        return waypointIndex == path.size() - 1
            ? finalWaypointReachDistance
            : WAYPOINT_REACH_DISTANCE;
    }

    private static boolean reached(
        double px,
        double py,
        double pz,
        Waypoint waypoint,
        double reachDistance
    ) {
        // Upward waypoints must actually be climbed; downward ones tolerate
        // the airborne tail of a fall.
        return horizontalDistance(px, pz, waypoint) <= reachDistance
            && py >= waypoint.y() - 0.05
            && py - waypoint.y() <= 1.5;
    }

    private static double horizontalDistance(double px, double pz, Waypoint waypoint) {
        double dx = px - (waypoint.x() + 0.5);
        double dz = pz - (waypoint.z() + 0.5);
        return Math.sqrt(dx * dx + dz * dz);
    }

    private static double waypointDistance(
        double px,
        double py,
        double pz,
        Waypoint waypoint
    ) {
        double dx = px - (waypoint.x() + 0.5);
        double dy = py - waypoint.y();
        double dz = pz - (waypoint.z() + 0.5);
        return Math.sqrt(dx * dx + dy * dy + dz * dz);
    }

    private static double segmentDistance(
        double px,
        double py,
        double pz,
        Waypoint start,
        Waypoint end
    ) {
        double sx = start.x() + 0.5;
        double sy = start.y();
        double sz = start.z() + 0.5;
        double vx = end.x() - start.x();
        double vy = end.y() - start.y();
        double vz = end.z() - start.z();
        double lengthSquared = vx * vx + vy * vy + vz * vz;
        if (lengthSquared == 0.0) {
            return waypointDistance(px, py, pz, start);
        }
        double projection = ((px - sx) * vx + (py - sy) * vy + (pz - sz) * vz) / lengthSquared;
        double t = Math.max(0.0, Math.min(1.0, projection));
        double dx = px - (sx + t * vx);
        double dy = py - (sy + t * vy);
        double dz = pz - (sz + t * vz);
        return Math.sqrt(dx * dx + dy * dy + dz * dz);
    }

    public int waypointIndex() {
        return index;
    }

    public int pathLength() {
        return path.size();
    }
}
