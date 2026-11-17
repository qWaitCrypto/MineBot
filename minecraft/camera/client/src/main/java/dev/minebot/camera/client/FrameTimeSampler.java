package dev.minebot.camera.client;

import dev.minebot.camera.protocol.CameraClientTelemetry;
import dev.minebot.camera.protocol.CameraDirective;

import java.util.Arrays;

final class FrameTimeSampler {
    static final int CAPACITY = CameraClientTelemetry.MAX_SAMPLES;
    static final int MIN_REPORT_SAMPLES = 30;
    static final long REPORT_INTERVAL_NANOS = 1_000_000_000L;
    private static final long MAX_FRAME_NANOS = 60_000_000_000L;

    private final long[] samples = new long[CAPACITY];
    private int size;
    private int next;
    private long previousFrameNanos;
    private long lastReportNanos;

    void recordFrame(long nowNanos) {
        if (previousFrameNanos != 0L) {
            long frameNanos = nowNanos - previousFrameNanos;
            if (frameNanos > 0L && frameNanos <= MAX_FRAME_NANOS) {
                samples[next] = frameNanos;
                next = (next + 1) % CAPACITY;
                size = Math.min(size + 1, CAPACITY);
            }
        }
        previousFrameNanos = nowNanos;
    }

    boolean shouldReport(long nowNanos) {
        return size >= MIN_REPORT_SAMPLES
            && (lastReportNanos == 0L || nowNanos - lastReportNanos >= REPORT_INTERVAL_NANOS);
    }

    CameraClientTelemetry snapshot(
        long nowNanos,
        long generation,
        CameraDirective.Mode mode,
        String adapterState
    ) {
        if (!shouldReport(nowNanos)) {
            return null;
        }
        long[] ordered = Arrays.copyOf(samples, size);
        Arrays.sort(ordered);
        long total = 0L;
        for (long sample : ordered) {
            total += sample;
        }
        lastReportNanos = nowNanos;
        return new CameraClientTelemetry(
            generation,
            mode,
            adapterState,
            size,
            Math.max(1L, total / 1_000_000L),
            nanosToMillis((double) total / size),
            nanosToMillis(percentile(ordered, 0.50)),
            nanosToMillis(percentile(ordered, 0.95)),
            nanosToMillis(percentile(ordered, 0.99))
        );
    }

    void reset() {
        size = 0;
        next = 0;
        previousFrameNanos = 0L;
        lastReportNanos = 0L;
    }

    int size() {
        return size;
    }

    private static long percentile(long[] ordered, double percentile) {
        int index = Math.max(0, (int) Math.ceil(percentile * ordered.length) - 1);
        return ordered[index];
    }

    private static double nanosToMillis(double nanos) {
        return nanos / 1_000_000.0;
    }
}
