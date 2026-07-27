package dev.minebot.body.action;

import com.google.gson.JsonObject;
import dev.minebot.body.control.FakePlayerActionOwner;
import dev.minebot.body.control.OwnerPriority;
import dev.minebot.body.event.BotEventStream;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * Orchestrates the action lifecycle across ownership, physical-input hygiene,
 * the registry, and the event stream. Guarantees: a submit is acknowledged,
 * never terminal; a strictly higher priority class preempts the current owner
 * and that owner receives a {@code preempted} terminal; every terminal clears
 * held inputs before it is recorded and emitted; exactly one terminal exists
 * per action id.
 */
public final class ActionRuntime {
    public static final String CLASS_COMPLETED = "completed";
    public static final String CLASS_FAILED = "failed";
    public static final String CLASS_TIMEOUT = "timeout";
    public static final String CLASS_PREEMPTED = "preempted";
    public static final String CLASS_CANCELED = "canceled";
    public static final String CLASS_UNSAFE = "unsafe";

    /** Ticked engine driving one running action; finishes itself through the runtime. */
    public interface TickExecutor {
        void tick(int serverTick);
    }

    public sealed interface Submission {
        record Accepted() implements Submission {
        }

        /** The id is already known; carries its current status instead of restarting it. */
        record Duplicate(ActionRegistry.ActionStatus status) implements Submission {
        }

        record Rejected(String code, FakePlayerActionOwner.Owner currentOwner) implements Submission {
        }
    }

    private final FakePlayerActionOwner owner;
    private final BotControls controls;
    private final ActionRegistry registry;
    private final BotEventStream events;
    private final Map<String, TickExecutor> executors = new HashMap<>();

    public ActionRuntime(
        FakePlayerActionOwner owner,
        BotControls controls,
        ActionRegistry registry,
        BotEventStream events
    ) {
        this.owner = owner;
        this.controls = controls;
        this.registry = registry;
        this.events = events;
    }

    public Submission submit(String bot, String actionId, String type, OwnerPriority priority, int serverTick) {
        ActionRegistry.ActionStatus existing = registry.status(actionId);
        if (existing.state() != ActionRegistry.State.UNKNOWN) {
            return new Submission.Duplicate(existing);
        }
        FakePlayerActionOwner.Acquisition acquisition = owner.acquire(bot, actionId, priority);
        if (acquisition instanceof FakePlayerActionOwner.Acquisition.Busy busy) {
            return new Submission.Rejected("owner_busy", busy.current());
        }
        FakePlayerActionOwner.Acquisition.Acquired acquired = (FakePlayerActionOwner.Acquisition.Acquired) acquisition;
        if (acquired.preempted()) {
            FakePlayerActionOwner.Owner previous = acquired.previous();
            JsonObject facts = new JsonObject();
            facts.addProperty("preempted_by", actionId);
            facts.addProperty("preempted_by_priority", priority.name());
            terminate(bot, previous.actionId(), CLASS_PREEMPTED, facts, serverTick, false);
        }
        registry.submit(bot, actionId, type, serverTick);
        events.emit(bot, serverTick, "owner_acquired", actionId, ownerFacts(type, priority));
        return new Submission.Accepted();
    }

    public void attachExecutor(String actionId, TickExecutor executor) {
        executors.put(actionId, executor);
    }

    /** Ticks every running executor; executors finish themselves via {@link #finish}. */
    public void tick(int serverTick) {
        for (TickExecutor executor : List.copyOf(executors.values())) {
            executor.tick(serverTick);
        }
    }

    public boolean requestCancel(String actionId) {
        return registry.requestCancel(actionId);
    }

    /** Immediately releases a body that died or disappeared. */
    public boolean abortCurrent(String bot, String reason, int serverTick) {
        FakePlayerActionOwner.Owner current = owner.current(bot);
        if (current == null) {
            controls.clearAll(bot);
            return false;
        }
        JsonObject facts = new JsonObject();
        facts.addProperty("reason", reason);
        facts.addProperty("success", false);
        return terminate(bot, current.actionId(), CLASS_FAILED, facts, serverTick, true);
    }

    public boolean cancelRequested(String actionId) {
        return registry.cancelRequested(actionId);
    }

    /** Terminates a running action: input hygiene, single terminal, release, terminal event. */
    public boolean finish(String bot, String actionId, String classification, JsonObject facts, int serverTick) {
        return terminate(bot, actionId, classification, facts, serverTick, true);
    }

    private boolean terminate(
        String bot,
        String actionId,
        String classification,
        JsonObject facts,
        int serverTick,
        boolean releaseOwner
    ) {
        executors.remove(actionId);
        controls.clearAll(bot);
        JsonObject terminal = facts == null ? new JsonObject() : facts.deepCopy();
        terminal.addProperty("classification", classification);
        if (!registry.markTerminal(actionId, terminal)) {
            return false;
        }
        if (releaseOwner) {
            owner.release(bot, actionId);
        }
        events.emit(bot, serverTick, "action_terminal", actionId, terminal);
        return true;
    }

    private static JsonObject ownerFacts(String type, OwnerPriority priority) {
        JsonObject facts = new JsonObject();
        facts.addProperty("type", type);
        facts.addProperty("priority", priority.name());
        return facts;
    }

    /** Running action ids in insertion-independent order, for diagnostics. */
    public List<String> runningExecutorActionIds() {
        return new ArrayList<>(executors.keySet());
    }

    /** The bot's current physical owner, or null when idle. */
    public FakePlayerActionOwner.Owner currentOwner(String botName) {
        return owner.current(botName);
    }

    public int pendingActionCount(String botName) {
        return owner.current(botName) == null ? 0 : 1;
    }
}
