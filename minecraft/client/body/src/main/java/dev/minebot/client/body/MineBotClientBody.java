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
            startObjective(run, rawObjective);
            // The join callback can run before the first authoritative player
            // position arrives. Coordinates belong in the periodic state
            // sample below; logging them here would create false evidence.
            LOGGER.info("MINEBOT_PHASE_A objective_started objective={}", rawObjective);
        } catch (IllegalArgumentException error) {
            activeRun = null;
            LOGGER.error("MINEBOT_PHASE_A failure=unsupported_objective objective={} reason={}", rawObjective, error.getMessage());
        } catch (RuntimeException error) {
            activeRun = null;
            LOGGER.error("MINEBOT_PHASE_A failure=objective_start_failed objective={} reason={}", rawObjective, error.toString());
        }
    }

    private static void startObjective(ProbeRun run, String rawObjective) {
        IBaritone baritone = run.baritone();
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
            run.startMine(normalizeBlockName(parts[1]));
            return;
        }
        if (parts.length == 5 && parts[0].equals("mineat") && !parts[1].isBlank()) {
            int x = parseCoordinate(parts[2], "x");
            int y = parseCoordinate(parts[3], "y");
            int z = parseCoordinate(parts[4], "z");
            run.prepareDirectedMine(normalizeBlockName(parts[1]), x, y, z);
            return;
        }
        if (parts.length == 2 && parts[0].equals("cancel")) {
            throw new IllegalArgumentException("cancel_requires_active_objective");
        }
        throw new IllegalArgumentException("expected goto:x:y:z, mine:block_name, or mineat:block_name:x:y:z");
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
        if (run.hasDirectedMine() && !run.mineStarted()
            && !run.baritone().getCustomGoalProcess().isActive()
            && !run.baritone().getPathingBehavior().isPathing()
            && client.player.distanceToSqr(run.mineX() + 0.5, run.mineY(), run.mineZ() + 0.5) <= 4.0) {
            run.startMine(run.mineTarget());
            LOGGER.info("MINEBOT_PHASE_A directed_mine_started objective={} target={} elapsed_ms={}",
                run.objective(), run.mineTarget(), run.elapsedMs());
        }
        if (run.mineStarted() && run.targetItemCount(client) > 0 && !run.collectionLogged()) {
            run.markCollectionLogged();
            LOGGER.info("MINEBOT_PHASE_A collection_reached objective={} target={} count={} elapsed_ms={}",
                run.objective(), run.mineTarget(), run.targetItemCount(client), run.elapsedMs());
        }
    }

    private static final class ProbeRun implements AbstractGameEventListener {
        private final IBaritone baritone;
        private final String objective;
        private final long startedNanos = System.nanoTime();
        private final int cancelAfterTicks = Integer.getInteger("minebot.client.cancel_after_ticks", 0);
        private String mineTarget;
        private int mineX;
        private int mineY;
        private int mineZ;
        private boolean directedMine;
        private boolean mineStarted;
        private boolean collectionLogged;
        private boolean cancelSent;
        private boolean cancelEventLogged;

        private ProbeRun(IBaritone baritone, String objective) {
            this.baritone = baritone;
            this.objective = objective;
        }

        private void startMine(String target) {
            mineTarget = target;
            mineStarted = true;
            baritone.getMineProcess().mineByName(1, target);
        }

        private void prepareDirectedMine(String target, int x, int y, int z) {
            mineTarget = target;
            mineX = x;
            mineY = y - 1;
            mineZ = z;
            directedMine = true;
            baritone.getCustomGoalProcess().setGoalAndPath(new GoalBlock(mineX, mineY, mineZ));
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
            if (mineTarget == null) {
                return -1;
            }
            int count = 0;
            for (int slot = 0; slot < client.player.getInventory().getContainerSize(); slot++) {
                ItemStack stack = client.player.getInventory().getItem(slot);
                if (!stack.isEmpty() && BuiltInRegistries.ITEM.getKey(stack.getItem()).getPath().equals(mineTarget)) {
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
        private String mineTarget() { return mineTarget; }
        private boolean hasDirectedMine() { return directedMine; }
        private int mineX() { return mineX; }
        private int mineY() { return mineY; }
        private int mineZ() { return mineZ; }
        private boolean mineStarted() { return mineStarted; }
        private boolean collectionLogged() { return collectionLogged; }
        private void markCollectionLogged() { collectionLogged = true; }
        private int cancelAfterTicks() { return cancelAfterTicks; }
        private boolean cancelSent() { return cancelSent; }
        private void markCancelSent() { cancelSent = true; }
    }
}
