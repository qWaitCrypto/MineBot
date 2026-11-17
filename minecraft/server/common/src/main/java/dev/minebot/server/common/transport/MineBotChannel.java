package dev.minebot.server.common.transport;

import com.google.gson.JsonObject;

public interface MineBotChannel {
    String name();

    void handle(MineBotConnection connection, JsonObject request, int serverTick);

    default void tick(int serverTick) {
    }

    default void connectionClosed(MineBotConnection connection, int serverTick) {
    }

    default void close(int serverTick) {
    }
}
