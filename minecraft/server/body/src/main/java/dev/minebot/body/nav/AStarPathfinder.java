package dev.minebot.body.nav;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.PriorityQueue;
import java.util.function.LongSupplier;

/**
 * Resumable anytime A* over a {@link WorldView}. One search may span several
 * calls to {@link #step(int)} — the tick-sliced planning contract — and a
 * budget or boundary stop returns the best-so-far partial path with an honest
 * classification instead of a bare failure.
 */
public final class AStarPathfinder {
    public static final int DEFAULT_TOTAL_NODE_CAP = 50_000;
    public static final int DEFAULT_UNLOADED_TOUCH_CAP = 50;
    public static final double MIN_PARTIAL_PROGRESS_BLOCKS = 5.0;

    public record Waypoint(int x, int y, int z, double feetY, boolean dry) {
        public Waypoint(int x, int y, int z) {
            this(x, y, z, y, false);
        }

        public Waypoint(int x, int y, int z, double feetY) {
            this(x, y, z, feetY, true);
        }
    }

    public enum Outcome {
        IN_PROGRESS,
        COMPLETE,
        PARTIAL,
        NO_PATH
    }

    public record Result(
        Outcome outcome,
        List<Waypoint> path,
        String reason,
        int expandedNodes,
        int unloadedTouches
    ) {
    }

    private record Open(long key, double f, double g) {
    }

    private final WorldView world;
    private final MoveGenerator moves;
    private final Goal goal;
    private final int startX;
    private final int startY;
    private final int startZ;
    private final int totalNodeCap;
    private final int unloadedTouchCap;
    private final boolean directReturnRequired;
    private final boolean allowGoalStaging;

    private final PriorityQueue<Open> open = new PriorityQueue<>((a, b) -> Double.compare(a.f, b.f));
    private final Map<Long, Double> gScore = new HashMap<>();
    private final Map<Long, Long> cameFrom = new HashMap<>();
    private int expanded;
    private int unloadedTouches;
    private long bestSoFarKey;
    private double bestSoFarH = Double.MAX_VALUE;
    private boolean hasBestSoFar;
    private Result finished;

    public AStarPathfinder(WorldView world, Goal goal, int startX, int startY, int startZ) {
        this(
            world,
            goal,
            startX,
            startY,
            startZ,
            DEFAULT_TOTAL_NODE_CAP,
            DEFAULT_UNLOADED_TOUCH_CAP,
            false,
            false
        );
    }

    public AStarPathfinder(
        WorldView world,
        Goal goal,
        int startX,
        int startY,
        int startZ,
        boolean directReturnRequired
    ) {
        this(
            world,
            goal,
            startX,
            startY,
            startZ,
            DEFAULT_TOTAL_NODE_CAP,
            DEFAULT_UNLOADED_TOUCH_CAP,
            directReturnRequired,
            false
        );
    }

    /** Allow a safe best-so-far segment only when a bounded search is interrupted. */
    public AStarPathfinder(
        WorldView world,
        Goal goal,
        int startX,
        int startY,
        int startZ,
        boolean directReturnRequired,
        boolean allowGoalStaging
    ) {
        this(
            world,
            goal,
            startX,
            startY,
            startZ,
            DEFAULT_TOTAL_NODE_CAP,
            DEFAULT_UNLOADED_TOUCH_CAP,
            directReturnRequired,
            allowGoalStaging
        );
    }

    public AStarPathfinder(
        WorldView world,
        Goal goal,
        int startX,
        int startY,
        int startZ,
        int totalNodeCap,
        int unloadedTouchCap
    ) {
        this(world, goal, startX, startY, startZ, totalNodeCap, unloadedTouchCap, false, false);
    }

    public AStarPathfinder(
        WorldView world,
        Goal goal,
        int startX,
        int startY,
        int startZ,
        int totalNodeCap,
        int unloadedTouchCap,
        boolean directReturnRequired
    ) {
        this(
            world,
            goal,
            startX,
            startY,
            startZ,
            totalNodeCap,
            unloadedTouchCap,
            directReturnRequired,
            false
        );
    }

    public AStarPathfinder(
        WorldView world,
        Goal goal,
        int startX,
        int startY,
        int startZ,
        int totalNodeCap,
        int unloadedTouchCap,
        boolean directReturnRequired,
        boolean allowGoalStaging
    ) {
        this(
            world,
            goal,
            startX,
            startY,
            startZ,
            totalNodeCap,
            unloadedTouchCap,
            directReturnRequired,
            allowGoalStaging,
            0,
            MoveGenerator.Policy.defaults()
        );
    }

