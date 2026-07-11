package dev.minebot.camera.client;

import dev.minebot.camera.protocol.FollowSettings;

public final class FollowPoseMath {
    private FollowPoseMath() {
    }

    public static Pose desired(
        double targetX,
        double targetY,
        double targetZ,
        float targetYaw,
        FollowSettings settings
    ) {
        double lookY = targetY + settings.heightOffset();
        double yawRadians = Math.toRadians(targetYaw + settings.azimuthDeg());
        double elevationRadians = Math.toRadians(settings.elevationDeg());
        double horizontal = Math.cos(elevationRadians) * settings.distance();
        return new Pose(
            targetX + Math.sin(yawRadians) * horizontal,
            lookY + Math.sin(elevationRadians) * settings.distance(),
            targetZ + Math.cos(yawRadians) * horizontal,
            targetX,
            lookY,
            targetZ
        );
    }

    public static Pose smooth(Pose previous, Pose desired, double stiffness) {
        if (previous == null) {
            return desired;
        }
        double inverse = 1.0 - stiffness;
        return new Pose(
            previous.eyeX() * inverse + desired.eyeX() * stiffness,
            previous.eyeY() * inverse + desired.eyeY() * stiffness,
            previous.eyeZ() * inverse + desired.eyeZ() * stiffness,
            desired.targetX(),
            desired.targetY(),
            desired.targetZ()
        );
    }

    public static Rotation lookAt(Pose pose) {
        double dx = pose.targetX() - pose.eyeX();
        double dy = pose.targetY() - pose.eyeY();
        double dz = pose.targetZ() - pose.eyeZ();
        double horizontal = Math.sqrt(dx * dx + dz * dz);
        return new Rotation(
            (float) Math.toDegrees(Math.atan2(-dx, dz)),
            (float) -Math.toDegrees(Math.atan2(dy, horizontal))
        );
    }

    public record Pose(
        double eyeX,
        double eyeY,
        double eyeZ,
        double targetX,
        double targetY,
        double targetZ
    ) {
    }

    public record Rotation(float yaw, float pitch) {
    }
}
