package dev.minebot.bridge.version;

import java.util.UUID;

public record TargetState(
    UUID id,
    String name,
    String dimension,
    double x,
    double y,
    double z,
    float yaw,
    float pitch
) {
}
