package dev.minebot.body.action;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import dev.minebot.body.nav.NavigateExecutor;

/**
 * Governed dig-up vertical escape ("挖竖井垂直逃生"): when the bot is trapped
 * with no horizontal route out, it clears the column straight up to a target
 * height or open sky. Each solid ceiling block goes through the same fail-
 * closed governance round trip as collection — propose the break, wait for an
 * explicit allow, dig only then, verify the world changed — before the body
 * rises into the cleared cell. It never mines into a hazard and never places
 * (pillar-up is the placement twin, tracked separately).
 *
 * The controller logic and its governance discipline are unit-proven here; the
 * live physical jump-into-the-gap timing is validated separately against the
 * server, as every Body slice has been.
 */
public final class AscendExecutor implements ActionRuntime.TickExecutor {
    public static final int MAX_DIG_STEPS = 64;
    public static final int BREAK_TIMEOUT_TICKS = 300;
    public static final int RISE_TIMEOUT_TICKS = 40;

    /** Live block truth; null id when the position is unloaded. */
    public interface BlockReader {
        String blockIdAt(int x, int y, int z);

        /** Whether the column above this cell reaches open sky (no ceiling). */
        boolean skyAbove(int x, int y, int z);
    }

    /** Whether a block id is a hazard the bot must not dig into. */
    public interface HazardPolicy {
        boolean isHazard(String blockId);
    }

    private enum Phase {
        EVALUATE,
        AWAITING_VERDICT,
        BREAKING,
        RISING
    }

    private final String bot;
    private final String actionId;
    private final int targetY;
    private final CollectExecutor.MiningControls mining;
    private final dev.minebot.body.nav.MovementControls movement;
    private final BotControls hygiene;
    private final BlockReader blocks;
    private final HazardPolicy hazards;
    private final MutationGate gate;
    private final CollectExecutor.ProposalSink proposals;
    private final NavigateExecutor.PositionSource positions;
    private final NavigateExecutor.EventSink events;
    private final ActionRuntime runtime;
    private final int timeoutTicks;

    private Phase phase = Phase.EVALUATE;
    private String proposalId;
    private int ceilingX;
    private int ceilingY;
    private int ceilingZ;
    private String ceilingBlock;
    private int phaseStartedTick = -1;
    private int riseFromY;
    private int elapsedTicks;
    private int digSteps;
    private final JsonArray brokenLedger = new JsonArray();

