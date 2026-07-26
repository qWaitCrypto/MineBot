package dev.minebot.body;

import dev.minebot.body.protocol.FakePlayerBodyChannel;
import dev.minebot.body.search.LoadedBlockIndex;
import dev.minebot.server.common.transport.MineBotChannelRouter;
import dev.minebot.server.common.transport.MineBotWebSocketServer;
import net.fabricmc.api.DedicatedServerModInitializer;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerChunkEvents;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerLifecycleEvents;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerTickEvents;
import net.minecraft.server.MinecraftServer;

import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.UnknownHostException;
import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;

public final class MineBotBodyMod implements DedicatedServerModInitializer {
    public static final String MOD_ID = "minebot-body";
    private static final String HOST = System.getProperty("minebot.body.host", "127.0.0.1");
    private static final int PORT = Integer.getInteger("minebot.body.port", 8766);

    private static final AtomicInteger TICK_COUNTER = new AtomicInteger();
    private static MineBotWebSocketServer bodyServer;
    private static final LoadedBlockIndex BLOCK_INDEX = new LoadedBlockIndex();

    @Override
    public void onInitializeServer() {
        requireLoopback(HOST);
        ServerLifecycleEvents.SERVER_STARTED.register(this::startBody);
        ServerLifecycleEvents.SERVER_STOPPING.register(server -> stopBody());
        ServerChunkEvents.CHUNK_LOAD.register((level, chunk, generated) -> BLOCK_INDEX.enqueue(level, chunk));
        ServerChunkEvents.CHUNK_UNLOAD.register((level, chunk) -> BLOCK_INDEX.remove(level, chunk.getPos().x(), chunk.getPos().z()));
        ServerTickEvents.END_SERVER_TICK.register(server -> {
            int tick = TICK_COUNTER.incrementAndGet();
            BLOCK_INDEX.tick();
            MineBotWebSocketServer current = bodyServer;
            if (current != null) {
                current.tick(tick);
            }
        });
    }

    private void startBody(MinecraftServer server) {
        if (bodyServer != null) {
            return;
        }
        FakePlayerBodyChannel channel = new FakePlayerBodyChannel(server, BLOCK_INDEX);
        MineBotChannelRouter router = new MineBotChannelRouter(List.of(channel));
        bodyServer = new MineBotWebSocketServer(new InetSocketAddress(HOST, PORT), server::execute, router);
        bodyServer.start();
        log("websocket listening on " + HOST + ":" + PORT);
    }

    private static void stopBody() {
        MineBotWebSocketServer current = bodyServer;
        bodyServer = null;
        if (current == null) {
            return;
        }
        try {
            current.stop(1000);
        } catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
        } catch (RuntimeException error) {
            log("websocket stop failed: " + error.getClass().getSimpleName());
        }
    }

    private static void requireLoopback(String host) {
        try {
            if (!InetAddress.getByName(host).isLoopbackAddress()) {
                throw new IllegalArgumentException("minebot body must bind a loopback address");
            }
        } catch (UnknownHostException error) {
            throw new IllegalArgumentException("invalid minebot body host", error);
        }
    }

    private static void log(String message) {
        System.out.println("[MineBotBody] " + message);
    }
}
