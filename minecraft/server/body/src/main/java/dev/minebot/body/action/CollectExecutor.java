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
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * Runs one COLLECT_BLOCK action: submit the complete live candidate domain to
 * navigation, lock the candidate selected by the reached stand, propose that
 * exact mutation to Python and wait fail-closed for the verdict, mine only
 * after an explicit allow, verify the block actually changed, then collect the
 * drop and prove it with an authoritative inventory delta. Candidate failures
 * are typed and bounded; success is never inferred from a dispatched command.
 */
public final class CollectExecutor implements ActionRuntime.TickExecutor {
    public static final int MAX_CANDIDATE_ATTEMPTS = 128;
    public static final int MAX_PLANNING_CANDIDATES = Goal.MAX_COMPOSITE_MEMBERS;
    public static final int BREAK_TIMEOUT_TICKS = 300;
    public static final int PICKUP_TIMEOUT_TICKS = 20;
    public static final int DROP_LOCATE_TIMEOUT_TICKS = 60;
    public static final double DROP_REPLAN_DISTANCE = 0.5;
    public static final double DROP_SEARCH_RADIUS = 8.0;
    public static final double PICKUP_FINAL_REACH_DISTANCE = 0.15;
    public static final int APPROACH_REPLAN_LIMIT = 3;
    public static final int MAX_APPROACH_BREAKS = 8;
    public static final int MAX_EXCAVATION_REPLANS = 3;
    public static final int EXCAVATION_MOVE_TIMEOUT_TICKS = 100;
    public static final int DROP_URGENT_AFTER_TICKS = 200;
    public static final int DROP_LOST_AFTER_TICKS = 80;
    public static final int MAX_ACTIVE_DROP_GOALS = 8;
    public static final int MAX_DROP_APPROACH_ATTEMPTS = 8;
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

    /** Re-reads the authoritative loaded-world candidate domain during one objective. */
    public interface CandidateSource {
        CandidateScan scan(int x, int y, int z, int serverTick);
    }

    public record Candidate(int x, int y, int z, String blockId) {
    }

    public record CandidateScan(
        List<Candidate> candidates,
        boolean coverageComplete,
        int unloadedChunkCount,
        boolean resultCapped
    ) {
        public CandidateScan {
            candidates = List.copyOf(candidates);
        }
    }

    public record Drop(String entityId, String itemId, int count, double x, double y, double z) {
    }

    private record TrackedDrop(Drop drop, int firstSeenTick, int lastSeenTick) {
        TrackedDrop observed(Drop current, int serverTick) {
            return new TrackedDrop(current, firstSeenTick, serverTick);
        }
    }

    private enum Phase {
        NEXT_CANDIDATE,
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
    private final List<Candidate> candidates = new ArrayList<>();
    private final List<Candidate> remainingCandidates = new ArrayList<>();
    private final Set<String> seenCandidateKeys = new HashSet<>();
    private final Set<String> retiredCandidateKeys = new HashSet<>();
    private final CandidateSource candidateSource;
    private final List<String> expectedItemIds;
    private final JsonObject searchFacts;
    private final WorldView world;
    private final MovementControls movement;
    private final ExactBlockBreaker blockBreaker;
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
    private final int targetCount;
    private final int maxCandidateAttempts;
    private final int maxTargetMutations;

    private Phase phase = Phase.NEXT_CANDIDATE;
    private int candidateAttempts;
    private int candidatesRetired;
    private Candidate current;
    private List<Candidate> activeDomain = List.of();
    private ApproachController approach;
    private String proposalId;
    private int phaseStartedTick = -1;
    private int elapsedTicks;
    private int replansTotal;
    private int candidateScans;
    private int candidatesDiscoveredAfterStart;
    private int lastScanMatches;
    private int lastScanUnloadedChunks;
    private boolean lastScanCoverageComplete = true;
    private boolean anyScanResultCapped;
    private Drop pickupDrop;
    private Drop plannedPickupDrop;
    private final JsonArray attemptFailures = new JsonArray();
    private final Map<String, Integer> baselineCounts;
    private final List<Candidate> minedTargets = new ArrayList<>();
    private final Map<String, TrackedDrop> pendingDrops = new LinkedHashMap<>();
    private final Set<String> retiredDropIds = new HashSet<>();
    private final Set<String> reportedLostDropIds = new HashSet<>();
    private final Map<String, Integer> dropApproachAttempts = new HashMap<>();
    private List<Drop> activePickupDomain = List.of();
    private final JsonArray minedLedger = new JsonArray();
    private final JsonArray pickupLedger = new JsonArray();
    private final JsonArray dropLossLedger = new JsonArray();
    private int targetBreaks;
    private int mutationsCompleted;
    private int collectedAtCycleStart;
    private int recordedCollectedTotal;
    private int pickupCredit;
    private String lastFailureReason;
    private ExcavationPlanner.Plan excavationPlan;
    private Candidate excavationTarget;
    private ExcavationPlanner.Cell excavationBlock;
    private String excavationBlockId;
    private int excavationStepIndex;
    private int excavationBlockerIndex;
    private int excavationBreaks;
    private int excavationReplans;
    private int descentStableTicks;
    private boolean excavationAttempted;
    private boolean excavationUsed;
    private String excavationFailure;
    private final Set<ExcavationPlanner.Cell> excavationDenied = new HashSet<>();
    private final JsonArray approachBrokenLedger = new JsonArray();

    public CollectExecutor(
        String bot,
        String actionId,
        List<Candidate> candidates,
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
        int timeoutTicks
    ) {
        this(
            bot,
            actionId,
            candidates,
            expectedItemIds,
            searchFacts,
            world,
            movement,
            blockBreaker,
            hygiene,
            blocks,
            inventory,
            drops,
            gate,
            proposals,
            positions,
            events,
            runtime,
            timeoutTicks,
            1,
            8,
            8,
            null
        );
    }

    public CollectExecutor(
        String bot,
        String actionId,
        List<Candidate> candidates,
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
        int targetCount,
        int maxCandidateAttempts,
        int maxTargetMutations
    ) {
        this(
            bot,
            actionId,
            candidates,
            expectedItemIds,
            searchFacts,
            world,
            movement,
            blockBreaker,
            hygiene,
            blocks,
            inventory,
            drops,
            gate,
            proposals,
            positions,
            events,
            runtime,
            timeoutTicks,
            targetCount,
            maxCandidateAttempts,
            maxTargetMutations,
            null
        );
    }

