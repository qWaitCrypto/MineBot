package dev.minebot.client.body.gametest;

import baritone.api.BaritoneAPI;
import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import dev.minebot.client.body.SpawnMessage;
import net.fabricmc.fabric.api.client.gametest.v1.FabricClientGameTest;
import net.fabricmc.fabric.api.client.gametest.v1.context.ClientGameTestContext;
import net.fabricmc.fabric.api.client.gametest.v1.context.TestServerContext;
import net.fabricmc.fabric.api.client.gametest.v1.context.TestSingleplayerContext;
import net.minecraft.core.BlockPos;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.world.item.ItemStack;

/**
 * Body actions driven over the client's own IPC in a singleplayer world,
 * with no server, no Python and no model: the regression suite for what
 * failed live. Each case builds its arena with commands, so it proves one
 * Body link, never a capability (from-zero runs do that).
 *
 * Run: ./gradlew :client:body:runClientGameTest
 */
public class BodyClientGameTest implements FabricClientGameTest {
    private static final int X = 0, Y = 120, Z = 0; // the floor's top face is at Y

    @Override
    public void runTest(ClientGameTestContext context) {
        try (TestSingleplayerContext world = context.worldBuilder().create()) {
            TestServerContext server = world.getServer();
            server.runCommand("difficulty peaceful");
            server.runCommand("time set day");
            server.runCommand("gamemode survival @p");
            server.runCommand("forceload add " + (X - 16) + " " + (Z - 16) + " " + (X + 32) + " " + (Z + 32));
            world.getClientLevel().waitForChunksDownload();
            // The floor first: teleported above an unbuilt arena, the player fell to its death.
            arena(server);
            server.runCommand("tp @p " + (X + 3) + ".5 " + Y + " " + (Z + 4) + ".5");
            context.waitFor(client -> client.player.onGround() && client.player.getY() >= Y - 0.01, 400);
            // As in the live client's baritone/settings.txt: Baritone keeps the
            // best pickaxe in hotbar slot 0 and a throwaway block in slot 8.
            context.runOnClient(client -> BaritoneAPI.getSettings().allowInventory.value = true);
            try (BodyIpc body = BodyIpc.connect(context)) {
                String only = System.getenv().getOrDefault("MINEBOT_GAMETEST_CASE", "");
                if (only.equals("dragonFacts")) {
                    dragonFacts(context, server, body);
                    return;
                }
                if (only.equals("pathAroundCloud")) {
                    pathAroundCloud(context, server, body);
                    return;
                }
                if (only.equals("walkLookDown")) {
                    walkLookDown(context, server, body);
                    return;
                }
                if (only.equals("stepToLookDown")) {
                    stepToLookDown(context, server, body);
                    return;
                }
                if (only.equals("keepMovingStaysSafe")) {
                    keepMovingStaysSafe(context, server, body);
                    return;
                }
                if (only.equals("keepMovingFromOutsideArea")) {
                    keepMovingFromOutsideArea(context, server, body);
                    return;
                }
                if (only.equals("keepMovingFromArrivalPlatform")) {
                    keepMovingFromArrivalPlatform(context, server, body);
                    return;
                }
                if (only.equals("legsKeepGoingThroughHands")) {
                    legsKeepGoingThroughHands(context, server, body);
                    return;
                }
                if (only.equals("legsResumeAfterLegAction")) {
                    legsResumeAfterLegAction(context, server, body);
                    return;
                }
                if (only.equals("legsEndWhenWalledIn")) {
                    legsEndWhenWalledIn(context, server, body);
                    return;
                }
                if (only.equals("gazeHoldsStanding")) {
                    gazeHoldsStanding(context, server, body);
                    return;
                }
                if (only.equals("legsResetOnDeath")) {
                    legsResetOnDeath(context, server, body);
                    return;
                }
                if (only.equals("legsAll")) {
                    legsKeepGoingThroughHands(context, server, body);
                    legsResumeAfterLegAction(context, server, body);
                    legsEndWhenWalledIn(context, server, body);
                    gazeHoldsStanding(context, server, body);
                    legsResetOnDeath(context, server, body);
                    return;
                }
                if (only.equals("handsWhileLegsMove")) {
                    handsWhileLegsMove(context, server, body);
                    return;
                }
                if (only.equals("keepMovingLookDown")) {
                    keepMovingLookDown(context, server, body);
                    return;
                }
                if (only.equals("walkIntoEndPortal")) {
                    walkIntoEndPortal(context, server, body);
                    return;
                }
                if (only.equals("endBedExplodesNearDragon")) {
                    endBedExplodesNearDragon(context, server, body);
                    return;
                }
                if (only.isEmpty() || only.equals("threatFacts")) threatFacts(context, server, body);
                if (only.equals("threatFacts")) return;
                if (only.isEmpty() || only.startsWith("shelter")) shelterCases(context, server, body, only);
                if (only.isEmpty() || only.equals("pillarUpRaisesThreeBlocks")) {
                    pillarUpRaisesThreeBlocks(context, server, body);
                }
                if (only.isEmpty() || only.equals("raiseShieldBlocksSkeletonArrows")) {
                    raiseShieldBlocksSkeletonArrows(context, server, body);
                }
                if (only.isEmpty() || only.equals("rangedAttackRemovesEndCrystal")) {
                    rangedAttackRemovesEndCrystal(context, server, body);
                }
                if (only.isEmpty() || only.equals("bowHitsPillarCrystals")) bowHitsPillarCrystals(context, server, body);
                if (only.isEmpty() || only.equals("leadHitsMovingTarget")) leadHitsMovingTarget(context, server, body);
                if (only.isEmpty() || only.equals("rangedEndsAtRelease")) rangedEndsAtRelease(context, server, body);
                if (only.isEmpty() || only.equals("rangedOutOfRangeHonest")) rangedOutOfRangeHonest(context, server, body);
                if (only.isEmpty() || only.equals("pillarFromOffCentre")) pillarFromOffCentre(context, server, body);
                if (only.isEmpty() || only.equals("pillarShovedMidColumn")) pillarShovedMidColumn(context, server, body);
                if (only.isEmpty() || only.equals("pillarToCagedCrystal")) {
                    pillarToCagedCrystal(context, server, body);
                }
                if (only.isEmpty() || only.equals("collectRoutesToStandingCell")) {
                    collectRoutesToStandingCell(context, server, body);
                }
                if (only.isEmpty() || only.equals("collectOreRegression") || only.equals("collectOreInReach")) {
                    collectOreInReach(context, server, body);
                }
                if (only.isEmpty() || only.equals("collectOreRegression") || only.equals("collectOreInCeiling")) {
                    collectOreInCeiling(context, server, body);
                }
                if (only.isEmpty() || only.equals("meleePerchedDragon")) {
                    meleePerchedDragon(context, server, body);
                }
                if (only.isEmpty() || only.equals("farDragonAndFall")) farDragonAndFall(context, server, body);
                if (only.isEmpty() || only.equals("cushionFall")) cushionFall(context, server, body);
                if (only.isEmpty() || only.equals("hurtHere")) hurtHere(context, server, body);
                if (only.isEmpty() || only.equals("discardItemTowardPiglin")) {
                    discardItemTowardPiglin(context, server, body);
                }
                if (only.isEmpty() || only.equals("discardItemUnknownTargetDropsNothing")) {
                    discardItemUnknownTargetDropsNothing(context, server, body);
                }
                if (only.isEmpty() || only.equals("raiseShieldWithoutShieldReportsNoShield")) {
                    raiseShieldWithoutShieldReportsNoShield(context, server, body);
                }
                if (only.isEmpty() || only.equals("noRouteSaysWhy")) noRouteSaysWhy(context, server, body);
                if (only.isEmpty() || only.equals("engageSight")) engageSight(context, server, body);
                if (only.equals("engageDrawsTheBestWeapon")) engageDrawsTheBestWeapon(context, server, body);
                if (only.equals("engageTrade")) engageTrade(context, server, body);
                if (only.isEmpty() || only.equals("endArrivalTunnel")) endArrivalTunnel(context, server, body);
                if (only.isEmpty() || only.equals("makeWayOutOfFloodedPocket")) makeWayOutOfFloodedPocket(context, server, body);
                if (only.isEmpty() || only.equals("ascendMakesWayFromFloodedPit")) ascendMakesWayFromFloodedPit(context, server, body);
                if (only.isEmpty() || only.startsWith("carriedBed")) carriedBedCases(context, server, body, only);
                if (only.isEmpty() || only.equals("spawnMessages")) spawnMessagesReachTheState(context, server, body);
                if (only.isEmpty() || only.equals("chest")) chestTransfersReachBothHalves(context, server, body);
                if (only.equals("lava")) {
                    leavesLavaWhenTheNamedExitTurnedToLava(context, server, body);
                    leavesLavaOntoTheReportedExit(context, server, body);
                }
                if (only.equals("harmfulCloudFacts")) harmfulCloudFacts(context, server, body);
                if (only.isEmpty() || only.equals("stepsAndStepTo")) stepsAndStepTo(context, server, body);
                if (only.equals("exposedOreFacts")) exposedOreFacts(context, server, body);
                if (!only.isEmpty()) return;
                heldItemSurvivesBaritone(context, server, body);
                bucketFillsFromSource(context, server, body);
                waterPouredThroughAFloorFace(context, server, body);
                flintLightsTheCellAbove(context, server, body);
                calmNeutralMobsAreNotHostile(context, server, body);
                engageDrawsTheBestWeapon(context, server, body);
                aLeftoverScreenIsClosedFirst(context, server, body);
                digsUpThroughSolidStone(context, server, body);
                digsUpFromAPillarInACave(context, server, body);
                leavesLavaWhenTheNamedExitTurnedToLava(context, server, body);
                // Last: the player leaves it burning.
                leavesLavaOntoTheReportedExit(context, server, body);
            }
        }
    }

    // Fails if the ranged controller stops resolving its generic target UUID to any Entity.
    private static void rangedAttackRemovesEndCrystal(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        server.runCommand("difficulty peaceful");
        server.runCommand("kill @e[type=!player]");
        arena(server);
        server.runCommand("clear @p");
        server.runCommand("give @p minecraft:bow");
        server.runCommand("give @p minecraft:arrow 8");
        server.runCommand("setblock " + (X + 15) + " " + (Y - 1) + " " + (Z + 4) + " minecraft:bedrock");
        server.runCommand("summon minecraft:end_crystal " + (X + 15) + ".5 " + (Y + 1) + " " + (Z + 4) + ".5 {ShowBottom:0b}");
        server.runCommand("tp @p " + (X + 3) + ".5 " + Y + " " + (Z + 4) + ".5");
        context.waitFor(client -> client.player.onGround() && countItem(client, "arrow") == 8, 200);
        String crystalId = context.computeOnClient(client -> java.util.stream.StreamSupport.stream(client.level.entitiesForRendering().spliterator(), false)
            .filter(entity -> entity.getType() == net.minecraft.world.entity.EntityType.END_CRYSTAL)
            .findFirst().orElseThrow().getUUID().toString());
        for (var value : body.state().getAsJsonArray("entities")) {
            JsonObject entity = value.getAsJsonObject();
            if (crystalId.equals(entity.get("id").getAsString())) {
                check(entity.has("caged") && !entity.get("caged").getAsBoolean(), "open crystal cage fact: " + entity);
            }
        }
        JsonObject params = new JsonObject();
        params.addProperty("target_id", crystalId);
        params.addProperty("weapon", "bow");
        params.addProperty("shots", 1);
        params.addProperty("max_range", 24);
        JsonObject terminal = body.command("rangedAttack", params, 500);
        check("completed".equals(terminal.get("status").getAsString()), "rangedAttack ended " + terminal);
        JsonObject shot = awaitShot(context, body, 120);
        check("hit".equals(shot.get("outcome").getAsString()), "the arrow at the crystal: " + shot);
        check(server.computeOnServer(s -> s.overworld().getEntitiesOfClass(net.minecraft.world.entity.boss.enderdragon.EndCrystal.class,
            new net.minecraft.world.phys.AABB(X, Y, Z, X + 32, Y + 8, Z + 16)).isEmpty()),
            "server still has an end crystal after rangedAttack");
    }

    /** Route 2: reach a caged crystal, remove its shell, and return safely. */
    private static void pillarToCagedCrystal(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        server.runCommand("difficulty peaceful");
        server.runCommand("kill @e[type=!player]");
        arena(server);
        server.runCommand("clear @p");
        server.runCommand("item replace entity @p hotbar.0 with minecraft:diamond_pickaxe");
        server.runCommand("give @p minecraft:cobblestone 24");
        // The Body pillars up just outside the cage's west wall, as a player
        // would beside a caged pillar, and opens the wall facing the crystal.
        int px = X + 7, pz = Z + 4;
        int crystalX = X + 10;
        int crystalY = Y + 21;
        int crystalZ = Z + 4;
        server.runCommand(fill(crystalX, Y, crystalZ, crystalX, Y + 19, crystalZ, "obsidian"));
        server.runCommand("setblock " + crystalX + " " + (Y + 20) + " " + crystalZ + " minecraft:bedrock");
        server.runCommand("summon minecraft:end_crystal " + crystalX + ".5 " + crystalY + " " + crystalZ + ".5 {ShowBottom:0b}");
        for (int dx = -2; dx <= 2; dx++) for (int dy = -1; dy <= 2; dy++) for (int dz = -2; dz <= 2; dz++) {
            if (Math.abs(dx) == 2 || Math.abs(dz) == 2 || dy == 2) {
                server.runCommand("setblock " + (crystalX + dx) + " " + (crystalY + dy) + " "
                    + (crystalZ + dz) + " minecraft:iron_bars");
            }
        }
        server.runCommand("tp @p " + px + ".5 " + Y + " " + pz + ".5");
        context.waitFor(client -> client.player.onGround() && countItem(client, "cobblestone") == 24,
            200);
        boolean cageFact = false;
        for (var value : body.state().getAsJsonArray("entities")) {
            JsonObject entity = value.getAsJsonObject();
            if ("end_crystal".equals(entity.get("type").getAsString())) {
                cageFact = entity.has("caged") && entity.get("caged").getAsBoolean();
            }
        }
        check(cageFact, "caged crystal fact was absent: " + body.state());
        System.setProperty("minebot.client.disposable_world", "true");
        try {
            for (int blocks : new int[] {16, 4}) {
                JsonObject up = new JsonObject();
                up.addProperty("blocks", blocks);
                JsonArray scaffold = new JsonArray();
                scaffold.add("cobblestone");
                up.add("scaffold_blocks", scaffold);
                JsonObject raised = body.command("pillarUp", up, 1200);
                check("completed".equals(raised.get("status").getAsString()), "pillarUp ended " + raised);
            }
            check(context.computeOnClient(client -> client.player.blockPosition().getY()) == Y + 20,
                "Body did not reach the cage height");

            // The bars between the Body and the crystal: the west wall, three
            // wide and three high around the crystal's line.
            java.util.List<BlockPos> opening = new java.util.ArrayList<>();
            for (int dy = -1; dy <= 1; dy++) for (int dz = -1; dz <= 1; dz++) {
                opening.add(new BlockPos(crystalX - 2, crystalY + dy, crystalZ + dz));
            }
            // The same command mine_block_collect sends (cluster_collection.py).
            JsonArray targets = new JsonArray();
            for (BlockPos at : opening) {
                JsonArray target = new JsonArray();
                target.add(at.getX()); target.add(at.getY()); target.add(at.getZ());
                targets.add(target);
            }
            JsonArray ids = new JsonArray();
            ids.add("iron_bars");
            JsonObject mine = new JsonObject();
            mine.add("targets", targets);
            mine.add("expected_block_ids", ids);
            mine.add("expected_item_ids", ids);
            mine.addProperty("timeout_s", 60);
            JsonObject mined = body.command("collectBlocks", mine, 1400);
            // First run: 8 of 9 then resource_cluster_incomplete, said honestly;
            // the caller goes again for what is left, as the Agent would.
            if (!"completed".equals(mined.get("status").getAsString())) {
                JsonArray left = new JsonArray();
                for (BlockPos at : opening) {
                    if ("air".equals(block(server, at))) continue;
                    JsonArray target = new JsonArray();
                    target.add(at.getX()); target.add(at.getY()); target.add(at.getZ());
                    left.add(target);
                }
                mine.add("targets", left);
                System.out.println("MINEBOT_END pillarToCagedCrystal retry left=" + left);
                mined = body.command("collectBlocks", mine, 1400);
            }
            check("completed".equals(mined.get("status").getAsString()), "collectBlocks ended " + mined);
            context.waitTicks(10);
            for (BlockPos at : opening) {
                check("air".equals(block(server, at)), "iron bar remained at " + at);
            }

            JsonObject down = new JsonObject();
            down.addProperty("target_y", Y);
            down.addProperty("max_steps", 24);
            down.addProperty("max_clear_fall", 2);
            down.addProperty("max_distance", 24);
            down.addProperty("max_mutations", 32);
            JsonObject lowered = body.command("digDown", down, 1200);
            check("completed".equals(lowered.get("status").getAsString()), "digDown ended " + lowered);
        } finally {
            System.clearProperty("minebot.client.disposable_world");
        }
        context.waitFor(client -> client.player.onGround() && client.player.blockPosition().getY() == Y, 300);
        float health = context.computeOnClient(client -> client.player.getHealth());
        check(health > 10.0f, "Body returned from the cage with health " + health);
        System.out.println("MINEBOT_END PASS pillarToCagedCrystal bars_removed=true ground_y=" + Y + " health=" + health);
    }

    /**
     * A target out of reach and floating in the air: collectBlocks routes to a
     * floored cell within reach and breaks it from there, rather than standing
     * in the target's cell (pillaring or bridging into the air to get there).
     */
    private static void collectRoutesToStandingCell(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        server.runCommand("difficulty peaceful");
        server.runCommand("kill @e[type=!player]");
        arena(server);
        server.runCommand("clear @p");
        server.runCommand("item replace entity @p hotbar.0 with minecraft:diamond_pickaxe");
        server.runCommand("give @p minecraft:cobblestone 16");
        BlockPos target = new BlockPos(X + 10, Y + 3, Z + 4);
        server.runCommand("setblock " + target.getX() + " " + target.getY() + " " + target.getZ() + " minecraft:iron_bars");
        server.runCommand("tp @p " + (X + 1) + ".5 " + Y + " " + (Z + 4) + ".5");
        context.waitFor(client -> client.player.onGround() && countItem(client, "cobblestone") == 16
            && client.player.blockPosition().getX() == X + 1, 200);
        JsonArray targets = new JsonArray();
        JsonArray at = new JsonArray();
        at.add(target.getX()); at.add(target.getY()); at.add(target.getZ());
        targets.add(at);
        JsonArray ids = new JsonArray();
        ids.add("iron_bars");
        JsonObject mine = new JsonObject();
        mine.add("targets", targets);
        mine.add("expected_block_ids", ids);
        mine.add("expected_item_ids", ids);
        mine.addProperty("timeout_s", 60);
        JsonObject mined;
        System.setProperty("minebot.client.disposable_world", "true");
        try {
            mined = body.command("collectBlocks", mine, 1400);
        } finally {
            System.clearProperty("minebot.client.disposable_world");
        }
        check("completed".equals(mined.get("status").getAsString()), "collectBlocks ended " + mined);
        check("air".equals(block(server, target)), "the floating bar remained at " + target);
        int feetY = context.computeOnClient(client -> client.player.blockPosition().getY());
        int cobble = context.computeOnClient(client -> countItem(client, "cobblestone"));
        check(feetY == Y, "Body left the floor to collect: feet y " + feetY);
        check(cobble == 16, "Body placed blocks to collect: cobblestone " + cobble);
        System.out.println("MINEBOT_END PASS collectRoutesToStandingCell feet_y=" + feetY + " cobblestone=" + cobble
            + " position=" + mined.get("position") + " reason=" + mined.get("reason"));
    }

    private static JsonObject collectOne(BodyIpc body, BlockPos target, String block, String item) {
        JsonArray targets = new JsonArray();
        JsonArray at = new JsonArray();
        at.add(target.getX()); at.add(target.getY()); at.add(target.getZ());
        targets.add(at);
        JsonArray blocks = new JsonArray();
        blocks.add(block);
        JsonArray items = new JsonArray();
        items.add(item);
        JsonObject mine = new JsonObject();
        mine.add("targets", targets);
        mine.add("expected_block_ids", blocks);
        mine.add("expected_item_ids", items);
        mine.addProperty("timeout_s", 90);
        System.setProperty("minebot.client.disposable_world", "true");
        try {
            return body.command("collectBlocks", mine, 2000);
        } finally {
            System.clearProperty("minebot.client.disposable_world");
        }
    }

    /** Ore in a wall at arm's reach is broken from where the Body stands. */
    private static void collectOreInReach(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        server.runCommand("difficulty peaceful");
        server.runCommand("kill @e[type=!player]");
        arena(server);
        server.runCommand("clear @p");
        server.runCommand("item replace entity @p hotbar.0 with minecraft:diamond_pickaxe");
        server.runCommand(fill(X + 3, Y, Z + 2, X + 3, Y + 3, Z + 6, "stone"));
        BlockPos ore = new BlockPos(X + 3, Y + 1, Z + 4);
        server.runCommand("setblock " + ore.getX() + " " + ore.getY() + " " + ore.getZ() + " minecraft:coal_ore");
        server.runCommand("tp @p " + (X + 1) + ".5 " + Y + " " + (Z + 4) + ".5");
        context.waitFor(client -> client.player.onGround() && client.player.blockPosition().getX() == X + 1, 200);
        BlockPos before = context.computeOnClient(client -> client.player.blockPosition());
        JsonObject mined = collectOne(body, ore, "coal_ore", "coal");
        check("completed".equals(mined.get("status").getAsString()), "collectBlocks ended " + mined);
        check("air".equals(block(server, ore)), "the coal ore remained at " + ore);
        BlockPos after = context.computeOnClient(client -> client.player.blockPosition());
        int coal = context.computeOnClient(client -> countItem(client, "coal"));
        int edits = mined.get("world_edit_count").getAsInt();
        // Broken from where it stood: only the ore changed (the old goal walked
        // into the ore's cell and dug the stone above it too). Walking a step
        // on the floor afterwards to pick the drop up is allowed.
        check(edits == 1, "collecting ore in reach changed " + edits + " blocks: " + mined);
        check(after.getY() == Y && after.getX() < X + 3, "Body left the floor or entered the wall: " + before + " -> " + after);
        check(coal >= 1, "no coal in the inventory: " + mined);
        System.out.println("MINEBOT_END PASS collectOreInReach start=" + before.toShortString() + " feet=" + after.toShortString()
            + " world_edits=" + edits + " coal=" + coal
            + " path_events=" + mined.get("path_events") + " reason=" + mined.get("reason"));
    }

    /**
     * Ore in the ceiling of a closed room, out of reach and with no floored
     * cell within reach of it: the upstream goal is kept and Baritone pillars.
     */
    private static void collectOreInCeiling(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        server.runCommand("difficulty peaceful");
        server.runCommand("kill @e[type=!player]");
        arena(server);
        server.runCommand("clear @p");
        server.runCommand("item replace entity @p hotbar.0 with minecraft:diamond_pickaxe");
        server.runCommand("give @p minecraft:cobblestone 16");
        // Interior x X+3..X+7, z Z+2..Z+6, feet Y..Y+6; the ceiling layer is Y+7.
        server.runCommand(fill(X + 2, Y - 1, Z + 1, X + 8, Y + 7, Z + 7, "stone") + " hollow");
        BlockPos ore = new BlockPos(X + 5, Y + 7, Z + 4);
        server.runCommand("setblock " + ore.getX() + " " + ore.getY() + " " + ore.getZ() + " minecraft:iron_ore");
        server.runCommand("tp @p " + (X + 5) + ".5 " + Y + " " + (Z + 4) + ".5");
        context.waitFor(client -> client.player.onGround() && countItem(client, "cobblestone") == 16
            && client.player.blockPosition().getX() == X + 5 && client.player.blockPosition().getY() == Y, 200);
        JsonObject mined = collectOne(body, ore, "iron_ore", "raw_iron");
        context.waitFor(client -> client.player.onGround(), 100);
        String below = context.computeOnClient(client -> path(client.level.getBlockState(client.player.getOnPos()).getBlock().asItem().getDefaultInstance()));
        boolean solid = context.computeOnClient(client -> client.level.getBlockState(client.player.getOnPos())
            .isCollisionShapeFullBlock(client.level, client.player.getOnPos()));
        int feetY = context.computeOnClient(client -> client.player.blockPosition().getY());
        int rawIron = context.computeOnClient(client -> countItem(client, "raw_iron"));
        int cobble = context.computeOnClient(client -> countItem(client, "cobblestone"));
        String drops = server.computeOnServer(s -> s.overworld().getEntitiesOfClass(net.minecraft.world.entity.item.ItemEntity.class,
            new net.minecraft.world.phys.AABB(X - 4, Y - 2, Z - 4, X + 14, Y + 12, Z + 12)).stream()
            .map(e -> path(e.getItem()) + "@" + e.blockPosition().toShortString()).toList().toString());
        System.out.println("MINEBOT_END collectOreInCeiling result feet=" + context.computeOnClient(client -> client.player.blockPosition().toShortString())
            + " drops=" + drops + " edits=" + mined.get("world_edit_count") + " cobblestone=" + cobble + " raw_iron=" + rawIron);
        server.runCommand(fill(X + 2, Y, Z + 1, X + 8, Y + 7, Z + 7, "air"));
        check("completed".equals(mined.get("status").getAsString()), "collectBlocks ended " + mined);
        check("air".equals(block(server, ore)), "the iron ore remained at " + ore);
        check(rawIron >= 1, "no raw iron in the inventory: " + mined);
        check(solid, "Body did not end on solid ground: on " + below + " feet y " + feetY);
        System.out.println("MINEBOT_END PASS collectOreInCeiling feet_y=" + feetY + " on=" + below + " raw_iron=" + rawIron
            + " cobblestone=" + cobble + " reason=" + mined.get("reason"));
    }

