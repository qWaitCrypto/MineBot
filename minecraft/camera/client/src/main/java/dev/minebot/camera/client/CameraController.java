package dev.minebot.camera.client;

import dev.minebot.camera.protocol.CameraDirective;
import dev.minebot.camera.protocol.FixedPose;
import dev.minebot.camera.protocol.FollowSettings;
import net.minecraft.client.DeltaTracker;
import net.minecraft.client.Minecraft;
import net.minecraft.client.gui.screens.PauseScreen;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.level.ClipContext;
import net.minecraft.world.phys.BlockHitResult;
import net.minecraft.world.phys.HitResult;
import net.minecraft.world.phys.Vec3;
import net.xolt.freecam.Freecam;
import net.xolt.freecam.util.FreeCamera;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

public final class CameraController {
    private static final Logger LOGGER = LoggerFactory.getLogger("minebot-camera-client");
    private static final CameraController INSTANCE = new CameraController();

    private final CameraTelemetryReporter telemetry = new CameraTelemetryReporter();
    private CameraDirective active;
    private FollowPoseMath.Pose lastPose;
    private long lastGeneration;
    private Boolean previousHideGui;
    private Integer previousFov;
    private String errorCode;

    private CameraController() {
    }

    public static CameraController instance() {
        return INSTANCE;
    }

    public void accept(Minecraft minecraft, CameraDirective directive) {
        if (isStale(directive.generation(), directive.mode(), lastGeneration)) {
            LOGGER.warn("Ignoring stale Camera directive generation {}", directive.generation());
            return;
        }
        if (directive.mode() == CameraDirective.Mode.DETACHED) {
            lastGeneration = directive.generation();
            deactivate(minecraft);
            return;
        }

        boolean resetPose = active == null
            || active.mode() != directive.mode()
            || !java.util.Objects.equals(active.targetId(), directive.targetId());
        lastGeneration = directive.generation();
        active = directive;
        if (resetPose) {
            lastPose = null;
        }
        activate(minecraft);
    }

    static boolean isStale(long generation, CameraDirective.Mode mode, long acceptedGeneration) {
        return mode == CameraDirective.Mode.DETACHED
            ? generation < acceptedGeneration
            : generation <= acceptedGeneration;
    }

    public void applyFrame(Minecraft minecraft, DeltaTracker deltaTracker) {
        CameraDirective directive = active;
        telemetry.onFrame(directive, errorCode);
        if (directive == null || minecraft.player == null || minecraft.level == null) {
            return;
        }
        if (minecraft.screen instanceof PauseScreen) {
            minecraft.setScreen(null);
        }
        if (!Freecam.isEnabled()) {
            activate(minecraft);
        }
        FreeCamera camera = Freecam.getFreeCamera();
        if (!Freecam.isEnabled() || camera == null) {
            setError("freecam_unavailable");
            return;
        }

        float partialTick = deltaTracker.getGameTimeDeltaPartialTick(false);
        PoseAndRotation pose = switch (directive.mode()) {
            case FOLLOW -> followPose(minecraft, directive, partialTick);
            case FIXED -> fixedPose(directive.fixed());
            case DETACHED -> null;
        };
        if (pose == null) {
            return;
        }

        applyCameraEntity(camera, pose);
        if (minecraft.getCameraEntity() != camera) {
            minecraft.setCameraEntity(camera);
        }
        minecraft.options.hideGui = true;
        minecraft.options.fov().set((int) Math.round(fov(directive)));
        clearError();
    }

    public void disconnect(Minecraft minecraft) {
        deactivate(minecraft);
        telemetry.reset();
        lastGeneration = 0;
    }

    private void activate(Minecraft minecraft) {
        if (minecraft.player == null || minecraft.level == null) {
            setError("client_not_ready");
            return;
        }
        if (!Freecam.isEnabled()) {
            Freecam.toggle();
        }
        if (!Freecam.isEnabled() || Freecam.getFreeCamera() == null) {
            setError("freecam_unavailable");
            return;
        }
        if (previousHideGui == null) {
            previousHideGui = minecraft.options.hideGui;
            previousFov = minecraft.options.fov().get();
        }
        minecraft.options.hideGui = true;
        minecraft.options.fov().set((int) Math.round(fov(active)));
        clearError();
        LOGGER.info("Applied Camera directive generation {} mode {}", active.generation(), active.mode());
    }

