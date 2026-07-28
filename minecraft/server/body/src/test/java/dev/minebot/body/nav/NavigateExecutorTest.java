package dev.minebot.body.nav;

import com.google.gson.JsonObject;
import dev.minebot.body.action.ActionRegistry;
import dev.minebot.body.action.ActionRuntime;
import dev.minebot.body.action.BotControls;
import dev.minebot.body.control.FakePlayerActionOwner;
import dev.minebot.body.control.OwnerPriority;
import dev.minebot.body.event.BotEventStream;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

final class NavigateExecutorTest {
    private static final int FLOOR = 63;
    private static final int STAND = FLOOR + 1;

    /** Fake body: teleports toward the look target a fixed distance per tick. */
    private static final class FakeBody implements MovementControls, BotControls {
        double x = 0.5;
        double y = STAND;
        double z = 0.5;
        double lookX;
        double lookZ;
        boolean moving;
        boolean frozen;
        final List<String> log = new ArrayList<>();

        @Override
        public void lookAt(String bot, double tx, double ty, double tz) {
            lookX = tx;
            lookZ = tz;
        }

        @Override
        public void moveForward(String bot) {
            moving = true;
            log.add("forward");
        }

        @Override
        public void stopMovement(String bot) {
            moving = false;
            log.add("stop");
        }

        @Override
        public void jumpOnce(String bot) {
            log.add("jump");
        }

        @Override
        public void sprint(String bot) {
            log.add("sprint");
        }

        @Override
        public void clearAll(String bot) {
            moving = false;
            log.add("clearAll");
        }

        void physicsTick() {
            if (!moving || frozen) {
                return;
            }
            double dx = lookX - x;
            double dz = lookZ - z;
            double distance = Math.sqrt(dx * dx + dz * dz);
            double step = Math.min(0.25, distance);
            if (distance > 1e-6) {
                x += dx / distance * step;
                z += dz / distance * step;
            }
        }
    }

    private record Harness(
        ActionRuntime runtime,
        ActionRegistry registry,
        FakeBody body,
        BotEventStream events
    ) {
        static Harness create(WorldView world, Goal goal, int timeoutTicks) {
            return create(world, goal, timeoutTicks, PathFollower.WAYPOINT_REACH_DISTANCE);
        }

        static Harness create(
            WorldView world,
            Goal goal,
            int timeoutTicks,
            double finalReachDistance
        ) {
            FakeBody body = new FakeBody();
            ActionRegistry registry = new ActionRegistry();
            BotEventStream events = new BotEventStream();
            ActionRuntime runtime = new ActionRuntime(new FakePlayerActionOwner(), body, registry, events);
            runtime.submit("Bot", "nav-1", "NAVIGATE", OwnerPriority.ACTION, 0);
            NavigateExecutor executor = new NavigateExecutor(
                "Bot",
                "nav-1",
                goal,
                world,
                body,
                bot -> new NavigateExecutor.PositionSource.Position(body.x, body.y, body.z),
                (bot, tick, name, actionId, data) -> events.emit(bot, tick, name, actionId, data),
                runtime,
                timeoutTicks,
                finalReachDistance
            );
            runtime.attachExecutor("nav-1", executor);
            return new Harness(runtime, registry, body, events);
        }

        JsonObject runUntilTerminal(int maxTicks) {
            for (int tick = 1; tick <= maxTicks; tick++) {
                runtime.tick(tick);
                body.physicsTick();
                ActionRegistry.ActionStatus status = registry.status("nav-1");
                if (status.state() == ActionRegistry.State.TERMINAL) {
                    return status.terminal();
                }
            }
            throw new AssertionError("no terminal within " + maxTicks + " ticks");
        }
    }

    @Test
    void completesASimpleWalkWithGoalVerification() {
        Harness harness = Harness.create(new FakeWorld(FLOOR), new Goal.Near(10, STAND, 0, 1.5), 2_400);

        JsonObject terminal = harness.runUntilTerminal(2_000);

        assertEquals("completed", terminal.get("classification").getAsString());
        assertEquals("goal_satisfied", terminal.get("reason").getAsString());
        assertTrue(terminal.get("final_x").getAsDouble() > 8.0);
        assertTrue(harness.body().log.contains("sprint"));
        assertTrue(harness.body().log.contains("clearAll"), "terminal must clear inputs");
    }

