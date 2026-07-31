package dev.minebot.body.action;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import dev.minebot.body.nav.MovementControls;
import dev.minebot.body.nav.NavigateExecutor;
import dev.minebot.body.nav.WorldView;

/**
 * Descends one governed shaft until the requested Y is reached. The executor
 * centers the player before opening each floor, checks the complete landing
 * column, waits for authoritative gravity/landing truth, and only then starts
 * the next step.
 */
public final class DigDownExecutor implements ActionRuntime.TickExecutor {
    public static final int MAX_STEPS = 64;
    public static final int BREAK_TIMEOUT_TICKS = 300;
    public static final int CENTER_TIMEOUT_TICKS = 100;
    public static final int DESCENT_TIMEOUT_TICKS = 100;
    private static final double CENTER_TOLERANCE = 0.10;
    private static final int CENTER_STABLE_TICKS = 2;
    private static final int LANDING_STABLE_TICKS = 3;
    private static final double LANDING_Y_TOLERANCE = 0.08;
    private static final double PLAYER_EYE_HEIGHT = 1.62;

    public interface BlockReader {
        String blockIdAt(int x, int y, int z);
    }

    public interface HazardPolicy {
        boolean isHazard(String blockId);
    }

    private enum Phase {
        EVALUATE,
        CENTERING,
        AWAITING_VERDICT,
        BREAKING,
        DESCENDING
    }

    private final String bot;
    private final String actionId;
    private final int targetY;
    private final int maxSteps;
    private final int maxClearFall;
    private final WorldView world;
    private final MovementControls movement;
    private final ExactBlockBreaker blockBreaker;
    private final BotControls hygiene;
    private final BlockReader blocks;
    private final HazardPolicy hazards;
    private final MutationGate gate;
    private final CollectExecutor.ProposalSink proposals;
    private final NavigateExecutor.PositionSource positions;
    private final NavigateExecutor.EventSink events;
    private final ActionRuntime runtime;
    private final int timeoutTicks;

    private final JsonArray brokenLedger = new JsonArray();
    private final JsonArray stepLedger = new JsonArray();
    private Phase phase = Phase.EVALUATE;
    private String proposalId;
    private String floorBlockId;
    private int shaftX;
    private int shaftZ;
    private int stepOriginY;
    private int fallClearance;
    private int phaseStartedTick = -1;
    private int elapsedTicks;
    private int stepsCompleted;
    private int blocksDescended;
    private Integer actionOriginY;
    private int centeredTicks;
    private int landingTicks;
    private boolean moving;

    public DigDownExecutor(
        String bot,
        String actionId,
        int targetY,
        int maxSteps,
        int maxClearFall,
        WorldView world,
        MovementControls movement,
        ExactBlockBreaker blockBreaker,
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
        this.maxSteps = maxSteps;
        this.maxClearFall = maxClearFall;
        this.world = world;
        this.movement = movement;
        this.blockBreaker = blockBreaker;
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
        NavigateExecutor.PositionSource.Position position = positions.position(bot);
        if (position == null) {
            finish(serverTick, ActionRuntime.CLASS_FAILED, "body_missing", null);
            return;
        }
        if (!Double.isFinite(position.x())
            || !Double.isFinite(position.y())
            || !Double.isFinite(position.z())) {
            finish(serverTick, ActionRuntime.CLASS_FAILED, "body_position_invalid", position);
            return;
        }
        if (actionOriginY == null) {
            actionOriginY = position.feetBlockY();
        }
        if (position.feetBlockY() <= targetY && phase == Phase.EVALUATE) {
            finish(serverTick, ActionRuntime.CLASS_COMPLETED, "dig_down_target_reached", position);
            return;
        }
        if (runtime.cancelRequested(actionId)) {
            finish(serverTick, ActionRuntime.CLASS_CANCELED, "cancel_requested", position);
            return;
        }
        if (elapsedTicks > timeoutTicks) {
            finishProgressAware(serverTick, "dig_down_timeout", position);
            return;
        }

        switch (phase) {
            case EVALUATE -> evaluate(serverTick, position);
            case CENTERING -> centeringTick(serverTick, position);
            case AWAITING_VERDICT -> verdictTick(serverTick, position);
            case BREAKING -> breakingTick(serverTick, position);
            case DESCENDING -> descendingTick(serverTick, position);
        }
    }

