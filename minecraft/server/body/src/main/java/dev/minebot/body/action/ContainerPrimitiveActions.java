package dev.minebot.body.action;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import dev.minebot.body.event.BotEventStream;

/** Governed, component-preserving movement between one block container and the player inventory. */
public final class ContainerPrimitiveActions {
    private ContainerPrimitiveActions() {}

    public interface InventoryAccess {
        int size();
        boolean slotEmpty(int slot);
        String itemIdAt(int slot);
        int itemCountAt(int slot);
        boolean sameStack(int slot, InventoryAccess other, int otherSlot);
        int maxStackSizeAt(int slot);
        int destinationMaxStackSize(int slot, InventoryAccess source, int sourceSlot);
        boolean canMoveTo(int sourceSlot, InventoryAccess destination, int destinationSlot);
        void moveItemsTo(int sourceSlot, InventoryAccess destination, int destinationSlot, int count);
    }

    public record Target(String blockId, InventoryAccess container, InventoryAccess bot, String unavailableReason) {
        public static Target unavailable(String reason) {
            return new Target(null, null, null, reason);
        }

        public boolean available() {
            return blockId != null && container != null && bot != null && unavailableReason == null;
        }
    }

    @FunctionalInterface
    public interface TargetResolver {
        Target resolve();
    }

    @FunctionalInterface
    public interface ProposalSender {
        void send(MutationGate.Proposal proposal);
    }

    public record Outcome(boolean success, String reason, JsonObject facts) {
        public String classification() {
            return success ? ActionRuntime.CLASS_COMPLETED : ActionRuntime.CLASS_FAILED;
        }
    }

    public static Outcome transfer(
        Target target,
        String direction,
        int containerSlot,
        int botSlot,
        int requestedCount,
        int requestedMaxStack
    ) {
        JsonObject before = baseFacts(direction, containerSlot, botSlot);
        if (!target.available()) {
            return failure(target.unavailableReason() == null ? "container_unavailable" : target.unavailableReason(), before);
        }
        if (!direction.equals("container_to_bot") && !direction.equals("bot_to_container")) {
            return failure("invalid_direction", before);
        }
        if (containerSlot < 0 || containerSlot >= target.container().size()
            || botSlot < 0 || botSlot >= target.bot().size()) {
            return failure("invalid_slot", before);
        }

        InventoryAccess source = direction.equals("container_to_bot") ? target.container() : target.bot();
        InventoryAccess destination = direction.equals("container_to_bot") ? target.bot() : target.container();
        int sourceSlot = direction.equals("container_to_bot") ? containerSlot : botSlot;
        int destinationSlot = direction.equals("container_to_bot") ? botSlot : containerSlot;
        JsonObject containerBefore = slotFact(target.container(), containerSlot);
        JsonObject botBefore = slotFact(target.bot(), botSlot);
        before.add("container_before", containerBefore);
        before.add("bot_before", botBefore);

        if (source.slotEmpty(sourceSlot)) {
            return outcome(false, "source_empty", "empty", 0, before, target, containerSlot, botSlot);
        }
        String item = source.itemIdAt(sourceSlot);
        int sourceCount = source.itemCountAt(sourceSlot);
        if (!destination.slotEmpty(destinationSlot)
            && !source.sameStack(sourceSlot, destination, destinationSlot)) {
            return outcome(false, "destination_occupied", item, 0, before, target, containerSlot, botSlot);
        }
        if (!source.canMoveTo(sourceSlot, destination, destinationSlot)) {
            return outcome(false, "destination_rejected", item, 0, before, target, containerSlot, botSlot);
        }

        int wanted = requestedCount <= 0 ? sourceCount : Math.min(requestedCount, sourceCount);
        int stackLimit = Math.min(
            Math.max(1, requestedMaxStack),
            Math.min(source.maxStackSizeAt(sourceSlot), destination.destinationMaxStackSize(destinationSlot, source, sourceSlot))
        );
        int room = stackLimit - destination.itemCountAt(destinationSlot);
        if (room <= 0) {
            return outcome(false, "destination_full", item, 0, before, target, containerSlot, botSlot);
        }
        int moved = Math.min(wanted, room);
        int destinationBeforeCount = destination.itemCountAt(destinationSlot);
        source.moveItemsTo(sourceSlot, destination, destinationSlot, moved);
        int sourceAfterCount = source.itemCountAt(sourceSlot);
        int destinationAfterCount = destination.itemCountAt(destinationSlot);
        if (sourceAfterCount != sourceCount - moved || destinationAfterCount != destinationBeforeCount + moved) {
            return outcome(false, "transfer_verification_failed", item, moved, before, target, containerSlot, botSlot);
        }
        return outcome(true, moved == sourceCount ? "completed" : "partial", item, moved, before, target, containerSlot, botSlot);
    }

    private static Outcome failure(String reason, JsonObject facts) {
        facts.addProperty("success", false);
        facts.addProperty("reason", reason);
        facts.addProperty("stopped_reason", reason);
        return new Outcome(false, reason, facts);
    }

    private static Outcome outcome(
        boolean success,
        String reason,
        String item,
        int count,
        JsonObject facts,
        Target target,
        int containerSlot,
        int botSlot
    ) {
        facts.addProperty("success", success);
        facts.addProperty("reason", reason);
        facts.addProperty("stopped_reason", reason);
        facts.addProperty("item", item);
        facts.addProperty("count", count);
        facts.add("container_after", slotFact(target.container(), containerSlot));
        facts.add("bot_after", slotFact(target.bot(), botSlot));
        return new Outcome(success, reason, facts);
    }

    private static JsonObject baseFacts(String direction, int containerSlot, int botSlot) {
        JsonObject facts = new JsonObject();
        facts.addProperty("direction", direction);
        facts.addProperty("container_slot", containerSlot);
        facts.addProperty("bot_slot", botSlot);
        return facts;
    }

