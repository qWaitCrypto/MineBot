package dev.minebot.body.search;

import org.junit.jupiter.api.Test;

import java.util.HashSet;
import java.util.List;
import java.util.Set;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

final class ChunkRingTest {
    @Test
    void ringsPartitionTheFullSquareWithoutOverlap() {
        Set<Long> seen = new HashSet<>();
        int maxRing = 3;
        for (int ring = 0; ring <= maxRing; ring++) {
            for (int[] offset : ChunkRing.ringOffsets(ring)) {
                int chebyshev = Math.max(Math.abs(offset[0]), Math.abs(offset[1]));
                assertEquals(ring, chebyshev, "offset must sit exactly on its ring");
                assertTrue(seen.add(((long) offset[0] << 32) | (offset[1] & 0xFFFFFFFFL)), "offsets must not repeat");
            }
        }
        int side = 2 * maxRing + 1;
        assertEquals(side * side, seen.size(), "rings 0..r must tile the full square");
    }

    @Test
    void ringZeroIsExactlyTheCenterChunk() {
        List<int[]> center = ChunkRing.ringOffsets(0);
        assertEquals(1, center.size());
        assertEquals(0, center.get(0)[0]);
        assertEquals(0, center.get(0)[1]);
    }

    @Test
    void maxRingCoversTheBlockRadius() {
        assertEquals(0, ChunkRing.maxRing(0));
        assertEquals(1, ChunkRing.maxRing(1));
        assertEquals(1, ChunkRing.maxRing(16));
        assertEquals(2, ChunkRing.maxRing(17));
        assertEquals(8, ChunkRing.maxRing(128));
    }

    @Test
    void minBlockDistanceIsAConservativeLowerBound() {
        assertEquals(0, ChunkRing.minBlockDistanceSquared(0));
        // Adjacent ring: a center block on its chunk edge is 1 block away.
        assertEquals(1, ChunkRing.minBlockDistanceSquared(1));
        // Ring 2: at least one full chunk (16 blocks) lies between.
        assertEquals(17L * 17L, ChunkRing.minBlockDistanceSquared(2));
        assertTrue(ChunkRing.minBlockDistanceSquared(3) > ChunkRing.minBlockDistanceSquared(2));
    }

    @Test
    void circleIntersectionMatchesNearestPointGeometry() {
        // Center block at (0, 0): chunk (0,0) always intersects.
        assertTrue(ChunkRing.chunkIntersectsCircle(0, 0, 0, 0, 0));
        // Chunk (1,0) starts at x=16; radius 15 cannot reach it, radius 16 can.
        assertFalse(ChunkRing.chunkIntersectsCircle(1, 0, 0, 0, 15L * 15L));
        assertTrue(ChunkRing.chunkIntersectsCircle(1, 0, 0, 0, 16L * 16L));
        // Diagonal chunk (1,1): nearest corner is (16,16).
        assertFalse(ChunkRing.chunkIntersectsCircle(1, 1, 0, 0, 511));
        assertTrue(ChunkRing.chunkIntersectsCircle(1, 1, 0, 0, 512));
    }
}
