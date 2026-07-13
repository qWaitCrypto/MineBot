package dev.minebot.camera.client;

import dev.minebot.camera.protocol.CameraClientTelemetry;
import dev.minebot.camera.protocol.CameraClientTelemetryPayload;
import dev.minebot.camera.protocol.CameraDirective;
import net.fabricmc.fabric.api.client.networking.v1.ClientPlayNetworking;

final class CameraTelemetryReporter {
    private final FrameTimeSampler sampler = new FrameTimeSampler();
    private long generation;

    void onFrame(CameraDirective directive, String errorCode) {
        if (directive == null || directive.mode() == CameraDirective.Mode.DETACHED) {
            reset();
            return;
        }
        if (directive.generation() != generation) {
            sampler.reset();
            generation = directive.generation();
        }
        long nowNanos = System.nanoTime();
        sampler.recordFrame(nowNanos);
        CameraClientTelemetry telemetry = sampler.snapshot(
            nowNanos,
            directive.generation(),
            directive.mode(),
            errorCode == null ? "ready" : errorCode
        );
        if (telemetry == null) {
            return;
        }
        try {
            if (ClientPlayNetworking.canSend(CameraClientTelemetryPayload.TYPE)) {
                ClientPlayNetworking.send(new CameraClientTelemetryPayload(telemetry));
            }
        } catch (RuntimeException ignored) {
            // Diagnostic telemetry must never disturb rendering or cleanup.
        }
    }

    void reset() {
        sampler.reset();
        generation = 0L;
    }
}
