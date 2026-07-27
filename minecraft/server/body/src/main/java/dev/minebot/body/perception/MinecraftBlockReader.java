package dev.minebot.body.perception;

import net.minecraft.core.BlockPos;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.world.level.block.Blocks;
import net.minecraft.world.level.block.state.BlockState;
import net.minecraft.world.level.block.state.properties.Property;
import net.minecraft.world.level.chunk.LevelChunk;
import net.minecraft.world.level.levelgen.Heightmap;

import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.Map;

/** Loaded-world block facts. Reads never load an absent chunk. */
public final class MinecraftBlockReader {
    private final ServerLevel level;

    public MinecraftBlockReader(ServerLevel level) {
        this.level = level;
    }

    public BlockSnapshot.Fact read(BlockSnapshot.Position position) {
        return read(position.x(), position.y(), position.z());
    }

    public BlockSnapshot.Fact read(int x, int y, int z) {
        if (y < level.getMinY() || y > level.getMaxY()) {
            return new BlockSnapshot.Fact(x, y, z, "minecraft:void_air", "CLEAR", Map.of());
        }
        LevelChunk chunk = level.getChunkSource().getChunkNow(x >> 4, z >> 4);
        if (chunk == null) {
            return new BlockSnapshot.Fact(x, y, z, "unknown", "UNLOADED", Map.of());
        }
        BlockState blockState = chunk.getBlockState(new BlockPos(x, y, z));
        String blockId = BuiltInRegistries.BLOCK.getKey(blockState.getBlock()).toString();
        return new BlockSnapshot.Fact(
            x,
            y,
            z,
            blockId,
            classify(blockState),
            properties(blockState)
        );
    }

    public BlockSnapshot.SurfaceColumn readSurfaceColumn(int x, int z) {
        LevelChunk chunk = level.getChunkSource().getChunkNow(x >> 4, z >> 4);
        if (chunk == null) {
            int y = level.getMinY();
            BlockSnapshot.Fact unloaded = new BlockSnapshot.Fact(x, y, z, "unknown", "UNLOADED", Map.of());
            return new BlockSnapshot.SurfaceColumn(
                x,
                z,
                y,
                unloaded,
                new BlockSnapshot.Fact(x, y + 1, z, "unknown", "UNLOADED", Map.of()),
                new BlockSnapshot.Fact(x, y - 1, z, "unknown", "UNLOADED", Map.of())
            );
        }
        int feetY = level.getHeight(Heightmap.Types.WORLD_SURFACE, x, z);
        return new BlockSnapshot.SurfaceColumn(
            x,
            z,
            feetY,
            read(x, feetY, z),
            read(x, feetY + 1, z),
            read(x, feetY - 1, z)
        );
    }

    public static String classify(BlockState state) {
        if (state.is(Blocks.WATER) || state.is(Blocks.LAVA)) {
            return "LIQUID";
        }
        if (state.isAir() || state.canBeReplaced()) {
            return "CLEAR";
        }
        return "SOLID";
    }

    private static Map<String, String> properties(BlockState state) {
        Map<String, String> properties = new LinkedHashMap<>();
        state.getProperties().stream()
            .sorted(Comparator.comparing(Property::getName))
            .forEach(property -> properties.put(property.getName(), property.value(state).valueName()));
        return properties;
    }
}
