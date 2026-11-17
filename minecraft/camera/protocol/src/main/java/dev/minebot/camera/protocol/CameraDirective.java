package dev.minebot.camera.protocol;

import java.util.Objects;
import java.util.UUID;

public record CameraDirective(
    long generation,
    Mode mode,
    UUID targetId,
    FollowSettings follow,
    FixedPose fixed
) {
    public enum Mode {
        FOLLOW,
        FIXED,
        DETACHED
    }

    public CameraDirective {
        if (generation < 1) {
            throw new IllegalArgumentException("generation must be positive");
        }
        Objects.requireNonNull(mode, "mode");
        switch (mode) {
            case FOLLOW -> {
                Objects.requireNonNull(targetId, "follow targetId");
                Objects.requireNonNull(follow, "follow settings");
                if (fixed != null) {
                    throw new IllegalArgumentException("follow directive cannot contain a fixed pose");
                }
            }
            case FIXED -> {
                Objects.requireNonNull(fixed, "fixed pose");
                if (targetId != null || follow != null) {
                    throw new IllegalArgumentException("fixed directive cannot contain follow settings");
                }
            }
            case DETACHED -> {
                if (targetId != null || follow != null || fixed != null) {
                    throw new IllegalArgumentException("detached directive cannot contain a pose");
                }
            }
        }
    }

    public static CameraDirective follow(long generation, UUID targetId, FollowSettings settings) {
        return new CameraDirective(generation, Mode.FOLLOW, targetId, settings, null);
    }

    public static CameraDirective fixed(long generation, FixedPose pose) {
        return new CameraDirective(generation, Mode.FIXED, null, null, pose);
    }

    public static CameraDirective detached(long generation) {
        return new CameraDirective(generation, Mode.DETACHED, null, null, null);
    }
}
