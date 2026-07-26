package dev.minebot.body.search;

import net.minecraft.core.BlockPos;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.resources.ResourceKey;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.world.level.Level;
import net.minecraft.world.level.chunk.LevelChunk;
import net.minecraft.world.level.chunk.LevelChunkSection;
import net.minecraft.world.level.block.state.BlockState;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

public final class LoadedBlockIndex {
    private static final int MAX_SNAPSHOT_MATCHES = 4_096;
    private static final long INDEX_TICK_BUDGET_NANOS = 5_000_000L;

    private final Map<ChunkKey, IndexedChunk> indexedChunks = new HashMap<>();
    private final ArrayDeque<PendingChunk> pendingChunks = new ArrayDeque<>();
    private final Set<ChunkKey> pendingKeys = new HashSet<>();
    private long generation;

    public void enqueue(ServerLevel level, LevelChunk chunk) {
        ChunkKey key = ChunkKey.of(level, chunk.getPos().x(), chunk.getPos().z());
        if (pendingKeys.add(key)) {
            pendingChunks.addLast(new PendingChunk(level, chunk, key));
        }
    }

    public void invalidate(ServerLevel level, BlockPos position) {
        ChunkKey key = ChunkKey.of(level, position.getX() >> 4, position.getZ() >> 4);
        indexedChunks.remove(key);
        if (level.hasChunk(key.x, key.z)) {
            enqueue(level, level.getChunk(key.x, key.z));
        }
        generation++;
    }

    public void remove(ServerLevel level, int chunkX, int chunkZ) {
        ChunkKey key = ChunkKey.of(level, chunkX, chunkZ);
        pendingKeys.remove(key);
        indexedChunks.remove(key);
        generation++;
    }

    public void tick() {
        long deadline = System.nanoTime() + INDEX_TICK_BUDGET_NANOS;
        while (!pendingChunks.isEmpty() && System.nanoTime() < deadline) {
            PendingChunk pending = pendingChunks.removeFirst();
            pendingKeys.remove(pending.key);
            if (!pending.level.hasChunk(pending.key.x, pending.key.z)) {
                continue;
            }
            indexedChunks.put(pending.key, indexChunk(pending.level, pending.chunk));
            generation++;
        }
    }

    public long generation() {
        return generation;
    }

    public LoadedSearchResult search(
        ServerLevel level,
        BlockPos center,
        Set<String> blockIds,
        int radius,
        int verticalRadius
    ) {
        int minChunkX = Math.floorDiv(center.getX() - radius, 16);
        int maxChunkX = Math.floorDiv(center.getX() + radius, 16);
        int minChunkZ = Math.floorDiv(center.getZ() - radius, 16);
        int maxChunkZ = Math.floorDiv(center.getZ() + radius, 16);
        int minY = center.getY() - verticalRadius;
        int maxY = center.getY() + verticalRadius;
        long radiusSquared = (long) radius * radius;
        List<SearchMatch> matches = new ArrayList<>();
        int unloaded = 0;
        int pending = 0;
        boolean capped = false;

        for (int chunkX = minChunkX; chunkX <= maxChunkX; chunkX++) {
            for (int chunkZ = minChunkZ; chunkZ <= maxChunkZ; chunkZ++) {
                if (!chunkIntersectsCircle(chunkX, chunkZ, center.getX(), center.getZ(), radiusSquared)) {
                    continue;
                }
                if (!level.hasChunk(chunkX, chunkZ)) {
                    unloaded++;
                    continue;
                }
                ChunkKey key = ChunkKey.of(level, chunkX, chunkZ);
                IndexedChunk indexed = indexedChunks.get(key);
                if (indexed == null) {
                    pending++;
                    enqueue(level, level.getChunk(chunkX, chunkZ));
                    continue;
                }
                for (String blockId : blockIds) {
                    int[] positions = indexed.positionsByBlock.get(blockId);
                    if (positions == null) {
                        continue;
                    }
                    for (int packed : positions) {
                        int x = (chunkX << 4) | (packed & 15);
                        int z = (chunkZ << 4) | ((packed >>> 4) & 15);
                        int y = indexed.minY + (packed >>> 8);
                        if (y < minY || y > maxY) {
                            continue;
                        }
                        long dx = x - center.getX();
                        long dz = z - center.getZ();
                        long horizontalDistanceSquared = dx * dx + dz * dz;
                        if (horizontalDistanceSquared > radiusSquared) {
                            continue;
                        }
                        matches.add(new SearchMatch(x, y, z, blockId, horizontalDistanceSquared + (long) (y - center.getY()) * (y - center.getY())));
                        if (matches.size() > MAX_SNAPSHOT_MATCHES) {
                            capped = true;
                            break;
                        }
                    }
                    if (capped) {
                        break;
                    }
                }
                if (capped) {
                    break;
                }
            }
            if (capped) {
                break;
            }
        }
        matches.sort(Comparator.comparingDouble(SearchMatch::distanceSquared));
        if (capped) {
            matches = List.copyOf(matches.subList(0, MAX_SNAPSHOT_MATCHES));
        }
        return new LoadedSearchResult(generation, List.copyOf(matches), unloaded, pending, capped);
    }

