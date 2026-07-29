package dev.minebot.body.action;

import dev.minebot.body.nav.Goal;
import dev.minebot.body.nav.MinecraftWorldView;
import dev.minebot.body.nav.NavigateExecutor;
import dev.minebot.body.nav.WorldView;
import net.minecraft.core.BlockPos;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.world.level.levelgen.Heightmap;

import java.util.ArrayList;
import java.util.List;

/** Loaded-world surface targets for the canonical vertical recovery objective. */
public final class MinecraftSurfaceAccess implements AscendExecutor.SurfaceAccess {
    public static final int SEARCH_RADIUS = 16;

    private final ServerLevel level;
    private final WorldView world;

    public MinecraftSurfaceAccess(ServerLevel level) {
        this.level = level;
        this.world = new MinecraftWorldView(level);
    }

    @Override
    public boolean isSurfaceStand(int x, int y, int z) {
        return new Goal.Stand(x, y, z).isSatisfied(world, x, y, z)
            && level.canSeeSky(new BlockPos(x, y + 1, z));
    }

    @Override
    public Goal findSurfaceGoal(NavigateExecutor.PositionSource.Position position) {
        List<Goal> candidates = new ArrayList<>();
        int baseX = position.blockX();
        int baseZ = position.blockZ();
        addColumn(candidates, baseX, baseZ);
        for (int radius = 1; radius <= SEARCH_RADIUS
            && candidates.size() < Goal.MAX_COMPOSITE_MEMBERS; radius++) {
            for (int delta = -radius; delta <= radius; delta++) {
                addColumn(candidates, baseX + radius, baseZ + delta);
                addColumn(candidates, baseX - radius, baseZ + delta);
            }
            for (int delta = -radius + 1; delta < radius; delta++) {
                addColumn(candidates, baseX + delta, baseZ + radius);
                addColumn(candidates, baseX + delta, baseZ - radius);
            }
        }
        if (candidates.isEmpty()) {
            return null;
        }
        return candidates.size() == 1
            ? candidates.getFirst()
            : new Goal.Composite(candidates);
    }

    @Override
    public WorldView world() {
        return world;
    }

    private void addColumn(List<Goal> candidates, int x, int z) {
        if (candidates.size() >= Goal.MAX_COMPOSITE_MEMBERS
            || level.getChunkSource().getChunkNow(x >> 4, z >> 4) == null) {
            return;
        }
        int y = level.getHeight(Heightmap.Types.WORLD_SURFACE, x, z);
        if (isSurfaceStand(x, y, z)) {
            candidates.add(new Goal.Stand(x, y, z));
        }
    }
}
