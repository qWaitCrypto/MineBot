package dev.minebot.body.action;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import dev.minebot.body.nav.ApproachController;
import dev.minebot.body.nav.Goal;
import dev.minebot.body.nav.MovementControls;
import dev.minebot.body.nav.NavigateExecutor;
import dev.minebot.body.nav.WorldView;

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
    public static final int PICKUP_TIMEOUT_TICKS = 80;
    public static final int APPROACH_REPLAN_LIMIT = 3;

    /** Live block truth; null id when the position is unloaded. */
    public interface BlockReader {
        String blockIdAt(int x, int y, int z);
    }

    /** Authoritative server-side inventory count for an item id. */
    public interface InventoryReader {
        int countOf(String itemId);
    }

    /** Sends a MUTATION_PROPOSAL to the governance side. */
    public interface ProposalSink {
        void send(MutationGate.Proposal proposal);
    }

    /** The one extra physical control mining needs beyond movement. */
    public interface MiningControls {
        void attackContinuous(String botName);
    }

    public record Candidate(int x, int y, int z, String blockId) {
    }

    private enum Phase {
        NEXT_CANDIDATE,
        APPROACHING,
        AWAITING_VERDICT,
        MINING,
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
    private final MiningControls mining;
    private final BotControls hygiene;
    private final BlockReader blocks;
    private final InventoryReader inventory;
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
        MiningControls mining,
        BotControls hygiene,
        BlockReader blocks,
        InventoryReader inventory,
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
        this.mining = mining;
        this.hygiene = hygiene;
        this.blocks = blocks;
        this.inventory = inventory;
        this.gate = gate;
        this.proposals = proposals;
        this.positions = positions;
        this.events = events;
        this.runtime = runtime;
        this.timeoutTicks = timeoutTicks;
        this.baselineCounts = new java.util.HashMap<>();
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
                    bot, actionId, "break", current.x(), current.y(), current.z(), current.blockId(), serverTick
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
                movement.lookAt(bot, current.x() + 0.5, current.y() + 0.5, current.z() + 0.5);
                mining.attackContinuous(bot);
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
            hygiene.clearAll(bot);
            JsonObject data = new JsonObject();
            data.addProperty("block_id", current.blockId());
            data.addProperty("now", observed);
            data.addProperty("break_ticks", serverTick - phaseStartedTick);
            events.emit(bot, serverTick, "mutation_verified", actionId, data);
            approach = new ApproachController(
                bot,
                actionId,
                new Goal.Near(current.x(), current.y(), current.z(), 1.2),
                world,
                movement,
                events,
                APPROACH_REPLAN_LIMIT
            );
            phase = Phase.PICKUP_APPROACH;
            return;
        }
        movement.lookAt(bot, current.x() + 0.5, current.y() + 0.5, current.z() + 0.5);
        if (serverTick - phaseStartedTick > BREAK_TIMEOUT_TICKS) {
            hygiene.clearAll(bot);
            recordAttemptFailure("break_timeout", null);
            phase = Phase.NEXT_CANDIDATE;
        }
    }

    private void pickupApproachTick(int serverTick, NavigateExecutor.PositionSource.Position position) {
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
        JsonObject delta = inventoryDelta();
        if (delta != null) {
            JsonObject facts = new JsonObject();
            facts.add("inventory_delta", delta);
            facts.add("mined_block", blockFacts());
            finishWithFacts(serverTick, ActionRuntime.CLASS_COMPLETED, "collected", facts);
            return;
        }
        if (serverTick - phaseStartedTick > PICKUP_TIMEOUT_TICKS) {
            recordAttemptFailure("pickup_not_observed", null);
            phase = Phase.NEXT_CANDIDATE;
        }
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
