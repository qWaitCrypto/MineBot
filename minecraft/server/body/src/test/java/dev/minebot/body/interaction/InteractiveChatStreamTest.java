package dev.minebot.body.interaction;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

final class InteractiveChatStreamTest {
    @Test
    void watchStartsCaptureBeforeTheFirstReplay() {
        InteractiveChatStream stream = new InteractiveChatStream();

        stream.receive("Guide", "before handshake", 40, false);
        stream.watch("Bot");
        stream.receive("Guide", "/goal collect one log", 42, false);

        var replay = stream.replay("Bot", 0);
        assertEquals(1, replay.events().size());
        assertEquals(
            "/goal collect one log",
            replay.events().get(0).data().get("message").getAsString()
        );
    }

    @Test
    void fansPublicChatOutToEveryWatchedBotExceptTheSender() {
        InteractiveChatStream stream = new InteractiveChatStream();
        stream.watch("A");
        stream.watch("B");

        stream.receive("Guide", "/goal collect one log", 42, false);
        stream.receive("A", "self chatter", 43, false);

        var a = stream.replay("A", 0);
        var b = stream.replay("B", 0);
        assertEquals(1, a.events().size());
        assertEquals("Guide", a.events().get(0).data().get("sender").getAsString());
        assertEquals("/goal collect one log", a.events().get(0).data().get("message").getAsString());
        assertEquals(2, b.events().size());
        assertEquals("self chatter", b.events().get(1).data().get("message").getAsString());
    }

    @Test
    void excludedCameraChatAndBlankMessagesNeverEnterTheStream() {
        InteractiveChatStream stream = new InteractiveChatStream();
        stream.watch("Bot");

        stream.receive("Camera", "ignored", 1, true);
        stream.receive("Guide", "  ", 2, false);

        assertTrue(stream.replay("Bot", 0).events().isEmpty());
        assertEquals(0, stream.lastSeq("Bot"));
    }

    @Test
    void replayUsesTheSameTypedGapDisciplineAsBodyEvents() {
        InteractiveChatStream stream = new InteractiveChatStream();
        stream.watch("Bot");
        for (int i = 0; i < 1_034; i++) {
            stream.receive("Guide", "m" + i, i, false);
        }

        var replay = stream.replay("Bot", 3);
        assertTrue(replay.hasGap());
        assertEquals(4, replay.gapFrom());
        assertEquals(10, replay.gapTo());
        assertFalse(replay.events().isEmpty());
    }
}
