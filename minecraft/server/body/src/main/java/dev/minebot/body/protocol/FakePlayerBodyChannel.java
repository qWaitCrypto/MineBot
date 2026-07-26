package dev.minebot.body.protocol;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import dev.minebot.body.action.ActionRegistry;
import dev.minebot.body.action.ActionRuntime;
import dev.minebot.body.control.FakePlayerActionOwner;
import dev.minebot.body.control.HeldInputs;
import dev.minebot.body.control.OwnerPriority;
import dev.minebot.body.control.PlayerCommandAdapter;
import dev.minebot.body.event.BotEventStream;
import dev.minebot.body.nav.Goal;
import dev.minebot.body.nav.MinecraftWorldView;
import dev.minebot.body.nav.NavigateExecutor;
import dev.minebot.server.common.transport.MineBotChannel;
import dev.minebot.server.common.transport.MineBotChannelRouter;
import dev.minebot.server.common.transport.MineBotConnection;
import dev.minebot.server.common.transport.MineBotWebSocketServer;
import dev.minebot.body.search.LoadedBlockScanner;
import dev.minebot.body.search.LoadedSearchResult;
import dev.minebot.body.search.SearchMatch;
import dev.minebot.body.search.SearchSnapshotStore;
import net.minecraft.core.BlockPos;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.resources.Identifier;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.world.level.block.Block;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

public final class FakePlayerBodyChannel implements MineBotChannel {
    public static final String CHANNEL = "fakeplayer-body";
    public static final String PROTOCOL = "fakeplayer-body/1";
    private static final Set<String> REQUEST_TYPES =
        Set.of("HELLO", "FIND_BLOCKS", "NAVIGATE", "RESUME_EVENTS", "CANCEL_ACTION", "QUERY_ACTION");
    private static final int MAX_RADIUS = 128;
    private static final int MAX_VERTICAL_RADIUS = 64;
    private static final int MAX_PAGE_LIMIT = 128;
    private static final int MAX_REPLAY_EVENTS_PER_RESPONSE = 256;
    private static final int MAX_TIMEOUT_TICKS = 12_000;

    private final MinecraftServer server;
    private final LoadedBlockScanner scanner = new LoadedBlockScanner();
    private final SearchSnapshotStore snapshots = new SearchSnapshotStore();
    private final BotEventStream events = new BotEventStream();
    private final ActionRegistry actions = new ActionRegistry();
    private final PlayerCommandAdapter adapter;
    private final ActionRuntime runtime;
    private final Set<MineBotConnection> subscribers = new LinkedHashSet<>();
    private int currentTick;

    public FakePlayerBodyChannel(MinecraftServer server) {
        this.server = server;
        this.adapter = new PlayerCommandAdapter(server, new HeldInputs());
        this.runtime = new ActionRuntime(new FakePlayerActionOwner(), adapter, actions, events);
    }

    /** Emits an event and pushes it to every live subscriber. Server thread only. */
    public void publishEvent(String bot, int serverTick, String name, String actionId, JsonObject data) {
        BotEventStream.Event event = events.emit(bot, serverTick, name, actionId, data);
        JsonObject json = event.toJson(CHANNEL);
        for (MineBotConnection subscriber : subscribers) {
            subscriber.send(json, serverTick);
        }
    }

    public ActionRuntime runtime() {
        return runtime;
    }

    @Override
    public String name() {
        return CHANNEL;
    }

    @Override
    public void tick(int serverTick) {
        currentTick = serverTick;
        runtime.tick(serverTick);
    }

    @Override
    public void connectionClosed(MineBotConnection connection, int serverTick) {
        subscribers.remove(connection);
    }

