package dev.minebot.body.action;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import dev.minebot.body.nav.MovementControls;
import dev.minebot.body.nav.NavigateExecutor;

/**
 * Governed staircase escape to open sky. The executor carves one adjacent
 * step up at a time, walks the real FakePlayer onto that step through the
 * shared navigation controller, and repeats until the server observes sky.
 * Every broken foot/head block receives a fresh Python governance verdict.
 */
public final class AscendExecutor implements ActionRuntime.TickExecutor {
    public static final int MAX_ASCEND_STEPS = 64;
    public static final int MAX_BREAK_STEPS = MAX_ASCEND_STEPS * 3;
    public static final int BREAK_TIMEOUT_TICKS = 300;
    public static final int STEP_TIMEOUT_TICKS = 80;
    private static final int JUMP_INTERVAL_TICKS = 5;
    private static final int LANDING_STABLE_TICKS = 3;

    /** Live block truth; null id when the position is unloaded. */
    public interface BlockReader {
        String blockIdAt(int x, int y, int z);

        boolean skyAbove(int x, int y, int z);
    }

    public interface HazardPolicy {
        boolean isHazard(String blockId);
    }

    private enum Phase {
        EVALUATE,
        AWAITING_VERDICT,
        BREAKING,
        MOVING
    }

    private static final int[][] DIRECTIONS = {
        {1, 0}, {0, 1}, {-1, 0}, {0, -1}
    };

    private final String bot;
    private final String actionId;
    private final int targetY;
    private final ExactBlockBreaker blockBreaker;
    private final MovementControls movement;
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
    private int breakX;
    private int breakY;
    private int breakZ;
    private String breakBlock;
    private int targetX;
    private int targetFeetY;
    private int targetZ;
    private int originX;
    private int originZ;
    private int clearIndex;
    private int preferredDirection;
    private int landingStableTicks;
    private int phaseStartedTick = -1;
    private int elapsedTicks;
    private int ascendSteps;
    private int breakSteps;
    private final JsonArray brokenLedger = new JsonArray();

