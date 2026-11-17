package dev.minebot.client.body;

/**
 * The game's own words on the respawn point (vision-and-home-20261028.md §7.7 item 5).
 *
 * <p>"Respawn point set" comes from the server when a bed or a charged respawn anchor sets it;
 * "You have no home bed or charged respawn anchor, or it was obstructed" is said by the client
 * on respawning without one. Both arrive as system messages that keep their translation key.
 */
public final class SpawnMessage {
    public static final String SET = "block.minecraft.set_spawn";
    public static final String NOT_VALID = "block.minecraft.spawn.not_valid";

    private SpawnMessage() {}

    /** Whether a system message with this translation key is about the respawn point. */
    public static boolean about(String key) {
        return SET.equals(key) || NOT_VALID.equals(key);
    }

    /** Whether a block with this registry path can be the respawn point a message set. */
    public static boolean respawnBlock(String path) {
        return path != null && (path.endsWith("_bed") || path.equals("respawn_anchor"));
    }
}
