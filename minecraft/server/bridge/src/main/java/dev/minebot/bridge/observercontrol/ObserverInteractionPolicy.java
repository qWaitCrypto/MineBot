package dev.minebot.bridge.observercontrol;

import com.google.gson.JsonObject;
import dev.minebot.bridge.version.Mojmap2612ObserverAccess;
import dev.minebot.bridge.version.ObserverAccessException;
import net.fabricmc.fabric.api.event.player.AttackBlockCallback;
import net.fabricmc.fabric.api.event.player.AttackEntityCallback;
import net.fabricmc.fabric.api.event.player.PlayerBlockBreakEvents;
import net.fabricmc.fabric.api.event.player.UseBlockCallback;
import net.fabricmc.fabric.api.event.player.UseEntityCallback;
import net.fabricmc.fabric.api.event.player.UseItemCallback;
import net.fabricmc.fabric.api.message.v1.ServerMessageEvents;
import net.fabricmc.fabric.api.networking.v1.ServerPlayConnectionEvents;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.world.InteractionResult;
import net.minecraft.world.entity.player.Player;

import java.util.UUID;
import java.util.concurrent.atomic.AtomicReference;
import java.util.concurrent.atomic.LongAdder;

/** Server-authoritative denial policy for the dedicated Camera observer. */
public final class ObserverInteractionPolicy {
    private static final AtomicReference<UUID> OBSERVER_ID = new AtomicReference<>();
    private static final LongAdder ATTACK_DENIED = new LongAdder();
    private static final LongAdder USE_DENIED = new LongAdder();
    private static final LongAdder BREAK_DENIED = new LongAdder();
    private static final LongAdder CHAT_DENIED = new LongAdder();
    private static final LongAdder COMMAND_DENIED = new LongAdder();
    private static final LongAdder INVENTORY_DENIED = new LongAdder();
    private static final LongAdder MOVEMENT_DENIED = new LongAdder();
    private static boolean registered;

    private ObserverInteractionPolicy() {
    }

    public static synchronized void register(UUID observerId) {
        UUID previous = OBSERVER_ID.getAndSet(observerId);
        if (registered) {
            if (previous != null && !previous.equals(observerId)) {
                throw new IllegalStateException("Camera observer UUID cannot change during one server process");
            }
            return;
        }
        registered = true;

        ServerPlayConnectionEvents.JOIN.register((handler, sender, server) -> {
            ServerPlayer player = handler.getPlayer();
            if (isObserver(player)) {
                try {
                    Mojmap2612ObserverAccess.enforceObserverPolicy(server, player);
                } catch (ObserverAccessException ignored) {
                    // The access layer already disconnected the observer on a non-retryable policy failure.
                }
            }
        });
        AttackBlockCallback.EVENT.register((player, level, hand, pos, direction) -> deny(player, ATTACK_DENIED));
        AttackEntityCallback.EVENT.register((player, level, hand, entity, hit) -> deny(player, ATTACK_DENIED));
        UseBlockCallback.EVENT.register((player, level, hand, hit) -> deny(player, USE_DENIED));
        UseEntityCallback.EVENT.register((player, level, hand, entity, hit) -> deny(player, USE_DENIED));
        UseItemCallback.EVENT.register((player, level, hand) -> deny(player, USE_DENIED));
        PlayerBlockBreakEvents.BEFORE.register(
            (level, player, pos, state, blockEntity) -> allow(player, BREAK_DENIED)
        );
        ServerMessageEvents.ALLOW_CHAT_MESSAGE.register(
            (message, sender, params) -> allow(sender, CHAT_DENIED)
        );
        ServerMessageEvents.ALLOW_COMMAND_MESSAGE.register(
            (message, source, params) -> allow(source.getPlayer(), COMMAND_DENIED)
        );
    }

    public static JsonObject denialCountsJson() {
        JsonObject counts = new JsonObject();
        counts.addProperty("attack", ATTACK_DENIED.sum());
        counts.addProperty("use", USE_DENIED.sum());
        counts.addProperty("break", BREAK_DENIED.sum());
        counts.addProperty("chat", CHAT_DENIED.sum());
        counts.addProperty("command", COMMAND_DENIED.sum());
        counts.addProperty("inventory", INVENTORY_DENIED.sum());
        counts.addProperty("movement", MOVEMENT_DENIED.sum());
        return counts;
    }

    public static boolean rejectBreak(Player player) {
        return reject(player, BREAK_DENIED);
    }

    public static boolean rejectUse(Player player) {
        return reject(player, USE_DENIED);
    }

    public static boolean rejectChat(Player player) {
        return reject(player, CHAT_DENIED);
    }

    public static boolean rejectCommand(Player player) {
        return reject(player, COMMAND_DENIED);
    }

    public static boolean rejectInventory(Player player) {
        return reject(player, INVENTORY_DENIED);
    }

    public static boolean rejectMovement(Player player) {
        return reject(player, MOVEMENT_DENIED);
    }

    static boolean isObserver(Player player) {
        UUID configured = OBSERVER_ID.get();
        return player != null && configured != null && configured.equals(player.getUUID());
    }

    private static InteractionResult deny(Player player, LongAdder counter) {
        if (!isObserver(player)) {
            return InteractionResult.PASS;
        }
        counter.increment();
        return InteractionResult.FAIL;
    }

    private static boolean allow(Player player, LongAdder counter) {
        return !reject(player, counter);
    }

    private static boolean reject(Player player, LongAdder counter) {
        boolean observer = isObserver(player);
        if (observer) {
            counter.increment();
        }
        return observer;
    }
}
