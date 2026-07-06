package dev.minebot.bridge.version;

import com.google.gson.JsonObject;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.level.ServerLevel;

public interface WorldAccess {
    String minecraftVersion();

    EntitySnapshot findEntity(MinecraftServer server, String entityName, String dimension);

    JsonObject sectionKeyframe(ServerLevel level, String subId, int sectionX, int sectionY, int sectionZ);

    String dimensionId(ServerLevel level);
}
