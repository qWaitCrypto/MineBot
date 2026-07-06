package dev.minebot.bridge.worldstream;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import dev.minebot.bridge.transport.OutboundMessage;
import dev.minebot.bridge.transport.BridgeConnection;
import dev.minebot.bridge.version.EntitySnapshot;
import dev.minebot.bridge.version.SectionSnapshot;
import dev.minebot.bridge.version.WorldAccess;
import net.minecraft.server.MinecraftServer;

import java.util.concurrent.atomic.AtomicInteger;

public final class WorldStreamChannel {
    private static final int MAX_RADIUS_CHUNKS = 6;
    private static final int MAX_SUBSCRIPTIONS = 4;
    private static final int MAX_RATE_HZ = 20;
    private static final int KEYFRAME_RESYNC_S = 10;

    private final MinecraftServer server;
    private final WorldAccess world;
    private final AtomicInteger tickCounter;

    public WorldStreamChannel(MinecraftServer server, WorldAccess world, AtomicInteger tickCounter) {
        this.server = server;
        this.world = world;
        this.tickCounter = tickCounter;
    }

    public int currentTick() {
        return tickCounter.get();
    }

    public void handle(BridgeConnection connection, JsonObject request) {
        String type = stringField(request, "type");
        if ("HELLO".equals(type)) {
            handleHello(connection, request);
        } else if ("SUBSCRIBE".equals(type)) {
            handleSubscribe(connection, request);
        } else if ("UNSUBSCRIBE".equals(type)) {
            handleUnsubscribe(connection, request);
        } else {
            connection.send(error(stringField(request, "id"), "unknown_type", "unknown world-stream message type", false), currentTick());
        }
    }

    public void tick(BridgeConnection connection, int serverTick) {
        for (WorldStreamSubscription subscription : connection.worldStreamSubscriptions().values()) {
            if (!subscription.shouldSendTransform(serverTick)) {
                continue;
            }
            EntitySnapshot snapshot = world.findEntity(server, subscription.entityName(), subscription.dimension());
            if (snapshot == null) {
                if (!subscription.entityMissing()) {
                    connection.send(error(null, "entity_not_found", "subscribed entity not found: " + subscription.entityName(), true), serverTick);
                    subscription.markEntityMissing(serverTick);
                }
                continue;
            }
            if (subscription.entityMissing()) {
                subscription.markEntityPresent();
                sendKeyframe(connection, subscription, snapshot, serverTick);
            }
            connection.send(transform(subscription, snapshot), serverTick);
            subscription.markTransformSent(serverTick);
        }
    }

    private void handleHello(BridgeConnection connection, JsonObject request) {
        String id = stringField(request, "id");
        String mcVersion = world.minecraftVersion();
        connection.send(new OutboundMessage.Json(() -> helloAck(id, mcVersion)), currentTick());
    }

    private static JsonObject helloAck(String id, String mcVersion) {
        JsonObject response = new JsonObject();
        response.addProperty("channel", "world-stream");
        response.addProperty("type", "HELLO_ACK");
        if (id != null) {
            response.addProperty("id", id);
        }
        response.addProperty("protocol", "world-stream/1");
        response.addProperty("mc_version", mcVersion);
        JsonArray capabilities = new JsonArray();
        capabilities.add("sections");
        capabilities.add("transform");
        response.add("capabilities", capabilities);
        response.addProperty("palette_mode", "per-section-string-state");
        JsonArray encodings = new JsonArray();
        encodings.add("json-array-debug-u16");
        encodings.add("base64-deflate-u8");
        encodings.add("base64-deflate-u16le");
        response.add("encodings", encodings);
        response.addProperty("section_size", 16);
        JsonObject limits = new JsonObject();
        limits.addProperty("max_radius_chunks", MAX_RADIUS_CHUNKS);
        JsonArray maxYBandSections = new JsonArray();
        maxYBandSections.add(4);
        maxYBandSections.add(4);
        limits.add("max_y_band_sections", maxYBandSections);
        limits.addProperty("max_subscriptions", MAX_SUBSCRIPTIONS);
        limits.addProperty("max_rate_hz", MAX_RATE_HZ);
        limits.addProperty("max_keyframes_per_tick", 4);
        limits.addProperty("keyframe_resync_s", KEYFRAME_RESYNC_S);
        response.add("limits", limits);
        return response;
    }

