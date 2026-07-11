package dev.minebot.bridge.observercontrol;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import dev.minebot.camera.protocol.CameraClientTelemetry;
import dev.minebot.camera.protocol.CameraDirective;
import dev.minebot.camera.protocol.FixedPose;
import dev.minebot.camera.protocol.FollowSettings;
import dev.minebot.bridge.transport.BridgeChannel;
import dev.minebot.bridge.transport.BridgeChannelRouter;
import dev.minebot.bridge.transport.BridgeConnection;
import dev.minebot.bridge.transport.BridgeWebSocketServer;
import dev.minebot.bridge.version.ObserverAccess;
import dev.minebot.bridge.version.ObserverAccessException;
import dev.minebot.bridge.version.ObserverState;
import dev.minebot.bridge.version.TargetState;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.level.ServerLevel;

import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Set;
import java.util.UUID;

public final class ObserverControlChannel implements BridgeChannel {
    public static final String CHANNEL = "observer-control";
    public static final String PROTOCOL = "observer-control/1";
    public static final int MAX_IDEMPOTENCY_ENTRIES = 128;
    private static final int CONTROL_INTERVAL_TICKS = 5;
    private static final Set<String> REQUEST_TYPES = Set.of(
        "HELLO", "ATTACH", "UPDATE", "HEARTBEAT", "STATUS", "DETACH"
    );

    private final MinecraftServer server;
    private final ObserverAccess access;
    private final UUID configuredObserverId;
    private final int leaseTtlTicks;
    private final LinkedHashMap<String, JsonObject> recentResponses = new LinkedHashMap<>();
    private Session session;
    private long lastGeneration;
    private int currentTick;
    private String lastErrorCode;
    private ClientTelemetrySnapshot clientTelemetry;

    public ObserverControlChannel(
        MinecraftServer server,
        ObserverAccess access,
        UUID configuredObserverId,
        int leaseTtlTicks
    ) {
        if (leaseTtlTicks < 40 || leaseTtlTicks > 1200) {
            throw new IllegalArgumentException("lease TTL must be between 40 and 1200 ticks");
        }
        this.server = server;
        this.access = access;
        this.configuredObserverId = configuredObserverId;
        this.leaseTtlTicks = leaseTtlTicks;
    }

    @Override
    public String name() {
        return CHANNEL;
    }

    @Override
    public void handle(BridgeConnection connection, JsonObject request, int serverTick) {
        currentTick = serverTick;
        String type = BridgeChannelRouter.stringField(request, "type");
        if (type == null || !REQUEST_TYPES.contains(type)) {
            sendError(connection, request, "unknown_type", "unknown observer-control request type", false);
            return;
        }
        try {
            switch (type) {
                case "HELLO" -> handleHello(connection, request);
                case "ATTACH" -> handleAttach(connection, request);
                case "UPDATE" -> handleUpdate(connection, request);
                case "HEARTBEAT" -> handleHeartbeat(connection, request);
                case "STATUS" -> handleStatus(connection, request);
                case "DETACH" -> handleDetach(connection, request);
                default -> throw new IllegalStateException("unreachable request type");
            }
        } catch (ObserverAccessException error) {
            lastErrorCode = error.code();
            sendError(connection, request, error.code(), error.getMessage(), error.retryable());
        } catch (IllegalArgumentException error) {
            sendError(connection, request, "invalid_request", error.getMessage(), false);
        } catch (RuntimeException error) {
            lastErrorCode = "internal_error";
            sendError(connection, request, "internal_error", "observer-control request failed", true);
        }
    }

    @Override
    public void tick(int serverTick) {
        currentTick = serverTick;
        Session active = session;
        if (active == null) {
            return;
        }
        if (serverTick >= active.leaseExpiresAtTick) {
            expireSession(true, "lease_expired");
            return;
        }
        if (serverTick < active.nextControlTick) {
            return;
        }
        active.nextControlTick = serverTick + CONTROL_INTERVAL_TICKS;
        try {
            access.enforceObserverPolicy(server, configuredObserverId);
            if (active.mode == ObserverMode.FOLLOW) {
                TargetState target = access.resolveTarget(server, active.targetId, active.targetName);
                active.targetId = target.id();
                active.targetName = target.name();
                access.attachToTarget(server, configuredObserverId, target.id());
                active.state = "follow";
            }
            lastErrorCode = null;
        } catch (ObserverAccessException error) {
            lastErrorCode = error.code();
            active.state = switch (error.code()) {
                case "observer_offline" -> "recovering";
                case "target_missing" -> "target_missing";
                default -> error.retryable() ? "recovering" : "policy_failed";
            };
            if (!error.retryable()) {
                expireSession(true, error.code());
            }
        }
    }

