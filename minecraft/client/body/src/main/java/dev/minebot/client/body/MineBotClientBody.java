package dev.minebot.client.body;

import baritone.api.BaritoneAPI;
import baritone.api.IBaritone;
import baritone.api.event.events.PathEvent;
import baritone.api.event.listener.AbstractGameEventListener;
import baritone.api.pathing.goals.GoalBlock;
import baritone.api.pathing.goals.Goal;
import baritone.api.pathing.goals.GoalComposite;
import baritone.api.pathing.goals.GoalXZ;
import baritone.api.pathing.goals.GoalNear;
import baritone.api.pathing.goals.GoalRunAway;
import baritone.api.pathing.goals.GoalYLevel;
import baritone.api.schematic.FillSchematic;
import baritone.api.selection.ISelection;
import baritone.api.utils.BlockOptionalMeta;
import baritone.api.utils.BlockOptionalMetaLookup;
import baritone.api.utils.BetterBlockPos;
import baritone.api.utils.Rotation;
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
import net.minecraft.world.phys.Vec3;
import net.minecraft.core.Direction;
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
            ready = false;
            joinedTicks = 0;
            if (menuRead != null) finishMenuRead(client, "server_disconnected");
            confirmedResults.clear();
        });
        ClientTickEvents.END_CLIENT_TICK.register(this::tick);
        Thread.ofPlatform().daemon().name("minebot-spike-ipc").start(this::serve);
        LOG.info("MINEBOT_PHASE_A client_body_loaded minecraft=26.1.2 baritone=1.18.0");
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
                baritone.getExploreProcess().applyJsonFilter(
                    Path.of(filter), request.has("invert") && request.get("invert").getAsBoolean()
                );
                JsonObject response = state(client);
                response.addProperty("type", type);
                response.addProperty("request_id", string(request, "request_id", ""));
                connection.send(response);
            } else if (type.equals("builder_open_schematic")) {
                if (run != null) throw new IllegalArgumentException("body_busy");
                baritone.getBuilderProcess().buildOpenSchematic();
                JsonObject response = state(client);
                response.addProperty("type", type);
                response.addProperty("request_id", string(request, "request_id", ""));
                connection.send(response);
            } else if (type.equals("builder_open_litematic")) {
                if (run != null) throw new IllegalArgumentException("body_busy");
                int index = request.has("index") ? request.get("index").getAsInt() : 0;
                if (index < 0 || index > 1024) throw new IllegalArgumentException("invalid_litematic_index");
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
        } else if (parts.length == 2 && (parts[0].equals("follow") || parts[0].equals("pickup"))) {
            candidate.target = parts[1];
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
        try {
            if (candidate.goal != null) baritone.getCustomGoalProcess().setGoalAndPath(candidate.goal);
            else switch (candidate.kind) {
                case "mine" -> baritone.getMineProcess().mineByName(candidate.mineQuantity, candidate.targets);
                case "get" -> baritone.getGetToBlockProcess().getToBlock(new BlockOptionalMeta(candidate.target));
                case "explore" -> baritone.getExploreProcess().explore(candidate.anchor.getX(), candidate.anchor.getZ());
                case "follow" -> baritone.getFollowProcess().follow(entity ->
                    entity.getName().getString().equals(candidate.target)
                        || BuiltInRegistries.ENTITY_TYPE.getKey(entity.getType()).getPath().equals(candidate.target));
                case "pickup" -> baritone.getFollowProcess().pickup(stack -> BuiltInRegistries.ITEM.getKey(stack.getItem()).getPath().equals(candidate.target));
                case "farm" -> baritone.getFarmProcess().farm(candidate.range, candidate.anchor);
                case "build" -> baritone.getBuilderProcess().build("minebot-owned-fixture", new FillSchematic(candidate.sx, candidate.sy, candidate.sz, new BlockOptionalMeta(candidate.target)), candidate.anchor);
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
            case "jump" -> client.player.input.makeJump();
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
        if (scope.equals("recipeData")) {
            data = recipeData(client, params);
        } else if (scope.equals("blockAt")) {
            BlockPos pos = new BlockPos(params.get("x").getAsInt(), params.get("y").getAsInt(), params.get("z").getAsInt());
            JsonObject fact = blockFact(client.level.getBlockState(pos), pos);
            for (var entry : fact.entrySet()) data.add(entry.getKey(), entry.getValue());
        } else if (scope.equals("blockCells")) {
            JsonArray cells = new JsonArray();
            JsonArray raw = params.has("cells") && params.get("cells").isJsonArray() ? params.getAsJsonArray("cells") : new JsonArray();
            for (var item : raw) {
                JsonArray xyz = item.getAsJsonArray();
                BlockPos pos = new BlockPos(xyz.get(0).getAsInt(), xyz.get(1).getAsInt(), xyz.get(2).getAsInt());
                cells.add(blockFact(client.level.getBlockState(pos), pos));
            }
            data.add("cells", cells);
            data.addProperty("count", cells.size());
        } else if (scope.equals("nearbyBlocks")) {
            JsonArray blocks = new JsonArray();
            JsonArray targets = params.has("block_types") && params.get("block_types").isJsonArray()
                ? params.getAsJsonArray("block_types") : new JsonArray();
            int radius = params.has("radius") ? Math.min(64, Math.max(1, params.get("radius").getAsInt())) : 16;
            for (var target : targets) {
                var locations = BaritoneAPI.getProvider().getWorldScanner().scanChunkRadius(
                    baritone.getPlayerContext(), new BlockOptionalMetaLookup(target.getAsString().replace("minecraft:", "")), radius, 10, 64
                );
                locations.forEach(pos -> {
                    JsonObject fact = new JsonObject();
                    fact.addProperty("x", pos.getX());
                    fact.addProperty("y", pos.getY());
                    fact.addProperty("z", pos.getZ());
                    fact.addProperty("type", target.getAsString().replace("minecraft:", ""));
                    blocks.add(fact);
                });
            }
            data.add("blocks", blocks);
            data.addProperty("count", blocks.size());
        } else {
            throw new IllegalArgumentException("unsupported_world_read_scope");
        }
        response.addProperty("ok", true);
        response.addProperty("complete", true);
        response.add("data", data);
        response.add("uncertainty", new JsonArray());
        connection.send(response);
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
            properties.addProperty(property.getName(), property.value(state).toString());
        }
        fact.add("properties", properties);
        return fact;
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
        client.gameMode.attack(client.player, selected);
    }

    private static void dropItem(Minecraft client, JsonObject params) {
        int slot = params.has("slot") ? params.get("slot").getAsInt() : client.player.getInventory().getSelectedSlot();
        if (slot < 0 || slot >= client.player.getInventory().getContainerSize()) throw new IllegalArgumentException("invalid_inventory_slot");
        client.player.getInventory().setSelectedSlot(Math.max(0, Math.min(8, slot)));
        client.player.drop("all".equals(string(params, "mode", "one")));
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
            if (run.structured && Minecraft.getInstance().player != null) {
                Minecraft client = Minecraft.getInstance();
                if (isVertical(run) && client.gameMode != null) client.gameMode.stopDestroyBlock();
                if (client.player.isUsingItem() && client.gameMode != null) {
                    client.gameMode.releaseUsingItem(client.player);
                }
                if (run.kind.equals("rangedAttack")) {
                    client.options.keyUse.setDown(false);
                }
                if (run.kind.equals("containerTransfer") || run.kind.equals("furnaceTransfer") || run.kind.equals("craftItem")) {
                    if (client.player.containerMenu.containerId == run.openMenuId) client.player.closeContainer();
                }
            }
        }
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
            if (current.item != null && count(client, current.item) - current.before >= 1) {
                requestStop("completed", "inventory_increased");
            } else if (current.goal != null && current.goal.isInGoal(client.player.blockPosition()) && client.player.onGround()) {
                requestStop("completed", "goal_reached");
            } else if (current.elapsed() > current.timeout) {
                requestStop("timeout", "deadline_exceeded");
            } else if (current.ticks > 20 && !processActive() && !baritone.getPathingBehavior().isPathing()) {
                boolean finite = current.seenActive && (current.kind.equals("build") || current.kind.equals("clear")
                    || current.kind.equals("get") || current.kind.equals("farm") || current.kind.equals("elytra"));
                requestStop(finite ? "completed" : "failed", finite ? "process_finished_verify_world" : "baritone_stopped_without_result");
            }
        }
        if (current.ticks % 20 == 0) {
            JsonObject response = state(client);
            response.addProperty("type", "progress");
            response.addProperty("action_id", current.id);
            emit(current, response);
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
                case "rangedAttack" -> tickRanged(client, current);
                case "craftItem" -> tickCraft(client, current);
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
            if (!itemPath(result).equals(outputItem) || result.getCount() != current.craftUnitCount) {
                requestStop("failed", "recipe_output_mismatch");
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
        Entity recipient = null;
        for (Entity entity : client.level.getEntities(client.player, client.player.getBoundingBox().inflate(32.0), value -> true)) {
            if (entity.getName().getString().equals(receiver)) {
                recipient = entity;
                break;
            }
        }
        if (recipient == null) {
            requestStop("failed", "receiver_not_found");
            return;
        }
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
            if (stack.isEmpty() || !itemPath(stack).equals(item) || stack.getCount() < wanted) {
                requestStop("failed", "handoff_source_mismatch");
                return;
            }
            client.player.getInventory().setSelectedSlot(Math.max(0, Math.min(8, slot)));
            baritone.getPathingBehavior().cancelEverything();
            // Drop downward after rotation is sent; a horizontal throw can
            // pass the recipient before vanilla's pickup delay expires.
            lookAtDropPoint(client);
            current.handoffReceiverId = recipient.getUUID().toString();
            current.handoffReceiverName = recipient.getName().getString();
            current.handoffExpected = wanted;
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
            lookAtDropPoint(client);
            if (current.ticks - current.phaseTick < 2 || client.player.getXRot() < 85.0f) return;
            for (int index = 0; index < wanted; index++) client.player.drop(false);
            current.phase = 2;
            current.phaseTick = current.ticks;
            return;
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

    private static void lookAtDropPoint(Minecraft client) {
        BaritoneAPI.getProvider().getBaritoneForMinecraft(client).getLookBehavior()
            .updateTarget(new Rotation(client.player.getYRot(), 90.0f), true);
    }

    private void tickVertical(Minecraft client, Run current) {
        if (client.player.isInLava() || client.player.isInWater()) {
            requestStop("failed", "vertical_liquid_hazard");
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

    public static void observeBlockUpdate(BlockPos pos, BlockState state) {
        if (instance == null || instance.run == null || !isVertical(instance.run)) return;
        Run current = instance.run;
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
        JsonObject response = state(client);
        response.addProperty("type", "terminal");
        response.addProperty("action_id", current.id);
        response.addProperty("status", status);
        response.addProperty("reason", reason);
        response.addProperty("objective", current.objective);
        response.addProperty("elapsed_ms", current.elapsed());
        response.addProperty("stop_latency_ms", current.stopAt == 0 ? 0 : (System.nanoTime() - current.stopAt) / 1_000_000);
        response.addProperty("baritone_stop_latency_ms", current.stopAt == 0 || current.baritoneStoppedAt == 0
            ? 0 : (current.baritoneStoppedAt - current.stopAt) / 1_000_000);
        response.addProperty("inventory_before", current.before);
        response.addProperty("inventory_after", count(client, current.item));
        response.addProperty("inventory_delta", count(client, current.item) - current.before);
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

    private JsonObject state(Minecraft client) {
        JsonObject state = new JsonObject();
        state.addProperty("protocol", "minebot-client-spike/1");
        state.addProperty("ready", ready);
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
            state.addProperty("health", client.player.getHealth());
            state.addProperty("food", client.player.getFoodData().getFoodLevel());
            state.addProperty("oxygen", client.player.getAirSupply());
            state.addProperty("yaw", client.player.getYRot());
            state.addProperty("pitch", client.player.getXRot());
            state.addProperty("selected_slot", client.player.getInventory().getSelectedSlot());
            state.addProperty("selected_item", itemPath(client.player.getMainHandItem()));
            state.addProperty("offhand_item", itemPath(client.player.getOffhandItem()));
            state.addProperty("fall_flying", client.player.isFallFlying());
            state.addProperty("menu", client.player.containerMenu.getClass().getSimpleName());
            state.addProperty("chest_item", BuiltInRegistries.ITEM.getKey(client.player.getItemBySlot(EquipmentSlot.CHEST).getItem()).getPath());
            state.addProperty("feet_chunk_loaded", client.level.hasChunkAt(client.player.blockPosition()));
            JsonObject inventory = new JsonObject();
            for (int slot = 0; slot < client.player.getInventory().getContainerSize(); slot++) {
                ItemStack stack = client.player.getInventory().getItem(slot);
                if (!stack.isEmpty()) {
                    String item = BuiltInRegistries.ITEM.getKey(stack.getItem()).getPath();
                    inventory.addProperty(item, (inventory.has(item) ? inventory.get(item).getAsInt() : 0) + stack.getCount());
                }
            }
            state.add("inventory", inventory);
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
                entities.add(observed);
            }
            state.add("entities", entities);
        }
        if (baritone != null) {
            state.addProperty("pathing", baritone.getPathingBehavior().isPathing());
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
            baritone.getPathingBehavior().estimatedTicksToGoal()
                .ifPresent(value -> state.addProperty("path_estimated_ticks", value));
            var goal = baritone.getPathingBehavior().getGoal();
            state.addProperty("baritone_goal", goal == null ? "" : goal.toString());
            state.addProperty("allow_break", BaritoneAPI.getSettings().allowBreak.value);
            state.addProperty("allow_place", BaritoneAPI.getSettings().allowPlace.value);
            state.addProperty("explore_for_blocks", BaritoneAPI.getSettings().exploreForBlocks.value);
            state.addProperty("replant_crops", BaritoneAPI.getSettings().replantCrops.value);
            state.addProperty("farm_max_scan_size", BaritoneAPI.getSettings().farmMaxScanSize.value);
            state.addProperty("legit_mine_y", BaritoneAPI.getSettings().legitMineYLevel.value);
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
        String target;
        String item;
        String[] targets = new String[0];
        int mineQuantity = 1;
        boolean structured;
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
        final Set<Integer> handoffExistingDrops = new HashSet<>();
        final Map<Integer, Integer> handoffDrops = new LinkedHashMap<>();
        int handoffPickedUp;
        boolean handoffReceipt;
        final Map<BlockPos, BlockState> verticalTouched = new LinkedHashMap<>();
        final Set<BlockPos> verticalConfirmed = new HashSet<>();
        int verticalDistance;
        int verticalSettledTicks;
        int verticalIdleTicks;
        List<net.minecraft.world.item.Item> previousThrowawayItems;
        String stopStatus;
        String stopReason;
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