    private static JsonObject slotFact(InventoryAccess inventory, int slot) {
        JsonObject fact = new JsonObject();
        boolean empty = slot < 0 || slot >= inventory.size() || inventory.slotEmpty(slot);
        fact.addProperty("slot", slot);
        fact.addProperty("empty", empty);
        fact.addProperty("item", empty ? null : inventory.itemIdAt(slot));
        fact.addProperty("count", empty ? 0 : inventory.itemCountAt(slot));
        return fact;
    }

    public static final class Executor implements ActionRuntime.TickExecutor {
        private enum Phase { PROPOSE, AWAITING_VERDICT, FINISHED }

        private final String bot;
        private final String actionId;
        private final int x;
        private final int y;
        private final int z;
        private final String direction;
        private final int containerSlot;
        private final int botSlot;
        private final int requestedCount;
        private final int maxStack;
        private final TargetResolver targets;
        private final MutationGate gate;
        private final ProposalSender proposals;
        private final BotEventStream events;
        private final ActionRuntime runtime;
        private Phase phase = Phase.PROPOSE;
        private String proposalId;
        private String proposedBlockId;

        public Executor(
            String bot,
            String actionId,
            int x,
            int y,
            int z,
            String direction,
            int containerSlot,
            int botSlot,
            int requestedCount,
            int maxStack,
            TargetResolver targets,
            MutationGate gate,
            ProposalSender proposals,
            BotEventStream events,
            ActionRuntime runtime
        ) {
            this.bot = bot;
            this.actionId = actionId;
            this.x = x;
            this.y = y;
            this.z = z;
            this.direction = direction;
            this.containerSlot = containerSlot;
            this.botSlot = botSlot;
            this.requestedCount = requestedCount;
            this.maxStack = maxStack;
            this.targets = targets;
            this.gate = gate;
            this.proposals = proposals;
            this.events = events;
            this.runtime = runtime;
        }

        @Override
        public void tick(int serverTick) {
            if (phase == Phase.FINISHED) {
                return;
            }
            if (runtime.cancelRequested(actionId)) {
                if (proposalId != null) {
                    gate.discard(proposalId);
                }
                finish(serverTick, ActionRuntime.CLASS_CANCELED, "canceled", null);
                return;
            }
            try {
                if (phase == Phase.PROPOSE) {
                    propose(serverTick);
                } else {
                    awaitVerdict(serverTick);
                }
            } catch (RuntimeException error) {
                if (proposalId != null) {
                    gate.discard(proposalId);
                }
                finish(serverTick, ActionRuntime.CLASS_FAILED, "container_transfer_internal_error", null);
            }
        }

        private void propose(int serverTick) {
            Target target = targets.resolve();
            if (!target.available()) {
                finish(serverTick, ActionRuntime.CLASS_FAILED, target.unavailableReason(), null);
                return;
            }
            proposedBlockId = target.blockId();
            MutationGate.Proposal proposal = gate.propose(
                bot, actionId, "open", x, y, z, proposedBlockId, "activate", serverTick
            );
            proposalId = proposal.proposalId();
            proposals.send(proposal);
            JsonObject data = new JsonObject();
            data.addProperty("proposal_id", proposalId);
            data.addProperty("kind", "open");
            data.addProperty("block_id", proposedBlockId);
            events.emit(bot, serverTick, "mutation_proposed", actionId, data);
            phase = Phase.AWAITING_VERDICT;
        }

        private void awaitVerdict(int serverTick) {
            MutationGate.State state = gate.poll(proposalId, serverTick);
            if (state == MutationGate.State.PENDING) {
                return;
            }
            if (state == MutationGate.State.DENIED || state == MutationGate.State.TIMED_OUT) {
                String reason = state == MutationGate.State.DENIED
                    ? "governance_denied:" + gate.reason(proposalId)
                    : "governance_verdict_timeout";
                emitVerdict(serverTick, "mutation_denied", reason);
                gate.discard(proposalId);
                finish(serverTick, ActionRuntime.CLASS_FAILED, reason, null);
                return;
            }

            emitVerdict(serverTick, "mutation_allowed", gate.reason(proposalId));
            gate.discard(proposalId);
            Target current = targets.resolve();
            if (!current.available()) {
                finish(serverTick, ActionRuntime.CLASS_FAILED, current.unavailableReason(), null);
                return;
            }
            if (!proposedBlockId.equals(current.blockId())) {
                finish(serverTick, ActionRuntime.CLASS_FAILED, "target_changed", null);
                return;
            }
            Outcome outcome = transfer(current, direction, containerSlot, botSlot, requestedCount, maxStack);
            finish(serverTick, outcome.classification(), outcome.reason(), outcome.facts());
        }

        private void emitVerdict(int serverTick, String name, String reason) {
            JsonObject data = new JsonObject();
            data.addProperty("proposal_id", proposalId);
            data.addProperty("reason", reason);
            events.emit(bot, serverTick, name, actionId, data);
        }

        private void finish(int serverTick, String classification, String reason, JsonObject source) {
            JsonObject facts = source == null ? baseFacts(direction, containerSlot, botSlot) : source.deepCopy();
            facts.addProperty("reason", reason == null ? "container_unavailable" : reason);
            facts.addProperty("stopped_reason", reason == null ? "container_unavailable" : reason);
            facts.addProperty("success", classification.equals(ActionRuntime.CLASS_COMPLETED));
            JsonArray pos = new JsonArray();
            pos.add(x);
            pos.add(y);
            pos.add(z);
            facts.add("pos", pos);
            phase = Phase.FINISHED;
            runtime.finish(bot, actionId, classification, facts, serverTick);
        }
    }
}
