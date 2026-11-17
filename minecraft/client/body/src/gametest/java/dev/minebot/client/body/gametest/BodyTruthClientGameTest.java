package dev.minebot.client.body.gametest;

import baritone.api.BaritoneAPI;
import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import net.fabricmc.fabric.api.client.gametest.v1.FabricClientGameTest;
import net.fabricmc.fabric.api.client.gametest.v1.context.ClientGameTestContext;
import net.fabricmc.fabric.api.client.gametest.v1.context.TestServerContext;
import net.fabricmc.fabric.api.client.gametest.v1.context.TestSingleplayerContext;
import net.minecraft.core.BlockPos;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.entity.LivingEntity;
import net.minecraft.world.entity.player.Player;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.phys.Vec3;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;

/** Six investigations of the 2026-10-19 Body gaps. No Agent or Decision participates.
 * Evidence is recorded before each truth assertion. The three repaired gaps
 * (B19, B20, B22) are hard assertions; the remaining measurements retain the
 * historical observed-failure reporting behavior. Setup, transport,
 * instrumentation and cleanup failures also fail the GameTest.
 */
public class BodyTruthClientGameTest implements FabricClientGameTest {
    private static final int Y = 120;
    private static final List<JsonObject> SWINGS = new ArrayList<>();
    private static final List<JsonObject> SERVER_HITS = java.util.Collections.synchronizedList(new ArrayList<>());
    private static volatile boolean recordingSwings;

    @Override
    public void runTest(ClientGameTestContext context) {
        try (TestSingleplayerContext world = context.worldBuilder().create()) {
            TestServerContext server = world.getServer();
            server.runCommand("forceload add -48 -16 48 16");
            world.getClientLevel().waitForChunksDownload();
            server.runOnServer(s -> {
                var rules = s.overworld().getGameRules();
                rules.set(net.minecraft.world.level.gamerules.GameRules.SPAWN_MOBS, false, s);
                rules.set(net.minecraft.world.level.gamerules.GameRules.ADVANCE_TIME, false, s);
            });
            try (BodyIpc body = BodyIpc.connect(context)) {
                sprintDuringTwentyBlockEscape(context, server, body);
                cancelThenMoveInTheOppositeDirection(context, server, body);
                moveOutOfAnOwnedClosedHut(context, server, body);
                surfaceArrivalRequiresDryGround(context, server, body);
                firstMeleeSwingHitsWithinReach(context, server, body);
                fullInventoryCraftReportsInventoryTruth(context, server, body);
            }
        }
    }

    private static void sprintDuringTwentyBlockEscape(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        for (int food : new int[]{20, 6}) {
            reset(context, server, body);
            // A flat, straight corridor prevents a run-away goal choosing another direction.
            server.runCommand(fill(-3, Y, -1, 25, Y + 2, -1, "bedrock"));
            server.runCommand(fill(-3, Y, 1, 25, Y + 2, 1, "bedrock"));
            server.runCommand(fill(-3, Y + 2, -1, 25, Y + 2, 1, "bedrock"));
            server.runCommand(fill(-3, Y, 0, -3, Y + 1, 0, "bedrock"));
            server.runOnServer(s -> {
                var data = s.getPlayerList().getPlayers().getFirst().getFoodData();
                data.setFoodLevel(food);
                data.setSaturation(0);
            });
            context.waitFor(client -> client.player.getFoodData().getFoodLevel() == food, 200);
            boolean allowSprint = context.computeOnClient(client -> BaritoneAPI.getSettings().allowSprint.value);
            List<Pose> samples = new ArrayList<>();
            samples.add(pose(context));
            String id = body.startObjective("away:-1:" + Y + ":0:22", 600);
            JsonObject terminal = body.terminal(id, 640, () -> samples.add(pose(context)));
            Pose start = samples.getFirst();
            Pose end = samples.stream().filter(p -> p.x() >= start.x() + 20).findFirst().orElse(null);
            Pose moving = samples.stream().filter(p -> p.x() > start.x() + 0.001).findFirst().orElse(null);
            JsonObject fact = evidence("B5", "sprintDuringTwentyBlockEscape", terminal);
            fact.addProperty("food", food);
            fact.addProperty("allowSprint", allowSprint);
            fact.addProperty("sprinting_ticks", samples.stream().filter(Pose::sprinting).count());
            fact.addProperty("sample_ticks", samples.size());
            if (end != null && moving != null) {
                // Interpolate the crossing: both runs measure exactly 20 horizontal blocks.
                int at = samples.indexOf(end);
                Pose before = samples.get(at - 1);
                double crossing = before.tick() + (start.x() + 20 - before.x()) / (end.x() - before.x());
                fact.addProperty("twenty_blocks_ticks", crossing - start.tick());
                fact.addProperty("speed_including_path_start_mps", 400.0 / (crossing - start.tick()));
                fact.addProperty("moving_speed_mps", (start.x() + 20 - moving.x()) * 20 / (crossing - moving.tick()));
            }
            fact.addProperty("twenty_blocks_observed", end != null);
            record(fact); // B5 is measurement only: deliberately no speed/sprint assertion.
        }
    }

