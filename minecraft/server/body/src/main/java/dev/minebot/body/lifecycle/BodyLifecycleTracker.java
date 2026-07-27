package dev.minebot.body.lifecycle;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;

import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.Map;
import java.util.Set;

/** Tracks watched FakePlayers and emits server-observed lifecycle facts. */
public final class BodyLifecycleTracker {
    @FunctionalInterface
    public interface EventSink {
        void emit(String bot, int serverTick, String name, String actionId, JsonObject data);
    }

    public record Snapshot(
        double x,
        double y,
        double z,
        float health,
        String inventoryHash,
        JsonObject inventoryCounts
    ) {
        public Snapshot {
            inventoryCounts = inventoryCounts == null ? new JsonObject() : inventoryCounts.deepCopy();
        }
    }

    private final EventSink events;
    private final Set<String> watched = new LinkedHashSet<>();
    private final Set<String> pendingRespawnEvents = new LinkedHashSet<>();
    private final Set<String> explicitDespawns = new LinkedHashSet<>();
    private final Set<String> deathNotices = new LinkedHashSet<>();
    private final Set<String> missingNotices = new LinkedHashSet<>();
    private final Map<String, Snapshot> lastSnapshots = new LinkedHashMap<>();

    public BodyLifecycleTracker(EventSink events) {
        this.events = events;
    }

    public void watch(String botName) {
        watched.add(botName);
    }

    public Set<String> watchedBots() {
        return Set.copyOf(watched);
    }

    public void requestSpawn(String botName, boolean emitRespawned) {
        watch(botName);
        explicitDespawns.remove(botName);
        if (emitRespawned) {
            pendingRespawnEvents.add(botName);
        }
    }

    public void requestDespawn(String botName) {
        watch(botName);
        explicitDespawns.add(botName);
        pendingRespawnEvents.remove(botName);
    }

    public void observePresent(String botName, Snapshot snapshot, int serverTick) {
        if (!watched.contains(botName)) {
            return;
        }
        lastSnapshots.put(botName, snapshot);
        boolean reappeared = missingNotices.remove(botName);
        boolean emitRespawned = pendingRespawnEvents.remove(botName);
        if (emitRespawned) {
            JsonObject facts = new JsonObject();
            facts.add("final_pos", position(snapshot));
            events.emit(botName, serverTick, "respawned", null, facts);
        }
        if (reappeared || emitRespawned) {
            deathNotices.remove(botName);
        }
    }

    public boolean observeMissing(String botName, int serverTick) {
        if (!watched.contains(botName) || !missingNotices.add(botName)) {
            return false;
        }
        Snapshot snapshot = lastSnapshots.get(botName);
        JsonObject facts = new JsonObject();
        facts.add("lastPos", position(snapshot));
        boolean explicitlyDespawned = explicitDespawns.remove(botName);
        facts.addProperty("reason", deathNotices.contains(botName)
            ? "death"
            : explicitlyDespawned ? "despawn" : "disconnected");
        events.emit(botName, serverTick, "bodyMissing", null, facts);
        return true;
    }

    public void afterDeath(String botName, Snapshot snapshot, int serverTick) {
        if (!watched.contains(botName) || explicitDespawns.contains(botName)) {
            return;
        }
        if (!deathNotices.add(botName)) {
            return;
        }
        Snapshot beforeDeath = lastSnapshots.get(botName);
        Snapshot deathPosition = snapshot == null ? beforeDeath : snapshot;
        if (deathPosition != null) {
            lastSnapshots.put(
                botName,
                new Snapshot(
                    deathPosition.x(),
                    deathPosition.y(),
                    deathPosition.z(),
                    deathPosition.health(),
                    beforeDeath == null ? deathPosition.inventoryHash() : beforeDeath.inventoryHash(),
                    beforeDeath == null ? deathPosition.inventoryCounts() : beforeDeath.inventoryCounts()
                )
            );
        }
        JsonObject facts = new JsonObject();
        facts.add("pos", position(deathPosition));
        facts.addProperty("inventory_before", "");
        facts.addProperty(
            "inventory_hash",
            beforeDeath == null ? (snapshot == null ? "" : snapshot.inventoryHash()) : beforeDeath.inventoryHash()
        );
        facts.add(
            "inventory_counts_before",
            beforeDeath == null
                ? (snapshot == null ? new JsonObject() : snapshot.inventoryCounts().deepCopy())
                : beforeDeath.inventoryCounts().deepCopy()
        );
        events.emit(botName, serverTick, "death", null, facts);
    }

    public void afterDamage(
        String botName,
        float amount,
        float healthAfter,
        String source,
        boolean blocked,
        int serverTick
    ) {
        if (!watched.contains(botName)) {
            return;
        }
        JsonObject facts = new JsonObject();
        facts.addProperty("amount", amount);
        facts.addProperty("health_after", healthAfter);
        facts.addProperty("source", source);
        facts.addProperty("blocked", blocked);
        events.emit(botName, serverTick, "damaged", null, facts);
    }

    private static JsonArray position(Snapshot snapshot) {
        JsonArray pos = new JsonArray();
        if (snapshot == null) {
            pos.add(0.0);
            pos.add(0.0);
            pos.add(0.0);
        } else {
            pos.add(snapshot.x());
            pos.add(snapshot.y());
            pos.add(snapshot.z());
        }
        return pos;
    }
}
