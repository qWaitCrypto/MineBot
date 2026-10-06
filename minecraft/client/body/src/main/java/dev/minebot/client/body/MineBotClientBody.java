package dev.minebot.client.body;

import baritone.api.BaritoneAPI;
import baritone.api.IBaritone;
import baritone.api.event.events.PathEvent;
import baritone.api.event.listener.AbstractGameEventListener;
import baritone.api.pathing.goals.GoalBlock;
import net.fabricmc.api.ClientModInitializer;
import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientTickEvents;
import net.fabricmc.fabric.api.client.networking.v1.ClientPlayConnectionEvents;
import net.minecraft.client.Minecraft;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.world.item.ItemStack;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.Locale;

/**
 * A deliberately small real-client substrate probe.
 *
 * This class owns no MineBot Body contract and no replacement navigation. It
 * starts one upstream Baritone process from an explicit launch objective and
 * emits facts needed by the Phase A evidence harness.
 */
public final class MineBotClientBody implements ClientModInitializer {
    public static final String MOD_ID = "minebot-client-body";
    private static final Logger LOGGER = LoggerFactory.getLogger(MOD_ID);

    private static ProbeRun activeRun;
    private static int tickCount;

    @Override
    public void onInitializeClient() {
        ClientPlayConnectionEvents.JOIN.register((handler, sender, client) ->
            client.execute(() -> startRun(client))
        );
        ClientPlayConnectionEvents.DISCONNECT.register((handler, client) -> {
            ProbeRun run = activeRun;
            activeRun = null;
            tickCount = 0;
            if (run != null) {
                LOGGER.info("MINEBOT_PHASE_A disconnected objective={} elapsed_ms={}", run.objective(), run.elapsedMs());
            }
        });
        ClientTickEvents.END_CLIENT_TICK.register(MineBotClientBody::tick);
        LOGGER.info("MINEBOT_PHASE_A client_body_loaded minecraft=26.1.2 baritone=1.18.0");
    }

    private static void startRun(Minecraft client) {
        if (client.player == null) {
            LOGGER.warn("MINEBOT_PHASE_A failure=player_unavailable");
            return;
        }
        String rawObjective = System.getProperty("minebot.client.objective", "").trim();
        if (rawObjective.isEmpty()) {
            LOGGER.info("MINEBOT_PHASE_A ready objective=none");
            return;
        }
        IBaritone baritone = BaritoneAPI.getProvider().getBaritoneForMinecraft(client);
        if (baritone == null) {
            LOGGER.error("MINEBOT_PHASE_A failure=baritone_context_unavailable");
            return;
        }
        ProbeRun run = new ProbeRun(baritone, rawObjective);
        activeRun = run;
        baritone.getGameEventHandler().registerEventListener(run);
        try {
            startObjective(baritone, rawObjective);
            LOGGER.info("MINEBOT_PHASE_A objective_started objective={} x={} y={} z={}",
                rawObjective, client.player.getX(), client.player.getY(), client.player.getZ());
        } catch (IllegalArgumentException error) {
            activeRun = null;
            LOGGER.error("MINEBOT_PHASE_A failure=unsupported_objective objective={} reason={}", rawObjective, error.getMessage());
        } catch (RuntimeException error) {
            activeRun = null;
            LOGGER.error("MINEBOT_PHASE_A failure=objective_start_failed objective={} reason={}", rawObjective, error.toString());
        }
    }

    private static void startObjective(IBaritone baritone, String rawObjective) {
        String[] parts = rawObjective.split(":", -1);
        if (parts.length == 4 && (parts[0].equals("goto") || parts[0].equals("navigate"))) {
            baritone.getCustomGoalProcess().setGoalAndPath(new GoalBlock(
                parseCoordinate(parts[1], "x"),
                parseCoordinate(parts[2], "y"),
                parseCoordinate(parts[3], "z")
            ));
            return;
        }
        if (parts.length == 2 && parts[0].equals("mine") && !parts[1].isBlank()) {
            baritone.getMineProcess().mineByName(1, normalizeBlockName(parts[1]));
            return;
        }
        if (parts.length == 2 && parts[0].equals("cancel")) {
            throw new IllegalArgumentException("cancel_requires_active_objective");
        }
        throw new IllegalArgumentException("expected goto:x:y:z or mine:block_name");
    }

    private static String normalizeBlockName(String name) {
        String normalized = name.toLowerCase(Locale.ROOT).trim();
        return normalized.startsWith("minecraft:") ? normalized.substring("minecraft:".length()) : normalized;
    }

    private static int parseCoordinate(String value, String axis) {
        try {
            return Integer.parseInt(value);
        } catch (NumberFormatException error) {
            throw new IllegalArgumentException("invalid_" + axis + "=" + value, error);
        }
    }

    private static void tick(Minecraft client) {
        ProbeRun run = activeRun;
        if (run == null || client.player == null) {
            return;
        }
        tickCount++;
        if (tickCount % 20 == 0) {
            LOGGER.info("MINEBOT_PHASE_A state objective={} active={} pathing={} x={} y={} z={} target_count={} elapsed_ms={}",
                run.objective(), run.baritone().getCustomGoalProcess().isActive(),
                run.baritone().getPathingBehavior().isPathing(),
                client.player.getX(), client.player.getY(), client.player.getZ(),
                run.targetItemCount(client), run.elapsedMs());
        }
        int cancelAfterTicks = run.cancelAfterTicks();
        if (cancelAfterTicks > 0 && tickCount >= cancelAfterTicks && !run.cancelSent()) {
            boolean canceled = run.baritone().getPathingBehavior().cancelEverything();
            run.markCancelSent();
            LOGGER.info("MINEBOT_PHASE_A canceled objective={} accepted={} elapsed_ms={}",
                run.objective(), canceled, run.elapsedMs());
        }
    }

    private static final class ProbeRun implements AbstractGameEventListener {
        private final IBaritone baritone;
        private final String objective;
        private final long startedNanos = System.nanoTime();
        private final int cancelAfterTicks = Integer.getInteger("minebot.client.cancel_after_ticks", 0);
        private boolean cancelSent;
        private boolean cancelEventLogged;

        private ProbeRun(IBaritone baritone, String objective) {
            this.baritone = baritone;
            this.objective = objective;
        }

        @Override
        public void onPathEvent(PathEvent event) {
            if (event == PathEvent.CANCELED && cancelEventLogged) {
                return;
            }
            cancelEventLogged = event == PathEvent.CANCELED;
            LOGGER.info("MINEBOT_PHASE_A path_event objective={} event={} elapsed_ms={}", objective, event, elapsedMs());
        }

        private int targetItemCount(Minecraft client) {
            if (!objective.startsWith("mine:")) {
                return -1;
            }
            String target = normalizeBlockName(objective.substring("mine:".length()));
            int count = 0;
            for (int slot = 0; slot < client.player.getInventory().getContainerSize(); slot++) {
                ItemStack stack = client.player.getInventory().getItem(slot);
                if (!stack.isEmpty() && BuiltInRegistries.ITEM.getKey(stack.getItem()).getPath().equals(target)) {
                    count += stack.getCount();
                }
            }
            return count;
        }

        private long elapsedMs() {
            return (System.nanoTime() - startedNanos) / 1_000_000L;
        }

        private IBaritone baritone() { return baritone; }
        private String objective() { return objective; }
        private int cancelAfterTicks() { return cancelAfterTicks; }
        private boolean cancelSent() { return cancelSent; }
        private void markCancelSent() { cancelSent = true; }
    }
}
