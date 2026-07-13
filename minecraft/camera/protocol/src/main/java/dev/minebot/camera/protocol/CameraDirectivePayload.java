package dev.minebot.camera.protocol;

import net.minecraft.network.RegistryFriendlyByteBuf;
import net.minecraft.network.codec.StreamCodec;
import net.minecraft.network.protocol.common.custom.CustomPacketPayload;
import net.minecraft.resources.Identifier;

import java.util.UUID;

public record CameraDirectivePayload(CameraDirective directive) implements CustomPacketPayload {
    public static final Type<CameraDirectivePayload> TYPE = new Type<>(
        Identifier.fromNamespaceAndPath("minebot", "camera_directive")
    );
    public static final StreamCodec<RegistryFriendlyByteBuf, CameraDirectivePayload> CODEC =
        CustomPacketPayload.codec(CameraDirectivePayload::write, CameraDirectivePayload::decode);

    public CameraDirectivePayload {
        if (directive == null) {
            throw new IllegalArgumentException("directive is required");
        }
    }

    @Override
    public Type<CameraDirectivePayload> type() {
        return TYPE;
    }

    private void write(RegistryFriendlyByteBuf buffer) {
        CameraDirective value = directive;
        buffer.writeVarLong(value.generation());
        buffer.writeEnum(value.mode());
        switch (value.mode()) {
            case FOLLOW -> writeFollow(buffer, value.targetId(), value.follow());
            case FIXED -> writeFixed(buffer, value.fixed());
            case DETACHED -> {
            }
        }
    }

    private static CameraDirectivePayload decode(RegistryFriendlyByteBuf buffer) {
        long generation = buffer.readVarLong();
        CameraDirective.Mode mode = buffer.readEnum(CameraDirective.Mode.class);
        CameraDirective value = switch (mode) {
            case FOLLOW -> CameraDirective.follow(generation, buffer.readUUID(), readFollow(buffer));
            case FIXED -> CameraDirective.fixed(generation, readFixed(buffer));
            case DETACHED -> CameraDirective.detached(generation);
        };
        return new CameraDirectivePayload(value);
    }

    private static void writeFollow(RegistryFriendlyByteBuf buffer, UUID targetId, FollowSettings value) {
        buffer.writeUUID(targetId);
        buffer.writeDouble(value.distance());
        buffer.writeDouble(value.azimuthDeg());
        buffer.writeDouble(value.elevationDeg());
        buffer.writeDouble(value.heightOffset());
        buffer.writeDouble(value.stiffness());
        buffer.writeDouble(value.fovDeg());
        buffer.writeDouble(value.collisionMargin());
    }

    private static FollowSettings readFollow(RegistryFriendlyByteBuf buffer) {
        return new FollowSettings(
            buffer.readDouble(),
            buffer.readDouble(),
            buffer.readDouble(),
            buffer.readDouble(),
            buffer.readDouble(),
            buffer.readDouble(),
            buffer.readDouble()
        );
    }

    private static void writeFixed(RegistryFriendlyByteBuf buffer, FixedPose value) {
        buffer.writeUtf(value.dimension(), 128);
        buffer.writeDouble(value.x());
        buffer.writeDouble(value.y());
        buffer.writeDouble(value.z());
        buffer.writeFloat(value.yaw());
        buffer.writeFloat(value.pitch());
        buffer.writeBoolean(value.allowChunkLoading());
    }

    private static FixedPose readFixed(RegistryFriendlyByteBuf buffer) {
        return new FixedPose(
            buffer.readUtf(128),
            buffer.readDouble(),
            buffer.readDouble(),
            buffer.readDouble(),
            buffer.readFloat(),
            buffer.readFloat(),
            buffer.readBoolean()
        );
    }
}