    @Override
    public void close(int serverTick) {
        currentTick = serverTick;
        expireSession(true, "bridge_stopping");
    }

    public void acceptClientTelemetry(UUID senderId, CameraClientTelemetry telemetry, int serverTick) {
        Session active = session;
        if (active == null || !isCurrentClientTelemetry(
            configuredObserverId,
            senderId,
            active.generation,
            active.mode,
            telemetry
        )) {
            return;
        }
        clientTelemetry = new ClientTelemetrySnapshot(telemetry, serverTick);
    }

    static boolean isCurrentClientTelemetry(
        UUID configuredObserverId,
        UUID senderId,
        long activeGeneration,
        ObserverMode activeMode,
        CameraClientTelemetry telemetry
    ) {
        return configuredObserverId.equals(senderId)
            && telemetry.generation() == activeGeneration
            && telemetry.mode().name().equals(activeMode.name());
    }

    private void handleHello(BridgeConnection connection, JsonObject request) {
        String protocol = requiredString(request, "protocol", 64);
        if (!PROTOCOL.equals(protocol)) {
            throw new IllegalArgumentException("unsupported observer-control protocol");
        }
        JsonObject response = baseResponse(request, "HELLO_ACK");
        response.addProperty("protocol", PROTOCOL);
        response.addProperty("minecraft_version", access.minecraftVersion());
        response.addProperty("max_request_bytes", BridgeWebSocketServer.MAX_REQUEST_BYTES);
        response.addProperty("max_requests_per_second", BridgeConnection.MAX_REQUESTS_PER_SECOND);
        response.addProperty("lease_ttl_ticks", leaseTtlTicks);
        JsonArray requestTypes = new JsonArray();
        REQUEST_TYPES.stream().sorted().forEach(requestTypes::add);
        response.add("request_types", requestTypes);
        connection.send(response, currentTick);
    }

    private void handleAttach(BridgeConnection connection, JsonObject request) {
        Mutation mutation = mutation(request);
        JsonObject cached = cached(mutation);
        if (cached != null) {
            connection.send(cached, currentTick);
            return;
        }
        if (session != null) {
            throw new IllegalArgumentException("another observer lease is active");
        }
        if (mutation.generation <= lastGeneration) {
            throw new IllegalArgumentException("generation is stale");
        }

        ObserverMode mode = ObserverMode.parse(requiredString(request, "mode", 16));
        Session candidate = new Session(mutation, mode, currentTick + leaseTtlTicks);
        access.enforceObserverPolicy(server, configuredObserverId);
        applyMode(candidate, request);
        session = candidate;
        clientTelemetry = null;
        lastGeneration = Math.max(lastGeneration, mutation.generation);
        lastErrorCode = null;

        JsonObject response = sessionResponse(request, "ATTACH_ACK", candidate);
        cache(mutation, response);
        connection.send(response, currentTick);
    }

    private void handleUpdate(BridgeConnection connection, JsonObject request) {
        Mutation mutation = mutation(request);
        JsonObject cached = cached(mutation);
        if (cached != null) {
            connection.send(cached, currentTick);
            return;
        }
        Session active = requireSuccessorSession(mutation);
        ObserverMode mode = ObserverMode.parse(requiredString(request, "mode", 16));
        Session candidate = active.copyForMutation(mutation, mode);
        applyMode(candidate, request);
        candidate.leaseExpiresAtTick = currentTick + leaseTtlTicks;
        candidate.nextControlTick = currentTick + CONTROL_INTERVAL_TICKS;
        session = candidate;
        clientTelemetry = null;
        lastGeneration = Math.max(lastGeneration, mutation.generation);
        lastErrorCode = null;

        JsonObject response = sessionResponse(request, "UPDATE_ACK", candidate);
        cache(mutation, response);
        connection.send(response, currentTick);
    }