    private static void cancelThenMoveInTheOppositeDirection(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        reset(context, server, body);
        String first = body.startObjective("near:30:" + Y + ":0:0", 600);
        context.waitFor(client -> client.player.getX() >= 14.5, 400);
        Pose halfway = pose(context);
        JsonObject ack = body.cancel(first);
        JsonObject canceled = body.terminal(first, 100, () -> {});
        check("canceled".equals(string(canceled, "status")), "the normal cancel did not finish: " + canceled);
        Vec3 target = Vec3.atBottomCenterOf(new BlockPos(-15, Y, 0));
        List<Double> distances = new ArrayList<>();
        String second = body.startObjective("near:-15:" + Y + ":0:0", 600);
        JsonObject terminal = body.terminal(second, 640, () -> distances.add(context.computeOnClient(
            client -> client.player.position().distanceTo(target))));
        double nearest = distances.getFirst(), regression = 0;
        for (double distance : distances) {
            regression = Math.max(regression, distance - nearest);
            nearest = Math.min(nearest, distance);
        }
        JsonObject fact = evidence("B18", "cancelThenMoveInTheOppositeDirection", terminal);
        fact.addProperty("cancel_x", halfway.x());
        fact.addProperty("cancel_ack", string(ack, "type"));
        fact.add("first_terminal", summary(canceled));
        fact.addProperty("maximum_distance_regression", regression);
        JsonArray trace = new JsonArray();
        distances.forEach(trace::add);
        fact.add("distances_to_second_target", trace);
        record(fact);
        double measuredRegression = regression;
        truth("B18", () -> {
            check(measuredRegression <= 0.2, "distance to the opposite target increased by " + measuredRegression);
            check(distances.getLast() < distances.getFirst(), "the opposite target never became closer");
            check(completed(terminal), "the second move ended " + summary(terminal));
        });
    }

