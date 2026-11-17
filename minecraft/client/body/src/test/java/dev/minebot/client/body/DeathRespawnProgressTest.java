package dev.minebot.client.body;

import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

class DeathRespawnProgressTest {
    @Test void deathAndRequestAreSeparateEdgesAndNeitherRepeats() {
        var progress = new DeathRespawnProgress();
        assertEquals(new DeathRespawnProgress.Changes(true, false, false),
            progress.observe(true, true, false, true));
        assertEquals(new DeathRespawnProgress.Changes(false, true, false),
            progress.observe(true, true, true, true));
        assertEquals(new DeathRespawnProgress.Changes(false, false, false),
            progress.observe(true, true, true, true));
    }

    @Test void aDeathScreenOnStartupRequestsRespawnWithoutAnAction() {
        var progress = new DeathRespawnProgress();
        assertEquals(new DeathRespawnProgress.Changes(true, true, false),
            progress.observe(true, true, true, true));
        assertTrue(progress.observe(true, false, false, true).respawned());
    }

    @Test void missingPlayerOrUnloadedWorldDoesNotProveRespawn() {
        var progress = new DeathRespawnProgress();
        progress.observe(true, true, true, true);
        assertFalse(progress.observe(false, false, false, false).respawned());
        assertFalse(progress.observe(true, false, false, false).respawned());
        assertFalse(progress.observe(true, false, true, true).respawned());
        assertTrue(progress.observe(true, false, false, true).respawned());
    }

    @Test void aNewDeathAfterRespawnProducesAnotherPairOfEdges() {
        var progress = new DeathRespawnProgress();
        progress.observe(true, true, true, true);
        progress.observe(true, false, false, true);
        assertTrue(progress.observe(true, true, true, true).requestRespawn());
    }
}
