package dev.minebot.body.action;

import com.google.gson.JsonObject;
import dev.minebot.body.control.FakePlayerActionOwner;
import dev.minebot.body.control.OwnerPriority;
import dev.minebot.body.event.BotEventStream;
import dev.minebot.body.nav.FakeWorld;
import dev.minebot.body.nav.MovementControls;
import dev.minebot.body.nav.NavigateExecutor;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

final class FollowExecutorTest {
    private static final class Harness implements
        MovementControls,
        EngageExecutor.TargetSource,
        NavigateExecutor.PositionSource,
        BotControls {
        EngageExecutor.Target target = targetAt(5.5);
        double x = 0.5;
        int clears;
        boolean moving;

        @Override public void lookAt(String bot, double x, double y, double z) { }
        @Override public void moveForward(String bot) { moving = true; }
        @Override public void stopMovement(String bot) { moving = false; }
        @Override public void jumpOnce(String bot) { }
        @Override public void sprint(String bot) { }
        @Override public void clearAll(String bot) { clears++; }

        @Override
        public EngageExecutor.Lookup acquire(String bot, String spec, double radius) {
            return "missing".equals(spec)
                ? EngageExecutor.Lookup.missing("target_not_found")
                : EngageExecutor.Lookup.found(target);
        }

        @Override
        public EngageExecutor.Lookup refresh(String bot, String targetId, double radius) {
            return EngageExecutor.Lookup.found(target);
        }

        @Override public Double bodyHealth(String bot) { return 20.0; }
        @Override public boolean hasLineOfSight(String bot, EngageExecutor.Target ignored) { return true; }
        @Override public NavigateExecutor.PositionSource.Position position(String bot) {
            if (moving) {
                x += 1.0;
            }
            return new NavigateExecutor.PositionSource.Position(x, 64.0, 0.5);
        }
    }

    @Test
    void holdsDistanceUntilWindowEndsThenReportsArrived() {
        Harness harness = new Harness();
        harness.target = targetAt(1.5);
        ActionRegistry registry = new ActionRegistry();
        BotEventStream events = new BotEventStream();
        ActionRuntime runtime = runtime(harness, registry, events);
        submit(runtime, harness, events, "follow-1", "Target", 2.0, 3);

        for (int tick = 1; tick <= 4; tick++) {
            runtime.tick(tick);
        }

        JsonObject terminal = registry.status("follow-1").terminal();
        assertEquals("completed", terminal.get("classification").getAsString());
        assertEquals("arrived", terminal.get("reason").getAsString());
        assertTrue(terminal.get("final_distance").getAsDouble() <= 2.0);
        assertTrue(harness.clears > 0);
    }

    @Test
    void replansAgainstTheSameTargetWhenItMoves() {
        Harness harness = new Harness();
        ActionRegistry registry = new ActionRegistry();
        BotEventStream events = new BotEventStream();
        ActionRuntime runtime = runtime(harness, registry, events);
        submit(runtime, harness, events, "follow-2", "Target", 1.5, 16);

        runtime.tick(1);
        harness.target = targetAt(9.5);
        for (int tick = 2; tick <= 17; tick++) {
            runtime.tick(tick);
        }

        JsonObject terminal = registry.status("follow-2").terminal();
        assertEquals("completed", terminal.get("classification").getAsString());
        assertEquals("target-1", terminal.get("target_id").getAsString());
        assertTrue(terminal.get("target_replans").getAsInt() >= 1);
        assertTrue(terminal.get("final_distance").getAsDouble() <= 1.5);
    }

    private static ActionRuntime runtime(
        Harness harness,
        ActionRegistry registry,
        BotEventStream events
    ) {
        return new ActionRuntime(new FakePlayerActionOwner(), harness, registry, events);
    }

    private static void submit(
        ActionRuntime runtime,
        Harness harness,
        BotEventStream events,
        String actionId,
        String target,
        double keepRadius,
        int timeoutTicks
    ) {
        runtime.submit("Bot", actionId, "FOLLOW_ENTITY", OwnerPriority.ACTION, 0);
        runtime.attachExecutor(actionId, new FollowExecutor(
            "Bot",
            actionId,
            target,
            keepRadius,
            2.0,
            16,
            new FakeWorld(63),
            harness,
            harness,
            harness,
            events::emit,
            runtime,
            timeoutTicks
        ));
    }

    private static EngageExecutor.Target targetAt(double x) {
        return new EngageExecutor.Target(
            "target-1", "minecraft:player", "Target", x, 64.0, 0.5, 20.0, true
        );
    }
}
