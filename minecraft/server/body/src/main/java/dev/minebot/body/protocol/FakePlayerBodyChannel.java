package dev.minebot.body.protocol;

import com.google.gson.JsonArray;
import com.google.gson.JsonNull;
import com.google.gson.JsonObject;
import com.mojang.serialization.JsonOps;
import dev.minebot.body.action.ActionRegistry;
import dev.minebot.body.action.ActionRuntime;
import dev.minebot.body.action.AscendExecutor;
import dev.minebot.body.action.BlockPrimitiveActions;
import dev.minebot.body.action.CollectExecutor;
import dev.minebot.body.action.ContainerPrimitiveActions;
import dev.minebot.body.action.CraftPrimitiveActions;
import dev.minebot.body.action.FurnacePrimitiveActions;
import dev.minebot.body.action.EngageExecutor;
import dev.minebot.body.action.FollowExecutor;
import dev.minebot.body.action.MutationGate;
import dev.minebot.body.action.MinecraftSurvivalEnvironment;
import dev.minebot.body.action.PlayerPrimitiveActions;
import dev.minebot.body.action.SpecialUseActions;
import dev.minebot.body.action.SurvivalReflexController;
import dev.minebot.body.control.FakePlayerActionOwner;
import dev.minebot.body.control.HeldInputs;
import dev.minebot.body.control.OwnerPriority;
import dev.minebot.body.control.PlayerCommandAdapter;
import dev.minebot.body.control.ServerPlayerBlockBreaker;
import dev.minebot.body.event.BotEventStream;
import dev.minebot.body.inventory.InventorySnapshot;
import dev.minebot.body.lifecycle.BodyLifecycleTracker;
import dev.minebot.body.nav.Goal;
import dev.minebot.body.nav.MinecraftWorldView;
import dev.minebot.body.nav.NavigateExecutor;
import dev.minebot.body.perception.EntityReadService;
import dev.minebot.body.perception.RecipeReadService;
import dev.minebot.body.perception.WorldReadService;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.level.block.entity.AbstractFurnaceBlockEntity;
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
import net.minecraft.world.entity.LivingEntity;
import net.minecraft.world.entity.monster.Enemy;
import net.minecraft.world.Container;
import net.minecraft.world.level.block.Block;
import net.minecraft.world.level.block.ChestBlock;
import net.minecraft.world.level.block.Blocks;
import net.minecraft.world.level.block.entity.BarrelBlockEntity;
import net.minecraft.world.phys.AABB;

import java.util.ArrayList;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;

public final class FakePlayerBodyChannel implements MineBotChannel {
    public static final String CHANNEL = "fakeplayer-body";
    public static final String PROTOCOL = "fakeplayer-body/1";
    private static final Set<String> REQUEST_TYPES = Set.of(
        "HELLO", "EVENT_HEAD", "SPAWN", "DESPAWN", "INTERRUPT",
        "SET_SURVIVAL_OWNER",
        "FIND_BLOCKS", "BODY_STATE", "INVENTORY", "CONTAINER_READ", "RECIPE_READ",
        "WORLD_READ", "ENTITY_READ", "NAVIGATE", "COLLECT_BLOCK", "ASCEND",
        "FOLLOW_ENTITY", "ENGAGE_ENTITY",
        "PLAYER_ACTION", "CONTAINER_TRANSFER", "CRAFT_ITEM", "FURNACE_TRANSFER",
        "MUTATION_VERDICT", "RESUME_EVENTS", "CANCEL_ACTION", "QUERY_ACTION"
    );
    private static final Set<String> HAZARD_GATED_REQUEST_TYPES = Set.of(
        "NAVIGATE", "COLLECT_BLOCK", "ASCEND", "FOLLOW_ENTITY", "ENGAGE_ENTITY", "PLAYER_ACTION",
        "CONTAINER_TRANSFER", "CRAFT_ITEM", "FURNACE_TRANSFER"
    );
    private static final int MAX_RADIUS = 128;
    private static final int MAX_VERTICAL_RADIUS = 64;
    private static final int MAX_PAGE_LIMIT = 128;
    private static final int MAX_REPLAY_EVENTS_PER_RESPONSE = 256;
    private static final int MAX_TIMEOUT_TICKS = 12_000;
    private static final Map<String, String> SEED_TO_CROP = Map.of(
        "minecraft:wheat_seeds", "minecraft:wheat",
        "minecraft:beetroot_seeds", "minecraft:beetroots",
        "minecraft:carrot", "minecraft:carrots",
        "minecraft:potato", "minecraft:potatoes"
    );

    private final MinecraftServer server;
    private final LoadedBlockScanner scanner = new LoadedBlockScanner();
    private final SearchSnapshotStore snapshots = new SearchSnapshotStore();
    private final BotEventStream events = new BotEventStream();
    private final ActionRegistry actions = new ActionRegistry();
    private final MutationGate mutationGate = new MutationGate();
    private final ServerPlayerBlockBreaker blockBreaker;
    private final PlayerCommandAdapter adapter;
    private final ActionRuntime runtime;
    private final SurvivalReflexController survival;
    private final BodyLifecycleTracker lifecycle;
    private final Set<MineBotConnection> subscribers = new LinkedHashSet<>();
    private final Map<String, String> pendingSpawnGameModes = new LinkedHashMap<>();
    private final String eventEpoch = UUID.randomUUID().toString();
    private int currentTick;

    public FakePlayerBodyChannel(MinecraftServer server) {
        this.server = server;
        this.blockBreaker = new ServerPlayerBlockBreaker(server);
        this.adapter = new PlayerCommandAdapter(server, new HeldInputs(), blockBreaker);
        this.runtime = new ActionRuntime(new FakePlayerActionOwner(), adapter, actions, events);
        this.survival = new SurvivalReflexController(
            runtime,
            adapter,
            new MinecraftSurvivalEnvironment(server),
            this::publishEvent
        );
        this.lifecycle = new BodyLifecycleTracker(this::publishEvent);
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
        survival.tick(serverTick);
        runtime.tick(serverTick);
        for (String botName : lifecycle.watchedBots()) {
            ServerPlayer player = server.getPlayerList().getPlayerByName(botName);
            if (present(player)) {
                // Carpet applies its spawn mode after JOIN; honor the request on the next tick.
                applyPendingSpawnGameMode(botName, player);
                lifecycle.observePresent(botName, lifecycleSnapshot(player), serverTick);
            } else if (lifecycle.observeMissing(botName, serverTick)) {
                runtime.abortCurrent(botName, "body_missing", serverTick);
            }
        }
    }

    public void playerJoined(ServerPlayer player, int serverTick) {
        if (player != null) {
            String botName = player.getGameProfile().name();
            lifecycle.observePresent(botName, lifecycleSnapshot(player), serverTick);
        }
    }

    public void playerLeft(ServerPlayer player, int serverTick) {
        if (player != null && lifecycle.observeMissing(player.getGameProfile().name(), serverTick)) {
            runtime.abortCurrent(player.getGameProfile().name(), "body_missing", serverTick);
        }
    }

    public void playerDied(ServerPlayer player, int serverTick) {
        if (player == null) {
            return;
        }
        String botName = player.getGameProfile().name();
        runtime.abortCurrent(botName, "death", serverTick);
        lifecycle.afterDeath(botName, lifecycleSnapshot(player), serverTick);
    }

    public void playerDamaged(
        ServerPlayer player,
        float amount,
        String source,
        boolean blocked,
        int serverTick
    ) {
        if (player != null) {
            lifecycle.afterDamage(
                player.getGameProfile().name(),
                amount,
                player.getHealth(),
                source,
                blocked,
                serverTick
            );
        }
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
        String botName = MineBotChannelRouter.stringField(request, "bot_name");
        if (botName != null && !hazardActionAllowed(botName, type, request)) {
            sendError(
                connection,
                request,
                serverTick,
                "hazard_unresolved",
                "an unresolved survival hazard blocks ordinary actions",
                false
            );
            return;
        }
        switch (type) {
            case "HELLO" -> handleHello(connection, request, serverTick);
            case "SET_SURVIVAL_OWNER" -> handleSetSurvivalOwner(connection, request, serverTick);
            case "EVENT_HEAD" -> handleEventHead(connection, request, serverTick);
            case "SPAWN" -> handleSpawn(connection, request, serverTick);
            case "DESPAWN" -> handleDespawn(connection, request, serverTick);
            case "INTERRUPT" -> handleInterrupt(connection, request, serverTick);
            case "FIND_BLOCKS" -> handleFindBlocks(connection, request, serverTick);
            case "BODY_STATE" -> handleBodyState(connection, request, serverTick);
            case "INVENTORY" -> handleInventory(connection, request, serverTick);
            case "CONTAINER_READ" -> handleContainerRead(connection, request, serverTick);
            case "RECIPE_READ" -> handleRecipeRead(connection, request, serverTick);
            case "WORLD_READ" -> handleWorldRead(connection, request, serverTick);
            case "ENTITY_READ" -> handleEntityRead(connection, request, serverTick);
            case "NAVIGATE" -> handleNavigate(connection, request, serverTick);
            case "COLLECT_BLOCK" -> handleCollectBlock(connection, request, serverTick);
            case "ASCEND" -> handleAscend(connection, request, serverTick);
            case "FOLLOW_ENTITY" -> handleFollowEntity(connection, request, serverTick);
            case "ENGAGE_ENTITY" -> handleEngageEntity(connection, request, serverTick);
            case "PLAYER_ACTION" -> handlePlayerAction(connection, request, serverTick);
            case "CONTAINER_TRANSFER" -> handleContainerTransfer(connection, request, serverTick);
            case "CRAFT_ITEM" -> handleCraftItem(connection, request, serverTick);
            case "FURNACE_TRANSFER" -> handleFurnaceTransfer(connection, request, serverTick);
            case "MUTATION_VERDICT" -> handleMutationVerdict(request);
            case "RESUME_EVENTS" -> handleResumeEvents(connection, request, serverTick);
            case "CANCEL_ACTION" -> handleCancelAction(connection, request, serverTick);
            case "QUERY_ACTION" -> handleQueryAction(connection, request, serverTick);
            default -> throw new IllegalStateException("unreachable request type " + type);
        }
    }

