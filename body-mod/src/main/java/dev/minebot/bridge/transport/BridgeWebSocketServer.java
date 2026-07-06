package dev.minebot.bridge.transport;

import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import dev.minebot.bridge.worldstream.WorldStreamChannel;
import net.minecraft.server.MinecraftServer;
import org.java_websocket.WebSocket;
import org.java_websocket.handshake.ClientHandshake;
import org.java_websocket.server.WebSocketServer;

import java.net.InetSocketAddress;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicLong;

public final class BridgeWebSocketServer extends WebSocketServer {
    private final MinecraftServer server;
    private final WorldStreamChannel worldStream;
    private final AtomicLong sequence = new AtomicLong();
    private final ExecutorService outboundExecutor = Executors.newSingleThreadExecutor(runnable -> {
        Thread thread = new Thread(runnable, "minebot-bridge-outbound");
        thread.setDaemon(true);
        return thread;
    });
    private final Map<WebSocket, BridgeConnection> connections = new ConcurrentHashMap<>();

    public BridgeWebSocketServer(InetSocketAddress address, MinecraftServer server, WorldStreamChannel worldStream) {
        super(address);
        this.server = server;
        this.worldStream = worldStream;
    }

    public void tick(int serverTick) {
        for (BridgeConnection connection : connections.values()) {
            worldStream.tick(connection, serverTick);
        }
    }

    @Override
    public void onOpen(WebSocket conn, ClientHandshake handshake) {
        connections.put(conn, new BridgeConnection(conn, sequence, outboundExecutor));
    }

    @Override
    public void onClose(WebSocket conn, int code, String reason, boolean remote) {
        BridgeConnection connection = connections.remove(conn);
        if (connection != null) {
            connection.close();
        }
    }

    @Override
    public void onMessage(WebSocket conn, String message) {
        BridgeConnection connection = connections.get(conn);
        if (connection == null) {
            return;
        }
        JsonObject request;
        try {
            request = JsonParser.parseString(message).getAsJsonObject();
        } catch (RuntimeException ex) {
            connection.send(error(null, "invalid_json", "request must be a JSON object", false), worldStream.currentTick());
            return;
        }
        server.execute(() -> dispatch(connection, request));
    }

    private void dispatch(BridgeConnection connection, JsonObject request) {
        String channel = stringField(request, "channel");
        if ("world-stream".equals(channel)) {
            worldStream.handle(connection, request);
            return;
        }
        connection.send(error(stringField(request, "id"), "unknown_channel", "unknown channel", false), worldStream.currentTick());
    }

    @Override
    public void onError(WebSocket conn, Exception ex) {
        ex.printStackTrace();
    }

    @Override
    public void onStart() {
        setConnectionLostTimeout(20);
    }

    @Override
    public void stop(int timeout) throws InterruptedException {
        super.stop(timeout);
        outboundExecutor.shutdownNow();
    }

    private static String stringField(JsonObject object, String name) {
        return object.has(name) && !object.get(name).isJsonNull() ? object.get(name).getAsString() : null;
    }

    private static JsonObject error(String id, String code, String message, boolean retryable) {
        JsonObject error = new JsonObject();
        error.addProperty("channel", "world-stream");
        error.addProperty("type", "ERROR");
        if (id != null) {
            error.addProperty("id", id);
        }
        error.addProperty("code", code);
        error.addProperty("message", message);
        error.addProperty("retryable", retryable);
        return error;
    }
}
