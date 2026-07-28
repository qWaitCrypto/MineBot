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
        int continuousJumps;

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
        public dev.minebot.body.nav.Goal findDryEgressGoal(
            String botName,
            SurvivalReflexController.Position ignored
        ) {
            SurvivalReflexController.Position value = target.position();
            return new dev.minebot.body.nav.Goal.Near(
                value.blockX(), value.blockY(), value.blockZ(), 0.5
            );
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

        @Override public void jumpContinuous(String botName) { continuousJumps++; }

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

    private static final class WaterEnvironment
        implements SurvivalReflexController.Environment, MovementControls, BotControls {
        SurvivalReflexController.Position position =
            new SurvivalReflexController.Position(0.5, 64.0, 0.5);
        double lookX;
        double lookZ;
        boolean moving;
        int continuousJumps;

        @Override
        public SurvivalReflexController.Position position(String botName) {
            return position;
        }

        @Override
        public SurvivalReflexController.Kind detectHazard(
            String botName,
            SurvivalReflexController.Position ignored
        ) {
            return isDry(position) ? null : SurvivalReflexController.Kind.WATER;
        }

        @Override
        public boolean hazardPresent(
            String botName,
            SurvivalReflexController.Kind kind,
            SurvivalReflexController.Position ignored
        ) {
            return kind == SurvivalReflexController.Kind.WATER && !isDry(position);
        }

        @Override
        public SurvivalReflexController.Target findEscapeTarget(
            String botName,
            SurvivalReflexController.Kind kind,
            SurvivalReflexController.Position ignored,
            boolean dryOnly
        ) {
            if (dryOnly) {
                return null;
            }
            return new SurvivalReflexController.Target(position, false);
        }

        @Override
        public dev.minebot.body.nav.Goal findDryEgressGoal(
            String botName,
            SurvivalReflexController.Position ignored
        ) {
            return new dev.minebot.body.nav.Goal.Near(12, 64, 0, 0.5);
        }

        @Override
        public boolean isDryStand(
            String botName,
            SurvivalReflexController.Position ignored
        ) {
            return isDry(position);
        }

        @Override
        public WorldView world(String botName) {
            return (x, y, z) -> {
                if (Math.abs(x) > 20 || Math.abs(z) > 20) {
                    return WorldView.NodeKind.UNLOADED;
                }
                if (y == 63) {
                    return WorldView.NodeKind.SOLID;
                }
                if (y == 64 && x < 12) {
                    return WorldView.NodeKind.LIQUID;
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

        @Override public void jumpOnce(String botName) { }

        @Override public void jumpContinuous(String botName) { continuousJumps++; }

        @Override public void sprint(String botName) { }

        @Override public void clearAll(String botName) { moving = false; }

        void physics() {
            if (!moving) {
                return;
            }
            double dx = lookX - position.x();
            double dz = lookZ - position.z();
            double norm = Math.sqrt(dx * dx + dz * dz);
            if (norm <= 0.01) {
                return;
            }
            double step = Math.min(0.45, norm);
            position = new SurvivalReflexController.Position(
                position.x() + dx / norm * step,
                position.y(),
                position.z() + dz / norm * step
            );
        }

        private static boolean isDry(SurvivalReflexController.Position position) {
            return position.x() >= 12.0;
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

    @Test
    void waterReflexFindsAndReachesDryGroundBeyondTheLegacyScanRadius() {
        WaterEnvironment environment = new WaterEnvironment();
        ActionRegistry registry = new ActionRegistry();
        BotEventStream events = new BotEventStream();
        ActionRuntime runtime = new ActionRuntime(
            new FakePlayerActionOwner(), environment, registry, events
        );
        SurvivalReflexController controller = new SurvivalReflexController(
            runtime, environment, environment, events::emit
        );
        controller.watch("Bot");

        for (int tick = 1; tick <= 100; tick++) {
            controller.tick(tick);
            runtime.tick(tick);
            environment.physics();
            if (environment.isDryStand("Bot", environment.position)) {
                controller.tick(tick + 1);
                runtime.tick(tick + 1);
                break;
            }
        }

        assertTrue(environment.position.x() >= 12.0, environment.position.toString());
        assertTrue(environment.continuousJumps > 0);
        BotEventStream.Event retargeted = events.replay("Bot", 0).events().stream()
            .filter(event -> event.name().equals("reflexRetargeted"))
            .findFirst()
            .orElseThrow();
        assertEquals("reachable_dry_stand", retargeted.data().get("goal").getAsString());
        BotEventStream.Event completed = events.replay("Bot", 0).events().stream()
            .filter(event -> event.name().equals("reflexCompleted"))
            .findFirst()
            .orElseThrow();
        assertTrue(completed.data().get("escaped_hazard").getAsBoolean());
        assertTrue(completed.data().get("final_is_dry_stand").getAsBoolean());
        assertTrue(completed.data().get("target_is_dry_stand").getAsBoolean());
        assertNull(runtime.currentOwner("Bot"));
    }
}