    @Override
    public void handle(MineBotConnection connection, JsonObject request, int serverTick) {
        currentTick = serverTick;
        String type = MineBotChannelRouter.stringField(request, "type");
        if (type == null || !REQUEST_TYPES.contains(type)) {
            connection.send(MineBotChannelRouter.error(CHANNEL, requestId(request), "unknown_type", "unknown body request type", false), serverTick);
            return;
        }
        if (!PROTOCOL.equals(MineBotChannelRouter.stringField(request, "protocol"))) {
            connection.send(MineBotChannelRouter.error(CHANNEL, requestId(request), "unsupported_protocol", "unsupported body protocol", false), serverTick);
            return;
        }
        switch (type) {
            case "HELLO" -> handleHello(connection, request, serverTick);
            case "FIND_BLOCKS" -> handleFindBlocks(connection, request, serverTick);
            case "NAVIGATE" -> handleNavigate(connection, request, serverTick);
            case "RESUME_EVENTS" -> handleResumeEvents(connection, request, serverTick);
            case "CANCEL_ACTION" -> handleCancelAction(connection, request, serverTick);
            case "QUERY_ACTION" -> handleQueryAction(connection, request, serverTick);
            default -> throw new IllegalStateException("unreachable request type " + type);
        }
    }

    private void handleNavigate(MineBotConnection connection, JsonObject request, int serverTick) {
        try {
            String botName = requiredString(request, "bot_name", 64);
            String actionId = requiredString(request, "action_id", 128);
            ServerPlayer player = server.getPlayerList().getPlayerByName(botName);
            if (player == null || player.isRemoved() || !(player.level() instanceof ServerLevel level)) {
                sendError(connection, request, serverTick, "body_missing", "FakePlayer is not present", true);
                return;
            }
            if (!request.has("goal") || !request.get("goal").isJsonObject()) {
                throw new IllegalArgumentException("goal is required");
            }
            Goal goal = parseGoal(request.getAsJsonObject("goal"), true);
            int timeoutTicks = boundedOptionalInt(request, "timeout_ticks", NavigateExecutor.DEFAULT_TIMEOUT_TICKS, 20, MAX_TIMEOUT_TICKS);

            ActionRuntime.Submission submission = runtime.submit(botName, actionId, "NAVIGATE", OwnerPriority.ACTION, serverTick);
            JsonObject response = baseResponse(request, "NAVIGATE_ACK");
            response.addProperty("action_id", actionId);
            switch (submission) {
                case ActionRuntime.Submission.Accepted ignored -> {
                    NavigateExecutor executor = new NavigateExecutor(
                        botName,
                        actionId,
                        goal,
                        new MinecraftWorldView(level),
                        adapter,
                        this::observedPosition,
                        this::publishEvent,
                        runtime,
                        timeoutTicks
                    );
                    runtime.attachExecutor(actionId, executor);
                    response.addProperty("state", "accepted");
                }
                case ActionRuntime.Submission.Duplicate duplicate -> {
                    response.addProperty("state", duplicate.status().state() == ActionRegistry.State.RUNNING ? "running" : "terminal");
                    if (duplicate.status().terminal() != null) {
                        response.add("terminal", duplicate.status().terminal());
                    }
                }
                case ActionRuntime.Submission.Rejected rejected -> {
                    JsonObject error = MineBotChannelRouter.error(CHANNEL, requestId(request), rejected.code(), "another action owns this bot", true);
                    error.addProperty("owner_action_id", rejected.currentOwner().actionId());
                    error.addProperty("owner_priority", rejected.currentOwner().priority().name());
                    connection.send(error, serverTick);
                    return;
                }
            }
            connection.send(response, serverTick);
        } catch (IllegalArgumentException error) {
            sendError(connection, request, serverTick, "invalid_request", String.valueOf(error.getMessage()), false);
        }
    }

    private NavigateExecutor.PositionSource.Position observedPosition(String botName) {
        ServerPlayer player = server.getPlayerList().getPlayerByName(botName);
        if (player == null || player.isRemoved()) {
            return null;
        }
        return new NavigateExecutor.PositionSource.Position(player.getX(), player.getY(), player.getZ());
    }

