package dev.minebot.body.action;

import com.google.gson.JsonObject;

import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * Action lifecycle truth with an exactly-one-terminal guarantee. A submitted
 * action is RUNNING until exactly one terminal is recorded; duplicate
 * submissions are reported instead of restarted; terminals are retained in a
 * bounded window so reconciliation can read them after the fact, and an
 * evicted or never-seen id reconciles as {@code unknown}.
 */
public final class ActionRegistry {
    public static final int MAX_RETAINED_TERMINALS = 256;

    public enum State {
        RUNNING,
        TERMINAL,
        UNKNOWN
    }

    public record ActionRecord(String bot, String actionId, String type, long startedTick) {
    }

    public record ActionStatus(State state, ActionRecord record, JsonObject terminal, boolean cancelRequested) {
        public static ActionStatus unknown() {
            return new ActionStatus(State.UNKNOWN, null, null, false);
        }
    }

    private final Map<String, ActionRecord> running = new HashMap<>();
    private final Map<String, Boolean> cancelRequests = new HashMap<>();
    private final Map<String, JsonObject> terminals = new LinkedHashMap<>(MAX_RETAINED_TERMINALS + 1, 0.75F, false) {
        @Override
        protected boolean removeEldestEntry(Map.Entry<String, JsonObject> eldest) {
            return size() > MAX_RETAINED_TERMINALS;
        }
    };
    private final Map<String, ActionRecord> terminalRecords = new HashMap<>();

    /** Registers a new action; a known id reports its current status instead of restarting. */
    public synchronized ActionStatus submit(String bot, String actionId, String type, long startedTick) {
        ActionStatus existing = status(actionId);
        if (existing.state() != State.UNKNOWN) {
            return existing;
        }
        ActionRecord record = new ActionRecord(bot, actionId, type, startedTick);
        running.put(actionId, record);
        return new ActionStatus(State.RUNNING, record, null, false);
    }

    /** Records the terminal exactly once; returns false when the action is not running. */
    public synchronized boolean markTerminal(String actionId, JsonObject terminalFacts) {
        ActionRecord record = running.remove(actionId);
        if (record == null) {
            return false;
        }
        cancelRequests.remove(actionId);
        terminals.put(actionId, terminalFacts);
        terminalRecords.put(actionId, record);
        terminalRecords.keySet().retainAll(terminals.keySet());
        return true;
    }

    /** Flags a running action for cancellation; false when it is not running. */
    public synchronized boolean requestCancel(String actionId) {
        if (!running.containsKey(actionId)) {
            return false;
        }
        cancelRequests.put(actionId, Boolean.TRUE);
        return true;
    }

    public synchronized boolean cancelRequested(String actionId) {
        return cancelRequests.getOrDefault(actionId, Boolean.FALSE);
    }

    public synchronized ActionStatus status(String actionId) {
        ActionRecord record = running.get(actionId);
        if (record != null) {
            return new ActionStatus(State.RUNNING, record, null, cancelRequested(actionId));
        }
        JsonObject terminal = terminals.get(actionId);
        if (terminal != null) {
            return new ActionStatus(State.TERMINAL, terminalRecords.get(actionId), terminal, false);
        }
        return ActionStatus.unknown();
    }
}
