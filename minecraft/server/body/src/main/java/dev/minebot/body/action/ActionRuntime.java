package dev.minebot.body.action;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonNull;
import com.google.gson.JsonObject;
import dev.minebot.body.control.FakePlayerActionOwner;
import dev.minebot.body.control.OwnerPriority;
import dev.minebot.body.event.BotEventStream;

import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * Orchestrates the action lifecycle across ownership, physical-input hygiene,
 * the registry, and the event stream. Guarantees: a submit is acknowledged,
 * never terminal; a strictly higher priority class preempts the current owner
 * and that owner receives a {@code preempted} terminal; every terminal clears
 * held inputs before it is recorded and emitted; exactly one terminal exists
 * per action id.
 */
public final class ActionRuntime {
    public static final int MAX_TERMINAL_BYTES = 256 * 1024;
    public static final String CLASS_COMPLETED = "completed";
    public static final String CLASS_PARTIAL = "partial";
    public static final String CLASS_FAILED = "failed";
    public static final String CLASS_TIMEOUT = "timeout";
    public static final String CLASS_PREEMPTED = "preempted";
    public static final String CLASS_CANCELED = "canceled";
    public static final String CLASS_UNSAFE = "unsafe";
    private static final int MAX_OMITTED_FIELD_NAMES = 32;
    private static final Set<String> TERMINAL_CORE_FIELDS = Set.of(
        "classification",
        "reason",
        "reason_class",
        "goal",
        "attempts_consumed",
        "budget_remaining",
        "closest_approach_distance",
        "unloaded_chunk_count",
        "elapsed_ticks",
        "evidence_truncated",
        "evidence_omitted_fields"
    );

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
            facts.addProperty("reason", CLASS_PREEMPTED);
            facts.addProperty("success", false);
            facts.addProperty("paused", true);
            facts.addProperty("preempted_by", actionId);
            facts.addProperty("preempted_by_priority", priority.name());
            terminate(bot, previous.actionId(), CLASS_PREEMPTED, facts, serverTick, false);
            JsonObject handoff = new JsonObject();
            handoff.addProperty("previous_owner", previous.actionId());
            handoff.addProperty("previous_priority", previous.priority().name());
            handoff.addProperty("new_owner", actionId);
            handoff.addProperty("new_priority", priority.name());
            handoff.addProperty("reason", CLASS_PREEMPTED);
            events.emit(bot, serverTick, "ownerPreempted", previous.actionId(), handoff);
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
        ActionRegistry.ActionRecord record = registry.status(actionId).record();
        JsonObject terminal = terminalEnvelope(
            bot,
            actionId,
            classification,
            facts,
            record,
            serverTick
        );
        if (!registry.markTerminal(actionId, terminal)) {
            return false;
        }
        if (releaseOwner) {
            owner.release(bot, actionId);
        }
        events.emit(bot, serverTick, "action_terminal", actionId, terminal);
        return true;
    }

    private static JsonObject terminalEnvelope(
        String bot,
        String actionId,
        String classification,
        JsonObject facts,
        ActionRegistry.ActionRecord record,
        int serverTick
    ) {
        JsonObject terminal = facts == null ? new JsonObject() : facts.deepCopy();
        String reason = stringValue(terminal.get("reason"), classification);
        terminal.addProperty("classification", classification);
        terminal.addProperty("reason", reason);
        if (!terminal.has("reason_class")) {
            terminal.addProperty("reason_class", reasonClass(classification, reason));
        }

        JsonObject goal = new JsonObject();
        goal.addProperty("action_id", actionId);
        goal.addProperty("objective", record == null ? "unknown" : record.type());
        goal.addProperty("bot", record == null ? bot : record.bot());
        terminal.add("goal", goal);
        if (!terminal.has("attempts_consumed")) {
            terminal.addProperty("attempts_consumed", attemptsConsumed(terminal));
        }
        if (!terminal.has("budget_remaining")) {
            terminal.add("budget_remaining", JsonNull.INSTANCE);
        }
        if (!terminal.has("closest_approach_distance")) {
            JsonElement distance = firstPresent(
                terminal,
                "final_distance",
                "final_reach_distance"
            );
            terminal.add(
                "closest_approach_distance",
                distance == null ? JsonNull.INSTANCE : distance.deepCopy()
            );
        }
        if (!terminal.has("unloaded_chunk_count")) {
            JsonElement unloaded = firstPresent(
                terminal,
                "unloaded_chunk_count",
                "unloaded_touches"
            );
            terminal.add(
                "unloaded_chunk_count",
                unloaded == null ? JsonNull.INSTANCE : unloaded.deepCopy()
            );
        }
        if (!terminal.has("elapsed_ticks")) {
            long startedTick = record == null ? serverTick : record.startedTick();
            terminal.addProperty("elapsed_ticks", Math.max(0L, serverTick - startedTick));
        }
        return boundTerminal(terminal);
    }

    private static JsonObject boundTerminal(JsonObject terminal) {
        if (serializedBytes(terminal) <= MAX_TERMINAL_BYTES) {
            return terminal;
        }
        terminal.addProperty("evidence_truncated", true);
        JsonArray omitted = new JsonArray();
        List<Map.Entry<String, JsonElement>> removable = terminal.entrySet().stream()
            .filter(entry -> !TERMINAL_CORE_FIELDS.contains(entry.getKey()))
            .sorted(Comparator.comparingInt(
                (Map.Entry<String, JsonElement> entry) -> serializedBytes(entry.getValue())
            ).reversed())
            .toList();
        for (Map.Entry<String, JsonElement> entry : removable) {
            terminal.remove(entry.getKey());
            if (omitted.size() < MAX_OMITTED_FIELD_NAMES) {
                omitted.add(entry.getKey());
            }
            terminal.add("evidence_omitted_fields", omitted);
            if (serializedBytes(terminal) <= MAX_TERMINAL_BYTES) {
                return terminal;
            }
        }

        JsonObject coreOnly = new JsonObject();
        for (String field : TERMINAL_CORE_FIELDS) {
            if (terminal.has(field)) {
                coreOnly.add(field, terminal.get(field).deepCopy());
            }
        }
        coreOnly.addProperty("evidence_truncated", true);
        if (!coreOnly.has("evidence_omitted_fields")) {
            JsonArray allNonCore = new JsonArray();
            allNonCore.add("terminal_non_core");
            coreOnly.add("evidence_omitted_fields", allNonCore);
        }
        if (serializedBytes(coreOnly) > MAX_TERMINAL_BYTES) {
            throw new IllegalStateException("decision-grade terminal core exceeds transport bound");
        }
        return coreOnly;
    }

    static int serializedBytes(JsonElement value) {
        return value.toString().getBytes(StandardCharsets.UTF_8).length;
    }

    private static int attemptsConsumed(JsonObject terminal) {
        for (String key : List.of("candidates_tried", "replans", "steps_completed")) {
            JsonElement value = terminal.get(key);
            if (value != null && value.isJsonPrimitive() && value.getAsJsonPrimitive().isNumber()) {
                return Math.max(0, value.getAsInt());
            }
        }
        return 0;
    }

    private static JsonElement firstPresent(JsonObject source, String... keys) {
        for (String key : keys) {
            JsonElement value = source.get(key);
            if (value != null && !value.isJsonNull()) {
                return value;
            }
        }
        return null;
    }

    private static String stringValue(JsonElement value, String fallback) {
        return value != null && value.isJsonPrimitive() && value.getAsJsonPrimitive().isString()
            ? value.getAsString()
            : fallback;
    }

    private static String reasonClass(String classification, String reason) {
        if (CLASS_COMPLETED.equals(classification)) {
            return "completed";
        }
        if (CLASS_TIMEOUT.equals(classification)) {
            return "timeout";
        }
        if (CLASS_CANCELED.equals(classification) || CLASS_PREEMPTED.equals(classification)) {
            return "canceled";
        }
        if (CLASS_UNSAFE.equals(classification)
            || reason.contains("denied")
            || reason.contains("governance")) {
            return "denied";
        }
        if (CLASS_PARTIAL.equals(classification)
            || reason.contains("budget")
            || reason.contains("exhausted")) {
            return "exhausted";
        }
        return "blocked";
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
