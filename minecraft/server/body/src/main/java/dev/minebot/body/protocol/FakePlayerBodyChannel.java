package dev.minebot.body.protocol;

import com.google.gson.JsonArray;
import com.google.gson.JsonNull;
import com.google.gson.JsonObject;
import com.mojang.serialization.JsonOps;
import dev.minebot.body.action.ActionRegistry;
import dev.minebot.body.action.ActionRuntime;
import dev.minebot.body.action.AscendExecutor;
import dev.minebot.body.action.CollectExecutor;
import dev.minebot.body.action.MutationGate;
import dev.minebot.body.control.FakePlayerActionOwner;
import dev.minebot.body.control.HeldInputs;
import dev.minebot.body.control.OwnerPriority;
import dev.minebot.body.control.PlayerCommandAdapter;
import dev.minebot.body.control.ServerPlayerBlockBreaker;
import dev.minebot.body.event.BotEventStream;
import dev.minebot.body.inventory.InventorySnapshot;
import dev.minebot.body.nav.Goal;
import dev.minebot.body.nav.MinecraftWorldView;
import dev.minebot.body.nav.NavigateExecutor;
import dev.minebot.body.perception.EntityReadService;
import dev.minebot.body.perception.WorldReadService;
import net.minecraft.world.item.ItemStack;
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
import net.minecraft.world.entity.item.ItemEntity;
import net.minecraft.world.level.block.Block;
import net.minecraft.world.phys.AABB;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

public final class FakePlayerBodyChannel implements MineBotChannel {
    public static final String CHANNEL = "fakeplayer-body";
    public static final String PROTOCOL = "fakeplayer-body/1";
    private static final Set<String> REQUEST_TYPES = Set.of(
        "HELLO", "FIND_BLOCKS", "BODY_STATE", "INVENTORY", "WORLD_READ", "ENTITY_READ", "NAVIGATE", "COLLECT_BLOCK", "ASCEND",
        "MUTATION_VERDICT", "RESUME_EVENTS", "CANCEL_ACTION", "QUERY_ACTION"
    );
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
    private final MutationGate mutationGate = new MutationGate();
    private final ServerPlayerBlockBreaker blockBreaker;
    private final PlayerCommandAdapter adapter;
    private final ActionRuntime runtime;
    private final Set<MineBotConnection> subscribers = new LinkedHashSet<>();
    private int currentTick;

    public FakePlayerBodyChannel(MinecraftServer server) {
        this.server = server;
        this.blockBreaker = new ServerPlayerBlockBreaker(server);
        this.adapter = new PlayerCommandAdapter(server, new HeldInputs(), blockBreaker);
        this.runtime = new ActionRuntime(new FakePlayerActionOwner(), adapter, actions, events);
        // Every emitted event — runtime lifecycle included — is pushed live.
        events.setListener(event -> {
            JsonObject json = event.toJson(CHANNEL);
            for (MineBotConnection subscriber : subscribers) {
                subscriber.send(json, event.tick());
            }
        });
    }

