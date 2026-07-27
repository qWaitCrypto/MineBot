package dev.minebot.body.action;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

final class MutationGateTest {
    @Test
    void proposalsArePendingUntilAnExplicitVerdict() {
        MutationGate gate = new MutationGate();
        var proposal = gate.propose("Bot", "a-1", "break", 1, 64, 2, "minecraft:oak_log", "collect", 100);

        assertEquals("collect", proposal.context());
        assertEquals(MutationGate.State.PENDING, gate.poll(proposal.proposalId(), 150));
        assertTrue(gate.verdict(proposal.proposalId(), true, "natural_terrain"));
        assertEquals(MutationGate.State.ALLOWED, gate.poll(proposal.proposalId(), 150));
        assertEquals("natural_terrain", gate.reason(proposal.proposalId()));
    }

    @Test
    void denialIsFinal() {
        MutationGate gate = new MutationGate();
        var proposal = gate.propose("Bot", "a-1", "break", 1, 64, 2, "minecraft:stone", "collect", 100);

        assertTrue(gate.verdict(proposal.proposalId(), false, "protected_region"));
        assertEquals(MutationGate.State.DENIED, gate.poll(proposal.proposalId(), 101));
        // A later allow cannot flip a resolved verdict.
        assertFalse(gate.verdict(proposal.proposalId(), true, "retry"));
        assertEquals(MutationGate.State.DENIED, gate.poll(proposal.proposalId(), 102));
    }

    @Test
    void missingVerdictTimesOutIntoDenialFailClosed() {
        MutationGate gate = new MutationGate();
        var proposal = gate.propose("Bot", "a-1", "break", 1, 64, 2, "minecraft:oak_log", "collect", 100);

        assertEquals(MutationGate.State.PENDING, gate.poll(proposal.proposalId(), 100 + MutationGate.VERDICT_TIMEOUT_TICKS));
        assertEquals(MutationGate.State.TIMED_OUT, gate.poll(proposal.proposalId(), 101 + MutationGate.VERDICT_TIMEOUT_TICKS));
        // A verdict arriving after the timeout is ignored.
        assertFalse(gate.verdict(proposal.proposalId(), true, "late"));
        assertEquals(MutationGate.State.TIMED_OUT, gate.poll(proposal.proposalId(), 999));
    }

    @Test
    void unknownAndDiscardedProposalsAreDenied() {
        MutationGate gate = new MutationGate();
        assertFalse(gate.verdict("mp-none", true, "x"));
        assertEquals(MutationGate.State.TIMED_OUT, gate.poll("mp-none", 0));

        var proposal = gate.propose("Bot", "a-1", "break", 1, 64, 2, "minecraft:oak_log", "collect", 100);
        gate.verdict(proposal.proposalId(), true, null);
        gate.discard(proposal.proposalId());
        assertEquals(MutationGate.State.TIMED_OUT, gate.poll(proposal.proposalId(), 101), "no permit survives discard");
    }
}
