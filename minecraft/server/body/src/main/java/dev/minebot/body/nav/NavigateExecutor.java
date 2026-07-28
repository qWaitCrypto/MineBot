package dev.minebot.body.nav;

import com.google.gson.JsonObject;
import dev.minebot.body.action.ActionRuntime;

/**
 * Runs one NAVIGATE action: the shared {@link ApproachController} does the
 * plan-follow-replan work; this wrapper owns cancellation, timeout, missing
 * body, and the single typed terminal with observed facts.
 */
public final class NavigateExecutor implements ActionRuntime.TickExecutor {
    /** Frozen in tests/fixtures/java_body_budgets.json. */
    public static final int NODES_PER_TICK = 2_000;
    public static final int REPLAN_LIMIT = 5;
    /** Frozen in tests/fixtures/java_body_budgets.json. */
    public static final int DEFAULT_TIMEOUT_TICKS = 2_400;

    /** Observed bot position; null when the bot is gone. */
    public interface PositionSource {
        record Position(double x, double y, double z) {
            private static final double FEET_Y_INTEGER_EPSILON = 1.0e-4;

            public int blockX() {
                return (int) Math.floor(x);
            }

            public int blockZ() {
                return (int) Math.floor(z);
            }

            /**
             * The server may report a standing player's Y infinitesimally
             * below an integer. Snap only that numerical tail upward before
             * mapping the player's feet to a voxel.
             */
            public int feetBlockY() {
                double nearestInteger = Math.rint(y);
                if (Math.abs(y - nearestInteger) <= FEET_Y_INTEGER_EPSILON) {
                    return (int) nearestInteger;
                }
                return (int) Math.floor(y);
            }
        }

        Position position(String botName);
    }

    /** Emits protocol events; bound to the channel's publish in production. */
    public interface EventSink {
        void emit(String bot, int tick, String name, String actionId, JsonObject data);
    }

    private final String bot;
    private final String actionId;
    private final Goal goal;
    private final PositionSource positions;
    private final ActionRuntime runtime;
    private final int timeoutTicks;
    private final ApproachController approach;

    private int elapsedTicks;

    public NavigateExecutor(
        String bot,
        String actionId,
        Goal goal,
        WorldView world,
        MovementControls controls,
        PositionSource positions,
        EventSink events,
        ActionRuntime runtime,
        int timeoutTicks
    ) {
        this(
            bot,
            actionId,
            goal,
            world,
            controls,
            positions,
            events,
            runtime,
            timeoutTicks,
            PathFollower.WAYPOINT_REACH_DISTANCE
        );
    }

    public NavigateExecutor(
        String bot,
        String actionId,
        Goal goal,
        WorldView world,
        MovementControls controls,
        PositionSource positions,
        EventSink events,
        ActionRuntime runtime,
        int timeoutTicks,
        double finalReachDistance
    ) {
        this.bot = bot;
        this.actionId = actionId;
        this.goal = goal;
        this.positions = positions;
        this.runtime = runtime;
        this.timeoutTicks = timeoutTicks;
        this.approach = new ApproachController(
            bot,
            actionId,
            goal,
            world,
            controls,
            events,
            REPLAN_LIMIT,
            finalReachDistance
        );
    }

    @Override
    public void tick(int serverTick) {
        elapsedTicks++;
        if (runtime.cancelRequested(actionId)) {
            finish(serverTick, ActionRuntime.CLASS_CANCELED, "cancel_requested", null);
            return;
        }
        if (elapsedTicks > timeoutTicks) {
            finish(serverTick, ActionRuntime.CLASS_TIMEOUT, "timeout_ticks_exhausted", null);
            return;
        }
        PositionSource.Position position = positions.position(bot);
        if (position == null) {
            finish(serverTick, ActionRuntime.CLASS_FAILED, "body_missing", null);
            return;
        }
        ApproachController.Outcome outcome = approach.tick(serverTick, position.x(), position.y(), position.z());
        switch (outcome.status()) {
            case WORKING -> {
            }
            case COMPLETED -> finish(serverTick, ActionRuntime.CLASS_COMPLETED, outcome.reason(), positionFacts(position));
            case FAILED -> finish(serverTick, ActionRuntime.CLASS_FAILED, outcome.reason(), positionFacts(position));
        }
    }

    private void finish(int serverTick, String classification, String reason, JsonObject extraFacts) {
        approach.halt();
        JsonObject facts = extraFacts == null ? new JsonObject() : extraFacts;
        facts.addProperty("reason", reason);
        facts.addProperty("elapsed_ticks", elapsedTicks);
        facts.addProperty("replans", approach.replans());
        facts.addProperty("expanded_nodes", approach.expandedNodes());
        facts.addProperty("unloaded_touches", approach.unloadedTouches());
        facts.addProperty("final_reach_distance", approach.finalWaypointReachDistance());
        runtime.finish(bot, actionId, classification, facts, serverTick);
    }

    private static JsonObject positionFacts(PositionSource.Position position) {
        JsonObject facts = new JsonObject();
        facts.addProperty("final_x", position.x());
        facts.addProperty("final_y", position.y());
        facts.addProperty("final_z", position.z());
        return facts;
    }
}
