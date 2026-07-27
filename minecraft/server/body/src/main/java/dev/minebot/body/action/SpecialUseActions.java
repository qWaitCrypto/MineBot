package dev.minebot.body.action;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;

/** Governed fire/crop use with physical-first execution and observed terminal truth. */
public final class SpecialUseActions {
    private static final double MAX_INTERACTION_DISTANCE = 4.5;
    public enum Mode {
        IGNITE,
        SOW
    }

    public interface WorldAccess {
        boolean present();
        String blockIdAt(int x, int y, int z);
        String selectedItemId();
        int selectedItemCount();
        PlayerPrimitiveActions.Position position();
        boolean substituteFire(int x, int y, int z);
        boolean substituteCrop(int x, int y, int z, String cropBlockId, String seedItemId);
    }

    public interface ProposalSink {
        void send(MutationGate.Proposal proposal);
    }

    private enum Phase {
        PREPARE,
        AWAITING_VERDICT,
        PHYSICAL
    }

    public static final class Executor implements ActionRuntime.TickExecutor {
        private final String bot;
        private final String actionId;
        private final Mode mode;
        private final int x;
        private final int y;
        private final int z;
        private final String expectedBlockId;
        private final String itemId;
        private final String context;
        private final boolean allowSubstitute;
        private final int timeoutTicks;
        private final WorldAccess world;
        private final PlayerPrimitiveActions.Controls controls;
        private final MutationGate gate;
        private final ProposalSink proposals;
        private final ActionRuntime runtime;

        private Phase phase = Phase.PREPARE;
        private String proposalId;
        private int startedTick = -1;
        private int physicalStartedTick = -1;
        private int itemCountBefore;
        private String blockBefore;
        private String method = "physical";
        private boolean substituteAttempted;

        public Executor(
            String bot,
            String actionId,
            Mode mode,
            int x,
            int y,
            int z,
            String expectedBlockId,
            String itemId,
            String context,
            boolean allowSubstitute,
            int timeoutTicks,
            WorldAccess world,
            PlayerPrimitiveActions.Controls controls,
            MutationGate gate,
            ProposalSink proposals,
            ActionRuntime runtime
        ) {
            this.bot = bot;
            this.actionId = actionId;
            this.mode = mode;
            this.x = x;
            this.y = y;
            this.z = z;
            this.expectedBlockId = expectedBlockId;
            this.itemId = itemId;
            this.context = context;
            this.allowSubstitute = allowSubstitute;
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
                finish(false, actionName() + "_timeout", ActionRuntime.CLASS_TIMEOUT, serverTick);
                return;
            }
            switch (phase) {
                case PREPARE -> propose(serverTick);
                case AWAITING_VERDICT -> pollVerdict(serverTick);
                case PHYSICAL -> verifyOrSubstitute(serverTick);
            }
        }

        private void propose(int serverTick) {
            String invalid = precondition();
            if (invalid != null) {
                finish(false, invalid, ActionRuntime.CLASS_FAILED, serverTick);
                return;
            }
            blockBefore = observedBlock();
            if (expectedBlockId.equals(blockBefore)) {
                finish(true, mode == Mode.IGNITE ? "already_lit" : "already_sown",
                    ActionRuntime.CLASS_COMPLETED, serverTick);
                return;
            }
            MutationGate.Proposal proposal = gate.propose(
                bot, actionId, "interact", x, y, z, world.blockIdAt(x, y, z), context, serverTick
            );
            proposalId = proposal.proposalId();
            proposals.send(proposal);
            phase = Phase.AWAITING_VERDICT;
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
            String invalid = precondition();
            if (invalid != null) {
                finish(false, invalid + "_after_allow", ActionRuntime.CLASS_FAILED, serverTick);
                return;
            }
            if (!blockBefore.equals(observedBlock())) {
                finish(false, "target_changed_after_allow", ActionRuntime.CLASS_FAILED, serverTick);
                return;
            }
            itemCountBefore = world.selectedItemCount();
            controls.lookAt(bot, x + 0.5, y - 0.2, z + 0.5);
            controls.useOnce(bot);
            physicalStartedTick = serverTick;
            phase = Phase.PHYSICAL;
        }

