package dev.minebot.bridge.version;

import dev.minebot.camera.protocol.CameraDirective;
import net.minecraft.server.MinecraftServer;

import java.util.UUID;

public interface ObserverAccess {
    String minecraftVersion();

    ObserverState observerState(MinecraftServer server, UUID observerId);

    ObserverState enforceObserverPolicy(MinecraftServer server, UUID observerId);

    TargetState resolveTarget(MinecraftServer server, UUID targetId, String targetName);

    void attachToTarget(MinecraftServer server, UUID observerId, UUID targetId);

    void holdFixed(
        MinecraftServer server,
        UUID observerId,
        String dimension,
        double x,
        double y,
        double z,
        float yaw,
        float pitch,
        boolean allowChunkLoading
    );

    void sendDirective(MinecraftServer server, UUID observerId, CameraDirective directive);

    void detach(MinecraftServer server, UUID observerId, boolean disconnect);
}
