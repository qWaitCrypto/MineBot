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
import net.minecraft.world.InteractionHand;
import net.minecraft.world.InteractionResult;
import net.minecraft.world.level.block.state.BlockState;
import net.minecraft.world.level.block.state.properties.Property;
import net.minecraft.world.phys.BlockHitResult;
import net.minecraft.world.phys.Vec3;
import net.minecraft.core.Direction;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.entity.EquipmentSlot;
import net.minecraft.world.entity.monster.Enemy;
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
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.Locale;
import java.util.Map;
import java.util.Set;

/** A loopback-only Phase A experiment, not a production Body implementation. */
public final class MineBotClientBody implements ClientModInitializer {
    private static final Logger LOG = LoggerFactory.getLogger("minebot-client-body");
    private static final int MAX_FRAME = 1024 * 1024;
    private final Set<String> usedIds = new HashSet<>();
    private IBaritone baritone;
    private Run run;
    private volatile Peer peer;
    private int joinedTicks;
    private volatile boolean ready;
    private long readyAtMs;
    private final long loadedAt = System.nanoTime();

    @Override
    public void onInitializeClient() {
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
        if (run != null) {
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
        if (scope.equals("blockAt")) {
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
        throw new IllegalArgumentException("item_not_in_hotbar");
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
        if (from < 0 || to < 0 || from >= client.player.containerMenu.slots.size() || to >= client.player.containerMenu.slots.size()) {
            throw new IllegalArgumentException("invalid_inventory_slot");
        }
        int menu = client.player.containerMenu.containerId;
        client.gameMode.handleContainerInput(menu, from, 0, net.minecraft.world.inventory.ContainerInput.PICKUP, client.player);
        client.gameMode.handleContainerInput(menu, to, 0, net.minecraft.world.inventory.ContainerInput.PICKUP, client.player);
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
        }
    }

    private void tick(Minecraft client) {
        if (client.player == null || client.level == null || baritone == null) {
            return;
        }
        joinedTicks++;
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
        if (processActive()) current.seenActive = true;
        int cancelTicks = Integer.getInteger("minebot.client.cancel_after_ticks", 0);
        if (cancelTicks > 0 && current.ticks >= cancelTicks) {
            requestStop("canceled", "launch_cancel_requested");
        }
        if (current.stopStatus == null) {
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

    private void terminal(Minecraft client, String status, String reason) {
        Run current = run;
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
        run = null;
        emit(current, response);
        LOG.info("MINEBOT_PHASE_A terminal {}", response);
    }

    private JsonObject state(Minecraft client) {
        JsonObject state = new JsonObject();
        state.addProperty("protocol", "minebot-client-spike/1");
        state.addProperty("ready", ready);
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
