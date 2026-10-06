package dev.minebot.client.body;

import baritone.api.BaritoneAPI;
import baritone.api.IBaritone;
import baritone.api.event.events.PathEvent;
import baritone.api.event.listener.AbstractGameEventListener;
import baritone.api.pathing.goals.GoalBlock;
import baritone.api.utils.BlockOptionalMetaLookup;
import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import net.fabricmc.api.ClientModInitializer;
import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientTickEvents;
import net.fabricmc.fabric.api.client.networking.v1.ClientPlayConnectionEvents;
import net.minecraft.client.Minecraft;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.world.item.ItemStack;
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
import java.util.HashSet;
import java.util.LinkedHashMap;
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
    private boolean ready;
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
                            response.addProperty("mod_to_ready_ms", (System.nanoTime() - loadedAt) / 1_000_000);
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
            } else if (type.equals("settings")) {
                if (run != null) {
                    throw new IllegalArgumentException("body_busy");
                }
                if (request.has("explore_for_blocks")) {
                    BaritoneAPI.getSettings().exploreForBlocks.value = request.get("explore_for_blocks").getAsBoolean();
                }
                JsonObject response = state(client);
                response.addProperty("type", "settings");
                connection.send(response);
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
            response.addProperty("reason", error.getMessage());
            connection.send(response);
        } catch (RuntimeException error) {
            JsonObject response = envelope("rejected", id);
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
        if (parts.length == 4 && (parts[0].equals("goto") || parts[0].equals("navigate"))) {
            try {
                candidate.goal = new GoalBlock(
                    Integer.parseInt(parts[1]), Integer.parseInt(parts[2]), Integer.parseInt(parts[3])
                );
            } catch (NumberFormatException error) {
                throw new IllegalArgumentException("invalid_coordinates");
            }
        } else if ((parts.length == 2 || parts.length == 3) && parts[0].equals("mine") && !parts[1].isBlank()) {
            candidate.target = parts[1];
            candidate.item = parts.length == 3 ? parts[2] : parts[1];
        } else {
            throw new IllegalArgumentException("unsupported_objective");
        }
        candidate.before = count(client, candidate.item);
        usedIds.add(id);
        run = candidate;
        if (candidate.goal != null) {
            baritone.getCustomGoalProcess().setGoalAndPath(candidate.goal);
        } else {
            baritone.getMineProcess().mineByName(1, candidate.target);
        }
        emit(candidate, envelope("accepted", id));
        LOG.info("MINEBOT_PHASE_A objective_started id={} objective={}", id, objective);
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
            JsonObject response = state(client);
            response.addProperty("type", "ready");
            response.addProperty("mod_to_ready_ms", (System.nanoTime() - loadedAt) / 1_000_000);
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
                requestStop("failed", "baritone_stopped_without_result");
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
        }
        if (baritone != null) {
            state.addProperty("pathing", baritone.getPathingBehavior().isPathing());
            state.addProperty("mine_active", baritone.getMineProcess().isActive());
            state.addProperty("goal_active", baritone.getCustomGoalProcess().isActive());
            state.addProperty("any_process_active", processActive());
            var goal = baritone.getPathingBehavior().getGoal();
            state.addProperty("baritone_goal", goal == null ? "" : goal.toString());
            state.addProperty("allow_break", BaritoneAPI.getSettings().allowBreak.value);
            state.addProperty("allow_place", BaritoneAPI.getSettings().allowPlace.value);
            state.addProperty("explore_for_blocks", BaritoneAPI.getSettings().exploreForBlocks.value);
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

    private static String string(JsonObject object, String key, String fallback) {
        return object.has(key) ? object.get(key).getAsString() : fallback;
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
        GoalBlock goal;
        String target;
        String item;
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
