package dev.minebot.client.body;

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
}
