package dev.minebot.body.search;

import net.minecraft.core.BlockPos;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.world.level.block.Block;
import net.minecraft.world.level.block.state.BlockState;
import net.minecraft.world.level.chunk.LevelChunk;
import net.minecraft.world.level.chunk.LevelChunkSection;

import java.util.Map;
import java.util.function.Predicate;

/**
 * Stateless loaded-world block search. The presence test is the live section
 * palette itself: sections whose palette cannot contain a requested block are
 * skipped without touching cells, and matching sections are scanned against
 * live chunk data at request time, so returned positions are authoritative at
 * snapshot time. There is no materialized position index and therefore no
 * invalidation machinery to go stale.
 *
 * Chunks are visited in outward ring order, which makes the snapshot cap
 * distance-correct: everything dropped is farther than everything kept.
 * Unloaded chunks are counted as incomplete coverage and never loaded
 * synchronously.
 */
public final class LoadedBlockScanner {
    public static final int MAX_SNAPSHOT_MATCHES = 4_096;

    public LoadedSearchResult scan(
        ServerLevel level,
        BlockPos center,
        Map<Block, String> requestedBlocks,
        int radius,
        int verticalRadius,
        long snapshotTick
    ) {
        Predicate<BlockState> target = state -> requestedBlocks.containsKey(state.getBlock());
        NearestMatchCollector collector = new NearestMatchCollector(MAX_SNAPSHOT_MATCHES);
        int centerChunkX = center.getX() >> 4;
        int centerChunkZ = center.getZ() >> 4;
        long radiusSquared = (long) radius * radius;
        int minY = center.getY() - verticalRadius;
        int maxY = center.getY() + verticalRadius;
        int unloaded = 0;

        int maxRing = ChunkRing.maxRing(radius);
        for (int ring = 0; ring <= maxRing; ring++) {
            if (collector.canStop(ChunkRing.minBlockDistanceSquared(ring))) {
                collector.markTruncated();
                break;
            }
            for (int[] offset : ChunkRing.ringOffsets(ring)) {
                int chunkX = centerChunkX + offset[0];
                int chunkZ = centerChunkZ + offset[1];
                if (!ChunkRing.chunkIntersectsCircle(chunkX, chunkZ, center.getX(), center.getZ(), radiusSquared)) {
                    continue;
                }
                LevelChunk chunk = level.getChunkSource().getChunkNow(chunkX, chunkZ);
                if (chunk == null) {
                    unloaded++;
                    continue;
                }
                scanChunk(level, chunk, chunkX, chunkZ, center, requestedBlocks, target, radiusSquared, minY, maxY, collector);
            }
        }
        return new LoadedSearchResult(snapshotTick, collector.finish(), unloaded, collector.capped());
    }

    private static void scanChunk(
        ServerLevel level,
        LevelChunk chunk,
        int chunkX,
        int chunkZ,
        BlockPos center,
        Map<Block, String> requestedBlocks,
        Predicate<BlockState> target,
        long radiusSquared,
        int minY,
        int maxY,
        NearestMatchCollector collector
    ) {
        int levelMinY = level.getMinY();
        LevelChunkSection[] sections = chunk.getSections();
        for (int sectionIndex = 0; sectionIndex < sections.length; sectionIndex++) {
            LevelChunkSection section = sections[sectionIndex];
            if (section == null || section.hasOnlyAir()) {
                continue;
            }
            int sectionMinY = levelMinY + (sectionIndex << 4);
            if (sectionMinY + 15 < minY || sectionMinY > maxY) {
                continue;
            }
            if (!section.maybeHas(target)) {
                continue;
            }
            int localYStart = Math.max(0, minY - sectionMinY);
            int localYEnd = Math.min(15, maxY - sectionMinY);
            for (int localY = localYStart; localY <= localYEnd; localY++) {
                for (int localZ = 0; localZ < 16; localZ++) {
                    for (int localX = 0; localX < 16; localX++) {
                        BlockState state = section.getBlockState(localX, localY, localZ);
                        String blockId = requestedBlocks.get(state.getBlock());
                        if (blockId == null) {
                            continue;
                        }
                        int x = (chunkX << 4) | localX;
                        int z = (chunkZ << 4) | localZ;
                        long dx = x - center.getX();
                        long dz = z - center.getZ();
                        long horizontalDistanceSquared = dx * dx + dz * dz;
                        if (horizontalDistanceSquared > radiusSquared) {
                            continue;
                        }
                        int y = sectionMinY + localY;
                        long dy = y - center.getY();
                        collector.add(new SearchMatch(x, y, z, blockId, horizontalDistanceSquared + dy * dy));
                    }
                }
            }
        }
    }

    /** Registry id for a block, matching the id vocabulary of requests. */
    public static String blockId(Block block) {
        return BuiltInRegistries.BLOCK.getKey(block).toString();
    }
}
