package dev.minebot.body.nav;

import java.util.ArrayList;
import java.util.List;

/**
 * The v1 walk-first move set: cardinal and diagonal walking, one-block step
 * up, and bounded falls. No break/place edges and no liquid traversal — those
 * arrive in later slices behind governance and recovery contracts. All cost
 * constants live in one place for the later calibration freeze.
 */
public final class MoveGenerator {
    public static final double WALK_COST = 4.6;
    public static final double DIAGONAL_COST = 6.5;
    public static final double STEP_UP_COST = 5.5;
    public static final double FALL_PER_BLOCK_COST = 1.5;
    public static final int MAX_FALL_BLOCKS = 3;

    public record Move(int x, int y, int z, double cost) {
    }

    /** Result of probing one node's neighbors; unloaded touches are honest facts. */
    public record Moves(List<Move> moves, int unloadedTouches) {
    }

    private static final int[][] CARDINALS = {{1, 0}, {-1, 0}, {0, 1}, {0, -1}};
    private static final int[][] DIAGONALS = {{1, 1}, {1, -1}, {-1, 1}, {-1, -1}};

    private final WorldView world;

    public MoveGenerator(WorldView world) {
        this.world = world;
    }

    /** Legal moves from a standing cell (feet at y, floor at y-1). */
    public Moves movesFrom(int x, int y, int z) {
        List<Move> moves = new ArrayList<>(8);
        int[] unloaded = {0};
        for (int[] step : CARDINALS) {
            probeColumn(x, y, z, x + step[0], z + step[1], WALK_COST, false, moves, unloaded);
        }
        for (int[] step : DIAGONALS) {
            // No corner cutting: both adjacent cardinal bodies must be clear.
            if (bodyClear(x + step[0], y, z) && bodyClear(x, y, z + step[1])) {
                probeColumn(x, y, z, x + step[0], z + step[1], DIAGONAL_COST, true, moves, unloaded);
            }
        }
        return new Moves(List.copyOf(moves), unloaded[0]);
    }

    private void probeColumn(
        int startX,
        int y,
        int startZ,
        int nx,
        int nz,
        double baseCost,
        boolean diagonal,
        List<Move> moves,
        int[] unloaded
    ) {
        WorldView.NodeKind bodyLow = world.kindAt(nx, y, nz);
        WorldView.NodeKind bodyHigh = world.kindAt(nx, y + 1, nz);
        if (bodyLow == WorldView.NodeKind.UNLOADED || bodyHigh == WorldView.NodeKind.UNLOADED) {
            unloaded[0]++;
            return;
        }
        if (bodyLow == WorldView.NodeKind.SOLID) {
            // Step up: destination floor at y; body at y+1/y+2; jump clearance
            // above the start cell's head. Diagonal step-ups are not planned.
            if (!diagonal
                && bodyHigh == WorldView.NodeKind.PASSABLE
                && passable(nx, y + 2, nz)
                && passable(startX, y + 2, startZ)) {
                moves.add(new Move(nx, y + 1, nz, STEP_UP_COST));
            }
            return;
        }
        if (bodyLow != WorldView.NodeKind.PASSABLE || bodyHigh != WorldView.NodeKind.PASSABLE) {
            return;
        }
        WorldView.NodeKind floor = world.kindAt(nx, y - 1, nz);
        if (floor == WorldView.NodeKind.UNLOADED) {
            unloaded[0]++;
            return;
        }
        if (floor == WorldView.NodeKind.SOLID) {
            moves.add(new Move(nx, y, nz, baseCost));
            return;
        }
        if (floor != WorldView.NodeKind.PASSABLE || diagonal) {
            // Liquid/hazard floors are not walkable; diagonal falls are skipped.
            return;
        }
        // Bounded fall: find solid ground within MAX_FALL_BLOCKS below.
        for (int drop = 2; drop <= MAX_FALL_BLOCKS + 1; drop++) {
            WorldView.NodeKind below = world.kindAt(nx, y - drop, nz);
            if (below == WorldView.NodeKind.UNLOADED) {
                unloaded[0]++;
                return;
            }
            if (below == WorldView.NodeKind.SOLID) {
                moves.add(new Move(nx, y - drop + 1, nz, baseCost + (drop - 1) * FALL_PER_BLOCK_COST));
                return;
            }
            if (below != WorldView.NodeKind.PASSABLE) {
                return;
            }
        }
    }

    private boolean bodyClear(int x, int y, int z) {
        return passable(x, y, z) && passable(x, y + 1, z);
    }

    private boolean passable(int x, int y, int z) {
        return world.kindAt(x, y, z) == WorldView.NodeKind.PASSABLE;
    }
}
