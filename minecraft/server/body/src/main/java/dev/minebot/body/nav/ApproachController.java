package dev.minebot.body.nav;

import com.google.gson.JsonArray;
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

    private static final double PLAYER_EYE_HEIGHT = 1.62;
    private static final double STABLE_STAND_Y_TOLERANCE = 0.08;
    private static final int STABLE_STAND_TICKS = 2;

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
    private boolean continuousJumping;
    private int replans;
    private int expandedTotal;
    private int unloadedTotal;
    private int stableArrivalTicks;
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
        this.finalWaypointReachDistance = Math.min(
            finalWaypointReachDistance,
            goal.finalReachDistanceLimit()
        );
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

    public double finalWaypointReachDistance() {
        return finalWaypointReachDistance;
    }

    public Outcome tick(int serverTick, double px, double py, double pz) {
        if (phase == Phase.PLANNING) {
            return planTick(serverTick, px, py, pz);
        }
        return followTick(serverTick, px, py, pz);
    }

    /** Stops any movement this controller started; owner calls before discarding it. */
    public void halt() {
        if (moving || continuousJumping) {
            controls.stopMovement(bot);
        }
        moving = false;
        continuousJumping = false;
    }

    private Outcome planTick(int serverTick, double px, double py, double pz) {
        maintainPlanningPosture(px, py, pz);
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
        stableArrivalTicks = 0;
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
        JsonArray preview = new JsonArray();
        for (int index = 0; index < Math.min(path.size(), 16); index++) {
            Waypoint waypoint = path.get(index);
            JsonArray point = new JsonArray();
            point.add(waypoint.x());
            point.add(waypoint.y());
            point.add(waypoint.z());
            preview.add(point);
        }
        data.add("waypoint_preview", preview);
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
                boolean swimming = swimmingAt(px, py, pz);
                double lookY = swimming ? py + PLAYER_EYE_HEIGHT : target.y() + 0.5;
                boolean shouldHoldJump = swimming || directive.jump();
                if (shouldHoldJump && !continuousJumping) {
                    controls.jumpContinuous(bot);
                    continuousJumping = true;
                } else if (!shouldHoldJump && continuousJumping) {
                    // Carpet's public stop command releases continuous jump as
                    // well as forward movement. Re-engage the route controls
                    // below once the upward movement is complete.
                    controls.stopMovement(bot);
                    controls.sprint(bot);
                    moving = false;
                    continuousJumping = false;
                }
                if (!moving) {
                    JsonObject data = new JsonObject();
                    JsonArray from = new JsonArray();
                    from.add(px);
                    from.add(py);
                    from.add(pz);
                    JsonArray waypoint = new JsonArray();
                    waypoint.add(target.x());
                    waypoint.add(target.y());
                    waypoint.add(target.z());
                    data.add("from", from);
                    data.add("waypoint", waypoint);
                    data.addProperty("swimming", swimming);
                    data.addProperty("look_y", lookY);
                    events.emit(bot, serverTick, "path_follow_started", actionId, data);
                }
                controls.lookAt(bot, target.x() + 0.5, lookY, target.z() + 0.5);
                if (!moving) {
                    controls.moveForward(bot);
                    moving = true;
                }
                return Outcome.WORKING_OUTCOME;
            }
            case ARRIVED -> {
                halt();
                int fx = (int) Math.floor(px);
                int fy = (int) Math.floor(py);
                int fz = (int) Math.floor(pz);
                if (goal.isSatisfied(world, fx, fy, fz)) {
                    if (isDrySupportedStand(fx, fy, fz)) {
                        if (Math.abs(py - fy) > STABLE_STAND_Y_TOLERANCE) {
                            stableArrivalTicks = 0;
                            return Outcome.WORKING_OUTCOME;
                        }
                        if (follower.finalHorizontalDistance(px, pz)
                            > finalWaypointReachDistance + 1.0e-6) {
                            stableArrivalTicks = 0;
                            return replanOrFail(serverTick, "arrival_drift");
                        }
                        stableArrivalTicks++;
                        if (stableArrivalTicks < STABLE_STAND_TICKS) {
                            return Outcome.WORKING_OUTCOME;
                        }
                    }
                    return new Outcome(Status.COMPLETED, "goal_satisfied");
                }
                stableArrivalTicks = 0;
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
        stableArrivalTicks = 0;
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

    private boolean swimmingAt(double px, double py, double pz) {
        int x = (int) Math.floor(px);
        int y = (int) Math.floor(py);
        int z = (int) Math.floor(pz);
        return world.kindAt(x, y, z) == WorldView.NodeKind.LIQUID
            || world.kindAt(x, y - 1, z) == WorldView.NodeKind.LIQUID;
    }

    private boolean isDrySupportedStand(int x, int y, int z) {
        return world.kindAt(x, y, z) == WorldView.NodeKind.PASSABLE
            && world.kindAt(x, y + 1, z) == WorldView.NodeKind.PASSABLE
            && world.kindAt(x, y - 1, z) == WorldView.NodeKind.SOLID;
    }

    private void maintainPlanningPosture(double px, double py, double pz) {
        boolean swimming = swimmingAt(px, py, pz);
        if (swimming && !continuousJumping) {
            controls.jumpContinuous(bot);
            continuousJumping = true;
        } else if (!swimming && continuousJumping) {
            controls.stopMovement(bot);
            continuousJumping = false;
        }
    }
}
