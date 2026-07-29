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
    /** Shared with Python's complete interaction stand-domain limit. */
    int MAX_COMPOSITE_MEMBERS = 32;

    boolean isSatisfied(int x, int y, int z);

    /** World-aware terminal truth; geometry-only goals keep their old behavior. */
    default boolean isSatisfied(WorldView world, int x, int y, int z) {
        return isSatisfied(x, y, z);
    }

    /** Whether an anytime partial route may end at this movement node. */
    default boolean acceptsPartialEndpoint(WorldView world, int x, int y, int z) {
        return true;
    }

    /** Whether a node-cap stop may commit movement before a complete route exists. */
    default boolean allowsNodeBudgetPartial() {
        return true;
    }

    /** Maximum physical distance from the final node center before handoff. */
    default double finalReachDistanceLimit() {
        return PathFollower.WAYPOINT_REACH_DISTANCE;
    }

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

    /** One exact dry, supported standing cell. */
    record Stand(int x, int y, int z) implements Goal {
        @Override
        public boolean isSatisfied(int nx, int ny, int nz) {
            return nx == x && ny == y && nz == z;
        }

        @Override
        public boolean isSatisfied(WorldView world, int nx, int ny, int nz) {
            return isSatisfied(nx, ny, nz) && isDryStand(world, nx, ny, nz);
        }

        @Override
        public boolean acceptsPartialEndpoint(WorldView world, int nx, int ny, int nz) {
            return isDryStand(world, nx, ny, nz);
        }

        @Override
        public boolean allowsNodeBudgetPartial() {
            return false;
        }

        @Override
        public double finalReachDistanceLimit() {
            return 0.15;
        }

        @Override
        public double heuristic(int nx, int ny, int nz) {
            double dx = nx - x;
            double dy = ny - y;
            double dz = nz - z;
            return Math.sqrt(dx * dx + dy * dy + dz * dz) * HEURISTIC_TICKS_PER_BLOCK;
        }

        private static boolean isDryStand(WorldView world, int x, int y, int z) {
            return world.kindAt(x, y, z) == WorldView.NodeKind.PASSABLE
                && world.kindAt(x, y + 1, z) == WorldView.NodeKind.PASSABLE
                && world.kindAt(x, y - 1, z) == WorldView.NodeKind.SOLID
                && !world.isBodyPositionHazardous(x, y, z);
        }
    }

    /**
     * A standing cell whose player touch volume contains an item entity.
     * Minecraft checks entities inside the player box inflated by 1 block on
     * X/Z and 0.5 blocks on Y; these bounds conservatively ignore the item's
     * own width so a planned terminal cannot stop outside the real volume.
     */
    record Pickup(double targetX, double targetY, double targetZ) implements Goal {
        public static final double HORIZONTAL_REACH = 1.3;
        public static final double BELOW_FEET_REACH = 0.5;
        public static final double ABOVE_FEET_REACH = 2.3;
        private static final double FINAL_POSITION_MARGIN = 0.15;
        private static final double PLANNED_HORIZONTAL_REACH =
            HORIZONTAL_REACH - FINAL_POSITION_MARGIN;
        private static final double PLANNED_BELOW_FEET_REACH =
            BELOW_FEET_REACH - FINAL_POSITION_MARGIN;
        private static final double PLANNED_ABOVE_FEET_REACH =
            ABOVE_FEET_REACH - FINAL_POSITION_MARGIN;

        @Override
        public boolean isSatisfied(int nx, int ny, int nz) {
            double centerX = nx + 0.5;
            double centerZ = nz + 0.5;
            return Math.abs(centerX - targetX) <= PLANNED_HORIZONTAL_REACH
                && Math.abs(centerZ - targetZ) <= PLANNED_HORIZONTAL_REACH
                && targetY >= ny - PLANNED_BELOW_FEET_REACH
                && targetY <= ny + PLANNED_ABOVE_FEET_REACH;
        }

        @Override
        public boolean isSatisfied(WorldView world, int nx, int ny, int nz) {
            return isSatisfied(nx, ny, nz)
                && isOccupiable(world.kindAt(nx, ny, nz))
                && isOccupiable(world.kindAt(nx, ny + 1, nz));
        }

        @Override
        public boolean acceptsPartialEndpoint(WorldView world, int nx, int ny, int nz) {
            return isOccupiable(world.kindAt(nx, ny, nz))
                && isOccupiable(world.kindAt(nx, ny + 1, nz));
        }

        @Override
        public boolean allowsNodeBudgetPartial() {
            return false;
        }

        @Override
        public double heuristic(int nx, int ny, int nz) {
            double dx = Math.max(
                0.0,
                Math.abs(nx + 0.5 - targetX) - PLANNED_HORIZONTAL_REACH
            );
            double dz = Math.max(
                0.0,
                Math.abs(nz + 0.5 - targetZ) - PLANNED_HORIZONTAL_REACH
            );
            double dy;
            if (targetY < ny - PLANNED_BELOW_FEET_REACH) {
                dy = ny - PLANNED_BELOW_FEET_REACH - targetY;
            } else if (targetY > ny + PLANNED_ABOVE_FEET_REACH) {
                dy = targetY - (ny + PLANNED_ABOVE_FEET_REACH);
            } else {
                dy = 0.0;
            }
            return Math.sqrt(dx * dx + dy * dy + dz * dz) * HEURISTIC_TICKS_PER_BLOCK;
        }

        private static boolean isOccupiable(WorldView.NodeKind kind) {
            return kind == WorldView.NodeKind.PASSABLE
                || kind == WorldView.NodeKind.LIQUID
                || kind == WorldView.NodeKind.CLIMBABLE;
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
     * A dry supported standing cell within interaction range and with a clear
     * voxel line to one target face. Execution still rechecks exact live
     * legality before mutation.
     */
    record Interact(int targetX, int targetY, int targetZ, double range) implements Goal {
        public static final double MINE_RANGE = 4.5;
        private static final double EYE_HEIGHT = 1.62;

        @Override
        public boolean isSatisfied(int nx, int ny, int nz) {
            return eyeDistanceSquared(nx, ny, nz) <= range * range;
        }

        @Override
        public boolean isSatisfied(WorldView world, int nx, int ny, int nz) {
            return isSatisfied(nx, ny, nz)
                && isDryStand(world, nx, ny, nz)
                && hasInteractionLine(world, nx, ny, nz);
        }

        @Override
        public boolean acceptsPartialEndpoint(WorldView world, int nx, int ny, int nz) {
            return isDryStand(world, nx, ny, nz);
        }

        @Override
        public boolean allowsNodeBudgetPartial() {
            // A distance-only partial toward a known block can end underneath
            // an occluding floor. Wait for a complete executable route instead.
            return false;
        }

        @Override
        public double finalReachDistanceLimit() {
            return 0.1;
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

        private static boolean isDryStand(WorldView world, int x, int y, int z) {
            return world.kindAt(x, y, z) == WorldView.NodeKind.PASSABLE
                && world.kindAt(x, y + 1, z) == WorldView.NodeKind.PASSABLE
                && world.kindAt(x, y - 1, z) == WorldView.NodeKind.SOLID;
        }

        private boolean hasInteractionLine(WorldView world, int x, int y, int z) {
            double eyeX = x + 0.5;
            double eyeY = y + EYE_HEIGHT;
            double eyeZ = z + 0.5;
            double[][] targetPoints = {
                {targetX + 0.01, targetY + 0.5, targetZ + 0.5},
                {targetX + 0.99, targetY + 0.5, targetZ + 0.5},
                {targetX + 0.5, targetY + 0.01, targetZ + 0.5},
                {targetX + 0.5, targetY + 0.99, targetZ + 0.5},
                {targetX + 0.5, targetY + 0.5, targetZ + 0.01},
                {targetX + 0.5, targetY + 0.5, targetZ + 0.99},
            };
            for (double[] target : targetPoints) {
                if (rayClear(world, eyeX, eyeY, eyeZ, target[0], target[1], target[2])) {
                    return true;
                }
            }
            return false;
        }

        private boolean rayClear(
            WorldView world,
            double fromX,
            double fromY,
            double fromZ,
            double toX,
            double toY,
            double toZ
        ) {
            double dx = toX - fromX;
            double dy = toY - fromY;
            double dz = toZ - fromZ;
            int samples = Math.max(1, (int) Math.ceil(Math.sqrt(dx * dx + dy * dy + dz * dz) * 16.0));
            for (int index = 1; index < samples; index++) {
                double fraction = index / (double) samples;
                int sampleX = (int) Math.floor(fromX + dx * fraction);
                int sampleY = (int) Math.floor(fromY + dy * fraction);
                int sampleZ = (int) Math.floor(fromZ + dz * fraction);
                if (sampleX == targetX && sampleY == targetY && sampleZ == targetZ) {
                    return true;
                }
                WorldView.NodeKind kind = world.kindAt(sampleX, sampleY, sampleZ);
                if (kind == WorldView.NodeKind.SOLID || kind == WorldView.NodeKind.UNLOADED) {
                    return false;
                }
            }
            return true;
        }
    }

    /** OR-combination; the first satisfied member wins. */
    record Composite(List<Goal> goals) implements Goal {
        public Composite {
            if (goals.isEmpty()) {
                throw new IllegalArgumentException("composite goal must not be empty");
            }
            if (goals.size() > MAX_COMPOSITE_MEMBERS) {
                throw new IllegalArgumentException(
                    "composite goal supports at most " + MAX_COMPOSITE_MEMBERS + " members"
                );
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
        public boolean isSatisfied(WorldView world, int x, int y, int z) {
            for (Goal goal : goals) {
                if (goal.isSatisfied(world, x, y, z)) {
                    return true;
                }
            }
            return false;
        }

        @Override
        public boolean acceptsPartialEndpoint(WorldView world, int x, int y, int z) {
            for (Goal goal : goals) {
                if (goal.acceptsPartialEndpoint(world, x, y, z)) {
                    return true;
                }
            }
            return false;
        }

        @Override
        public boolean allowsNodeBudgetPartial() {
            for (Goal goal : goals) {
                if (!goal.allowsNodeBudgetPartial()) {
                    return false;
                }
            }
            return true;
        }

        @Override
        public double finalReachDistanceLimit() {
            double tightest = PathFollower.WAYPOINT_REACH_DISTANCE;
            for (Goal goal : goals) {
                tightest = Math.min(tightest, goal.finalReachDistanceLimit());
            }
            return tightest;
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