    private void handleRecipeRead(MineBotConnection connection, JsonObject request, int serverTick) {
        long startedNanos = System.nanoTime();
        try {
            String botName = requiredString(request, "bot_name", 64);
            String itemId = requiredItemId(request, "item");
            String recipeType = MineBotChannelRouter.stringField(request, "recipe_type");
            if (recipeType == null || recipeType.isBlank()) {
                recipeType = "crafting";
            }
            if (!Set.of("crafting", "smelting").contains(recipeType)) {
                throw new IllegalArgumentException("recipe_type must be crafting or smelting");
            }
            JsonObject response = baseResponse(request, "RECIPE_READ_RESULT");
            response.addProperty("bot", botName);
            ServerPlayer player = server.getPlayerList().getPlayerByName(botName);
            if (player == null || player.isRemoved() || !(player.level() instanceof ServerLevel level)) {
                response.addProperty("missing", true);
                connection.send(response, serverTick);
                return;
            }
            JsonArray variants = RecipeReadService.read(
                server.getRecipeManager().getRecipes(), level, itemId, recipeType
            );
            response.addProperty("missing", false);
            response.addProperty("found", !variants.isEmpty());
            response.addProperty("item", itemId);
            response.addProperty("recipe_type", recipeType);
            response.addProperty("variant_count", variants.size());
            response.add("variants", variants);
            response.addProperty("server_cost_micros", (System.nanoTime() - startedNanos) / 1_000L);
            connection.send(response, serverTick);
        } catch (IllegalArgumentException error) {
            sendError(connection, request, serverTick, "invalid_request", String.valueOf(error.getMessage()), false);
        } catch (RuntimeException error) {
            sendError(connection, request, serverTick, "recipe_read_internal_error", "recipe read failed", true);
        }
    }