    /** Route 2: the melee target is a dragon entity whose reachable part is not its centre. */
    private static void meleePerchedDragon(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        server.runCommand("difficulty peaceful");
        server.runCommand("kill @e[type=!player]");
        arena(server);
        server.runCommand("clear @p");
        server.runCommand("give @p minecraft:diamond_sword");
        int px = X + 3, pz = Z + 4;
        server.runCommand("tp @p " + px + ".5 " + Y + " " + pz + ".5");
        server.runCommand(fill(px + 1, Y, pz - 1, px + 3, Y, pz + 1, "bedrock"));
        server.runCommand("setblock " + (px + 2) + " " + Y + " " + pz + " minecraft:end_portal");
        server.runCommand("summon minecraft:ender_dragon " + (px + 2.5) + " " + (Y + 1) + " " + pz
            + " {NoAI:1b,Health:10.0f}");
        context.waitFor(client -> !client.level.getEntities(client.player, client.player.getBoundingBox().inflate(16),
            entity -> entity instanceof net.minecraft.world.entity.boss.enderdragon.EnderDragon).isEmpty(), 200);
        JsonObject dragon = null;
        for (var value : body.state().getAsJsonArray("entities")) {
            JsonObject candidate = value.getAsJsonObject();
            if ("ender_dragon".equals(candidate.get("type").getAsString())) dragon = candidate;
        }
        check(dragon != null && dragon.get("health").getAsDouble() > 0.0, "dragon was not observed: " + dragon);
        double before = dragon.get("health").getAsDouble();
        JsonObject params = new JsonObject();
        params.addProperty("target_uuid", dragon.get("id").getAsString());
        params.addProperty("attack_range", 4.5);
        params.addProperty("cooldown_ticks", 10);
        params.addProperty("timeout_ticks", 240);
        JsonObject terminal = body.command("engageEntity", params, 400);
        double after = server.computeOnServer(s -> s.overworld().getEntitiesOfClass(
            net.minecraft.world.entity.boss.enderdragon.EnderDragon.class,
            new net.minecraft.world.phys.AABB(X, Y, Z, X + 16, Y + 16, Z + 16)).stream()
                .findFirst().map(net.minecraft.world.entity.boss.enderdragon.EnderDragon::getHealth).orElse(0.0f));
        check(after < before, "engageEntity did not reach a dragon part: before " + before + " after " + after
            + " terminal " + terminal);
        System.out.println("MINEBOT_END PASS meleePerchedDragon health_before=" + before + " health_after=" + after);
    }

    /** End baseline run-4: ten arrows at pillar crystals 22-38 blocks away, aimed
     * straight at them, hit none. Aimed with the arrow's drop, one arrow per
     * action hits a crystal on a pillar 20 up and 30 away, and one 8 up and 40
     * away. Shot from beside a pillar, the arrow strikes it and the result says
     * where (the "missed" fact). */
    private static void bowHitsPillarCrystals(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        server.runCommand("difficulty peaceful");
        server.runCommand("kill @e[type=!player]");
        arena(server);
        server.runCommand("clear @p");
        server.runCommand("give @p minecraft:bow");
        server.runCommand("give @p minecraft:arrow 16");
        // On the arena floor: "-6.5" would be block -7, off its edge.
        int px = X - 4, pz = Z + 4;
        server.runCommand("tp @p " + px + ".5 " + Y + " " + pz + ".5");
        context.waitFor(client -> client.player.onGround() && countItem(client, "arrow") == 16, 200);
        int[][] crystals = {{px + 30, Y + 20, pz}, {px + 38, Y + 8, pz + 12}};
        try {
            for (int[] at : crystals) {
                server.runCommand(fill(at[0] - 1, Y, at[2] - 1, at[0] + 1, at[1] - 2, at[2] + 1, "obsidian"));
                server.runCommand("setblock " + at[0] + " " + (at[1] - 1) + " " + at[2] + " minecraft:bedrock");
                server.runCommand("summon minecraft:end_crystal " + at[0] + ".5 " + at[1] + " " + at[2] + ".5 {ShowBottom:0b}");
                context.waitFor(client -> crystalNear(client, at) != null, 100);
                String id = context.computeOnClient(client -> crystalNear(client, at).getUUID().toString());
                JsonObject params = new JsonObject();
                params.addProperty("target_id", id);
                params.addProperty("weapon", "bow");
                params.addProperty("max_range", 64);
                JsonObject terminal = body.command("rangedAttack", params, 300);
                JsonObject shot = awaitShot(context, body, 120);
                boolean gone = context.computeOnClient(client -> crystalNear(client, at) == null);
                check(gone && terminal.get("shots").getAsInt() == 1 && "hit".equals(shot.get("outcome").getAsString()),
                    "one arrow at the crystal " + java.util.Arrays.toString(at)
                    + " ended " + terminal.get("status") + " " + terminal.get("reason") + " shots " + terminal.get("shots")
                    + " last_shot " + shot + " pitch " + terminal.get("pitch"));
            }
            // A block beside the Bot in the way: the arrow is loosed and the
            // result names what it struck (the path check was withdrawn, §6.y.15).
            server.runCommand("setblock " + (px + 1) + " " + (Y + 1) + " " + pz + " minecraft:iron_bars");
            server.runCommand("setblock " + (px + 1) + " " + (Y + 2) + " " + pz + " minecraft:iron_bars");
            int[] far = {px + 30, Y + 2, pz};
            server.runCommand("summon minecraft:end_crystal " + far[0] + ".5 " + far[1] + " " + far[2] + ".5 {ShowBottom:0b}");
            context.waitFor(client -> crystalNear(client, far) != null, 100);
            JsonObject wall = new JsonObject();
            String wallId = context.computeOnClient(client -> crystalNear(client, far).getUUID().toString());
            wall.addProperty("target_id", wallId);
            wall.addProperty("weapon", "bow");
            wall.addProperty("max_range", 64);
            JsonObject walled = body.command("rangedAttack", wall, 300);
            JsonObject struck = awaitShot(context, body, 120);
            check("arrow_released".equals(walled.get("reason").getAsString()) && walled.get("shots").getAsInt() == 1
                && "missed".equals(struck.get("outcome").getAsString())
                && struck.has("arrow_landed_block") && "iron_bars".equals(struck.get("arrow_landed_block").getAsString()),
                "a shot into the bars beside the Bot ended " + walled.get("reason") + " shots " + walled.get("shots")
                + " last_shot " + struck);
            System.out.println("MINEBOT_RANGED PASS bowHitsPillarCrystals; beside: " + struck.get("arrow_landed_block")
                + " at " + struck.get("arrow_landed"));
        } finally {
            server.runCommand("kill @e[type=end_crystal]");
            server.runCommand(fill(px, Y, pz - 2, px + 40, Y + 20, pz + 14, "air"));
            arena(server);
        }
    }

    /** A target moving across the line of sight needs the lead supplied by the
     * Body; the same target with the test-only lead flag off should miss. */
    private static void leadHitsMovingTarget(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        server.runCommand("difficulty peaceful");
        server.runCommand("kill @e[type=!player]");
        arena(server);
        server.runCommand("clear @p");
        server.runCommand("give @p minecraft:bow");
        server.runCommand("give @p minecraft:arrow 24");
        int px = X - 4, pz = Z + 4;
        server.runCommand("tp @p " + px + ".5 " + Y + " " + pz + ".5");
        context.waitFor(client -> client.player.onGround() && countItem(client, "arrow") == 24, 200);
        int leadHits = 0;
        int plainHits = 0;
        final int shots = 3;
        try {
            for (int shot = 0; shot < shots; shot++) {
                String plainId = movingTarget(context, server, px, pz);
                JsonObject plain = new JsonObject();
                plain.addProperty("target_id", plainId);
                plain.addProperty("weapon", "bow");
                plain.addProperty("max_range", 64);
                plain.addProperty("lead", false); // comparison control used only by this GameTest
                JsonObject withoutLead = body.command("rangedAttack", plain, 300);
                JsonObject plainShot = awaitShot(context, body, 120);
                withoutLead.add("last_shot", plainShot);
                if (!"missed".equals(plainShot.get("outcome").getAsString())) plainHits++;
                check(withoutLead.has("lead_blocks") && withoutLead.get("lead_blocks").getAsDouble() < 0.01,
                    "lead-off shot did not report zero lead: " + withoutLead);
                server.runCommand("kill @e[tag=lead]");
                server.runCommand("kill @e[type=minecraft:arrow]");

                String leadId = movingTarget(context, server, px, pz);
                JsonObject aimed = new JsonObject();
                aimed.addProperty("target_id", leadId);
                aimed.addProperty("weapon", "bow");
                aimed.addProperty("max_range", 64);
                JsonObject withLead = body.command("rangedAttack", aimed, 300);
                JsonObject leadShot = awaitShot(context, body, 120);
                withLead.add("last_shot", leadShot);
                if (!"missed".equals(leadShot.get("outcome").getAsString())) leadHits++;
                System.out.println("MINEBOT_LEAD shot=" + shot + " off=" + shotSummary(withoutLead) + " on=" + shotSummary(withLead));
                check(withLead.has("lead_blocks") && withLead.get("lead_blocks").getAsDouble() > 0.1
                        && withLead.has("predicted_target_pos"),
                    "moving-target shot reported no lead: " + withLead);
                server.runCommand("kill @e[tag=lead]");
                server.runCommand("kill @e[type=minecraft:arrow]");
            }
            check(plainHits == 0 && leadHits >= 2,
                "moving-target comparison: lead hits " + leadHits + "/" + shots + ", lead-off hits " + plainHits + "/" + shots);
            System.out.println("MINEBOT_RANGED PASS leadHitsMovingTarget lead_hits=" + leadHits + "/" + shots
                + " lead_off_hits=" + plainHits + "/" + shots);
        } finally {
            server.runCommand("setblock " + (px + 30) + " " + (Y - 4) + " " + (pz - 12) + " minecraft:air");
            server.runCommand("kill @e[tag=lead]");
            server.runCommand("kill @e[type=minecraft:arrow]");
            arena(server);
        }
    }

    /** The outcome of the last arrow, as the Body follows it after the shot ended (last_shot). */
    private static JsonObject awaitShot(ClientGameTestContext context, BodyIpc body, int ticks) {
        JsonObject shot = null;
        for (int tick = 0; tick <= ticks; tick += 2) {
            JsonObject state = body.state();
            shot = state.has("last_shot") ? state.getAsJsonObject("last_shot") : null;
            if (shot != null && !"in_flight".equals(shot.get("outcome").getAsString())) return shot;
            context.waitTicks(2);
        }
        throw new AssertionError("the last shot never resolved: " + shot);
    }

    /** User 2026-11-05 (end run-10): one shot planted the Body ~2 s. The action
     * now ends at the release, and what the arrow did reaches the caller
     * afterwards as last_shot in the state: a crystal and a golem. */
    private static void rangedEndsAtRelease(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        server.runCommand("difficulty peaceful");
        server.runCommand("kill @e[type=!player]");
        arena(server);
        server.runCommand("clear @p");
        server.runCommand("give @p minecraft:bow");
        server.runCommand("give @p minecraft:arrow 8");
        int px = X - 4, pz = Z + 4;
        server.runCommand("tp @p " + px + ".5 " + Y + " " + pz + ".5");
        context.waitFor(client -> client.player.onGround() && countItem(client, "arrow") == 8, 200);
        StringBuilder summary = new StringBuilder();
        try {
            int[] at = {px + 16, Y + 1, pz};
            server.runCommand("setblock " + at[0] + " " + Y + " " + at[2] + " minecraft:bedrock");
            server.runCommand("summon minecraft:end_crystal " + at[0] + ".5 " + at[1] + " " + at[2] + ".5 {ShowBottom:0b}");
            context.waitFor(client -> crystalNear(client, at) != null, 100);
            String crystal = context.computeOnClient(client -> crystalNear(client, at).getUUID().toString());
            server.runCommand("summon minecraft:iron_golem " + (px + 12) + ".5 " + Y + " " + (pz + 6) + ".5 {NoAI:1b,Silent:1b,Tags:[\"shot\"]}");
            context.waitFor(client -> !client.level.getEntities(client.player, client.player.getBoundingBox().inflate(32),
                entity -> entity.getType() == net.minecraft.world.entity.EntityType.IRON_GOLEM).isEmpty(), 100);
            String golem = context.computeOnClient(client -> client.level.getEntities(client.player,
                client.player.getBoundingBox().inflate(32), entity -> entity.getType() == net.minecraft.world.entity.EntityType.IRON_GOLEM)
                .stream().findFirst().orElseThrow().getUUID().toString());
            for (String id : new String[] {crystal, golem}) {
                JsonObject params = new JsonObject();
                params.addProperty("target_id", id);
                params.addProperty("weapon", "bow");
                params.addProperty("max_range", 64);
                int[] tick = {0};
                int[] lastDrawn = {-1};
                JsonObject terminal = body.terminal(body.startCommand("rangedAttack", params, 300), 340, () -> {
                    if (context.computeOnClient(client -> client.player.isUsingItem())) lastDrawn[0] = tick[0];
                    tick[0]++;
                });
                int afterRelease = tick[0] - lastDrawn[0];
                JsonObject shot = awaitShot(context, body, 120);
                summary.append(id.equals(crystal) ? "crystal" : "golem").append(": ").append(terminal.get("reason"))
                    .append(" ").append(afterRelease).append(" ticks after release, last_shot ").append(shot).append("; ");
                check("completed".equals(terminal.get("status").getAsString()) && "arrow_released".equals(terminal.get("reason").getAsString())
                    && terminal.get("shots").getAsInt() == 1, "the shot ended " + terminal.get("status") + " " + terminal.get("reason"));
                check(lastDrawn[0] >= 0 && afterRelease <= 10, "the action ended " + afterRelease + " ticks after the release");
                check(!"missed".equals(shot.get("outcome").getAsString()), "the arrow missed: " + shot);
            }
            System.out.println("MINEBOT_RANGED PASS rangedEndsAtRelease " + summary);
        } finally {
            server.runCommand("kill @e[type=end_crystal]");
            server.runCommand("kill @e[type=iron_golem]");
            server.runCommand("kill @e[type=minecraft:arrow]");
            arena(server);
        }
    }

    /** A living target beyond max_range is not walked to: the action fails
     * out_of_range with the distance and the Body stays where it stood. */
    private static void rangedOutOfRangeHonest(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        server.runCommand("difficulty peaceful");
        server.runCommand("kill @e[type=!player]");
        arena(server);
        server.runCommand("clear @p");
        server.runCommand("give @p minecraft:bow");
        server.runCommand("give @p minecraft:arrow 8");
        int px = X - 4, pz = Z + 4;
        server.runCommand("tp @p " + px + ".5 " + Y + " " + pz + ".5");
        context.waitFor(client -> client.player.onGround() && countItem(client, "arrow") == 8, 200);
        try {
            server.runCommand(fill(px + 1, Y - 1, pz - 1, px + 40, Y - 1, pz + 1, "stone"));
            server.runCommand("summon minecraft:iron_golem " + (px + 36) + ".5 " + Y + " " + pz + ".5 {NoAI:1b,Silent:1b}");
            context.waitFor(client -> !client.level.getEntities(client.player, client.player.getBoundingBox().inflate(48),
                entity -> entity.getType() == net.minecraft.world.entity.EntityType.IRON_GOLEM).isEmpty(), 100);
            String golem = context.computeOnClient(client -> client.level.getEntities(client.player,
                client.player.getBoundingBox().inflate(48), entity -> entity.getType() == net.minecraft.world.entity.EntityType.IRON_GOLEM)
                .stream().findFirst().orElseThrow().getUUID().toString());
            double[] start = context.computeOnClient(client -> new double[] {client.player.getX(), client.player.getZ()});
            JsonObject params = new JsonObject();
            params.addProperty("target_id", golem);
            params.addProperty("weapon", "bow");
            params.addProperty("max_range", 24);
            double[] farthest = {0.0};
            JsonObject terminal = body.terminal(body.startCommand("rangedAttack", params, 300), 340, () -> {
                double moved = context.computeOnClient(client -> Math.hypot(client.player.getX() - start[0], client.player.getZ() - start[1]));
                farthest[0] = Math.max(farthest[0], moved);
            });
            String summary = "status=" + terminal.get("status") + " reason=" + terminal.get("reason") + " target_distance="
                + terminal.get("target_distance") + " max_range=" + terminal.get("max_range") + " shots=" + terminal.get("shots")
                + " moved=" + farthest[0];
            check("failed".equals(terminal.get("status").getAsString()) && "out_of_range".equals(terminal.get("reason").getAsString())
                && terminal.has("target_distance") && terminal.get("target_distance").getAsDouble() > 24.0
                && terminal.get("shots").getAsInt() == 0 && farthest[0] < 0.3, "an out-of-range golem: " + summary);
            System.out.println("MINEBOT_RANGED PASS rangedOutOfRangeHonest " + summary);
        } finally {
            server.runCommand("kill @e[type=iron_golem]");
            server.runCommand(fill(px + 1, Y - 1, pz - 1, px + 40, Y - 1, pz + 1, "air"));
            arena(server);
        }
    }

    // keep_moving cases: a floating platform 26 by 26, centred on (KX, KZ), feet at KY.
    private static final int KX = 114, KY = 160, KZ = 0;

    private static void keepMovingPlatform(ClientGameTestContext context, TestServerContext server) {
        server.runCommand("forceload add " + (KX - 24) + " " + (KZ - 24) + " " + (KX + 24) + " " + (KZ + 24));
        server.runCommand(fill(KX - 16, KY - 1, KZ - 16, KX + 16, KY + 4, KZ + 16, "air"));
        server.runCommand(fill(KX - 13, KY - 1, KZ - 13, KX + 12, KY - 1, KZ + 12, "stone"));
        server.runCommand("tp @p " + KX + ".5 " + KY + " " + KZ + ".5");
        context.waitFor(client -> client.player.onGround() && client.player.blockPosition().getY() == KY
            && client.player.blockPosition().getX() == KX && client.player.blockPosition().getZ() == KZ, 300);
        context.waitTicks(20);
    }

    private static void keepMovingCleanup(TestServerContext server) {
        server.runCommand("kill @e[type=minecraft:area_effect_cloud]");
        server.runCommand(fill(KX - 16, KY - 1, KZ - 16, KX + 16, KY + 4, KZ + 16, "air"));
        server.runCommand("forceload remove " + (KX - 24) + " " + (KZ - 24) + " " + (KX + 24) + " " + (KZ + 24));
        server.runCommand("tp @p " + (X + 3) + ".5 " + Y + " " + (Z + 4) + ".5");
    }

    /** keep_moving (user 2026-11-05: 玩家一直会处于移动甚至奔跑中): a minute
     * sprinting about a platform over the void, radius 12 reaching past its
     * edges, with a harming cloud and a magma patch inside. Sampled every tick
     * on the server: the Body never falls, never stands still for more than
     * a second, visits many points, is in the cloud or on magma at most a
     * moment, and stops at once when cancelled. */
    private static void keepMovingStaysSafe(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        server.runCommand("difficulty peaceful");
        server.runCommand("kill @e[type=!player]");
        server.runCommand("clear @p");
        server.runCommand("effect clear @p");
        keepMovingPlatform(context, server);
        int cloudX = KX - 5, cloudZ = KZ - 4;
        int magmaX = KX + 5, magmaZ = KZ + 5;
        try {
            server.runCommand(fill(magmaX - 1, KY - 1, magmaZ - 1, magmaX + 1, KY - 1, magmaZ + 1, "magma_block"));
            server.runCommand("summon minecraft:area_effect_cloud " + cloudX + ".5 " + KY + " " + cloudZ + ".5"
                + " {Radius:3.0f,RadiusPerTick:0.0f,Duration:6000,WaitTime:0,ReapplicationDelay:20,potion_contents:{custom_effects:[{id:\"minecraft:instant_damage\",amplifier:0b,duration:1}]}}");
            server.runCommand("effect give @p minecraft:regeneration infinite 1 true");
            context.waitFor(client -> !client.level.getEntitiesOfClass(net.minecraft.world.entity.AreaEffectCloud.class,
                client.player.getBoundingBox().inflate(16)).isEmpty(), 100);
            JsonObject params = new JsonObject();
            params.addProperty("radius", 12);
            params.addProperty("sprint", true);
            String id = body.startCommand("keepMoving", params, 1600);
            final int ticks = 1200;
            double lowest = Double.MAX_VALUE;
            int still = 0, longestStill = 0, inCloud = 0, longestCloud = 0, cloudTicks = 0, onMagma = 0, longestMagma = 0;
            int sprintTicks = 0;
            double travelled = 0.0;
            java.util.Set<String> cells = new java.util.HashSet<>();
            double[] last = null;
            for (int tick = 0; tick < ticks; tick++) {
                context.waitTick();
                double[] at = server.computeOnServer(s -> {
                    var player = s.getPlayerList().getPlayers().get(0);
                    return new double[] {player.getX(), player.getY(), player.getZ(), player.isSprinting() ? 1 : 0,
                        s.overworld().getBlockState(player.getOnPos()).is(net.minecraft.world.level.block.Blocks.MAGMA_BLOCK) ? 1 : 0};
                });
                check(body.terminalIfAny(id) == null, "keepMoving ended by itself at tick " + tick);
                lowest = Math.min(lowest, at[1]);
                if (last != null) {
                    double step = Math.hypot(at[0] - last[0], at[2] - last[2]);
                    travelled += step;
                    still = step < 0.02 ? still + 1 : 0;
                    longestStill = Math.max(longestStill, still);
                }
                last = at;
                if (at[3] > 0) sprintTicks++;
                cells.add(Math.floorDiv((int) Math.floor(at[0]), 2) + "," + Math.floorDiv((int) Math.floor(at[2]), 2));
                boolean cloud = Math.hypot(at[0] - (cloudX + 0.5), at[2] - (cloudZ + 0.5)) <= 3.0;
                inCloud = cloud ? inCloud + 1 : 0;
                if (cloud) cloudTicks++;
                longestCloud = Math.max(longestCloud, inCloud);
                onMagma = at[4] > 0 ? onMagma + 1 : 0;
                longestMagma = Math.max(longestMagma, onMagma);
            }
            body.cancel(id);
            int[] stopTicks = {0};
            JsonObject terminal = body.terminal(id, 100, () -> stopTicks[0]++);
            JsonObject moving = terminal.has("keep_moving") ? terminal.getAsJsonObject("keep_moving") : new JsonObject();
            int visited = moving.has("points_visited") ? moving.get("points_visited").getAsInt() : -1;
            float health = context.computeOnClient(client -> client.player.getHealth());
            String summary = "ticks=" + ticks + " travelled=" + Math.round(travelled) + " points_visited=" + visited
                + " cells2x2=" + cells.size() + " longest_still_ticks=" + longestStill + " lowest_y=" + lowest
                + " cloud_ticks=" + cloudTicks + " longest_in_cloud=" + longestCloud + " longest_on_magma=" + longestMagma
                + " sprint_ticks=" + sprintTicks + " health=" + health + " stop=" + terminal.get("status") + "/"
                + terminal.get("reason") + " in " + stopTicks[0] + " ticks; keep_moving=" + moving;
            System.out.println("MINEBOT_KEEP keepMovingStaysSafe " + summary);
            check(lowest >= KY - 0.6, "the Body fell: " + summary);
            check(longestStill <= 20, "the Body stood still too long: " + summary);
            check(visited >= 15 && cells.size() >= 30, "too few points: " + summary);
            check(longestCloud <= 20 && longestMagma <= 10, "the Body stayed in the damage: " + summary);
            check(sprintTicks >= ticks / 3, "the Body hardly sprinted: " + summary);
            check("canceled".equals(terminal.get("status").getAsString()) && stopTicks[0] <= 10, "the cancel was not prompt: " + summary);
            System.out.println("MINEBOT_KEEP PASS keepMovingStaysSafe " + summary);
        } finally {
            server.runCommand("effect clear @p");
            keepMovingCleanup(server);
        }
    }

    // Legs, hands, eyes (body-channels-20261105 §6.1, §8): the leg mode is held by the Body.
    private static JsonObject movement(String mode, int radius) {
        JsonObject params = new JsonObject();
        params.addProperty("mode", mode);
        if (mode.equals("keep_moving")) {
            params.addProperty("radius", radius);
            params.addProperty("sprint", true);
        }
        return params;
    }

    private static JsonObject gaze(boolean down) {
        JsonObject params = new JsonObject();
        params.addProperty("look_down", down);
        return params;
    }

    private static String golemId(ClientGameTestContext context) {
        return context.computeOnClient(client -> client.level.getEntitiesOfClass(
            net.minecraft.world.entity.animal.golem.IronGolem.class, client.player.getBoundingBox().inflate(48))
            .stream().findFirst().orElseThrow().getUUID().toString());
    }

    private static float golemHealth(TestServerContext server) {
        return server.computeOnServer(s -> s.overworld().getEntitiesOfClass(
            net.minecraft.world.entity.animal.golem.IronGolem.class, new net.minecraft.world.phys.AABB(-200, 0, -200, 300, 300, 300))
            .stream().findFirst().map(g -> g.getHealth()).orElse(-1.0f));
    }

    /** Server positions, tick by tick: the longest run of ticks the Body stood still. */
    private static final class Stillness implements Runnable {
        final TestServerContext server;
        double[] last;
        int still, longest, ticks;
        double travelled;
        Stillness(TestServerContext server) { this.server = server; }
        public void run() {
            double[] at = serverPlayer(server);
            if (last != null) {
                double step = Math.hypot(at[0] - last[0], at[2] - last[2]);
                travelled += step;
                still = step < 0.02 ? still + 1 : 0;
                longest = Math.max(longest, still);
            }
            last = at;
            ticks++;
        }
        void waitTicks(ClientGameTestContext context, int n) {
            for (int i = 0; i < n; i++) { context.waitTick(); run(); }
        }
        public String toString() {
            return "ticks=" + ticks + " travelled=" + Math.round(travelled) + " longest_still=" + longest;
        }
    }