    private void handleSubscribe(BridgeConnection connection, JsonObject request) {
        String id = stringField(request, "id");
        if (connection.worldStreamSubscriptions().size() >= MAX_SUBSCRIPTIONS) {
            connection.send(error(id, "limit_exceeded", "too many world-stream subscriptions", false), currentTick());
            return;
        }
        String subId = stringField(request, "sub_id");
        if (subId == null || subId.isBlank()) {
            connection.send(error(id, "invalid_subscription", "sub_id is required", false), currentTick());
            return;
        }
        JsonObject center = objectField(request, "center");
        if (center == null || !"entity".equals(stringField(center, "type"))) {
            connection.send(error(id, "unsupported_center", "Stage 0 supports center.type=entity", false), currentTick());
            return;
        }
        String entityName = stringField(center, "entity");
        if (entityName == null || entityName.isBlank()) {
            connection.send(error(id, "invalid_subscription", "center.entity is required", false), currentTick());
            return;
        }
        String dimension = stringField(request, "dimension");
        int requestedRateHz = intField(request, "rate_hz", MAX_RATE_HZ);
        int appliedRateHz = Math.max(1, Math.min(MAX_RATE_HZ, requestedRateHz));
        int requestedRadius = intField(request, "radius_chunks", 1);
        int appliedRadius = Math.max(1, Math.min(MAX_RADIUS_CHUNKS, requestedRadius));

        EntitySnapshot snapshot = world.findEntity(server, entityName, dimension);
        if (snapshot == null) {
            connection.send(error(id, "entity_not_found", "entity not found: " + entityName, true), currentTick());
            return;
        }

        WorldStreamSubscription subscription = new WorldStreamSubscription(
            subId,
            entityName,
            world.dimensionId(snapshot.level()),
            appliedRadius,
            appliedRateHz
        );
        connection.worldStreamSubscriptions().put(subId, subscription);
        connection.send(ack(id, subId, appliedRadius, appliedRateHz), currentTick());
        connection.send(transform(subscription, snapshot), currentTick());
        sendKeyframe(connection, subscription, snapshot, currentTick());
    }

    private void handleUnsubscribe(BridgeConnection connection, JsonObject request) {
        String subId = stringField(request, "sub_id");
        if (subId != null) {
            connection.worldStreamSubscriptions().remove(subId);
        }
        connection.send(ack(stringField(request, "id"), subId, 0, 0), currentTick());
    }

    private static OutboundMessage transform(WorldStreamSubscription subscription, EntitySnapshot snapshot) {
        String subId = subscription.subId();
        String entity = snapshot.name();
        String dimension = snapshot.level().dimension().identifier().toString();
        double x = snapshot.x();
        double y = snapshot.y();
        double z = snapshot.z();
        float yaw = snapshot.yaw();
        float pitch = snapshot.pitch();
        boolean onGround = snapshot.onGround();
        String pose = snapshot.pose();
        return new OutboundMessage.Json(() -> {
            JsonObject message = new JsonObject();
            message.addProperty("channel", "world-stream");
            message.addProperty("type", "TRANSFORM");
            message.addProperty("sub_id", subId);
            message.addProperty("entity", entity);
            message.addProperty("dimension", dimension);
            JsonArray pos = new JsonArray();
            pos.add(x);
            pos.add(y);
            pos.add(z);
            message.add("pos", pos);
            message.addProperty("yaw", yaw);
            message.addProperty("pitch", pitch);
            message.addProperty("on_ground", onGround);
            message.addProperty("pose", pose);
            return message;
        });
    }

    private void sendKeyframe(BridgeConnection connection, WorldStreamSubscription subscription, EntitySnapshot snapshot, int serverTick) {
        SectionSnapshot section = world.sectionSnapshot(snapshot.level(), subscription.subId(), snapshot.sectionX(), snapshot.sectionY(), snapshot.sectionZ());
        connection.send(new OutboundMessage.SectionKeyframe(section), serverTick);
    }

    private static OutboundMessage ack(String id, String subId, int radiusChunks, int rateHz) {
        return new OutboundMessage.Json(() -> {
            JsonObject response = new JsonObject();
            response.addProperty("channel", "world-stream");
            response.addProperty("type", "ACK");
            if (id != null) {
                response.addProperty("id", id);
            }
            if (subId != null) {
                response.addProperty("sub_id", subId);
            }
            JsonObject applied = new JsonObject();
            if (radiusChunks > 0) {
                applied.addProperty("radius_chunks", radiusChunks);
            }
            if (rateHz > 0) {
                applied.addProperty("rate_hz", rateHz);
            }
            response.add("applied", applied);
            return response;
        });
    }

    private static OutboundMessage error(String id, String code, String message, boolean retryable) {
        return new OutboundMessage.Json(() -> {
            JsonObject response = new JsonObject();
            response.addProperty("channel", "world-stream");
            response.addProperty("type", "ERROR");
            if (id != null) {
                response.addProperty("id", id);
            }
            response.addProperty("code", code);
            response.addProperty("message", message);
            response.addProperty("retryable", retryable);
            return response;
        });
    }

    private static String stringField(JsonObject object, String name) {
        return object.has(name) && !object.get(name).isJsonNull() ? object.get(name).getAsString() : null;
    }

    private static int intField(JsonObject object, String name, int fallback) {
        try {
            return object.has(name) && !object.get(name).isJsonNull() ? object.get(name).getAsInt() : fallback;
        } catch (RuntimeException ex) {
            return fallback;
        }
    }

    private static JsonObject objectField(JsonObject object, String name) {
        return object.has(name) && object.get(name).isJsonObject() ? object.getAsJsonObject(name) : null;
    }
}