    private IndexedChunk indexChunk(ServerLevel level, LevelChunk chunk) {
        Map<String, IntAccumulator> byBlock = new HashMap<>();
        int minY = level.getMinY();
        LevelChunkSection[] sections = chunk.getSections();
        for (int sectionIndex = 0; sectionIndex < sections.length; sectionIndex++) {
            LevelChunkSection section = sections[sectionIndex];
            if (section == null || section.hasOnlyAir()) {
                continue;
            }
            int baseY = sectionIndex << 4;
            for (int localY = 0; localY < 16; localY++) {
                for (int localZ = 0; localZ < 16; localZ++) {
                    for (int localX = 0; localX < 16; localX++) {
                        BlockState state = section.getBlockState(localX, localY, localZ);
                        if (state.isAir()) {
                            continue;
                        }
                        String blockId = BuiltInRegistries.BLOCK.getKey(state.getBlock()).toString();
                        int packed = localX | (localZ << 4) | ((baseY + localY) << 8);
                        byBlock.computeIfAbsent(blockId, ignored -> new IntAccumulator()).add(packed);
                    }
                }
            }
        }
        Map<String, int[]> compact = new HashMap<>();
        byBlock.forEach((blockId, positions) -> compact.put(blockId, positions.toArray()));
        return new IndexedChunk(minY, Map.copyOf(compact));
    }

    private static boolean chunkIntersectsCircle(int chunkX, int chunkZ, int centerX, int centerZ, long radiusSquared) {
        int minX = chunkX << 4;
        int minZ = chunkZ << 4;
        int nearestX = clamp(centerX, minX, minX + 15);
        int nearestZ = clamp(centerZ, minZ, minZ + 15);
        long dx = nearestX - centerX;
        long dz = nearestZ - centerZ;
        return dx * dx + dz * dz <= radiusSquared;
    }

    private static int clamp(int value, int min, int max) {
        return Math.max(min, Math.min(value, max));
    }

    private record ChunkKey(ResourceKey<Level> dimension, int x, int z) {
        private static ChunkKey of(ServerLevel level, int x, int z) {
            return new ChunkKey(level.dimension(), x, z);
        }
    }

    private record IndexedChunk(int minY, Map<String, int[]> positionsByBlock) {
    }

    private record PendingChunk(ServerLevel level, LevelChunk chunk, ChunkKey key) {
    }

    private static final class IntAccumulator {
        private int[] values = new int[32];
        private int size;

        private void add(int value) {
            if (size == values.length) {
                int[] expanded = new int[values.length * 2];
                System.arraycopy(values, 0, expanded, 0, values.length);
                values = expanded;
            }
            values[size++] = value;
        }

        private int[] toArray() {
            int[] result = new int[size];
            System.arraycopy(values, 0, result, 0, size);
            return result;
        }
    }
}