    private static Goal parseGoal(JsonObject json, boolean allowComposite) {
        String kind = MineBotChannelRouter.stringField(json, "kind");
        if (kind == null) {
            throw new IllegalArgumentException("goal.kind is required");
        }
        return switch (kind) {
            case "near" -> new Goal.Near(
                requiredInt(json, "x"),
                requiredInt(json, "y"),
                requiredInt(json, "z"),
                optionalDouble(json, "range", 1.5, 0.5, 64.0)
            );
            case "xz" -> new Goal.XZ(requiredInt(json, "x"), requiredInt(json, "z"));
            case "interact" -> new Goal.Interact(
                requiredInt(json, "x"),
                requiredInt(json, "y"),
                requiredInt(json, "z"),
                optionalDouble(json, "range", Goal.Interact.MINE_RANGE, 1.0, 6.0)
            );
            case "composite" -> {
                if (!allowComposite) {
                    throw new IllegalArgumentException("composite goals cannot nest");
                }
                if (!json.has("goals") || !json.get("goals").isJsonArray() || json.getAsJsonArray("goals").isEmpty()) {
                    throw new IllegalArgumentException("composite goal needs a nonempty goals array");
                }
                if (json.getAsJsonArray("goals").size() > 16) {
                    throw new IllegalArgumentException("composite goal supports at most 16 members");
                }
                List<Goal> members = new ArrayList<>();
                json.getAsJsonArray("goals").forEach(member -> {
                    if (!member.isJsonObject()) {
                        throw new IllegalArgumentException("composite members must be goal objects");
                    }
                    members.add(parseGoal(member.getAsJsonObject(), false));
                });
                yield new Goal.Composite(members);
            }
            default -> throw new IllegalArgumentException("unknown goal kind: " + kind);
        };
    }

    private static int requiredInt(JsonObject json, String name) {
        if (!json.has(name) || !json.get(name).isJsonPrimitive()) {
            throw new IllegalArgumentException("goal." + name + " is required");
        }
        try {
            return json.get(name).getAsInt();
        } catch (NumberFormatException error) {
            throw new IllegalArgumentException("goal." + name + " must be an integer");
        }
    }

    private static double optionalDouble(JsonObject json, String name, double fallback, double min, double max) {
        if (!json.has(name)) {
            return fallback;
        }
        double value;
        try {
            value = json.get(name).getAsDouble();
        } catch (RuntimeException error) {
            throw new IllegalArgumentException("goal." + name + " must be a number");
        }
        if (value < min || value > max) {
            throw new IllegalArgumentException("goal." + name + " is out of bounds");
        }
        return value;
    }

    private void handleHello(MineBotConnection connection, JsonObject request, int serverTick) {
        subscribers.add(connection);
        JsonObject response = baseResponse(request, "HELLO_ACK");
        response.addProperty("protocol", PROTOCOL);
        response.addProperty("minecraft_version", server.getServerVersion());
        response.addProperty("max_request_bytes", MineBotWebSocketServer.MAX_REQUEST_BYTES);
        response.addProperty("max_requests_per_second", MineBotConnection.MAX_REQUESTS_PER_SECOND);
        JsonArray requestTypes = new JsonArray();
        REQUEST_TYPES.stream().sorted().forEach(requestTypes::add);
        response.add("request_types", requestTypes);
        connection.send(response, serverTick);
    }

    private void handleResumeEvents(MineBotConnection connection, JsonObject request, int serverTick) {
        try {
            String botName = requiredString(request, "bot_name", 64);
            long afterSeq = request.has("after_seq") ? request.get("after_seq").getAsLong() : 0L;
            if (afterSeq < 0) {
                throw new IllegalArgumentException("after_seq must be >= 0");
            }
            BotEventStream.Replay replay = events.replay(botName, afterSeq);
            JsonObject response = baseResponse(request, "RESUME_EVENTS_RESULT");
            response.addProperty("bot", botName);
            if (replay.hasGap()) {
                JsonObject gap = new JsonObject();
                gap.addProperty("from", replay.gapFrom());
                gap.addProperty("to", replay.gapTo());
                response.add("event_gap", gap);
            } else {
                response.add("event_gap", com.google.gson.JsonNull.INSTANCE);
            }
            JsonArray replayed = new JsonArray();
            boolean truncated = replay.events().size() > MAX_REPLAY_EVENTS_PER_RESPONSE;
            replay.events().stream()
                .limit(MAX_REPLAY_EVENTS_PER_RESPONSE)
                .forEach(event -> replayed.add(event.toJson(CHANNEL)));
            response.add("events", replayed);
            response.addProperty("replay_complete", !truncated);
            response.addProperty("last_seq", events.lastSeq(botName));
            connection.send(response, serverTick);
        } catch (IllegalArgumentException | UnsupportedOperationException error) {
            sendError(connection, request, serverTick, "invalid_request", String.valueOf(error.getMessage()), false);
        }
    }

