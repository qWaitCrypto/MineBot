package dev.minebot.body.control;

import java.util.EnumSet;
import java.util.HashMap;
import java.util.Map;
import java.util.Set;

/**
 * Per-bot registry of engaged continuous inputs, so cancellation cleanup is a
 * total operation over what was actually engaged instead of a best-effort
 * guess. The command adapter records engagements here and reads the exact set
 * back when clearing.
 */
public final class HeldInputs {
    public enum Input {
        MOVEMENT,
        JUMP,
        SNEAK,
        SPRINT,
        ATTACK,
        USE
    }

    private final Map<String, EnumSet<Input>> engagedByBot = new HashMap<>();

    public synchronized void engage(String botName, Input input) {
        engagedByBot.computeIfAbsent(botName, ignored -> EnumSet.noneOf(Input.class)).add(input);
    }

    public synchronized void disengage(String botName, Input input) {
        EnumSet<Input> engaged = engagedByBot.get(botName);
        if (engaged == null) {
            return;
        }
        engaged.remove(input);
        if (engaged.isEmpty()) {
            engagedByBot.remove(botName);
        }
    }

    /** Snapshot of engaged inputs; clearing consumes the registry entry. */
    public synchronized Set<Input> drain(String botName) {
        EnumSet<Input> engaged = engagedByBot.remove(botName);
        return engaged == null ? Set.of() : Set.copyOf(engaged);
    }

    public synchronized Set<Input> engaged(String botName) {
        EnumSet<Input> engaged = engagedByBot.get(botName);
        return engaged == null ? Set.of() : Set.copyOf(engaged);
    }
}