        private void verifyOrSubstitute(int serverTick) {
            String observed = observedBlock();
            int itemCountAfter = world.selectedItemCount();
            if (expectedBlockId.equals(observed)) {
                if (mode == Mode.SOW && itemCountAfter != itemCountBefore - 1) {
                    finish(false, "sow_inventory_delta_missing", ActionRuntime.CLASS_FAILED, serverTick);
                } else {
                    finish(true, "completed", ActionRuntime.CLASS_COMPLETED, serverTick);
                }
                return;
            }
            if (mode == Mode.SOW && itemCountAfter < itemCountBefore) {
                finish(false, "seed_consumed_without_crop", ActionRuntime.CLASS_FAILED, serverTick);
                return;
            }
            if (allowSubstitute && !substituteAttempted && serverTick - physicalStartedTick >= 2) {
                String invalid = precondition();
                if (invalid != null) {
                    finish(false, invalid + "_before_substitute", ActionRuntime.CLASS_FAILED, serverTick);
                    return;
                }
                if (!blockBefore.equals(observedBlock())) {
                    finish(false, "target_changed_before_substitute", ActionRuntime.CLASS_FAILED, serverTick);
                    return;
                }
                substituteAttempted = true;
                method = "substitute";
                boolean changed = mode == Mode.IGNITE
                    ? world.substituteFire(x, y, z)
                    : world.substituteCrop(x, y, z, expectedBlockId, itemId);
                if (!changed) {
                    finish(false, actionName() + "_substitute_rejected", ActionRuntime.CLASS_FAILED, serverTick);
                    return;
                }
                verifyOrSubstitute(serverTick);
            }
        }

        private String precondition() {
            String target = world.blockIdAt(x, y, z);
            if (target == null || observedBlock() == null) {
                return "target_unloaded";
            }
            PlayerPrimitiveActions.Position player = world.position();
            double dx = x + 0.5 - player.x();
            double dy = y + 0.5 - player.y();
            double dz = z + 0.5 - player.z();
            if (Math.sqrt(dx * dx + dy * dy + dz * dz) > MAX_INTERACTION_DISTANCE) {
                return "target_out_of_range";
            }
            if (!itemId.equals(world.selectedItemId()) || world.selectedItemCount() <= 0) {
                return "selected_item_mismatch";
            }
            if (mode == Mode.SOW && !"minecraft:farmland".equals(target)) {
                return "sow_target_not_farmland";
            }
            return null;
        }

        private String observedBlock() {
            return world.blockIdAt(x, mode == Mode.SOW ? y + 1 : y, z);
        }

        private String actionName() {
            return mode == Mode.IGNITE ? "ignite" : "sow";
        }

        private void finish(boolean success, String reason, String classification, int serverTick) {
            if (proposalId != null) {
                gate.discard(proposalId);
                proposalId = null;
            }
            JsonObject facts = new JsonObject();
            facts.addProperty("success", success);
            facts.addProperty("reason", reason);
            facts.add("target", positionJson(x, y, z));
            if (mode == Mode.SOW) {
                facts.add("crop_pos", positionJson(x, y + 1, z));
            }
            facts.addProperty("expected_type", expectedBlockId);
            facts.addProperty("block_after", observedBlock());
            facts.addProperty("item", itemId);
            facts.addProperty("method", success ? method : "failed");
            facts.addProperty("item_count_before", itemCountBefore);
            facts.addProperty("item_count_after", world.selectedItemCount());
            facts.add("final_pos", positionJson(world.position()));
            facts.addProperty("elapsed_ticks", Math.max(0, serverTick - startedTick));
            facts.addProperty("block_before", blockBefore);
            runtime.finish(bot, actionId, classification, facts, serverTick);
        }
    }

    private static JsonArray positionJson(int x, int y, int z) {
        JsonArray position = new JsonArray();
        position.add(x);
        position.add(y);
        position.add(z);
        return position;
    }

    private static JsonArray positionJson(PlayerPrimitiveActions.Position source) {
        JsonArray position = new JsonArray();
        if (source != null) {
            position.add(source.x());
            position.add(source.y());
            position.add(source.z());
        }
        return position;
    }

    private SpecialUseActions() {}
}
