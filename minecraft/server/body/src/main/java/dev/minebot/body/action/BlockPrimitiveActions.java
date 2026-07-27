package dev.minebot.body.action;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import dev.minebot.body.nav.MovementControls;

/** Governed exact block work and observed one-shot jumping for Python transactions. */
public final class BlockPrimitiveActions {
    private static final double JUMP_MIN_GAIN = 0.05;

    private BlockPrimitiveActions() {}

    public interface WorldAccess {
        boolean present();
        String blockIdAt(int x, int y, int z);
        boolean canReplaceAt(int x, int y, int z, boolean replaceLiquid);
        boolean playerIntersects(int x, int y, int z);
        String selectedItemId();
        int selectedItemCount();
        PlayerPrimitiveActions.Position position();
    }

    public interface ProposalSink {
        void send(MutationGate.Proposal proposal);
    }

    private enum MutationPhase {
        PREPARE,
        AWAITING_VERDICT,
        EXECUTING
    }

    public static final class MineExecutor implements ActionRuntime.TickExecutor {
        private final String bot;
        private final String actionId;
        private final int x;
        private final int y;
        private final int z;
        private final String expectedBlockId;
        private final String context;
        private final int timeoutTicks;
        private final WorldAccess world;
        private final MovementControls controls;
        private final ExactBlockBreaker breaker;
        private final MutationGate gate;
        private final ProposalSink proposals;
        private final ActionRuntime runtime;

        private MutationPhase phase = MutationPhase.PREPARE;
        private String proposalId;
        private int startedTick = -1;

        public MineExecutor(
            String bot,
            String actionId,
            int x,
            int y,
            int z,
            String expectedBlockId,
            String context,
            int timeoutTicks,
            WorldAccess world,
            MovementControls controls,
            ExactBlockBreaker breaker,
            MutationGate gate,
            ProposalSink proposals,
            ActionRuntime runtime
        ) {
            this.bot = bot;
            this.actionId = actionId;
            this.x = x;
            this.y = y;
            this.z = z;
            this.expectedBlockId = expectedBlockId;
            this.context = context;
            this.timeoutTicks = Math.max(1, timeoutTicks);
            this.world = world;
            this.controls = controls;
            this.breaker = breaker;
            this.gate = gate;
            this.proposals = proposals;
            this.runtime = runtime;
        }

        @Override
        public void tick(int serverTick) {
            if (startedTick < 0) {
                startedTick = serverTick;
            }
            if (runtime.cancelRequested(actionId)) {
                finish(false, "canceled", ActionRuntime.CLASS_CANCELED, serverTick);
                return;
            }
            if (!world.present()) {
                finish(false, "body_missing", ActionRuntime.CLASS_FAILED, serverTick);
                return;
            }
            if (serverTick - startedTick > timeoutTicks) {
                finish(false, "mine_timeout", ActionRuntime.CLASS_TIMEOUT, serverTick);
                return;
            }
            switch (phase) {
                case PREPARE -> propose(serverTick);
                case AWAITING_VERDICT -> pollVerdict(serverTick);
                case EXECUTING -> mine(serverTick);
            }
        }

        private void propose(int serverTick) {
            String observed = world.blockIdAt(x, y, z);
            if (observed == null) {
                finish(false, "target_unloaded", ActionRuntime.CLASS_FAILED, serverTick);
                return;
            }
            if (!expectedBlockId.equals(observed)) {
                finish(false, "target_changed", ActionRuntime.CLASS_FAILED, serverTick);
                return;
            }
            MutationGate.Proposal proposal = gate.propose(
                bot, actionId, "break", x, y, z, expectedBlockId, context, serverTick
            );
            proposalId = proposal.proposalId();
            proposals.send(proposal);
            phase = MutationPhase.AWAITING_VERDICT;
        }

