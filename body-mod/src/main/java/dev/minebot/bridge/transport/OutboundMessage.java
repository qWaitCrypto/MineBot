package dev.minebot.bridge.transport;

import com.google.gson.JsonObject;

import java.util.function.Supplier;

public sealed interface OutboundMessage permits OutboundMessage.Json, OutboundMessage.SectionKeyframe {
    JsonObject toJson();

    record Json(Supplier<JsonObject> builder) implements OutboundMessage {
        @Override
        public JsonObject toJson() {
            return builder.get();
        }
    }

    record SectionKeyframe(dev.minebot.bridge.version.SectionSnapshot snapshot) implements OutboundMessage {
        @Override
        public JsonObject toJson() {
            return SectionKeyframeEncoder.encode(snapshot);
        }
    }
}