    public AscendExecutor(
        String bot,
        String actionId,
        int targetY,
        ExactBlockBreaker blockBreaker,
        MovementControls movement,
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
        this.blockBreaker = blockBreaker;
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
            case MOVING -> movingTick(serverTick, position);
        }
    }

    private void evaluate(int serverTick, NavigateExecutor.PositionSource.Position position) {
        int x = position.blockX();
        int feetY = position.feetBlockY();
        int z = position.blockZ();
        if (feetY >= targetY || blocks.skyAbove(x, feetY, z)) {
            finishWithLedger(serverTick, ActionRuntime.CLASS_COMPLETED, "surface_reached", feetY);
            return;
        }
        if (ascendSteps >= MAX_ASCEND_STEPS || breakSteps >= MAX_BREAK_STEPS) {
            finishWithLedger(serverTick, ActionRuntime.CLASS_FAILED, "ascend_step_budget_exhausted", feetY);
            return;
        }

        boolean sawHazard = false;
        boolean selected = false;
        for (int offset : new int[] {0, 1, 3, 2}) {
            int direction = (preferredDirection + offset) % DIRECTIONS.length;
            int candidateX = x + DIRECTIONS[direction][0];
            int candidateZ = z + DIRECTIONS[direction][1];
            String support = blocks.blockIdAt(candidateX, feetY, candidateZ);
            String feet = blocks.blockIdAt(candidateX, feetY + 1, candidateZ);
            String head = blocks.blockIdAt(candidateX, feetY + 2, candidateZ);
            if (support == null || feet == null || head == null) {
                continue;
            }
            if (hazards.isHazard(support) || hazards.isHazard(feet) || hazards.isHazard(head)) {
                sawHazard = true;
                continue;
            }
            if (isPassable(support)) {
                continue;
            }
            targetX = candidateX;
            targetFeetY = feetY + 1;
            targetZ = candidateZ;
            originX = x;
            originZ = z;
            preferredDirection = direction;
            selected = true;
            break;
        }
        if (!selected) {
            finishWithLedger(
                serverTick,
                sawHazard ? ActionRuntime.CLASS_UNSAFE : ActionRuntime.CLASS_FAILED,
                sawHazard ? "hazard_blocks_stair_route" : "stair_route_unavailable",
                feetY
            );
            return;
        }
        clearIndex = 0;
        clearNext(serverTick);
    }

    private void clearNext(int serverTick) {
        while (clearIndex < 3) {
            if (clearIndex == 0) {
                // The body rises while jumping toward the next stair, so its
                // current above-head cell must be clear as well.
                breakX = originX;
                breakY = targetFeetY + 1;
                breakZ = originZ;
            } else {
                breakX = targetX;
                breakY = targetFeetY + clearIndex - 1;
                breakZ = targetZ;
            }
            breakBlock = blocks.blockIdAt(breakX, breakY, breakZ);
            if (breakBlock == null) {
                finish(serverTick, ActionRuntime.CLASS_FAILED, "stair_block_unloaded");
                return;
            }
            if (hazards.isHazard(breakBlock)) {
                finish(serverTick, ActionRuntime.CLASS_UNSAFE, "hazard_in_stair_route:" + breakBlock);
                return;
            }
            if (isPassable(breakBlock)) {
                clearIndex++;
                continue;
            }
            MutationGate.Proposal proposal = gate.propose(
                bot, actionId, "break", breakX, breakY, breakZ, breakBlock, serverTick
            );
            proposalId = proposal.proposalId();
            proposals.send(proposal);
            JsonObject data = new JsonObject();
            data.addProperty("proposal_id", proposalId);
            data.addProperty("block_id", breakBlock);
            data.addProperty("x", breakX);
            data.addProperty("y", breakY);
            data.addProperty("z", breakZ);
            events.emit(bot, serverTick, "ascent_break_proposed", actionId, data);
            phase = Phase.AWAITING_VERDICT;
            return;
        }
        beginMove(serverTick);
    }

    private void verdictTick(int serverTick) {
        MutationGate.State state = gate.poll(proposalId, serverTick);
        switch (state) {
            case PENDING -> {
            }
            case ALLOWED -> {
                gate.discard(proposalId);
                movement.lookAt(bot, breakX + 0.5, breakY + 0.5, breakZ + 0.5);
                ExactBlockBreaker.Outcome outcome = blockBreaker.begin(
                    bot, breakX, breakY, breakZ, breakBlock, serverTick
                );
                if (outcome.state() == ExactBlockBreaker.State.FAILED) {
                    finish(
                        serverTick,
                        ActionRuntime.CLASS_FAILED,
                        "exact_break_failed:" + outcome.reason()
                    );
                    return;
                }
                phaseStartedTick = serverTick;
                phase = Phase.BREAKING;
            }
            case DENIED, TIMED_OUT -> {
                String reason = state == MutationGate.State.DENIED
                    ? "governance_denied:" + gate.reason(proposalId)
                    : "governance_verdict_timeout";
                gate.discard(proposalId);
                finish(serverTick, ActionRuntime.CLASS_FAILED, reason);
            }
        }
    }

    private void breakingTick(int serverTick) {
        String now = blocks.blockIdAt(breakX, breakY, breakZ);
        if (now == null) {
            finish(serverTick, ActionRuntime.CLASS_FAILED, "stair_block_unloaded_during_break");
            return;
        }
        if (isPassable(now)) {
            blockBreaker.abort(bot);
            hygiene.clearAll(bot);
            JsonObject broken = new JsonObject();
            broken.addProperty("x", breakX);
            broken.addProperty("y", breakY);
            broken.addProperty("z", breakZ);
            broken.addProperty("block_id", breakBlock);
            brokenLedger.add(broken);
            breakSteps++;
            clearIndex++;
            events.emit(bot, serverTick, "ascent_break_verified", actionId, broken.deepCopy());
            phase = Phase.EVALUATE;
            clearNext(serverTick);
            return;
        }
        ExactBlockBreaker.Outcome outcome = blockBreaker.tick(bot, serverTick);
        if (outcome.state() == ExactBlockBreaker.State.FAILED) {
            finish(
                serverTick,
                ActionRuntime.CLASS_FAILED,
                "exact_break_failed:" + outcome.reason()
            );
            return;
        }
        if (serverTick - phaseStartedTick > BREAK_TIMEOUT_TICKS) {
            finish(serverTick, ActionRuntime.CLASS_FAILED, "ascent_break_timeout");
        }
    }

    private void beginMove(int serverTick) {
        phaseStartedTick = serverTick;
        landingStableTicks = 0;
        movement.sprint(bot);
        movement.lookAt(bot, targetX + 0.5, targetFeetY + 0.5, targetZ + 0.5);
        movement.moveForward(bot);
        movement.jumpOnce(bot);
        phase = Phase.MOVING;
    }

    private void movingTick(int serverTick, NavigateExecutor.PositionSource.Position position) {
        boolean standingOnTarget = position.blockX() == targetX
            && position.blockZ() == targetZ
            && Math.abs(position.y() - targetFeetY) <= 0.05;
        landingStableTicks = standingOnTarget ? landingStableTicks + 1 : 0;
        if (landingStableTicks >= LANDING_STABLE_TICKS) {
            hygiene.clearAll(bot);
            ascendSteps++;
            JsonObject data = new JsonObject();
            data.addProperty("x", targetX);
            data.addProperty("y", targetFeetY);
            data.addProperty("z", targetZ);
            data.addProperty("ascend_steps", ascendSteps);
            events.emit(bot, serverTick, "ascent_step_verified", actionId, data);
            phase = Phase.EVALUATE;
            return;
        }
        if (serverTick - phaseStartedTick > STEP_TIMEOUT_TICKS) {
            finish(serverTick, ActionRuntime.CLASS_FAILED, "stair_step_timeout");
            return;
        }
        movement.lookAt(bot, targetX + 0.5, targetFeetY + 0.5, targetZ + 0.5);
        if ((serverTick - phaseStartedTick) % JUMP_INTERVAL_TICKS == 0) {
            movement.jumpOnce(bot);
        }
    }

    private static boolean isPassable(String blockId) {
        return blockId.equals("minecraft:air")
            || blockId.equals("minecraft:cave_air")
            || blockId.equals("minecraft:void_air");
    }

    private void finish(int serverTick, String classification, String reason) {
        NavigateExecutor.PositionSource.Position position = positions.position(bot);
        int finalY = position == null ? targetY : position.feetBlockY();
        finishWithLedger(serverTick, classification, reason, finalY);
    }

    private void finishWithLedger(int serverTick, String classification, String reason, int finalY) {
        blockBreaker.abort(bot);
        hygiene.clearAll(bot);
        JsonObject facts = new JsonObject();
        facts.addProperty("reason", reason);
        facts.addProperty("final_y", finalY);
        facts.addProperty("target_y", targetY);
        facts.addProperty("ascend_steps", ascendSteps);
        facts.addProperty("break_steps", breakSteps);
        facts.addProperty("elapsed_ticks", elapsedTicks);
        facts.add("broken", brokenLedger.deepCopy());
        runtime.finish(bot, actionId, classification, facts, serverTick);
    }
}
