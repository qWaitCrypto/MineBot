package dev.minebot.body.control;

import java.util.HashMap;
import java.util.Map;

public final class FakePlayerActionOwner {
    private final Map<String, String> actionByBot = new HashMap<>();

    public synchronized boolean acquire(String botName, String actionId) {
        String current = actionByBot.get(botName);
        if (current != null && !current.equals(actionId)) {
            return false;
        }
        actionByBot.put(botName, actionId);
        return true;
    }

    public synchronized boolean release(String botName, String actionId) {
        String current = actionByBot.get(botName);
        if (!actionId.equals(current)) {
            return false;
        }
        actionByBot.remove(botName);
        return true;
    }

    public synchronized String current(String botName) {
        return actionByBot.get(botName);
    }
}
