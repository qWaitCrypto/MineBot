package dev.minebot.client.body;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class EditSettleTest {
    @Test
    void waitsWhileTheServerHasNotConfirmedACellYet() {
        assertTrue(EditSettle.waitMore(1, 0));
        assertTrue(EditSettle.waitMore(1, EditSettle.MAX_WAIT_TICKS - 1));
    }

    @Test
    void endsWhenEverythingIsConfirmedOrTheWaitIsOver() {
        assertFalse(EditSettle.waitMore(0, 0));
        assertFalse(EditSettle.waitMore(2, EditSettle.MAX_WAIT_TICKS));
    }
}
