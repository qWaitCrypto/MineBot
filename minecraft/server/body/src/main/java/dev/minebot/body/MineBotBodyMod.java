package dev.minebot.body;

import dev.minebot.body.protocol.FakePlayerBodyChannel;
import dev.minebot.server.common.transport.MineBotChannelRouter;
import dev.minebot.server.common.transport.MineBotWebSocketServer;
import net.fabricmc.api.DedicatedServerModInitializer;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerLifecycleEvents;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerTickEvents;
import net.fabricmc.fabric.api.entity.event.v1.ServerLivingEntityEvents;
import net.fabricmc.fabric.api.entity.event.v1.ServerPlayerEvents;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.level.ServerPlayer;

import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.UnknownHostException;
import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;

public final class MineBotBodyMod implements DedicatedServerModInitializer {
    public static final String MOD_ID = "minebot-body";
    // Default distinct from the observer Bridge (8766) so every launch path —
    // including tools/reset-world.sh, which passes only camera JVM args —
    // binds both servers without collision.
    private static final String HOST = System.getProperty("minebot.body.host", "127.0.0.1");
    private static final int PORT = Integer.getInteger("minebot.body.port", 8767);

    private static final AtomicInteger TICK_COUNTER = new AtomicInteger();
    private static MineBotWebSocketServer bodyServer;
    private static FakePlayerBodyChannel bodyChannel;

    @Override
    public void onInitializeServer() {
        requireLoopback(HOST);
        ServerLifecycleEvents.SERVER_STARTED.register(this::startBody);
        ServerLifecycleEvents.SERVER_STOPPING.register(server -> stopBody());
        ServerLivingEntityEvents.AFTER_DEATH.register((entity, source) -> {
            FakePlayerBodyChannel channel = bodyChannel;
            if (channel != null && entity instanceof ServerPlayer player) {
                channel.playerDied(player, TICK_COUNTER.get());
            }
        });
        ServerLivingEntityEvents.AFTER_DAMAGE.register((entity, source, baseDamage, damageTaken, blocked) -> {
            FakePlayerBodyChannel channel = bodyChannel;
            if (channel != null && entity instanceof ServerPlayer player) {
                channel.playerDamaged(
                    player,
                    damageTaken,
                    source.type().msgId(),
                    blocked,
                    TICK_COUNTER.get()
                );
            }
        });
        ServerPlayerEvents.JOIN.register(player -> {
            FakePlayerBodyChannel channel = bodyChannel;
            if (channel != null) {
                channel.playerJoined(player, TICK_COUNTER.get());
            }
        });
        ServerPlayerEvents.AFTER_RESPAWN.register((oldPlayer, newPlayer, alive) -> {
            FakePlayerBodyChannel channel = bodyChannel;
            if (channel != null) {
                channel.playerJoined(newPlayer, TICK_COUNTER.get());
            }
        });
        ServerPlayerEvents.LEAVE.register(player -> {
            FakePlayerBodyChannel channel = bodyChannel;
            if (channel != null) {
                channel.playerLeft(player, TICK_COUNTER.get());
            }
        });
        ServerTickEvents.END_SERVER_TICK.register(server -> {
            int tick = TICK_COUNTER.incrementAndGet();
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
        FakePlayerBodyChannel channel = new FakePlayerBodyChannel(server);
        bodyChannel = channel;
        MineBotChannelRouter router = new MineBotChannelRouter(List.of(channel));
        bodyServer = new MineBotWebSocketServer(new InetSocketAddress(HOST, PORT), server::execute, router);
        bodyServer.start();
        log("websocket listening on " + HOST + ":" + PORT);
    }

    private static void stopBody() {
        MineBotWebSocketServer current = bodyServer;
        bodyServer = null;
        bodyChannel = null;
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
