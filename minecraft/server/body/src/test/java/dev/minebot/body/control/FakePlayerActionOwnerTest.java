package dev.minebot.body.control;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertInstanceOf;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

final class FakePlayerActionOwnerTest {
    @Test
    void firstAcquisitionOwnsTheBot() {
        FakePlayerActionOwner owner = new FakePlayerActionOwner();
        var result = owner.acquire("Bot", "a-1", OwnerPriority.ACTION);

        var acquired = assertInstanceOf(FakePlayerActionOwner.Acquisition.Acquired.class, result);
        assertFalse(acquired.preempted());
        assertEquals("a-1", owner.current("Bot").actionId());
    }

    @Test
    void reacquiringTheSameActionIsIdempotent() {
        FakePlayerActionOwner owner = new FakePlayerActionOwner();
        owner.acquire("Bot", "a-1", OwnerPriority.ACTION);

        var again = owner.acquire("Bot", "a-1", OwnerPriority.ACTION);

        var acquired = assertInstanceOf(FakePlayerActionOwner.Acquisition.Acquired.class, again);
        assertFalse(acquired.preempted());
    }

    @Test
    void equalOrLowerPriorityIsBusyWithOwnerFacts() {
        FakePlayerActionOwner owner = new FakePlayerActionOwner();
        owner.acquire("Bot", "a-1", OwnerPriority.ACTION);

        var equal = owner.acquire("Bot", "a-2", OwnerPriority.ACTION);
        var lower = owner.acquire("Bot", "a-3", OwnerPriority.IDLE);

        var busyEqual = assertInstanceOf(FakePlayerActionOwner.Acquisition.Busy.class, equal);
        assertEquals("a-1", busyEqual.current().actionId());
        assertEquals(OwnerPriority.ACTION, busyEqual.current().priority());
        assertInstanceOf(FakePlayerActionOwner.Acquisition.Busy.class, lower);
        assertEquals("a-1", owner.current("Bot").actionId());
    }

    @Test
    void higherPriorityPreemptsAndReportsThePreviousOwner() {
        FakePlayerActionOwner owner = new FakePlayerActionOwner();
        owner.acquire("Bot", "a-1", OwnerPriority.ACTION);

        var result = owner.acquire("Bot", "reflex-1", OwnerPriority.SURVIVAL);

        var acquired = assertInstanceOf(FakePlayerActionOwner.Acquisition.Acquired.class, result);
        assertTrue(acquired.preempted());
        assertEquals("a-1", acquired.previous().actionId());
        assertEquals("reflex-1", owner.current("Bot").actionId());
    }

    @Test
    void priorityLadderIsSurvivalOverRecoveryOverActionOverIdle() {
        assertTrue(OwnerPriority.SURVIVAL.outranks(OwnerPriority.RECOVERY));
        assertTrue(OwnerPriority.RECOVERY.outranks(OwnerPriority.ACTION));
        assertTrue(OwnerPriority.ACTION.outranks(OwnerPriority.IDLE));
        assertFalse(OwnerPriority.ACTION.outranks(OwnerPriority.ACTION));
        assertFalse(OwnerPriority.IDLE.outranks(OwnerPriority.SURVIVAL));
    }

    @Test
    void onlyTheCurrentOwnerCanRelease() {
        FakePlayerActionOwner owner = new FakePlayerActionOwner();
        owner.acquire("Bot", "a-1", OwnerPriority.ACTION);

        assertFalse(owner.release("Bot", "a-2"));
        assertTrue(owner.release("Bot", "a-1"));
        assertNull(owner.current("Bot"));
        assertFalse(owner.release("Bot", "a-1"));
    }

    @Test
    void botsAreIndependent() {
        FakePlayerActionOwner owner = new FakePlayerActionOwner();
        owner.acquire("BotA", "a-1", OwnerPriority.ACTION);

        var result = owner.acquire("BotB", "b-1", OwnerPriority.ACTION);

        assertInstanceOf(FakePlayerActionOwner.Acquisition.Acquired.class, result);
        assertEquals("a-1", owner.current("BotA").actionId());
        assertEquals("b-1", owner.current("BotB").actionId());
    }
}