    /** §8.1: keep moving, four shots at a golem and one eat: the legs never stop, every shot flies true. */
    private static void legsKeepGoingThroughHands(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        server.runCommand("difficulty easy");
        server.runCommand("kill @e[type=!player]");
        server.runCommand("clear @p");
        server.runCommand("effect clear @p");
        keepMovingPlatform(context, server);
        int gx = KX, gz = KZ + 18;
        try {
            server.runCommand(fill(gx - 1, KY - 1, gz - 1, gx + 1, KY - 1, gz + 1, "stone"));
            server.runCommand("summon minecraft:iron_golem " + gx + ".5 " + KY + " " + gz + ".5 {NoAI:1b,Silent:1b}");
            server.runCommand("give @p minecraft:bow");
            server.runCommand("give @p minecraft:arrow 32");
            server.runCommand("give @p minecraft:cooked_beef 8");
            server.runCommand("effect give @p minecraft:resistance infinite 4 true");
            context.waitFor(client -> countItem(client, "cooked_beef") == 8 && !client.level.getEntitiesOfClass(
                net.minecraft.world.entity.animal.golem.IronGolem.class, client.player.getBoundingBox().inflate(40)).isEmpty(), 200);
            String golem = golemId(context);
            float before = golemHealth(server);
            JsonObject set = body.command("setMovement", movement("keep_moving", 10), 40);
            check("movement_set".equals(set.get("reason").getAsString()), "setMovement: " + set);
            Stillness legs = new Stillness(server);
            legs.waitTicks(context, 60);
            StringBuilder shots = new StringBuilder();
            for (int i = 0; i < 4; i++) {
                JsonObject params = new JsonObject();
                params.addProperty("target_id", golem);
                params.addProperty("weapon", "bow");
                params.addProperty("max_range", 64);
                JsonObject shot = body.terminal(body.startCommand("rangedAttack", params, 300), 340, legs);
                shots.append(shot.get("reason").getAsString()).append(' ');
                check("arrow_released".equals(shot.get("reason").getAsString()), "shot " + i + ": " + shot.get("reason") + " " + legs);
                legs.waitTicks(context, 25);
            }
            server.runOnServer(s -> s.getPlayerList().getPlayers().get(0).getFoodData().setFoodLevel(10));
            legs.waitTicks(context, 5);
            JsonObject eat = new JsonObject();
            eat.addProperty("item", "cooked_beef");
            eat.addProperty("mode", "continuous");
            eat.addProperty("ticks", 80);
            JsonObject ate = body.terminal(body.startCommand("useItem", eat, 200), 240, legs);
            legs.waitTicks(context, 20);
            float after = golemHealth(server);
            JsonObject state = body.state();
            String summary = legs + " shots=" + shots + "golem " + before + "->" + after + " eat=" + ate.get("reason")
                + " movement=" + state.get("movement");
            System.out.println("MINEBOT_LEGS legsKeepGoingThroughHands " + summary);
            check(legs.longest <= 20, "the legs stopped: " + summary);
            check(after <= before - 18.0f, "fewer than three hits: " + summary);
            check("item_consumed".equals(ate.get("reason").getAsString()), "not eaten: " + summary);
            check("keep_moving".equals(state.getAsJsonObject("movement").get("mode").getAsString()), "the mode ended: " + summary);
            System.out.println("MINEBOT_LEGS PASS legsKeepGoingThroughHands " + summary);
        } finally {
            body.command("setMovement", movement("stand", 0), 40);
            server.runCommand("effect clear @p");
            server.runCommand("kill @e[type=iron_golem]");
            server.runCommand("kill @e[type=minecraft:arrow]");
            server.runCommand(fill(gx - 1, KY - 1, gz - 1, gx + 1, KY - 1, gz + 1, "air"));
            server.runCommand("difficulty peaceful");
            keepMovingCleanup(server);
        }
    }

    /** A leg action (a walk) holds the legs; when it ends the mode goes on by itself, no new command. */
    private static void legsResumeAfterLegAction(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        server.runCommand("difficulty peaceful");
        server.runCommand("kill @e[type=!player]");
        keepMovingPlatform(context, server);
        try {
            body.command("setMovement", movement("keep_moving", 10), 40);
            Stillness legs = new Stillness(server);
            legs.waitTicks(context, 40);
            String walk = body.startObjective("near:" + (KX + 6) + ":" + KY + ":" + (KZ + 6) + ":1", 300);
            boolean[] held = {false};
            JsonObject walked = body.terminal(walk, 340, () -> {
                JsonObject movement = body.state().getAsJsonObject("movement");
                if (movement.has("held_by_action") && movement.get("held_by_action").getAsBoolean()) held[0] = true;
            });
            Stillness after = new Stillness(server);
            after.waitTicks(context, 60);
            JsonObject movement = body.state().getAsJsonObject("movement");
            String summary = "walk=" + walked.get("status") + "/" + walked.get("reason") + " held_during_walk=" + held[0]
                + " after: " + after + " movement=" + movement;
            System.out.println("MINEBOT_LEGS legsResumeAfterLegAction " + summary);
            check("completed".equals(walked.get("status").getAsString()), "the walk failed: " + summary);
            check(held[0], "the walk did not hold the legs: " + summary);
            check(after.travelled >= 3.0 && after.longest <= 20, "the legs did not go on: " + summary);
            check("keep_moving".equals(movement.get("mode").getAsString()) && !movement.get("held_by_action").getAsBoolean(),
                "the mode is not back: " + summary);
            System.out.println("MINEBOT_LEGS PASS legsResumeAfterLegAction " + summary);
        } finally {
            body.command("setMovement", movement("stand", 0), 40);
            keepMovingCleanup(server);
        }
    }

    /** §8.3: walled in, the mode ends by itself and says where and why; it never stands silently. */
    private static void legsEndWhenWalledIn(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        server.runCommand("difficulty peaceful");
        server.runCommand("kill @e[type=!player]");
        keepMovingPlatform(context, server);
        try {
            server.runCommand(fill(KX - 1, KY, KZ - 1, KX + 1, KY + 2, KZ + 1, "glass"));
            server.runCommand(fill(KX, KY, KZ, KX, KY + 1, KZ, "air"));
            context.waitTicks(10);
            body.command("setMovement", movement("keep_moving", 10), 40);
            int[] ticks = {0};
            JsonObject ended = body.bodyEvent("movement_ended", 600, () -> ticks[0]++);
            JsonObject data = ended.getAsJsonObject("data");
            JsonObject movement = body.state().getAsJsonObject("movement");
            String summary = "after " + ticks[0] + " ticks reason=" + data.get("reason") + " position=" + data.get("position")
                + " movement=" + movement;
            System.out.println("MINEBOT_LEGS legsEndWhenWalledIn " + summary);
            check(data.has("reason") && data.has("position"), "no reason or position: " + summary);
            check("stand".equals(movement.get("mode").getAsString()), "still a mode: " + summary);
            System.out.println("MINEBOT_LEGS PASS legsEndWhenWalledIn " + summary);
        } finally {
            body.command("setMovement", movement("stand", 0), 40);
            server.runCommand(fill(KX - 1, KY, KZ - 1, KX + 1, KY + 2, KZ + 1, "air"));
            keepMovingCleanup(server);
        }
    }

    /** §6.1 eyes: the look-down setting holds while standing; a shot aims and the eyes go back down. */
    private static void gazeHoldsStanding(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        server.runCommand("difficulty peaceful");
        server.runCommand("kill @e[type=!player]");
        arena(server);
        server.runCommand("clear @p");
        server.runCommand("give @p minecraft:bow");
        server.runCommand("give @p minecraft:arrow 8");
        int px = X - 4, pz = Z + 4;
        server.runCommand("tp @p " + px + ".5 " + Y + " " + pz + ".5 0 0");
        context.waitFor(client -> client.player.onGround() && countItem(client, "arrow") == 8, 200);
        try {
            server.runCommand("summon minecraft:iron_golem " + (px + 12) + ".5 " + Y + " " + pz + ".5 {NoAI:1b,Silent:1b}");
            context.waitFor(client -> !client.level.getEntitiesOfClass(net.minecraft.world.entity.animal.golem.IronGolem.class,
                client.player.getBoundingBox().inflate(32)).isEmpty(), 100);
            String golem = golemId(context);
            body.command("setGaze", gaze(true), 40);
            float lowestIdle = 90.0f;
            for (int i = 0; i < 40; i++) {
                context.waitTick();
                if (i >= 2) lowestIdle = Math.min(lowestIdle, context.computeOnClient(client -> client.player.getXRot()));
            }
            JsonObject params = new JsonObject();
            params.addProperty("target_id", golem);
            params.addProperty("weapon", "bow");
            params.addProperty("max_range", 64);
            float[] aimed = {90.0f};
            JsonObject shot = body.terminal(body.startCommand("rangedAttack", params, 300), 340,
                () -> aimed[0] = Math.min(aimed[0], context.computeOnClient(client -> client.player.getXRot())));
            int back = -1;
            for (int i = 0; i < 20 && back < 0; i++) {
                context.waitTick();
                if (context.computeOnClient(client -> client.player.getXRot()) >= 60.0f) back = i;
            }
            JsonObject state = body.state();
            String summary = "idle_lowest_pitch=" + lowestIdle + " aimed_pitch=" + aimed[0] + " shot=" + shot.get("reason")
                + " back_down_after=" + back + " gaze_look_down=" + state.get("gaze_look_down");
            System.out.println("MINEBOT_LEGS gazeHoldsStanding " + summary);
            check(lowestIdle >= 60.0f, "standing did not look down: " + summary);
            check(aimed[0] < 30.0f, "the shot did not aim: " + summary);
            check(back >= 0 && back <= 10, "the eyes did not go back down: " + summary);
            check(state.get("gaze_look_down").getAsBoolean(), "the setting is gone: " + summary);
            System.out.println("MINEBOT_LEGS PASS gazeHoldsStanding " + summary);
        } finally {
            body.command("setGaze", gaze(false), 40);
            server.runCommand("kill @e[type=iron_golem]");
            server.runCommand("kill @e[type=minecraft:arrow]");
            arena(server);
        }
    }

    /** Death resets the legs to stand and the eyes to level, and the death facts say so. */
    private static void legsResetOnDeath(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        server.runCommand("difficulty peaceful");
        server.runCommand("kill @e[type=!player]");
        keepMovingPlatform(context, server);
        try {
            body.command("setMovement", movement("keep_moving", 10), 40);
            body.command("setGaze", gaze(true), 40);
            context.waitTicks(40);
            server.runCommand("kill @p");
            JsonObject death = body.bodyEvent("death", 100, () -> {}).getAsJsonObject("data");
            body.bodyEvent("respawned", 400, () -> {});
            context.waitTicks(20);
            JsonObject state = body.state();
            String summary = "movement_reset=" + death.get("movement_reset") + " gaze_reset=" + death.get("gaze_reset")
                + " movement=" + state.get("movement") + " gaze_look_down=" + state.get("gaze_look_down");
            System.out.println("MINEBOT_LEGS legsResetOnDeath " + summary);
            check(death.has("movement_reset") && death.get("movement_reset").getAsBoolean()
                && death.has("gaze_reset") && death.get("gaze_reset").getAsBoolean(), "the death facts: " + summary);
            check("stand".equals(state.getAsJsonObject("movement").get("mode").getAsString())
                && !state.get("gaze_look_down").getAsBoolean(), "not reset: " + summary);
            System.out.println("MINEBOT_LEGS PASS legsResetOnDeath " + summary);
        } finally {
            keepMovingCleanup(server);
            context.waitFor(client -> client.player.isAlive() && client.player.onGround(), 400);
        }
    }

    /** Feasibility probe (body-channels-20261105 §9 step 4): can a hand act
     * while Baritone's legs keep going? keepMoving runs; from the test the bow
     * is drawn 22 ticks and released, aimed at a golem off the platform, and
     * cooked beef is eaten. Measured on the server: the Body's speed while
     * drawing and eating, the arrow's launch yaw/pitch against the aim, the
     * golem's health, the beef count. A standing shot is the control. Prints
     * numbers; checks only that keepMoving was not ended by the hand. */
    private static void handsWhileLegsMove(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        server.runCommand("difficulty peaceful");
        server.runCommand("kill @e[type=!player]");
        server.runCommand("clear @p");
        server.runCommand("effect clear @p");
        keepMovingPlatform(context, server);
        int gx = KX, gz = KZ + 18;
        try {
            server.runCommand(fill(gx - 1, KY - 1, gz - 1, gx + 1, KY - 1, gz + 1, "stone"));
            server.runCommand("summon minecraft:iron_golem " + gx + ".5 " + KY + " " + gz + ".5 {NoAI:1b,Silent:1b}");
            server.runCommand("give @p minecraft:bow");
            server.runCommand("give @p minecraft:arrow 32");
            server.runCommand("give @p minecraft:cooked_beef 8");
            server.runCommand("effect give @p minecraft:resistance infinite 4 true");
            context.waitFor(client -> countItem(client, "cooked_beef") == 8 && !client.level.getEntitiesOfClass(
                net.minecraft.world.entity.animal.golem.IronGolem.class, client.player.getBoundingBox().inflate(40)).isEmpty(), 200);
            StringBuilder out = new StringBuilder();
            out.append(probeShot(context, server, "standing")).append(" | ");
            JsonObject params = new JsonObject();
            params.addProperty("radius", 10);
            params.addProperty("sprint", true);
            String id = body.startCommand("keepMoving", params, 3000);
            context.waitTicks(60);
            for (int i = 0; i < 8; i++) {
                out.append(probeShot(context, server, (i < 2 ? "moving" : "moving_snap") + i, i >= 2)).append(" | ");
                check(body.terminalIfAny(id) == null, "keepMoving ended during the shot: " + out);
                context.waitTicks(15);
            }
            // Peaceful refills food: eat on easy, with no mob near the sky platform.
            server.runCommand("difficulty easy");
            server.runOnServer(s -> s.getPlayerList().getPlayers().get(0).getFoodData().setFoodLevel(10));
            context.waitTicks(10);
            server.runCommand("kill @e[type=!player,type=!iron_golem]");
            out.append(probeEat(context, server)).append(" | ");
            check(body.terminalIfAny(id) == null, "keepMoving ended during the eat: " + out);
            body.cancel(id);
            body.terminal(id, 100, () -> {});
            System.out.println("MINEBOT_CHANNELS PROBE handsWhileLegsMove " + out);
        } finally {
            server.runCommand("effect clear @p");
            server.runCommand("kill @e[type=iron_golem]");
            server.runCommand("kill @e[type=minecraft:arrow]");
            server.runCommand(fill(gx - 1, KY - 1, gz - 1, gx + 1, KY - 1, gz + 1, "air"));
            keepMovingCleanup(server);
        }
    }

    private static double[] serverPlayer(TestServerContext server) {
        return server.computeOnServer(s -> {
            var player = s.getPlayerList().getPlayers().get(0);
            return new double[] {player.getX(), player.getY(), player.getZ(), player.getYRot(), player.getXRot()};
        });
    }

    /** One bow draw from the test thread, aimed every tick at the golem; the legs are whatever runs. */
    private static String probeShot(ClientGameTestContext context, TestServerContext server, String label) {
        return probeShot(context, server, label, false);
    }

    private static String probeShot(ClientGameTestContext context, TestServerContext server, String label, boolean snapAtRelease) {
        float golemBefore = server.computeOnServer(s -> s.overworld().getEntitiesOfClass(
            net.minecraft.world.entity.animal.golem.IronGolem.class, new net.minecraft.world.phys.AABB(KX - 40, KY - 10, KZ - 40, KX + 40, KY + 10, KZ + 40))
            .stream().findFirst().map(g -> g.getHealth()).orElse(-1.0f));
        java.util.Set<Integer> arrowsBefore = server.computeOnServer(s -> new java.util.HashSet<>(s.overworld().getEntitiesOfClass(
            net.minecraft.world.entity.projectile.arrow.Arrow.class, new net.minecraft.world.phys.AABB(KX - 60, KY - 30, KZ - 60, KX + 60, KY + 30, KZ + 60))
            .stream().map(a -> a.getId()).toList()));
        float[] aim = new float[2];
        double moved = 0.0;
        double[] last = serverPlayer(server);
        for (int tick = 0; tick <= 22; tick++) {
            final int t = tick;
            context.runOnClient(client -> {
                var golem = client.level.getEntitiesOfClass(net.minecraft.world.entity.animal.golem.IronGolem.class,
                    client.player.getBoundingBox().inflate(48)).stream().findFirst().orElseThrow();
                double dx = golem.getX() - client.player.getX(), dz = golem.getZ() - client.player.getZ();
                double dy = golem.getY() + 1.4 - client.player.getEyeY();
                double h = Math.hypot(dx, dz);
                aim[0] = (float) (Math.atan2(dz, dx) * 180.0 / Math.PI) - 90.0f;
                aim[1] = (float) (-Math.atan2(dy, h) * 180.0 / Math.PI) - (float) (h * 0.12);
                client.player.setYRot(aim[0]);
                client.player.setXRot(aim[1]);
                if (t == 0) {
                    for (int slot = 0; slot < 9; slot++) {
                        if (client.player.getInventory().getItem(slot).is(net.minecraft.world.item.Items.BOW)) {
                            client.player.getInventory().setSelectedSlot(slot);
                        }
                    }
                    client.options.keyUse.setDown(true);
                    client.gameMode.useItem(client.player, net.minecraft.world.InteractionHand.MAIN_HAND);
                } else if (t == 22) {
                    client.options.keyUse.setDown(false);
                    if (snapAtRelease) {
                        // A player's look is its aim: the server takes the arrow's
                        // direction from the last rotation it was sent.
                        client.getConnection().send(new net.minecraft.network.protocol.game.ServerboundMovePlayerPacket.Rot(
                            aim[0], aim[1], client.player.onGround(), client.player.horizontalCollision));
                    }
                    client.gameMode.releaseUsingItem(client.player);
                }
            });
            context.waitTick();
            double[] now = serverPlayer(server);
            if (t < 22) moved += Math.hypot(now[0] - last[0], now[2] - last[2]);
            last = now;
        }
        boolean drawn = true;
        context.waitTicks(2);
        double[] launch = server.computeOnServer(s -> s.overworld().getEntitiesOfClass(
            net.minecraft.world.entity.projectile.arrow.Arrow.class, new net.minecraft.world.phys.AABB(KX - 60, KY - 30, KZ - 60, KX + 60, KY + 30, KZ + 60))
            .stream().filter(a -> !arrowsBefore.contains(a.getId())).findFirst()
            .map(a -> { var v = a.getDeltaMovement(); return new double[] {Math.atan2(v.z, v.x) * 180.0 / Math.PI - 90.0,
                -Math.atan2(v.y, Math.hypot(v.x, v.z)) * 180.0 / Math.PI, v.length()}; }).orElse(null));
        context.waitTicks(40);
        float golemAfter = server.computeOnServer(s -> s.overworld().getEntitiesOfClass(
            net.minecraft.world.entity.animal.golem.IronGolem.class, new net.minecraft.world.phys.AABB(KX - 40, KY - 10, KZ - 40, KX + 40, KY + 10, KZ + 40))
            .stream().findFirst().map(g -> g.getHealth()).orElse(-1.0f));
        String yawErr = launch == null ? "no_arrow" : String.format("%.1f", net.minecraft.util.Mth.wrapDegrees((float) (launch[0] - aim[0])));
        String pitchErr = launch == null ? "-" : String.format("%.1f", launch[1] - aim[1]);
        return label + ": moved_while_drawing=" + String.format("%.2f", moved) + " arrow_yaw_err=" + yawErr
            + " arrow_pitch_vs_aim=" + pitchErr + " speed=" + (launch == null ? "-" : String.format("%.2f", launch[2]))
            + " golem " + golemBefore + "->" + golemAfter;
    }

    /** Eat cooked beef from the test thread while the legs are whatever runs. */
    private static String probeEat(ClientGameTestContext context, TestServerContext server) {
        int food = context.computeOnClient(client -> client.player.getFoodData().getFoodLevel());
        int before = countItem(context, "cooked_beef");
        double moved = 0.0;
        double[] last = serverPlayer(server);
        int ticks = 0;
        for (; ticks < 80; ticks++) {
            final int t = ticks;
            context.runOnClient(client -> {
                if (t == 0) {
                    for (int slot = 0; slot < 9; slot++) {
                        if (client.player.getInventory().getItem(slot).is(net.minecraft.world.item.Items.COOKED_BEEF)) {
                            client.player.getInventory().setSelectedSlot(slot);
                        }
                    }
                }
                client.options.keyUse.setDown(true);
                if (!client.player.isUsingItem()) client.gameMode.useItem(client.player, net.minecraft.world.InteractionHand.MAIN_HAND);
            });
            context.waitTick();
            double[] now = serverPlayer(server);
            moved += Math.hypot(now[0] - last[0], now[2] - last[2]);
            last = now;
            if (countItem(context, "cooked_beef") < before) break;
        }
        context.runOnClient(client -> client.options.keyUse.setDown(false));
        int after = countItem(context, "cooked_beef");
        int foodAfter = context.computeOnClient(client -> client.player.getFoodData().getFoodLevel());
        return "eat: food " + food + "->" + foodAfter + " beef " + before + "->" + after + " in " + ticks
            + " ticks, moved_while_eating=" + String.format("%.2f", moved);
    }

    /** End run-11: the plan's circle was 60 blocks from the Body; every point
     * 4-8 blocks from it was outside the circle and the call ended
     * no_safe_point 34 times. Started 43 blocks outside on a strip that joins
     * the platform (one block higher out there), the Body walks into the
     * circle and then keeps moving inside it. */
    private static void keepMovingFromOutsideArea(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        server.runCommand("difficulty peaceful");
        server.runCommand("kill @e[type=!player]");
        server.runCommand("clear @p");
        server.runCommand("forceload add " + (KX + 12) + " " + (KZ - 8) + " " + (KX + 64) + " " + (KZ + 8));
        keepMovingPlatform(context, server);
        int startX = KX + 55;
        try {
            server.runCommand(fill(KX + 13, KY - 1, KZ - 2, KX + 35, KY - 1, KZ + 2, "stone"));
            server.runCommand(fill(KX + 36, KY, KZ - 2, KX + 60, KY, KZ + 2, "stone"));
            server.runCommand("tp @p " + startX + ".5 " + (KY + 1) + " " + KZ + ".5");
            context.waitFor(client -> client.player.onGround() && client.player.blockPosition().getX() == startX, 300);
            JsonObject params = new JsonObject();
            JsonArray center = new JsonArray();
            center.add(KX + 0.5); center.add(KY); center.add(KZ + 0.5);
            params.add("center", center);
            params.addProperty("radius", 12);
            params.addProperty("sprint", true);
            params.addProperty("look_down", true);
            String id = body.startCommand("keepMoving", params, 1600);
            int enteredAt = -1, insideTicks = 0;
            double lowest = Double.MAX_VALUE;
            for (int tick = 0; tick < 700; tick++) {
                context.waitTick();
                double[] at = server.computeOnServer(s -> {
                    var player = s.getPlayerList().getPlayers().get(0);
                    return new double[] {player.getX(), player.getY(), player.getZ()};
                });
                JsonObject early = body.terminalIfAny(id);
                check(early == null, "keepMoving ended at tick " + tick + ": " + early);
                lowest = Math.min(lowest, at[1]);
                boolean inside = Math.hypot(at[0] - (KX + 0.5), at[2] - (KZ + 0.5)) <= 12.0;
                if (inside && enteredAt < 0) enteredAt = tick;
                if (inside) insideTicks++;
                if (tick % 50 == 0) System.out.println("MINEBOT_KEEP keepMovingFromOutsideArea tick " + tick + " at "
                    + Math.round(at[0] * 10) / 10.0 + "," + Math.round(at[1] * 10) / 10.0 + "," + Math.round(at[2] * 10) / 10.0);
            }
            body.cancel(id);
            JsonObject terminal = body.terminal(id, 100, () -> {});
            JsonObject moving = terminal.has("keep_moving") ? terminal.getAsJsonObject("keep_moving") : new JsonObject();
            int visited = moving.has("points_visited") ? moving.get("points_visited").getAsInt() : -1;
            String summary = "start_outside_by=" + (startX - KX - 12) + " entered_at_tick=" + enteredAt + " inside_ticks=" + insideTicks
                + " points_visited=" + visited + " lowest_y=" + lowest + " stop=" + terminal.get("status") + "/" + terminal.get("reason")
                + " keep_moving=" + moving;
            System.out.println("MINEBOT_KEEP keepMovingFromOutsideArea " + summary);
            check(enteredAt >= 0 && lowest >= KY - 0.6, "the Body did not walk into the circle: " + summary);
            check(visited >= 5, "too few points after entering: " + summary);
            check("canceled".equals(terminal.get("status").getAsString()), "the cancel was not honoured: " + summary);
            System.out.println("MINEBOT_KEEP PASS keepMovingFromOutsideArea " + summary);
        } finally {
            server.runCommand(fill(KX + 13, KY - 1, KZ - 2, KX + 60, KY + 2, KZ + 2, "air"));
            server.runCommand("forceload remove " + (KX + 12) + " " + (KZ - 8) + " " + (KX + 64) + " " + (KZ + 8));
            keepMovingCleanup(server);
        }
    }

    /** End run-25/28: from the obsidian arrival platform, below the island and
     * apart from it, keep_moving toward the island never left the platform and
     * ended cannot_move (area_no_route 22, stalls 15), because the walk never
     * placed a block, while move_to bridged up in 15 s. Here a 5 by 5 platform
     * floats 5 blocks below and 3 blocks apart from a strip joined to the
     * circle's platform; the Body carries cobblestone. It must get into the
     * circle and then walk inside it without building there. */
    private static void keepMovingFromArrivalPlatform(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        server.runCommand("difficulty peaceful");
        server.runCommand("kill @e[type=!player]");
        server.runCommand("clear @p");
        server.runCommand("forceload add " + (KX + 12) + " " + (KZ - 8) + " " + (KX + 40) + " " + (KZ + 8));
        keepMovingPlatform(context, server);
        int px = KX + 30, py = KY - 5;
        try {
            server.runCommand(fill(KX + 13, KY - 1, KZ - 2, KX + 24, KY - 1, KZ + 2, "stone"));
            server.runCommand(fill(px - 2, py - 1, KZ - 2, px + 2, py - 1, KZ + 2, "obsidian"));
            server.runCommand("give @p minecraft:cobblestone 64");
            server.runCommand("tp @p " + px + ".5 " + py + " " + KZ + ".5");
            context.waitFor(client -> client.player.onGround() && client.player.blockPosition().getX() == px
                && client.player.blockPosition().getY() == py, 300);
            JsonObject params = new JsonObject();
            JsonArray center = new JsonArray();
            center.add(KX + 0.5); center.add(KY); center.add(KZ + 0.5);
            params.add("center", center);
            params.addProperty("radius", 10);
            params.addProperty("sprint", true);
            params.addProperty("look_down", true);
            String id = body.startCommand("keepMoving", params, 1600);
            int enteredAt = -1, insideTicks = 0;
            for (int tick = 0; tick < 900; tick++) {
                context.waitTick();
                double[] at = server.computeOnServer(s -> {
                    var player = s.getPlayerList().getPlayers().get(0);
                    return new double[] {player.getX(), player.getY(), player.getZ()};
                });
                JsonObject early = body.terminalIfAny(id);
                check(early == null, "keepMoving ended at tick " + tick + ": " + early);
                check(at[1] > py - 3, "the Body fell off at tick " + tick);
                boolean inside = Math.hypot(at[0] - (KX + 0.5), at[2] - (KZ + 0.5)) <= 10.0;
                if (inside && enteredAt < 0) enteredAt = tick;
                if (inside) insideTicks++;
                if (tick % 50 == 0) System.out.println("MINEBOT_KEEP keepMovingFromArrivalPlatform tick " + tick + " at "
                    + Math.round(at[0] * 10) / 10.0 + "," + Math.round(at[1] * 10) / 10.0 + "," + Math.round(at[2] * 10) / 10.0);
            }
            body.cancel(id);
            JsonObject terminal = body.terminal(id, 100, () -> {});
            JsonObject moving = terminal.has("keep_moving") ? terminal.getAsJsonObject("keep_moving") : new JsonObject();
            int visited = moving.has("points_visited") ? moving.get("points_visited").getAsInt() : -1;
            int built = server.computeOnServer(s -> {
                var level = s.overworld();
                int n = 0;
                for (int x = KX - 12; x <= KX + 12; x++) for (int z = KZ - 12; z <= KZ + 12; z++)
                    for (int y = KY; y <= KY + 3; y++)
                        if (level.getBlockState(new net.minecraft.core.BlockPos(x, y, z)).is(net.minecraft.world.level.block.Blocks.COBBLESTONE)) n++;
                return n;
            });
            String summary = "entered_at_tick=" + enteredAt + " inside_ticks=" + insideTicks + " points_visited=" + visited
                + " built_inside=" + built + " stop=" + terminal.get("status") + "/" + terminal.get("reason") + " keep_moving=" + moving;
            System.out.println("MINEBOT_KEEP keepMovingFromArrivalPlatform " + summary);
            check(enteredAt >= 0, "the Body did not get into the circle: " + summary);
            check(visited >= 3, "too few points after entering: " + summary);
            check(built == 0, "the walk built inside the circle: " + summary);
            check("canceled".equals(terminal.get("status").getAsString()), "the cancel was not honoured: " + summary);
            System.out.println("MINEBOT_KEEP PASS keepMovingFromArrivalPlatform " + summary);
        } finally {
            server.runCommand(fill(KX + 13, KY - 7, KZ - 3, KX + 33, KY + 3, KZ + 3, "air"));
            server.runCommand("clear @p");
            server.runCommand("forceload remove " + (KX + 12) + " " + (KZ - 8) + " " + (KX + 40) + " " + (KZ + 8));
            keepMovingCleanup(server);
        }
    }

