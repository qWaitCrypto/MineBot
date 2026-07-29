package dev.minebot.body.protocol;

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
}