    private static void moveOutOfAnOwnedClosedHut(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        reset(context, server, body);
        List<BlockPos> hut = new ArrayList<>();
        for (int x = -2; x <= 2; x++) for (int z = -2; z <= 2; z++) for (int y = Y - 1; y <= Y + 2; y++) {
            if (y == Y - 1 || y == Y + 2 || Math.abs(x) == 2 || Math.abs(z) == 2) hut.add(new BlockPos(x, y, z));
        }
        for (BlockPos pos : hut) server.runCommand("setblock " + coordinates(pos) + " minecraft:oak_planks");
        server.runCommand("give @p minecraft:iron_axe");
        context.waitFor(client -> count(client.player, "iron_axe") == 1
            && client.level.getBlockState(new BlockPos(2, Y, 0)).is(net.minecraft.world.level.block.Blocks.OAK_PLANKS), 200);
        check(server.computeOnServer(s -> hut.stream().allMatch(p -> s.overworld().getBlockState(p)
            .is(net.minecraft.world.level.block.Blocks.OAK_PLANKS))), "hut fixture is not closed planks");
        // The production ledger is Python-side. Keep a real policy/ledger alive
        // while navigating, restored exactly as persisted place_block rows are.
        try (LedgerFixture ledger = LedgerFixture.load(hut)) {
            JsonArray protectedPositions = new JsonArray();
            hut.forEach(pos -> protectedPositions.add(xyz(pos)));
            JsonObject protectedSettings = new JsonObject();
            protectedSettings.add("protected_positions", protectedPositions);
            body.settings(protectedSettings);
            String id = body.startObjective("near:10:" + Y + ":0:0", 800);
            JsonObject terminal = body.terminal(id, 840, () -> {});
            JsonArray edits = body.worldEdits(id), broken = new JsonArray();
            for (var raw : edits) {
                JsonObject edit = raw.getAsJsonObject();
                JsonArray at = edit.getAsJsonArray("pos");
                BlockPos pos = new BlockPos(at.get(0).getAsInt(), at.get(1).getAsInt(), at.get(2).getAsInt());
                if ("break".equals(string(edit, "op")) && hut.contains(pos)) broken.add(edit);
            }
            JsonArray missing = server.computeOnServer(s -> {
                JsonArray result = new JsonArray();
                for (BlockPos pos : hut) if (!s.overworld().getBlockState(pos).is(net.minecraft.world.level.block.Blocks.OAK_PLANKS)) {
                    result.add(xyz(pos));
                }
                return result;
            });
            JsonObject fact = evidence("B19", "moveOutOfAnOwnedClosedHut", terminal);
            boolean completeReceipt = terminal.has("world_edit_count")
                && terminal.get("world_edit_count").getAsInt() == edits.size()
                && !terminal.get("world_edits_truncated").getAsBoolean()
                && terminal.get("world_edits_unconfirmed").getAsInt() == 0;
            fact.add("ledger", ledger.ready);
            fact.addProperty("allowBreak", context.computeOnClient(client -> BaritoneAPI.getSettings().allowBreak.value).booleanValue());
            fact.addProperty("hut_blocks", hut.size());
            fact.addProperty("complete_receipt", completeReceipt);
            fact.add("hut_break_receipts", broken);
            fact.add("hut_blocks_removed", missing);
            record(fact);
            hardTruth("B19", () -> {
                check(completeReceipt, "world edit receipts are incomplete; hut preservation is not established");
                check(!completed(terminal), "navigation reported success despite no legal route: " + summary(terminal));
                check(broken.isEmpty() && missing.isEmpty(),
                    "navigation broke own hut blocks " + broken + "; missing cells " + missing);
            });
        } finally {
            // The set outlives a rejoin, so later cases must not inherit the hut.
            JsonObject cleared = new JsonObject();
            cleared.add("protected_positions", new JsonArray());
            body.settings(cleared);
        }
    }

    private static void surfaceArrivalRequiresDryGround(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        reset(context, server, body);
        // Six blocks of water below a stone cover, no air at the starting
        // head. One open column leads to air beside the dry bank at y=120.
        server.runCommand(fill(-3, Y - 7, -3, 3, Y - 1, 3, "stone"));
        server.runCommand(fill(-2, Y - 6, -2, 2, Y - 1, 2, "water"));
        server.runCommand(fill(-2, Y, -2, 2, Y, 2, "stone"));
        server.runCommand("setblock 2 " + Y + " 0 minecraft:air");
        // Keep a failed ascent observable for its entire deadline. This changes
        // health capacity, not oxygen consumption or swimming physics.
        server.runCommand("attribute @p minecraft:max_health base set 100");
        server.runOnServer(s -> s.getPlayerList().getPlayers().getFirst().setHealth(100));
        server.runCommand("tp @p 0.5 " + (Y - 6) + " 0.5");
        context.waitFor(client -> client.player.isUnderWater() && client.player.getAirSupply() < 280, 400);
        int oxygenBefore = context.computeOnClient(client -> client.player.getAirSupply());
        // go_to_surface and the surface default both dispatch this typed action.
        JsonObject terminal = body.command("ascend", new JsonObject(), 800);
        JsonObject stand = server.computeOnServer(s -> {
            var player = s.getPlayerList().getPlayers().getFirst();
            BlockPos feet = player.blockPosition(), head = BlockPos.containing(player.getEyePosition());
            var level = s.overworld();
            JsonObject out = new JsonObject();
            out.add("feet", xyz(feet));
            out.addProperty("y", player.getY());
            out.addProperty("on_ground", player.onGround());
            out.addProperty("support", BuiltInRegistries.BLOCK.getKey(level.getBlockState(feet.below()).getBlock()).getPath());
            out.addProperty("feet_fluid", !level.getFluidState(feet).isEmpty());
            out.addProperty("head_air", level.getBlockState(head).isAir());
            out.addProperty("dry_stand", player.onGround() && level.getFluidState(feet).isEmpty()
                && level.getFluidState(feet.below()).isEmpty() && !level.getBlockState(feet.below()).getCollisionShape(level, feet.below()).isEmpty()
                && level.getBlockState(head).isAir());
            return out;
        });
        JsonObject fact = evidence("B20", "surfaceArrivalRequiresDryGround", terminal);
        fact.addProperty("oxygen_before", oxygenBefore);
        fact.add("server_stand", stand);
        record(fact);
        hardTruth("B20", () -> check(!completed(terminal) || stand.get("dry_stand").getAsBoolean(),
            "arrival reported without a dry stand: " + stand));
    }