    private void handleHeartbeat(BridgeConnection connection, JsonObject request) {
        Mutation mutation = mutation(request);
        JsonObject cached = cached(mutation);
        if (cached != null) {
            connection.send(cached, currentTick);
            return;
        }
        Session active = requireCurrentSession(mutation);
        active.leaseExpiresAtTick = currentTick + leaseTtlTicks;
        JsonObject response = sessionResponse(request, "HEARTBEAT_ACK", active);
        cache(mutation, response);
        connection.send(response, currentTick);
    }

    private void handleStatus(BridgeConnection connection, JsonObject request) {
        JsonObject response = baseResponse(request, "STATUS_ACK");
        response.add("observer", observerJson(access.observerState(server, configuredObserverId)));
        response.add("server", serverJson(server));
        Session active = session;
        if (active == null) {
            response.addProperty("state", "detached");
        } else {
            response.addProperty("state", active.state);
            response.addProperty("mode", active.mode.wireName());
            response.addProperty("generation", active.generation);
            response.addProperty("lease_remaining_ticks", Math.max(0, active.leaseExpiresAtTick - currentTick));
            if (active.targetId != null) {
                response.addProperty("target_id", active.targetId.toString());
            }
            if (active.targetName != null) {
                response.addProperty("target_name", active.targetName);
            }
        }
        if (lastErrorCode != null) {
            response.addProperty("error_code", lastErrorCode);
        }
        ClientTelemetrySnapshot telemetry = clientTelemetry;
        if (active != null && telemetry != null && telemetry.value.generation() == active.generation) {
            response.add("client_telemetry", clientTelemetryJson(telemetry, currentTick));
        }
        connection.send(response, currentTick);
    }

    private static JsonObject clientTelemetryJson(ClientTelemetrySnapshot snapshot, int currentTick) {
        CameraClientTelemetry telemetry = snapshot.value;
        int ageTicks = Math.max(0, currentTick - snapshot.receivedAtTick);
        JsonObject value = new JsonObject();
        value.addProperty("generation", telemetry.generation());
        value.addProperty("mode", telemetry.mode().name().toLowerCase(java.util.Locale.ROOT));
        value.addProperty("adapter_state", telemetry.adapterState());
        value.addProperty("sample_count", telemetry.sampleCount());
        value.addProperty("window_ms", telemetry.windowMillis());
        value.addProperty("mean_frame_ms", telemetry.meanFrameMillis());
        value.addProperty("p50_frame_ms", telemetry.p50FrameMillis());
        value.addProperty("p95_frame_ms", telemetry.p95FrameMillis());
        value.addProperty("p99_frame_ms", telemetry.p99FrameMillis());
        value.addProperty("age_ticks", ageTicks);
        value.addProperty("stale", ageTicks > 60);
        return value;
    }

    private static JsonObject serverJson(MinecraftServer server) {
        int loadedChunks = 0;
        for (ServerLevel level : server.getAllLevels()) {
            loadedChunks += level.getChunkSource().getLoadedChunksCount();
        }
        JsonObject value = new JsonObject();
        value.addProperty("loaded_chunks", loadedChunks);
        value.addProperty("online_players", server.getPlayerCount());
        value.addProperty("max_players", server.getMaxPlayers());
        value.add("observer_denials", ObserverInteractionPolicy.denialCountsJson());
        return value;
    }

    private void handleDetach(BridgeConnection connection, JsonObject request) {
        Mutation mutation = mutation(request);
        JsonObject cached = cached(mutation);
        if (cached != null) {
            connection.send(cached, currentTick);
            return;
        }
        Session active = requireSuccessorSession(mutation);
        Session candidate = active.copyForMutation(mutation, active.mode);
        JsonObject response = sessionResponse(request, "DETACH_ACK", candidate);
        cache(mutation, response);
        expireSession(false, "detached", mutation.generation);
        connection.send(response, currentTick);
    }