    /** Construct a search with the finite profile supplied by the Body seam. */
    public AStarPathfinder(
        WorldView world,
        Goal goal,
        int startX,
        int startY,
        int startZ,
        int totalNodeCap,
        int unloadedTouchCap,
        boolean directReturnRequired,
        boolean allowGoalStaging,
        int gridRadius,
        MoveGenerator.Policy movePolicy
    ) {
        this.world = gridRadius > 0
            ? new RadiusLimitedWorldView(world, startX, startZ, gridRadius)
            : world;
        this.moves = new MoveGenerator(this.world, movePolicy);
        this.goal = goal;
        this.startX = startX;
        this.startY = startY;
        this.startZ = startZ;
        this.totalNodeCap = totalNodeCap;
        this.unloadedTouchCap = unloadedTouchCap;
        this.directReturnRequired = directReturnRequired;
        this.allowGoalStaging = allowGoalStaging;
        long startKey = pack(startX, startY, startZ);
        gScore.put(startKey, 0.0);
        open.add(new Open(startKey, goal.heuristic(this.world, startX, startY, startZ), 0.0));
        bestSoFarKey = startKey;
        trackBestSoFar(startKey, startX, startY, startZ);
    }

    private static final class RadiusLimitedWorldView implements WorldView {
        private final WorldView delegate;
        private final int originX;
        private final int originZ;
        private final int radius;

        private RadiusLimitedWorldView(WorldView delegate, int originX, int originZ, int radius) {
            this.delegate = delegate;
            this.originX = originX;
            this.originZ = originZ;
            this.radius = radius;
        }

        @Override
        public NodeKind kindAt(int x, int y, int z) {
            if (Math.abs(x - originX) > radius || Math.abs(z - originZ) > radius) {
                return NodeKind.UNLOADED;
            }
            return delegate.kindAt(x, y, z);
        }

        @Override
        public double standingHeight(int x, int y, int z) {
            return Math.abs(x - originX) > radius || Math.abs(z - originZ) > radius
                ? Double.NaN : delegate.standingHeight(x, y, z);
        }

        @Override
        public boolean isDryVolumeClear(double minX, double minY, double minZ,
                                        double maxX, double maxY, double maxZ) {
            return minX >= originX - radius && maxX <= originX + radius + 1
                && minZ >= originZ - radius && maxZ <= originZ + radius + 1
                && delegate.isDryVolumeClear(minX, minY, minZ, maxX, maxY, maxZ);
        }

        @Override
        public boolean isBodyPositionHazardous(int x, int y, int z) {
            return delegate.isBodyPositionHazardous(x, y, z);
        }
    }

    /** Expands up to {@code nodeBudget} nodes; call again while IN_PROGRESS. */
    public Result step(int nodeBudget) {
        return step(nodeBudget, Long.MAX_VALUE);
    }

    /** Cooperative wall-time slice: one expansion is indivisible. */
    public Result step(int nodeBudget, long timeBudgetNanos) {
        return step(nodeBudget, timeBudgetNanos, System::nanoTime);
    }

    Result step(int nodeBudget, long timeBudgetNanos, LongSupplier clock) {
        if (timeBudgetNanos <= 0) {
            throw new IllegalArgumentException("planning time slice must be positive");
        }
        if (finished != null) {
            return finished;
        }
        long started = clock.getAsLong();
        int stepExpanded = 0;
        while (stepExpanded < nodeBudget) {
            if (stepExpanded > 0 && clock.getAsLong() - started >= timeBudgetNanos) {
                break;
            }
            Open current = open.poll();
            if (current == null) {
                return finish(exhaustedOutcome());
            }
            Double bestG = gScore.get(current.key());
            if (bestG == null || current.g() > bestG) {
                continue;
            }
            int x = unpackX(current.key());
            int y = unpackY(current.key());
            int z = unpackZ(current.key());
            if (goal.isSatisfied(world, x, y, z)) {
                return finish(new Result(Outcome.COMPLETE, reconstruct(current.key()), "goal_satisfied", expanded, unloadedTouches));
            }
            expanded++;
            stepExpanded++;
            trackBestSoFar(current.key(), x, y, z);
            MoveGenerator.Moves neighbors = moves.movesFrom(x, y, z);
            unloadedTouches += neighbors.unloadedTouches();
            for (MoveGenerator.Move move : neighbors.moves()) {
                if (directReturnRequired && !moves.hasDirectReturnMove(x, y, z, move)) {
                    continue;
                }
                long key = pack(move.x(), move.y(), move.z());
                double tentative = current.g() + move.cost();
                Double known = gScore.get(key);
                if (known != null && known <= tentative) {
                    continue;
                }
                gScore.put(key, tentative);
                cameFrom.put(key, current.key());
                open.add(new Open(key, tentative + goal.heuristic(world, move.x(), move.y(), move.z()), tentative));
            }
            if (expanded >= totalNodeCap) {
                return finish(boundedOutcome("node_budget"));
            }
            if (unloadedTouches >= unloadedTouchCap) {
                return finish(boundedOutcome("unloaded_boundary"));
            }
        }
        return new Result(Outcome.IN_PROGRESS, List.of(), "planning", expanded, unloadedTouches);
    }