    @Test
    void preciseApproachFinishesAtTheSelectedStandCenter() {
        Harness harness = Harness.create(
            new FakeWorld(FLOOR),
            new Goal.Near(10, STAND, 0, 0.5),
            2_400,
            0.1
        );

        JsonObject terminal = harness.runUntilTerminal(2_000);

        assertEquals("completed", terminal.get("classification").getAsString());
        double dx = terminal.get("final_x").getAsDouble() - 10.5;
        double dz = terminal.get("final_z").getAsDouble() - 0.5;
        assertTrue(Math.sqrt(dx * dx + dz * dz) <= 0.1 + 1.0e-6);
        assertEquals(0.1, terminal.get("final_reach_distance").getAsDouble());
    }

    @Test
    void noPathIsATypedFailureNotARetryLoop() {
        FakeWorld world = new FakeWorld(FLOOR);
        // Box the bot in completely.
        for (int x = -2; x <= 2; x++) {
            for (int z = -2; z <= 2; z++) {
                if (x == -2 || x == 2 || z == -2 || z == 2) {
                    world.wall(x, z, STAND + 3);
                }
            }
        }
        Harness harness = Harness.create(world, new Goal.Near(40, STAND, 0, 0.5), 2_400);

        JsonObject terminal = harness.runUntilTerminal(2_000);

        assertEquals("failed", terminal.get("classification").getAsString());
        assertEquals("no_path", terminal.get("reason").getAsString());
    }

    @Test
    void frozenBodyEndsStuckAfterBoundedRecovery() {
        Harness harness = Harness.create(new FakeWorld(FLOOR), new Goal.Near(20, STAND, 0, 0.5), 6_000);
        harness.body().frozen = true;

        JsonObject terminal = harness.runUntilTerminal(5_000);

        assertEquals("failed", terminal.get("classification").getAsString());
        String reason = terminal.get("reason").getAsString();
        assertTrue(reason.equals("stuck") || reason.startsWith("replan_budget_exhausted"), reason);
        assertTrue(terminal.get("replans").getAsInt() <= NavigateExecutor.REPLAN_LIMIT);
    }

    @Test
    void cancellationProducesACanceledTerminalWithCleanInputs() {
        Harness harness = Harness.create(new FakeWorld(FLOOR), new Goal.Near(50, STAND, 0, 0.5), 2_400);
        harness.runtime().tick(1);
        harness.body().physicsTick();
        harness.runtime().requestCancel("nav-1");
        harness.runtime().tick(2);

        ActionRegistry.ActionStatus status = harness.registry().status("nav-1");
        assertEquals(ActionRegistry.State.TERMINAL, status.state());
        assertEquals("canceled", status.terminal().get("classification").getAsString());
        assertTrue(harness.body().log.contains("clearAll"));
    }

    @Test
    void timeoutIsHonest() {
        Harness harness = Harness.create(new FakeWorld(FLOOR), new Goal.Near(400, STAND, 0, 0.5), 40);

        JsonObject terminal = harness.runUntilTerminal(200);

        assertEquals("timeout", terminal.get("classification").getAsString());
    }

    @Test
    void interactGoalWalksToALegalStandAndCompletes() {
        FakeWorld world = new FakeWorld(FLOOR);
        int treeX = 9;
        for (int y = STAND; y <= STAND + 4; y++) {
            world.set(treeX, y, 0, WorldView.NodeKind.SOLID);
        }
        Harness harness = Harness.create(world, new Goal.Interact(treeX, STAND + 1, 0, 4.5), 2_400);

        JsonObject terminal = harness.runUntilTerminal(2_000);

        assertEquals("completed", terminal.get("classification").getAsString());
        double fx = terminal.get("final_x").getAsDouble();
        double distance = Math.abs(fx - (treeX + 0.5));
        assertTrue(distance <= 4.5 + 0.5, "the bot ends within interaction range: " + distance);
        assertTrue(distance >= 0.5, "the bot does not walk inside the trunk");
    }

}
