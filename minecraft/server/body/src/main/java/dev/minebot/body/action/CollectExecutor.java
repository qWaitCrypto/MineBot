package dev.minebot.body.action;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import dev.minebot.body.nav.ApproachController;
import dev.minebot.body.nav.ExcavationPlanner;
import dev.minebot.body.nav.Goal;
import dev.minebot.body.nav.MovementControls;
import dev.minebot.body.nav.NavigateExecutor;
import dev.minebot.body.nav.WorldView;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * Executes one caller-designated BREAK_COLLECT objective. The only semantic
 * target is the exact block supplied by the request. Every internal retry is
 * bounded by one non-replenishing outer resource: action ticks bound every
 * phase, approach controllers bound their replans, excavation consumes either
 * the fixed mutation budget or the fixed replan budget, and pickup consumes
 * its locate/wait tick windows. None can discover or switch to another
 * resource target.
 */
public final class CollectExecutor implements ActionRuntime.TickExecutor {
    public static final Set<String> TERMINAL_REASONS = Set.of(
        "collected",
        "target_gone",
        "target_block_mismatch",
        "no_path",
        "governance_denied",
        "excavation_budget_exhausted",
        "mutation_budget_exhausted",
        "pickup_not_observed",
        "timeout",
        "canceled",
        "body_missing"
    );
    public static final int BREAK_TIMEOUT_TICKS = 300;
    public static final int PICKUP_TIMEOUT_TICKS = 120;
    public static final int DROP_LOCATE_TIMEOUT_TICKS = 60;
    public static final int PICKUP_SETTLEMENT_TIMEOUT_TICKS = 200;
    public static final double DROP_REPLAN_DISTANCE = Goal.Pickup.MAX_UNREPLANNED_DRIFT;
    public static final double DROP_SEARCH_RADIUS = 8.0;
    public static final double PICKUP_FINAL_REACH_DISTANCE = 0.15;
    public static final int APPROACH_REPLAN_LIMIT = 3;
    public static final int MAX_EXCAVATION_REPLANS = 3;
    public static final int EXCAVATION_MOVE_TIMEOUT_TICKS = 100;
    private static final int DESCENT_STABLE_TICKS = 3;

    /** Live block truth; null id when the position is unloaded. */
    public interface BlockReader {
        String blockIdAt(int x, int y, int z);
    }

    /** Authoritative server-side inventory count for an item id. */
    public interface InventoryReader {
        int countOf(String itemId);
    }

    /** Server-authoritative item entities near the block that was mined. */
    public interface DropReader {
        List<Drop> nearby(String itemId, int x, int y, int z, double radius);
    }

    /** Sends a MUTATION_PROPOSAL to the governance side. */
    public interface ProposalSink {
        void send(MutationGate.Proposal proposal);
    }

    public interface BreakToolSelector {
        BreakToolSelection select(String bot, String blockId);
    }

    public record BreakToolSelection(
        boolean success,
        boolean correctForDrops,
        String itemId,
        String reason
    ) {
    }

    public record Candidate(int x, int y, int z, String blockId) {
    }

    public record Drop(String entityId, String itemId, int count, double x, double y, double z) {
    }

    private enum Phase {
        STARTING,
        APPROACHING,
        EXCAVATION_AWAITING_VERDICT,
        EXCAVATION_BREAKING,
        EXCAVATION_MOVING,
        EXCAVATION_DESCENDING,
        AWAITING_VERDICT,
        MINING,
        PICKUP_LOCATE,
        PICKUP_APPROACH,
        PICKUP_WAIT
    }

    private final String bot;
    private final String actionId;
    private final Candidate target;
    private final List<String> expectedItemIds;
    private final JsonObject searchFacts;
    private final WorldView world;
    private final MovementControls movement;
    private final ExactBlockBreaker blockBreaker;
    private final BreakToolSelector breakToolSelector;
    private final BotControls hygiene;
    private final BlockReader blocks;
    private final InventoryReader inventory;
    private final DropReader drops;
    private final MutationGate gate;
    private final ProposalSink proposals;
    private final NavigateExecutor.PositionSource positions;
    private final NavigateExecutor.EventSink events;
    private final ActionRuntime runtime;
    private final int timeoutTicks;
    private final int maxExcavationMutations;

    private Phase phase = Phase.STARTING;
    private Candidate current;
    private ApproachController approach;
    private String proposalId;
    private int phaseStartedTick = -1;
    private int targetBrokenTick = -1;
    private int elapsedTicks;
    private int replansTotal;
    private Map<String, Integer> dropsBeforeBreak = Map.of();
    private Drop pickupDrop;
    private Drop plannedPickupDrop;
    private boolean pickupApproachFailed;
    private final Map<String, Integer> baselineCounts;
    private ExcavationPlanner.Plan excavationPlan;
    private Candidate excavationTarget;
    private ExcavationPlanner.Cell excavationBlock;
    private String excavationBlockId;
    private int excavationStepIndex;
    private int excavationBlockerIndex;
    private int excavationBreaks;
    private boolean targetBroken;
    private int excavationReplans;
    private int descentStableTicks;
    private boolean excavationAttempted;
    private boolean excavationUsed;
    private String excavationSourceReason;
    private String excavationFailure;
    private String excavationFailureDetail;
    private final Set<ExcavationPlanner.Cell> excavationDenied = new HashSet<>();
    private final JsonArray approachBrokenLedger = new JsonArray();