    /** keep_moving with look_down keeps the pitch the server sees on the ground while moving. */
    private static void keepMovingLookDown(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        server.runCommand("difficulty peaceful");
        server.runCommand("kill @e[type=!player]");
        server.runCommand("clear @p");
        keepMovingPlatform(context, server);
        try {
            JsonObject params = new JsonObject();
            params.addProperty("radius", 10);
            params.addProperty("look_down", true);
            String id = body.startCommand("keepMoving", params, 800);
            java.util.List<Float> walking = new java.util.ArrayList<>();
            double[] last = {Double.NaN, Double.NaN};
            for (int tick = 0; tick < 300; tick++) {
                context.waitTick();
                float pitch = server.computeOnServer(s -> s.getPlayerList().getPlayers().get(0).getXRot());
                double[] xz = server.computeOnServer(s -> {
                    var player = s.getPlayerList().getPlayers().get(0);
                    return new double[] {player.getX(), player.getZ()};
                });
                if (!Double.isNaN(last[0]) && Math.hypot(xz[0] - last[0], xz[1] - last[1]) > 0.02) walking.add(pitch);
                last[0] = xz[0];
                last[1] = xz[1];
            }
            body.cancel(id);
            JsonObject terminal = body.terminal(id, 100, () -> {});
            float min = walking.stream().min(Float::compare).orElse(Float.NaN);
            float max = walking.stream().max(Float::compare).orElse(Float.NaN);
            String summary = "walking_ticks=" + walking.size() + " server_pitch=" + min + ".." + max + " stop=" + terminal.get("status")
                + " keep_moving=" + terminal.get("keep_moving");
            check(walking.size() >= 150, "too few walking ticks: " + summary);
            check(min >= 55.0f, "keep_moving with look_down looked up: " + summary);
            System.out.println("MINEBOT_KEEP PASS keepMovingLookDown " + summary);
        } finally {
            keepMovingCleanup(server);
        }
    }

    private static String shotSummary(JsonObject result) {
        StringBuilder out = new StringBuilder();
        for (String key : new String[] {"status", "reason", "lead_blocks", "predicted_target_pos", "last_shot", "shots"}) {
            if (result.has(key)) out.append(key).append('=').append(result.get(key)).append(' ');
        }
        return out.toString();
    }

    private static String movingTarget(ClientGameTestContext context, TestServerContext server, int px, int pz) {
        context.waitFor(client -> client.level.getEntities(client.player, client.player.getBoundingBox().inflate(64),
            entity -> entity.getType() == net.minecraft.world.entity.EntityType.IRON_GOLEM).isEmpty(), 100);
        // Motion alone does not move an armor stand (first run: it stood still
        // and was hit with zero lead); a repeating command block teleports a
        // still iron golem (1.4 wide; a crystal's position never reaches the
        // client) 0.25 blocks a tick across the line of sight instead.
        server.runCommand("setblock " + (px + 30) + " " + (Y - 4) + " " + (pz - 12)
            + " minecraft:repeating_command_block{auto:1b,Command:\"execute as @e[tag=lead] at @s run tp @s ~ ~ ~0.25\"}");
        server.runCommand("summon minecraft:iron_golem " + (px + 30) + ".5 " + (Y + 1) + " " + (pz - 8)
            + ".5 {NoAI:1b,NoGravity:1b,Silent:1b,Tags:[\"lead\"]}");
        context.waitFor(client -> client.level.getEntities(client.player, client.player.getBoundingBox().inflate(64),
            entity -> entity.getType() == net.minecraft.world.entity.EntityType.IRON_GOLEM).size() == 1, 100);
        return context.computeOnClient(client -> client.level.getEntities(client.player,
            client.player.getBoundingBox().inflate(64),
            entity -> entity.getType() == net.minecraft.world.entity.EntityType.IRON_GOLEM)
            .stream().findFirst().orElseThrow().getUUID().toString());
    }

    /** End run-6: the dragon 44-51 blocks off by its parts was offered to shoot
     * and then not found ten times (searched within 32); thrown by it, the Body
     * had no fact that it was falling. A dragon held 40+ blocks off is found and
     * shot without walking up to it; a fall says what lies below. */
    private static void farDragonAndFall(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        server.runCommand("difficulty peaceful");
        server.runCommand("kill @e[type=!player]");
        arena(server);
        server.runCommand("clear @p");
        server.runCommand("give @p minecraft:bow");
        server.runCommand("give @p minecraft:arrow 8");
        int px = X - 4, pz = Z + 4;
        server.runCommand("tp @p " + px + ".5 " + Y + " " + pz + ".5");
        context.waitFor(client -> client.player.onGround() && countItem(client, "arrow") == 8, 200);
        server.runCommand("summon minecraft:ender_dragon " + (px + 48) + " " + (Y + 6) + " " + pz + " {NoAI:1b}");
        context.waitFor(client -> !client.level.getEntities(client.player, client.player.getBoundingBox().inflate(64),
            entity -> entity instanceof net.minecraft.world.entity.boss.enderdragon.EnderDragon).isEmpty(), 200);
        try {
            JsonObject dragon = null;
            for (var value : body.state().getAsJsonArray("entities")) {
                if ("ender_dragon".equals(value.getAsJsonObject().get("type").getAsString())) dragon = value.getAsJsonObject();
            }
            check(dragon != null && dragon.get("nearest_dist").getAsDouble() > 32.0,
                "the dragon is not beyond the old 32-block search: " + dragon);
            JsonObject params = new JsonObject();
            params.addProperty("target_id", dragon.get("id").getAsString());
            params.addProperty("weapon", "bow");
            params.addProperty("max_range", 64);
            double[] start = context.computeOnClient(client -> new double[] {client.player.getX(), client.player.getZ()});
            JsonObject terminal = body.command("rangedAttack", params, 300);
            double moved = context.computeOnClient(client -> Math.hypot(client.player.getX() - start[0], client.player.getZ() - start[1]));
            String reason = terminal.get("reason").getAsString();
            check(!reason.equals("target_not_found") && !reason.equals("target_lost") && moved < 1.0
                && terminal.get("shots").getAsInt() == 1,
                "the far dragon shot ended " + terminal.get("status") + " " + reason + " shots " + terminal.get("shots") + " moved " + moved + " from " + start[0] + "," + start[1]);
            System.out.println("MINEBOT_RANGED PASS farDragon nearest " + dragon.get("nearest_dist") + " ended " + reason
                + " shots " + terminal.get("shots"));
        } finally {
            server.runCommand("kill @e[type=minecraft:ender_dragon]");
        }
        server.runCommand("effect give @p minecraft:resistance 30 4 true");
        server.runCommand("tp @p " + px + ".5 " + (Y + 14) + " " + pz + ".5");
        JsonObject fall = null;
        for (int tick = 0; tick < 80 && fall == null; tick++) {
            JsonObject state = body.state();
            if (state.has("fall")) fall = state.getAsJsonObject("fall");
            else context.waitTick();
        }
        check(fall != null && fall.has("ground_below") && fall.get("ground_below").getAsDouble() > 0.0
            && fall.has("lands_on") && !fall.has("void_below"), "no fall fact while falling: " + fall);
        context.waitFor(client -> client.player.onGround(), 200);
        check(!body.state().has("fall"), "a fall fact stayed after landing");
        System.out.println("MINEBOT_FALL PASS farDragonAndFall fall " + fall);
    }

    /** A 30-block fall onto stone kills a Body with 20 health and no armour;
     * cushionFall, started as it falls, pours the bucket in time and it lands
     * in the water unhurt (dragon-unit-tests-20261030 §6.y, form B). */
    private static void cushionFall(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        server.runCommand("difficulty peaceful");
        server.runCommand("kill @e[type=!player]");
        arena(server);
        server.runCommand("clear @p");
        server.runCommand("effect clear @p");
        int px = X - 4, pz = Z + 4;
        server.runCommand("tp @p " + px + ".5 " + Y + " " + pz + ".5");
        context.waitFor(client -> client.player.onGround() && client.player.getHealth() >= 20.0f, 200);
        // One bucket for both falls: after each landing cushionFall itself takes
        // the water it poured back (end run-35: left to a later choice, it was not).
        server.runCommand("give @p minecraft:water_bucket");
        context.waitFor(client -> countItem(client, "water_bucket") == 1, 100);
        for (int height : new int[] {30, 50}) {
        server.runCommand("tp @p " + px + ".5 " + (Y + height) + " " + pz + ".5");
        context.waitFor(client -> !client.player.onGround() && client.player.getY() > Y + height - 5, 100);
        try {
            JsonObject params = new JsonObject();
            params.addProperty("item", "water_bucket");
            JsonObject terminal = body.command("cushionFall", params, 400);
            context.waitTicks(10);
            float health = context.computeOnClient(client -> client.player.getHealth());
            boolean alive = context.computeOnClient(client -> client.player.isAlive());
            check(alive && health >= 19.0f && "cushioned".equals(terminal.get("reason").getAsString())
                && terminal.has("cushion_at"),
                "the " + height + "-block fall ended " + terminal.get("status") + " " + terminal.get("reason") + " at "
                + terminal.get("cushion_at") + " health " + health);
            int full = countItem(context, "water_bucket");
            String seen = context.computeOnClient(client -> "buckets " + countItem(client, "bucket") + " pitch " + client.player.getXRot()
                + " feet " + client.player.blockPosition() + " cell " + client.level.getFluidState(client.player.blockPosition()).getType());
            check(full == 1 && terminal.has("water_taken_back") && terminal.get("water_taken_back").getAsBoolean(),
                "after landing from " + height + " the water was not taken back: water_taken_back "
                + terminal.get("water_taken_back") + " water buckets " + full + " " + seen);
            System.out.println("MINEBOT_FALL PASS cushionFall from " + height + " water at " + terminal.get("cushion_at")
                + " health " + health + ", water taken back");
        } finally {
            server.runCommand(fill(px - 3, Y, pz - 3, px + 3, Y + 2, pz + 3, "air"));
            arena(server);
            server.runCommand("tp @p " + px + ".5 " + Y + " " + pz + ".5");
            context.waitFor(client -> client.player.onGround(), 100);
        }
        }
    }

    private static net.minecraft.world.entity.Entity crystalNear(net.minecraft.client.Minecraft client, int[] at) {
        for (var entity : client.level.entitiesForRendering()) {
            if (entity.getType() == net.minecraft.world.entity.EntityType.END_CRYSTAL && !entity.isRemoved()
                && entity.distanceToSqr(at[0] + 0.5, at[1], at[2] + 0.5) < 4.0) return entity;
        }
        return null;
    }

    // Fails if dropItem no longer resolves target_uuid and aims the thrown stack at that entity.
    private static void discardItemTowardPiglin(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        server.runCommand("difficulty peaceful");
        server.runCommand("kill @e[type=!player]");
        server.runCommand("clear @p");
        server.runCommand("item replace entity @p armor.head with minecraft:golden_helmet");
        server.runCommand("give @p minecraft:gold_ingot 3");
        server.runCommand("summon minecraft:piglin " + (X + 8) + ".5 " + Y + " " + (Z + 4) + ".5 {IsImmuneToZombification:1b}");
        context.waitFor(client -> countItem(client, "gold_ingot") == 3, 200);
        String piglinId = context.computeOnClient(client -> java.util.stream.StreamSupport.stream(client.level.entitiesForRendering().spliterator(), false)
            .filter(entity -> entity.getType() == net.minecraft.world.entity.EntityType.PIGLIN)
            .findFirst().orElseThrow().getUUID().toString());
        JsonObject params = new JsonObject();
        Integer goldSlot = context.computeOnClient(client -> {
            for (int slot = 0; slot < 9; slot++) {
                if (client.player.getInventory().getItem(slot).is(net.minecraft.world.item.Items.GOLD_INGOT)) return slot;
            }
            throw new AssertionError("gold ingot missing from hotbar");
        });
        params.addProperty("slot", (Number) goldSlot);
        params.addProperty("mode", "one");
        params.addProperty("target_uuid", piglinId);
        // dropItem is a primitive on the wire, as InventoryTransactions sends it.
        body.primitive("dropItem", params);
        // The server is read from the test thread only, so poll it tick by tick.
        for (int tick = 0; tick < 160 && !server.computeOnServer(s -> {
            var piglins = s.overworld().getEntitiesOfClass(net.minecraft.world.entity.monster.piglin.Piglin.class,
                new net.minecraft.world.phys.AABB(X, Y, Z, X + 16, Y + 8, Z + 16));
            var drops = s.overworld().getEntitiesOfClass(net.minecraft.world.entity.item.ItemEntity.class,
                new net.minecraft.world.phys.AABB(X, Y, Z, X + 16, Y + 8, Z + 16));
            return piglins.stream().anyMatch(p -> p.getOffhandItem().is(net.minecraft.world.item.Items.GOLD_INGOT)
                || p.getMainHandItem().is(net.minecraft.world.item.Items.GOLD_INGOT)) || drops.isEmpty();
        }); tick++) context.waitTick();
        check(countItem(context, "gold_ingot") < 3, "Body inventory still has all three ingots");
        check(server.computeOnServer(s -> {
            var player = s.getPlayerList().getPlayers().getFirst();
            if (!player.getItemBySlot(net.minecraft.world.entity.EquipmentSlot.HEAD).is(net.minecraft.world.item.Items.GOLDEN_HELMET)) return false;
            var piglins = s.overworld().getEntitiesOfClass(net.minecraft.world.entity.monster.piglin.Piglin.class,
                new net.minecraft.world.phys.AABB(X, Y, Z, X + 16, Y + 8, Z + 16));
            var drops = s.overworld().getEntitiesOfClass(net.minecraft.world.entity.item.ItemEntity.class,
                new net.minecraft.world.phys.AABB(X, Y, Z, X + 16, Y + 8, Z + 16));
            return piglins.stream().anyMatch(p -> p.getOffhandItem().is(net.minecraft.world.item.Items.GOLD_INGOT)
                || p.getMainHandItem().is(net.minecraft.world.item.Items.GOLD_INGOT)) || drops.isEmpty();
        }), "piglin did not hold gold and dropped ingots remained");
    }

    // Fails if dropItem executes its inventory throw before resolving the target UUID.
    private static void discardItemUnknownTargetDropsNothing(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        server.runCommand("clear @p");
        server.runCommand("give @p minecraft:gold_ingot 3");
        context.waitFor(client -> countItem(client, "gold_ingot") == 3, 200);
        int before = countItem(context, "gold_ingot");
        JsonObject params = new JsonObject();
        Integer goldSlot = context.computeOnClient(client -> {
            for (int slot = 0; slot < 9; slot++) {
                if (client.player.getInventory().getItem(slot).is(net.minecraft.world.item.Items.GOLD_INGOT)) return slot;
            }
            throw new AssertionError("gold ingot missing from hotbar");
        });
        params.addProperty("slot", (Number) goldSlot);
        params.addProperty("mode", "one");
        params.addProperty("target_uuid", "00000000-0000-0000-0000-000000000000");
        JsonObject reply = body.primitiveReply("dropItem", params);
        check(!"primitive".equals(reply.has("type") ? reply.get("type").getAsString() : "")
            && reply.toString().contains("target_not_found"), "unknown target drop was " + reply);
        check(countItem(context, "gold_ingot") == before, "gold ingot left inventory for unseen target");
    }

    private static void dragonFacts(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        server.runCommand("summon minecraft:ender_dragon " + (X + 12) + " " + (Y + 8) + " " + Z);
        context.waitFor(client -> !client.level.getEntities(client.player,
            client.player.getBoundingBox().inflate(64), entity -> entity instanceof net.minecraft.world.entity.boss.enderdragon.EnderDragon).isEmpty(), 200);
        JsonObject state = body.state();
        JsonArray entities = state.getAsJsonArray("entities");
        JsonObject dragon = null;
        for (var value : entities) {
            JsonObject candidate = value.getAsJsonObject();
            if ("ender_dragon".equals(candidate.get("type").getAsString())) dragon = candidate;
        }
        check(dragon != null, "client state omitted the summoned dragon: " + state);
        check(dragon.has("phase_id") && dragon.get("phase_id").isJsonPrimitive(), "dragon phase id missing: " + dragon);
        check(dragon.has("phase") && !dragon.get("phase").getAsString().isBlank(), "dragon phase missing: " + dragon);
        JsonArray head = dragon.getAsJsonArray("head_pos");
        check(head != null && head.size() == 3, "dragon head position missing: " + dragon);
        // The creature facts every kind gets (dragon-unit-tests-20261030 §5.2).
        check(dragon.has("max_health") && dragon.get("max_health").getAsDouble() == 200.0, "dragon max health: " + dragon);
        check(dragon.has("boss") && dragon.get("boss").getAsBoolean(), "dragon not said as a boss: " + dragon);
        check(dragon.has("velocity") && dragon.getAsJsonArray("velocity").size() == 3, "dragon velocity missing: " + dragon);
        check(dragon.has("facing_body") && dragon.has("approaching"), "dragon bearing missing: " + dragon);
        double centre = Math.sqrt(dragon.get("dist2").getAsDouble());
        check(dragon.has("nearest_dist") && dragon.get("nearest_dist").getAsDouble() < centre,
            "the nearest part was not nearer than the centre: " + dragon);
        check(Double.isFinite(head.get(0).getAsDouble()) && Double.isFinite(head.get(1).getAsDouble())
            && Double.isFinite(head.get(2).getAsDouble()), "dragon head position is not finite: " + dragon);
    }

    /** A placed bed is used through the same useItem primitive that use_on_block sends. */
    /** End run-12 (dragon-unit-tests-20261030 §6.y.21): move_to the overworld
     * end_portal cell was refused, Baritone does not walk into a portal block.
     * An end_portal set into the floor (a 1x1 hole, as the staging's setblock),
     * the walk to its cell (move_to: near radius 0) goes beside it, steps in and
     * ends in the End; once from 6 blocks off, once from the cell beside it. */
    private static void walkIntoEndPortal(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        int portalX = X + 9, portalZ = Z + 4;
        String overworld = "execute in minecraft:overworld run ";
        try {
            for (int[] start : new int[][] {{portalX - 6, portalZ}, {portalX - 1, portalZ}}) {
                server.runCommand(overworld + "tp @p " + (X + 3) + ".5 " + Y + " " + (Z + 4) + ".5");
                context.waitFor(client -> client.player != null
                    && client.player.level().dimension().identifier().toString().equals("minecraft:overworld")
                    && client.player.onGround(), 400);
                arena(server);
                server.runCommand("setblock " + portalX + " " + (Y - 1) + " " + portalZ + " minecraft:end_portal");
                server.runCommand("tp @p " + start[0] + ".5 " + Y + " " + start[1] + ".5");
                context.waitFor(client -> client.player.onGround() && client.player.blockPosition().getX() == start[0]
                    && client.player.blockPosition().getZ() == start[1], 200);
                // A player that just changed dimension waits before a portal takes it again.
                context.waitTicks(120);
                String goal = "near:" + portalX + ":" + (Y - 1) + ":" + portalZ + ":0";
                JsonObject terminal = body.awaitCommand(body.startObjective(goal, 600), 700);
                context.waitFor(client -> client.player != null
                    && client.player.level().dimension().identifier().toString().equals("minecraft:the_end"), 200);
                String dimension = context.computeOnClient(client -> client.player.level().dimension().identifier().toString());
                check("completed".equals(terminal.get("status").getAsString())
                    && "entered_portal".equals(terminal.get("reason").getAsString())
                    && "minecraft:the_end".equals(dimension),
                    "walk into the end portal from " + start[0] + "," + start[1] + " ended " + terminal.get("status")
                        + " " + terminal.get("reason") + " in " + dimension);
                System.out.println("MINEBOT_PORTAL PASS walkIntoEndPortal from=" + (portalX - start[0])
                    + " status=" + terminal.get("status") + " reason=" + terminal.get("reason") + " dimension=" + dimension);
            }
        } finally {
            server.runCommand(overworld + "tp @p " + (X + 3) + ".5 " + Y + " " + (Z + 4) + ".5");
            context.waitFor(client -> client.player != null
                && client.player.level().dimension().identifier().toString().equals("minecraft:overworld"), 400);
            arena(server);
        }
    }

    private static void endBedExplodesNearDragon(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        server.runCommand("difficulty peaceful");
        server.runCommand("kill @e[type=!player]");
        // 1000 blocks off the exit portal: no dragon fight starts there. The End's
        // chunks are not loaded until asked, and a fill into them is refused.
        int ex = X + 1000, ez = Z + 1000;
        int px = ex + 3, pz = ez + 4;
        String end = "execute in minecraft:the_end run ";
        server.runCommand(end + "forceload add " + (ex - 16) + " " + (ez - 16) + " " + (ex + 16) + " " + (ez + 16));
        context.waitTicks(40);
        server.runCommand(end + fill(ex - 6, Y - 1, ez - 6, ex + 14, Y + 6, ez + 12, "air"));
        server.runCommand(end + fill(ex - 6, Y - 2, ez - 6, ex + 14, Y - 1, ez + 12, "stone"));
        server.runCommand(end + "tp @p " + px + ".5 " + Y + " " + pz + ".5");
        server.runCommand("clear @p");
        server.runCommand("give @p minecraft:white_bed");
        context.waitFor(client -> client.player != null && client.player.level().dimension().identifier().toString().equals("minecraft:the_end")
            && client.player.onGround() && countItem(client, "white_bed") == 1, 300);
        BlockPos bed = new BlockPos(ex + 6, Y, ez + 4);
        JsonObject place = new JsonObject();
        place.add("target", xyz(bed));
        place.addProperty("block_type", "white_bed");
        JsonObject placed = body.command("placeBlock", place, 300);
        check("completed".equals(placed.get("status").getAsString()), "bed placement ended " + placed);
        context.waitFor(client -> client.level.getBlockState(bed).getBlock() == net.minecraft.world.level.block.Blocks.WHITE_BED, 100);

        // The wall is between the Body and the dragon while the bed remains clickable from the side.
        server.runCommand(end + "setblock " + (ex + 5) + " " + Y + " " + (ez + 5) + " minecraft:obsidian");
        server.runCommand(end + "setblock " + (ex + 5) + " " + (Y + 1) + " " + (ez + 5) + " minecraft:obsidian");
        server.runCommand(end + "summon minecraft:ender_dragon " + (ex + 6) + " " + (Y + 2) + " " + (ez + 8) + " {NoAI:1b,Health:200.0f}");
        context.waitFor(client -> !client.level.getEntities(client.player, client.player.getBoundingBox().inflate(16),
            entity -> entity instanceof net.minecraft.world.entity.boss.enderdragon.EnderDragon).isEmpty(), 200);
        server.runCommand(end + "tp @p " + (ex + 4) + ".5 " + Y + " " + (ez + 3) + ".5");
        context.waitFor(client -> client.player.onGround() && client.player.blockPosition().getX() == ex + 4, 100);
        float before = context.computeOnClient(client -> client.player.getHealth());
        JsonObject look = new JsonObject();
        look.add("target", xyz(new net.minecraft.world.phys.Vec3(bed.getX() + 0.5, bed.getY() + 0.5, bed.getZ() + 0.5)));
        body.primitive("lookAt", look);
        JsonObject use = new JsonObject();
        use.addProperty("mode", "once");
        use.addProperty("ticks", 1);
        JsonObject exploded = body.command("useItem", use, 200);
        context.waitFor(client -> client.level.getBlockState(bed).isAir(), 100);
        float after = context.computeOnClient(client -> client.player.getHealth());
        double dragonHealth = context.computeOnClient(client -> client.level.getEntities(client.player,
            client.player.getBoundingBox().inflate(16), entity -> entity instanceof net.minecraft.world.entity.boss.enderdragon.EnderDragon)
            .stream().mapToDouble(entity -> ((net.minecraft.world.entity.boss.enderdragon.EnderDragon) entity).getHealth()).findFirst().orElse(0.0));
        server.runCommand(end + "kill @e[type=minecraft:ender_dragon]");
        server.runCommand(end + "forceload remove " + (ex - 16) + " " + (ez - 16) + " " + (ex + 16) + " " + (ez + 16));
        check("completed".equals(exploded.get("status").getAsString())
            && dragonHealth < 200.0 && after > 4.0f,
            "End bed did not explode near the dragon: " + exploded + " health " + before + " -> " + after
                + " dragon=" + dragonHealth);
        System.out.println("MINEBOT_END_BED PASS endBedExplodesNearDragon body_health=" + before + " -> " + after
            + " dragon_health=" + dragonHealth);
    }

    private static JsonArray xyz(BlockPos pos) {
        JsonArray result = new JsonArray();
        result.add(pos.getX()); result.add(pos.getY()); result.add(pos.getZ());
        return result;
    }

    private static JsonArray xyz(net.minecraft.world.phys.Vec3 pos) {
        JsonArray result = new JsonArray();
        result.add(pos.x); result.add(pos.y); result.add(pos.z);
        return result;
    }

