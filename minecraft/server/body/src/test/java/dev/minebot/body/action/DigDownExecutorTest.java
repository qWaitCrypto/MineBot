package dev.minebot.body.action;

import com.google.gson.JsonObject;
import dev.minebot.body.control.FakePlayerActionOwner;
import dev.minebot.body.control.OwnerPriority;
import dev.minebot.body.event.BotEventStream;
import dev.minebot.body.nav.FakeWorld;
import dev.minebot.body.nav.MovementControls;
import dev.minebot.body.nav.NavigateExecutor;
import dev.minebot.body.nav.WorldView;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

final class DigDownExecutorTest {
    private static final String STONE = "minecraft:stone";
    private static final String AIR = "minecraft:air";

    private static final class FakeBody
        implements MovementControls, ExactBlockBreaker, BotControls {
        double x = 0.88;
        double y = 64.0;
        double z = 0.18;
        double lookX;
        double lookZ;
        boolean moving;
        boolean attacking;
        int breakX;
        int breakY;
        int breakZ;
        int breakTicks;
        final List<String> breaks = new ArrayList<>();

        @Override
        public void lookAt(String bot, double tx, double ty, double tz) {
            lookX = tx;
            lookZ = tz;
        }

        @Override
        public void moveForward(String bot) {
            moving = true;
        }

        @Override
        public void stopMovement(String bot) {
            moving = false;
        }

        @Override public void jumpOnce(String bot) {
        }
        @Override public void jumpContinuous(String bot) {
        }
        @Override public void sprint(String bot) {
        }

        @Override
        public Outcome begin(String bot, int x, int y, int z, String blockId, int tick) {
            breakX = x;
            breakY = y;
            breakZ = z;
            breakTicks = 0;
            attacking = true;
            breaks.add(x + "," + y + "," + z + ":" + blockId);
            return Outcome.working();
        }

        @Override
        public Outcome tick(String bot, int tick) {
            breakTicks++;
            return Outcome.working();
        }

        @Override
        public void abort(String bot) {
            attacking = false;
        }

        @Override
        public void clearAll(String bot) {
            moving = false;
            attacking = false;
        }

        void physicsTick(FakeWorld world) {
            if (moving) {
                double dx = lookX - x;
                double dz = lookZ - z;
                double distance = Math.sqrt(dx * dx + dz * dz);
                if (distance > 1.0e-6) {
                    double step = Math.min(0.08, distance);
                    x += dx / distance * step;
                    z += dz / distance * step;
                }
            }
            int feetY = NavigateExecutor.PositionSource.Position.feetBlockY(y);
            while (world.kindAt((int) Math.floor(x), feetY - 1, (int) Math.floor(z))
                != WorldView.NodeKind.SOLID) {
                feetY--;
            }
            if (feetY < NavigateExecutor.PositionSource.Position.feetBlockY(y)) {
                y = feetY;
            }
        }
    }

    private static final class Harness {
        final FakeBody body = new FakeBody();
        final FakeWorld world;
        final Map<String, String> blocks = new HashMap<>();
        final List<MutationGate.Proposal> proposals = new ArrayList<>();
        final MutationGate gate = new MutationGate();
        final ActionRegistry registry = new ActionRegistry();
        final BotEventStream events = new BotEventStream();
        final ActionRuntime runtime = new ActionRuntime(
            new FakePlayerActionOwner(), body, registry, events
        );

        Harness(int floorY) {
            world = new FakeWorld(floorY);
        }

        static String key(int x, int y, int z) {
            return x + "," + y + "," + z;
        }

        void solid(int x, int y, int z) {
            blocks.put(key(x, y, z), STONE);
            world.set(x, y, z, WorldView.NodeKind.SOLID);
        }

        String blockAt(int x, int y, int z) {
            String explicit = blocks.get(key(x, y, z));
            if (explicit != null) {
                return explicit;
            }
            return world.kindAt(x, y, z) == WorldView.NodeKind.SOLID ? STONE : AIR;
        }

