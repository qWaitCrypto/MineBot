package dev.minebot.body.action;

import com.google.gson.JsonObject;
import dev.minebot.body.control.FakePlayerActionOwner;
import dev.minebot.body.control.OwnerPriority;
import dev.minebot.body.event.BotEventStream;
import dev.minebot.body.nav.MovementControls;
import dev.minebot.body.nav.WorldView;
import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

final class SurvivalReflexControllerTest {
    private static final class FakeEnvironment
        implements SurvivalReflexController.Environment, MovementControls, BotControls {
        SurvivalReflexController.Position position =
            new SurvivalReflexController.Position(0.5, 64.0, 0.5);
        SurvivalReflexController.Kind hazard = SurvivalReflexController.Kind.LAVA;
        SurvivalReflexController.Target target = new SurvivalReflexController.Target(
            new SurvivalReflexController.Position(4.5, 64.0, 0.5), true
        );
        double lookX;
        double lookZ;
        boolean moving;
        int clears;

        @Override
        public SurvivalReflexController.Position position(String botName) {
            return position;
        }

        @Override
        public SurvivalReflexController.Kind detectHazard(
            String botName,
            SurvivalReflexController.Position ignored
        ) {
            return hazard;
        }

        @Override
        public boolean hazardPresent(
            String botName,
            SurvivalReflexController.Kind kind,
            SurvivalReflexController.Position ignored
        ) {
            return hazard == kind;
        }

        @Override
        public SurvivalReflexController.Target findEscapeTarget(
            String botName,
            SurvivalReflexController.Kind kind,
            SurvivalReflexController.Position ignored,
            boolean dryOnly
        ) {
            return target;
        }

        @Override
        public boolean isDryStand(
            String botName,
            SurvivalReflexController.Position ignored
        ) {
            return true;
        }

        @Override
        public WorldView world(String botName) {
            return (x, y, z) -> {
                if (y == 63) {
                    return WorldView.NodeKind.SOLID;
                }
                if (y == 64 || y == 65) {
                    return WorldView.NodeKind.PASSABLE;
                }
                return WorldView.NodeKind.UNLOADED;
            };
        }

        @Override
        public void lookAt(String botName, double x, double y, double z) {
            lookX = x;
            lookZ = z;
        }

        @Override
        public void moveForward(String botName) {
            moving = true;
        }

        @Override
        public void stopMovement(String botName) {
            moving = false;
        }

        @Override public void sprint(String botName) { }

        @Override public void jumpOnce(String botName) { }

        @Override
        public void clearAll(String botName) {
            moving = false;
            clears++;
        }

        void physics() {
            if (!moving) {
                return;
            }
            double dx = lookX - position.x();
            double dz = lookZ - position.z();
            double norm = Math.sqrt(dx * dx + dz * dz);
            if (norm > 0.01) {
                double step = Math.min(0.45, norm);
                position = new SurvivalReflexController.Position(
                    position.x() + dx / norm * step,
                    position.y(),
                    position.z() + dz / norm * step
                );
            }
            if (position.x() >= 2.5) {
                hazard = null;
            }
        }
    }

    private record Harness(
        FakeEnvironment environment,
        ActionRegistry registry,
        ActionRuntime runtime,
        BotEventStream events,
        SurvivalReflexController controller
    ) {
        static Harness create() {
            FakeEnvironment environment = new FakeEnvironment();
            ActionRegistry registry = new ActionRegistry();
            BotEventStream events = new BotEventStream();
            ActionRuntime runtime = new ActionRuntime(
                new FakePlayerActionOwner(), environment, registry, events
            );
            SurvivalReflexController controller = new SurvivalReflexController(
                runtime, environment, environment, events::emit
            );
            controller.watch("Bot");
            return new Harness(environment, registry, runtime, events, controller);
        }

        void tick(int serverTick) {
            controller.tick(serverTick);
            runtime.tick(serverTick);
            environment.physics();
        }
    }

    @Test
    void lavaReflexPreemptsMovesEscapesAndReleasesOwnership() {
        Harness harness = Harness.create();
        harness.runtime.submit("Bot", "navigate-1", "NAVIGATE", OwnerPriority.ACTION, 0);

        harness.controller.tick(1);

        assertEquals("lavaReflex", harness.controller.activeOwnerName("Bot"));
        JsonObject preempted = harness.registry.status("navigate-1").terminal();
        assertEquals("preempted", preempted.get("classification").getAsString());
        assertEquals("preempted", preempted.get("reason").getAsString());
        assertTrue(preempted.get("paused").getAsBoolean());

        for (int tick = 1; tick <= 80 && harness.controller.activeOwnerName("Bot") != null; tick++) {
            harness.runtime.tick(tick);
            harness.environment.physics();
            harness.controller.tick(tick + 1);
        }

        assertNull(harness.controller.activeOwnerName("Bot"));
        assertNull(harness.runtime.currentOwner("Bot"));
        assertNull(harness.controller.hazardUnresolved("Bot"));
        assertTrue(harness.environment.position.x() >= 2.5);

        List<BotEventStream.Event> emitted = harness.events.replay("Bot", 0).events();
        assertTrue(emitted.stream().anyMatch(event -> event.name().equals("ownerPreempted")));
        BotEventStream.Event completed = emitted.stream()
            .filter(event -> event.name().equals("reflexCompleted"))
            .findFirst()
            .orElseThrow();
        assertEquals("lava", completed.data().get("kind").getAsString());
        assertTrue(completed.data().get("escaped_hazard").getAsBoolean());
        assertTrue(completed.data().get("final_is_dry_stand").getAsBoolean());
    }

    @Test
    void failedReflexLatchesTheHazardAndBlocksOrdinaryActions() {
        Harness harness = Harness.create();
        harness.environment.target = null;

        harness.controller.tick(10);

        JsonObject unresolved = harness.controller.hazardUnresolved("Bot");
        assertEquals("lava", unresolved.get("kind").getAsString());
        assertTrue(unresolved.get("recovery_target").isJsonNull());
        assertFalse(harness.controller.actionAllowed("Bot", false));
        assertTrue(harness.controller.actionAllowed("Bot", true));
        assertNull(harness.runtime.currentOwner("Bot"));

        BotEventStream.Event completed = harness.events.replay("Bot", 0).events().stream()
            .filter(event -> event.name().equals("reflexCompleted"))
            .findFirst()
            .orElseThrow();
        assertFalse(completed.data().get("escaped_hazard").getAsBoolean());
        assertEquals(
            "escape_target_unavailable",
            completed.data().get("reason").getAsString()
        );

        harness.environment.hazard = null;
        harness.controller.tick(11);
        assertNull(harness.controller.hazardUnresolved("Bot"));
        assertTrue(harness.controller.actionAllowed("Bot", false));
    }

    @Test
    void unwatchPreventsCompositeFromStartingASecondReflexOwner() {
        Harness harness = Harness.create();
        harness.controller.unwatch("Bot");

        harness.tick(1);

        assertNull(harness.controller.activeOwnerName("Bot"));
        assertTrue(harness.events.replay("Bot", 0).events().isEmpty());
    }
}
