package dev.minebot.bridge;

import dev.minebot.bridge.transport.BridgeWebSocketServer;
import dev.minebot.bridge.version.Mojmap2612WorldAccess;
import dev.minebot.worldstream.WorldStreamChannel;
import net.fabricmc.api.DedicatedServerModInitializer;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerLifecycleEvents;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerTickEvents;
import net.minecraft.server.MinecraftServer;

import java.net.InetSocketAddress;
import java.util.concurrent.atomic.AtomicInteger;

public final class MineBotBridgeMod implements DedicatedServerModInitializer {
    public static final String MOD_ID = "minebot-bridge";
    private static final String HOST = "127.0.0.1";
    private static final int PORT = 8765;

    private static BridgeWebSocketServer bridgeServer;
    private static final AtomicInteger tickCounter = new AtomicInteger();

    @Override
    public void onInitializeServer() {
        ServerLifecycleEvents.SERVER_STARTED.register(this::startBridge);
        ServerLifecycleEvents.SERVER_STOPPING.register(server -> stopBridge());
        ServerTickEvents.END_SERVER_TICK.register(server -> {
            int tick = tickCounter.incrementAndGet();
            BridgeWebSocketServer current = bridgeServer;
            if (current != null) {
                current.tick(tick);
            }
        });
    }

    private void startBridge(MinecraftServer server) {
        if (bridgeServer != null) {
            return;
        }
        WorldStreamChannel worldStream = new WorldStreamChannel(server, new Mojmap2612WorldAccess(), tickCounter);
        bridgeServer = new BridgeWebSocketServer(new InetSocketAddress(HOST, PORT), server, worldStream);
        bridgeServer.start();
        log("bridge websocket listening on " + HOST + ":" + PORT);
    }

    private static void stopBridge() {
        BridgeWebSocketServer current = bridgeServer;
        bridgeServer = null;
        if (current != null) {
            try {
                current.stop(1000);
            } catch (InterruptedException ex) {
                Thread.currentThread().interrupt();
            } catch (Exception ex) {
                log("bridge stop failed: " + ex.getClass().getSimpleName());
            }
        }
    }

    private static void log(String message) {
        System.out.println("[MineBotBridge] " + message);
    }
}