    public CollectExecutor(
        String bot,
        String actionId,
        Candidate target,
        List<String> expectedItemIds,
        JsonObject searchFacts,
        WorldView world,
        MovementControls movement,
        ExactBlockBreaker blockBreaker,
        BotControls hygiene,
        BlockReader blocks,
        InventoryReader inventory,
        DropReader drops,
        MutationGate gate,
        ProposalSink proposals,
        NavigateExecutor.PositionSource positions,
        NavigateExecutor.EventSink events,
        ActionRuntime runtime,
        int timeoutTicks,
        int maxExcavationMutations,
        BreakToolSelector breakToolSelector
    ) {
        this.bot = bot;
        this.actionId = actionId;
        this.target = target;
        this.current = target;
        this.expectedItemIds = List.copyOf(expectedItemIds);
        this.searchFacts = searchFacts;
        this.world = world;
        this.movement = movement;
        this.blockBreaker = blockBreaker;
        this.breakToolSelector = breakToolSelector;
        this.hygiene = hygiene;
        this.blocks = blocks;
        this.inventory = inventory;
        this.drops = drops;
        this.gate = gate;
        this.proposals = proposals;
        this.positions = positions;
        this.events = events;
        this.runtime = runtime;
        this.timeoutTicks = timeoutTicks;
        this.maxExcavationMutations = Math.max(0, maxExcavationMutations);
        this.baselineCounts = new HashMap<>();
        for (String itemId : expectedItemIds) {
            baselineCounts.putIfAbsent(itemId, inventory.countOf(itemId));
        }
    }

    @Override
    public void tick(int serverTick) {
        elapsedTicks++;
        if (runtime.cancelRequested(actionId)) {
            finishDetailed(serverTick, ActionRuntime.CLASS_CANCELED, "canceled", "cancel_requested");
            return;
        }
        // Before the requested mutation, the caller's deadline is absolute.
        // Once the designated block has been broken, inventory collection is
        // the transaction's authoritative postcondition. Do not strand that
        // postcondition merely because approach/mining consumed the deadline;
        // the pickup phase has its own fixed, non-replenishing bound.
        if (!targetBroken && elapsedTicks > timeoutTicks) {
            finishDetailed(serverTick, ActionRuntime.CLASS_TIMEOUT, "timeout", "timeout_ticks_exhausted");
            return;
        }
        if (targetBroken
            && targetBrokenTick >= 0
            && serverTick - targetBrokenTick > PICKUP_SETTLEMENT_TIMEOUT_TICKS) {
            finishDetailed(
                serverTick,
                ActionRuntime.CLASS_FAILED,
                "pickup_not_observed",
                "pickup_settlement_timeout"
            );
            return;
        }
        NavigateExecutor.PositionSource.Position position = positions.position(bot);
        if (position == null) {
            finish(serverTick, ActionRuntime.CLASS_FAILED, "body_missing");
            return;
        }
        switch (phase) {
            case STARTING -> startTarget(serverTick);
            case APPROACHING -> approachTick(serverTick, position);
            case EXCAVATION_AWAITING_VERDICT -> excavationVerdictTick(serverTick, position);
            case EXCAVATION_BREAKING -> excavationBreakingTick(serverTick, position);
            case EXCAVATION_MOVING -> excavationMovingTick(serverTick, position);
            case EXCAVATION_DESCENDING -> excavationDescendingTick(serverTick, position);
            case AWAITING_VERDICT -> verdictTick(serverTick);
            case MINING -> miningTick(serverTick);
            case PICKUP_LOCATE -> pickupLocateTick(serverTick);
            case PICKUP_APPROACH -> pickupApproachTick(serverTick, position);
            case PICKUP_WAIT -> pickupWaitTick(serverTick, position);
        }
    }

    private void startTarget(int serverTick) {
        current = target;
        approach = null;
        String observed = blocks.blockIdAt(target.x(), target.y(), target.z());
        if (observed == null) {
            finishDetailed(serverTick, ActionRuntime.CLASS_FAILED, "target_gone", "target_unloaded");
            return;
        }
        if (!target.blockId().equals(observed)) {
            finishDetailed(
                serverTick,
                ActionRuntime.CLASS_FAILED,
                "target_block_mismatch",
                "observed=" + observed
            );
            return;
        }
        JsonObject data = new JsonObject();
        data.addProperty("x", target.x());
        data.addProperty("y", target.y());
        data.addProperty("z", target.z());
        data.addProperty("block_id", target.blockId());
        events.emit(bot, serverTick, "target_committed", actionId, data);
        approach = new ApproachController(
            bot,
            actionId,
            interactGoal(target),
            world,
            movement,
            events,
            APPROACH_REPLAN_LIMIT
        );
        phase = Phase.APPROACHING;
    }

