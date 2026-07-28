package dev.minebot.body.action;

import com.google.gson.JsonArray;
import com.google.gson.JsonNull;
import com.google.gson.JsonObject;
import dev.minebot.body.nav.ApproachController;
import dev.minebot.body.nav.Goal;
import dev.minebot.body.nav.MovementControls;
import dev.minebot.body.nav.NavigateExecutor;
import dev.minebot.body.nav.WorldView;

/** Maintains a bounded distance band around one server-locked moving entity. */
public final class FollowExecutor implements ActionRuntime.TickExecutor {
    public static final int APPROACH_REPLAN_LIMIT = 5;

    private final String bot;
    private final String actionId;
    private final String targetSpec;
    private final double keepRadius;
    private final double replanDistance;
    private final int acquireRadius;
    private final WorldView world;
    private final MovementControls movement;
    private final EngageExecutor.TargetSource targets;
    private final NavigateExecutor.PositionSource positions;
    private final NavigateExecutor.EventSink events;
    private final ActionRuntime runtime;
    private final int timeoutTicks;

    private EngageExecutor.Target lockedTarget;
    private EngageExecutor.Target plannedTarget;
    private ApproachController approach;
    private int elapsedTicks;
    private int targetReplans;
    private int completedApproachReplans;
    private int completedExpandedNodes;
    private int completedUnloadedTouches;

    public FollowExecutor(
        String bot,
        String actionId,
        String targetSpec,
        double keepRadius,
        double replanDistance,
        int acquireRadius,
        WorldView world,
        MovementControls movement,
        EngageExecutor.TargetSource targets,
        NavigateExecutor.PositionSource positions,
        NavigateExecutor.EventSink events,
        ActionRuntime runtime,
        int timeoutTicks
    ) {
        if (targetSpec == null || targetSpec.isBlank()) {
            throw new IllegalArgumentException("target_spec must not be blank");
        }
        if (keepRadius < 0.0 || keepRadius > 32.0) {
            throw new IllegalArgumentException("keep_radius is out of bounds");
        }
        if (replanDistance < 0.5 || replanDistance > 16.0) {
            throw new IllegalArgumentException("replan_distance is out of bounds");
        }
        if (acquireRadius < 1 || acquireRadius > 64) {
            throw new IllegalArgumentException("acquire_radius is out of bounds");
        }
        if (timeoutTicks < 1) {
            throw new IllegalArgumentException("timeout_ticks must be positive");
        }
        this.bot = bot;
        this.actionId = actionId;
        this.targetSpec = targetSpec;
        this.keepRadius = keepRadius;
        this.replanDistance = replanDistance;
        this.acquireRadius = acquireRadius;
        this.world = world;
        this.movement = movement;
        this.targets = targets;
        this.positions = positions;
        this.events = events;
        this.runtime = runtime;
        this.timeoutTicks = timeoutTicks;
    }

    @Override
    public void tick(int serverTick) {
        elapsedTicks++;
        if (runtime.cancelRequested(actionId)) {
            finish(serverTick, ActionRuntime.CLASS_CANCELED, "cancel_requested", null);
            return;
        }

        NavigateExecutor.PositionSource.Position position = positions.position(bot);
        if (position == null) {
            finish(serverTick, ActionRuntime.CLASS_FAILED, "body_missing", null);
            return;
        }
        if (!refreshTarget(serverTick)) {
            return;
        }

        double distance = distance(position, lockedTarget);
        if (elapsedTicks > timeoutTicks) {
            finish(
                serverTick,
                distance <= keepRadius ? ActionRuntime.CLASS_COMPLETED : ActionRuntime.CLASS_TIMEOUT,
                distance <= keepRadius ? "arrived" : "timeout",
                distance
            );
            return;
        }

        if (distance <= keepRadius) {
            haltApproach();
            return;
        }

        if (approach == null || plannedTarget == null || movedEnough(plannedTarget, lockedTarget)) {
            if (approach != null) {
                recordAndHaltApproach();
                targetReplans++;
            }
            approach = new ApproachController(
                bot,
                actionId,
                new Goal.Near(
                    lockedTarget.blockX(),
                    lockedTarget.blockY(),
                    lockedTarget.blockZ(),
                    Math.max(0.5, keepRadius)
                ),
                world,
                movement,
                events,
                APPROACH_REPLAN_LIMIT,
                0.35
            );
            plannedTarget = lockedTarget;
        }

        ApproachController.Outcome outcome = approach.tick(
            serverTick, position.x(), position.y(), position.z()
        );
        if (outcome.status() == ApproachController.Status.FAILED) {
            finish(serverTick, ActionRuntime.CLASS_FAILED, outcome.reason(), distance);
        } else if (outcome.status() == ApproachController.Status.COMPLETED) {
            recordAndHaltApproach();
        }
    }

