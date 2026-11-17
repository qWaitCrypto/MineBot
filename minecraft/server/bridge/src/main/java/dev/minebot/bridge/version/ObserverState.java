package dev.minebot.bridge.version;

import java.util.UUID;

public record ObserverState(
    UUID id,
    boolean connected,
    String name,
    boolean spectator,
    boolean operator,
    String dimension,
    double x,
    double y,
    double z,
    UUID cameraTargetId
) {
    public static ObserverState offline(UUID id) {
        return new ObserverState(id, false, null, false, false, null, 0.0, 0.0, 0.0, null);
    }
}
