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
    private final AtomicLong sequence = new AtomicLong();
    private final Executor outboundExecutor;
    private final Map<String, WorldStreamSubscription> worldStreamSubscriptions = new ConcurrentHashMap<>();

    public BridgeConnection(WebSocket socket, Executor outboundExecutor) {
        this.socket = socket;
        this.outboundExecutor = outboundExecutor;
    }

    public Map<String, WorldStreamSubscription> worldStreamSubscriptions() {
        return worldStreamSubscriptions;
    }

    public void send(JsonObject message, int serverTick) {
        send(new OutboundMessage.Json(() -> message), serverTick);
    }

    public void send(OutboundMessage outbound, int serverTick) {
        long seq = sequence.incrementAndGet();
        long sentAtMs = System.currentTimeMillis();
        outboundExecutor.execute(() -> {
            JsonObject message = outbound.toJson();
            if (!message.has("seq")) {
                message.addProperty("seq", seq);
            }
            if (!message.has("server_tick")) {
                message.addProperty("server_tick", serverTick);
            }
            if (!message.has("sent_at_ms")) {
                message.addProperty("sent_at_ms", sentAtMs);
            }
            String encoded = GSON.toJson(message);
            if (socket.isOpen()) {
                socket.send(encoded);
            }
        });
    }

    public void close() {
        worldStreamSubscriptions.clear();
    }
}
