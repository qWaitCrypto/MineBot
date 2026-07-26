package dev.minebot.body.nav;

import net.minecraft.core.BlockPos;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.world.level.block.Blocks;
import net.minecraft.world.level.block.state.BlockState;
import net.minecraft.world.level.chunk.LevelChunk;
import net.minecraft.world.level.material.Fluids;

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
        if (!state.getFluidState().isEmpty()) {
            return state.getFluidState().is(Fluids.WATER) || state.getFluidState().is(Fluids.FLOWING_WATER)
                ? NodeKind.LIQUID
                : NodeKind.HAZARD;
        }
        if (state.is(Blocks.FIRE) || state.is(Blocks.SOUL_FIRE) || state.is(Blocks.MAGMA_BLOCK)) {
            return NodeKind.HAZARD;
        }
        if (state.getCollisionShape(level, pos).isEmpty()) {
            return NodeKind.PASSABLE;
        }
        return NodeKind.SOLID;
    }
}
