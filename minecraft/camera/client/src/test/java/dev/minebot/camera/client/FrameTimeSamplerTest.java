package dev.minebot.camera.client;

import dev.minebot.camera.protocol.CameraClientTelemetry;
import dev.minebot.camera.protocol.CameraDirective;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

final class FrameTimeSamplerTest {
    @Test
    void reportsOrderedPercentilesFromABoundedWindow() {
        FrameTimeSampler sampler = new FrameTimeSampler();
        long now = 1_000_000_000L;
        sampler.recordFrame(now);
        for (int index = 0; index < 60; index++) {
            now += 20_000_000L;
            sampler.recordFrame(now);
        }

        CameraClientTelemetry telemetry = sampler.snapshot(
            now,
            7L,
            CameraDirective.Mode.FOLLOW,
            "ready"
        );

        assertNotNull(telemetry);
        assertEquals(60, telemetry.sampleCount());
        assertEquals(20.0, telemetry.meanFrameMillis(), 0.0001);
        assertEquals(20.0, telemetry.p50FrameMillis(), 0.0001);
        assertEquals(20.0, telemetry.p95FrameMillis(), 0.0001);
        assertEquals(20.0, telemetry.p99FrameMillis(), 0.0001);
        assertNull(sampler.snapshot(now, 7L, CameraDirective.Mode.FOLLOW, "ready"));
    }

    @Test
    void neverRetainsMoreThanTheProtocolLimit() {
        FrameTimeSampler sampler = new FrameTimeSampler();
        long now = 1L;
        sampler.recordFrame(now);
        for (int index = 0; index < FrameTimeSampler.CAPACITY + 100; index++) {
            now += 16_000_000L;
            sampler.recordFrame(now);
        }

        assertEquals(FrameTimeSampler.CAPACITY, sampler.size());
        assertTrue(sampler.shouldReport(now));
        sampler.reset();
        assertEquals(0, sampler.size());
        assertFalse(sampler.shouldReport(now));
    }
}
