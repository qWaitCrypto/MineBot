package dev.minebot.body.nav;

import com.google.gson.JsonObject;
import dev.minebot.body.action.ActionRuntime;
import dev.minebot.body.nav.AStarPathfinder.Waypoint;

import java.util.List;

/**
 * Runs one NAVIGATE action: tick-sliced planning, waypoint following through
 * the public command adapter, bounded replans on deviation or stuckness, and
 * a single typed terminal with observed facts. Partial paths continue with a
 * fresh search from the reached frontier; repeated stuckness at the same cell
 * is an honest {@code stuck} terminal, never an endless retry.
 */
public final class NavigateExecutor implements ActionRuntime.TickExecutor {
    public static final int NODES_PER_TICK = 2_000;
    public static final int REPLAN_LIMIT = 5;
    public static final int DEFAULT_TIMEOUT_TICKS = 2_400;
    private static final int JUMP_COOLDOWN_TICKS = 5;

    /** Observed bot position; null when the bot is gone. */
    public interface PositionSource {
        record Position(double x, double y, double z) {
        }

        Position position(String botName);
    }

    /** Emits protocol events; bound to the channel's publish in production. */
    public interface EventSink {
        void emit(String bot, int tick, String name, String actionId, JsonObject data);
    }

    private enum Phase {
        PLANNING,
        FOLLOWING
    }

    private final String bot;
    private final String actionId;
    private final Goal goal;
    private final WorldView world;
    private final MovementControls controls;
    private final PositionSource positions;
    private final EventSink events;
    private final ActionRuntime runtime;
    private final int timeoutTicks;

