package dev.minebot.body.interaction;

import com.google.gson.JsonObject;
import dev.minebot.body.event.BotEventStream;

import java.util.LinkedHashSet;
import java.util.Set;

/** Per-bot replayable public-chat ingress, separate from physical action events. */
public final class InteractiveChatStream {
    private final BotEventStream events = new BotEventStream();
    private final Set<String> watchedBots = new LinkedHashSet<>();

    public synchronized void watch(String botName) {
        watchedBots.add(botName);
    }

    public void receive(String sender, String message, int serverTick, boolean excludedSender) {
        if (excludedSender || message == null || message.isBlank()) {
            return;
        }
        Set<String> recipients;
        synchronized (this) {
            recipients = Set.copyOf(watchedBots);
        }
        for (String botName : recipients) {
            if (botName.equals(sender)) {
                continue;
            }
            JsonObject data = new JsonObject();
            data.addProperty("sender", sender);
            data.addProperty("message", message);
            events.emit(botName, serverTick, "agentChat", null, data);
        }
    }

    public BotEventStream.Replay replay(String botName, long afterSeq) {
        watch(botName);
        return events.replay(botName, afterSeq);
    }

    public long lastSeq(String botName) {
        return events.lastSeq(botName);
    }
}