    private void handleCraftItem(MineBotConnection connection, JsonObject request, int serverTick) {
        try {
            String botName = requiredString(request, "bot_name", 64);
            String actionId = requiredString(request, "action_id", 128);
            CraftPrimitiveActions.Request craft = requiredCraftRequest(request);
            ServerPlayer player = server.getPlayerList().getPlayerByName(botName);
            if (player == null || player.isRemoved() || !(player.level() instanceof ServerLevel level)) {
                sendError(connection, request, serverTick, "body_missing", "FakePlayer is not present", true);
                return;
            }

            ActionRuntime.Submission submission = runtime.submit(
                botName, actionId, "CRAFT_ITEM", OwnerPriority.ACTION, serverTick
            );
            JsonObject response = baseResponse(request, "CRAFT_ITEM_ACK");
            response.addProperty("action_id", actionId);
            switch (submission) {
                case ActionRuntime.Submission.Accepted ignored -> {
                    runtime.attachExecutor(actionId, new CraftPrimitiveActions.Executor(
                        botName,
                        actionId,
                        () -> CraftPrimitiveActions.craft(
                            craft,
                            player.getInventory(),
                            server.getRecipeManager().getRecipes(),
                            level
                        ),
                        runtime
                    ));
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
        } catch (RuntimeException error) {
            sendError(connection, request, serverTick, "craft_internal_error", "craft submission failed", true);
        }
    }

    private void handleContainerTransfer(MineBotConnection connection, JsonObject request, int serverTick) {
        try {
            String botName = requiredString(request, "bot_name", 64);
            String actionId = requiredString(request, "action_id", 128);
            BlockPos pos = requiredBlockPos(request, "pos");
            String direction = requiredString(request, "direction", 32);
            if (!Set.of("container_to_bot", "bot_to_container").contains(direction)) {
                throw new IllegalArgumentException("unsupported container direction: " + direction);
            }
            int containerSlot = boundedInt(request, "container_slot", Integer.MIN_VALUE, Integer.MAX_VALUE);
            int botSlot = boundedInt(request, "bot_slot", Integer.MIN_VALUE, Integer.MAX_VALUE);
            int count = boundedOptionalInt(request, "count", -1, -1, Integer.MAX_VALUE);
            int maxStack = boundedOptionalInt(request, "max_stack", 64, 1, 99);
            ServerPlayer player = server.getPlayerList().getPlayerByName(botName);
            if (player == null || player.isRemoved() || !(player.level() instanceof ServerLevel level)) {
                sendError(connection, request, serverTick, "body_missing", "FakePlayer is not present", true);
                return;
            }

            ActionRuntime.Submission submission = runtime.submit(
                botName, actionId, "CONTAINER_TRANSFER", OwnerPriority.ACTION, serverTick
            );
            JsonObject response = baseResponse(request, "CONTAINER_TRANSFER_ACK");
            response.addProperty("action_id", actionId);
            switch (submission) {
                case ActionRuntime.Submission.Accepted ignored -> {
                    runtime.attachExecutor(actionId, new ContainerPrimitiveActions.Executor(
                        botName,
                        actionId,
                        pos.getX(),
                        pos.getY(),
                        pos.getZ(),
                        direction,
                        containerSlot,
                        botSlot,
                        count,
                        maxStack,
                        () -> resolveContainerTarget(player, level, pos),
                        mutationGate,
                        this::publishProposal,
                        events,
                        runtime
                    ));
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
        } catch (RuntimeException error) {
            sendError(connection, request, serverTick, "container_transfer_internal_error", "container transfer failed", true);
        }
    }

    private void handleFurnaceTransfer(MineBotConnection connection, JsonObject request, int serverTick) {
        try {
            String botName = requiredString(request, "bot_name", 64);
            String actionId = requiredString(request, "action_id", 128);
            BlockPos pos = requiredBlockPos(request, "pos");
            String direction = requiredString(request, "direction", 32);
            if (!Set.of("furnace_to_bot", "bot_to_furnace").contains(direction)) {
                throw new IllegalArgumentException("unsupported furnace direction: " + direction);
            }
            String furnaceSlot = requiredString(request, "furnace_slot", 16);
            if (!Set.of("input", "fuel", "output").contains(furnaceSlot)) {
                throw new IllegalArgumentException("unsupported furnace slot: " + furnaceSlot);
            }
            int botSlot = boundedInt(request, "bot_slot", Integer.MIN_VALUE, Integer.MAX_VALUE);
            int count = boundedOptionalInt(request, "count", -1, -1, Integer.MAX_VALUE);
            int maxStack = boundedOptionalInt(request, "max_stack", 64, 1, 99);
            ServerPlayer player = server.getPlayerList().getPlayerByName(botName);
            if (player == null || player.isRemoved() || !(player.level() instanceof ServerLevel level)) {
                sendError(connection, request, serverTick, "body_missing", "FakePlayer is not present", true);
                return;
            }

            ActionRuntime.Submission submission = runtime.submit(
                botName, actionId, "FURNACE_TRANSFER", OwnerPriority.ACTION, serverTick
            );
            JsonObject response = baseResponse(request, "FURNACE_TRANSFER_ACK");
            response.addProperty("action_id", actionId);
            switch (submission) {
                case ActionRuntime.Submission.Accepted ignored -> {
                    runtime.attachExecutor(actionId, new FurnacePrimitiveActions.Executor(
                        botName,
                        actionId,
                        pos.getX(),
                        pos.getY(),
                        pos.getZ(),
                        direction,
                        furnaceSlot,
                        botSlot,
                        count,
                        maxStack,
                        () -> resolveFurnaceTarget(player, level, pos),
                        mutationGate,
                        this::publishProposal,
                        events,
                        runtime
                    ));
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
        } catch (RuntimeException error) {
            sendError(connection, request, serverTick, "furnace_transfer_internal_error", "furnace transfer failed", true);
        }
    }

    private void handlePlayerAction(MineBotConnection connection, JsonObject request, int serverTick) {
        try {
            String botName = requiredString(request, "bot_name", 64);
            String actionId = requiredString(request, "action_id", 128);
            String action = requiredString(request, "action", 32);
            if (!Set.of(
                "selectItem", "lookAt", "stop", "useItem", "moveItem", "dropItem",
                "handoffItem", "igniteBlock", "sowCrop", "mineBlock", "placeBlock", "jump"
            ).contains(action)) {
                throw new IllegalArgumentException("unsupported player action: " + action);
            }
            JsonObject params = request.has("params") && request.get("params").isJsonObject()
                ? request.getAsJsonObject("params")
                : new JsonObject();
            final String itemId = Set.of("selectItem", "handoffItem", "igniteBlock").contains(action)
                ? requiredItemId(params, "item")
                : action.equals("sowCrop")
                    ? requiredItemId(params, "seed_item")
                    : action.equals("useItem") ? optionalItemId(params, "item") : null;
            final String receiverName = action.equals("handoffItem")
                ? requiredString(params, "receiver", 64)
                : null;
            final int handoffCount = action.equals("handoffItem")
                ? boundedOptionalInt(params, "count", 1, 1, 99)
                : 0;
            final PlayerPrimitiveActions.Position target = action.equals("lookAt")
                ? requiredPosition(params, "target")
                : null;
            final String useMode = action.equals("useItem") ? optionalMode(params) : null;
            final int useTicks = action.equals("useItem")
                ? boundedOptionalInt(params, "ticks", 1, 1, PlayerPrimitiveActions.MAX_USE_TICKS)
                : 1;
            final int fromSlot = action.equals("moveItem")
                ? boundedInt(params, "from_slot", Integer.MIN_VALUE, Integer.MAX_VALUE)
                : -1;
            final int toSlot = action.equals("moveItem")
                ? boundedInt(params, "to_slot", Integer.MIN_VALUE, Integer.MAX_VALUE)
                : -1;
            final int moveCount = action.equals("moveItem")
                ? boundedOptionalInt(params, "count", -1, -1, Integer.MAX_VALUE)
                : -1;
            final int maxStack = action.equals("moveItem")
                ? boundedOptionalInt(params, "max_stack", 64, 1, 99)
                : 64;
            final int dropSlot = action.equals("dropItem")
                ? boundedOptionalInt(params, "slot", 0, Integer.MIN_VALUE, Integer.MAX_VALUE)
                : -1;
            final String dropMode = action.equals("dropItem")
                ? optionalDropMode(params)
                : null;
            final BlockPos blockTarget = Set.of(
                "igniteBlock", "sowCrop", "mineBlock", "placeBlock"
            ).contains(action)
                ? requiredBlockPos(params, "target")
                : null;
            final String blockId = Set.of("mineBlock", "placeBlock").contains(action)
                ? requiredBlockId(params, "block_type")
                : action.equals("igniteBlock")
                    ? "minecraft:fire"
                    : action.equals("sowCrop") ? requiredBlockId(params, "crop_block") : null;
            if (action.equals("sowCrop") && !blockId.equals(SEED_TO_CROP.get(itemId))) {
                throw new IllegalArgumentException("seed_item does not produce crop_block");
            }
            final boolean allowServerSubstitute = Set.of("igniteBlock", "sowCrop").contains(action)
                && optionalBoolean(params, "allow_server_substitute", false);
            final String specialUseContext = action.equals("igniteBlock")
                ? "activate"
                : action.equals("sowCrop") ? "farm" : null;
            final String mutationContext = action.equals("mineBlock")
                ? requiredChoice(
                    params, "context",
                    Set.of("path", "travel", "collect", "collect_approach", "farm", "recovery", "direct", "bot_cleanup")
                )
                : action.equals("placeBlock")
                    ? requiredChoice(
                        params, "context",
                        Set.of("travel", "work", "farm", "recovery", "direct")
                    )
                    : null;
            final String placeFace = action.equals("placeBlock")
                ? optionalChoice(params, "face", "up", Set.of("up", "down", "north", "south", "east", "west"))
                : null;
            final boolean replaceLiquid = action.equals("placeBlock")
                && optionalBoolean(params, "replace_liquid", false);
            final int actionTimeout = Set.of("mineBlock", "placeBlock").contains(action)
                ? boundedOptionalInt(params, "timeout_ticks", action.equals("mineBlock") ? 600 : 40, 1, MAX_TIMEOUT_TICKS)
                : action.equals("jump")
                    ? boundedOptionalInt(params, "timeout_ticks", 20, 1, MAX_TIMEOUT_TICKS)
                    : action.equals("handoffItem")
                        ? boundedOptionalInt(
                            params, "timeout_ticks", 60, 1, PlayerPrimitiveActions.MAX_HANDOFF_TICKS
                        )
                        : Set.of("igniteBlock", "sowCrop").contains(action)
                            ? boundedOptionalInt(params, "timeout_ticks", 20, 1, 200)
                            : 1;
            ServerPlayer player = server.getPlayerList().getPlayerByName(botName);
            if (player == null || player.isRemoved() || !(player.level() instanceof ServerLevel level)) {
                sendError(connection, request, serverTick, "body_missing", "FakePlayer is not present", true);
                return;
            }

            ActionRuntime.Submission submission = runtime.submit(
                botName, actionId, "PLAYER_ACTION:" + action, OwnerPriority.ACTION, serverTick
            );
            JsonObject response = baseResponse(request, "PLAYER_ACTION_ACK");
            response.addProperty("action_id", actionId);
            switch (submission) {
                case ActionRuntime.Submission.Accepted ignored -> {
                    PlayerPrimitiveActions.PlayerAccess access = playerAccess(player);
                    BlockPrimitiveActions.WorldAccess blockAccess = blockWorldAccess(player, level);
                    ActionRuntime.TickExecutor executor = switch (action) {
                        case "selectItem" -> {
                            yield new PlayerPrimitiveActions.ImmediateExecutor(
                                botName,
                                actionId,
                                () -> PlayerPrimitiveActions.selectItem(botName, itemId, access, adapter),
                                runtime
                            );
                        }
                        case "lookAt" -> {
                            yield new PlayerPrimitiveActions.ImmediateExecutor(
                                botName,
                                actionId,
                                () -> PlayerPrimitiveActions.lookAt(botName, target, access, adapter),
                                runtime
                            );
                        }
                        case "stop" -> new PlayerPrimitiveActions.ImmediateExecutor(
                            botName,
                            actionId,
                            PlayerPrimitiveActions::stop,
                            runtime
                        );
                        case "moveItem" -> new PlayerPrimitiveActions.ImmediateExecutor(
                            botName,
                            actionId,
                            () -> PlayerPrimitiveActions.moveItem(
                                fromSlot, toSlot, moveCount, maxStack, access
                            ),
                            runtime
                        );
                        case "dropItem" -> new PlayerPrimitiveActions.DropExecutor(
                            botName,
                            actionId,
                            dropSlot,
                            dropMode,
                            access,
                            adapter,
                            runtime
                        );
                        case "handoffItem" -> new PlayerPrimitiveActions.HandoffExecutor(
                            botName,
                            actionId,
                            receiverName,
                            itemId,
                            handoffCount,
                            actionTimeout,
                            access,
                            handoffAccess(player, level, receiverName, itemId),
                            runtime
                        );
                        case "igniteBlock", "sowCrop" -> new SpecialUseActions.Executor(
                            botName,
                            actionId,
                            action.equals("igniteBlock")
                                ? SpecialUseActions.Mode.IGNITE
                                : SpecialUseActions.Mode.SOW,
                            blockTarget.getX(),
                            blockTarget.getY(),
                            blockTarget.getZ(),
                            blockId,
                            itemId,
                            specialUseContext,
                            allowServerSubstitute,
                            actionTimeout,
                            specialUseAccess(player, level),
                            adapter,
                            mutationGate,
                            this::publishProposal,
                            runtime
                        );
                        case "useItem" -> {
                            yield new PlayerPrimitiveActions.UseExecutor(
                                botName,
                                actionId,
                                useMode,
                                itemId == null ? "unknown" : itemId,
                                useTicks,
                                access,
                                adapter,
                                runtime
                            );
                        }
                        case "mineBlock" -> new BlockPrimitiveActions.MineExecutor(
                            botName,
                            actionId,
                            blockTarget.getX(),
                            blockTarget.getY(),
                            blockTarget.getZ(),
                            blockId,
                            mutationContext,
                            actionTimeout,
                            blockAccess,
                            adapter,
                            blockBreaker,
                            mutationGate,
                            this::publishProposal,
                            runtime
                        );
                        case "placeBlock" -> new BlockPrimitiveActions.PlaceExecutor(
                            botName,
                            actionId,
                            blockTarget.getX(),
                            blockTarget.getY(),
                            blockTarget.getZ(),
                            blockId,
                            placeFace,
                            mutationContext,
                            replaceLiquid,
                            actionTimeout,
                            blockAccess,
                            adapter,
                            mutationGate,
                            this::publishProposal,
                            runtime
                        );
                        case "jump" -> new BlockPrimitiveActions.JumpExecutor(
                            botName,
                            actionId,
                            actionTimeout,
                            blockAccess,
                            adapter,
                            runtime
                        );
                        default -> throw new IllegalStateException("validated player action disappeared");
                    };
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
        } catch (RuntimeException error) {
            sendError(connection, request, serverTick, "player_action_internal_error", "player action failed", true);
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
            String botName = requiredBotName(request);
            lifecycle.watch(botName);
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
            BodyLifecycleTracker.Snapshot snapshot = lifecycleSnapshot(player);
            lifecycle.observePresent(botName, snapshot, serverTick);
            response.add("inventory_counts", snapshot.inventoryCounts().deepCopy());
            response.addProperty("inventory_raw", inventoryRaw(player));
            response.addProperty("inventory_hash", snapshot.inventoryHash());
            ItemStack selected = player.getMainHandItem();
            ItemStack offhand = player.getOffhandItem();
            response.addProperty("selected_slot", player.getInventory().getSelectedSlot());
            response.addProperty("selected_item", selected.isEmpty()
                ? null : BuiltInRegistries.ITEM.getKey(selected.getItem()).toString());
            response.addProperty("offhand_item", offhand.isEmpty()
                ? null : BuiltInRegistries.ITEM.getKey(offhand.getItem()).toString());
            JsonArray effects = new JsonArray();
            player.getActiveEffects().stream()
                .sorted((left, right) -> left.getEffect().getRegisteredName().compareTo(right.getEffect().getRegisteredName()))
                .forEach(effect -> {
                    JsonObject value = new JsonObject();
                    value.addProperty("id", effect.getEffect().getRegisteredName());
                    value.addProperty("duration", effect.getDuration());
                    value.addProperty("amplifier", effect.getAmplifier());
                    value.addProperty("ambient", effect.isAmbient());
                    value.addProperty("visible", effect.isVisible());
                    value.addProperty("show_icon", effect.showIcon());
                    effects.add(value);
                });
            response.add("effects", effects);
            response.addProperty("sleeping", player.isSleeping());
            response.addProperty(
                "weather",
                level.isThundering() ? "thunder" : level.isRaining() ? "rain" : "clear"
            );
            var owner = runtime.currentOwner(botName);
            String reflexOwner = survival.activeOwnerName(botName);
            response.addProperty(
                "body_owner",
                reflexOwner != null ? reflexOwner : owner == null ? null : owner.actionId()
            );
            response.addProperty("pending_action_count", runtime.pendingActionCount(botName));
            JsonObject unresolved = survival.hazardUnresolved(botName);
            response.add(
                "hazard_unresolved",
                unresolved == null ? JsonNull.INSTANCE : unresolved
            );
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

    /** Paged server-authoritative contents for a real single or combined block container. */
    private void handleContainerRead(MineBotConnection connection, JsonObject request, int serverTick) {
        try {
            String botName = requiredString(request, "bot_name", 64);
            BlockPos pos = requiredBlockPos(request, "pos");
            JsonObject response = baseResponse(request, "CONTAINER_READ_RESULT");
            response.addProperty("bot", botName);
            ServerPlayer player = server.getPlayerList().getPlayerByName(botName);
            if (player == null || player.isRemoved() || !(player.level() instanceof ServerLevel level)) {
                response.addProperty("missing", true);
                connection.send(response, serverTick);
                return;
            }
            var chunk = level.getChunkSource().getChunkNow(pos.getX() >> 4, pos.getZ() >> 4);
            if (chunk == null) {
                sendError(connection, request, serverTick, "container_unloaded", "container chunk is not loaded", true);
                return;
            }
            var state = chunk.getBlockState(pos);
            Container container = containerAt(level, pos, state.getBlock());
            if (container == null) {
                sendError(connection, request, serverTick, "container_unavailable", "target is not an openable container", false);
                return;
            }

            int totalSlots = Math.min(54, container.getContainerSize());
            if (totalSlots <= 0) {
                sendError(connection, request, serverTick, "container_unavailable", "container has no slots", false);
                return;
            }
            int requestedStart = boundedOptionalInt(request, "start", 0, Integer.MIN_VALUE, Integer.MAX_VALUE);
            int requestedLimit = boundedOptionalInt(request, "limit", totalSlots, Integer.MIN_VALUE, Integer.MAX_VALUE);
            int start = Math.max(0, Math.min(totalSlots - 1, requestedStart));
            int limit = Math.max(1, Math.min(totalSlots, requestedLimit));
            int end = Math.min(totalSlots, start + limit);
            JsonArray slots = new JsonArray();
            for (int slot = start; slot < end; slot++) {
                ItemStack stack = container.getItem(slot);
                JsonObject entry = new JsonObject();
                entry.addProperty("slot", slot);
                entry.addProperty("slotType", "container");
                entry.addProperty("slotLabel", "container." + slot);
                entry.addProperty("empty", stack.isEmpty());
                entry.addProperty("item", stack.isEmpty() ? null : BuiltInRegistries.ITEM.getKey(stack.getItem()).toString());
                entry.addProperty("count", stack.isEmpty() ? 0 : stack.getCount());
                entry.addProperty("stackRaw", stack.isEmpty() ? null : encodeStack(stack));
                slots.add(entry);
            }

            response.addProperty("missing", false);
            response.addProperty("start", start);
            response.addProperty("limit", limit);
            if (end >= totalSlots) {
                response.add("nextStart", JsonNull.INSTANCE);
            } else {
                response.addProperty("nextStart", end);
            }
            response.addProperty("totalSlots", totalSlots);
            JsonArray responsePos = new JsonArray();
            responsePos.add(pos.getX());
            responsePos.add(pos.getY());
            responsePos.add(pos.getZ());
            response.add("pos", responsePos);
            response.add("slots", slots);
            connection.send(response, serverTick);
        } catch (IllegalArgumentException error) {
            sendError(connection, request, serverTick, "invalid_request", String.valueOf(error.getMessage()), false);
        } catch (RuntimeException error) {
            sendError(connection, request, serverTick, "container_read_internal_error", "container snapshot failed", true);
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
        mutation.addProperty("context", proposal.context());
        frame.add("mutation", mutation);
        for (MineBotConnection subscriber : subscribers) {
            subscriber.send(frame, currentTick);
        }
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
            double finalReachDistance = optionalDouble(
                request,
                "final_reach_distance",
                dev.minebot.body.nav.PathFollower.WAYPOINT_REACH_DISTANCE,
                0.05,
                dev.minebot.body.nav.PathFollower.WAYPOINT_REACH_DISTANCE
            );
            boolean survivalRecovery = optionalBoolean(
                request, "survival_recovery", false
            );

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
                        timeoutTicks,
                        finalReachDistance
                    );
                    runtime.attachExecutor(actionId, executor);
                    response.addProperty("state", "accepted");
                    response.addProperty("survival_recovery", survivalRecovery);
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

    private void handleFollowEntity(MineBotConnection connection, JsonObject request, int serverTick) {
        try {
            String botName = requiredString(request, "bot_name", 64);
            String actionId = requiredString(request, "action_id", 128);
            String targetSpec = requiredString(request, "target_spec", 256);
            double keepRadius = boundedOptionalDouble(request, "keep_radius", 3.0, 0.0, 32.0);
            double replanDistance = boundedOptionalDouble(
                request, "replan_distance", 2.0, 0.5, 16.0
            );
            int acquireRadius = boundedOptionalInt(request, "acquire_radius", 32, 1, 64);
            int timeoutTicks = boundedOptionalInt(
                request, "timeout_ticks", 600, 1, MAX_TIMEOUT_TICKS
            );
            ServerPlayer player = server.getPlayerList().getPlayerByName(botName);
            if (player == null || player.isRemoved() || !(player.level() instanceof ServerLevel level)) {
                sendError(connection, request, serverTick, "body_missing", "FakePlayer is not present", true);
                return;
            }

            ActionRuntime.Submission submission = runtime.submit(
                botName, actionId, "FOLLOW_ENTITY", OwnerPriority.ACTION, serverTick
            );
            JsonObject response = baseResponse(request, "FOLLOW_ENTITY_ACK");
            response.addProperty("action_id", actionId);
            switch (submission) {
                case ActionRuntime.Submission.Accepted ignored -> {
                    runtime.attachExecutor(actionId, new FollowExecutor(
                        botName,
                        actionId,
                        targetSpec,
                        keepRadius,
                        replanDistance,
                        acquireRadius,
                        new MinecraftWorldView(level),
                        adapter,
                        engageTargetSource(player, level),
                        this::observedPosition,
                        this::publishEvent,
                        runtime,
                        timeoutTicks
                    ));
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
        } catch (RuntimeException error) {
            sendError(connection, request, serverTick, "follow_entity_internal_error", "follow submission failed", true);
        }
    }

    private void handleEngageEntity(MineBotConnection connection, JsonObject request, int serverTick) {
        try {
            String botName = requiredString(request, "bot_name", 64);
            String actionId = requiredString(request, "action_id", 128);
            String targetSpec = requiredString(request, "target_spec", 256);
            double attackRange = boundedOptionalDouble(request, "attack_range", 2.0, 1.2, 3.0);
            int cooldownTicks = boundedOptionalInt(request, "cooldown_ticks", 10, 1, 200);
            int acquireRadius = boundedOptionalInt(request, "acquire_radius", 32, 1, 64);
            int timeoutTicks = boundedOptionalInt(
                request, "timeout_ticks", 400, 1, MAX_TIMEOUT_TICKS
            );
            double disengageHealth = boundedOptionalDouble(
                request, "disengage_health", 6.0, 0.0, 40.0
            );
            ServerPlayer player = server.getPlayerList().getPlayerByName(botName);
            if (player == null || player.isRemoved() || !(player.level() instanceof ServerLevel level)) {
                sendError(connection, request, serverTick, "body_missing", "FakePlayer is not present", true);
                return;
            }

            ActionRuntime.Submission submission = runtime.submit(
                botName, actionId, "ENGAGE_ENTITY", OwnerPriority.ACTION, serverTick
            );
            JsonObject response = baseResponse(request, "ENGAGE_ENTITY_ACK");
            response.addProperty("action_id", actionId);
            switch (submission) {
                case ActionRuntime.Submission.Accepted ignored -> {
                    runtime.attachExecutor(actionId, new EngageExecutor(
                        botName,
                        actionId,
                        targetSpec,
                        attackRange,
                        cooldownTicks,
                        acquireRadius,
                        disengageHealth,
                        new MinecraftWorldView(level),
                        adapter,
                        adapter,
                        engageTargetSource(player, level),
                        this::observedPosition,
                        this::publishEvent,
                        runtime,
                        timeoutTicks
                    ));
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
        } catch (RuntimeException error) {
            sendError(connection, request, serverTick, "engage_entity_internal_error", "engagement submission failed", true);
        }
    }

    private EngageExecutor.TargetSource engageTargetSource(ServerPlayer actor, ServerLevel level) {
        return new EngageExecutor.TargetSource() {
            @Override
            public EngageExecutor.Lookup acquire(String botName, String targetSpec, double radius) {
                if ("player".equals(targetSpec) || "minecraft:player".equals(targetSpec)) {
                    return EngageExecutor.Lookup.missing("player_target_requires_name");
                }
                if (actor.getGameProfile().name().equals(targetSpec)) {
                    return EngageExecutor.Lookup.missing("self_target_disallowed");
                }
                List<net.minecraft.world.entity.Entity> matches = nearbyLivingEntities(radius, entity -> {
                    if ("nearest_hostile".equals(targetSpec)) {
                        return entity instanceof Enemy;
                    }
                    if (targetSpec.equals(entity.getUUID().toString())) {
                        return true;
                    }
                    String entityType = BuiltInRegistries.ENTITY_TYPE.getKey(entity.getType()).toString();
                    String normalizedType = targetSpec.contains(":") ? targetSpec : "minecraft:" + targetSpec;
                    return normalizedType.equals(entityType)
                        || targetSpec.equals(entity.getName().getString())
                        || (entity instanceof ServerPlayer player
                            && targetSpec.equals(player.getGameProfile().name()));
                });
                return closestLookup(matches);
            }

            @Override
            public EngageExecutor.Lookup refresh(String botName, String targetId, double radius) {
                List<net.minecraft.world.entity.Entity> matches = nearbyLivingEntities(
                    radius, entity -> targetId.equals(entity.getUUID().toString())
                );
                return closestLookup(matches);
            }

            @Override
            public Double bodyHealth(String botName) {
                return actor.isRemoved() ? null : (double) actor.getHealth();
            }

            @Override
            public boolean hasLineOfSight(String botName, EngageExecutor.Target target) {
                List<net.minecraft.world.entity.Entity> matches = nearbyLivingEntities(
                    64.0, entity -> target.id().equals(entity.getUUID().toString())
                );
                return !matches.isEmpty() && actor.hasLineOfSight(matches.get(0));
            }

            private List<net.minecraft.world.entity.Entity> nearbyLivingEntities(
                double radius,
                java.util.function.Predicate<net.minecraft.world.entity.Entity> predicate
            ) {
                double boundedRadius = Math.max(1.0, Math.min(64.0, radius));
                return level.getEntities(
                    actor,
                    actor.getBoundingBox().inflate(boundedRadius),
                    entity -> !entity.isRemoved()
                        && !entity.getUUID().equals(actor.getUUID())
                        && !entity.entityTags().contains("minebot.camera.observer")
                        && entity instanceof LivingEntity living
                        && living.isAlive()
                        && predicate.test(entity)
                );
            }

            private EngageExecutor.Lookup closestLookup(List<net.minecraft.world.entity.Entity> matches) {
                net.minecraft.world.entity.Entity closest = matches.stream()
                    .min(java.util.Comparator.comparingDouble(actor::distanceToSqr))
                    .orElse(null);
                if (closest == null) {
                    return EngageExecutor.Lookup.missing("target_not_found");
                }
                LivingEntity living = (LivingEntity) closest;
                return EngageExecutor.Lookup.found(new EngageExecutor.Target(
                    closest.getUUID().toString(),
                    BuiltInRegistries.ENTITY_TYPE.getKey(closest.getType()).toString(),
                    closest.getName().getString(),
                    closest.getX(),
                    closest.getY(),
                    closest.getZ(),
                    (double) living.getHealth(),
                    living.isAlive()
                ));
            }
        };
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
                if (json.getAsJsonArray("goals").size() > Goal.MAX_COMPOSITE_MEMBERS) {
                    throw new IllegalArgumentException(
                        "composite goal supports at most " + Goal.MAX_COMPOSITE_MEMBERS + " members"
                    );
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

    private void handleSetSurvivalOwner(
        MineBotConnection connection,
        JsonObject request,
        int serverTick
    ) {
        try {
            String botName = requiredBotName(request);
            boolean enabled = optionalBoolean(request, "enabled", false);
            lifecycle.watch(botName);
            if (enabled) {
                survival.watch(botName);
            } else {
                survival.unwatch(botName);
            }
            JsonObject response = baseResponse(request, "SET_SURVIVAL_OWNER_RESULT");
            response.addProperty("bot", botName);
            response.addProperty("enabled", enabled);
            connection.send(response, serverTick);
        } catch (IllegalArgumentException error) {
            sendError(connection, request, serverTick, "invalid_request", error.getMessage(), false);
        }
    }

    private void handleEventHead(MineBotConnection connection, JsonObject request, int serverTick) {
        try {
            String botName = requiredBotName(request);
            lifecycle.watch(botName);
            JsonObject response = baseResponse(request, "EVENT_HEAD_RESULT");
            response.addProperty("bot", botName);
            response.addProperty("epoch", eventEpoch);
            response.addProperty("event_seq", events.lastSeq(botName));
            response.addProperty("chat_seq", 0);
            var owner = runtime.currentOwner(botName);
            String reflexOwner = survival.activeOwnerName(botName);
            response.addProperty(
                "owner",
                reflexOwner != null ? reflexOwner : owner == null ? null : owner.actionId()
            );
            response.addProperty("pending_action_count", runtime.pendingActionCount(botName));
            response.addProperty("tick", serverTick);
            connection.send(response, serverTick);
        } catch (IllegalArgumentException error) {
            sendError(connection, request, serverTick, "invalid_request", error.getMessage(), false);
        }
    }

    private void handleSpawn(MineBotConnection connection, JsonObject request, int serverTick) {
        try {
            String botName = requiredBotName(request);
            BlockPos pos = optionalBlockPos(request, "pos");
            Float yaw = optionalFiniteFloat(request, "yaw");
            Float pitch = optionalFiniteFloat(request, "pitch");
            if ((yaw == null) != (pitch == null)) {
                throw new IllegalArgumentException("yaw and pitch must be supplied together");
            }
            String dimension = optionalIdentifier(request, "dimension");
            String gamemode = optionalChoice(
                request,
                "gamemode",
                null,
                Set.of("survival", "creative", "adventure", "spectator")
            );
            ServerPlayer before = server.getPlayerList().getPlayerByName(botName);
            boolean emitRespawned = optionalBoolean(request, "emit_respawned", false);
            lifecycle.requestSpawn(botName, emitRespawned && !present(before));
            if (gamemode == null) {
                pendingSpawnGameModes.remove(botName);
            } else {
                pendingSpawnGameModes.put(botName, gamemode);
            }
            if (!present(before)) {
                adapter.spawn(
                    botName,
                    pos == null ? null : pos.getX(),
                    pos == null ? null : pos.getY(),
                    pos == null ? null : pos.getZ(),
                    yaw,
                    pitch,
                    dimension
                );
            }
            ServerPlayer player = server.getPlayerList().getPlayerByName(botName);
            if (present(player)) {
                applyPendingSpawnGameMode(botName, player);
                adapter.clearAll(botName);
                lifecycle.observePresent(botName, lifecycleSnapshot(player), serverTick);
            }

            JsonObject response = baseResponse(request, "SPAWN_RESULT");
            response.addProperty("bot", botName);
            response.addProperty("accepted", true);
            response.addProperty("present", present(player));
            response.addProperty("already_present", present(before));
            connection.send(response, serverTick);
        } catch (IllegalArgumentException error) {
            sendError(connection, request, serverTick, "invalid_request", error.getMessage(), false);
        } catch (RuntimeException error) {
            sendError(connection, request, serverTick, "spawn_internal_error", "FakePlayer spawn failed", true);
        }
    }

    private void handleDespawn(MineBotConnection connection, JsonObject request, int serverTick) {
        try {
            String botName = requiredBotName(request);
            lifecycle.requestDespawn(botName);
            pendingSpawnGameModes.remove(botName);
            runtime.abortCurrent(botName, "despawn", serverTick);
            ServerPlayer before = server.getPlayerList().getPlayerByName(botName);
            if (present(before)) {
                adapter.despawn(botName);
            }
            ServerPlayer after = server.getPlayerList().getPlayerByName(botName);
            if (!present(after)) {
                lifecycle.observeMissing(botName, serverTick);
            }

            JsonObject response = baseResponse(request, "DESPAWN_RESULT");
            response.addProperty("bot", botName);
            response.addProperty("accepted", true);
            response.addProperty("missing", !present(after));
            response.addProperty("already_missing", !present(before));
            connection.send(response, serverTick);
        } catch (IllegalArgumentException error) {
            sendError(connection, request, serverTick, "invalid_request", error.getMessage(), false);
        } catch (RuntimeException error) {
            sendError(connection, request, serverTick, "despawn_internal_error", "FakePlayer despawn failed", true);
        }
    }

    private void handleInterrupt(MineBotConnection connection, JsonObject request, int serverTick) {
        try {
            String botName = requiredBotName(request);
            lifecycle.watch(botName);
            var owner = runtime.currentOwner(botName);
            boolean requested = owner != null && runtime.requestCancel(owner.actionId());
            if (owner == null) {
                adapter.clearAll(botName);
            }
            JsonObject response = baseResponse(request, "INTERRUPT_RESULT");
            response.addProperty("bot", botName);
            response.addProperty("accepted", owner == null || requested);
            response.addProperty("complete", owner == null);
            response.addProperty("owner_action_id", owner == null ? null : owner.actionId());
            response.addProperty("reason", MineBotChannelRouter.stringField(request, "reason"));
            connection.send(response, serverTick);
        } catch (IllegalArgumentException error) {
            sendError(connection, request, serverTick, "invalid_request", error.getMessage(), false);
        }
    }

    private boolean hazardActionAllowed(String botName, String type, JsonObject request) {
        if (!HAZARD_GATED_REQUEST_TYPES.contains(type)) {
            return true;
        }
        if ("PLAYER_ACTION".equals(type)
            && "stop".equals(MineBotChannelRouter.stringField(request, "action"))) {
            return true;
        }
        boolean survivalRecovery = false;
        if ("NAVIGATE".equals(type)
            && request.has("survival_recovery")
            && request.get("survival_recovery").isJsonPrimitive()) {
            try {
                survivalRecovery = request.get("survival_recovery").getAsBoolean();
            } catch (RuntimeException ignored) {
                // The request handler will report the malformed field.
            }
        }
        return survival.actionAllowed(botName, survivalRecovery);
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

    private ContainerPrimitiveActions.Target resolveContainerTarget(
        ServerPlayer player,
        ServerLevel level,
        BlockPos pos
    ) {
        if (player.isRemoved() || server.getPlayerList().getPlayer(player.getUUID()) != player) {
            return ContainerPrimitiveActions.Target.unavailable("missing_body");
        }
        var chunk = level.getChunkSource().getChunkNow(pos.getX() >> 4, pos.getZ() >> 4);
        if (chunk == null) {
            return ContainerPrimitiveActions.Target.unavailable("container_unloaded");
        }
        var state = chunk.getBlockState(pos);
        String blockId = LoadedBlockScanner.blockId(state.getBlock());
        if (!(state.getBlock() instanceof ChestBlock)
            && !(level.getBlockEntity(pos) instanceof BarrelBlockEntity)) {
            return ContainerPrimitiveActions.Target.unavailable("container_wrong_type");
        }
        Container container = containerAt(level, pos, state.getBlock());
        if (container == null) {
            return ContainerPrimitiveActions.Target.unavailable("container_unavailable");
        }
        if (!container.stillValid(player)) {
            return ContainerPrimitiveActions.Target.unavailable("container_out_of_range");
        }
        return new ContainerPrimitiveActions.Target(
            blockId,
            new MinecraftInventoryAccess(container),
            new MinecraftInventoryAccess(player.getInventory()),
            null
        );
    }

    private ContainerPrimitiveActions.Target resolveFurnaceTarget(
        ServerPlayer player,
        ServerLevel level,
        BlockPos pos
    ) {
        if (player.isRemoved() || server.getPlayerList().getPlayer(player.getUUID()) != player) {
            return ContainerPrimitiveActions.Target.unavailable("missing_body");
        }
        var chunk = level.getChunkSource().getChunkNow(pos.getX() >> 4, pos.getZ() >> 4);
        if (chunk == null) {
            return ContainerPrimitiveActions.Target.unavailable("furnace_unloaded");
        }
        var state = chunk.getBlockState(pos);
        String blockId = LoadedBlockScanner.blockId(state.getBlock());
        if (!(level.getBlockEntity(pos) instanceof AbstractFurnaceBlockEntity furnace)) {
            return ContainerPrimitiveActions.Target.unavailable("furnace_wrong_type");
        }
        if (!furnace.stillValid(player)) {
            return ContainerPrimitiveActions.Target.unavailable("furnace_out_of_range");
        }
        return new ContainerPrimitiveActions.Target(
            blockId,
            new MinecraftInventoryAccess(furnace),
            new MinecraftInventoryAccess(player.getInventory()),
            null
        );
    }

    private static Container containerAt(ServerLevel level, BlockPos pos, Block block) {
        if (block instanceof ChestBlock chest) {
            return ChestBlock.getContainer(chest, level.getBlockState(pos), level, pos, false);
        }
        return level.getBlockEntity(pos) instanceof Container container ? container : null;
    }

    private String encodeStack(ItemStack stack) {
        return ItemStack.CODEC.encodeStart(
            server.registryAccess().createSerializationContext(JsonOps.INSTANCE),
            stack
        ).getOrThrow().toString();
    }

    private BodyLifecycleTracker.Snapshot lifecycleSnapshot(ServerPlayer player) {
        return new BodyLifecycleTracker.Snapshot(
            player.getX(),
            player.getY(),
            player.getZ(),
            player.getHealth(),
            sha256(inventoryRaw(player)),
            inventoryCounts(player)
        );
    }

    private JsonObject inventoryCounts(ServerPlayer player) {
        JsonObject counts = new JsonObject();
        var inventory = player.getInventory();
        for (int slot = 0; slot < inventory.getContainerSize(); slot++) {
            ItemStack stack = inventory.getItem(slot);
            if (stack.isEmpty()) {
                continue;
            }
            String itemId = BuiltInRegistries.ITEM.getKey(stack.getItem()).toString();
            int existing = counts.has(itemId) ? counts.get(itemId).getAsInt() : 0;
            counts.addProperty(itemId, existing + stack.getCount());
        }
        return counts;
    }

    private String inventoryRaw(ServerPlayer player) {
        StringBuilder raw = new StringBuilder();
        var inventory = player.getInventory();
        for (int slot = 0; slot < inventory.getContainerSize(); slot++) {
            ItemStack stack = inventory.getItem(slot);
            raw.append(slot).append('=');
            if (!stack.isEmpty()) {
                raw.append(encodeStack(stack));
            }
            raw.append(';');
        }
        return raw.toString();
    }

    private static String sha256(String value) {
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256").digest(
                value.getBytes(StandardCharsets.UTF_8)
            );
            StringBuilder hex = new StringBuilder(digest.length * 2);
            for (byte part : digest) {
                hex.append(Character.forDigit((part >>> 4) & 0xf, 16));
                hex.append(Character.forDigit(part & 0xf, 16));
            }
            return hex.toString();
        } catch (NoSuchAlgorithmException impossible) {
            throw new IllegalStateException("SHA-256 is unavailable", impossible);
        }
    }

    private void applyPendingSpawnGameMode(String botName, ServerPlayer player) {
        if (!present(player)) {
            return;
        }
        String gameMode = pendingSpawnGameModes.get(botName);
        if (gameMode == null) {
            return;
        }
        adapter.setGameMode(botName, gameMode);
        pendingSpawnGameModes.remove(botName, gameMode);
    }

    private static boolean present(ServerPlayer player) {
        return player != null && !player.isRemoved();
    }

    private static final class MinecraftInventoryAccess implements ContainerPrimitiveActions.InventoryAccess {
        private final Container container;

        private MinecraftInventoryAccess(Container container) {
            this.container = container;
        }

        @Override
        public int size() {
            return container.getContainerSize();
        }

        @Override
        public boolean slotEmpty(int slot) {
            return container.getItem(slot).isEmpty();
        }

        @Override
        public String itemIdAt(int slot) {
            ItemStack stack = container.getItem(slot);
            return stack.isEmpty() ? null : BuiltInRegistries.ITEM.getKey(stack.getItem()).toString();
        }

        @Override
        public int itemCountAt(int slot) {
            return container.getItem(slot).getCount();
        }

        @Override
        public boolean sameStack(int slot, ContainerPrimitiveActions.InventoryAccess other, int otherSlot) {
            if (!(other instanceof MinecraftInventoryAccess target)) {
                return false;
            }
            return ItemStack.isSameItemSameComponents(container.getItem(slot), target.container.getItem(otherSlot));
        }

        @Override
        public int maxStackSizeAt(int slot) {
            return container.getItem(slot).getMaxStackSize();
        }

        @Override
        public int destinationMaxStackSize(
            int slot,
            ContainerPrimitiveActions.InventoryAccess source,
            int sourceSlot
        ) {
            if (!(source instanceof MinecraftInventoryAccess origin)) {
                return 0;
            }
            return container.getMaxStackSize(origin.container.getItem(sourceSlot));
        }

        @Override
        public boolean canMoveTo(
            int sourceSlot,
            ContainerPrimitiveActions.InventoryAccess destination,
            int destinationSlot
        ) {
            if (!(destination instanceof MinecraftInventoryAccess target)) {
                return false;
            }
            ItemStack stack = container.getItem(sourceSlot);
            return container.canTakeItem(target.container, sourceSlot, stack)
                && target.container.canPlaceItem(destinationSlot, stack);
        }

        @Override
        public void moveItemsTo(
            int sourceSlot,
            ContainerPrimitiveActions.InventoryAccess destination,
            int destinationSlot,
            int count
        ) {
            if (!(destination instanceof MinecraftInventoryAccess target)) {
                throw new IllegalArgumentException("destination is not a Minecraft inventory");
            }
            ItemStack source = container.getItem(sourceSlot);
            ItemStack existing = target.container.getItem(destinationSlot);
            ItemStack moved = source.split(count);
            if (existing.isEmpty()) {
                target.container.setItem(destinationSlot, moved);
            } else {
                existing.grow(moved.getCount());
            }
            if (source.isEmpty()) {
                container.setItem(sourceSlot, ItemStack.EMPTY);
            }
            container.setChanged();
            target.container.setChanged();
        }
    }

    private BlockPrimitiveActions.WorldAccess blockWorldAccess(ServerPlayer player, ServerLevel level) {
        return new BlockPrimitiveActions.WorldAccess() {
            @Override
            public boolean present() {
                return !player.isRemoved()
                    && server.getPlayerList().getPlayer(player.getUUID()) == player
                    && player.level() == level;
            }

            @Override
            public String blockIdAt(int x, int y, int z) {
                return observedBlockId(level, x, y, z);
            }

            @Override
            public boolean canReplaceAt(int x, int y, int z, boolean replaceLiquid) {
                var chunk = level.getChunkSource().getChunkNow(x >> 4, z >> 4);
                if (chunk == null || y < level.getMinY() || y > level.getMaxY()) {
                    return false;
                }
                var state = chunk.getBlockState(new BlockPos(x, y, z));
                return state.isAir()
                    || state.canBeReplaced()
                    || (replaceLiquid && !state.getFluidState().isEmpty());
            }

            @Override
            public boolean playerIntersects(int x, int y, int z) {
                return player.getBoundingBox().intersects(new AABB(new BlockPos(x, y, z)));
            }

            @Override
            public String selectedItemId() {
                ItemStack selected = player.getMainHandItem();
                return selected.isEmpty() ? null : BuiltInRegistries.ITEM.getKey(selected.getItem()).toString();
            }

            @Override
            public int selectedItemCount() {
                return player.getMainHandItem().getCount();
            }

            @Override
            public PlayerPrimitiveActions.Position position() {
                return new PlayerPrimitiveActions.Position(player.getX(), player.getY(), player.getZ());
            }
        };
    }

    private SpecialUseActions.WorldAccess specialUseAccess(ServerPlayer player, ServerLevel level) {
        return new SpecialUseActions.WorldAccess() {
            @Override
            public boolean present() {
                return !player.isRemoved()
                    && server.getPlayerList().getPlayer(player.getUUID()) == player
                    && player.level() == level;
            }

            @Override
            public String blockIdAt(int x, int y, int z) {
                return observedBlockId(level, x, y, z);
            }

            @Override
            public String selectedItemId() {
                ItemStack selected = player.getMainHandItem();
                return selected.isEmpty() ? null : BuiltInRegistries.ITEM.getKey(selected.getItem()).toString();
            }

            @Override
            public int selectedItemCount() {
                return player.getMainHandItem().getCount();
            }

            @Override
            public PlayerPrimitiveActions.Position position() {
                return new PlayerPrimitiveActions.Position(player.getX(), player.getY(), player.getZ());
            }

            @Override
            public boolean substituteFire(int x, int y, int z) {
                BlockPos pos = new BlockPos(x, y, z);
                var chunk = level.getChunkSource().getChunkNow(x >> 4, z >> 4);
                if (chunk == null) {
                    return false;
                }
                var current = chunk.getBlockState(pos);
                var fire = Blocks.FIRE.defaultBlockState();
                if (!current.canBeReplaced() || !fire.canSurvive(level, pos)) {
                    return false;
                }
                return level.setBlockAndUpdate(pos, fire);
            }

            @Override
            public boolean substituteCrop(
                int x,
                int y,
                int z,
                String cropBlockId,
                String seedItemId
            ) {
                if (!cropBlockId.equals(SEED_TO_CROP.get(seedItemId))) {
                    return false;
                }
                BlockPos farmlandPos = new BlockPos(x, y, z);
                BlockPos cropPos = farmlandPos.above();
                var chunk = level.getChunkSource().getChunkNow(x >> 4, z >> 4);
                if (chunk == null
                    || chunk.getBlockState(farmlandPos).getBlock() != Blocks.FARMLAND
                    || !chunk.getBlockState(cropPos).canBeReplaced()) {
                    return false;
                }
                Identifier cropIdentifier = Identifier.tryParse(cropBlockId);
                Block crop = cropIdentifier == null
                    ? null
                    : BuiltInRegistries.BLOCK.getOptional(cropIdentifier).orElse(null);
                ItemStack selected = player.getMainHandItem();
                if (crop == null || selected.isEmpty()
                    || !seedItemId.equals(BuiltInRegistries.ITEM.getKey(selected.getItem()).toString())) {
                    return false;
                }
                var cropState = crop.defaultBlockState();
                if (!cropState.canSurvive(level, cropPos) || !level.setBlockAndUpdate(cropPos, cropState)) {
                    return false;
                }
                selected.shrink(1);
                player.getInventory().setChanged();
                return true;
            }
        };
    }

    private PlayerPrimitiveActions.PlayerAccess playerAccess(ServerPlayer player) {
        return new PlayerPrimitiveActions.PlayerAccess() {
            @Override
            public boolean present() {
                return !player.isRemoved() && server.getPlayerList().getPlayer(player.getUUID()) == player;
            }

            @Override
            public int inventorySize() {
                return player.getInventory().getContainerSize();
            }

            @Override
            public String itemIdAt(int slot) {
                ItemStack stack = player.getInventory().getItem(slot);
                return stack.isEmpty() ? null : BuiltInRegistries.ITEM.getKey(stack.getItem()).toString();
            }

            @Override
            public int itemCountAt(int slot) {
                return player.getInventory().getItem(slot).getCount();
            }

            @Override
            public boolean slotEmpty(int slot) {
                return player.getInventory().getItem(slot).isEmpty();
            }

            @Override
            public void moveWholeStack(int fromSlot, int toSlot) {
                var inventory = player.getInventory();
                if (!inventory.getItem(toSlot).isEmpty()) {
                    throw new IllegalStateException("destination inventory slot is occupied");
                }
                ItemStack moved = inventory.removeItemNoUpdate(fromSlot);
                inventory.setItem(toSlot, moved);
                inventory.setChanged();
            }

            @Override
            public boolean sameStack(int firstSlot, int secondSlot) {
                return ItemStack.isSameItemSameComponents(
                    player.getInventory().getItem(firstSlot),
                    player.getInventory().getItem(secondSlot)
                );
            }

            @Override
            public int maxStackSizeAt(int slot) {
                return player.getInventory().getItem(slot).getItem().getDefaultMaxStackSize();
            }

            @Override
            public void moveItems(int fromSlot, int toSlot, int count) {
                var inventory = player.getInventory();
                ItemStack source = inventory.getItem(fromSlot);
                ItemStack destination = inventory.getItem(toSlot);
                ItemStack moved = source.split(count);
                if (destination.isEmpty()) {
                    inventory.setItem(toSlot, moved);
                } else {
                    destination.grow(moved.getCount());
                }
                if (source.isEmpty()) {
                    inventory.setItem(fromSlot, ItemStack.EMPTY);
                }
                inventory.setChanged();
            }

            @Override
            public int selectedHotbarSlot() {
                return player.getInventory().getSelectedSlot();
            }

            @Override
            public String selectedItemId() {
                ItemStack selected = player.getMainHandItem();
                return selected.isEmpty() ? null : BuiltInRegistries.ITEM.getKey(selected.getItem()).toString();
            }

            @Override
            public String inventoryFingerprint() {
                StringBuilder fingerprint = new StringBuilder();
                var inventory = player.getInventory();
                for (int slot = 0; slot < inventory.getContainerSize(); slot++) {
                    ItemStack stack = inventory.getItem(slot);
                    fingerprint
                        .append(slot).append(':')
                        .append(ItemStack.hashItemAndComponents(stack)).append(':')
                        .append(stack.getCount()).append(';');
                }
                return fingerprint.toString();
            }

            @Override
            public PlayerPrimitiveActions.Position position() {
                return new PlayerPrimitiveActions.Position(player.getX(), player.getY(), player.getZ());
            }

            @Override
            public PlayerPrimitiveActions.Position eyePosition() {
                var position = player.getEyePosition();
                return new PlayerPrimitiveActions.Position(position.x(), position.y(), position.z());
            }

            @Override
            public PlayerPrimitiveActions.Position lookDirection() {
                var look = player.getLookAngle();
                return new PlayerPrimitiveActions.Position(look.x(), look.y(), look.z());
            }

            @Override
            public float yaw() {
                return player.getYRot();
            }

            @Override
            public float pitch() {
                return player.getXRot();
            }
        };
    }

    private PlayerPrimitiveActions.HandoffAccess handoffAccess(
        ServerPlayer giver,
        ServerLevel level,
        String receiverName,
        String itemId
    ) {
        return new PlayerPrimitiveActions.HandoffAccess() {
            private ServerPlayer receiver() {
                ServerPlayer receiver = server.getPlayerList().getPlayerByName(receiverName);
                if (receiver == null || receiver.isRemoved() || receiver.level() != level) {
                    return null;
                }
                return receiver;
            }

            @Override
            public boolean receiverPresent() {
                return receiver() != null;
            }

            @Override
            public PlayerPrimitiveActions.Position receiverPosition() {
                ServerPlayer receiver = receiver();
                return receiver == null
                    ? new PlayerPrimitiveActions.Position(0.0, 0.0, 0.0)
                    : new PlayerPrimitiveActions.Position(receiver.getX(), receiver.getY(), receiver.getZ());
            }

            @Override
            public int receiverItemCount(String requestedItemId) {
                ServerPlayer receiver = receiver();
                if (receiver == null) {
                    return 0;
                }
                int total = 0;
                var inventory = receiver.getInventory();
                for (int slot = 0; slot < inventory.getContainerSize(); slot++) {
                    ItemStack stack = inventory.getItem(slot);
                    if (!stack.isEmpty()
                        && requestedItemId.equals(BuiltInRegistries.ITEM.getKey(stack.getItem()).toString())) {
                        total += stack.getCount();
                    }
                }
                return total;
            }

            @Override
            public int spawnAtReceiver(int sourceSlot, int count) {
                ServerPlayer receiver = receiver();
                if (receiver == null || count <= 0) {
                    return 0;
                }
                var inventory = giver.getInventory();
                if (sourceSlot < 0 || sourceSlot >= inventory.getContainerSize()) {
                    return 0;
                }
                ItemStack source = inventory.getItem(sourceSlot);
                if (source.isEmpty()
                    || !itemId.equals(BuiltInRegistries.ITEM.getKey(source.getItem()).toString())) {
                    return 0;
                }
                ItemStack moved = source.split(Math.min(count, source.getCount()));
                if (moved.isEmpty()) {
                    return 0;
                }
                ItemEntity item = new ItemEntity(
                    level,
                    receiver.getX(),
                    receiver.getY() + 0.25,
                    receiver.getZ(),
                    moved
                );
                item.setNoPickUpDelay();
                item.setTarget(receiver.getUUID());
                if (!level.addFreshEntity(item)) {
                    source.grow(moved.getCount());
                    return 0;
                }
                if (source.isEmpty()) {
                    inventory.setItem(sourceSlot, ItemStack.EMPTY);
                }
                inventory.setChanged();
                return moved.getCount();
            }
        };
    }

    private static PlayerPrimitiveActions.Position requiredPosition(JsonObject params, String name) {
        if (!params.has(name) || !params.get(name).isJsonArray() || params.getAsJsonArray(name).size() != 3) {
            throw new IllegalArgumentException(name + " must be a three-number array");
        }
        JsonArray values = params.getAsJsonArray(name);
        double x = values.get(0).getAsDouble();
        double y = values.get(1).getAsDouble();
        double z = values.get(2).getAsDouble();
        if (!Double.isFinite(x) || !Double.isFinite(y) || !Double.isFinite(z)) {
            throw new IllegalArgumentException(name + " must contain finite coordinates");
        }
        return new PlayerPrimitiveActions.Position(x, y, z);
    }

    private static BlockPos requiredBlockPos(JsonObject params, String name) {
        if (!params.has(name) || !params.get(name).isJsonArray() || params.getAsJsonArray(name).size() != 3) {
            throw new IllegalArgumentException(name + " must be a three-number array");
        }
        JsonArray values = params.getAsJsonArray(name);
        double x = values.get(0).getAsDouble();
        double y = values.get(1).getAsDouble();
        double z = values.get(2).getAsDouble();
        if (!Double.isFinite(x) || !Double.isFinite(y) || !Double.isFinite(z)
            || x != Math.floor(x) || y != Math.floor(y) || z != Math.floor(z)) {
            throw new IllegalArgumentException(name + " must contain finite integer coordinates");
        }
        return new BlockPos((int) x, (int) y, (int) z);
    }

    private static String requiredItemId(JsonObject params, String name) {
        String itemId = optionalItemId(params, name);
        if (itemId == null) {
            throw new IllegalArgumentException(name + " is required");
        }
        return itemId;
    }

    private static String requiredBlockId(JsonObject params, String name) {
        String value = MineBotChannelRouter.stringField(params, name);
        if (value == null || value.isBlank()) {
            throw new IllegalArgumentException(name + " is required");
        }
        String normalized = value.contains(":") ? value : "minecraft:" + value;
        if (!normalized.matches("[a-z0-9_.-]+:[a-z0-9_/.-]+")) {
            throw new IllegalArgumentException(name + " is not a valid block identifier");
        }
        Identifier identifier = Identifier.tryParse(normalized);
        if (identifier == null || BuiltInRegistries.BLOCK.getOptional(identifier).isEmpty()) {
            throw new IllegalArgumentException("unknown block id: " + normalized);
        }
        return normalized;
    }

    private static CraftPrimitiveActions.Request requiredCraftRequest(JsonObject request) {
        if (!request.has("inputs") || !request.get("inputs").isJsonArray()) {
            throw new IllegalArgumentException("inputs must be a nonempty array");
        }
        JsonArray rawInputs = request.getAsJsonArray("inputs");
        if (rawInputs.isEmpty() || rawInputs.size() > InventorySnapshot.TOTAL_SLOTS) {
            throw new IllegalArgumentException("inputs must contain 1 to 46 entries");
        }
        List<CraftPrimitiveActions.Input> inputs = new ArrayList<>();
        for (var element : rawInputs) {
            if (!element.isJsonObject()) {
                throw new IllegalArgumentException("inputs entries must be objects");
            }
            JsonObject input = element.getAsJsonObject();
            inputs.add(new CraftPrimitiveActions.Input(
                boundedInt(input, "slot", 0, InventorySnapshot.TOTAL_SLOTS - 1),
                requiredItemId(input, "item"),
                boundedInt(input, "count", 1, 99)
            ));
        }
        if (!request.has("output") || !request.get("output").isJsonObject()) {
            throw new IllegalArgumentException("output must be an object");
        }
        JsonObject rawOutput = request.getAsJsonObject("output");
        CraftPrimitiveActions.Output output = new CraftPrimitiveActions.Output(
            boundedInt(rawOutput, "slot", 0, InventorySnapshot.TOTAL_SLOTS - 1),
            requiredItemId(rawOutput, "item"),
            boundedInt(rawOutput, "count", 1, 99)
        );
        List<CraftPrimitiveActions.Remainder> remainders = new ArrayList<>();
        if (request.has("remainders")) {
            if (!request.get("remainders").isJsonArray()
                || request.getAsJsonArray("remainders").size() > InventorySnapshot.TOTAL_SLOTS) {
                throw new IllegalArgumentException("remainders must be an array with at most 46 entries");
            }
            for (var element : request.getAsJsonArray("remainders")) {
                if (!element.isJsonObject()) {
                    throw new IllegalArgumentException("remainder entries must be objects");
                }
                JsonObject remainder = element.getAsJsonObject();
                remainders.add(new CraftPrimitiveActions.Remainder(
                    boundedInt(remainder, "slot", 0, InventorySnapshot.TOTAL_SLOTS - 1),
                    requiredItemId(remainder, "item"),
                    boundedInt(remainder, "count", 1, 99)
                ));
            }
        }
        return new CraftPrimitiveActions.Request(
            List.copyOf(inputs),
            output,
            List.copyOf(remainders),
            boundedOptionalInt(request, "max_stack", 64, 1, 99)
        );
    }

    private static String optionalItemId(JsonObject params, String name) {
        String value = MineBotChannelRouter.stringField(params, name);
        if (value == null || value.isBlank()) {
            return null;
        }
        String normalized = value.contains(":") ? value : "minecraft:" + value;
        if (!normalized.matches("[a-z0-9_.-]+:[a-z0-9_/.-]+")) {
            throw new IllegalArgumentException(name + " is not a valid item identifier");
        }
        Identifier identifier = Identifier.tryParse(normalized);
        if (identifier == null || BuiltInRegistries.ITEM.getOptional(identifier).isEmpty()) {
            throw new IllegalArgumentException("unknown item id: " + normalized);
        }
        return normalized;
    }

    private static String optionalMode(JsonObject params) {
        String mode = MineBotChannelRouter.stringField(params, "mode");
        if (mode == null || mode.isBlank()) {
            return "once";
        }
        if (!mode.equals("once") && !mode.equals("continuous")) {
            throw new IllegalArgumentException("mode must be once or continuous");
        }
        return mode;
    }

    private static String optionalDropMode(JsonObject params) {
        String mode = MineBotChannelRouter.stringField(params, "mode");
        if (mode == null || mode.isBlank()) {
            return "one";
        }
        if (!mode.equals("one") && !mode.equals("all")) {
            throw new IllegalArgumentException("mode must be one or all");
        }
        return mode;
    }

    private static String optionalChoice(
        JsonObject params,
        String name,
        String fallback,
        Set<String> allowed
    ) {
        String value = MineBotChannelRouter.stringField(params, name);
        if (value == null || value.isBlank()) {
            return fallback;
        }
        if (!allowed.contains(value)) {
            throw new IllegalArgumentException(name + " has unsupported value: " + value);
        }
        return value;
    }

    private static String requiredChoice(JsonObject params, String name, Set<String> allowed) {
        String value = MineBotChannelRouter.stringField(params, name);
        if (value == null || value.isBlank()) {
            throw new IllegalArgumentException(name + " is required");
        }
        if (!allowed.contains(value)) {
            throw new IllegalArgumentException(name + " has unsupported value: " + value);
        }
        return value;
    }

    private static boolean optionalBoolean(JsonObject params, String name, boolean fallback) {
        if (!params.has(name) || params.get(name).isJsonNull()) {
            return fallback;
        }
        if (!params.get(name).isJsonPrimitive()
            || !params.getAsJsonPrimitive(name).isBoolean()) {
            throw new IllegalArgumentException(name + " must be a boolean");
        }
        return params.get(name).getAsBoolean();
    }

    private static String requiredString(JsonObject request, String name, int maxLength) {
        String value = MineBotChannelRouter.stringField(request, name);
        if (value == null || value.isBlank() || value.length() > maxLength) {
            throw new IllegalArgumentException(name + " is required");
        }
        return value;
    }

    private static String requiredBotName(JsonObject request) {
        String value = requiredString(request, "bot_name", 16);
        if (!value.matches("[A-Za-z0-9_]{1,16}")) {
            throw new IllegalArgumentException("bot_name is not a valid Minecraft player name");
        }
        return value;
    }

    private static BlockPos optionalBlockPos(JsonObject request, String name) {
        if (!request.has(name) || request.get(name).isJsonNull()) {
            return null;
        }
        return requiredBlockPos(request, name);
    }

    private static Float optionalFiniteFloat(JsonObject request, String name) {
        if (!request.has(name) || request.get(name).isJsonNull()) {
            return null;
        }
        if (!request.get(name).isJsonPrimitive()) {
            throw new IllegalArgumentException(name + " must be a finite number");
        }
        float value;
        try {
            value = request.get(name).getAsFloat();
        } catch (RuntimeException error) {
            throw new IllegalArgumentException(name + " must be a finite number");
        }
        if (!Float.isFinite(value)) {
            throw new IllegalArgumentException(name + " must be a finite number");
        }
        return value;
    }

    private static String optionalIdentifier(JsonObject request, String name) {
        String value = MineBotChannelRouter.stringField(request, name);
        if (value == null || value.isBlank()) {
            return null;
        }
        String normalized = value.contains(":") ? value : "minecraft:" + value;
        if (Identifier.tryParse(normalized) == null) {
            throw new IllegalArgumentException(name + " is not a valid identifier");
        }
        return normalized;
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

    private static double boundedOptionalDouble(
        JsonObject request,
        String name,
        double fallback,
        double min,
        double max
    ) {
        if (!request.has(name) || request.get(name).isJsonNull()) {
            return fallback;
        }
        if (!request.get(name).isJsonPrimitive()) {
            throw new IllegalArgumentException(name + " must be a number");
        }
        double value;
        try {
            value = request.get(name).getAsDouble();
        } catch (RuntimeException error) {
            throw new IllegalArgumentException(name + " must be a number");
        }
        if (!Double.isFinite(value) || value < min || value > max) {
            throw new IllegalArgumentException(name + " is out of bounds");
        }
        return value;
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