    private PoseAndRotation followPose(Minecraft minecraft, CameraDirective directive, float partialTick) {
        Entity target = minecraft.level.getEntity(directive.targetId());
        if (target == null || target.isRemoved()) {
            setError("target_missing");
            return null;
        }
        FollowSettings settings = directive.follow();
        Vec3 interpolated = target.getPosition(partialTick);
        FollowPoseMath.Pose desired = FollowPoseMath.desired(
            interpolated.x,
            interpolated.y,
            interpolated.z,
            target.getYRot(partialTick),
            settings
        );
        FollowPoseMath.Pose smoothed = FollowPoseMath.smooth(lastPose, desired, settings.stiffness());
        lastPose = collide(minecraft, target, smoothed, settings.collisionMargin());
        FollowPoseMath.Rotation rotation = FollowPoseMath.lookAt(lastPose);
        return new PoseAndRotation(lastPose.eyeX(), lastPose.eyeY(), lastPose.eyeZ(), rotation.yaw(), rotation.pitch());
    }

    private static FollowPoseMath.Pose collide(
        Minecraft minecraft,
        Entity target,
        FollowPoseMath.Pose pose,
        double margin
    ) {
        Vec3 from = new Vec3(pose.targetX(), pose.targetY(), pose.targetZ());
        Vec3 desired = new Vec3(pose.eyeX(), pose.eyeY(), pose.eyeZ());
        Vec3 ray = desired.subtract(from);
        double desiredDistance = ray.length();
        if (desiredDistance <= 1.0e-6) {
            return pose;
        }
        BlockHitResult hit = minecraft.level.clip(
            new ClipContext(from, desired, ClipContext.Block.COLLIDER, ClipContext.Fluid.NONE, target)
        );
        if (hit.getType() != HitResult.Type.BLOCK) {
            return pose;
        }
        double allowedDistance = Math.max(0.0, from.distanceTo(hit.getLocation()) - margin);
        Vec3 eye = from.add(ray.scale(Math.min(desiredDistance, allowedDistance) / desiredDistance));
        return new FollowPoseMath.Pose(
            eye.x,
            eye.y,
            eye.z,
            pose.targetX(),
            pose.targetY(),
            pose.targetZ()
        );
    }

    private static PoseAndRotation fixedPose(FixedPose pose) {
        return new PoseAndRotation(pose.x(), pose.y(), pose.z(), pose.yaw(), pose.pitch());
    }

    private static void applyCameraEntity(FreeCamera camera, PoseAndRotation pose) {
        camera.xo = pose.x();
        camera.yo = pose.y();
        camera.zo = pose.z();
        camera.xOld = pose.x();
        camera.yOld = pose.y();
        camera.zOld = pose.z();
        camera.setPos(pose.x(), pose.y(), pose.z());
        camera.yRotO = pose.yaw();
        camera.xRotO = pose.pitch();
        camera.setYRot(pose.yaw());
        camera.setXRot(pose.pitch());
    }

    private static double fov(CameraDirective directive) {
        return directive.mode() == CameraDirective.Mode.FOLLOW ? directive.follow().fovDeg() : 70.0;
    }

    private void deactivate(Minecraft minecraft) {
        active = null;
        lastPose = null;
        telemetry.reset();
        if (Freecam.isEnabled()) {
            Freecam.toggle();
        }
        if (previousHideGui != null) {
            minecraft.options.hideGui = previousHideGui;
            minecraft.options.fov().set(previousFov);
        }
        previousHideGui = null;
        previousFov = null;
        clearError();
        LOGGER.info("Camera directive cleared");
    }

    private void setError(String code) {
        if (!code.equals(errorCode)) {
            errorCode = code;
            LOGGER.warn("Camera adapter state {}", code);
        }
    }

    private void clearError() {
        errorCode = null;
    }

    private record PoseAndRotation(double x, double y, double z, float yaw, float pitch) {
    }
}
