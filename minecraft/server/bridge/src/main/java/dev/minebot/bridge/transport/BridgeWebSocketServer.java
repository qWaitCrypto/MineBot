package dev.minebot.bridge.transport;

import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import net.minecraft.server.MinecraftServer;
import org.java_websocket.WebSocket;
import org.java_websocket.handshake.ClientHandshake;
import org.java_websocket.server.WebSocketServer;

import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.Map;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;

public final class BridgeWebSocketServer extends WebSocketServer {
    public static final int MAX_REQUEST_BYTES = 16 * 1024;
    public static final int MAX_PENDING_OUTBOUND = 256;

    private final MinecraftServer server;
    private final BridgeChannelRouter router;
    private final ThreadPoolExecutor outboundExecutor = new ThreadPoolExecutor(
        1,
        1,
        0L,
        TimeUnit.MILLISECONDS,
        new ArrayBlockingQueue<>(MAX_PENDING_OUTBOUND),
        runnable -> {
            Thread thread = new Thread(runnable, "minebot-bridge-outbound");
            thread.setDaemon(true);
            return thread;
        },
        new ThreadPoolExecutor.AbortPolicy()
    );
    private final Map<WebSocket, BridgeConnection> connections = new ConcurrentHashMap<>();
    private volatile int currentTick;

    public BridgeWebSocketServer(
        InetSocketAddress address,
        MinecraftServer server,
        BridgeChannelRouter router
    ) {
        super(address);
        this.server = server;
        this.router = router;
    }

    public void tick(int serverTick) {
        currentTick = serverTick;
        router.tick(serverTick);
    }

    @Override
    public void onOpen(WebSocket socket, ClientHandshake handshake) {
        connections.put(socket, new BridgeConnection(socket, outboundExecutor));
    }

    @Override
    public void onClose(WebSocket socket, int code, String reason, boolean remote) {
        BridgeConnection connection = connections.remove(socket);
        if (connection != null) {
            connection.close();
            server.execute(() -> router.connectionClosed(connection, currentTick));
        }
    }

    @Override
    public void onMessage(WebSocket socket, String message) {
        BridgeConnection connection = connections.get(socket);
        if (connection == null) {
            return;
        }
        if (!connection.allowRequest(System.nanoTime())) {
            connection.send(BridgeChannelRouter.error(null, null, "rate_limited", "request rate exceeds limit", true), currentTick);
            return;
        }
        if (message.getBytes(StandardCharsets.UTF_8).length > MAX_REQUEST_BYTES) {
            connection.send(BridgeChannelRouter.error(null, null, "request_too_large", "request exceeds byte limit", false), currentTick);
            return;
        }
        JsonObject request;
        try {
            JsonElement parsed = JsonParser.parseString(message);
            if (!parsed.isJsonObject()) {
                throw new IllegalArgumentException("not an object");
            }
            request = parsed.getAsJsonObject();
        } catch (RuntimeException invalid) {
            connection.send(BridgeChannelRouter.error(null, null, "invalid_json", "request must be a JSON object", false), currentTick);
            return;
        }
        server.execute(() -> router.dispatch(connection, request, currentTick));
    }

    @Override
    public void onError(WebSocket socket, Exception error) {
        System.err.println("[MineBotBridge] websocket error: " + error.getClass().getSimpleName());
    }

    @Override
    public void onStart() {
        setConnectionLostTimeout(20);
    }

    @Override
    public void stop(int timeout) throws InterruptedException {
        router.close(currentTick);
        super.stop(timeout);
        outboundExecutor.shutdownNow();
        connections.values().forEach(BridgeConnection::close);
        connections.clear();
    }
}