    /** Exercises the dedicated jump/place controller used by pillar_up. */
    /** End run-11: on uneven End ground the Body arrived 0.4 off its cell's
     * centre and pillarUp ended displaced at once, three times, nothing raised.
     * Started 0.4 off centre beside a one-block step, it shuffles onto the
     * centre and raises ten blocks. */
    /** A shove while pillaring (body-channels-20261105.md §6.4): back to the centre, the column goes on. */
    private static void pillarShovedMidColumn(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        server.runCommand("difficulty peaceful");
        server.runCommand("kill @e[type=!player]");
        arena(server);
        server.runCommand("clear @p");
        server.runCommand("give @p minecraft:cobblestone 12");
        int px = X + 5, pz = Z + 4;
        try {
            server.runCommand(fill(px - 2, Y, pz - 2, px + 2, Y + 14, pz + 2, "air"));
            server.runCommand("tp @p " + px + ".5 " + Y + " " + pz + ".5");
            context.waitFor(client -> client.player.onGround() && countItem(client, "cobblestone") == 12, 200);
            JsonObject params = new JsonObject();
            params.addProperty("blocks", 8);
            JsonArray scaffold = new JsonArray();
            scaffold.add("cobblestone");
            params.add("scaffold_blocks", scaffold);
            boolean[] shoved = {false};
            double[] offAfter = {0.0};
            JsonObject terminal = body.terminal(body.startCommand("pillarUp", params, 1200), 1240, () -> {
                if (shoved[0]) {
                    offAfter[0] = Math.max(offAfter[0], context.computeOnClient(client ->
                        Math.hypot(client.player.getX() - (px + 0.5), client.player.getZ() - (pz + 0.5))));
                    return;
                }
                boolean ready = context.computeOnClient(client -> client.player.onGround()
                    && client.player.getY() >= Y + 4 - 0.01);
                if (ready) {
                    // A knockback-like push east while standing on the column top.
                    context.runOnClient(client -> client.player.setDeltaMovement(0.07, 0.0, 0.0));
                    shoved[0] = true;
                }
            });
            int feetY = context.computeOnClient(client -> client.player.blockPosition().getY());
            int placed = 0;
            for (int i = 0; i < 8; i++) {
                if ("cobblestone".equals(block(server, new BlockPos(px, Y + i, pz)))) placed++;
            }
            String summary = "status=" + terminal.get("status") + " reason=" + terminal.get("reason")
                + " shoved=" + shoved[0] + " max_off_after_shove=" + Math.round(offAfter[0] * 100) / 100.0
                + " feet_y=" + feetY + " (start " + Y + ") column_blocks=" + placed + " pillar=" + terminal.get("pillar");
            check(shoved[0] && offAfter[0] > 0.3, "the shove did not move the Body off centre: " + summary);
            check("completed".equals(terminal.get("status").getAsString()) && feetY == Y + 8 && placed == 8
                && terminal.getAsJsonObject("pillar").get("recentred").getAsInt() >= 1, "pillar after a shove: " + summary);
            System.out.println("MINEBOT_PROTECTION PASS pillarShovedMidColumn " + summary);
        } finally {
            server.runCommand(fill(px - 2, Y, pz - 2, px + 2, Y + 14, pz + 2, "air"));
            arena(server);
            server.runCommand("tp @p " + (X + 3) + ".5 " + Y + " " + (Z + 4) + ".5");
            context.waitFor(client -> client.player.onGround(), 200);
        }
    }

    private static void pillarFromOffCentre(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        server.runCommand("difficulty peaceful");
        server.runCommand("kill @e[type=!player]");
        arena(server);
        server.runCommand("clear @p");
        server.runCommand("give @p minecraft:cobblestone 12");
        int px = X + 5, pz = Z + 4;
        try {
            server.runCommand(fill(px - 2, Y, pz - 2, px + 2, Y + 14, pz + 2, "air"));
            server.runCommand("setblock " + (px + 1) + " " + Y + " " + pz + " minecraft:stone");      // a step east
            server.runCommand("setblock " + (px - 1) + " " + (Y - 1) + " " + (pz + 1) + " minecraft:air"); // a dip south-west
            server.runCommand("tp @p " + px + ".9 " + Y + " " + pz + ".5");
            context.waitFor(client -> client.player.onGround() && countItem(client, "cobblestone") == 12
                && client.player.blockPosition().getX() == px, 200);
            double off = context.computeOnClient(client -> Math.hypot(client.player.getX() - (px + 0.5), client.player.getZ() - (pz + 0.5)));
            JsonObject params = new JsonObject();
            params.addProperty("blocks", 10);
            JsonArray scaffold = new JsonArray();
            scaffold.add("cobblestone");
            params.add("scaffold_blocks", scaffold);
            JsonObject terminal = body.command("pillarUp", params, 1200);
            int feetY = context.computeOnClient(client -> client.player.blockPosition().getY());
            String where = context.computeOnClient(client -> Math.round(client.player.getX() * 100) / 100.0 + ","
                + Math.round(client.player.getY() * 100) / 100.0 + "," + Math.round(client.player.getZ() * 100) / 100.0);
            int placed = 0;
            for (int i = 0; i < 10; i++) {
                if ("cobblestone".equals(block(server, new BlockPos(px, Y + i, pz)))) placed++;
            }
            String summary = "start_off_centre=" + Math.round(off * 100) / 100.0 + " status=" + terminal.get("status")
                + " reason=" + terminal.get("reason") + " feet_y=" + feetY + " (start " + Y + ") column_blocks=" + placed + " at=" + where;
            check("completed".equals(terminal.get("status").getAsString()) && feetY == Y + 10 && placed == 10,
                "pillar from off centre: " + summary);
            System.out.println("MINEBOT_PROTECTION PASS pillarFromOffCentre " + summary);
        } finally {
            server.runCommand(fill(px - 2, Y, pz - 2, px + 2, Y + 14, pz + 2, "air"));
            arena(server);
            server.runCommand("tp @p " + (X + 3) + ".5 " + Y + " " + (Z + 4) + ".5");
            context.waitFor(client -> client.player.onGround(), 200);
        }
    }

    private static void pillarUpRaisesThreeBlocks(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        server.runCommand("difficulty peaceful");
        server.runCommand("kill @e[type=!player]");
        arena(server);
        server.runCommand("clear @p");
        server.runCommand("give @p minecraft:cobblestone 3");
        server.runCommand("difficulty easy");
        server.runCommand("summon minecraft:zombie " + (X + 4) + ".5 " + Y + " " + (Z + 4) + ".5 {PersistenceRequired:1b}");
        server.runCommand("tp @p " + (X + 3) + ".5 " + Y + " " + (Z + 4) + ".5");
        context.waitFor(client -> client.player.onGround() && countItem(client, "cobblestone") == 3, 200);
        int px = context.computeOnClient(client -> client.player.blockPosition().getX());
        int py = context.computeOnClient(client -> client.player.blockPosition().getY());
        int pz = context.computeOnClient(client -> client.player.blockPosition().getZ());
        // Same native controller that pillar_up dispatches on the real client.
        JsonObject params = new JsonObject();
        params.addProperty("blocks", 3);
        JsonArray scaffold = new JsonArray();
        scaffold.add("cobblestone");
        params.add("scaffold_blocks", scaffold);
        JsonObject terminal = body.command("pillarUp", params, 600);
        check("completed".equals(terminal.get("status").getAsString()), "pillarUp ended " + terminal);
        for (int i = 0; i < 3; i++) {
            BlockPos pillar = new BlockPos(px, py + i, pz);
            check("cobblestone".equals(block(server, pillar)), "server did not confirm pillar block at " + pillar);
        }
        server.runCommand("gamerule minecraft:natural_health_regeneration false");
        float before = context.computeOnClient(client -> client.player.getHealth());
        context.waitTicks(100);
        float after = context.computeOnClient(client -> client.player.getHealth());
        check(context.computeOnClient(client -> client.player.blockPosition().getY()) == py + 3,
            "Body feet did not rise three blocks");
        check(before == after, "adjacent zombie changed health from " + before + " to " + after);
        server.runCommand("fill " + px + " " + py + " " + pz + " " + px + " " + (py + 2) + " " + pz + " minecraft:air");
        server.runCommand("difficulty peaceful");
        server.runCommand("kill @e[type=!player]");
        server.runCommand("time set day");
        server.runCommand("gamerule minecraft:natural_health_regeneration true");
        server.runCommand("effect give @p minecraft:instant_health 1 10 true");
        context.waitFor(client -> client.player.getHealth() == 20, 200);
        System.out.println("MINEBOT_PROTECTION PASS pillarUpRaisesThreeBlocks blocks=3 zombie_hits=0");
    }

    /** B11: a walk that cannot be routed says so, with what the Body stands in and may change. */
    /** easy run-5, 17:51-17:52: a spider under a one-block overhang had its
     * eyes behind stone and its body in view; the Body stood beside it for
     * 20 s without a swing, twice. A player hits the part it sees, and stops
     * and says so when it sees none. */
    private static void engageSight(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        arena(server);
        server.runCommand("clear @p");
        server.runCommand("item replace entity @p hotbar.0 with minecraft:stone_pickaxe");
        server.runCommand("item replace entity @p hotbar.3 with minecraft:iron_sword");
        context.waitFor(client -> countItem(client, "iron_sword") == 1, 200);
        body.primitive("selectItem", params("item", "stone_pickaxe"));
        server.runCommand("difficulty easy");
        int sx = X + 4, sz = Z + 4;
        server.runCommand("setblock " + sx + " " + (Y + 1) + " " + sz + " minecraft:stone");
        server.runCommand("summon minecraft:spider " + sx + ".5 " + Y + " " + sz + ".5 {NoAI:1b,Silent:1b,PersistenceRequired:1b}");
        server.runCommand("tp @p " + sx + ".5 " + Y + " " + (sz + 1) + ".7 0 0");
        context.waitFor(client -> client.player.onGround() && !client.level.getEntities(client.player,
            client.player.getBoundingBox().inflate(8), entity -> entity.getType() == net.minecraft.world.entity.EntityType.SPIDER).isEmpty(), 200);
        JsonObject fight = new JsonObject();
        fight.addProperty("target_type", "spider");
        fight.addProperty("attack_range", 2.0);
        fight.addProperty("disengage_health", 0);
        JsonObject terminal = body.command("engageEntity", fight, 400);
        server.runCommand("kill @e[type=!player]");
        server.runCommand("setblock " + sx + " " + (Y + 1) + " " + sz + " minecraft:air");
        check("killed".equals(terminal.get("reason").getAsString()) && terminal.get("attacks").getAsInt() > 0,
            "the spider under an overhang, its body in view: " + terminal);
        System.out.println("MINEBOT_ENGAGE PASS partly seen " + terminal.get("reason") + " attacks " + terminal.get("attacks"));

        int zx = X + 8, zz = Z + 4;
        server.runCommand(fill(zx - 1, Y - 1, zz - 1, zx + 1, Y + 2, zz + 1, "bedrock"));
        server.runCommand(fill(zx, Y, zz, zx, Y + 1, zz, "air"));
        server.runCommand("summon minecraft:zombie " + zx + ".5 " + Y + " " + zz + ".5 {NoAI:1b,Silent:1b,PersistenceRequired:1b}");
        server.runCommand("tp @p " + zx + ".5 " + Y + " " + (zz + 2) + ".4 180 0");
        context.waitFor(client -> client.player.onGround() && !client.level.getEntities(client.player,
            client.player.getBoundingBox().inflate(8), entity -> entity.getType() == net.minecraft.world.entity.EntityType.ZOMBIE).isEmpty(), 200);
        fight.addProperty("target_type", "zombie");
        long started = System.nanoTime();
        terminal = body.command("engageEntity", fight, 400);
        double seconds = (System.nanoTime() - started) / 1e9;
        server.runCommand("kill @e[type=!player]");
        server.runCommand(fill(zx - 1, Y - 1, zz - 1, zx + 1, Y + 2, zz + 1, "air"));
        server.runCommand(fill(zx - 1, Y - 1, zz - 1, zx + 1, Y - 1, zz + 1, "stone"));
        server.runCommand("difficulty peaceful");
        check("no_line_of_sight".equals(terminal.get("reason").getAsString()) && seconds < 8.0,
            "the zombie sealed in bedrock, after " + seconds + " s: " + terminal);
        check(terminal.has("sight_blocked_by") && terminal.has("target_distance"), "no facts on what is in the way: " + terminal);
        System.out.println("MINEBOT_ENGAGE PASS sealed in " + String.format("%.1f", seconds) + " s " + terminal.get("sight_blocked_by")
            + " distance " + terminal.get("target_distance"));
    }

    /** Combat boundaries 2026-11-10 (docs/design-docs/body-tool-boundaries-20261110.md):
     * what zombies cost. Easy, an iron sword, no armour, no natural
     * regeneration, at night so nothing burns, in a walled arena. Scenes:
     * single (one zombie walks up), pair (two at once), close (one already at
     * arm's length: the reflex answered late), tunnel (a 1-wide, 2-high
     * corridor), baby (a baby zombie). The Body fights as the reflex layer
     * asks (disengage 0), call after call until no zombie is left. Prints
     * health lost per trial; MINEBOT_ENGAGE_STYLES compares ways of fighting. */
    private static void engageTrade(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        String[] scenes = System.getenv().getOrDefault("MINEBOT_TRADE_SCENES", "single,pair,close,tunnel,baby").split(",");
        StringBuilder summary = new StringBuilder();
        for (String scene : scenes) {
            for (String style : System.getenv().getOrDefault("MINEBOT_ENGAGE_STYLES", "stand,kite").split(",")) {
                summary.append(engageTradeScene(context, server, body, scene.trim(), style.trim())).append(' ');
            }
        }
        System.out.println("MINEBOT_TRADE TABLE " + summary);
    }

    private static String engageTradeScene(ClientGameTestContext context, TestServerContext server, BodyIpc body,
                                           String scene, String style) {
        int trials = Integer.parseInt(System.getenv().getOrDefault("MINEBOT_TRADE_TRIALS", "4"));
        server.runCommand("gamerule minecraft:natural_health_regeneration false");
        server.runCommand("time set 18000");
        server.runCommand("difficulty easy");
        double total = 0.0;
        int cleared = 0;
        try {
            for (int i = 0; i < trials; i++) {
                server.runCommand("kill @e[type=!player]");
                // The last trial's zombie stays on the client while it dies: wait it out.
                context.waitFor(client -> client.level.getEntities(client.player, client.player.getBoundingBox().inflate(24),
                    entity -> entity.getType() == net.minecraft.world.entity.EntityType.ZOMBIE).isEmpty(), 200);
                arena(server);
                // Walled: a knocked-back zombie fell off the open arena and was "lost".
                server.runCommand(fill(X - 6, Y, Z - 6, X + 14, Y + 2, Z - 6, "glass"));
                server.runCommand(fill(X - 6, Y, Z + 12, X + 14, Y + 2, Z + 12, "glass"));
                server.runCommand(fill(X - 6, Y, Z - 6, X - 6, Y + 2, Z + 12, "glass"));
                server.runCommand(fill(X + 14, Y, Z - 6, X + 14, Y + 2, Z + 12, "glass"));
                int px = X + 4;
                double pz = Z + 1.5;
                if (scene.equals("tunnel")) {
                    server.runCommand(fill(px - 1, Y, Z - 5, px + 1, Y + 2, Z + 11, "stone"));
                    server.runCommand(fill(px, Y, Z - 4, px, Y + 1, Z + 10, "air"));
                }
                server.runCommand("clear @p");
                server.runCommand("item replace entity @p hotbar.0 with minecraft:iron_sword");
                server.runCommand("effect clear @p");
                server.runCommand("effect give @p minecraft:instant_health 1 10 true");
                server.runCommand("tp @p " + px + ".5 " + Y + " " + pz + " 0 0");
                context.waitFor(client -> countItem(client, "iron_sword") == 1 && client.player.getHealth() >= 20.0f
                    && client.player.onGround(), 200);
                String tag = " {PersistenceRequired:1b,CanPickUpLoot:0b,IsBaby:" + (scene.equals("baby") ? "1b" : "0b") + "}";
                if (scene.equals("close")) {
                    server.runCommand("summon minecraft:zombie " + px + ".5 " + Y + " " + (pz + 1.3) + tag);
                } else if (scene.equals("pair")) {
                    server.runCommand("summon minecraft:zombie " + (px - 2) + ".5 " + Y + " " + (pz + 7) + tag);
                    server.runCommand("summon minecraft:zombie " + (px + 3) + ".5 " + Y + " " + (pz + 8) + tag);
                } else {
                    server.runCommand("summon minecraft:zombie " + px + ".5 " + Y + " " + (pz + 8) + tag);
                }
                for (String slot : new String[] {"weapon.mainhand", "weapon.offhand", "armor.head", "armor.chest", "armor.legs", "armor.feet"}) {
                    server.runCommand("item replace entity @e[type=zombie] " + slot + " with minecraft:air");
                }
                int expected = scene.equals("pair") ? 2 : 1;
                context.waitFor(client -> client.level.getEntities(client.player, client.player.getBoundingBox().inflate(16),
                    entity -> entity.getType() == net.minecraft.world.entity.EntityType.ZOMBIE && entity.isAlive()).size() >= expected, 200);
                float before = context.computeOnClient(client -> client.player.getHealth());
                long started = System.nanoTime();
                int calls = 0, attacks = 0;
                String last = "";
                while (calls < 6 && context.computeOnClient(client -> !client.level.getEntities(client.player,
                        client.player.getBoundingBox().inflate(20), entity -> entity.getType() == net.minecraft.world.entity.EntityType.ZOMBIE
                        && entity.isAlive()).isEmpty())) {
                    JsonObject fight = new JsonObject();
                    fight.addProperty("target_type", "zombie");
                    fight.addProperty("disengage_health", 0);
                    // "stand:2" = style stand, attack_range 2 (what the reflex layer passed live in run-13).
                    String[] parts = style.split(":");
                    fight.addProperty("style", parts[0]);
                    if (parts.length > 1) fight.addProperty("attack_range", Double.parseDouble(parts[1]));
                    JsonObject terminal = body.command("engageEntity", fight, 900);
                    calls++;
                    attacks += terminal.get("attacks").getAsInt();
                    last = terminal.get("reason").getAsString();
                    if (context.computeOnClient(client -> client.player.getHealth() <= 0.0f || client.player.isDeadOrDying())) break;
                    context.waitTicks(5);
                }
                double seconds = (System.nanoTime() - started) / 1e9;
                float after = context.computeOnClient(client -> client.player.getHealth());
                boolean left = context.computeOnClient(client -> client.level.getEntities(client.player,
                    client.player.getBoundingBox().inflate(20), entity -> entity.getType() == net.minecraft.world.entity.EntityType.ZOMBIE
                    && entity.isAlive()).isEmpty());
                total += before - after;
                if (left) cleared++;
                System.out.println("MINEBOT_TRADE scene=" + scene + " style=" + style + " trial=" + i
                    + " lost=" + String.format("%.1f", before - after) + " secs=" + String.format("%.1f", seconds)
                    + " calls=" + calls + " attacks=" + attacks + " last=" + last + " cleared=" + left);
                if (after <= 0.0f) {
                    context.waitTicks(40);
                    server.runCommand("kill @e[type=!player]");
                }
            }
        } finally {
            server.runCommand("kill @e[type=!player]");
            arena(server);
            server.runCommand("gamerule minecraft:natural_health_regeneration true");
            server.runCommand("effect give @p minecraft:instant_health 1 10 true");
            server.runCommand("time set day");
            server.runCommand("difficulty peaceful");
        }
        String line = scene + "/" + style + "=" + String.format("%.2f", total / trials) + "(" + cleared + "/" + trials + ")";
        System.out.println("MINEBOT_TRADE SUMMARY " + line);
        return line;
    }

    private static void noRouteSaysWhy(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        server.runCommand("difficulty peaceful");
        arena(server);
        server.runCommand("clear @p");
        server.runCommand("give @p minecraft:cobblestone 7");
        server.runCommand(fill(X + 1, Y, Z + 1, X + 3, Y + 3, Z + 3, "stone"));
        server.runCommand(fill(X + 2, Y, Z + 2, X + 2, Y + 1, Z + 2, "air"));
        server.runCommand("tp @p " + (X + 2) + ".5 " + Y + " " + (Z + 2) + ".5");
        context.waitFor(client -> client.player.onGround() && countItem(client, "cobblestone") == 7, 200);
        context.runOnClient(client -> {
            BaritoneAPI.getSettings().allowBreak.value = false;
            BaritoneAPI.getSettings().allowPlace.value = false;
        });
        try {
            JsonObject terminal = body.awaitCommand(
                body.startObjective("goto:" + (X + 10) + ":" + Y + ":" + (Z + 2), 400), 400);
            check(terminal.has("status") && "failed".equals(terminal.get("status").getAsString()), "sealed walk ended " + terminal);
            check("no_route_found".equals(terminal.get("reason").getAsString()), "sealed walk gave reason " + terminal.get("reason"));
            check(terminal.has("route"), "failed walk has no route facts " + terminal);
            JsonObject route = terminal.getAsJsonObject("route");
            check(!route.get("route_found").getAsBoolean(), "sealed walk claims a route " + route);
            check(!route.get("may_break").getAsBoolean() && !route.get("may_place").getAsBoolean(),
                "route facts misreport permissions " + route);
            check(route.get("placeable_blocks").getAsInt() == 7, "route facts miscount blocks " + route);
            System.out.println("MINEBOT_ROUTE PASS noRouteSaysWhy " + route);
        } finally {
            context.runOnClient(client -> {
                BaritoneAPI.getSettings().allowBreak.value = true;
                BaritoneAPI.getSettings().allowPlace.value = true;
            });
            server.runCommand(fill(X + 1, Y, Z + 1, X + 3, Y + 3, Z + 3, "air"));
        }
    }

    /** End baseline run-3, 15:19: the arrival platform was cut into the underside of
     * the island's edge, end stone above and to the west, open air below. With no
     * pickaxe in the kit, five walks west found no route. With a pickaxe the Body
     * tunnels there; without one the walk ends saying it found no route. */
    private static void endArrivalTunnel(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        int bx = X + 8, by = Y + 12, bz = Z + 24;
        String box = fill(bx - 17, by, bz - 4, bx + 3, by + 8, bz + 4, "end_stone");
        String goal = "goto:" + (bx - 15) + ":" + (by + 1) + ":" + bz;
        try {
            for (boolean pickaxe : new boolean[] {true, false}) {
                server.runCommand(box);
                server.runCommand(fill(bx - 2, by + 1, bz - 2, bx + 2, by + 3, bz + 2, "air"));
                server.runCommand(fill(bx - 2, by, bz - 2, bx + 2, by, bz + 2, "obsidian"));
                server.runCommand("clear @p");
                if (pickaxe) server.runCommand("item replace entity @p hotbar.0 with minecraft:diamond_pickaxe");
                server.runCommand("give @p minecraft:cobblestone 64");
                server.runCommand("tp @p " + bx + ".5 " + (by + 1) + " " + bz + ".5");
                context.waitFor(client -> client.player.onGround() && client.player.blockPosition().getY() == by + 1
                    && countItem(client, "cobblestone") == 64, 200);
                JsonObject terminal = body.awaitCommand(body.startObjective(goal, 2400), 2400);
                BlockPos at = context.computeOnClient(client -> client.player.blockPosition());
                String reason = terminal.get("reason").getAsString();
                if (pickaxe) {
                    check("completed".equals(terminal.get("status").getAsString())
                        && Math.abs(at.getX() - (bx - 15)) <= 1 && Math.abs(at.getZ() - bz) <= 1,
                        "with a pickaxe the tunnel walk ended " + terminal.get("status") + " " + reason + " at " + at
                        + ", path events " + terminal.get("path_events"));
                } else if ("failed".equals(terminal.get("status").getAsString()) && terminal.has("route")
                        && !terminal.getAsJsonObject("route").get("route_found").getAsBoolean()) {
                    check("no_route_found".equals(reason), "a walk with no route gave reason " + reason);
                }
                System.out.println("MINEBOT_ROUTE PASS endArrivalTunnel pickaxe=" + pickaxe + " status="
                    + terminal.get("status") + " reason=" + reason + " at=" + at + " route=" + terminal.get("route"));
            }
        } finally {
            server.runCommand(fill(bx - 17, by, bz - 4, bx + 3, by + 8, bz + 4, "air"));
        }
    }

    /** look_down (user 2026-11-05: 默认平视，传参之后就低头): endermen anger at a
     * crosshair on their head. Three stand at eye height 22-30 blocks ahead in
     * the line of a walk. Walking there with look_down keeps the pitch the
     * server sees down and all three calm; the same walk without it is
     * reported as it happens. A walk with look_down through a wall still
     * breaks its way and arrives: the aim for breaking is not held down. */
    /** A walk goes around a cloud on the ground, not through it (body-preemption-20261106.md §8.3). */
    private static void pathAroundCloud(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        int z = Z + 3, x0 = X - 4, x1 = X + 30, goal = X + 20, cloudX = X + 10;
        server.runCommand("forceload add " + (x0 - 4) + " " + (z - 10) + " " + (x1 + 4) + " " + (z + 10));
        server.runCommand("gamerule minecraft:spawn_mobs false");
        server.runCommand("kill @e[type=!player]");
        server.runCommand(fill(x0, Y - 1, z - 7, x1, Y + 6, z + 7, "air"));
        server.runCommand(fill(x0, Y - 2, z - 7, x1, Y - 1, z + 7, "stone"));
        server.runCommand("clear @p");
        server.runCommand("tp @p " + X + ".5 " + Y + " " + z + ".5 -90 0");
        context.waitFor(client -> client.player.onGround() && client.player.blockPosition().getX() == X
            && client.player.blockPosition().getZ() == z, 200);
        // Harmless here (no effect): what is tested is the route around it.
        server.runCommand("summon minecraft:area_effect_cloud " + cloudX + ".5 " + Y + " " + z + ".5"
            + " {Radius:2.5f,Duration:4000,WaitTime:0,RadiusOnUse:0f,RadiusPerTick:0f,"
            + "Particle:{type:\"minecraft:dragon_breath\"}}");
        context.waitFor(client -> client.level.getEntitiesOfClass(net.minecraft.world.entity.AreaEffectCloud.class,
            client.player.getBoundingBox().inflate(20)).size() == 1, 100);
        double[] closest = {Double.MAX_VALUE};
        String id = body.startObjective("near:" + goal + ":" + Y + ":" + z + ":1", 600, false);
        JsonObject terminal = body.terminal(id, 640, () -> {
            double[] xz = server.computeOnServer(sv -> {
                var player = sv.getPlayerList().getPlayers().get(0);
                return new double[] {player.getX(), player.getZ()};
            });
            closest[0] = Math.min(closest[0], Math.hypot(xz[0] - (cloudX + 0.5), xz[1] - (z + 0.5)));
        });
        BlockPos at = context.computeOnClient(client -> client.player.blockPosition());
        String summary = "status=" + terminal.get("status") + " reason=" + terminal.get("reason") + " at=" + at
            + " closest_to_cloud_centre=" + String.format("%.2f", closest[0]) + " radius=2.5";
        server.runCommand("kill @e[type=minecraft:area_effect_cloud]");
        check(Math.abs(at.getX() - goal) <= 2 && Math.abs(at.getZ() - z) <= 2, "walk did not arrive: " + summary);
        check(closest[0] > 2.8, "the walk went through the cloud: " + summary);
        System.out.println("MINEBOT_CLOUD PASS pathAroundCloud " + summary);
    }