    private void applyMode(Session candidate, JsonObject request) {
        if (candidate.mode == ObserverMode.FOLLOW) {
            UUID targetId = optionalUuid(request, "target_id");
            String targetName = optionalString(request, "target_name", 64);
            if (targetId == null && targetName == null) {
                throw new IllegalArgumentException("follow mode requires target_id or target_name");
            }
            FollowSettings follow = parseFollow(request.getAsJsonObject("follow"));
            TargetState target = access.resolveTarget(server, targetId, targetName);
            if (target.id().equals(configuredObserverId)) {
                throw new IllegalArgumentException("observer cannot target itself");
            }
            access.attachToTarget(server, configuredObserverId, target.id());
            access.sendDirective(
                server,
                configuredObserverId,
                CameraDirective.follow(candidate.generation, target.id(), follow)
            );
            candidate.targetId = target.id();
            candidate.targetName = target.name();
            candidate.follow = follow;
            candidate.fixed = null;
            candidate.state = "follow";
            return;
        }
        FixedPose fixed = parseFixed(request.getAsJsonObject("fixed"));
        access.holdFixed(
            server,
            configuredObserverId,
            fixed.dimension(),
            fixed.x(),
            fixed.y(),
            fixed.z(),
            fixed.yaw(),
            fixed.pitch(),
            fixed.allowChunkLoading()
        );
        access.sendDirective(server, configuredObserverId, CameraDirective.fixed(candidate.generation, fixed));
        candidate.targetId = null;
        candidate.targetName = null;
        candidate.follow = null;
        candidate.fixed = fixed;
        candidate.state = "fixed";
    }

    private void expireSession(boolean disconnect, String reason) {
        Session active = session;
        long cleanupGeneration = active == null ? lastGeneration : nextGeneration(active.generation);
        expireSession(disconnect, reason, cleanupGeneration);
    }

    private void expireSession(boolean disconnect, String reason, long cleanupGeneration) {
        Session active = session;
        session = null;
        clientTelemetry = null;
        if (active != null) {
            lastGeneration = Math.max(lastGeneration, cleanupGeneration);
            try {
                access.sendDirective(server, configuredObserverId, CameraDirective.detached(cleanupGeneration));
            } catch (RuntimeException ignored) {
                // Disconnect cleanup below remains authoritative when the client is unavailable.
            }
        }
        try {
            access.detach(server, configuredObserverId, disconnect);
        } catch (RuntimeException ignored) {
            lastErrorCode = "detach_failed";
        }
        if ("detached".equals(reason)) {
            lastErrorCode = null;
        } else {
            lastErrorCode = reason;
        }
    }

    private Session requireCurrentSession(Mutation mutation) {
        Session active = session;
        if (active == null) {
            throw new IllegalArgumentException("no observer lease is active");
        }
        if (!isCurrentGeneration(active.leaseId, active.generation, mutation.leaseId, mutation.generation)) {
            throw new IllegalArgumentException("lease or generation is stale");
        }
        return active;
    }

    private Session requireSuccessorSession(Mutation mutation) {
        Session active = session;
        if (active == null) {
            throw new IllegalArgumentException("no observer lease is active");
        }
        if (!isSuccessorGeneration(active.leaseId, active.generation, mutation.leaseId, mutation.generation)) {
            throw new IllegalArgumentException("lease or generation is stale");
        }
        return active;
    }

    static boolean isCurrentGeneration(
        String activeLeaseId,
        long activeGeneration,
        String requestLeaseId,
        long requestGeneration
    ) {
        return activeLeaseId.equals(requestLeaseId) && activeGeneration == requestGeneration;
    }

    static boolean isSuccessorGeneration(
        String activeLeaseId,
        long activeGeneration,
        String requestLeaseId,
        long requestGeneration
    ) {
        return activeLeaseId.equals(requestLeaseId) && requestGeneration > activeGeneration;
    }

    private static long nextGeneration(long generation) {
        return generation == Long.MAX_VALUE ? generation : generation + 1;
    }

    private Mutation mutation(JsonObject request) {
        String requestId = requiredString(request, "request_id", 128);
        String leaseId = requiredString(request, "lease_id", 128);
        if (leaseId.length() < 16) {
            throw new IllegalArgumentException("lease_id is too short");
        }
        UUID observerId = requiredUuid(request, "observer_id");
        if (!configuredObserverId.equals(observerId)) {
            throw new IllegalArgumentException("observer_id is not the configured observer");
        }
        long generation = requiredLong(request, "generation", 1, Long.MAX_VALUE);
        return new Mutation(requestId, leaseId, generation);
    }

