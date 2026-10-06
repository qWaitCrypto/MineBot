package dev.minebot.body.nav;

import net.minecraft.SharedConstants;
import net.minecraft.core.BlockPos;
import net.minecraft.server.Bootstrap;
import net.minecraft.world.level.EmptyBlockGetter;
import net.minecraft.world.level.BlockGetter;
import net.minecraft.world.level.block.entity.BlockEntity;
import net.minecraft.world.level.material.FluidState;
import net.minecraft.world.level.block.Block;
import net.minecraft.world.level.block.Blocks;
import net.minecraft.world.level.block.state.BlockState;
import net.minecraft.world.level.block.state.properties.BlockStateProperties;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

final class MinecraftWorldViewTest {
    @BeforeAll
    static void bootstrapMinecraft() {
        SharedConstants.tryDetectVersion();
        Bootstrap.bootStrap();
    }

    private static WorldView.NodeKind classify(BlockState state) {
        return MinecraftWorldView.classify(state, EmptyBlockGetter.INSTANCE, BlockPos.ZERO);
    }

    static BlockGetter blocks(Map<BlockPos, BlockState> states) {
        return new BlockGetter() {
            public BlockEntity getBlockEntity(BlockPos pos) { return null; }
            public BlockState getBlockState(BlockPos pos) {
                return states.getOrDefault(pos, Blocks.AIR.defaultBlockState());
            }
            public FluidState getFluidState(BlockPos pos) { return getBlockState(pos).getFluidState(); }
            public int getHeight() { return 384; }
            public int getMinY() { return -64; }
        };
    }

    static WorldView nativeWorld(Map<BlockPos, BlockState> states) {
        BlockGetter blocks = blocks(states);
        return new WorldView() {
            public NodeKind kindAt(int x, int y, int z) {
                var pos = new BlockPos(x, y, z);
                return MinecraftWorldView.classify(blocks.getBlockState(pos), blocks, pos);
            }
            public double standingHeight(int x, int y, int z) {
                return MinecraftWorldView.standingHeight(blocks, x, y, z);
            }
            public boolean isStableDryPosition(double x, double y, double z) {
                return MinecraftWorldView.hasStableCollisionSupport(blocks, x, y, z);
            }
            public boolean isDryVolumeClear(double minX, double minY, double minZ,
                                            double maxX, double maxY, double maxZ) {
                return MinecraftWorldView.dryVolumeClear(blocks,
                    new net.minecraft.world.phys.AABB(minX, minY, minZ, maxX, maxY, maxZ));
            }
        };
    }

    @Test
    void stableContactUsesThePhysicalSurfaceAndBodyClearance() {
        for (var block : new Block[] {
            Blocks.STONE, Blocks.OAK_SLAB, Blocks.WHITE_CARPET,
            Blocks.OAK_TRAPDOOR, Blocks.OAK_FENCE, Blocks.COBBLESTONE_WALL
        }) {
            BlockState state = block.defaultBlockState();
            double height = state.getCollisionShape(EmptyBlockGetter.INSTANCE, BlockPos.ZERO)
                .max(net.minecraft.core.Direction.Axis.Y);
            var world = blocks(Map.of(BlockPos.ZERO, state));
            assertTrue(MinecraftWorldView.hasStableCollisionSupport(world, 0.5, height, 0.5), block.toString());
            assertFalse(MinecraftWorldView.hasStableCollisionSupport(world, 0.5, height + 0.1, 0.5), block.toString());
            assertFalse(MinecraftWorldView.hasStableCollisionSupport(world, 0.5, height - 0.1, 0.5), block.toString());
            var ceiling = blocks(Map.of(BlockPos.ZERO, state,
                new BlockPos(0, 2, 0), Blocks.STONE.defaultBlockState()));
            if (height + 1.8 > 2.0) {
                assertFalse(MinecraftWorldView.hasStableCollisionSupport(ceiling, 0.5, height, 0.5), block.toString());
            }
        }
        assertFalse(MinecraftWorldView.hasStableCollisionSupport(blocks(Map.of()), 0.5, 1, 0.5));
        assertFalse(MinecraftWorldView.hasStableCollisionSupport(blocks(Map.of()), Double.NaN, 1, 0.5));
    }

    @Test
    void physicalFootprintChecksAdjacentObstructionsAndMissingSupport() {
        var floor = blocks(Map.of(BlockPos.ZERO, Blocks.STONE.defaultBlockState()));
        assertTrue(MinecraftWorldView.hasStableCollisionSupport(floor, 0.9, 1, 0.5));
        assertFalse(MinecraftWorldView.hasStableCollisionSupport(floor, 1.31, 1, 0.5));
        var obstruction = blocks(Map.of(
            BlockPos.ZERO, Blocks.STONE.defaultBlockState(),
            new BlockPos(1, 2, 0), Blocks.STONE.defaultBlockState()
        ));
        assertFalse(MinecraftWorldView.hasStableCollisionSupport(obstruction, 0.9, 1, 0.5));
    }

    @Test
    void waterloggedCollisionCannotBecomeAnOpenSwimmingCell() {
        for (var block : new Block[] {
            Blocks.OAK_SLAB, Blocks.OAK_STAIRS, Blocks.OAK_FENCE, Blocks.OAK_TRAPDOOR
        }) {
            BlockState wet = block.defaultBlockState().setValue(BlockStateProperties.WATERLOGGED, true);
            assertEquals(WorldView.NodeKind.SOLID, classify(wet), block.toString());
        }
    }

    @Test
    void collisionFreeWaterRemainsSwimmableAndHazardsRemainDenied() {
        assertEquals(WorldView.NodeKind.LIQUID, classify(Blocks.WATER.defaultBlockState()));
        assertEquals(WorldView.NodeKind.LIQUID, classify(Blocks.KELP.defaultBlockState()));
        assertEquals(WorldView.NodeKind.HAZARD, classify(Blocks.LAVA.defaultBlockState()));
        assertEquals(WorldView.NodeKind.HAZARD, classify(Blocks.FIRE.defaultBlockState()));
        assertEquals(WorldView.NodeKind.HAZARD, classify(Blocks.MAGMA_BLOCK.defaultBlockState()));
        assertEquals(WorldView.NodeKind.PASSABLE, classify(Blocks.AIR.defaultBlockState()));
    }

    @Test
    void waterloggedLadderRetainsItsClimbingSemantics() {
        assertEquals(WorldView.NodeKind.CLIMBABLE, classify(Blocks.LADDER.defaultBlockState()));
        assertEquals(WorldView.NodeKind.CLIMBABLE, classify(Blocks.LADDER.defaultBlockState()
            .setValue(BlockStateProperties.WATERLOGGED, true)));
    }
}