    private void trackBestSoFar(long key, int x, int y, int z) {
        if (!goal.acceptsPartialEndpoint(world, x, y, z)) {
            return;
        }
        double h = goal.heuristic(world, x, y, z);
        if (h < bestSoFarH) {
            bestSoFarH = h;
            bestSoFarKey = key;
            hasBestSoFar = true;
        }
    }

    /** Open set exhausted: the reachable region is fully explored. */
    private Result exhaustedOutcome() {
        String reason = unloadedTouches > 0 ? "no_path_incomplete_coverage" : "no_path";
        return new Result(Outcome.NO_PATH, List.of(), reason, expanded, unloadedTouches);
    }

    /** Budget/boundary stop: usable partial progress or an honest no-path. */
    private Result boundedOutcome(String reason) {
        if ("node_budget".equals(reason)
            && !goal.allowsNodeBudgetPartial()
            && !allowGoalStaging) {
            return new Result(
                Outcome.NO_PATH,
                List.of(),
                "node_budget_without_complete_route",
                expanded,
                unloadedTouches
            );
        }
        return partialOrNoPath(reason);
    }

    private Result partialOrNoPath(String reason) {
        if (!hasBestSoFar) {
            return new Result(
                Outcome.NO_PATH,
                List.of(),
                reason + "_without_stable_endpoint",
                expanded,
                unloadedTouches
            );
        }
        List<Waypoint> partial = reconstruct(bestSoFarKey);
        Waypoint last = partial.get(partial.size() - 1);
        double dx = last.x() - startX;
        double dz = last.z() - startZ;
        if (Math.sqrt(dx * dx + dz * dz) >= MIN_PARTIAL_PROGRESS_BLOCKS) {
            return new Result(Outcome.PARTIAL, partial, reason, expanded, unloadedTouches);
        }
        return new Result(Outcome.NO_PATH, List.of(), reason + "_without_progress", expanded, unloadedTouches);
    }

    private Result finish(Result result) {
        finished = result;
        open.clear();
        return result;
    }

    private List<Waypoint> reconstruct(long key) {
        List<Waypoint> path = new ArrayList<>();
        Long cursor = key;
        while (cursor != null) {
            int x = unpackX(cursor);
            int y = unpackY(cursor);
            int z = unpackZ(cursor);
            double height = world.standingHeight(x, y, z);
            path.add(Double.isFinite(height) ? new Waypoint(x, y, z, height) : new Waypoint(x, y, z));
            cursor = cameFrom.get(cursor);
        }
        List<Waypoint> forward = new ArrayList<>(path.size());
        for (int i = path.size() - 1; i >= 0; i--) {
            forward.add(path.get(i));
        }
        return List.copyOf(forward);
    }

    // 26-bit x, 26-bit z, 12-bit y with sign extension on unpack.
    private static long pack(int x, int y, int z) {
        return ((long) (x & 0x3FFFFFF) << 38) | ((long) (z & 0x3FFFFFF) << 12) | (y & 0xFFF);
    }

    private static int unpackX(long key) {
        return ((int) (key >>> 38) << 6) >> 6;
    }

    private static int unpackZ(long key) {
        return ((int) ((key >>> 12) & 0x3FFFFFF) << 6) >> 6;
    }

    private static int unpackY(long key) {
        return ((int) (key & 0xFFF) << 20) >> 20;
    }
}