    private static void firstMeleeSwingHitsWithinReach(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        reset(context, server, body);
        server.runCommand("give @p minecraft:iron_sword");
        context.waitFor(client -> count(client.player, "iron_sword") == 1, 200);
        body.primitive("selectItem", param("item", "iron_sword"));
        server.runCommand("summon minecraft:zombie 5.5 " + Y + " 0.5 {NoAI:1b,Silent:1b,PersistenceRequired:1b}");
        context.waitFor(client -> !client.level.getEntities(client.player, client.player.getBoundingBox().inflate(10),
            entity -> entity.getType() == net.minecraft.world.entity.EntityType.ZOMBIE).isEmpty(), 200);
        context.runOnClient(client -> { SWINGS.clear(); SERVER_HITS.clear(); recordingSwings = true; });
        JsonObject terminal;
        try {
            JsonObject params = param("target_type", "zombie");
            params.addProperty("attack_range", 2.0); // engage_entity's public-tool default, combat.py
            terminal = body.command("engageEntity", params, 600);
        } finally {
            context.runOnClient(client -> recordingSwings = false);
        }
        List<JsonObject> swings = context.computeOnClient(client -> List.copyOf(SWINGS));
        List<JsonObject> hits;
        synchronized (SERVER_HITS) { hits = List.copyOf(SERVER_HITS); }
        JsonObject fact = evidence("B21", "firstMeleeSwingHitsWithinReach", terminal);
        JsonArray trace = new JsonArray();
        swings.forEach(trace::add);
        fact.add("swings", trace);
        JsonArray damage = new JsonArray();
        hits.forEach(damage::add);
        fact.add("server_attacks", damage);
        int alive = server.computeOnServer(s -> s.overworld().getEntities(
            net.minecraft.world.entity.EntityType.ZOMBIE, entity -> entity.isAlive()).size());
        fact.addProperty("server_zombies_alive", alive);
        record(fact);
        truth("B21", () -> {
            check(!swings.isEmpty(), "no melee attack was issued");
            // Observe every received attack packet, even one rejected before
            // Player.attack: the server's first accepted attack might be a
            // later swing. Client health synchronization is not the verdict.
            check(!hits.isEmpty() && hits.getFirst().get("health_after").getAsDouble()
                < hits.getFirst().get("health_before").getAsDouble(), "the server's first attack did not reduce zombie health: " + damage);
            for (JsonObject swing : swings) check(swing.get("distance").getAsDouble() <= 3.0,
                "swing outside 3.0 blocks: " + swing);
            check(completed(terminal), "engage ended " + summary(terminal));
        });
    }

    /** Called by a GameTest-only mixin at the exact attack call, before it sends the packet. */
    public static void observeAttack(Player player, Entity target) {
        if (!recordingSwings || !(target instanceof LivingEntity living)) return;
        JsonObject sample = new JsonObject();
        sample.addProperty("tick", player.tickCount);
        sample.addProperty("distance", player.distanceTo(target));
        sample.addProperty("health_before", living.getHealth());
        SWINGS.add(sample);
    }

    public static boolean recordingServerAttack() { return recordingSwings; }

    public static void observeServerAttack(Entity target, float before) {
        if (!recordingSwings || !(target instanceof LivingEntity living)) return;
        JsonObject sample = new JsonObject();
        sample.addProperty("health_before", before);
        sample.addProperty("health_after", living.getHealth());
        SERVER_HITS.add(sample);
    }