    private JsonObject cached(Mutation mutation) {
        JsonObject response = recentResponses.get(mutation.cacheKey());
        return response == null ? null : response.deepCopy();
    }

    private void cache(Mutation mutation, JsonObject response) {
        recentResponses.put(mutation.cacheKey(), response.deepCopy());
        while (recentResponses.size() > MAX_IDEMPOTENCY_ENTRIES) {
            Iterator<Map.Entry<String, JsonObject>> iterator = recentResponses.entrySet().iterator();
            iterator.next();
            iterator.remove();
        }
    }

    private void sendError(
        BridgeConnection connection,
        JsonObject request,
        String code,
        String message,
        boolean retryable
    ) {
        connection.send(
            BridgeChannelRouter.error(CHANNEL, BridgeChannelRouter.stringField(request, "request_id"), code, message, retryable),
            currentTick
        );
    }

    private static JsonObject baseResponse(JsonObject request, String type) {
        JsonObject response = new JsonObject();
        response.addProperty("channel", CHANNEL);
        response.addProperty("type", type);
        String requestId = BridgeChannelRouter.stringField(request, "request_id");
        if (requestId != null) {
            response.addProperty("request_id", requestId);
        }
        return response;
    }

    private static JsonObject sessionResponse(JsonObject request, String type, Session active) {
        JsonObject response = baseResponse(request, type);
        response.addProperty("state", active.state);
        response.addProperty("mode", active.mode.wireName());
        response.addProperty("generation", active.generation);
        return response;
    }

    private static JsonObject observerJson(ObserverState state) {
        JsonObject value = new JsonObject();
        value.addProperty("id", state.id().toString());
        value.addProperty("connected", state.connected());
        if (!state.connected()) {
            return value;
        }
        value.addProperty("name", state.name());
        value.addProperty("spectator", state.spectator());
        value.addProperty("operator", state.operator());
        value.addProperty("dimension", state.dimension());
        JsonArray position = new JsonArray();
        position.add(state.x());
        position.add(state.y());
        position.add(state.z());
        value.add("position", position);
        if (state.cameraTargetId() != null) {
            value.addProperty("camera_target_id", state.cameraTargetId().toString());
        }
        return value;
    }

    private static FollowSettings parseFollow(JsonObject follow) {
        FollowSettings defaults = FollowSettings.defaults();
        if (follow == null) {
            return defaults;
        }
        return new FollowSettings(
            optionalDouble(follow, "distance", defaults.distance()),
            optionalDouble(follow, "azimuth_deg", defaults.azimuthDeg()),
            optionalDouble(follow, "elevation_deg", defaults.elevationDeg()),
            optionalDouble(follow, "height_offset", defaults.heightOffset()),
            optionalDouble(follow, "stiffness", defaults.stiffness()),
            optionalDouble(follow, "fov_deg", defaults.fovDeg()),
            optionalDouble(follow, "collision_margin", defaults.collisionMargin())
        );
    }

    private static FixedPose parseFixed(JsonObject fixed) {
        if (fixed == null) {
            throw new IllegalArgumentException("fixed mode requires fixed settings");
        }
        JsonArray position = requiredArray(fixed, "position", 3);
        String dimension = requiredString(fixed, "dimension", 128);
        double x = finiteArrayValue(position, 0, "position");
        double y = finiteArrayValue(position, 1, "position");
        double z = finiteArrayValue(position, 2, "position");
        float yaw;
        float pitch;
        if (fixed.has("look_at")) {
            JsonArray lookAt = requiredArray(fixed, "look_at", 3);
            double dx = finiteArrayValue(lookAt, 0, "look_at") - x;
            double dy = finiteArrayValue(lookAt, 1, "look_at") - y;
            double dz = finiteArrayValue(lookAt, 2, "look_at") - z;
            double horizontal = Math.sqrt(dx * dx + dz * dz);
            yaw = (float) Math.toDegrees(Math.atan2(-dx, dz));
            pitch = (float) -Math.toDegrees(Math.atan2(dy, horizontal));
        } else {
            yaw = (float) requiredDouble(fixed, "yaw");
            pitch = (float) requiredDouble(fixed, "pitch");
        }
        boolean allowChunkLoading = optionalBoolean(fixed, "allow_chunk_loading", false);
        return new FixedPose(dimension, x, y, z, yaw, pitch, allowChunkLoading);
    }

