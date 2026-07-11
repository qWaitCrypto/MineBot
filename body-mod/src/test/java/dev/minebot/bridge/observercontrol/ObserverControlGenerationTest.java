package dev.minebot.bridge.observercontrol;

import dev.minebot.camera.protocol.CameraClientTelemetry;
import dev.minebot.camera.protocol.CameraDirective;
import org.junit.jupiter.api.Test;

import java.util.UUID;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

final class ObserverControlGenerationTest {
    private static final String LEASE = "0123456789abcdef";

    @Test
    void heartbeatRequiresTheCurrentLeaseAndGeneration() {
        assertTrue(ObserverControlChannel.isCurrentGeneration(LEASE, 7, LEASE, 7));
        assertFalse(ObserverControlChannel.isCurrentGeneration(LEASE, 7, LEASE, 6));
        assertFalse(ObserverControlChannel.isCurrentGeneration(LEASE, 7, "different-lease", 7));
    }

    @Test
    void cameraMutationRequiresANewerGenerationOnTheSameLease() {
        assertTrue(ObserverControlChannel.isSuccessorGeneration(LEASE, 7, LEASE, 8));
        assertTrue(ObserverControlChannel.isSuccessorGeneration(LEASE, 7, LEASE, 12));
        assertFalse(ObserverControlChannel.isSuccessorGeneration(LEASE, 7, LEASE, 7));
        assertFalse(ObserverControlChannel.isSuccessorGeneration(LEASE, 7, LEASE, 6));
        assertFalse(ObserverControlChannel.isSuccessorGeneration(LEASE, 7, "different-lease", 8));
    }

    @Test
    void clientTelemetryRequiresAllowlistedIdentityCurrentGenerationAndMode() {
        UUID observer = UUID.randomUUID();
        CameraClientTelemetry telemetry = new CameraClientTelemetry(
            7L,
            CameraDirective.Mode.FOLLOW,
            "ready",
            60,
            1000L,
            16.6,
            16.0,
            18.0,
            20.0
        );

        assertTrue(ObserverControlChannel.isCurrentClientTelemetry(
            observer, observer, 7L, ObserverMode.FOLLOW, telemetry
        ));
        assertFalse(ObserverControlChannel.isCurrentClientTelemetry(
            observer, UUID.randomUUID(), 7L, ObserverMode.FOLLOW, telemetry
        ));
        assertFalse(ObserverControlChannel.isCurrentClientTelemetry(
            observer, observer, 8L, ObserverMode.FOLLOW, telemetry
        ));
        assertFalse(ObserverControlChannel.isCurrentClientTelemetry(
            observer, observer, 7L, ObserverMode.FIXED, telemetry
        ));
    }
}