    /** Emits an event; the stream listener pushes it to every subscriber. */
    public void publishEvent(String bot, int serverTick, String name, String actionId, JsonObject data) {
        events.emit(bot, serverTick, name, actionId, data);
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
            case "BODY_STATE" -> handleBodyState(connection, request, serverTick);
            case "INVENTORY" -> handleInventory(connection, request, serverTick);
            case "WORLD_READ" -> handleWorldRead(connection, request, serverTick);
            case "ENTITY_READ" -> handleEntityRead(connection, request, serverTick);
            case "NAVIGATE" -> handleNavigate(connection, request, serverTick);
            case "COLLECT_BLOCK" -> handleCollectBlock(connection, request, serverTick);
            case "ASCEND" -> handleAscend(connection, request, serverTick);
            case "MUTATION_VERDICT" -> handleMutationVerdict(request);
            case "RESUME_EVENTS" -> handleResumeEvents(connection, request, serverTick);
            case "CANCEL_ACTION" -> handleCancelAction(connection, request, serverTick);
            case "QUERY_ACTION" -> handleQueryAction(connection, request, serverTick);
            default -> throw new IllegalStateException("unreachable request type " + type);
        }
    }

    private void handleAscend(MineBotConnection connection, JsonObject request, int serverTick) {
        try {
            String botName = requiredString(request, "bot_name", 64);
            String actionId = requiredString(request, "action_id", 128);
            ServerPlayer player = server.getPlayerList().getPlayerByName(botName);
            if (player == null || player.isRemoved() || !(player.level() instanceof ServerLevel level)) {
                sendError(connection, request, serverTick, "body_missing", "FakePlayer is not present", true);
                return;
            }
            int targetY = boundedOptionalInt(request, "target_y", level.getMaxY(), level.getMinY(), level.getMaxY());
            int timeoutTicks = boundedOptionalInt(request, "timeout_ticks", MAX_TIMEOUT_TICKS, 20, MAX_TIMEOUT_TICKS);
            ActionRuntime.Submission submission = runtime.submit(
                botName, actionId, "ASCEND", OwnerPriority.RECOVERY, serverTick
            );
            JsonObject response = baseResponse(request, "ASCEND_ACK");
            response.addProperty("action_id", actionId);
            switch (submission) {
                case ActionRuntime.Submission.Accepted ignored -> {
                    AscendExecutor.BlockReader blockReader = new AscendExecutor.BlockReader() {
                        @Override
                        public String blockIdAt(int x, int y, int z) {
                            return observedBlockId(level, x, y, z);
                        }

                        @Override
                        public boolean skyAbove(int x, int y, int z) {
                            return level.canSeeSky(new BlockPos(x, y + 1, z));
                        }
                    };
                    AscendExecutor executor = new AscendExecutor(
                        botName,
                        actionId,
                        targetY,
                        blockBreaker,
                        adapter,
                        adapter,
                        blockReader,
                        FakePlayerBodyChannel::isAscendHazard,
                        mutationGate,
                        this::publishProposal,
                        this::observedPosition,
                        this::publishEvent,
                        runtime,
                        timeoutTicks
                    );
                    runtime.attachExecutor(actionId, executor);
                    response.addProperty("state", "accepted");
                }
                case ActionRuntime.Submission.Duplicate duplicate -> {
                    response.addProperty(
                        "state",
                        duplicate.status().state() == ActionRegistry.State.RUNNING ? "running" : "terminal"
                    );
                    if (duplicate.status().terminal() != null) {
                        response.add("terminal", duplicate.status().terminal());
                    }
                }
                case ActionRuntime.Submission.Rejected rejected -> {
                    JsonObject error = MineBotChannelRouter.error(
                        CHANNEL, requestId(request), rejected.code(), "another action owns this bot", true
                    );
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

    private static boolean isAscendHazard(String blockId) {
        return blockId.equals("minecraft:water")
            || blockId.equals("minecraft:lava")
            || blockId.equals("minecraft:fire")
            || blockId.equals("minecraft:soul_fire")
            || blockId.equals("minecraft:magma_block")
            || blockId.equals("minecraft:powder_snow")
            || blockId.equals("minecraft:bubble_column");
    }

    private void handleCollectBlock(MineBotConnection connection, JsonObject request, int serverTick) {
        try {
            String botName = requiredString(request, "bot_name", 64);
            String actionId = requiredString(request, "action_id", 128);
            ServerPlayer player = server.getPlayerList().getPlayerByName(botName);
            if (player == null || player.isRemoved() || !(player.level() instanceof ServerLevel level)) {
                sendError(connection, request, serverTick, "body_missing", "FakePlayer is not present", true);
                return;
            }
            Map<Block, String> requestedBlocks = requiredBlocks(request);
            int radius = boundedOptionalInt(request, "radius", 48, 4, 64);
            int verticalRadius = boundedOptionalInt(request, "vertical_radius", 16, 1, MAX_VERTICAL_RADIUS);
            int timeoutTicks = boundedOptionalInt(request, "timeout_ticks", NavigateExecutor.DEFAULT_TIMEOUT_TICKS, 20, MAX_TIMEOUT_TICKS);

            ActionRuntime.Submission submission = runtime.submit(botName, actionId, "COLLECT_BLOCK", OwnerPriority.ACTION, serverTick);
            JsonObject response = baseResponse(request, "COLLECT_BLOCK_ACK");
            response.addProperty("action_id", actionId);
            switch (submission) {
                case ActionRuntime.Submission.Accepted ignored -> {
                    BlockPos center = player.blockPosition();
                    LoadedSearchResult search = scanner.scan(level, center, requestedBlocks, radius, verticalRadius, serverTick);
                    JsonObject searchFacts = new JsonObject();
                    searchFacts.addProperty("radius", radius);
                    searchFacts.addProperty("matches", search.matches().size());
                    searchFacts.addProperty("coverage_complete", search.coverageComplete());
                    searchFacts.addProperty("unloaded_chunk_count", search.unloadedChunkCount());
                    List<CollectExecutor.Candidate> candidates = new ArrayList<>();
                    for (SearchMatch match : search.matches()) {
                        boolean spread = candidates.stream().allMatch(kept ->
                            Math.abs(match.x() - kept.x()) + Math.abs(match.z() - kept.z()) >= 2);
                        if (spread) {
                            candidates.add(new CollectExecutor.Candidate(match.x(), match.y(), match.z(), match.blockId()));
                        }
                        if (candidates.size() >= CollectExecutor.MAX_CANDIDATE_ATTEMPTS) {
                            break;
                        }
                    }
                    Map<String, String> itemIds = new LinkedHashMap<>();
                    requestedBlocks.values().forEach(blockId -> itemIds.put(blockId, blockId));
                    CollectExecutor executor = new CollectExecutor(
                        botName,
                        actionId,
                        candidates,
                        itemIds,
                        searchFacts,
                        new MinecraftWorldView(level),
                        adapter,
                        blockBreaker,
                        adapter,
                        (x, y, z) -> observedBlockId(level, x, y, z),
                        itemId -> observedItemCount(botName, itemId),
                        (itemId, x, y, z, dropRadius) -> observedDrops(
                            level, itemId, x, y, z, dropRadius
                        ),
                        mutationGate,
                        this::publishProposal,
                        this::observedPosition,
                        this::publishEvent,
                        runtime,
                        timeoutTicks
                    );
                    runtime.attachExecutor(actionId, executor);
                    response.addProperty("state", "accepted");
                    response.addProperty("candidates", candidates.size());
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

    /** Authoritative body-state snapshot: the Java Body's read of the same
     * server truth the neutral Body contract's get_state() promises. */
    private void handleBodyState(MineBotConnection connection, JsonObject request, int serverTick) {
        try {
            String botName = requiredString(request, "bot_name", 64);
            ServerPlayer player = server.getPlayerList().getPlayerByName(botName);
            JsonObject response = baseResponse(request, "BODY_STATE_RESULT");
            response.addProperty("bot", botName);
            response.addProperty("server_tick", serverTick);
            if (player == null || player.isRemoved() || !(player.level() instanceof ServerLevel level)) {
                response.addProperty("missing", true);
                connection.send(response, serverTick);
                return;
            }
            response.addProperty("missing", false);
            JsonObject position = new JsonObject();
            position.addProperty("x", player.getX());
            position.addProperty("y", player.getY());
            position.addProperty("z", player.getZ());
            response.add("position", position);
            response.addProperty("yaw", player.getYRot());
            response.addProperty("pitch", player.getXRot());
            response.addProperty("health", player.getHealth());
            response.addProperty("food", player.getFoodData().getFoodLevel());
            response.addProperty("air", player.getAirSupply());
            response.addProperty("dimension", level.dimension().identifier().toString());
            response.addProperty("game_time", level.getGameTime());
            JsonObject inventoryCounts = new JsonObject();
            var inventory = player.getInventory();
            for (int slot = 0; slot < inventory.getContainerSize(); slot++) {
                ItemStack stack = inventory.getItem(slot);
                if (!stack.isEmpty()) {
                    String itemId = BuiltInRegistries.ITEM.getKey(stack.getItem()).toString();
                    int existing = inventoryCounts.has(itemId) ? inventoryCounts.get(itemId).getAsInt() : 0;
                    inventoryCounts.addProperty(itemId, existing + stack.getCount());
                }
            }
            response.add("inventory_counts", inventoryCounts);
            ItemStack selected = player.getMainHandItem();
            ItemStack offhand = player.getOffhandItem();
            response.addProperty("selected_item", selected.isEmpty()
                ? null : BuiltInRegistries.ITEM.getKey(selected.getItem()).toString());
            response.addProperty("offhand_item", offhand.isEmpty()
                ? null : BuiltInRegistries.ITEM.getKey(offhand.getItem()).toString());
            var owner = runtime.currentOwner(botName);
            response.addProperty("body_owner", owner == null ? null : owner.actionId());
            connection.send(response, serverTick);
        } catch (IllegalArgumentException error) {
            sendError(connection, request, serverTick, "invalid_request", String.valueOf(error.getMessage()), false);
        }
    }

    /** Paged slot-level inventory truth with the legacy 46-slot logical mapping. */
    private void handleInventory(MineBotConnection connection, JsonObject request, int serverTick) {
        try {
            String botName = requiredString(request, "bot_name", 64);
            JsonObject response = baseResponse(request, "INVENTORY_RESULT");
            response.addProperty("bot", botName);
            ServerPlayer player = server.getPlayerList().getPlayerByName(botName);
            if (player == null || player.isRemoved()) {
                response.addProperty("missing", true);
                connection.send(response, serverTick);
                return;
            }

            List<InventorySnapshot.StackValue> backingSlots = new ArrayList<>();
            var inventory = player.getInventory();
            for (int slot = 0; slot < inventory.getContainerSize(); slot++) {
                ItemStack stack = inventory.getItem(slot);
                if (stack.isEmpty()) {
                    backingSlots.add(InventorySnapshot.StackValue.emptySlot());
                    continue;
                }
                String itemId = BuiltInRegistries.ITEM.getKey(stack.getItem()).toString();
                String stackRaw = ItemStack.CODEC.encodeStart(
                    server.registryAccess().createSerializationContext(JsonOps.INSTANCE),
                    stack
                ).getOrThrow().toString();
                backingSlots.add(new InventorySnapshot.StackValue(itemId, stack.getCount(), stackRaw));
            }

            int requestedStart = boundedOptionalInt(
                request, "start", 0, Integer.MIN_VALUE, Integer.MAX_VALUE
            );
            int requestedLimit = boundedOptionalInt(
                request, "limit", InventorySnapshot.TOTAL_SLOTS, Integer.MIN_VALUE, Integer.MAX_VALUE
            );
            InventorySnapshot.Page page = InventorySnapshot.page(backingSlots, requestedStart, requestedLimit);
            response.addProperty("missing", false);
            response.addProperty("start", page.start());
            response.addProperty("limit", page.limit());
            if (page.nextStart() == null) {
                response.add("nextStart", JsonNull.INSTANCE);
            } else {
                response.addProperty("nextStart", page.nextStart());
            }
            response.addProperty("totalSlots", page.totalSlots());
            JsonArray slots = new JsonArray();
            for (InventorySnapshot.Slot slot : page.slots()) {
                JsonObject entry = new JsonObject();
                entry.addProperty("slot", slot.slot());
                entry.addProperty("slotType", slot.slotType());
                entry.addProperty("slotLabel", slot.slotLabel());
                entry.addProperty("empty", slot.empty());
                entry.addProperty("item", slot.item());
                entry.addProperty("count", slot.count());
                entry.addProperty("stackRaw", slot.stackRaw());
                slots.add(entry);
            }
            response.add("slots", slots);
            connection.send(response, serverTick);
        } catch (IllegalArgumentException error) {
            sendError(connection, request, serverTick, "invalid_request", String.valueOf(error.getMessage()), false);
        } catch (RuntimeException error) {
            sendError(connection, request, serverTick, "inventory_internal_error", "inventory snapshot failed", true);
        }
    }

    private void handleWorldRead(MineBotConnection connection, JsonObject request, int serverTick) {
        long startedNanos = System.nanoTime();
        try {
            String botName = requiredString(request, "bot_name", 64);
            String scope = requiredString(request, "scope", 64);
            if (!request.has("params") || !request.get("params").isJsonObject()) {
                throw new IllegalArgumentException("params must be an object");
            }
            JsonObject response = baseResponse(request, "WORLD_READ_RESULT");
            response.addProperty("bot", botName);
            response.addProperty("scope", scope);
            ServerPlayer player = server.getPlayerList().getPlayerByName(botName);
            if (player == null || player.isRemoved() || !(player.level() instanceof ServerLevel level)) {
                response.addProperty("missing", true);
                response.addProperty("server_cost_micros", (System.nanoTime() - startedNanos) / 1_000L);
                connection.send(response, serverTick);
                return;
            }
            WorldReadService.Result result = new WorldReadService(player, level).read(
                scope, request.getAsJsonObject("params")
            );
            response.addProperty("missing", false);
            response.addProperty("ok", true);
            response.addProperty("complete", result.complete());
            response.addProperty("server_cost_micros", (System.nanoTime() - startedNanos) / 1_000L);
            response.add("data", result.data());
            response.add("uncertainty", result.uncertainty());
            if (result.next() == null) {
                response.add("next", JsonNull.INSTANCE);
            } else {
                response.addProperty("next", result.next());
            }
            connection.send(response, serverTick);
        } catch (IllegalArgumentException error) {
            sendError(connection, request, serverTick, "invalid_request", String.valueOf(error.getMessage()), false);
        } catch (RuntimeException error) {
            sendError(connection, request, serverTick, "world_read_internal_error", "world read failed", true);
        }
    }

    private void handleEntityRead(MineBotConnection connection, JsonObject request, int serverTick) {
        long startedNanos = System.nanoTime();
        try {
            String botName = requiredString(request, "bot_name", 64);
            String scope = requiredString(request, "scope", 64);
            if (!request.has("params") || !request.get("params").isJsonObject()) {
                throw new IllegalArgumentException("params must be an object");
            }
            JsonObject response = baseResponse(request, "ENTITY_READ_RESULT");
            response.addProperty("bot", botName);
            response.addProperty("scope", scope);
            ServerPlayer player = server.getPlayerList().getPlayerByName(botName);
            if (player == null || player.isRemoved() || !(player.level() instanceof ServerLevel level)) {
                response.addProperty("missing", true);
                response.addProperty("server_cost_micros", (System.nanoTime() - startedNanos) / 1_000L);
                connection.send(response, serverTick);
                return;
            }
            EntityReadService.Result result = new EntityReadService(player, level).read(
                scope, request.getAsJsonObject("params")
            );
            response.addProperty("missing", false);
            response.addProperty("ok", true);
            response.addProperty("complete", result.complete());
            response.addProperty("server_cost_micros", (System.nanoTime() - startedNanos) / 1_000L);
            response.add("data", result.data());
            response.add("uncertainty", result.uncertainty());
            if (result.next() == null) {
                response.add("next", JsonNull.INSTANCE);
            } else {
                response.addProperty("next", result.next());
            }
            connection.send(response, serverTick);
        } catch (IllegalArgumentException error) {
            sendError(connection, request, serverTick, "invalid_request", String.valueOf(error.getMessage()), false);
        } catch (RuntimeException error) {
            sendError(connection, request, serverTick, "entity_read_internal_error", "entity read failed", true);
        }
    }

    /** Fire-and-forget by design: a lost or malformed verdict times out into a denial. */
    private void handleMutationVerdict(JsonObject request) {
        String proposalId = MineBotChannelRouter.stringField(request, "proposal_id");
        if (proposalId == null || !request.has("allow") || !request.get("allow").isJsonPrimitive()) {
            return;
        }
        boolean allow;
        try {
            allow = request.get("allow").getAsBoolean();
        } catch (RuntimeException invalid) {
            return;
        }
        String reason = MineBotChannelRouter.stringField(request, "reason");
        mutationGate.verdict(proposalId, allow, reason);
    }

    private void publishProposal(MutationGate.Proposal proposal) {
        JsonObject frame = new JsonObject();
        frame.addProperty("channel", CHANNEL);
        frame.addProperty("type", "MUTATION_PROPOSAL");
        frame.addProperty("proposal_id", proposal.proposalId());
        frame.addProperty("bot", proposal.bot());
        frame.addProperty("action_id", proposal.actionId());
        JsonObject mutation = new JsonObject();
        mutation.addProperty("kind", proposal.mutationKind());
        mutation.addProperty("x", proposal.x());
        mutation.addProperty("y", proposal.y());
        mutation.addProperty("z", proposal.z());
        mutation.addProperty("block_id", proposal.blockId());
        mutation.addProperty("context", mutationContext(proposal.actionId()));
        frame.add("mutation", mutation);
        for (MineBotConnection subscriber : subscribers) {
            subscriber.send(frame, currentTick);
        }
    }

    private String mutationContext(String actionId) {
        ActionRegistry.ActionStatus status = actions.status(actionId);
        if (status.record() == null) {
            return "direct";
        }
        return switch (status.record().type()) {
            case "ASCEND" -> "recovery";
            case "COLLECT_BLOCK" -> "collect";
            default -> "direct";
        };
    }

    private String observedBlockId(ServerLevel level, int x, int y, int z) {
        var chunk = level.getChunkSource().getChunkNow(x >> 4, z >> 4);
        if (chunk == null) {
            return null;
        }
        return LoadedBlockScanner.blockId(chunk.getBlockState(new BlockPos(x, y, z)).getBlock());
    }

    private int observedItemCount(String botName, String itemId) {
        ServerPlayer player = server.getPlayerList().getPlayerByName(botName);
        if (player == null || player.isRemoved()) {
            return 0;
        }
        int total = 0;
        var inventory = player.getInventory();
        for (int slot = 0; slot < inventory.getContainerSize(); slot++) {
            ItemStack stack = inventory.getItem(slot);
            if (!stack.isEmpty()
                && BuiltInRegistries.ITEM.getKey(stack.getItem()).toString().equals(itemId)) {
                total += stack.getCount();
            }
        }
        return total;
    }

    private List<CollectExecutor.Drop> observedDrops(
        ServerLevel level,
        String itemId,
        int x,
        int y,
        int z,
        double radius
    ) {
        AABB bounds = new AABB(
            x + 0.5 - radius,
            y + 0.5 - radius,
            z + 0.5 - radius,
            x + 0.5 + radius,
            y + 0.5 + radius,
            z + 0.5 + radius
        );
        List<CollectExecutor.Drop> found = new ArrayList<>();
        for (ItemEntity entity : level.getEntitiesOfClass(ItemEntity.class, bounds)) {
            ItemStack stack = entity.getItem();
            if (stack.isEmpty()
                || !BuiltInRegistries.ITEM.getKey(stack.getItem()).toString().equals(itemId)) {
                continue;
            }
            found.add(new CollectExecutor.Drop(
                entity.getUUID().toString(),
                itemId,
                stack.getCount(),
                entity.getX(),
                entity.getY(),
                entity.getZ()
            ));
        }
        return List.copyOf(found);
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
        long startedNanos = System.nanoTime();
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
                sendPage(connection, request, serverTick, resumed.page(), startedNanos);
                return;
            }
            BlockPos center = player.blockPosition();
            LoadedSearchResult result = scanner.scan(level, center, requestedBlocks, radius, verticalRadius, serverTick);
            sendPage(connection, request, serverTick, snapshots.first(fingerprint, result, limit), startedNanos);
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
        SearchSnapshotStore.Page page,
        long startedNanos
    ) {
        JsonObject response = baseResponse(request, "FIND_BLOCKS_RESULT");
        // Server-thread handler cost: the search's true tick impact, for the
        // frozen 40 ms search-caused-tick ceiling.
        response.addProperty("server_cost_micros", (System.nanoTime() - startedNanos) / 1_000L);
        response.addProperty("start", page.start());
        response.addProperty("total_matches", page.totalMatches());
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
            entry.addProperty("state", match.state());
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
