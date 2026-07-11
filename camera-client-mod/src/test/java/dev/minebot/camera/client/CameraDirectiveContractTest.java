package dev.minebot.camera.client;

import dev.minebot.camera.protocol.CameraDirective;
import dev.minebot.camera.protocol.FixedPose;
import dev.minebot.camera.protocol.FollowSettings;
import org.junit.jupiter.api.Test;

import java.util.UUID;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

final class CameraDirectiveContractTest {
    @Test
    void preservesValidatedFollowDefaults() {
        FollowSettings defaults = FollowSettings.defaults();

        assertEquals(5.0, defaults.distance());
        assertEquals(180.0, defaults.azimuthDeg());
        assertEquals(25.0, defaults.elevationDeg());
        assertEquals(1.6, defaults.heightOffset());
        assertEquals(0.2, defaults.stiffness());
        assertEquals(70.0, defaults.fovDeg());
        assertEquals(0.25, defaults.collisionMargin());
    }

    @Test
    void rejectsNonFiniteAndOutOfRangeFollowSettings() {
        assertThrows(IllegalArgumentException.class, () -> settings(Double.NaN, 25.0, 0.2));
        assertThrows(IllegalArgumentException.class, () -> settings(0.0, 25.0, 0.2));
        assertThrows(IllegalArgumentException.class, () -> settings(5.0, 90.0, 0.2));
        assertThrows(IllegalArgumentException.class, () -> settings(5.0, 25.0, -0.01));
        assertThrows(
            IllegalArgumentException.class,
            () -> new FollowSettings(5.0, 180.0, 25.0, 1.6, 0.2, 171.0, 0.25)
        );
    }

    @Test
    void enforcesModeSpecificDirectiveShapeAndPositiveGeneration() {
        UUID target = UUID.randomUUID();
        CameraDirective follow = CameraDirective.follow(7, target, FollowSettings.defaults());
        CameraDirective fixed = CameraDirective.fixed(
            8,
            new FixedPose("minecraft:overworld", 1.0, 64.0, 2.0, 90.0F, 0.0F, false)
        );
        CameraDirective detached = CameraDirective.detached(9);

        assertEquals(CameraDirective.Mode.FOLLOW, follow.mode());
        assertEquals(target, follow.targetId());
        assertEquals(CameraDirective.Mode.FIXED, fixed.mode());
        assertNull(fixed.targetId());
        assertEquals(CameraDirective.Mode.DETACHED, detached.mode());
        assertThrows(IllegalArgumentException.class, () -> CameraDirective.detached(0));
        assertThrows(
            NullPointerException.class,
            () -> new CameraDirective(1, CameraDirective.Mode.FOLLOW, null, FollowSettings.defaults(), null)
        );
    }

    @Test
    void rejectsUnsafeFixedPoseValues() {
        assertThrows(
            IllegalArgumentException.class,
            () -> new FixedPose("", 0.0, 64.0, 0.0, 0.0F, 0.0F, false)
        );
        assertThrows(
            IllegalArgumentException.class,
            () -> new FixedPose("minecraft:overworld", Double.NaN, 64.0, 0.0, 0.0F, 0.0F, false)
        );
        assertThrows(
            IllegalArgumentException.class,
            () -> new FixedPose("minecraft:overworld", 0.0, 64.0, 0.0, 0.0F, 91.0F, false)
        );
    }

    @Test
    void rejectsDuplicateOrOlderPoseDirectivesButAllowsAuthoritativeCleanup() {
        assertTrue(CameraController.isStale(4, CameraDirective.Mode.FOLLOW, 4));
        assertTrue(CameraController.isStale(3, CameraDirective.Mode.FIXED, 4));
        assertTrue(CameraController.isStale(3, CameraDirective.Mode.DETACHED, 4));
        assertFalse(CameraController.isStale(5, CameraDirective.Mode.FOLLOW, 4));
        assertFalse(CameraController.isStale(4, CameraDirective.Mode.DETACHED, 4));
    }

    private static FollowSettings settings(double distance, double elevation, double stiffness) {
        return new FollowSettings(distance, 180.0, elevation, 1.6, stiffness, 70.0, 0.25);
    }
}