        private void pollVerdict(int serverTick) {
            MutationGate.State state = gate.poll(proposalId, serverTick);
            if (state == MutationGate.State.PENDING) {
                return;
            }
            String verdictReason = gate.reason(proposalId);
            gate.discard(proposalId);
            proposalId = null;
            if (state != MutationGate.State.ALLOWED) {
                String reason = state == MutationGate.State.TIMED_OUT
                    ? "governance_verdict_timeout"
                    : "governance_denied:" + verdictReason;
                finish(false, reason, ActionRuntime.CLASS_UNSAFE, serverTick);
                return;
            }
            String observed = world.blockIdAt(x, y, z);
            if (!expectedBlockId.equals(observed)) {
                finish(false, "target_changed_after_allow", ActionRuntime.CLASS_FAILED, serverTick);
                return;
            }
            controls.lookAt(bot, x + 0.5, y + 0.5, z + 0.5);
            ExactBlockBreaker.Outcome outcome = breaker.begin(bot, x, y, z, expectedBlockId, serverTick);
            if (outcome.state() == ExactBlockBreaker.State.FAILED) {
                finish(false, "exact_break_failed:" + outcome.reason(), ActionRuntime.CLASS_FAILED, serverTick);
                return;
            }
            phase = MutationPhase.EXECUTING;
            verifyOrContinue(outcome, serverTick);
        }

        private void mine(int serverTick) {
            String observed = world.blockIdAt(x, y, z);
            if (observed == null) {
                finish(false, "target_unloaded_during_mine", ActionRuntime.CLASS_FAILED, serverTick);
                return;
            }
            if (!expectedBlockId.equals(observed)) {
                finish(true, "completed", ActionRuntime.CLASS_COMPLETED, serverTick);
                return;
            }
            verifyOrContinue(breaker.tick(bot, serverTick), serverTick);
        }

        private void verifyOrContinue(ExactBlockBreaker.Outcome outcome, int serverTick) {
            if (outcome.state() == ExactBlockBreaker.State.FAILED) {
                finish(false, "exact_break_failed:" + outcome.reason(), ActionRuntime.CLASS_FAILED, serverTick);
                return;
            }
            if (outcome.state() == ExactBlockBreaker.State.COMPLETE) {
                String observed = world.blockIdAt(x, y, z);
                if (observed != null && !expectedBlockId.equals(observed)) {
                    finish(true, "completed", ActionRuntime.CLASS_COMPLETED, serverTick);
                } else {
                    finish(false, "break_verification_failed", ActionRuntime.CLASS_FAILED, serverTick);
                }
            }
        }

        private void finish(boolean success, String reason, String classification, int serverTick) {
            breaker.abort(bot);
            if (proposalId != null) {
                gate.discard(proposalId);
                proposalId = null;
            }
            JsonObject facts = mutationFacts(success, reason, x, y, z, expectedBlockId, world.position());
            facts.addProperty("block_after", world.blockIdAt(x, y, z));
            facts.addProperty("elapsed_ticks", Math.max(0, serverTick - startedTick));
            runtime.finish(bot, actionId, classification, facts, serverTick);
        }
    }

    public static final class PlaceExecutor implements ActionRuntime.TickExecutor {
        private final String bot;
        private final String actionId;
        private final int x;
        private final int y;
        private final int z;
        private final String blockId;
        private final String face;
        private final String context;
        private final boolean replaceLiquid;
        private final int timeoutTicks;
        private final WorldAccess world;
        private final PlayerPrimitiveActions.Controls controls;
        private final MutationGate gate;
        private final ProposalSink proposals;
        private final ActionRuntime runtime;

        private MutationPhase phase = MutationPhase.PREPARE;
        private String proposalId;
        private int startedTick = -1;
        private int itemCountBefore;

        public PlaceExecutor(
            String bot,
            String actionId,
            int x,
            int y,
            int z,
            String blockId,
            String face,
            String context,
            boolean replaceLiquid,
            int timeoutTicks,
            WorldAccess world,
            PlayerPrimitiveActions.Controls controls,
            MutationGate gate,
            ProposalSink proposals,
            ActionRuntime runtime
        ) {
            this.bot = bot;
            this.actionId = actionId;
            this.x = x;
            this.y = y;
            this.z = z;
            this.blockId = blockId;
            this.face = face;
            this.context = context;
            this.replaceLiquid = replaceLiquid;
            this.timeoutTicks = Math.max(1, timeoutTicks);
            this.world = world;
            this.controls = controls;
            this.gate = gate;
            this.proposals = proposals;
            this.runtime = runtime;
        }

