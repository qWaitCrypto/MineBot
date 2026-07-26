package dev.minebot.body.event;

import com.google.gson.JsonObject;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * Per-bot ordered event stream with a bounded replay buffer. Every event
 * carries a per-bot monotonic {@code seq} and the server tick. Replay from a
 * cursor returns everything still buffered after it; when the buffer no
 * longer covers the cursor the loss is a typed gap, never silence.
 */
public final class BotEventStream {
    public static final int MAX_BUFFERED_EVENTS_PER_BOT = 1_024;

    public record Event(String bot, long seq, int tick, String name, String actionId, JsonObject data) {
        public JsonObject toJson(String channel) {
            JsonObject json = new JsonObject();
            json.addProperty("channel", channel);
            json.addProperty("type", "EVENT");
            json.addProperty("bot", bot);
            json.addProperty("seq", seq);
            json.addProperty("tick", tick);
            json.addProperty("event", name);
            if (actionId != null) {
                json.addProperty("action_id", actionId);
            }
            json.add("data", data == null ? new JsonObject() : data);
            return json;
        }
    }

    public record Replay(List<Event> events, long gapFrom, long gapTo) {
        public boolean hasGap() {
            return gapFrom > 0;
        }
    }

    private static final class BotBuffer {
        long nextSeq = 1;
        long droppedThroughSeq;
        final ArrayDeque<Event> events = new ArrayDeque<>();
    }

    private final Map<String, BotBuffer> buffers = new HashMap<>();
    private volatile java.util.function.Consumer<Event> listener;

    /**
     * One listener receives every emitted event for live push. Every emitter
     * (runtime lifecycle, executors, reflexes) goes through {@link #emit}, so
     * wiring the push here guarantees no event category is buffered-but-silent.
     */
    public void setListener(java.util.function.Consumer<Event> listener) {
        this.listener = listener;
    }

    public Event emit(String bot, int tick, String name, String actionId, JsonObject data) {
        Event event;
        synchronized (this) {
            BotBuffer buffer = buffers.computeIfAbsent(bot, ignored -> new BotBuffer());
            event = new Event(bot, buffer.nextSeq++, tick, name, actionId, data);
            buffer.events.addLast(event);
            while (buffer.events.size() > MAX_BUFFERED_EVENTS_PER_BOT) {
                Event dropped = buffer.events.removeFirst();
                buffer.droppedThroughSeq = dropped.seq();
            }
        }
        java.util.function.Consumer<Event> push = listener;
        if (push != null) {
            push.accept(event);
        }
        return event;
    }

    /** Buffered events with {@code seq > afterSeq}; a gap when eviction passed the cursor. */
    public synchronized Replay replay(String bot, long afterSeq) {
        BotBuffer buffer = buffers.get(bot);
        if (buffer == null) {
            return new Replay(List.of(), 0, 0);
        }
        List<Event> events = new ArrayList<>();
        for (Event event : buffer.events) {
            if (event.seq() > afterSeq) {
                events.add(event);
            }
        }
        if (afterSeq < buffer.droppedThroughSeq) {
            return new Replay(List.copyOf(events), afterSeq + 1, buffer.droppedThroughSeq);
        }
        return new Replay(List.copyOf(events), 0, 0);
    }

    public synchronized long lastSeq(String bot) {
        BotBuffer buffer = buffers.get(bot);
        return buffer == null ? 0 : buffer.nextSeq - 1;
    }
}