    private static void walkLookDown(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        int z = Z + 3, x0 = X - 4, x1 = X + 34, goal = X + 18;
        server.runCommand("forceload add " + (x0 - 4) + " " + (z - 10) + " " + (x1 + 4) + " " + (z + 10));
        server.runCommand("gamerule minecraft:spawn_mobs false");
        server.runCommand("gamerule minecraft:mob_griefing false");
        server.runCommand("kill @e[type=!player]");
        server.runCommand(fill(x0, Y - 1, z - 6, x1, Y + 6, z + 6, "air"));
        server.runCommand(fill(x0, Y - 2, z - 6, x1, Y - 1, z + 6, "stone"));
        String walk = "near:" + goal + ":" + Y + ":" + z + ":1";
        int[][] endermen = {{X + 22, z}, {X + 26, z + 1}, {X + 30, z - 1}};
        try {
            server.runCommand("difficulty normal");
            server.runCommand("time set midnight");
            for (boolean lookDown : new boolean[] {true, false}) {
                server.runCommand("kill @e[type=minecraft:enderman]");
                server.runCommand("clear @p");
                // Each stands in a one-deep pit: its eyes at the player's eye height.
                for (int[] at : endermen) {
                    server.runCommand("setblock " + at[0] + " " + (Y - 1) + " " + at[1] + " minecraft:air");
                    server.runCommand("summon minecraft:enderman " + at[0] + ".5 " + (Y - 1) + " " + at[1]
                        + ".5 {PersistenceRequired:1b}");
                }
                server.runCommand("effect give @e[type=minecraft:enderman] minecraft:slowness infinite 255 true");
                // Facing away from them, level, before the walk.
                server.runCommand("tp @p " + X + ".5 " + Y + " " + z + ".5 90 0");
                context.waitFor(client -> client.player.onGround() && client.player.blockPosition().getX() == X
                    && client.player.blockPosition().getZ() == z, 200);
                context.waitTicks(20);
                check(angryEndermen(server) == 0, "endermen were angry before the walk");
                java.util.List<Float> walking = new java.util.ArrayList<>();
                int[] angryTicks = {0};
                double[] last = {Double.NaN, Double.NaN};
                String id = body.startObjective(walk, 600, lookDown);
                JsonObject terminal = body.terminal(id, 640, () -> {
                    float pitch = server.computeOnServer(s -> s.getPlayerList().getPlayers().get(0).getXRot());
                    double[] xz = server.computeOnServer(s -> {
                        var player = s.getPlayerList().getPlayers().get(0);
                        return new double[] {player.getX(), player.getZ()};
                    });
                    if (!Double.isNaN(last[0]) && Math.hypot(xz[0] - last[0], xz[1] - last[1]) > 0.02) walking.add(pitch);
                    last[0] = xz[0];
                    last[1] = xz[1];
                    if (angryEndermen(server) > 0) angryTicks[0]++;
                });
                BlockPos at = context.computeOnClient(client -> client.player.blockPosition());
                int angry = angryEndermen(server);
                float min = walking.stream().min(Float::compare).orElse(Float.NaN);
                float max = walking.stream().max(Float::compare).orElse(Float.NaN);
                String summary = "look_down=" + lookDown + " status=" + terminal.get("status") + " reason="
                    + terminal.get("reason") + " at=" + at + " walking_ticks=" + walking.size() + " server_pitch="
                    + min + ".." + max + " angry_now=" + angry + " angry_ticks=" + angryTicks[0];
                if (lookDown) {
                    // Without it, angry endermen may stop the walk: that is reported, not asserted.
                    check(Math.abs(at.getX() - goal) <= 2 && Math.abs(at.getZ() - z) <= 2, "walk did not arrive: " + summary);
                    check(walking.size() >= 10, "too few walking ticks sampled: " + summary);
                    check(min >= 55.0f, "a look_down walk looked up: " + summary);
                    check(angryTicks[0] == 0 && angry == 0, "a look_down walk angered endermen: " + summary);
                }
                System.out.println("MINEBOT_LOOK " + (lookDown ? "PASS walkLookDown " : "OBSERVED walkLevel ") + summary);
            }
            server.runCommand("kill @e[type=minecraft:enderman]");
            server.runCommand("difficulty peaceful");
            // A wall across the whole platform, four high, and no blocks to climb it.
            server.runCommand(fill(X + 9, Y, z - 6, X + 9, Y + 3, z + 6, "stone"));
            server.runCommand("clear @p");
            server.runCommand("item replace entity @p hotbar.0 with minecraft:diamond_pickaxe");
            server.runCommand("tp @p " + X + ".5 " + Y + " " + z + ".5 90 0");
            context.waitFor(client -> client.player.onGround() && client.player.blockPosition().getX() == X
                && client.player.blockPosition().getZ() == z, 200);
            java.util.List<Float> pitches = new java.util.ArrayList<>();
            java.util.List<Float> walking = new java.util.ArrayList<>();
            double[] last = {Double.NaN, Double.NaN};
            JsonObject terminal = body.terminal(body.startObjective(walk, 800, true), 840, () -> {
                float pitch = server.computeOnServer(s -> s.getPlayerList().getPlayers().get(0).getXRot());
                double[] xz = server.computeOnServer(s -> {
                    var player = s.getPlayerList().getPlayers().get(0);
                    return new double[] {player.getX(), player.getZ()};
                });
                pitches.add(pitch);
                if (!Double.isNaN(last[0]) && Math.hypot(xz[0] - last[0], xz[1] - last[1]) > 0.02) walking.add(pitch);
                last[0] = xz[0];
                last[1] = xz[1];
            });
            BlockPos at = context.computeOnClient(client -> client.player.blockPosition());
            long down = pitches.stream().filter(p -> p >= 55.0f).count();
            long walkingDown = walking.stream().filter(p -> p >= 55.0f).count();
            float min = pitches.stream().min(Float::compare).orElse(Float.NaN);
            String broken = context.computeOnClient(client -> {
                StringBuilder cells = new StringBuilder();
                for (int dz = -6; dz <= 6; dz++) for (int dy = 0; dy <= 3; dy++) {
                    if (client.level.getBlockState(new BlockPos(X + 9, Y + dy, z + dz)).isAir()) {
                        cells.append("[").append(z + dz).append(",").append(Y + dy).append("]");
                    }
                }
                return cells.toString();
            });
            String summary = "status=" + terminal.get("status") + " reason=" + terminal.get("reason") + " at=" + at
                + " broken=" + broken + " ticks=" + pitches.size() + " pitch>=55 on " + down + " min_pitch=" + min
                + " walking_ticks=" + walking.size() + " walking_pitch>=55 on " + walkingDown;
            check("completed".equals(terminal.get("status").getAsString())
                && Math.abs(at.getX() - goal) <= 2 && Math.abs(at.getZ() - z) <= 2, "look_down walk through a wall: " + summary);
            check(!broken.isEmpty(), "the wall was not broken: " + summary);
            check(walking.size() >= 10 && walkingDown * 10 >= walking.size() * 8, "the walk did not look down again: " + summary);
            check(min < 55.0f, "breaking never aimed off the ground: " + summary);
            System.out.println("MINEBOT_LOOK PASS walkLookDownBreaksWall " + summary);
        } finally {
            server.runCommand("kill @e[type=minecraft:enderman]");
            server.runCommand("difficulty peaceful");
            server.runCommand("time set day");
            server.runCommand(fill(x0, Y - 2, z - 6, x1, Y + 6, z + 6, "air"));
            arena(server);
        }
    }

    private static int angryEndermen(TestServerContext server) {
        return server.computeOnServer(s -> (int) s.overworld().getEntitiesOfClass(
            net.minecraft.world.entity.monster.EnderMan.class,
            new net.minecraft.world.phys.AABB(X - 64, Y - 32, Z - 64, X + 96, Y + 32, Z + 64))
            .stream().filter(e -> e.getTarget() != null || e.isAngry() || e.isCreepy()).count());
    }

    /** B (dragon-unit-tests-20261030 §6): the steps the Body reports are checked
     * cell by cell, a wall and a drop into the void cut them short, a one-block
     * bump is stepped onto, and stepTo carries the player to the named cell. */
    private static void stepsAndStepTo(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        arena(server);
        int px = X + 4, pz = Z + 4;
        server.runCommand(fill(px + 2, Y, pz - 2, px + 2, Y + 2, pz + 2, "stone"));      // east: a wall 2 away
        server.runCommand(fill(px - 3, Y - 2, pz - 2, px - 2, Y - 1, pz + 2, "air"));    // west: no ground
        server.runCommand("setblock " + px + " " + Y + " " + (pz + 2) + " minecraft:stone"); // south: a bump
        server.runCommand("clear @p");
        server.runCommand("tp @p " + px + ".5 " + Y + " " + pz + ".5");
        context.waitFor(client -> client.player.onGround() && client.player.blockPosition().getX() == px
            && client.player.blockPosition().getZ() == pz, 200);
        try {
            JsonObject state = body.state();
            check(state.has("steps") && state.get("steps").isJsonArray(), "state has no steps: " + state);
            java.util.Map<String, String> to = new java.util.HashMap<>();
            for (var value : state.getAsJsonArray("steps")) {
                JsonObject step = value.getAsJsonObject();
                to.put(step.get("direction").getAsString(), step.get("to").toString());
            }
            check(("[" + px + "," + Y + "," + (pz - 4) + "]").equals(to.get("north")), "north step " + to);
            check(("[" + px + "," + Y + "," + (pz + 4) + "]").equals(to.get("south")), "south step over the bump " + to);
            check(!to.containsKey("east") && !to.containsKey("west"), "a wall or the void was stepped through " + to);
            JsonArray cell = new JsonArray();
            cell.add(px); cell.add(Y); cell.add(pz - 4);
            JsonObject step = new JsonObject();
            step.add("target", cell);
            JsonObject terminal = body.command("stepTo", step, 100);
            BlockPos at = context.computeOnClient(client -> client.player.blockPosition());
            check("completed".equals(terminal.get("status").getAsString()) && at.getX() == px
                && Math.abs(at.getZ() - (pz - 4)) <= 1, "stepTo north ended " + terminal.get("status") + " "
                + terminal.get("reason") + " at " + at);
            System.out.println("MINEBOT_STEP PASS stepsAndStepTo " + to + " stepped to " + at);
        } finally {
            arena(server);
        }
    }

    /** stepTo with look_down (the Agent's set_walk_gaze reaches Decision's own steps) keeps the
     * pitch the server sees on the ground while stepping; it starts level. */
    private static void stepToLookDown(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        arena(server);
        int px = X + 4, pz = Z + 4;
        server.runCommand("clear @p");
        server.runCommand("tp @p " + px + ".5 " + Y + " " + pz + ".5 0 0");
        context.waitFor(client -> client.player.onGround() && client.player.blockPosition().getX() == px
            && client.player.blockPosition().getZ() == pz, 200);
        context.waitTicks(5);
        try {
            float before = server.computeOnServer(s -> s.getPlayerList().getPlayers().get(0).getXRot());
            JsonArray cell = new JsonArray();
            cell.add(px); cell.add(Y); cell.add(pz - 4);
            JsonObject step = new JsonObject();
            step.add("target", cell);
            step.addProperty("look_down", true);
            java.util.List<Float> moving = new java.util.ArrayList<>();
            double[] last = {Double.NaN, Double.NaN};
            String id = body.startCommand("stepTo", step, 100);
            JsonObject terminal = body.terminal(id, 140, () -> {
                float pitch = server.computeOnServer(s -> s.getPlayerList().getPlayers().get(0).getXRot());
                double[] xz = server.computeOnServer(s -> {
                    var player = s.getPlayerList().getPlayers().get(0);
                    return new double[] {player.getX(), player.getZ()};
                });
                if (!Double.isNaN(last[0]) && Math.hypot(xz[0] - last[0], xz[1] - last[1]) > 0.02) moving.add(pitch);
                last[0] = xz[0];
                last[1] = xz[1];
            });
            BlockPos at = context.computeOnClient(client -> client.player.blockPosition());
            float min = moving.stream().min(Float::compare).orElse(Float.NaN);
            float max = moving.stream().max(Float::compare).orElse(Float.NaN);
            String summary = "status=" + terminal.get("status") + " reason=" + terminal.get("reason") + " at=" + at
                + " pitch_before=" + before + " moving_ticks=" + moving.size() + " server_pitch=" + min + ".." + max;
            check("completed".equals(terminal.get("status").getAsString()) && at.getX() == px
                && Math.abs(at.getZ() - (pz - 4)) <= 1, "stepTo with look_down did not arrive: " + summary);
            check(Math.abs(before) < 10.0f, "the step did not start level: " + summary);
            check(moving.size() >= 3, "too few moving ticks: " + summary);
            check(min >= 55.0f, "stepTo with look_down looked up: " + summary);
            System.out.println("MINEBOT_STEP PASS stepToLookDown " + summary);
        } finally {
            arena(server);
        }
    }

    /** A stone pocket with water pouring in at head height, the H6 obsidian-pit shape. */
    private static void floodedPocket(ClientGameTestContext context, TestServerContext server) {
        server.runCommand("difficulty peaceful");
        arena(server);
        server.runCommand("clear @p");
        server.runCommand("give @p minecraft:iron_pickaxe");
        server.runCommand("give @p minecraft:cobblestone 16");
        server.runCommand(fill(X + 1, Y, Z + 1, X + 3, Y + 3, Z + 3, "stone"));
        server.runCommand(fill(X + 2, Y, Z + 2, X + 2, Y + 1, Z + 2, "air"));
        server.runCommand("setblock " + (X + 3) + " " + (Y + 1) + " " + (Z + 2) + " minecraft:water");
        server.runCommand("tp @p " + (X + 2) + ".5 " + Y + " " + (Z + 2) + ".5");
        context.waitFor(client -> countItem(client, "cobblestone") == 16, 200);
        context.waitTicks(20);
    }

    private static void makeWayOutOfFloodedPocket(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        try {
            floodedPocket(context, server);
            context.runOnClient(client -> {
                BaritoneAPI.getSettings().allowBreak.value = false;
                BaritoneAPI.getSettings().allowPlace.value = false;
            });
            JsonObject denied = body.awaitCommand(body.startObjective("goto:" + (X + 10) + ":" + Y + ":" + (Z + 2), 400), 400);
            check(denied.has("route") && denied.getAsJsonObject("route").get("make_way_steps").getAsInt() == 0,
                "walk without leave made a way " + denied);
            floodedPocket(context, server);
            context.runOnClient(client -> {
                BaritoneAPI.getSettings().allowBreak.value = true;
                BaritoneAPI.getSettings().allowPlace.value = true;
            });
            JsonObject terminal = body.awaitCommand(body.startObjective("goto:" + (X + 2) + ":" + Y + ":" + (Z + 10), 1200), 1200);
            check(terminal.has("status") && "completed".equals(terminal.get("status").getAsString()),
                "walk with leave ended " + terminal);
            System.out.println("MINEBOT_ROUTE PASS makeWayOutOfFloodedPocket denied=" + denied.get("route")
                + " cobblestone_left=" + context.computeOnClient(client -> countItem(client, "cobblestone")));
        } finally {
            context.runOnClient(client -> {
                BaritoneAPI.getSettings().allowBreak.value = true;
                BaritoneAPI.getSettings().allowPlace.value = true;
            });
            server.runCommand(fill(X + 1, Y, Z + 1, X + 3, Y + 3, Z + 3, "air"));
        }
    }

    /** A shaft capped with stone, water pouring in halfway up: swimming finds no air. */
    private static void floodedPit(ClientGameTestContext context, TestServerContext server) {
        server.runCommand("difficulty peaceful");
        arena(server);
        server.runCommand(fill(X + 1, Y, Z + 1, X + 3, Y + 7, Z + 3, "air"));
        server.runCommand("clear @p");
        server.runCommand("give @p minecraft:iron_pickaxe");
        server.runCommand("give @p minecraft:cobblestone 32");
        server.runCommand(fill(X + 1, Y, Z + 1, X + 3, Y + 7, Z + 3, "stone"));
        server.runCommand(fill(X + 2, Y, Z + 2, X + 2, Y + 5, Z + 2, "air"));
        server.runCommand("setblock " + (X + 3) + " " + (Y + 3) + " " + (Z + 2) + " minecraft:water");
        server.runCommand("tp @p " + (X + 2) + ".5 " + Y + " " + (Z + 2) + ".5");
        context.waitFor(client -> countItem(client, "cobblestone") == 32, 200);
        context.waitTicks(40);
    }

    private static void ascendMakesWayFromFloodedPit(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        try {
            floodedPit(context, server);
            JsonObject plain = body.command("ascend", new JsonObject(), 1200);
            check(plain.has("status") && "failed".equals(plain.get("status").getAsString()), "plain ascend ended " + plain);
            server.runCommand("effect give @p minecraft:instant_health 1 10 true");
            // Live runs report disposable_world=true; vertical work may change cells there.
            System.setProperty("minebot.client.disposable_world", "true");
            floodedPit(context, server);
            JsonObject makeWay = new JsonObject();
            makeWay.addProperty("make_way", true);
            JsonObject terminal = body.command("ascend", makeWay, 2400);
            check(terminal.has("status") && "completed".equals(terminal.get("status").getAsString()),
                "make-way ascend ended " + terminal);
            int feetY = context.computeOnClient(client -> client.player.blockPosition().getY());
            check(feetY >= Y + 6, "make-way ascend stopped at y " + feetY);
            System.out.println("MINEBOT_ROUTE PASS ascendMakesWayFromFloodedPit plain=" + plain.get("reason")
                + " feet_y=" + feetY + " reason=" + terminal.get("reason"));
        } finally {
            System.clearProperty("minebot.client.disposable_world");
            server.runCommand(fill(X + 1, Y, Z + 1, X + 3, Y + 9, Z + 3, "air"));
            server.runCommand("tp @p " + X + ".5 " + Y + " " + Z + ".5");
            server.runCommand("effect give @p minecraft:instant_health 1 10 true");
        }
    }

    private static JsonArray coordinates(int x, int y, int z) {
        JsonArray result = new JsonArray();
        result.add(x); result.add(y); result.add(z);
        return result;
    }

    private static void raiseShieldBlocksSkeletonArrows(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        server.runCommand("difficulty peaceful");
        server.runCommand("kill @e[type=!player]");
        arena(server);
        server.runCommand("clear @p");
        server.runCommand("give @p minecraft:shield");
        server.runCommand("difficulty easy");
        int px = X + 3, pz = Z + 4;
        // A summoned skeleton gets no bow by itself; without one it walks up and punches.
        // The helmet keeps it from burning in the daylight the earlier cases leave.
        server.runCommand("summon minecraft:skeleton " + (px + 10) + ".5 " + Y + " " + pz
            + ".5 {PersistenceRequired:1b,equipment:{mainhand:{id:\"minecraft:bow\",count:1},"
            + "head:{id:\"minecraft:leather_helmet\",count:1}}}");
        server.runCommand("tp @p " + px + ".5 " + Y + " " + pz + ".5");
        context.waitFor(client -> client.player.onGround() && countItem(client, "shield") == 1
            && path(client.player.getOffhandItem()).equals("air"), 200);
        // As protection.raise_shield does through equip_item: moveItem, a primitive on the wire, puts the shield in the offhand.
        JsonObject equip = new JsonObject();
        equip.addProperty("from_slot", 0);
        equip.addProperty("to_slot", 40);
        equip.addProperty("count", 1);
        body.primitive("moveItem", equip);
        context.waitFor(client -> path(client.player.getOffhandItem()).equals("shield"), 100);
        context.waitFor(client -> client.level.getEntities(client.player, client.player.getBoundingBox().inflate(16),
            entity -> entity.getType() == net.minecraft.world.entity.EntityType.SKELETON).size() == 1, 100);
        String targetUuid = context.computeOnClient(client -> client.level.getEntities(client.player,
            client.player.getBoundingBox().inflate(16), entity -> entity.getType() == net.minecraft.world.entity.EntityType.SKELETON)
            .stream().map(net.minecraft.world.entity.Entity::getUUID).findFirst().map(java.util.UUID::toString).orElse(""));
        check(!targetUuid.isBlank(), "skeleton was not present at 10 blocks");
        JsonObject action = new JsonObject();
        action.addProperty("target_uuid", targetUuid);
        action.addProperty("duration_ticks", 120);
        float before = context.computeOnClient(client -> client.player.getHealth());
        // Arrows seen server-side while blocking: the skeleton really shot.
        java.util.Set<java.util.UUID> arrows = new java.util.HashSet<>();
        String shieldId = body.startCommand("raiseShield", action, 180);
        JsonObject terminal = body.terminal(shieldId, 220, () -> arrows.addAll(server.computeOnServer(s ->
            s.overworld().getEntities(net.minecraft.world.entity.EntityType.ARROW,
                new net.minecraft.world.phys.AABB(px - 16, Y - 4, pz - 16, px + 16, Y + 8, pz + 16),
                e -> e.getOwner() != null && e.getOwner().getType() == net.minecraft.world.entity.EntityType.SKELETON)
                .stream().map(net.minecraft.world.entity.Entity::getUUID).toList())));
        System.out.println("MINEBOT_PROTECTION shield arrows_seen=" + arrows.size());
        check(!arrows.isEmpty(), "the skeleton shot no arrow during the shield action");
        float after = context.computeOnClient(client -> client.player.getHealth());
        check("completed".equals(terminal.get("status").getAsString()), "raiseShield ended " + terminal);
        check(before == after && terminal.get("health_before").getAsFloat() == terminal.get("health_after").getAsFloat(),
            "shield action changed health from " + before + " to " + after + ": " + terminal);
        check(terminal.get("hits_blocked").getAsInt() > 0, "no blocked hit was reported: " + terminal);
        check(terminal.get("blocking_ticks").getAsInt() > 0, "shield was never observed blocking: " + terminal);
        server.runCommand("difficulty peaceful");
        server.runCommand("kill @e[type=!player]");
        server.runCommand("time set day");
        server.runCommand("gamerule minecraft:natural_health_regeneration true");
        server.runCommand("effect give @p minecraft:instant_health 1 10 true");
        context.waitFor(client -> client.player.getHealth() == 20, 200);
        System.out.println("MINEBOT_PROTECTION PASS raiseShieldBlocksSkeletonArrows health_unchanged=true " + terminal);
    }

    private static void raiseShieldWithoutShieldReportsNoShield(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        server.runCommand("difficulty peaceful");
        server.runCommand("kill @e[type=!player]");
        arena(server);
        server.runCommand("clear @p");
        server.runCommand("difficulty easy");
        int px = X + 3, pz = Z + 4;
        server.runCommand("summon minecraft:skeleton " + (px + 10) + ".5 " + Y + " " + pz
            + ".5 {PersistenceRequired:1b,equipment:{mainhand:{id:\"minecraft:bow\",count:1},"
            + "head:{id:\"minecraft:leather_helmet\",count:1}}}");
        server.runCommand("tp @p " + px + ".5 " + Y + " " + pz + ".5");
        context.waitFor(client -> client.player.onGround() && path(client.player.getOffhandItem()).equals("air"), 200);
        context.waitFor(client -> client.level.getEntities(client.player, client.player.getBoundingBox().inflate(16),
            entity -> entity.getType() == net.minecraft.world.entity.EntityType.SKELETON).size() == 1, 100);
        String targetUuid = context.computeOnClient(client -> client.level.getEntities(client.player,
            client.player.getBoundingBox().inflate(16), entity -> entity.getType() == net.minecraft.world.entity.EntityType.SKELETON)
            .stream().map(net.minecraft.world.entity.Entity::getUUID).findFirst().map(java.util.UUID::toString).orElse(""));
        check(!targetUuid.isBlank(), "skeleton was not present for no-shield result");
        JsonObject action = new JsonObject();
        action.addProperty("target_uuid", targetUuid);
        action.addProperty("duration_ticks", 20);
        String actionId = body.startCommand("raiseShield", action, 40);
        JsonObject terminal = body.terminal(actionId, 60, () -> {});
        check("failed".equals(terminal.get("status").getAsString())
            && "no_shield".equals(terminal.get("reason").getAsString()), "missing shield ended " + terminal);
        server.runCommand("difficulty peaceful");
        server.runCommand("kill @e[type=!player]");
        System.out.println("MINEBOT_PROTECTION PASS raiseShieldWithoutShieldReportsNoShield " + terminal);
    }

    private static void carriedBedCases(ClientGameTestContext context, TestServerContext server, BodyIpc body, String only) {
        check(only.isEmpty() || only.equals("carriedBedSleepsAndRecovers") || only.equals("carriedBedReportsSleepDenial")
                || only.equals("carriedBedTurnsToAnOpenSide"),
            "unknown carried bed GameTest case: " + only);
        for (String name : new String[]{"carriedBedSleepsAndRecovers", "carriedBedReportsSleepDenial", "carriedBedTurnsToAnOpenSide"}) {
            if (!only.isEmpty() && !only.equals(name)) continue;
            server.runCommand("difficulty peaceful");
            server.runCommand("kill @e[type=!player]");
            arena(server);
            server.runCommand("clear @p");
            server.runCommand("give @p minecraft:white_bed");
            server.runCommand("tp @p " + (X + 3) + ".5 " + Y + " " + (Z + 4) + ".5 0 0");
            if (name.equals("carriedBedTurnsToAnOpenSide")) {
                // Facing south (yaw 0) at a stone wall: only the old facing-cell check
                // fails here (production line: the side loop in tickPlaceBedAndSleep).
                server.runCommand("setblock " + (X + 3) + " " + Y + " " + (Z + 5) + " minecraft:stone");
                server.runCommand("time set midnight");
            } else if (name.equals("carriedBedSleepsAndRecovers")) server.runCommand("time set midnight");
            else {
                server.runCommand("time set midnight");
                server.runCommand("difficulty easy");
                server.runCommand("summon minecraft:zombie " + (X + 8) + ".5 " + Y + " " + (Z + 4) + ".5 {NoAI:1b,Silent:1b,PersistenceRequired:1b}");
            }
            context.waitFor(client -> client.player.onGround() && countItem(client, "white_bed") == 1, 200);
            JsonObject terminal = body.command("placeBedAndSleep", new JsonObject(), 6000);
            int bedCount = countItem(context, "white_bed");
            if (name.equals("carriedBedTurnsToAnOpenSide")) {
                check("completed".equals(terminal.get("status").getAsString()) && bedCount == 1
                    && terminal.get("bed_recovered").getAsBoolean() && terminal.has("bed_blocked_cells")
                    && "foot_cell_occupied".equals(terminal.getAsJsonArray("bed_blocked_cells").get(0).getAsJsonObject()
                        .get("blocked").getAsString()),
                    "carried bed did not turn to an open side: " + terminal);
                server.runCommand("setblock " + (X + 3) + " " + Y + " " + (Z + 5) + " minecraft:air");
            } else if (name.equals("carriedBedSleepsAndRecovers")) {
                check("completed".equals(terminal.get("status").getAsString()) && bedCount == 1
                    && terminal.get("bed_recovered").getAsBoolean(), "carried bed did not sleep and return: " + terminal);
            } else {
                check("cannot_sleep_now".equals(terminal.get("reason").getAsString())
                    && terminal.get("bed_game_message").getAsString().length() > 0
                    && terminal.get("bed_recovered").getAsBoolean() && bedCount == 1,
                    "sleep denial omitted the game's message or bed recovery: " + terminal);
            }
            server.runCommand("difficulty peaceful");
            server.runCommand("kill @e[type=!player]");
            server.runCommand("time set day");
            server.runCommand("gamerule minecraft:natural_health_regeneration true");
            server.runCommand("effect give @p minecraft:instant_health 1 10 true");
            context.waitFor(client -> client.player.getHealth() == 20, 200);
            System.out.println("MINEBOT_PROTECTION PASS " + name + " bed_count=" + bedCount + " " + terminal);
        }
    }

