package dev.minebot.bridge.version;

import net.minecraft.server.MinecraftServer;
import net.minecraft.server.level.ServerLevel;

public interface WorldAccess {
    String minecraftVersion();

    EntitySnapshot findEntity(MinecraftServer server, String entityName, String dimension);

    SectionSnapshot sectionSnapshot(ServerLevel level, String subId, int sectionX, int sectionY, int sectionZ);

    boolean hasChunk(ServerLevel level, int sectionX, int sectionZ);

    String dimensionId(ServerLevel level);
}
