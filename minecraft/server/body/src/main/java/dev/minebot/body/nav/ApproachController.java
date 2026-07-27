package dev.minebot.body.nav;

import com.google.gson.JsonObject;
import dev.minebot.body.nav.AStarPathfinder.Waypoint;

import java.util.List;

/**
 * The reusable plan-follow-replan core: tick-sliced planning, waypoint
 * following, bounded replans on deviation/stuckness, and partial-path
 * continuation. It reports an outcome to its owner instead of terminating
 * the action — NavigateExecutor wraps it with a terminal, CollectExecutor
 * runs one per candidate approach.
 */
public final class ApproachController {
    public enum Status {
        WORKING,
        COMPLETED,
        FAILED
    }

    public record Outcome(Status status, String reason) {
        static final Outcome WORKING_OUTCOME = new Outcome(Status.WORKING, "working");
    }

    private enum Phase {
        PLANNING,
        FOLLOWING
    }

    private static final int JUMP_COOLDOWN_TICKS = 5;

    private final String bot;
    private final String actionId;
    private final Goal goal;
    private final WorldView world;
    private final MovementControls controls;
    private final NavigateExecutor.EventSink events;
    private final int replanLimit;
    private final double finalWaypointReachDistance;

    private Phase phase = Phase.PLANNING;
    private AStarPathfinder pathfinder;
    private PathFollower follower;
    private boolean followingPartialPath;
    private boolean moving;
    private boolean sprinted;
    private int replans;
    private int expandedTotal;
    private int unloadedTotal;
    private int lastJumpTick = Integer.MIN_VALUE;
    private long lastStuckCell = Long.MIN_VALUE;

    public ApproachController(
        String bot,
        String actionId,
        Goal goal,
        WorldView world,
        MovementControls controls,
        NavigateExecutor.EventSink events,
        int replanLimit
    ) {
        this(
            bot,
            actionId,
            goal,
            world,
            controls,
            events,
            replanLimit,
            PathFollower.WAYPOINT_REACH_DISTANCE
        );
    }

    public ApproachController(
        String bot,
        String actionId,
        Goal goal,
        WorldView world,
        MovementControls controls,
        NavigateExecutor.EventSink events,
        int replanLimit,
        double finalWaypointReachDistance
    ) {
        this.bot = bot;
        this.actionId = actionId;
        this.goal = goal;
        this.world = world;
        this.controls = controls;
        this.events = events;
        this.replanLimit = replanLimit;
        this.finalWaypointReachDistance = finalWaypointReachDistance;
    }

    public int replans() {
        return replans;
    }

    public int expandedNodes() {
        return expandedTotal;
    }

    public int unloadedTouches() {
        return unloadedTotal;
    }

    public Outcome tick(int serverTick, double px, double py, double pz) {
        if (phase == Phase.PLANNING) {
            return planTick(serverTick, px, py, pz);
        }
        return followTick(serverTick, px, py, pz);
    }

    /** Stops any movement this controller started; owner calls before discarding it. */
    public void halt() {
        if (moving) {
            controls.stopMovement(bot);
            moving = false;
        }
    }

    private Outcome planTick(int serverTick, double px, double py, double pz) {
        if (pathfinder == null) {
            pathfinder = new AStarPathfinder(
                world, goal, (int) Math.floor(px), (int) Math.floor(py), (int) Math.floor(pz)
            );
        }
        AStarPathfinder.Result result = pathfinder.step(NavigateExecutor.NODES_PER_TICK);
        switch (result.outcome()) {
            case IN_PROGRESS -> {
                return Outcome.WORKING_OUTCOME;
            }
            case COMPLETE, PARTIAL -> {
                startFollowing(serverTick, result);
                return Outcome.WORKING_OUTCOME;
            }
            default -> {
                expandedTotal += result.expandedNodes();
                unloadedTotal += result.unloadedTouches();
                return new Outcome(Status.FAILED, result.reason());
            }
        }
    }

    private void startFollowing(int serverTick, AStarPathfinder.Result result) {
        expandedTotal += result.expandedNodes();
        unloadedTotal += result.unloadedTouches();
        followingPartialPath = result.outcome() == AStarPathfinder.Outcome.PARTIAL;
        List<Waypoint> path = result.path();
        follower = new PathFollower(path, finalWaypointReachDistance);
        pathfinder = null;
        phase = Phase.FOLLOWING;
        moving = false;
        JsonObject data = new JsonObject();
        data.addProperty("waypoints", path.size());
        data.addProperty("partial", followingPartialPath);
        data.addProperty("expanded_nodes", result.expandedNodes());
        data.addProperty("reason", result.reason());
        events.emit(bot, serverTick, "path_planned", actionId, data);
        if (!sprinted) {
            controls.sprint(bot);
            sprinted = true;
        }
    }

    private Outcome followTick(int serverTick, double px, double py, double pz) {
        PathFollower.Directive directive = follower.tick(px, py, pz);
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
                return Outcome.WORKING_OUTCOME;
            }
            case ARRIVED -> {
                halt();
                int fx = (int) Math.floor(px);
                int fy = (int) Math.floor(py);
                int fz = (int) Math.floor(pz);
                if (goal.isSatisfied(fx, fy, fz)) {
                    return new Outcome(Status.COMPLETED, "goal_satisfied");
                }
                return replanOrFail(
                    serverTick,
                    followingPartialPath ? "partial_path_continuation" : "arrival_goal_unsatisfied"
                );
            }
            case DEVIATED -> {
                halt();
                return replanOrFail(serverTick, "path_deviation");
            }
            default -> {
                halt();
                long cell = cellOf(px, py, pz);
                if (cell == lastStuckCell) {
                    return new Outcome(Status.FAILED, "stuck");
                }
                lastStuckCell = cell;
                return replanOrFail(serverTick, "stuck_recovery");
            }
        }
    }

    private Outcome replanOrFail(int serverTick, String reason) {
        if (replans >= replanLimit) {
            return new Outcome(Status.FAILED, "replan_budget_exhausted:" + reason);
        }
        replans++;
        follower = null;
        pathfinder = null;
        phase = Phase.PLANNING;
        JsonObject data = new JsonObject();
        data.addProperty("reason", reason);
        data.addProperty("replans", replans);
        events.emit(bot, serverTick, "replan_started", actionId, data);
        return Outcome.WORKING_OUTCOME;
    }

    private static long cellOf(double px, double py, double pz) {
        return (((long) Math.floor(px) & 0x3FFFFFF) << 38)
            | (((long) Math.floor(pz) & 0x3FFFFFF) << 12)
            | ((long) Math.floor(py) & 0xFFF);
    }
}