    public AscendExecutor(
        String bot,
        String actionId,
        int targetY,
        CollectExecutor.MiningControls mining,
        dev.minebot.body.nav.MovementControls movement,
        BotControls hygiene,
        BlockReader blocks,
        HazardPolicy hazards,
        MutationGate gate,
        CollectExecutor.ProposalSink proposals,
        NavigateExecutor.PositionSource positions,
        NavigateExecutor.EventSink events,
        ActionRuntime runtime,
        int timeoutTicks
    ) {
        this.bot = bot;
        this.actionId = actionId;
        this.targetY = targetY;
        this.mining = mining;
        this.movement = movement;
        this.hygiene = hygiene;
        this.blocks = blocks;
        this.hazards = hazards;
        this.gate = gate;
        this.proposals = proposals;
        this.positions = positions;
        this.events = events;
        this.runtime = runtime;
        this.timeoutTicks = timeoutTicks;
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
            case EVALUATE -> evaluate(serverTick, position);
            case AWAITING_VERDICT -> verdictTick(serverTick);
            case BREAKING -> breakingTick(serverTick);
            case RISING -> risingTick(serverTick, position);
        }
    }

    private void evaluate(int serverTick, NavigateExecutor.PositionSource.Position position) {
        int x = (int) Math.floor(position.x());
        int feetY = (int) Math.floor(position.y());
        int z = (int) Math.floor(position.z());
        if (feetY >= targetY || blocks.skyAbove(x, feetY, z)) {
            finishWithLedger(serverTick, ActionRuntime.CLASS_COMPLETED, "surface_reached", feetY);
            return;
        }
        if (digSteps >= MAX_DIG_STEPS) {
            finishWithLedger(serverTick, ActionRuntime.CLASS_FAILED, "dig_step_budget_exhausted", feetY);
            return;
        }
        // The ceiling cell is the block above the head (feet+2).
        ceilingX = x;
        ceilingY = feetY + 2;
        ceilingZ = z;
        ceilingBlock = blocks.blockIdAt(ceilingX, ceilingY, ceilingZ);
        if (ceilingBlock == null) {
            finishWithLedger(serverTick, ActionRuntime.CLASS_FAILED, "ceiling_unloaded", feetY);
            return;
        }
        if (hazards.isHazard(ceilingBlock)) {
            finishWithLedger(serverTick, ActionRuntime.CLASS_UNSAFE, "hazard_above:" + ceilingBlock, feetY);
            return;
        }
        if (_isPassable(ceilingBlock)) {
            // Already open above: just rise into it.
            beginRise(serverTick, feetY);
            return;
        }
        MutationGate.Proposal proposal = gate.propose(
            bot, actionId, "break", ceilingX, ceilingY, ceilingZ, ceilingBlock, serverTick
        );
        proposalId = proposal.proposalId();
        proposals.send(proposal);
        JsonObject data = new JsonObject();
        data.addProperty("proposal_id", proposalId);
        data.addProperty("block_id", ceilingBlock);
        data.addProperty("y", ceilingY);
        events.emit(bot, serverTick, "ascent_break_proposed", actionId, data);
        phase = Phase.AWAITING_VERDICT;
    }

    private void verdictTick(int serverTick) {
        MutationGate.State state = gate.poll(proposalId, serverTick);
        switch (state) {
            case PENDING -> {
            }
            case ALLOWED -> {
                gate.discard(proposalId);
                movement.lookAt(bot, ceilingX + 0.5, ceilingY + 0.5, ceilingZ + 0.5);
                mining.attackContinuous(bot);
                phaseStartedTick = serverTick;
                phase = Phase.BREAKING;
            }
            case DENIED, TIMED_OUT -> {
                String reason = state == MutationGate.State.DENIED
                    ? "governance_denied:" + gate.reason(proposalId)
                    : "governance_verdict_timeout";
                gate.discard(proposalId);
                NavigateExecutor.PositionSource.Position pos = positions.position(bot);
                int feetY = pos == null ? targetY : (int) Math.floor(pos.y());
                finishWithLedger(serverTick, ActionRuntime.CLASS_FAILED, reason, feetY);
            }
        }
    }

    private void breakingTick(int serverTick) {
        String now = blocks.blockIdAt(ceilingX, ceilingY, ceilingZ);
        if (now == null) {
            hygiene.clearAll(bot);
            NavigateExecutor.PositionSource.Position pos = positions.position(bot);
            int feetY = pos == null ? targetY : (int) Math.floor(pos.y());
            finishWithLedger(serverTick, ActionRuntime.CLASS_FAILED, "ceiling_unloaded_during_break", feetY);
            return;
        }
        if (_isPassable(now)) {
            hygiene.clearAll(bot);
            JsonObject broken = new JsonObject();
            broken.addProperty("x", ceilingX);
            broken.addProperty("y", ceilingY);
            broken.addProperty("z", ceilingZ);
            broken.addProperty("block_id", ceilingBlock);
            brokenLedger.add(broken);
            digSteps++;
            events.emit(bot, serverTick, "ascent_break_verified", actionId, broken.deepCopy());
            NavigateExecutor.PositionSource.Position pos = positions.position(bot);
            beginRise(serverTick, pos == null ? ceilingY - 2 : (int) Math.floor(pos.y()));
            return;
        }
        movement.lookAt(bot, ceilingX + 0.5, ceilingY + 0.5, ceilingZ + 0.5);
        if (serverTick - phaseStartedTick > BREAK_TIMEOUT_TICKS) {
            hygiene.clearAll(bot);
            NavigateExecutor.PositionSource.Position pos = positions.position(bot);
            int feetY = pos == null ? targetY : (int) Math.floor(pos.y());
            finishWithLedger(serverTick, ActionRuntime.CLASS_FAILED, "ascent_break_timeout", feetY);
        }
    }

    private void beginRise(int serverTick, int fromFeetY) {
        riseFromY = fromFeetY;
        phaseStartedTick = serverTick;
        // Jump up into the cleared cell; the follower physics carry the rise.
        movement.jumpOnce(bot);
        phase = Phase.RISING;
    }

    private void risingTick(int serverTick, NavigateExecutor.PositionSource.Position position) {
        int feetY = (int) Math.floor(position.y());
        if (feetY > riseFromY) {
            phase = Phase.EVALUATE;
            return;
        }
        if (serverTick - phaseStartedTick > RISE_TIMEOUT_TICKS) {
            // Nudge the jump again; give up after the step budget via EVALUATE.
            movement.jumpOnce(bot);
            phaseStartedTick = serverTick;
            if (digSteps >= MAX_DIG_STEPS) {
                finishWithLedger(serverTick, ActionRuntime.CLASS_FAILED, "rise_stalled", feetY);
            } else {
                phase = Phase.EVALUATE;
            }
        }
    }

    private static boolean _isPassable(String blockId) {
        return blockId.equals("minecraft:air")
            || blockId.equals("minecraft:cave_air")
            || blockId.equals("minecraft:void_air");
    }

    private void finish(int serverTick, String classification, String reason) {
        finishWithLedger(serverTick, classification, reason, targetY);
    }

    private void finishWithLedger(int serverTick, String classification, String reason, int finalY) {
        hygiene.clearAll(bot);
        JsonObject facts = new JsonObject();
        facts.addProperty("reason", reason);
        facts.addProperty("final_y", finalY);
        facts.addProperty("target_y", targetY);
        facts.addProperty("dig_steps", digSteps);
        facts.addProperty("elapsed_ticks", elapsedTicks);
        facts.add("broken", brokenLedger.deepCopy());
        runtime.finish(bot, actionId, classification, facts, serverTick);
    }
}
