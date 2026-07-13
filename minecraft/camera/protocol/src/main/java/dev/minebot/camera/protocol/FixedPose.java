package dev.minebot.camera.protocol;

public record FixedPose(
    String dimension,
    double x,
    double y,
    double z,
    float yaw,
    float pitch,
    boolean allowChunkLoading
) {
    public FixedPose {
        if (dimension == null || dimension.isBlank() || dimension.length() > 128) {
            throw new IllegalArgumentException("dimension is required");
        }
        if (!Double.isFinite(x) || !Double.isFinite(y) || !Double.isFinite(z)
            || !Float.isFinite(yaw) || !Float.isFinite(pitch)) {
            throw new IllegalArgumentException("fixed pose values must be finite");
        }
        if (Math.abs(x) > 30_000_000.0 || Math.abs(z) > 30_000_000.0 || y < -2048.0 || y > 2048.0) {
            throw new IllegalArgumentException("fixed pose is outside world bounds");
        }
        if (pitch < -90.0F || pitch > 90.0F) {
            throw new IllegalArgumentException("fixed pitch is outside the allowed range");
        }
    }
}