    private void approachTick(int serverTick, NavigateExecutor.PositionSource.Position position) {
        ApproachController.Outcome outcome = approach.tick(serverTick, position.x(), position.y(), position.z());
        switch (outcome.status()) {
            case WORKING -> {
            }
            case FAILED -> {
                replansTotal += approach.replans();
                approach.halt();
                approach = null;
                if (maxExcavationMutations <= 0
                    || !beginExcavation(serverTick, position, outcome.reason())) {
                    finishDetailed(serverTick, ActionRuntime.CLASS_FAILED, "no_path", outcome.reason());
                }
            }
            case COMPLETED -> {
                replansTotal += approach.replans();
                approach.halt();
                approach = null;
                String observed = blocks.blockIdAt(target.x(), target.y(), target.z());
                if (observed == null) {
                    finishDetailed(serverTick, ActionRuntime.CLASS_FAILED, "target_gone", "target_unloaded");
                    return;
                }
                if (!target.blockId().equals(observed)) {
                    finishDetailed(serverTick, ActionRuntime.CLASS_FAILED, "target_block_mismatch", "target_changed");
                    return;
                }
                current = target;
                beginTargetMutation(serverTick);
            }
        }
    }

    private boolean beginExcavation(
        int serverTick,
        NavigateExecutor.PositionSource.Position position,
        String sourceFailure
    ) {
        excavationAttempted = true;
        excavationSourceReason = sourceFailure;
        return planExcavation(serverTick, position, sourceFailure);
    }

    private boolean planExcavation(
        int serverTick,
        NavigateExecutor.PositionSource.Position position,
        String sourceFailure
    ) {
        List<ExcavationPlanner.Target> targets = target.blockId().equals(
            blocks.blockIdAt(target.x(), target.y(), target.z())
        )
            ? List.of(new ExcavationPlanner.Target(target.x(), target.y(), target.z()))
            : List.of();
        int remainingBreaks = maxExcavationMutations - excavationBreaks;
        ExcavationPlanner.Result result = ExcavationPlanner.plan(
            world,
            new ExcavationPlanner.Cell(position.blockX(), position.feetBlockY(), position.blockZ()),
            targets,
            excavationDenied,
            remainingBreaks
        );
        if (!result.success()) {
            excavationFailure = result.reason();
            excavationFailureDetail = sourceFailure;
            return false;
        }
        excavationPlan = result.plan();
        excavationTarget = target.x() == excavationPlan.target().x()
            && target.y() == excavationPlan.target().y()
            && target.z() == excavationPlan.target().z()
            ? target
            : null;
        if (excavationTarget == null) {
            excavationFailure = "target_mapping_failed";
            excavationFailureDetail = sourceFailure;
            return false;
        }
        excavationStepIndex = 0;
        excavationBlockerIndex = 0;
        excavationBlock = null;
        excavationBlockId = null;

        JsonObject data = new JsonObject();
        data.addProperty("target_x", excavationTarget.x());
        data.addProperty("target_y", excavationTarget.y());
        data.addProperty("target_z", excavationTarget.z());
        data.addProperty("steps", excavationPlan.steps().size());
        data.addProperty("planned_breaks", excavationPlan.breakCount());
        data.addProperty("break_budget_remaining", remainingBreaks);
        data.addProperty("expanded_nodes", excavationPlan.expandedNodes());
        events.emit(bot, serverTick, "collect_excavation_planned", actionId, data);
        advanceExcavation(serverTick, position);
        return true;
    }

    private void advanceExcavation(
        int serverTick,
        NavigateExecutor.PositionSource.Position position
    ) {
        if (excavationStepIndex >= excavationPlan.steps().size()) {
            completeExcavationApproach(serverTick, position);
            return;
        }
        ExcavationPlanner.Step step = excavationPlan.steps().get(excavationStepIndex);
        while (excavationBlockerIndex < step.blockers().size()) {
            ExcavationPlanner.Cell blocker = step.blockers().get(excavationBlockerIndex);
            WorldView.NodeKind kind = world.kindAt(blocker.x(), blocker.y(), blocker.z());
            if (kind == WorldView.NodeKind.PASSABLE || kind == WorldView.NodeKind.CLIMBABLE) {
                excavationBlockerIndex++;
                continue;
            }
            if (kind != WorldView.NodeKind.SOLID) {
                failOrReplanExcavation(
                    serverTick, position, "blocker_not_solid", kind.name().toLowerCase()
                );
                return;
            }
            if (world.isBlockClearanceHazardous(blocker.x(), blocker.y(), blocker.z())) {
                excavationDenied.add(blocker);
                failOrReplanExcavation(serverTick, position, "hazardous_block_clearance");
                return;
            }
            String blockId = blocks.blockIdAt(blocker.x(), blocker.y(), blocker.z());
            if (blockId == null) {
                failOrReplanExcavation(serverTick, position, "blocker_unloaded");
                return;
            }
            excavationBlock = blocker;
            excavationBlockId = blockId;
            MutationGate.Proposal proposal = gate.propose(
                bot,
                actionId,
                "break",
                blocker.x(),
                blocker.y(),
                blocker.z(),
                blockId,
                "collect_approach",
                serverTick
            );
            proposalId = proposal.proposalId();
            proposals.send(proposal);
            JsonObject data = new JsonObject();
            data.addProperty("proposal_id", proposalId);
            data.addProperty("block_id", blockId);
            data.addProperty("x", blocker.x());
            data.addProperty("y", blocker.y());
            data.addProperty("z", blocker.z());
            events.emit(bot, serverTick, "collect_excavation_mutation_proposed", actionId, data);
            phase = Phase.EXCAVATION_AWAITING_VERDICT;
            return;
        }

        excavationBlockerIndex = 0;
        ExcavationPlanner.Cell stand = step.stand();
        if (step.mode() == ExcavationPlanner.StepMode.DESCEND
            && position.blockX() == stand.x()
            && position.blockZ() == stand.z()
            && position.y() <= stand.y() + 1.05) {
            hygiene.clearAll(bot);
            descentStableTicks = 0;
            phaseStartedTick = serverTick;
            phase = Phase.EXCAVATION_DESCENDING;
            return;
        }
        if (position.blockX() == stand.x()
            && position.feetBlockY() == stand.y()
            && position.blockZ() == stand.z()
            && world.isStableDryPosition(position.x(), position.y(), position.z())) {
            excavationStepIndex++;
            advanceExcavation(serverTick, position);
            return;
        }
        phaseStartedTick = serverTick;
        approach = new ApproachController(
            bot,
            actionId,
            new Goal.Near(stand.x(), stand.y(), stand.z(), 0.0),
            world,
            movement,
            events,
            APPROACH_REPLAN_LIMIT,
            0.15
        );
        phase = Phase.EXCAVATION_MOVING;
    }