    private void handleCancelAction(MineBotConnection connection, JsonObject request, int serverTick) {
        try {
            String actionId = requiredString(request, "action_id", 128);
            ActionRegistry.ActionStatus status = actions.status(actionId);
            JsonObject response = baseResponse(request, "CANCEL_ACTION_RESULT");
            response.addProperty("action_id", actionId);
            switch (status.state()) {
                case RUNNING -> {
                    runtime.requestCancel(actionId);
                    response.addProperty("state", "cancel_requested");
                }
                case TERMINAL -> {
                    response.addProperty("state", "terminal");
                    response.add("terminal", status.terminal());
                }
                case UNKNOWN -> response.addProperty("state", "unknown");
            }
            connection.send(response, serverTick);
        } catch (IllegalArgumentException error) {
            sendError(connection, request, serverTick, "invalid_request", String.valueOf(error.getMessage()), false);
        }
    }

    private void handleQueryAction(MineBotConnection connection, JsonObject request, int serverTick) {
        try {
            String actionId = requiredString(request, "action_id", 128);
            ActionRegistry.ActionStatus status = actions.status(actionId);
            JsonObject response = baseResponse(request, "QUERY_ACTION_RESULT");
            response.addProperty("action_id", actionId);
            switch (status.state()) {
                case RUNNING -> {
                    response.addProperty("state", "running");
                    response.addProperty("bot", status.record().bot());
                    response.addProperty("action_type", status.record().type());
                    response.addProperty("cancel_requested", status.cancelRequested());
                }
                case TERMINAL -> {
                    response.addProperty("state", "terminal");
                    response.add("terminal", status.terminal());
                }
                case UNKNOWN -> response.addProperty("state", "unknown");
            }
            connection.send(response, serverTick);
        } catch (IllegalArgumentException error) {
            sendError(connection, request, serverTick, "invalid_request", String.valueOf(error.getMessage()), false);
        }
    }

    private void handleFindBlocks(MineBotConnection connection, JsonObject request, int serverTick) {
        try {
            String botName = requiredString(request, "bot_name", 64);
            ServerPlayer player = server.getPlayerList().getPlayerByName(botName);
            if (player == null || player.isRemoved() || !(player.level() instanceof ServerLevel level)) {
                sendError(connection, request, serverTick, "body_missing", "FakePlayer is not present", true);
                return;
            }
            Map<Block, String> requestedBlocks = requiredBlocks(request);
            int radius = boundedInt(request, "radius", 0, MAX_RADIUS);
            int verticalRadius = boundedOptionalInt(request, "vertical_radius", Math.min(radius, 16), 0, MAX_VERTICAL_RADIUS);
            int limit = boundedOptionalInt(request, "limit", 32, 1, MAX_PAGE_LIMIT);
            String fingerprint = fingerprint(player, requestedBlocks.values(), radius, verticalRadius);
            String cursor = MineBotChannelRouter.stringField(request, "cursor");
            if (cursor != null) {
                SearchSnapshotStore.ResumeResult resumed = snapshots.resume(cursor, fingerprint, limit);
                if (resumed.error() != null) {
                    sendError(connection, request, serverTick, resumed.error(), "search cursor is no longer usable", true);
                    return;
                }
                sendPage(connection, request, serverTick, resumed.page());
                return;
            }
            BlockPos center = player.blockPosition();
            LoadedSearchResult result = scanner.scan(level, center, requestedBlocks, radius, verticalRadius, serverTick);
            sendPage(connection, request, serverTick, snapshots.first(fingerprint, result, limit));
        } catch (IllegalArgumentException error) {
            sendError(connection, request, serverTick, "invalid_request", error.getMessage(), false);
        } catch (RuntimeException error) {
            sendError(connection, request, serverTick, "search_internal_error", "indexed search failed", true);
        }
    }

