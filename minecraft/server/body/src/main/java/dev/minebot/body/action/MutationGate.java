package dev.minebot.body.action;

import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.atomic.AtomicLong;

/**
 * Fail-closed pending-verdict registry for the two-tier governance contract.
 * The Java Body proposes every physical mutation to Python and may not touch
 * the world until an explicit allow arrives; a missing, late, or malformed
 * verdict is a denial. Verdicts are one-shot and never cached across
 * proposals.
 */
public final class MutationGate {
    /** Frozen in tests/fixtures/java_body_budgets.json. */
    public static final int VERDICT_TIMEOUT_TICKS = 100;

    public enum State {
        PENDING,
        ALLOWED,
        DENIED,
        TIMED_OUT
    }

    public record Proposal(
        String proposalId,
        String bot,
        String actionId,
        String mutationKind,
        int x,
        int y,
        int z,
        String blockId,
        int deadlineTick
    ) {
    }

    private record Pending(Proposal proposal, State state, String reason) {
    }

    private final AtomicLong counter = new AtomicLong();
    private final Map<String, Pending> pending = new HashMap<>();

    public synchronized Proposal propose(
        String bot,
        String actionId,
        String mutationKind,
        int x,
        int y,
        int z,
        String blockId,
        int currentTick
    ) {
        String proposalId = "mp-" + counter.incrementAndGet();
        Proposal proposal = new Proposal(
            proposalId, bot, actionId, mutationKind, x, y, z, blockId, currentTick + VERDICT_TIMEOUT_TICKS
        );
        pending.put(proposalId, new Pending(proposal, State.PENDING, null));
        return proposal;
    }

    /** Resolves a pending proposal exactly once; late or unknown verdicts are ignored. */
    public synchronized boolean verdict(String proposalId, boolean allow, String reason) {
        Pending current = pending.get(proposalId);
        if (current == null || current.state() != State.PENDING) {
            return false;
        }
        pending.put(proposalId, new Pending(current.proposal(), allow ? State.ALLOWED : State.DENIED, reason));
        return true;
    }

    /** Current state; a pending proposal past its deadline becomes TIMED_OUT (a denial). */
    public synchronized State poll(String proposalId, int currentTick) {
        Pending current = pending.get(proposalId);
        if (current == null) {
            return State.TIMED_OUT;
        }
        if (current.state() == State.PENDING && currentTick > current.proposal().deadlineTick()) {
            pending.put(proposalId, new Pending(current.proposal(), State.TIMED_OUT, "verdict_timeout"));
            return State.TIMED_OUT;
        }
        return pending.get(proposalId).state();
    }

    public synchronized String reason(String proposalId) {
        Pending current = pending.get(proposalId);
        return current == null ? null : current.reason();
    }

    /** Drops a resolved proposal after its consumer read the outcome. */
    public synchronized void discard(String proposalId) {
        pending.remove(proposalId);
    }
}
