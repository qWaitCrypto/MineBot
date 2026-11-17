package dev.minebot.server.common.transport;

import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
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
import java.util.function.Consumer;

public final class MineBotWebSocketServer extends WebSocketServer {
    public static final int MAX_REQUEST_BYTES = 16 * 1024;
    public static final int MAX_PENDING_OUTBOUND = 256;

    private final Consumer<Runnable> mainThreadExecutor;
    private final MineBotChannelRouter router;
    private final ThreadPoolExecutor outboundExecutor = new ThreadPoolExecutor(
        1,
        1,
        0L,
        TimeUnit.MILLISECONDS,
        new ArrayBlockingQueue<>(MAX_PENDING_OUTBOUND),
        runnable -> {
            Thread thread = new Thread(runnable, "minebot-websocket-outbound");
            thread.setDaemon(true);
            return thread;
        },
        new ThreadPoolExecutor.AbortPolicy()
    );
    private final Map<WebSocket, MineBotConnection> connections = new ConcurrentHashMap<>();
    private volatile int currentTick;

    public MineBotWebSocketServer(
        InetSocketAddress address,
        Consumer<Runnable> mainThreadExecutor,
        MineBotChannelRouter router
    ) {
        super(address);
        this.mainThreadExecutor = mainThreadExecutor;
        this.router = router;
    }

    public void tick(int serverTick) {
        currentTick = serverTick;
        router.tick(serverTick);
    }

    @Override
    public void onOpen(WebSocket socket, ClientHandshake handshake) {
        connections.put(socket, new MineBotConnection(socket, outboundExecutor));
    }

    @Override
    public void onClose(WebSocket socket, int code, String reason, boolean remote) {
        MineBotConnection connection = connections.remove(socket);
        if (connection != null) {
            connection.close();
            mainThreadExecutor.accept(() -> router.connectionClosed(connection, currentTick));
        }
    }

    @Override
    public void onMessage(WebSocket socket, String message) {
        MineBotConnection connection = connections.get(socket);
        if (connection == null) {
            return;
        }
        if (!connection.allowRequest(System.nanoTime())) {
            connection.send(MineBotChannelRouter.error(null, null, "rate_limited", "request rate exceeds limit", true), currentTick);
            return;
        }
        if (message.getBytes(StandardCharsets.UTF_8).length > MAX_REQUEST_BYTES) {
            connection.send(MineBotChannelRouter.error(null, null, "request_too_large", "request exceeds byte limit", false), currentTick);
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
            connection.send(MineBotChannelRouter.error(null, null, "invalid_json", "request must be a JSON object", false), currentTick);
            return;
        }
        mainThreadExecutor.accept(() -> router.dispatch(connection, request, currentTick));
    }

    @Override
    public void onError(WebSocket socket, Exception error) {
        System.err.println("[MineBot] websocket error: " + error.getClass().getSimpleName());
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
        connections.values().forEach(MineBotConnection::close);
        connections.clear();
    }
}
