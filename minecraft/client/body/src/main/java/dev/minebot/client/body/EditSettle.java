package dev.minebot.client.body;

/**
 * Whether a stopped run should wait for the server to confirm what the client
 * already shows. A break or place appears on the client at once and the
 * server's confirmation follows a tick or two later; ending the run first lost
 * the last edit of a clear (house run 2026-10-18: two cells cleared, one
 * receipt). One second covers a normal round trip; whatever is still not
 * confirmed then is reported as unconfirmed, never as done.
 */
final class EditSettle {
    static final int MAX_WAIT_TICKS = 20;

    private EditSettle() {}

    static boolean waitMore(int unconfirmedCells, int ticksWaited) {
        return unconfirmedCells > 0 && ticksWaited < MAX_WAIT_TICKS;
    }
}
