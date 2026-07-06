package dev.minebot.body;

import com.mojang.authlib.GameProfile;
import net.fabricmc.api.DedicatedServerModInitializer;
import net.fabricmc.fabric.api.entity.FakePlayer;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerLifecycleEvents;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerTickEvents;
import net.minecraft.core.BlockPos;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.world.entity.EntityType;
import net.minecraft.world.entity.Mob;
import net.minecraft.world.entity.monster.Zombie;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.item.Items;
import net.minecraft.world.level.ChunkPos;
import net.minecraft.world.level.GameType;
import net.minecraft.world.level.block.Blocks;
import net.minecraft.world.level.block.PressurePlateBlock;
import net.minecraft.world.level.block.state.BlockState;
import net.minecraft.world.phys.AABB;
import org.java_websocket.WebSocket;
import org.java_websocket.handshake.ClientHandshake;
import org.java_websocket.server.WebSocketServer;

import java.net.InetSocketAddress;
import java.util.Locale;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

public final class MineBotBodyMod implements DedicatedServerModInitializer {
    public static final String MOD_ID = "minebot-body";

    private static final UUID BOT_UUID = UUID.fromString("00000000-0000-0000-0000-00000000b0d1");
    private static final GameProfile BOT_PROFILE = new GameProfile(BOT_UUID, "MineBotBody");

    private static MinecraftServer server;
    private static BodyWsServer wsServer;
    private static ServerPlayer bot;
    private static int forwardTicks;
    private static boolean jumpNextTick;
    private static final AtomicInteger tickCounter = new AtomicInteger();

    @Override
    public void onInitializeServer() {
        ServerLifecycleEvents.SERVER_STARTED.register(started -> {
            server = started;
            ensureBot();
            startWebSocket();
            log("started");
        });
        ServerLifecycleEvents.SERVER_STOPPING.register(stopping -> {
            if (wsServer != null) {
                try {
                    wsServer.stop(1000);
                } catch (Exception ignored) {
                }
            }
        });
        ServerTickEvents.END_SERVER_TICK.register(current -> {
            tickCounter.incrementAndGet();
            tickBody();
        });
    }

    private static void startWebSocket() {
        if (wsServer != null) return;
        wsServer = new BodyWsServer(new InetSocketAddress("127.0.0.1", 8765));
        wsServer.start();
        log("websocket listening on 127.0.0.1:8765");
    }

    private static void tickBody() {
        ServerPlayer player = bot;
        if (player == null || player.isRemoved()) return;

        if (jumpNextTick) {
            player.jumpFromGround();
            jumpNextTick = false;
        }

        if (forwardTicks > 0) {
            float yaw = player.getYRot();
            double radians = Math.toRadians(yaw);
            double speed = 0.215D;
            double dx = -Math.sin(radians) * speed;
            double dz = Math.cos(radians) * speed;
            player.setDeltaMovement(dx, player.getDeltaMovement().y, dz);
            player.hurtMarked = true;
            forwardTicks--;
        }
    }

    private static ServerPlayer ensureBot() {
        if (server == null) return null;
        if (bot != null && !bot.isRemoved()) return bot;

        ServerLevel level = server.overworld();
        bot = FakePlayer.get(level, BOT_PROFILE);
        bot.setGameMode(GameType.SURVIVAL);
        bot.teleportTo(level, 0.5D, 80.0D, 0.5D, 0.0F, 0.0F);
        bot.getInventory().clearContent();
        bot.getInventory().add(new ItemStack(Items.DIAMOND_PICKAXE));
        level.getChunkSource().addRegionTicket(net.minecraft.server.level.TicketType.PLAYER, new ChunkPos(bot.blockPosition()), 3, bot.blockPosition());
        log("fake player ready at " + posJson(bot));
        return bot;
    }