    private void evaluate(int serverTick, NavigateExecutor.PositionSource.Position position) {
        if (position.feetBlockY() <= targetY) {
            finish(serverTick, ActionRuntime.CLASS_COMPLETED, "dig_down_target_reached", position);
            return;
        }
        if (stepsCompleted >= maxSteps) {
            finishProgressAware(serverTick, "dig_down_step_budget_exhausted", position);
            return;
        }
        shaftX = position.blockX();
        shaftZ = position.blockZ();
        stepOriginY = position.feetBlockY();
        centeredTicks = 0;
        landingTicks = 0;

        String feet = blocks.blockIdAt(shaftX, stepOriginY, shaftZ);
        String head = blocks.blockIdAt(shaftX, stepOriginY + 1, shaftZ);
        if (feet == null || head == null) {
            finishProgressAware(serverTick, "dig_down_start_unloaded", position);
            return;
        }
        if (isLiquidOrHazard(feet) || isLiquidOrHazard(head)) {
            finishProgressAware(serverTick, "dig_down_start_liquid", position);
            return;
        }

        floorBlockId = blocks.blockIdAt(shaftX, stepOriginY - 1, shaftZ);
        if (floorBlockId == null) {
            finishProgressAware(serverTick, "dig_down_target_unloaded", position);
            return;
        }
        if (isLiquidOrHazard(floorBlockId)
            || world.kindAt(shaftX, stepOriginY - 1, shaftZ) == WorldView.NodeKind.LIQUID) {
            finishProgressAware(serverTick, "dig_down_target_liquid", position);
            return;
        }
        if (!scanLandingColumn(serverTick, position)) {
            return;
        }
        beginCentering(serverTick, position);
    }

    private boolean scanLandingColumn(
        int serverTick,
        NavigateExecutor.PositionSource.Position position
    ) {
        fallClearance = 1;
        for (int depth = 1; depth <= maxClearFall; depth++) {
            int y = stepOriginY - 1 - depth;
            String blockId = blocks.blockIdAt(shaftX, y, shaftZ);
            if (blockId == null) {
                finishProgressAware(serverTick, "dig_down_landing_unloaded", position);
                return false;
            }
            WorldView.NodeKind kind = world.kindAt(shaftX, y, shaftZ);
            if (kind == WorldView.NodeKind.LIQUID || isLiquidOrHazard(blockId)) {
                finishProgressAware(serverTick, "dig_down_landing_liquid", position);
                return false;
            }
            if (kind == WorldView.NodeKind.SOLID) {
                return true;
            }
            if (kind == WorldView.NodeKind.UNLOADED) {
                finishProgressAware(serverTick, "dig_down_landing_unloaded", position);
                return false;
            }
            fallClearance++;
            if (fallClearance > maxClearFall) {
                finishProgressAware(serverTick, "dig_down_fall_risk", position);
                return false;
            }
        }
        finishProgressAware(serverTick, "dig_down_fall_risk", position);
        return false;
    }

    private void beginCentering(
        int serverTick,
        NavigateExecutor.PositionSource.Position position
    ) {
        phaseStartedTick = serverTick;
        phase = Phase.CENTERING;
        centeringTick(serverTick, position);
    }

    private void centeringTick(
        int serverTick,
        NavigateExecutor.PositionSource.Position position
    ) {
        if (position.feetBlockY() < stepOriginY) {
            stopMovement();
            phaseStartedTick = serverTick;
            phase = Phase.DESCENDING;
            descendingTick(serverTick, position);
            return;
        }
        if (position.blockX() != shaftX || position.blockZ() != shaftZ) {
            stopMovement();
            finishProgressAware(serverTick, "dig_down_alignment_left_column", position);
            return;
        }
        double dx = shaftX + 0.5 - position.x();
        double dz = shaftZ + 0.5 - position.z();
        double distance = Math.sqrt(dx * dx + dz * dz);
        if (distance <= CENTER_TOLERANCE) {
            stopMovement();
            centeredTicks++;
            if (centeredTicks < CENTER_STABLE_TICKS) {
                return;
            }
            beginFloorAction(serverTick, position);
            return;
        }
        centeredTicks = 0;
        movement.lookAt(bot, shaftX + 0.5, position.y() + PLAYER_EYE_HEIGHT, shaftZ + 0.5);
        if (!moving) {
            movement.moveForward(bot);
            moving = true;
        }
        if (serverTick - phaseStartedTick > CENTER_TIMEOUT_TICKS) {
            stopMovement();
            finishProgressAware(serverTick, "dig_down_alignment_timeout", position);
        }
    }

