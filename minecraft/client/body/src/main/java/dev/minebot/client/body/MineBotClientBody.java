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
import net.fabricmc.fabric.api.client.networking.v1.ClientPlayConnectionEvents;
import net.minecraft.client.Minecraft;
import net.minecraft.core.BlockPos;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.item.CrossbowItem;
import net.minecraft.world.InteractionHand;
import net.minecraft.world.InteractionResult;
import net.minecraft.world.level.block.state.BlockState;
import net.minecraft.world.level.block.state.properties.Property;
import net.minecraft.world.phys.BlockHitResult;
import net.minecraft.world.level.ClipContext;
import net.minecraft.world.phys.HitResult;
import net.minecraft.world.phys.EntityHitResult;
import net.minecraft.world.phys.Vec3;
import net.minecraft.core.Direction;
import net.minecraft.tags.FluidTags;
import net.minecraft.util.Mth;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.entity.EquipmentSlot;
import net.minecraft.world.entity.LivingEntity;
import net.minecraft.world.entity.item.ItemEntity;
import net.minecraft.world.entity.monster.Enemy;
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
    private static final Logger LOG = LoggerFactory.getLogger("minebot-client-body");
    private static final int MAX_FRAME = 1024 * 1024;
    private final Set<String> usedIds = new HashSet<>();
    private IBaritone baritone;
    private Run run;
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

    @Override
    public void onInitializeClient() {
        instance = this;
        ClientPlayConnectionEvents.JOIN.register((handler, sender, client) -> {
            joinedTicks = 0;
            ready = false;
            baritone = BaritoneAPI.getProvider().getBaritoneForMinecraft(client);
            baritone.getGameEventHandler().registerEventListener(new AbstractGameEventListener() {
                @Override
                public void onPathEvent(PathEvent event) {
                    if (run != null) {
                        run.lastPathEvent = event.name();
                        run.pathEvents.merge(event.name(), 1, Integer::sum);
                        if (run.swimRouteGoal != null && event == PathEvent.CALC_FAILED) {
                            run.swimPathFailed = true;
                        }
                        LOG.info("MINEBOT_PHASE_A path_event id={} event={}", run.id, event);
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
            ready = false;
            joinedTicks = 0;
            if (menuRead != null) finishMenuRead(client, "server_disconnected");
            confirmedResults.clear();
        });
        ClientTickEvents.END_CLIENT_TICK.register(this::tick);
        Thread.ofPlatform().daemon().name("minebot-spike-ipc").start(this::serve);
        LOG.info("MINEBOT_PHASE_A client_body_loaded minecraft=26.1.2 baritone=1.18.0+minebot.1");
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
        if (request.has("action") && request.get("action").isJsonObject()) {
            JsonObject action = request.getAsJsonObject("action");
            String actionName = string(action, "name", "").trim();
            if (actionName.isBlank()) throw new IllegalArgumentException("missing_action_name");
            candidate.kind = actionName;
            candidate.params = action.has("params") && action.get("params").isJsonObject()
                ? action.getAsJsonObject("params") : new JsonObject();
            candidate.structured = true;
            candidate.item = string(candidate.params, "item", "").replace("minecraft:", "");
            if (actionName.equals("craftItem")) {
                JsonObject output = candidate.params.has("output") && candidate.params.get("output").isJsonObject()
                    ? candidate.params.getAsJsonObject("output") : new JsonObject();
                candidate.item = string(output, "item", "").replace("minecraft:", "");
            }
            candidate.before = count(client, candidate.item);
            usedIds.add(id);
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
        candidate.before = count(client, candidate.item);
        usedIds.add(id);
        run = candidate;
        beginAttention(candidate);
        try {
            if (candidate.goal != null) baritone.getCustomGoalProcess().setGoalAndPath(candidate.goal);
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
                    baritone.getBuilderProcess().build("minebot-owned-fixture", new FillSchematic(candidate.sx, candidate.sy, candidate.sz, new BlockOptionalMeta(candidate.target)), candidate.anchor);
                }
                case "clear" -> baritone.getBuilderProcess().clearArea(candidate.anchor, candidate.end);
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

    private static JsonArray xyz(BlockPos pos) {
        JsonArray result = new JsonArray();
        result.add(pos.getX()); result.add(pos.getY()); result.add(pos.getZ());
        return result;
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
            if (!(client.player.containerMenu instanceof InventoryMenu menu)) {
                throw new IllegalArgumentException("inventory_menu_unavailable");
            }
            if (!menu.getCarried().isEmpty()) throw new IllegalArgumentException("inventory_cursor_occupied");
            int hotbar = client.player.getInventory().getSelectedSlot();
            for (int candidate = 0; candidate < 9; candidate++) {
                if (client.player.getInventory().getItem(candidate).isEmpty()) {
                    hotbar = candidate;
                    break;
                }
            }
            click(menu, menuSlotForPlayer(menu, slot), hotbar, ContainerInput.SWAP, client);
            client.player.getInventory().setSelectedSlot(hotbar);
            return;
        }
        throw new IllegalArgumentException("not_in_inventory");
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
        int slot = params.has("slot") ? params.get("slot").getAsInt() : client.player.getInventory().getSelectedSlot();
        if (slot < 0 || slot >= client.player.getInventory().getContainerSize()) throw new IllegalArgumentException("invalid_inventory_slot");
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
            baritone.getPathingBehavior().cancelEverything();
            baritone.getInputOverrideHandler().clearAllKeys();
            restoreSwimSettings(run);
            if (run.structured && Minecraft.getInstance().player != null) {
                Minecraft client = Minecraft.getInstance();
                if (isVertical(run) && client.gameMode != null) client.gameMode.stopDestroyBlock();
                if (client.player.isUsingItem() && client.gameMode != null) {
                    client.gameMode.releaseUsingItem(client.player);
                }
                if (run.kind.equals("rangedAttack") || run.kind.equals("useItem")) {
                    client.options.keyUse.setDown(false);
                }
                if (run.swimmingUp) client.options.keyJump.setDown(false);
                if (run.kind.equals("containerTransfer") || run.kind.equals("furnaceTransfer") || run.kind.equals("craftItem")) {
                    if (client.player.containerMenu.containerId == run.openMenuId) client.player.closeContainer();
                }
            }
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
        if (menuRead != null) tickMenuRead(client);
        if (!ready && joinedTicks >= 20 && client.level.hasChunkAt(client.player.blockPosition()) && client.player.onGround()) {
            ready = true;
            readyAtMs = (System.nanoTime() - loadedAt) / 1_000_000;
            JsonObject response = state(client);
            response.addProperty("type", "ready");
            response.addProperty("mod_to_ready_ms", readyAtMs);
            if (peer != null) {
                peer.send(response);
            }
            LOG.info("MINEBOT_PHASE_A ready {}", response);
        }
        Run current = run;
        if (current == null) {
            return;
        }
        current.ticks++;
        if (current.stopStatus == null) observeHeldTool(client, current);
        if (current.stopStatus == null) observeNavigation(client, current);
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
        if (current.stopStatus == null && !current.structured) {
            if (current.item != null && count(client, current.item) - current.before >= (current.kind.equals("mine") ? current.mineQuantity : 1)) {
                requestStop("completed", "inventory_increased");
            } else if (current.goal != null && current.goal.isInGoal(client.player.blockPosition()) && client.player.onGround()) {
                requestStop("completed", "goal_reached");
            } else if (current.kind.equals("follow")) {
                tickFollow(client, current);
            } else if (current.elapsed() > current.timeout) {
                requestStop("timeout", "deadline_exceeded");
            } else if (current.ticks > 20 && !processActive() && !baritone.getPathingBehavior().isPathing()
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
                    requestStop(finite ? "completed" : "failed", finite ? "process_finished_verify_world" : "baritone_stopped_without_result");
                }
            }
        }
        if (current.ticks % 20 == 0) {
            JsonObject response = state(client);
            if (current.kind.equals("build")) response.add("construction_progress", cuboidProgress(client, current));
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
            boolean stopped = !processActive() && !baritone.getPathingBehavior().isPathing();
            if (stopped && current.baritoneStoppedAt == 0) {
                current.baritoneStoppedAt = System.nanoTime();
            }
            current.stableTicks = stopped ? current.stableTicks + 1 : 0;
            if (current.stableTicks >= 3) {
                terminal(client, current.stopStatus, current.stopReason);
            }
        }
    }

    private void tickBuilderFile(Minecraft client, Run current) {
        if (current.phase == 0) {
            String file = string(current.params, "path", "");
            BlockPos origin = positionParam(current.params, "origin");
            if (file.isBlank() || origin == null) throw new IllegalArgumentException("missing_builder_file");
            enableBuilderInventory(current);
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
                case "collectBlocks" -> tickCollectBlocks(client, current);
                case "rangedAttack" -> tickRanged(client, current);
                case "craftItem" -> tickCraft(client, current);
                case "builderBuildFile" -> tickBuilderFile(client, current);
                case "containerTransfer", "furnaceTransfer" -> tickMenuTransfer(client, current);
                case "handoffItem" -> tickHandoff(client, current);
                case "ascend", "digDown", "digUp" -> tickVertical(client, current);
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
                startCollectionGoal(client, current);
                current.phase = 1;
                current.phaseTick = current.ticks;
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
        boolean removed = current.collectionTargets.stream().allMatch(pos -> client.level.getBlockState(pos).isAir());
        if (removed && collectionCount(client, current) - current.before >= current.collectionTargets.size()) {
            requestStop("completed", "resource_cluster_collected");
        } else if (removed && current.phase == 1) {
            baritone.getPathingBehavior().cancelEverything();
            current.previousFollowRadius = BaritoneAPI.getSettings().followRadius.value;
            BaritoneAPI.getSettings().followRadius.value = 0;
            baritone.getFollowProcess().follow(entity -> entity instanceof ItemEntity item
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
            if (!current.collectionTree && current.goal.isInGoal(client.player.blockPosition())) {
                startCollectionGoal(client, current);
                current.phaseTick = current.ticks;
            } else {
                requestStop("failed", "resource_cluster_incomplete");
            }
        }
    }

    private void startCollectionGoal(Minecraft client, Run current) {
        current.goal = new GoalComposite(current.collectionTargets.stream()
            .filter(pos -> !client.level.getBlockState(pos).isAir())
            .map(GoalTwoBlocks::new).toArray(Goal[]::new));
        baritone.getCustomGoalProcess().setGoalAndPath(current.goal);
    }

    private static int collectionCount(Minecraft client, Run current) {
        return current.collectionItems.stream().mapToInt(item -> count(client, item)).sum();
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
        if (current.phase == 0) {
            if (!current.item.isBlank()) selectItem(client, current.item);
            else current.item = itemPath(client.player.getMainHandItem());
            current.before = count(client, current.item);
            current.phaseTick = current.ticks; current.phase = 1;
            return;
        }
        if (mode.equals("once")) {
            if (current.phase == 1 && current.ticks - current.phaseTick >= 3) {
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
        double disengage = number(current.params, "disengage_health", 0.0);
        if (disengage > 0.0 && client.player.getHealth() <= disengage) {
            requestStop("failed", "disengaged_low_health");
            return;
        }
        double attackRange = clamp(number(current.params, "attack_range", 3.0), 1.5, 4.5);
        boolean visible = hasLineOfSight(client, target);
        if (client.player.distanceTo(target) > attackRange || !visible) {
            approachCombatTarget(client, target, attackRange);
            return;
        }
        baritone.getPathingBehavior().cancelEverything();
        lookAtEntity(client, target);
        int cooldown = Math.max(1, integer(current.params, "cooldown_ticks", 10));
        if (current.lastAttackTick < 0 || current.ticks - current.lastAttackTick >= cooldown) {
            if (client.player.getAttackStrengthScale(0.0f) >= 0.9f) {
                client.gameMode.attack(client.player, target);
                client.player.resetAttackStrengthTicker();
                current.attacks++;
                current.lastAttackTick = current.ticks;
            }
        }
    }

    private void tickRanged(Minecraft client, Run current) {
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
        double maxRange = Math.max(2.0, Math.min(32.0, number(current.params, "max_range", 24.0)));
        boolean requireLineOfSight = booleanValue(current.params, "require_line_of_sight", false);
        if (client.player.distanceTo(target) > maxRange
            || (requireLineOfSight && !hasLineOfSight(client, target))) {
            // The search radius is not a navigation goal.  Close to a bounded
            // combat distance until the shot is in range; callers that need
            // strict visibility can opt in without making ordinary ranged
            // actions spin forever on a stale client-side ray trace.
            approachCombatTarget(client, target, number(current.params, "approach_range", 3.0));
            return;
        }
        baritone.getPathingBehavior().cancelEverything();
        lookAtEntity(client, target);
        int expected = Math.max(1, integer(current.params, "expected_shots", 1));
        int interval = Math.max(1, integer(current.params, "use_interval_ticks", weapon.equals("crossbow") ? 30 : 20));
        if (current.shots > 0 && (target.getHealth() < current.initialHealth - 0.01f
            || (current.shotHealthBaseline >= 0.0f && target.getHealth() < current.shotHealthBaseline - 0.01f))) {
            current.damageObserved = true;
        }
        if (current.shots >= expected && !client.player.isUsingItem()) {
            if (current.damageObserved) {
                requestStop("completed", "damage_observed");
            } else if (current.ticks - current.lastShotTick >= Math.max(40, interval * 3)) {
                requestStop("failed", "no_damage_observed");
            }
            return;
        }
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
        if (current.damageObserved && current.shots >= expected && !client.player.isUsingItem()) {
            requestStop("completed", "damage_observed");
        }
    }

    private LivingEntity combatTarget(Minecraft client, Run current) {
        String targetId = string(current.params, "target_uuid", string(current.params, "target_id", ""));
        if (targetId.isBlank() && current.targetEntity != null) targetId = current.targetEntity.getUUID().toString();
        String targetName = string(current.params, "target_name", "");
        String targetType = string(current.params, "target_type", "").replace("minecraft:", "");
        double radius = Math.max(1.0, Math.min(64.0, number(current.params, "acquire_radius", number(current.params, "radius", 32.0))));
        LivingEntity best = null;
        double bestDistance = Double.MAX_VALUE;
        for (Entity entity : client.level.getEntities(client.player, client.player.getBoundingBox().inflate(radius), value -> value instanceof LivingEntity)) {
            if (!(entity instanceof LivingEntity living) || !living.isAlive()) continue;
            boolean match;
            if (!targetId.isBlank()) match = entity.getUUID().toString().equals(targetId);
            else if (!targetName.isBlank()) match = entity.getName().getString().equals(targetName);
            else if (!targetType.isBlank()) match = BuiltInRegistries.ENTITY_TYPE.getKey(entity.getType()).getPath().equals(targetType);
            else match = entity instanceof Enemy;
            if (match && client.player.distanceToSqr(entity) < bestDistance) {
                best = living;
                bestDistance = client.player.distanceToSqr(entity);
            }
        }
        return best;
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
        double dx = target.getX() - client.player.getX();
        double dy = target.getY() + target.getBbHeight() * 0.55 - client.player.getEyeY();
        double dz = target.getZ() - client.player.getZ();
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
        if (client.player.containerMenu.containerId != 0 || !client.player.containerMenu.getCarried().isEmpty()) {
            throw new IllegalArgumentException("menu_busy");
        }
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
            if (integer(want, "count", 0) > 0 && !string(want, "item", "").equals(string(have, "item", ""))) return false;
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

    /** Cosmetic camera update after upstream has applied movement/packet rotation. */
    public static void smoothTravelLook(float previousYaw, boolean blockInteract) {
        if (instance == null || instance.run == null || instance.run.stopStatus != null || blockInteract) return;
        Minecraft client = Minecraft.getInstance();
        if (client.player == null || client.player.isFallFlying() || instance.run.kind.equals("elytra")) return;
        float delta = Mth.wrapDegrees(client.player.getYRot() - previousYaw);
        client.player.setYRot(previousYaw + Mth.clamp(delta, -24.0f, 24.0f));
    }

    private void tickVertical(Minecraft client, Run current) {
        if (client.player.isInLava()) {
            requestStop("failed", "vertical_liquid_hazard");
            return;
        }
        if (current.kind.equals("ascend") && (current.swimmingUp || client.player.isInWater())) {
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
                requestStop("failed", "surface_not_found");
                return;
            }
            if (Math.abs(targetY - current.anchor.getY()) > current.verticalDistance
                || targetY < client.level.getMinY() || targetY + 1 >= client.level.getMaxY()) {
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
        if (SurfaceSwimProgress.completed(headOut, current.swimPreviousAir, air, client.player.getMaxAirSupply())) {
            client.options.keyJump.setDown(false);
            requestStop("completed", "surface_reached");
            return;
        }
        current.swimPreviousAir = air;
        if (current.swimPathFailed) {
            // Upstream has already proved these goal cells unreachable. Try
            // the next ring immediately rather than wait motionless for the
            // ordinary physical stall detector to expire again.
            if (!routeSwimToAir(client, current)) {
                requestStop("failed", "swim_surface_route_not_found");
                return;
            }
            current.swimNoRiseTicks = 0;
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

    private boolean routeSwimToAir(Minecraft client, Run current) {
        SurfaceSwimProgress.Space space = new SurfaceSwimProgress.Space() {
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
        var anchor = new SurfaceSwimProgress.Cell(current.anchor.getX(), current.anchor.getY(), current.anchor.getZ());
        var targets = SurfaceSwimProgress.routeTargets(space, anchor, client.level.getMinY(),
            client.level.getMaxY(), current.verticalDistance, current.swimNextRing);
        if (targets.isEmpty()) return false;
        var settings = BaritoneAPI.getSettings();
        if (current.swimPreviousAllowBreak == null) {
            current.swimPreviousAllowBreak = settings.allowBreak.value;
            current.swimPreviousAllowPlace = settings.allowPlace.value;
        }
        // An emergency detour must not dig through somebody's ceiling or place
        // blocks. Restore the caller's settings on every stop path.
        settings.allowBreak.value = false;
        settings.allowPlace.value = false;
        var first = targets.getFirst();
        current.swimNextRing = Math.max(Math.abs(first.x() - anchor.x()), Math.abs(first.z() - anchor.z())) + 1;
        current.swimRouteGoal = new GoalComposite(targets.stream()
            .map(p -> (Goal) new GoalBlock(p.x(), p.y(), p.z())).toArray(Goal[]::new));
        current.goal = current.swimRouteGoal;
        current.swimPathFailed = false;
        current.phase = 2;
        current.phaseTick = current.ticks;
        baritone.getCustomGoalProcess().setGoalAndPath(current.swimRouteGoal);
        return true;
    }

    private static void restoreSwimSettings(Run current) {
        if (current == null || current.swimPreviousAllowBreak == null) return;
        BaritoneAPI.getSettings().allowBreak.value = current.swimPreviousAllowBreak;
        BaritoneAPI.getSettings().allowPlace.value = current.swimPreviousAllowPlace;
        current.swimPreviousAllowBreak = null;
        current.swimPreviousAllowPlace = null;
    }

    private int findSurfaceY(Minecraft client, Run current) {
        int y = client.player.blockPosition().getY();
        if (client.level.canSeeSky(client.player.blockPosition()) && client.player.onGround()) return y;
        int limit = current.verticalDistance;
        for (int offset = 1; offset <= limit; offset++) {
            BlockPos feet = new BlockPos(client.player.blockPosition().getX(), y + offset, client.player.blockPosition().getZ());
            if (client.level.hasChunkAt(feet) && client.level.canSeeSky(feet)
                && client.level.getBlockState(feet).isAir() && client.level.getBlockState(feet.above()).isAir()
                && !client.level.getBlockState(feet.below()).getCollisionShape(client.level, feet.below()).isEmpty()) return y + offset;
        }
        return Integer.MIN_VALUE;
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
        if (client.level == null || instance.run.worldTouched.size() >= 4096) return;
        instance.run.worldTouched.putIfAbsent(pos.immutable(), client.level.getBlockState(pos));
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
        Boolean target = editTarget(current, pos, before);
        if (target == null) edit.add("target", com.google.gson.JsonNull.INSTANCE);
        else edit.addProperty("target", target);
        edit.addProperty("tick", current.ticks);
        return edit;
    }

    /** Whether a confirmed edit lies inside the action's declared target; null when the action declares none. */
    private static Boolean editTarget(Run current, BlockPos pos, BlockState before) {
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
            case "placeBlock" -> pos.equals(current.anchor);
            default -> NAVIGATION_KINDS.contains(current.kind) ? Boolean.FALSE : null;
        };
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
            if (hasVerticalLiquidHazard(client, pos)) denial = "vertical_liquid_hazard";
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
        if (current != null && status.equals("failed") && reason.equals("no_damage_observed")
            && current.kind.equals("rangedAttack") && current.shots > 0
            && current.targetEntity instanceof LivingEntity target
            && current.shotHealthBaseline >= 0.0f
            && target.getHealth() < current.shotHealthBaseline - 0.01f) {
            current.damageObserved = true;
            status = "completed";
            reason = "damage_observed";
        }
        if (current.previousFollowRadius != null) {
            BaritoneAPI.getSettings().followRadius.value = current.previousFollowRadius;
        }
        restoreAttention(current);
        restoreBuilderInventory(current);
        if (current.worldEditsSent < current.worldEdits.size()) emit(current, liteProgress(client, current));
        JsonObject response = state(client);
        response.addProperty("flight_observed", current.seenFlying);
        response.addProperty("type", "terminal");
        response.addProperty("action_id", current.id);
        response.addProperty("status", status);
        response.addProperty("native_stopped", !processActive() && !baritone.getPathingBehavior().isPathing());
        response.addProperty("reason", reason);
        response.addProperty("objective", current.objective);
        response.addProperty("elapsed_ms", current.elapsed());
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
            response.addProperty("damage_observed", current.damageObserved);
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
            state.addProperty("creative", client.player.getAbilities().instabuild);
            state.addProperty("dimension", client.level.dimension().identifier().toString());
            state.addProperty("difficulty", client.level.getDifficulty().name().toLowerCase(java.util.Locale.ROOT));
            state.addProperty("health", client.player.getHealth());
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
            state.add("inventory", inventory);
            state.addProperty("scaffold_blocks", scaffoldBlocks);
            state.addProperty("inventory_complete", true);
            state.addProperty("inventory_includes_selected", true);
            state.addProperty("inventory_count_scope", "all_player_inventory_slots_including_selected_hotbar");
            state.add("inventory_slots", inventorySlots(client));
            JsonArray entities = new JsonArray();
            var nearby = client.level.getEntities(
                client.player,
                client.player.getBoundingBox().inflate(64.0),
                entity -> true
            );
            for (Entity entity : nearby) {
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
                observed.addProperty("hostile", entity instanceof Enemy);
                if (entity instanceof net.minecraft.world.entity.LivingEntity living) {
                    observed.addProperty("health", living.getHealth());
                }
                if (entity instanceof net.minecraft.world.entity.animal.sheep.Sheep sheep) {
                    JsonObject properties = new JsonObject();
                    properties.addProperty("sheared", sheep.isSheared());
                    properties.addProperty("color", sheep.getColor().getName());
                    observed.add("properties", properties);
                }
                entities.add(observed);
            }
            state.add("entities", entities);
        }
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
        final String id;
        final String objective;
        final Peer peer;
        final long started = System.nanoTime();
        final long timeout;
        final Map<String, Integer> pathEvents = new LinkedHashMap<>();
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
        JsonObject params = new JsonObject();
        int phase;
        int phaseTick;
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
        int shots;
        int lastAttackTick = -1;
        int lastShotTick = -1;
        int useStartedTick = -1;
        double shotHealthBaseline = -1.0;
        boolean damageObserved;
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
        boolean swimmingUp;
        int swimPreviousAir;
        double swimBestY;
        int swimNoRiseTicks;
        Vec3 swimProgressPosition;
        Goal swimRouteGoal;
        volatile boolean swimPathFailed;
        int swimNextRing = 1;
        Boolean swimPreviousAllowBreak;
        Boolean swimPreviousAllowPlace;
        List<net.minecraft.world.item.Item> previousThrowawayItems;
        Integer previousFollowRadius;
        final Set<BlockPos> collectionTargets = new HashSet<>();
        final Map<BlockPos, BlockState> worldTouched = new LinkedHashMap<>();
        final JsonArray worldEdits = new JsonArray();
        int worldEditsSent;
        final Set<String> collectionBlocks = new HashSet<>();
        final Set<String> collectionItems = new HashSet<>();
        boolean collectionTree;
        boolean[] attentionSettings;
        double[] attentionNoise;
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
