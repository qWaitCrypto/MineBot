package dev.minebot.body.nav;

import net.minecraft.core.BlockPos;
import net.minecraft.core.Direction;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.world.level.BlockGetter;
import net.minecraft.world.level.block.Blocks;
import net.minecraft.world.level.block.state.BlockState;
import net.minecraft.world.level.chunk.LevelChunk;
import net.minecraft.world.level.material.Fluids;
import net.minecraft.world.phys.AABB;
import net.minecraft.world.phys.shapes.BooleanOp;
import net.minecraft.world.phys.shapes.Shapes;
import java.util.TreeSet;

/**
 * Live loaded-world view for the planner. Server thread only; never loads a
 * chunk. Unknown terrain is UNLOADED, water is LIQUID, lava and fire are
 * HAZARD — no state is ever softened into "air".
 */
public final class MinecraftWorldView implements WorldView {
    private final ServerLevel level;

    public MinecraftWorldView(ServerLevel level) {
        this.level = level;
    }

    @Override
    public NodeKind kindAt(int x, int y, int z) {
        if (y < level.getMinY() || y > level.getMaxY()) {
            return y < level.getMinY() ? NodeKind.HAZARD : NodeKind.PASSABLE;
        }
        LevelChunk chunk = level.getChunkSource().getChunkNow(x >> 4, z >> 4);
        if (chunk == null) {
            return NodeKind.UNLOADED;
        }
        BlockPos pos = new BlockPos(x, y, z);
        BlockState state = chunk.getBlockState(pos);
        return classify(state, level, pos);
    }

    static NodeKind classify(BlockState state, BlockGetter level, BlockPos pos) {
        var fluid = state.getFluidState();
        boolean water = fluid.is(Fluids.WATER) || fluid.is(Fluids.FLOWING_WATER);
        if (!fluid.isEmpty() && !water) {
            return NodeKind.HAZARD;
        }
        if (state.is(Blocks.FIRE) || state.is(Blocks.SOUL_FIRE) || state.is(Blocks.MAGMA_BLOCK)) {
            return NodeKind.HAZARD;
        }
        if (state.is(Blocks.LADDER)
            || state.is(Blocks.VINE)
            || state.is(Blocks.SCAFFOLDING)
            || state.is(Blocks.TWISTING_VINES)
            || state.is(Blocks.TWISTING_VINES_PLANT)
            || state.is(Blocks.WEEPING_VINES)
            || state.is(Blocks.WEEPING_VINES_PLANT)) {
            return NodeKind.CLIMBABLE;
        }
        // Waterlogging does not remove the block's collision. Only an empty
        // body-volume shape may become an ordinary swimming cell.
        if (!state.getCollisionShape(level, pos).isEmpty()) {
            return NodeKind.SOLID;
        }
        return water ? NodeKind.LIQUID : NodeKind.PASSABLE;
    }

    @Override
    public double standingHeight(int x, int y, int z) {
        if (!loadedNeighborhood(x, z)) {
            return Double.NaN;
        }
        double height = standingHeight(level, x, y, z);
        return Double.isFinite(height) && isStableDryPosition(x + 0.5, height, z + 0.5)
            ? height : Double.NaN;
    }

    static double standingHeight(BlockGetter blocks, int x, int y, int z) {
        var heights = new TreeSet<Double>(java.util.Comparator.reverseOrder());
        for (int dx = -1; dx <= 1; dx++) {
            for (int dz = -1; dz <= 1; dz++) {
                for (int dy = -2; dy <= 0; dy++) {
                    var pos = new BlockPos(x + dx, y + dy, z + dz);
                    for (AABB box : blocks.getBlockState(pos).getCollisionShape(blocks, pos).toAabbs()) {
                        double top = box.maxY + pos.getY();
                        if (top >= y && top < y + 1
                            && box.maxX + pos.getX() > x + 0.2 && box.minX + pos.getX() < x + 0.8
                            && box.maxZ + pos.getZ() > z + 0.2 && box.minZ + pos.getZ() < z + 0.8) {
                            heights.add(top);
                        }
                    }
                }
            }
        }
        for (double height : heights) {
            if (hasStableCollisionSupport(blocks, x + 0.5, height, z + 0.5)
                && dryVolumeClear(blocks, new AABB(x + 0.2, height, z + 0.2,
                    x + 0.8, height + 1.8, z + 0.8))) {
                return height;
            }
        }
        return Double.NaN;
    }

    private boolean loadedNeighborhood(int x, int z) {
        for (int dx = -2; dx <= 2; dx++) {
            for (int dz = -2; dz <= 2; dz++) {
                if (level.getChunkSource().getChunkNow((x + dx) >> 4, (z + dz) >> 4) == null) {
                    return false;
                }
            }
        }
        return true;
    }

    @Override
    public boolean isDryVolumeClear(double minX, double minY, double minZ,
                                    double maxX, double maxY, double maxZ) {
        for (int x = (int) Math.floor(minX); x < Math.ceil(maxX); x++) {
            for (int z = (int) Math.floor(minZ); z < Math.ceil(maxZ); z++) {
                if (!loadedNeighborhood(x, z)) {
                    return false;
                }
            }
        }
        return dryVolumeClear(level, new AABB(minX, minY, minZ, maxX, maxY, maxZ));
    }