    private void beginFloorAction(
        int serverTick,
        NavigateExecutor.PositionSource.Position position
    ) {
        String observed = blocks.blockIdAt(shaftX, stepOriginY - 1, shaftZ);
        if (observed == null) {
            finishProgressAware(serverTick, "dig_down_target_unloaded", position);
            return;
        }
        WorldView.NodeKind kind = world.kindAt(shaftX, stepOriginY - 1, shaftZ);
        if (kind != WorldView.NodeKind.SOLID) {
            phaseStartedTick = serverTick;
            phase = Phase.DESCENDING;
            return;
        }
        floorBlockId = observed;
        MutationGate.Proposal proposal = gate.propose(
            bot,
            actionId,
            "break",
            shaftX,
            stepOriginY - 1,
            shaftZ,
            floorBlockId,
            "direct",
            serverTick
        );
        proposalId = proposal.proposalId();
        proposals.send(proposal);
        JsonObject data = new JsonObject();
        data.addProperty("proposal_id", proposalId);
        data.addProperty("block_id", floorBlockId);
        data.addProperty("x", shaftX);
        data.addProperty("y", stepOriginY - 1);
        data.addProperty("z", shaftZ);
        events.emit(bot, serverTick, "dig_down_mutation_proposed", actionId, data);
        phase = Phase.AWAITING_VERDICT;
    }

    private void verdictTick(
        int serverTick,
        NavigateExecutor.PositionSource.Position position
    ) {
        MutationGate.State state = gate.poll(proposalId, serverTick);
        if (state == MutationGate.State.PENDING) {
            return;
        }
        if (state != MutationGate.State.ALLOWED) {
            String reason = state == MutationGate.State.DENIED
                ? "dig_down_denied:" + gate.reason(proposalId)
                : "dig_down_governance_timeout";
            gate.discard(proposalId);
            proposalId = null;
            finishProgressAware(serverTick, reason, position);
            return;
        }
        gate.discard(proposalId);
        proposalId = null;

        String observed = blocks.blockIdAt(shaftX, stepOriginY - 1, shaftZ);
        if (observed == null) {
            finishProgressAware(serverTick, "dig_down_approved_block_unloaded", position);
            return;
        }
        if (!observed.equals(floorBlockId)) {
            if (world.kindAt(shaftX, stepOriginY - 1, shaftZ) != WorldView.NodeKind.SOLID) {
                phaseStartedTick = serverTick;
                phase = Phase.DESCENDING;
            } else {
                finishProgressAware(serverTick, "dig_down_approved_block_changed", position);
            }
            return;
        }

        movement.lookAt(bot, shaftX + 0.5, stepOriginY - 0.5, shaftZ + 0.5);
        ExactBlockBreaker.Outcome outcome = blockBreaker.begin(
            bot,
            shaftX,
            stepOriginY - 1,
            shaftZ,
            floorBlockId,
            serverTick
        );
        if (outcome.state() == ExactBlockBreaker.State.FAILED) {
            finishProgressAware(serverTick, "dig_down_exact_break_failed:" + outcome.reason(), position);
            return;
        }
        phaseStartedTick = serverTick;
        phase = Phase.BREAKING;
    }

