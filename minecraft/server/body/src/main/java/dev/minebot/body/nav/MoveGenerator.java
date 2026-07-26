package dev.minebot.body.nav;

import java.util.ArrayList;
import java.util.List;

/**
 * The v1 move set: cardinal and diagonal walking, one-block step up, bounded
 * falls, and — added in the M3 recovery slice — water traversal and water-
 * column vertical escape. Swimming edges cost more than walking so land is
 * preferred, but water is no longer a wall: this is what turns the golden-
 * spawn water-gated {@code no_path} into a reachable route.
 *
 * Still out of scope by contract (each needs break/place edges under
 * governance, a separate slice): dig-shaft and pillar-up vertical escape, and
 * ladder/vine climbing. Lava and fire stay deny-by-default — the body is never
 * moved into or over a hazard with nothing to stand on.
 *
 * All cost constants live in one place for the later calibration freeze.
 */
public final class MoveGenerator {
    public static final double WALK_COST = 4.6;
    public static final double DIAGONAL_COST = 6.5;
    public static final double STEP_UP_COST = 5.5;
    public static final double FALL_PER_BLOCK_COST = 1.5;
    public static final int MAX_FALL_BLOCKS = 3;
    public static final double SWIM_COST = 9.0;
    public static final double SWIM_DIAGONAL_COST = 12.7;
    public static final double SWIM_UP_COST = 11.0;
    public static final double SWIM_DOWN_COST = 8.0;
    /** Falling into water is safe, so a water landing tolerates a deep drop. */
    public static final int MAX_WATER_FALL_BLOCKS = 20;

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

    /** Legal moves from a standing or floating cell (feet at y, head at y+1). */
    public Moves movesFrom(int x, int y, int z) {
        List<Move> moves = new ArrayList<>(10);
        int[] unloaded = {0};
        for (int[] step : CARDINALS) {
            probeColumn(x, y, z, x + step[0], z + step[1], WALK_COST, SWIM_COST, false, moves, unloaded);
        }
        for (int[] step : DIAGONALS) {
            // No corner cutting: both adjacent cardinal bodies must be clear.
            if (bodyClear(x + step[0], y, z) && bodyClear(x, y, z + step[1])) {
                probeColumn(x, y, z, x + step[0], z + step[1], DIAGONAL_COST, SWIM_DIAGONAL_COST, true, moves, unloaded);
            }
        }
        addVerticalSwim(x, y, z, moves);
        return new Moves(List.copyOf(moves), unloaded[0]);
    }

    private void probeColumn(
        int startX,
        int y,
        int startZ,
        int nx,
        int nz,
        double baseCost,
        double swimCost,
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
            // Step up (or climb out of water onto land): destination floor at
            // y, body at y+1/y+2, head clearance above the start cell.
            if (!diagonal
                && occupiable(bodyHigh)
                && occupiable(world.kindAt(nx, y + 2, nz))
                && occupiable(world.kindAt(startX, y + 2, startZ))) {
                moves.add(new Move(nx, y + 1, nz, STEP_UP_COST));
            }
            return;
        }
        // Never move the body into a hazard or a headless solid.
        if (bodyLow == WorldView.NodeKind.HAZARD || bodyHigh == WorldView.NodeKind.HAZARD) {
            return;
        }
        if (bodyHigh == WorldView.NodeKind.SOLID) {
            return;
        }
        if (bodyLow == WorldView.NodeKind.LIQUID) {
            // Swim into water at the same height.
            moves.add(new Move(nx, y, nz, swimCost));
            return;
        }
        // bodyLow is PASSABLE (air): decide how the body settles in this column.
        WorldView.NodeKind floor = world.kindAt(nx, y - 1, nz);
        if (floor == WorldView.NodeKind.UNLOADED) {
            unloaded[0]++;
            return;
        }
        if (floor == WorldView.NodeKind.SOLID) {
            moves.add(new Move(nx, y, nz, baseCost));
            return;
        }
        if (floor == WorldView.NodeKind.LIQUID) {
            // Air above a water surface: drop into the water and float there.
            moves.add(new Move(nx, y - 1, nz, swimCost));
            return;
        }
        if (floor == WorldView.NodeKind.HAZARD || diagonal) {
            // Falling onto lava/fire is denied; diagonal falls are not planned.
            return;
        }
        // Bounded fall: solid ground within MAX_FALL_BLOCKS, or water (a safe
        // landing) within the deeper MAX_WATER_FALL_BLOCKS.
        for (int drop = 2; drop <= MAX_WATER_FALL_BLOCKS + 1; drop++) {
            WorldView.NodeKind below = world.kindAt(nx, y - drop, nz);
            if (below == WorldView.NodeKind.UNLOADED) {
                unloaded[0]++;
                return;
            }
            if (below == WorldView.NodeKind.LIQUID) {
                moves.add(new Move(nx, y - drop + 1, nz, baseCost + SWIM_COST));
                return;
            }
            if (below == WorldView.NodeKind.SOLID) {
                if (drop <= MAX_FALL_BLOCKS + 1) {
                    moves.add(new Move(nx, y - drop + 1, nz, baseCost + (drop - 1) * FALL_PER_BLOCK_COST));
                }
                return;
            }
            if (below == WorldView.NodeKind.HAZARD) {
                return;
            }
            // PASSABLE: keep falling.
        }
    }

    /** Water-column vertical escape: rise or sink while the body is in water. */
    private void addVerticalSwim(int x, int y, int z, List<Move> moves) {
        if (world.kindAt(x, y, z) != WorldView.NodeKind.LIQUID) {
            return;
        }
        // Swim up: the body rises to occupy (y+1, y+2). Water or air above both.
        if (occupiable(world.kindAt(x, y + 1, z)) && occupiable(world.kindAt(x, y + 2, z))) {
            moves.add(new Move(x, y + 1, z, SWIM_UP_COST));
        }
        // Sink one cell if the cell below is still water.
        if (world.kindAt(x, y - 1, z) == WorldView.NodeKind.LIQUID) {
            moves.add(new Move(x, y - 1, z, SWIM_DOWN_COST));
        }
    }

    private boolean bodyClear(int x, int y, int z) {
        return occupiable(world.kindAt(x, y, z)) && occupiable(world.kindAt(x, y + 1, z));
    }

    private static boolean occupiable(WorldView.NodeKind kind) {
        return kind == WorldView.NodeKind.PASSABLE || kind == WorldView.NodeKind.LIQUID;
    }
}
