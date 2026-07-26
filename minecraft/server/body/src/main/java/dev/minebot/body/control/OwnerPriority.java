package dev.minebot.body.control;

/**
 * Ownership priority classes for the per-bot physical writer. Higher wins:
 * SURVIVAL > RECOVERY > ACTION > IDLE. A higher class may preempt a lower
 * one; equal or lower classes are rejected as busy.
 */
public enum OwnerPriority {
    IDLE(0),
    ACTION(1),
    RECOVERY(2),
    SURVIVAL(3);

    private final int rank;

    OwnerPriority(int rank) {
        this.rank = rank;
    }

    public boolean outranks(OwnerPriority other) {
        return rank > other.rank;
    }
}
