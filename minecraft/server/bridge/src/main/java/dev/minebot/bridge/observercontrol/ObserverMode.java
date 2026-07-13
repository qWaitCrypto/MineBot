package dev.minebot.bridge.observercontrol;

public enum ObserverMode {
    FOLLOW("follow"),
    FIXED("fixed");

    private final String wireName;

    ObserverMode(String wireName) {
        this.wireName = wireName;
    }

    public String wireName() {
        return wireName;
    }

    public static ObserverMode parse(String value) {
        for (ObserverMode mode : values()) {
            if (mode.wireName.equals(value)) {
                return mode;
            }
        }
        throw new IllegalArgumentException("mode must be follow or fixed");
    }
}
