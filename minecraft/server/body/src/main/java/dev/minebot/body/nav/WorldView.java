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
        /** Water. Impassable to the v1 walk-first move set. */
        LIQUID,
        /** Lava, fire, and other must-not-touch blocks. */
        HAZARD,
        /** Outside loaded chunks. Impassable and counted as a boundary touch. */
        UNLOADED
    }

    NodeKind kindAt(int x, int y, int z);
}
