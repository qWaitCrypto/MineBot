package dev.minebot.worldstream;

public final class WorldStreamSubscription {
    private final String subId;
    private final String entityName;
    private final String dimension;
    private final int radiusChunks;
    private final int rateHz;
    private int lastTransformTick = Integer.MIN_VALUE;

    public WorldStreamSubscription(String subId, String entityName, String dimension, int radiusChunks, int rateHz) {
        this.subId = subId;
        this.entityName = entityName;
        this.dimension = dimension;
        this.radiusChunks = radiusChunks;
        this.rateHz = rateHz;
    }

    public String subId() {
        return subId;
    }

    public String entityName() {
        return entityName;
    }

    public String dimension() {
        return dimension;
    }

    public int radiusChunks() {
        return radiusChunks;
    }

    public int rateHz() {
        return rateHz;
    }

    public boolean shouldSendTransform(int serverTick) {
        int interval = Math.max(1, 20 / Math.max(1, rateHz));
        return lastTransformTick == Integer.MIN_VALUE || serverTick - lastTransformTick >= interval;
    }

    public void markTransformSent(int serverTick) {
        lastTransformTick = serverTick;
    }
}
