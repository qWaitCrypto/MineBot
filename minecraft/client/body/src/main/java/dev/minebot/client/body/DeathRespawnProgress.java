package dev.minebot.client.body;

/** Native lifecycle edges, independent of an action or an IPC connection. */
public final class DeathRespawnProgress {
    private boolean dead;
    private boolean requested;

    public record Changes(boolean died, boolean requestRespawn, boolean respawned) {}

    public Changes observe(boolean hasPlayer, boolean playerDead, boolean deathScreen,
                           boolean livingInLoadedWorld) {
        boolean died = hasPlayer && playerDead && !dead;
        if (died) {
            dead = true;
            requested = false;
        }
        boolean request = dead && hasPlayer && deathScreen && !requested;
        if (request) requested = true;
        boolean respawned = dead && hasPlayer && !playerDead && !deathScreen && livingInLoadedWorld;
        if (respawned) {
            dead = false;
            requested = false;
        }
        return new Changes(died, request, respawned);
    }
}