    static boolean dryVolumeClear(BlockGetter blocks, AABB body) {
        var bodyShape = Shapes.create(body);
        for (int x = (int) Math.floor(body.minX) - 1; x <= Math.floor(body.maxX) + 1; x++) {
            for (int z = (int) Math.floor(body.minZ) - 1; z <= Math.floor(body.maxZ) + 1; z++) {
                for (int y = (int) Math.floor(body.minY) - 2; y <= Math.floor(body.maxY); y++) {
                    var pos = new BlockPos(x, y, z);
                    var state = blocks.getBlockState(pos);
                    var shape = state.getCollisionShape(blocks, pos).move(x, y, z);
                    if (Shapes.joinIsNotEmpty(shape, bodyShape, BooleanOp.AND)) {
                        return false;
                    }
                    if (body.intersects(new AABB(pos))
                        && (!state.getFluidState().isEmpty()
                            || classify(state, blocks, pos) == NodeKind.HAZARD)) {
                        return false;
                    }
                }
            }
        }
        return true;
    }

    @Override
    public boolean isStableDryPosition(double x, double y, double z) {
        if (!Double.isFinite(x) || !Double.isFinite(y) || !Double.isFinite(z)) {
            return false;
        }
        int bx = (int) Math.floor(x);
        int by = NavigateExecutor.PositionSource.Position.feetBlockY(y);
        int bz = (int) Math.floor(z);
        // Collision shapes may extend outside their block. Validate the whole
        // bounded neighborhood before asking Minecraft for state/shape data.
        if (!loadedNeighborhood(bx, bz)) {
            return false;
        }
        if (isBodyPositionHazardous(bx, by, bz)
            || kindAt(bx, by - 1, bz) == NodeKind.HAZARD
            || kindAt(bx, by, bz) == NodeKind.HAZARD
            || kindAt(bx, by + 1, bz) == NodeKind.HAZARD
            || !level.getBlockState(new BlockPos(bx, by, bz)).getFluidState().isEmpty()
            || !level.getBlockState(new BlockPos(bx, by + 1, bz)).getFluidState().isEmpty()) {
            return false;
        }
        return hasStableCollisionSupport(level, x, y, z);
    }

    /** Standing-player contact proof shared with native-state regressions. */
    static boolean hasStableCollisionSupport(BlockGetter blocks, double x, double y, double z) {
        if (!Double.isFinite(x) || !Double.isFinite(y) || !Double.isFinite(z)) {
            return false;
        }
        AABB body = new AABB(x - 0.3, y, z - 0.3, x + 0.3, y + 1.8, z + 0.3);
        var bodyShape = Shapes.create(body);
        int bx = (int) Math.floor(x);
        int by = (int) Math.floor(y);
        int bz = (int) Math.floor(z);
        double fall = -STABLE_SUPPORT_DISTANCE;
        for (int dx = -1; dx <= 1; dx++) {
            for (int dz = -1; dz <= 1; dz++) {
                for (int dy = -2; dy <= 2; dy++) {
                    BlockPos pos = new BlockPos(bx + dx, by + dy, bz + dz);
                    var shape = blocks.getBlockState(pos).getCollisionShape(blocks, pos)
                        .move(pos.getX(), pos.getY(), pos.getZ());
                    if (Shapes.joinIsNotEmpty(shape, bodyShape, BooleanOp.AND)) {
                        return false;
                    }
                    fall = shape.collide(Direction.Axis.Y, body, fall);
                }
            }
        }
        return fall > -STABLE_SUPPORT_DISTANCE + 1.0e-7;
    }

    @Override
    public boolean isBodyPositionHazardous(int x, int y, int z) {
        return isLavaNearBody(level, x, y, z);
    }

    @Override
    public boolean isBlockClearanceHazardous(int x, int y, int z) {
        // Clearing either the feet or head cell can open a same-level lava
        // pocket or let lava fall from the layer above into the new corridor.
        // Keep this mutation-only check out of ordinary movement semantics.
        return isLavaNearBody(level, x, y, z)
            || isLavaNearBody(level, x, y + 1, z);
    }

    /** Exact lava envelope used by both ordinary routing and survival reflexes. */
    public static boolean isLavaNearBody(ServerLevel level, int x, int y, int z) {
        for (int dx = -1; dx <= 1; dx++) {
            for (int dy = -1; dy <= 0; dy++) {
                for (int dz = -1; dz <= 1; dz++) {
                    LevelChunk chunk = level.getChunkSource().getChunkNow(
                        (x + dx) >> 4, (z + dz) >> 4
                    );
                    if (chunk == null) {
                        continue;
                    }
                    var fluid = chunk.getBlockState(
                        new BlockPos(x + dx, y + dy, z + dz)
                    ).getFluidState();
                    if (fluid.is(Fluids.LAVA) || fluid.is(Fluids.FLOWING_LAVA)) {
                        return true;
                    }
                }
            }
        }
        return false;
    }
}
