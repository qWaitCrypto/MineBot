package dev.minebot.client.body;

import net.minecraft.core.BlockPos;
import org.junit.jupiter.api.Test;

import java.util.Set;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class CrystalFactsTest {
    @Test
    void reportsTheIronBarShellAroundACrystal() {
        BlockPos center = new BlockPos(0, 20, 0);
        Set<BlockPos> bars = Set.of(
            center.offset(-2, -1, 0), center.offset(2, -1, 0),
            center.offset(0, -1, -2), center.offset(0, -1, 2));

        assertTrue(MineBotClientBody.isCagedCrystal(bars::contains, center));
    }

    @Test
    void doesNotCallAnOpenCrystalCaged() {
        BlockPos center = new BlockPos(0, 20, 0);
        Set<BlockPos> bars = Set.of(
            center.offset(-2, -1, 0), center.offset(2, -1, 0), center.offset(0, -1, -2));

        assertFalse(MineBotClientBody.isCagedCrystal(bars::contains, center));
    }
}
