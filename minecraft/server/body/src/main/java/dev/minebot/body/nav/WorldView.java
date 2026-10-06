package dev.minebot.body.nav;

/**
 * The planner's only window onto the world, kept implementation-neutral so
 * the navigation core is fully unit-testable on synthetic worlds. Unknown,
 * unloaded, and liquid states are first-class kinds — never softened into
 * "air".
 */
public interface WorldView {
    double STABLE_SUPPORT_DISTANCE = 0.08;
    enum NodeKind {
        /** No collision: air, grass, flowers. A body cell must be PASSABLE. */
        PASSABLE,
        /** Collides and can be stood on. A floor cell must be SOLID. */
        SOLID,
        /** Water. A body cell may occupy it (swimming); costed above walking. */
        LIQUID,
        /** Ladders, vines, scaffolding: a body cell that also grants vertical climb. */
        CLIMBABLE,
        /** Lava, fire, and other must-not-touch blocks. */
        HAZARD,
        /** Outside loaded chunks. Impassable and counted as a boundary touch. */
        UNLOADED
    }

    NodeKind kindAt(int x, int y, int z);

    /** Centered dry standing height in this feet voxel; NaN means no standing pose. */
    default double standingHeight(int x, int y, int z) {
        return isStableDryPosition(x + 0.5, y, z + 0.5) ? y : Double.NaN;
    }

    /** Conservative clearance for a rise, horizontal traverse, then landing. */
    default boolean isDryTransitionClear(int x, double y, int z, int nx, double ny, int nz) {
        double travelY = Math.max(y, ny);
        for (int bx = Math.min(x, nx); bx <= Math.max(x, nx); bx++) {
            for (int bz = Math.min(z, nz); bz <= Math.max(z, nz); bz++) {
                for (int by = (int) Math.floor(Math.min(y, ny)); by <= Math.floor(travelY); by++) {
                    if (isBodyPositionHazardous(bx, by, bz)) {
                        return false;
                    }
                }
            }
        }
        return isDryVolumeClear(x + 0.2, y, z + 0.2, x + 0.8, travelY + 1.8, z + 0.8)
            && isDryVolumeClear(Math.min(x, nx) + 0.2, travelY, Math.min(z, nz) + 0.2,
                Math.max(x, nx) + 0.8, travelY + 1.8, Math.max(z, nz) + 0.8)
            && isDryVolumeClear(nx + 0.2, ny, nz + 0.2, nx + 0.8, travelY + 1.8, nz + 0.8);
    }

    /** Full-cell fallback for synthetic worlds; live worlds use native shapes. */
    default boolean isDryVolumeClear(double minX, double minY, double minZ,
                                     double maxX, double maxY, double maxZ) {
        for (int x = (int) Math.floor(minX); x < Math.ceil(maxX); x++) {
            for (int y = (int) Math.floor(minY); y < Math.ceil(maxY); y++) {
                for (int z = (int) Math.floor(minZ); z < Math.ceil(maxZ); z++) {
                    if (kindAt(x, y, z) != NodeKind.PASSABLE) {
                        return false;
                    }
                }
            }
        }
        return true;
    }

    /** Physical support at the observed position, not an integer-height guess. */
    default boolean isStableDryPosition(double x, double y, double z) {
        if (!Double.isFinite(x) || !Double.isFinite(y) || !Double.isFinite(z)) {
            return false;
        }
        int bx = (int) Math.floor(x);
        int by = NavigateExecutor.PositionSource.Position.feetBlockY(y);
        int bz = (int) Math.floor(z);
        return Math.abs(y - by) <= STABLE_SUPPORT_DISTANCE
            && kindAt(bx, by, bz) == NodeKind.PASSABLE
            && kindAt(bx, by + 1, bz) == NodeKind.PASSABLE
            && kindAt(bx, by - 1, bz) == NodeKind.SOLID
            && !isBodyPositionHazardous(bx, by, bz);
    }

    /**
     * Whether placing the player's feet at this node enters a server-observed
     * danger envelope even though the node itself may be passable. Live world
     * views use this for hazards such as adjacent lava; synthetic worlds are
     * safe by default unless a test marks an envelope explicitly.
     */
    default boolean isBodyPositionHazardous(int x, int y, int z) {
        return false;
    }

    /**
     * Whether removing this solid body-volume cell can expose a hazard before
     * the player occupies the planned destination.  This is intentionally
     * separate from {@link #isBodyPositionHazardous}: ordinary movement may be
     * safe beside a sealed fluid pocket while breaking the seal is not.
     */
    default boolean isBlockClearanceHazardous(int x, int y, int z) {
        return false;
    }
}
