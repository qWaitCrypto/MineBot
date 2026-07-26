package dev.minebot.body.nav;

import java.util.List;

/**
 * Goals are predicates over standing cells. {@code Interact} is the
 * structural fix for the stand-selection failure family: reachability and
 * stand choice are the same A* computation, because the search terminates at
 * any node from which the target can be interacted with.
 */
public sealed interface Goal {
    /** Cost-model scale for heuristics: optimistic ticks per block. */
    double HEURISTIC_TICKS_PER_BLOCK = 3.5;

    boolean isSatisfied(int x, int y, int z);

    /** Optimistic remaining cost in ticks from a standing cell. */
    double heuristic(int x, int y, int z);

    record Near(int x, int y, int z, double range) implements Goal {
        @Override
        public boolean isSatisfied(int nx, int ny, int nz) {
            double dx = nx - x;
            double dy = ny - y;
            double dz = nz - z;
            return dx * dx + dy * dy + dz * dz <= range * range;
        }

        @Override
        public double heuristic(int nx, int ny, int nz) {
            double dx = nx - x;
            double dy = ny - y;
            double dz = nz - z;
            double distance = Math.sqrt(dx * dx + dy * dy + dz * dz) - range;
            return Math.max(0, distance) * HEURISTIC_TICKS_PER_BLOCK;
        }
    }

    /** Column goal for far or unloaded targets; any Y counts. */
    record XZ(int x, int z) implements Goal {
        @Override
        public boolean isSatisfied(int nx, int ny, int nz) {
            return nx == x && nz == z;
        }

        @Override
        public double heuristic(int nx, int ny, int nz) {
            double dx = nx - x;
            double dz = nz - z;
            return Math.sqrt(dx * dx + dz * dz) * HEURISTIC_TICKS_PER_BLOCK;
        }
    }

    /**
     * Any standing cell whose eye position is within interaction range of the
     * target block center. Exact line-of-sight and legality stay with the
     * execution-time server checks; the goal only shapes the search.
     */
    record Interact(int targetX, int targetY, int targetZ, double range) implements Goal {
        public static final double MINE_RANGE = 4.5;
        private static final double EYE_HEIGHT = 1.62;

        @Override
        public boolean isSatisfied(int nx, int ny, int nz) {
            return eyeDistanceSquared(nx, ny, nz) <= range * range;
        }

        @Override
        public double heuristic(int nx, int ny, int nz) {
            double distance = Math.sqrt(eyeDistanceSquared(nx, ny, nz)) - range;
            return Math.max(0, distance) * HEURISTIC_TICKS_PER_BLOCK;
        }

        private double eyeDistanceSquared(int nx, int ny, int nz) {
            double dx = nx + 0.5 - (targetX + 0.5);
            double dy = ny + EYE_HEIGHT - (targetY + 0.5);
            double dz = nz + 0.5 - (targetZ + 0.5);
            return dx * dx + dy * dy + dz * dz;
        }
    }

    /** OR-combination; the first satisfied member wins. */
    record Composite(List<Goal> goals) implements Goal {
        public Composite {
            if (goals.isEmpty()) {
                throw new IllegalArgumentException("composite goal must not be empty");
            }
            goals = List.copyOf(goals);
        }

        @Override
        public boolean isSatisfied(int x, int y, int z) {
            for (Goal goal : goals) {
                if (goal.isSatisfied(x, y, z)) {
                    return true;
                }
            }
            return false;
        }

        @Override
        public double heuristic(int x, int y, int z) {
            double best = Double.MAX_VALUE;
            for (Goal goal : goals) {
                best = Math.min(best, goal.heuristic(x, y, z));
            }
            return best;
        }
    }
}
