package dev.minebot.body.action;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import dev.minebot.body.nav.ApproachController;
import dev.minebot.body.nav.Goal;
import dev.minebot.body.nav.MovementControls;
import dev.minebot.body.nav.NavigateExecutor;
import dev.minebot.body.nav.WorldView;

import java.util.List;

/**
 * Return to a dry, supported surface. The executor first follows an ordinary
 * complete route, then carves a stair or pillars in place when no such route
 * exists. Every broken or placed block receives a fresh Python governance
 * verdict.
 */
public final class AscendExecutor implements ActionRuntime.TickExecutor {
    public static final int MAX_ASCEND_STEPS = 64;
    public static final int MAX_BREAK_STEPS = MAX_ASCEND_STEPS * 3;
    public static final int BREAK_TIMEOUT_TICKS = 300;
    public static final int STEP_TIMEOUT_TICKS = 80;
    public static final int PILLAR_TIMEOUT_TICKS = 100;
    private static final int JUMP_INTERVAL_TICKS = 5;
    private static final int LANDING_STABLE_TICKS = 3;
    private static final int CENTER_STABLE_TICKS = 2;
    private static final double CENTER_TOLERANCE = 0.10;
    private static final double PILLAR_JUMP_MIN_GAIN = 0.15;
    private static final List<String> PILLAR_BLOCKS = List.of(
        "minecraft:cobblestone",
        "minecraft:cobbled_deepslate",
        "minecraft:deepslate",
        "minecraft:stone",
        "minecraft:dirt",
        "minecraft:netherrack"
    );

    /** Live block truth; null id when the position is unloaded. */
    public interface BlockReader {
        String blockIdAt(int x, int y, int z);

        boolean skyAbove(int x, int y, int z);
    }

    public interface HazardPolicy {
        boolean isHazard(String blockId);
    }

    /** Finds and verifies ordinary walk/swim exits before terrain mutation. */
    public interface SurfaceAccess {
        boolean isSurfaceStand(int x, int y, int z);

        Goal findSurfaceGoal(NavigateExecutor.PositionSource.Position position);

        WorldView world();
    }

    /** Inventory selection and public /player controls needed by pillar-up. */
    public interface PillarAccess {
        ScaffoldSelection selectScaffold(String bot, List<String> candidates);

        String selectedItemId();

        int selectedItemCount();

        void useOnce(String bot);

        void sneak(String bot);
    }

    public record ScaffoldSelection(boolean success, String blockId, int count, String reason) {
    }

    private enum Phase {
        EVALUATE,
        SURFACE_ROUTING,
        AWAITING_VERDICT,
        BREAKING,
        MOVING,
        PILLAR_CENTERING,
        PILLAR_JUMPING,
        PILLAR_SETTLING
    }

    private enum AscentMode {
        STAIR,
        PILLAR
    }

    private enum PendingMutation {
        BREAK,
        PILLAR_PLACE
    }

    private static final int[][] DIRECTIONS = {
        {1, 0}, {0, 1}, {-1, 0}, {0, -1}
    };

    private final String bot;
    private final String actionId;
    private final ExactBlockBreaker blockBreaker;
    private final MovementControls movement;
    private final BotControls hygiene;
    private final PillarAccess pillar;
    private final BlockReader blocks;
    private final SurfaceAccess surface;
    private final HazardPolicy hazards;
    private final MutationGate gate;
    private final CollectExecutor.ProposalSink proposals;
    private final NavigateExecutor.PositionSource positions;
    private final NavigateExecutor.EventSink events;
    private final ActionRuntime runtime;
    private final int timeoutTicks;

    private Phase phase = Phase.EVALUATE;
    private AscentMode mode = AscentMode.STAIR;
    private PendingMutation pendingMutation;
    private String proposalId;
    private int breakX;
    private int breakY;
    private int breakZ;
    private String breakBlock;
    private int targetX;
    private int targetFeetY;
    private int targetZ;
    private int originX;
    private int originFeetY;
    private int originZ;
    private int clearIndex;
    private int preferredDirection;
    private int landingStableTicks;
    private int centeringStableTicks;
    private int phaseStartedTick = -1;
    private int elapsedTicks;
    private int ascendSteps;
    private int breakSteps;
    private int pillarSteps;
    private int pillarCountBefore;
    private String pillarBlock;
    private String pillarFallbackReason;
    private ApproachController surfaceApproach;
    private boolean surfaceRouteAttempted;
    private boolean surfaceRouteUsed;
    private int surfaceRouteReplans;
    private int surfaceCandidateCount;
    private String surfaceRouteFailure;
    private boolean cancelPending;
    private final JsonArray brokenLedger = new JsonArray();
    private final JsonArray placedLedger = new JsonArray();

