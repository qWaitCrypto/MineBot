package dev.minebot.bridge.transport;

import com.google.gson.JsonObject;

import java.util.function.Supplier;

public sealed interface OutboundMessage permits OutboundMessage.Json {
    JsonObject toJson();

    record Json(Supplier<JsonObject> builder) implements OutboundMessage {
        @Override
        public JsonObject toJson() {
            return builder.get();
        }
    }
}
