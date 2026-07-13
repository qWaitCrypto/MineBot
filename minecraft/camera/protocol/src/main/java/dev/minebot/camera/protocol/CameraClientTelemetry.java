package dev.minebot.camera.protocol;

public record CameraClientTelemetry(
    long generation,
    CameraDirective.Mode mode,
    String adapterState,
    int sampleCount,
    long windowMillis,
    double meanFrameMillis,
    double p50FrameMillis,
    double p95FrameMillis,
    double p99FrameMillis
) {
    public static final int MAX_SAMPLES = 1024;
    public static final long MAX_WINDOW_MILLIS = MAX_SAMPLES * 60_000L;
    public static final double MAX_FRAME_MILLIS = 60_000.0;

    public CameraClientTelemetry {
        if (generation <= 0) {
            throw new IllegalArgumentException("generation must be positive");
        }
        if (mode == null || mode == CameraDirective.Mode.DETACHED) {
            throw new IllegalArgumentException("telemetry requires an active Camera mode");
        }
        if (adapterState == null || adapterState.isBlank() || adapterState.length() > 64) {
            throw new IllegalArgumentException("adapter state must contain 1-64 characters");
        }
        if (sampleCount < 2 || sampleCount > MAX_SAMPLES) {
            throw new IllegalArgumentException("sample count is outside the bounded range");
        }
        if (windowMillis < 1 || windowMillis > MAX_WINDOW_MILLIS) {
            throw new IllegalArgumentException("telemetry window is outside the bounded range");
        }
        requireMetric("mean frame time", meanFrameMillis);
        requireMetric("p50 frame time", p50FrameMillis);
        requireMetric("p95 frame time", p95FrameMillis);
        requireMetric("p99 frame time", p99FrameMillis);
        if (p50FrameMillis > p95FrameMillis || p95FrameMillis > p99FrameMillis) {
            throw new IllegalArgumentException("frame-time percentiles must be ordered");
        }
    }

    private static void requireMetric(String name, double value) {
        if (!Double.isFinite(value) || value <= 0.0 || value > MAX_FRAME_MILLIS) {
            throw new IllegalArgumentException(name + " is outside the bounded range");
        }
    }
}
