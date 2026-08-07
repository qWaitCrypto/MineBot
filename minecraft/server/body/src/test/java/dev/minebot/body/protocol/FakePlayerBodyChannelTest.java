package dev.minebot.body.protocol;

import com.google.gson.JsonObject;
import net.minecraft.SharedConstants;
import net.minecraft.server.Bootstrap;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.level.block.Blocks;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertDoesNotThrow;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

final class FakePlayerBodyChannelTest {
    @Test
    void canonicalMultiTargetExplorationFitsOneBlockSearch() {
        assertEquals(64, FakePlayerBodyChannel.MAX_REQUESTED_BLOCK_IDS);
        assertDoesNotThrow(() -> FakePlayerBodyChannel.validateRequestedBlockCount(32));
        assertDoesNotThrow(() -> FakePlayerBodyChannel.validateRequestedBlockCount(64));
    }

    @Test
    void blockSearchStillRejectsEmptyAndOversizedQueries() {
        assertThrows(
            IllegalArgumentException.class,
            () -> FakePlayerBodyChannel.validateRequestedBlockCount(0)
        );
        assertThrows(
            IllegalArgumentException.class,
            () -> FakePlayerBodyChannel.validateRequestedBlockCount(65)
        );
    }

    @Test
    void helloEventSubscriptionIsExplicitAndBackwardCompatible() {
        JsonObject legacy = new JsonObject();
        assertEquals(true, FakePlayerBodyChannel.eventSubscriptionRequested(legacy));

        JsonObject readOnly = new JsonObject();
        readOnly.addProperty("subscribe_events", false);
        assertEquals(false, FakePlayerBodyChannel.eventSubscriptionRequested(readOnly));
    }

    @Test
    void dropToolCheckDistinguishesHandHarvestableBlocksFromToolRequiredBlocks() {
        SharedConstants.tryDetectVersion();
        Bootstrap.bootStrap();
        assertEquals(
            true,
            FakePlayerBodyChannel.correctToolForDrops(
                Blocks.OAK_LOG.defaultBlockState(),
                ItemStack.EMPTY
            )
        );
        assertEquals(
            false,
            FakePlayerBodyChannel.correctToolForDrops(
                Blocks.STONE.defaultBlockState(),
                ItemStack.EMPTY
            )
        );
    }
}