    private void breakingTick(
        int serverTick,
        NavigateExecutor.PositionSource.Position position
    ) {
        String observed = blocks.blockIdAt(shaftX, stepOriginY - 1, shaftZ);
        if (observed == null) {
            finishProgressAware(serverTick, "dig_down_target_unloaded_during_break", position);
            return;
        }
        if (!observed.equals(floorBlockId)) {
            blockBreaker.abort(bot);
            hygiene.clearAll(bot);
            JsonObject broken = new JsonObject();
            broken.addProperty("x", shaftX);
            broken.addProperty("y", stepOriginY - 1);
            broken.addProperty("z", shaftZ);
            broken.addProperty("block_id", floorBlockId);
            broken.addProperty("break_ticks", serverTick - phaseStartedTick);
            brokenLedger.add(broken);
            events.emit(bot, serverTick, "dig_down_mutation_verified", actionId, broken.deepCopy());
            phaseStartedTick = serverTick;
            phase = Phase.DESCENDING;
            return;
        }
        ExactBlockBreaker.Outcome outcome = blockBreaker.tick(bot, serverTick);
        if (outcome.state() == ExactBlockBreaker.State.FAILED) {
            blockBreaker.abort(bot);
            finishProgressAware(serverTick, "dig_down_exact_break_failed:" + outcome.reason(), position);
            return;
        }
        if (serverTick - phaseStartedTick > BREAK_TIMEOUT_TICKS) {
            blockBreaker.abort(bot);
            finishProgressAware(serverTick, "dig_down_break_timeout", position);
        }
    }

    private void descendingTick(
        int serverTick,
        NavigateExecutor.PositionSource.Position position
    ) {
        stopMovement();
        int observedY = position.feetBlockY();
        if (observedY < stepOriginY) {
            boolean settled = Math.abs(position.y() - observedY) <= LANDING_Y_TOLERANCE;
            landingTicks = settled ? landingTicks + 1 : 0;
            if (landingTicks >= LANDING_STABLE_TICKS) {
                int descended = stepOriginY - observedY;
                blocksDescended += descended;
                stepsCompleted++;
                JsonObject step = new JsonObject();
                step.addProperty("from_y", stepOriginY);
                step.addProperty("to_y", observedY);
                step.addProperty("blocks_descended", descended);
                step.addProperty("fall_clearance", fallClearance);
                stepLedger.add(step);
                phase = Phase.EVALUATE;
                evaluate(serverTick, position);
                return;
            }
        } else {
            landingTicks = 0;
        }
        if (serverTick - phaseStartedTick > DESCENT_TIMEOUT_TICKS) {
            finishProgressAware(serverTick, "dig_down_descent_timeout", position);
        }
    }

    private boolean isLiquidOrHazard(String blockId) {
        return blockId.equals("minecraft:water")
            || blockId.equals("minecraft:lava")
            || hazards.isHazard(blockId);
    }

    private void stopMovement() {
        if (moving) {
            movement.stopMovement(bot);
            moving = false;
        }
    }

    private void finishProgressAware(
        int serverTick,
        String reason,
        NavigateExecutor.PositionSource.Position position
    ) {
        if (position != null && position.feetBlockY() <= targetY) {
            finish(serverTick, ActionRuntime.CLASS_COMPLETED, "dig_down_target_reached", position);
            return;
        }
        boolean progressed = position != null
            && actionOriginY != null
            && position.feetBlockY() < actionOriginY;
        finish(
            serverTick,
            progressed ? ActionRuntime.CLASS_PARTIAL : ActionRuntime.CLASS_FAILED,
            progressed ? "dig_down_partial:" + reason : reason,
            position
        );
    }

    private void finish(
        int serverTick,
        String classification,
        String reason,
        NavigateExecutor.PositionSource.Position position
    ) {
        stopMovement();
        blockBreaker.abort(bot);
        if (proposalId != null) {
            gate.discard(proposalId);
            proposalId = null;
        }
        JsonObject facts = new JsonObject();
        facts.addProperty("reason", reason);
        facts.addProperty("elapsed_ticks", elapsedTicks);
        facts.addProperty("target_y", targetY);
        facts.addProperty("steps_completed", stepsCompleted);
        facts.addProperty("blocks_descended", blocksDescended);
        facts.addProperty("max_steps", maxSteps);
        facts.addProperty("max_clear_fall", maxClearFall);
        if (position != null) {
            facts.addProperty("final_x", position.x());
            facts.addProperty("final_y", position.y());
            facts.addProperty("final_z", position.z());
            facts.addProperty("final_feet_y", position.feetBlockY());
        }
        facts.add("steps", stepLedger.deepCopy());
        facts.add("broken", brokenLedger.deepCopy());
        runtime.finish(bot, actionId, classification, facts, serverTick);
    }
}