    private static String requiredString(JsonObject object, String name, int maxLength) {
        String value = BridgeChannelRouter.stringField(object, name);
        if (value == null || value.isBlank() || value.length() > maxLength) {
            throw new IllegalArgumentException(name + " is required and must be at most " + maxLength + " characters");
        }
        return value;
    }

    private static String optionalString(JsonObject object, String name, int maxLength) {
        if (!object.has(name) || object.get(name).isJsonNull()) {
            return null;
        }
        return requiredString(object, name, maxLength);
    }

    private static UUID requiredUuid(JsonObject object, String name) {
        String value = requiredString(object, name, 36);
        try {
            return UUID.fromString(value);
        } catch (IllegalArgumentException error) {
            throw new IllegalArgumentException(name + " must be a UUID");
        }
    }

    private static UUID optionalUuid(JsonObject object, String name) {
        if (!object.has(name) || object.get(name).isJsonNull()) {
            return null;
        }
        return requiredUuid(object, name);
    }

    private static long requiredLong(JsonObject object, String name, long minimum, long maximum) {
        try {
            JsonElement value = object.get(name);
            long parsed = value == null ? Long.MIN_VALUE : value.getAsLong();
            if (parsed < minimum || parsed > maximum) {
                throw new IllegalArgumentException(name + " is outside the allowed range");
            }
            return parsed;
        } catch (RuntimeException error) {
            throw new IllegalArgumentException(name + " must be an integer");
        }
    }

    private static double requiredDouble(JsonObject object, String name) {
        try {
            double value = object.get(name).getAsDouble();
            if (!Double.isFinite(value)) {
                throw new IllegalArgumentException(name + " must be finite");
            }
            return value;
        } catch (RuntimeException error) {
            throw new IllegalArgumentException(name + " must be a finite number");
        }
    }

    private static double optionalDouble(JsonObject object, String name, double fallback) {
        return object.has(name) ? requiredDouble(object, name) : fallback;
    }

    private static boolean optionalBoolean(JsonObject object, String name, boolean fallback) {
        if (!object.has(name)) {
            return fallback;
        }
        try {
            return object.get(name).getAsBoolean();
        } catch (RuntimeException error) {
            throw new IllegalArgumentException(name + " must be a boolean");
        }
    }

    private static JsonArray requiredArray(JsonObject object, String name, int size) {
        JsonElement value = object.get(name);
        if (value == null || !value.isJsonArray() || value.getAsJsonArray().size() != size) {
            throw new IllegalArgumentException(name + " must be an array of length " + size);
        }
        return value.getAsJsonArray();
    }

    private static double finiteArrayValue(JsonArray array, int index, String name) {
        try {
            double value = array.get(index).getAsDouble();
            if (!Double.isFinite(value)) {
                throw new IllegalArgumentException(name + " values must be finite");
            }
            return value;
        } catch (RuntimeException error) {
            throw new IllegalArgumentException(name + " values must be finite numbers");
        }
    }

    private record Mutation(String requestId, String leaseId, long generation) {
        private String cacheKey() {
            return leaseId + "\u0000" + generation + "\u0000" + requestId;
        }
    }

    private record ClientTelemetrySnapshot(CameraClientTelemetry value, int receivedAtTick) {
    }

    private static final class Session {
        private final String leaseId;
        private final long generation;
        private ObserverMode mode;
        private int leaseExpiresAtTick;
        private int nextControlTick;
        private UUID targetId;
        private String targetName;
        private FollowSettings follow;
        private FixedPose fixed;
        private String state = "attaching";

        private Session(Mutation mutation, ObserverMode mode, int leaseExpiresAtTick) {
            this.leaseId = mutation.leaseId;
            this.generation = mutation.generation;
            this.mode = mode;
            this.leaseExpiresAtTick = leaseExpiresAtTick;
            this.nextControlTick = 0;
        }

        private Session copyForMutation(Mutation mutation, ObserverMode replacementMode) {
            Session copy = new Session(mutation, replacementMode, leaseExpiresAtTick);
            copy.nextControlTick = nextControlTick;
            copy.targetId = targetId;
            copy.targetName = targetName;
            copy.follow = follow;
            copy.fixed = fixed;
            copy.state = state;
            return copy;
        }
    }
}
