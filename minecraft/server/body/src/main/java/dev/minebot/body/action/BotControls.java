package dev.minebot.body.action;

/**
 * The subset of physical control the action runtime needs for lifecycle
 * hygiene. Production binds this to the public {@code /player} command
 * adapter; tests bind a recording fake.
 */
public interface BotControls {
    /** Total cleanup of every engaged input for the bot. */
    void clearAll(String botName);
}
