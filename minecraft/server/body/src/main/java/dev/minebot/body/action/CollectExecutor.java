package dev.minebot.body.action;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import dev.minebot.body.nav.ApproachController;
import dev.minebot.body.nav.Goal;
import dev.minebot.body.nav.MovementControls;
import dev.minebot.body.nav.NavigateExecutor;
import dev.minebot.body.nav.WorldView;

import java.util.Comparator;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * Runs one COLLECT_BLOCK action: iterate natural candidates, approach each
 * through interact-goal navigation, propose the exact mutation to Python and
 * wait fail-closed for the verdict, mine only after an explicit allow, verify
 * the block actually changed, then collect the drop and prove it with an
 * authoritative inventory delta. Every candidate failure is typed and
 * bounded; success is never inferred from a dispatched command.
 */
public final class CollectExecutor implements ActionRuntime.TickExecutor {
    public static final int MAX_CANDIDATE_ATTEMPTS = 8;
    public static final int BREAK_TIMEOUT_TICKS = 300;
    public static final int PICKUP_TIMEOUT_TICKS = 120;
    public static final int DROP_LOCATE_TIMEOUT_TICKS = 60;
    public static final int DROP_SETTLE_TICKS = 5;
    public static final double DROP_SETTLE_DISTANCE = 0.05;
    public static final double DROP_SEARCH_RADIUS = 8.0;
    public static final double PICKUP_STAND_RANGE = 1.0;
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
    private int candidateIndex = -1;
    private Candidate current;
    private ApproachController approach;
    private String proposalId;
    private int phaseStartedTick = -1;
    private int elapsedTicks;
    private int replansTotal;
    private Map<String, Integer> dropsBeforeBreak = Map.of();
    private Drop pickupDrop;
    private int stableDropTicks;
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
            case PICKUP_WAIT -> pickupWaitTick(serverTick);
        }
    }

    private void nextCandidate(int serverTick) {
        candidateIndex++;
        if (candidateIndex >= candidates.size() || candidateIndex >= MAX_CANDIDATE_ATTEMPTS) {
            String reason = candidates.isEmpty() ? "target_not_found" : "candidate_targets_exhausted";
            finish(serverTick, ActionRuntime.CLASS_FAILED, reason);
            return;
        }
        current = candidates.get(candidateIndex);
        String observed = blocks.blockIdAt(current.x(), current.y(), current.z());
        if (observed == null || !observed.equals(current.blockId())) {
            recordAttemptFailure("target_changed", observed);
            phase = Phase.NEXT_CANDIDATE;
            return;
        }
        JsonObject data = new JsonObject();
        data.addProperty("x", current.x());
        data.addProperty("y", current.y());
        data.addProperty("z", current.z());
        data.addProperty("block_id", current.blockId());
        data.addProperty("attempt", candidateIndex + 1);
        events.emit(bot, serverTick, "candidate_selected", actionId, data);
        approach = new ApproachController(
            bot,
            actionId,
            new Goal.Interact(current.x(), current.y(), current.z(), Goal.Interact.MINE_RANGE),
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
                recordAttemptFailure("approach_failed:" + outcome.reason(), null);
                phase = Phase.NEXT_CANDIDATE;
            }
            case COMPLETED -> {
                replansTotal += approach.replans();
                approach.halt();
                String observed = blocks.blockIdAt(current.x(), current.y(), current.z());
                if (observed == null || !observed.equals(current.blockId())) {
                    recordAttemptFailure("target_changed", observed);
                    phase = Phase.NEXT_CANDIDATE;
                    return;
                }
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
            stableDropTicks = 0;
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
        if (pickupDrop != null
            && pickupDrop.entityId().equals(observed.entityId())
            && distanceSquared(pickupDrop, observed) <= DROP_SETTLE_DISTANCE * DROP_SETTLE_DISTANCE) {
            stableDropTicks++;
        } else {
            stableDropTicks = 0;
        }
        pickupDrop = observed;
        if (stableDropTicks < DROP_SETTLE_TICKS) {
            return;
        }
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
        if (approach == null) {
            approach = new ApproachController(
                bot,
                actionId,
                new Goal.Near(
                    (int) Math.floor(pickupDrop.x()),
                    (int) Math.floor(pickupDrop.y()),
                    (int) Math.floor(pickupDrop.z()),
                    PICKUP_STAND_RANGE
                ),
                world,
                movement,
                events,
                APPROACH_REPLAN_LIMIT,
                PICKUP_FINAL_REACH_DISTANCE
            );
        }
        ApproachController.Outcome outcome = approach.tick(serverTick, position.x(), position.y(), position.z());
        if (outcome.status() == ApproachController.Status.WORKING) {
            return;
        }
        // Whether or not the walk-in fully succeeded, give the drop time to
        // arrive and let the inventory delta be the only truth.
        approach.halt();
        phaseStartedTick = serverTick;
        phase = Phase.PICKUP_WAIT;
    }

    private void pickupWaitTick(int serverTick) {
        if (completeFromInventoryDelta(serverTick)) {
            return;
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
        failure.addProperty("attempt", candidateIndex + 1);
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
        facts.addProperty("candidates_tried", Math.max(0, candidateIndex + (phase == Phase.NEXT_CANDIDATE ? 0 : 1)));
        facts.addProperty("replans", replansTotal);
        facts.add("attempt_failures", attemptFailures.deepCopy());
        facts.add("search", searchFacts.deepCopy());
        runtime.finish(bot, actionId, classification, facts, serverTick);
    }
}