    private void excavationVerdictTick(
        int serverTick,
        NavigateExecutor.PositionSource.Position position
    ) {
        MutationGate.State state = gate.poll(proposalId, serverTick);
        if (state == MutationGate.State.PENDING) {
            return;
        }
        if (state != MutationGate.State.ALLOWED) {
            String reason = state == MutationGate.State.DENIED
                ? gate.reason(proposalId)
                : "governance_verdict_timeout";
            emitVerdict(serverTick, "collect_excavation_mutation_denied", reason);
            gate.discard(proposalId);
            proposalId = null;
            excavationDenied.add(excavationBlock);
            if (state == MutationGate.State.DENIED) {
                finishDetailed(serverTick, ActionRuntime.CLASS_UNSAFE, "governance_denied", reason);
                return;
            }
            failOrReplanExcavation(serverTick, position, reason);
            return;
        }

        emitVerdict(serverTick, "collect_excavation_mutation_allowed", gate.reason(proposalId));
        gate.discard(proposalId);
        proposalId = null;
        String observed = blocks.blockIdAt(
            excavationBlock.x(), excavationBlock.y(), excavationBlock.z()
        );
        if (observed == null) {
            failOrReplanExcavation(serverTick, position, "approved_block_unloaded");
            return;
        }
        if (!observed.equals(excavationBlockId)) {
            if (world.kindAt(excavationBlock.x(), excavationBlock.y(), excavationBlock.z())
                == WorldView.NodeKind.PASSABLE) {
                excavationBlockerIndex++;
                advanceExcavation(serverTick, position);
            } else {
                failOrReplanExcavation(serverTick, position, "approved_block_changed");
            }
            return;
        }
        if (world.isBlockClearanceHazardous(
            excavationBlock.x(), excavationBlock.y(), excavationBlock.z()
        )) {
            excavationDenied.add(excavationBlock);
            failOrReplanExcavation(serverTick, position, "hazardous_block_clearance");
            return;
        }
        movement.lookAt(
            bot,
            excavationBlock.x() + 0.5,
            excavationBlock.y() + 0.5,
            excavationBlock.z() + 0.5
        );
        BreakToolSelection selection = selectBreakTool(excavationBlockId, false);
        if (!selection.success()) {
            failOrReplanExcavation(serverTick, position, "tool_selection_failed");
            return;
        }
        ExactBlockBreaker.Outcome outcome = blockBreaker.begin(
            bot,
            excavationBlock.x(),
            excavationBlock.y(),
            excavationBlock.z(),
            excavationBlockId,
            serverTick
        );
        if (outcome.state() == ExactBlockBreaker.State.FAILED) {
            failOrReplanExcavation(
                serverTick, position, "exact_break_failed", outcome.reason()
            );
            return;
        }
        phaseStartedTick = serverTick;
        phase = Phase.EXCAVATION_BREAKING;
    }

