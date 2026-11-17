package dev.minebot.client.body;

import java.util.ArrayList;
import java.util.List;

/** Small, side-effect-free policy used by the ascend-in-water action. */
public final class SurfaceSwimProgress {
    private SurfaceSwimProgress() {}

    /** Ticks of holding jump without rising after which the way up is taken to be closed. */
    public static final int BLOCKED_TICKS = 40;

    /** Surfacing is complete once the head is out and air is recovering or already full. */
    public static boolean completed(boolean headOutOfWater, int previousAir, int currentAir, int maxAir) {
        return headOutOfWater && (currentAir > previousAir || currentAir >= maxAir);
    }

    /** Holding jump for this long without rising means something is overhead. */
    public static boolean blocked(int ticksWithoutRise) {
        return ticksWithoutRise >= BLOCKED_TICKS;
    }

    /** A swim target must stay in a loaded, vertically valid part of the world. */
    public static boolean allowed(boolean inLava, boolean chunkLoaded, int y, int minY, int maxY) {
        return !inLava && chunkLoaded && y >= minY && y + 1 < maxY;
    }

    public record Cell(int x, int y, int z) {}

    public interface Space {
        boolean water(int x, int y, int z);
        boolean breathable(int x, int y, int z);
    }

    /** Use upstream's actual feet cell, after releasing jump under a ceiling. */
    public static boolean routeStartClear(Space space, Cell feet) {
        return (space.water(feet.x(), feet.y(), feet.z()) || space.breathable(feet.x(), feet.y(), feet.z()))
            && (space.water(feet.x(), feet.y() + 1, feet.z()) || space.breathable(feet.x(), feet.y() + 1, feet.z()));
    }

    /** Find the nearest ring of open water columns, without breaking or placing.
     * The goal is one block below the top water cell: upstream can path through
     * submerged cells, then the ordinary jump key takes the head into the air.
     */
    public static List<Cell> routeTargets(Space space, Cell anchor, int minY,
                                         int maxY, int maxDistance, int firstRing) {
        for (int radius = firstRing; radius <= maxDistance; radius++) {
            List<Cell> targets = new ArrayList<>();
            for (int dx = -radius; dx <= radius; dx++) {
                for (int dz = -radius; dz <= radius; dz++) {
                    if (Math.max(Math.abs(dx), Math.abs(dz)) != radius) continue;
                    int x = anchor.x() + dx, z = anchor.z() + dz;
                    for (int y = Math.max(minY, anchor.y() - 1);
                         y <= Math.min(maxY - 3, anchor.y() + maxDistance); y++) {
                        // A shallow edge is already breathable at this feet
                        // cell; do not require a deep column to escape into.
                        if (space.water(x, y, z) && space.breathable(x, y + 1, z)) {
                            targets.add(new Cell(x, y, z));
                            break;
                        }
                        if (space.water(x, y, z) && space.water(x, y + 1, z)
                                && space.breathable(x, y + 2, z)) {
                            targets.add(new Cell(x, y, z));
                            break;
                        }
                    }
                }
            }
            if (!targets.isEmpty()) return targets;
        }
        return List.of();
    }
}
