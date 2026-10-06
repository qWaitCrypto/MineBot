package dev.minebot.body.nav;

import net.minecraft.SharedConstants;
import net.minecraft.core.BlockPos;
import net.minecraft.server.Bootstrap;
import net.minecraft.world.level.block.Blocks;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;
import java.util.Set;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

final class ExcavationGeometryTest {
    @BeforeAll
    static void bootstrapMinecraft() {
        SharedConstants.tryDetectVersion();
        Bootstrap.bootStrap();
    }

    @Test
    void existingLowSurfaceStandDoesNotRequireClearingItsSupport() {
        for (var block : List.of(Blocks.STONE_SLAB, Blocks.WHITE_CARPET, Blocks.OAK_TRAPDOOR)) {
            var world = MinecraftWorldViewTest.nativeWorld(Map.of(
                new BlockPos(0, 63, 0), Blocks.STONE.defaultBlockState(),
                new BlockPos(1, 63, 0), block.defaultBlockState(),
                new BlockPos(2, 64, 0), Blocks.OAK_LOG.defaultBlockState()
            ));
            var stand = new ExcavationPlanner.Cell(1, 63, 0);
            assertTrue(new Goal.Interact(2, 64, 0, 4.5).isSatisfied(world, 1, 63, 0));
            var result = ExcavationPlanner.plan(world, new ExcavationPlanner.Cell(0, 64, 0),
                List.of(new ExcavationPlanner.Target(2, 64, 0)), Set.of(stand), 4);
            assertTrue(result.success(), block + ": " + result.reason());
            assertTrue(result.plan().reachesTarget());
            assertEquals(0, result.plan().breakCount());
            assertEquals(stand, result.plan().steps().getLast().stand());
            assertTrue(result.plan().steps().stream().allMatch(step -> step.blockers().isEmpty()));
        }
    }

    @Test
    void physicalStandOnTheTargetStillRequiresASafePostMineLanding() {
        var world = MinecraftWorldViewTest.nativeWorld(Map.of(
            new BlockPos(1, 63, 0), Blocks.OAK_FENCE.defaultBlockState()
        ));
        assertEquals(64.5, world.standingHeight(1, 64, 0));
        var result = ExcavationPlanner.plan(world, new ExcavationPlanner.Cell(1, 64, 0),
            List.of(new ExcavationPlanner.Target(1, 63, 0)), Set.of(), 4);
        assertFalse(result.success());
        assertEquals("no_terminal_stands", result.reason());
    }
}
