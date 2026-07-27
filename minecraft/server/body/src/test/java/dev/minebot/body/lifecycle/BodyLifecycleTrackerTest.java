package dev.minebot.body.lifecycle;

import dev.minebot.body.event.BotEventStream;
import org.junit.jupiter.api.Test;

import com.google.gson.JsonObject;

import static org.junit.jupiter.api.Assertions.assertEquals;

final class BodyLifecycleTrackerTest {
    @Test
    void deathMissingAndRespawnCarryObservedFactsOnce() {
        BotEventStream events = new BotEventStream();
        BodyLifecycleTracker tracker = new BodyLifecycleTracker(events::emit);
        JsonObject inventory = new JsonObject();
        inventory.addProperty("minecraft:bread", 2);
        var before = new BodyLifecycleTracker.Snapshot(1.5, -70.0, 2.5, 0.0f, "inv-hash", inventory);

        tracker.watch("Bot");
        tracker.observePresent("Bot", before, 10);
        var callbackAfterDrops = new BodyLifecycleTracker.Snapshot(
            1.5, -80.0, 2.5, 0.0f, "empty-after-drops", new JsonObject()
        );
        tracker.afterDeath("Bot", callbackAfterDrops, 11);
        tracker.afterDeath("Bot", callbackAfterDrops, 11);
        tracker.observePresent("Bot", callbackAfterDrops, 11);
        tracker.observeMissing("Bot", 12);
        tracker.observeMissing("Bot", 13);
        tracker.requestSpawn("Bot", true);
        tracker.observePresent(
            "Bot",
            new BodyLifecycleTracker.Snapshot(3.0, 59.0, 0.0, 20.0f, "after", new JsonObject()),
            14
        );

        var replay = events.replay("Bot", 0).events();
        assertEquals(3, replay.size());
        assertEquals("death", replay.get(0).name());
        assertEquals("inv-hash", replay.get(0).data().get("inventory_hash").getAsString());
        assertEquals(2, replay.get(0).data().getAsJsonObject("inventory_counts_before").get("minecraft:bread").getAsInt());
        assertEquals(-80.0, replay.get(0).data().getAsJsonArray("pos").get(1).getAsDouble());
        assertEquals("bodyMissing", replay.get(1).name());
        assertEquals("death", replay.get(1).data().get("reason").getAsString());
        assertEquals("respawned", replay.get(2).name());
        assertEquals(59.0, replay.get(2).data().getAsJsonArray("final_pos").get(1).getAsDouble());
    }

    @Test
    void explicitDespawnDoesNotPretendThePlayerDied() {
        BotEventStream events = new BotEventStream();
        BodyLifecycleTracker tracker = new BodyLifecycleTracker(events::emit);
        var snapshot = new BodyLifecycleTracker.Snapshot(1.0, 64.0, 1.0, 20.0f, "hash", new JsonObject());

        tracker.watch("Bot");
        tracker.observePresent("Bot", snapshot, 1);
        tracker.requestDespawn("Bot");
        tracker.afterDeath("Bot", snapshot, 2);
        tracker.observeMissing("Bot", 3);

        var replay = events.replay("Bot", 0).events();
        assertEquals(1, replay.size());
        assertEquals("bodyMissing", replay.getFirst().name());
        assertEquals("despawn", replay.getFirst().data().get("reason").getAsString());
    }
}
