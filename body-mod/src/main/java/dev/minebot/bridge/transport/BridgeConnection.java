package dev.minebot.bridge.transport;

import com.google.gson.Gson;
import com.google.gson.JsonObject;
import dev.minebot.bridge.worldstream.WorldStreamSubscription;
import org.java_websocket.WebSocket;

import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.Executor;
import java.util.concurrent.atomic.AtomicLong;

public final class BridgeConnection {
    private static final Gson GSON = new Gson();

    private final WebSocket socket;
    private final AtomicLong sequence;
    private final Executor outboundExecutor;
    private final Map<String, WorldStreamSubscription> worldStreamSubscriptions = new ConcurrentHashMap<>();

    public BridgeConnection(WebSocket socket, AtomicLong sequence, Executor outboundExecutor) {
        this.socket = socket;
        this.sequence = sequence;
        this.outboundExecutor = outboundExecutor;
    }

    public Map<String, WorldStreamSubscription> worldStreamSubscriptions() {
        return worldStreamSubscriptions;
    }

    public void send(JsonObject message, int serverTick) {
        if (!message.has("seq")) {
            message.addProperty("seq", sequence.incrementAndGet());
        }
        if (!message.has("server_tick")) {
            message.addProperty("server_tick", serverTick);
        }
        if (!message.has("sent_at_ms")) {
            message.addProperty("sent_at_ms", System.currentTimeMillis());
        }
        String encoded = GSON.toJson(message);
        outboundExecutor.execute(() -> {
            if (socket.isOpen()) {
                socket.send(encoded);
            }
        });
    }

    public void close() {
        worldStreamSubscriptions.clear();
    }
}