        void build(int targetY, int maxSteps, int maxClearFall) {
            runtime.submit("Bot", "dig-1", "DIG_DOWN", OwnerPriority.ACTION, 0);
            runtime.attachExecutor(
                "dig-1",
                new DigDownExecutor(
                    "Bot",
                    "dig-1",
                    targetY,
                    maxSteps,
                    maxClearFall,
                    world,
                    body,
                    body,
                    body,
                    this::blockAt,
                    blockId -> blockId.equals("minecraft:lava") || blockId.equals("minecraft:water"),
                    gate,
                    proposals::add,
                    bot -> new NavigateExecutor.PositionSource.Position(body.x, body.y, body.z),
                    (bot, tick, name, actionId, data) -> events.emit(bot, tick, name, actionId, data),
                    runtime,
                    2_400
                )
            );
        }

        JsonObject run(int ticks, boolean allow) {
            for (int tick = 1; tick <= ticks; tick++) {
                runtime.tick(tick);
                for (MutationGate.Proposal proposal : proposals) {
                    gate.verdict(proposal.proposalId(), allow, allow ? "natural_terrain" : "protected_region");
                }
                if (body.attacking && body.breakTicks >= 2) {
                    blocks.put(key(body.breakX, body.breakY, body.breakZ), AIR);
                    world.set(body.breakX, body.breakY, body.breakZ, WorldView.NodeKind.PASSABLE);
                }
                body.physicsTick(world);
                ActionRegistry.ActionStatus status = registry.status("dig-1");
                if (status.state() == ActionRegistry.State.TERMINAL) {
                    return status.terminal();
                }
            }
            throw new AssertionError("no terminal within " + ticks + " ticks");
        }
    }

    @Test
    void centersBeforeBreakingAndConvergesFromFinalPosition() {
        Harness harness = new Harness(61);
        harness.solid(0, 63, 0);
        harness.solid(0, 62, 0);
        harness.build(62, 4, 1);

        JsonObject terminal = harness.run(500, true);

        assertEquals("completed", terminal.get("classification").getAsString(), terminal.toString());
        assertEquals("dig_down_target_reached", terminal.get("reason").getAsString());
        assertEquals(62, terminal.get("final_feet_y").getAsInt());
        assertEquals(2, terminal.get("steps_completed").getAsInt());
        assertEquals(
            List.of("0,63,0:" + STONE, "0,62,0:" + STONE),
            harness.body.breaks
        );
        assertTrue(Math.abs(harness.body.x - 0.5) <= 0.10);
        assertTrue(Math.abs(harness.body.z - 0.5) <= 0.10);
        assertEquals(2, harness.proposals.size());
    }

    @Test
    void governanceDenialHappensAfterCenteringAndBeforeMutation() {
        Harness harness = new Harness(62);
        harness.solid(0, 63, 0);
        harness.build(63, 1, 1);

        JsonObject terminal = harness.run(300, false);

        assertEquals("failed", terminal.get("classification").getAsString());
        assertEquals("dig_down_denied", terminal.get("reason").getAsString());
        assertEquals("protected_region", terminal.get("governance_reason").getAsString());
        assertEquals(STONE, harness.blocks.get(Harness.key(0, 63, 0)));
        assertTrue(harness.body.breaks.isEmpty());
        assertEquals(1, harness.proposals.size());
    }

    @Test
    void excessiveClearFallRefusesBeforeCenteringOrMutation() {
        Harness harness = new Harness(59);
        harness.solid(0, 63, 0);
        harness.world.set(0, 62, 0, WorldView.NodeKind.PASSABLE);
        harness.world.set(0, 61, 0, WorldView.NodeKind.PASSABLE);
        harness.build(63, 1, 2);

        JsonObject terminal = harness.run(20, true);

        assertEquals("failed", terminal.get("classification").getAsString());
        assertEquals("dig_down_fall_risk", terminal.get("reason").getAsString());
        assertTrue(harness.proposals.isEmpty());
        assertTrue(harness.body.breaks.isEmpty());
        assertFalse(harness.body.moving);
    }
}
