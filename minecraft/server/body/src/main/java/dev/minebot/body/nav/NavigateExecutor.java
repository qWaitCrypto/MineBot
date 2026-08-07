package dev.minebot.body.nav;

import com.google.gson.JsonObject;
import dev.minebot.body.action.ActionRuntime;
import dev.minebot.body.action.BotControls;
import dev.minebot.body.action.ExactBlockBreaker;
import dev.minebot.body.action.MutationGate;

import java.util.HashSet;
import java.util.Set;

/**
 * Runs one caller-designated NAVIGATE objective. Ordinary navigation is pure
 * movement. An explicitly enabled governed fallback may clear a bounded
 * number of player-volume blockers toward the same goal; it cannot discover a
 * target, widen the goal, place blocks, or replenish its mutation budget.
 */
public final class NavigateExecutor implements ActionRuntime.TickExecutor {
    /** Frozen in tests/fixtures/java_body_budgets.json. */
    public static final int NODES_PER_TICK = 2_000;
    public static final int REPLAN_LIMIT = 5;
    /** Frozen in tests/fixtures/java_body_budgets.json. */
    public static final int DEFAULT_TIMEOUT_TICKS = 2_400;
    public static final int BREAK_TIMEOUT_TICKS = 300;

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

            /** Snap only the numerical tail below an integer before voxel mapping. */
            public int feetBlockY() {
                return feetBlockY(y);
            }

