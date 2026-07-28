package dev.minebot.body.action;

import dev.minebot.body.nav.MinecraftWorldView;
import dev.minebot.body.nav.WorldView;
import net.minecraft.core.BlockPos;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.world.level.chunk.LevelChunk;
import net.minecraft.world.level.material.Fluids;

/** Live loaded-world facts and generic escape-target selection for reflexes. */
public final class MinecraftSurvivalEnvironment implements SurvivalReflexController.Environment {
    public static final int WATER_AIR_THRESHOLD = 80;
    public static final int ESCAPE_RADIUS = 8;
    private static final int[] Y_OFFSETS = {0, 1, -1, 2, -2, 3, -3};

    private final MinecraftServer server;

    public MinecraftSurvivalEnvironment(MinecraftServer server) {
        this.server = server;
    }

    @Override
    public SurvivalReflexController.Position position(String botName) {
        ServerPlayer player = player(botName);
        if (player == null) {
            return null;
        }
        return new SurvivalReflexController.Position(
            player.getX(), player.getY(), player.getZ()
        );
    }

    @Override
    public SurvivalReflexController.Kind detectHazard(
        String botName,
        SurvivalReflexController.Position position
    ) {
        ServerPlayer player = player(botName);
        if (player == null || !(player.level() instanceof ServerLevel level)) {
            return null;
        }
        if (lavaNear(level, position.blockX(), position.blockY(), position.blockZ())) {
            return SurvivalReflexController.Kind.LAVA;
        }
        if (player.isOnFire()) {
            return SurvivalReflexController.Kind.FIRE;
        }
        if (waterRisk(player, level, position)) {
            return SurvivalReflexController.Kind.WATER;
        }
        return null;
    }

    @Override
    public boolean hazardPresent(
        String botName,
        SurvivalReflexController.Kind kind,
        SurvivalReflexController.Position position
    ) {
        ServerPlayer player = player(botName);
        if (player == null || !(player.level() instanceof ServerLevel level)) {
            return false;
        }
        return switch (kind) {
            case LAVA -> lavaNear(level, position.blockX(), position.blockY(), position.blockZ());
            case FIRE -> player.isOnFire();
            case WATER -> waterRisk(player, level, position);
        };
    }

    @Override
    public SurvivalReflexController.Target findEscapeTarget(
        String botName,
        SurvivalReflexController.Kind kind,
        SurvivalReflexController.Position position,
        boolean dryOnly
    ) {
        ServerPlayer player = player(botName);
        if (player == null || !(player.level() instanceof ServerLevel level)) {
            return null;
        }
        WorldView world = new MinecraftWorldView(level);
        SurvivalReflexController.Target dry = findDryStand(world, level, kind, position);
        if (dry != null || dryOnly || kind != SurvivalReflexController.Kind.WATER) {
            return dry;
        }
        return findBreathableSurface(world, level, position);
    }

    @Override
    public boolean isDryStand(
        String botName,
        SurvivalReflexController.Position position
    ) {
        ServerPlayer player = player(botName);
        if (player == null || !(player.level() instanceof ServerLevel level)) {
            return false;
        }
        return isDryStand(
            new MinecraftWorldView(level),
            position.blockX(), position.blockY(), position.blockZ()
        );
    }

    @Override
    public WorldView world(String botName) {
        ServerPlayer player = player(botName);
        if (player == null || !(player.level() instanceof ServerLevel level)) {
            return null;
        }
        return new MinecraftWorldView(level);
    }

    private SurvivalReflexController.Target findDryStand(
        WorldView world,
        ServerLevel level,
        SurvivalReflexController.Kind kind,
        SurvivalReflexController.Position origin
    ) {
        int baseX = origin.blockX();
        int baseY = origin.blockY();
        int baseZ = origin.blockZ();
        for (int radius = 1; radius <= ESCAPE_RADIUS; radius++) {
            for (int yOffset : Y_OFFSETS) {
                int y = baseY + yOffset;
                for (int delta = -radius; delta <= radius; delta++) {
                    SurvivalReflexController.Target target = dryCandidate(
                        world, level, kind, baseX + radius, y, baseZ + delta
                    );
                    if (target != null) {
                        return target;
                    }
                    target = dryCandidate(
                        world, level, kind, baseX - radius, y, baseZ + delta
                    );
                    if (target != null) {
                        return target;
                    }
                }
                for (int delta = -radius + 1; delta < radius; delta++) {
                    SurvivalReflexController.Target target = dryCandidate(
                        world, level, kind, baseX + delta, y, baseZ + radius
                    );
                    if (target != null) {
                        return target;
                    }
                    target = dryCandidate(
                        world, level, kind, baseX + delta, y, baseZ - radius
                    );
                    if (target != null) {
                        return target;
                    }
                }
            }
        }
        return null;
    }