    private static void fullInventoryCraftReportsInventoryTruth(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        reset(context, server, body);
        // All slots filled except the recipe's four planks and one output slot.
        for (int slot = 0; slot < 35; slot++) server.runCommand("item replace entity @p "
            + (slot < 9 ? "hotbar." + slot : "inventory." + (slot - 9)) + " with minecraft:stone 64");
        server.runCommand("item replace entity @p hotbar.0 with minecraft:oak_planks 4");
        server.runCommand("recipe give @p minecraft:crafting_table");
        context.waitFor(client -> count(client.player, "stone") == 34 * 64 && count(client.player, "oak_planks") == 4, 400);
        context.waitFor(client -> client.player.getRecipeBook().getCollections().stream()
            .flatMap(collection -> collection.getRecipes().stream()).anyMatch(entry -> entry.display().result()
                .resolveForFirstStack(net.minecraft.world.item.crafting.display.SlotDisplayContext.fromLevel(client.level))
                .is(net.minecraft.world.item.Items.CRAFTING_TABLE)), 400);
        JsonObject input = param("item", "oak_planks");
        input.addProperty("slot", 0); input.addProperty("count", 4);
        JsonArray inputs = new JsonArray(); inputs.add(input);
        JsonObject output = param("item", "crafting_table");
        output.addProperty("slot", 35); output.addProperty("count", 1);
        JsonObject params = new JsonObject(); params.add("inputs", inputs); params.add("output", output);
        JsonObject terminal = body.command("craftItem", params, 400);
        context.waitTicks(20);
        int serverTables = server.computeOnServer(s -> count(s.getPlayerList().getPlayers().getFirst(), "crafting_table"));
        int clientTables = context.computeOnClient(client -> count(client.player, "crafting_table"));
        JsonObject fact = evidence("B22", "fullInventoryCraftReportsInventoryTruth", terminal);
        fact.addProperty("occupied_slots_before", 35);
        fact.addProperty("server_tables_after", serverTables);
        fact.addProperty("client_tables_after", clientTables);
        fact.addProperty("success", completed(terminal)); // RealClientBody's success mapping
        record(fact);
        hardTruth("B22", () -> check(completed(terminal) == (serverTables > 0),
            "tool success=" + completed(terminal) + " but server table count=" + serverTables));
    }

    private static void reset(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        body.primitive("stop", new JsonObject());
        server.runCommand("difficulty easy");
        server.runCommand("time set midnight");
        server.runCommand("gamemode survival @p");
        server.runCommand("attribute @p minecraft:max_health base set 20");
        server.runCommand("kill @e[type=!player]");
        server.runCommand("clear @p");
        server.runCommand(fill(-40, Y - 8, -6, 40, Y + 6, 6, "air"));
        server.runCommand(fill(-40, Y - 2, -6, 40, Y - 1, 6, "stone"));
        server.runCommand("tp @p 0.5 " + Y + " 0.5");
        server.runOnServer(s -> {
            var player = s.getPlayerList().getPlayers().getFirst();
            player.setHealth(player.getMaxHealth());
            player.setAirSupply(player.getMaxAirSupply());
            player.getFoodData().setFoodLevel(20);
            player.getFoodData().setSaturation(5);
        });
        context.waitFor(client -> client.player.onGround() && Math.abs(client.player.getX() - 0.5) < 0.01
            && Math.abs(client.player.getY() - Y) < 0.01 && count(client.player, "stone") == 0, 400);
        context.waitTicks(5);
    }

    private record Pose(int tick, double x, double z, boolean sprinting) {}

    private static Pose pose(ClientGameTestContext context) {
        return context.computeOnClient(client -> new Pose(client.player.tickCount, client.player.getX(),
            client.player.getZ(), client.player.isSprinting()));
    }

    private static JsonObject evidence(String gap, String name, JsonObject terminal) {
        JsonObject fact = param("gap", gap);
        fact.addProperty("test", name);
        fact.add("tool", summary(terminal));
        return fact;
    }

    private static JsonObject summary(JsonObject terminal) {
        JsonObject out = new JsonObject();
        for (String key : List.of("type", "status", "reason", "position", "native_stopped", "attacks", "target_health",
            "inventory_before", "inventory_after", "inventory_delta", "world_edit_count",
            "world_edits_truncated", "world_edits_unconfirmed")) {
            if (terminal.has(key)) out.add(key, terminal.get(key));
        }
        return out;
    }

    private static void record(JsonObject fact) {
        System.out.println("BODY_TRUTH " + fact);
    }