    /** The game's own words on the respawn point, and the world spawn, reach the state
     * (vision-and-home-20261028.md §7.7 item 5): sleeping in the carried bed sets the respawn
     * point to it; with the bed picked up again, dying finds no bed. */
    private static void spawnMessagesReachTheState(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        server.runCommand("difficulty peaceful");
        server.runCommand("kill @e[type=!player]");
        arena(server);
        server.runCommand("clear @p");
        server.runCommand("setworldspawn " + (X + 6) + " " + Y + " " + (Z + 6));
        server.runCommand("give @p minecraft:white_bed");
        server.runCommand("tp @p " + (X + 3) + ".5 " + Y + " " + (Z + 4) + ".5");
        server.runCommand("time set midnight");
        context.waitFor(client -> client.player.onGround() && countItem(client, "white_bed") == 1, 200);
        JsonObject spawn = body.state().getAsJsonObject("world_spawn");
        check(spawn != null && "minecraft:overworld".equals(spawn.get("dimension").getAsString())
                && spawn.getAsJsonArray("pos").get(0).getAsInt() == X + 6
                && spawn.getAsJsonArray("pos").get(2).getAsInt() == Z + 6,
            "the state's world spawn is not the one set: " + spawn);
        JsonObject terminal = body.command("placeBedAndSleep", new JsonObject(), 6000);
        check("completed".equals(terminal.get("status").getAsString()), "placeBedAndSleep ended " + terminal);
        JsonObject set = body.state().getAsJsonObject("spawn_message");
        check(set != null && SpawnMessage.SET.equals(set.get("key").getAsString()) && set.has("block")
                && "white_bed".equals(set.get("block").getAsString()) && set.getAsJsonArray("pos").size() == 3,
            "sleeping did not leave 'Respawn point set' with the bed in the state: " + set);
        // placeBedAndSleep picked the bed up again, so dying finds no bed.
        server.runCommand("kill @p");
        context.waitFor(client -> client.player == null || !client.player.isAlive()
            || client.screen instanceof net.minecraft.client.gui.screens.DeathScreen, 200);
        context.waitFor(client -> client.player != null && client.player.isAlive()
            && !(client.screen instanceof net.minecraft.client.gui.screens.DeathScreen), 400);
        JsonObject lost = null;
        for (int tick = 0; tick < 100; tick++) {
            lost = body.state().getAsJsonObject("spawn_message");
            if (lost != null && SpawnMessage.NOT_VALID.equals(lost.get("key").getAsString())) break;
            context.waitTick();
        }
        check(lost != null && SpawnMessage.NOT_VALID.equals(lost.get("key").getAsString())
                && lost.get("seq").getAsLong() > set.get("seq").getAsLong(),
            "respawning without the bed did not leave 'no home bed' in the state: " + lost);
        server.runCommand("time set day");
        server.runCommand("setworldspawn " + (X + 3) + " " + Y + " " + (Z + 4));
        server.runCommand("tp @p " + (X + 3) + ".5 " + Y + " " + (Z + 4) + ".5");
        context.waitFor(client -> client.player.onGround(), 200);
        System.out.println("MINEBOT_SPAWN_MESSAGES PASS set=" + set + " lost=" + lost + " world_spawn=" + spawn);
    }

    /** What store_items and take_items stand on (vision-and-home-20261028.md §7.7, Body): the client reads all
     * 54 slots of a double chest, and moves a stack into its second half and back out, checked on the server. */
    private static void chestTransfersReachBothHalves(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        server.runCommand("difficulty peaceful");
        server.runCommand("time set day");
        arena(server);
        server.runCommand("clear @p");
        // A left chest facing north joins the right one east of it.
        BlockPos left = new BlockPos(X + 5, Y, Z + 6), right = new BlockPos(X + 6, Y, Z + 6);
        server.runCommand("setblock " + left.getX() + " " + Y + " " + left.getZ() + " minecraft:chest[facing=north,type=left]");
        server.runCommand("setblock " + right.getX() + " " + Y + " " + right.getZ() + " minecraft:chest[facing=north,type=right]");
        server.runCommand("item replace block " + right.getX() + " " + Y + " " + right.getZ()
            + " container.0 with minecraft:cobblestone 64");
        server.runCommand("give @p minecraft:dirt 10");
        server.runCommand("tp @p " + (X + 5) + ".5 " + Y + " " + (Z + 4) + ".5");
        context.waitFor(client -> client.player.onGround() && countItem(client, "dirt") == 10, 200);
        int dirtSlot = context.computeOnClient(client -> {
            for (int slot = 0; slot < 36; slot++) if ("dirt".equals(path(client.player.getInventory().getItem(slot)))) return slot;
            return -1;
        });
        JsonObject where = new JsonObject();
        JsonArray pos = new JsonArray();
        pos.add(left.getX()); pos.add(left.getY()); pos.add(left.getZ());
        where.add("pos", pos);
        JsonObject before = body.control("container_read", where);
        check(before.get("ok").getAsBoolean() && before.getAsJsonObject("data").getAsJsonArray("slots").size() == 54
                && slotCount(before, "cobblestone") == 64,
            "the double chest was not read whole: " + before);

        JsonObject store = transfer(pos, "bot_to_container", 30, dirtSlot);
        JsonObject stored = body.command("containerTransfer", store, 200);
        JsonObject after = body.control("container_read", where);
        JsonObject thirty = after.getAsJsonObject("data").getAsJsonArray("slots").get(30).getAsJsonObject();
        check("completed".equals(stored.get("status").getAsString()) && stored.get("count").getAsInt() == 10
                && thirty.has("item") && "minecraft:dirt".equals(thirty.get("item").getAsString())
                && thirty.get("count").getAsInt() == 10
                && chestCount(server, left, "dirt") + chestCount(server, right, "dirt") == 10 && countItem(context, "dirt") == 0,
            "storing into slot 30 ended " + stored + "; slot 30 reads " + thirty);

        JsonObject taken = body.command("containerTransfer", transfer(pos, "container_to_bot", 30, dirtSlot), 200);
        check("completed".equals(taken.get("status").getAsString()) && countItem(context, "dirt") == 10
                && chestCount(server, left, "dirt") + chestCount(server, right, "dirt") == 0
                && chestCount(server, left, "cobblestone") + chestCount(server, right, "cobblestone") == 64,
            "taking slot 30 back ended " + taken + "; inventory " + inventory(context));
        server.runCommand(fill(X + 5, Y, Z + 6, X + 6, Y, Z + 6, "air"));
        server.runCommand("clear @p");
        System.out.println("MINEBOT_CHEST PASS stored=" + stored.get("count") + " taken=" + taken.get("count"));
    }

    private static JsonObject transfer(JsonArray pos, String direction, int containerSlot, int botSlot) {
        JsonObject params = new JsonObject();
        params.add("pos", pos);
        params.addProperty("direction", direction);
        params.addProperty("container_slot", containerSlot);
        params.addProperty("bot_slot", botSlot);
        params.addProperty("count", 10);
        params.addProperty("item", "dirt");
        return params;
    }

    private static int slotCount(JsonObject read, String item) {
        int total = 0;
        for (var element : read.getAsJsonObject("data").getAsJsonArray("slots")) {
            JsonObject slot = element.getAsJsonObject();
            if (slot.has("item") && slot.get("item").getAsString().equals("minecraft:" + item)) total += slot.get("count").getAsInt();
        }
        return total;
    }

    private static int chestCount(TestServerContext server, BlockPos pos, String item) {
        return server.computeOnServer(s -> {
            if (!(s.overworld().getBlockEntity(pos) instanceof net.minecraft.world.level.block.entity.ChestBlockEntity chest)) return -1;
            int total = 0;
            for (int slot = 0; slot < chest.getContainerSize(); slot++) {
                ItemStack stack = chest.getItem(slot);
                if (!stack.isEmpty() && path(stack).equals(item)) total += stack.getCount();
            }
            return total;
        });
    }

    private static void shelterCases(ClientGameTestContext context, TestServerContext server, BodyIpc body, String only) {
        check(only.isEmpty() || java.util.Set.of("shelter", "shelterSealsAgainstZombie", "shelterRefusesInsufficientBlocks",
            "shelterHandlesOccupiedWallTruthfully", "shelterFillsOnlyTunnelOpenings", "shelterLookOutClosesHeadPort",
            "shelterNeverHitsCreeper", "shelterReportsDisplacement").contains(only), "unknown GameTest case: " + only);
        if (only.isEmpty() || only.equals("shelter") || only.equals("shelterSealsAgainstZombie")) shelterSealsAgainstZombie(context, server, body);
        if (only.isEmpty() || only.equals("shelter") || only.equals("shelterRefusesInsufficientBlocks")) shelterRefusesInsufficientBlocks(context, server, body);
        if (only.isEmpty() || only.equals("shelter") || only.equals("shelterHandlesOccupiedWallTruthfully")) shelterHandlesOccupiedWallTruthfully(context, server, body);
        if (only.isEmpty() || only.equals("shelter") || only.equals("shelterFillsOnlyTunnelOpenings")) shelterFillsOnlyTunnelOpenings(context, server, body);
        if (only.isEmpty() || only.equals("shelter") || only.equals("shelterLookOutClosesHeadPort")) shelterLookOutClosesHeadPort(context, server, body);
        if (only.isEmpty() || only.equals("shelter") || only.equals("shelterNeverHitsCreeper")) shelterNeverHitsCreeper(context, server, body);
        if (only.isEmpty() || only.equals("shelter") || only.equals("shelterReportsDisplacement")) shelterReportsDisplacement(context, server, body);
        // Give the later cases the world they were written for.
        server.runCommand("difficulty peaceful");
        server.runCommand("kill @e[type=!player]");
        server.runCommand("time set day");
        server.runCommand("gamerule minecraft:natural_health_regeneration true");
        server.runCommand("effect give @p minecraft:instant_health 1 10 true");
        context.waitFor(client -> client.player.getHealth() == 20, 200);
    }

    private static void threatFacts(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        server.runCommand("difficulty easy");
        server.runCommand("kill @e[type=!player]");
        server.runCommand("tp @p " + (X + 3) + ".5 " + Y + " " + (Z + 4) + ".5");
        // An ordinary creeper three blocks away walks up and starts to swell by itself.
        server.runCommand("summon minecraft:creeper " + (X + 6) + ".5 " + Y + " " + (Z + 4) + ".5 {PersistenceRequired:1b}");
        // body.state() waits ticks, so it is polled here on the test thread.
        boolean swelling = false;
        JsonObject seen = null;
        for (int tick = 0; tick < 200 && !swelling; tick++) {
            seen = body.state();
            if (seen.has("entities")) for (var raw : seen.getAsJsonArray("entities")) {
                JsonObject entity = raw.getAsJsonObject();
                if (entity.has("type") && entity.get("type").getAsString().endsWith("creeper")
                    && entity.has("swelling") && entity.get("swelling").getAsBoolean()) swelling = true;
            }
            if (!swelling) context.waitTick();
        }
        server.runCommand("kill @e[type=creeper]");
        check(swelling, "a creeper next to the Body was not reported swelling: " + seen);
        server.runCommand("kill @e[type=creeper]");
        server.runCommand("summon minecraft:zombie " + (X + 5) + ".5 " + Y + " " + (Z + 4) + ".5 {PersistenceRequired:1b}");
        context.waitFor(client -> client.player.getHealth() < 20, 400);
        JsonObject state = body.state();
        check(state.has("hurt_source"), "damage event did not reach Body state: " + state);
        check("zombie".equals(state.getAsJsonObject("hurt_source").get("type").getAsString()),
            "hurt source was not the zombie: " + state);
        System.out.println("MINEBOT_THREAT_FACTS PASS creeper_swelling=true hurt_source=zombie " + state);
        server.runCommand("difficulty peaceful");
        server.runCommand("kill @e[type=!player]");
        server.runCommand("effect give @p minecraft:instant_health 1 10 true");
        context.waitFor(client -> client.player.getHealth() == 20, 200);
    }

    private static final BlockPos SHELTER_CENTER = new BlockPos(X + 3, Y, Z + 4);

    private static void shelterScene(ClientGameTestContext context, TestServerContext server, int blocks) {
        server.runCommand("difficulty peaceful");
        server.runCommand("kill @e[type=!player]");
        arena(server);
        server.runCommand("time set midnight");
        server.runCommand("gamerule minecraft:natural_health_regeneration false");
        server.runCommand("clear @p");
        server.runCommand("effect give @p minecraft:instant_health 1 10 true");
        server.runCommand("give @p minecraft:cobblestone " + blocks);
        server.runCommand("tp @p " + (SHELTER_CENTER.getX() + 0.5) + " " + Y + " " + (SHELTER_CENTER.getZ() + 0.5));
        context.waitFor(client -> client.player.onGround() && client.player.blockPosition().equals(SHELTER_CENTER)
            && countItem(client, "cobblestone") == blocks && client.player.getHealth() == 20, 400);
        // Wait for the client's version of the cleared arena, not just its inventory.
        context.waitFor(client -> client.level.getBlockState(SHELTER_CENTER.above(2)).isAir()
            && client.level.getBlockState(SHELTER_CENTER.north()).isAir(), 200);
    }

    private static void shelterZombie(ClientGameTestContext context, TestServerContext server, BlockPos pos, boolean noAi) {
        server.runCommand("difficulty easy");
        server.runCommand("summon minecraft:zombie " + (pos.getX() + 0.5) + " " + pos.getY() + " " + (pos.getZ() + 0.5)
            + " {Silent:1b,PersistenceRequired:1b" + (noAi ? ",NoAI:1b" : "") + "}");
        context.waitFor(client -> !client.level.getEntities(client.player, client.player.getBoundingBox().inflate(16),
            entity -> entity.getType() == net.minecraft.world.entity.EntityType.ZOMBIE && entity.isAlive()).isEmpty(), 200);
    }

    private static java.util.List<BlockPos> envelope() {
        java.util.List<BlockPos> cells = new java.util.ArrayList<>();
        cells.add(SHELTER_CENTER.below());
        cells.add(SHELTER_CENTER.above(2));
        for (net.minecraft.core.Direction side : new net.minecraft.core.Direction[]{net.minecraft.core.Direction.NORTH,
            net.minecraft.core.Direction.EAST, net.minecraft.core.Direction.SOUTH, net.minecraft.core.Direction.WEST}) {
            cells.add(SHELTER_CENTER.relative(side));
            cells.add(SHELTER_CENTER.above().relative(side));
        }
        return cells;
    }

    private static java.util.List<BlockPos> openServerCells(TestServerContext server) {
        return server.computeOnServer(s -> envelope().stream().filter(pos -> !s.overworld().getBlockState(pos)
            .isCollisionShapeFullBlock(s.overworld(), pos)).toList());
    }

    private static void shelterResultMatchesServer(TestServerContext server, JsonObject terminal) {
        java.util.Set<BlockPos> stated = new java.util.HashSet<>();
        for (var raw : terminal.getAsJsonArray("open_cells")) {
            JsonArray p = raw.getAsJsonArray();
            stated.add(new BlockPos(p.get(0).getAsInt(), p.get(1).getAsInt(), p.get(2).getAsInt()));
        }
        check(stated.equals(new java.util.HashSet<>(openServerCells(server))), "open cells disagree with the server: " + terminal);
        check("completed".equals(terminal.get("status").getAsString()) == stated.isEmpty(), "shelter status disagrees with its holes: " + terminal);
    }

    private static void shelterSealsAgainstZombie(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        shelterScene(context, server, 16);
        shelterZombie(context, server, SHELTER_CENTER.south(5), false);
        JsonObject terminal = body.command("shelterInPlace", new JsonObject(), 1200);
        shelterResultMatchesServer(server, terminal);
        check("completed".equals(terminal.get("status").getAsString()), "shelter did not seal: " + terminal);
        float health = server.computeOnServer(s -> s.getPlayerList().getPlayers().getFirst().getHealth());
        long until = server.computeOnServer(s -> s.overworld().getGameTime()) + 100;
        for (int i = 0; i < 1000 && server.computeOnServer(s -> s.overworld().getGameTime()) < until; i++) {
            context.waitTick();
            float now = server.computeOnServer(s -> s.getPlayerList().getPlayers().getFirst().getHealth());
            check(now == health, "zombie reached the enclosed Body: health " + health + " -> " + now);
        }
        check(server.computeOnServer(s -> s.overworld().getGameTime()) >= until, "server did not run the required five seconds");
        check(server.computeOnServer(s -> s.overworld().getEntitiesOfClass(net.minecraft.world.entity.monster.zombie.Zombie.class,
            new net.minecraft.world.phys.AABB(SHELTER_CENTER).inflate(16)).stream().anyMatch(entity -> entity.isAlive())), "zombie vanished during the shelter proof");
        System.out.println("MINEBOT_SHELTER PASS shelterSealsAgainstZombie server_shell=solid health_unchanged_server_ticks=100 " + terminal);
    }

    private static void shelterRefusesInsufficientBlocks(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        shelterScene(context, server, 3);
        JsonObject terminal = body.command("shelterInPlace", new JsonObject(), 1200);
        check("not_enough_blocks".equals(terminal.get("reason").getAsString()), "short inventory ended " + terminal);
        check(terminal.get("blocks_needed").getAsInt() == 10 && terminal.get("blocks_held").getAsInt() == 3, "wrong material count: " + terminal);
        check(countItem(context, "cobblestone") == 3 && openServerCells(server).size() == 9
            && "air".equals(block(server, SHELTER_CENTER.above(2).north())), "short inventory changed the world: " + terminal);
        check(terminal.get("world_edit_count").getAsInt() == 0, "refusal emitted edits: " + terminal);
        System.out.println("MINEBOT_SHELTER PASS shelterRefusesInsufficientBlocks placed=0 " + terminal);
    }

    private static void shelterHandlesOccupiedWallTruthfully(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        shelterScene(context, server, 16);
        BlockPos occupied = SHELTER_CENTER.north();
        shelterZombie(context, server, occupied, true);
        JsonObject terminal = body.command("shelterInPlace", new JsonObject(), 1200);
        check(terminal.get("attacks").getAsInt() == 1, "wall blocker did not receive exactly one attack: " + terminal);
        check(server.computeOnServer(s -> s.overworld().getEntitiesOfClass(net.minecraft.world.entity.monster.zombie.Zombie.class,
            new net.minecraft.world.phys.AABB(SHELTER_CENTER).inflate(16)).stream().anyMatch(z -> z.getHealth() < 20)), "the reported hit did no server damage");
        shelterResultMatchesServer(server, terminal);
        if (!"completed".equals(terminal.get("status").getAsString())) {
            check("shelter_unsealable".equals(terminal.get("reason").getAsString()), "occupied wall ended " + terminal);
            boolean named = false;
            for (var raw : terminal.getAsJsonArray("blocked_cells")) {
                JsonObject cell = raw.getAsJsonObject();
                if (cell.getAsJsonArray("pos").toString().equals("[" + occupied.getX() + "," + occupied.getY() + "," + occupied.getZ() + "]")
                    && cell.has("mob") && cell.get("mob").getAsString().equals("zombie")) named = true;
            }
            check(named, "unsealable result did not name the occupied cell and zombie: " + terminal);
        }
        System.out.println("MINEBOT_SHELTER PASS shelterHandlesOccupiedWallTruthfully attacks=1 result_matches_server=true " + terminal);
    }

    private static void shelterFillsOnlyTunnelOpenings(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        shelterScene(context, server, 16);
        BlockPos c = SHELTER_CENTER;
        server.runCommand(fill(c.getX() - 2, Y, c.getZ() - 2, c.getX() + 2, Y + 3, c.getZ() + 2, "stone"));
        server.runCommand(fill(c.getX(), Y, c.getZ() - 2, c.getX(), Y + 1, c.getZ() + 2, "air"));
        context.waitFor(client -> client.level.getBlockState(c.above(2)).is(net.minecraft.world.level.block.Blocks.STONE)
            && client.level.getBlockState(c.north()).isAir(), 200);
        JsonObject terminal = body.command("shelterInPlace", new JsonObject(), 1200);
        shelterResultMatchesServer(server, terminal);
        check("completed".equals(terminal.get("status").getAsString()), "tunnel did not seal: " + terminal);
        check(terminal.get("blocks_needed").getAsInt() == 4 && countItem(context, "cobblestone") == 12, "tunnel used blocks for existing walls: " + terminal);
        check("stone".equals(block(server, c.east())) && "stone".equals(block(server, c.west()))
            && "stone".equals(block(server, c.above(2))) && terminal.get("world_edit_count").getAsInt() == 4, "tunnel changed existing stone: " + terminal);
        System.out.println("MINEBOT_SHELTER PASS shelterFillsOnlyTunnelOpenings placed=4 existing_stone_unchanged=true " + terminal);
    }

    private static void shelterLookOutClosesHeadPort(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        shelterScene(context, server, 16);
        shelterZombie(context, server, SHELTER_CENTER.south(5), true);
        JsonObject sealed = body.command("shelterInPlace", new JsonObject(), 1200);
        check("completed".equals(sealed.get("status").getAsString()), "port fixture did not seal: " + sealed);
        // Default side faces the last observed hostile; no side argument here.
        JsonObject terminal = body.command("shelterInPlace", params("action", "look_out"), 1600);
        check("completed".equals(terminal.get("status").getAsString()), "look_out ended " + terminal);
        boolean seen = false;
        for (var raw : terminal.getAsJsonArray("seen")) {
            JsonObject entity = raw.getAsJsonObject();
            if (entity.get("type").getAsString().equals("zombie") && Math.abs(entity.get("distance").getAsDouble() - 5.0) < 0.1) seen = true;
        }
        check(seen, "the port did not report the zombie five blocks away: " + terminal);
        check(terminal.getAsJsonArray("port").toString().equals("[" + SHELTER_CENTER.getX() + "," + (Y + 1) + "," + (SHELTER_CENTER.getZ() + 1) + "]"), "wrong port: " + terminal);
        check("cobblestone".equals(block(server, SHELTER_CENTER.above().south()))
            && "cobblestone".equals(block(server, SHELTER_CENTER.south())) && terminal.get("port_closed").getAsBoolean(), "head port was not resealed: " + terminal);
        check(openServerCells(server).isEmpty(), "look_out left a hole in the server shell");
        System.out.println("MINEBOT_SHELTER PASS shelterLookOutClosesHeadPort seen=zombie distance=5 port_closed=true feet_unchanged=true " + terminal);
    }

    private static void shelterNeverHitsCreeper(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        shelterScene(context, server, 16);
        server.runCommand("difficulty easy");
        server.runCommand("summon minecraft:creeper " + (SHELTER_CENTER.getX() + 0.5) + " " + Y + " " + (SHELTER_CENTER.getZ() - 0.5) + " {NoAI:1b,PersistenceRequired:1b}");
        context.waitFor(client -> !client.level.getEntities(client.player, client.player.getBoundingBox().inflate(4), entity -> entity instanceof net.minecraft.world.entity.monster.Creeper).isEmpty(), 200);
        JsonObject terminal = body.command("shelterInPlace", new JsonObject(), 1200);
        check("shelter_unsealable".equals(terminal.get("reason").getAsString()) && terminal.get("attacks").getAsInt() == 0, "creeper was struck: " + terminal);
        shelterResultMatchesServer(server, terminal);
        System.out.println("MINEBOT_SHELTER PASS shelterNeverHitsCreeper attacks=0 " + terminal);
    }

    private static void shelterReportsDisplacement(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        shelterScene(context, server, 16);
        String actionId = body.startCommand("shelterInPlace", new JsonObject(), 1200);
        context.waitTicks(4);
        server.runCommand("tp @p " + (SHELTER_CENTER.getX() + 3.5) + " " + Y + " " + (SHELTER_CENTER.getZ() + 0.5));
        JsonObject terminal = body.awaitCommand(actionId, 1200);
        check("displaced".equals(terminal.get("reason").getAsString()), "displacement ended " + terminal);
        check(Math.floor(terminal.getAsJsonArray("position").get(0).getAsDouble()) == SHELTER_CENTER.getX() + 3, "displacement omitted actual position: " + terminal);
        context.waitTicks(20);
        check(context.computeOnClient(client -> client.player.blockPosition().getX()) == SHELTER_CENTER.getX() + 3, "shelter walked back by itself");
        System.out.println("MINEBOT_SHELTER PASS shelterReportsDisplacement no_walk_back=true " + terminal);
    }

    private static void arena(TestServerContext server) {
        server.runCommand(fill(X - 6, Y - 1, Z - 6, X + 14, Y + 6, Z + 12, "air"));
        server.runCommand(fill(X - 6, Y - 2, Z - 6, X + 14, Y - 1, Z + 12, "stone"));
    }

    /** eval-20261019 track P: every bucket and fire use and 114 meals failed
     * because the item was put into slot 0 and Baritone put the pickaxe back. */
    private static void heldItemSurvivesBaritone(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        fullHotbar(context, server);
        body.primitive("selectItem", params("item", "water_bucket"));
        String right = inventory(context) + " | server " + serverInventory(server);
        settle(context, () -> false);
        String held = context.computeOnClient(client -> path(client.player.getMainHandItem()));
        check("water_bucket".equals(held), "after selectItem with a full hotbar the hand holds " + held
            + "; right after: " + right + "; 100 ticks later: " + inventory(context) + " | server " + serverInventory(server));
    }

    private static void bucketFillsFromSource(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        BlockPos source = new BlockPos(X + 5, Y - 1, Z + 4);
        server.runCommand("setblock " + source.getX() + " " + source.getY() + " " + source.getZ() + " minecraft:water");
        fullHotbar(context, server);
        use(context, body, "bucket", source.getX() + 0.5, Y - 0.05, source.getZ() + 0.5);
        settle(context, () -> countItem(context, "water_bucket") == 2 && "air".equals(block(server, source)));
        check(countItem(context, "water_bucket") == 2, "the bucket was not filled: " + inventory(context));
        check("air".equals(block(server, source)), "the water source is still " + block(server, source));
    }

    /** Liquid goes into the cell in front of the clicked face (AltoClef's casts). */
    private static void waterPouredThroughAFloorFace(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        BlockPos cell = new BlockPos(X + 7, Y, Z + 4);
        fullHotbar(context, server);
        use(context, body, "water_bucket", cell.getX() + 0.5, Y - 0.01, cell.getZ() + 0.5);
        settle(context, () -> "water".equals(block(server, cell)));
        check("water".equals(block(server, cell)), "the cell above the clicked floor is " + block(server, cell));
        server.runCommand("setblock " + cell.getX() + " " + cell.getY() + " " + cell.getZ() + " minecraft:air");
    }

