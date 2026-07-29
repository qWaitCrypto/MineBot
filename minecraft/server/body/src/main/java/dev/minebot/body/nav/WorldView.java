package dev.minebot.body.nav;

/**
 * The planner's only window onto the world, kept implementation-neutral so
 * the navigation core is fully unit-testable on synthetic worlds. Unknown,
 * unloaded, and liquid states are first-class kinds — never softened into
 * "air".
 */
public interface WorldView {
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

    /**
     * Whether placing the player's feet at this node enters a server-observed
     * danger envelope even though the node itself may be passable. Live world
     * views use this for hazards such as adjacent lava; synthetic worlds are
     * safe by default unless a test marks an envelope explicitly.
     */
    default boolean isBodyPositionHazardous(int x, int y, int z) {
        return false;
    }
}
