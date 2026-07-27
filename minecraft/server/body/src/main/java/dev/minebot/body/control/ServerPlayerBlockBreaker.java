package dev.minebot.body.control;

import dev.minebot.body.action.ExactBlockBreaker;
import net.minecraft.core.BlockPos;
import net.minecraft.core.Direction;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.network.protocol.game.ServerboundPlayerActionPacket;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.world.level.block.state.BlockState;

import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * Drives Minecraft's own ServerPlayer block-breaking path for one exact
 * coordinate. START performs the same reach/protection/tool checks as a real
 * client packet; completion uses ServerPlayerGameMode so tool durability,
 * drops, enchantments, and block callbacks remain authoritative.
 */
public final class ServerPlayerBlockBreaker implements ExactBlockBreaker {
    private record Session(BlockPos pos, String expectedBlockId, int startedTick) {
    }

    private record Target(ServerPlayer player, ServerLevel level, BlockState state) {
    }

    private final MinecraftServer server;
    private final Map<String, Session> sessions = new HashMap<>();
    private final AtomicInteger sequence = new AtomicInteger();

    public ServerPlayerBlockBreaker(MinecraftServer server) {
        this.server = server;
    }

    @Override
    public Outcome begin(
        String botName,
        int x,
        int y,
        int z,
        String expectedBlockId,
        int serverTick
    ) {
        abort(botName);
        BlockPos pos = new BlockPos(x, y, z);
        Target target = target(botName, pos, expectedBlockId);
        if (target == null) {
            return Outcome.failed(targetFailure(botName, pos, expectedBlockId));
        }
        String restriction = restriction(target.player(), target.level(), pos);
        if (restriction != null) {
            return Outcome.failed(restriction);
        }

        Session session = new Session(pos.immutable(), expectedBlockId, serverTick);
        sessions.put(botName, session);
        target.player().gameMode.handleBlockBreakAction(
            pos,
            ServerboundPlayerActionPacket.Action.START_DESTROY_BLOCK,
            Direction.UP,
            target.level().getMaxY(),
            sequence.incrementAndGet()
        );
        if (!blockId(target.level(), pos).equals(expectedBlockId)) {
            sessions.remove(botName);
            return Outcome.complete();
        }
        return Outcome.working();
    }

    @Override
    public Outcome tick(String botName, int serverTick) {
        Session session = sessions.get(botName);
        if (session == null) {
            return Outcome.failed("exact_break_not_started");
        }
        Target target = target(botName, session.pos(), session.expectedBlockId());
        if (target == null) {
            String observed = observedBlockId(botName, session.pos());
            if (observed != null && !observed.equals(session.expectedBlockId())) {
                sessions.remove(botName);
                return Outcome.complete();
            }
            abort(botName);
            return Outcome.failed(observed == null ? "body_missing_or_target_unloaded" : "target_changed");
        }
        String restriction = restriction(target.player(), target.level(), session.pos());
        if (restriction != null) {
            abort(botName);
            return Outcome.failed(restriction);
        }

        float progressPerTick = target.state().getDestroyProgress(
            target.player(), target.level(), session.pos()
        );
        int elapsedTicks = Math.max(1, serverTick - session.startedTick() + 1);
        if (progressPerTick > 0.0F && progressPerTick * elapsedTicks >= 1.0F) {
            boolean destroyed = target.player().gameMode.destroyBlock(session.pos());
            clearVanillaProgress(target.player(), target.level(), session.pos());
            sessions.remove(botName);
            if (destroyed && !blockId(target.level(), session.pos()).equals(session.expectedBlockId())) {
                return Outcome.complete();
            }
            return Outcome.failed("server_player_destroy_rejected");
        }
        return Outcome.working();
    }

    @Override
    public void abort(String botName) {
        Session session = sessions.remove(botName);
        if (session == null) {
            return;
        }
        ServerPlayer player = server.getPlayerList().getPlayerByName(botName);
        if (player == null || player.isRemoved() || !(player.level() instanceof ServerLevel level)) {
            return;
        }
        player.gameMode.handleBlockBreakAction(
            session.pos(),
            ServerboundPlayerActionPacket.Action.ABORT_DESTROY_BLOCK,
            Direction.UP,
            level.getMaxY(),
            sequence.incrementAndGet()
        );
        level.destroyBlockProgress(player.getId(), session.pos(), -1);
    }

    private Target target(String botName, BlockPos pos, String expectedBlockId) {
        ServerPlayer player = server.getPlayerList().getPlayerByName(botName);
        if (player == null || player.isRemoved() || !(player.level() instanceof ServerLevel level)) {
            return null;
        }
        BlockState state = level.getBlockState(pos);
        if (!blockId(state).equals(expectedBlockId)) {
            return null;
        }
        return new Target(player, level, state);
    }

    private String targetFailure(String botName, BlockPos pos, String expectedBlockId) {
        String observed = observedBlockId(botName, pos);
        if (observed == null) {
            return "body_missing_or_target_unloaded";
        }
        return observed.equals(expectedBlockId) ? "target_unavailable" : "target_changed";
    }

    private String observedBlockId(String botName, BlockPos pos) {
        ServerPlayer player = server.getPlayerList().getPlayerByName(botName);
        if (player == null || player.isRemoved() || !(player.level() instanceof ServerLevel level)) {
            return null;
        }
        return blockId(level, pos);
    }

    private String restriction(ServerPlayer player, ServerLevel level, BlockPos pos) {
        if (!player.isWithinBlockInteractionRange(pos, 1.0)) {
            return "target_out_of_reach";
        }
        if (server.isUnderSpawnProtection(level, pos, player)) {
            return "spawn_protected";
        }
        if (!level.mayInteract(player, pos)) {
            return "server_interaction_denied";
        }
        if (player.blockActionRestricted(level, pos, player.gameMode())) {
            return "player_action_restricted";
        }
        return null;
    }

    private void clearVanillaProgress(ServerPlayer player, ServerLevel level, BlockPos pos) {
        player.gameMode.handleBlockBreakAction(
            pos,
            ServerboundPlayerActionPacket.Action.ABORT_DESTROY_BLOCK,
            Direction.UP,
            level.getMaxY(),
            sequence.incrementAndGet()
        );
        level.destroyBlockProgress(player.getId(), pos, -1);
    }

    private static String blockId(ServerLevel level, BlockPos pos) {
        return blockId(level.getBlockState(pos));
    }

    private static String blockId(BlockState state) {
        return BuiltInRegistries.BLOCK.getKey(state.getBlock()).toString();
    }
}
