package dev.minebot.body.action;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import dev.minebot.body.nav.ApproachController;
import dev.minebot.body.nav.Goal;
import dev.minebot.body.nav.MovementControls;
import dev.minebot.body.nav.NavigateExecutor;
import dev.minebot.body.nav.WorldView;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * Runs one COLLECT_BLOCK action: submit the complete live candidate domain to
 * navigation, lock the candidate selected by the reached stand, propose that
 * exact mutation to Python and wait fail-closed for the verdict, mine only
 * after an explicit allow, verify the block actually changed, then collect the
 * drop and prove it with an authoritative inventory delta. Candidate failures
 * are typed and bounded; success is never inferred from a dispatched command.
 */
public final class CollectExecutor implements ActionRuntime.TickExecutor {
    public static final int MAX_CANDIDATE_ATTEMPTS = 8;
    public static final int MAX_PLANNING_CANDIDATES = Goal.MAX_COMPOSITE_MEMBERS;
    public static final int BREAK_TIMEOUT_TICKS = 300;
    public static final int PICKUP_TIMEOUT_TICKS = 120;
    public static final int DROP_LOCATE_TIMEOUT_TICKS = 60;
    public static final double DROP_REPLAN_DISTANCE = 0.5;
    public static final double DROP_SEARCH_RADIUS = 8.0;
    public static final double PICKUP_FINAL_REACH_DISTANCE = 0.15;
    public static final int APPROACH_REPLAN_LIMIT = 3;

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

    public record Candidate(int x, int y, int z, String blockId) {
    }

    public record Drop(String entityId, String itemId, int count, double x, double y, double z) {
    }

    private enum Phase {
        NEXT_CANDIDATE,
        APPROACHING,
        AWAITING_VERDICT,
        MINING,
        PICKUP_LOCATE,
        PICKUP_APPROACH,
        PICKUP_WAIT
    }

    private final String bot;
    private final String actionId;
    private final List<Candidate> candidates;
    private final List<Candidate> remainingCandidates;
    private final Map<String, String> itemIdByBlockId;
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
    private Map<String, Integer> dropsBeforeBreak = Map.of();
    private Drop pickupDrop;
    private Drop plannedPickupDrop;
    private boolean pickupApproachFailed;
    private final JsonArray attemptFailures = new JsonArray();
    private final Map<String, Integer> baselineCounts;

    public CollectExecutor(
        String bot,
        String actionId,
        List<Candidate> candidates,
        Map<String, String> itemIdByBlockId,
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
        this.bot = bot;
        this.actionId = actionId;
        this.candidates = List.copyOf(candidates);
        this.remainingCandidates = new ArrayList<>(candidates);
        this.itemIdByBlockId = Map.copyOf(itemIdByBlockId);
        this.searchFacts = searchFacts;
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
        this.baselineCounts = new HashMap<>();
        for (String itemId : itemIdByBlockId.values()) {
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
            finish(serverTick, ActionRuntime.CLASS_TIMEOUT, "timeout_ticks_exhausted");
            return;
        }
        NavigateExecutor.PositionSource.Position position = positions.position(bot);
        if (position == null) {
            finish(serverTick, ActionRuntime.CLASS_FAILED, "body_missing");
            return;
        }
        switch (phase) {
            case NEXT_CANDIDATE -> nextCandidate(serverTick);
            case APPROACHING -> approachTick(serverTick, position);
            case AWAITING_VERDICT -> verdictTick(serverTick);
            case MINING -> miningTick(serverTick);
            case PICKUP_LOCATE -> pickupLocateTick(serverTick);
            case PICKUP_APPROACH -> pickupApproachTick(serverTick, position);
            case PICKUP_WAIT -> pickupWaitTick(serverTick, position);
        }
    }