    private void excavationBreakingTick(
        int serverTick,
        NavigateExecutor.PositionSource.Position position
    ) {
        String observed = blocks.blockIdAt(
            excavationBlock.x(), excavationBlock.y(), excavationBlock.z()
        );
        if (observed == null) {
            failOrReplanExcavation(serverTick, position, "blocker_unloaded_during_break");
            return;
        }
        if (!observed.equals(excavationBlockId)) {
            blockBreaker.abort(bot);
            hygiene.clearAll(bot);
            WorldView.NodeKind kind = world.kindAt(
                excavationBlock.x(), excavationBlock.y(), excavationBlock.z()
            );
            if (kind != WorldView.NodeKind.PASSABLE && kind != WorldView.NodeKind.CLIMBABLE) {
                failOrReplanExcavation(serverTick, position, "blocker_changed_to_solid");
                return;
            }
            excavationBreaks++;
            excavationUsed = true;
            JsonObject broken = new JsonObject();
            broken.addProperty("x", excavationBlock.x());
            broken.addProperty("y", excavationBlock.y());
            broken.addProperty("z", excavationBlock.z());
            broken.addProperty("block_id", excavationBlockId);
            approachBrokenLedger.add(broken);
            events.emit(bot, serverTick, "collect_excavation_block_verified", actionId, broken.deepCopy());
            excavationBlockerIndex++;
            advanceExcavation(serverTick, position);
            return;
        }
        if (world.isBlockClearanceHazardous(
            excavationBlock.x(), excavationBlock.y(), excavationBlock.z()
        )) {
            blockBreaker.abort(bot);
            hygiene.clearAll(bot);
            excavationDenied.add(excavationBlock);
            failOrReplanExcavation(serverTick, position, "hazardous_block_clearance");
            return;
        }
        ExactBlockBreaker.Outcome outcome = blockBreaker.tick(bot, serverTick);
        if (outcome.state() == ExactBlockBreaker.State.FAILED) {
            failOrReplanExcavation(
                serverTick, position, "exact_break_failed", outcome.reason()
            );
            return;
        }
        if (serverTick - phaseStartedTick > BREAK_TIMEOUT_TICKS) {
            failOrReplanExcavation(serverTick, position, "break_timeout");
        }
    }

    private void excavationMovingTick(
        int serverTick,
        NavigateExecutor.PositionSource.Position position
    ) {
        ApproachController.Outcome outcome = approach.tick(
            serverTick, position.x(), position.y(), position.z()
        );
        if (outcome.status() == ApproachController.Status.WORKING) {
            return;
        }
        replansTotal += approach.replans();
        approach.halt();
        approach = null;
        if (outcome.status() == ApproachController.Status.FAILED) {
            failOrReplanExcavation(
                serverTick, position, "movement_failed", outcome.reason()
            );
            return;
        }
        excavationUsed = true;
        excavationStepIndex++;
        advanceExcavation(serverTick, position);
    }

    private void excavationDescendingTick(
        int serverTick,
        NavigateExecutor.PositionSource.Position position
    ) {
        ExcavationPlanner.Cell stand = excavationPlan.steps().get(excavationStepIndex).stand();
        boolean settled = position.blockX() == stand.x()
            && position.blockZ() == stand.z()
            && Math.abs(position.y() - stand.y()) <= 0.08;
        descentStableTicks = settled ? descentStableTicks + 1 : 0;
        if (descentStableTicks >= DESCENT_STABLE_TICKS) {
            excavationUsed = true;
            excavationStepIndex++;
            advanceExcavation(serverTick, position);
            return;
        }
        if (position.y() < stand.y() - 0.1) {
            failOrReplanExcavation(serverTick, position, "descent_overshot");
            return;
        }
        if (serverTick - phaseStartedTick > EXCAVATION_MOVE_TIMEOUT_TICKS) {
            failOrReplanExcavation(serverTick, position, "descent_timeout");
        }
    }

    private void completeExcavationApproach(
        int serverTick,
        NavigateExecutor.PositionSource.Position position
    ) {
        String observed = blocks.blockIdAt(
            excavationTarget.x(), excavationTarget.y(), excavationTarget.z()
        );
        if (!excavationTarget.blockId().equals(observed)) {
            finishDetailed(
                serverTick,
                ActionRuntime.CLASS_FAILED,
                observed == null ? "target_gone" : "target_block_mismatch",
                "target_changed_after_excavation"
            );
            return;
        }
        if (!interactGoal(excavationTarget).isSatisfied(
            world, position.blockX(), position.feetBlockY(), position.blockZ()
        )) {
            failOrReplanExcavation(serverTick, position, "terminal_interaction_unsatisfied");
            return;
        }
        current = excavationTarget;
        beginTargetMutation(serverTick);
    }

    private void failOrReplanExcavation(
        int serverTick,
        NavigateExecutor.PositionSource.Position position,
        String reason
    ) {
        failOrReplanExcavation(serverTick, position, reason, null);
    }

    private void failOrReplanExcavation(
        int serverTick,
        NavigateExecutor.PositionSource.Position position,
        String reason,
        String detail
    ) {
        excavationFailure = reason;
        excavationFailureDetail = detail;
        blockBreaker.abort(bot);
        hygiene.clearAll(bot);
        if (proposalId != null) {
            gate.discard(proposalId);
            proposalId = null;
        }
        if (approach != null) {
            approach.halt();
            approach = null;
        }
        if (excavationReplans >= MAX_EXCAVATION_REPLANS) {
            String terminalReason = excavationBreaks >= maxExcavationMutations
                ? "excavation_budget_exhausted"
                : "no_path";
            finishDetailed(serverTick, ActionRuntime.CLASS_FAILED, terminalReason, reason);
            return;
        }
        excavationReplans++;
        if (!planExcavation(serverTick, position, reason)) {
            String terminalReason = excavationBreaks >= maxExcavationMutations
                ? "excavation_budget_exhausted"
                : "no_path";
            finishDetailed(serverTick, ActionRuntime.CLASS_FAILED, terminalReason, excavationFailure);
        }
    }