    private Phase phase = Phase.PLANNING;
    private AStarPathfinder pathfinder;
    private PathFollower follower;
    private boolean followingPartialPath;
    private boolean moving;
    private int elapsedTicks;
    private int replans;
    private int expandedTotal;
    private int unloadedTotal;
    private int lastJumpTick = Integer.MIN_VALUE;
    private long lastStuckCell = Long.MIN_VALUE;

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
        this.bot = bot;
        this.actionId = actionId;
        this.goal = goal;
        this.world = world;
        this.controls = controls;
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
        if (elapsedTicks > timeoutTicks) {
            finish(serverTick, ActionRuntime.CLASS_TIMEOUT, "timeout_ticks_exhausted", null);
            return;
        }
        PositionSource.Position position = positions.position(bot);
        if (position == null) {
            finish(serverTick, ActionRuntime.CLASS_FAILED, "body_missing", null);
            return;
        }
        if (phase == Phase.PLANNING) {
            planTick(serverTick, position);
            return;
        }
        followTick(serverTick, position);
    }

    private void planTick(int serverTick, PositionSource.Position position) {
        if (pathfinder == null) {
            pathfinder = new AStarPathfinder(
                world,
                goal,
                (int) Math.floor(position.x()),
                (int) Math.floor(position.y()),
                (int) Math.floor(position.z())
            );
        }
        AStarPathfinder.Result result = pathfinder.step(NODES_PER_TICK);
        switch (result.outcome()) {
            case IN_PROGRESS -> {
            }
            case COMPLETE, PARTIAL -> startFollowing(serverTick, result);
            case NO_PATH -> {
                expandedTotal += result.expandedNodes();
                unloadedTotal += result.unloadedTouches();
                finish(serverTick, ActionRuntime.CLASS_FAILED, result.reason(), null);
            }
        }
    }

    private void startFollowing(int serverTick, AStarPathfinder.Result result) {
        expandedTotal += result.expandedNodes();
        unloadedTotal += result.unloadedTouches();
        followingPartialPath = result.outcome() == AStarPathfinder.Outcome.PARTIAL;
        List<Waypoint> path = result.path();
        follower = new PathFollower(path);
        pathfinder = null;
        phase = Phase.FOLLOWING;
        moving = false;
        JsonObject data = new JsonObject();
        data.addProperty("waypoints", path.size());
        data.addProperty("partial", followingPartialPath);
        data.addProperty("expanded_nodes", result.expandedNodes());
        data.addProperty("reason", result.reason());
        events.emit(bot, serverTick, "path_planned", actionId, data);
        controls.sprint(bot);
    }

    private void followTick(int serverTick, PositionSource.Position position) {
        PathFollower.Directive directive = follower.tick(position.x(), position.y(), position.z());
        switch (directive.state()) {
            case CONTINUE -> {
                Waypoint target = directive.lookTarget();
                controls.lookAt(bot, target.x() + 0.5, target.y() + 0.5, target.z() + 0.5);
                if (!moving) {
                    controls.moveForward(bot);
                    moving = true;
                }
                if (directive.jump() && serverTick - lastJumpTick >= JUMP_COOLDOWN_TICKS) {
                    controls.jumpOnce(bot);
                    lastJumpTick = serverTick;
                }
            }
            case ARRIVED -> {
                controls.stopMovement(bot);
                moving = false;
                int fx = (int) Math.floor(position.x());
                int fy = (int) Math.floor(position.y());
                int fz = (int) Math.floor(position.z());
                if (goal.isSatisfied(fx, fy, fz)) {
                    JsonObject facts = positionFacts(position);
                    finish(serverTick, ActionRuntime.CLASS_COMPLETED, "goal_satisfied", facts);
                    return;
                }
                // End of a partial path, or an arrival that does not satisfy
                // the goal predicate: continue with a fresh search from here.
                replanOrFail(serverTick, followingPartialPath ? "partial_path_continuation" : "arrival_goal_unsatisfied", position);
            }
            case DEVIATED -> {
                controls.stopMovement(bot);
                moving = false;
                replanOrFail(serverTick, "path_deviation", position);
            }
            case STUCK -> {
                controls.stopMovement(bot);
                moving = false;
                long cell = cellOf(position);
                if (cell == lastStuckCell) {
                    finish(serverTick, ActionRuntime.CLASS_FAILED, "stuck", positionFacts(position));
                    return;
                }
                lastStuckCell = cell;
                replanOrFail(serverTick, "stuck_recovery", position);
            }
        }
    }

    private void replanOrFail(int serverTick, String reason, PositionSource.Position position) {
        if (replans >= REPLAN_LIMIT) {
            finish(serverTick, ActionRuntime.CLASS_FAILED, "replan_budget_exhausted:" + reason, positionFacts(position));
            return;
        }
        replans++;
        follower = null;
        pathfinder = null;
        phase = Phase.PLANNING;
        JsonObject data = new JsonObject();
        data.addProperty("reason", reason);
        data.addProperty("replans", replans);
        events.emit(bot, serverTick, "replan_started", actionId, data);
    }

    private void finish(int serverTick, String classification, String reason, JsonObject extraFacts) {
        JsonObject facts = extraFacts == null ? new JsonObject() : extraFacts;
        facts.addProperty("reason", reason);
        facts.addProperty("elapsed_ticks", elapsedTicks);
        facts.addProperty("replans", replans);
        facts.addProperty("expanded_nodes", expandedTotal);
        facts.addProperty("unloaded_touches", unloadedTotal);
        runtime.finish(bot, actionId, classification, facts, serverTick);
    }

    private static JsonObject positionFacts(PositionSource.Position position) {
        JsonObject facts = new JsonObject();
        facts.addProperty("final_x", position.x());
        facts.addProperty("final_y", position.y());
        facts.addProperty("final_z", position.z());
        return facts;
    }

    private static long cellOf(PositionSource.Position position) {
        return (((long) Math.floor(position.x()) & 0x3FFFFFF) << 38)
            | (((long) Math.floor(position.z()) & 0x3FFFFFF) << 12)
            | ((long) Math.floor(position.y()) & 0xFFF);
    }
}
