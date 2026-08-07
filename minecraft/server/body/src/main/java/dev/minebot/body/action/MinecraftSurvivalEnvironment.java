package dev.minebot.body.action;

import dev.minebot.body.nav.Goal;
import dev.minebot.body.nav.AStarPathfinder;
import dev.minebot.body.nav.MinecraftWorldView;
import dev.minebot.body.nav.WorldView;
import net.minecraft.core.BlockPos;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.world.level.block.Blocks;
import net.minecraft.world.level.chunk.LevelChunk;
import net.minecraft.world.level.levelgen.Heightmap;
import net.minecraft.world.level.material.Fluids;

import java.util.ArrayList;
import java.util.List;

/** Live loaded-world facts and generic escape-target selection for reflexes. */
public final class MinecraftSurvivalEnvironment implements SurvivalReflexController.Environment {
    public static final int WATER_AIR_THRESHOLD = 80;
    public static final int ESCAPE_RADIUS = 8;
    public static final int WATER_SHORE_RADIUS = 96;
    private static final int MAX_DRY_CANDIDATES = Goal.MAX_COMPOSITE_MEMBERS;
    private static final int ESCAPE_PATH_NODE_CAP = 8_000;
    private static final int ESCAPE_PATH_UNLOADED_CAP = 16;
    private static final int ESCAPE_PATH_STEP_BUDGET = 2_000;
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
        // Reflex ownership is for an active bodily hazard. Route planning uses
        // a wider lava clearance envelope, but nearby lava alone must not
        // preempt a player whose server-authoritative fluid state is safe.
        if (player.isInLava()) {
            return SurvivalReflexController.Kind.LAVA;
        }
        if (touchesActiveFire(player, level)) {
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
            case LAVA -> player.isInLava();
            case FIRE -> touchesActiveFire(player, level);
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
        WorldView safeWorld = new MinecraftWorldView(level);
        if (kind == SurvivalReflexController.Kind.WATER && !dryOnly) {
            return findBreathableSurface(safeWorld, level, position);
        }
        return findDryStand(
            safeWorld,
            reflexPathWorld(level, needsLavaEgress(level, kind, position)),
            level,
            kind,
            position
        );
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
    public Goal findDryEgressGoal(
        String botName,
        SurvivalReflexController.Position position
    ) {
        ServerPlayer player = player(botName);
        if (player == null || !(player.level() instanceof ServerLevel level)) {
            return null;
        }
        WorldView world = new MinecraftWorldView(level);
        List<Goal> candidates = new ArrayList<>();
        int baseX = position.blockX();
        int baseZ = position.blockZ();
        for (int radius = 1; radius <= WATER_SHORE_RADIUS
            && candidates.size() < Goal.MAX_COMPOSITE_MEMBERS; radius++) {
            for (int delta = -radius; delta <= radius; delta++) {
                addShoreCandidate(candidates, world, level, baseX + radius, baseZ + delta);
                addShoreCandidate(candidates, world, level, baseX - radius, baseZ + delta);
            }
            for (int delta = -radius + 1; delta < radius; delta++) {
                addShoreCandidate(candidates, world, level, baseX + delta, baseZ + radius);
                addShoreCandidate(candidates, world, level, baseX + delta, baseZ - radius);
            }
        }
        if (candidates.isEmpty()) {
            return null;
        }
        return findReachableDryGoal(world, position, candidates);
    }

    @Override
    public WorldView world(String botName) {
        ServerPlayer player = player(botName);
        if (player == null || !(player.level() instanceof ServerLevel level)) {
            return null;
        }
        return reflexPathWorld(
            level,
            player.isInLava()
                || (player.isOnFire() && MinecraftWorldView.isLavaNearBody(
                    level, player.getBlockX(), player.getBlockY(), player.getBlockZ()
                ))
        );
    }

    private SurvivalReflexController.Target findDryStand(
        WorldView safeWorld,
        WorldView pathWorld,
        ServerLevel level,
        SurvivalReflexController.Kind kind,
        SurvivalReflexController.Position origin
    ) {
        int baseX = origin.blockX();
        int baseY = origin.blockY();
        int baseZ = origin.blockZ();
        List<SurvivalReflexController.Target> candidates = new ArrayList<>();
        for (int radius = 1; radius <= ESCAPE_RADIUS
            && candidates.size() < MAX_DRY_CANDIDATES; radius++) {
            for (int yOffset : Y_OFFSETS) {
                int y = baseY + yOffset;
                for (int delta = -radius; delta <= radius; delta++) {
                    SurvivalReflexController.Target target = dryCandidate(
                        safeWorld, level, kind, baseX + radius, y, baseZ + delta
                    );
                    if (target != null) {
                        candidates.add(target);
                        if (candidates.size() >= MAX_DRY_CANDIDATES) {
                            break;
                        }
                    }
                    target = dryCandidate(
                        safeWorld, level, kind, baseX - radius, y, baseZ + delta
                    );
                    if (target != null) {
                        candidates.add(target);
                        if (candidates.size() >= MAX_DRY_CANDIDATES) {
                            break;
                        }
                    }
                }
                if (candidates.size() >= MAX_DRY_CANDIDATES) {
                    break;
                }
                for (int delta = -radius + 1; delta < radius; delta++) {
                    SurvivalReflexController.Target target = dryCandidate(
                        safeWorld, level, kind, baseX + delta, y, baseZ + radius
                    );
                    if (target != null) {
                        candidates.add(target);
                        if (candidates.size() >= MAX_DRY_CANDIDATES) {
                            break;
                        }
                    }
                    target = dryCandidate(
                        safeWorld, level, kind, baseX + delta, y, baseZ - radius
                    );
                    if (target != null) {
                        candidates.add(target);
                        if (candidates.size() >= MAX_DRY_CANDIDATES) {
                            break;
                        }
                    }
                }
                if (candidates.size() >= MAX_DRY_CANDIDATES) {
                    break;
                }
            }
        }
        return reachableDryCandidate(pathWorld, origin, candidates);
    }

    /**
     * A player already in lava must be able to traverse the hazardous source
     * component to leave it. Safe endpoint selection still uses the ordinary
     * conservative world; this view is only the bounded reflex path substrate.
     */
    private static WorldView reflexPathWorld(
        ServerLevel level,
        boolean lavaEgress
    ) {
        WorldView conservative = new MinecraftWorldView(level);
        if (!lavaEgress) {
            return conservative;
        }
        return new WorldView() {
            @Override
            public NodeKind kindAt(int x, int y, int z) {
                return isLava(level, x, y, z)
                    ? NodeKind.LIQUID
                    : conservative.kindAt(x, y, z);
            }

            @Override
            public boolean isBodyPositionHazardous(int x, int y, int z) {
                return false;
            }
        };
    }

    private static boolean needsLavaEgress(
        ServerLevel level,
        SurvivalReflexController.Kind kind,
        SurvivalReflexController.Position position
    ) {
        return kind == SurvivalReflexController.Kind.LAVA
            || (kind == SurvivalReflexController.Kind.FIRE
                && MinecraftWorldView.isLavaNearBody(
                    level, position.blockX(), position.blockY(), position.blockZ()
                ));
    }

    /**
     * Geometry alone is not an escape target.  Validate the finite candidate
     * set through the same loaded-world A* used by the reflex executor, then
     * return the candidate at the actual path endpoint.
     */
    private static SurvivalReflexController.Target reachableDryCandidate(
        WorldView world,
        SurvivalReflexController.Position origin,
        List<SurvivalReflexController.Target> candidates
    ) {
        if (candidates.isEmpty()) {
            return null;
        }
        List<Goal> goals = candidates.stream()
            .map(target -> (Goal) new Goal.Stand(
                target.position().blockX(),
                target.position().blockY(),
                target.position().blockZ()
            ))
            .toList();
        Goal goal = goals.size() == 1 ? goals.getFirst() : new Goal.Composite(goals);
        AStarPathfinder.Result result = new AStarPathfinder(
            world,
            goal,
            origin.blockX(),
            origin.blockY(),
            origin.blockZ(),
            ESCAPE_PATH_NODE_CAP,
            ESCAPE_PATH_UNLOADED_CAP
        ).step(ESCAPE_PATH_STEP_BUDGET);
        if (result.outcome() != AStarPathfinder.Outcome.COMPLETE || result.path().isEmpty()) {
            return null;
        }
        AStarPathfinder.Waypoint endpoint = result.path().getLast();
        return candidates.stream()
            .filter(target -> target.position().blockX() == endpoint.x()
                && target.position().blockY() == endpoint.y()
                && target.position().blockZ() == endpoint.z())
            .findFirst()
            .orElse(null);
    }

    private static Goal findReachableDryGoal(
        WorldView world,
        SurvivalReflexController.Position origin,
        List<Goal> candidates
    ) {
        if (candidates.isEmpty()) {
            return null;
        }
        List<SurvivalReflexController.Target> targets = candidates.stream()
            .map(goal -> {
                if (goal instanceof Goal.Stand stand) {
                    return new SurvivalReflexController.Target(
                        new SurvivalReflexController.Position(stand.x() + 0.5, stand.y(), stand.z() + 0.5),
                        true
                    );
                }
                return null;
            })
            .filter(target -> target != null)
            .toList();
        SurvivalReflexController.Target selected = reachableDryCandidate(world, origin, targets);
        if (selected == null) {
            return null;
        }
        return new Goal.Stand(
            selected.position().blockX(),
            selected.position().blockY(),
            selected.position().blockZ()
        );
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
        if (kind == SurvivalReflexController.Kind.LAVA
            && world.isBodyPositionHazardous(x, y, z)) {
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
        for (int y = baseY; y <= level.getMaxY() - 2; y++) {
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

    private static boolean touchesActiveFire(ServerPlayer player, ServerLevel level) {
        return level.getBlockStates(player.getBoundingBox().deflate(1.0e-7)).anyMatch(
            state -> state.is(Blocks.FIRE) || state.is(Blocks.SOUL_FIRE)
        );
    }

    private static void addShoreCandidate(
        List<Goal> candidates,
        WorldView world,
        ServerLevel level,
        int x,
        int z
    ) {
        if (candidates.size() >= Goal.MAX_COMPOSITE_MEMBERS
            || level.getChunkSource().getChunkNow(x >> 4, z >> 4) == null) {
            return;
        }
        int y = level.getHeight(Heightmap.Types.WORLD_SURFACE, x, z);
        if (!isDryStand(world, x, y, z) || !waterBesideShore(world, x, y, z)) {
            return;
        }
        candidates.add(new Goal.Stand(x, y, z));
    }

    private static boolean waterBesideShore(WorldView world, int x, int y, int z) {
        int[][] neighbors = {{1, 0}, {-1, 0}, {0, 1}, {0, -1}};
        for (int[] neighbor : neighbors) {
            int nx = x + neighbor[0];
            int nz = z + neighbor[1];
            if (world.kindAt(nx, y, nz) == WorldView.NodeKind.LIQUID
                || world.kindAt(nx, y - 1, nz) == WorldView.NodeKind.LIQUID) {
                return true;
            }
        }
        return false;
    }

    private static boolean waterRisk(
        ServerPlayer player,
        ServerLevel level,
        SurvivalReflexController.Position position
    ) {
        return isWater(level, position.blockX(), position.blockY() + 1, position.blockZ())
            && player.getAirSupply() <= WATER_AIR_THRESHOLD;
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
