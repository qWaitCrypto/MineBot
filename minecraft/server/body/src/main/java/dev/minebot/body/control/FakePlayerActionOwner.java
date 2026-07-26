package dev.minebot.body.control;

import java.util.HashMap;
import java.util.Map;

/**
 * Per-bot single-writer arbitration. Exactly one owner may drive a bot's
 * physical controls at a time. Re-acquiring with the same action id is
 * idempotent; a strictly higher priority class preempts the current owner
 * (the preempted owner is reported so its action can be terminated with a
 * {@code preempted} terminal); anything else is rejected as busy.
 */
public final class FakePlayerActionOwner {
    public record Owner(String actionId, OwnerPriority priority) {
    }

    public sealed interface Acquisition {
        record Acquired(Owner previous) implements Acquisition {
            public boolean preempted() {
                return previous != null;
            }
        }

        record Busy(Owner current) implements Acquisition {
        }
    }

    private final Map<String, Owner> ownerByBot = new HashMap<>();

    public synchronized Acquisition acquire(String botName, String actionId, OwnerPriority priority) {
        Owner current = ownerByBot.get(botName);
        if (current == null) {
            ownerByBot.put(botName, new Owner(actionId, priority));
            return new Acquisition.Acquired(null);
        }
        if (current.actionId().equals(actionId)) {
            return new Acquisition.Acquired(null);
        }
        if (priority.outranks(current.priority())) {
            ownerByBot.put(botName, new Owner(actionId, priority));
            return new Acquisition.Acquired(current);
        }
        return new Acquisition.Busy(current);
    }

    /** Release succeeds only for the current owner; stale releases are no-ops. */
    public synchronized boolean release(String botName, String actionId) {
        Owner current = ownerByBot.get(botName);
        if (current == null || !current.actionId().equals(actionId)) {
            return false;
        }
        ownerByBot.remove(botName);
        return true;
    }

    public synchronized Owner current(String botName) {
        return ownerByBot.get(botName);
    }
}
