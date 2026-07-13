package dev.minebot.camera.protocol;

public record FollowSettings(
    double distance,
    double azimuthDeg,
    double elevationDeg,
    double heightOffset,
    double stiffness,
    double fovDeg,
    double collisionMargin
) {
    public static FollowSettings defaults() {
        return new FollowSettings(5.0, 180.0, 25.0, 1.6, 0.2, 70.0, 0.25);
    }

    public FollowSettings {
        finite("distance", distance);
        finite("azimuth_deg", azimuthDeg);
        finite("elevation_deg", elevationDeg);
        finite("height_offset", heightOffset);
        finite("stiffness", stiffness);
        finite("fov_deg", fovDeg);
        finite("collision_margin", collisionMargin);
        range("distance", distance, 0.1, 64.0);
        range("elevation_deg", elevationDeg, -89.0, 89.0);
        range("height_offset", heightOffset, -64.0, 64.0);
        range("stiffness", stiffness, 0.0, 1.0);
        range("fov_deg", fovDeg, 1.0, 170.0);
        range("collision_margin", collisionMargin, 0.0, 8.0);
    }

    private static void finite(String name, double value) {
        if (!Double.isFinite(value)) {
            throw new IllegalArgumentException(name + " must be finite");
        }
    }

    private static void range(String name, double value, double minimum, double maximum) {
        if (value < minimum || value > maximum) {
            throw new IllegalArgumentException(name + " is outside the allowed range");
        }
    }
}
