package dev.minebot.client.body.gametest;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import net.fabricmc.fabric.api.client.gametest.v1.context.ClientGameTestContext;

import java.io.DataInputStream;
import java.io.DataOutputStream;
import java.io.IOException;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.function.Predicate;

/**
 * The Body's length-prefixed JSON IPC, as Python's RealClientIpc speaks it.
 * The game only ticks when the test waits, so replies are awaited by
 * ticking, never by blocking on the socket.
 */
final class BodyIpc implements AutoCloseable {
    private final Socket socket;
    private final DataOutputStream output;
    private final LinkedBlockingQueue<JsonObject> incoming = new LinkedBlockingQueue<>();
    private final List<JsonObject> held = new ArrayList<>();
    private final ClientGameTestContext context;
    private int next;
    private final String prefix = "gt-" + java.util.UUID.randomUUID() + "-";

    private BodyIpc(ClientGameTestContext context, Socket socket) throws IOException {
        this.context = context;
        this.socket = socket;
        this.output = new DataOutputStream(socket.getOutputStream());
        DataInputStream input = new DataInputStream(socket.getInputStream());
        Thread.ofPlatform().daemon().name("body-gametest-ipc").start(() -> {
            try {
                while (true) {
                    byte[] bytes = input.readNBytes(input.readInt());
                    incoming.add(JsonParser.parseString(new String(bytes, StandardCharsets.UTF_8)).getAsJsonObject());
                }
            } catch (IOException | RuntimeException ignored) {
                // closed
            }
        });
    }

    static BodyIpc connect(ClientGameTestContext context) {
        int port = Integer.getInteger("minebot.client.ipc_port", 8768);
        try {
            BodyIpc ipc = new BodyIpc(context, new Socket("127.0.0.1", port));
            ipc.await(message -> "ready".equals(type(message)), 200, "the Body never said ready");
            return ipc;
        } catch (IOException error) {
            throw new AssertionError("Body IPC on port " + port + " unavailable: " + error);
        }
    }

    JsonObject primitive(String name, JsonObject params) {
        JsonObject reply = primitiveReply(name, params);
        BodyClientGameTest.check("primitive".equals(type(reply)), name + " was " + reply);
        return reply;
    }

    /** The raw reply to a primitive, which may be an error. */
    JsonObject primitiveReply(String name, JsonObject params) {
        String id = prefix + next++;
        JsonObject request = new JsonObject();
        request.addProperty("type", "primitive");
        request.addProperty("request_id", id);
        request.addProperty("action_name", name);
        request.add("params", params);
        send(request);
        return await(message -> id.equals(string(message, "request_id")), 100, name + " got no reply");
    }

    JsonObject state() {
        String id = prefix + next++;
        JsonObject request = new JsonObject();
        request.addProperty("type", "state");
        request.addProperty("request_id", id);
        send(request);
        return await(message -> id.equals(string(message, "request_id")) && "state".equals(type(message)), 100, "state got no reply");
    }

    /** A control request such as container_read, answered under its request id. */
    JsonObject control(String type, JsonObject params) {
        String id = prefix + next++;
        JsonObject request = new JsonObject();
        request.addProperty("type", type);
        request.addProperty("request_id", id);
        request.add("params", params);
        send(request);
        return await(message -> id.equals(string(message, "request_id")), 200, type + " got no reply");
    }

    JsonObject settings(JsonObject values) {
        String id = prefix + next++;
        JsonObject request = new JsonObject();
        request.addProperty("type", "settings");
        request.addProperty("request_id", id);
        request.add("protected_positions", values.get("protected_positions"));
        send(request);
        return await(message -> id.equals(string(message, "request_id")) && "settings".equals(type(message)),
            100, "settings got no reply");
    }

    JsonObject command(String name, JsonObject params, int timeoutTicks) {
        return terminal(startCommand(name, params, timeoutTicks), timeoutTicks + 40, () -> {});
    }

    String startCommand(String name, JsonObject params, int timeoutTicks) {
        JsonObject action = new JsonObject();
        action.addProperty("name", name);
        action.add("params", params);
        JsonObject request = new JsonObject();
        request.add("action", action);
        return start(request, timeoutTicks);
    }