    public CollectExecutor(
        String bot,
        String actionId,
        List<Candidate> candidates,
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
        int targetCount,
        int maxCandidateAttempts,
        int maxTargetMutations,
        CandidateSource candidateSource
    ) {
        this.bot = bot;
        this.actionId = actionId;
        for (Candidate candidate : candidates) {
            addDiscoveredCandidate(candidate);
        }
        this.candidateSource = candidateSource;
        this.expectedItemIds = List.copyOf(expectedItemIds);
        this.searchFacts = searchFacts;
        this.candidateScans = searchFacts.has("matches") ? 1 : 0;
        this.lastScanMatches = searchFacts.has("matches")
            ? searchFacts.get("matches").getAsInt()
            : candidates.size();
        this.lastScanCoverageComplete = !searchFacts.has("coverage_complete")
            || searchFacts.get("coverage_complete").getAsBoolean();
        this.lastScanUnloadedChunks = searchFacts.has("unloaded_chunk_count")
            ? searchFacts.get("unloaded_chunk_count").getAsInt()
            : 0;
        this.anyScanResultCapped = searchFacts.has("result_capped")
            && searchFacts.get("result_capped").getAsBoolean();
        this.world = world;
        this.movement = movement;
        this.blockBreaker = blockBreaker;
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
        this.targetCount = targetCount;
        this.maxCandidateAttempts = Math.min(MAX_CANDIDATE_ATTEMPTS, maxCandidateAttempts);
        this.maxTargetMutations = maxTargetMutations;
        updateSearchFacts();
        this.baselineCounts = new HashMap<>();
        for (String itemId : expectedItemIds) {
            baselineCounts.putIfAbsent(itemId, inventory.countOf(itemId));
        }
    }

    @Override
    public void tick(int serverTick) {
        elapsedTicks++;
        if (runtime.cancelRequested(actionId)) {
            finish(serverTick, ActionRuntime.CLASS_CANCELED, "cancel_requested");
            return;
        }
        if (elapsedTicks > timeoutTicks) {
            finishProgressAware(serverTick, "timeout_ticks_exhausted");
            return;
        }
        NavigateExecutor.PositionSource.Position position = positions.position(bot);
        if (position == null) {
            finish(serverTick, ActionRuntime.CLASS_FAILED, "body_missing");
            return;
        }
        if (phase != Phase.MINING
            && phase != Phase.EXCAVATION_BREAKING
            && completeFromInventoryTarget(serverTick)) {
            return;
        }
        switch (phase) {
            case NEXT_CANDIDATE -> nextObjective(serverTick, position);
            case APPROACHING -> approachTick(serverTick, position);
            case EXCAVATION_AWAITING_VERDICT -> excavationVerdictTick(serverTick, position);
            case EXCAVATION_BREAKING -> excavationBreakingTick(serverTick, position);
            case EXCAVATION_MOVING -> excavationMovingTick(serverTick, position);
            case EXCAVATION_DESCENDING -> excavationDescendingTick(serverTick, position);
            case AWAITING_VERDICT -> verdictTick(serverTick);
            case MINING -> miningTick(serverTick);
            case PICKUP_LOCATE -> pickupLocateTick(serverTick, position);
            case PICKUP_APPROACH -> pickupApproachTick(serverTick, position);
            case PICKUP_WAIT -> pickupWaitTick(serverTick, position);
        }
    }

    private void nextObjective(
        int serverTick,
        NavigateExecutor.PositionSource.Position position
    ) {
        current = null;
        approach = null;
        pickupDrop = null;
        plannedPickupDrop = null;
        if (completeFromInventoryTarget(serverTick)) {
            return;
        }

        refreshLiveCandidates();
        if (remainingCandidates.isEmpty()
            && candidateAttempts < maxCandidateAttempts
            && targetBreaks < maxTargetMutations) {
            rescanCandidateDomain(serverTick, position);
            refreshLiveCandidates();
        }
        refreshPendingDrops(position, serverTick);
        for (TrackedDrop tracked : List.copyOf(pendingDrops.values())) {
            if (hasSafePickupStand(tracked.drop())) {
                continue;
            }
            Drop unsafe = tracked.drop();
            pendingDrops.remove(unsafe.entityId());
            retiredDropIds.add(unsafe.entityId());
            recordDropLoss(serverTick, unsafe, "unsafe_pickup_domain");
        }

        boolean candidateBudgetAvailable = candidateAttempts < maxCandidateAttempts
            && targetBreaks < maxTargetMutations
            && !remainingCandidates.isEmpty();
        List<Drop> availableDrops = pendingDrops.values().stream()
            .map(TrackedDrop::drop)
            .sorted(Comparator
                .comparingInt((Drop drop) -> pendingDrops.get(drop.entityId()).firstSeenTick())
                .thenComparingDouble(drop -> distanceFromPositionSquared(position, drop)))
            .limit(MAX_ACTIVE_DROP_GOALS)
            .toList();
        if (!candidateBudgetAvailable && availableDrops.isEmpty()) {
            String reason = candidates.isEmpty() ? "target_not_found" : "candidate_targets_exhausted";
            finishProgressAware(serverTick, reason);
            return;
        }

        int remainingNeeded = Math.max(1, targetCount - collectedTotal());
        int pendingCount = availableDrops.stream().mapToInt(Drop::count).sum();
        int oldestDropAge = availableDrops.stream()
            .mapToInt(drop -> serverTick - pendingDrops.get(drop.entityId()).firstSeenTick())
            .max()
            .orElse(0);
        boolean pickupUrgent = !availableDrops.isEmpty()
            && (!candidateBudgetAvailable
                || pendingCount >= remainingNeeded
                || oldestDropAge >= DROP_URGENT_AFTER_TICKS);

        activePickupDomain = pickupUrgent
            ? availableDrops
            : availableDrops.stream()
                .filter(drop -> dropApproachAttempts.getOrDefault(drop.entityId(), 0) == 0)
                .toList();
        if (pickupUrgent) {
            pickupDrop = activePickupDomain.stream()
                .min(Comparator.comparingDouble(drop -> distanceFromPositionSquared(position, drop)))
                .orElseThrow();
            plannedPickupDrop = null;
            collectedAtCycleStart = collectedTotal();
            phase = Phase.PICKUP_APPROACH;
            return;
        }
        int candidateSlots = pickupUrgent
            ? 0
            : MAX_PLANNING_CANDIDATES - activePickupDomain.size();
        int domainSize = candidateBudgetAvailable
            ? Math.min(remainingCandidates.size(), Math.max(0, candidateSlots))
            : 0;
        activeDomain = domainSize == 0
            ? List.of()
            : List.copyOf(remainingCandidates.subList(0, domainSize));
        List<Goal> goals = new ArrayList<>();
        for (Drop drop : activePickupDomain) {
            goals.add(pickupGoal(drop));
        }
        for (Candidate candidate : activeDomain) {
            goals.add(interactGoal(candidate));
        }
        if (goals.isEmpty()) {
            finishProgressAware(serverTick, "resource_objective_domain_empty");
            return;
        }
        Goal domainGoal = goals.size() == 1 ? goals.get(0) : new Goal.Composite(goals);

        JsonObject data = new JsonObject();
        data.addProperty("candidate_count", activeDomain.size());
        data.addProperty("pickup_count", activePickupDomain.size());
        data.addProperty("pickup_urgent", pickupUrgent);
        data.addProperty("remaining_count", remainingNeeded);
        data.addProperty("candidates_retired", candidatesRetired);
        events.emit(bot, serverTick, "collection_objective_domain_planned", actionId, data);
        collectedAtCycleStart = collectedTotal();
        approach = new ApproachController(
            bot,
            actionId,
            domainGoal,
            world,
            movement,
            events,
            APPROACH_REPLAN_LIMIT,
            true
        );
        phase = Phase.APPROACHING;
    }

