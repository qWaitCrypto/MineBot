package dev.minebot.body.event;

import com.google.gson.JsonObject;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

final class BotEventStreamTest {
    @Test
    void sequencesArePerBotMonotoneFromOne() {
        BotEventStream stream = new BotEventStream();
        assertEquals(1, stream.emit("A", 10, "e1", null, null).seq());
        assertEquals(2, stream.emit("A", 11, "e2", null, null).seq());
        assertEquals(1, stream.emit("B", 11, "e1", null, null).seq());
        assertEquals(2, stream.lastSeq("A"));
        assertEquals(0, stream.lastSeq("unknown"));
    }

    @Test
    void replayReturnsEverythingAfterTheCursor() {
        BotEventStream stream = new BotEventStream();
        stream.emit("A", 10, "e1", null, null);
        stream.emit("A", 11, "e2", "a-1", null);
        stream.emit("A", 12, "e3", null, null);

        BotEventStream.Replay replay = stream.replay("A", 1);

        assertFalse(replay.hasGap());
        assertEquals(2, replay.events().size());
        assertEquals("e2", replay.events().get(0).name());
        assertEquals("a-1", replay.events().get(0).actionId());
    }

    @Test
    void evictionPastTheCursorIsATypedGapNeverSilence() {
        BotEventStream stream = new BotEventStream();
        for (int i = 0; i < BotEventStream.MAX_BUFFERED_EVENTS_PER_BOT + 10; i++) {
            stream.emit("A", i, "e", null, null);
        }

        BotEventStream.Replay replay = stream.replay("A", 3);

        assertTrue(replay.hasGap());
        assertEquals(4, replay.gapFrom());
        assertEquals(10, replay.gapTo());
        assertEquals(BotEventStream.MAX_BUFFERED_EVENTS_PER_BOT, replay.events().size());
        assertEquals(11, replay.events().get(0).seq());
    }

    @Test
    void replayForAnUnknownBotIsEmptyWithoutGap() {
        BotEventStream.Replay replay = new BotEventStream().replay("nobody", 0);
        assertFalse(replay.hasGap());
        assertTrue(replay.events().isEmpty());
    }

    @Test
    void eventJsonCarriesTheProtocolEnvelope() {
        JsonObject data = new JsonObject();
        data.addProperty("k", "v");
        BotEventStream stream = new BotEventStream();
        JsonObject json = stream.emit("A", 42, "waypoint_reached", "a-7", data).toJson("fakeplayer-body");

        assertEquals("fakeplayer-body", json.get("channel").getAsString());
        assertEquals("EVENT", json.get("type").getAsString());
        assertEquals("A", json.get("bot").getAsString());
        assertEquals(1, json.get("seq").getAsLong());
        assertEquals(42, json.get("tick").getAsInt());
        assertEquals("waypoint_reached", json.get("event").getAsString());
        assertEquals("a-7", json.get("action_id").getAsString());
        assertEquals("v", json.getAsJsonObject("data").get("k").getAsString());

        JsonObject noAction = stream.emit("A", 43, "tick", null, null).toJson("fakeplayer-body");
        assertNull(noAction.get("action_id"));
        assertTrue(noAction.getAsJsonObject("data").isEmpty());
    }
}
