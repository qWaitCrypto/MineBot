package dev.minebot.body.interaction;

import net.minecraft.nbt.CompoundTag;
import net.minecraft.resources.Identifier;
import net.minecraft.server.MinecraftServer;

import java.util.UUID;
import java.util.regex.Pattern;

/** Persistent identity stored in the same command-storage cell as the legacy path. */
public final class WorldIdentityStore {
    private static final Identifier STORAGE = Identifier.fromNamespaceAndPath("minebot", "runtime");
    private static final String KEY = "world_id";
    private static final Pattern VALID = Pattern.compile("[A-Za-z0-9][A-Za-z0-9._:-]{0,127}");

    private WorldIdentityStore() {
    }

    public static String getOrCreate(MinecraftServer server) {
        CompoundTag stored = server.getCommandStorage().get(STORAGE);
        String worldId = stored.getStringOr(KEY, "");
        if (!worldId.isBlank()) {
            if (!VALID.matcher(worldId).matches()) {
                throw new IllegalStateException("stored world identity is invalid");
            }
            return worldId;
        }
        worldId = "world-" + UUID.randomUUID().toString().replace("-", "");
        CompoundTag updated = stored.copy();
        updated.putString(KEY, worldId);
        server.getCommandStorage().set(STORAGE, updated);
        return worldId;
    }
}
