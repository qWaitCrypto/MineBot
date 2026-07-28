package dev.minebot.body.action;

import com.google.gson.JsonObject;
import dev.minebot.body.control.FakePlayerActionOwner;
import dev.minebot.body.control.OwnerPriority;
import dev.minebot.body.event.BotEventStream;
import dev.minebot.body.nav.MovementControls;
import dev.minebot.body.nav.NavigateExecutor;
import dev.minebot.body.nav.WorldView;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

final class EngageExecutorTest {
    private static final class Harness implements
        MovementControls,
        EngageExecutor.CombatControls,
        EngageExecutor.TargetSource,
        NavigateExecutor.PositionSource,
        BotControls {
        EngageExecutor.Target target = new EngageExecutor.Target(
            "target-1", "minecraft:husk", "Husk", 1.5, 64.0, 0.5, 6.0, true
        );
        double bodyHealth = 20.0;
        double x = 0.5;
        double y = 64.0;
        double z = 0.5;
        int attacks;
        int clears;
        final List<String> controls = new ArrayList<>();

        @Override
        public void lookAt(String bot, double x, double y, double z) {
            controls.add("look");
        }

        @Override
        public void attackOnce(String bot) {
            attacks++;
            double health = target.health() == null ? 0.0 : Math.max(0.0, target.health() - 3.0);
            target = new EngageExecutor.Target(
                target.id(), target.type(), target.name(), target.x(), target.y(), target.z(),
                health, health > 0.0
            );
            controls.add("attack");
        }

        @Override public void moveForward(String bot) { controls.add("forward"); }
        @Override public void stopMovement(String bot) { controls.add("stop"); }
        @Override public void jumpOnce(String bot) { controls.add("jump"); }
        @Override public void jumpContinuous(String bot) { controls.add("jumpContinuous"); }
        @Override public void sprint(String bot) { controls.add("sprint"); }

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

        @Override public Double bodyHealth(String bot) { return bodyHealth; }
        @Override public boolean hasLineOfSight(String bot, EngageExecutor.Target target) { return true; }
        @Override public NavigateExecutor.PositionSource.Position position(String bot) {
            return new NavigateExecutor.PositionSource.Position(x, y, z);
        }
        @Override public void clearAll(String bot) { clears++; }
    }

    private static WorldView openWorld() {
        return (x, y, z) -> WorldView.NodeKind.PASSABLE;
    }

    @Test
    void locksTargetAndOnlyCompletesAfterObservedDeath() {
        Harness harness = new Harness();
        ActionRegistry registry = new ActionRegistry();
        BotEventStream events = new BotEventStream();
        ActionRuntime runtime = new ActionRuntime(
            new FakePlayerActionOwner(), harness, registry, events
        );
        runtime.submit("Bot", "engage-1", "ENGAGE_ENTITY", OwnerPriority.ACTION, 0);
        runtime.attachExecutor("engage-1", new EngageExecutor(
            "Bot", "engage-1", "nearest_hostile", 2.0, 2, 16, 6.0,
            openWorld(), harness, harness, harness, harness, events::emit, runtime, 20
        ));

        runtime.tick(1);
        assertEquals(1, harness.attacks);
        assertEquals(ActionRegistry.State.RUNNING, registry.status("engage-1").state());
        runtime.tick(2);
        runtime.tick(3);
        assertEquals(2, harness.attacks);
        assertEquals(ActionRegistry.State.RUNNING, registry.status("engage-1").state());
        runtime.tick(4);

        JsonObject terminal = registry.status("engage-1").terminal();
        assertEquals(ActionRegistry.State.TERMINAL, registry.status("engage-1").state());
        assertEquals("completed", terminal.get("classification").getAsString());
        assertEquals("killed", terminal.get("reason").getAsString());
        assertEquals("target-1", terminal.get("target_id").getAsString());
        assertTrue(terminal.get("damage_observed").getAsBoolean());
        assertEquals(2, terminal.get("attacks").getAsInt());
        assertTrue(harness.clears > 0);
    }

    @Test
    void lowHealthDisengagesWithoutSwinging() {
        Harness harness = new Harness();
        harness.bodyHealth = 5.0;
        ActionRegistry registry = new ActionRegistry();
        BotEventStream events = new BotEventStream();
        ActionRuntime runtime = new ActionRuntime(
            new FakePlayerActionOwner(), harness, registry, events
        );
        runtime.submit("Bot", "engage-2", "ENGAGE_ENTITY", OwnerPriority.ACTION, 0);
        runtime.attachExecutor("engage-2", new EngageExecutor(
            "Bot", "engage-2", "nearest_hostile", 2.0, 2, 16, 6.0,
            openWorld(), harness, harness, harness, harness, events::emit, runtime, 20
        ));

        runtime.tick(1);

        JsonObject terminal = registry.status("engage-2").terminal();
        assertEquals("disengaged_low_health", terminal.get("reason").getAsString());
        assertEquals(0, harness.attacks);
    }
}