    private static void sendPage(
        MineBotConnection connection,
        JsonObject request,
        int serverTick,
        SearchSnapshotStore.Page page
    ) {
        JsonObject response = baseResponse(request, "FIND_BLOCKS_RESULT");
        response.addProperty("index_generation", page.generation());
        response.addProperty("coverage_complete", page.coverageComplete());
        response.addProperty("unloaded_chunk_count", page.unloadedChunkCount());
        response.addProperty("result_capped", page.resultCapped());
        if (page.nextCursor() == null) {
            response.add("next_cursor", com.google.gson.JsonNull.INSTANCE);
        } else {
            response.addProperty("next_cursor", page.nextCursor());
        }
        JsonArray matches = new JsonArray();
        for (SearchMatch match : page.matches()) {
            JsonObject entry = new JsonObject();
            entry.addProperty("x", match.x());
            entry.addProperty("y", match.y());
            entry.addProperty("z", match.z());
            entry.addProperty("block_id", match.blockId());
            entry.addProperty("distance_squared", match.distanceSquared());
            matches.add(entry);
        }
        response.add("matches", matches);
        connection.send(response, serverTick);
    }

    private static Map<Block, String> requiredBlocks(JsonObject request) {
        if (!request.has("block_ids") || !request.get("block_ids").isJsonArray()) {
            throw new IllegalArgumentException("block_ids must be a nonempty array");
        }
        Map<Block, String> blocks = new LinkedHashMap<>();
        for (var element : request.getAsJsonArray("block_ids")) {
            if (!element.isJsonPrimitive()) {
                throw new IllegalArgumentException("block_ids must contain strings");
            }
            String blockId = element.getAsString();
            if (!blockId.matches("[a-z0-9_.-]+:[a-z0-9_/.-]+")) {
                throw new IllegalArgumentException("block_ids contains an invalid identifier");
            }
            Identifier location = Identifier.tryParse(blockId);
            Block block = location == null
                ? null
                : BuiltInRegistries.BLOCK.getOptional(location).orElse(null);
            if (block == null) {
                throw new IllegalArgumentException("unknown block id: " + blockId);
            }
            blocks.put(block, blockId);
        }
        if (blocks.isEmpty() || blocks.size() > 16) {
            throw new IllegalArgumentException("block_ids must contain 1 to 16 identifiers");
        }
        return Map.copyOf(blocks);
    }

    private static String requiredString(JsonObject request, String name, int maxLength) {
        String value = MineBotChannelRouter.stringField(request, name);
        if (value == null || value.isBlank() || value.length() > maxLength) {
            throw new IllegalArgumentException(name + " is required");
        }
        return value;
    }

    private static int boundedInt(JsonObject request, String name, int min, int max) {
        if (!request.has(name) || !request.get(name).isJsonPrimitive()) {
            throw new IllegalArgumentException(name + " is required");
        }
        try {
            int value = request.get(name).getAsInt();
            if (value < min || value > max) {
                throw new IllegalArgumentException(name + " is out of bounds");
            }
            return value;
        } catch (NumberFormatException error) {
            throw new IllegalArgumentException(name + " must be an integer");
        }
    }

    private static int boundedOptionalInt(JsonObject request, String name, int fallback, int min, int max) {
        return request.has(name) ? boundedInt(request, name, min, max) : fallback;
    }

    private static String fingerprint(ServerPlayer player, Iterable<String> blockIds, int radius, int verticalRadius) {
        List<String> sortedIds = new ArrayList<>();
        blockIds.forEach(sortedIds::add);
        sortedIds.sort(String::compareTo);
        return player.getUUID() + "|" + player.level().dimension().identifier() + "|" + String.join(",", sortedIds) + "|" + radius + "|" + verticalRadius;
    }

    private static void sendError(MineBotConnection connection, JsonObject request, int serverTick, String code, String message, boolean retryable) {
        connection.send(MineBotChannelRouter.error(CHANNEL, requestId(request), code, message, retryable), serverTick);
    }

    private static JsonObject baseResponse(JsonObject request, String type) {
        JsonObject response = new JsonObject();
        response.addProperty("channel", CHANNEL);
        response.addProperty("type", type);
        String requestId = requestId(request);
        if (requestId != null) {
            response.addProperty("request_id", requestId);
        }
        return response;
    }

    private static String requestId(JsonObject request) {
        return MineBotChannelRouter.stringField(request, "request_id");
    }
}