    private static SurvivalReflexController.Target dryCandidate(
        WorldView world,
        ServerLevel level,
        SurvivalReflexController.Kind kind,
        int x,
        int y,
        int z
    ) {
        if (!isDryStand(world, x, y, z)) {
            return null;
        }
        if (kind == SurvivalReflexController.Kind.LAVA && lavaNear(level, x, y, z)) {
            return null;
        }
        return new SurvivalReflexController.Target(
            new SurvivalReflexController.Position(x + 0.5, y, z + 0.5),
            true
        );
    }

    private static SurvivalReflexController.Target findBreathableSurface(
        WorldView world,
        ServerLevel level,
        SurvivalReflexController.Position origin
    ) {
        int baseX = origin.blockX();
        int baseY = origin.blockY();
        int baseZ = origin.blockZ();
        int[][] offsets = {{0, 0}, {1, 0}, {-1, 0}, {0, 1}, {0, -1}};
        for (int rise = 0; rise <= ESCAPE_RADIUS; rise++) {
            int y = baseY + rise;
            for (int[] offset : offsets) {
                int x = baseX + offset[0];
                int z = baseZ + offset[1];
                if (isWater(level, x, y, z)
                    && !isWater(level, x, y + 1, z)
                    && world.kindAt(x, y + 1, z) == WorldView.NodeKind.PASSABLE) {
                    return new SurvivalReflexController.Target(
                        new SurvivalReflexController.Position(x + 0.5, y, z + 0.5),
                        false
                    );
                }
            }
        }
        return null;
    }

    private static boolean isDryStand(WorldView world, int x, int y, int z) {
        return world.kindAt(x, y, z) == WorldView.NodeKind.PASSABLE
            && world.kindAt(x, y + 1, z) == WorldView.NodeKind.PASSABLE
            && world.kindAt(x, y - 1, z) == WorldView.NodeKind.SOLID;
    }

    private static boolean waterRisk(
        ServerPlayer player,
        ServerLevel level,
        SurvivalReflexController.Position position
    ) {
        return isWater(level, position.blockX(), position.blockY() + 1, position.blockZ())
            && player.getAirSupply() <= WATER_AIR_THRESHOLD;
    }

    private static boolean lavaNear(ServerLevel level, int x, int y, int z) {
        for (int dx = -1; dx <= 1; dx++) {
            for (int dy = -1; dy <= 0; dy++) {
                for (int dz = -1; dz <= 1; dz++) {
                    if (isLava(level, x + dx, y + dy, z + dz)) {
                        return true;
                    }
                }
            }
        }
        return false;
    }

    private static boolean isWater(ServerLevel level, int x, int y, int z) {
        LevelChunk chunk = level.getChunkSource().getChunkNow(x >> 4, z >> 4);
        if (chunk == null) {
            return false;
        }
        var fluid = chunk.getBlockState(new BlockPos(x, y, z)).getFluidState();
        return fluid.is(Fluids.WATER) || fluid.is(Fluids.FLOWING_WATER);
    }

    private static boolean isLava(ServerLevel level, int x, int y, int z) {
        LevelChunk chunk = level.getChunkSource().getChunkNow(x >> 4, z >> 4);
        if (chunk == null) {
            return false;
        }
        var fluid = chunk.getBlockState(new BlockPos(x, y, z)).getFluidState();
        return fluid.is(Fluids.LAVA) || fluid.is(Fluids.FLOWING_LAVA);
    }

    private ServerPlayer player(String botName) {
        ServerPlayer player = server.getPlayerList().getPlayerByName(botName);
        return player == null || player.isRemoved() ? null : player;
    }
}