        @Override
        public void tick(int serverTick) {
            if (startedTick < 0) {
                startedTick = serverTick;
            }
            if (runtime.cancelRequested(actionId)) {
                finish(false, "canceled", ActionRuntime.CLASS_CANCELED, serverTick);
                return;
            }
            if (!world.present()) {
                finish(false, "body_missing", ActionRuntime.CLASS_FAILED, serverTick);
                return;
            }
            if (serverTick - startedTick > timeoutTicks) {
                finish(false, "place_timeout", ActionRuntime.CLASS_TIMEOUT, serverTick);
                return;
            }
            switch (phase) {
                case PREPARE -> propose(serverTick);
                case AWAITING_VERDICT -> pollVerdict(serverTick);
                case EXECUTING -> verify(serverTick);
            }
        }

        private void propose(int serverTick) {
            String invalid = placementPrecondition();
            if (invalid != null) {
                finish(false, invalid, ActionRuntime.CLASS_FAILED, serverTick);
                return;
            }
            MutationGate.Proposal proposal = gate.propose(
                bot, actionId, "place", x, y, z, blockId, context, serverTick
            );
            proposalId = proposal.proposalId();
            proposals.send(proposal);
            phase = MutationPhase.AWAITING_VERDICT;
        }

        private void pollVerdict(int serverTick) {
            MutationGate.State state = gate.poll(proposalId, serverTick);
            if (state == MutationGate.State.PENDING) {
                return;
            }
            String verdictReason = gate.reason(proposalId);
            gate.discard(proposalId);
            proposalId = null;
            if (state != MutationGate.State.ALLOWED) {
                String reason = state == MutationGate.State.TIMED_OUT
                    ? "governance_verdict_timeout"
                    : "governance_denied:" + verdictReason;
                finish(false, reason, ActionRuntime.CLASS_UNSAFE, serverTick);
                return;
            }
            String invalid = placementPrecondition();
            if (invalid != null) {
                finish(false, invalid + "_after_allow", ActionRuntime.CLASS_FAILED, serverTick);
                return;
            }
            itemCountBefore = world.selectedItemCount();
            PlayerPrimitiveActions.Position aim = placementAim(x, y, z, face);
            controls.lookAt(bot, aim.x(), aim.y(), aim.z());
            controls.useOnce(bot);
            phase = MutationPhase.EXECUTING;
        }

        private void verify(int serverTick) {
            String observed = world.blockIdAt(x, y, z);
            int countAfter = world.selectedItemCount();
            if (blockId.equals(observed)) {
                if (countAfter >= itemCountBefore) {
                    finish(false, "placement_inventory_delta_missing", ActionRuntime.CLASS_FAILED, serverTick);
                } else {
                    finish(true, "completed", ActionRuntime.CLASS_COMPLETED, serverTick);
                }
                return;
            }
            if (!world.canReplaceAt(x, y, z, replaceLiquid)) {
                finish(false, "target_changed_during_place", ActionRuntime.CLASS_FAILED, serverTick);
            }
        }

        private String placementPrecondition() {
            if (!world.canReplaceAt(x, y, z, replaceLiquid)) {
                return "target_occupied";
            }
            if (world.playerIntersects(x, y, z)) {
                return "body_collision";
            }
            if (!blockId.equals(world.selectedItemId()) || world.selectedItemCount() <= 0) {
                return "selected_item_mismatch";
            }
            return null;
        }

        private void finish(boolean success, String reason, String classification, int serverTick) {
            if (proposalId != null) {
                gate.discard(proposalId);
                proposalId = null;
            }
            JsonObject facts = mutationFacts(success, reason, x, y, z, blockId, world.position());
            facts.addProperty("face", face);
            facts.addProperty("block_after", world.blockIdAt(x, y, z));
            facts.addProperty("item_count_before", itemCountBefore);
            facts.addProperty("item_count_after", world.selectedItemCount());
            facts.addProperty("elapsed_ticks", Math.max(0, serverTick - startedTick));
            runtime.finish(bot, actionId, classification, facts, serverTick);
        }
    }

