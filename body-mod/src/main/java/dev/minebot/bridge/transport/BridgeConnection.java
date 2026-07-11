package dev.minebot.bridge.transport;

import com.google.gson.Gson;
import com.google.gson.JsonObject;
import org.java_websocket.WebSocket;

import java.util.UUID;
import java.util.concurrent.Executor;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicLong;

public final class BridgeConnection {
    private static final Gson GSON = new Gson();
    public static final int MAX_REQUESTS_PER_SECOND = 40;

    private final String id = UUID.randomUUID().toString();
    private final WebSocket socket;
    private final Executor outboundExecutor;
    private final AtomicLong sequence = new AtomicLong();
    private final AtomicBoolean closed = new AtomicBoolean();
    private long requestWindowStartedNanos = System.nanoTime();
    private int requestsInWindow;

    public BridgeConnection(WebSocket socket, Executor outboundExecutor) {
        this.socket = socket;
        this.outboundExecutor = outboundExecutor;
    }

    public String id() {
        return id;
    }

    public synchronized boolean allowRequest(long nowNanos) {
        if (nowNanos - requestWindowStartedNanos >= 1_000_000_000L) {
            requestWindowStartedNanos = nowNanos;
            requestsInWindow = 0;
        }
        if (requestsInWindow >= MAX_REQUESTS_PER_SECOND) {
            return false;
        }
        requestsInWindow++;
        return true;
    }

    public boolean send(JsonObject message, int serverTick) {
        return send(new OutboundMessage.Json(() -> message), serverTick);
    }

    public boolean send(OutboundMessage outbound, int serverTick) {
        if (closed.get()) {
            return false;
        }
        try {
            outboundExecutor.execute(() -> encodeAndSend(outbound, serverTick));
            return true;
        } catch (RejectedExecutionException rejected) {
            closeOverloaded();
            return false;
        }
    }

    private void encodeAndSend(OutboundMessage outbound, int serverTick) {
        if (closed.get() || !socket.isOpen()) {
            return;
        }
        JsonObject message = outbound.toJson();
        if (!message.has("seq")) {
            message.addProperty("seq", sequence.incrementAndGet());
        }
        if (!message.has("server_tick")) {
            message.addProperty("server_tick", serverTick);
        }
        if (!message.has("sent_at_ms")) {
            message.addProperty("sent_at_ms", System.currentTimeMillis());
        }
        socket.send(GSON.toJson(message));
    }

    private void closeOverloaded() {
        if (closed.compareAndSet(false, true)) {
            socket.close(1013, "bridge outbound queue full");
        }
    }

    public void close() {
        closed.set(true);
    }
}