    private static void flintLightsTheCellAbove(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        BlockPos cell = new BlockPos(X + 3, Y, Z + 6);
        fullHotbar(context, server);
        use(context, body, "flint_and_steel", cell.getX() + 0.5, Y - 0.01, cell.getZ() + 0.5);
        settle(context, () -> "fire".equals(block(server, cell)));
        check("fire".equals(block(server, cell)), "the cell above the floor is " + block(server, cell));
    }

    /** eval-20261028-ladder run-1: the named exit was dry when chosen, then
     * the lava flowed into it, and leaveLava walked the player to its own
     * cell until the deadline. Given an exit that is lava now, it still gets
     * out onto dry ground. */
    private static void leavesLavaWhenTheNamedExitTurnedToLava(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        int px = X + 11, pz = Z + 9;
        server.runCommand("clear @p");
        server.runCommand("effect give @p minecraft:instant_health 1 10 true");
        server.runCommand(fill(px - 1, Y - 1, pz - 1, px + 1, Y - 1, pz + 1, "lava"));
        server.runCommand("tp @p " + px + ".5 " + (Y - 1) + " " + pz + ".5");
        context.waitFor(client -> client.player.isInLava(), 200);
        JsonArray stale = new JsonArray();
        stale.add(px + 1);
        stale.add(Y - 1);
        stale.add(pz);
        JsonObject leave = new JsonObject();
        leave.add("target", stale);
        JsonObject terminal = body.command("leaveLava", leave, 100);
        check("completed".equals(terminal.get("status").getAsString()), "leaveLava to a cell turned lava ended " + terminal);
        float health = context.computeOnClient(client -> client.player.getHealth());
        boolean stillInLava = context.computeOnClient(client -> client.player.isInLava());
        check(health > 0 && !stillInLava, "leaveLava to a cell turned lava ended with health " + health
            + (stillInLava ? ", still in lava" : ""));
        server.runCommand(fill(px - 1, Y - 1, pz - 1, px + 1, Y - 1, pz + 1, "stone"));
        context.waitFor(client -> !client.player.isOnFire(), 400);
        server.runCommand("effect give @p minecraft:instant_health 1 10 true");
    }

    /** eval-20261020-m1 run-2: the Body burned to death in lava while every
     * frame offered only staying. The state says it is in lava and names the
     * dry cell to step out onto; leaveLava gets the player out alive. (A
     * Baritone walk there, as move_to does, failed at once: no path starts in
     * lava.) */
    private static void leavesLavaOntoTheReportedExit(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        int px = X + 11, pz = Z + 9;
        server.runCommand("clear @p");
        server.runCommand(fill(px - 1, Y - 1, pz - 1, px + 1, Y - 1, pz + 1, "lava"));
        server.runCommand("tp @p " + px + ".5 " + (Y - 1) + " " + pz + ".5");
        context.waitFor(client -> client.player.isInLava(), 200);
        JsonObject inLava = body.state();
        check(inLava.has("in_lava") && inLava.get("in_lava").getAsBoolean(), "in lava, the state said " + inLava.get("in_lava"));
        check(inLava.has("lava_exit") && inLava.get("lava_exit").isJsonArray(), "in lava, no exit was named: " + inLava.get("lava_exit"));
        JsonArray exit = inLava.getAsJsonArray("lava_exit");
        int ex = exit.get(0).getAsInt(), ey = exit.get(1).getAsInt(), ez = exit.get(2).getAsInt();
        check(ey == Y && Math.max(Math.abs(ex - px), Math.abs(ez - pz)) == 2,
            "the exit " + exit + " is not on the rim around " + px + "," + (Y - 1) + "," + pz);
        JsonObject leave = new JsonObject();
        leave.add("target", exit);
        JsonObject terminal = body.command("leaveLava", leave, 100);
        check("completed".equals(terminal.get("status").getAsString()), "leaveLava ended " + terminal);
        float health = context.computeOnClient(client -> client.player.getHealth());
        boolean stillInLava = context.computeOnClient(client -> client.player.isInLava());
        check(health > 0 && !stillInLava, "leaveLava ended with health " + health
            + (stillInLava ? ", still in lava" : ""));
        JsonObject out = body.state();
        check(!out.get("in_lava").getAsBoolean() && out.get("lava_exit").isJsonNull(),
            "out of the lava, the state still said in_lava " + out.get("in_lava") + ", exit " + out.get("lava_exit"));
        check(out.get("on_fire").getAsBoolean(), "just out of the lava the state did not say on fire");
        // put_out_fire: the water bucket poured through the top face of the
        // block the state says the player stands on, as use_on_block aims it.
        check(out.get("standing_on").isJsonArray(), "burning on the ground, the state named no block stood on");
        JsonArray on = out.getAsJsonArray("standing_on");
        BlockPos cell = new BlockPos(on.get(0).getAsInt(), on.get(1).getAsInt() + 1, on.get(2).getAsInt());
        server.runCommand("give @p minecraft:water_bucket");
        context.waitFor(client -> countItem(client, "water_bucket") == 1, 100);
        use(context, body, "water_bucket", cell.getX() + 0.5, cell.getY() - 0.01, cell.getZ() + 0.5);
        settle(context, () -> !context.computeOnClient(client -> client.player.isOnFire()));
        check(!context.computeOnClient(client -> client.player.isOnFire()), "the poured water did not put the fire out; "
            + cell + " is " + block(server, cell));
    }

    /** The client carries a synced cloud's radius and particle, and the existing
     * run-away objective gets the Body beyond the cloud's radius plus two. */
    /** D5 (mechanism-map §11.6): an ore open to the air is said with its
     * position; one sealed in stone is not. */
    private static void exposedOreFacts(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        arena(server);
        try {
            server.runCommand("setblock " + (X + 3) + " " + Y + " " + (Z + 4) + " minecraft:coal_ore");
            server.runCommand(fill(X + 7, Y, Z + 2, X + 9, Y + 2, Z + 4, "stone"));
            server.runCommand("setblock " + (X + 8) + " " + (Y + 1) + " " + (Z + 3) + " minecraft:iron_ore");
            server.runCommand("tp @p " + (X + 1) + ".5 " + Y + " " + (Z + 1) + ".5");
            context.waitFor(client -> client.player.onGround()
                && "coal_ore".equals(BuiltInRegistries.BLOCK.getKey(client.level.getBlockState(new BlockPos(X + 3, Y, Z + 4)).getBlock()).getPath())
                && "iron_ore".equals(BuiltInRegistries.BLOCK.getKey(client.level.getBlockState(new BlockPos(X + 8, Y + 1, Z + 3)).getBlock()).getPath()), 200);
            JsonObject state = body.state();
            check(state.has("exposed_ores") && state.get("exposed_ores").isJsonArray(), "no exposed_ores list: " + state.get("exposed_ores"));
            JsonArray ores = state.getAsJsonArray("exposed_ores");
            check(ores.size() == 1, "expected only the open coal_ore, got " + ores);
            JsonObject coal = ores.get(0).getAsJsonObject();
            check("coal_ore".equals(coal.get("block").getAsString()), "ore was " + coal);
            JsonArray pos = coal.getAsJsonArray("pos");
            check(pos.get(0).getAsInt() == X + 3 && pos.get(1).getAsInt() == Y && pos.get(2).getAsInt() == Z + 4, "ore pos was " + coal);
            check(coal.get("dist").getAsDouble() > 2.0 && coal.get("dist").getAsDouble() < 6.0, "ore dist was " + coal);
        } finally {
            server.runCommand(fill(X + 3, Y, Z + 4, X + 3, Y, Z + 4, "air"));
            server.runCommand(fill(X + 7, Y, Z + 2, X + 9, Y + 2, Z + 4, "air"));
        }
    }

    /** The place that keeps taking health is said as a place (hurt_here), and
     * it is gone once the Body stands elsewhere (dragon-unit-tests-20261030 §6.y.11; end run-7). */
    private static void hurtHere(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        arena(server);
        int cx = X + 3, cz = Z + 4;
        try {
            server.runCommand("gamemode survival @p");
            server.runCommand("kill @e[type=minecraft:area_effect_cloud]");
            server.runCommand("tp @p " + cx + ".5 " + Y + " " + cz + ".5");
            context.waitFor(client -> client.player.onGround() && client.level.getEntitiesOfClass(
                net.minecraft.world.entity.AreaEffectCloud.class, client.player.getBoundingBox().inflate(8)).isEmpty(), 200);
            JsonObject before = body.state();
            check(before.get("hurt_here").isJsonNull(), "before any hurt, hurt_here was " + before.get("hurt_here"));
            server.runCommand("summon minecraft:area_effect_cloud " + cx + ".5 " + Y + " " + cz + ".5"
                + " {custom_particle:{type:\"minecraft:dragon_breath\"},Radius:2.0f,RadiusPerTick:0.02f,Duration:1200,WaitTime:0,ReapplicationDelay:20,potion_contents:{custom_effects:[{id:\"minecraft:instant_damage\",amplifier:0b,duration:1}]}}");
            context.waitTicks(50);
            JsonObject hurt = body.state();
            System.out.println("MINEBOT_HURT in cloud: hurt_here " + hurt.get("hurt_here") + " cloud " + hurt.get("cloud") + " health " + hurt.get("health"));
            check(hurt.get("hurt_here").isJsonObject() && hurt.getAsJsonObject("hurt_here").get("times").getAsInt() >= 1,
                "standing in the cloud, hurt_here was " + hurt.get("hurt_here"));
            server.runCommand("effect give @p minecraft:instant_health 1 3");
            server.runCommand("tp @p " + (cx - 9) + ".5 " + Y + " " + cz + ".5");
            context.waitTicks(5);
            JsonObject away = body.state();
            System.out.println("MINEBOT_HURT away: hurt_here " + away.get("hurt_here") + " health " + away.get("health"));
            check(away.get("hurt_here").isJsonNull(), "standing elsewhere, hurt_here was " + away.get("hurt_here"));
            System.out.println("MINEBOT_HURT PASS hurtHere");
        } finally {
            server.runCommand("kill @e[type=minecraft:area_effect_cloud]");
            server.runCommand("effect give @p minecraft:instant_health 1 3");
            arena(server);
        }
    }

    private static void harmfulCloudFacts(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        int cx = X + 3, cz = Z + 4;
        try {
            server.runCommand("kill @e[type=minecraft:area_effect_cloud]");
            context.waitFor(client -> client.level.getEntitiesOfClass(net.minecraft.world.entity.AreaEffectCloud.class,
                client.player.getBoundingBox().inflate(8)).isEmpty(), 200);
            JsonObject absent = body.state();
            check(absent.has("cloud") && absent.get("cloud").isJsonNull(), "without a cloud, the fact was " + absent);
            server.runCommand("summon minecraft:area_effect_cloud " + cx + " " + Y + " " + cz
                + " {custom_particle:{type:\"minecraft:dragon_breath\"},Radius:3.0f,Duration:1200,WaitTime:0,ReapplicationDelay:10,potion_contents:{custom_effects:[{id:\"minecraft:instant_damage\",amplifier:0b,duration:1}]}}");
            context.waitFor(client -> !client.level.getEntitiesOfClass(net.minecraft.world.entity.AreaEffectCloud.class,
                client.player.getBoundingBox().inflate(8)).isEmpty(), 200);
            JsonObject inCloud = body.state();
            check(inCloud.has("cloud") && inCloud.getAsJsonObject("cloud").get("inside").getAsBoolean(),
                "inside the cloud, the state did not say inside: " + inCloud);
            JsonObject cloud = inCloud.getAsJsonObject("cloud");
            check("dragon_breath".equals(cloud.get("particle").getAsString()), "cloud particle was " + cloud);
            check(Math.abs(cloud.get("radius").getAsDouble() - 3.0) < 0.01, "cloud radius was " + cloud);
            JsonObject terminal = body.awaitCommand(body.startObjective("away:" + cx + ":" + Y + ":" + cz + ":5", 800), 840);
            check("completed".equals(terminal.get("status").getAsString()), "move_away objective ended " + terminal);
            double distance = context.computeOnClient(client -> {
                double dx = client.player.getX() - cx, dz0 = client.player.getZ() - cz;
                return Math.sqrt(dx * dx + dz0 * dz0);
            });
            check(distance >= 5.0, "the Body stopped only " + distance + " blocks from the cloud");
            JsonObject out = body.state();
            check(out.get("cloud").isJsonNull() || !out.getAsJsonObject("cloud").get("inside").getAsBoolean(),
                "after moving away, the cloud still said inside: " + out);
        } finally {
            server.runCommand("kill @e[type=minecraft:area_effect_cloud]");
            arena(server);
            server.runCommand("tp @p " + (X + 3) + ".5 " + Y + " " + (Z + 4) + ".5");
        }
    }

    /** A calm zombified piglin or enderman is no threat to fight back against;
     * a zombie is. (Easy for the summons, NoAI so none of them moves.) */
    private static void calmNeutralMobsAreNotHostile(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        server.runCommand("difficulty easy");
        String[] types = {"zombified_piglin", "enderman", "zombie"};
        for (int i = 0; i < types.length; i++) {
            server.runCommand("summon minecraft:" + types[i] + " " + (X + 1 + 2 * i) + " " + Y + " " + (Z + 10)
                + " {NoAI:1b,Silent:1b,PersistenceRequired:1b}");
        }
        context.waitFor(client -> client.level.getEntities(client.player, client.player.getBoundingBox().inflate(32),
            entity -> entity instanceof net.minecraft.world.entity.Mob).size() >= types.length, 200);
        java.util.Map<String, Boolean> hostile = new java.util.HashMap<>();
        for (var entity : body.state().getAsJsonArray("entities")) {
            JsonObject observed = entity.getAsJsonObject();
            hostile.put(observed.get("type").getAsString(), observed.get("hostile").getAsBoolean());
        }
        server.runCommand("kill @e[type=!player]");
        server.runCommand("difficulty peaceful");
        check(Boolean.FALSE.equals(hostile.get("zombified_piglin")) && Boolean.FALSE.equals(hostile.get("enderman"))
            && Boolean.TRUE.equals(hostile.get("zombie")), "hostile flags: " + hostile);
    }

    /** eval-20261020-m2 run-1, death 1: engage swung the diamond pickaxe in
     * hand at magma cubes while an iron sword sat in the inventory. */
    private static void engageDrawsTheBestWeapon(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        fullHotbar(context, server);  // pickaxe in slot 0, a stone sword in slot 1
        server.runCommand("item replace entity @p inventory.0 with minecraft:iron_sword");
        context.waitFor(client -> countItem(client, "iron_sword") == 1, 200);
        body.primitive("selectItem", params("item", "diamond_pickaxe"));
        server.runCommand("difficulty easy");
        server.runCommand("summon minecraft:zombie " + (X + 3) + ".5 " + Y + " " + (Z + 6) + ".5"
            + " {NoAI:1b,Silent:1b,PersistenceRequired:1b}");
        context.waitFor(client -> !client.level.getEntities(client.player, client.player.getBoundingBox().inflate(16),
            entity -> entity.getType() == net.minecraft.world.entity.EntityType.ZOMBIE).isEmpty(), 200);
        JsonObject terminal = body.command("engageEntity", params("target_type", "zombie"), 600);
        String held = context.computeOnClient(client -> path(client.player.getMainHandItem()));
        server.runCommand("kill @e[type=!player]");
        server.runCommand("difficulty peaceful");
        check("iron_sword".equals(held), "engage ended " + terminal + " holding " + held + "; " + inventory(context));
    }

    /** eval-20261020-m2 run-1, 2026-10-23: a smelt cancelled mid-transfer
     * left a container screen open. For 17 minutes every craft then ended
     * menu_busy and every draw from the main inventory
     * inventory_menu_unavailable, until the Agent gave up. */
    private static void aLeftoverScreenIsClosedFirst(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        int px = X + 7, pz = Z - 3;
        BlockPos table = new BlockPos(px - 2, Y, pz);
        BlockPos furnace = new BlockPos(px + 2, Y, pz);
        server.runCommand("setblock " + table.getX() + " " + Y + " " + pz + " minecraft:crafting_table");
        server.runCommand("setblock " + furnace.getX() + " " + Y + " " + pz + " minecraft:furnace");
        server.runCommand("clear @p");
        server.runCommand("item replace entity @p hotbar.2 with minecraft:raw_iron 3");
        server.runCommand("item replace entity @p inventory.0 with minecraft:iron_sword");
        server.runCommand("tp @p " + px + ".5 " + Y + " " + pz + ".5");
        context.waitFor(client -> countItem(client, "iron_sword") == 1 && client.player.onGround(), 200);
        leaveOpen(context, body, table);
        body.primitive("selectItem", params("item", "iron_sword"));
        String held = context.computeOnClient(client -> path(client.player.getMainHandItem()));
        check("iron_sword".equals(held), "with a crafting table left open, selectItem left the hand holding " + held);
        leaveOpen(context, body, table);
        JsonObject transfer = new JsonObject();
        JsonArray pos = new JsonArray();
        pos.add(furnace.getX()); pos.add(Y); pos.add(pz);
        transfer.add("pos", pos);
        transfer.addProperty("direction", "bot_to_furnace");
        transfer.addProperty("furnace_slot", "input");
        transfer.addProperty("bot_slot", 2);
        transfer.addProperty("item", "raw_iron");
        transfer.addProperty("count", 3);
        JsonObject terminal = body.command("furnaceTransfer", transfer, 200);
        String menu = context.computeOnClient(client -> client.player.containerMenu.getClass().getSimpleName());
        server.runCommand("setblock " + table.getX() + " " + Y + " " + pz + " minecraft:air");
        server.runCommand("setblock " + furnace.getX() + " " + Y + " " + pz + " minecraft:air");
        check("completed".equals(terminal.get("status").getAsString()),
            "with a crafting table left open, the furnace transfer ended " + terminal.get("status") + " " + terminal.get("reason"));
        check("InventoryMenu".equals(menu), "after the furnace transfer the open menu is " + menu);
    }

    private static void leaveOpen(ClientGameTestContext context, BodyIpc body, BlockPos pos) {
        JsonObject look = new JsonObject();
        JsonArray target = new JsonArray();
        target.add(pos.getX() + 0.5); target.add(pos.getY() + 0.5); target.add(pos.getZ() + 0.5);
        look.add("target", target);
        body.primitive("lookAt", look);
        context.waitTicks(5);
        context.runOnClient(client -> client.gameMode.useItemOn(client.player, net.minecraft.world.InteractionHand.MAIN_HAND,
            new net.minecraft.world.phys.BlockHitResult(net.minecraft.world.phys.Vec3.atCenterOf(pos),
                net.minecraft.core.Direction.UP, pos, false)));
        context.waitFor(client -> client.player.containerMenu instanceof net.minecraft.world.inventory.CraftingMenu, 100);
    }

    /** eval-20261020-m1 run-2, 18:50: under a solid stone column with two
     * air cells for the player, dig_up and go_to_surface ended
     * vertical_no_path at once, without breaking a block. */
    private static void digsUpThroughSolidStone(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        int px = X - 4, pz = Z + 10;
        server.runCommand(fill(px - 2, Y, pz - 2, px + 2, Y + 14, pz + 2, "stone"));
        server.runCommand(fill(px, Y, pz, px, Y + 1, pz, "air"));
        server.runCommand("clear @p");
        server.runCommand("item replace entity @p hotbar.0 with minecraft:diamond_pickaxe");
        server.runCommand("give @p minecraft:cobblestone 64");
        server.runCommand("tp @p " + px + ".5 " + Y + " " + pz + ".5");
        context.waitFor(client -> client.player.onGround() && client.player.blockPosition().getX() == px
            && countItem(client, "cobblestone") == 64, 200);
        JsonObject params = new JsonObject();
        params.addProperty("target_y", Y + 8);
        params.addProperty("max_steps", 16);
        params.addProperty("max_distance", 16);
        params.addProperty("max_mutations", 48);
        JsonArray scaffold = new JsonArray();
        scaffold.add("cobblestone");
        params.add("scaffold_blocks", scaffold);
        // As the live client in a disposable world: vertical mutation allowed.
        System.setProperty("minebot.client.disposable_world", "true");
        JsonObject terminal = body.command("digUp", params, 1200);
        System.clearProperty("minebot.client.disposable_world");
        int y = context.computeOnClient(client -> client.player.blockPosition().getY());
        check("completed".equals(terminal.get("status").getAsString()) && y == Y + 8,
            "digUp under solid stone ended " + terminal.get("status") + " " + terminal.get("reason") + " at y " + y
            + " (wanted " + (Y + 8) + "), path events " + terminal.get("path_events"));
    }

    /** The live geometry of run-2 18:50: standing on its own one-block pillar
     * in a cave three blocks high, stone ceiling above, surface 33 up. */
    private static void digsUpFromAPillarInACave(ClientGameTestContext context, TestServerContext server, BodyIpc body) {
        int px = X + 8, pz = Z - 3;
        server.runCommand(fill(px - 3, Y, pz - 3, px + 3, Y + 36, pz + 3, "stone"));
        server.runCommand(fill(px - 2, Y, pz - 2, px + 2, Y + 2, pz + 2, "air"));
        server.runCommand("setblock " + px + " " + Y + " " + pz + " minecraft:cobbled_deepslate");
        server.runCommand("clear @p");
        server.runCommand("item replace entity @p hotbar.0 with minecraft:diamond_pickaxe");
        server.runCommand("give @p minecraft:cobbled_deepslate 54");
        server.runCommand("tp @p " + px + ".5 " + (Y + 1) + " " + pz + ".5");
        context.waitFor(client -> client.player.onGround() && client.player.blockPosition().getY() == Y + 1
            && countItem(client, "cobbled_deepslate") == 54, 200);
        JsonObject params = new JsonObject();
        params.addProperty("target_y", Y + 34);
        params.addProperty("max_steps", 64);
        params.addProperty("max_distance", 64);
        params.addProperty("max_mutations", 192);
        JsonArray scaffold = new JsonArray();
        scaffold.add("cobbled_deepslate");
        params.add("scaffold_blocks", scaffold);
        System.setProperty("minebot.client.disposable_world", "true");
        JsonObject terminal = body.command("digUp", params, 3000);
        System.clearProperty("minebot.client.disposable_world");
        int y = context.computeOnClient(client -> client.player.blockPosition().getY());
        check("completed".equals(terminal.get("status").getAsString()) && y == Y + 34,
            "digUp from a pillar in a cave ended " + terminal.get("status") + " " + terminal.get("reason") + " at y " + y
            + " (wanted " + (Y + 34) + "), path events " + terminal.get("path_events"));
    }

    private static void use(ClientGameTestContext context, BodyIpc body, String item, double x, double y, double z) {
        JsonObject look = new JsonObject();
        JsonArray target = new JsonArray();
        target.add(x); target.add(y); target.add(z);
        look.add("target", target);
        body.primitive("lookAt", look);
        context.waitTicks(2);
        JsonObject use = params("item", item);
        use.addProperty("mode", "once");
        use.addProperty("ticks", 1);
        JsonObject terminal = body.command("useItem", use, 200);
        check("completed".equals(terminal.get("status").getAsString()), "useItem " + item + " ended " + terminal);
        context.waitTicks(10);
    }

    private static void fullHotbar(ClientGameTestContext context, TestServerContext server) {
        server.runCommand("clear @p");
        server.runCommand("item replace entity @p hotbar.0 with minecraft:diamond_pickaxe");
        String[] filler = {"stone_sword", "torch 16", "dirt 32", "stone_axe", "bread 4", "oak_planks 16", "stone_shovel"};
        for (int slot = 1; slot <= 7; slot++) {
            server.runCommand("item replace entity @p hotbar." + slot + " with minecraft:" + filler[slot - 1]);
        }
        server.runCommand("item replace entity @p hotbar.8 with minecraft:cobblestone 64");
        for (String item : new String[]{"bucket", "water_bucket", "flint_and_steel"}) {
            server.runCommand("give @p minecraft:" + item);
        }
        server.runCommand("tp @p " + (X + 3) + ".5 " + Y + " " + (Z + 4) + ".5");
        // The integrated server may run seconds behind the client's ticks.
        context.waitFor(client -> countItem(client, "flint_and_steel") == 1 && countItem(client, "water_bucket") == 1
            && "diamond_pickaxe".equals(path(client.player.getInventory().getItem(0))), 400);
    }

    /** Up to 100 ticks for server truth to arrive; the checks after it decide. */
    private static void settle(ClientGameTestContext context, java.util.function.BooleanSupplier done) {
        for (int tick = 0; tick < 100 && !done.getAsBoolean(); tick++) context.waitTick();
    }

    private static String serverInventory(TestServerContext server) {
        return server.computeOnServer(s -> {
            var player = s.getPlayerList().getPlayers().get(0);
            StringBuilder out = new StringBuilder();
            for (int slot = 0; slot < 36; slot++) {
                ItemStack stack = player.getInventory().getItem(slot);
                if (!stack.isEmpty()) out.append(slot).append('=').append(path(stack)).append(' ');
            }
            return out + "selected=" + player.getInventory().getSelectedSlot();
        });
    }

    private static String inventory(ClientGameTestContext context) {
        return context.computeOnClient(client -> {
            StringBuilder out = new StringBuilder();
            for (int slot = 0; slot < 36; slot++) {
                ItemStack stack = client.player.getInventory().getItem(slot);
                if (!stack.isEmpty()) out.append(slot).append('=').append(path(stack)).append(' ');
            }
            return out + "selected=" + client.player.getInventory().getSelectedSlot();
        });
    }

    private static int countItem(ClientGameTestContext context, String item) {
        return context.computeOnClient(client -> countItem(client, item));
    }

    private static int countItem(net.minecraft.client.Minecraft client, String item) {
        int total = 0;
        for (int slot = 0; slot < 36; slot++) {
            ItemStack stack = client.player.getInventory().getItem(slot);
            if (!stack.isEmpty() && path(stack).equals(item)) total += stack.getCount();
        }
        return total;
    }

    private static String block(TestServerContext server, BlockPos pos) {
        return server.computeOnServer(s -> BuiltInRegistries.BLOCK.getKey(s.overworld().getBlockState(pos).getBlock()).getPath());
    }

    private static String path(ItemStack stack) {
        return BuiltInRegistries.ITEM.getKey(stack.getItem()).getPath();
    }

    private static String fill(int x1, int y1, int z1, int x2, int y2, int z2, String block) {
        return "fill " + x1 + " " + y1 + " " + z1 + " " + x2 + " " + y2 + " " + z2 + " minecraft:" + block;
    }

    private static JsonObject params(String key, String value) {
        JsonObject object = new JsonObject();
        object.addProperty(key, value);
        return object;
    }

    static void check(boolean condition, String message) {
        if (!condition) throw new AssertionError(message);
    }
}