    private void beginTargetMutation(int serverTick) {
        MutationGate.Proposal proposal = gate.propose(
            bot, actionId, "break", current.x(), current.y(), current.z(), current.blockId(),
            "collect", serverTick
        );
        proposalId = proposal.proposalId();
        proposals.send(proposal);
        JsonObject data = new JsonObject();
        data.addProperty("proposal_id", proposalId);
        data.addProperty("block_id", current.blockId());
        events.emit(bot, serverTick, "mutation_proposed", actionId, data);
        phase = Phase.AWAITING_VERDICT;
    }

    private void verdictTick(int serverTick) {
        MutationGate.State state = gate.poll(proposalId, serverTick);
        switch (state) {
            case PENDING -> {
            }
            case ALLOWED -> {
                emitVerdict(serverTick, "mutation_allowed", gate.reason(proposalId));
                gate.discard(proposalId);
                if (world.isBlockClearanceHazardous(
                    current.x(), current.y(), current.z()
                )) {
                    finishDetailed(
                        serverTick,
                        ActionRuntime.CLASS_FAILED,
                        "no_path",
                        "hazardous_block_clearance"
                    );
                    return;
                }
                dropsBeforeBreak = nearbyDropCounts();
                movement.lookAt(bot, current.x() + 0.5, current.y() + 0.5, current.z() + 0.5);
                BreakToolSelection selection = selectBreakTool(current.blockId(), true);
                if (!selection.success() || !selection.correctForDrops()) {
                    finishDetailed(
                        serverTick,
                        ActionRuntime.CLASS_FAILED,
                        "no_path",
                        "missing_required_tool"
                    );
                    return;
                }
                ExactBlockBreaker.Outcome outcome = blockBreaker.begin(
                    bot, current.x(), current.y(), current.z(), current.blockId(), serverTick
                );
                if (outcome.state() == ExactBlockBreaker.State.FAILED) {
                    finishDetailed(serverTick, ActionRuntime.CLASS_FAILED, "no_path", outcome.reason());
                    return;
                }
                phaseStartedTick = serverTick;
                phase = Phase.MINING;
            }
            case DENIED, TIMED_OUT -> {
                String reason = state == MutationGate.State.DENIED
                    ? gate.reason(proposalId)
                    : "governance_verdict_timeout";
                emitVerdict(serverTick, "mutation_denied", reason);
                gate.discard(proposalId);
                finishDetailed(
                    serverTick,
                    state == MutationGate.State.DENIED
                        ? ActionRuntime.CLASS_UNSAFE
                        : ActionRuntime.CLASS_FAILED,
                    state == MutationGate.State.DENIED ? "governance_denied" : "no_path",
                    reason
                );
            }
        }
    }

    private void miningTick(int serverTick) {
        String observed = blocks.blockIdAt(current.x(), current.y(), current.z());
        if (observed == null) {
            hygiene.clearAll(bot);
            finishDetailed(serverTick, ActionRuntime.CLASS_FAILED, "target_gone", "target_unloaded_during_mine");
            return;
        }
        if (!observed.equals(current.blockId())) {
            // The world actually changed: the mine is verified.
            targetBroken = true;
            targetBrokenTick = serverTick;
            blockBreaker.abort(bot);
            hygiene.clearAll(bot);
            JsonObject data = new JsonObject();
            data.addProperty("block_id", current.blockId());
            data.addProperty("now", observed);
            data.addProperty("break_ticks", serverTick - phaseStartedTick);
            events.emit(bot, serverTick, "mutation_verified", actionId, data);
            approach = null;
            pickupDrop = null;
            plannedPickupDrop = null;
            pickupApproachFailed = false;
            phaseStartedTick = serverTick;
            phase = Phase.PICKUP_LOCATE;
            return;
        }
        if (world.isBlockClearanceHazardous(current.x(), current.y(), current.z())) {
            blockBreaker.abort(bot);
            hygiene.clearAll(bot);
            finishDetailed(
                serverTick,
                ActionRuntime.CLASS_FAILED,
                "no_path",
                "hazardous_block_clearance"
            );
            return;
        }
        ExactBlockBreaker.Outcome outcome = blockBreaker.tick(bot, serverTick);
        if (outcome.state() == ExactBlockBreaker.State.FAILED) {
            blockBreaker.abort(bot);
            finishDetailed(serverTick, ActionRuntime.CLASS_FAILED, "no_path", outcome.reason());
            return;
        }
        if (serverTick - phaseStartedTick > BREAK_TIMEOUT_TICKS) {
            blockBreaker.abort(bot);
            hygiene.clearAll(bot);
            finishDetailed(serverTick, ActionRuntime.CLASS_TIMEOUT, "timeout", "break_timeout");
        }
    }

