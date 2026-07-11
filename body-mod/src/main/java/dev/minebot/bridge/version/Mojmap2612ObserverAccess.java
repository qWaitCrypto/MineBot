package dev.minebot.bridge.version;

import dev.minebot.camera.protocol.CameraDirective;
import dev.minebot.camera.protocol.CameraDirectivePayload;
import net.fabricmc.fabric.api.networking.v1.ServerPlayNetworking;
import net.minecraft.network.chat.Component;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.level.GameType;

import java.util.Set;
import java.util.UUID;

public final class Mojmap2612ObserverAccess implements ObserverAccess {
    public static final String CAMERA_TAG = "minebot.camera.observer";

    @Override
    public String minecraftVersion() {
        return "26.1.2";
    }

    @Override
    public ObserverState observerState(MinecraftServer server, UUID observerId) {
        ServerPlayer observer = server.getPlayerList().getPlayer(observerId);
        return observer == null ? ObserverState.offline(observerId) : snapshot(server, observer);
    }

    @Override
    public ObserverState enforceObserverPolicy(MinecraftServer server, UUID observerId) {
        ServerPlayer observer = requireObserver(server, observerId);
        return enforceObserverPolicy(server, observer);
    }

    public static ObserverState enforceObserverPolicy(MinecraftServer server, ServerPlayer observer) {
        if (server.getPlayerList().isOp(observer.nameAndId())) {
            observer.connection.disconnect(Component.literal("MineBot Camera observer must not be an operator"));
            throw new ObserverAccessException("observer_is_operator", "configured observer must be non-op", false);
        }
        if (observer.gameMode() != GameType.SPECTATOR && !observer.setGameMode(GameType.SPECTATOR)) {
            observer.connection.disconnect(Component.literal("MineBot Camera could not enforce spectator mode"));
            throw new ObserverAccessException("spectator_policy_failed", "failed to force observer spectator policy", false);
        }
        observer.addTag(CAMERA_TAG);
        return snapshot(server, observer);
    }

    @Override
    public TargetState resolveTarget(MinecraftServer server, UUID targetId, String targetName) {
        Entity target = null;
        if (targetId != null) {
            target = server.overworld().getEntityInAnyDimension(targetId);
        } else if (targetName != null && !targetName.isBlank()) {
            target = server.getPlayerList().getPlayerByName(targetName);
        }
        if (target == null || target.isRemoved()) {
            throw new ObserverAccessException("target_missing", "configured target is not present", true);
        }
        if (!(target.level() instanceof ServerLevel level)) {
            throw new ObserverAccessException("target_invalid", "target is not in a server level", false);
        }
        return new TargetState(
            target.getUUID(),
            target.getScoreboardName(),
            level.dimension().identifier().toString(),
            target.getX(),
            target.getY(),
            target.getZ(),
            target.getYRot(),
            target.getXRot()
        );
    }

    @Override
    public void attachToTarget(MinecraftServer server, UUID observerId, UUID targetId) {
        ServerPlayer observer = requireObserver(server, observerId);
        enforceObserverPolicy(server, observerId);
        Entity target = server.overworld().getEntityInAnyDimension(targetId);
        if (target == null || target.isRemoved() || !(target.level() instanceof ServerLevel targetLevel)) {
            throw new ObserverAccessException("target_missing", "configured target is not present", true);
        }
        if (observer.level() == targetLevel && observer.getCamera() == target) {
            return;
        }
        if (observer.getCamera() != observer) {
            observer.setCamera(observer);
        }
        boolean moved = observer.teleportTo(
            targetLevel,
            target.getX(),
            target.getY(),
            target.getZ(),
            Set.of(),
            target.getYRot(),
            target.getXRot(),
            true
        );
        if (!moved) {
            throw new ObserverAccessException("anchor_failed", "failed to move observer to target dimension", true);
        }
        observer.setCamera(target);
    }

    @Override
    public void holdFixed(
        MinecraftServer server,
        UUID observerId,
        String dimension,
        double x,
        double y,
        double z,
        float yaw,
        float pitch,
        boolean allowChunkLoading
    ) {
        ServerPlayer observer = requireObserver(server, observerId);
        enforceObserverPolicy(server, observerId);
        ServerLevel level = findLevel(server, dimension);
        int chunkX = Math.floorDiv((int) Math.floor(x), 16);
        int chunkZ = Math.floorDiv((int) Math.floor(z), 16);
        if (!allowChunkLoading && !level.hasChunk(chunkX, chunkZ)) {
            throw new ObserverAccessException("fixed_chunk_unloaded", "fixed camera destination chunk is not loaded", false);
        }
        if (observer.getCamera() != observer) {
            observer.setCamera(observer);
        }
        boolean moved = observer.teleportTo(level, x, y, z, Set.of(), yaw, pitch, true);
        if (!moved) {
            throw new ObserverAccessException("anchor_failed", "failed to move observer to fixed pose", true);
        }
    }

    @Override
    public void sendDirective(MinecraftServer server, UUID observerId, CameraDirective directive) {
        ServerPlayer observer = requireObserver(server, observerId);
        if (!ServerPlayNetworking.canSend(observer, CameraDirectivePayload.TYPE)) {
            throw new ObserverAccessException(
                "client_adapter_unavailable",
                "observer client does not expose the MineBot Camera directive channel",
                true
            );
        }
        ServerPlayNetworking.send(observer, new CameraDirectivePayload(directive));
    }

    @Override
    public void detach(MinecraftServer server, UUID observerId, boolean disconnect) {
        ServerPlayer observer = server.getPlayerList().getPlayer(observerId);
        if (observer == null) {
            return;
        }
        if (observer.getCamera() != observer) {
            observer.setCamera(observer);
        }
        if (disconnect) {
            observer.removeTag(CAMERA_TAG);
            observer.connection.disconnect(Component.literal("MineBot Camera lease ended"));
        } else {
            enforceObserverPolicy(server, observer);
        }
    }

    private static ServerPlayer requireObserver(MinecraftServer server, UUID observerId) {
        ServerPlayer observer = server.getPlayerList().getPlayer(observerId);
        if (observer == null || observer.isRemoved()) {
            throw new ObserverAccessException("observer_offline", "configured observer is not connected", true);
        }
        return observer;
    }

    private static ServerLevel findLevel(MinecraftServer server, String dimension) {
        for (ServerLevel level : server.getAllLevels()) {
            if (level.dimension().identifier().toString().equals(dimension)) {
                return level;
            }
        }
        throw new ObserverAccessException("dimension_missing", "configured dimension is not available", false);
    }

    private static ObserverState snapshot(MinecraftServer server, ServerPlayer observer) {
        Entity camera = observer.getCamera();
        UUID cameraTargetId = camera == null || camera == observer ? null : camera.getUUID();
        return new ObserverState(
            observer.getUUID(),
            true,
            observer.getScoreboardName(),
            observer.gameMode() == GameType.SPECTATOR,
            server.getPlayerList().isOp(observer.nameAndId()),
            observer.level().dimension().identifier().toString(),
            observer.getX(),
            observer.getY(),
            observer.getZ(),
            cameraTargetId
        );
    }
}
