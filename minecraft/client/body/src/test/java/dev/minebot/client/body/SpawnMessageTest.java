package dev.minebot.client.body;

import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

class SpawnMessageTest {
    @Test void onlyTheTwoRespawnMessagesAreKept() {
        assertTrue(SpawnMessage.about("block.minecraft.set_spawn"));
        assertTrue(SpawnMessage.about("block.minecraft.spawn.not_valid"));
        assertFalse(SpawnMessage.about("block.minecraft.bed.no_sleep"));
        assertFalse(SpawnMessage.about(null));
    }

    @Test void bedsAndRespawnAnchorsAreRespawnBlocks() {
        assertTrue(SpawnMessage.respawnBlock("white_bed"));
        assertTrue(SpawnMessage.respawnBlock("respawn_anchor"));
        assertFalse(SpawnMessage.respawnBlock("chest"));
        assertFalse(SpawnMessage.respawnBlock(null));
    }
}
