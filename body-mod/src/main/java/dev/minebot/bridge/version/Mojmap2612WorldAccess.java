package dev.minebot.bridge.version;

import net.minecraft.core.BlockPos;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.world.level.block.state.BlockState;
import net.minecraft.world.level.block.state.properties.Property;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.IdentityHashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;

public final class Mojmap2612WorldAccess implements WorldAccess {
    private final Map<BlockState, String> stateStringCache = new IdentityHashMap<>();

    @Override
    public String minecraftVersion() {
        return "26.1.2";
    }

    @Override
    public EntitySnapshot findEntity(MinecraftServer server, String entityName, String dimension) {
        for (ServerPlayer player : server.getPlayerList().getPlayers()) {
            if (player.getScoreboardName().equals(entityName) && dimensionMatches(player.level(), dimension)) {
                return snapshot(player);
            }
        }
        return null;
    }

    @Override
    public SectionSnapshot sectionSnapshot(ServerLevel level, String subId, int sectionX, int sectionY, int sectionZ) {
        Map<String, Integer> paletteIndex = new LinkedHashMap<>();
        List<String> palette = new ArrayList<>();
        int[] indices = new int[SectionSnapshot.SECTION_VOLUME];
        int cursor = 0;
        for (int y = 0; y < 16; y++) {
            for (int z = 0; z < 16; z++) {
                for (int x = 0; x < 16; x++) {
                    BlockPos pos = new BlockPos(sectionX * 16 + x, sectionY * 16 + y, sectionZ * 16 + z);
                    String state = canonicalState(level.getBlockState(pos));
                    Integer index = paletteIndex.get(state);
                    if (index == null) {
                        index = palette.size();
                        paletteIndex.put(state, index);
                        palette.add(state);
                    }
                    indices[cursor++] = index;
                }
            }
        }
        return new SectionSnapshot(subId, dimensionId(level), sectionX, sectionY, sectionZ, palette, indices);
    }

    @Override
    public boolean hasChunk(ServerLevel level, int sectionX, int sectionZ) {
        return level.hasChunk(sectionX, sectionZ);
    }

    @Override
    public String dimensionId(ServerLevel level) {
        return level.dimension().identifier().toString();
    }

    private static EntitySnapshot snapshot(ServerPlayer player) {
        return new EntitySnapshot(
            player.getScoreboardName(),
            player.level(),
            player.getX(),
            player.getY(),
            player.getZ(),
            player.getYRot(),
            player.getXRot(),
            player.onGround(),
            player.getPose().name().toLowerCase()
        );
    }

    private static boolean dimensionMatches(ServerLevel level, String dimension) {
        return dimension == null || dimension.isBlank() || level.dimension().identifier().toString().equals(dimension);
    }

    private String canonicalState(BlockState state) {
        synchronized (stateStringCache) {
            String cached = stateStringCache.get(state);
            if (cached != null) {
                return cached;
            }
            String computed = computeCanonicalState(state);
            stateStringCache.put(state, computed);
            return computed;
        }
    }

    private static String computeCanonicalState(BlockState state) {
        String id = BuiltInRegistries.BLOCK.getKey(state.getBlock()).toString();
        List<Property.Value<?>> values = state.getValues()
            .sorted(Comparator.comparing(value -> value.property().getName()))
            .collect(Collectors.toList());
        if (values.isEmpty()) {
            return id;
        }
        StringBuilder builder = new StringBuilder(id).append("[");
        boolean first = true;
        for (Property.Value<?> value : values) {
            if (!first) {
                builder.append(",");
            }
            first = false;
            builder.append(value.property().getName()).append("=");
            builder.append(value.valueName());
        }
        return builder.append("]").toString();
    }
}
