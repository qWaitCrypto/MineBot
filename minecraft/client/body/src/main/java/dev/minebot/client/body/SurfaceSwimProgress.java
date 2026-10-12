package dev.minebot.client.body;

/** Small, side-effect-free policy used by the ascend-in-water action. */
public final class SurfaceSwimProgress {
    private SurfaceSwimProgress() {}

    /** Surfacing is complete once the head is out and air is recovering. */
    public static boolean completed(boolean headOutOfWater, int previousAir, int currentAir) {
        return headOutOfWater && currentAir > previousAir;
    }

    /** A swim target must stay in a loaded, vertically valid part of the world. */
    public static boolean allowed(boolean inLava, boolean chunkLoaded, int y, int minY, int maxY) {
        return !inLava && chunkLoaded && y >= minY && y + 1 < maxY;
    }
}
