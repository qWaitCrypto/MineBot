package dev.minebot.body.nav;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.PriorityQueue;

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

    public record Waypoint(int x, int y, int z) {
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

    private final MoveGenerator moves;
    private final Goal goal;
    private final int startX;
    private final int startY;
    private final int startZ;
    private final int totalNodeCap;
    private final int unloadedTouchCap;

    private final PriorityQueue<Open> open = new PriorityQueue<>((a, b) -> Double.compare(a.f, b.f));
    private final Map<Long, Double> gScore = new HashMap<>();
    private final Map<Long, Long> cameFrom = new HashMap<>();
    private int expanded;
    private int unloadedTouches;
    private long bestSoFarKey;
    private double bestSoFarH = Double.MAX_VALUE;
    private Result finished;

    public AStarPathfinder(WorldView world, Goal goal, int startX, int startY, int startZ) {
        this(world, goal, startX, startY, startZ, DEFAULT_TOTAL_NODE_CAP, DEFAULT_UNLOADED_TOUCH_CAP);
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
        this.moves = new MoveGenerator(world);
        this.goal = goal;
        this.startX = startX;
        this.startY = startY;
        this.startZ = startZ;
        this.totalNodeCap = totalNodeCap;
        this.unloadedTouchCap = unloadedTouchCap;
        long startKey = pack(startX, startY, startZ);
        gScore.put(startKey, 0.0);
        open.add(new Open(startKey, goal.heuristic(startX, startY, startZ), 0.0));
        bestSoFarKey = startKey;
        bestSoFarH = goal.heuristic(startX, startY, startZ);
    }

    /** Expands up to {@code nodeBudget} nodes; call again while IN_PROGRESS. */
    public Result step(int nodeBudget) {
        if (finished != null) {
            return finished;
        }
        int stepExpanded = 0;
        while (stepExpanded < nodeBudget) {
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
            if (goal.isSatisfied(x, y, z)) {
                return finish(new Result(Outcome.COMPLETE, reconstruct(current.key()), "goal_satisfied", expanded, unloadedTouches));
            }
            expanded++;
            stepExpanded++;
            trackBestSoFar(current.key(), x, y, z);
            MoveGenerator.Moves neighbors = moves.movesFrom(x, y, z);
            unloadedTouches += neighbors.unloadedTouches();
            for (MoveGenerator.Move move : neighbors.moves()) {
                long key = pack(move.x(), move.y(), move.z());
                double tentative = current.g() + move.cost();
                Double known = gScore.get(key);
                if (known != null && known <= tentative) {
                    continue;
                }
                gScore.put(key, tentative);
                cameFrom.put(key, current.key());
                open.add(new Open(key, tentative + goal.heuristic(move.x(), move.y(), move.z()), tentative));
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
        double h = goal.heuristic(x, y, z);
        if (h < bestSoFarH) {
            bestSoFarH = h;
            bestSoFarKey = key;
        }
    }

    /** Open set exhausted: the reachable region is fully explored. */
    private Result exhaustedOutcome() {
        String reason = unloadedTouches > 0 ? "no_path_incomplete_coverage" : "no_path";
        return new Result(Outcome.NO_PATH, List.of(), reason, expanded, unloadedTouches);
    }

    /** Budget/boundary stop: usable partial progress or an honest no-path. */
    private Result boundedOutcome(String reason) {
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
            path.add(new Waypoint(unpackX(cursor), unpackY(cursor), unpackZ(cursor)));
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