    private void pickupLocateTick(int serverTick) {
        if (completeFromInventoryDelta(serverTick)) {
            return;
        }
        Drop observed = attributableDrops().stream()
            .min(Comparator.comparingDouble(this::distanceFromMinedBlockSquared))
            .orElse(null);
        if (observed == null) {
            if (serverTick - phaseStartedTick > DROP_LOCATE_TIMEOUT_TICKS) {
                finishDetailed(serverTick, ActionRuntime.CLASS_FAILED, "pickup_not_observed", "drop_not_located");
            }
            return;
        }
        pickupDrop = observed;
        JsonObject data = new JsonObject();
        data.addProperty("entity_id", pickupDrop.entityId());
        data.addProperty("item_id", pickupDrop.itemId());
        data.addProperty("x", pickupDrop.x());
        data.addProperty("y", pickupDrop.y());
        data.addProperty("z", pickupDrop.z());
        events.emit(bot, serverTick, "pickup_target_acquired", actionId, data);
        phase = Phase.PICKUP_APPROACH;
    }

    private void pickupApproachTick(int serverTick, NavigateExecutor.PositionSource.Position position) {
        if (completeFromInventoryDelta(serverTick)) {
            return;
        }
        Drop observed = currentPickupDrop();
        if (observed != null) {
            boolean moved = plannedPickupDrop != null
                && distanceSquared(plannedPickupDrop, observed)
                >= DROP_REPLAN_DISTANCE * DROP_REPLAN_DISTANCE;
            pickupDrop = observed;
            if (moved && approach != null) {
                approach.halt();
                approach = null;
                plannedPickupDrop = null;
                pickupApproachFailed = false;
            }
        }
        if (approach == null) {
            approach = new ApproachController(
                bot,
                actionId,
                pickupGoal(pickupDrop),
                world,
                movement,
                events,
                APPROACH_REPLAN_LIMIT,
                PICKUP_FINAL_REACH_DISTANCE
            );
            plannedPickupDrop = pickupDrop;
        }
        ApproachController.Outcome outcome = approach.tick(serverTick, position.x(), position.y(), position.z());
        if (outcome.status() == ApproachController.Status.WORKING) {
            return;
        }
        // Whether or not the walk-in fully succeeded, give the drop time to
        // arrive and let the inventory delta be the only truth.
        approach.halt();
        approach = null;
        pickupApproachFailed = outcome.status() == ApproachController.Status.FAILED;
        phaseStartedTick = serverTick;
        phase = Phase.PICKUP_WAIT;
    }

    private void pickupWaitTick(
        int serverTick,
        NavigateExecutor.PositionSource.Position position
    ) {
        if (completeFromInventoryDelta(serverTick)) {
            return;
        }
        Drop observed = currentPickupDrop();
        if (observed != null) {
            boolean moved = plannedPickupDrop != null
                && distanceSquared(plannedPickupDrop, observed)
                >= DROP_REPLAN_DISTANCE * DROP_REPLAN_DISTANCE;
            pickupDrop = observed;
            boolean outsidePickupVolume = !pickupGoal(pickupDrop).isWithinPhysicalReach(
                position.x(), position.y(), position.z()
            );
            if (outsidePickupVolume && (!pickupApproachFailed || moved)) {
                plannedPickupDrop = null;
                pickupApproachFailed = false;
                phase = Phase.PICKUP_APPROACH;
                return;
            }
        }
        if (serverTick - phaseStartedTick > PICKUP_TIMEOUT_TICKS) {
            finishDetailed(serverTick, ActionRuntime.CLASS_FAILED, "pickup_not_observed", "pickup_timeout");
        }
    }

    private boolean completeFromInventoryDelta(int serverTick) {
        JsonObject delta = inventoryDelta();
        if (delta == null) {
            return false;
        }
        JsonObject facts = new JsonObject();
        facts.add("inventory_delta", delta);
        facts.add("mined_block", blockFacts());
        if (pickupDrop != null) {
            JsonObject drop = new JsonObject();
            drop.addProperty("entity_id", pickupDrop.entityId());
            drop.addProperty("x", pickupDrop.x());
            drop.addProperty("y", pickupDrop.y());
            drop.addProperty("z", pickupDrop.z());
            facts.add("pickup_drop", drop);
        }
        finishWithFacts(serverTick, ActionRuntime.CLASS_COMPLETED, "collected", facts);
        return true;
    }

    private Map<String, Integer> nearbyDropCounts() {
        Map<String, Integer> counts = new HashMap<>();
        for (Drop drop : nearbyDrops()) {
            counts.put(drop.entityId(), drop.count());
        }
        return Map.copyOf(counts);
    }

    private List<Drop> attributableDrops() {
        return nearbyDrops().stream()
            .filter(drop -> drop.count() > dropsBeforeBreak.getOrDefault(drop.entityId(), 0))
            .toList();
    }

    private List<Drop> nearbyDrops() {
        List<Drop> found = new ArrayList<>();
        for (String itemId : expectedItemIds) {
            found.addAll(drops.nearby(
                itemId,
                current.x(),
                current.y(),
                current.z(),
                DROP_SEARCH_RADIUS
            ));
        }
        return List.copyOf(found);
    }

