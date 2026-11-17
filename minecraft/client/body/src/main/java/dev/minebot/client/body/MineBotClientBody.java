package dev.minebot.client.body;

import baritone.api.BaritoneAPI;
import baritone.api.IBaritone;
import baritone.api.event.events.PathEvent;
import baritone.api.event.listener.AbstractGameEventListener;
import baritone.api.pathing.goals.GoalBlock;
import baritone.api.pathing.goals.GoalTwoBlocks;
import baritone.api.pathing.goals.Goal;
import baritone.api.pathing.goals.GoalComposite;
import baritone.api.pathing.goals.GoalXZ;
import baritone.api.pathing.goals.GoalNear;
import baritone.api.pathing.goals.GoalRunAway;
import baritone.api.pathing.goals.GoalYLevel;
import baritone.api.schematic.FillSchematic;
import baritone.pathing.path.PathExecutor;
import baritone.pathing.movement.Movement;
import baritone.pathing.movement.MovementHelper;
import baritone.utils.BlockStateInterface;
import dev.minebot.client.body.mixin.MiningProgressAccessor;
import baritone.api.schematic.MaskSchematic;
import baritone.api.selection.ISelection;
import baritone.api.utils.BlockOptionalMeta;
import baritone.api.utils.BlockOptionalMetaLookup;
import baritone.api.utils.BetterBlockPos;
import baritone.api.utils.Rotation;
import baritone.api.utils.RotationUtils;
import baritone.api.utils.input.Input;
import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import net.fabricmc.api.ClientModInitializer;
import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientTickEvents;
import net.fabricmc.fabric.api.client.message.v1.ClientReceiveMessageEvents;
import net.fabricmc.fabric.api.client.networking.v1.ClientPlayConnectionEvents;
import net.fabricmc.fabric.api.event.player.UseBlockCallback;
import net.minecraft.client.Minecraft;
import net.minecraft.client.gui.screens.DeathScreen;
import net.minecraft.core.BlockPos;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.item.CrossbowItem;
import net.minecraft.world.InteractionHand;
import net.minecraft.world.InteractionResult;
import net.minecraft.world.level.block.state.BlockState;
import net.minecraft.world.level.block.state.properties.Property;
import net.minecraft.world.level.block.state.properties.BlockStateProperties;
import net.minecraft.world.level.block.state.properties.DoubleBlockHalf;
import net.minecraft.world.phys.BlockHitResult;
import net.minecraft.world.level.ClipContext;
import net.minecraft.world.phys.HitResult;
import net.minecraft.world.phys.EntityHitResult;
import net.minecraft.world.phys.Vec3;
import net.minecraft.core.Direction;
import net.minecraft.tags.FluidTags;
import net.minecraft.util.Mth;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.entity.AreaEffectCloud;
import net.minecraft.world.entity.Mob;
import net.minecraft.world.entity.EquipmentSlot;
import net.minecraft.world.entity.LivingEntity;
import net.minecraft.world.entity.item.ItemEntity;
import net.minecraft.world.entity.monster.Enemy;
import net.minecraft.world.entity.monster.Creeper;
import net.minecraft.world.entity.monster.EnderMan;
import net.minecraft.world.entity.monster.piglin.Piglin;
import net.minecraft.world.entity.NeutralMob;
import net.minecraft.network.chat.contents.TranslatableContents;
import net.minecraft.network.protocol.game.ClientboundDamageEventPacket;
import net.minecraft.world.inventory.AbstractContainerMenu;
import net.minecraft.world.inventory.AbstractFurnaceMenu;
import net.minecraft.world.inventory.ContainerInput;
import net.minecraft.world.inventory.InventoryMenu;
import net.minecraft.world.inventory.AbstractCraftingMenu;
import net.minecraft.world.inventory.CraftingMenu;
import net.minecraft.world.inventory.Slot;
import net.minecraft.world.item.crafting.display.RecipeDisplay;
import net.minecraft.world.item.crafting.display.RecipeDisplayEntry;
import net.minecraft.world.item.crafting.display.ShapedCraftingRecipeDisplay;
import net.minecraft.world.item.crafting.display.ShapelessCraftingRecipeDisplay;
import net.minecraft.world.item.crafting.display.FurnaceRecipeDisplay;
import net.minecraft.world.item.crafting.display.SlotDisplay;
import net.minecraft.world.item.crafting.display.SlotDisplayContext;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.DataInputStream;
import java.io.DataOutputStream;
import java.io.EOFException;
import java.io.IOException;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.LinkedHashMap;
import java.util.Locale;
import java.util.Map;
import java.util.Set;

/** Real-client Body adapter: typed actions are executed on the client tick. */
public final class MineBotClientBody implements ClientModInitializer {
    private static MineBotClientBody instance;
    private static volatile String lastGameMessage = "";
    private static volatile long lastGameMessageSequence;
    // The game's last word on the respawn point, and the block last used, which a
    // respawn anchor or a bed used by day sets it to (vision-and-home-20261028.md §7.7 item 5).
    private static volatile JsonObject lastSpawnMessage;
    private static long spawnMessageSequence;
    private static volatile BlockPos lastUsedBlock;
    private static final Logger LOG = LoggerFactory.getLogger("minebot-client-body");
    private static final int MAX_FRAME = 1024 * 1024;
    private final Set<String> usedIds = new HashSet<>();
    private IBaritone baritone;
    private Run run;
    /**
     * Legs (body-channels-20261105 §6.1): the leg mode the Body holds outside
     * the Run, null = stand. Hand Runs (a shot, the shield, eating) run on top
     * of it; every other Run holds the legs while it runs and they go on by
     * themselves afterwards (user 2026-11-06: 很多时候，失败都是因为它站在原地).
     */
    private Run legs;
    private boolean legsPaused;
    private int legsSeq;
    private Vec3 legsMovedFrom;
    private int legsMovedTick;
    private double legsWalked;
    /** Legs that have not moved 1.5 blocks in this many ticks cannot move: the mode ends and says so. */
    private static final int LEGS_CANNOT_MOVE_TICKS = 200;
    /** Eyes: the gaze setting; on, the Body looks at the ground whenever nothing aims. */
    private boolean gazeDown;
    private MenuRead menuRead;
    private long menuContentRevision;
    private int contentMenuId = -1;
    private final Map<Integer, ItemStack> confirmedResults = new LinkedHashMap<>();
    private long resultRevision;
    private volatile Peer peer;
    private int joinedTicks;
    private volatile boolean ready;
    private long readyAtMs;
    private final long loadedAt = System.nanoTime();
    private DeathRespawnProgress lifecycle = new DeathRespawnProgress();
    private final List<JsonObject> lifecycleEvents = new ArrayList<>();
    private long lifecycleSeq;
    private final String lifecycleEpoch = java.util.UUID.randomUUID().toString();
    private JsonObject lastLivingState;
    private JsonObject lastDamageSource;
    private long lastDamageTick = -1L;
    // Hurts that came from where the Body stood, not from a mob's blow, an
    // arrow or an effect it carries: {game tick, x, y, z}. Whatever the place
    // holds (breath, fire, lava, a block), the Body knows it as health lost
    // there (dragon-unit-tests-20261030 §6.y.11).
    private final java.util.ArrayDeque<double[]> placeHurts = new java.util.ArrayDeque<>();
    // The last arrow a ranged action loosed, followed after the action ended
    // until it hits, kills or misses; said in the state as last_shot.
    private ShotWatch lastShot;


    @Override
    public void onInitializeClient() {
        instance = this;
        ClientPlayConnectionEvents.JOIN.register((handler, sender, client) -> {
            lifecycle = new DeathRespawnProgress();
            lifecycleEvents.clear();
            lastLivingState = null;
            joinedTicks = 0;
            shelterOwned.clear();
            lastShelterHostile = null;
            ready = false;
            baritone = BaritoneAPI.getProvider().getBaritoneForMinecraft(client);
            // Paths go around a cloud on the ground (dragon's breath), not
            // through it; mobs are not avoided: the Body walks up to what it
            // fights (body-preemption-20261106.md §8.3, patched Baritone).
            BaritoneAPI.getSettings().avoidance.value = true;
            BaritoneAPI.getSettings().mobAvoidanceCoefficient.value = 1.0;
            BaritoneAPI.getSettings().mobSpawnerAvoidanceCoefficient.value = 1.0;
            BaritoneAPI.getSettings().cloudAvoidanceCoefficient.value = 8.0;
            // The protected set outlives a rejoin; Python resends it on each IPC handshake.
            lastDamageSource = null;
            lastDamageTick = -1L;
            lastShot = null;
            baritone.getGameEventHandler().registerEventListener(new AbstractGameEventListener() {
                @Override
                public void onPathEvent(PathEvent event) {
                    if (run != null) {
                        run.lastPathEvent = event.name();
                        run.pathEvents.merge(event.name(), 1, Integer::sum);
                        // The stop itself cancels Baritone, so the last event is no answer
                        // to "was a route found"; the last calculation outcome is.
                        if (event == PathEvent.CALC_FAILED) run.routeFound = false;
                        if (event == PathEvent.CALC_FINISHED_NOW_EXECUTING
                            || event == PathEvent.NEXT_SEGMENT_CALC_FINISHED) run.routeFound = true;
                        if (run.swimRouteGoal != null && event == PathEvent.CALC_FAILED) {
                            run.swimPathFailed = true;
                        }
                        // A Baritone builder cancels its segment every tick while it
                        // breaks or places, and the world being gone does the same: one
                        // line per tick filled 62 MB of client.log in H5 (2026-10-29).
                        // The count is kept in the run either way.
                        int seen = run.pathEvents.get(event.name());
                        if (event != PathEvent.CANCELED || seen == 1 || seen % 200 == 0) {
                            LOG.info("MINEBOT_PHASE_A path_event id={} event={} count={}", run.id, event, seen);
                        }
                    }
                }
            });
            if (Boolean.getBoolean("minebot.client.early_start")) {
                String objective = System.getProperty("minebot.client.objective", "").trim();
                if (!objective.isBlank()) {
                    JsonObject request = envelope("command", "early-start");
                    request.addProperty("objective", objective);
                    request.addProperty("timeout_ms", 45000);
                    try {
                        start(client, request, null);
                        LOG.warn("MINEBOT_PHASE_A early_start objective={} player={} chunk_loaded={} on_ground={}",
                            objective, client.player == null ? "none" : client.player.getName().getString(),
                            client.level != null && client.player != null && client.level.hasChunkAt(client.player.blockPosition()),
                            client.player != null && client.player.onGround());
                    } catch (RuntimeException error) {
                        LOG.error("MINEBOT_PHASE_A early_start_failed objective={} reason={}", objective, error.toString());
                    }
                }
            }
        });
        ClientPlayConnectionEvents.DISCONNECT.register((handler, client) -> {
            if (run != null) {
                requestStop("failed", "server_disconnected");
            }
            restoreAttention(run);
            restoreBuilderInventory(run);
            restoreBreakFromAbove(run);
            if (run != null) endRunOnDisconnect(run);
            ready = false;
            joinedTicks = 0;
            if (menuRead != null) finishMenuRead(client, "server_disconnected");
            confirmedResults.clear();
            emitLifecycle(client, "world_disconnected", state(client));
            lastSpawnMessage = null;
            lastUsedBlock = null;
        });
        ClientTickEvents.END_CLIENT_TICK.register(this::tick);
        ClientReceiveMessageEvents.GAME.register((message, overlay) -> {
            lastGameMessage = message.getString();
            lastGameMessageSequence++;
            if (message.getContents() instanceof TranslatableContents translatable) {
                noteSpawnMessage(Minecraft.getInstance(), translatable.getKey(), lastGameMessage);
            }
        });
        UseBlockCallback.EVENT.register((player, level, hand, hit) -> {
            if (level.isClientSide()) lastUsedBlock = hit.getBlockPos();
            return InteractionResult.PASS;
        });
        Thread.ofPlatform().daemon().name("minebot-spike-ipc").start(this::serve);
        LOG.info("MINEBOT_PHASE_A client_body_loaded minecraft=26.1.2 baritone=1.18.0+minebot.2");
    }

    private void serve() {
        try (ServerSocket server = new ServerSocket()) {
            int port = Integer.getInteger("minebot.client.ipc_port", 8768);
            server.bind(new InetSocketAddress(InetAddress.getByName("127.0.0.1"), port), 1);
            LOG.info("MINEBOT_PHASE_A ipc_listening port={}", server.getLocalPort());
            while (true) {
                try (Socket socket = server.accept()) {
                    socket.setTcpNoDelay(true);
                    Peer connection = new Peer(socket);
                    peer = connection;
                    if (ready) {
                        Minecraft.getInstance().execute(() -> {
                            JsonObject response = state(Minecraft.getInstance());
                            response.addProperty("type", "ready");
                            response.addProperty("mod_to_ready_ms", readyAtMs);
                            connection.send(response);
                            for (JsonObject event : lifecycleEvents) connection.send(event);
                        });
                    }
                    while (true) {
                        int length = connection.input.readInt();
                        if (length <= 0 || length > MAX_FRAME) {
                            throw new IOException("invalid frame length");
                        }
                        byte[] bytes = connection.input.readNBytes(length);
                        if (bytes.length != length) {
                            throw new EOFException();
                        }
                        JsonObject request = JsonParser.parseString(
                            new String(bytes, StandardCharsets.UTF_8)
                        ).getAsJsonObject();
                        Minecraft.getInstance().execute(() -> {
                            if (peer == connection) {
                                handle(Minecraft.getInstance(), request, connection);
                            }
                        });
                    }
                } catch (IOException | RuntimeException error) {
                    Peer lost = peer;
                    peer = null;
                    Minecraft.getInstance().execute(() -> {
                        if (run != null && run.peer == lost) {
                            requestStop("failed", "ipc_disconnected");
                        }
                    });
                }
            }
        } catch (IOException error) {
            LOG.error("MINEBOT_PHASE_A ipc_unavailable {}", error.toString());
        }
    }

    private void handle(Minecraft client, JsonObject request, Peer connection) {
        String type = string(request, "type", "");
        String id = string(request, "action_id", "");
        try {
            if (type.equals("state")) {
                JsonObject response = state(client);
                response.addProperty("type", "state");
                response.addProperty("request_id", string(request, "request_id", ""));
                connection.send(response);
            } else if (type.equals("scan")) {
                if (!ready) {
                    throw new IllegalArgumentException("client_not_ready");
                }
                String target = string(request, "target", "");
                var locations = BaritoneAPI.getProvider().getWorldScanner().scanChunkRadius(
                    baritone.getPlayerContext(), new BlockOptionalMetaLookup(target), 64, 10, 32
                );
                JsonObject response = state(client);
                response.addProperty("type", "scan");
                response.addProperty("request_id", string(request, "request_id", ""));
                JsonArray positions = new JsonArray();
                locations.forEach(pos -> {
                    JsonArray xyz = new JsonArray();
                    xyz.add(pos.getX());
                    xyz.add(pos.getY());
                    xyz.add(pos.getZ());
                    positions.add(xyz);
                });
                response.add("locations", positions);
                connection.send(response);
            } else if (type.equals("world_read")) {
                if (!ready) throw new IllegalArgumentException("client_not_ready");
                worldRead(client, request, connection);
            } else if (type.equals("container_read")) {
                if (!ready) throw new IllegalArgumentException("client_not_ready");
                if (run != null || menuRead != null) throw new IllegalArgumentException("body_busy");
                JsonObject params = request.getAsJsonObject("params");
                BlockPos pos = positionParam(params, "pos");
                menuRead = new MenuRead(string(request, "request_id", ""), connection, pos, menuContentRevision);
                try {
                    openMenu(client, pos);
                } catch (RuntimeException error) {
                    finishMenuRead(client, error.getMessage());
                }
            } else if (type.equals("settings")) {
                if (run != null) {
                    throw new IllegalArgumentException("body_busy");
                }
                if (request.has("explore_for_blocks")) {
                    BaritoneAPI.getSettings().exploreForBlocks.value = request.get("explore_for_blocks").getAsBoolean();
                }
                if (request.has("allow_break")) {
                    BaritoneAPI.getSettings().allowBreak.value = request.get("allow_break").getAsBoolean();
                }
                if (request.has("allow_place")) {
                    BaritoneAPI.getSettings().allowPlace.value = request.get("allow_place").getAsBoolean();
                }
                if (request.has("build_in_layers")) {
                    BaritoneAPI.getSettings().buildInLayers.value = request.get("build_in_layers").getAsBoolean();
                }
                if (request.has("replant_crops")) {
                    BaritoneAPI.getSettings().replantCrops.value = request.get("replant_crops").getAsBoolean();
                }
                if (request.has("farm_max_scan_size")) {
                    int size = request.get("farm_max_scan_size").getAsInt();
                    if (size < 1 || size > 256) throw new IllegalArgumentException("invalid_farm_max_scan_size");
                    BaritoneAPI.getSettings().farmMaxScanSize.value = size;
                }
                if (request.has("protected_positions")) {
                    JsonArray raw = request.get("protected_positions").isJsonArray()
                        ? request.getAsJsonArray("protected_positions") : null;
                    if (raw == null || raw.size() > 4096) throw new IllegalArgumentException("invalid_protected_positions");
                    Set<BlockPos> positions = new HashSet<>();
                    for (var entry : raw) {
                        if (!entry.isJsonArray() || entry.getAsJsonArray().size() != 3) {
                            throw new IllegalArgumentException("invalid_protected_position");
                        }
                        JsonArray xyz = entry.getAsJsonArray();
                        positions.add(new BlockPos(xyz.get(0).getAsInt(), xyz.get(1).getAsInt(), xyz.get(2).getAsInt()));
                    }
                    BaritoneAPI.getSettings().blocksToDisallowBreakingAt.clear();
                    BaritoneAPI.getSettings().blocksToDisallowBreakingAt.addAll(positions);
                }
                JsonObject response = state(client);
                response.addProperty("type", "settings");
                response.addProperty("request_id", string(request, "request_id", ""));
                connection.send(response);
            } else if (type.equals("builder_pause") || type.equals("builder_resume")) {
                if (type.equals("builder_pause")) baritone.getBuilderProcess().pause();
                else baritone.getBuilderProcess().resume();
                JsonObject response = state(client);
                response.addProperty("type", type);
                response.addProperty("request_id", string(request, "request_id", ""));
                connection.send(response);
            } else if (type.equals("close_screen")) {
                client.player.closeContainer();
                client.setScreen(null);
                JsonObject response = state(client);
                response.addProperty("type", type);
                response.addProperty("request_id", string(request, "request_id", ""));
                connection.send(response);
            } else if (type.equals("explore_filter")) {
                if (run != null) throw new IllegalArgumentException("body_busy");
                String filter = string(request, "path", "");
                if (filter.isBlank()) throw new IllegalArgumentException("missing_filter_path");
                try {
                    baritone.getExploreProcess().applyJsonFilter(
                        Path.of(filter), request.has("invert") && request.get("invert").getAsBoolean()
                    );
                } catch (Exception error) {
                    throw new IllegalArgumentException("explore_filter_failed", error);
                }
                JsonObject response = state(client);
                response.addProperty("type", type);
                response.addProperty("request_id", string(request, "request_id", ""));
                connection.send(response);
            } else if (type.equals("builder_open_schematic")) {
                if (run != null) throw new IllegalArgumentException("body_busy");
                if (!net.fabricmc.loader.api.FabricLoader.getInstance().isModLoaded("schematica")) {
                    throw new IllegalArgumentException("schematica_not_installed");
                }
                baritone.getBuilderProcess().buildOpenSchematic();
                JsonObject response = state(client);
                response.addProperty("type", type);
                response.addProperty("request_id", string(request, "request_id", ""));
                connection.send(response);
            } else if (type.equals("builder_open_litematic")) {
                if (run != null) throw new IllegalArgumentException("body_busy");
                int index = request.has("index") ? request.get("index").getAsInt() : 0;
                if (index < 0 || index > 1024) throw new IllegalArgumentException("invalid_litematic_index");
                if (!net.fabricmc.loader.api.FabricLoader.getInstance().isModLoaded("litematica")) {
                    throw new IllegalArgumentException("litematica_not_installed");
                }
                baritone.getBuilderProcess().buildOpenLitematic(index);
                JsonObject response = state(client);
                response.addProperty("type", type);
                response.addProperty("request_id", string(request, "request_id", ""));
                connection.send(response);
            } else if (type.equals("builder_file")) {
                if (run != null) throw new IllegalArgumentException("body_busy");
                String path = string(request, "path", "").trim();
                String name = string(request, "name", "").trim();
                if (path.isBlank() || name.isBlank()) throw new IllegalArgumentException("missing_builder_file");
                boolean accepted = baritone.getBuilderProcess().build(
                    name, Path.of(path).toFile(), blockPosition(request, "origin")
                );
                if (!accepted) throw new IllegalArgumentException("builder_file_rejected");
                JsonObject response = state(client);
                response.addProperty("type", type);
                response.addProperty("request_id", string(request, "request_id", ""));
                connection.send(response);
            } else if (type.equals("get_to_block_blacklist")) {
                if (!baritone.getGetToBlockProcess().isActive()) {
                    throw new IllegalArgumentException("get_to_block_not_active");
                }
                boolean blacklisted = baritone.getGetToBlockProcess().blacklistClosest();
                JsonObject response = state(client);
                response.addProperty("type", type);
                response.addProperty("request_id", string(request, "request_id", ""));
                response.addProperty("get_to_block_blacklist", blacklisted);
                connection.send(response);
            } else if (type.equals("builder_selection")) {
                builderSelection(client, request, connection);
            } else if (type.equals("elytra_repack")) {
                if (run != null) throw new IllegalArgumentException("body_busy");
                baritone.getElytraProcess().repackChunks();
                JsonObject response = state(client);
                response.addProperty("type", type);
                response.addProperty("request_id", string(request, "request_id", ""));
                connection.send(response);
            } else if (type.equals("elytra_reset")) {
                if (run != null) throw new IllegalArgumentException("body_busy");
                baritone.getElytraProcess().resetState();
                JsonObject response = state(client);
                response.addProperty("type", type);
                response.addProperty("request_id", string(request, "request_id", ""));
                connection.send(response);
            } else if (type.equals("baritone_command")) {
                if (run != null) throw new IllegalArgumentException("body_busy");
                String command = string(request, "command", "").trim();
                if (command.isBlank()) throw new IllegalArgumentException("missing_baritone_command");
                if (!baritone.getCommandManager().execute(command)) {
                    throw new IllegalArgumentException("baritone_command_rejected");
                }
                JsonObject response = state(client);
                response.addProperty("type", type);
                response.addProperty("request_id", string(request, "request_id", ""));
                connection.send(response);
            } else if (type.equals("primitive")) {
                if (!ready) throw new IllegalArgumentException("client_not_ready");
                primitive(client, request, connection);
            } else if (type.equals("cancel")) {
                if (run == null || !run.id.equals(id)) {
                    throw new IllegalArgumentException("action_not_active");
                }
                requestStop("canceled", "cancel_requested");
                JsonObject response = envelope("cancel_ack", id);
                response.addProperty("elapsed_ms", run.elapsed());
                connection.send(response);
            } else if (type.equals("command")) {
                if (!ready) {
                    throw new IllegalArgumentException("client_not_ready");
                }
                start(client, request, connection);
            } else {
                throw new IllegalArgumentException("unsupported_request");
            }
        } catch (IllegalArgumentException error) {
            JsonObject response = envelope("rejected", id);
            response.addProperty("request_id", string(request, "request_id", ""));
            response.addProperty("reason", error.getMessage());
            connection.send(response);
        } catch (RuntimeException error) {
            JsonObject response = envelope("rejected", id);
            response.addProperty("request_id", string(request, "request_id", ""));
            response.addProperty("reason", "request_failed");
            response.addProperty("detail", error.toString());
            connection.send(response);
        }
    }

    private void start(Minecraft client, JsonObject request, Peer connection) {
        String id = string(request, "action_id", "");
        if (id.isBlank()) {
            throw new IllegalArgumentException("missing_action_id");
        }
        if (request.has("action") && request.get("action").isJsonObject()) {
            JsonObject setting = request.getAsJsonObject("action");
            String settingName = string(setting, "name", "").trim();
            if (settingName.equals("setMovement") || settingName.equals("setGaze")) {
                // Settings, not actions: they never wait for the Body to be free.
                if (usedIds.contains(id)) throw new IllegalArgumentException("duplicate_action_id");
                JsonObject params = setting.has("params") && setting.get("params").isJsonObject()
                    ? setting.getAsJsonObject("params") : new JsonObject();
                String reason = settingName.equals("setGaze") ? setGaze(params) : setMovement(client, params);
                usedIds.add(id);
                connection.send(envelope("accepted", id));
                JsonObject response = state(client);
                response.addProperty("type", "terminal");
                response.addProperty("action_id", id);
                response.addProperty("status", "completed");
                response.addProperty("native_stopped", true);
                response.addProperty("reason", reason);
                response.addProperty("objective", "");
                response.addProperty("elapsed_ms", 0);
                connection.send(response);
                LOG.info("MINEBOT_BODY setting {} {}", settingName, params);
                return;
            }
        }
        if (run != null || menuRead != null) {
            throw new IllegalArgumentException("body_busy");
        }
        if (usedIds.contains(id)) {
            throw new IllegalArgumentException("duplicate_action_id");
        }
        String objective = string(request, "objective", "");
        String[] parts = objective.split(":", -1);
        long timeout = request.has("timeout_ms") ? request.get("timeout_ms").getAsLong() : 60000;
        if (timeout <= 0 || timeout > 300000) {
            throw new IllegalArgumentException("invalid_timeout");
        }
        Run candidate = new Run(id, objective, connection, timeout);
        candidate.lookDown = booleanValue(request, "look_down", false);
        if (request.has("action") && request.get("action").isJsonObject()) {
            JsonObject action = request.getAsJsonObject("action");
            String actionName = string(action, "name", "").trim();
            if (actionName.isBlank()) throw new IllegalArgumentException("missing_action_name");
            candidate.kind = actionName;
            candidate.params = action.has("params") && action.get("params").isJsonObject()
                ? action.getAsJsonObject("params") : new JsonObject();
            candidate.structured = true;
            candidate.lookDown = candidate.lookDown || booleanValue(candidate.params, "look_down", false);
            candidate.item = string(candidate.params, "item", "").replace("minecraft:", "");
            if (actionName.equals("craftItem")) {
                JsonObject output = candidate.params.has("output") && candidate.params.get("output").isJsonObject()
                    ? candidate.params.getAsJsonObject("output") : new JsonObject();
                candidate.item = string(output, "item", "").replace("minecraft:", "");
            }
            candidate.before = count(client, candidate.item);
            usedIds.add(id);
            if (legs != null && !legsPaused && !isHandRun(candidate)) pauseLegs();
            run = candidate;
            beginAttention(candidate);
            emit(candidate, envelope("accepted", id));
            LOG.info("MINEBOT_BODY action_started id={} action={}", id, actionName);
            return;
        }
        candidate.kind = parts[0];
        if (parts[0].equals("any") || parts[0].equals("away") || parts[0].equals("y") || parts[0].equals("block")) {
            candidate.goal = parseGoalObjective(objective);
        } else if (parts.length == 4 && (parts[0].equals("goto") || parts[0].equals("navigate"))) {
            try {
                candidate.goal = new GoalBlock(
                    Integer.parseInt(parts[1]), Integer.parseInt(parts[2]), Integer.parseInt(parts[3])
                );
            } catch (NumberFormatException error) {
                throw new IllegalArgumentException("invalid_coordinates");
            }
        } else if ((parts.length == 2 || parts.length == 3 || parts.length == 4) && parts[0].equals("mine") && !parts[1].isBlank()) {
            if (parts.length == 4) {
                candidate.mineQuantity = Integer.parseInt(parts[1]);
                if (candidate.mineQuantity < 1 || candidate.mineQuantity > 4096) {
                    throw new IllegalArgumentException("invalid_mine_quantity");
                }
                candidate.targets = parts[2].split(",", -1);
                if (candidate.targets.length == 0) throw new IllegalArgumentException("missing_mine_targets");
                candidate.target = candidate.targets[0];
                candidate.item = parts[3];
            } else {
                candidate.target = parts[1];
                candidate.targets = new String[] {parts[1]};
                candidate.item = parts.length == 3 ? parts[2] : parts[1];
            }
        } else if (parts.length == 3 && parts[0].equals("xz")) {
            candidate.goal = new GoalXZ(Integer.parseInt(parts[1]), Integer.parseInt(parts[2]));
        } else if (parts.length == 5 && parts[0].equals("near")) {
            candidate.goal = new GoalNear(position(parts, 1), Integer.parseInt(parts[4]));
        } else if (parts.length == 2 && parts[0].equals("get")) {
            candidate.target = parts[1];
        } else if (parts.length == 3 && parts[0].equals("explore")) {
            candidate.anchor = new BlockPos(Integer.parseInt(parts[1]), 0, Integer.parseInt(parts[2]));
        } else if ((parts.length == 2 || ((parts.length == 3 || parts.length == 4) && parts[0].equals("follow"))) && (parts[0].equals("follow") || parts[0].equals("pickup"))) {
            candidate.target = parts[1];
            if (parts.length >= 3) candidate.range = Integer.parseInt(parts[2]);
            candidate.followKeepDistance = parts.length == 4 ? Double.parseDouble(parts[3]) : Math.max(3.0, candidate.range);
            if (!Double.isFinite(candidate.followKeepDistance) || candidate.followKeepDistance < 0) {
                throw new IllegalArgumentException("invalid_follow_distance");
            }
            if (parts[0].equals("pickup")) candidate.item = parts[1];
        } else if (parts.length == 5 && parts[0].equals("farm")) {
            candidate.range = Integer.parseInt(parts[1]);
            candidate.anchor = position(parts, 2);
        } else if (parts.length == 8 && parts[0].equals("build")) {
            candidate.target = parts[1];
            candidate.anchor = position(parts, 2);
            candidate.sx = Integer.parseInt(parts[5]);
            candidate.sy = Integer.parseInt(parts[6]);
            candidate.sz = Integer.parseInt(parts[7]);
            if (candidate.sx < 1 || candidate.sy < 1 || candidate.sz < 1 || candidate.sx * candidate.sy * candidate.sz > 256) {
                throw new IllegalArgumentException("invalid_fixture_size");
            }
        } else if (parts.length == 7 && parts[0].equals("clear")) {
            candidate.anchor = position(parts, 1);
            candidate.end = position(parts, 4);
        } else if (parts.length == 4 && parts[0].equals("elytra")) {
            candidate.anchor = position(parts, 1);
        } else {
            throw new IllegalArgumentException("unsupported_objective");
        }
        // A walk to a portal block's own cell (goto, or near with radius 0):
        // Baritone never paths into a portal block (end_portal is not passable
        // to it), so the Body walks to a cell beside it and steps in, as a
        // player does. End run-12: move_to the overworld end_portal cell was
        // refused and getting in took three minutes (dragon-unit-tests-20261030 §6.y.21).
        boolean exactCell = parts[0].equals("goto") || parts[0].equals("navigate")
            || (parts[0].equals("near") && parts.length == 5 && Integer.parseInt(parts[4]) == 0);
        if (exactCell && client.level != null && candidate.goal instanceof baritone.api.utils.interfaces.IGoalRenderPos at
            && isPortal(client.level.getBlockState(at.getGoalPos()))) {
            candidate.portalCell = at.getGoalPos().immutable();
            candidate.portalDimension = client.level.dimension();
            List<Goal> beside = besidePortal(client, candidate.portalCell);
            candidate.goal = beside.isEmpty() ? null : new GoalComposite(beside.toArray(Goal[]::new));
        }
        candidate.before = count(client, candidate.item);
        usedIds.add(id);
        if (legs != null && !legsPaused) pauseLegs();
        run = candidate;
        beginAttention(candidate);
        try {
            if (candidate.portalCell != null && candidate.goal == null) requestStop("failed", "no_standable_cell_beside_portal");
            else if (candidate.goal != null) baritone.getCustomGoalProcess().setGoalAndPath(candidate.goal);
            else switch (candidate.kind) {
                case "mine" -> baritone.getMineProcess().mineByName(candidate.before + candidate.mineQuantity, candidate.targets);
                case "get" -> baritone.getGetToBlockProcess().getToBlock(new BlockOptionalMeta(candidate.target));
                case "explore" -> baritone.getExploreProcess().explore(candidate.anchor.getX(), candidate.anchor.getZ());
                case "follow" -> {
                    candidate.previousFollowRadius = BaritoneAPI.getSettings().followRadius.value;
                    if (parts.length >= 3) BaritoneAPI.getSettings().followRadius.value = Math.max(0, candidate.range);
                    // Resolve once in tickFollow; navigation and attention use
                    // the same entity, including type-based requests.
                    baritone.getFollowProcess().follow(entity -> entity == candidate.targetEntity);
                }
                case "pickup" -> baritone.getFollowProcess().pickup(stack -> BuiltInRegistries.ITEM.getKey(stack.getItem()).getPath().equals(candidate.target));
                case "farm" -> baritone.getFarmProcess().farm(candidate.range, candidate.anchor);
                case "build" -> {
                    enableBuilderInventory(candidate);
                    enableBreakFromAbove(candidate);
                    baritone.getBuilderProcess().build("minebot-owned-fixture", new FillSchematic(candidate.sx, candidate.sy, candidate.sz, new BlockOptionalMeta(candidate.target)), candidate.anchor);
                }
                case "clear" -> {
                    enableBreakFromAbove(candidate);
                    baritone.getBuilderProcess().clearArea(candidate.anchor, candidate.end);
                }
                case "elytra" -> {
                    if (!baritone.getElytraProcess().isLoaded()) throw new IllegalArgumentException("elytra_process_not_loaded");
                    baritone.getElytraProcess().pathTo(candidate.anchor);
                }
                default -> throw new IllegalArgumentException("unsupported_objective");
            }
        } catch (RuntimeException error) {
            requestStop("failed", "process_start_failed:" + error.getMessage());
        }
        emit(candidate, envelope("accepted", id));
        LOG.info("MINEBOT_PHASE_A objective_started id={} objective={}", id, objective);
    }

    private void primitive(Minecraft client, JsonObject request, Peer connection) {
        String action = string(request, "action_name", "");
        JsonObject params = request.has("params") && request.get("params").isJsonObject()
            ? request.getAsJsonObject("params") : new JsonObject();
        if (client.player == null || client.gameMode == null) {
            throw new IllegalArgumentException("player_not_ready");
        }
        switch (action) {
            case "jump" -> {
                if (!client.player.onGround()) throw new IllegalArgumentException("jump_not_grounded");
                client.player.jumpFromGround();
            }
            case "stop" -> {
                baritone.getPathingBehavior().cancelEverything();
                baritone.getInputOverrideHandler().clearAllKeys();
            }
            case "selectItem" -> selectItem(client, string(params, "item", ""));
            case "selectSlot" -> selectSlot(client, params);
            case "lookAt" -> lookAt(client, params);
            case "useItem" -> useItem(client, params);
            case "attackEntity" -> attackEntity(client, params);
            case "dropItem" -> dropItem(client, params);
            case "moveItem" -> moveItem(client, params);
            case "mineBlock", "collectBlock" -> mineBlock(client, params);
            case "placeBlock" -> placeBlock(client, params);
            case "igniteBlock", "sowCrop" -> useOnBlock(client, action, params);
            default -> throw new IllegalArgumentException("unsupported_primitive_action");
        }
        JsonObject response = state(client);
        response.addProperty("type", "primitive");
        response.addProperty("request_id", string(request, "request_id", ""));
        response.addProperty("success", true);
        response.addProperty("reason", "action_applied");
        connection.send(response);
    }

    private void builderSelection(Minecraft client, JsonObject request, Peer connection) {
        String operation = string(request, "operation", "");
        var manager = baritone.getSelectionManager();
        ISelection result = null;
        switch (operation) {
            case "add" -> result = manager.addSelection(
                new BetterBlockPos(blockPosition(request, "pos1")),
                new BetterBlockPos(blockPosition(request, "pos2"))
            );
            case "clear" -> manager.removeAllSelections();
            case "expand", "contract", "shift" -> {
                ISelection selected = manager.getOnlySelection();
                if (selected == null) throw new IllegalArgumentException("builder_selection_missing");
                Direction direction = selectionDirection(request);
                int amount = request.has("amount") ? request.get("amount").getAsInt() : 1;
                if (amount < 1 || amount > 256) throw new IllegalArgumentException("invalid_selection_amount");
                result = switch (operation) {
                    case "expand" -> manager.expand(selected, direction, amount);
                    case "contract" -> manager.contract(selected, direction, amount);
                    default -> manager.shift(selected, direction, amount);
                };
            }
            default -> throw new IllegalArgumentException("unsupported_builder_selection_operation");
        }
        JsonObject response = state(client);
        response.addProperty("type", "builder_selection");
        response.addProperty("request_id", string(request, "request_id", ""));
        response.add("builder_selections", selectionFacts(manager));
        if (result != null) response.add("selection", selectionFact(result));
        connection.send(response);
    }

    private static Direction selectionDirection(JsonObject request) {
        String raw = string(request, "direction", "").trim().toUpperCase(Locale.ROOT);
        try {
            return Direction.valueOf(raw);
        } catch (IllegalArgumentException error) {
            throw new IllegalArgumentException("invalid_selection_direction", error);
        }
    }

    private static JsonArray selectionFacts(baritone.api.selection.ISelectionManager manager) {
        JsonArray selections = new JsonArray();
        for (ISelection selection : manager.getSelections()) selections.add(selectionFact(selection));
        return selections;
    }

    private static JsonObject selectionFact(ISelection selection) {
        JsonObject fact = new JsonObject();
        JsonArray pos1 = new JsonArray();
        pos1.add(selection.pos1().getX());
        pos1.add(selection.pos1().getY());
        pos1.add(selection.pos1().getZ());
        JsonArray pos2 = new JsonArray();
        pos2.add(selection.pos2().getX());
        pos2.add(selection.pos2().getY());
        pos2.add(selection.pos2().getZ());
        fact.add("pos1", pos1);
        fact.add("pos2", pos2);
        return fact;
    }

    private void worldRead(Minecraft client, JsonObject request, Peer connection) {
        String scope = string(request, "scope", "");
        JsonObject params = request.has("params") && request.get("params").isJsonObject()
            ? request.getAsJsonObject("params") : new JsonObject();
        JsonObject response = envelope("world_read", string(request, "request_id", ""));
        response.addProperty("request_id", string(request, "request_id", ""));
        JsonObject data = new JsonObject();
        if (scope.equals("environment")) {
            data = environment(client, params);
        } else if (scope.equals("recipeData")) {
            data = recipeData(client, params);
        } else if (scope.equals("blockAt")) {
            BlockPos pos = new BlockPos(params.get("x").getAsInt(), params.get("y").getAsInt(), params.get("z").getAsInt());
            JsonObject fact = blockFact(client.level.getBlockState(pos), pos);
            for (var entry : fact.entrySet()) data.add(entry.getKey(), entry.getValue());
            double reach = params.has("interaction_radius") ? params.get("interaction_radius").getAsDouble() : 4.5;
            if (!Double.isFinite(reach) || reach <= 0) throw new IllegalArgumentException("invalid_interaction_radius");
            data.addProperty("interaction_ready", RotationUtils.reachable(baritone.getPlayerContext(), pos, Math.min(reach, 4.5)).isPresent());
        } else if (scope.equals("blockCells")) {
            JsonArray cells = new JsonArray();
            boolean loaded = true;
            JsonArray raw = params.has("cells") && params.get("cells").isJsonArray() ? params.getAsJsonArray("cells") : new JsonArray();
            for (var item : raw) {
                JsonArray xyz = item.getAsJsonArray();
                BlockPos pos = new BlockPos(xyz.get(0).getAsInt(), xyz.get(1).getAsInt(), xyz.get(2).getAsInt());
                if (clientChunkLoaded(client, pos)) {
                    cells.add(blockFact(client.level.getBlockState(pos), pos));
                } else {
                    loaded = false;
                    JsonObject unknown = new JsonObject();
                    unknown.addProperty("x", pos.getX());
                    unknown.addProperty("y", pos.getY());
                    unknown.addProperty("z", pos.getZ());
                    unknown.addProperty("state", "UNKNOWN");
                    unknown.addProperty("observed", false);
                    unknown.addProperty("reason", "chunk_not_loaded");
                    cells.add(unknown);
                }
            }
            data.add("cells", cells);
            data.addProperty("count", cells.size());
            response.addProperty("complete", loaded);
        } else if (scope.equals("nearbyBlocks") || scope.equals("findBlocks")) {
            java.util.Set<String> targets = new java.util.HashSet<>();
            if (params.has("block_types")) for (var target : params.getAsJsonArray("block_types")) {
                targets.add(target.getAsString().replace("minecraft:", ""));
            }
            int radius = Math.min(64, Math.max(1, params.has("radius") ? params.get("radius").getAsInt() : 16));
            int yRadius = Math.min(64, Math.max(1, params.has("y_radius") ? params.get("y_radius").getAsInt() : radius));
            int limit = Math.min(256, Math.max(1, params.has("limit") ? params.get("limit").getAsInt() : 32));
            int start = Math.max(0, params.has("start") ? params.get("start").getAsInt() : 0);
            BlockPos center = client.player.blockPosition();
            java.util.List<BlockPos> matches = new java.util.ArrayList<>();
            boolean loaded = true;
            for (int x = center.getX() - radius; x <= center.getX() + radius; x++) {
                for (int z = center.getZ() - radius; z <= center.getZ() + radius; z++) {
                    if (client.level.getChunkSource().getChunk(x >> 4, z >> 4,
                        net.minecraft.world.level.chunk.status.ChunkStatus.FULL, false) == null) { loaded = false; continue; }
                    for (int y = Math.max(client.level.getMinY(), center.getY() - yRadius);
                         y <= Math.min(client.level.getMaxY() - 1, center.getY() + yRadius); y++) {
                        BlockPos pos = new BlockPos(x, y, z);
                        BlockState block = client.level.getBlockState(pos);
                        if (!block.isAir() && (targets.isEmpty() || targets.contains(BuiltInRegistries.BLOCK.getKey(block.getBlock()).getPath()))) {
                            matches.add(pos);
                        }
                    }
                }
            }
            matches.sort(java.util.Comparator.comparingDouble((BlockPos pos) -> pos.distSqr(center))
                .thenComparingInt(BlockPos::getX).thenComparingInt(BlockPos::getY).thenComparingInt(BlockPos::getZ));
            JsonArray blocks = new JsonArray();
            for (int i = start; i < Math.min(matches.size(), start + limit); i++) {
                BlockPos pos = matches.get(i);
                blocks.add(blockFact(client.level.getBlockState(pos), pos));
            }
            boolean more = start + limit < matches.size();
            data.add("blocks", blocks);
            data.add("matches", blocks);
            data.addProperty("count", blocks.size());
            data.addProperty("totalMatches", matches.size());
            data.addProperty("coverage_complete", loaded);
            if (!loaded) {
                JsonArray uncertainty = new JsonArray();
                JsonObject boundary = new JsonObject();
                boundary.addProperty("reason", "unloaded_boundary");
                uncertainty.add(boundary);
                response.add("uncertainty", uncertainty);
            }
            if (more) data.addProperty("nextStart", start + limit);
            response.addProperty("complete", loaded && !more);
        } else {
            throw new IllegalArgumentException("unsupported_world_read_scope");
        }
        response.addProperty("ok", true);
        if (!response.has("complete")) response.addProperty("complete", true);
        response.add("data", data);
        if (!response.has("uncertainty")) response.add("uncertainty", new JsonArray());
        connection.send(response);
    }

    /** Loaded client terrain only: no chunk loading, world seed or server-wide lookup. */
    private static JsonObject environment(Minecraft client, JsonObject params) {
        JsonObject data = new JsonObject();
        BlockPos here = client.player.blockPosition();
        int surface = client.level.getHeight(
            net.minecraft.world.level.levelgen.Heightmap.Types.MOTION_BLOCKING_NO_LEAVES,
            here.getX(), here.getZ());
        data.add("pos", xyz(here));
        data.addProperty("dimension", client.level.dimension().identifier().toString());
        data.addProperty("difficulty", client.level.getDifficulty().name().toLowerCase(java.util.Locale.ROOT));
        data.addProperty("time", Math.floorMod(client.level.getOverworldClockTime(), 24000));
        data.addProperty("weather", client.level.isThundering() ? "thunder" : client.level.isRaining() ? "rain" : "clear");
        data.addProperty("can_see_sky", client.level.canSeeSky(here));
        data.addProperty("surface_y", surface);
        data.addProperty("depth_below_surface", Math.max(0, surface - here.getY()));
        data.addProperty("space", client.player.isUnderWater() ? "underwater"
            : surface - here.getY() > 4 ? "underground" : "surface");
        data.addProperty("biome", client.level.getBiome(here).unwrapKey()
            .map(key -> key.identifier().toString()).orElse("unknown"));
        JsonArray columns = new JsonArray();
        if (params.has("columns")) {
            columns = params.getAsJsonArray("columns");
            if (columns.size() > 128) throw new IllegalArgumentException("too_many_columns");
        } else {
            int radius = Math.max(16, Math.min(96, params.has("radius") ? params.get("radius").getAsInt() : 48));
            for (int dx = -radius; dx <= radius; dx += 16) {
                for (int dz = -radius; dz <= radius; dz += 16) {
                    JsonArray xz = new JsonArray();
                    xz.add(here.getX() + dx); xz.add(here.getZ() + dz); columns.add(xz);
                }
            }
        }
        JsonArray samples = new JsonArray();
        for (var column : columns) {
            JsonArray xz = column.getAsJsonArray();
            if (xz.size() != 2) throw new IllegalArgumentException("invalid_column");
            int x = xz.get(0).getAsInt(), z = xz.get(1).getAsInt();
            JsonObject sample = new JsonObject();
            sample.add("xz", xz.deepCopy());
            boolean loaded = client.level.getChunkSource().getChunk(x >> 4, z >> 4,
                net.minecraft.world.level.chunk.status.ChunkStatus.FULL, false) != null;
            sample.addProperty("loaded", loaded);
            if (loaded) {
                int y = client.level.getHeight(
                    net.minecraft.world.level.levelgen.Heightmap.Types.MOTION_BLOCKING_NO_LEAVES, x, z);
                BlockPos feet = new BlockPos(x, y, z);
                BlockState ground = client.level.getBlockState(feet.below());
                boolean dry = ground.getFluidState().isEmpty();
                sample.add("surface_pos", xyz(feet));
                sample.addProperty("biome", client.level.getBiome(feet).unwrapKey()
                    .map(key -> key.identifier().toString()).orElse("unknown"));
                sample.addProperty("surface_block", BuiltInRegistries.BLOCK.getKey(ground.getBlock()).toString());
                sample.addProperty("water", !dry);
                sample.addProperty("canopy_height", Math.max(0, client.level.getHeight(
                    net.minecraft.world.level.levelgen.Heightmap.Types.WORLD_SURFACE, x, z) - y));
                sample.addProperty("standable", dry && !ground.getCollisionShape(client.level, feet.below()).isEmpty()
                    && client.level.getBlockState(feet).getCollisionShape(client.level, feet).isEmpty()
                    && client.level.getBlockState(feet.above()).getCollisionShape(client.level, feet.above()).isEmpty());
            }
            samples.add(sample);
        }
        data.add("samples", samples);
        data.addProperty("source", "loaded_client_heightmap_and_blocks");
        data.addProperty("sampling", "point samples; gaps are unknown; standable does not prove reachable");
        return data;
    }

    /**
     * A mob that will attack the player. Zombified piglins and endermen (and
     * piglins, which a gold-wearing player does not anger) stay calm until
     * provoked; said hostile while calm, they were offered as targets to
     * fight back against, and one hit angers a whole zombified piglin group.
     * Aggression is what the client observes: the mob's aggressive flag, or
     * an enderman's scream.
     */
    private static final Set<String> BOSSES = Set.of("ender_dragon", "wither", "elder_guardian", "warden");

    /** How a creature moves and where it faces, the same for every creature
     *  (dragon-unit-tests-20261030 §5.2). The mob's AI target is not sent to
     *  clients; facing and approaching are what a player could see. */
    private static void creatureMotion(Minecraft client, Entity entity, JsonObject observed) {
        Vec3 eye = client.player.getEyePosition();
        double vx = (entity.getX() - entity.xo) * 20.0, vy = (entity.getY() - entity.yo) * 20.0, vz = (entity.getZ() - entity.zo) * 20.0;
        JsonArray velocity = new JsonArray();
        velocity.add(Math.round(vx * 100.0) / 100.0);
        velocity.add(Math.round(vy * 100.0) / 100.0);
        velocity.add(Math.round(vz * 100.0) / 100.0);
        observed.add("velocity", velocity);
        double nearest = entity.getBoundingBox().distanceToSqr(eye);
        Vec3 front = entity.getViewVector(1.0F);
        if (entity instanceof net.minecraft.world.entity.boss.enderdragon.EnderDragon dragon) {
            // A dragon is hit and hits with its parts; it faces where its head is.
            for (var part : dragon.getSubEntities()) nearest = Math.min(nearest, part.getBoundingBox().distanceToSqr(eye));
            Vec3 toHead = dragon.head.position().subtract(dragon.position());
            if (toHead.lengthSqr() > 1e-6) front = toHead.normalize();
        }
        observed.addProperty("nearest_dist", Math.round(Math.sqrt(nearest) * 100.0) / 100.0);
        // Whether blocks stand between the Body's eyes and the creature's, as a
        // player sees it: Decision sent a sealed-in Body out through its own
        // walls after a skeleton three blocks away behind them (easy run-12).
        observed.addProperty("in_sight", hasLineOfSight(client, entity));
        Vec3 toBody = eye.subtract(entity.getEyePosition());
        double length = toBody.length();
        if (length > 1e-3) {
            Vec3 dir = toBody.scale(1.0 / length);
            observed.addProperty("facing_body", front.dot(dir) > 0.866);
            observed.addProperty("approaching", vx * dir.x + vy * dir.y + vz * dir.z > 0.5);
        }
        observed.addProperty("boss", BOSSES.contains(BuiltInRegistries.ENTITY_TYPE.getKey(entity.getType()).getPath()));
        if (entity instanceof Mob mob) observed.addProperty("aggressive", mob.isAggressive());
    }

    private static boolean hostile(Entity entity) {
        if (!(entity instanceof Enemy)) return false;
        if (entity instanceof EnderMan enderman) return enderman.isCreepy() || enderman.isAggressive();
        if ((entity instanceof NeutralMob || entity instanceof Piglin) && entity instanceof Mob mob) return mob.isAggressive();
        return true;
    }

    /** Keeps "Respawn point set" with the bed or anchor that set it, or "no home bed". */
    private static void noteSpawnMessage(Minecraft client, String key, String text) {
        if (!SpawnMessage.about(key)) return;
        JsonObject fact = new JsonObject();
        fact.addProperty("seq", ++spawnMessageSequence);
        fact.addProperty("key", key);
        fact.addProperty("text", text);
        fact.addProperty("at_ms", System.currentTimeMillis());
        if (client.level != null) fact.addProperty("dimension", client.level.dimension().identifier().toString());
        if (SpawnMessage.SET.equals(key) && client.player != null && client.level != null) {
            // Sleeping sets it to the bed slept in; a bed used by day or an anchor, to the block used.
            BlockPos at = client.player.getSleepingPos().orElse(lastUsedBlock);
            if (at != null) {
                String block = BuiltInRegistries.BLOCK.getKey(client.level.getBlockState(at).getBlock()).getPath();
                if (SpawnMessage.respawnBlock(block)) {
                    fact.add("pos", xyz(at));
                    fact.addProperty("block", block);
                }
            }
        }
        lastSpawnMessage = fact;
    }

    private static JsonArray xyz(BlockPos pos) {
        JsonArray result = new JsonArray();
        result.add(pos.getX()); result.add(pos.getY()); result.add(pos.getZ());
        return result;
    }

    private static JsonArray xyz(Vec3 pos) {
        JsonArray result = new JsonArray();
        result.add(pos.x); result.add(pos.y); result.add(pos.z);
        return result;
    }

    /**
     * Lava and fire as the player observes them. In lava, also the nearest cell
     * a player would step out onto: air (no lava, no fire) for feet and head
     * over a block with a solid top. Burning on the ground, the block it stands
     * on: water poured onto it reaches the player. Without these facts the
     * reflex frames of a Body burning in lava said only that health fell, and
     * offered nothing but staying (eval-20261020-m1 run-2, 18:33).
     */
    private static void hazardFacts(Minecraft client, JsonObject into) {
        boolean inLava = client.player.isInLava();
        boolean onFire = client.player.isOnFire();
        into.addProperty("in_lava", inLava);
        into.addProperty("on_fire", onFire);
        // Always said: a progress frame without them would leave earlier
        // cells standing in the merged execution facts.
        into.add("lava_exit", com.google.gson.JsonNull.INSTANCE);
        into.add("cloud", com.google.gson.JsonNull.INSTANCE);
        into.add("hurt_here", com.google.gson.JsonNull.INSTANCE);
        MineBotClientBody body = instance;
        JsonObject hurt = body == null ? null : body.hurtHere(client);
        if (hurt != null) into.add("hurt_here", hurt);
        // Not the cell under the player's centre: on a bank's edge that was
        // the lava, and the poured water turned it to obsidian while the
        // player burned on (client GameTest 2026-10-20).
        into.add("standing_on", onFire && !inLava && client.player.onGround()
            ? xyz(client.player.getOnPos()) : com.google.gson.JsonNull.INSTANCE);
        AreaEffectCloud cloud = nearestCloud(client);
        if (cloud != null) into.add("cloud", cloudFact(client, cloud));
        into.add("steps", stepFacts(client));
        if (!inLava) return;
        BlockPos best = nearestLavaExit(client);
        if (best != null) into.add("lava_exit", xyz(best));
    }

    private static AreaEffectCloud nearestCloud(Minecraft client) {
        AreaEffectCloud best = null;
        double bestDistance = Double.MAX_VALUE;
        for (Entity entity : client.level.entitiesForRendering()) {
            if (!(entity instanceof AreaEffectCloud cloud)) continue;
            double dx = cloud.getX() - client.player.getX();
            double dz = cloud.getZ() - client.player.getZ();
            double horizontal = Math.sqrt(dx * dx + dz * dz);
            if (horizontal > cloud.getRadius() + 4.0 || Math.abs(cloud.getY() - client.player.getY()) > 3.0) continue;
            if (horizontal < bestDistance) {
                best = cloud;
                bestDistance = horizontal;
            }
        }
        return best;
    }

    private static JsonObject cloudFact(Minecraft client, AreaEffectCloud cloud) {
        JsonObject fact = new JsonObject();
        JsonArray pos = new JsonArray();
        pos.add(round2(cloud.getX()));
        pos.add(round2(cloud.getY()));
        pos.add(round2(cloud.getZ()));
        fact.add("pos", pos);
        fact.addProperty("radius", round2(cloud.getRadius()));
        fact.addProperty("particle", BuiltInRegistries.PARTICLE_TYPE.getKey(cloud.getParticle().getType()).getPath());
        fact.addProperty("inside", cloud.getBoundingBox().intersects(client.player.getBoundingBox()));
        return fact;
    }

    private static double round2(double value) {
        return Math.round(value * 100.0) / 100.0;
    }

    /**
     * Ore blocks within 8 blocks that have a face open to air or water, nearest
     * first, at most four: the ores a player sees on the way. Said in the full
     * state only (once a second while work runs), always as a list, so an ore
     * mined meanwhile does not stay in the merged facts (mechanism-map §11.6, D5).
     */
    private static JsonArray exposedOres(Minecraft client) {
        BlockPos origin = client.player.blockPosition();
        net.minecraft.world.phys.Vec3 eye = client.player.getEyePosition();
        java.util.List<BlockPos> found = new java.util.ArrayList<>();
        BlockPos.MutableBlockPos at = new BlockPos.MutableBlockPos();
        for (int dx = -8; dx <= 8; dx++) for (int dy = -8; dy <= 8; dy++) for (int dz = -8; dz <= 8; dz++) {
            at.set(origin.getX() + dx, origin.getY() + dy, origin.getZ() + dz);
            if (!isOre(client.level.getBlockState(at))) continue;
            BlockPos ore = at.immutable();
            for (Direction side : Direction.values()) {
                net.minecraft.world.level.block.state.BlockState next = client.level.getBlockState(ore.relative(side));
                if (next.isAir() || next.getFluidState().is(FluidTags.WATER)) {
                    found.add(ore);
                    break;
                }
            }
        }
        found.sort(java.util.Comparator.comparingDouble(pos -> pos.getCenter().distanceToSqr(eye)));
        JsonArray ores = new JsonArray();
        for (BlockPos pos : found.subList(0, Math.min(4, found.size()))) {
            JsonObject ore = new JsonObject();
            ore.addProperty("block", BuiltInRegistries.BLOCK.getKey(client.level.getBlockState(pos).getBlock()).getPath());
            ore.add("pos", xyz(pos));
            ore.addProperty("dist", Math.round(Math.sqrt(pos.getCenter().distanceToSqr(eye)) * 10.0) / 10.0);
            ores.add(ore);
        }
        return ores;
    }

    private static boolean isOre(net.minecraft.world.level.block.state.BlockState state) {
        String id = BuiltInRegistries.BLOCK.getKey(state.getBlock()).getPath();
        return id.endsWith("_ore") || id.equals("ancient_debris");
    }

    /** The nearest cell, from where the player is now, to stand on dry and clear. */
    private static BlockPos nearestLavaExit(Minecraft client) {
        BlockPos origin = client.player.blockPosition();
        BlockPos best = null;
        double bestDistance = Double.MAX_VALUE;
        for (int dx = -LAVA_EXIT_REACH; dx <= LAVA_EXIT_REACH; dx++) {
            for (int dz = -LAVA_EXIT_REACH; dz <= LAVA_EXIT_REACH; dz++) {
                for (int dy = -1; dy <= 1; dy++) {
                    BlockPos feet = origin.offset(dx, dy, dz);
                    if (!clearOfHazard(client, feet) || !clearOfHazard(client, feet.above())) continue;
                    BlockPos below = feet.below();
                    BlockState ground = client.level.getBlockState(below);
                    if (!ground.getFluidState().isEmpty() || ground.is(net.minecraft.world.level.block.Blocks.MAGMA_BLOCK)
                        || !ground.isFaceSturdy(client.level, below, Direction.UP)) continue;
                    double distance = client.player.position().distanceToSqr(feet.getX() + 0.5, feet.getY(), feet.getZ() + 0.5);
                    if (distance < bestDistance) {
                        best = feet;
                        bestDistance = distance;
                    }
                }
            }
        }
        return best;
    }

    private static final int LAVA_EXIT_REACH = 3;

    // Eight directions a player can step in, +x east and +z south.
    private static final String[] STEP_NAMES = {"north", "northeast", "east", "southeast", "south", "southwest", "west", "northwest"};
    private static final int[][] STEP_DIRS = {{0, -1}, {1, -1}, {1, 0}, {1, 1}, {0, 1}, {-1, 1}, {-1, 0}, {-1, -1}};
    private static final int STEP_REACH = 4;

    /**
     * Where a player could step to now: in each of eight directions, walking
     * straight cell by cell up to STEP_REACH, the farthest cell it stands on.
     * Each cell holds the feet and head, clear of fluid and fire, with ground
     * under it one block up, level, or up to three down; the walk stops at a
     * wall or where no ground is that near (the void). Directions that reach
     * fewer than two cells are left out. The End baseline Body stood still
     * while the dragon came on, and its only way off was a walk the
     * pathfinder planned (dragon-unit-tests-20261030 §6, B).
     */
    private static JsonArray stepFacts(Minecraft client) {
        JsonArray steps = new JsonArray();
        BlockPos origin = client.player.blockPosition();
        for (int d = 0; d < STEP_DIRS.length; d++) {
            BlockPos at = origin;
            int walked = 0;
            for (int i = 1; i <= STEP_REACH; i++) {
                BlockPos next = stepCell(client, at, STEP_DIRS[d][0], STEP_DIRS[d][1]);
                if (next == null) break;
                at = next;
                walked = i;
            }
            if (walked < 2) continue;
            JsonObject step = new JsonObject();
            step.addProperty("direction", STEP_NAMES[d]);
            step.add("to", xyz(at));
            step.addProperty("cells", walked);
            steps.add(step);
        }
        return steps;
    }

    private static BlockPos stepCell(Minecraft client, BlockPos from, int dx, int dz) {
        // Diagonally the player's body passes one of the two side cells.
        if (dx != 0 && dz != 0) {
            BlockPos side = from.offset(dx, 0, 0), other = from.offset(0, 0, dz);
            if (!(clearOfHazard(client, side) && clearOfHazard(client, side.above()))
                && !(clearOfHazard(client, other) && clearOfHazard(client, other.above()))) return null;
        }
        BlockPos level = from.offset(dx, 0, dz);
        if (!clearOfHazard(client, level.above())) return null;
        if (!clearOfHazard(client, level)) {
            // One block up, jumped onto: room above the head on both cells.
            BlockPos up = level.above();
            return clearOfHazard(client, up.above()) && clearOfHazard(client, from.above(2)) && groundUnder(client, up)
                ? up : null;
        }
        for (int drop = 0; drop <= 3; drop++) {
            BlockPos feet = level.below(drop);
            if (drop > 0 && !clearOfHazard(client, feet)) return null;
            if (groundUnder(client, feet)) return feet;
        }
        return null;
    }

    private static boolean groundUnder(Minecraft client, BlockPos feet) {
        BlockPos below = feet.below();
        BlockState ground = client.level.getBlockState(below);
        return ground.getFluidState().isEmpty() && !ground.is(net.minecraft.world.level.block.Blocks.MAGMA_BLOCK)
            && ground.isFaceSturdy(client.level, below, Direction.UP);
    }

    private static boolean isPortal(BlockState state) {
        return state.is(net.minecraft.world.level.block.Blocks.END_PORTAL)
            || state.is(net.minecraft.world.level.block.Blocks.NETHER_PORTAL);
    }

    /** Cells beside a portal block the Body can stand in (two clear cells, a
     * floor, no portal), on the nearest ring around it that has any, up to
     * three out; a 3x3 end portal's middle cell is reached from its frame. */
    private static List<Goal> besidePortal(Minecraft client, BlockPos portal) {
        for (int ring = 1; ring <= 3; ring++) {
            List<Goal> goals = new ArrayList<>();
            for (int dx = -ring; dx <= ring; dx++) {
                for (int dz = -ring; dz <= ring; dz++) {
                    if (Math.max(Math.abs(dx), Math.abs(dz)) != ring) continue;
                    for (int dy = -2; dy <= 1; dy++) {
                        BlockPos feet = portal.offset(dx, dy, dz);
                        BlockState floor = client.level.getBlockState(feet.below());
                        if (clearOfHazard(client, feet) && clearOfHazard(client, feet.above())
                            && !isPortal(client.level.getBlockState(feet)) && !isPortal(client.level.getBlockState(feet.above()))
                            && !isPortal(floor) && floor.getFluidState().isEmpty()
                            && !floor.getCollisionShape(client.level, feet.below()).isEmpty()) {
                            goals.add(new GoalBlock(feet));
                        }
                    }
                }
            }
            if (!goals.isEmpty()) return goals;
        }
        return List.of();
    }

    /** Whether the player's box is in a portal block (an end portal only where its shape is). */
    private static boolean insidePortal(Minecraft client) {
        var box = client.player.getBoundingBox().deflate(1.0E-7);
        for (BlockPos pos : BlockPos.betweenClosed(BlockPos.containing(box.minX, box.minY, box.minZ),
                BlockPos.containing(box.maxX, box.maxY, box.maxZ))) {
            BlockState state = client.level.getBlockState(pos);
            if (!isPortal(state)) continue;
            var shape = state.getShape(client.level, pos);
            if (state.is(net.minecraft.world.level.block.Blocks.NETHER_PORTAL) || net.minecraft.world.phys.shapes.Shapes.joinIsNotEmpty(
                    net.minecraft.world.phys.shapes.Shapes.create(box.move(-pos.getX(), -pos.getY(), -pos.getZ())),
                    shape, net.minecraft.world.phys.shapes.BooleanOp.AND)) return true;
        }
        return false;
    }

    /**
     * A walk into a portal block: Baritone to a cell beside it, then a step
     * straight toward the portal cell's centre with the movement keys, letting
     * go over it so the Body drops into a floor portal. Done when the dimension
     * changes; for a nether portal, when the Body stands in it (the game
     * carries it after a few seconds of standing). Returns false while
     * Baritone still walks, so its route failures are said as for any walk.
     */
    private boolean tickPortalWalk(Minecraft client, Run current) {
        var player = client.player;
        if (client.level.dimension() != current.portalDimension) {
            requestStop("completed", "entered_portal");
            return true;
        }
        boolean nether = client.level.getBlockState(current.portalCell).is(net.minecraft.world.level.block.Blocks.NETHER_PORTAL);
        if (insidePortal(client)) {
            client.options.keyUp.setDown(false);
            client.options.keyJump.setDown(false);
            if (nether) { requestStop("completed", "standing_in_portal"); return true; }
            if (current.portalInsideTick < 0) current.portalInsideTick = current.ticks;
            if (current.ticks - current.portalInsideTick > 100) requestStop("failed", "portal_did_not_carry_the_body");
            return true;
        }
        if (current.portalPhase == 0) {
            if (current.goal == null || !current.goal.isInGoal(player.blockPosition()) || !player.onGround()) return false;
            baritone.getPathingBehavior().cancelEverything();
            current.portalPhase = 1;
            current.portalStepTick = current.ticks;
        }
        if (current.ticks - current.portalStepTick > 100) {
            requestStop("failed", "portal_not_entered");
            return true;
        }
        double dx = current.portalCell.getX() + 0.5 - player.getX();
        double dz = current.portalCell.getZ() + 0.5 - player.getZ();
        if (dx * dx + dz * dz <= 0.15 * 0.15) {
            // Over the cell's centre: let go and drop in.
            client.options.keyUp.setDown(false);
            client.options.keyJump.setDown(false);
            return true;
        }
        player.setYRot((float) (Math.atan2(dz, dx) * 180.0 / Math.PI) - 90.0f);
        player.setSprinting(false);
        client.options.keyUp.setDown(true);
        client.options.keyJump.setDown(player.onGround() && player.horizontalCollision);
        return true;
    }

    private static boolean clearOfHazard(Minecraft client, BlockPos pos) {
        BlockState state = client.level.getBlockState(pos);
        return state.getFluidState().isEmpty() && state.getCollisionShape(client.level, pos).isEmpty()
            && !state.is(net.minecraft.tags.BlockTags.FIRE);
    }

    private static JsonObject blockFact(BlockState state, BlockPos pos) {
        JsonObject fact = new JsonObject();
        fact.addProperty("x", pos.getX());
        fact.addProperty("y", pos.getY());
        fact.addProperty("z", pos.getZ());
        fact.addProperty("type", BuiltInRegistries.BLOCK.getKey(state.getBlock()).toString());
        String kind = state.isAir() || state.canBeReplaced() ? "CLEAR"
            : (!state.getFluidState().isEmpty() ? "FLUID" : "SOLID");
        fact.addProperty("state", kind);
        JsonObject properties = new JsonObject();
        for (Property<?> property : state.getProperties()) {
            properties.addProperty(property.getName(), propertyValue(state, property));
        }
        fact.add("properties", properties);
        return fact;
    }

    private static <T extends Comparable<T>> String propertyValue(BlockState state, Property<T> property) {
        return property.getName(state.getValue(property));
    }

    /** Best first; the same order as MELEE_WEAPONS in minebot/app/reflex_question.py. */
    private static final List<String> MELEE_WEAPONS = List.of(
        "netherite_sword", "diamond_sword", "iron_sword", "copper_sword", "stone_sword", "golden_sword", "wooden_sword",
        "netherite_axe", "diamond_axe", "iron_axe", "copper_axe", "stone_axe", "golden_axe", "wooden_axe");

    /**
     * A player draws its best weapon before it swings. Engage hit with
     * whatever was in hand: a diamond pickaxe against magma cubes while an
     * iron sword sat in the inventory (eval-20261020-m2 run-1, death 1).
     * Checked on every swing, since pathing to the target can change the hand.
     */
    private static void holdBestMeleeWeapon(Minecraft client) {
        String held = itemPath(client.player.getMainHandItem());
        for (String weapon : MELEE_WEAPONS) {
            if (weapon.equals(held)) return;
            if (count(client, weapon) > 0) {
                try {
                    selectItem(client, weapon);
                } catch (IllegalArgumentException ignored) {
                    // Cursor busy or no inventory menu: swing with what is in hand this tick.
                }
                return;
            }
        }
    }

    private static boolean opensMenus(Run run) {
        return run.kind.equals("containerTransfer") || run.kind.equals("furnaceTransfer") || run.kind.equals("craftItem");
    }

    /**
     * A container screen no action holds any more is closed first, as a
     * player presses Escape before reaching into its own inventory.
     */
    private static void closeLeftoverMenu(Minecraft client) {
        if (client.player.containerMenu.containerId == 0 || !client.player.containerMenu.getCarried().isEmpty()) return;
        if (instance != null && (instance.menuRead != null || (instance.run != null && opensMenus(instance.run)))) return;
        client.player.closeContainer();
    }

    private static void selectItem(Minecraft client, String item) {
        if (item.isBlank()) throw new IllegalArgumentException("missing_item");
        String wanted = item.replace("minecraft:", "");
        for (int slot = 0; slot < 9; slot++) {
            ItemStack stack = client.player.getInventory().getItem(slot);
            if (!stack.isEmpty() && BuiltInRegistries.ITEM.getKey(stack.getItem()).getPath().equals(wanted)) {
                client.player.getInventory().setSelectedSlot(slot);
                return;
            }
        }
        for (int slot = 9; slot < 36; slot++) {
            ItemStack stack = client.player.getInventory().getItem(slot);
            if (stack.isEmpty() || !itemPath(stack).equals(wanted)) continue;
            closeLeftoverMenu(client);
            if (!(client.player.containerMenu instanceof InventoryMenu menu)) {
                throw new IllegalArgumentException("inventory_menu_unavailable");
            }
            if (!menu.getCarried().isEmpty()) throw new IllegalArgumentException("inventory_cursor_occupied");
            int hotbar = borrowedHotbarSlot(client);
            click(menu, menuSlotForPlayer(menu, slot), hotbar, ContainerInput.SWAP, client);
            client.player.getInventory().setSelectedSlot(hotbar);
            return;
        }
        throw new IllegalArgumentException("not_in_inventory");
    }

    /**
     * The hotbar slot an item from the main inventory is brought into.
     * Baritone's inventory behaviour keeps the best pickaxe in slot 0 and a
     * throwaway block in slot 8, swapping them back every tick
     * (InventoryBehavior.onTick). Brought into slot 0 with a full hotbar, a
     * water bucket, flint and steel or an apple was swapped out again before
     * it was used: every bucket and fire use and 114 meals failed silently in
     * the eval-20261019 track P run. Slots 1-7 as in Baritone's own
     * getTempHotbarSlot: an empty one first, else one without a tool or weapon.
     */
    private static int borrowedHotbarSlot(Minecraft client) {
        for (int candidate = 1; candidate < 8; candidate++) {
            if (client.player.getInventory().getItem(candidate).isEmpty()) return candidate;
        }
        for (int candidate = 7; candidate >= 1; candidate--) {
            if (!client.player.getInventory().getItem(candidate).isDamageableItem()) return candidate;
        }
        return 7;
    }

    /** Digging work that puts a torch down where it has got dark. */
    private static final Set<String> LIGHT_THE_WAY_KINDS = Set.of("mine", "collectBlocks", "mineBlock", "digDown");
    /** Torch light falls by one a block: placed at 4 or less, torches stand about ten blocks apart. */
    private static final int DARK_AT = 4;

    /**
     * A player digging where neither a torch nor the sky lights the place puts a
     * torch down as they go (project-review-20261110 §9): a mob spawns where no
     * torch reaches and the sky does not light. From torches already carried;
     * whether to carry any is the Agent's. On a side wall of the cell the Body
     * stands in, never the wall ahead (it is the next to be dug); on the floor
     * only when not digging down through it. Pillaring and digging up stay
     * out: they put blocks into the cell a torch would take.
     */
    private void lightTheWay(Minecraft client, Run current) {
        if (!LIGHT_THE_WAY_KINDS.contains(current.kind) || current.collectionTree) return;
        if (current.ticks < current.lightNextTick || !client.player.onGround() || client.player.isInWater()) return;
        BlockPos feet = client.player.blockPosition();
        if (client.level.getBrightness(net.minecraft.world.level.LightLayer.BLOCK, feet) > DARK_AT
            || client.level.getBrightness(net.minecraft.world.level.LightLayer.SKY, feet) > DARK_AT) return;
        if (!client.level.getBlockState(feet).isAir() || count(client, "torch") <= 0) return;
        current.lightNextTick = current.ticks + 40;
        BlockHitResult hit = torchSupport(client, feet, !current.kind.equals("digDown"));
        if (hit == null || !client.player.isWithinBlockInteractionRange(hit.getBlockPos(), 0)) return;
        int held = client.player.getInventory().getSelectedSlot();
        boolean placed;
        try {
            selectItem(client, "torch");
            placed = client.gameMode.useItemOn(client.player, InteractionHand.MAIN_HAND, hit).consumesAction();
        } catch (IllegalArgumentException busy) {
            placed = false;
        }
        client.player.getInventory().setSelectedSlot(held);
        if (!placed) {
            current.lightNextTick = current.ticks + 200;
            return;
        }
        current.torchesPlaced++;
        current.torchCells.add(xyz(feet));
    }

    /** A face a torch in ``feet`` hangs on: the side walls, then the floor, then the wall behind. */
    private static BlockHitResult torchSupport(Minecraft client, BlockPos feet, boolean floorToo) {
        Direction facing = client.player.getDirection();
        List<Direction> walls = List.of(facing.getClockWise(), facing.getCounterClockWise(), facing.getOpposite());
        List<Direction> order = new ArrayList<>(walls.subList(0, 2));
        if (floorToo) order.add(Direction.DOWN);
        order.add(walls.get(2));
        for (Direction toward : order) {
            BlockPos support = feet.relative(toward);
            Direction face = toward.getOpposite();
            if (!client.level.getBlockState(support).isFaceSturdy(client.level, support, face)) continue;
            Vec3 at = Vec3.atCenterOf(support).add(Vec3.atLowerCornerOf(face.getUnitVec3i()).scale(0.5));
            return new BlockHitResult(at, face, support, false);
        }
        return null;
    }

    private static void selectSlot(Minecraft client, JsonObject params) {
        int slot = params.has("slot") ? params.get("slot").getAsInt() : -1;
        if (slot < 0 || slot >= 9) throw new IllegalArgumentException("invalid_hotbar_slot");
        client.player.getInventory().setSelectedSlot(slot);
    }

    private static void lookAt(Minecraft client, JsonObject params) {
        JsonArray target = params.has("target") && params.get("target").isJsonArray()
            ? params.getAsJsonArray("target") : null;
        if (target == null || target.size() != 3) throw new IllegalArgumentException("invalid_look_target");
        double dx = target.get(0).getAsDouble() - client.player.getX();
        double dy = target.get(1).getAsDouble() - (client.player.getY() + client.player.getEyeHeight());
        double dz = target.get(2).getAsDouble() - client.player.getZ();
        double horizontal = Math.sqrt(dx * dx + dz * dz);
        float yaw = (float) (Math.atan2(dz, dx) * 180.0 / Math.PI) - 90.0f;
        float pitch = (float) -(Math.atan2(dy, horizontal) * 180.0 / Math.PI);
        client.player.setYRot(yaw);
        client.player.setXRot(pitch);
        BaritoneAPI.getProvider().getBaritoneForMinecraft(client).getLookBehavior()
            .updateTarget(new Rotation(yaw, pitch), true);
    }

    private static void useItem(Minecraft client, JsonObject params) {
        if (params.has("item")) selectItem(client, string(params, "item", ""));
        client.gameMode.useItem(client.player, InteractionHand.MAIN_HAND);
    }

    private static void attackEntity(Minecraft client, JsonObject params) {
        String wantedId = string(params, "target_id", "");
        String wantedName = string(params, "target_name", "");
        String wantedType = string(params, "target_type", "").replace("minecraft:", "");
        Entity selected = null;
        double best = Double.MAX_VALUE;
        for (Entity entity : client.level.getEntities(client.player, client.player.getBoundingBox().inflate(32.0), value -> true)) {
            boolean match = (!wantedId.isBlank() && entity.getUUID().toString().equals(wantedId))
                || (!wantedName.isBlank() && entity.getName().getString().equals(wantedName))
                || (!wantedType.isBlank() && BuiltInRegistries.ENTITY_TYPE.getKey(entity.getType()).getPath().equals(wantedType));
            if (match && client.player.distanceToSqr(entity) < best) {
                selected = entity;
                best = client.player.distanceToSqr(entity);
            }
        }
        if (selected == null) throw new IllegalArgumentException("target_not_found");
        lookAt(client, lookParams(selected.getEyePosition()));
        client.gameMode.attack(client.player, selected);
    }

    private static void dropItem(Minecraft client, JsonObject params) {
        String targetId = string(params, "target_uuid", "");
        if (!targetId.isBlank()) {
            Entity target = client.level.getEntities(client.player, client.player.getBoundingBox().inflate(32.0),
                entity -> entity.getUUID().toString().equals(targetId)).stream().findFirst().orElse(null);
            if (target == null) throw new IllegalArgumentException("target_not_found");
            lookAt(client, lookParams(target.getEyePosition()));
        }
        int slot = params.has("slot") ? params.get("slot").getAsInt() : client.player.getInventory().getSelectedSlot();
        if (slot < 0 || slot >= client.player.getInventory().getContainerSize()) throw new IllegalArgumentException("invalid_inventory_slot");
        closeLeftoverMenu(client);
        if (!(client.player.containerMenu instanceof InventoryMenu menu)) {
            throw new IllegalArgumentException("inventory_menu_unavailable");
        }
        if (!menu.getCarried().isEmpty()) throw new IllegalArgumentException("inventory_cursor_occupied");
        int source = menuSlotForPlayer(menu, slot);
        if (source < 0) throw new IllegalArgumentException("invalid_inventory_slot");
        if (menu.getSlot(source).getItem().isEmpty()) throw new IllegalArgumentException("source_empty");
        click(menu, source, "all".equals(string(params, "mode", "one")) ? 1 : 0,
            ContainerInput.THROW, client);
    }

    private static void moveItem(Minecraft client, JsonObject params) {
        int from = params.get("from_slot").getAsInt();
        int to = params.get("to_slot").getAsInt();
        if (from < 0 || from > 44 || to < 0 || to > 40 || from == to) {
            throw new IllegalArgumentException("invalid_inventory_slot");
        }
        closeLeftoverMenu(client);
        if (!(client.player.containerMenu instanceof InventoryMenu menu)) {
            throw new IllegalArgumentException("inventory_menu_unavailable");
        }
        if (!menu.getCarried().isEmpty()) throw new IllegalArgumentException("inventory_cursor_occupied");
        // Protocol 41–44 are personal crafting inputs, not player inventory indices.
        int source = from < 41 ? menuSlotForPlayer(menu, from) : from - 40;
        int destination = menuSlotForPlayer(menu, to);
        if (source < 0 || destination < 0) throw new IllegalArgumentException("invalid_inventory_slot");
        Slot sourceSlot = menu.getSlot(source);
        Slot destinationSlot = menu.getSlot(destination);
        ItemStack stack = sourceSlot.getItem();
        ItemStack target = destinationSlot.getItem();
        if (stack.isEmpty()) throw new IllegalArgumentException("source_empty");
        if (!target.isEmpty() && !ItemStack.isSameItemSameComponents(stack, target)) {
            throw new IllegalArgumentException("destination_occupied");
        }
        int amount = integer(params, "count", stack.getCount());
        int capacity = Math.min(integer(params, "max_stack", 64), destinationSlot.getMaxStackSize(stack));
        if (amount <= 0 || amount > stack.getCount() || target.getCount() + amount > capacity
            || !sourceSlot.mayPickup(client.player) || !destinationSlot.mayPlace(stack)) {
            throw new IllegalArgumentException("move_count_or_capacity_mismatch");
        }
        moveCount(client, menu, source, destination, amount);
    }

    private static void mineBlock(Minecraft client, JsonObject params) {
        BlockPos target = blockPosition(params, "target");
        if (!client.player.isWithinBlockInteractionRange(target, 5.0)) throw new IllegalArgumentException("target_out_of_range");
        lookAt(client, lookParams(Vec3.atCenterOf(target)));
        if (!client.gameMode.destroyBlock(target)) throw new IllegalArgumentException("block_break_rejected");
    }

    private static void placeBlock(Minecraft client, JsonObject params) {
        BlockPos target = blockPosition(params, "target");
        if (params.has("block_type")) selectItem(client, string(params, "block_type", ""));
        BlockHitResult hit = new BlockHitResult(Vec3.atCenterOf(target), Direction.UP, target, false);
        InteractionResult result = client.gameMode.useItemOn(client.player, InteractionHand.MAIN_HAND, hit);
        if (!result.consumesAction()) throw new IllegalArgumentException("block_place_rejected");
    }

    private static void useOnBlock(Minecraft client, String action, JsonObject params) {
        BlockPos target = blockPosition(params, "target");
        if (params.has("item")) selectItem(client, string(params, "item", ""));
        if (params.has("seed_item")) selectItem(client, string(params, "seed_item", ""));
        BlockHitResult hit = new BlockHitResult(Vec3.atCenterOf(target), Direction.UP, target, false);
        lookAt(client, lookParams(hit.getLocation()));
        InteractionResult result = client.gameMode.useItemOn(client.player, InteractionHand.MAIN_HAND, hit);
        if (!result.consumesAction()) throw new IllegalArgumentException(action + "_rejected");
    }

    private static BlockPos blockPosition(JsonObject params, String key) {
        JsonArray raw = params.has(key) && params.get(key).isJsonArray() ? params.getAsJsonArray(key) : null;
        if (raw == null || raw.size() != 3) throw new IllegalArgumentException("invalid_target");
        return new BlockPos(raw.get(0).getAsInt(), raw.get(1).getAsInt(), raw.get(2).getAsInt());
    }

    private void requestStop(String status, String reason) {
        if (run != null && run.stopStatus == null) {
            run.stopStatus = status;
            run.stopReason = reason;
            run.stopAt = System.nanoTime();
            if (!(legsWalking() && isHandRun(run))) {
                baritone.getPathingBehavior().cancelEverything();
                baritone.getInputOverrideHandler().clearAllKeys();
            }
            restoreSwimSettings(run);
            restoreWaySettings(run);
            if (run.kind.equals("engageEntity") && Minecraft.getInstance().player != null) {
                kiteKeys(Minecraft.getInstance(), false, false, false);
            }
            if (run.portalCell != null && Minecraft.getInstance().player != null) {
                Minecraft.getInstance().options.keyUp.setDown(false);
                Minecraft.getInstance().options.keyJump.setDown(false);
            }
            if (run.structured && Minecraft.getInstance().player != null) {
                Minecraft client = Minecraft.getInstance();
                if ((isVertical(run) || run.kind.equals("shelterInPlace") || run.kind.equals("placeBedAndSleep")
                    || run.kind.equals("collectBlocks")) && client.gameMode != null) client.gameMode.stopDestroyBlock();
                if (client.player.isUsingItem() && client.gameMode != null) {
                    client.gameMode.releaseUsingItem(client.player);
                }
                if (run.kind.equals("rangedAttack") || run.kind.equals("useItem") || run.kind.equals("raiseShield")) {
                    client.options.keyUse.setDown(false);
                }
                if (run.kind.equals("placeBedAndSleep")) client.options.keyAttack.setDown(false);
                if (run.swimmingUp || run.kind.equals("pillarUp")) client.options.keyJump.setDown(false);
                if (run.kind.equals("pillarUp")) {
                    client.options.keyUp.setDown(false);
                    client.options.keyShift.setDown(false);
                }
                if (run.kind.equals("leaveLava") || run.kind.equals("stepTo")) {
                    client.options.keyUp.setDown(false);
                    client.options.keyJump.setDown(false);
                    client.player.setSprinting(false);
                }
                if (opensMenus(run)) {
                    // Whatever the action opened, in whichever phase it stopped: before
                    // its id was recorded, or after its reopen, the recorded id is not
                    // the open one. Left open, it made every later inventory and menu
                    // action fail (eval-20261020-m2 run-1, 2026-10-23).
                    if (client.player.containerMenu.containerId != 0) client.player.closeContainer();
                }
            }
        }
    }

    /** Baritone settings a run changed for its walk (make_way, keep_moving), put back. */
    private static void restoreWaySettings(Run current) {
        if (current.wayPreviousAllowBreak != null) {
            BaritoneAPI.getSettings().allowBreak.value = current.wayPreviousAllowBreak;
            BaritoneAPI.getSettings().allowPlace.value = current.wayPreviousAllowPlace;
            current.wayPreviousAllowBreak = null;
        }
        if (current.keepPreviousSprint != null) {
            BaritoneAPI.getSettings().allowSprint.value = current.keepPreviousSprint;
            current.keepPreviousSprint = null;
        }
    }

    /**
     * A tool that wears out in the hand ends the action, as it would stop a
     * player. What to do next is decided by whoever asked for the action.
     */
    private void observeHeldTool(Minecraft client, Run current) {
        ItemStack held = client.player.getMainHandItem();
        int slot = client.player.getInventory().getSelectedSlot();
        if (current.heldToolLastUse && current.heldToolSlot == slot && held.isEmpty()) {
            current.brokenItem = current.heldTool;
            current.heldToolLastUse = false;
            requestStop("failed", "tool_broke");
            return;
        }
        current.heldToolSlot = slot;
        current.heldTool = itemPath(held);
        // Only a stack on its last use that then vanishes from the same slot broke;
        // a moved, dropped or swapped item did not.
        current.heldToolLastUse = held.isDamageableItem() && held.getDamageValue() >= held.getMaxDamage() - 1;
    }

    private void tick(Minecraft client) {
        if (client.player == null || client.level == null || baritone == null) {
            return;
        }
        joinedTicks++;
        boolean playerDead = client.player.isDeadOrDying();
        var changes = lifecycle.observe(true, playerDead, client.screen instanceof DeathScreen,
            client.level.hasChunkAt(client.player.blockPosition()));
        if (changes.died()) {
            JsonObject facts = state(client);
            if (lastLivingState != null) facts.add("last_living_state", lastLivingState.deepCopy());
            lifecycleEvents.clear();
            // Death resets the legs to stand and the eyes to level (§6.1).
            if (legs != null) facts.addProperty("movement_reset", true);
            if (gazeDown) facts.addProperty("gaze_reset", true);
            if (legs != null) endLegs(client, "death", false);
            gazeDown = false;
            emitLifecycle(client, "death", facts);
            if (run != null) requestStop("failed", "death");
        }
        if (changes.requestRespawn()) client.player.respawn();
        if (changes.respawned()) {
            lastDamageSource = null;
            lastDamageTick = -1L;
            emitLifecycle(client, "respawned", state(client));
        }
        if (playerDead) {
            if (run != null && !processActive() && !baritone.getPathingBehavior().isPathing())
                terminal(client, "failed", "death");
            return;
        }
        // These are native observations, not a second danger detector.
        if (joinedTicks % 20 == 0) lastLivingState = state(client);
        watchShot(client);
        if (menuRead != null) tickMenuRead(client);
        if (!ready && joinedTicks >= 20 && client.level.hasChunkAt(client.player.blockPosition()) && client.player.onGround()) {
            ready = true;
            readyAtMs = (System.nanoTime() - loadedAt) / 1_000_000;
            JsonObject response = state(client);
            response.addProperty("type", "ready");
            response.addProperty("mod_to_ready_ms", readyAtMs);
            if (peer != null) {
                peer.send(response);
                for (JsonObject event : lifecycleEvents) peer.send(event);
            }
            LOG.info("MINEBOT_PHASE_A ready {}", response);
            emitLifecycle(client, "world_ready", state(client));
        }
        tickLegs(client);
        Run current = run;
        if (current == null) {
            return;
        }
        current.ticks++;
        // The walk starts looking down, before the first path is ready.
        if (current.lookDown && current.ticks == 1 && current.stopStatus == null) {
            client.player.setXRot(LOOK_DOWN_PITCH);
        }
        if (current.stopStatus == null) observeHeldTool(client, current);
        if (current.stopStatus == null) observeNavigation(client, current);
        if (current.stopStatus == null) lightTheWay(client, current);
        if (current.kind.equals("mine") && current.stopStatus == null
            && baritone.getInputOverrideHandler().isInputForcedDown(Input.CLICK_LEFT)) {
            baritone.getPlayerContext().getSelectedBlock().ifPresent(pos -> {
                String block = BuiltInRegistries.BLOCK.getKey(client.level.getBlockState(pos).getBlock()).getPath();
                for (String target : current.targets) {
                    if (target.replace("minecraft:", "").equals(block)) current.lastResourceTarget = pos.immutable();
                }
            });
        }
        if (current.kind.equals("elytra") && client.player.isFallFlying()) current.seenFlying = true;
        if (current.structured) {
            current.seenActive = true;
            if (current.stopStatus == null) {
                tickStructuredAction(client, current);
            }
        }
        if (processActive()) current.seenActive = true;
        int cancelTicks = Integer.getInteger("minebot.client.cancel_after_ticks", 0);
        if (cancelTicks > 0 && current.ticks >= cancelTicks) {
            requestStop("canceled", "launch_cancel_requested");
        }
        if (current.stopStatus == null && !current.structured && tickSealWater(client, current)) {
            // Sealing water owns the Body for these ticks.
        } else if (current.stopStatus == null && !current.structured && current.portalCell != null
            && tickPortalWalk(client, current)) {
            // Stepping into the portal owns the Body for these ticks.
        } else if (current.stopStatus == null && !current.structured) {
            if (current.wayGoal != null && current.goal != current.wayGoal
                && current.goal.isInGoal(client.player.blockPosition()) && client.player.onGround()) {
                current.goal = current.wayGoal;
                current.wayHoldUntil = current.ticks + 20;
                baritone.getCustomGoalProcess().setGoalAndPath(current.goal);
            } else if (current.item != null && count(client, current.item) - current.before >= (current.kind.equals("mine") ? current.mineQuantity : 1)) {
                requestStop("completed", "inventory_increased");
            } else if (current.goal != null && current.goal.isInGoal(client.player.blockPosition()) && client.player.onGround()) {
                requestStop("completed", "goal_reached");
            } else if (current.kind.equals("follow")) {
                tickFollow(client, current);
            } else if (current.elapsed() > current.timeout) {
                requestStop("timeout", "deadline_exceeded");
            } else if (current.ticks > 20 && current.ticks >= current.wayHoldUntil
                && !processActive() && !baritone.getPathingBehavior().isPathing()
                // Baritone may release control one tick before landing in the
                // goal cell. Let the existing arrival check observe the landing;
                // the action deadline still bounds an unsettled destination.
                && !(current.goal != null && current.goal.isInGoal(client.player.blockPosition()))) {
                if (current.kind.equals("elytra")) {
                    boolean arrived = current.seenFlying && client.player.onGround()
                        && client.player.position().distanceToSqr(Vec3.atBottomCenterOf(current.anchor)) <= 64;
                    requestStop(arrived ? "completed" : "failed", arrived ? "elytra_destination_reached"
                        : current.seenFlying ? "elytra_stopped_outside_destination" : "elytra_stopped_without_flight");
                } else if (current.kind.equals("build") || current.kind.equals("clear")) {
                    // An already-satisfied schematic can finish before our
                    // first tick observes an active process. Verify the cells;
                    // seenActive is diagnostic, not the completion condition.
                    boolean verified = cuboidMatches(client, current);
                    requestStop(verified ? "completed" : "failed",
                        verified ? "world_result_verified" : "world_result_mismatch");
                } else {
                    boolean finite = current.seenActive && (current.kind.equals("get") || current.kind.equals("farm"));
                    if (!finite && makeWayStep(client, current)) return;
                    // The last route calculation failed: say so rather than "stopped".
                    // Five walks out of the End arrival cell ended this way and read
                    // as a Baritone stop (end baseline run-3, 15:19).
                    boolean noRoute = !current.routeFound && current.pathEvents.getOrDefault("CALC_FAILED", 0) > 0;
                    requestStop(finite ? "completed" : "failed", finite ? "process_finished_verify_world"
                        : noRoute ? "no_route_found" : "baritone_stopped_without_result");
                }
            }
        }
        if (current.ticks % 20 == 0) {
            JsonObject response = state(client);
            if (current.kind.equals("build")) response.add("construction_progress", cuboidProgress(client, current));
            if (current.kind.equals("keepMoving")) response.add("keep_moving", keepMovingFacts(current));
            response.addProperty("type", "progress");
            response.addProperty("action_id", current.id);
            emit(current, withTelemetry(current, response));
        } else if (current.ticks % 5 == 0) {
            emit(current, liteProgress(client, current));
        }
        if (current.stopStatus != null) {
            // The action terminal means that Baritone no longer owns a process
            // or emits pathing input.  Physical settling (for example, landing
            // after a cancel while falling) is a separate world fact and must
            // not turn a stopped action into a multi-second IPC timeout.
            // A hand on walking legs never owned Baritone: the legs' walk goes on.
            boolean stopped = (legsWalking() && isHandRun(current))
                || (!processActive() && !baritone.getPathingBehavior().isPathing());
            if (stopped && current.baritoneStoppedAt == 0) {
                current.baritoneStoppedAt = System.nanoTime();
            }
            current.stableTicks = stopped ? current.stableTicks + 1 : 0;
            if (current.stableTicks >= 3) {
                if (EditSettle.waitMore(unconfirmedEdits(client, current), current.settleTicks)) {
                    current.settleTicks++;
                    return;
                }
                terminal(client, current.stopStatus, current.stopReason);
            }
        }
    }

    private void emitLifecycle(Minecraft client, String name, JsonObject facts) {
        JsonObject event = new JsonObject();
        event.addProperty("type", "body_event");
        event.addProperty("epoch", lifecycleEpoch);
        event.addProperty("seq", ++lifecycleSeq);
        event.addProperty("tick", client.level == null ? 0 : client.level.getGameTime());
        event.addProperty("name", name);
        event.add("data", facts);
        lifecycleEvents.add(event);
        if (peer != null) peer.send(event);
        LOG.info("MINEBOT_LIFECYCLE {}", event);
    }

    private void tickBuilderFile(Minecraft client, Run current) {
        if (current.phase == 0) {
            String file = string(current.params, "path", "");
            BlockPos origin = positionParam(current.params, "origin");
            if (file.isBlank() || origin == null) throw new IllegalArgumentException("missing_builder_file");
            enableBuilderInventory(current);
            enableBreakFromAbove(current);
            current.phase = 1;
            current.phaseTick = current.ticks;
            if (!baritone.getBuilderProcess().build(Path.of(file).getFileName().toString(), Path.of(file).toFile(), origin)) {
                requestStop("failed", "builder_file_rejected");
            }
        } else if (current.ticks > current.phaseTick + 1 && !baritone.getBuilderProcess().isActive()
            && !baritone.getPathingBehavior().isPathing()) {
            requestStop("completed", "builder_finished_verify_world");
        }
    }

    private static void enableBuilderInventory(Run current) {
        current.previousAllowInventory = BaritoneAPI.getSettings().allowInventory.value;
        BaritoneAPI.getSettings().allowInventory.value = true;
    }

    private static void restoreBuilderInventory(Run current) {
        if (current == null || current.previousAllowInventory == null) return;
        BaritoneAPI.getSettings().allowInventory.value = current.previousAllowInventory;
        current.previousAllowInventory = null;
    }

    /** Record the source named by the server's damage event packet for the local player. */
    public static void observeDamage(ClientboundDamageEventPacket packet) {
        MineBotClientBody body = instance;
        Minecraft client = Minecraft.getInstance();
        if (body == null || client.player == null || client.level == null
            || packet.entityId() != client.player.getId()) return;
        Entity cause = client.level.getEntity(packet.sourceCauseId());
        Entity direct = client.level.getEntity(packet.sourceDirectId());
        Entity source = cause != null ? cause : direct;
        JsonObject observed = new JsonObject();
        observed.addProperty("damage_type", packet.sourceType().getRegisteredName());
        if (source != null) {
            observed.addProperty("id", source.getUUID().toString());
            observed.addProperty("name", source.getName().getString());
            observed.addProperty("type", BuiltInRegistries.ENTITY_TYPE.getKey(source.getType()).getPath());
            observed.addProperty("distance", client.player.distanceTo(source));
            JsonArray sourcePos = new JsonArray();
            sourcePos.add(source.getX());
            sourcePos.add(source.getY());
            sourcePos.add(source.getZ());
            observed.add("position", sourcePos);
            if (direct != null && direct != source) observed.addProperty("direct_id", direct.getUUID().toString());
        }
        body.lastDamageSource = observed;
        body.lastDamageTick = client.level.getGameTime();
        if (!(direct instanceof LivingEntity) && !(direct instanceof net.minecraft.world.entity.projectile.Projectile)
            && !NOT_FROM_PLACE.contains(packet.sourceType().getRegisteredName())) {
            synchronized (body.placeHurts) {
                body.placeHurts.addLast(new double[]{client.level.getGameTime(), client.player.getX(), client.player.getY(), client.player.getZ()});
                while (body.placeHurts.size() > 32) body.placeHurts.removeFirst();
            }
        }
    }

    // Damage that comes with the Body wherever it stands: burning, effects,
    // hunger, a landing.
    private static final java.util.Set<String> NOT_FROM_PLACE = java.util.Set.of(
        "minecraft:on_fire", "minecraft:magic", "minecraft:wither", "minecraft:starve", "minecraft:fall",
        "minecraft:generic", "minecraft:generic_kill", "minecraft:fly_into_wall");
    private static final int HURT_HERE_TICKS = 60;

    /** How often the place the Body stands on now has hurt it in the last three seconds. */
    private JsonObject hurtHere(Minecraft client) {
        long now = client.level.getGameTime();
        int times = 0;
        long first = now;
        synchronized (placeHurts) {
            for (double[] hurt : placeHurts) {
                if (now - (long) hurt[0] > HURT_HERE_TICKS) continue;
                double dx = hurt[1] - client.player.getX(), dz = hurt[3] - client.player.getZ();
                if (Math.sqrt(dx * dx + dz * dz) > 1.5 || Math.abs(hurt[2] - client.player.getY()) > 2.0) continue;
                times++;
                first = Math.min(first, (long) hurt[0]);
            }
        }
        if (times == 0) return null;
        JsonObject fact = new JsonObject();
        fact.addProperty("times", times);
        fact.addProperty("seconds", Math.max(1.0, round2((now - first) / 20.0)));
        return fact;
    }

    /**
     * Let the builder break blocks below the feet level, the way a player digs
     * a trench beside where they stand. Baritone's default only breaks from the
     * feet level up and never takes a stand above the block, so every clear or
     * single-block collect below the feet failed to path (house run 2026-10-17:
     * trunks above the feet 500+ cleared, every surface and stone block failed).
     * The block the Body stands on is still never broken.
     */
    private static void enableBreakFromAbove(Run current) {
        var settings = BaritoneAPI.getSettings();
        current.previousBreakFromAbove = new Boolean[] {settings.breakFromAbove.value, settings.goalBreakFromAbove.value};
        settings.breakFromAbove.value = true;
        settings.goalBreakFromAbove.value = true;
    }

    private static void restoreBreakFromAbove(Run current) {
        if (current == null || current.previousBreakFromAbove == null) return;
        BaritoneAPI.getSettings().breakFromAbove.value = current.previousBreakFromAbove[0];
        BaritoneAPI.getSettings().goalBreakFromAbove.value = current.previousBreakFromAbove[1];
        current.previousBreakFromAbove = null;
    }

    /** Read native execution failures, not CALC_STARTED or generic cancellation. */
    private void observeNavigation(Minecraft client, Run current) {
        var progress = current.navigationProgress;
        progress.observePosition(client.player.getX(), client.player.getY(), client.player.getZ(), current.elapsed());
        if (client.gameMode instanceof MiningProgressAccessor mining && mining.minebot$isDestroying()) {
            BlockPos pos = mining.minebot$destroyBlockPos().immutable();
            float fraction = mining.minebot$destroyProgress();
            float highest = current.miningHighWater.getOrDefault(pos, 0.0f);
            // Repeating the same partial break after a reset is not new progress.
            if (fraction > highest + 0.01f) {
                current.miningHighWater.put(pos, fraction);
                progress.progressed(current.elapsed());
            }
        }
        PathExecutor previous = current.observedExecutor;
        if (previous != null && previous != current.checkedFailedExecutor
            && previous.movementFailure() != null) {
            current.checkedFailedExecutor = previous;
            JsonObject failure = movementFacts(client, previous);
            String key = movementKey(failure) + failure.get("pending_break") + failure.get("pending_place");
            current.navigation = failure;
            failure.addProperty("last_failure", previous.movementFailure().reason());
            LOG.info("MINEBOT_BODY movement_failed id={} step={}", current.id, failure);
            if (progress.executionFailed(key)) {
                requestStop("failed", "no_progress");
                return;
            }
        }
        var executor = baritone.getPathingBehavior().getCurrent();
        if (!(executor instanceof PathExecutor active)) return;
        current.observedExecutor = active;
        JsonObject facts = movementFacts(client, active);
        String step = movementKey(facts);
        String world = "" + facts.get("pending_break") + facts.get("pending_place");
        if (step.equals(current.navigationStep) && !world.equals(current.navigationWorld)) {
            progress.progressed(current.elapsed());
        }
        current.navigationStep = step;
        current.navigationWorld = world;
        current.navigation = facts;
    }

    private static String movementKey(JsonObject facts) {
        return "" + facts.get("movement") + facts.get("src") + facts.get("dest");
    }

    private JsonObject movementFacts(Minecraft client, PathExecutor executor) {
        JsonObject facts = new JsonObject();
        var failure = executor.movementFailure();
        int index = failure == null ? executor.getPosition() : failure.position();
        facts.addProperty("path_position", index);
        var movements = executor.getPath().movements();
        facts.addProperty("path_movements", movements.size());
        facts.addProperty("feet_block", client.level.getBlockState(client.player.blockPosition()).toString());
        if (index >= 0 && index < movements.size()) {
            var movement = failure == null ? movements.get(index) : failure.movement();
            facts.addProperty("movement", movement.getClass().getSimpleName());
            facts.add("src", coordinates(movement.getSrc()));
            facts.add("dest", coordinates(movement.getDest()));
            if (movement instanceof Movement nativeMovement) {
                var blocks = new BlockStateInterface(baritone.getPlayerContext());
                facts.add("pending_break", navigationBlocks(client, nativeMovement.toBreak(blocks)));
                facts.add("pending_place", navigationBlocks(client, nativeMovement.toPlace(blocks)));
            }
        }
        return facts;
    }

    private static JsonArray navigationBlocks(Minecraft client, List<BlockPos> positions) {
        JsonArray blocks = new JsonArray();
        for (BlockPos pos : positions) {
            JsonObject block = new JsonObject();
            block.add("pos", coordinates(pos));
            block.addProperty("state", client.level.getBlockState(pos).toString());
            blocks.add(block);
        }
        return blocks;
    }

    private static JsonArray coordinates(BlockPos pos) {
        JsonArray result = new JsonArray();
        result.add(pos.getX()); result.add(pos.getY()); result.add(pos.getZ());
        return result;
    }

    private boolean cuboidMatches(Minecraft client, Run current) {
        BlockPos end = current.kind.equals("build")
            ? current.anchor.offset(current.sx - 1, current.sy - 1, current.sz - 1) : current.end;
        var wanted = new BlockOptionalMeta(current.kind.equals("clear") ? "air" : current.target);
        for (BlockPos pos : BlockPos.betweenClosed(current.anchor, end)) {
            if (!clientChunkLoaded(client, pos) || !wanted.matches(client.level.getBlockState(pos))) return false;
        }
        return true;
    }

    private JsonObject cuboidProgress(Minecraft client, Run current) {
        long expected = (long) current.sx * current.sy * current.sz;
        int sampled = 0, observed = 0, matched = 0;
        var wanted = new BlockOptionalMeta(current.target);
        BlockPos end = current.anchor.offset(current.sx - 1, current.sy - 1, current.sz - 1);
        for (BlockPos pos : BlockPos.betweenClosed(current.anchor, end)) {
            if (sampled++ >= 4096) break;
            if (clientChunkLoaded(client, pos)) {
                observed++;
                if (wanted.matches(client.level.getBlockState(pos))) matched++;
            }
        }
        JsonObject facts = new JsonObject();
        facts.addProperty("expected_cells", expected);
        facts.addProperty("observed_cells", observed);
        facts.addProperty("matching_cells", matched);
        facts.addProperty("complete", observed == expected);
        var material = wanted.getBlock().asItem();
        facts.addProperty("material_item", material == net.minecraft.world.item.Items.AIR
            ? (String) null : BuiltInRegistries.ITEM.getKey(material).getPath());
        return facts;
    }

    private static boolean clientChunkLoaded(Minecraft client, BlockPos pos) {
        return client.level.getChunkSource().getChunk(pos.getX() >> 4, pos.getZ() >> 4,
            net.minecraft.world.level.chunk.status.ChunkStatus.FULL, false) != null;
    }

    private void tickFollow(Minecraft client, Run current) {
        if (current.targetEntity == null) {
            for (Entity entity : client.level.entitiesForRendering()) {
                boolean match = entity.getUUID().toString().equals(current.target)
                    || entity.getName().getString().equals(current.target)
                    || BuiltInRegistries.ENTITY_TYPE.getKey(entity.getType()).getPath().equals(current.target);
                if (match && entity != client.player && (current.targetEntity == null
                    || client.player.distanceToSqr(entity) < client.player.distanceToSqr(current.targetEntity))) {
                    current.targetEntity = entity;
                }
            }
        }
        if (current.targetEntity == null) {
            if (current.ticks > 20) requestStop("failed", "follow_target_not_found");
        } else if (current.targetEntity.isRemoved() || !current.targetEntity.isAlive()) {
            requestStop("failed", "follow_target_lost");
        } else if (current.elapsed() > current.timeout) {
            boolean reached = client.player.onGround()
                && client.player.distanceTo(current.targetEntity) <= current.followKeepDistance;
            requestStop(reached ? "completed" : "timeout", reached ? "follow_window_completed" : "follow_distance_not_reached");
        }
        if (current.stopStatus == null && current.targetEntity != null) {
            if (!baritone.getFollowProcess().isActive()) {
                baritone.getFollowProcess().follow(entity -> entity == current.targetEntity);
            }
            if (!baritone.getPathingBehavior().isPathing()
                && client.player.distanceTo(current.targetEntity) <= current.followKeepDistance) {
                lookAtEntityGently(client, current.targetEntity);
            }
        }
    }

    private boolean processActive() {
        return baritone.getCustomGoalProcess().isActive()
            || baritone.getMineProcess().isActive()
            || baritone.getFollowProcess().isActive()
            || baritone.getBuilderProcess().isActive()
            || baritone.getExploreProcess().isActive()
            || baritone.getFarmProcess().isActive()
            || baritone.getGetToBlockProcess().isActive()
            || baritone.getElytraProcess().isActive();
    }

    /** Execute Body actions that have no upstream Baritone process. */
    private void tickStructuredAction(Minecraft client, Run current) {
        try {
            if (current.elapsed() > current.timeout) {
                requestStop("timeout", "deadline_exceeded");
                return;
            }
            switch (current.kind) {
                case "engageEntity" -> tickEngage(client, current);
                case "useItem" -> tickUseItem(client, current);
                case "placeBlock" -> tickPlaceBlock(client, current);
                case "shelterInPlace" -> tickShelter(client, current);
                case "placeBedAndSleep" -> tickPlaceBedAndSleep(client, current);
                case "pillarUp" -> tickPillarUp(client, current);
                case "raiseShield" -> tickRaiseShield(client, current);
                case "collectBlocks" -> tickCollectBlocks(client, current);
                case "rangedAttack" -> tickRanged(client, current);
                case "craftItem" -> tickCraft(client, current);
                case "builderBuildFile" -> tickBuilderFile(client, current);
                case "containerTransfer", "furnaceTransfer" -> tickMenuTransfer(client, current);
                case "handoffItem" -> tickHandoff(client, current);
                case "ascend", "digDown", "digUp" -> tickVertical(client, current);
                case "leaveLava" -> tickLeaveLava(client, current);
                case "stepTo" -> tickStepTo(client, current);
                case "keepMoving" -> tickKeepMoving(client, current);
                case "cushionFall" -> tickCushionFall(client, current);
                default -> requestStop("failed", "unsupported_client_action");
            }
        } catch (RuntimeException error) {
            requestStop("failed", error instanceof IllegalArgumentException ? error.getMessage()
                : "client_action_failed:" + error.getClass().getSimpleName());
            LOG.warn("MINEBOT_BODY action_failed id={} action={} detail={}", current.id, current.kind, error.toString());
        }
    }

    private void tickCollectBlocks(Minecraft client, Run current) {
        if (current.phase == 0) {
            if (!Boolean.getBoolean("minebot.client.disposable_world")) {
                throw new IllegalArgumentException("native_mutation_requires_disposable_world");
            }
            JsonArray targets = current.params.getAsJsonArray("targets");
            if (targets == null || targets.isEmpty() || targets.size() > 256) {
                throw new IllegalArgumentException("invalid_collection_targets");
            }
            for (var id : current.params.getAsJsonArray("expected_block_ids")) {
                current.collectionBlocks.add(id.getAsString().replace("minecraft:", ""));
            }
            for (var id : current.params.getAsJsonArray("expected_item_ids")) {
                current.collectionItems.add(id.getAsString().replace("minecraft:", ""));
            }
            if (current.collectionBlocks.isEmpty() || current.collectionItems.isEmpty()) {
                throw new IllegalArgumentException("missing_collection_filter");
            }
            for (var raw : targets) {
                JsonArray xyz = raw.getAsJsonArray();
                BlockPos pos = new BlockPos(xyz.get(0).getAsInt(), xyz.get(1).getAsInt(), xyz.get(2).getAsInt());
                if (!client.level.hasChunkAt(pos) || !current.collectionBlocks.contains(
                    BuiltInRegistries.BLOCK.getKey(client.level.getBlockState(pos).getBlock()).getPath())) {
                    throw new IllegalArgumentException("collection_target_changed");
                }
                current.collectionTargets.add(pos);
            }
            int minX = current.collectionTargets.stream().mapToInt(BlockPos::getX).min().orElseThrow();
            int minY = current.collectionTargets.stream().mapToInt(BlockPos::getY).min().orElseThrow();
            int minZ = current.collectionTargets.stream().mapToInt(BlockPos::getZ).min().orElseThrow();
            int sizeX = current.collectionTargets.stream().mapToInt(BlockPos::getX).max().orElseThrow() - minX + 1;
            int sizeY = current.collectionTargets.stream().mapToInt(BlockPos::getY).max().orElseThrow() - minY + 1;
            int sizeZ = current.collectionTargets.stream().mapToInt(BlockPos::getZ).max().orElseThrow() - minZ + 1;
            if (Math.max(sizeX, Math.max(sizeY, sizeZ)) > 64) {
                throw new IllegalArgumentException("collection_cluster_too_large");
            }
            BlockPos origin = new BlockPos(minX, minY, minZ);
            current.before = collectionCount(client, current);
            current.collectionTree = current.collectionBlocks.stream().allMatch(id -> id.endsWith("_log") || id.endsWith("_stem"));
            if (!current.collectionTree) {
                // Use upstream mining-style goals for finite ore clusters.
                // Builder clearing can keep swinging without moving on stone.
                // Blocks within reach are broken from where the Body stands;
                // only the rest needs a route (phase 3 below).
                current.phase = 1;
                current.phaseTick = current.ticks;
                if (collectionReachable(client, current) == null) startCollectionGoal(client, current);
                return;
            }
            // Upstream builder handles movement, tool choice, digging and scaffolding.
            // Only the selected resource cells belong to this clearing schematic.
            var air = new FillSchematic(sizeX, sizeY, sizeZ, new BlockOptionalMeta("air"));
            var mask = new MaskSchematic(air) {
                @Override
                protected boolean partOfMask(int x, int y, int z, BlockState state) {
                    BlockPos pos = origin.offset(x, y, z);
                    return current.collectionTargets.contains(pos) && (state == null || state.isAir()
                        || current.collectionBlocks.contains(BuiltInRegistries.BLOCK.getKey(state.getBlock()).getPath()));
                }
            };
            // A player without dirt to stand on uses the wood just cut. The usual
            // footing blocks stay first; what is being gathered is the last resort.
            List<net.minecraft.world.item.Item> footing =
                new ArrayList<>(BaritoneAPI.getSettings().acceptableThrowawayItems.value);
            for (String id : current.collectionBlocks) {
                var item = new BlockOptionalMeta(id).getBlock().asItem();
                if (!footing.contains(item)) footing.add(item);
            }
            current.previousThrowawayItems = BaritoneAPI.getSettings().acceptableThrowawayItems.value;
            BaritoneAPI.getSettings().acceptableThrowawayItems.value = footing;
            baritone.getBuilderProcess().build("minebot-resource-cluster", mask, origin);
            current.phase = 1;
            current.phaseTick = current.ticks;
            return;
        }
        if (!current.collectionTree && (current.phase == 3 || current.phase == 1
            && !baritone.getCustomGoalProcess().isActive() && !baritone.getPathingBehavior().isPathing())
            && tickCollectInPlace(client, current)) return;
        boolean removed = current.collectionTargets.stream().allMatch(pos -> client.level.getBlockState(pos).isAir());
        if (removed && collectionCount(client, current) - current.before >= current.collectionTargets.size()) {
            requestStop("completed", "resource_cluster_collected");
        } else if (removed && current.phase == 1) {
            baritone.getPathingBehavior().cancelEverything();
            current.previousFollowRadius = BaritoneAPI.getSettings().followRadius.value;
            BaritoneAPI.getSettings().followRadius.value = 0;
            // A drop still in the air is not a place to walk to; it is
            // followed once it lies on something.
            baritone.getFollowProcess().follow(entity -> entity instanceof ItemEntity item && item.onGround()
                && current.collectionItems.contains(itemPath(item.getItem()))
                && current.collectionTargets.stream().anyMatch(pos ->
                    Math.abs(item.getX() - pos.getX()) <= 4 && Math.abs(item.getZ() - pos.getZ()) <= 4
                    && item.getY() >= pos.getY() - 3 && item.getY() <= pos.getY() + 3));
            current.phase = 2;
            current.phaseTick = current.ticks;
        } else if (current.phase == 2 && current.ticks - current.phaseTick > 20
            && !baritone.getFollowProcess().isActive()) {
            // Every target is mined and no drop is left in reach. A drop that
            // rolled away or fell out of sight is a shortfall the inventory
            // delta already states, not a failure of the work.
            requestStop("completed", "resource_cluster_collected");
        } else if (current.phase == 1 && current.ticks - current.phaseTick > 20
            && !baritone.getBuilderProcess().isActive() && !baritone.getCustomGoalProcess().isActive()
            && !baritone.getPathingBehavior().isPathing()) {
            // At a standing cell with nothing in reach (the view is blocked):
            // route again a few times, then say the cluster is incomplete.
            if (!current.collectionTree && current.goal.isInGoal(client.player.blockPosition())
                && current.collectionRegoals++ < 4) {
                startCollectionGoal(client, current);
                current.phaseTick = current.ticks;
            } else {
                requestStop("failed", "resource_cluster_incomplete");
            }
        }
    }

    /**
     * Routes only to cells the Body can stand in: the target's own cell or the
     * one under it (as upstream GoalTwoBlocks) when that cell has a floor, else
     * a floored cell within reach of the target that the Body can get to
     * through open space. Only a target with neither keeps the upstream goal,
     * so Baritone may still pillar or bridge to it.
     */
    private void startCollectionGoal(Minecraft client, Run current) {
        var ctx = baritone.getPlayerContext();
        double reach = ctx.playerController().getBlockReachDistance() - 0.5;
        Set<BlockPos> stands = new java.util.LinkedHashSet<>();
        List<Goal> unfloored = new ArrayList<>();
        Set<BlockPos> open = null;
        for (BlockPos target : current.collectionTargets) {
            if (client.level.getBlockState(target).isAir()) continue;
            boolean own = false;
            for (BlockPos feet : List.of(target, target.below())) {
                if (collectionStand(client, current, feet, true)) { stands.add(feet); own = true; }
            }
            if (own) continue;
            Vec3 centre = Vec3.atCenterOf(target);
            boolean beside = false;
            if (open == null) open = collectionOpenSpace(client);
            for (BlockPos feet : BlockPos.betweenClosed(target.offset(-4, -5, -4), target.offset(4, 3, 4))) {
                // A cell on the far side of a wall or roof is not "beside" the
                // target for this Body: getting there means digging out (a
                // closed room's ceiling ore was broken from the roof and its
                // drop fell back into the room, GameTest collectOreInCeiling).
                if (Vec3.atBottomCenterOf(feet).add(0, 1.62, 0).distanceTo(centre) <= reach
                    && open.contains(feet) && collectionStand(client, current, feet, false)) {
                    stands.add(feet.immutable()); beside = true;
                }
            }
            if (!beside) unfloored.add(new GoalTwoBlocks(target));
        }
        // GoalComposite scores every member per node: keep the nearest cells.
        Vec3 at = client.player.position();
        List<Goal> goals = new ArrayList<>(unfloored);
        stands.stream()
            .sorted(java.util.Comparator.comparingDouble(feet -> Vec3.atBottomCenterOf(feet).distanceToSqr(at)))
            .limit(256).forEach(feet -> goals.add(new GoalBlock(feet)));
        current.goal = new GoalComposite(goals.toArray(Goal[]::new));
        baritone.getCustomGoalProcess().setGoalAndPath(current.goal);
    }

    /** Cells joined to the Body's feet through passable cells, within 32 blocks (bounded search). */
    private Set<BlockPos> collectionOpenSpace(Minecraft client) {
        var ctx = baritone.getPlayerContext();
        BlockPos start = client.player.blockPosition();
        Set<BlockPos> seen = new HashSet<>();
        java.util.ArrayDeque<BlockPos> queue = new java.util.ArrayDeque<>();
        seen.add(start);
        queue.add(start);
        while (!queue.isEmpty() && seen.size() < 32768) {
            BlockPos at = queue.poll();
            for (Direction side : Direction.values()) {
                BlockPos next = at.relative(side);
                if (next.distManhattan(start) > 32 || seen.contains(next) || !client.level.hasChunkAt(next)
                    || !MovementHelper.fullyPassable(ctx, next)) continue;
                seen.add(next);
                queue.add(next);
            }
        }
        return seen;
    }

    /** A standing cell: a floor that is not itself being collected, and (beside the target) room for the body. */
    private boolean collectionStand(Minecraft client, Run current, BlockPos feet, boolean diggable) {
        var ctx = baritone.getPlayerContext();
        BlockPos floor = feet.below();
        if (!client.level.hasChunkAt(floor) || current.collectionTargets.contains(floor)
            || !MovementHelper.canWalkOn(ctx, floor)) return false;
        for (BlockPos body : List.of(feet, feet.above())) {
            // The target's own cells may be dug into, as upstream GoalTwoBlocks does.
            if (!diggable && !MovementHelper.fullyPassable(ctx, body)) return false;
        }
        return true;
    }

    /** The nearest remaining target the Body can break from where it stands, never the block it stands on. */
    private BlockPos collectionReachable(Minecraft client, Run current) {
        if (!client.player.onGround()) return null;
        BlockPos support = client.player.getOnPos();
        Vec3 eye = client.player.getEyePosition();
        return current.collectionTargets.stream()
            .filter(pos -> !pos.equals(support) && !client.level.getBlockState(pos).isAir())
            .filter(pos -> RotationUtils.reachable(baritone.getPlayerContext(), pos).isPresent())
            .min(java.util.Comparator.comparingDouble(pos -> Vec3.atCenterOf(pos).distanceToSqr(eye)))
            .orElse(null);
    }

    /**
     * Phase 3: break targets in reach as a player does, without a route. Returns
     * false once nothing is left in reach, after handing the rest to a route.
     */
    private boolean tickCollectInPlace(Minecraft client, Run current) {
        BlockPos target = current.collectionBreaking;
        if (target == null || client.level.getBlockState(target).isAir()) {
            target = collectionReachable(client, current);
            if (target == null) {
                if (current.collectionBreaking != null) client.gameMode.stopDestroyBlock();
                current.collectionBreaking = null;
                if (current.phase == 3) {
                    current.phase = 1;
                    current.phaseTick = current.ticks;
                    if (current.collectionTargets.stream().anyMatch(pos -> !client.level.getBlockState(pos).isAir())) {
                        startCollectionGoal(client, current);
                        return true;
                    }
                }
                return false;
            }
            if (current.phase != 3) {
                baritone.getPathingBehavior().cancelEverything();
                baritone.getInputOverrideHandler().clearAllKeys();
                current.phase = 3;
            }
            current.collectionBreaking = target;
            current.collectionBreakTick = current.ticks;
            MovementHelper.switchToBestToolFor(baritone.getPlayerContext(), client.level.getBlockState(target));
        }
        if (current.ticks - current.collectionBreakTick > 600) {
            client.gameMode.stopDestroyBlock();
            requestStop("failed", "collection_block_not_breaking");
            return true;
        }
        Rotation look = RotationUtils.reachable(baritone.getPlayerContext(), target).orElse(null);
        if (look == null) {
            // Out of reach now (pushed, or the view closed): choose again next tick.
            client.gameMode.stopDestroyBlock();
            current.collectionBreaking = null;
            return true;
        }
        client.player.setYRot(look.getYaw());
        client.player.setXRot(look.getPitch());
        baritone.getLookBehavior().updateTarget(look, true);
        // Repeated continueDestroyBlock uses vanilla hardness, tool and server validation.
        client.gameMode.continueDestroyBlock(target, Direction.UP);
        client.player.swing(InteractionHand.MAIN_HAND);
        return true;
    }

    private static int collectionCount(Minecraft client, Run current) {
        return current.collectionItems.stream().mapToInt(item -> count(client, item)).sum();
    }

    private static final Direction[] SHELTER_SIDES = {Direction.NORTH, Direction.EAST, Direction.SOUTH, Direction.WEST};
    // Confirmed placements only, scoped to this client world and dimension.
    private final Map<String, String> shelterOwned = new LinkedHashMap<>();
    private Vec3 lastShelterHostile;

    private static String shelterKey(Minecraft client, BlockPos pos) {
        return client.level.dimension().identifier() + ":" + pos.asLong();
    }

    private static boolean shelterSolid(Minecraft client, BlockPos pos) {
        BlockState state = client.level.getBlockState(pos);
        return clientChunkLoaded(client, pos) && !state.canBeReplaced() && state.isCollisionShapeFullBlock(client.level, pos);
    }

    /** Feet walls, head walls, floor and center roof are the physical envelope. */
    private static List<BlockPos> shelterEnvelope(BlockPos center) {
        List<BlockPos> cells = new ArrayList<>();
        cells.add(center.below());
        for (Direction side : SHELTER_SIDES) {
            cells.add(center.relative(side));
            cells.add(center.above().relative(side));
        }
        cells.add(center.above(2));
        return cells;
    }

    private static JsonArray shelterOpenCells(Minecraft client, Run current) {
        JsonArray open = new JsonArray();
        for (BlockPos pos : shelterEnvelope(current.anchor)) if (!shelterSolid(client, pos)) open.add(xyz(pos));
        return open;
    }

    private static boolean shelterMaterial(Minecraft client, ItemStack stack) {
        if (stack.isEmpty() || !(stack.getItem() instanceof net.minecraft.world.item.BlockItem item)) return false;
        var block = item.getBlock();
        BlockState state = block.defaultBlockState();
        // A shell needs full cubes that stay put, rather than plants, slabs or falling blocks.
        return !(block instanceof net.minecraft.world.level.block.FallingBlock)
            && !state.canBeReplaced() && state.isCollisionShapeFullBlock(client.level, client.player.blockPosition())
            && !state.hasBlockEntity();
    }

    private static BlockHitResult shelterSupport(Minecraft client, BlockPos target) {
        for (Direction face : Direction.values()) {
            BlockPos support = target.relative(face.getOpposite());
            if (!shelterSolid(client, support)) continue;
            Vec3 hit = Vec3.atCenterOf(support).add(Vec3.atLowerCornerOf(face.getUnitVec3i()).scale(0.5));
            return new BlockHitResult(hit, face, support, false);
        }
        return null;
    }

    private static Mob shelterMob(Minecraft client, BlockPos pos) {
        return client.level.getEntities(client.player, new net.minecraft.world.phys.AABB(pos),
            entity -> entity instanceof Mob && entity.isAlive()).stream().map(entity -> (Mob) entity)
            .min(java.util.Comparator.comparingDouble(client.player::distanceToSqr)).orElse(null);
    }

    private void shelterFail(Minecraft client, Run current, BlockPos blocked) {
        if (blocked != null) {
            JsonObject cell = new JsonObject();
            cell.add("pos", xyz(blocked));
            Mob mob = shelterMob(client, blocked);
            if (mob != null) {
                cell.addProperty("mob", BuiltInRegistries.ENTITY_TYPE.getKey(mob.getType()).getPath());
                cell.addProperty("name", mob.getName().getString());
                cell.addProperty("id", mob.getUUID().toString());
            }
            current.shelterFacts.getAsJsonArray("blocked_cells").add(cell);
        }
        current.shelterFacts.add("open_cells", shelterOpenCells(client, current));
        current.shelterFacts.addProperty("message", "The shelter has open cells.");
        requestStop("failed", "shelter_unsealable");
    }

    private boolean shelterAllowed(Run current, BlockPos pos, String item) {
        if (!current.params.has("allowed_placements")) return true; // direct native IPC caller
        for (var raw : current.params.getAsJsonArray("allowed_placements")) {
            JsonObject entry = raw.getAsJsonObject();
            if (!pos.equals(blockPosition(entry, "pos"))) continue;
            for (var allowed : entry.getAsJsonArray("items")) if (item.equals(allowed.getAsString().replace("minecraft:", ""))) return true;
        }
        return false;
    }

    private String shelterItem(Minecraft client, Run current, BlockPos pos) {
        for (int slot = 0; slot < client.player.getInventory().getContainerSize(); slot++) {
            ItemStack stack = client.player.getInventory().getItem(slot);
            if (shelterMaterial(client, stack) && shelterAllowed(current, pos, itemPath(stack))) return itemPath(stack);
        }
        return "";
    }

    private static void shelterSelectItem(Minecraft client, String item) {
        boolean inMainInventory = false;
        for (int slot = 0; slot < 36; slot++) inMainInventory |= itemPath(client.player.getInventory().getItem(slot)).equals(item);
        if (!inMainInventory && itemPath(client.player.getOffhandItem()).equals(item)) {
            closeLeftoverMenu(client);
            if (!(client.player.containerMenu instanceof InventoryMenu menu) || !menu.getCarried().isEmpty())
                throw new IllegalArgumentException("inventory_menu_unavailable");
            click(menu, menuSlotForPlayer(menu, 40), borrowedHotbarSlot(client), ContainerInput.SWAP, client);
        }
        selectItem(client, item);
    }

    private void tickShelter(Minecraft client, Run current) {
        if (current.phase == 0) {
            String operation = string(current.params, "action", "seal");
            if (!operation.equals("seal") && !operation.equals("look_out")) throw new IllegalArgumentException("invalid_shelter_action");
            current.anchor = positionParam(current.params, "center");
            if (current.anchor == null) current.anchor = client.player.blockPosition().immutable();
            current.shelterFacts.add("center", xyz(current.anchor));
            current.shelterFacts.add("blocked_cells", new JsonArray());
            current.shelterFacts.add("seen", new JsonArray());
            if (!client.player.blockPosition().equals(current.anchor)) { shelterDisplaced(client, current); return; }
            if (operation.equals("look_out")) {
                beginShelterPort(client, current);
                return;
            }
            // Capture the last visible hostile before the walls obscure it.
            client.level.getEntities(client.player, client.player.getBoundingBox().inflate(32),
                entity -> hostile(entity) && hasLineOfSight(client, entity)).stream()
                .min(java.util.Comparator.comparingDouble(client.player::distanceToSqr))
                .ifPresent(entity -> lastShelterHostile = entity.position());
            List<Direction> sides = new ArrayList<>(List.of(SHELTER_SIDES));
            Vec3 hostilePosition = lastShelterHostile;
            if (hostilePosition != null) sides.sort(java.util.Comparator.comparingDouble(side ->
                Vec3.atCenterOf(current.anchor.relative(side)).distanceToSqr(hostilePosition)));
            // Dependency order: optional floor/foundations, feet before head, rim before roof.
            if (!shelterSolid(client, current.anchor.below())) current.shelterPlan.add(current.anchor.below());
            for (Direction side : sides) {
                BlockPos feet = current.anchor.relative(side);
                if (!shelterSolid(client, feet) && shelterSupport(client, feet) == null) current.shelterPlan.add(feet.below());
            }
            for (Direction side : sides) {
                current.shelterPlan.add(current.anchor.relative(side));
                current.shelterPlan.add(current.anchor.above().relative(side));
            }
            if (!shelterSolid(client, current.anchor.above(2))) {
                if (shelterSupport(client, current.anchor.above(2)) == null) current.shelterPlan.add(current.anchor.above(2).north());
                current.shelterPlan.add(current.anchor.above(2));
            }
            int needed = (int) current.shelterPlan.stream().filter(pos -> !shelterSolid(client, pos)).count();
            int held = 0;
            for (int slot = 0; slot < client.player.getInventory().getContainerSize(); slot++) {
                ItemStack stack = client.player.getInventory().getItem(slot);
                if (shelterMaterial(client, stack)) held += stack.getCount();
            }
            current.shelterFacts.addProperty("blocks_needed", needed);
            current.shelterFacts.addProperty("blocks_held", held);
            if (held < needed) {
                current.shelterFacts.addProperty("message", "The shell needs " + needed + " blocks; the inventory holds " + held + " placeable blocks.");
                requestStop("failed", "not_enough_blocks");
                return;
            }
            current.phase = 1;
        }
        if (!client.player.blockPosition().equals(current.anchor)) { shelterDisplaced(client, current); return; }
        if (current.phase >= 10) { tickShelterPort(client, current); return; }
        if (current.phase == 1) {
            while (current.shelterIndex < current.shelterPlan.size()
                && shelterSolid(client, current.shelterPlan.get(current.shelterIndex))) current.shelterIndex++;
            if (current.shelterIndex == current.shelterPlan.size()) {
                JsonArray open = shelterOpenCells(client, current); // success requires a fresh world read of EVERY envelope cell
                current.shelterFacts.add("open_cells", open);
                if (!open.isEmpty()) { shelterFail(client, current, null); return; }
                current.shelterFacts.addProperty("message", "Every wall, the roof and the floor are solid.");
                requestStop("completed", "shelter_sealed");
                return;
            }
            current.shelterTarget = current.shelterPlan.get(current.shelterIndex);
            current.item = shelterItem(client, current, current.shelterTarget);
            current.shelterHit = false;
            current.shelterRetried = false;
            current.phase = 2;
            current.phaseTick = current.ticks;
        }
        if (current.phase == 2) {
            if (!shelterPreparePlacement(client, current)) return;
            current.phase = 3;
            current.phaseTick = current.ticks;
            return;
        }
        if (current.phase == 3 && current.ticks - current.phaseTick >= 3) shelterPlace(client, current, 4);
        if (current.phase == 4) {
            if (current.placementConfirmed && shelterSolid(client, current.shelterTarget)) {
                current.shelterIndex++;
                current.phase = 1;
            } else if (current.ticks - current.phaseTick >= 20) {
                Mob mob = shelterMob(client, current.shelterTarget);
                if (!current.shelterRetried && mob != null) current.phase = 5;
                else shelterFail(client, current, current.shelterTarget);
            }
        }
        if (current.phase == 5 && shelterClearMobOnce(client, current)) {
            current.shelterRetried = true;
            current.phase = 2;
        }
    }

    private void tickRaiseShield(Minecraft client, Run current) {
        if (current.phase == 0) {
            String targetUuid = string(current.params, "target_uuid", "");
            try {
                java.util.UUID uuid = targetUuid.isBlank() ? null : java.util.UUID.fromString(targetUuid);
                current.targetEntity = uuid == null ? null : client.level.getEntities(
                    client.player, client.player.getBoundingBox().inflate(64.0), entity -> entity.getUUID().equals(uuid)
                ).stream().findFirst().orElse(null);
            } catch (IllegalArgumentException ignored) {
                current.targetEntity = null;
            }
            if (!(current.targetEntity instanceof LivingEntity)) throw new IllegalArgumentException("target_not_found");
            if (!BuiltInRegistries.ITEM.getKey(client.player.getOffhandItem().getItem()).getPath().equals("shield"))
                throw new IllegalArgumentException("no_shield");
            current.initialHealth = client.player.getHealth();
            current.shieldLastHurtTime = client.player.hurtTime;
            current.shieldDuration = Math.max(1, Math.min(1200, integer(current.params, "duration_ticks", 100)));
            current.phase = 1;
            current.phaseTick = current.ticks;
            lookAtEntity(client, current.targetEntity);
        }
        if (current.targetEntity == null || !current.targetEntity.isAlive()) {
            requestStop("completed", "target_gone");
            return;
        }
        lookAtEntity(client, current.targetEntity);
        if (!client.player.isUsingItem()) client.gameMode.useItem(client.player, InteractionHand.OFF_HAND);
        client.options.keyUse.setDown(true);
        if (!client.player.isUsingItem()) { requestStop("failed", "shield_use_not_started"); return; }
        if (client.player.isBlocking()) current.shieldBlockingTicks++;
        if (client.player.hurtTime > 0 && current.shieldLastHurtTime == 0) {
            // Damage got through; a fully blocked hit is counted by observeShieldBlock.
            current.shieldHitsTaken++;
        }
        current.shieldLastHurtTime = client.player.hurtTime;
        if (current.ticks - current.phaseTick >= current.shieldDuration) {
            requestStop("completed", "shield_duration_elapsed");
        }
    }

    /** Count a shield-blocked hit on the local player while raiseShield runs. */
    public static void observeShieldBlock(String sound, double x, double y, double z) {
        MineBotClientBody body = instance;
        Minecraft client = Minecraft.getInstance();
        if (body == null || body.run == null || client.player == null || !body.run.kind.equals("raiseShield")
            || !sound.equals("item.shield.block") || client.player.distanceToSqr(x, y, z) > 2.25) return;
        body.run.shieldHitsBlocked++;
    }

    /**
     * The column to pillar on: the cell the Body stands in when it has a floor
     * and room for feet and head; otherwise the nearest such neighbour at the
     * same level (a Body on an edge stands over its neighbour's floor); null
     * when none of the nine qualifies.
     */
    private static BlockPos pillarColumn(Minecraft client) {
        BlockPos feet = client.player.blockPosition();
        BlockPos best = null;
        double bestDistance = Double.MAX_VALUE;
        for (int dx = -1; dx <= 1; dx++) for (int dz = -1; dz <= 1; dz++) {
            BlockPos cell = feet.offset(dx, 0, dz);
            if (!clearOfHazard(client, cell) || !clearOfHazard(client, cell.above()) || !groundUnder(client, cell)) continue;
            double distance = client.player.position().distanceToSqr(cell.getX() + 0.5, client.player.getY(), cell.getZ() + 0.5);
            if (distance < bestDistance) {
                best = cell;
                bestDistance = distance;
            }
        }
        return best;
    }

    private void tickPillarUp(Minecraft client, Run current) {
        if (current.phase == 0) {
            current.pillarBlocks = Math.max(1, Math.min(16, integer(current.params, "blocks", 3)));
            BlockPos column = pillarColumn(client);
            // Standing on a slab or other partial block: the cell it stands in, as before.
            if (column == null) column = client.player.blockPosition();
            current.pillarX = column.getX();
            current.pillarZ = column.getZ();
            current.pillarStartY = column.getY();
            JsonArray materials = current.params.has("scaffold_blocks")
                ? current.params.getAsJsonArray("scaffold_blocks") : new JsonArray();
            for (int slot = 0; slot < 36 && current.item.isEmpty(); slot++) {
                ItemStack stack = client.player.getInventory().getItem(slot);
                String path = itemPath(stack);
                for (var material : materials) {
                    if (!stack.isEmpty() && path.equals(material.getAsString().replace("minecraft:", ""))) {
                        current.item = path;
                        break;
                    }
                }
            }
            if (current.item.isEmpty() || count(client, current.item) < current.pillarBlocks) {
                requestStop("failed", "not_enough_blocks"); return;
            }
            current.phase = 4;
            current.phaseTick = current.ticks;
        }
        double dx = client.player.getX() - (current.pillarX + 0.5);
        double dz = client.player.getZ() - (current.pillarZ + 0.5);
        if (current.phase == 4) {
            // First onto the centre of the column, as a player shuffles onto
            // it before jumping: crouched, so the step cannot carry it off an
            // edge. End run-11: started 0.4 off centre on uneven ground, three
            // pillars ended displaced at once with nothing raised.
            Vec3 motion = client.player.getDeltaMovement();
            boolean still = motion.x * motion.x + motion.z * motion.z < 1.0E-5;
            if (still && client.player.onGround() && dx * dx + dz * dz <= 0.04) {
                // Standing still within 0.2 of the centre: start (displaced is 0.3).
                current.phase = 1;
                client.player.setXRot(90.0F);
                selectItem(client, current.item);
                return;
            }
            if (dx * dx + dz * dz <= 0.01) {
                // Near the centre: let go and wait until the Body stands still.
                client.options.keyUp.setDown(false);
                client.options.keyShift.setDown(false);
                return;
            }
            if (current.ticks - current.phaseTick > 60) {
                client.options.keyUp.setDown(false);
                client.options.keyShift.setDown(false);
                requestStop("failed", "could_not_reach_column_centre");
                return;
            }
            client.player.setYRot((float) (Math.atan2(-dz, -dx) * 180.0 / Math.PI) - 90.0f);
            client.options.keyShift.setDown(true);
            client.options.keyUp.setDown(true);
            return;
        }
        if (dx * dx + dz * dz > 0.09) {
            double top = current.pillarStartY + current.pillarRaised;
            // In the air above the column (a jump, a hit mid-jump): land first.
            if (!client.player.onGround() && client.player.getY() >= top - 0.5) {
                client.options.keyJump.setDown(false);
                return;
            }
            // Shoved but still standing on the column top: back to the centre
            // and go on, as a player shuffles back. End run-16 21:15:02: a 1 HP
            // hit moved the Body 1.4 blocks and the column stopped at 10 of 15.
            if (client.player.onGround() && Math.abs(client.player.getY() - top) < 0.6
                && Math.abs(dx) < 1.0 && Math.abs(dz) < 1.0) {
                client.options.keyJump.setDown(false);
                current.pillarRecentred++;
                current.phase = 4;
                current.phaseTick = current.ticks;
                return;
            }
            // Knocked off the column: said with the column, so a next call can go on with it.
            requestStop("failed", "displaced");
            return;
        }
        if (current.pillarRaised >= current.pillarBlocks) {
            if (client.player.onGround() && client.player.getY() >= current.pillarStartY + current.pillarBlocks - 0.01) {
                requestStop("completed", "pillar_height_reached");
            }
            return;
        }
        BlockPos target = new BlockPos(current.pillarX, current.pillarStartY + current.pillarRaised, current.pillarZ);
        BlockPos head = target.above(2);
        if (!client.level.getBlockState(head).getCollisionShape(client.level, head).isEmpty()) {
            requestStop("failed", "blocked_above"); return;
        }
        if (!client.level.getBlockState(target).canBeReplaced()) {
            requestStop("failed", "placement_blocked"); return;
        }
        selectItem(client, current.item);
        client.player.setXRot(90.0F);
        if (client.player.onGround()) client.options.keyJump.setDown(true);
        else client.options.keyJump.setDown(false);
        if (client.player.getY() >= current.pillarStartY + current.pillarRaised + 0.75) {
            BlockPos support = target.below();
            Vec3 hit = Vec3.atCenterOf(support).add(0, 0.5, 0);
            BlockHitResult placement = new BlockHitResult(hit, Direction.UP, support, false);
            client.gameMode.useItemOn(client.player, InteractionHand.MAIN_HAND, placement);
            if (BuiltInRegistries.BLOCK.getKey(client.level.getBlockState(target).getBlock()).getPath().equals(current.item)) {
                current.pillarRaised++;
                current.phaseTick = current.ticks;
                client.options.keyJump.setDown(false);
            }
        }
    }

    private void tickPlaceBedAndSleep(Minecraft client, Run current) {
        if (current.phase == 0) {
            current.item = findBedItem(client);
            if (current.item.isEmpty()) throw new IllegalArgumentException("bed_not_in_inventory");
            current.bedInventoryBefore = count(client, current.item);
            current.bedMessageSequenceBefore = lastGameMessageSequence;
            // The side the Body faces first, then the others, as a player turns
            // round to find room: trying only the facing cell failed twice in a
            // row beside a wall in H6 (2026-10-29) while open ground lay behind.
            Direction facing = client.player.getDirection();
            current.anchor = null;
            current.bedBlockedCells = new JsonArray();
            for (Direction side : new Direction[]{facing, facing.getClockWise(), facing.getOpposite(), facing.getCounterClockWise()}) {
                BlockPos foot = client.player.blockPosition().relative(side);
                BlockPos head = foot.relative(side);
                String blocked = !client.level.getBlockState(foot).canBeReplaced() ? "foot_cell_occupied"
                    : !client.level.getBlockState(head).canBeReplaced() ? "head_cell_occupied"
                    : client.level.getBlockState(foot.below()).getCollisionShape(client.level, foot.below()).isEmpty() ? "no_support_below"
                    : null;
                if (blocked == null) { current.anchor = foot; break; }
                JsonObject cell = new JsonObject();
                cell.addProperty("side", side.getSerializedName());
                cell.add("pos", xyz(foot));
                cell.addProperty("blocked", blocked);
                current.bedBlockedCells.add(cell);
            }
            if (current.anchor == null) throw new IllegalArgumentException("bed_placement_space_blocked");
            selectItem(client, current.item);
            BlockPos support = current.anchor.below();
            Vec3 hit = Vec3.atCenterOf(support).add(0, 0.5, 0);
            lookAt(client, lookParams(hit));
            current.placementHit = new BlockHitResult(hit, Direction.UP, support, false);
            current.phase = 1;
            current.phaseTick = current.ticks;
            return;
        }
        if (current.phase == 1) {
            if (current.ticks - current.phaseTick < 3) return;
            selectItem(client, current.item);
            if (!client.gameMode.useItemOn(client.player, InteractionHand.MAIN_HAND, current.placementHit).consumesAction()) {
                throw new IllegalArgumentException("bed_placement_rejected");
            }
            current.phase = 2;
            current.phaseTick = current.ticks;
            return;
        }
        if (current.phase == 2) {
            if (isBedBlock(client, current.anchor)) {
                current.bedPlaced = true;
                current.phase = 3;
                current.phaseTick = current.ticks;
                current.bedMessageSequenceBefore = lastGameMessageSequence;
                lookAt(client, lookParams(Vec3.atCenterOf(current.anchor)));
                client.gameMode.useItemOn(client.player, InteractionHand.MAIN_HAND,
                    new BlockHitResult(Vec3.atCenterOf(current.anchor), Direction.UP, current.anchor, false));
            } else if (current.ticks - current.phaseTick > 40) {
                throw new IllegalArgumentException("bed_placement_unconfirmed");
            }
            return;
        }
        if (current.phase == 3) {
            if (client.player.isSleeping()) {
                current.phase = 4;
                return;
            }
            // The integrated or a remote server may answer the use a second later.
            if (current.ticks - current.phaseTick > 60) {
                current.bedSleepReason = "cannot_sleep_now";
                current.bedGameMessage = lastGameMessageSequence > current.bedMessageSequenceBefore ? lastGameMessage : "";
                beginBedBreak(client, current);
            }
            return;
        }
        if (current.phase == 4) {
            if (!client.player.isSleeping()) beginBedBreak(client, current);
            return;
        }
        if (current.phase == 5) {
            BlockPos bed = current.anchor;
            if (!isBedBlock(client, bed)) {
                client.gameMode.stopDestroyBlock();
                current.phase = 6;
                current.phaseTick = current.ticks;
                return;
            }
            if (current.ticks - current.phaseTick > 100) {
                client.gameMode.stopDestroyBlock();
                requestStop("failed", "bed_not_recovered");
                return;
            }
            // Waking moves the Body beside the bed; the held attack key acts on the
            // crosshair, so face the bed every tick or the two fight over the block.
            lookAt(client, lookParams(Vec3.atCenterOf(bed)));
            client.options.keyAttack.setDown(true);
            client.gameMode.continueDestroyBlock(bed, Direction.UP);
            return;
        }
        if (current.phase == 6) {
            if (count(client, current.item) >= current.bedInventoryBefore) {
                baritone.getPathingBehavior().cancelEverything();
                current.bedRecovered = true;
                requestStop(current.bedSleepReason.isEmpty() ? "completed" : "failed",
                    current.bedSleepReason.isEmpty() ? "bed_recovered_after_sleep" : current.bedSleepReason);
            } else if (current.ticks - current.phaseTick > 200) {
                baritone.getPathingBehavior().cancelEverything();
                requestStop("failed", "bed_not_recovered");
            } else if (current.ticks - current.phaseTick == 10) {
                // The dropped bed can land out of pickup reach; walk over to it like a player.
                String item = current.item;
                client.level.getEntities(client.player, client.player.getBoundingBox().inflate(6),
                        entity -> entity instanceof ItemEntity drop && itemPath(drop.getItem()).equals(item)).stream()
                    .min(java.util.Comparator.comparingDouble(client.player::distanceToSqr))
                    .ifPresent(drop -> baritone.getCustomGoalProcess().setGoalAndPath(
                        new baritone.api.pathing.goals.GoalBlock(drop.blockPosition())));
            }
        }
    }

    private void beginBedBreak(Minecraft client, Run current) {
        current.phase = 5;
        current.phaseTick = current.ticks;
        lookAt(client, lookParams(Vec3.atCenterOf(current.anchor)));
        client.gameMode.startDestroyBlock(current.anchor, Direction.UP);
    }

    private static String findBedItem(Minecraft client) {
        for (int slot = 0; slot < 36; slot++) {
            ItemStack stack = client.player.getInventory().getItem(slot);
            String item = itemPath(stack);
            if (!stack.isEmpty() && item.endsWith("_bed")) return item;
        }
        return "";
    }

    private static boolean isBedBlock(Minecraft client, BlockPos pos) {
        return BuiltInRegistries.BLOCK.getKey(client.level.getBlockState(pos).getBlock()).getPath().endsWith("_bed");
    }

    private void shelterDisplaced(Minecraft client, Run current) {
        current.shelterFacts.add("open_cells", shelterOpenCells(client, current));
        current.shelterFacts.addProperty("message", "The Body left the shelter center cell.");
        requestStop("failed", "displaced");
    }

    private boolean shelterPreparePlacement(Minecraft client, Run current) {
        if (current.item.isEmpty() || !client.level.getBlockState(current.shelterTarget).canBeReplaced()) {
            shelterFail(client, current, current.shelterTarget); return false;
        }
        current.placementHit = shelterSupport(client, current.shelterTarget);
        if (current.placementHit == null) { shelterFail(client, current, current.shelterTarget); return false; }
        shelterSelectItem(client, current.item);
        lookAt(client, lookParams(current.placementHit.getLocation()));
        return true;
    }

    /** After a refused placement, one vanilla cooldown/LOS constrained hit, then one retry. */
    private boolean shelterClearMobOnce(Minecraft client, Run current) {
        Mob mob = shelterMob(client, current.shelterTarget);
        if (mob != null && !current.shelterHit) {
            if (mob instanceof net.minecraft.world.entity.monster.Creeper || !hasLineOfSight(client, mob)
                || client.player.distanceTo(mob) > 3.0) { shelterFail(client, current, current.shelterTarget); return false; }
            holdBestMeleeWeapon(client);
            if (client.player.getAttackStrengthScale(0) < 0.9f) return false;
            lookAtEntity(client, mob);
            client.gameMode.attack(client.player, mob); // at most ONE normal melee hit for this cell
            client.player.resetAttackStrengthTicker();
            current.attacks++;
            current.shelterHit = true;
        }
        return true;
    }

    private void shelterPlace(Minecraft client, Run current, int nextPhase) {
        shelterSelectItem(client, current.item);
        current.placementConfirmed = false;
        touchWorld(current.shelterTarget);
        client.gameMode.useItemOn(client.player, InteractionHand.MAIN_HAND, current.placementHit);
        current.phase = nextPhase;
        current.phaseTick = current.ticks;
    }

    private void beginShelterPort(Minecraft client, Run current) {
        Direction side;
        if (current.params.has("side")) {
            side = Direction.byName(string(current.params, "side", ""));
            if (side == null || side.getAxis().isVertical()) throw new IllegalArgumentException("invalid_shelter_side");
        } else {
            Vec3 target = lastShelterHostile;
            side = target == null ? client.player.getDirection() : Direction.getApproximateNearest(
                target.x - client.player.getX(), 0, target.z - client.player.getZ());
        }
        current.shelterTarget = current.anchor.above().relative(side);
        current.shelterFacts.add("port", xyz(current.shelterTarget));
        current.shelterFacts.addProperty("port_closed", shelterSolid(client, current.shelterTarget));
        current.item = BuiltInRegistries.BLOCK.getKey(client.level.getBlockState(current.shelterTarget).getBlock()).getPath();
        String owned = current.params.has("owned_blocks") ? null : shelterOwned.get(shelterKey(client, current.shelterTarget));
        if (current.params.has("owned_blocks")) for (var raw : current.params.getAsJsonArray("owned_blocks")) {
            JsonObject entry = raw.getAsJsonObject();
            if (current.shelterTarget.equals(blockPosition(entry, "pos"))) owned = string(entry, "block_type", "");
        }
        if (!current.item.equals(owned) || !shelterSolid(client, current.shelterTarget)) {
            current.shelterFacts.addProperty("message", "The head-height port is not an owned solid block.");
            requestStop("failed", "port_not_owned"); return;
        }
        if (!booleanValue(current.params, "keep_open", false) && (count(client, current.item) < 1
            || !shelterAllowed(current, current.shelterTarget, current.item))) {
            current.shelterFacts.addProperty("blocks_needed", 1);
            current.shelterFacts.addProperty("blocks_held", count(client, current.item));
            current.shelterFacts.addProperty("message", "Closing the port needs 1 matching block; the inventory holds " + count(client, current.item) + ".");
            requestStop("failed", "not_enough_blocks"); return;
        }
        current.phase = 10;
        current.phaseTick = current.ticks;
        touchWorld(current.shelterTarget);
    }

    private void tickShelterPort(Minecraft client, Run current) {
        if (current.phase == 10) {
            // Normal survival mining; never delete a server block or the feet wall.
            lookAt(client, lookParams(Vec3.atCenterOf(current.shelterTarget)));
            if (client.level.getBlockState(current.shelterTarget).isAir() && current.shelterPortOpened) {
                client.gameMode.stopDestroyBlock();
                current.shelterFacts.addProperty("port_closed", false);
                JsonArray seen = new JsonArray();
                Vec3 from = client.player.getEyePosition();
                for (Entity entity : client.level.getEntities(client.player, client.player.getBoundingBox().inflate(32), value -> hostile(value))) {
                    Vec3 to = entity.getEyePosition();
                    if (new net.minecraft.world.phys.AABB(current.shelterTarget).clip(from, to).isEmpty() || !hasLineOfSight(client, entity)) continue;
                    JsonObject fact = new JsonObject();
                    fact.addProperty("type", BuiltInRegistries.ENTITY_TYPE.getKey(entity.getType()).getPath());
                    fact.addProperty("name", entity.getName().getString());
                    fact.addProperty("id", entity.getUUID().toString());
                    fact.addProperty("distance", client.player.distanceTo(entity));
                    seen.add(fact);
                    lastShelterHostile = entity.position();
                }
                current.shelterFacts.add("seen", seen);
                if (booleanValue(current.params, "keep_open", false)) {
                    current.shelterFacts.addProperty("message", "The head-height port was opened and left open.");
                    requestStop("completed", "looked_out"); return;
                }
                current.phase = 11;
                return;
            }
            BlockState port = client.level.getBlockState(current.shelterTarget);
            if (!port.isAir() && !BuiltInRegistries.BLOCK.getKey(port.getBlock()).getPath().equals(current.item)) {
                shelterFail(client, current, current.shelterTarget); return;
            }
            if (current.ticks - current.phaseTick > 400) { shelterFail(client, current, current.shelterTarget); return; }
            // Repeated continueDestroyBlock uses vanilla hardness, tool and server validation.
            client.gameMode.continueDestroyBlock(current.shelterTarget, Direction.UP);
            return;
        }
        if (current.phase == 11) {
            if (!shelterPreparePlacement(client, current)) return;
            current.phase = 12;
            current.phaseTick = current.ticks;
            return;
        }
        if (current.phase == 12 && current.ticks - current.phaseTick >= 3) shelterPlace(client, current, 13);
        if (current.phase == 13) {
            boolean closed = current.placementConfirmed && shelterSolid(client, current.shelterTarget)
                && BuiltInRegistries.BLOCK.getKey(client.level.getBlockState(current.shelterTarget).getBlock()).getPath().equals(current.item);
            current.shelterFacts.addProperty("port_closed", closed);
            if (closed) {
                current.shelterFacts.addProperty("message", "The head-height port was opened, observed and closed with the same block type.");
                requestStop("completed", "looked_out");
            } else if (current.ticks - current.phaseTick >= 20) {
                if (!current.shelterRetried && shelterMob(client, current.shelterTarget) != null) current.phase = 14;
                else shelterFail(client, current, current.shelterTarget);
            }
        }
        if (current.phase == 14 && shelterClearMobOnce(client, current)) {
            current.shelterRetried = true;
            current.phase = 11;
        }
    }

    private void tickPlaceBlock(Minecraft client, Run current) {
        if (current.phase == 0) {
            current.anchor = blockPosition(current.params, "target");
            current.item = string(current.params, "block_type", "").replace("minecraft:", "");
            if (current.item.isBlank()) throw new IllegalArgumentException("missing_block_type");
            if (!client.level.getBlockState(current.anchor).canBeReplaced()) throw new IllegalArgumentException("target_occupied");
            selectItem(client, current.item);
            current.goal = new GoalNear(current.anchor, 3);
            baritone.getCustomGoalProcess().setGoalAndPath(current.goal);
            current.phase = 1;
        }
        if (current.phase == 1) {
            if (client.player.position().distanceToSqr(Vec3.atCenterOf(current.anchor)) > 16 || !client.player.onGround()) return;
            baritone.getPathingBehavior().cancelEverything();
            Direction face = null;
            BlockPos support = null;
            String wantedFace = string(current.params, "face", "auto");
            for (Direction candidate : Direction.values()) {
                if (!wantedFace.equals("auto") && !candidate.getName().equals(wantedFace)) continue;
                BlockPos neighbor = current.anchor.relative(candidate.getOpposite());
                if (!client.level.getBlockState(neighbor).getCollisionShape(client.level, neighbor).isEmpty()) {
                    face = candidate; support = neighbor; break;
                }
            }
            if (support == null) throw new IllegalArgumentException("placement_support_missing");
            Vec3 hit = Vec3.atCenterOf(support).add(Vec3.atLowerCornerOf(face.getUnitVec3i()).scale(0.5));
            lookAt(client, lookParams(hit));
            current.placementHit = new BlockHitResult(hit, face, support, false);
            current.phase = 2;
            current.phaseTick = current.ticks;
            return;
        }
        if (current.phase == 2 && current.ticks - current.phaseTick >= 3) {
            if (!client.player.isWithinBlockInteractionRange(current.placementHit.getBlockPos(), 0)) throw new IllegalArgumentException("target_out_of_range");
            selectItem(client, current.item);
            if (!client.gameMode.useItemOn(client.player, InteractionHand.MAIN_HAND, current.placementHit).consumesAction()) {
                throw new IllegalArgumentException("block_place_rejected");
            }
            current.phase = 3;
            current.phaseTick = current.ticks;
        }
        if (current.phase == 3 && current.placementConfirmed) requestStop("completed", "placed_server_confirmed");
        else if (current.phase == 3 && current.ticks - current.phaseTick > 40) requestStop("failed", "placement_unconfirmed");
    }

    private static JsonObject lookParams(Vec3 target) {
        JsonObject params = new JsonObject();
        JsonArray xyz = new JsonArray(); xyz.add(target.x); xyz.add(target.y); xyz.add(target.z);
        params.add("target", xyz); return params;
    }

    private void tickUseItem(Minecraft client, Run current) {
        String mode = string(current.params, "mode", "once");
        int duration = integer(current.params, "ticks", 1);
        if ((!mode.equals("once") && !mode.equals("continuous")) || duration < 1) throw new IllegalArgumentException("invalid_use_duration_or_mode");
        // Where to look while using it, when the caller says: an empty bucket
        // takes the water source it is aimed at.
        if (current.params.has("look_at") && current.params.get("look_at").isJsonArray()) {
            JsonArray at = current.params.getAsJsonArray("look_at");
            Vec3 eye = client.player.getEyePosition();
            double dx = at.get(0).getAsDouble() - eye.x, dy = at.get(1).getAsDouble() - eye.y, dz = at.get(2).getAsDouble() - eye.z;
            client.player.setYRot((float) (Math.atan2(dz, dx) * 180.0 / Math.PI) - 90.0f);
            client.player.setXRot((float) -(Math.atan2(dy, Math.sqrt(dx * dx + dz * dz)) * 180.0 / Math.PI));
        }
        if (current.phase == 0) {
            if (!current.item.isBlank()) selectItem(client, current.item);
            else current.item = itemPath(client.player.getMainHandItem());
            current.before = count(client, current.item);
            current.phaseTick = current.ticks; current.phase = 1;
            return;
        }
        if (mode.equals("once")) {
            if (current.phase == 1 && current.ticks - current.phaseTick >= 3) {
                // Never use whatever the hand holds instead: the right click
                // would apply the other item and report nothing.
                if (!itemPath(client.player.getMainHandItem()).equals(current.item)) { requestStop("failed", "held_item_changed"); return; }
                InteractionResult result = InteractionResult.PASS;
                if (client.hitResult instanceof EntityHitResult hit && client.player.distanceToSqr(hit.getEntity()) <= 25) {
                    result = client.gameMode.interact(client.player, hit.getEntity(), hit, InteractionHand.MAIN_HAND);
                } else if (client.hitResult instanceof BlockHitResult hit && hit.getType() == HitResult.Type.BLOCK
                    && client.player.isWithinBlockInteractionRange(hit.getBlockPos(), 0)) {
                    result = client.gameMode.useItemOn(client.player, InteractionHand.MAIN_HAND, hit);
                }
                if (!result.consumesAction()) client.gameMode.useItem(client.player, InteractionHand.MAIN_HAND);
                current.phase = 2; current.phaseTick = current.ticks;
            }
            if (current.phase == 2 && current.ticks - current.phaseTick >= 6) requestStop("completed", "interaction_applied");
            return;
        }
        if (count(client, current.item) < current.before) { requestStop("completed", "item_consumed"); return; }
        if (current.ticks - current.phaseTick >= duration) { requestStop("completed", "use_duration_elapsed"); return; }
        if (!itemPath(client.player.getMainHandItem()).equals(current.item)) { requestStop("failed", "held_item_changed"); return; }
        client.options.keyUse.setDown(true);
        if (!client.player.isUsingItem()) client.gameMode.useItem(client.player, InteractionHand.MAIN_HAND);
    }

    private void tickEngage(Minecraft client, Run current) {
        if (current.bodyHealthStart < 0) current.bodyHealthStart = client.player.getHealth();
        if (current.targetEntity instanceof LivingEntity previous
            && (!previous.isAlive() || previous.getHealth() <= 0.0f)) {
            current.kills++;
            // A fight is with the mobs at hand, not one of them (body-tool-boundaries-20261110:
            // in run-13 a second zombie's blow re-bound the fight to it and cut the first
            // one short, 9 times). Then the next hostile within reach of a few steps.
            LivingEntity next = booleanValue(current.params, "sweep", true) ? nextAtHand(client, previous) : null;
            if (next == null) {
                kiteKeys(client, false, false, false);
                requestStop("completed", "killed");
                return;
            }
            current.sweepTarget = next;
            current.targetEntity = next;
            current.initialHealth = next.getHealth();
            current.engageIdleTicks = 0;
        }
        LivingEntity target = current.sweepTarget != null && current.sweepTarget.isAlive()
            ? current.sweepTarget : combatTarget(client, current);
        if (target == null) {
            requestStop("failed", current.targetEntity == null ? "target_not_found" : "target_lost");
            return;
        }
        current.targetEntity = target;
        if (current.initialHealth < 0) current.initialHealth = target.getHealth();
        if (!current.engageWayDecided) {
            current.engageWayDecided = true;
            // A fight walks to a mob farther than a few steps; it does not tunnel to
            // it. run-14 02:15: an engage dug through stone after a creeper 15 blocks
            // away into a cave with three creepers. Within ENGAGE_DIG_REACH a player
            // does dig the block or two in the way: walk-only, run-15 02:58 ended
            // "no_route_found" three times at a zombie 4 blocks off (§12, §13).
            if (!booleanValue(current.params, "make_way", false) && client.player.distanceTo(target) > ENGAGE_DIG_REACH) {
                var settings = BaritoneAPI.getSettings();
                current.wayPreviousAllowBreak = settings.allowBreak.value;
                current.wayPreviousAllowPlace = settings.allowPlace.value;
                settings.allowBreak.value = false;
                settings.allowPlace.value = false;
            }
        }
        if (!target.isAlive() || target.getHealth() <= 0.0f) {
            requestStop("completed", "killed");
            return;
        }
        double disengage = number(current.params, "disengage_health", 0.0);
        if (disengage > 0.0 && client.player.getHealth() <= disengage) {
            requestStop("failed", "disengaged_low_health");
            return;
        }
        double attackRange = clamp(number(current.params, "attack_range", 3.0), 1.5, 4.5);
        double distance = client.player.distanceTo(target);
        Vec3 aim = visibleHitboxPoint(client, target);
        // "kite" (user 2026-11-10: 打怪本质上和打龙一样，应该是拉扯，伺机攻击): while the
        // blow recharges, back off out of the mob's reach and let it walk into
        // the next swing. Not the default: in the zombie benchmark (engageTrade)
        // swinging from 3 blocks already cost 0 health against one zombie, and
        // backing off cost more against two (body-tool-boundaries-20261110 §10).
        boolean kite = "kite".equals(string(current.params, "style", "stand"));
        boolean charged = client.player.getAttackStrengthScale(0.0f) >= 0.9f;
        if (distance > attackRange || aim == null) {
            if (kite && aim != null && !charged && distance <= attackRange + 3.0) {
                // Recharging and the mob is a few steps off: face it and let it come.
                kiteKeys(client, false, false, false);
                baritone.getPathingBehavior().cancelEverything();
                lookAtPoint(client, aim);
                current.engageIdleTicks = 0;
                return;
            }
            kiteKeys(client, false, false, false);
            if (engageStalled(client, current, target, distance, attackRange)) return;
            approachCombatTarget(client, target, attackRange);
            return;
        }
        current.engageIdleTicks = 0;
        baritone.getPathingBehavior().cancelEverything();
        lookAtPoint(client, aim);
        holdBestMeleeWeapon(client);
        int cooldown = Math.max(1, integer(current.params, "cooldown_ticks", 10));
        boolean swung = false;
        if (current.lastAttackTick < 0 || current.ticks - current.lastAttackTick >= cooldown) {
            if (charged) {
                client.gameMode.attack(client.player, target);
                client.player.resetAttackStrengthTicker();
                current.attacks++;
                current.lastAttackTick = current.ticks;
                swung = true;
            }
        }
        if (kite && !swung) kiteBack(client, current, target, distance);
        else kiteKeys(client, false, false, false);
    }

    /** A zombie strikes from about 1.4 blocks, the Body from 3: kept between
     * the two while its blow recharges, the mob walks into the next swing
     * without landing one. Backs straight off, else off to a side, onto a
     * cell with a floor at most one block down and no liquid or fire; with
     * no such cell it stands and swings as before. */
    private static final double KITE_KEEP = 2.6;
    private static final double ENGAGE_DIG_REACH = 6.0;

    private void kiteBack(Minecraft client, Run current, LivingEntity target, double distance) {
        if (distance >= KITE_KEEP || client.player.isUsingItem()) {
            kiteKeys(client, false, false, false);
            return;
        }
        double fx = target.getX() - client.player.getX(), fz = target.getZ() - client.player.getZ();
        double len = Math.sqrt(fx * fx + fz * fz);
        if (len < 1e-3) {
            kiteKeys(client, false, false, false);
            return;
        }
        fx /= len; fz /= len;
        // Facing f, the left key moves toward (f.z, -f.x).
        double lx = fz, lz = -fx;
        boolean[][] keys = {{true, false, false}, {true, true, false}, {true, false, true}, {false, true, false}, {false, false, true}};
        for (boolean[] k : keys) {
            double mx = (k[0] ? -fx : 0) + (k[1] ? lx : 0) + (k[2] ? -lx : 0);
            double mz = (k[0] ? -fz : 0) + (k[1] ? lz : 0) + (k[2] ? -lz : 0);
            double m = Math.sqrt(mx * mx + mz * mz);
            BlockPos cell = BlockPos.containing(client.player.getX() + mx / m * 0.9, client.player.getY() + 0.01,
                client.player.getZ() + mz / m * 0.9);
            if (kiteFloor(client, cell)) {
                kiteKeys(client, k[0], k[1], k[2]);
                current.kiteTicks++;
                return;
            }
        }
        kiteKeys(client, false, false, false);
    }

    /** The nearest hostile in sight within a few steps, not the one just
     * killed and not a creeper: closing on a creeper is Decision's call. */
    private static final double SWEEP_RADIUS = 6.0;

    private static LivingEntity nextAtHand(Minecraft client, Entity killed) {
        LivingEntity best = null;
        double bestDistance = Double.MAX_VALUE;
        for (Entity entity : client.level.getEntities(client.player, client.player.getBoundingBox().inflate(SWEEP_RADIUS),
                value -> value instanceof LivingEntity)) {
            if (entity == killed || !(entity instanceof LivingEntity living) || !living.isAlive()
                || living.getHealth() <= 0.0f || !hostile(entity) || entity instanceof Creeper) continue;
            double distance = client.player.distanceTo(entity);
            if (distance > SWEEP_RADIUS || visibleHitboxPoint(client, entity) == null) continue;
            if (distance < bestDistance) {
                best = living;
                bestDistance = distance;
            }
        }
        return best;
    }

    private static boolean kiteFloor(Minecraft client, BlockPos feet) {
        if (!clearOfHazard(client, feet) || !clearOfHazard(client, feet.above())) return false;
        for (int down = 1; down <= 2; down++) {
            BlockPos floor = feet.below(down);
            BlockState state = client.level.getBlockState(floor);
            if (!state.getFluidState().isEmpty() || state.is(net.minecraft.tags.BlockTags.FIRE)) return false;
            if (!state.getCollisionShape(client.level, floor).isEmpty()) return true;
        }
        return false;
    }

    private static void kiteKeys(Minecraft client, boolean back, boolean left, boolean right) {
        client.options.keyDown.setDown(back);
        client.options.keyLeft.setDown(left);
        client.options.keyRight.setDown(right);
    }

    /**
     * One arrow at one target. The action ends as soon as the arrow is loosed:
     * standing on to watch it planted the Body ~2 s per shot (end run-10, user
     * 2026-11-05). What the arrow did is followed afterwards and said in the
     * state as last_shot. A target out of reach or out of sight is not walked
     * to: the action says so with the distance, and whether to close in is the
     * caller's choice.
     */
    private void tickRanged(Minecraft client, Run current) {
        int expected = Math.max(1, integer(current.params, "expected_shots", 1));
        if (current.shots >= expected && !client.player.isUsingItem()) {
            beginShotWatch(client, current);
            requestStop("completed", "arrow_released");
            return;
        }
        if (current.targetEntity != null && !(current.targetEntity instanceof LivingEntity)
            && current.targetEntity.isRemoved()) {
            requestStop("completed", "target_gone");
            return;
        }
        double maxRange = Math.max(2.0, Math.min(64.0, number(current.params, "max_range", 24.0)));
        Entity nonLiving = rangedNonLivingTarget(client, current);
        if (nonLiving != null) {
            if (nonLiving.isRemoved()) {
                requestStop("completed", "target_gone");
                return;
            }
            current.targetEntity = nonLiving;
            String weapon = string(current.params, "weapon", "bow").replace("minecraft:", "");
            if (!itemPath(client.player.getMainHandItem()).equals(weapon)) {
                selectItem(client, weapon);
                return;
            }
            if (client.player.distanceTo(nonLiving) > maxRange) {
                current.rangeDistance = client.player.distanceTo(nonLiving);
                current.rangeMax = maxRange;
                requestStop("failed", "out_of_range");
                return;
            }
            if (!legsWalking()) baritone.getPathingBehavior().cancelEverything();
            current.arrowsBefore = arrowIds(client);
            aimBow(client, current, nonLiving);
            ItemStack held = client.player.getMainHandItem();
            if (current.useStartedTick < 0 && client.player.getProjectile(held).isEmpty()) {
                requestStop("failed", "no_ammunition");
                return;
            }
            if (current.useStartedTick < 0 && current.lastShotTick < 0) {
                client.options.keyUse.setDown(true);
                client.gameMode.useItem(client.player, InteractionHand.MAIN_HAND);
                current.useStartedTick = current.ticks;
            } else if (current.useStartedTick >= 0 && client.player.isUsingItem()
                && client.player.getTicksUsingItem() >= 21) {
                client.options.keyUse.setDown(false);
                sendAim(client);
                client.gameMode.releaseUsingItem(client.player);
                current.useStartedTick = -1;
                current.shots++;
                current.lastShotTick = current.ticks;
            }
            return;
        }
        if (current.targetEntity instanceof LivingEntity previous
            && (!previous.isAlive() || previous.getHealth() <= 0.0f)) {
            requestStop("completed", "killed");
            return;
        }
        LivingEntity target = combatTarget(client, current);
        if (target == null) {
            requestStop("failed", current.targetEntity == null ? "target_not_found" : "target_lost");
            return;
        }
        current.targetEntity = target;
        if (current.initialHealth < 0) current.initialHealth = target.getHealth();
        if (!target.isAlive() || target.getHealth() <= 0.0f) {
            requestStop("completed", "killed");
            return;
        }
        String weapon = string(current.params, "weapon", "bow").replace("minecraft:", "");
        if (!itemPath(client.player.getMainHandItem()).equals(weapon)) {
            selectItem(client, weapon);
            return;
        }
        boolean requireLineOfSight = booleanValue(current.params, "require_line_of_sight", false);
        double distance = Math.sqrt(reachDistanceSqr(client, target));
        boolean tooFar = distance > maxRange;
        if (tooFar || (requireLineOfSight && !hasLineOfSight(client, target))) {
            current.rangeDistance = distance;
            current.rangeMax = maxRange;
            requestStop("failed", tooFar ? "out_of_range" : "no_line_of_sight");
            return;
        }
        if (!legsWalking()) baritone.getPathingBehavior().cancelEverything();
        if (current.shots == 0) current.arrowsBefore = arrowIds(client);
        aimBow(client, current, target);
        int interval = Math.max(1, integer(current.params, "use_interval_ticks", weapon.equals("crossbow") ? 30 : 20));
        ItemStack held = client.player.getMainHandItem();
        if (!client.player.getAbilities().instabuild && current.useStartedTick < 0
            && !(held.getItem() instanceof CrossbowItem && CrossbowItem.isCharged(held))
            && client.player.getProjectile(held).isEmpty()) {
            requestStop("failed", "no_ammunition");
            return;
        }
        int required = held.getItem() instanceof CrossbowItem ? CrossbowItem.getChargeDuration(held, client.player) : 20;
        if (held.getItem() instanceof CrossbowItem && CrossbowItem.isCharged(held)
            && current.useStartedTick < 0
            && (current.lastShotTick < 0 || current.ticks - current.lastShotTick >= interval)) {
            current.shotHealthBaseline = target.getHealth();
            sendAim(client);
            client.gameMode.useItem(client.player, InteractionHand.MAIN_HAND);
        } else if (current.useStartedTick < 0
            && (current.lastShotTick < 0 || current.ticks - current.lastShotTick >= interval)) {
            current.shotHealthBaseline = target.getHealth();
            client.options.keyUse.setDown(true);
            client.gameMode.useItem(client.player, InteractionHand.MAIN_HAND);
            current.useStartedTick = current.ticks;
        } else if (current.useStartedTick >= 0 && (
            held.getItem() instanceof CrossbowItem && CrossbowItem.isCharged(held)
            || client.player.isUsingItem() && client.player.getTicksUsingItem() >= required + 1)) {
            client.options.keyUse.setDown(false);
            sendAim(client);
                client.gameMode.releaseUsingItem(client.player);
            if (!(held.getItem() instanceof CrossbowItem)) {
                current.shots++;
                current.lastShotTick = current.ticks;
            }
            current.useStartedTick = -1;
        } else if (current.useStartedTick >= 0 && !client.player.isUsingItem()) {
            // A server inventory update may interrupt charging after a swap.
            client.options.keyUse.setDown(false);
            current.useStartedTick = -1;
        }
    }

    /** Follow the arrow a ranged action just loosed, after the action has ended. */
    private void beginShotWatch(Minecraft client, Run current) {
        Entity target = current.targetEntity;
        float before = Float.NaN;
        if (target instanceof LivingEntity living) {
            before = current.shotHealthBaseline >= 0.0f ? (float) current.shotHealthBaseline : living.getHealth();
        }
        long released = client.level.getGameTime() - Math.max(0, current.ticks - Math.max(0, current.lastShotTick));
        lastShot = new ShotWatch(target, before, current.arrowsBefore, released, current.predictedTargetPos,
            client.player.position());
    }

    /**
     * Where the last arrow went: the target hurt, killed or gone, or the arrow
     * stuck in a block (named, as a player sees it), or gone without the
     * target hurt. An arrow not seen to land within five seconds is a miss.
     */
    private void watchShot(Minecraft client) {
        ShotWatch shot = lastShot;
        if (shot == null || !shot.outcome.equals("in_flight")) return;
        long now = client.level.getGameTime();
        long age = now - shot.releasedTick;
        if (shot.target instanceof LivingEntity living) {
            if (!living.isAlive() || living.getHealth() <= 0.0f) {
                shot.resolve("killed", now);
                shot.healthAfter = 0.0f;
                return;
            }
            if (!Float.isNaN(shot.healthBefore) && living.getHealth() < shot.healthBefore - 0.01f) {
                shot.healthAfter = living.getHealth();
                shot.resolve("hit", now);
                return;
            }
        } else if (shot.target != null && shot.target.isRemoved()) {
            shot.targetGone = true;
            shot.resolve("hit", now);
            return;
        }
        if (shot.arrow == null && age <= 10) {
            // The arrow that was not there while the bow was drawn; its spawn
            // may reach the client a tick or two after the release.
            double best = Double.MAX_VALUE;
            for (Entity entity : client.level.entitiesForRendering()) {
                if (!(entity instanceof net.minecraft.world.entity.projectile.arrow.AbstractArrow arrow)
                    || shot.arrowsBefore.contains(arrow.getId())) continue;
                double distance = arrow.position().distanceToSqr(shot.from);
                if (distance < best) {
                    shot.arrow = arrow;
                    best = distance;
                }
            }
        }
        Entity arrow = shot.arrow;
        if (arrow != null && !arrow.isRemoved()) {
            Vec3 at = arrow.position();
            if (shot.arrowLast != null && at.distanceToSqr(shot.arrowLast) < 1.0E-4) {
                if (++shot.still >= 2) {
                    // The arrow sits in the face of the block it struck.
                    Vec3 heading = shot.arrowHeading == null ? Vec3.ZERO : shot.arrowHeading.normalize().scale(0.2);
                    shot.landed = BlockPos.containing(at.add(heading));
                    shot.landedBlock = BuiltInRegistries.BLOCK.getKey(client.level.getBlockState(shot.landed).getBlock()).getPath();
                    shot.resolve("missed", now);
                    return;
                }
            } else {
                shot.still = 0;
                if (shot.arrowLast != null) shot.arrowHeading = at.subtract(shot.arrowLast);
            }
            shot.arrowLast = at;
        } else if (arrow != null && shot.goneTick < 0) {
            shot.goneTick = now;
        }
        // Gone without the target hurt: a health update may trail the arrow's removal by a few ticks.
        if (shot.goneTick >= 0 && now - shot.goneTick > 6) shot.resolve("missed", now);
        else if (age > 100) shot.resolve("missed", now);
    }

    private static final class ShotWatch {
        final Entity target;
        final String targetId;
        final String targetType;
        final float healthBefore;
        final java.util.Set<Integer> arrowsBefore;
        final long releasedTick;
        final Vec3 predicted;
        final Vec3 from;
        Entity arrow;
        Vec3 arrowLast;
        Vec3 arrowHeading;
        int still;
        long goneTick = -1L;
        String outcome = "in_flight";
        long resolvedTick = -1L;
        BlockPos landed;
        String landedBlock;
        float healthAfter = Float.NaN;
        boolean targetGone;

        ShotWatch(Entity target, float healthBefore, java.util.Set<Integer> arrowsBefore, long releasedTick,
                  Vec3 predicted, Vec3 from) {
            this.target = target;
            this.targetId = target == null ? "" : target.getUUID().toString();
            this.targetType = target == null ? "" : BuiltInRegistries.ENTITY_TYPE.getKey(target.getType()).getPath();
            this.healthBefore = healthBefore;
            this.arrowsBefore = arrowsBefore == null ? java.util.Set.of() : arrowsBefore;
            this.releasedTick = releasedTick;
            this.predicted = predicted;
            this.from = from;
        }

        void resolve(String outcome, long tick) {
            this.outcome = outcome;
            this.resolvedTick = tick;
        }

        JsonObject fact(long now) {
            JsonObject fact = new JsonObject();
            fact.addProperty("target_id", targetId);
            fact.addProperty("target_type", targetType);
            fact.addProperty("outcome", outcome);
            fact.addProperty("age_s", round2(Math.max(0L, now - releasedTick) / 20.0));
            if (resolvedTick >= 0) fact.addProperty("resolved_after_s", round2((resolvedTick - releasedTick) / 20.0));
            fact.add("shot_from", xyz(from));
            if (predicted != null) fact.add("predicted_target_pos", xyz(predicted));
            if (landed != null) fact.add("arrow_landed", xyz(landed));
            if (landedBlock != null) fact.addProperty("arrow_landed_block", landedBlock);
            if (targetGone) fact.addProperty("target_gone", true);
            if (!Float.isNaN(healthBefore)) fact.addProperty("health_before", healthBefore);
            if (!Float.isNaN(healthAfter)) fact.addProperty("health_after", healthAfter);
            return fact;
        }
    }

    // A full-power bow arrow: 3 blocks a tick, then each tick 0.99 drag and 0.05 down.
    private static final double ARROW_SPEED = 3.0;

    /**
     * Aim as a player who knows the arrow drops: the elevation whose path,
     * flown tick by tick as the game flies it, passes the target's centre.
     * Aimed straight at the centre, ten arrows at End crystals 22-38 blocks
     * away hit none: they arrive 2-4 blocks low (end baseline run-4).
     */
    private static void aimBow(Minecraft client, Run current, Entity target) {
        double fromY = client.player.getEyeY() - 0.1;
        Vec3 targetPos = targetAimPoint(target);
        rememberTargetPosition(current, target, targetPos);
        Vec3 predicted = targetPos;
        Vec3 velocity = booleanValue(current.params, "lead", true) ? observedTargetVelocity(current) : Vec3.ZERO;
        Double elevation = null;
        // Re-solving after each flight-time estimate keeps the lead tied to the
        // arrow's actual drag and drop, rather than to a one-tick guess.
        for (int iteration = 0; iteration < 3; iteration++) {
            double dx = predicted.x - client.player.getX();
            double dz = predicted.z - client.player.getZ();
            double dy = predicted.y - fromY;
            elevation = bowElevation(Math.sqrt(dx * dx + dz * dz), dy);
            if (elevation == null || velocity.lengthSqr() < 1.0E-10) break;
            double flight = arrowFlightTicks(elevation, Math.sqrt(dx * dx + dz * dz));
            predicted = targetPos.add(velocity.scale(flight));
        }
        if (elevation != null && velocity.lengthSqr() >= 1.0E-10) {
            double dx = predicted.x - client.player.getX();
            double dz = predicted.z - client.player.getZ();
            elevation = bowElevation(Math.sqrt(dx * dx + dz * dz), predicted.y - fromY);
        }
        current.predictedTargetPos = predicted;
        current.leadBlocks = predicted.distanceTo(targetPos);
        double dx = predicted.x - client.player.getX();
        double dz = predicted.z - client.player.getZ();
        if (elevation == null) {
            lookAtEntity(client, target);
            return;
        }
        client.player.setYRot((float) (Math.atan2(dz, dx) * 180.0 / Math.PI) - 90.0f);
        client.player.setXRot((float) -elevation.doubleValue());
    }

    /** The lowest elevation in degrees whose arrow reaches (horizontal, dy), or null out of reach. */
    static Double bowElevation(double horizontal, double dy) {
        double previous = Double.NaN;
        for (double elevation = -89.0; elevation <= 89.0; elevation += 0.25) {
            double height = arrowHeightAt(elevation, horizontal);
            if (height >= dy) {
                if (Double.isNaN(previous) || Double.isInfinite(previous)) return elevation;
                return elevation - 0.25 * (height - dy) / (height - previous);
            }
            previous = height;
        }
        return null;
    }

    private static double arrowHeightAt(double elevation, double horizontal) {
        double radians = Math.toRadians(elevation);
        double vx = ARROW_SPEED * Math.cos(radians), vy = ARROW_SPEED * Math.sin(radians);
        double x = 0.0, y = 0.0;
        for (int tick = 0; tick < 400; tick++) {
            double nx = x + vx, ny = y + vy;
            if (nx >= horizontal) return vx <= 0.0 ? ny : y + (ny - y) * (horizontal - x) / (nx - x);
            x = nx;
            y = ny;
            vx *= 0.99;
            vy = vy * 0.99 - 0.05;
            if (vx < 1.0E-3) break;
        }
        return Double.NEGATIVE_INFINITY;
    }

    private static double arrowFlightTicks(double elevation, double horizontal) {
        if (horizontal <= 0.0) return 0.0;
        double radians = Math.toRadians(elevation);
        double vx = ARROW_SPEED * Math.cos(radians);
        double x = 0.0;
        for (int tick = 0; tick < 400; tick++) {
            double nx = x + vx;
            if (nx >= horizontal) return tick + (horizontal - x) / Math.max(1.0E-9, nx - x);
            x = nx;
            vx *= 0.99;
            if (vx < 1.0E-3) break;
        }
        return 0.0;
    }

    private static Vec3 targetAimPoint(Entity target) {
        return new Vec3(target.getX(), target.getY() + target.getBbHeight() * 0.5, target.getZ());
    }

    private static void rememberTargetPosition(Run current, Entity target, Vec3 position) {
        String id = target.getUUID().toString();
        if (!id.equals(current.motionTargetId)) {
            current.motionTargetId = id;
            current.targetObservations.clear();
        }
        if (!current.targetObservations.isEmpty()
            && current.targetObservations.get(current.targetObservations.size() - 1).tick() == current.ticks) {
            current.targetObservations.set(current.targetObservations.size() - 1,
                new TargetObservation(current.ticks, position));
        } else {
            current.targetObservations.add(new TargetObservation(current.ticks, position));
        }
        while (current.targetObservations.size() > 6
            || (!current.targetObservations.isEmpty()
                && current.ticks - current.targetObservations.get(0).tick() > 8)) {
            current.targetObservations.remove(0);
        }
    }

    private static Vec3 observedTargetVelocity(Run current) {
        if (current.targetObservations.size() < 3) return Vec3.ZERO;
        TargetObservation first = current.targetObservations.get(0);
        TargetObservation last = current.targetObservations.get(current.targetObservations.size() - 1);
        long ticks = last.tick() - first.tick();
        if (ticks < 2) return Vec3.ZERO;
        return last.position().subtract(first.position()).scale(1.0 / ticks);
    }

    /** A crystal surrounded by the iron-bar shell used on caged End pillars. */
    static boolean isCagedCrystal(java.util.function.Predicate<BlockPos> isIronBars, BlockPos center) {
        int bars = 0;
        // EndSpikeFeature's cage is a 5x5 shell: perimeter walls for three
        // layers and a roof above the crystal (the vanilla layout).
        for (int dx = -2; dx <= 2; dx++) for (int dy = -1; dy <= 2; dy++) for (int dz = -2; dz <= 2; dz++) {
            if ((Math.abs(dx) == 2 || Math.abs(dz) == 2 || dy == 2)
                && isIronBars.test(center.offset(dx, dy, dz))) bars++;
        }
        return bars >= 4;
    }

    private static java.util.Set<Integer> arrowIds(Minecraft client) {
        java.util.Set<Integer> ids = new java.util.HashSet<>();
        for (Entity entity : client.level.entitiesForRendering()) {
            if (entity instanceof net.minecraft.world.entity.projectile.arrow.AbstractArrow) ids.add(entity.getId());
        }
        return ids;
    }

    private Entity rangedNonLivingTarget(Minecraft client, Run current) {
        String targetId = string(current.params, "target_uuid", string(current.params, "target_id", ""));
        if (targetId.isBlank()) return null;
        Entity entity = client.level.getEntities(client.player, client.player.getBoundingBox().inflate(64.0),
            value -> value.getUUID().toString().equals(targetId)).stream().findFirst().orElse(null);
        return entity != null && !(entity instanceof LivingEntity) ? entity : null;
    }

    private LivingEntity combatTarget(Minecraft client, Run current) {
        String targetId = string(current.params, "target_uuid", string(current.params, "target_id", ""));
        if (targetId.isBlank() && current.targetEntity != null) targetId = current.targetEntity.getUUID().toString();
        String targetName = string(current.params, "target_name", "");
        String targetType = string(current.params, "target_type", "").replace("minecraft:", "");
        // A ranged action looks as far as a bow can reach, whatever max_range
        // it was given: a target found beyond max_range is out_of_range with
        // its distance, not "not found" (the dragon 44-51 blocks off by its
        // nearest part was not found within 32 ten times, end run-6).
        double defaultRadius = current.kind.equals("rangedAttack") ? 64.0 : 32.0;
        double radius = Math.max(1.0, Math.min(64.0, number(current.params, "acquire_radius", number(current.params, "radius", defaultRadius))));
        LivingEntity best = null;
        double bestDistance = Double.MAX_VALUE;
        for (Entity entity : client.level.getEntities(client.player, client.player.getBoundingBox().inflate(radius), value -> value instanceof LivingEntity)) {
            if (!(entity instanceof LivingEntity living) || !living.isAlive()) continue;
            boolean match;
            if (!targetId.isBlank()) match = entity.getUUID().toString().equals(targetId);
            else if (!targetName.isBlank()) match = entity.getName().getString().equals(targetName);
            else if (!targetType.isBlank()) match = BuiltInRegistries.ENTITY_TYPE.getKey(entity.getType()).getPath().equals(targetType);
            else match = hostile(entity);
            double distance = reachDistanceSqr(client, entity);
            if (match && distance <= radius * radius && distance < bestDistance) {
                best = living;
                bestDistance = distance;
            }
        }
        return best;
    }

    /**
     * A fall the Body is in: how far it has fallen, and what lies straight
     * below, a block or water some blocks down, or nothing to the bottom of
     * the world. In end run-6 the dragon threw the Body off a pillar and off
     * the island four times; move_away then said it had moved away.
     */
    static JsonObject fallFact(Minecraft client) {
        var player = client.player;
        if (player.onGround() || player.isInWater() || player.isInLava() || player.getAbilities().flying
            || player.isFallFlying() || player.getDeltaMovement().y >= -0.1) return null;
        BlockPos feet = player.blockPosition();
        if (!client.level.hasChunkAt(feet)) return null;
        JsonObject fall = new JsonObject();
        fall.addProperty("fallen", Math.round(player.fallDistance * 10.0) / 10.0);
        for (int y = feet.getY(); y >= client.level.getMinY(); y--) {
            BlockPos at = new BlockPos(feet.getX(), y, feet.getZ());
            boolean water = client.level.getFluidState(at).is(FluidTags.WATER);
            if (water || !client.level.getBlockState(at).getCollisionShape(client.level, at).isEmpty()) {
                double top = water ? y + 1.0 : y + client.level.getBlockState(at).getCollisionShape(client.level, at).max(net.minecraft.core.Direction.Axis.Y);
                fall.addProperty("ground_below", Math.round(Math.max(0.0, player.getY() - top) * 10.0) / 10.0);
                fall.addProperty("lands_on", water ? "water" : BuiltInRegistries.BLOCK.getKey(client.level.getBlockState(at).getBlock()).getPath());
                fall.add("landing_cell", xyz(at));
                return fall;
            }
        }
        fall.addProperty("void_below", true);
        return fall;
    }

    /**
     * The nearest water source an empty bucket held now could take: within
     * reach, and the first thing a bucket's ray meets on the way to it. A
     * Body that landed a fall in its water is carried off the source by the
     * flow within ticks (GameTest cushionFall), so where it stands says
     * nothing (dragon-unit-tests-20261030 §6.y).
     */
    static BlockPos waterSourceInReach(Minecraft client) {
        var player = client.player;
        Vec3 eye = player.getEyePosition();
        double reach = player.blockInteractionRange();
        BlockPos feet = player.blockPosition();
        BlockPos best = null;
        double bestDistance = Double.MAX_VALUE;
        int r = (int) Math.ceil(reach);
        for (BlockPos at : BlockPos.betweenClosed(feet.offset(-r, -r, -r), feet.offset(r, r, r))) {
            if (!client.level.getFluidState(at).isSourceOfType(net.minecraft.world.level.material.Fluids.WATER)) continue;
            Vec3 aim = new Vec3(at.getX() + 0.5, at.getY() + 0.4, at.getZ() + 0.5);
            double distance = aim.distanceTo(eye);
            if (distance > reach || distance >= bestDistance) continue;
            BlockHitResult hit = client.level.clip(new ClipContext(eye, aim.add(aim.subtract(eye).normalize().scale(0.5)),
                ClipContext.Block.OUTLINE, ClipContext.Fluid.SOURCE_ONLY, player));
            if (hit.getType() == HitResult.Type.BLOCK && hit.getBlockPos().equals(at)) {
                best = at.immutable();
                bestDistance = distance;
            }
        }
        return best;
    }

    /** From the eyes to the nearest of the entity's hit boxes: a dragon by its parts, as the frame says it. */
    static double reachDistanceSqr(Minecraft client, Entity entity) {
        Vec3 eye = client.player.getEyePosition();
        double nearest = entity.getBoundingBox().distanceToSqr(eye);
        if (entity instanceof net.minecraft.world.entity.boss.enderdragon.EnderDragon dragon) {
            for (var part : dragon.getSubEntities()) nearest = Math.min(nearest, part.getBoundingBox().distanceToSqr(eye));
        }
        return nearest;
    }

    /** Vanilla may fire a charged crossbow between two Body ticks. */
    public static boolean observeCrossbowUse() {
        if (instance == null || instance.run == null || !instance.run.kind.equals("rangedAttack")) return true;
        Run current = instance.run;
        Minecraft client = Minecraft.getInstance();
        ItemStack held = client.player.getMainHandItem();
        if (!(held.getItem() instanceof CrossbowItem) || !CrossbowItem.isCharged(held)) return true;
        int expected = Math.max(1, integer(current.params, "expected_shots", 1));
        client.options.keyUse.setDown(false);
        if (current.stopStatus != null || current.shots >= expected) return false;
        if (current.targetEntity instanceof LivingEntity target) current.shotHealthBaseline = target.getHealth();
        current.shots++;
        current.lastShotTick = current.ticks;
        current.useStartedTick = -1;
        return true;
    }

    /**
     * The walk toward the target has ended and the target is still out of
     * reach or out of sight. A player stops and looks again; the Body stood
     * next to a spider for 20 s without a swing until the deadline (easy
     * run-5, 17:51-17:52). The action ends with what is in the way.
     */
    private boolean engageStalled(Minecraft client, Run current, LivingEntity target, double distance, double attackRange) {
        // Only walking a path counts: a route that keeps failing is
        // recalculated every tick and would never look idle.
        current.engageIdleTicks = baritone.getPathingBehavior().isPathing() ? 0 : current.engageIdleTicks + 1;
        if (current.engageIdleTicks < 40) return false;
        current.rangeDistance = distance;
        current.rangeMax = attackRange;
        if (distance <= attackRange) {
            BlockHitResult hit = client.level.clip(new ClipContext(client.player.getEyePosition(), target.getEyePosition(),
                ClipContext.Block.VISUAL, ClipContext.Fluid.NONE, client.player));
            if (hit.getType() == HitResult.Type.BLOCK) {
                current.sightBlockedPos = hit.getBlockPos();
                current.sightBlockedBlock = BuiltInRegistries.BLOCK.getKey(client.level.getBlockState(hit.getBlockPos()).getBlock()).getPath();
            }
            requestStop("failed", "no_line_of_sight");
        } else {
            boolean noRoute = !current.routeFound && current.pathEvents.getOrDefault("CALC_FAILED", 0) > 0;
            requestStop("failed", noRoute ? "no_route_found" : "target_out_of_reach");
        }
        return true;
    }

    /**
     * A point of the target's body the Body can see, or null. A player hits
     * whatever part of a mob is in view, not only its eyes: the spider under
     * a one-block overhang had its eyes behind stone and its body in plain
     * view, and the next try one block lower killed it in four swings (easy
     * run-5, 17:52).
     */
    private static Vec3 visibleHitboxPoint(Minecraft client, Entity target) {
        Vec3 from = client.player.getEyePosition();
        net.minecraft.world.phys.AABB box = target.getBoundingBox();
        Vec3 centre = box.getCenter();
        double in = 0.1;
        List<Vec3> points = List.of(
            target.getEyePosition(), centre,
            new Vec3(centre.x, box.minY + in, centre.z), new Vec3(centre.x, box.maxY - in, centre.z),
            new Vec3(box.minX + in, centre.y, box.minZ + in), new Vec3(box.minX + in, centre.y, box.maxZ - in),
            new Vec3(box.maxX - in, centre.y, box.minZ + in), new Vec3(box.maxX - in, centre.y, box.maxZ - in));
        for (Vec3 point : points) {
            if (client.level.clip(new ClipContext(from, point, ClipContext.Block.VISUAL, ClipContext.Fluid.NONE, client.player)).getType()
                == HitResult.Type.MISS) return point;
        }
        return null;
    }

    private void approachCombatTarget(Minecraft client, LivingEntity target, double range) {
        // Baritone tests block coordinates; leave room for the players' actual
        // sub-block positions so goal completion also puts the target in reach.
        Goal next = new GoalNear(target.blockPosition(), (int) Math.max(0, Math.floor(Math.min(8.0, range) - 1.0)));
        if (!baritone.getCustomGoalProcess().isActive() || baritone.getPathingBehavior().getGoal() == null
            || !baritone.getPathingBehavior().getGoal().toString().equals(next.toString())) {
            baritone.getCustomGoalProcess().setGoalAndPath(next);
        }
    }

    private static boolean hasLineOfSight(Minecraft client, Entity target) {
        Vec3 from = client.player.getEyePosition();
        Vec3 to = target.getEyePosition();
        return client.level.clip(new ClipContext(from, to, ClipContext.Block.VISUAL, ClipContext.Fluid.NONE, client.player)).getType()
            == HitResult.Type.MISS;
    }

    private static void lookAtEntity(Minecraft client, Entity target) {
        lookAtPoint(client, new Vec3(target.getX(), target.getY() + target.getBbHeight() * 0.55, target.getZ()));
    }

    private static void lookAtPoint(Minecraft client, Vec3 point) {
        double dx = point.x - client.player.getX();
        double dy = point.y - client.player.getEyeY();
        double dz = point.z - client.player.getZ();
        double horizontal = Math.sqrt(dx * dx + dz * dz);
        float yaw = (float) (Math.atan2(dz, dx) * 180.0 / Math.PI) - 90.0f;
        float pitch = (float) -(Math.atan2(dy, horizontal) * 180.0 / Math.PI);
        BaritoneAPI.getProvider().getBaritoneForMinecraft(client).getLookBehavior()
            .updateTarget(new Rotation(yaw, pitch), true);
    }

    private static void openMenu(Minecraft client, BlockPos pos) {
        if (pos == null || !client.level.hasChunkAt(pos) || !client.player.isWithinBlockInteractionRange(pos, 0.0)) {
            throw new IllegalArgumentException("container_out_of_range");
        }
        if (!client.player.containerMenu.getCarried().isEmpty()) throw new IllegalArgumentException("menu_busy");
        // Only the action about to hold a menu opens one, so any screen still
        // open was left behind; a player closes it and opens the next.
        if (client.player.containerMenu.containerId != 0) client.player.closeContainer();
        BlockHitResult hit = new BlockHitResult(Vec3.atCenterOf(pos), Direction.UP, pos, false);
        lookAt(client, lookParams(hit.getLocation()));
        if (!client.gameMode.useItemOn(client.player, InteractionHand.MAIN_HAND, hit).consumesAction()) {
            throw new IllegalArgumentException("container_open_rejected");
        }
    }

    public static void observeMenuContent(int menuId, List<ItemStack> items) {
        if (instance == null) return;
        instance.menuContentRevision++;
        instance.contentMenuId = menuId;
        if (!items.isEmpty()) instance.confirmedResults.put(menuId, items.getFirst().copy());
        instance.resultRevision++;
    }

    public static void observeMenuSlot(int menuId, int slot, ItemStack stack) {
        if (instance != null && slot == 0) {
            instance.confirmedResults.put(menuId, stack.copy());
            instance.resultRevision++;
        }
    }

    private boolean freshMenu(Minecraft client, long revision) {
        return client.player.containerMenu.containerId != 0
            && contentMenuId == client.player.containerMenu.containerId && menuContentRevision > revision;
    }

    private void tickMenuRead(Minecraft client) {
        if (freshMenu(client, menuRead.revision)) finishMenuRead(client, null);
        else if (System.nanoTime() - menuRead.started > 5_000_000_000L) finishMenuRead(client, "container_open_timeout");
    }

    private void finishMenuRead(Minecraft client, String error) {
        MenuRead current = menuRead;
        if (current == null) return;
        menuRead = null;
        JsonObject response = envelope("container_read", current.id);
        response.addProperty("request_id", current.id);
        response.addProperty("ok", error == null);
        if (error != null) response.addProperty("error", error);
        if (error == null) {
            AbstractContainerMenu menu = client.player.containerMenu;
            JsonObject data = new JsonObject();
            JsonArray pos = new JsonArray();
            pos.add(current.pos.getX()); pos.add(current.pos.getY()); pos.add(current.pos.getZ());
            data.add("pos", pos);
            data.addProperty("menu", menu.getClass().getSimpleName());
            data.addProperty("menu_id", menu.containerId);
            data.addProperty("state_id", menu.getStateId());
            JsonArray slots = new JsonArray();
            for (Slot slot : menu.slots) {
                if (!(slot.container instanceof net.minecraft.world.entity.player.Inventory)) {
                    slots.add(stackFact(slot.getItem(), slots.size(), "container", "container." + slots.size()));
                }
            }
            data.add("slots", slots);
            if (menu instanceof AbstractFurnaceMenu furnace) {
                data.addProperty("lit", furnace.isLit());
                data.addProperty("burn_progress", furnace.getBurnProgress());
            }
            response.add("data", data);
            client.player.closeContainer();
        }
        current.peer.send(response);
    }

    private static JsonObject stackFact(ItemStack stack, int slot, String type, String label) {
        JsonObject fact = new JsonObject();
        fact.addProperty("slot", slot);
        fact.addProperty("slotType", type);
        fact.addProperty("slotLabel", label);
        fact.addProperty("empty", stack.isEmpty());
        fact.addProperty("count", stack.getCount());
        if (!stack.isEmpty()) {
            fact.addProperty("item", BuiltInRegistries.ITEM.getKey(stack.getItem()).toString());
            fact.addProperty("stackRaw", stack.getComponentsPatch().toString());
            fact.addProperty("maxStack", stack.getMaxStackSize());
        }
        return fact;
    }

    private static JsonArray inventorySlots(Minecraft client) {
        JsonArray slots = new JsonArray();
        InventoryMenu menu = client.player.inventoryMenu;
        for (int slot = 0; slot < 46; slot++) {
            int mapped = slot < 41 ? menuSlotForPlayer(menu, slot) : slot == 45 ? 0 : slot - 40;
            ItemStack stack = mapped < 0 ? ItemStack.EMPTY : menu.getSlot(mapped).getItem();
            String type = slot < 9 ? "hotbar" : slot < 36 ? "inventory" : slot < 40 ? "armor" : slot == 40 ? "offhand" : "aux";
            slots.add(stackFact(stack, slot, type, type + "." + slot));
        }
        return slots;
    }

    private static JsonObject recipeData(Minecraft client, JsonObject params) {
        String wanted = string(params, "item", "").replace("minecraft:", "");
        JsonArray variants = new JsonArray();
        var context = SlotDisplayContext.fromLevel(client.level);
        for (var collection : client.player.getRecipeBook().getCollections()) {
            for (RecipeDisplayEntry entry : collection.getRecipes()) {
                RecipeDisplay display = entry.display();
                ItemStack output = display.result().resolveForFirstStack(context);
                if (output.isEmpty() || !itemPath(output).equals(wanted)) continue;
                JsonObject variant = new JsonObject();
                List<SlotDisplay> ingredients;
                if (display instanceof ShapedCraftingRecipeDisplay shaped) {
                    variant.addProperty("recipe_kind", "shaped");
                    variant.addProperty("width", shaped.width());
                    variant.addProperty("height", shaped.height());
                    ingredients = shaped.ingredients();
                } else if (display instanceof ShapelessCraftingRecipeDisplay shapeless) {
                    variant.addProperty("recipe_kind", "shapeless");
                    ingredients = shapeless.ingredients();
                } else if (display instanceof FurnaceRecipeDisplay furnace) {
                    variant.addProperty("recipe_kind", "smelting");
                    variant.addProperty("duration_ticks", furnace.duration());
                    ingredients = List.of(furnace.ingredient());
                } else continue;
                String requestedType = string(params, "type", "crafting").replace("minecraft:", "");
                if (requestedType.equals("crafting") && display instanceof FurnaceRecipeDisplay) continue;
                if (!requestedType.equals("crafting") && !(display instanceof FurnaceRecipeDisplay)) continue;
                variant.addProperty("output_item", BuiltInRegistries.ITEM.getKey(output.getItem()).toString());
                variant.addProperty("output_count", output.getCount());
                variant.addProperty("output_max_stack", output.getMaxStackSize());
                int maxCrafts = Math.max(1, output.getMaxStackSize() / output.getCount());
                for (SlotDisplay ingredient : ingredients) {
                    int ingredientCapacity = ingredient.resolveForStacks(context).stream()
                        .mapToInt(ItemStack::getMaxStackSize).min().orElse(64);
                    maxCrafts = Math.min(maxCrafts, ingredientCapacity);
                }
                variant.addProperty("max_craft_count", maxCrafts * output.getCount());
                JsonArray groups = new JsonArray();
                for (SlotDisplay ingredient : ingredients) {
                    JsonArray alternatives = new JsonArray();
                    ingredient.resolveForStacks(context).stream().map(stack -> BuiltInRegistries.ITEM.getKey(stack.getItem()).toString())
                        .distinct().forEach(alternatives::add);
                    groups.add(alternatives);
                }
                variant.add("ingredient_groups", groups);
                variants.add(variant);
            }
        }
        JsonObject data = new JsonObject();
        data.add("variants", variants);
        data.addProperty("recipe_source", "server_synced_unlocked_recipe_book");
        return data;
    }

    private void tickCraft(Minecraft client, Run current) {
        JsonObject output = current.params.has("output") && current.params.get("output").isJsonObject()
            ? current.params.getAsJsonObject("output") : new JsonObject();
        String outputItem = string(output, "item", "").replace("minecraft:", "");
        int outputCount = integer(output, "count", 0);
        int outputInventorySlot = integer(output, "slot", -1);
        if (outputItem.isBlank() || outputCount <= 0 || outputInventorySlot < 0 || outputInventorySlot >= 36) {
            requestStop("failed", "invalid_recipe_output");
            return;
        }
        if (current.phase == 0) {
            if (!current.params.has("inputs") || !current.params.get("inputs").isJsonArray()
                || current.params.getAsJsonArray("inputs").isEmpty() || !validCraftEntries(current.params.getAsJsonArray("inputs"))) {
                requestStop("failed", "invalid_recipe_inputs");
                return;
            }
            if (current.params.has("remainders") && (!current.params.get("remainders").isJsonArray()
                || !validCraftEntries(current.params.getAsJsonArray("remainders")))) {
                requestStop("failed", "invalid_remainder_slot");
                return;
            }
            BlockPos table = positionParam(current.params, "crafting_table");
            if (table != null) {
                current.menuRevision = menuContentRevision;
                openMenu(client, table);
                current.phase = 10;
                return;
            }
            current.phase = 11;
        }
        if (current.phase == 10) {
            if (!freshMenu(client, current.menuRevision)) return;
            if (!(client.player.containerMenu instanceof CraftingMenu)) {
                requestStop("failed", "crafting_table_menu_missing");
                return;
            }
            current.phase = 11;
        }
        if (!(client.player.containerMenu instanceof AbstractCraftingMenu menu)) {
            requestStop("failed", "crafting_menu_unavailable");
            return;
        }
        if (current.phase == 11) {
            current.openMenuId = menu.containerId;
            current.outputMenuSlot = menuSlotForPlayer(menu, outputInventorySlot);
            if (!menu.getCarried().isEmpty() || menu.getInputGridSlots().stream().anyMatch(slot -> slot.hasItem())) {
                requestStop("failed", "crafting_grid_not_empty");
                return;
            }
            ItemStack destination = menu.getSlot(current.outputMenuSlot).getItem();
            if ((!destination.isEmpty() && !itemPath(destination).equals(outputItem))
                || destination.getCount() + outputCount > (destination.isEmpty() ? integer(current.params, "max_stack", 64) : destination.getMaxStackSize())) {
                requestStop("failed", "craft_output_no_space");
                return;
            }
            current.craftBefore = inventorySlots(client);
            current.resultRevision = resultRevision;
            if (!craftInputs(client, menu, current)) return;
            current.outputExpected = outputItem;
            current.outputRequested = outputCount;
            current.phase = 1;
            current.phaseTick = current.ticks;
            return;
        }
        if (menu.containerId != current.openMenuId) {
            requestStop("failed", "crafting_menu_changed");
            return;
        }
        if (current.phase == 1) {
            ItemStack result = confirmedResults.getOrDefault(menu.containerId, ItemStack.EMPTY);
            if (resultRevision <= current.resultRevision || result.isEmpty()) return;
            if (!CraftOutputProgress.matches(itemPath(result), result.getCount(), outputItem, current.craftUnitCount)) {
                // The menu can publish an intermediate result while the placed inputs
                // are still being resolved. A newer result revision is the evidence
                // needed to decide; do not turn that transient state into a false
                // terminal failure.
                current.resultRevision = resultRevision;
                return;
            }
            current.resultRevision = resultRevision;
            click(menu, menu.getResultSlot().index, 0, ContainerInput.PICKUP, client);
            click(menu, current.outputMenuSlot, 0, ContainerInput.PICKUP, client);
            current.craftTaken += result.getCount();
            current.phase = 2;
            current.phaseTick = current.ticks;
        } else if (current.phase == 2 && resultRevision > current.resultRevision && current.ticks - current.phaseTick >= 3) {
            if (current.craftTaken < current.outputRequested) {
                current.phase = 1;
            } else {
                if (!collectCraftRemainders(client, menu, current)) return;
                current.phase = 3;
                current.phaseTick = current.ticks;
            }
        } else if (current.phase == 3 && current.ticks - current.phaseTick >= 6) {
            if (!craftInventoryMatches(client, current)) {
                requestStop("failed", "craft_inventory_mismatch");
                return;
            }
            requestStop("completed", "crafted_verified");
        }
    }

    private static boolean validCraftEntries(JsonArray entries) {
        for (var raw : entries) {
            if (!raw.isJsonObject()) return false;
            JsonObject entry = raw.getAsJsonObject();
            int slot = integer(entry, "slot", -1);
            if (slot < 0 || slot >= 36 || integer(entry, "count", 0) <= 0 || string(entry, "item", "").isBlank()) return false;
        }
        return true;
    }

    private boolean craftInputs(Minecraft client, AbstractCraftingMenu menu, Run current) {
        if (!current.params.has("inputs") || !current.params.get("inputs").isJsonArray()) {
            requestStop("failed", "invalid_recipe_inputs");
            return false;
        }
        List<Integer> sourceSlots = new ArrayList<>();
        List<ItemStack> sourceItems = new ArrayList<>();
        for (var raw : current.params.getAsJsonArray("inputs")) {
            if (!raw.isJsonObject()) {
                requestStop("failed", "invalid_recipe_inputs");
                return false;
            }
            JsonObject input = raw.getAsJsonObject();
            int inventorySlot = integer(input, "slot", -1);
            int amount = integer(input, "count", 0);
            String wanted = string(input, "item", "").replace("minecraft:", "");
            int source = menuSlotForPlayer(menu, inventorySlot);
            if (inventorySlot < 0 || inventorySlot >= 36 || source < 0
                || source >= menu.slots.size() || menu.getSlot(source).getItem().isEmpty()
                || !itemPath(menu.getSlot(source).getItem()).equals(wanted)
                || menu.getSlot(source).getItem().getCount() < amount) {
                requestStop("failed", "craft_input_mismatch");
                return false;
            }
            for (int unit = 0; unit < amount; unit++) {
                sourceSlots.add(source);
                sourceItems.add(menu.getSlot(source).getItem().copy());
            }
        }
        RecipePlacement placement = findRecipePlacement(client, menu, current, sourceItems);
        if (placement == null) {
            requestStop("failed", "recipe_not_found_or_inputs_do_not_match");
            return false;
        }
        current.craftUnitCount = placement.outputCount;
        for (int index = 0; index < placement.gridSlots.size(); index++) {
            int gridSlot = placement.gridSlots.get(index);
            int source = sourceSlots.get(placement.sourceUnits.get(index));
            click(menu, source, 0, ContainerInput.PICKUP, client);
            click(menu, gridSlot, 1, ContainerInput.PICKUP, client);
            click(menu, source, 0, ContainerInput.PICKUP, client);
        }
        return true;
    }

    private RecipePlacement findRecipePlacement(Minecraft client, AbstractCraftingMenu menu, Run current, List<ItemStack> sourceItems) {
        String wantedOutput = string(current.params.getAsJsonObject("output"), "item", "").replace("minecraft:", "");
        var context = SlotDisplayContext.fromLevel(client.level);
        for (var collection : client.player.getRecipeBook().getCollections()) {
            for (RecipeDisplayEntry entry : collection.getRecipes()) {
                RecipeDisplay display = entry.display();
                ItemStack output = display.result().resolveForFirstStack(context);
                if (output.isEmpty() || !itemPath(output).equals(wantedOutput)) continue;
                int requested = integer(current.params.getAsJsonObject("output"), "count", 1);
                if (requested % output.getCount() != 0) continue;
                int crafts = requested / output.getCount();
                List<SlotDisplay> ingredients;
                int width;
                int height;
                if (display instanceof ShapedCraftingRecipeDisplay shaped) {
                    width = shaped.width();
                    height = shaped.height();
                    ingredients = shaped.ingredients();
                } else if (display instanceof ShapelessCraftingRecipeDisplay shapeless) {
                    width = Math.min(menu.getGridWidth(), shapeless.ingredients().size());
                    height = (shapeless.ingredients().size() + menu.getGridWidth() - 1) / menu.getGridWidth();
                    ingredients = shapeless.ingredients();
                } else {
                    continue;
                }
                if (width < 1 || height < 1 || width > menu.getGridWidth() || height > menu.getGridHeight()) continue;
                List<Integer> nonEmpty = new ArrayList<>();
                for (int craft = 0; craft < crafts; craft++) {
                    for (int index = 0; index < ingredients.size(); index++) {
                        if (!ingredients.get(index).resolveForStacks(context).isEmpty()) nonEmpty.add(index);
                    }
                }
                if (nonEmpty.size() != sourceItems.size()) continue;
                int[] assigned = new int[sourceItems.size()];
                java.util.Arrays.fill(assigned, -1);
                if (!assignRecipeUnits(ingredients, nonEmpty, context, sourceItems, 0, assigned, new boolean[sourceItems.size()])) continue;
                List<Integer> gridSlots = new ArrayList<>();
                List<Integer> units = new ArrayList<>();
                for (int requirement = 0; requirement < nonEmpty.size(); requirement++) {
                    int displayIndex = nonEmpty.get(requirement);
                    int row = displayIndex / width;
                    int column = displayIndex % width;
                    gridSlots.add(menu.getInputGridSlots().get(row * menu.getGridWidth() + column).index);
                    units.add(assigned[requirement]);
                }
                return new RecipePlacement(gridSlots, units, output.getCount());
            }
        }
        return null;
    }

    private static boolean assignRecipeUnits(
        List<SlotDisplay> ingredients,
        List<Integer> nonEmpty,
        net.minecraft.util.context.ContextMap context,
        List<ItemStack> sourceItems,
        int requirement,
        int[] assigned,
        boolean[] used
    ) {
        if (requirement >= nonEmpty.size()) return true;
        SlotDisplay display = ingredients.get(nonEmpty.get(requirement));
        List<ItemStack> accepted = display.resolveForStacks(context);
        for (int unit = 0; unit < sourceItems.size(); unit++) {
            if (used[unit]) continue;
            ItemStack source = sourceItems.get(unit);
            boolean match = accepted.stream().anyMatch(candidate -> ItemStack.isSameItem(candidate, source));
            if (!match) continue;
            used[unit] = true;
            assigned[requirement] = unit;
            if (assignRecipeUnits(ingredients, nonEmpty, context, sourceItems, requirement + 1, assigned, used)) return true;
            used[unit] = false;
        }
        return false;
    }

    private record RecipePlacement(List<Integer> gridSlots, List<Integer> sourceUnits, int outputCount) {}

    private boolean collectCraftRemainders(Minecraft client, AbstractCraftingMenu menu, Run current) {
        JsonArray remainders = current.params.has("remainders") ? current.params.getAsJsonArray("remainders") : new JsonArray();
        for (var raw : remainders) {
            JsonObject remainder = raw.getAsJsonObject();
            String item = string(remainder, "item", "").replace("minecraft:", "");
            int remaining = integer(remainder, "count", 1);
            int destination = menuSlotForPlayer(menu, integer(remainder, "slot", -1));
            if (destination < 0) { requestStop("failed", "invalid_remainder_slot"); return false; }
            for (Slot slot : menu.getInputGridSlots()) {
                if (!itemPath(slot.getItem()).equals(item)) continue;
                int amount = Math.min(remaining, slot.getItem().getCount());
                moveCount(client, menu, slot.index, destination, amount);
                remaining -= amount;
                if (remaining == 0) break;
            }
            if (remaining != 0) { requestStop("failed", "craft_remainder_missing"); return false; }
        }
        if (menu.getInputGridSlots().stream().anyMatch(Slot::hasItem)) {
            requestStop("failed", "craft_residue_remaining");
            return false;
        }
        return true;
    }

    private boolean craftInventoryMatches(Minecraft client, Run current) {
        JsonArray expected = current.craftBefore.deepCopy();
        for (var raw : current.params.getAsJsonArray("inputs")) {
            JsonObject input = raw.getAsJsonObject();
            JsonObject slot = expected.get(integer(input, "slot", -1)).getAsJsonObject();
            int left = integer(slot, "count", 0) - integer(input, "count", 0);
            slot.addProperty("count", left);
            slot.addProperty("empty", left == 0);
        }
        List<JsonObject> gains = new ArrayList<>();
        gains.add(current.params.getAsJsonObject("output"));
        if (current.params.has("remainders")) current.params.getAsJsonArray("remainders").forEach(raw -> gains.add(raw.getAsJsonObject()));
        for (JsonObject gain : gains) {
            JsonObject slot = expected.get(integer(gain, "slot", -1)).getAsJsonObject();
            slot.addProperty("item", string(gain, "item", ""));
            slot.addProperty("count", integer(slot, "count", 0) + integer(gain, "count", 0));
            slot.addProperty("empty", false);
        }
        JsonArray actual = inventorySlots(client);
        for (int index = 0; index < expected.size(); index++) {
            JsonObject want = expected.get(index).getAsJsonObject();
            JsonObject have = actual.get(index).getAsJsonObject();
            if (integer(want, "count", 0) != integer(have, "count", 0)) return false;
            if (integer(want, "count", 0) > 0
                && !itemPath(string(want, "item", "")).equals(itemPath(string(have, "item", "")))) return false;
        }
        return menuEmpty(client);
    }

    private static boolean menuEmpty(Minecraft client) {
        return client.player.containerMenu.getCarried().isEmpty();
    }

    private void tickMenuTransfer(Minecraft client, Run current) {
        if (current.phase == 0) {
            int botSlot = integer(current.params, "bot_slot", 0);
            if (botSlot < 0 || botSlot >= 36) {
                requestStop("failed", "invalid_transfer_slot");
                return;
            }
            BlockPos pos = positionParam(current.params, "pos", "target");
            current.menuRevision = menuContentRevision;
            openMenu(client, pos);
            current.phase = 1;
            current.phaseTick = current.ticks;
            return;
        }
        if (current.phase == 1) {
            if (!freshMenu(client, current.menuRevision)) return;
            current.openMenuId = client.player.containerMenu.containerId;
            if (current.kind.equals("furnaceTransfer") && !(client.player.containerMenu instanceof AbstractFurnaceMenu)) {
                requestStop("failed", "furnace_menu_missing");
                return;
            }
            if (!transferMenuSlots(client, current)) return;
            current.phase = 2;
            current.phaseTick = current.ticks;
            return;
        }
        if (current.phase == 2 && current.ticks - current.phaseTick >= 3) {
            if (client.player.containerMenu.containerId != current.openMenuId || !menuEmpty(client)) {
                requestStop("failed", "transfer_menu_changed_or_cursor_occupied");
                return;
            }
            // A new native open sends complete server contents, independently
            // of the click prediction and its optimized acknowledgement.
            client.player.closeContainer();
            current.menuRevision = menuContentRevision;
            openMenu(client, positionParam(current.params, "pos", "target"));
            current.phase = 3;
        } else if (current.phase == 3 && freshMenu(client, current.menuRevision)) {
            current.openMenuId = client.player.containerMenu.containerId;
            if (verifyMenuTransfer(client, current)) requestStop("completed", "transfer_verified");
            else requestStop("failed", "transfer_verification_failed");
        }
    }

    private boolean transferMenuSlots(Minecraft client, Run current) {
        AbstractContainerMenu menu = client.player.containerMenu;
        String direction = string(current.params, "direction", "");
        int containerSlot = integer(current.params, "container_slot", current.kind.equals("furnaceTransfer") ? 2 : 0);
        if (current.kind.equals("furnaceTransfer")) {
            String furnaceSlot = string(current.params, "furnace_slot", "output");
            containerSlot = furnaceSlot.equals("input") ? 0 : furnaceSlot.equals("fuel") ? 1 : 2;
            direction = direction.equals("bot_to_furnace") ? "bot_to_container"
                : direction.equals("furnace_to_bot") ? "container_to_bot" : direction;
        }
        int botSlot = integer(current.params, "bot_slot", 0);
        int source = menuSlotForContainer(menu, containerSlot);
        int destination = menuSlotForPlayer(menu, botSlot);
        if (direction.equals("bot_to_container")) {
            int swap = source;
            source = destination;
            destination = swap;
        } else if (!direction.equals("container_to_bot")) {
            requestStop("failed", "invalid_transfer_direction");
            return false;
        }
        if (source < 0 || destination < 0 || source >= menu.slots.size() || destination >= menu.slots.size()) {
            requestStop("failed", "invalid_transfer_slot");
            return false;
        }
        ItemStack sourceStack = menu.getSlot(source).getItem();
        if (sourceStack.isEmpty()) {
            requestStop("failed", "source_empty");
            return false;
        }
        String expectedItem = string(current.params, "item", "").replace("minecraft:", "");
        if (!expectedItem.isBlank() && !itemPath(sourceStack).equals(expectedItem)) {
            requestStop("failed", "transfer_source_changed");
            return false;
        }
        ItemStack destinationStack = menu.getSlot(destination).getItem();
        if (!destinationStack.isEmpty() && !ItemStack.isSameItemSameComponents(sourceStack, destinationStack)) {
            requestStop("failed", "destination_occupied");
            return false;
        }
        int requested = integer(current.params, "count", 0);
        int amount = requested <= 0 ? sourceStack.getCount() : requested;
        Slot destinationSlot = menu.getSlot(destination);
        if (amount > sourceStack.getCount() || !destinationSlot.mayPlace(sourceStack)
            || amount + destinationStack.getCount() > Math.min(integer(current.params, "max_stack", 64), destinationSlot.getMaxStackSize(sourceStack))) {
            requestStop("failed", "transfer_count_or_capacity_mismatch");
            return false;
        }
        current.transferSource = source;
        current.transferDestination = destination;
        current.transferItem = itemPath(sourceStack);
        current.transferBeforeSource = sourceStack.getCount();
        current.transferBeforeDestination = destinationStack.getCount();
        current.transferRequested = amount;
        current.transferToContainer = direction.equals("bot_to_container");
        current.transferContainerSlot = containerSlot;
        current.transferBotSlot = botSlot;
        moveCount(client, menu, source, destination, amount);
        return true;
    }

    private boolean verifyMenuTransfer(Minecraft client, Run current) {
        AbstractContainerMenu menu = client.player.containerMenu;
        if (current.transferSource < 0 || current.transferDestination < 0
            || current.transferSource >= menu.slots.size() || current.transferDestination >= menu.slots.size()) return false;
        ItemStack source = menu.getSlot(current.transferSource).getItem();
        ItemStack destination = menu.getSlot(current.transferDestination).getItem();
        boolean ok = source.getCount() == current.transferBeforeSource - current.transferRequested
            && destination.getCount() == current.transferBeforeDestination + current.transferRequested
            && itemPath(destination).equals(current.transferItem);
        if (!ok && current.kind.equals("furnaceTransfer") && current.transferToContainer
            && current.transferContainerSlot == 1 && menu instanceof AbstractFurnaceMenu furnace && furnace.isLit()) {
            ok = source.getCount() == current.transferBeforeSource - current.transferRequested
                && destination.getCount() <= current.transferBeforeDestination + current.transferRequested
                && (destination.isEmpty() || itemPath(destination).equals(current.transferItem));
        }
        return ok;
    }

    private static void moveCount(Minecraft client, AbstractContainerMenu menu, int source, int destination, int amount) {
        int sourceCount = menu.getSlot(source).getItem().getCount();
        click(menu, source, 0, ContainerInput.PICKUP, client);
        if (amount < sourceCount) {
            for (int index = 0; index < amount; index++) click(menu, destination, 1, ContainerInput.PICKUP, client);
            click(menu, source, 0, ContainerInput.PICKUP, client);
        } else click(menu, destination, 0, ContainerInput.PICKUP, client);
    }

    private void tickHandoff(Minecraft client, Run current) {
        String receiver = string(current.params, "receiver", "");
        String item = string(current.params, "item", "").replace("minecraft:", "");
        int slot = integer(current.params, "slot", client.player.getInventory().getSelectedSlot());
        int wanted = Math.max(1, integer(current.params, "count", 1));
        Entity recipient = current.targetEntity;
        for (Entity entity : client.level.getEntities(client.player, client.player.getBoundingBox().inflate(32.0), value -> true)) {
            if (recipient != null) break;
            if (entity.getName().getString().equals(receiver)) {
                recipient = entity;
                break;
            }
        }
        if (recipient == null || recipient.isRemoved() || !recipient.isAlive()) {
            requestStop("failed", "receiver_not_found");
            return;
        }
        current.targetEntity = recipient;
        if (current.phase == 0) {
            if (recipient == client.player) {
                requestStop("failed", "handoff_to_self");
                return;
            }
            if (!current.params.has("slot") && !itemPath(client.player.getMainHandItem()).equals(item)) {
                selectItem(client, item);
                return;
            }
            if (slot < 0 || slot >= 9) {
                requestStop("failed", "invalid_hotbar_slot");
                return;
            }
            if (client.player.distanceTo(recipient) > 1.5) {
                if (recipient instanceof LivingEntity living) approachCombatTarget(client, living, 1.0);
                return;
            }
            ItemStack stack = slot >= 0 && slot < client.player.getInventory().getContainerSize()
                ? client.player.getInventory().getItem(slot) : ItemStack.EMPTY;
            if (stack.isEmpty() || !itemPath(stack).equals(item) || count(client,item) < wanted
                || (current.params.has("slot") && stack.getCount() < wanted)) {
                requestStop("failed", "handoff_source_mismatch");
                return;
            }
            client.player.getInventory().setSelectedSlot(Math.max(0, Math.min(8, slot)));
            baritone.getPathingBehavior().cancelEverything();
            // Drop downward after rotation is sent; a horizontal throw can
            // pass the recipient before vanilla's pickup delay expires.
            lookAtDropPoint(client, recipient);
            current.handoffReceiverId = recipient.getUUID().toString();
            current.handoffReceiverName = recipient.getName().getString();
            current.handoffExpected = wanted;
            current.handoffRemaining = wanted;
            for (Entity entity : client.level.entitiesForRendering()) {
                if (entity instanceof ItemEntity) current.handoffExistingDrops.add(entity.getId());
            }
            current.phase = 1;
            current.phaseTick = current.ticks;
            return;
        }
        if (current.phase == 1) {
            // Let the client send the downward rotation before the drop
            // packet.  A vertical throw keeps the item in the receiver's
            // pickup radius through vanilla's ten-tick pickup delay.
            lookAtDropPoint(client, recipient);
            if (current.ticks - current.phaseTick < 2 || client.player.getXRot() < 85.0f) return;
            ItemStack held = client.player.getMainHandItem();
            if (held.isEmpty() || !itemPath(held).equals(item)) {
                requestStop("failed", "handoff_source_changed");
                return;
            }
            int batch = Math.min(held.getCount(), current.handoffRemaining);
            for (int index = 0; index < batch; index++) client.player.drop(false);
            current.handoffRemaining -= batch;
            if (current.handoffRemaining > 0) {
                selectItem(client, item);
                // Vanilla sends the newly selected hotbar slot on the next
                // client tick. Dropping immediately would use the old server slot.
                current.phaseTick = current.ticks;
                return;
            }
            // Vanilla permits the sender to pick up its own drops. Leave
            // that overlapping pickup area during the native pickup delay;
            // facing the receiver alone cannot transfer item ownership.
            current.goal = new GoalRunAway(3.0, recipient.blockPosition());
            baritone.getCustomGoalProcess().setGoalAndPath(current.goal);
            current.phase = 2;
            current.phaseTick = current.ticks;
            return;
        }
        if (current.goal.isInGoal(client.player.blockPosition()) && client.player.onGround()) {
            baritone.getPathingBehavior().cancelEverything();
            lookAtEntityGently(client, recipient);
        }
        for (Entity entity : client.level.entitiesForRendering()) {
            if (entity instanceof ItemEntity drop && !current.handoffExistingDrops.contains(drop.getId())
                && itemPath(drop.getItem()).equals(item) && drop.distanceToSqr(client.player) < 9.0) {
                current.handoffDrops.merge(drop.getId(), drop.getItem().getCount(), Math::max);
            }
        }
        if (current.handoffPickedUp >= current.handoffExpected
            && count(client, item) == current.before - current.handoffExpected) {
            current.handoffReceipt = true;
            lookAtEntity(client, recipient);
            requestStop("completed", "handoff_pickup_observed");
            return;
        }
        int waitTicks = Math.max(20, integer(current.params, "pickup_timeout_ticks", 100));
        if (current.handoffDrops.isEmpty() && current.ticks - current.phaseTick >= 20) {
            requestStop("failed", "handoff_drop_not_observed");
            return;
        }
        if (current.ticks - current.phaseTick >= waitTicks) {
            requestStop("failed", "receiver_pickup_unconfirmed");
        }
    }

    /** Called on the client thread before vanilla removes a picked-up item. */
    public static void observePickup(int itemId, int recipientId, int amount) {
        if (instance == null || instance.run == null) return;
        Run current = instance.run;
        Minecraft client = Minecraft.getInstance();
        if (!current.kind.equals("handoffItem") || current.phase != 2 || current.stopStatus != null
            || client.level == null) return;
        Entity recipient = client.level.getEntity(recipientId);
        Integer remaining = current.handoffDrops.get(itemId);
        if (recipient == null || !recipient.getUUID().toString().equals(current.handoffReceiverId)
            || remaining == null || amount <= 0) return;
        Entity item = client.level.getEntity(itemId);
        if (item instanceof ItemEntity drop) remaining = Math.max(remaining, drop.getItem().getCount());
        int received = Math.min(remaining, amount);
        current.handoffDrops.put(itemId, remaining - received);
        current.handoffPickedUp += received;
    }

    private static void lookAtDropPoint(Minecraft client, Entity recipient) {
        float yaw = (float) Math.toDegrees(Math.atan2(recipient.getZ() - client.player.getZ(),
            recipient.getX() - client.player.getX())) - 90.0f;
        BaritoneAPI.getProvider().getBaritoneForMinecraft(client).getLookBehavior()
            .updateTarget(new Rotation(yaw, 90.0f), true);
    }

    private static void lookAtEntityGently(Minecraft client, Entity target) {
        double dx = target.getX() - client.player.getX();
        double dy = target.getEyeY() - client.player.getEyeY();
        double dz = target.getZ() - client.player.getZ();
        float yaw = (float) Math.toDegrees(Math.atan2(dz, dx)) - 90.0f;
        float pitch = (float) -Math.toDegrees(Math.atan2(dy, Math.hypot(dx, dz)));
        yaw = client.player.getYRot() + Mth.clamp(Mth.wrapDegrees(yaw - client.player.getYRot()), -24.0f, 24.0f);
        pitch = client.player.getXRot() + Mth.clamp(pitch - client.player.getXRot(), -8.0f, 8.0f);
        BaritoneAPI.getProvider().getBaritoneForMinecraft(client).getLookBehavior()
            .updateTarget(new Rotation(yaw, pitch), true);
    }

    private static void beginAttention(Run current) {
        if (current.kind.equals("elytra")) return;
        var settings = BaritoneAPI.getSettings();
        current.attentionSettings = new boolean[] {settings.freeLook.value, settings.blockFreeLook.value,
            settings.remainWithExistingLookDirection.value, settings.smoothLook.value};
        current.attentionNoise = new double[] {settings.randomLooking.value, settings.randomLooking113.value};
        // Aim jitter changes which edge block is reachable and repeatedly resets survival mining.
        settings.randomLooking.value = 0.0;
        settings.randomLooking113.value = 0.0;
        settings.freeLook.value = false;
        settings.blockFreeLook.value = false;
        settings.remainWithExistingLookDirection.value = false;
        settings.smoothLook.value = false;
    }

    private static void restoreAttention(Run current) {
        if (current == null || current.attentionSettings == null) return;
        var settings = BaritoneAPI.getSettings();
        settings.freeLook.value = current.attentionSettings[0];
        settings.blockFreeLook.value = current.attentionSettings[1];
        settings.remainWithExistingLookDirection.value = current.attentionSettings[2];
        settings.smoothLook.value = current.attentionSettings[3];
        settings.randomLooking.value = current.attentionNoise[0];
        settings.randomLooking113.value = current.attentionNoise[1];
        current.attentionNoise = null;
        current.attentionSettings = null;
    }

    /** Pitch of a walk asked to look at the ground (look_down). */
    private static final float LOOK_DOWN_PITCH = 70.0f;

    /**
     * Camera update after upstream has applied movement/packet rotation.
     * A walk with look_down keeps its eyes on the ground: Baritone's walking
     * targets carry the current pitch (only the yaw steers), so the pitch set
     * here is what the next movement packet sends. Breaking, placing and
     * aimed uses ask with blockInteract and are left alone; the next walking
     * tick looks down again. Endermen anger at a crosshair on their head
     * (user 2026-11-05: 默认平视，传参之后就低头).
     */
    public static void smoothTravelLook(float previousYaw, boolean blockInteract) {
        if (instance == null || blockInteract) return;
        // The walker: the Run, or the legs walking under no Run / under a hand.
        Run walker = instance.run != null ? instance.run : instance.legs;
        if (walker == null || walker.stopStatus != null) return;
        Minecraft client = Minecraft.getInstance();
        if (client.player == null || client.player.isFallFlying() || walker.kind.equals("elytra")) return;
        float delta = Mth.wrapDegrees(client.player.getYRot() - previousYaw);
        client.player.setYRot(previousYaw + Mth.clamp(delta, -24.0f, 24.0f));
        if (walker.lookDown || (instance.gazeDown && instance.gazeFree(walker))) client.player.setXRot(LOOK_DOWN_PITCH);
    }

    /** Whether the gaze setting may hold the eyes down now: nothing aims (bow, shield, melee, an aimed use). */
    private boolean gazeFree(Run walker) {
        if (walker == null) return true;
        if (walker.kind.equals("rangedAttack") || walker.kind.equals("raiseShield") || walker.kind.equals("engageEntity")) return false;
        return !(walker.kind.equals("useItem") && walker.params != null && walker.params.has("look_at"));
    }

    /** Hand Runs: what a player does with its hands while it keeps walking (§6.1). */
    private boolean isHandRun(Run current) {
        if (current == null || !current.structured || current.params == null) return false;
        if (current.kind.equals("rangedAttack") || current.kind.equals("raiseShield")) return true;
        return current.kind.equals("useItem") && string(current.params, "mode", "once").equals("continuous")
            && !current.params.has("look_at");
    }

    private boolean legsWalking() {
        return legs != null && !legsPaused;
    }

    /** The server takes a shot's direction from the last rotation it was sent: send the aim now. */
    private static void sendAim(Minecraft client) {
        if (client.getConnection() == null) return;
        client.getConnection().send(new net.minecraft.network.protocol.game.ServerboundMovePlayerPacket.Rot(
            client.player.getYRot(), client.player.getXRot(), client.player.onGround(), client.player.horizontalCollision));
    }

    private String setGaze(JsonObject params) {
        gazeDown = booleanValue(params, "look_down", false);
        return "gaze_set";
    }

    /** setMovement: keep_moving (centre, radius, sprint) or stand; replaces the mode held now. */
    private String setMovement(Minecraft client, JsonObject params) {
        String mode = string(params, "mode", "keep_moving");
        if (!mode.equals("keep_moving") && !mode.equals("stand")) throw new IllegalArgumentException("invalid_movement_mode");
        if (legs != null) endLegs(client, "replaced", false);
        if (mode.equals("stand")) return "movement_set";
        JsonObject fixed = params.deepCopy();
        if (!(fixed.has("center") && fixed.get("center").isJsonArray() && fixed.getAsJsonArray("center").size() == 3)) {
            // The area is where the Body stands now, kept for the whole mode.
            fixed.add("center", xyz(client.player.position()));
        }
        Run mode_ = new Run("legs-" + (++legsSeq), "", null, Long.MAX_VALUE / 4);
        mode_.kind = "keepMoving";
        mode_.params = fixed;
        mode_.structured = true;
        mode_.lookDown = false;
        legs = mode_;
        legsMovedFrom = client.player.position();
        legsMovedTick = 0;
        legsWalked = 0.0;
        legsPaused = run != null && !isHandRun(run);
        if (!legsPaused) beginAttention(legs);
        return "movement_set";
    }

    private void pauseLegs() {
        legsPaused = true;
        baritone.getPathingBehavior().cancelEverything();
        restoreWaySettings(legs);
        legs.keepTarget = null;
    }

    /** One tick of the legs: idle gaze, resume after a leg action, walk, and end when they cannot move. */
    private void tickLegs(Minecraft client) {
        if (gazeDown && run == null && legs == null) client.player.setXRot(LOOK_DOWN_PITCH);
        if (legs == null) return;
        if (run != null && !isHandRun(run)) return;
        if (legsPaused) {
            legsPaused = false;
            legs.phase = 0;
            legs.keepTarget = null;
            legs.keepHeadingIn = false;
            if (legs.attentionSettings == null) beginAttention(legs);
            legsMovedFrom = client.player.position();
            legsMovedTick = legs.ticks;
            legsWalked = 0.0;
        }
        legs.ticks++;
        tickKeepMoving(client, legs);
        if (legs == null) return;
        // Walked, not displaced: a loop back to the same place is moving; climbing a
        // placed step is moving too.
        Vec3 at = client.player.position();
        legsWalked += at.distanceTo(legsMovedFrom);
        legsMovedFrom = at;
        if (legs.ticks - legsMovedTick >= LEGS_CANNOT_MOVE_TICKS) {
            if (legsWalked < 1.5) {
                endLegs(client, "cannot_move", true);
                return;
            }
            legsWalked = 0.0;
            legsMovedTick = legs.ticks;
        }
    }

    private void keepStop(Minecraft client, Run current, String reason) {
        if (current == legs) endLegs(client, reason, true);
        else requestStop("failed", reason);
    }

    /** The leg mode ends: replaced, death, or the Body cannot move (then it says where and why). */
    private void endLegs(Minecraft client, String reason, boolean emitEvent) {
        Run ended = legs;
        if (ended == null) return;
        legs = null;
        if (!legsPaused) {
            baritone.getPathingBehavior().cancelEverything();
            baritone.getInputOverrideHandler().clearAllKeys();
            restoreWaySettings(ended);
        }
        legsPaused = false;
        if (ended.attentionSettings != null) {
            if (run != null && run.attentionSettings != null) {
                // The Run saved the legs' settings: it puts the original ones back.
                run.attentionSettings = ended.attentionSettings;
                run.attentionNoise = ended.attentionNoise;
                ended.attentionSettings = null;
                ended.attentionNoise = null;
            } else if (run == null) {
                restoreAttention(ended);
            }
        }
        if (emitEvent) {
            JsonObject facts = state(client);
            facts.addProperty("reason", reason);
            facts.addProperty("mode", "keep_moving");
            facts.add("keep_moving", keepMovingFacts(ended));
            emitLifecycle(client, "movement_ended", facts);
            LOG.info("MINEBOT_BODY movement_ended reason={} facts={}", reason, keepMovingFacts(ended));
        }
    }

    private JsonObject movementFacts() {
        JsonObject facts = new JsonObject();
        if (legs == null) {
            facts.addProperty("mode", "stand");
            return facts;
        }
        facts.addProperty("mode", "keep_moving");
        facts.add("center", legs.params.getAsJsonArray("center").deepCopy());
        facts.addProperty("radius", clamp(number(legs.params, "radius", 12.0), 4.0, 32.0));
        facts.addProperty("sprint", booleanValue(legs.params, "sprint", true));
        facts.addProperty("since_ms", legs.elapsed());
        facts.addProperty("points_visited", legs.keepVisited);
        facts.addProperty("held_by_action", legsPaused);
        return facts;
    }

    /**
     * Out of lava the way a player gets out: face the dry cell and hold
     * forward and jump (jumping swims up in lava and climbs the bank). Baritone
     * plans no path that starts in lava: walking to the exit with it failed
     * at once and the player stayed in the lava (client GameTest 2026-10-20).
     * Done as soon as the player stands on ground out of the lava.
     * The exit is chosen again every tick from where the player is: the
     * given cell was dry when named, the lava flowed into it, and the player
     * stood in it walking to its own cell until the deadline and burned
     * (eval-20261028-ladder run-1, 15:27).
     */
    private void tickLeaveLava(Minecraft client, Run current) {
        JsonArray target = current.params.has("target") && current.params.get("target").isJsonArray()
            ? current.params.getAsJsonArray("target") : null;
        if (target == null || target.size() != 3) throw new IllegalArgumentException("invalid_lava_exit");
        if (!client.player.isInLava() && client.player.onGround()) {
            client.options.keyUp.setDown(false);
            client.options.keyJump.setDown(false);
            requestStop("completed", "out_of_lava");
            return;
        }
        BlockPos live = client.player.isInLava() ? nearestLavaExit(client) : null;
        if (live != null) {
            target = xyz(live);
            current.params.add("target", target);
        }
        double dx = target.get(0).getAsInt() + 0.5 - client.player.getX();
        double dz = target.get(2).getAsInt() + 0.5 - client.player.getZ();
        client.player.setYRot((float) (Math.atan2(dz, dx) * 180.0 / Math.PI) - 90.0f);
        client.player.setXRot(0.0f);
        client.player.setSprinting(false);
        client.options.keyUp.setDown(true);
        client.options.keyJump.setDown(true);
    }

    /**
     * One quick step, as a player taps a direction: face the named cell and
     * sprint to it, jumping onto a block one higher. Done standing within half
     * a block of the cell's centre; ended when half a second brings no
     * headway. Stepping on is Decision's choice in its next frame, not this
     * action's (dragon-unit-tests-20261030 §6, B).
     */
    private void tickStepTo(Minecraft client, Run current) {
        JsonArray target = current.params.has("target") && current.params.get("target").isJsonArray()
            ? current.params.getAsJsonArray("target") : null;
        if (target == null || target.size() != 3) throw new IllegalArgumentException("invalid_step_target");
        double dx = target.get(0).getAsInt() + 0.5 - client.player.getX();
        double dz = target.get(2).getAsInt() + 0.5 - client.player.getZ();
        double left = Math.sqrt(dx * dx + dz * dz);
        if (left <= 0.5 && client.player.onGround()) {
            client.options.keyUp.setDown(false);
            client.options.keyJump.setDown(false);
            client.player.setSprinting(false);
            requestStop("completed", "stepped");
            return;
        }
        if (current.ticks % 10 == 0) {
            if (current.stepLeft - left < 0.3) {
                client.options.keyUp.setDown(false);
                client.options.keyJump.setDown(false);
                client.player.setSprinting(false);
                requestStop("failed", "step_blocked");
                return;
            }
            current.stepLeft = left;
        }
        client.player.setYRot((float) (Math.atan2(dz, dx) * 180.0 / Math.PI) - 90.0f);
        // A step asked to look at the ground keeps its eyes there (look_down; the
        // Agent's set_walk_gaze reaches Decision's steps): endermen anger at a
        // crosshair on their head (user 2026-11-05: 死亡之后刷新).
        client.player.setXRot(current.lookDown ? LOOK_DOWN_PITCH : 0.0f);
        client.player.setSprinting(left > 1.5);
        client.options.keyUp.setDown(true);
        BlockPos ahead = BlockPos.containing(client.player.getX() + dx / left * 0.7, client.player.getY() + 0.01,
            client.player.getZ() + dz / left * 0.7);
        client.options.keyJump.setDown(client.player.onGround() && !clearOfHazard(client, ahead)
            && clearOfHazard(client, ahead.above()));
    }

    // keep_moving: how far ahead the next point is, how long a place that hurt
    // is avoided, how near a drop may be and how deep a drop counts as one.
    private static final int[] KEEP_STEPS = {4, 6, 8};
    private static final int KEEP_DIRECTIONS = 16;
    private static final long KEEP_HURT_TICKS = 200;
    private static final int KEEP_DROP_MARGIN = 2;
    private static final int KEEP_MAX_DROP = 4;
    private static final int KEEP_NO_POINT_TICKS = 20;

    /**
     * Keep moving around an area, as a player in a fight does: walk to a
     * point a few blocks off, and on arriving at once to the next one, in a
     * new direction and not back where the Body just was. Each point lies in
     * the circle, has a floor the Body stands on, and solid ground below
     * every cell within two blocks of it (no drop to the void or deeper than
     * four blocks beside it); none is where the Body was hurt in the last ten
     * seconds by its place (not a mob's blow) or inside an area-effect cloud.
     * Baritone walks there without breaking or placing, sprinting when asked.
     * The action never ends by itself: it is stopped by its caller, and when
     * no point qualifies it says so (no_safe_point) instead of standing.
     * Nothing here knows of enemies; whom to fight and when is the caller's.
     */
    private void tickKeepMoving(Minecraft client, Run current) {
        var player = client.player;
        if (current.phase == 0) {
            JsonArray center = current.params.has("center") && current.params.get("center").isJsonArray()
                ? current.params.getAsJsonArray("center") : null;
            current.keepCenter = center != null && center.size() == 3
                ? new Vec3(center.get(0).getAsDouble(), center.get(1).getAsDouble(), center.get(2).getAsDouble())
                : player.position();
            current.keepRadius = clamp(number(current.params, "radius", 12.0), 4.0, 32.0);
            current.keepSprint = booleanValue(current.params, "sprint", true);
            var settings = BaritoneAPI.getSettings();
            current.wayPreviousAllowBreak = settings.allowBreak.value;
            current.wayPreviousAllowPlace = settings.allowPlace.value;
            current.keepPreviousSprint = settings.allowSprint.value;
            settings.allowBreak.value = false;
            settings.allowPlace.value = false;
            settings.allowSprint.value = current.keepSprint;
            current.keepRandom = new java.util.Random(current.id.hashCode());
            current.keepCheckPos = player.position();
            current.keepCheckTick = current.ticks;
            current.phase = 1;
        }
        boolean outside = Math.hypot(player.getX() - current.keepCenter.x, player.getZ() - current.keepCenter.z)
            > current.keepRadius;
        if (!outside) current.keepEntered = true;
        // Walking into the circle is getting there, like move_to: blocks may be placed
        // (a gap, a step up from the End's arrival platform); inside, the walk never builds.
        BaritoneAPI.getSettings().allowPlace.value = outside;
        if (current.keepHeadingIn) {
            // No point inside the circle was seen yet: look again every half second
            // on the way; Baritone stopping short ends it honestly.
            if (!outside) {
                current.keepHeadingIn = false;
            } else if (current.ticks - current.keepHeadingInTick > 20 && !baritone.getCustomGoalProcess().isActive()
                && !baritone.getPathingBehavior().isPathing()) {
                keepStop(client, current, "no_safe_point");
                return;
            } else if (current.ticks % 10 != 0) {
                return;
            }
        }
        boolean pick = current.keepTarget == null;
        if (current.keepTarget != null) {
            double dx = current.keepTarget.getX() + 0.5 - player.getX();
            double dz = current.keepTarget.getZ() + 0.5 - player.getZ();
            boolean arrived = Math.sqrt(dx * dx + dz * dz) <= 1.6
                && Math.abs(current.keepTarget.getY() - player.getY()) <= 1.5;
            if (arrived) {
                // Arrived at a walk-in point and still outside: not that one again.
                if (current.keepApproaching && outside) current.keepBad.add(current.keepTarget);
                if (!current.keepApproaching) {
                    current.keepVisited++;
                    current.keepRecent.addLast(current.keepTarget);
                    while (current.keepRecent.size() > 4) current.keepRecent.removeFirst();
                }
                pick = true;
            } else if (current.ticks - current.keepGoalTick > 10 && !baritone.getCustomGoalProcess().isActive()
                && !baritone.getPathingBehavior().isPathing()) {
                // Baritone gave the point up (no route without breaking or placing).
                current.keepBad.add(current.keepTarget);
                pick = true;
            } else if (current.ticks % 5 == 0 && keepPointRejection(client, current, current.keepTarget, null) != null) {
                // A cloud spread over it, or the Body was hurt there on the way.
                current.keepRepicks++;
                pick = true;
            } else if (current.keepApproaching && !outside) {
                // In the circle now: the ordinary walk from point to point starts here.
                pick = true;
            }
        }
        if (current.ticks - current.keepCheckTick >= 20) {
            Vec3 at = player.position();
            double moved = at.distanceTo(current.keepCheckPos);
            if (moved < 0.6 && current.keepTarget != null && !pick) {
                current.keepStalls++;
                current.keepBad.add(current.keepTarget);
                pick = true;
            }
            current.keepCheckPos = at;
            current.keepCheckTick = current.ticks;
        }
        while (current.keepBad.size() > 16) current.keepBad.remove(0);
        if (!pick) {
            current.keepNoPointTicks = 0;
            return;
        }
        // Outside the circle, first walk into it; inside, the next point near the
        // Body, and when none qualifies there, any point the circle holds.
        BlockPos next = outside ? null : keepMovingPoint(client, current);
        if (next == null) next = keepAreaPoint(client, current, !outside);
        current.keepApproaching = outside && next != null;
        if (next == null && outside) {
            if (!current.keepHeadingIn) {
                current.keepHeadingIn = true;
                current.keepHeadingInTick = current.ticks;
                current.keepTarget = null;
                baritone.getCustomGoalProcess().setGoalAndPath(new GoalXZ(
                    (int) Math.floor(current.keepCenter.x), (int) Math.floor(current.keepCenter.z)));
            }
            return;
        }
        current.keepHeadingIn = false;
        if (next == null) {
            current.keepTarget = null;
            if (++current.keepNoPointTicks >= KEEP_NO_POINT_TICKS) keepStop(client, current, "no_safe_point");
            return;
        }
        current.keepNoPointTicks = 0;
        current.keepHeading = Math.atan2(next.getZ() + 0.5 - player.getZ(), next.getX() + 0.5 - player.getX());
        current.keepTarget = next;
        current.keepGoalTick = current.ticks;
        baritone.getCustomGoalProcess().setGoalAndPath(new GoalNear(next, 1));
    }

    /** The next point to walk to, or null; counts why candidates were refused. */
    private BlockPos keepMovingPoint(Minecraft client, Run current) {
        var player = client.player;
        BlockPos feet = player.blockPosition();
        JsonObject rejected = new JsonObject();
        java.util.List<AreaEffectCloud> clouds = new ArrayList<>();
        for (Entity entity : client.level.entitiesForRendering()) {
            if (entity instanceof AreaEffectCloud cloud) clouds.add(cloud);
        }
        BlockPos best = null, bestRecent = null;
        double bestScore = -1.0, bestRecentScore = -1.0;
        double offset = current.keepRandom.nextDouble() * Math.PI * 2.0 / KEEP_DIRECTIONS;
        double fromCenter = Math.hypot(player.getX() - current.keepCenter.x, player.getZ() - current.keepCenter.z);
        for (int d = 0; d < KEEP_DIRECTIONS; d++) {
            double angle = offset + d * Math.PI * 2.0 / KEEP_DIRECTIONS;
            for (int step : KEEP_STEPS) {
                int x = (int) Math.floor(player.getX() + Math.cos(angle) * step);
                int z = (int) Math.floor(player.getZ() + Math.sin(angle) * step);
                BlockPos candidate = keepStandable(client, x, feet.getY(), z);
                String why = candidate == null ? "no_floor" : keepPointRejection(client, current, candidate, clouds);
                if (why == null) {
                    for (BlockPos bad : current.keepBad) {
                        if (bad.distSqr(candidate) <= 2.0) why = "no_route";
                    }
                }
                boolean recent = false;
                if (why == null) {
                    for (BlockPos was : current.keepRecent) {
                        if (Math.hypot(was.getX() - candidate.getX(), was.getZ() - candidate.getZ()) < 3.0) recent = true;
                    }
                }
                if (why != null) {
                    rejected.addProperty(why, (rejected.has(why) ? rejected.get(why).getAsInt() : 0) + 1);
                    continue;
                }
                double heading = Math.atan2(candidate.getZ() + 0.5 - player.getZ(), candidate.getX() + 0.5 - player.getX());
                double score = current.keepRandom.nextDouble();
                if (!Double.isNaN(current.keepHeading)) {
                    double turn = Math.abs(Mth.wrapDegrees(Math.toDegrees(heading - current.keepHeading)));
                    // A new direction, neither straight on nor straight back.
                    score += turn >= 45.0 && turn <= 135.0 ? 1.0 : turn < 45.0 ? 0.5 : 0.0;
                }
                if (fromCenter > current.keepRadius * 0.6) {
                    double toCenter = Math.hypot(candidate.getX() + 0.5 - current.keepCenter.x, candidate.getZ() + 0.5 - current.keepCenter.z);
                    score += clamp((fromCenter - toCenter) / 8.0, -1.0, 1.0);
                }
                if (recent) {
                    if (score > bestRecentScore) {
                        bestRecent = candidate;
                        bestRecentScore = score;
                    }
                } else if (score > bestScore) {
                    best = candidate;
                    bestScore = score;
                }
            }
        }
        current.keepRejections = rejected;
        // Back to a point just visited only when nothing else qualifies.
        return best != null ? best : bestRecent;
    }

    /**
     * A point anywhere in the circle, sampled on rings from its centre, with
     * the floor searched well above and below both the Body and the centre
     * (End islands rise and fall). The nearest to the Body wins; inside the
     * circle, one at least three blocks off, so the Body still moves.
     */
    private BlockPos keepAreaPoint(Minecraft client, Run current, boolean inside) {
        var player = client.player;
        int top = (int) Math.floor(Math.max(player.getY(), current.keepCenter.y)) + 12;
        int bottom = (int) Math.floor(Math.min(player.getY(), current.keepCenter.y)) - 12;
        java.util.List<AreaEffectCloud> clouds = new ArrayList<>();
        for (Entity entity : client.level.entitiesForRendering()) {
            if (entity instanceof AreaEffectCloud cloud) clouds.add(cloud);
        }
        JsonObject rejected = inside ? current.keepRejections.deepCopy() : new JsonObject();
        BlockPos best = null;
        double bestDistance = Double.MAX_VALUE;
        double[] rings = {0.0, 0.3, 0.55, 0.8, 0.95};
        for (double ring : rings) {
            int directions = ring == 0.0 ? 1 : KEEP_DIRECTIONS;
            for (int d = 0; d < directions; d++) {
                double angle = d * Math.PI * 2.0 / KEEP_DIRECTIONS;
                int x = (int) Math.floor(current.keepCenter.x + Math.cos(angle) * ring * current.keepRadius);
                int z = (int) Math.floor(current.keepCenter.z + Math.sin(angle) * ring * current.keepRadius);
                BlockPos candidate = keepColumnFloor(client, x, z, top, bottom);
                String why = candidate == null ? "no_floor" : keepPointRejection(client, current, candidate, clouds);
                if (why == null) {
                    for (BlockPos bad : current.keepBad) {
                        if (bad.distSqr(candidate) <= 2.0) why = "no_route";
                    }
                }
                double distance = candidate == null ? 0.0
                    : Math.hypot(candidate.getX() + 0.5 - player.getX(), candidate.getZ() + 0.5 - player.getZ());
                if (why == null && inside && distance < 3.0) why = "too_close";
                // Walking in: a point well inside, so arriving there is being in the circle.
                if (why == null && !inside && Math.hypot(candidate.getX() + 0.5 - current.keepCenter.x,
                    candidate.getZ() + 0.5 - current.keepCenter.z) > current.keepRadius - 2.0) why = "near_edge";
                if (why != null) {
                    String key = "area_" + why;
                    rejected.addProperty(key, (rejected.has(key) ? rejected.get(key).getAsInt() : 0) + 1);
                    continue;
                }
                if (distance < bestDistance) {
                    best = candidate;
                    bestDistance = distance;
                }
            }
        }
        current.keepRejections = rejected;
        return best;
    }

    /** The feet cell to stand in at column (x, z), up to three above or four below the Body's feet. */
    private static BlockPos keepStandable(Minecraft client, int x, int feetY, int z) {
        return keepColumnFloor(client, x, z, feetY + 3, feetY - 4);
    }

    /** The highest feet cell to stand in at column (x, z) between top and bottom, or null when none or unloaded. */
    private static BlockPos keepColumnFloor(Minecraft client, int x, int z, int top, int bottom) {
        for (int y = top; y >= bottom; y--) {
            BlockPos feet = new BlockPos(x, y, z);
            if (!clientChunkLoaded(client, feet)) return null;
            if (clearOfHazard(client, feet) && clearOfHazard(client, feet.above()) && groundUnder(client, feet)) return feet;
        }
        return null;
    }

    /** Why a point is not one to walk to now, or null when it is. */
    private String keepPointRejection(Minecraft client, Run current, BlockPos point, java.util.List<AreaEffectCloud> clouds) {
        double cx = point.getX() + 0.5, cz = point.getZ() + 0.5;
        if (Math.hypot(cx - current.keepCenter.x, cz - current.keepCenter.z) > current.keepRadius) return "outside_area";
        for (int dx = -KEEP_DROP_MARGIN; dx <= KEEP_DROP_MARGIN; dx++) {
            for (int dz = -KEEP_DROP_MARGIN; dz <= KEEP_DROP_MARGIN; dz++) {
                BlockPos column = point.offset(dx, 0, dz);
                if (!clientChunkLoaded(client, column)) return "near_drop";
                boolean support = false;
                for (int dy = 1; dy >= -(KEEP_MAX_DROP + 1) && !support; dy--) {
                    BlockPos at = column.above(dy);
                    BlockState state = client.level.getBlockState(at);
                    if (Math.abs(dx) <= 1 && Math.abs(dz) <= 1 && dy >= -1 && (state.getFluidState().is(FluidTags.LAVA)
                        || state.is(net.minecraft.tags.BlockTags.FIRE) || state.is(net.minecraft.world.level.block.Blocks.MAGMA_BLOCK)
                        || state.is(net.minecraft.tags.BlockTags.CAMPFIRES))) return "hazard";
                    support = !state.getCollisionShape(client.level, at).isEmpty() || state.getFluidState().is(FluidTags.WATER);
                }
                if (!support) return "near_drop";
            }
        }
        long now = client.level.getGameTime();
        synchronized (placeHurts) {
            for (double[] hurt : placeHurts) {
                if (now - (long) hurt[0] > KEEP_HURT_TICKS) continue;
                if (Math.hypot(hurt[1] - cx, hurt[3] - cz) <= 2.5 && Math.abs(hurt[2] - point.getY()) <= 2.5) return "hurt_recently";
            }
        }
        if (clouds == null) {
            clouds = new ArrayList<>();
            for (Entity entity : client.level.entitiesForRendering()) {
                if (entity instanceof AreaEffectCloud cloud) clouds.add(cloud);
            }
        }
        for (AreaEffectCloud cloud : clouds) {
            if (Math.hypot(cloud.getX() - cx, cloud.getZ() - cz) <= cloud.getRadius() + 1.5
                && Math.abs(cloud.getY() - point.getY()) <= 3.0) return "cloud";
        }
        return null;
    }

    private static JsonObject keepMovingFacts(Run current) {
        JsonObject facts = new JsonObject();
        if (current.keepCenter != null) facts.add("center", xyz(new Vec3(round2(current.keepCenter.x),
            round2(current.keepCenter.y), round2(current.keepCenter.z))));
        facts.addProperty("radius", current.keepRadius);
        facts.addProperty("sprint", current.keepSprint);
        facts.addProperty("points_visited", current.keepVisited);
        facts.addProperty("stalls", current.keepStalls);
        facts.addProperty("repicks", current.keepRepicks);
        facts.addProperty("approaching", current.keepApproaching || current.keepHeadingIn);
        facts.addProperty("entered_area", current.keepEntered);
        if (current.keepTarget != null) facts.add("walking_to", xyz(current.keepTarget));
        facts.add("last_refusals", current.keepRejections.deepCopy());
        return facts;
    }

    /**
     * Cushion a fall with an item, as a player does with a water bucket: hold
     * it, look straight down, and use it the tick the ground comes within
     * reach, so the Body lands in the water, then take the water back into the
     * bucket as a player does. Ends after that, saying whether the water was
     * there and whether it came back (water_taken_back). Whether to do it is
     * Decision's choice from the Body's fall fact (dragon-unit-tests-20261030
     * §6.y, form B). Taking the water back was left as the next choice and a
     * fight was chosen instead: the bucket stayed empty from 12:39:48 and the
     * three later throws had nothing to pour (end run-35).
     */
    private void tickCushionFall(Minecraft client, Run current) {
        var player = client.player;
        String item = current.item.isBlank() ? "water_bucket" : current.item;
        if (!item.equals("water_bucket")) throw new IllegalArgumentException("unsupported_cushion_item");
        if (current.cushionAt != null && (player.isInWater() || current.cushionScoopTicks > 0)) {
            tickCushionScoop(client, current);
            return;
        }
        if (player.isInWater()) {
            requestStop("failed", "landed_in_other_water");
            return;
        }
        if (player.onGround()) {
            requestStop("failed", current.ticks <= 1 && current.cushionAt == null ? "not_falling"
                : current.cushionAt != null ? "landed_beside_water" : "landed_before_water");
            return;
        }
        if (current.cushionAt != null) return;
        if (!itemPath(player.getMainHandItem()).equals(item)) {
            selectItem(client, item);
            return;
        }
        player.setXRot(90.0f);
        net.minecraft.world.phys.HitResult hit = player.pick(player.blockInteractionRange(), 1.0f, false);
        if (hit instanceof BlockHitResult block && hit.getType() == HitResult.Type.BLOCK
            && block.getDirection() == net.minecraft.core.Direction.UP) {
            client.gameMode.useItem(player, InteractionHand.MAIN_HAND);
            if (!itemPath(player.getMainHandItem()).equals(item)) current.cushionAt = block.getBlockPos().above();
        }
    }

    /** Landed in the poured water: aim at it and use the empty bucket until it
     * is full again, for at most 20 ticks (the flow carries the Body off the
     * source within a second). The water leaves the cell a tick or two before
     * the bucket fills in the hand, so once used it waits for the hand. The
     * landing was cushioned either way. */
    private void tickCushionScoop(Minecraft client, Run current) {
        var player = client.player;
        current.cushionScoopTicks++;
        String hand = itemPath(player.getMainHandItem());
        if (hand.equals("water_bucket")) {
            current.cushionScooped = true;
            requestStop("completed", "cushioned");
            return;
        }
        var fluid = client.level.getFluidState(current.cushionAt);
        boolean water = fluid.is(FluidTags.WATER) && fluid.isSource();
        if (current.cushionScoopTicks > 20 || (!water && !current.cushionScoopUsed)) {
            requestStop("completed", "cushioned");
            return;
        }
        if (!water) return;
        if (!hand.equals("bucket")) {
            if (count(client, "bucket") == 0) { requestStop("completed", "cushioned"); return; }
            selectItem(client, "bucket");
            return;
        }
        Vec3 eye = player.getEyePosition();
        double dx = current.cushionAt.getX() + 0.5 - eye.x, dy = current.cushionAt.getY() + 0.4 - eye.y,
            dz = current.cushionAt.getZ() + 0.5 - eye.z;
        player.setYRot((float) (Math.atan2(dz, dx) * 180.0 / Math.PI) - 90.0f);
        player.setXRot((float) -(Math.atan2(dy, Math.sqrt(dx * dx + dz * dz)) * 180.0 / Math.PI));
        // Every fourth tick: the server's answer fills the hand a tick or two later.
        if ((current.cushionScoopTicks - 1) % 4 == 0) {
            client.gameMode.useItem(player, InteractionHand.MAIN_HAND);
            current.cushionScoopUsed = true;
        }
    }

    private void tickVertical(Minecraft client, Run current) {
        if (client.player.isInLava()) {
            requestStop("failed", "vertical_liquid_hazard");
            return;
        }
        if (tickSealWater(client, current)) return;
        if (current.swimAbandoned && current.ticks % 10 == 0 && current.makeWaySteps < 16) {
            // Climbing past a water source, block it before it fills the column again.
            BlockPos feet = client.player.blockPosition();
            boolean source = false;
            for (BlockPos cell : List.of(feet, feet.above())) for (Direction side : Direction.values()) {
                var fluid = client.level.getFluidState(cell.relative(side));
                source |= fluid.is(FluidTags.WATER) && fluid.isSource();
            }
            if (source && queueWaterSeal(client, current, List.of(feet, feet.above()))) {
                current.makeWaySteps++;
                return;
            }
        }
        if (current.kind.equals("ascend") && !current.swimAbandoned && (current.swimmingUp || client.player.isInWater())) {
            tickSwimUp(client, current);
            return;
        }
        if (current.phase == 0) {
            current.anchor = client.player.blockPosition();
            if (current.params.has("scaffold_blocks")) {
                List<net.minecraft.world.item.Item> allowed = new ArrayList<>();
                for (var name : current.params.getAsJsonArray("scaffold_blocks")) {
                    allowed.add(new BlockOptionalMeta(name.getAsString()).getBlock().asItem());
                }
                current.previousThrowawayItems = BaritoneAPI.getSettings().acceptableThrowawayItems.value;
                BaritoneAPI.getSettings().acceptableThrowawayItems.value = allowed;
            }
            current.verticalDistance = Math.max(1, Math.min(128, integer(current.params, "max_distance", 64)));
            int targetY = integer(current.params, "target_y",
                current.kind.equals("digUp") ? current.anchor.getY() + 1 : current.anchor.getY());
            if (current.kind.equals("ascend")) targetY = findSurfaceY(client, current);
            if (targetY == Integer.MIN_VALUE) {
                current.sameAgainHere = true;
                requestStop("failed", "surface_not_found");
                return;
            }
            if (Math.abs(targetY - current.anchor.getY()) > current.verticalDistance
                || targetY < client.level.getMinY() || targetY + 1 >= client.level.getMaxY()) {
                current.sameAgainHere = true;
                requestStop("failed", "vertical_target_out_of_bounds");
                return;
            }
            if (hasVerticalLiquidHazard(client, new BlockPos(current.anchor.getX(), targetY, current.anchor.getZ()))) {
                requestStop("failed", "vertical_liquid_hazard");
                return;
            }
            current.goal = new GoalBlock(current.anchor.getX(), targetY, current.anchor.getZ());
            current.phase = 1;
            current.phaseTick = current.ticks;
            baritone.getCustomGoalProcess().setGoalAndPath(current.goal);
        }
        if (current.goal.isInGoal(client.player.blockPosition()) && client.player.onGround()) {
            current.verticalSettledTicks++;
            if (current.verticalSettledTicks >= 5) {
                requestStop("completed", current.kind.equals("digDown") ? "target_depth_reached"
                    : current.kind.equals("ascend") ? "surface_reached" : "target_height_reached");
            }
        } else {
            current.verticalSettledTicks = 0;
            if (client.player.onGround() && !current.goal.isInGoal(client.player.blockPosition())
                && !processActive() && !baritone.getPathingBehavior().isPathing()) {
                if (++current.verticalIdleTicks >= 10) requestStop("failed", "vertical_no_path");
            } else current.verticalIdleTicks = 0;
        }
    }

    /** Swim up, using upstream water pathing to go around a closed column. */
    private void tickSwimUp(Minecraft client, Run current) {
        if (current.phase == 0) {
            current.anchor = client.player.blockPosition();
            current.verticalDistance = Math.max(1, Math.min(128, integer(current.params, "max_distance", 64)));
            current.swimmingUp = true;
            current.swimPreviousAir = client.player.getAirSupply();
            current.swimBestY = client.player.getY();
            current.swimProgressPosition = client.player.position();
            current.phase = 1;
            current.phaseTick = current.ticks;
        }
        BlockPos eye = BlockPos.containing(client.player.getEyePosition());
        boolean chunkLoaded = clientChunkLoaded(client, eye);
        if (!SurfaceSwimProgress.allowed(false, chunkLoaded, eye.getY(),
                client.level.getMinY(), client.level.getMaxY())) {
            client.options.keyJump.setDown(false);
            requestStop("failed", !chunkLoaded ? "vertical_chunk_unloaded"
                : "vertical_target_out_of_bounds");
            return;
        }
        int air = client.player.getAirSupply();
        boolean headOut = client.level.getFluidState(eye).isEmpty();
        if (SurfaceSwimProgress.completed(headOut, current.swimPreviousAir, air, client.player.getMaxAirSupply())
            && dryGroundArrival(client)) {
            client.options.keyJump.setDown(false);
            requestStop("completed", "surface_reached");
            return;
        }
        current.swimPreviousAir = air;
        if (current.swimPathFailed) {
            // Upstream has already proved these goal cells unreachable. Try
            // the next ring immediately rather than wait motionless for the
            // ordinary physical stall detector to expire again.
            if (!swimPathStartClear(client)) {
                // The jump has pushed the player onto the next cell boundary
                // under a ceiling. Let them sink into the clear cell before
                // retrying these goals; this is not an unreachable exit.
                current.swimRoutePending = true;
                current.swimPathFailed = false;
                baritone.getPathingBehavior().cancelEverything();
                baritone.getInputOverrideHandler().clearAllKeys();
            } else if (!routeSwimToAir(client, current)) {
                if (swimMakeWay(client, current)) return;
                requestStop("failed", "swim_surface_route_not_found");
                return;
            }
            current.swimNoRiseTicks = 0;
        }
        if (current.swimRoutePending) {
            client.options.keyJump.setDown(false);
            if (!swimPathStartClear(client)) return;
            current.swimRoutePending = false;
            baritone.getCustomGoalProcess().setGoalAndPath(current.swimRouteGoal);
        }
        // During a detour upstream controls all movement, including jumping in
        // water. Once it reaches the open column, jump through the final air
        // boundary, which upstream's water-pillar movement does not support.
        if (current.swimRouteGoal != null && current.swimRouteGoal.isInGoal(client.player.blockPosition())) {
            baritone.getPathingBehavior().cancelEverything();
            baritone.getInputOverrideHandler().clearAllKeys();
            current.swimRouteGoal = null;
            current.phase = 3;
        }
        client.options.keyJump.setDown(current.swimRouteGoal == null);
        double y = client.player.getY();
        boolean movedOnRoute = current.swimRouteGoal != null
            && client.player.position().distanceToSqr(current.swimProgressPosition) > 0.01;
        if (y > current.swimBestY + 0.05 || movedOnRoute) {
            current.swimBestY = y;
            current.swimProgressPosition = client.player.position();
            current.swimNoRiseTicks = 0;
        } else if (SurfaceSwimProgress.blocked(++current.swimNoRiseTicks)) {
            client.options.keyJump.setDown(false);
            if (!routeSwimToAir(client, current)) {
                if (swimMakeWay(client, current)) return;
                requestStop("failed", "swim_surface_route_not_found");
                return;
            }
            current.swimNoRiseTicks = 0;
            current.swimProgressPosition = client.player.position();
        }
        if (client.player.getY() - current.anchor.getY() > current.verticalDistance) {
            client.options.keyJump.setDown(false);
            requestStop("failed", "vertical_target_out_of_bounds");
        }
    }

    private static boolean dryGroundArrival(Minecraft client) {
        BlockPos feet = client.player.blockPosition();
        BlockPos head = BlockPos.containing(client.player.getEyePosition());
        BlockPos support = feet.below();
        return client.player.onGround()
            && client.level.getFluidState(feet).isEmpty()
            && client.level.getFluidState(support).isEmpty()
            && !client.level.getBlockState(support).getCollisionShape(client.level, support).isEmpty()
            && client.level.getBlockState(head).isAir();
    }

    private SurfaceSwimProgress.Space swimSpace(Minecraft client) {
        return new SurfaceSwimProgress.Space() {
            private boolean clear(BlockPos pos) {
                return clientChunkLoaded(client, pos)
                    && client.level.getBlockState(pos).getCollisionShape(client.level, pos).isEmpty();
            }
            public boolean water(int x, int y, int z) {
                BlockPos pos = new BlockPos(x, y, z);
                return clear(pos) && client.level.getFluidState(pos).is(FluidTags.WATER);
            }
            public boolean breathable(int x, int y, int z) {
                BlockPos pos = new BlockPos(x, y, z);
                return clear(pos) && client.level.getFluidState(pos).isEmpty();
            }
        };
    }

    private boolean routeSwimToAir(Minecraft client, Run current) {
        SurfaceSwimProgress.Space space = swimSpace(client);
        var anchor = new SurfaceSwimProgress.Cell(current.anchor.getX(), current.anchor.getY(), current.anchor.getZ());
        var targets = SurfaceSwimProgress.routeTargets(space, anchor, client.level.getMinY(),
            client.level.getMaxY(), current.verticalDistance, current.swimNextRing);
        if (targets.isEmpty()) return false;
        var settings = BaritoneAPI.getSettings();
        if (current.swimPreviousAllowBreak == null) {
            current.swimPreviousAllowBreak = settings.allowBreak.value;
            current.swimPreviousAllowPlace = settings.allowPlace.value;
            current.swimPreviousSubmergedNavigation = settings.allowSubmergedNavigation.value;
        }
        // An emergency detour must not dig through somebody's ceiling or place
        // blocks. Restore the caller's settings on every stop path.
        settings.allowBreak.value = false;
        settings.allowPlace.value = false;
        settings.allowSubmergedNavigation.value = true;
        var first = targets.getFirst();
        current.swimNextRing = Math.max(Math.abs(first.x() - anchor.x()), Math.abs(first.z() - anchor.z())) + 1;
        current.swimRouteGoal = new GoalComposite(targets.stream()
            .map(p -> (Goal) new GoalBlock(p.x(), p.y(), p.z())).toArray(Goal[]::new));
        current.goal = current.swimRouteGoal;
        current.swimPathFailed = false;
        current.swimRoutePending = true;
        current.phase = 2;
        current.phaseTick = current.ticks;
        baritone.getPathingBehavior().cancelEverything();
        baritone.getInputOverrideHandler().clearAllKeys();
        return true;
    }

    private boolean swimPathStartClear(Minecraft client) {
        BlockPos feet = baritone.getPlayerContext().playerFeet();
        return SurfaceSwimProgress.routeStartClear(swimSpace(client),
            new SurfaceSwimProgress.Cell(feet.getX(), feet.getY(), feet.getZ()));
    }

    private static void restoreSwimSettings(Run current) {
        if (current == null || current.swimPreviousAllowBreak == null) return;
        BaritoneAPI.getSettings().allowBreak.value = current.swimPreviousAllowBreak;
        BaritoneAPI.getSettings().allowPlace.value = current.swimPreviousAllowPlace;
        BaritoneAPI.getSettings().allowSubmergedNavigation.value = current.swimPreviousSubmergedNavigation;
        current.swimPreviousAllowBreak = null;
        current.swimPreviousAllowPlace = null;
        current.swimPreviousSubmergedNavigation = null;
    }

    private int findSurfaceY(Minecraft client, Run current) {
        int y = client.player.blockPosition().getY();
        if (client.level.canSeeSky(client.player.blockPosition()) && client.player.onGround()) return y;
        int limit = current.verticalDistance;
        for (int offset = 1; offset <= limit; offset++) {
            BlockPos feet = new BlockPos(client.player.blockPosition().getX(), y + offset, client.player.blockPosition().getZ());
            if (client.level.hasChunkAt(feet) && client.level.canSeeSky(feet)
                && passableForStanding(client, feet) && passableForStanding(client, feet.above())
                && !client.level.getBlockState(feet.below()).getCollisionShape(client.level, feet.below()).isEmpty()) return y + offset;
        }
        return Integer.MIN_VALUE;
    }

    /**
     * A player stands in grass, leaf litter or flowers without breaking them.
     * Requiring air made every surface under such a cover "not found"
     * (life-stage-20261015 b2: 871 failed ascends below a grassy camp).
     */
    private static boolean passableForStanding(Minecraft client, BlockPos pos) {
        BlockState state = client.level.getBlockState(pos);
        return state.isAir() || (state.canBeReplaced() && state.getFluidState().isEmpty()
            && state.getCollisionShape(client.level, pos).isEmpty());
    }

    private static boolean isVertical(Run current) {
        return current.kind.equals("ascend") || current.kind.equals("digUp") || current.kind.equals("digDown");
    }

    private static final Set<String> NAVIGATION_KINDS = Set.of(
        "any", "away", "y", "block", "goto", "navigate", "xz", "near", "get", "explore", "follow", "pickup", "elytra");

    /** Pre-change state of every cell this client tries to break or place in during a run. */
    public static void touchWorld(BlockPos pos) {
        if (instance == null || instance.run == null) return;
        Minecraft client = Minecraft.getInstance();
        if (client.level == null) return;
        if (instance.run.worldTouched.size() >= 4096) {
            // Cells past the cap get no receipt; say so instead of looking complete.
            if (!instance.run.worldTouched.containsKey(pos)) instance.run.worldTouchedTruncated = true;
            return;
        }
        instance.run.worldTouched.putIfAbsent(pos.immutable(), client.level.getBlockState(pos));
    }

    /**
     * The second cell a door or bed placed at {@code placed} takes. The game
     * fills it by itself, so without this the receipt held only the clicked
     * half (house run 2026-10-18: a door's upper half never reached the ledger).
     */
    public static void touchPlacementCompanion(net.minecraft.client.player.LocalPlayer player,
                                               net.minecraft.world.InteractionHand hand, BlockPos placed) {
        var item = player.getItemInHand(hand).getItem();
        if (item instanceof net.minecraft.world.item.DoubleHighBlockItem) {
            touchWorld(placed.above());
        } else if (item instanceof net.minecraft.world.item.BedItem) {
            touchWorld(placed.relative(player.getDirection()));
        }
    }

    /** Breaking one half of a door or bed removes the other half too. */
    public static void touchBreakCompanion(BlockPos pos) {
        Minecraft client = Minecraft.getInstance();
        if (client.level == null) return;
        BlockState state = client.level.getBlockState(pos);
        if (state.hasProperty(BlockStateProperties.DOUBLE_BLOCK_HALF)) {
            touchWorld(state.getValue(BlockStateProperties.DOUBLE_BLOCK_HALF) == DoubleBlockHalf.LOWER
                ? pos.above() : pos.below());
        } else if (state.hasProperty(BlockStateProperties.BED_PART)) {
            touchWorld(pos.relative(net.minecraft.world.level.block.BedBlock.getConnectedDirection(state)));
        }
    }

    /** A cell the same placement filled beside its target: the other half of a door or bed. */
    private static boolean companionOf(BlockPos pos, BlockPos anchor, BlockState after) {
        return (after.hasProperty(BlockStateProperties.DOUBLE_BLOCK_HALF) || after.hasProperty(BlockStateProperties.BED_PART))
            && pos.distManhattan(anchor) == 1;
    }

    private static JsonObject worldEdit(Run current, BlockPos pos, BlockState before, BlockState after) {
        JsonObject edit = new JsonObject();
        edit.addProperty("index", current.worldEdits.size());
        JsonArray at = new JsonArray();
        at.add(pos.getX());
        at.add(pos.getY());
        at.add(pos.getZ());
        edit.add("pos", at);
        edit.addProperty("before", BuiltInRegistries.BLOCK.getKey(before.getBlock()).getPath());
        edit.addProperty("after", BuiltInRegistries.BLOCK.getKey(after.getBlock()).getPath());
        edit.addProperty("op", after.canBeReplaced() && !before.canBeReplaced() ? "break"
            : before.canBeReplaced() && !after.canBeReplaced() ? "place" : "change");
        edit.addProperty("action_kind", current.kind);
        Boolean target = editTarget(current, pos, before, after);
        if (target == null) edit.add("target", com.google.gson.JsonNull.INSTANCE);
        else edit.addProperty("target", target);
        edit.addProperty("tick", current.ticks);
        return edit;
    }

    /** Whether a confirmed edit lies inside the action's declared target; null when the action declares none. */
    private static Boolean editTarget(Run current, BlockPos pos, BlockState before, BlockState after) {
        return switch (current.kind) {
            case "build" -> inBox(pos, current.anchor, current.anchor.offset(current.sx - 1, current.sy - 1, current.sz - 1));
            case "clear" -> inBox(pos, current.anchor, current.end);
            case "mine" -> {
                String block = BuiltInRegistries.BLOCK.getKey(before.getBlock()).getPath();
                boolean matched = false;
                for (String target : current.targets) matched |= target.replace("minecraft:", "").equals(block);
                yield matched;
            }
            case "collectBlocks" -> current.collectionTargets.contains(pos);
            case "shelterInPlace" -> current.shelterPlan.contains(pos) || pos.equals(current.shelterTarget);
            case "placeBlock" -> pos.equals(current.anchor) || companionOf(pos, current.anchor, after);
            default -> NAVIGATION_KINDS.contains(current.kind) ? Boolean.FALSE : null;
        };
    }

    /** Touched cells the client shows differently from the last state the server confirmed. */
    private static int unconfirmedEdits(Minecraft client, Run current) {
        if (client.level == null) return 0;
        int count = 0;
        for (var entry : current.worldTouched.entrySet()) {
            BlockPos pos = entry.getKey();
            if (client.level.isLoaded(pos) && !client.level.getBlockState(pos).equals(entry.getValue())) count++;
        }
        return count;
    }

    private static boolean inBox(BlockPos pos, BlockPos a, BlockPos b) {
        return pos.getX() >= Math.min(a.getX(), b.getX()) && pos.getX() <= Math.max(a.getX(), b.getX())
            && pos.getY() >= Math.min(a.getY(), b.getY()) && pos.getY() <= Math.max(a.getY(), b.getY())
            && pos.getZ() >= Math.min(a.getZ(), b.getZ()) && pos.getZ() <= Math.max(a.getZ(), b.getZ());
    }

    public static void observeBlockUpdate(BlockPos pos, BlockState state) {
        if (instance == null || instance.run == null) return;
        Run current = instance.run;
        BlockState last = current.worldTouched.get(pos);
        if (last != null && !last.equals(state)) {
            current.worldTouched.put(pos.immutable(), state);
            current.worldEdits.add(worldEdit(current, pos, last, state));
        }
        if (current.kind.equals("placeBlock") && current.phase == 3 && pos.equals(current.anchor)) {
            current.placementConfirmed = BuiltInRegistries.BLOCK.getKey(state.getBlock()).getPath().equals(current.item);
        }
        if (current.kind.equals("shelterInPlace") && pos.equals(current.shelterTarget)) {
            Minecraft client = Minecraft.getInstance();
            if (state.isAir()) {
                current.shelterPortOpened = true;
                instance.shelterOwned.remove(shelterKey(client, pos));
            } else if (BuiltInRegistries.BLOCK.getKey(state.getBlock()).getPath().equals(current.item)
                && state.isCollisionShapeFullBlock(client.level, pos)) {
                current.placementConfirmed = true;
                instance.shelterOwned.put(shelterKey(client, pos), current.item);
            }
        }
        if (!isVertical(current)) return;
        BlockState before = current.verticalTouched.get(pos);
        if (before != null && !before.equals(state) && current.verticalConfirmed.add(pos.immutable())) current.mutations++;
    }

    private static boolean hasVerticalLiquidHazard(Minecraft client, BlockPos pos) {
        if (!client.level.getBlockState(pos).getFluidState().isEmpty()) return true;
        for (Direction direction : Direction.values()) {
            if (!client.level.getBlockState(pos.relative(direction)).getFluidState().isEmpty()) return true;
        }
        return false;
    }

    /** Limits only the vertical transaction; shared-world mutation stays denied. */
    public static boolean allowVerticalMutation(BlockPos pos) {
        if (instance == null || instance.run == null || !isVertical(instance.run)) return true;
        Run current = instance.run;
        Minecraft client = Minecraft.getInstance();
        if (current.stopStatus != null) return false;
        String denial = null;
        if (!Boolean.getBoolean("minebot.client.disposable_world")) denial = "vertical_mutation_not_authorized";
        else if (current.anchor == null || Math.abs(pos.getX() - current.anchor.getX()) > current.verticalDistance
            || Math.abs(pos.getZ() - current.anchor.getZ()) > current.verticalDistance
            || Math.abs(pos.getY() - current.anchor.getY()) > current.verticalDistance + 2) denial = "vertical_target_out_of_bounds";
        else if (!client.level.hasChunkAt(pos)) denial = "vertical_chunk_unloaded";
        else {
            if (current.sealHit != null && pos.equals(current.sealQueue.isEmpty() ? null : current.sealQueue.getFirst())) {
                // The Body's own block going into the water it is sealing.
            } else if (current.swimAbandoned) {
                // Making a way, a player digs on through trickling water, blocks a
                // water source beside the next cell first, and never digs into lava.
                boolean lava = false;
                List<BlockPos> sources = new ArrayList<>();
                for (BlockPos cell : List.of(pos, pos.north(), pos.south(), pos.east(), pos.west(), pos.above(), pos.below())) {
                    var fluid = client.level.getFluidState(cell);
                    if (fluid.is(FluidTags.LAVA)) lava = true;
                    else if (fluid.is(FluidTags.WATER) && fluid.isSource()) sources.add(cell);
                }
                if (lava) denial = "vertical_liquid_hazard";
                else if (!sources.isEmpty() && current.makeWaySteps < 16
                    && instance.queueWaterSeal(client, current, List.of(pos))) {
                    current.makeWaySteps++;
                    return false;
                }
            } else if (hasVerticalLiquidHazard(client, pos)) denial = "vertical_liquid_hazard";
            if (!current.verticalTouched.containsKey(pos) && current.verticalTouched.size()
                >= Math.max(0, integer(current.params, "max_mutations", 64))) denial = "vertical_mutation_budget_exceeded";
        }
        if (denial != null) {
            instance.requestStop("failed", denial);
            return false;
        }
        current.verticalTouched.putIfAbsent(pos.immutable(), client.level.getBlockState(pos));
        return true;
    }

    private static int menuSlotForContainer(AbstractContainerMenu menu, int containerSlot) {
        int index = 0;
        for (int slot = 0; slot < menu.slots.size(); slot++) {
            if (!(menu.slots.get(slot).container instanceof net.minecraft.world.entity.player.Inventory)) {
                if (index++ == containerSlot) return slot;
            }
        }
        return -1;
    }

    private static int menuSlotForPlayer(AbstractContainerMenu menu, int inventorySlot) {
        for (int slot = 0; slot < menu.slots.size(); slot++) {
            Slot candidate = menu.slots.get(slot);
            if (candidate.container instanceof net.minecraft.world.entity.player.Inventory
                && candidate.getContainerSlot() == inventorySlot) return slot;
        }
        return -1;
    }

    private static void click(AbstractContainerMenu menu, int slot, int button, ContainerInput input, Minecraft client) {
        client.gameMode.handleContainerInput(menu.containerId, slot, button, input, client.player);
    }

    private static BlockPos positionParam(JsonObject params, String... names) {
        for (String name : names) {
            if (!params.has(name) || !params.get(name).isJsonArray()) continue;
            JsonArray raw = params.getAsJsonArray(name);
            if (raw.size() == 3) return new BlockPos(raw.get(0).getAsInt(), raw.get(1).getAsInt(), raw.get(2).getAsInt());
        }
        return null;
    }

    private static double number(JsonObject object, String key, double fallback) {
        return object.has(key) && object.get(key).isJsonPrimitive() ? object.get(key).getAsDouble() : fallback;
    }

    private static int integer(JsonObject object, String key, int fallback) {
        return object.has(key) && object.get(key).isJsonPrimitive() ? object.get(key).getAsInt() : fallback;
    }

    private static boolean booleanValue(JsonObject object, String key, boolean fallback) {
        return object.has(key) && object.get(key).isJsonPrimitive() ? object.get(key).getAsBoolean() : fallback;
    }

    private static double clamp(double value, double min, double max) {
        return Math.max(min, Math.min(max, value));
    }


    private void terminal(Minecraft client, String status, String reason) {
        Run current = run;
        if (current.previousFollowRadius != null) {
            BaritoneAPI.getSettings().followRadius.value = current.previousFollowRadius;
        }
        restoreAttention(current);
        restoreBuilderInventory(current);
        restoreBreakFromAbove(current);
        if (current.worldEditsSent < current.worldEdits.size()) emit(current, liteProgress(client, current));
        JsonObject response = state(client);
        if (current.kind.equals("shelterInPlace")) {
            for (var entry : current.shelterFacts.entrySet()) response.add(entry.getKey(), entry.getValue());
            if (string(current.params, "action", "seal").equals("look_out") && current.shelterTarget != null) {
                boolean closed = shelterSolid(client, current.shelterTarget)
                    && BuiltInRegistries.BLOCK.getKey(client.level.getBlockState(current.shelterTarget).getBlock()).getPath().equals(current.item);
                response.addProperty("port_closed", closed);
                if (status.equals("completed") && !booleanValue(current.params, "keep_open", false) && !closed) {
                    status = "failed";
                    reason = "shelter_unsealable";
                    response.addProperty("message", "The head-height port is open.");
                }
            }
            if (current.anchor != null) {
                JsonArray open = shelterOpenCells(client, current);
                response.add("open_cells", open);
                if (status.equals("completed") && string(current.params, "action", "seal").equals("seal") && !open.isEmpty()) {
                    status = "failed";
                    reason = "shelter_unsealable";
                    response.addProperty("message", "The shelter has open cells.");
                }
                if (status.equals("completed") && !client.player.blockPosition().equals(current.anchor)) {
                    status = "failed";
                    reason = "displaced";
                    response.addProperty("message", "The Body left the shelter center cell.");
                }
            }
        }
        response.addProperty("flight_observed", current.seenFlying);
        if (current.torchesPlaced > 0) {
            response.addProperty("torches_placed", current.torchesPlaced);
            response.add("torch_cells", current.torchCells);
        }
        response.addProperty("type", "terminal");
        response.addProperty("action_id", current.id);
        response.addProperty("status", status);
        response.addProperty("native_stopped", !processActive() && !baritone.getPathingBehavior().isPathing());
        response.addProperty("reason", reason);
        if (status.equals("failed") && current.goal != null && !current.structured) {
            response.add("route", routeFacts(client, current));
        }
        if (current.sameAgainHere) response.addProperty("retryable", false);
        response.addProperty("objective", current.objective);
        response.addProperty("world_edit_count", current.worldEdits.size());
        response.addProperty("world_edits_truncated", current.worldTouchedTruncated);
        response.addProperty("world_edits_unconfirmed", unconfirmedEdits(client, current));
        response.addProperty("elapsed_ms", current.elapsed());
        if (current.kind.equals("raiseShield")) {
            response.addProperty("hits_blocked", current.shieldHitsBlocked);
            response.addProperty("hits_taken", current.shieldHitsTaken);
            response.addProperty("blocking_ticks", current.shieldBlockingTicks);
            response.addProperty("health_before", current.initialHealth);
            response.addProperty("health_after", client.player.getHealth());
        }
        if (current.kind.equals("placeBedAndSleep")) {
            response.addProperty("bed_placed", current.bedPlaced);
            response.addProperty("bed_recovered", current.bedRecovered);
            response.addProperty("bed_inventory_before", current.bedInventoryBefore);
            response.addProperty("bed_inventory_after", count(client, current.item == null ? "" : current.item));
            response.addProperty("bed_game_message", current.bedGameMessage);
            if (current.anchor != null) response.add("bed_position", xyz(current.anchor));
            if (current.bedBlockedCells != null && !current.bedBlockedCells.isEmpty()) {
                response.add("bed_blocked_cells", current.bedBlockedCells.deepCopy());
            }
            if (status.equals("completed") && !current.bedRecovered) {
                status = "failed";
                reason = "bed_not_recovered";
            }
        }
        response.addProperty("stop_latency_ms", current.stopAt == 0 ? 0 : (System.nanoTime() - current.stopAt) / 1_000_000);
        response.addProperty("baritone_stop_latency_ms", current.stopAt == 0 || current.baritoneStoppedAt == 0
            ? 0 : (current.baritoneStoppedAt - current.stopAt) / 1_000_000);
        response.addProperty("inventory_before", current.before);
        response.addProperty("inventory_after", count(client, current.item));
        response.addProperty("inventory_delta", count(client, current.item) - current.before);
        if (current.lastResourceTarget != null) {
            JsonArray target = new JsonArray();
            target.add(current.lastResourceTarget.getX());
            target.add(current.lastResourceTarget.getY());
            target.add(current.lastResourceTarget.getZ());
            response.add("last_resource_target", target);
        }
        if (current.kind.equals("collectBlocks")) {
            response.addProperty("inventory_after", collectionCount(client, current));
            response.addProperty("inventory_delta", collectionCount(client, current) - current.before);
            response.addProperty("target_count", current.collectionTargets.size());
        }
        if (current.brokenItem != null) response.addProperty("broken_item", current.brokenItem);
        response.addProperty("process_kind", current.kind);
        response.addProperty("seen_active", current.seenActive);
        response.addProperty("last_path_event", current.lastPathEvent);
        JsonObject events = new JsonObject();
        current.pathEvents.forEach(events::addProperty);
        response.add("path_events", events);
        if (current.structured) {
            response.addProperty("action_kind", current.kind);
            response.addProperty("attacks", current.attacks);
            response.addProperty("shots", current.shots);
            if (current.kind.equals("engageEntity")) {
                response.addProperty("kite_ticks", current.kiteTicks);
                response.addProperty("kills", current.kills);
                if (current.bodyHealthStart >= 0 && client.player != null) {
                    response.addProperty("body_health_lost", round2(Math.max(0.0, current.bodyHealthStart - client.player.getHealth())));
                }
            }
            if (current.predictedTargetPos != null) {
                response.addProperty("lead_blocks", current.leadBlocks);
                response.add("predicted_target_pos", xyz(current.predictedTargetPos));
            }
            if (current.cushionAt != null) {
                response.add("cushion_at", xyz(current.cushionAt));
                response.addProperty("water_taken_back", current.cushionScooped);
            }
            // A ranged action ends when its arrow is loosed: whether it hit is
            // not known yet and is said afterwards as last_shot, not as "no damage".
            if (!current.kind.equals("rangedAttack")) response.addProperty("damage_observed", current.damageObserved);
            if (!Double.isNaN(current.rangeDistance)) {
                response.addProperty("target_distance", round2(current.rangeDistance));
                response.addProperty("max_range", round2(current.rangeMax));
            }
            if (current.sightBlockedPos != null) {
                JsonObject blocked = new JsonObject();
                blocked.addProperty("block", current.sightBlockedBlock);
                blocked.add("pos", xyz(current.sightBlockedPos));
                response.add("sight_blocked_by", blocked);
            }
            if (current.kind.equals("pillarUp") && current.item != null && !current.item.isEmpty()) {
                JsonObject column = new JsonObject();
                column.addProperty("x", current.pillarX);
                column.addProperty("z", current.pillarZ);
                column.addProperty("base_y", current.pillarStartY);
                column.addProperty("raised", current.pillarRaised);
                column.addProperty("top_y", current.pillarStartY + current.pillarRaised);
                column.addProperty("recentred", current.pillarRecentred);
                response.add("pillar", column);
            }
            if (current.kind.equals("keepMoving")) {
                response.add("keep_moving", keepMovingFacts(current));
                if (reason.equals("no_safe_point")) response.addProperty("message",
                    "No point in the area has a floor away from drops, clouds and places that just hurt the Body.");
            }
            response.addProperty("initial_health", current.initialHealth);
            response.addProperty("shot_health_baseline", current.shotHealthBaseline);
            if (current.targetEntity != null) {
                response.addProperty("target_id", current.targetEntity.getUUID().toString());
                if (current.targetEntity instanceof LivingEntity target) response.addProperty("target_health", target.getHealth());
            }
            response.addProperty("mutations", current.mutations);
            if (!current.handoffReceiverId.isBlank()) {
                response.addProperty("receiver_id", current.handoffReceiverId);
                response.addProperty("handoff_expected", current.handoffExpected);
                response.addProperty("handoff_picked_up", current.handoffPickedUp);
                response.addProperty("handoff_receipt", current.handoffReceipt);
                response.addProperty("spawned_count", current.phase >= 2 ? current.handoffExpected : 0);
                if (current.handoffReceipt) {
                    JsonObject receipt = new JsonObject();
                    receipt.addProperty("player", current.handoffReceiverName);
                    receipt.addProperty("item", current.item);
                    receipt.addProperty("count", current.handoffPickedUp);
                    receipt.addProperty("seq", 0);
                    receipt.addProperty("tick", client.level == null ? 0 : client.level.getGameTime());
                    response.add("pickup_receipt", receipt);
                }
            }
            if (current.kind.equals("follow") && current.targetEntity != null) {
                response.addProperty("target_uuid", current.targetEntity.getUUID().toString());
                response.addProperty("target_name", current.targetEntity.getName().getString());
                response.addProperty("keep_radius", current.followKeepDistance);
                response.addProperty("final_distance", client.player.distanceTo(current.targetEntity));
            }
            if (!current.transferItem.isBlank()) {
                response.addProperty("transfer_item", current.transferItem);
                response.addProperty("transfer_count", current.transferRequested);
                response.addProperty("count", status.equals("completed") ? current.transferRequested : 0);
            }
        }
        if (current.previousThrowawayItems != null) {
            BaritoneAPI.getSettings().acceptableThrowawayItems.value = current.previousThrowawayItems;
        }
        run = null;
        emit(current, response);
        LOG.info("MINEBOT_PHASE_A terminal {}", response);
    }

    /**
     * The world went away under a run. Its terminal is normally sent from
     * tick(), which stops running without a player or a level, so the run
     * never ended: Python waited for its IPC deadline (three minutes in H5,
     * 2026-10-29) and the client logged a cancelled path every tick until it
     * was closed. Nothing of the world can be read now; the edits it made are
     * reported unconfirmed.
     */
    private void endRunOnDisconnect(Run current) {
        restoreWaySettings(current);
        if (current.previousFollowRadius != null) {
            BaritoneAPI.getSettings().followRadius.value = current.previousFollowRadius;
        }
        if (current.previousThrowawayItems != null) {
            BaritoneAPI.getSettings().acceptableThrowawayItems.value = current.previousThrowawayItems;
        }
        JsonObject response = new JsonObject();
        response.addProperty("type", "terminal");
        response.addProperty("action_id", current.id);
        response.addProperty("status", "failed");
        response.addProperty("reason", "server_disconnected");
        response.addProperty("native_stopped", true);
        response.addProperty("objective", current.objective);
        response.addProperty("process_kind", current.kind);
        response.addProperty("world_edit_count", current.worldEdits.size());
        response.addProperty("world_edits_truncated", current.worldTouchedTruncated);
        response.addProperty("world_edits_unconfirmed", current.worldEdits.size());
        response.addProperty("elapsed_ms", current.elapsed());
        response.addProperty("last_path_event", current.lastPathEvent);
        JsonObject events = new JsonObject();
        current.pathEvents.forEach(events::addProperty);
        response.add("path_events", events);
        run = null;
        emit(current, response);
        LOG.info("MINEBOT_PHASE_A terminal {}", response);
    }

    /**
     * A player with no route makes one: water flowing round the Body is blocked
     * off, and a goal above or below is reached by a straight column (Baritone
     * digs or pillars it). Only when the caller let this walk break and place;
     * Baritone keeps away from the protected cells for every block it breaks.
     */
    private boolean makeWayStep(Minecraft client, Run current) {
        if (!(current.kind.equals("goto") || current.kind.equals("navigate")) || current.routeFound
            || current.makeWaySteps >= 4 || !BaritoneAPI.getSettings().allowBreak.value
            || !BaritoneAPI.getSettings().allowPlace.value) return false;
        if (current.wayGoal == null) current.wayGoal = current.goal;
        BlockPos feet = client.player.blockPosition();
        if (!(current.wayGoal instanceof baritone.api.utils.interfaces.IGoalRenderPos target)) return false;
        BlockPos goal = target.getGoalPos();
        List<BlockPos> way = new ArrayList<>(List.of(feet, feet.above()));
        int dx = goal.getX() - feet.getX();
        int dz = goal.getZ() - feet.getZ();
        if (dx != 0 || dz != 0) {
            // Baritone will not dig a block that has water beside it; a player blocks the water first.
            Direction toward = Math.abs(dx) >= Math.abs(dz) ? (dx > 0 ? Direction.EAST : Direction.WEST)
                : (dz > 0 ? Direction.SOUTH : Direction.NORTH);
            way.add(feet.relative(toward));
            way.add(feet.above().relative(toward));
        }
        if (queueWaterSeal(client, current, way)) {
            current.makeWaySteps++;
            return true;
        }
        int dy = Math.max(-16, Math.min(16, goal.getY() - feet.getY()));
        if (Math.abs(dy) < 2) return false;
        current.makeWaySteps++;
        current.goal = new GoalBlock(feet.getX(), feet.getY() + dy, feet.getZ());
        current.routeFound = true;
        current.wayHoldUntil = current.ticks + 20;
        baritone.getCustomGoalProcess().setGoalAndPath(current.goal);
        return true;
    }

    /** Queue every water cell beside the given cells (the Body's own two cells are left). */
    private boolean queueWaterSeal(Minecraft client, Run current, List<BlockPos> around) {
        if (throwawayItem(client) == null) return false;
        BlockPos feet = client.player.blockPosition();
        BlockPos head = feet.above();
        java.util.LinkedHashSet<BlockPos> cells = new java.util.LinkedHashSet<>();
        for (BlockPos body : around) {
            for (Direction side : Direction.values()) {
                BlockPos cell = body.relative(side).immutable();
                if (cell.equals(feet) || cell.equals(head) || current.sealQueue.contains(cell)) continue;
                if (client.level.getFluidState(cell).is(FluidTags.WATER)
                    && client.level.getBlockState(cell).canBeReplaced()) cells.add(cell);
            }
        }
        if (cells.isEmpty()) return false;
        current.sealQueue.addAll(cells);
        current.sealNextTick = current.ticks;
        baritone.getPathingBehavior().cancelEverything();
        baritone.getInputOverrideHandler().clearAllKeys();
        return true;
    }

    /**
     * Turn toward the face, then place three ticks later as place_block does;
     * when the queue is empty the walk or ascent resumes.
     */
    private boolean tickSealWater(Minecraft client, Run current) {
        if (current.sealQueue.isEmpty()) return false;
        if (current.ticks < current.sealNextTick) return true;
        BlockPos cell = current.sealQueue.getFirst();
        String item = throwawayItem(client);
        boolean open = client.level.getFluidState(cell).is(FluidTags.WATER) && client.level.getBlockState(cell).canBeReplaced();
        if (current.sealHit == null && item != null && open) {
            current.sealHit = wayPlacement(client, cell, current.sealTries++);
            if (current.sealHit != null) {
                shelterSelectItem(client, item);
                lookAt(client, lookParams(current.sealHit.getLocation()));
                current.sealNextTick = current.ticks + 3;
                return true;
            }
        }
        if (current.sealHit != null && item != null && open) {
            shelterSelectItem(client, item);
            touchWorld(cell);
            var used = client.gameMode.useItemOn(client.player, InteractionHand.MAIN_HAND, current.sealHit);
            if (used.consumesAction()) current.waterSealed++;
            LOG.info("MINEBOT_BODY make_way_seal id={} cell={} against={} result={} on_ground={}",
                current.id, cell, current.sealHit.getBlockPos(), used.getClass().getSimpleName(), client.player.onGround());
        }
        current.sealHit = null;
        current.sealQueue.removeFirst();
        current.sealNextTick = current.ticks + 2;
        if (current.sealQueue.isEmpty()) {
            current.wayHoldUntil = current.ticks + 20;
            if (current.goal != null) baritone.getCustomGoalProcess().setGoalAndPath(current.goal);
        }
        return true;
    }

    /**
     * A face to place against: the block below first, then the sides, the one
     * above last; each retry of the same cell takes the next face that is solid.
     */
    private static BlockHitResult wayPlacement(Minecraft client, BlockPos target, int attempt) {
        List<BlockHitResult> faces = new ArrayList<>();
        for (Direction face : List.of(Direction.UP, Direction.NORTH, Direction.SOUTH, Direction.EAST, Direction.WEST, Direction.DOWN)) {
            BlockPos support = target.relative(face.getOpposite());
            if (!shelterSolid(client, support)) continue;
            Vec3 hit = Vec3.atCenterOf(support).add(Vec3.atLowerCornerOf(face.getUnitVec3i()).scale(0.5));
            faces.add(new BlockHitResult(hit, face, support, false));
        }
        return faces.isEmpty() ? null : faces.get(attempt % faces.size());
    }

    private static String throwawayItem(Minecraft client) {
        var throwaway = BaritoneAPI.getSettings().acceptableThrowawayItems.value;
        for (int slot = 0; slot < 36; slot++) {
            ItemStack stack = client.player.getInventory().getItem(slot);
            if (!stack.isEmpty() && throwaway.contains(stack.getItem())) return itemPath(stack);
        }
        return null;
    }

    /** Swimming found no way up: with the caller's leave, seal the water and dig or pillar up. */
    private boolean swimMakeWay(Minecraft client, Run current) {
        if (current.swimAbandoned || !current.params.has("make_way")
            || !current.params.get("make_way").getAsBoolean() || throwawayItem(client) == null) return false;
        restoreSwimSettings(current);
        var settings = BaritoneAPI.getSettings();
        current.wayPreviousAllowBreak = settings.allowBreak.value;
        current.wayPreviousAllowPlace = settings.allowPlace.value;
        settings.allowBreak.value = true;
        settings.allowPlace.value = true;
        client.options.keyJump.setDown(false);
        current.swimAbandoned = true;
        current.swimmingUp = false;
        current.swimRouteGoal = null;
        current.makeWaySteps++;
        queueWaterSeal(client, current, List.of(client.player.blockPosition(), client.player.blockPosition().above()));
        current.goal = null;
        current.phase = 0;
        current.phaseTick = current.ticks;
        return true;
    }

    /**
     * Why a walk ended short of its goal, in things the caller can act on: whether
     * a route was ever found, what the Body stands in, and what it may change.
     */
    private JsonObject routeFacts(Minecraft client, Run current) {
        JsonObject route = new JsonObject();
        int failed = current.pathEvents.getOrDefault("CALC_FAILED", 0);
        route.addProperty("route_found", current.routeFound);
        route.addProperty("route_calculations_failed", failed);
        BlockPos feet = client.player.blockPosition();
        route.addProperty("feet_in_water", client.level.getFluidState(feet).is(FluidTags.WATER));
        route.addProperty("head_in_water", client.player.isEyeInFluid(FluidTags.WATER));
        boolean flowing = false;
        boolean lava = false;
        for (BlockPos near : BlockPos.betweenClosed(feet.offset(-2, -1, -2), feet.offset(2, 3, 2))) {
            var fluid = client.level.getFluidState(near);
            if (fluid.is(FluidTags.WATER) && !fluid.isSource()) flowing = true;
            if (fluid.is(FluidTags.LAVA)) lava = true;
        }
        route.addProperty("flowing_water_near", flowing);
        route.addProperty("lava_near", lava);
        route.addProperty("may_break", BaritoneAPI.getSettings().allowBreak.value);
        route.addProperty("may_place", BaritoneAPI.getSettings().allowPlace.value);
        int placeable = 0;
        var throwaway = BaritoneAPI.getSettings().acceptableThrowawayItems.value;
        for (int slot = 0; slot < client.player.getInventory().getContainerSize(); slot++) {
            ItemStack stack = client.player.getInventory().getItem(slot);
            if (!stack.isEmpty() && throwaway.contains(stack.getItem())) placeable += stack.getCount();
        }
        route.addProperty("placeable_blocks", placeable);
        route.addProperty("make_way_steps", current.makeWaySteps);
        route.addProperty("water_cells_sealed", current.waterSealed);
        return route;
    }

    private JsonObject navigationFacts(Run current) {
        JsonObject navigation = current.navigation.deepCopy();
        navigation.addProperty("repeated_failures", current.navigationProgress.repeatedFailures());
        navigation.addProperty("idle_ms", current.navigationProgress.idleMs(current.elapsed()));
        navigation.addProperty("path_calculations", current.pathEvents.getOrDefault("CALC_STARTED", 0));
        navigation.addProperty("calculating", baritone.getPathingBehavior().getInProgress().isPresent());
        navigation.addProperty("pathing", baritone.getPathingBehavior().isPathing());
        return navigation;
    }

    /** Observer telemetry between full samples: pose, vitals and navigation only. */
    private JsonObject liteProgress(Minecraft client, Run current) {
        JsonObject frame = new JsonObject();
        frame.addProperty("type", "progress");
        frame.addProperty("lite", true);
        frame.addProperty("action_id", current.id);
        frame.addProperty("sampled_at_ms", System.currentTimeMillis());
        if (client.player != null) {
            JsonArray pos = new JsonArray();
            pos.add(client.player.getX());
            pos.add(client.player.getY());
            pos.add(client.player.getZ());
            frame.add("position", pos);
            frame.addProperty("yaw", client.player.getYRot());
            frame.addProperty("pitch", client.player.getXRot());
            frame.addProperty("on_ground", client.player.onGround());
            frame.addProperty("head_in_liquid", client.player.isEyeInFluid(FluidTags.WATER) || client.player.isEyeInFluid(FluidTags.LAVA));
            hazardFacts(client, frame);
            frame.addProperty("health", client.player.getHealth());
            frame.addProperty("food", client.player.getFoodData().getFoodLevel());
            frame.addProperty("oxygen", client.player.getAirSupply());
            frame.addProperty("fall_flying", client.player.isFallFlying());
            frame.addProperty("selected_item", itemPath(client.player.getMainHandItem()));
        }
        frame.add("navigation", navigationFacts(current));
        frame.addProperty("pathing", baritone.getPathingBehavior().isPathing());
        frame.addProperty("mine_active", baritone.getMineProcess().isActive());
        frame.addProperty("goal_active", baritone.getCustomGoalProcess().isActive());
        frame.addProperty("get_active", baritone.getGetToBlockProcess().isActive());
        frame.addProperty("explore_active", baritone.getExploreProcess().isActive());
        frame.addProperty("follow_active", baritone.getFollowProcess().isActive());
        frame.addProperty("farm_active", baritone.getFarmProcess().isActive());
        frame.addProperty("builder_active", baritone.getBuilderProcess().isActive());
        frame.addProperty("builder_paused", baritone.getBuilderProcess().isPaused());
        frame.addProperty("elytra_active", baritone.getElytraProcess().isActive());
        frame.addProperty("any_process_active", processActive());
        var goal = baritone.getPathingBehavior().getGoal();
        frame.addProperty("baritone_goal", goal == null ? "" : goal.toString());
        return withTelemetry(current, frame);
    }

    /** Path events and the world edits not yet sent; observers only, never action settlement. */
    private static JsonObject withTelemetry(Run current, JsonObject frame) {
        JsonObject events = new JsonObject();
        current.pathEvents.forEach(events::addProperty);
        frame.add("path_events", events);
        frame.addProperty("last_path_event", current.lastPathEvent);
        if (current.worldEditsSent < current.worldEdits.size()) {
            JsonArray edits = new JsonArray();
            for (int index = current.worldEditsSent; index < current.worldEdits.size(); index++) {
                edits.add(current.worldEdits.get(index));
            }
            current.worldEditsSent = current.worldEdits.size();
            frame.add("world_edits", edits);
        }
        return frame;
    }

    private JsonObject state(Minecraft client) {
        JsonObject state = new JsonObject();
        state.addProperty("disposable_world", Boolean.getBoolean("minebot.client.disposable_world"));
        state.addProperty("protocol", "minebot-client-spike/1");
        state.addProperty("ready", ready);
        state.addProperty("active_action_known", true);
        state.addProperty("active_action_id", run == null ? null : run.id);
        state.addProperty("sampled_at_ms", System.currentTimeMillis());
        state.addProperty("disposable_world", Boolean.getBoolean("minebot.client.disposable_world"));
        state.addProperty("pid", ProcessHandle.current().pid());
        state.addProperty("heap_used_bytes", Runtime.getRuntime().totalMemory() - Runtime.getRuntime().freeMemory());
        state.addProperty("heap_committed_bytes", Runtime.getRuntime().totalMemory());
        if (client.player != null && client.level != null) {
            state.addProperty("player", client.player.getName().getString());
            JsonArray pos = new JsonArray();
            pos.add(client.player.getX());
            pos.add(client.player.getY());
            pos.add(client.player.getZ());
            state.add("position", pos);
            state.addProperty("on_ground", client.player.onGround());
            BlockPos source = waterSourceInReach(client);
            if (source != null) state.add("water_source_in_reach", xyz(source));
            state.addProperty("head_in_liquid", client.player.isEyeInFluid(FluidTags.WATER) || client.player.isEyeInFluid(FluidTags.LAVA));
            JsonObject fall = fallFact(client);
            if (fall != null) state.add("fall", fall);
            hazardFacts(client, state);
            ShotWatch shot = lastShot;
            if (shot != null) state.add("last_shot", shot.fact(client.level.getGameTime()));
            state.addProperty("creative", client.player.getAbilities().instabuild);
            state.addProperty("dimension", client.level.dimension().identifier().toString());
            state.addProperty("game_time", client.level.getGameTime());
            JsonObject spawnMessage = lastSpawnMessage;
            if (spawnMessage != null) state.add("spawn_message", spawnMessage.deepCopy());
            var respawn = client.level.getLevelData().getRespawnData();
            JsonObject worldSpawn = new JsonObject();
            worldSpawn.addProperty("dimension", respawn.dimension().identifier().toString());
            worldSpawn.add("pos", xyz(respawn.pos()));
            state.add("world_spawn", worldSpawn);
            state.addProperty("difficulty", client.level.getDifficulty().name().toLowerCase(java.util.Locale.ROOT));
            state.addProperty("health", client.player.getHealth());
            if (lastDamageSource != null && lastDamageTick >= 0) {
                JsonObject hurt = lastDamageSource.deepCopy();
                hurt.addProperty("seconds_since_hurt", Math.max(0.0,
                    (client.level.getGameTime() - lastDamageTick) / 20.0));
                state.add("hurt_source", hurt);
            }
            state.addProperty("food", client.player.getFoodData().getFoodLevel());
            state.addProperty("time", Math.floorMod(client.level.getOverworldClockTime(), 24000));
            state.addProperty("sleeping", client.player.isSleeping());
            state.addProperty("weather", client.level.isThundering() ? "thunder" : client.level.isRaining() ? "rain" : "clear");
            JsonArray effects = new JsonArray();
            for (var effect : client.player.getActiveEffects()) {
                JsonObject observed = new JsonObject();
                observed.addProperty("id", BuiltInRegistries.MOB_EFFECT.getKey(effect.getEffect().value()).toString());
                observed.addProperty("amplifier", effect.getAmplifier());
                observed.addProperty("duration", effect.getDuration());
                effects.add(observed);
            }
            state.add("effects", effects);
            state.addProperty("using_item", client.player.isUsingItem());
            state.addProperty("use_key_down", client.options.keyUse.isDown());
            state.addProperty("oxygen", client.player.getAirSupply());
            state.addProperty("yaw", client.player.getYRot());
            state.addProperty("pitch", client.player.getXRot());
            state.addProperty("selected_slot", client.player.getInventory().getSelectedSlot());
            state.addProperty("selected_item", itemPath(client.player.getMainHandItem()));
            state.addProperty("selected_item_known", true);
            state.addProperty("offhand_item", itemPath(client.player.getOffhandItem()));
            state.addProperty("fall_flying", client.player.isFallFlying());
            state.addProperty("menu", client.player.containerMenu.getClass().getSimpleName());
            state.addProperty("chest_item", BuiltInRegistries.ITEM.getKey(client.player.getItemBySlot(EquipmentSlot.CHEST).getItem()).getPath());
            // What the Body wears: the inventory counts worn armour with the
            // rest, so an iron chestplate in the bag read the same as one worn
            // (run-13: full iron armour sat unworn ~80 minutes).
            JsonObject armor = new JsonObject();
            armor.addProperty("head", itemPath(client.player.getItemBySlot(EquipmentSlot.HEAD)));
            armor.addProperty("chest", itemPath(client.player.getItemBySlot(EquipmentSlot.CHEST)));
            armor.addProperty("legs", itemPath(client.player.getItemBySlot(EquipmentSlot.LEGS)));
            armor.addProperty("feet", itemPath(client.player.getItemBySlot(EquipmentSlot.FEET)));
            state.add("armor", armor);
            // How many uses each tool, weapon and piece of armour has left, as the
            // durability bar shows a player: run-15 and run-18 each broke the only
            // iron pickaxe mid-dig without the Agent knowing it was worn
            // (body-tool-boundaries-20261110 §13).
            JsonArray wear = new JsonArray();
            for (int slot = 0; slot < client.player.getInventory().getContainerSize(); slot++) {
                ItemStack stack = client.player.getInventory().getItem(slot);
                if (stack.isEmpty() || !stack.isDamageableItem()) continue;
                JsonObject worn = new JsonObject();
                worn.addProperty("item", itemPath(stack));
                worn.addProperty("left", stack.getMaxDamage() - stack.getDamageValue());
                worn.addProperty("max", stack.getMaxDamage());
                wear.add(worn);
            }
            state.add("tool_wear", wear);
            // How bright it is where the Body stands, as a player sees it:
            // light from torches and other lit blocks, how much of the sky
            // reaches the spot, and whether open sky is overhead. The Agent had
            // no way to know a place was dark: four runs placed no torch while
            // a quarter to a third of their time was spent below y 60
            // (project-review-20261110 §9).
            BlockPos standsAt = client.player.blockPosition();
            JsonObject light = new JsonObject();
            light.addProperty("block", client.level.getBrightness(net.minecraft.world.level.LightLayer.BLOCK, standsAt));
            light.addProperty("sky", client.level.getBrightness(net.minecraft.world.level.LightLayer.SKY, standsAt));
            light.addProperty("open_sky", client.level.canSeeSky(standsAt));
            state.add("light", light);
            state.addProperty("feet_chunk_loaded", client.level.hasChunkAt(client.player.blockPosition()));
            JsonObject inventory = new JsonObject();
            // Blocks Baritone may place to climb or bridge: its own accepted list, as carried now.
            var scaffoldItems = BaritoneAPI.getSettings().acceptableThrowawayItems.value;
            int scaffoldBlocks = 0;
            for (int slot = 0; slot < client.player.getInventory().getContainerSize(); slot++) {
                ItemStack stack = client.player.getInventory().getItem(slot);
                if (!stack.isEmpty()) {
                    String item = BuiltInRegistries.ITEM.getKey(stack.getItem()).getPath();
                    inventory.addProperty(item, (inventory.has(item) ? inventory.get(item).getAsInt() : 0) + stack.getCount());
                    if (scaffoldItems.contains(stack.getItem())) scaffoldBlocks += stack.getCount();
                }
            }
            // Items in an open crafting grid or on the cursor are still the
            // player's (closing the menu gives them back). Counting only the 36
            // slots, a craft in progress read as missing its inputs: 4 oak_log
            // showed as 1 while two sat in the grid (step5-threat-claude8).
            List<ItemStack> held = new ArrayList<>();
            held.add(client.player.containerMenu.getCarried());
            if (client.player.containerMenu instanceof AbstractCraftingMenu crafting) {
                for (var slot : crafting.getInputGridSlots()) held.add(slot.getItem());
            }
            for (ItemStack stack : held) {
                if (!stack.isEmpty()) {
                    String item = BuiltInRegistries.ITEM.getKey(stack.getItem()).getPath();
                    inventory.addProperty(item, (inventory.has(item) ? inventory.get(item).getAsInt() : 0) + stack.getCount());
                }
            }
            state.add("inventory", inventory);
            state.addProperty("scaffold_blocks", scaffoldBlocks);
            state.addProperty("inventory_complete", true);
            state.addProperty("inventory_includes_selected", true);
            state.addProperty("inventory_count_scope", "all_player_inventory_slots_including_selected_hotbar_crafting_grid_and_cursor");
            state.add("inventory_slots", inventorySlots(client));
            JsonArray entities = new JsonArray();
            var nearby = client.level.getEntities(
                client.player,
                client.player.getBoundingBox().inflate(64.0),
                entity -> true
            );
            Entity latestVisibleHostile = null;
            for (Entity entity : nearby) {
                // A dragon's hitbox parts share its type; the dragon itself is listed once, with head_pos.
                if (entity instanceof net.minecraft.world.entity.boss.enderdragon.EnderDragonPart) continue;
                if (hostile(entity) && entity.isAlive() && hasLineOfSight(client, entity)
                    && (latestVisibleHostile == null || client.player.distanceToSqr(entity) < client.player.distanceToSqr(latestVisibleHostile)))
                    latestVisibleHostile = entity;
                JsonObject observed = new JsonObject();
                observed.addProperty("id", entity.getUUID().toString());
                observed.addProperty("name", entity.getName().getString());
                observed.addProperty("type", BuiltInRegistries.ENTITY_TYPE.getKey(entity.getType()).getPath());
                JsonArray entityPos = new JsonArray();
                entityPos.add(entity.getX());
                entityPos.add(entity.getY());
                entityPos.add(entity.getZ());
                observed.add("pos", entityPos);
                observed.addProperty("dist2", client.player.distanceToSqr(entity));
                observed.addProperty("hostile", hostile(entity));
                if (entity instanceof Creeper creeper) {
                    // Hissing near a player is the swell direction; isIgnited is only flint and steel.
                    observed.addProperty("swelling", creeper.isIgnited() || creeper.getSwellDir() > 0);
                }
                if (entity instanceof net.minecraft.world.entity.LivingEntity living) {
                    observed.addProperty("health", living.getHealth());
                    observed.addProperty("max_health", living.getMaxHealth());
                    creatureMotion(client, entity, observed);
                }
                if (entity instanceof net.minecraft.world.entity.animal.sheep.Sheep sheep) {
                    JsonObject properties = new JsonObject();
                    properties.addProperty("sheared", sheep.isSheared());
                    properties.addProperty("color", sheep.getColor().getName());
                    observed.add("properties", properties);
                }
                if (entity instanceof net.minecraft.world.entity.boss.enderdragon.EnderDragon dragon) {
                    int phaseId = dragon.getEntityData().get(net.minecraft.world.entity.boss.enderdragon.EnderDragon.DATA_PHASE);
                    var phase = net.minecraft.world.entity.boss.enderdragon.phases.EnderDragonPhase.getById(phaseId);
                    observed.addProperty("phase_id", phaseId);
                    observed.addProperty("phase", phase.toString());
                    JsonArray headPos = new JsonArray();
                    headPos.add(dragon.head.getX());
                    headPos.add(dragon.head.getY());
                    headPos.add(dragon.head.getZ());
                    observed.add("head_pos", headPos);
                }
                if (entity.getType() == net.minecraft.world.entity.EntityType.END_CRYSTAL) {
                    BlockPos center = entity.blockPosition();
                    observed.addProperty("caged", isCagedCrystal(
                        at -> "iron_bars".equals(BuiltInRegistries.BLOCK.getKey(client.level.getBlockState(at).getBlock()).getPath()),
                        center));
                }
                if (entity instanceof net.minecraft.world.entity.item.ItemEntity itemEntity) {
                    observed.addProperty("item", itemPath(itemEntity.getItem()));
                }
                entities.add(observed);
            }
            if (latestVisibleHostile != null) lastShelterHostile = latestVisibleHostile.position();
            state.add("entities", entities);
        }
        if (client.player != null && client.level != null) state.add("exposed_ores", exposedOres(client));
        if (baritone != null) {
            state.addProperty("pathing", baritone.getPathingBehavior().isPathing());
            state.addProperty("allow_inventory", BaritoneAPI.getSettings().allowInventory.value);
            if (run != null) {
                state.add("navigation", navigationFacts(run));
            }
            state.addProperty("mine_active", baritone.getMineProcess().isActive());
            state.addProperty("goal_active", baritone.getCustomGoalProcess().isActive());
            state.addProperty("get_active", baritone.getGetToBlockProcess().isActive());
            state.addProperty("explore_active", baritone.getExploreProcess().isActive());
            state.addProperty("follow_active", baritone.getFollowProcess().isActive());
            state.addProperty("farm_active", baritone.getFarmProcess().isActive());
            state.addProperty("builder_active", baritone.getBuilderProcess().isActive());
            state.addProperty("builder_paused", baritone.getBuilderProcess().isPaused());
            JsonArray placeable = new JsonArray();
            try {
                for (BlockState block : baritone.getBuilderProcess().getApproxPlaceable()) {
                    placeable.add(BuiltInRegistries.BLOCK.getKey(block.getBlock()).toString());
                }
            } catch (RuntimeException ignored) {
                // Builder inspection is advisory when no schematic is loaded.
            }
            state.add("builder_placeable", placeable);
            baritone.getBuilderProcess().getMinLayer().ifPresent(value -> state.addProperty("builder_min_layer", value));
            baritone.getBuilderProcess().getMaxLayer().ifPresent(value -> state.addProperty("builder_max_layer", value));
            state.add("builder_selections", selectionFacts(baritone.getSelectionManager()));
            state.addProperty("elytra_active", baritone.getElytraProcess().isActive());
            state.addProperty("elytra_loaded", baritone.getElytraProcess().isLoaded());
            state.addProperty("elytra_safe_to_cancel", baritone.getElytraProcess().isSafeToCancel());
            state.addProperty("elytra_destination", baritone.getElytraProcess().currentDestination() == null
                ? "" : baritone.getElytraProcess().currentDestination().toShortString());
            state.addProperty("elytra_path_size", baritone.getElytraProcess().getPath().size());
            JsonArray following = new JsonArray();
            var followed = baritone.getFollowProcess().following();
            if (followed != null) {
                for (Entity entity : followed) {
                    JsonObject observed = new JsonObject();
                    observed.addProperty("name", entity.getName().getString());
                    observed.addProperty("distance", client.player.distanceTo(entity));
                    following.add(observed);
                }
            }
            state.add("following", following);
            state.addProperty("any_process_active", processActive());
            // Goal heuristics can be expensive; never evaluate them while sampling client state.
            var goal = baritone.getPathingBehavior().getGoal();
            state.addProperty("baritone_goal", goal == null ? "" : goal.toString());
            state.addProperty("build_in_layers", BaritoneAPI.getSettings().buildInLayers.value);
            state.addProperty("allow_break", BaritoneAPI.getSettings().allowBreak.value);
            state.addProperty("allow_place", BaritoneAPI.getSettings().allowPlace.value);
            state.addProperty("explore_for_blocks", BaritoneAPI.getSettings().exploreForBlocks.value);
            state.addProperty("replant_crops", BaritoneAPI.getSettings().replantCrops.value);
            state.addProperty("farm_max_scan_size", BaritoneAPI.getSettings().farmMaxScanSize.value);
            state.addProperty("legit_mine_y", BaritoneAPI.getSettings().legitMineYLevel.value);
            state.addProperty("free_look", BaritoneAPI.getSettings().freeLook.value);
            state.add("movement", movementFacts());
            state.addProperty("gaze_look_down", gazeDown);
            state.addProperty("block_free_look", BaritoneAPI.getSettings().blockFreeLook.value);
            state.addProperty("remain_with_existing_look", BaritoneAPI.getSettings().remainWithExistingLookDirection.value);
            state.addProperty("smooth_look", BaritoneAPI.getSettings().smoothLook.value);
            state.addProperty("random_looking", BaritoneAPI.getSettings().randomLooking.value);
            state.addProperty("random_looking_113", BaritoneAPI.getSettings().randomLooking113.value);
        }
        return state;
    }

    private static int count(Minecraft client, String item) {
        if (item == null || client.player == null) {
            return 0;
        }
        int count = 0;
        for (int slot = 0; slot < client.player.getInventory().getContainerSize(); slot++) {
            ItemStack stack = client.player.getInventory().getItem(slot);
            if (!stack.isEmpty() && BuiltInRegistries.ITEM.getKey(stack.getItem()).getPath().equals(item)) {
                count += stack.getCount();
            }
        }
        return count;
    }

    private static String itemPath(ItemStack stack) {
        return stack.isEmpty() ? "" : BuiltInRegistries.ITEM.getKey(stack.getItem()).getPath();
    }

    private static String itemPath(String item) {
        return item == null ? "" : item.replace("minecraft:", "");
    }

    private static Goal parseGoalObjective(String objective) {
        String[] parts = objective.split(":", -1);
        try {
            if (parts[0].equals("y") && parts.length == 2) {
                return new GoalYLevel(Integer.parseInt(parts[1]));
            }
            if (parts[0].equals("block") && parts.length == 4) {
                return new GoalBlock(Integer.parseInt(parts[1]), Integer.parseInt(parts[2]), Integer.parseInt(parts[3]));
            }
            if (parts[0].equals("away") && parts.length == 5) {
                return new GoalRunAway(
                    Double.parseDouble(parts[4]),
                    new BlockPos(Integer.parseInt(parts[1]), Integer.parseInt(parts[2]), Integer.parseInt(parts[3]))
                );
            }
            if (parts[0].equals("any") && objective.startsWith("any:")) {
                String[] alternatives = objective.substring(4).split("\\|", -1);
                if (alternatives.length < 1 || alternatives.length > 64) {
                    throw new IllegalArgumentException("invalid_goal_composite_size");
                }
                Goal[] goals = new Goal[alternatives.length];
                for (int index = 0; index < alternatives.length; index++) {
                    goals[index] = parseGoalToken(alternatives[index]);
                }
                return new GoalComposite(goals);
            }
        } catch (NumberFormatException error) {
            throw new IllegalArgumentException("invalid_goal_coordinates", error);
        }
        throw new IllegalArgumentException("invalid_goal_objective");
    }

    private static Goal parseGoalToken(String token) {
        String[] parts = token.split(":", -1);
        try {
            return switch (parts[0]) {
                case "near" -> {
                    if (parts.length != 5) throw new IllegalArgumentException("invalid_near_goal");
                    yield new GoalNear(
                        new BlockPos(Integer.parseInt(parts[1]), Integer.parseInt(parts[2]), Integer.parseInt(parts[3])),
                        Integer.parseInt(parts[4])
                    );
                }
                case "xz" -> {
                    if (parts.length != 3) throw new IllegalArgumentException("invalid_xz_goal");
                    yield new GoalXZ(Integer.parseInt(parts[1]), Integer.parseInt(parts[2]));
                }
                case "y" -> {
                    if (parts.length != 2) throw new IllegalArgumentException("invalid_y_goal");
                    yield new GoalYLevel(Integer.parseInt(parts[1]));
                }
                case "block" -> {
                    if (parts.length != 4) throw new IllegalArgumentException("invalid_block_goal");
                    yield new GoalBlock(Integer.parseInt(parts[1]), Integer.parseInt(parts[2]), Integer.parseInt(parts[3]));
                }
                default -> throw new IllegalArgumentException("unsupported_goal_kind");
            };
        } catch (NumberFormatException error) {
            throw new IllegalArgumentException("invalid_goal_coordinates", error);
        }
    }

    private static String string(JsonObject object, String key, String fallback) {
        return object.has(key) ? object.get(key).getAsString() : fallback;
    }

    private static BlockPos position(String[] parts, int index) {
        return new BlockPos(Integer.parseInt(parts[index]), Integer.parseInt(parts[index + 1]), Integer.parseInt(parts[index + 2]));
    }

    private static JsonObject envelope(String type, String id) {
        JsonObject object = new JsonObject();
        object.addProperty("type", type);
        object.addProperty("action_id", id);
        return object;
    }

    private static void emit(Run run, JsonObject object) {
        if (run.peer != null) {
            run.peer.send(object);
        }
    }

    private static final class Run {
        JsonArray bedBlockedCells;
        // Torches put down while digging in the dark (lightTheWay).
        int lightNextTick;
        int torchesPlaced;
        final JsonArray torchCells = new JsonArray();
        final String id;
        final String objective;
        final Peer peer;
        final long started = System.nanoTime();
        final long timeout;
        final Map<String, Integer> pathEvents = new LinkedHashMap<>();
        boolean routeFound = true;
        // stepTo: horizontal distance left at the last headway check.
        double stepLeft = Double.MAX_VALUE;
        // B11 make-way: the walk's own goal while the Body digs, pillars or seals water.
        Goal wayGoal;
        int makeWaySteps;
        int waterSealed;
        int wayHoldUntil;
        int sealNextTick;
        int sealTries;
        BlockHitResult sealHit;
        final List<BlockPos> sealQueue = new ArrayList<>();
        boolean swimAbandoned;
        Boolean wayPreviousAllowBreak;
        Boolean wayPreviousAllowPlace;
        Goal goal;
        String kind;
        BlockPos anchor;
        BlockPos end;
        int range;
        int sx;
        int sy;
        int sz;
        boolean seenActive;
        boolean seenFlying;
        Boolean previousAllowInventory;
        Boolean[] previousBreakFromAbove;
        final NavigationProgress navigationProgress = new NavigationProgress();
        PathExecutor observedExecutor;
        PathExecutor checkedFailedExecutor;
        JsonObject navigation = new JsonObject();
        String navigationStep = "";
        String navigationWorld = "";
        final Map<BlockPos, Float> miningHighWater = new LinkedHashMap<>();
        String target;
        String item;
        String[] targets = new String[0];
        int mineQuantity = 1;
        BlockPos lastResourceTarget;
        boolean structured;
        BlockHitResult placementHit;
        boolean placementConfirmed;
        final List<BlockPos> shelterPlan = new ArrayList<>();
        final JsonObject shelterFacts = new JsonObject();
        BlockPos shelterTarget;
        int shelterIndex;
        boolean shelterHit;
        boolean shelterRetried;
        boolean shelterPortOpened;
        JsonObject params = new JsonObject();
        int phase;
        int phaseTick;
        // A walk into a portal block (tickPortalWalk): its cell, the dimension
        // it starts in, 1 once stepping in, and when the step and the stand began.
        BlockPos portalCell;
        net.minecraft.resources.ResourceKey<net.minecraft.world.level.Level> portalDimension;
        int portalPhase;
        int portalStepTick;
        int portalInsideTick = -1;
        int outputMenuSlot = -1;
        String outputExpected = "";
        int outputRequested;
        int craftUnitCount;
        int craftTaken;
        JsonArray craftBefore;
        long resultRevision;
        long menuRevision;
        Entity targetEntity;
        double initialHealth = -1.0;
        int attacks;
        int shieldDuration;
        int shieldHitsBlocked;
        int shieldHitsTaken;
        int shieldBlockingTicks;
        int shieldLastHurtTime;
        int bedInventoryBefore;
        boolean bedPlaced;
        boolean bedRecovered;
        int pillarBlocks;
        int pillarRaised;
        int pillarX;
        int pillarZ;
        int pillarStartY;
        int pillarRecentred;
        long bedMessageSequenceBefore;
        String bedGameMessage = "";
        String bedSleepReason = "";
        int shots;
        String motionTargetId = "";
        final List<TargetObservation> targetObservations = new ArrayList<>();
        double leadBlocks;
        Vec3 predictedTargetPos;
        int lastAttackTick = -1;
        int lastShotTick = -1;
        int useStartedTick = -1;
        double shotHealthBaseline = -1.0;
        // The arrows already in the world while the bow was drawn.
        java.util.Set<Integer> arrowsBefore = java.util.Set.of();
        BlockPos cushionAt;
        int cushionScoopTicks;
        boolean cushionScoopUsed;
        boolean cushionScooped;
        boolean damageObserved;
        // How far a ranged target was when the shot was refused, and the reach asked for.
        double rangeDistance = Double.NaN;
        double rangeMax = Double.NaN;
        // engageEntity: ticks the walk toward the target has been over, and
        // the block between the Body's eyes and the target's when it ended.
        int engageIdleTicks;
        int kiteTicks;
        boolean engageWayDecided;
        int kills;
        LivingEntity sweepTarget;
        float bodyHealthStart = -1.0f;
        BlockPos sightBlockedPos;
        String sightBlockedBlock = "";
        // keep_moving: the circle, the point walked to, and what was seen on the way.
        Vec3 keepCenter;
        double keepRadius;
        boolean keepSprint;
        BlockPos keepTarget;
        int keepGoalTick;
        Vec3 keepCheckPos;
        int keepCheckTick;
        int keepVisited;
        int keepStalls;
        int keepRepicks;
        int keepNoPointTicks;
        // Outside the circle: walking in to a point inside it (not counted as visited).
        boolean keepApproaching;
        // Outside with no point inside known yet: heading for the centre.
        boolean keepHeadingIn;
        int keepHeadingInTick;
        boolean keepEntered;
        double keepHeading = Double.NaN;
        final java.util.ArrayDeque<BlockPos> keepRecent = new java.util.ArrayDeque<>();
        final List<BlockPos> keepBad = new ArrayList<>();
        JsonObject keepRejections = new JsonObject();
        Boolean keepPreviousSprint;
        java.util.Random keepRandom;
        int openMenuId = -1;
        int transferSource = -1;
        int transferDestination = -1;
        String transferItem = "";
        int transferBeforeSource;
        int transferBeforeDestination;
        int transferRequested;
        boolean transferToContainer;
        int transferContainerSlot;
        int transferBotSlot;
        int mutations;
        String handoffReceiverId = "";
        String handoffReceiverName = "";
        int handoffExpected;
        int handoffRemaining;
        final Set<Integer> handoffExistingDrops = new HashSet<>();
        final Map<Integer, Integer> handoffDrops = new LinkedHashMap<>();
        int handoffPickedUp;
        boolean handoffReceipt;
        final Map<BlockPos, BlockState> verticalTouched = new LinkedHashMap<>();
        final Set<BlockPos> verticalConfirmed = new HashSet<>();
        int verticalDistance;
        int verticalSettledTicks;
        int verticalIdleTicks;
        /** The same call from this spot would fail the same way: say so in the terminal. */
        boolean sameAgainHere;
        boolean swimmingUp;
        int swimPreviousAir;
        double swimBestY;
        int swimNoRiseTicks;
        Vec3 swimProgressPosition;
        Goal swimRouteGoal;
        volatile boolean swimPathFailed;
        boolean swimRoutePending;
        int swimNextRing = 1;
        Boolean swimPreviousAllowBreak;
        Boolean swimPreviousAllowPlace;
        Boolean swimPreviousSubmergedNavigation;
        List<net.minecraft.world.item.Item> previousThrowawayItems;
        Integer previousFollowRadius;
        final Set<BlockPos> collectionTargets = new HashSet<>();
        final Map<BlockPos, BlockState> worldTouched = new LinkedHashMap<>();
        final JsonArray worldEdits = new JsonArray();
        int worldEditsSent;
        boolean worldTouchedTruncated;
        int settleTicks;
        final Set<String> collectionBlocks = new HashSet<>();
        final Set<String> collectionItems = new HashSet<>();
        boolean collectionTree;
        BlockPos collectionBreaking;
        int collectionBreakTick;
        int collectionRegoals;
        boolean[] attentionSettings;
        double[] attentionNoise;
        boolean lookDown;
        double followKeepDistance = 3.0;
        String stopStatus;
        String stopReason;
        String heldTool = "";
        String brokenItem;
        int heldToolSlot = -1;
        boolean heldToolLastUse;
        String lastPathEvent = "";
        int before;
        int ticks;
        int stableTicks;
        long stopAt;
        long baritoneStoppedAt;

        Run(String id, String objective, Peer peer, long timeout) {
            this.id = id;
            this.objective = objective;
            this.peer = peer;
            this.timeout = timeout;
        }

        long elapsed() {
            return (System.nanoTime() - started) / 1_000_000;
        }
    }

    private record TargetObservation(long tick, Vec3 position) {}

    private record MenuRead(String id, Peer peer, BlockPos pos, long revision, long started) {
        MenuRead(String id, Peer peer, BlockPos pos, long revision) {
            this(id, peer, pos, revision, System.nanoTime());
        }
    }

    private static final class Peer {
        final Socket socket;
        final DataInputStream input;
        final DataOutputStream output;

        Peer(Socket socket) throws IOException {
            this.socket = socket;
            input = new DataInputStream(socket.getInputStream());
            output = new DataOutputStream(socket.getOutputStream());
        }

        synchronized void send(JsonObject object) {
            try {
                byte[] bytes = object.toString().getBytes(StandardCharsets.UTF_8);
                output.writeInt(bytes.length);
                output.write(bytes);
                output.flush();
            } catch (IOException error) {
                try {
                    socket.close();
                } catch (IOException ignored) {
                    // The client loop will observe EOF.
                }
            }
        }
    }
}
