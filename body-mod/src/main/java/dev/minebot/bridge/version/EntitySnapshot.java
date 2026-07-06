package dev.minebot.bridge.version;

import net.minecraft.server.level.ServerLevel;

public record EntitySnapshot(
    String name,
    ServerLevel level,
    double x,
    double y,
    double z,
    float yaw,
    float pitch,
    boolean onGround,
    String pose
) {
    public int sectionX() {
        return floorSection(x);
    }

    public int sectionY() {
        return floorSection(y);
    }

    public int sectionZ() {
        return floorSection(z);
    }

    private static int floorSection(double value) {
        return ((int) Math.floor(value)) >> 4;
    }
}
