package dev.minebot.body.action;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import dev.minebot.body.event.BotEventStream;

/** Governed movement between one named furnace slot and the player inventory. */
public final class FurnacePrimitiveActions {
    private FurnacePrimitiveActions() {}

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
        ContainerPrimitiveActions.Target target,
        String direction,
        String furnaceSlot,
        int botSlot,
        int requestedCount,
        int requestedMaxStack
    ) {
        int slot = switch (furnaceSlot) {
            case "input" -> 0;
            case "fuel" -> 1;
            case "output" -> 2;
            default -> -1;
        };
        if (slot < 0) {
            return failure("invalid_furnace_slot", baseFacts(direction, furnaceSlot, botSlot));
        }
        String containerDirection = switch (direction) {
            case "furnace_to_bot" -> "container_to_bot";
            case "bot_to_furnace" -> "bot_to_container";
            default -> null;
        };
        if (containerDirection == null) {
            return failure("invalid_direction", baseFacts(direction, furnaceSlot, botSlot));
        }

        ContainerPrimitiveActions.Outcome raw = ContainerPrimitiveActions.transfer(
            target,
            containerDirection,
            slot,
            botSlot,
            requestedCount,
            requestedMaxStack
        );
        JsonObject facts = adaptFacts(raw.facts(), direction, furnaceSlot, botSlot);
        return new Outcome(raw.success(), raw.reason(), facts);
    }

    private static JsonObject adaptFacts(
        JsonObject source,
        String direction,
        String furnaceSlot,
        int botSlot
    ) {
        JsonObject facts = source.deepCopy();
        facts.addProperty("direction", direction);
        facts.remove("container_slot");
        facts.addProperty("furnace_slot", furnaceSlot);
        facts.addProperty("bot_slot", botSlot);
        rename(facts, "container_before", "furnace_before");
        rename(facts, "container_after", "furnace_after");
        return facts;
    }

    private static void rename(JsonObject facts, String from, String to) {
        JsonElement value = facts.remove(from);
        if (value != null) {
            facts.add(to, value);
        }
    }

    private static JsonObject baseFacts(String direction, String furnaceSlot, int botSlot) {
        JsonObject facts = new JsonObject();
        facts.addProperty("direction", direction);
        facts.addProperty("furnace_slot", furnaceSlot);
        facts.addProperty("bot_slot", botSlot);
        return facts;
    }

    private static Outcome failure(String reason, JsonObject facts) {
        facts.addProperty("success", false);
        facts.addProperty("reason", reason);
        facts.addProperty("stopped_reason", reason);
        return new Outcome(false, reason, facts);
    }

    public static final class Executor implements ActionRuntime.TickExecutor {
        private enum Phase { PROPOSE, AWAITING_VERDICT, FINISHED }

        private final String bot;
        private final String actionId;
        private final int x;
        private final int y;
        private final int z;
        private final String direction;
        private final String furnaceSlot;
        private final int botSlot;
        private final int requestedCount;
        private final int maxStack;
        private final ContainerPrimitiveActions.TargetResolver targets;
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
            String furnaceSlot,
            int botSlot,
            int requestedCount,
            int maxStack,
            ContainerPrimitiveActions.TargetResolver targets,
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
            this.furnaceSlot = furnaceSlot;
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
                finish(serverTick, ActionRuntime.CLASS_FAILED, "furnace_transfer_internal_error", null);
            }
        }

        private void propose(int serverTick) {
            ContainerPrimitiveActions.Target target = targets.resolve();
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
            ContainerPrimitiveActions.Target current = targets.resolve();
            if (!current.available()) {
                finish(serverTick, ActionRuntime.CLASS_FAILED, current.unavailableReason(), null);
                return;
            }
            if (!proposedBlockId.equals(current.blockId())) {
                finish(serverTick, ActionRuntime.CLASS_FAILED, "target_changed", null);
                return;
            }
            Outcome outcome = transfer(
                current, direction, furnaceSlot, botSlot, requestedCount, maxStack
            );
            finish(serverTick, outcome.classification(), outcome.reason(), outcome.facts());
        }

        private void emitVerdict(int serverTick, String name, String reason) {
            JsonObject data = new JsonObject();
            data.addProperty("proposal_id", proposalId);
            data.addProperty("reason", reason);
            events.emit(bot, serverTick, name, actionId, data);
        }

        private void finish(int serverTick, String classification, String reason, JsonObject source) {
            JsonObject facts = source == null
                ? baseFacts(direction, furnaceSlot, botSlot)
                : source.deepCopy();
            String terminalReason = reason == null ? "furnace_unavailable" : reason;
            facts.addProperty("reason", terminalReason);
            facts.addProperty("stopped_reason", terminalReason);
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