    private void approachTick(int serverTick, NavigateExecutor.PositionSource.Position position) {
        if (completeFromInventoryTarget(serverTick)) {
            return;
        }
        if (collectedTotal() > collectedAtCycleStart) {
            approach.halt();
            approach = null;
            recordPickupProgress(serverTick, "inventory_changed_during_route", null);
            phase = Phase.NEXT_CANDIDATE;
            return;
        }
        ApproachController.Outcome outcome = approach.tick(serverTick, position.x(), position.y(), position.z());
        switch (outcome.status()) {
            case WORKING -> {
            }
            case FAILED -> {
                replansTotal += approach.replans();
                approach.halt();
                approach = null;
                refreshPendingDrops(position, serverTick);
                if (activeDropMovedSincePlanning()) {
                    phase = Phase.NEXT_CANDIDATE;
                    return;
                }
                if (activeDomain.isEmpty()) {
                    retryOrRetireActiveDrops(serverTick, position, outcome.reason());
                    phase = Phase.NEXT_CANDIDATE;
                    return;
                }
                if (!beginExcavation(serverTick, position, outcome.reason())) {
                    recordDomainFailure("approach_failed:" + outcome.reason());
                    retireActiveCandidateDomain("candidate_domain_unreachable:" + outcome.reason());
                    phase = Phase.NEXT_CANDIDATE;
                }
            }
            case COMPLETED -> {
                replansTotal += approach.replans();
                approach.halt();
                approach = null;
                refreshPendingDrops(position, serverTick);
                if (completeFromInventoryTarget(serverTick)) {
                    return;
                }
                Drop reachedDrop = selectReachedDrop(position);
                if (reachedDrop != null) {
                    pickupDrop = reachedDrop;
                    plannedPickupDrop = reachedDrop;
                    collectedAtCycleStart = collectedTotal();
                    phaseStartedTick = serverTick;
                    phase = Phase.PICKUP_WAIT;
                    return;
                }
                current = selectReachedCandidate(position);
                if (current == null) {
                    boolean liveDomainRemains = activeDomain.stream()
                        .anyMatch(remainingCandidates::contains);
                    if (liveDomainRemains) {
                        recordDomainFailure("reached_stand_candidate_mismatch");
                        retireActiveCandidateDomain("reached_stand_candidate_mismatch");
                        phase = Phase.NEXT_CANDIDATE;
                        return;
                    }
                    phase = Phase.NEXT_CANDIDATE;
                    return;
                }
                retireCandidate(current);
                candidateAttempts++;
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
        return planExcavation(serverTick, position, sourceFailure);
    }

    private boolean planExcavation(
        int serverTick,
        NavigateExecutor.PositionSource.Position position,
        String sourceFailure
    ) {
        List<ExcavationPlanner.Target> targets = activeDomain.stream()
            .filter(remainingCandidates::contains)
            .filter(candidate -> candidate.blockId().equals(
                blocks.blockIdAt(candidate.x(), candidate.y(), candidate.z())
            ))
            .map(candidate -> new ExcavationPlanner.Target(candidate.x(), candidate.y(), candidate.z()))
            .toList();
        int remainingBreaks = MAX_APPROACH_BREAKS - excavationBreaks;
        if (remainingBreaks <= 0) {
            excavationFailure = sourceFailure + ":mutation_budget_exhausted";
            return false;
        }
        ExcavationPlanner.Result result = ExcavationPlanner.planWithReturnPath(
            world,
            new ExcavationPlanner.Cell(position.blockX(), position.feetBlockY(), position.blockZ()),
            targets,
            excavationDenied,
            remainingBreaks
        );
        if (!result.success()) {
            excavationFailure = sourceFailure + ":" + result.reason();
            return false;
        }
        excavationPlan = result.plan();
        excavationTarget = activeDomain.stream()
            .filter(candidate -> candidate.x() == excavationPlan.target().x())
            .filter(candidate -> candidate.y() == excavationPlan.target().y())
            .filter(candidate -> candidate.z() == excavationPlan.target().z())
            .findFirst()
            .orElse(null);
        if (excavationTarget == null) {
            excavationFailure = sourceFailure + ":target_mapping_failed";
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
        JsonArray preview = new JsonArray();
        for (int index = 0; index < Math.min(excavationPlan.steps().size(), 16); index++) {
            ExcavationPlanner.Step step = excavationPlan.steps().get(index);
            JsonObject item = new JsonObject();
            item.addProperty("x", step.stand().x());
            item.addProperty("y", step.stand().y());
            item.addProperty("z", step.stand().z());
            item.addProperty("mode", step.mode().name().toLowerCase());
            item.addProperty("blockers", step.blockers().size());
            preview.add(item);
        }
        data.add("step_preview", preview);
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
                failOrReplanExcavation(serverTick, position, "blocker_not_solid:" + kind.name().toLowerCase());
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
            && position.blockZ() == stand.z()) {
            excavationStepIndex++;
            advanceExcavation(serverTick, position);
            return;
        }
        phaseStartedTick = serverTick;
        approach = new ApproachController(
            bot,
            actionId,
            new Goal.Stand(stand.x(), stand.y(), stand.z()),
            world,
            movement,
            events,
            APPROACH_REPLAN_LIMIT,
            0.15,
            true
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
                ? "governance_denied:" + gate.reason(proposalId)
                : "governance_verdict_timeout";
            emitVerdict(serverTick, "collect_excavation_mutation_denied", reason);
            gate.discard(proposalId);
            proposalId = null;
            excavationDenied.add(excavationBlock);
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
        movement.lookAt(
            bot,
            excavationBlock.x() + 0.5,
            excavationBlock.y() + 0.5,
            excavationBlock.z() + 0.5
        );
        ExactBlockBreaker.Outcome outcome = blockBreaker.begin(
            bot,
            excavationBlock.x(),
            excavationBlock.y(),
            excavationBlock.z(),
            excavationBlockId,
            serverTick
        );
        if (outcome.state() == ExactBlockBreaker.State.FAILED) {
            failOrReplanExcavation(serverTick, position, "exact_break_failed:" + outcome.reason());
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
            mutationsCompleted++;
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
        if (completeFromInventoryTarget(serverTick)) {
            return;
        }
        ExactBlockBreaker.Outcome outcome = blockBreaker.tick(bot, serverTick);
        if (outcome.state() == ExactBlockBreaker.State.FAILED) {
            failOrReplanExcavation(serverTick, position, "exact_break_failed:" + outcome.reason());
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
            failOrReplanExcavation(serverTick, position, "movement_failed:" + outcome.reason());
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
            retireCandidate(excavationTarget);
            current = excavationTarget;
            recordCandidateRetired("target_changed", observed);
            current = null;
            phase = Phase.NEXT_CANDIDATE;
            return;
        }
        if (!interactGoal(excavationTarget).isSatisfied(
            world, position.blockX(), position.feetBlockY(), position.blockZ()
        )) {
            failOrReplanExcavation(serverTick, position, "terminal_interaction_unsatisfied");
            return;
        }
        current = excavationTarget;
        retireCandidate(current);
        candidateAttempts++;
        beginTargetMutation(serverTick);
    }

    private void failOrReplanExcavation(
        int serverTick,
        NavigateExecutor.PositionSource.Position position,
        String reason
    ) {
        excavationFailure = reason;
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
            recordDomainFailure("excavation_failed:" + reason);
            retireActiveCandidateDomain("candidate_domain_unreachable:excavation_" + reason);
            phase = Phase.NEXT_CANDIDATE;
            return;
        }
        excavationReplans++;
        if (!planExcavation(serverTick, position, reason)) {
            recordDomainFailure("excavation_failed:" + excavationFailure);
            retireActiveCandidateDomain("candidate_domain_unreachable:excavation_" + excavationFailure);
            phase = Phase.NEXT_CANDIDATE;
        }
    }

    private void beginTargetMutation(int serverTick) {
        if (targetBreaks >= maxTargetMutations) {
            recordAttemptFailure("mutation_budget_exhausted", null);
            phase = Phase.NEXT_CANDIDATE;
            return;
        }
        collectedAtCycleStart = collectedTotal();
        emitCandidateSelected(serverTick);
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
                movement.lookAt(bot, current.x() + 0.5, current.y() + 0.5, current.z() + 0.5);
                ExactBlockBreaker.Outcome outcome = blockBreaker.begin(
                    bot, current.x(), current.y(), current.z(), current.blockId(), serverTick
                );
                if (outcome.state() == ExactBlockBreaker.State.FAILED) {
                    recordAttemptFailure("exact_break_failed:" + outcome.reason(), current.blockId());
                    phase = Phase.NEXT_CANDIDATE;
                    return;
                }
                phaseStartedTick = serverTick;
                phase = Phase.MINING;
            }
            case DENIED, TIMED_OUT -> {
                String reason = state == MutationGate.State.DENIED
                    ? "governance_denied:" + gate.reason(proposalId)
                    : "governance_verdict_timeout";
                emitVerdict(serverTick, "mutation_denied", reason);
                gate.discard(proposalId);
                recordAttemptFailure(reason, null);
                phase = Phase.NEXT_CANDIDATE;
            }
        }
    }

    private void miningTick(int serverTick) {
        String observed = blocks.blockIdAt(current.x(), current.y(), current.z());
        if (observed == null) {
            hygiene.clearAll(bot);
            recordAttemptFailure("target_unloaded_during_mine", null);
            phase = Phase.NEXT_CANDIDATE;
            return;
        }
        if (!observed.equals(current.blockId())) {
            // The world actually changed: the mine is verified.
            blockBreaker.abort(bot);
            hygiene.clearAll(bot);
            JsonObject data = new JsonObject();
            data.addProperty("block_id", current.blockId());
            data.addProperty("now", observed);
            data.addProperty("break_ticks", serverTick - phaseStartedTick);
            events.emit(bot, serverTick, "mutation_verified", actionId, data);
            targetBreaks++;
            mutationsCompleted++;
            minedTargets.add(current);
            JsonObject mined = blockFacts();
            mined.addProperty("break_ticks", serverTick - phaseStartedTick);
            minedLedger.add(mined);
            approach = null;
            pickupDrop = null;
            plannedPickupDrop = null;
            phaseStartedTick = serverTick;
            phase = Phase.PICKUP_LOCATE;
            return;
        }
        if (completeFromInventoryTarget(serverTick)) {
            return;
        }
        ExactBlockBreaker.Outcome outcome = blockBreaker.tick(bot, serverTick);
        if (outcome.state() == ExactBlockBreaker.State.FAILED) {
            blockBreaker.abort(bot);
            recordAttemptFailure("exact_break_failed:" + outcome.reason(), observed);
            phase = Phase.NEXT_CANDIDATE;
            return;
        }
        if (serverTick - phaseStartedTick > BREAK_TIMEOUT_TICKS) {
            blockBreaker.abort(bot);
            hygiene.clearAll(bot);
            recordAttemptFailure("break_timeout", null);
            phase = Phase.NEXT_CANDIDATE;
        }
    }

    private void pickupLocateTick(
        int serverTick,
        NavigateExecutor.PositionSource.Position position
    ) {
        if (completeFromInventoryTarget(serverTick)) {
            return;
        }
        if (collectedTotal() > collectedAtCycleStart) {
            recordPickupProgress(serverTick, "inventory_changed_after_break", null);
            phase = Phase.NEXT_CANDIDATE;
            return;
        }
        refreshPendingDrops(position, serverTick);
        if (!pendingDrops.isEmpty()) {
            // The next planner domain contains both remaining resource blocks
            // and these live item entities, so it can choose by actual route
            // cost instead of forcing one mine -> one pickup serialization.
            phase = Phase.NEXT_CANDIDATE;
            return;
        }
        if (serverTick - phaseStartedTick > DROP_LOCATE_TIMEOUT_TICKS) {
            recordAttemptFailure("drop_not_observed_after_break", null);
            phase = Phase.NEXT_CANDIDATE;
        }
    }

    private void pickupApproachTick(int serverTick, NavigateExecutor.PositionSource.Position position) {
        if (completeFromInventoryTarget(serverTick)) {
            return;
        }
        refreshPendingDrops(position, serverTick);
        Drop observed = currentPickupDrop();
        if (observed == null) {
            if (pickupDrop != null && !retiredDropIds.contains(pickupDrop.entityId())) {
                recordDropLoss(serverTick, pickupDrop, "drop_no_longer_observed");
            }
            phase = Phase.NEXT_CANDIDATE;
            return;
        }
        if (observed != null) {
            boolean moved = plannedPickupDrop != null
                && distanceSquared(plannedPickupDrop, observed)
                >= DROP_REPLAN_DISTANCE * DROP_REPLAN_DISTANCE;
            pickupDrop = observed;
            if (moved && approach != null) {
                approach.halt();
                approach = null;
                plannedPickupDrop = null;
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
                PICKUP_FINAL_REACH_DISTANCE,
                true
            );
            plannedPickupDrop = pickupDrop;
        }
        ApproachController.Outcome outcome = approach.tick(serverTick, position.x(), position.y(), position.z());
        if (outcome.status() == ApproachController.Status.WORKING) {
            return;
        }
        approach.halt();
        approach = null;
        if (outcome.status() == ApproachController.Status.FAILED) {
            retryOrRetireDrop(serverTick, position, pickupDrop, outcome.reason());
            phase = Phase.NEXT_CANDIDATE;
            return;
        }
        phaseStartedTick = serverTick;
        phase = Phase.PICKUP_WAIT;
    }

    private void pickupWaitTick(
        int serverTick,
        NavigateExecutor.PositionSource.Position position
    ) {
        if (completeFromInventoryTarget(serverTick)) {
            return;
        }
        if (collectedTotal() > collectedAtCycleStart) {
            recordPickupProgress(serverTick, "pickup_verified", pickupDrop);
            phase = Phase.NEXT_CANDIDATE;
            return;
        }
        refreshPendingDrops(position, serverTick);
        Drop observed = currentPickupDrop();
        if (observed != null) {
            boolean moved = plannedPickupDrop != null
                && distanceSquared(plannedPickupDrop, observed)
                >= DROP_REPLAN_DISTANCE * DROP_REPLAN_DISTANCE;
            pickupDrop = observed;
            int x = (int) Math.floor(position.x());
            int y = (int) Math.floor(position.y());
            int z = (int) Math.floor(position.z());
            boolean outsidePickupVolume = !pickupGoal(pickupDrop).isSatisfied(world, x, y, z);
            if (outsidePickupVolume || moved) {
                plannedPickupDrop = null;
                phase = Phase.NEXT_CANDIDATE;
                return;
            }
        } else if (pickupDrop != null
            && !pendingDrops.containsKey(pickupDrop.entityId())
            && !retiredDropIds.contains(pickupDrop.entityId())) {
            recordDropLoss(serverTick, pickupDrop, "drop_no_longer_observed");
            phase = Phase.NEXT_CANDIDATE;
            return;
        } else if (pickupDrop != null && retiredDropIds.contains(pickupDrop.entityId())) {
            phase = Phase.NEXT_CANDIDATE;
            return;
        }
        if (serverTick - phaseStartedTick > PICKUP_TIMEOUT_TICKS) {
            retryOrRetireDrop(serverTick, position, pickupDrop, "pickup_timeout");
            phase = Phase.NEXT_CANDIDATE;
        }
    }

    private boolean completeFromInventoryTarget(int serverTick) {
        JsonObject delta = inventoryDelta();
        if (delta.get("total").getAsInt() < targetCount) {
            return false;
        }
        reconcileCompletionPickups(serverTick);
        JsonObject facts = new JsonObject();
        facts.add("inventory_delta", delta);
        facts.addProperty("target_count", targetCount);
        facts.addProperty("collected_count", delta.get("total").getAsInt());
        if (current != null) {
            facts.add("mined_block", blockFacts());
        }
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

    private void reconcileCompletionPickups(int serverTick) {
        int collected = collectedTotal();
        int gained = Math.max(0, collected - recordedCollectedTotal);
        recordedCollectedTotal = Math.max(recordedCollectedTotal, collected);
        pickupCredit += gained;
        for (TrackedDrop tracked : List.copyOf(pendingDrops.values())) {
            Drop drop = tracked.drop();
            if (pickupCredit < drop.count()) {
                continue;
            }
            pickupCredit -= drop.count();
            pendingDrops.remove(drop.entityId());
            retiredDropIds.add(drop.entityId());
            recordReconciledPickup(serverTick, drop);
        }
    }

    private void refreshPendingDrops(
        NavigateExecutor.PositionSource.Position position,
        int serverTick
    ) {
        Map<String, Drop> observed = new LinkedHashMap<>();
        List<int[]> centers = new ArrayList<>();
        centers.add(new int[] {position.blockX(), position.feetBlockY(), position.blockZ()});
        int firstRecent = Math.max(0, minedTargets.size() - MAX_ACTIVE_DROP_GOALS);
        for (int index = firstRecent; index < minedTargets.size(); index++) {
            Candidate mined = minedTargets.get(index);
            centers.add(new int[] {mined.x(), mined.y(), mined.z()});
        }
        List<Drop> trackingCenters = new ArrayList<>(activePickupDomain);
        if (pickupDrop != null) {
            trackingCenters.add(pickupDrop);
        }
        for (Drop drop : trackingCenters) {
            centers.add(new int[] {
                (int) Math.floor(drop.x()),
                (int) Math.floor(drop.y()),
                (int) Math.floor(drop.z()),
            });
        }
        for (String itemId : expectedItemIds) {
            for (int[] center : centers) {
                for (Drop drop : drops.nearby(
                    itemId,
                    center[0],
                    center[1],
                    center[2],
                    DROP_SEARCH_RADIUS
                )) {
                    if (!retiredDropIds.contains(drop.entityId())) {
                        observed.put(drop.entityId(), drop);
                    }
                }
            }
        }

        for (Drop drop : observed.values()) {
            TrackedDrop prior = pendingDrops.get(drop.entityId());
            pendingDrops.put(
                drop.entityId(),
                prior == null
                    ? new TrackedDrop(drop, serverTick, serverTick)
                    : prior.observed(drop, serverTick)
            );
        }
        for (TrackedDrop tracked : List.copyOf(pendingDrops.values())) {
            if (observed.containsKey(tracked.drop().entityId())) {
                continue;
            }
            if (serverTick - tracked.lastSeenTick() <= DROP_LOST_AFTER_TICKS) {
                continue;
            }
            pendingDrops.remove(tracked.drop().entityId());
            retiredDropIds.add(tracked.drop().entityId());
            if (pickupCredit >= tracked.drop().count()) {
                pickupCredit -= tracked.drop().count();
                recordReconciledPickup(serverTick, tracked.drop());
            } else {
                recordDropLoss(serverTick, tracked.drop(), "drop_no_longer_observed");
            }
        }
    }

    private Drop selectReachedDrop(NavigateExecutor.PositionSource.Position position) {
        int x = position.blockX();
        int y = position.feetBlockY();
        int z = position.blockZ();
        return activePickupDomain.stream()
            .map(drop -> {
                TrackedDrop tracked = pendingDrops.get(drop.entityId());
                return tracked == null ? null : tracked.drop();
            })
            .filter(drop -> drop != null && pickupGoal(drop).isSatisfied(world, x, y, z))
            .min(Comparator.comparingDouble(drop -> distanceFromPositionSquared(position, drop)))
            .orElse(null);
    }

    private boolean activeDropMovedSincePlanning() {
        for (Drop planned : activePickupDomain) {
            TrackedDrop tracked = pendingDrops.get(planned.entityId());
            if (tracked != null
                && distanceSquared(planned, tracked.drop())
                    >= DROP_REPLAN_DISTANCE * DROP_REPLAN_DISTANCE) {
                return true;
            }
        }
        return false;
    }

    private boolean hasSafePickupStand(Drop drop) {
        Goal.Pickup goal = pickupGoal(drop);
        int centerX = (int) Math.floor(drop.x());
        int centerY = (int) Math.floor(drop.y());
        int centerZ = (int) Math.floor(drop.z());
        for (int y = centerY - 2; y <= centerY + 1; y++) {
            for (int x = centerX - 2; x <= centerX + 2; x++) {
                for (int z = centerZ - 2; z <= centerZ + 2; z++) {
                    if (goal.isSatisfied(world, x, y, z)) {
                        return true;
                    }
                }
            }
        }
        return false;
    }

    private void retryOrRetireActiveDrops(
        int serverTick,
        NavigateExecutor.PositionSource.Position position,
        String reason
    ) {
        refreshPendingDrops(position, serverTick);
        for (Drop planned : activePickupDomain) {
            TrackedDrop tracked = pendingDrops.get(planned.entityId());
            if (tracked == null) {
                continue;
            }
            int attempts = dropApproachAttempts.merge(planned.entityId(), 1, Integer::sum);
            if (attempts < MAX_DROP_APPROACH_ATTEMPTS) {
                continue;
            }
            Drop currentDrop = tracked.drop();
            pendingDrops.remove(currentDrop.entityId());
            retiredDropIds.add(currentDrop.entityId());
            recordDropLoss(serverTick, currentDrop, "pickup_unreachable:" + reason);
        }
        activePickupDomain = List.of();
    }

    private void retryOrRetireDrop(
        int serverTick,
        NavigateExecutor.PositionSource.Position position,
        Drop planned,
        String reason
    ) {
        refreshPendingDrops(position, serverTick);
        if (planned == null) {
            return;
        }
        TrackedDrop tracked = pendingDrops.get(planned.entityId());
        if (tracked == null) {
            return;
        }
        int attempts = dropApproachAttempts.merge(planned.entityId(), 1, Integer::sum);
        if (attempts < MAX_DROP_APPROACH_ATTEMPTS) {
            return;
        }
        Drop currentDrop = tracked.drop();
        pendingDrops.remove(currentDrop.entityId());
        retiredDropIds.add(currentDrop.entityId());
        recordDropLoss(serverTick, currentDrop, "pickup_unreachable:" + reason);
    }

    private void retireActiveCandidateDomain(String reason) {
        for (Candidate candidate : List.copyOf(activeDomain)) {
            if (!retireCandidate(candidate)) {
                continue;
            }
            current = candidate;
            recordCandidateRetired(reason, blocks.blockIdAt(candidate.x(), candidate.y(), candidate.z()));
        }
        current = null;
        activeDomain = List.of();
    }

    private void recordPickupProgress(int serverTick, String reason, Drop drop) {
        int collected = collectedTotal();
        int gained = Math.max(0, collected - recordedCollectedTotal);
        recordedCollectedTotal = Math.max(recordedCollectedTotal, collected);
        pickupCredit += gained;
        JsonObject progress = new JsonObject();
        progress.addProperty("reason", reason);
        progress.addProperty("collected_count", collected);
        progress.addProperty("gained_count", gained);
        if (drop != null) {
            progress.addProperty("entity_id", drop.entityId());
            progress.addProperty("item_id", drop.itemId());
            pendingDrops.remove(drop.entityId());
            retiredDropIds.add(drop.entityId());
            pickupCredit = Math.max(0, pickupCredit - drop.count());
        }
        pickupLedger.add(progress);
        events.emit(bot, serverTick, "collection_inventory_progress", actionId, progress.deepCopy());
    }

    private void recordReconciledPickup(int serverTick, Drop drop) {
        JsonObject pickup = new JsonObject();
        pickup.addProperty("reason", "pickup_entity_reconciled");
        pickup.addProperty("entity_id", drop.entityId());
        pickup.addProperty("item_id", drop.itemId());
        pickup.addProperty("count", drop.count());
        pickup.addProperty("collected_count", collectedTotal());
        pickupLedger.add(pickup);
        events.emit(bot, serverTick, "collection_pickup_reconciled", actionId, pickup.deepCopy());
    }

    private void recordDropLoss(int serverTick, Drop drop, String reason) {
        if (!reportedLostDropIds.add(drop.entityId())) {
            return;
        }
        JsonObject loss = new JsonObject();
        loss.addProperty("entity_id", drop.entityId());
        loss.addProperty("item_id", drop.itemId());
        loss.addProperty("count", drop.count());
        loss.addProperty("x", drop.x());
        loss.addProperty("y", drop.y());
        loss.addProperty("z", drop.z());
        loss.addProperty("reason", reason);
        dropLossLedger.add(loss);
        events.emit(bot, serverTick, "collection_drop_retired", actionId, loss.deepCopy());
    }

    private static double distanceSquared(Drop left, Drop right) {
        double dx = left.x() - right.x();
        double dy = left.y() - right.y();
        double dz = left.z() - right.z();
        return dx * dx + dy * dy + dz * dz;
    }

    private static double distanceFromPositionSquared(
        NavigateExecutor.PositionSource.Position position,
        Drop drop
    ) {
        double dx = position.x() - drop.x();
        double dy = position.y() - drop.y();
        double dz = position.z() - drop.z();
        return dx * dx + dy * dy + dz * dz;
    }

    private Drop currentPickupDrop() {
        if (pickupDrop == null) {
            return null;
        }
        TrackedDrop tracked = pendingDrops.get(pickupDrop.entityId());
        return tracked == null ? null : tracked.drop();
    }

    private static Goal.Pickup pickupGoal(Drop drop) {
        return new Goal.Pickup(drop.x(), drop.y(), drop.z());
    }

    private int collectedTotal() {
        int total = 0;
        for (Map.Entry<String, Integer> entry : baselineCounts.entrySet()) {
            total += Math.max(0, inventory.countOf(entry.getKey()) - entry.getValue());
        }
        return total;
    }

    private JsonObject inventoryDelta() {
        JsonObject delta = new JsonObject();
        JsonObject items = new JsonObject();
        int total = 0;
        String firstChangedItem = null;
        int firstBefore = 0;
        int firstAfter = 0;
        for (Map.Entry<String, Integer> entry : baselineCounts.entrySet()) {
            int now = inventory.countOf(entry.getKey());
            int gained = Math.max(0, now - entry.getValue());
            JsonObject item = new JsonObject();
            item.addProperty("before", entry.getValue());
            item.addProperty("after", now);
            item.addProperty("delta", gained);
            items.add(entry.getKey(), item);
            total += gained;
            if (gained > 0 && firstChangedItem == null) {
                firstChangedItem = entry.getKey();
                firstBefore = entry.getValue();
                firstAfter = now;
            }
        }
        delta.addProperty("total", total);
        delta.add("items", items);
        if (firstChangedItem != null) {
            // Preserve the original single-item shape for old readers while
            // exposing aggregate truth for interchangeable resource families.
            delta.addProperty("item_id", firstChangedItem);
            delta.addProperty("before", firstBefore);
            delta.addProperty("after", firstAfter);
        }
        return delta;
    }

    private void emitVerdict(int serverTick, String eventName, String reason) {
        JsonObject data = new JsonObject();
        data.addProperty("proposal_id", proposalId);
        if (reason != null) {
            data.addProperty("reason", reason);
        }
        events.emit(bot, serverTick, eventName, actionId, data);
    }

    private void recordAttemptFailure(String reason, String observedBlock) {
        lastFailureReason = reason;
        JsonObject failure = new JsonObject();
        failure.addProperty("attempt", candidateAttempts);
        if (current != null) {
            failure.addProperty("x", current.x());
            failure.addProperty("y", current.y());
            failure.addProperty("z", current.z());
            failure.addProperty("block_id", current.blockId());
        }
        failure.addProperty("reason", reason);
        if (observedBlock != null) {
            failure.addProperty("observed", observedBlock);
        }
        attemptFailures.add(failure);
    }

    private void recordDomainFailure(String reason) {
        lastFailureReason = reason;
        JsonObject failure = new JsonObject();
        failure.addProperty("attempt", candidateAttempts + 1);
        failure.addProperty("candidate_count", activeDomain.size());
        failure.addProperty("reason", reason);
        attemptFailures.add(failure);
    }

    private void recordCandidateRetired(String reason, String observedBlock) {
        lastFailureReason = reason;
        candidatesRetired++;
        JsonObject failure = new JsonObject();
        failure.addProperty("candidate_retired", candidatesRetired);
        if (current != null) {
            failure.addProperty("x", current.x());
            failure.addProperty("y", current.y());
            failure.addProperty("z", current.z());
            failure.addProperty("block_id", current.blockId());
        }
        failure.addProperty("reason", reason);
        if (observedBlock != null) {
            failure.addProperty("observed", observedBlock);
        }
        attemptFailures.add(failure);
    }

    private void rescanCandidateDomain(
        int serverTick,
        NavigateExecutor.PositionSource.Position position
    ) {
        if (candidateSource == null) {
            return;
        }
        CandidateScan scan = candidateSource.scan(
            position.blockX(),
            position.feetBlockY(),
            position.blockZ(),
            serverTick
        );
        candidateScans++;
        lastScanMatches = scan.candidates().size();
        lastScanCoverageComplete = scan.coverageComplete();
        lastScanUnloadedChunks = scan.unloadedChunkCount();
        anyScanResultCapped |= scan.resultCapped();
        int discovered = 0;
        for (Candidate candidate : scan.candidates()) {
            if (addDiscoveredCandidate(candidate)) {
                discovered++;
            }
        }
        candidatesDiscoveredAfterStart += discovered;
        updateSearchFacts();

        JsonObject data = new JsonObject();
        data.addProperty("scan", candidateScans);
        data.addProperty("matches", scan.candidates().size());
        data.addProperty("new_candidates", discovered);
        data.addProperty("known_candidates", candidates.size());
        data.addProperty("remaining_candidates", remainingCandidates.size());
        data.addProperty("coverage_complete", scan.coverageComplete());
        data.addProperty("unloaded_chunk_count", scan.unloadedChunkCount());
        data.addProperty("result_capped", scan.resultCapped());
        events.emit(bot, serverTick, "collection_candidate_domain_refreshed", actionId, data);
    }

    private boolean addDiscoveredCandidate(Candidate candidate) {
        String key = candidateKey(candidate);
        if (seenCandidateKeys.contains(key) || retiredCandidateKeys.contains(key)) {
            return false;
        }
        seenCandidateKeys.add(key);
        candidates.add(candidate);
        remainingCandidates.add(candidate);
        return true;
    }

    private boolean retireCandidate(Candidate candidate) {
        retiredCandidateKeys.add(candidateKey(candidate));
        return remainingCandidates.remove(candidate);
    }

    private static String candidateKey(Candidate candidate) {
        return candidate.x() + "," + candidate.y() + "," + candidate.z() + ":" + candidate.blockId();
    }

    private void updateSearchFacts() {
        searchFacts.addProperty("scans", candidateScans);
        searchFacts.addProperty("unique_candidates", candidates.size());
        searchFacts.addProperty("discovered_after_start", candidatesDiscoveredAfterStart);
        searchFacts.addProperty("last_matches", lastScanMatches);
        searchFacts.addProperty("last_coverage_complete", lastScanCoverageComplete);
        searchFacts.addProperty("last_unloaded_chunk_count", lastScanUnloadedChunks);
        searchFacts.addProperty("any_result_capped", anyScanResultCapped);
    }

    private void refreshLiveCandidates() {
        int liveCandidates = 0;
        for (Candidate candidate : List.copyOf(remainingCandidates)) {
            String observed = blocks.blockIdAt(candidate.x(), candidate.y(), candidate.z());
            if (observed != null && observed.equals(candidate.blockId())) {
                liveCandidates++;
                if (liveCandidates >= MAX_PLANNING_CANDIDATES) {
                    break;
                }
                continue;
            }
            current = candidate;
            retireCandidate(candidate);
            recordCandidateRetired("target_changed", observed);
        }
        current = null;
    }

    private Candidate selectReachedCandidate(NavigateExecutor.PositionSource.Position position) {
        int standX = (int) Math.floor(position.x());
        int standY = (int) Math.floor(position.y());
        int standZ = (int) Math.floor(position.z());
        Candidate selected = null;
        double selectedDistance = Double.MAX_VALUE;
        for (Candidate candidate : activeDomain) {
            if (!remainingCandidates.contains(candidate)) {
                continue;
            }
            String observed = blocks.blockIdAt(candidate.x(), candidate.y(), candidate.z());
            if (observed == null || !observed.equals(candidate.blockId())) {
                current = candidate;
                retireCandidate(candidate);
                recordCandidateRetired("target_changed", observed);
                current = null;
                continue;
            }
            if (!interactGoal(candidate).isSatisfied(world, standX, standY, standZ)) {
                continue;
            }
            double distance = interactionDistanceSquared(position, candidate);
            if (distance < selectedDistance) {
                selected = candidate;
                selectedDistance = distance;
            }
        }
        return selected;
    }

    private static Goal.Interact interactGoal(Candidate candidate) {
        return new Goal.Interact(
            candidate.x(), candidate.y(), candidate.z(), Goal.Interact.MINE_RANGE
        );
    }

    private static double interactionDistanceSquared(
        NavigateExecutor.PositionSource.Position position,
        Candidate candidate
    ) {
        double dx = position.x() - (candidate.x() + 0.5);
        double dy = position.y() + 1.62 - (candidate.y() + 0.5);
        double dz = position.z() - (candidate.z() + 0.5);
        return dx * dx + dy * dy + dz * dz;
    }

    private void emitCandidateSelected(int serverTick) {
        JsonObject data = new JsonObject();
        data.addProperty("x", current.x());
        data.addProperty("y", current.y());
        data.addProperty("z", current.z());
        data.addProperty("block_id", current.blockId());
        data.addProperty("attempt", candidateAttempts);
        data.addProperty("domain_size", activeDomain.size());
        events.emit(bot, serverTick, "candidate_selected", actionId, data);
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

    private void finishProgressAware(int serverTick, String blocker) {
        int collected = collectedTotal();
        JsonObject facts = new JsonObject();
        facts.addProperty("blocker", blocker);
        String terminalReason = collected > 0
            ? "partial"
            : (lastFailureReason == null ? blocker : lastFailureReason);
        finishWithFacts(
            serverTick,
            collected > 0 ? ActionRuntime.CLASS_PARTIAL : ActionRuntime.CLASS_FAILED,
            terminalReason,
            facts
        );
    }

    private void finishWithFacts(int serverTick, String classification, String reason, JsonObject facts) {
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
        facts.addProperty("target_count", targetCount);
        facts.addProperty("collected_count", collectedTotal());
        facts.addProperty("remaining_count", Math.max(0, targetCount - collectedTotal()));
        facts.addProperty("candidates_tried", candidateAttempts);
        facts.addProperty("target_breaks", targetBreaks);
        facts.addProperty("mutations_completed", mutationsCompleted);
        facts.addProperty("max_candidate_attempts", maxCandidateAttempts);
        facts.addProperty("max_target_mutations", maxTargetMutations);
        facts.addProperty("replans", replansTotal);
        facts.addProperty("approach_excavation_attempted", excavationAttempted);
        facts.addProperty("approach_excavation_used", excavationUsed);
        facts.addProperty("approach_excavation_breaks", excavationBreaks);
        facts.addProperty("approach_excavation_replans", excavationReplans);
        if (excavationFailure != null) {
            facts.addProperty("approach_excavation_failure", excavationFailure);
        }
        facts.add("approach_broken", approachBrokenLedger.deepCopy());
        facts.add("mined", minedLedger.deepCopy());
        facts.add("pickups", pickupLedger.deepCopy());
        facts.add("drop_losses", dropLossLedger.deepCopy());
        facts.addProperty("pending_drop_count", pendingDrops.size());
        facts.add("inventory_delta", inventoryDelta());
        facts.add("attempt_failures", attemptFailures.deepCopy());
        facts.add("search", searchFacts.deepCopy());
        runtime.finish(bot, actionId, classification, facts, serverTick);
    }
}
