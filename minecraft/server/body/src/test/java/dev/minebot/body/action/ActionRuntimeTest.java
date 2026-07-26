package dev.minebot.body.action;

import com.google.gson.JsonObject;
import dev.minebot.body.control.FakePlayerActionOwner;
import dev.minebot.body.control.OwnerPriority;
import dev.minebot.body.event.BotEventStream;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertInstanceOf;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

final class ActionRuntimeTest {
    private static final class RecordingControls implements BotControls {
        final List<String> cleared = new ArrayList<>();

        @Override
        public void clearAll(String botName) {
            cleared.add(botName);
        }
    }

    private final FakePlayerActionOwner owner = new FakePlayerActionOwner();
    private final RecordingControls controls = new RecordingControls();
    private final ActionRegistry registry = new ActionRegistry();
    private final BotEventStream events = new BotEventStream();
    private final ActionRuntime runtime = new ActionRuntime(owner, controls, registry, events);

    @Test
    void submitAcceptsAndOwnsWithoutBeingTerminal() {
        var result = runtime.submit("Bot", "a-1", "NAVIGATE", OwnerPriority.ACTION, 10);

        assertInstanceOf(ActionRuntime.Submission.Accepted.class, result);
        assertEquals(ActionRegistry.State.RUNNING, registry.status("a-1").state());
        assertEquals("a-1", owner.current("Bot").actionId());
        assertTrue(controls.cleared.isEmpty(), "submit must not clear inputs");
    }

    @Test
    void duplicateSubmitReportsStateInsteadOfRestarting() {
        runtime.submit("Bot", "a-1", "NAVIGATE", OwnerPriority.ACTION, 10);

        var duplicate = runtime.submit("Bot", "a-1", "NAVIGATE", OwnerPriority.ACTION, 11);

        var dup = assertInstanceOf(ActionRuntime.Submission.Duplicate.class, duplicate);
        assertEquals(ActionRegistry.State.RUNNING, dup.status().state());
    }

    @Test
    void equalPriorityIsRejectedBusyWithOwnerFacts() {
        runtime.submit("Bot", "a-1", "NAVIGATE", OwnerPriority.ACTION, 10);

        var rejected = runtime.submit("Bot", "a-2", "NAVIGATE", OwnerPriority.ACTION, 11);

        var busy = assertInstanceOf(ActionRuntime.Submission.Rejected.class, rejected);
        assertEquals("owner_busy", busy.code());
        assertEquals("a-1", busy.currentOwner().actionId());
        assertEquals(ActionRegistry.State.UNKNOWN, registry.status("a-2").state());
    }

    @Test
    void survivalPreemptsAndThePreemptedActionGetsItsTerminal() {
        runtime.submit("Bot", "a-1", "NAVIGATE", OwnerPriority.ACTION, 10);

        var result = runtime.submit("Bot", "reflex-1", "SURVIVAL_REFLEX", OwnerPriority.SURVIVAL, 20);

        assertInstanceOf(ActionRuntime.Submission.Accepted.class, result);
        var status = registry.status("a-1");
        assertEquals(ActionRegistry.State.TERMINAL, status.state());
        assertEquals("preempted", status.terminal().get("classification").getAsString());
        assertEquals("reflex-1", status.terminal().get("preempted_by").getAsString());
        assertEquals("reflex-1", owner.current("Bot").actionId());
        assertEquals(List.of("Bot"), controls.cleared);
        assertEquals(ActionRegistry.State.RUNNING, registry.status("reflex-1").state());
    }

    @Test
    void finishClearsInputsExactlyOnceAndReleasesOwnership() {
        runtime.submit("Bot", "a-1", "NAVIGATE", OwnerPriority.ACTION, 10);
        JsonObject facts = new JsonObject();
        facts.addProperty("final_x", 12);

        assertTrue(runtime.finish("Bot", "a-1", ActionRuntime.CLASS_COMPLETED, facts, 30));
        assertFalse(runtime.finish("Bot", "a-1", ActionRuntime.CLASS_FAILED, null, 31), "second terminal must be refused");

        var status = registry.status("a-1");
        assertEquals(ActionRegistry.State.TERMINAL, status.state());
        assertEquals("completed", status.terminal().get("classification").getAsString());
        assertEquals(12, status.terminal().get("final_x").getAsInt());
        assertNull(owner.current("Bot"));

        BotEventStream.Replay replay = events.replay("Bot", 0);
        List<String> names = replay.events().stream().map(BotEventStream.Event::name).toList();
        assertEquals(List.of("owner_acquired", "action_terminal"), names);
    }

    @Test
    void cancelFlagsTheRunningActionAndExecutorFinishesIt() {
        runtime.submit("Bot", "a-1", "NAVIGATE", OwnerPriority.ACTION, 10);
        runtime.attachExecutor("a-1", serverTick -> {
            if (runtime.cancelRequested("a-1")) {
                runtime.finish("Bot", "a-1", ActionRuntime.CLASS_CANCELED, null, serverTick);
            }
        });

        assertTrue(runtime.requestCancel("a-1"));
        runtime.tick(40);

        var status = registry.status("a-1");
        assertEquals(ActionRegistry.State.TERMINAL, status.state());
        assertEquals("canceled", status.terminal().get("classification").getAsString());
        assertTrue(runtime.runningExecutorActionIds().isEmpty());
        assertFalse(runtime.requestCancel("a-1"), "cancel after terminal is refused");
    }

    @Test
    void unknownActionReconcilesAsUnknown() {
        assertEquals(ActionRegistry.State.UNKNOWN, registry.status("never-seen").state());
        assertFalse(runtime.requestCancel("never-seen"));
    }
}