    private static String runProbe() {
        ServerPlayer player = ensureBot();
        if (player == null) return "{\"success\":false,\"reason\":\"no server\"}";

        ServerLevel level = player.serverLevel();
        player.teleportTo(level, 0.5D, 80.0D, 0.5D, 0.0F, 0.0F);

        BlockPos feet = player.blockPosition();
        BlockPos platePos = new BlockPos(1, 79, 0);
        level.setBlock(platePos, Blocks.STONE_PRESSURE_PLATE.defaultBlockState(), 3);
        player.teleportTo(level, 1.5D, 80.0D, 0.5D, 0.0F, 0.0F);
        BlockState plateState = level.getBlockState(platePos);
        boolean pressurePlateAvailable = plateState.getBlock() instanceof PressurePlateBlock;

        Zombie zombie = new Zombie(EntityType.ZOMBIE, level);
        zombie.moveTo(3.5D, 80.0D, 0.5D, 270.0F, 0.0F);
        level.addFreshEntity(zombie);
        zombie.setTarget(player);
        boolean mobCanTarget = zombie.getTarget() == player;
        zombie.discard();

        float healthBefore = player.getHealth();
        player.hurt(player.damageSources().generic(), 2.0F);
        boolean damageWorks = player.getHealth() < healthBefore;
        player.setHealth(player.getMaxHealth());

        AABB box = player.getBoundingBox();
        boolean hasCollisionBox = box.getXsize() > 0.5D && box.getYsize() > 1.0D;
        int blockCount = 0;
        BlockPos center = player.blockPosition();
        for (BlockPos pos : BlockPos.betweenClosed(center.offset(-16, -2, -16), center.offset(16, 2, 16))) {
            level.getBlockState(pos);
            blockCount++;
        }
        int nearbyEntities = level.getEntitiesOfClass(Mob.class, player.getBoundingBox().inflate(16.0D)).size();
        int inventorySize = player.getInventory().items.size();
        boolean recipeManagerReadable = server.getRecipeManager() != null;
        float cooldown = player.getAttackStrengthScale(0.0F);

        return "{"
            + "\"success\":true,"
            + "\"fakePlayerClass\":\"" + player.getClass().getName() + "\","
            + "\"fabricFakePlayerAvailable\":true,"
            + "\"visibleToServerList\":" + server.getPlayerList().getPlayers().contains(player) + ","
            + "\"hasCollisionBox\":" + hasCollisionBox + ","
            + "\"mobCanTarget\":" + mobCanTarget + ","
            + "\"pressurePlateBlockAvailable\":" + pressurePlateAvailable + ","
            + "\"damageWorks\":" + damageWorks + ","
            + "\"respawnPath\":\"not exercised; Fabric API FakePlayer remains alive after damage reset\","
            + "\"chunkTicket\":\"registered PLAYER region ticket around bot\","
            + "\"blockStatesRead\":" + blockCount + ","
            + "\"nearbyMobCount\":" + nearbyEntities + ","
            + "\"inventorySlots\":" + inventorySize + ","
            + "\"recipeManagerReadable\":" + recipeManagerReadable + ","
            + "\"attackCooldown\":" + fmt(cooldown)
            + "}";
    }

    private static String handleMessage(String message) {
        if (message.contains("\"type\":\"ping\"") || message.contains("\"type\": \"ping\"")) {
            return "{\"type\":\"pong\",\"tick\":" + tickCounter.get() + "}";
        }
        if (message.contains("\"type\":\"getState\"") || message.contains("\"type\": \"getState\"")) {
            return stateJson();
        }
        if (message.contains("\"name\":\"lookAt\"") || message.contains("\"name\": \"lookAt\"")) {
            double x = numberParam(message, "x", 0.0D);
            double y = numberParam(message, "y", 80.0D);
            double z = numberParam(message, "z", 0.0D);
            ServerPlayer player = ensureBot();
            if (player != null) lookAt(player, x, y, z);
            return "{\"type\":\"result\",\"success\":true,\"thread\":\"server\"}";
        }
        if (message.contains("\"name\":\"moveForwardTicks\"") || message.contains("\"name\": \"moveForwardTicks\"")) {
            forwardTicks = (int) numberParam(message, "ticks", 20.0D);
            return "{\"type\":\"result\",\"success\":true,\"ticks\":" + forwardTicks + "}";
        }
        if (message.contains("\"name\":\"jump\"") || message.contains("\"name\": \"jump\"")) {
            jumpNextTick = true;
            return "{\"type\":\"result\",\"success\":true}";
        }
        if (message.contains("\"name\":\"probe\"") || message.contains("\"name\": \"probe\"")) {
            return "{\"type\":\"probe\",\"data\":" + runProbe() + "}";
        }
        return "{\"type\":\"error\",\"message\":\"unknown request\"}";
    }

