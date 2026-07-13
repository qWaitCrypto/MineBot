package dev.minebot.bridge.transport;

import com.google.gson.JsonObject;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

public final class BridgeChannelRouter {
    private final Map<String, BridgeChannel> channels;

    public BridgeChannelRouter(List<BridgeChannel> configuredChannels) {
        Map<String, BridgeChannel> byName = new LinkedHashMap<>();
        for (BridgeChannel channel : configuredChannels) {
            if (channel.name().isBlank() || byName.putIfAbsent(channel.name(), channel) != null) {
                throw new IllegalArgumentException("bridge channel names must be unique and nonblank");
            }
        }
        channels = Map.copyOf(byName);
    }

    public void dispatch(BridgeConnection connection, JsonObject request, int serverTick) {
        String channelName = stringField(request, "channel");
        BridgeChannel channel = channels.get(channelName);
        if (channel == null) {
            connection.send(error(channelName, stringField(request, "request_id"), "unknown_channel", "unknown channel", false), serverTick);
            return;
        }
        channel.handle(connection, request, serverTick);
    }

    public void tick(int serverTick) {
        for (BridgeChannel channel : channels.values()) {
            channel.tick(serverTick);
        }
    }

    public void connectionClosed(BridgeConnection connection, int serverTick) {
        for (BridgeChannel channel : channels.values()) {
            channel.connectionClosed(connection, serverTick);
        }
    }

    public void close(int serverTick) {
        for (BridgeChannel channel : channels.values()) {
            channel.close(serverTick);
        }
    }

    public static JsonObject error(
        String channel,
        String requestId,
        String code,
        String message,
        boolean retryable
    ) {
        JsonObject error = new JsonObject();
        error.addProperty("channel", channel == null ? "bridge" : channel);
        error.addProperty("type", "ERROR");
        if (requestId != null) {
            error.addProperty("request_id", requestId);
        }
        error.addProperty("code", code);
        error.addProperty("message", message);
        error.addProperty("retryable", retryable);
        return error;
    }

    public static String stringField(JsonObject object, String name) {
        if (!object.has(name) || object.get(name).isJsonNull() || !object.get(name).isJsonPrimitive()) {
            return null;
        }
        try {
            return object.get(name).getAsString();
        } catch (RuntimeException ignored) {
            return null;
        }
    }
}
