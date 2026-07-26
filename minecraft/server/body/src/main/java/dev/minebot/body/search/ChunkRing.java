package dev.minebot.body.search;

import java.util.ArrayList;
import java.util.List;

/**
 * Pure chunk-ring geometry for outward search iteration. Ring r is the set of
 * chunk offsets at Chebyshev distance r from the center chunk; iterating rings
 * outward makes result caps distance-correct: once the worst kept match is
 * closer than the nearest possible block of the next ring, no later ring can
 * improve the result.
 */
public final class ChunkRing {
    private ChunkRing() {
    }

    /** Chunk offsets at Chebyshev distance {@code ring}; ring 0 is the center chunk. */
    public static List<int[]> ringOffsets(int ring) {
        if (ring < 0) {
            throw new IllegalArgumentException("ring must be >= 0");
        }
        List<int[]> offsets = new ArrayList<>(ring == 0 ? 1 : 8 * ring);
        if (ring == 0) {
            offsets.add(new int[] {0, 0});
            return offsets;
        }
        for (int dx = -ring; dx <= ring; dx++) {
            offsets.add(new int[] {dx, -ring});
            offsets.add(new int[] {dx, ring});
        }
        for (int dz = -ring + 1; dz <= ring - 1; dz++) {
            offsets.add(new int[] {-ring, dz});
            offsets.add(new int[] {ring, dz});
        }
        return offsets;
    }

    /** Last ring that can still intersect a horizontal block radius around a center inside ring 0. */
    public static int maxRing(int radius) {
        if (radius <= 0) {
            return 0;
        }
        return ((radius - 1) >> 4) + 1;
    }

    /**
     * Lower bound on the squared horizontal block distance from any block in
     * the center chunk to any block in a ring-{@code ring} chunk. Conservative
     * by construction: real distances are never smaller.
     */
    public static long minBlockDistanceSquared(int ring) {
        if (ring <= 0) {
            return 0L;
        }
        long distance = (long) (ring - 1) * 16 + 1;
        return distance * distance;
    }

    /** Whether the chunk's horizontal footprint intersects the search circle. */
    public static boolean chunkIntersectsCircle(int chunkX, int chunkZ, int centerX, int centerZ, long radiusSquared) {
        int minX = chunkX << 4;
        int minZ = chunkZ << 4;
        long dx = clamp(centerX, minX, minX + 15) - centerX;
        long dz = clamp(centerZ, minZ, minZ + 15) - centerZ;
        return dx * dx + dz * dz <= radiusSquared;
    }

    private static int clamp(int value, int min, int max) {
        return Math.max(min, Math.min(value, max));
    }
}