    private void nextCandidate(int serverTick) {
        current = null;
        approach = null;
        if (candidateAttempts >= MAX_CANDIDATE_ATTEMPTS || remainingCandidates.isEmpty()) {
            String reason = candidates.isEmpty() ? "target_not_found" : "candidate_targets_exhausted";
            finish(serverTick, ActionRuntime.CLASS_FAILED, reason);
            return;
        }

        refreshLiveCandidates();
        if (candidateAttempts >= MAX_CANDIDATE_ATTEMPTS || remainingCandidates.isEmpty()) {
            finish(serverTick, ActionRuntime.CLASS_FAILED, "candidate_targets_exhausted");
            return;
        }

        int domainSize = Math.min(
            remainingCandidates.size(),
            MAX_PLANNING_CANDIDATES
        );
        activeDomain = List.copyOf(remainingCandidates.subList(0, domainSize));
        List<Goal> goals = activeDomain.stream()
            .map(candidate -> (Goal) interactGoal(candidate))
            .toList();
        Goal domainGoal = goals.size() == 1 ? goals.get(0) : new Goal.Composite(goals);

        JsonObject data = new JsonObject();
        data.addProperty("candidate_count", activeDomain.size());
        data.addProperty("candidates_retired", candidatesRetired);
        events.emit(bot, serverTick, "candidate_domain_planned", actionId, data);
        approach = new ApproachController(
            bot,
            actionId,
            domainGoal,
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
                recordDomainFailure("approach_failed:" + outcome.reason());
                finish(
                    serverTick,
                    ActionRuntime.CLASS_FAILED,
                    "candidate_domain_unreachable:" + outcome.reason()
                );
            }
            case COMPLETED -> {
                replansTotal += approach.replans();
                approach.halt();
                approach = null;
                current = selectReachedCandidate(position);
                if (current == null) {
                    boolean liveDomainRemains = activeDomain.stream()
                        .anyMatch(remainingCandidates::contains);
                    if (liveDomainRemains) {
                        recordDomainFailure("reached_stand_candidate_mismatch");
                        finish(
                            serverTick,
                            ActionRuntime.CLASS_FAILED,
                            "reached_stand_candidate_mismatch"
                        );
                        return;
                    }
                    phase = Phase.NEXT_CANDIDATE;
                    return;
                }
                remainingCandidates.remove(current);
                candidateAttempts++;
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
        }
    }

    private void verdictTick(int serverTick) {
        MutationGate.State state = gate.poll(proposalId, serverTick);
        switch (state) {
            case PENDING -> {
            }
            case ALLOWED -> {
                emitVerdict(serverTick, "mutation_allowed", gate.reason(proposalId));
                gate.discard(proposalId);
                dropsBeforeBreak = nearbyDropCounts();
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
            approach = null;
            pickupDrop = null;
            plannedPickupDrop = null;
            pickupApproachFailed = false;
            phaseStartedTick = serverTick;
            phase = Phase.PICKUP_LOCATE;
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

    private void pickupLocateTick(int serverTick) {
        if (completeFromInventoryDelta(serverTick)) {
            return;
        }
        Drop observed = attributableDrops().stream()
            .min(Comparator.comparingDouble(this::distanceFromMinedBlockSquared))
            .orElse(null);
        if (observed == null) {
            if (serverTick - phaseStartedTick > DROP_LOCATE_TIMEOUT_TICKS) {
                recordAttemptFailure("pickup_not_observed", null);
                phase = Phase.NEXT_CANDIDATE;
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
            int x = (int) Math.floor(position.x());
            int y = (int) Math.floor(position.y());
            int z = (int) Math.floor(position.z());
            boolean outsidePickupVolume = !pickupGoal(pickupDrop).isSatisfied(world, x, y, z);
            if (outsidePickupVolume && (!pickupApproachFailed || moved)) {
                plannedPickupDrop = null;
                pickupApproachFailed = false;
                phase = Phase.PICKUP_APPROACH;
                return;
            }
        }
        if (serverTick - phaseStartedTick > PICKUP_TIMEOUT_TICKS) {
            recordAttemptFailure("pickup_not_observed", null);
            phase = Phase.NEXT_CANDIDATE;
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
        return drops.nearby(
            itemIdByBlockId.get(current.blockId()),
            current.x(),
            current.y(),
            current.z(),
            DROP_SEARCH_RADIUS
        );
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

    private void recordAttemptFailure(String reason, String observedBlock) {
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
        JsonObject failure = new JsonObject();
        failure.addProperty("attempt", candidateAttempts + 1);
        failure.addProperty("candidate_count", activeDomain.size());
        failure.addProperty("reason", reason);
        attemptFailures.add(failure);
    }

    private void recordCandidateRetired(String reason, String observedBlock) {
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

    private void refreshLiveCandidates() {
        for (Candidate candidate : List.copyOf(remainingCandidates)) {
            String observed = blocks.blockIdAt(candidate.x(), candidate.y(), candidate.z());
            if (observed != null && observed.equals(candidate.blockId())) {
                continue;
            }
            current = candidate;
            remainingCandidates.remove(candidate);
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
                remainingCandidates.remove(candidate);
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

    private void finishWithFacts(int serverTick, String classification, String reason, JsonObject facts) {
        blockBreaker.abort(bot);
        if (approach != null) {
            approach.halt();
        }
        facts.addProperty("reason", reason);
        facts.addProperty("elapsed_ticks", elapsedTicks);
        facts.addProperty("candidates_tried", candidateAttempts);
        facts.addProperty("replans", replansTotal);
        facts.add("attempt_failures", attemptFailures.deepCopy());
        facts.add("search", searchFacts.deepCopy());
        runtime.finish(bot, actionId, classification, facts, serverTick);
    }
}
