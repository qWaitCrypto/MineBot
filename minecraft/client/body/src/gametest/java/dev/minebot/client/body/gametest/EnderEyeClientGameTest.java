package dev.minebot.client.body.gametest;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import net.fabricmc.fabric.api.client.gametest.v1.FabricClientGameTest;
import net.fabricmc.fabric.api.client.gametest.v1.context.ClientGameTestContext;
import net.fabricmc.fabric.api.client.gametest.v1.context.TestSingleplayerContext;
import net.fabricmc.fabric.api.client.gametest.v1.context.TestServerContext;
import net.minecraft.core.BlockPos;
import net.minecraft.core.registries.Registries;
import net.minecraft.tags.StructureTags;

import java.util.Locale;

/** Exercises the useItem action's eye and snowball trajectories in a seeded world. */
public class EnderEyeClientGameTest implements FabricClientGameTest {
    @Override
    public void runTest(ClientGameTestContext context) {
        String only = System.getenv().getOrDefault("MINEBOT_GAMETEST_CASE", "");
        if (!only.isEmpty() && !only.equals("enderEye")) return;
        try (TestSingleplayerContext world = context.worldBuilder().setUseConsistentSettings(false).adjustSettings(settings -> {
            settings.setName("MineBotEnderEyeTest");
            settings.setSeed("824729318740019200");
            settings.setGenerateStructures(true);
        }).create()) {
            TestServerContext server = world.getServer();
            server.runCommand("difficulty peaceful");
            server.runCommand("gamemode survival @p");
            world.getClientLevel().waitForChunksDownload();
            BlockPos stronghold = server.computeOnServer(s -> {
                var nearest = s.overworld().getChunkSource().getGenerator().findNearestMapStructure(
                    s.overworld(), s.registryAccess().lookupOrThrow(Registries.STRUCTURE)
                        .getOrThrow(StructureTags.EYE_OF_ENDER_LOCATED), BlockPos.ZERO, 500, false);
                return nearest == null ? null : nearest.getFirst();
            });
            check(stronghold != null, "seeded normal world has no eye-located stronghold");
            server.runCommand("forceload add " + (stronghold.getX() - 16) + " " + (stronghold.getZ() - 16)
                + " " + (stronghold.getX() + 16) + " " + (stronghold.getZ() + 16));
            server.runCommand("give @p minecraft:ender_eye 1");
            server.runCommand("give @p minecraft:snowball 1");
            server.runCommand("tp @p " + (stronghold.getX() + 40) + " 100 " + (stronghold.getZ() + 40));
            context.waitFor(client -> client.player != null && client.player.getInventory().countItem(
                net.minecraft.world.item.Items.ENDER_EYE) == 1, 200);
            context.waitTicks(30);
            try (BodyIpc body = BodyIpc.connect(context)) {
                JsonObject use = new JsonObject();
                use.addProperty("item", "ender_eye");
                JsonObject terminal = body.command("useItem", use, 100);
                check("completed".equals(terminal.get("status").getAsString()),
                    "eye use failed: " + terminal);
                JsonArray eyeDirection = observedDirection(context, body, "eye_of_ender");
                double[] eyeTarget = {stronghold.getX() - (stronghold.getX() + 40),
                    stronghold.getZ() - (stronghold.getZ() + 40)};
                assertDirection(eyeDirection, eyeTarget);

                // Open sky for the snowball: thrown from the terrain at Y=100 it hit a slope before the first sample.
                server.runCommand("setblock " + (stronghold.getX() + 40) + " 179 " + (stronghold.getZ() + 40) + " minecraft:barrier");
                server.runCommand("tp @p " + (stronghold.getX() + 40) + ".5 180 " + (stronghold.getZ() + 40) + ".5");
                context.waitTicks(20);
                // Throw a little upward: a snowball thrown at the feet lands before the first state sample.
                JsonObject look = new JsonObject();
                JsonArray ahead = context.computeOnClient(client -> {
                    JsonArray point = new JsonArray();
                    point.add(client.player.getX() + 10); point.add(client.player.getEyeY() + 6); point.add(client.player.getZ());
                    return point;
                });
                look.add("target", ahead);
                body.primitive("lookAt", look);
                JsonObject snowballUse = new JsonObject();
                snowballUse.addProperty("item", "snowball");
                JsonObject snowballTerminal = body.command("useItem", snowballUse, 100);
                check("completed".equals(snowballTerminal.get("status").getAsString()),
                    "snowball use failed: " + snowballTerminal);
                JsonArray snowballDirection = observedDirection(context, body, "snowball");
                check(snowballDirection.get(0).getAsDouble() != 0
                    || snowballDirection.get(1).getAsDouble() != 0, "snowball horizontal path was not observed");
            }
        }
    }

    private static JsonArray observedDirection(ClientGameTestContext context, BodyIpc body, String type) {
        JsonArray first = null, last = null;
        for (int tick = 0; tick < 20; tick++) {
            JsonObject state = body.state();
            for (var value : state.getAsJsonArray("entities")) {
                JsonObject entity = value.getAsJsonObject();
                if (!type.equals(entity.get("type").getAsString())) continue;
                if (first == null) first = entity.getAsJsonArray("pos").deepCopy();
                last = entity.getAsJsonArray("pos").deepCopy();
            }
            context.waitTick();
        }
        check(first != null && last != null, "no new " + type + " entity was observed; removing "
            + "`entities.add(observed)` from MineBotClientBody's nearby-entity snapshot should fail this case");
        double dx = last.get(0).getAsDouble() - first.get(0).getAsDouble();
        double dz = last.get(2).getAsDouble() - first.get(2).getAsDouble();
        double length = Math.hypot(dx, dz);
        check(length > 0, type + " horizontal path had zero length");
        JsonArray direction = new JsonArray();
        direction.add(dx / length);
        direction.add(dz / length);
        return direction;
    }

    private static void assertDirection(JsonArray direction, double[] targetVector) {
        check(targetVector != null && targetVector.length == 2,
            "server stronghold-directed eye entity was not observed");
        double dot = direction.get(0).getAsDouble() * targetVector[0]
            + direction.get(1).getAsDouble() * targetVector[1];
        double length = Math.hypot(targetVector[0], targetVector[1]);
        double angle = Math.toDegrees(Math.acos(Math.max(-1, Math.min(1, dot / length))));
        check(angle <= 5.0,
            String.format(Locale.ROOT, "eye direction differs from server stronghold lookup by %.2f degrees: %s", angle, direction));
    }

    private static void check(boolean condition, String message) {
        if (!condition) throw new AssertionError(message);
    }
}