    private double distanceFromMinedBlockSquared(Drop drop) {
        double dx = drop.x() - (current.x() + 0.5);
        double dy = drop.y() - (current.y() + 0.5);
        double dz = drop.z() - (current.z() + 0.5);
        return dx * dx + dy * dy + dz * dz;
    }

    private static double distanceSquared(Drop left, Drop right) {
        double dx = left.x() - right.x();
        double dy = left.y() - right.y();
        double dz = left.z() - right.z();
        return dx * dx + dy * dy + dz * dz;
    }

    private Drop currentPickupDrop() {
        if (pickupDrop == null) {
            return null;
        }
        return attributableDrops().stream()
            .filter(drop -> pickupDrop.entityId().equals(drop.entityId()))
            .findFirst()
            .orElse(null);
    }

    private static Goal.Pickup pickupGoal(Drop drop) {
        return new Goal.Pickup(drop.x(), drop.y(), drop.z());
    }

    private JsonObject inventoryDelta() {
        for (Map.Entry<String, Integer> entry : baselineCounts.entrySet()) {
            int now = inventory.countOf(entry.getKey());
            if (now > entry.getValue()) {
                JsonObject delta = new JsonObject();
                delta.addProperty("item_id", entry.getKey());
                delta.addProperty("before", entry.getValue());
                delta.addProperty("after", now);
                return delta;
            }
        }
        return null;
    }

    private void emitVerdict(int serverTick, String eventName, String reason) {
        JsonObject data = new JsonObject();
        data.addProperty("proposal_id", proposalId);
        if (reason != null) {
            data.addProperty("reason", reason);
        }
        events.emit(bot, serverTick, eventName, actionId, data);
    }

    private static Goal.Interact interactGoal(Candidate candidate) {
        return new Goal.Interact(
            candidate.x(), candidate.y(), candidate.z(), Goal.Interact.MINE_RANGE
        );
    }

    private JsonObject blockFacts() {
        JsonObject block = new JsonObject();
        block.addProperty("x", current.x());
        block.addProperty("y", current.y());
        block.addProperty("z", current.z());
        block.addProperty("block_id", current.blockId());
        return block;
    }

    private void finish(int serverTick, String classification, String reason) {
        finishWithFacts(serverTick, classification, reason, new JsonObject());
    }

    private void finishDetailed(
        int serverTick,
        String classification,
        String reason,
        String phaseReason
    ) {
        JsonObject facts = new JsonObject();
        facts.addProperty("phase_reason", phaseReason);
        finishWithFacts(serverTick, classification, reason, facts);
    }

    private BreakToolSelection selectBreakTool(String blockId, boolean requireCorrectForDrops) {
        if (breakToolSelector == null) {
            return new BreakToolSelection(true, !requireCorrectForDrops, null, "selector_unavailable");
        }
        BreakToolSelection selection = breakToolSelector.select(bot, blockId);
        return selection == null
            ? new BreakToolSelection(false, false, null, "selector_returned_null")
            : selection;
    }

    private void finishWithFacts(int serverTick, String classification, String reason, JsonObject facts) {
        if (!TERMINAL_REASONS.contains(reason)) {
            throw new IllegalStateException("unclassified collect terminal reason: " + reason);
        }
        blockBreaker.abort(bot);
        if (proposalId != null) {
            gate.discard(proposalId);
            proposalId = null;
        }
        if (approach != null) {
            approach.halt();
        }
        facts.addProperty("reason", reason);
        facts.addProperty("elapsed_ticks", elapsedTicks);
        facts.addProperty("terminal_phase", phase.name().toLowerCase(java.util.Locale.ROOT));
        if (approach != null) {
            facts.addProperty("active_approach_expanded_nodes", approach.expandedNodes());
            facts.addProperty("active_approach_unloaded_touches", approach.unloadedTouches());
        }
        facts.addProperty("attempts_consumed", 1 + excavationReplans);
        facts.addProperty("budget_remaining", Math.max(0, maxExcavationMutations - excavationBreaks));
        facts.addProperty("mutations_completed", excavationBreaks + (targetBroken ? 1 : 0));
        facts.addProperty("replans", replansTotal);
        facts.addProperty("approach_excavation_attempted", excavationAttempted);
        facts.addProperty("approach_excavation_used", excavationUsed);
        facts.addProperty("approach_excavation_breaks", excavationBreaks);
        facts.addProperty("approach_excavation_replans", excavationReplans);
        if (excavationFailure != null) {
            facts.addProperty("approach_excavation_failure", excavationFailure);
        }
        if (excavationSourceReason != null) {
            facts.addProperty("approach_excavation_source_reason", excavationSourceReason);
        }
        if (excavationFailureDetail != null) {
            facts.addProperty("approach_excavation_failure_detail", excavationFailureDetail);
        }
        facts.add("approach_broken", approachBrokenLedger.deepCopy());
        facts.add("search", searchFacts.deepCopy());
        runtime.finish(bot, actionId, classification, facts, serverTick);
    }
}
