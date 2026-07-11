package dev.minebot.bridge.transport;

import com.google.gson.JsonObject;

public interface BridgeChannel {
    String name();

    void handle(BridgeConnection connection, JsonObject request, int serverTick);

    default void tick(int serverTick) {
    }

    default void connectionClosed(BridgeConnection connection, int serverTick) {
    }

    default void close(int serverTick) {
    }
}
