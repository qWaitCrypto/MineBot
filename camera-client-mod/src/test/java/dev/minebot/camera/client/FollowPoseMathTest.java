package dev.minebot.camera.client;

import dev.minebot.camera.protocol.FollowSettings;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;

final class FollowPoseMathTest {
    private static final double EPSILON = 1.0e-9;

    @Test
    void preservesCameraBranchDefaultOrbitVector() {
        FollowPoseMath.Pose pose = FollowPoseMath.desired(10.0, 64.0, -3.0, 0.0F, FollowSettings.defaults());
        double horizontal = Math.cos(Math.toRadians(25.0)) * 5.0;

        assertEquals(10.0, pose.targetX(), EPSILON);
        assertEquals(65.6, pose.targetY(), EPSILON);
        assertEquals(-3.0, pose.targetZ(), EPSILON);
        assertEquals(10.0, pose.eyeX(), EPSILON);
        assertEquals(65.6 + Math.sin(Math.toRadians(25.0)) * 5.0, pose.eyeY(), EPSILON);
        assertEquals(-3.0 - horizontal, pose.eyeZ(), EPSILON);
    }

    @Test
    void preservesYawRelativeAzimuthAndDamping() {
        FollowSettings settings = FollowSettings.defaults();
        FollowPoseMath.Pose first = FollowPoseMath.desired(0.0, 64.0, 0.0, 90.0F, settings);
        FollowPoseMath.Pose desiredSecond = FollowPoseMath.desired(10.0, 64.0, 0.0, 90.0F, settings);
        FollowPoseMath.Pose second = FollowPoseMath.smooth(first, desiredSecond, 0.2);

        assertEquals(first.eyeX() * 0.8 + desiredSecond.eyeX() * 0.2, second.eyeX(), EPSILON);
        assertEquals(10.0, second.targetX(), EPSILON);
    }

    @Test
    void lookAtPointsBackAtTarget() {
        FollowPoseMath.Rotation rotation = FollowPoseMath.lookAt(
            new FollowPoseMath.Pose(0.0, 65.6, -5.0, 0.0, 65.6, 0.0)
        );

        assertEquals(0.0F, rotation.yaw(), 1.0e-6F);
        assertEquals(0.0F, rotation.pitch(), 1.0e-6F);
    }
}
