package dev.minebot.body.action;

/**
 * Exact-coordinate block breaking through a real ServerPlayer. The caller has
 * already obtained governance approval for the same coordinate and block id.
 */
public interface ExactBlockBreaker {
    enum State {
        WORKING,
        COMPLETE,
        FAILED
    }

    record Outcome(State state, String reason) {
        public static Outcome working() {
            return new Outcome(State.WORKING, "working");
        }

        public static Outcome complete() {
            return new Outcome(State.COMPLETE, "block_changed");
        }

        public static Outcome failed(String reason) {
            return new Outcome(State.FAILED, reason);
        }
    }

    Outcome begin(String botName, int x, int y, int z, String expectedBlockId, int serverTick);

    Outcome tick(String botName, int serverTick);

    void abort(String botName);
}