    private static String stateJson() {
        ServerPlayer player = ensureBot();
        if (player == null) {
            return "{\"type\":\"state\",\"available\":false}";
        }
        return "{"
            + "\"type\":\"state\","
            + "\"available\":true,"
            + "\"tick\":" + tickCounter.get() + ","
            + "\"pos\":" + posJson(player) + ","
            + "\"yaw\":" + fmt(player.getYRot()) + ","
            + "\"pitch\":" + fmt(player.getXRot()) + ","
            + "\"health\":" + fmt(player.getHealth()) + ","
            + "\"food\":" + player.getFoodData().getFoodLevel()
            + "}";
    }

    private static void lookAt(ServerPlayer player, double x, double y, double z) {
        double dx = x - player.getX();
        double dy = y - player.getEyeY();
        double dz = z - player.getZ();
        double horizontal = Math.sqrt(dx * dx + dz * dz);
        float yaw = (float) (Math.toDegrees(Math.atan2(-dx, dz)));
        float pitch = (float) (-Math.toDegrees(Math.atan2(dy, horizontal)));
        player.setYRot(yaw);
        player.setXRot(pitch);
        player.yHeadRot = yaw;
        player.yBodyRot = yaw;
        player.hurtMarked = true;
    }

    private static double numberParam(String json, String name, double fallback) {
        String needle = "\"" + name + "\"";
        int idx = json.indexOf(needle);
        if (idx < 0) return fallback;
        int colon = json.indexOf(':', idx + needle.length());
        if (colon < 0) return fallback;
        int start = colon + 1;
        while (start < json.length() && Character.isWhitespace(json.charAt(start))) start++;
        int end = start;
        while (end < json.length()) {
            char c = json.charAt(end);
            if ((c >= '0' && c <= '9') || c == '-' || c == '+' || c == '.' || c == 'e' || c == 'E') end++;
            else break;
        }
        try {
            return Double.parseDouble(json.substring(start, end));
        } catch (Exception ignored) {
            return fallback;
        }
    }

    private static String posJson(ServerPlayer player) {
        return "{"
            + "\"x\":" + fmt(player.getX()) + ","
            + "\"y\":" + fmt(player.getY()) + ","
            + "\"z\":" + fmt(player.getZ())
            + "}";
    }

    private static String fmt(double value) {
        return String.format(Locale.ROOT, "%.4f", value);
    }

    private static void log(String message) {
        System.out.println("[MineBotBody] " + message);
    }

    private static final class BodyWsServer extends WebSocketServer {
        private BodyWsServer(InetSocketAddress address) {
            super(address);
        }

        @Override
        public void onOpen(WebSocket conn, ClientHandshake handshake) {
            conn.send("{\"type\":\"hello\",\"mod\":\"minebot-body\"}");
        }

        @Override
        public void onClose(WebSocket conn, int code, String reason, boolean remote) {
        }

        @Override
        public void onMessage(WebSocket conn, String message) {
            MinecraftServer current = server;
            if (current == null) {
                conn.send("{\"type\":\"error\",\"message\":\"server not ready\"}");
                return;
            }
            CompletableFuture<String> future = new CompletableFuture<>();
            current.execute(() -> {
                try {
                    future.complete(handleMessage(message));
                } catch (Throwable t) {
                    future.complete("{\"type\":\"error\",\"message\":\"" + t.getClass().getSimpleName() + "\"}");
                    t.printStackTrace();
                }
            });
            try {
                conn.send(future.get(5, TimeUnit.SECONDS));
            } catch (Exception e) {
                conn.send("{\"type\":\"error\",\"message\":\"timeout\"}");
            }
        }

        @Override
        public void onError(WebSocket conn, Exception ex) {
            ex.printStackTrace();
        }

        @Override
        public void onStart() {
            setConnectionLostTimeout(20);
        }
    }
}