    private boolean refreshTarget(int serverTick) {
        EngageExecutor.Lookup lookup;
        if (lockedTarget == null) {
            lookup = targets.acquire(bot, targetSpec, acquireRadius);
            if (lookup.target() == null) {
                finish(
                    serverTick,
                    ActionRuntime.CLASS_FAILED,
                    lookup.reasonOr("target_not_found"),
                    null
                );
                return false;
            }
            lockedTarget = lookup.target();
            JsonObject data = new JsonObject();
            data.addProperty("target_id", lockedTarget.id());
            data.addProperty("target_type", lockedTarget.type());
            data.addProperty("target_name", lockedTarget.name());
            events.emit(bot, serverTick, "follow_target_acquired", actionId, data);
            return true;
        }

        lookup = targets.refresh(bot, lockedTarget.id(), acquireRadius);
        if (lookup.target() == null) {
            finish(serverTick, ActionRuntime.CLASS_FAILED, "target_lost", null);
            return false;
        }
        lockedTarget = lookup.target();
        return true;
    }

    private void finish(int serverTick, String classification, String reason, Double finalDistance) {
        recordAndHaltApproach();
        JsonObject facts = new JsonObject();
        facts.addProperty("success", ActionRuntime.CLASS_COMPLETED.equals(classification));
        facts.addProperty("target_spec", targetSpec);
        facts.addProperty("keep_radius", keepRadius);
        facts.addProperty("reason", reason);
        facts.addProperty("elapsed_ticks", elapsedTicks);
        facts.addProperty("target_replans", targetReplans);
        facts.addProperty("replans", completedApproachReplans);
        facts.addProperty("expanded_nodes", completedExpandedNodes);
        facts.addProperty("unloaded_touches", completedUnloadedTouches);
        if (finalDistance == null) {
            facts.add("final_distance", JsonNull.INSTANCE);
        } else {
            facts.addProperty("final_distance", finalDistance);
        }
        if (lockedTarget == null) {
            facts.add("target_id", JsonNull.INSTANCE);
            facts.add("target_type", JsonNull.INSTANCE);
            facts.add("target_name", JsonNull.INSTANCE);
            facts.add("target_pos", JsonNull.INSTANCE);
        } else {
            facts.addProperty("target_id", lockedTarget.id());
            facts.addProperty("target_type", lockedTarget.type());
            facts.addProperty("target_name", lockedTarget.name());
            JsonArray targetPos = new JsonArray();
            targetPos.add(lockedTarget.x());
            targetPos.add(lockedTarget.y());
            targetPos.add(lockedTarget.z());
            facts.add("target_pos", targetPos);
        }
        NavigateExecutor.PositionSource.Position finalPosition = positions.position(bot);
        if (finalPosition != null) {
            facts.addProperty("final_x", finalPosition.x());
            facts.addProperty("final_y", finalPosition.y());
            facts.addProperty("final_z", finalPosition.z());
        }
        runtime.finish(bot, actionId, classification, facts, serverTick);
    }

    private void haltApproach() {
        if (approach != null) {
            approach.halt();
            approach = null;
            plannedTarget = null;
        }
    }

    private void recordAndHaltApproach() {
        if (approach == null) {
            return;
        }
        completedApproachReplans += approach.replans();
        completedExpandedNodes += approach.expandedNodes();
        completedUnloadedTouches += approach.unloadedTouches();
        haltApproach();
    }

    private boolean movedEnough(EngageExecutor.Target first, EngageExecutor.Target second) {
        double dx = first.x() - second.x();
        double dy = first.y() - second.y();
        double dz = first.z() - second.z();
        return dx * dx + dy * dy + dz * dz >= replanDistance * replanDistance;
    }

    private static double distance(
        NavigateExecutor.PositionSource.Position position,
        EngageExecutor.Target target
    ) {
        double dx = position.x() - target.x();
        double dy = position.y() - target.y();
        double dz = position.z() - target.z();
        return Math.sqrt(dx * dx + dy * dy + dz * dz);
    }
}