    private static void truth(String gap, Runnable assertion) {
        try {
            assertion.run();
            System.out.println("BODY_TRUTH_ASSERT " + gap + " PASS");
        } catch (AssertionError observed) {
            // An investigation records the real defect and keeps the suite running.
            System.out.println("BODY_TRUTH_ASSERT " + gap + " OBSERVED_FAILURE " + observed.getMessage());
        }
    }

    private static void hardTruth(String gap, Runnable assertion) {
        assertion.run();
        System.out.println("BODY_TRUTH_ASSERT " + gap + " PASS");
    }

    private static boolean completed(JsonObject terminal) { return "completed".equals(string(terminal, "status")); }
    private static String string(JsonObject object, String key) { return object.has(key) ? object.get(key).getAsString() : ""; }
    private static void check(boolean condition, String message) { BodyClientGameTest.check(condition, message); }
    private static JsonObject param(String key, String value) { JsonObject out = new JsonObject(); out.addProperty(key, value); return out; }
    private static JsonArray xyz(BlockPos pos) { JsonArray out = new JsonArray(); out.add(pos.getX()); out.add(pos.getY()); out.add(pos.getZ()); return out; }
    private static String coordinates(BlockPos pos) { return pos.getX() + " " + pos.getY() + " " + pos.getZ(); }
    private static String fill(int x, int y, int z, int ex, int ey, int ez, String block) {
        return "fill " + x + " " + y + " " + z + " " + ex + " " + ey + " " + ez + " minecraft:" + block;
    }
    private static int count(Player player, String item) {
        int total = 0;
        for (int slot = 0; slot < 36; slot++) {
            ItemStack stack = player.getInventory().getItem(slot);
            if (BuiltInRegistries.ITEM.getKey(stack.getItem()).getPath().equals(item)) total += stack.getCount();
        }
        return total;
    }

    private static final class LedgerFixture implements AutoCloseable {
        private final Process process;
        private final BufferedReader reader;
        private final JsonObject ready;

        private LedgerFixture(Process process, BufferedReader reader, JsonObject ready) {
            this.process = process; this.reader = reader; this.ready = ready;
        }

        static LedgerFixture load(List<BlockPos> blocks) {
            try {
                Path root = Path.of("").toAbsolutePath();
                while (root != null && !Files.isDirectory(root.resolve("minebot"))) root = root.getParent();
                check(root != null, "cannot locate worktree for the production ownership ledger");
                String python = Path.of(System.getProperty("user.home"), "MineBot", ".venv", "bin", "python").toString();
                Path script = root.resolve("minecraft/client/body/src/gametest/resources/body_truth_ledger.py");
                ProcessBuilder builder = new ProcessBuilder(python, "-B", "-u", script.toString()).directory(root.toFile());
                builder.environment().put("PYTHONPATH", root.toString());
                builder.redirectError(ProcessBuilder.Redirect.INHERIT);
                Process process = builder.start();
                JsonArray rows = new JsonArray();
                for (BlockPos pos : blocks) rows.add(xyz(pos));
                process.getOutputStream().write((rows + "\n").getBytes(StandardCharsets.UTF_8));
                process.getOutputStream().flush();
                BufferedReader reader = new BufferedReader(new InputStreamReader(process.getInputStream(), StandardCharsets.UTF_8));
                String line = reader.readLine();
                check(line != null, "production ledger fixture exited before ready");
                JsonObject ready = JsonParser.parseString(line).getAsJsonObject();
                check(ready.get("owned").getAsInt() == blocks.size() && ready.get("protected").getAsInt() == blocks.size(),
                    "the hut ledger fixture did not protect every block: " + ready);
                return new LedgerFixture(process, reader, ready);
            } catch (java.io.IOException error) {
                throw new AssertionError("ledger fixture failed", error);
            }
        }

        @Override public void close() {
            try {
                process.getOutputStream().close();
                reader.close();
                check(process.waitFor(10, java.util.concurrent.TimeUnit.SECONDS) && process.exitValue() == 0,
                    "ownership ledger fixture failed to stop cleanly");
            } catch (java.io.IOException | InterruptedException error) {
                throw new AssertionError("ledger fixture cleanup failed", error);
            } finally { process.destroyForcibly(); }
        }
    }
}