    public static final class JumpExecutor implements ActionRuntime.TickExecutor {
        private final String bot;
        private final String actionId;
        private final int timeoutTicks;
        private final WorldAccess world;
        private final MovementControls controls;
        private final ActionRuntime runtime;
        private int startedTick = -1;
        private PlayerPrimitiveActions.Position before;

        public JumpExecutor(
            String bot,
            String actionId,
            int timeoutTicks,
            WorldAccess world,
            MovementControls controls,
            ActionRuntime runtime
        ) {
            this.bot = bot;
            this.actionId = actionId;
            this.timeoutTicks = Math.max(1, timeoutTicks);
            this.world = world;
            this.controls = controls;
            this.runtime = runtime;
        }

        @Override
        public void tick(int serverTick) {
            if (runtime.cancelRequested(actionId)) {
                finish(false, "canceled", ActionRuntime.CLASS_CANCELED, serverTick);
                return;
            }
            if (!world.present()) {
                finish(false, "body_missing", ActionRuntime.CLASS_FAILED, serverTick);
                return;
            }
            if (startedTick < 0) {
                startedTick = serverTick;
                before = world.position();
                controls.jumpOnce(bot);
                return;
            }
            PlayerPrimitiveActions.Position after = world.position();
            if (after.y() - before.y() > JUMP_MIN_GAIN) {
                finish(true, "completed", ActionRuntime.CLASS_COMPLETED, serverTick);
                return;
            }
            if (serverTick - startedTick > timeoutTicks) {
                finish(false, "jump_no_height_gain", ActionRuntime.CLASS_FAILED, serverTick);
            }
        }

        private void finish(boolean success, String reason, String classification, int serverTick) {
            PlayerPrimitiveActions.Position after = world.position();
            JsonObject facts = baseFacts(success, reason);
            facts.add("position_before", positionJson(before));
            facts.add("position_after", positionJson(after));
            facts.addProperty("gained_y", before == null ? 0.0 : after.y() - before.y());
            facts.addProperty("elapsed_ticks", startedTick < 0 ? 0 : serverTick - startedTick);
            runtime.finish(bot, actionId, classification, facts, serverTick);
        }
    }

    private static PlayerPrimitiveActions.Position placementAim(int x, int y, int z, String face) {
        return switch (face) {
            case "down" -> new PlayerPrimitiveActions.Position(x + 0.5, y + 1.2, z + 0.5);
            case "north" -> new PlayerPrimitiveActions.Position(x + 0.5, y + 0.5, z + 1.2);
            case "south" -> new PlayerPrimitiveActions.Position(x + 0.5, y + 0.5, z - 0.2);
            case "west" -> new PlayerPrimitiveActions.Position(x + 1.2, y + 0.5, z + 0.5);
            case "east" -> new PlayerPrimitiveActions.Position(x - 0.2, y + 0.5, z + 0.5);
            default -> new PlayerPrimitiveActions.Position(x + 0.5, y - 0.2, z + 0.5);
        };
    }

    private static JsonObject mutationFacts(
        boolean success,
        String reason,
        int x,
        int y,
        int z,
        String blockId,
        PlayerPrimitiveActions.Position position
    ) {
        JsonObject facts = baseFacts(success, reason);
        JsonArray target = new JsonArray();
        target.add(x);
        target.add(y);
        target.add(z);
        facts.add("target", target);
        facts.addProperty("block_type", blockId);
        facts.add("final_pos", positionJson(position));
        return facts;
    }

    private static JsonObject baseFacts(boolean success, String reason) {
        JsonObject facts = new JsonObject();
        facts.addProperty("success", success);
        facts.addProperty("reason", reason);
        facts.addProperty("stopped_reason", reason);
        return facts;
    }

    private static JsonArray positionJson(PlayerPrimitiveActions.Position position) {
        JsonArray value = new JsonArray();
        if (position == null) {
            return value;
        }
        value.add(position.x());
        value.add(position.y());
        value.add(position.z());
        return value;
    }
}