    String startObjective(String objective, int timeoutTicks) {
        JsonObject request = new JsonObject();
        request.addProperty("objective", objective);
        return start(request, timeoutTicks);
    }

    /** An objective walk, looking at the ground when lookDown (look_down). */
    String startObjective(String objective, int timeoutTicks, boolean lookDown) {
        JsonObject request = new JsonObject();
        request.addProperty("objective", objective);
        if (lookDown) request.addProperty("look_down", true);
        return start(request, timeoutTicks);
    }

    private String start(JsonObject request, int timeoutTicks) {
        String id = prefix + next++;
        request.addProperty("type", "command");
        request.addProperty("action_id", id);
        request.addProperty("timeout_ms", timeoutTicks * 50L);
        send(request);
        return id;
    }

    JsonObject cancel(String id) {
        JsonObject request = new JsonObject();
        request.addProperty("type", "cancel");
        request.addProperty("action_id", id);
        send(request);
        return await(message -> id.equals(string(message, "action_id"))
            && ("cancel_ack".equals(type(message)) || "rejected".equals(type(message))), 100, "cancel got no reply");
    }

    JsonObject terminal(String id, int timeoutTicks, Runnable sample) {
        return await(message -> id.equals(string(message, "action_id"))
            && ("terminal".equals(type(message)) || "rejected".equals(type(message))), timeoutTicks, id + " never ended", sample);
    }

    /** The terminal of an action if it has arrived, without waiting. */
    JsonObject terminalIfAny(String id) {
        for (JsonObject message; (message = incoming.poll()) != null; ) held.add(message);
        for (int i = 0; i < held.size(); i++) {
            JsonObject message = held.get(i);
            if (id.equals(string(message, "action_id"))
                && ("terminal".equals(type(message)) || "rejected".equals(type(message)))) return held.remove(i);
        }
        // Progress frames are not awaited by anyone here; keep the list short.
        held.removeIf(message -> id.equals(string(message, "action_id")) && "progress".equals(type(message)));
        return null;
    }

    JsonObject awaitCommand(String id, int timeoutTicks) {
        return terminal(id, timeoutTicks + 40, () -> {});
    }

    JsonArray worldEdits(String id) {
        for (JsonObject message; (message = incoming.poll()) != null; ) held.add(message);
        JsonArray edits = new JsonArray();
        for (JsonObject message : held) {
            if (!id.equals(string(message, "action_id")) || !message.has("world_edits")) continue;
            for (var edit : message.getAsJsonArray("world_edits")) edits.add(edit.deepCopy());
        }
        return edits;
    }

    /** The next lifecycle event with this name (death, respawned, movement_ended...). */
    JsonObject bodyEvent(String name, int ticks, Runnable sample) {
        return await(message -> "body_event".equals(type(message)) && name.equals(string(message, "name")),
            ticks, "no " + name + " event", sample);
    }

    private JsonObject await(Predicate<JsonObject> wanted, int ticks, String failure) {
        return await(wanted, ticks, failure, () -> {});
    }

    private JsonObject await(Predicate<JsonObject> wanted, int ticks, String failure, Runnable sample) {
        for (int tick = 0; tick <= ticks; tick++) {
            sample.run();
            for (JsonObject message; (message = incoming.poll()) != null; ) held.add(message);
            for (int i = 0; i < held.size(); i++) {
                if (wanted.test(held.get(i))) return held.remove(i);
            }
            context.waitTick();
        }
        throw new AssertionError(failure);
    }

    private void send(JsonObject request) {
        byte[] bytes = request.toString().getBytes(StandardCharsets.UTF_8);
        try {
            synchronized (output) {
                output.writeInt(bytes.length);
                output.write(bytes);
                output.flush();
            }
        } catch (IOException error) {
            throw new AssertionError("Body IPC send failed: " + error);
        }
    }

    private static String type(JsonObject message) {
        return string(message, "type");
    }

    private static String string(JsonObject message, String key) {
        return message.has(key) && message.get(key).isJsonPrimitive() ? message.get(key).getAsString() : "";
    }

    @Override
    public void close() {
        try {
            socket.close();
        } catch (IOException ignored) {
            // closing
        }
    }
}
