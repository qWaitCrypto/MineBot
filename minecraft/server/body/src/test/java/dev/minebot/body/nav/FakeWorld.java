package dev.minebot.body.nav;

import java.util.HashMap;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;

/**
 * Synthetic world for navigation tests: flat solid floor at a configured Y,
 * passable above, with explicit overrides and an optional loaded-region
 * boundary.
 */
public final class FakeWorld implements WorldView {
    private final int floorY;
    private final Map<Long, NodeKind> overrides = new HashMap<>();
    private final Set<Long> unloadedChunks = new HashSet<>();

    public FakeWorld(int floorY) {
        this.floorY = floorY;
    }

    public FakeWorld set(int x, int y, int z, NodeKind kind) {
        overrides.put(key(x, y, z), kind);
        return this;
    }

    /** A vertical solid column from floor level up to {@code topY}. */
    public FakeWorld wall(int x, int z, int topY) {
        for (int y = floorY + 1; y <= topY; y++) {
            set(x, y, z, NodeKind.SOLID);
        }
        return this;
    }

    public FakeWorld unloadChunk(int chunkX, int chunkZ) {
        unloadedChunks.add(((long) chunkX << 32) | (chunkZ & 0xFFFFFFFFL));
        return this;
    }

    @Override
    public NodeKind kindAt(int x, int y, int z) {
        if (unloadedChunks.contains(((long) (x >> 4) << 32) | ((z >> 4) & 0xFFFFFFFFL))) {
            return NodeKind.UNLOADED;
        }
        NodeKind override = overrides.get(key(x, y, z));
        if (override != null) {
            return override;
        }
        return y <= floorY ? NodeKind.SOLID : NodeKind.PASSABLE;
    }

    private static long key(int x, int y, int z) {
        return ((long) (x & 0x3FFFFFF) << 38) | ((long) (z & 0x3FFFFFF) << 12) | (y & 0xFFF);
    }
}