    public AscendExecutor(
        String bot,
        String actionId,
        ExactBlockBreaker blockBreaker,
        MovementControls movement,
        BotControls hygiene,
        PillarAccess pillar,
        BlockReader blocks,
        SurfaceAccess surface,
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
        this.blockBreaker = blockBreaker;
        this.movement = movement;
        this.hygiene = hygiene;
        this.pillar = pillar;
        this.blocks = blocks;
        this.surface = surface;
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
        if (runtime.cancelRequested(actionId)
            && phase != Phase.PILLAR_JUMPING
            && phase != Phase.PILLAR_SETTLING) {
            finish(serverTick, ActionRuntime.CLASS_CANCELED, "cancel_requested");
            return;
        }
        if (runtime.cancelRequested(actionId)) {
            cancelPending = true;
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
        if (!Double.isFinite(position.x())
            || !Double.isFinite(position.y())
            || !Double.isFinite(position.z())) {
            finish(serverTick, ActionRuntime.CLASS_FAILED, "body_position_invalid");
            return;
        }
        switch (phase) {
            case EVALUATE -> evaluate(serverTick, position);
            case SURFACE_ROUTING -> surfaceRoutingTick(serverTick, position);
            case AWAITING_VERDICT -> verdictTick(serverTick);
            case BREAKING -> breakingTick(serverTick);
            case MOVING -> movingTick(serverTick, position);
            case PILLAR_CENTERING -> pillarCenteringTick(serverTick, position);
            case PILLAR_JUMPING -> pillarJumpingTick(serverTick, position);
            case PILLAR_SETTLING -> pillarSettlingTick(serverTick, position);
        }
    }

    private void evaluate(int serverTick, NavigateExecutor.PositionSource.Position position) {
        int x = position.blockX();
        int feetY = position.feetBlockY();
        int z = position.blockZ();
        if (surface.isSurfaceStand(x, feetY, z)) {
            finishWithLedger(serverTick, ActionRuntime.CLASS_COMPLETED, "surface_reached", feetY);
            return;
        }
        if (!surfaceRouteAttempted) {
            surfaceRouteAttempted = true;
            Goal surfaceGoal = surface.findSurfaceGoal(position);
            if (surfaceGoal != null) {
                surfaceCandidateCount = surfaceGoal instanceof Goal.Composite composite
                    ? composite.goals().size()
                    : 1;
                surfaceApproach = new ApproachController(
                    bot,
                    actionId,
                    surfaceGoal,
                    surface.world(),
                    movement,
                    events,
                    5,
                    0.15
                );
                phase = Phase.SURFACE_ROUTING;
                return;
            }
            surfaceRouteFailure = "surface_target_unavailable";
        }
        if (ascendSteps >= MAX_ASCEND_STEPS || breakSteps >= MAX_BREAK_STEPS) {
            finishWithLedger(serverTick, ActionRuntime.CLASS_FAILED, "ascend_step_budget_exhausted", feetY);
            return;
        }

        mode = AscentMode.STAIR;
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
            beginPillar(
                serverTick,
                position,
                sawHazard ? "hazard_blocks_stair_route" : "stair_route_unavailable"
            );
            return;
        }
        clearIndex = 0;
        clearNext(serverTick);
    }

    private void surfaceRoutingTick(
        int serverTick,
        NavigateExecutor.PositionSource.Position position
    ) {
        ApproachController.Outcome outcome = surfaceApproach.tick(
            serverTick, position.x(), position.y(), position.z()
        );
        if (outcome.status() == ApproachController.Status.WORKING) {
            return;
        }
        surfaceRouteReplans += surfaceApproach.replans();
        surfaceApproach.halt();
        surfaceApproach = null;
        if (outcome.status() == ApproachController.Status.COMPLETED
            && surface.isSurfaceStand(position.blockX(), position.feetBlockY(), position.blockZ())) {
            surfaceRouteUsed = true;
            finishWithLedger(
                serverTick,
                ActionRuntime.CLASS_COMPLETED,
                "surface_reached",
                position.feetBlockY()
            );
            return;
        }
        surfaceRouteFailure = outcome.status() == ApproachController.Status.FAILED
            ? outcome.reason()
            : "surface_postcondition_failed";
        phase = Phase.EVALUATE;
    }

