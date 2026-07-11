package dev.minebot.camera.protocol;

import net.minecraft.network.RegistryFriendlyByteBuf;
import net.minecraft.network.codec.StreamCodec;
import net.minecraft.network.protocol.common.custom.CustomPacketPayload;
import net.minecraft.resources.Identifier;

public record CameraClientTelemetryPayload(CameraClientTelemetry telemetry) implements CustomPacketPayload {
    public static final Type<CameraClientTelemetryPayload> TYPE = new Type<>(
        Identifier.fromNamespaceAndPath("minebot", "camera_client_telemetry")
    );
    public static final StreamCodec<RegistryFriendlyByteBuf, CameraClientTelemetryPayload> CODEC =
        CustomPacketPayload.codec(CameraClientTelemetryPayload::write, CameraClientTelemetryPayload::decode);

    public CameraClientTelemetryPayload {
        if (telemetry == null) {
            throw new IllegalArgumentException("telemetry is required");
        }
    }

    @Override
    public Type<CameraClientTelemetryPayload> type() {
        return TYPE;
    }

    private void write(RegistryFriendlyByteBuf buffer) {
        buffer.writeVarLong(telemetry.generation());
        buffer.writeEnum(telemetry.mode());
        buffer.writeUtf(telemetry.adapterState(), 64);
        buffer.writeVarInt(telemetry.sampleCount());
        buffer.writeVarLong(telemetry.windowMillis());
        buffer.writeDouble(telemetry.meanFrameMillis());
        buffer.writeDouble(telemetry.p50FrameMillis());
        buffer.writeDouble(telemetry.p95FrameMillis());
        buffer.writeDouble(telemetry.p99FrameMillis());
    }

    private static CameraClientTelemetryPayload decode(RegistryFriendlyByteBuf buffer) {
        return new CameraClientTelemetryPayload(new CameraClientTelemetry(
            buffer.readVarLong(),
            buffer.readEnum(CameraDirective.Mode.class),
            buffer.readUtf(64),
            buffer.readVarInt(),
            buffer.readVarLong(),
            buffer.readDouble(),
            buffer.readDouble(),
            buffer.readDouble(),
            buffer.readDouble()
        ));
    }
}
