package dev.minebot.bridge;

import dev.minebot.camera.protocol.CameraClientTelemetryPayload;
import dev.minebot.camera.protocol.CameraDirectivePayload;
import dev.minebot.bridge.observercontrol.ObserverControlChannel;
import dev.minebot.bridge.observercontrol.ObserverInteractionPolicy;
import dev.minebot.bridge.transport.BridgeChannelRouter;
import dev.minebot.bridge.transport.BridgeWebSocketServer;
import dev.minebot.bridge.version.Mojmap2612ObserverAccess;
import net.fabricmc.api.DedicatedServerModInitializer;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerLifecycleEvents;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerTickEvents;
import net.fabricmc.fabric.api.networking.v1.PayloadTypeRegistry;
import net.fabricmc.fabric.api.networking.v1.ServerPlayNetworking;
import net.minecraft.server.MinecraftServer;

import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicInteger;

public final class MineBotBridgeMod implements DedicatedServerModInitializer {
    public static final String MOD_ID = "minebot-bridge";
    private static final String HOST = System.getProperty("minebot.bridge.host", "127.0.0.1");
    private static final int PORT = Integer.getInteger("minebot.bridge.port", 8765);

    private static final AtomicInteger TICK_COUNTER = new AtomicInteger();
    private static BridgeWebSocketServer bridgeServer;
    private static volatile ObserverControlChannel observerControlChannel;

    @Override
    public void onInitializeServer() {
        requireLoopback(HOST);
        PayloadTypeRegistry.clientboundPlay().register(CameraDirectivePayload.TYPE, CameraDirectivePayload.CODEC);
        PayloadTypeRegistry.serverboundPlay().register(
            CameraClientTelemetryPayload.TYPE,
            CameraClientTelemetryPayload.CODEC
        );
        ServerPlayNetworking.registerGlobalReceiver(CameraClientTelemetryPayload.TYPE, (payload, context) ->
            context.server().execute(() -> {
                ObserverControlChannel current = observerControlChannel;
                if (current != null) {
                    current.acceptClientTelemetry(context.player().getUUID(), payload.telemetry(), TICK_COUNTER.get());
                }
            })
        );
        ServerLifecycleEvents.SERVER_STARTED.register(this::startBridge);
        ServerLifecycleEvents.SERVER_STOPPING.register(server -> stopBridge());
        ServerTickEvents.END_SERVER_TICK.register(server -> {
            int tick = TICK_COUNTER.incrementAndGet();
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
        BridgeChannelRouter router = new BridgeChannelRouter(observerChannels(server));
        bridgeServer = new BridgeWebSocketServer(new InetSocketAddress(HOST, PORT), server, router);
        bridgeServer.start();
        log("bridge websocket listening on " + HOST + ":" + PORT);
    }

    private static List<dev.minebot.bridge.transport.BridgeChannel> observerChannels(MinecraftServer server) {
        String configured = System.getProperty("minebot.camera.observerUuid", "").trim();
        if (configured.isEmpty()) {
            observerControlChannel = null;
            log("observer-control disabled: minebot.camera.observerUuid is not configured");
            return List.of();
        }
        UUID observerId;
        try {
            observerId = UUID.fromString(configured);
        } catch (IllegalArgumentException error) {
            throw new IllegalArgumentException("minebot.camera.observerUuid must be a UUID");
        }
        int leaseTtlTicks = Integer.getInteger("minebot.camera.leaseTtlTicks", 200);
        ObserverInteractionPolicy.register(observerId);
        ObserverControlChannel channel = new ObserverControlChannel(
            server,
            new Mojmap2612ObserverAccess(),
            observerId,
            leaseTtlTicks
        );
        observerControlChannel = channel;
        return List.of(channel);
    }

    private static void stopBridge() {
        BridgeWebSocketServer current = bridgeServer;
        bridgeServer = null;
        observerControlChannel = null;
        if (current == null) {
            return;
        }
        try {
            current.stop(1000);
        } catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
        } catch (RuntimeException error) {
            log("bridge stop failed: " + error.getClass().getSimpleName());
        }
    }

    private static void requireLoopback(String host) {
        try {
            if (!InetAddress.getByName(host).isLoopbackAddress()) {
                throw new IllegalArgumentException("minebot bridge must bind a loopback address");
            }
        } catch (java.net.UnknownHostException error) {
            throw new IllegalArgumentException("invalid minebot bridge host", error);
        }
    }

    private static void log(String message) {
        System.out.println("[MineBotBridge] " + message);
    }
}