    private void clearNext(int serverTick) {
        int clearLimit = mode == AscentMode.PILLAR ? 2 : 3;
        while (clearIndex < clearLimit) {
            if (mode == AscentMode.PILLAR) {
                breakX = originX;
                breakY = originFeetY + 1 + clearIndex;
                breakZ = originZ;
            } else if (clearIndex == 0) {
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
                bot, actionId, "break", breakX, breakY, breakZ, breakBlock, "recovery", serverTick
            );
            proposalId = proposal.proposalId();
            pendingMutation = PendingMutation.BREAK;
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
        if (mode == AscentMode.PILLAR) {
            beginPillarCentering(serverTick);
        } else {
            beginMove(serverTick);
        }
    }

    private void verdictTick(int serverTick) {
        MutationGate.State state = gate.poll(proposalId, serverTick);
        switch (state) {
            case PENDING -> {
            }
            case ALLOWED -> {
                gate.discard(proposalId);
                proposalId = null;
                if (pendingMutation == PendingMutation.PILLAR_PLACE) {
                    String invalid = pillarPrecondition();
                    if (invalid != null) {
                        finish(serverTick, ActionRuntime.CLASS_FAILED, invalid + "_after_allow");
                        return;
                    }
                    startPillarJump(serverTick);
                    return;
                }
                movement.lookAt(bot, breakX + 0.5, breakY + 0.5, breakZ + 0.5);
                ExactBlockBreaker.Outcome outcome = blockBreaker.begin(
                    bot, breakX, breakY, breakZ, breakBlock, serverTick
                );
                if (outcome.state() == ExactBlockBreaker.State.FAILED) {
                    breakFailed(serverTick, "exact_break_failed:" + outcome.reason());
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
                proposalId = null;
                if (pendingMutation == PendingMutation.BREAK && mode == AscentMode.STAIR) {
                    fallbackFromStair(serverTick, reason);
                } else {
                    finish(
                        serverTick,
                        pendingMutation == PendingMutation.PILLAR_PLACE
                            ? ActionRuntime.CLASS_UNSAFE
                            : ActionRuntime.CLASS_FAILED,
                        reason
                    );
                }
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
            breakFailed(serverTick, "exact_break_failed:" + outcome.reason());
            return;
        }
        if (serverTick - phaseStartedTick > BREAK_TIMEOUT_TICKS) {
            breakFailed(serverTick, "ascent_break_timeout");
        }
    }

    private void breakFailed(int serverTick, String reason) {
        if (mode == AscentMode.STAIR) {
            fallbackFromStair(serverTick, reason);
        } else {
            finish(serverTick, ActionRuntime.CLASS_FAILED, reason);
        }
    }

    private void fallbackFromStair(int serverTick, String reason) {
        blockBreaker.abort(bot);
        hygiene.clearAll(bot);
        NavigateExecutor.PositionSource.Position position = positions.position(bot);
        if (position == null) {
            finish(serverTick, ActionRuntime.CLASS_FAILED, "body_missing");
            return;
        }
        beginPillar(serverTick, position, reason);
    }

    private void beginPillar(
        int serverTick,
        NavigateExecutor.PositionSource.Position position,
        String fallbackReason
    ) {
        int x = position.blockX();
        int feetY = position.feetBlockY();
        int z = position.blockZ();
        String support = blocks.blockIdAt(x, feetY - 1, z);
        String feet = blocks.blockIdAt(x, feetY, z);
        String head = blocks.blockIdAt(x, feetY + 1, z);
        String cap = blocks.blockIdAt(x, feetY + 2, z);
        if (support == null || feet == null || head == null || cap == null) {
            finish(serverTick, ActionRuntime.CLASS_FAILED, "pillar_column_unloaded");
            return;
        }
        if (hazards.isHazard(support) || hazards.isHazard(feet)
            || hazards.isHazard(head) || hazards.isHazard(cap)) {
            finish(serverTick, ActionRuntime.CLASS_UNSAFE, "hazard_in_pillar_column");
            return;
        }
        if (isPassable(support)) {
            finish(serverTick, ActionRuntime.CLASS_FAILED, "pillar_support_missing");
            return;
        }
        if (!isPassable(feet)) {
            finish(serverTick, ActionRuntime.CLASS_FAILED, "pillar_target_occupied");
            return;
        }
        ScaffoldSelection selection = pillar.selectScaffold(bot, PILLAR_BLOCKS);
        if (!selection.success() || selection.blockId() == null || selection.count() <= 0) {
            pillarFallbackReason = fallbackReason;
            finish(
                serverTick,
                fallbackReason.startsWith("hazard_")
                    ? ActionRuntime.CLASS_UNSAFE
                    : ActionRuntime.CLASS_FAILED,
                selection.reason() == null ? "pillar_no_scaffold_available" : selection.reason()
            );
            return;
        }
        mode = AscentMode.PILLAR;
        originX = x;
        originFeetY = feetY;
        originZ = z;
        pillarBlock = selection.blockId();
        pillarFallbackReason = fallbackReason;
        clearIndex = 0;
        clearNext(serverTick);
    }

    private void beginPillarCentering(int serverTick) {
        hygiene.clearAll(bot);
        phaseStartedTick = serverTick;
        centeringStableTicks = 0;
        phase = Phase.PILLAR_CENTERING;
    }

    private void pillarCenteringTick(
        int serverTick,
        NavigateExecutor.PositionSource.Position position
    ) {
        if (position.blockX() != originX || position.blockZ() != originZ
            || Math.abs(position.y() - originFeetY) > 0.10) {
            finish(serverTick, ActionRuntime.CLASS_FAILED, "pillar_origin_changed");
            return;
        }
        double dx = position.x() - (originX + 0.5);
        double dz = position.z() - (originZ + 0.5);
        boolean centered = Math.hypot(dx, dz) <= CENTER_TOLERANCE;
        centeringStableTicks = centered ? centeringStableTicks + 1 : 0;
        if (centeringStableTicks >= CENTER_STABLE_TICKS) {
            hygiene.clearAll(bot);
            proposePillarPlacement(serverTick);
            return;
        }
        if (serverTick - phaseStartedTick > STEP_TIMEOUT_TICKS) {
            finish(serverTick, ActionRuntime.CLASS_FAILED, "pillar_centering_timeout");
            return;
        }
        if (!centered) {
            movement.lookAt(bot, originX + 0.5, originFeetY + 0.1, originZ + 0.5);
            movement.moveForward(bot);
        }
    }

    private void proposePillarPlacement(int serverTick) {
        String invalid = pillarPrecondition();
        if (invalid != null) {
            finish(serverTick, ActionRuntime.CLASS_FAILED, invalid);
            return;
        }
        pillarCountBefore = pillar.selectedItemCount();
        MutationGate.Proposal proposal = gate.propose(
            bot,
            actionId,
            "place",
            originX,
            originFeetY,
            originZ,
            pillarBlock,
            "recovery",
            serverTick
        );
        proposalId = proposal.proposalId();
        pendingMutation = PendingMutation.PILLAR_PLACE;
        proposals.send(proposal);
        JsonObject data = new JsonObject();
        data.addProperty("proposal_id", proposalId);
        data.addProperty("block_id", pillarBlock);
        data.addProperty("x", originX);
        data.addProperty("y", originFeetY);
        data.addProperty("z", originZ);
        events.emit(bot, serverTick, "ascent_pillar_proposed", actionId, data);
        phase = Phase.AWAITING_VERDICT;
    }

    private String pillarPrecondition() {
        String support = blocks.blockIdAt(originX, originFeetY - 1, originZ);
        String target = blocks.blockIdAt(originX, originFeetY, originZ);
        String head = blocks.blockIdAt(originX, originFeetY + 1, originZ);
        String cap = blocks.blockIdAt(originX, originFeetY + 2, originZ);
        if (support == null || target == null || head == null || cap == null) {
            return "pillar_column_unloaded";
        }
        if (hazards.isHazard(support) || hazards.isHazard(target)
            || hazards.isHazard(head) || hazards.isHazard(cap)) {
            return "hazard_in_pillar_column";
        }
        if (isPassable(support)) {
            return "pillar_support_missing";
        }
        if (!isPassable(target)) {
            return "pillar_target_occupied";
        }
        if (!isPassable(head) || !isPassable(cap)) {
            return "pillar_headroom_blocked";
        }
        if (!pillarBlock.equals(pillar.selectedItemId()) || pillar.selectedItemCount() <= 0) {
            return "pillar_selected_item_mismatch";
        }
        return null;
    }

    private void startPillarJump(int serverTick) {
        hygiene.clearAll(bot);
        phaseStartedTick = serverTick;
        landingStableTicks = 0;
        pillar.sneak(bot);
        movement.lookAt(bot, originX + 0.5, originFeetY - 0.2, originZ + 0.5);
        movement.jumpOnce(bot);
        phase = Phase.PILLAR_JUMPING;
    }

    private void pillarJumpingTick(
        int serverTick,
        NavigateExecutor.PositionSource.Position position
    ) {
        String target = blocks.blockIdAt(originX, originFeetY, originZ);
        if (target == null) {
            finish(serverTick, ActionRuntime.CLASS_FAILED, "pillar_target_unloaded_during_place");
            return;
        }
        if (pillarBlock.equals(target)) {
            if (pillar.selectedItemCount() >= pillarCountBefore) {
                finish(serverTick, ActionRuntime.CLASS_FAILED, "pillar_inventory_delta_missing");
                return;
            }
            hygiene.clearAll(bot);
            phaseStartedTick = serverTick;
            landingStableTicks = 0;
            phase = Phase.PILLAR_SETTLING;
            return;
        }
        if (!isPassable(target)) {
            finish(serverTick, ActionRuntime.CLASS_FAILED, "pillar_target_changed_during_place");
            return;
        }
        if (cancelPending && position.y() <= originFeetY + 0.05) {
            finish(serverTick, ActionRuntime.CLASS_CANCELED, "cancel_requested");
            return;
        }
        int phaseTicks = serverTick - phaseStartedTick;
        if (phaseTicks > PILLAR_TIMEOUT_TICKS) {
            finish(serverTick, ActionRuntime.CLASS_FAILED, "pillar_timeout");
            return;
        }
        if (position.y() > originFeetY + PILLAR_JUMP_MIN_GAIN) {
            movement.lookAt(bot, originX + 0.5, originFeetY - 0.2, originZ + 0.5);
            if (phaseTicks % 2 == 0) {
                pillar.useOnce(bot);
            }
        } else if (phaseTicks % 10 == 0) {
            movement.jumpOnce(bot);
        }
    }

    private void pillarSettlingTick(
        int serverTick,
        NavigateExecutor.PositionSource.Position position
    ) {
        if (!pillarBlock.equals(blocks.blockIdAt(originX, originFeetY, originZ))) {
            finish(serverTick, ActionRuntime.CLASS_FAILED, "pillar_block_lost_while_settling");
            return;
        }
        boolean standingOnPillar = position.blockX() == originX
            && position.blockZ() == originZ
            && Math.abs(position.y() - (originFeetY + 1.0)) <= 0.08;
        landingStableTicks = standingOnPillar ? landingStableTicks + 1 : 0;
        if (landingStableTicks >= LANDING_STABLE_TICKS) {
            pillarSteps++;
            ascendSteps++;
            JsonObject placed = new JsonObject();
            placed.addProperty("x", originX);
            placed.addProperty("y", originFeetY);
            placed.addProperty("z", originZ);
            placed.addProperty("block_id", pillarBlock);
            placed.addProperty("item_count_before", pillarCountBefore);
            placed.addProperty("item_count_after", pillar.selectedItemCount());
            placedLedger.add(placed);
            events.emit(bot, serverTick, "ascent_pillar_verified", actionId, placed.deepCopy());
            if (cancelPending) {
                finish(serverTick, ActionRuntime.CLASS_CANCELED, "cancel_requested");
            } else {
                phase = Phase.EVALUATE;
            }
            return;
        }
        if (serverTick - phaseStartedTick > STEP_TIMEOUT_TICKS) {
            finish(serverTick, ActionRuntime.CLASS_FAILED, "pillar_settle_timeout");
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
        Integer finalY = position == null ? null : position.feetBlockY();
        finishWithLedger(serverTick, classification, reason, finalY);
    }

    private void finishWithLedger(int serverTick, String classification, String reason, Integer finalY) {
        if (surfaceApproach != null) {
            surfaceApproach.halt();
            surfaceApproach = null;
        }
        if (proposalId != null) {
            gate.discard(proposalId);
            proposalId = null;
        }
        blockBreaker.abort(bot);
        hygiene.clearAll(bot);
        JsonObject facts = new JsonObject();
        facts.addProperty("reason", reason);
        if (finalY != null) {
            facts.addProperty("final_y", finalY);
        }
        facts.addProperty("ascend_steps", ascendSteps);
        facts.addProperty("break_steps", breakSteps);
        facts.addProperty("pillar_steps", pillarSteps);
        facts.addProperty("elapsed_ticks", elapsedTicks);
        facts.addProperty("surface_route_attempted", surfaceRouteAttempted);
        facts.addProperty("surface_route_used", surfaceRouteUsed);
        facts.addProperty("surface_route_replans", surfaceRouteReplans);
        facts.addProperty("surface_candidate_count", surfaceCandidateCount);
        if (surfaceRouteFailure != null) {
            facts.addProperty("surface_route_failure", surfaceRouteFailure);
        }
        if (pillarFallbackReason != null) {
            facts.addProperty("pillar_fallback_from", pillarFallbackReason);
        }
        facts.add("broken", brokenLedger.deepCopy());
        facts.add("placed", placedLedger.deepCopy());
        runtime.finish(bot, actionId, classification, facts, serverTick);
    }
}