            public static int feetBlockY(double y) {
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

    public interface BlockReader {
        String blockIdAt(int x, int y, int z);
    }

    public interface ProposalSink {
        void send(MutationGate.Proposal proposal);
    }

    public interface BreakToolSelector {
        boolean select(String bot, String blockId);
    }

    private enum Phase {
        APPROACHING,
        RECOVERY_PREFIX,
        AWAITING_VERDICT,
        BREAKING
    }

    private record BreakCandidate(
        ExcavationPlanner.Cell stand,
        ExcavationPlanner.Cell blocker
    ) {
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
    private final double finalReachDistance;
    private final ApproachController.Profile profile;
    private final ExactBlockBreaker blockBreaker;
    private final BotControls hygiene;
    private final BlockReader blocks;
    private final MutationGate gate;
    private final ProposalSink proposals;
    private final BreakToolSelector breakToolSelector;
    private final String breakContext;
    private final int maxBreaks;
    private final Set<ExcavationPlanner.Cell> deniedBlocks;

    private Phase phase = Phase.APPROACHING;
    private ApproachController approach;
    private int elapsedTicks;
    private int breakStartedTick;
    private int breakSteps;
    private int replansTotal;
    private int expandedNodesTotal;
    private int unloadedTouchesTotal;
    private final double effectiveFinalReachDistance;
    private boolean governedBreakAttempted;
    private String governedBreakFailure;
    private BreakCandidate breakCandidate;
    private String blockerId;
    private String proposalId;

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
            bot, actionId, goal, world, controls, positions, events, runtime,
            timeoutTicks, PathFollower.WAYPOINT_REACH_DISTANCE,
            ApproachController.Profile.defaults()
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
        this(
            bot, actionId, goal, world, controls, positions, events, runtime,
            timeoutTicks, finalReachDistance, ApproachController.Profile.defaults()
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
        double finalReachDistance,
        ApproachController.Profile profile
    ) {
        this(
            bot, actionId, goal, world, controls, positions, events, runtime,
            timeoutTicks, finalReachDistance, profile,
            null, null, null, null, null, null, null, 0, Set.of()
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
        double finalReachDistance,
        ApproachController.Profile profile,
        ExactBlockBreaker blockBreaker,
        BotControls hygiene,
        BlockReader blocks,
        MutationGate gate,
        ProposalSink proposals,
        BreakToolSelector breakToolSelector,
        String breakContext,
        int maxBreaks,
        Set<ExcavationPlanner.Cell> deniedBlocks
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
        this.finalReachDistance = finalReachDistance;
        this.profile = profile;
        this.blockBreaker = blockBreaker;
        this.hygiene = hygiene;
        this.blocks = blocks;
        this.gate = gate;
        this.proposals = proposals;
        this.breakToolSelector = breakToolSelector;
        this.breakContext = breakContext;
        this.maxBreaks = Math.max(0, maxBreaks);
        this.deniedBlocks = new HashSet<>(deniedBlocks);
        this.approach = newApproach(goal, finalReachDistance);
        this.effectiveFinalReachDistance = Math.min(
            finalReachDistance, goal.finalReachDistanceLimit()
        );
    }

    @Override
    public void tick(int serverTick) {
        elapsedTicks++;
        if (runtime.cancelRequested(actionId)) {
            finish(serverTick, ActionRuntime.CLASS_CANCELED, "cancel_requested", null, null);
            return;
        }
        if (elapsedTicks > timeoutTicks) {
            finish(serverTick, ActionRuntime.CLASS_TIMEOUT, "timeout_ticks_exhausted", null, null);
            return;
        }
        PositionSource.Position position = positions.position(bot);
        if (position == null) {
            finish(serverTick, ActionRuntime.CLASS_FAILED, "body_missing", null, null);
            return;
        }
        switch (phase) {
            case APPROACHING -> approachTick(serverTick, position);
            case RECOVERY_PREFIX -> recoveryPrefixTick(serverTick, position);
            case AWAITING_VERDICT -> verdictTick(serverTick, position);
            case BREAKING -> breakingTick(serverTick, position);
        }
    }

    private void approachTick(int serverTick, PositionSource.Position position) {
        ApproachController.Outcome outcome = approach.tick(
            serverTick, position.x(), position.y(), position.z()
        );
        if (outcome.status() == ApproachController.Status.WORKING) {
            return;
        }
        recordAndHaltApproach();
        if (outcome.status() == ApproachController.Status.COMPLETED) {
            finish(
                serverTick,
                ActionRuntime.CLASS_COMPLETED,
                outcome.reason(),
                null,
                positionFacts(position)
            );
            return;
        }
        if (canGovernedBreak() && beginGovernedBreak(serverTick, position)) {
            return;
        }
        String reason = governedBreakAttempted && breakSteps >= maxBreaks
            ? "mutation_budget_exhausted"
            : "no_path";
        String detail = governedBreakFailure != null
            ? governedBreakFailure
            : outcome.reason();
        finish(serverTick, ActionRuntime.CLASS_FAILED, reason, detail, positionFacts(position));
    }

    private boolean canGovernedBreak() {
        return maxBreaks > breakSteps
            && blockBreaker != null
            && hygiene != null
            && blocks != null
            && gate != null
            && proposals != null
            && breakToolSelector != null
            && breakContext != null;
    }

    private boolean beginGovernedBreak(int serverTick, PositionSource.Position position) {
        governedBreakAttempted = true;
        ExcavationPlanner.Cell start = new ExcavationPlanner.Cell(
            position.blockX(), position.feetBlockY(), position.blockZ()
        );
        ExcavationPlanner.RouteResult result = ExcavationPlanner.planToGoal(
            world,
            start,
            goal,
            deniedBlocks,
            maxBreaks - breakSteps,
            Math.min(ExcavationPlanner.DEFAULT_NODE_CAP, profile.maxExpand()),
            ExcavationPlanner.DEFAULT_STEP_CAP,
            profile.movement()
        );
        expandedNodesTotal += result.expandedNodes();
        if (!result.success()) {
            governedBreakFailure = result.reason();
            return false;
        }

        ExcavationPlanner.Cell standBefore = start;
        BreakCandidate selected = null;
        for (ExcavationPlanner.Step step : result.plan().steps()) {
            if (!step.blockers().isEmpty()) {
                selected = new BreakCandidate(standBefore, step.blockers().get(0));
                break;
            }
            standBefore = step.stand();
        }
        if (selected == null) {
            governedBreakFailure = "no_governed_blocker";
            return false;
        }
        breakCandidate = selected;
        if (!atCell(position, selected.stand())) {
            approach = newApproach(
                new Goal.Near(
                    selected.stand().x(), selected.stand().y(), selected.stand().z(), 0.1
                ),
                0.15
            );
            phase = Phase.RECOVERY_PREFIX;
            return true;
        }
        return proposeBreak(serverTick, position);
    }

    private void recoveryPrefixTick(int serverTick, PositionSource.Position position) {
        ApproachController.Outcome outcome = approach.tick(
            serverTick, position.x(), position.y(), position.z()
        );
        if (outcome.status() == ApproachController.Status.WORKING) {
            return;
        }
        recordAndHaltApproach();
        if (outcome.status() == ApproachController.Status.FAILED) {
            finish(serverTick, ActionRuntime.CLASS_FAILED, "no_path", outcome.reason(), positionFacts(position));
            return;
        }
        proposeBreak(serverTick, position);
    }

    private boolean proposeBreak(int serverTick, PositionSource.Position position) {
        ExcavationPlanner.Cell blocker = breakCandidate.blocker();
        if (deniedBlocks.contains(blocker)) {
            governedBreakFailure = "known_governance_denial";
            finish(
                serverTick,
                ActionRuntime.CLASS_UNSAFE,
                "governance_denied",
                governedBreakFailure,
                positionFacts(position)
            );
            return true;
        }
        String observed = blocks.blockIdAt(blocker.x(), blocker.y(), blocker.z());
        if (observed == null) {
            finish(
                serverTick,
                ActionRuntime.CLASS_FAILED,
                "no_path",
                "blocker_unloaded",
                positionFacts(position)
            );
            return true;
        }
        if (world.kindAt(blocker.x(), blocker.y(), blocker.z()) != WorldView.NodeKind.SOLID) {
            restartMainApproach();
            return true;
        }
        if (world.isBlockClearanceHazardous(blocker.x(), blocker.y(), blocker.z())) {
            deniedBlocks.add(blocker);
            governedBreakFailure = "hazardous_block_clearance";
            return false;
        }
        blockerId = observed;
        MutationGate.Proposal proposal = gate.propose(
            bot,
            actionId,
            "break",
            blocker.x(),
            blocker.y(),
            blocker.z(),
            blockerId,
            breakContext,
            serverTick
        );
        proposalId = proposal.proposalId();
        proposals.send(proposal);
        JsonObject data = blockerFacts();
        data.addProperty("proposal_id", proposalId);
        data.addProperty("block_id", blockerId);
        data.addProperty("break_budget_remaining", maxBreaks - breakSteps);
        events.emit(bot, serverTick, "navigate_governed_break_proposed", actionId, data);
        phase = Phase.AWAITING_VERDICT;
        return true;
    }

    private void verdictTick(int serverTick, PositionSource.Position position) {
        MutationGate.State state = gate.poll(proposalId, serverTick);
        if (state == MutationGate.State.PENDING) {
            return;
        }
        if (state != MutationGate.State.ALLOWED) {
            String detail = state == MutationGate.State.DENIED
                ? gate.reason(proposalId)
                : "governance_verdict_timeout";
            deniedBlocks.add(breakCandidate.blocker());
            discardProposal();
            finish(
                serverTick,
                ActionRuntime.CLASS_UNSAFE,
                "governance_denied",
                detail,
                positionFacts(position)
            );
            return;
        }
        discardProposal();

        ExcavationPlanner.Cell blocker = breakCandidate.blocker();
        String observed = blocks.blockIdAt(blocker.x(), blocker.y(), blocker.z());
        if (observed == null) {
            finish(
                serverTick,
                ActionRuntime.CLASS_FAILED,
                "no_path",
                "approved_block_unloaded",
                positionFacts(position)
            );
            return;
        }
        if (!blockerId.equals(observed)) {
            if (world.kindAt(blocker.x(), blocker.y(), blocker.z()) == WorldView.NodeKind.PASSABLE
                || world.kindAt(blocker.x(), blocker.y(), blocker.z()) == WorldView.NodeKind.CLIMBABLE) {
                restartMainApproach();
            } else {
                finish(serverTick, ActionRuntime.CLASS_FAILED, "no_path", "approved_block_changed", positionFacts(position));
            }
            return;
        }
        if (world.isBlockClearanceHazardous(blocker.x(), blocker.y(), blocker.z())) {
            deniedBlocks.add(blocker);
            finish(
                serverTick,
                ActionRuntime.CLASS_FAILED,
                "no_path",
                "hazardous_block_clearance",
                positionFacts(position)
            );
            return;
        }
        if (!breakToolSelector.select(bot, blockerId)) {
            finish(serverTick, ActionRuntime.CLASS_FAILED, "no_path", "tool_selection_failed", positionFacts(position));
            return;
        }
        controls.lookAt(bot, blocker.x() + 0.5, blocker.y() + 0.5, blocker.z() + 0.5);
        ExactBlockBreaker.Outcome outcome = blockBreaker.begin(
            bot, blocker.x(), blocker.y(), blocker.z(), blockerId, serverTick
        );
        if (outcome.state() == ExactBlockBreaker.State.FAILED) {
            finish(serverTick, ActionRuntime.CLASS_FAILED, "no_path", outcome.reason(), positionFacts(position));
            return;
        }
        breakStartedTick = serverTick;
        phase = Phase.BREAKING;
    }

    private void breakingTick(int serverTick, PositionSource.Position position) {
        ExcavationPlanner.Cell blocker = breakCandidate.blocker();
        String observed = blocks.blockIdAt(blocker.x(), blocker.y(), blocker.z());
        if (observed == null) {
            blockBreaker.abort(bot);
            hygiene.clearAll(bot);
            finish(
                serverTick,
                ActionRuntime.CLASS_FAILED,
                "no_path",
                "blocker_unloaded_during_break",
                positionFacts(position)
            );
            return;
        }
        if (!blockerId.equals(observed)) {
            blockBreaker.abort(bot);
            hygiene.clearAll(bot);
            WorldView.NodeKind kind = world.kindAt(blocker.x(), blocker.y(), blocker.z());
            if (kind != WorldView.NodeKind.PASSABLE
                && kind != WorldView.NodeKind.CLIMBABLE) {
                finish(serverTick, ActionRuntime.CLASS_FAILED, "no_path", "blocker_changed_to_solid", positionFacts(position));
                return;
            }
            breakSteps++;
            JsonObject data = blockerFacts();
            data.addProperty("block_id", blockerId);
            data.addProperty("break_steps", breakSteps);
            data.addProperty("break_budget_remaining", maxBreaks - breakSteps);
            events.emit(bot, serverTick, "navigate_governed_break_verified", actionId, data);
            restartMainApproach();
            return;
        }
        if (world.isBlockClearanceHazardous(blocker.x(), blocker.y(), blocker.z())) {
            blockBreaker.abort(bot);
            hygiene.clearAll(bot);
            deniedBlocks.add(blocker);
            finish(
                serverTick,
                ActionRuntime.CLASS_FAILED,
                "no_path",
                "hazardous_block_clearance",
                positionFacts(position)
            );
            return;
        }
        ExactBlockBreaker.Outcome outcome = blockBreaker.tick(bot, serverTick);
        if (outcome.state() == ExactBlockBreaker.State.FAILED) {
            finish(serverTick, ActionRuntime.CLASS_FAILED, "no_path", outcome.reason(), positionFacts(position));
            return;
        }
        if (serverTick - breakStartedTick > BREAK_TIMEOUT_TICKS) {
            finish(serverTick, ActionRuntime.CLASS_FAILED, "no_path", "break_timeout", positionFacts(position));
        }
    }

    private void restartMainApproach() {
        breakCandidate = null;
        blockerId = null;
        approach = newApproach(goal, finalReachDistance);
        phase = Phase.APPROACHING;
    }

    private ApproachController newApproach(Goal target, double reachDistance) {
        return new ApproachController(
            bot,
            actionId,
            target,
            world,
            controls,
            events,
            REPLAN_LIMIT,
            reachDistance,
            false,
            false,
            profile
        );
    }

    private void recordAndHaltApproach() {
        if (approach == null) {
            return;
        }
        replansTotal += approach.replans();
        expandedNodesTotal += approach.expandedNodes();
        unloadedTouchesTotal += approach.unloadedTouches();
        approach.halt();
        approach = null;
    }

    private void finish(
        int serverTick,
        String classification,
        String reason,
        String detail,
        JsonObject extraFacts
    ) {
        recordAndHaltApproach();
        if (blockBreaker != null) {
            blockBreaker.abort(bot);
        }
        if (hygiene != null) {
            hygiene.clearAll(bot);
        }
        discardProposal();
        JsonObject facts = extraFacts == null ? new JsonObject() : extraFacts;
        facts.addProperty("reason", reason);
        facts.addProperty("elapsed_ticks", elapsedTicks);
        facts.addProperty("replans", replansTotal);
        facts.addProperty("expanded_nodes", expandedNodesTotal);
        facts.addProperty("unloaded_touches", unloadedTouchesTotal);
        facts.addProperty("unloaded_chunk_count", unloadedTouchesTotal);
        facts.addProperty("final_reach_distance", effectiveFinalReachDistance);
        facts.addProperty("attempts_consumed", replansTotal + breakSteps);
        facts.addProperty("budget_remaining", Math.max(0, maxBreaks - breakSteps));
        facts.addProperty("break_steps", breakSteps);
        facts.addProperty("mutations_completed", breakSteps);
        facts.addProperty("governed_break_attempted", governedBreakAttempted);
        if (governedBreakFailure != null) {
            facts.addProperty("governed_break_failure", governedBreakFailure);
        }
        if (detail != null) {
            facts.addProperty("failure_detail", detail);
        }
        PositionSource.Position finalPosition = positions.position(bot);
        if (finalPosition != null) {
            facts.addProperty(
                "closest_approach_distance",
                goal.heuristic(
                    finalPosition.blockX(), finalPosition.feetBlockY(), finalPosition.blockZ()
                ) / Goal.HEURISTIC_TICKS_PER_BLOCK
            );
        }
        runtime.finish(bot, actionId, classification, facts, serverTick);
    }

    private void discardProposal() {
        if (proposalId != null && gate != null) {
            gate.discard(proposalId);
        }
        proposalId = null;
    }

    private JsonObject blockerFacts() {
        JsonObject facts = new JsonObject();
        if (breakCandidate != null) {
            facts.addProperty("blocker_x", breakCandidate.blocker().x());
            facts.addProperty("blocker_y", breakCandidate.blocker().y());
            facts.addProperty("blocker_z", breakCandidate.blocker().z());
        }
        return facts;
    }

    private static boolean atCell(
        PositionSource.Position position,
        ExcavationPlanner.Cell cell
    ) {
        return position.blockX() == cell.x()
            && position.feetBlockY() == cell.y()
            && position.blockZ() == cell.z();
    }

    private static JsonObject positionFacts(PositionSource.Position position) {
        JsonObject facts = new JsonObject();
        facts.addProperty("final_x", position.x());
        facts.addProperty("final_y", position.y());
        facts.addProperty("final_z", position.z());
        return facts;
    }
}
