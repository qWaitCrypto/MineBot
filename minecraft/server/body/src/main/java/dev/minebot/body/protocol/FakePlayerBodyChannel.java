package dev.minebot.body.protocol;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
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
import java.util.List;
import java.util.Map;
import java.util.Set;

public final class FakePlayerBodyChannel implements MineBotChannel {
    public static final String CHANNEL = "fakeplayer-body";
    public static final String PROTOCOL = "fakeplayer-body/1";
    private static final Set<String> REQUEST_TYPES = Set.of("HELLO", "FIND_BLOCKS");
    private static final int MAX_RADIUS = 128;
    private static final int MAX_VERTICAL_RADIUS = 64;
    private static final int MAX_PAGE_LIMIT = 128;

    private final MinecraftServer server;
    private final LoadedBlockScanner scanner = new LoadedBlockScanner();
    private final SearchSnapshotStore snapshots = new SearchSnapshotStore();

    public FakePlayerBodyChannel(MinecraftServer server) {
        this.server = server;
    }

    @Override
    public String name() {
        return CHANNEL;
    }

    @Override
    public void handle(MineBotConnection connection, JsonObject request, int serverTick) {
        String type = MineBotChannelRouter.stringField(request, "type");
        if (type == null || !REQUEST_TYPES.contains(type)) {
            connection.send(MineBotChannelRouter.error(CHANNEL, requestId(request), "unknown_type", "unknown body request type", false), serverTick);
            return;
        }
        if (!PROTOCOL.equals(MineBotChannelRouter.stringField(request, "protocol"))) {
            connection.send(MineBotChannelRouter.error(CHANNEL, requestId(request), "unsupported_protocol", "unsupported body protocol", false), serverTick);
            return;
        }
        if ("HELLO".equals(type)) {
            handleHello(connection, request, serverTick);
            return;
        }
        handleFindBlocks(connection, request, serverTick);
    }

    private void handleHello(MineBotConnection connection, JsonObject request, int serverTick) {
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
