package dev.minebot.bridge.worldstream;

import java.util.ArrayDeque;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

public final class WorldStreamSubscription {
    private final String subId;
    private final String entityName;
    private final String dimension;
    private final int radiusChunks;
    private final int rateHz;
    private final int yBandBelow;
    private final int yBandAbove;
    private final int resyncWindowTicks;
    private final ArrayDeque<SectionKey> pendingSections = new ArrayDeque<>();
    private final Set<SectionKey> pendingSectionSet = new HashSet<>();
    private final Set<SectionKey> activeSections = new HashSet<>();
    private final Set<SectionKey> sentSections = new HashSet<>();
    private final Map<SectionKey, Integer> resyncDueTicks = new HashMap<>();
    private int lastTransformTick = Integer.MIN_VALUE;
    private int centerSectionX = Integer.MIN_VALUE;
    private int centerSectionY = Integer.MIN_VALUE;
    private int centerSectionZ = Integer.MIN_VALUE;
    private boolean entityMissing;

    public WorldStreamSubscription(
        String subId,
        String entityName,
        String dimension,
        int radiusChunks,
        int rateHz,
        int yBandBelow,
        int yBandAbove,
        int resyncWindowTicks
    ) {
        this.subId = subId;
        this.entityName = entityName;
        this.dimension = dimension;
        this.radiusChunks = radiusChunks;
        this.rateHz = rateHz;
        this.yBandBelow = yBandBelow;
        this.yBandAbove = yBandAbove;
        this.resyncWindowTicks = Math.max(1, resyncWindowTicks);
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

    public int yBandBelow() {
        return yBandBelow;
    }

    public int yBandAbove() {
        return yBandAbove;
    }

    public boolean shouldSendTransform(int serverTick) {
        int interval = Math.max(1, 20 / Math.max(1, rateHz));
        return lastTransformTick == Integer.MIN_VALUE || serverTick - lastTransformTick >= interval;
    }

    public void markTransformSent(int serverTick) {
        lastTransformTick = serverTick;
    }

    public boolean entityMissing() {
        return entityMissing;
    }

    public void markEntityMissing(int serverTick) {
        entityMissing = true;
        lastTransformTick = serverTick;
    }

    public void markEntityPresent() {
        entityMissing = false;
    }

    public boolean recenter(int sectionX, int sectionY, int sectionZ, List<SectionKey> nearestFirstRegion) {
        boolean changed = centerSectionX != sectionX || centerSectionY != sectionY || centerSectionZ != sectionZ;
        if (!changed && !activeSections.isEmpty()) {
            return false;
        }
        centerSectionX = sectionX;
        centerSectionY = sectionY;
        centerSectionZ = sectionZ;
        activeSections.clear();
        activeSections.addAll(nearestFirstRegion);
        for (SectionKey section : nearestFirstRegion) {
            if (!sentSections.contains(section)) {
                enqueue(section);
            }
        }
        sentSections.retainAll(activeSections);
        resyncDueTicks.keySet().retainAll(activeSections);
        pendingSections.removeIf(section -> {
            if (activeSections.contains(section)) {
                return false;
            }
            pendingSectionSet.remove(section);
            return true;
        });
        return true;
    }

    public SectionKey pollPendingSection() {
        SectionKey section = pendingSections.pollFirst();
        if (section != null) {
            pendingSectionSet.remove(section);
        }
        return section;
    }

    public void markSectionSent(SectionKey section, int serverTick) {
        sentSections.add(section);
        resyncDueTicks.put(section, serverTick + jitteredResyncDelay(section, resyncWindowTicks));
    }

    public int pendingSectionCount() {
        return pendingSections.size();
    }

    public void enqueueDueResyncs(int serverTick) {
        for (SectionKey section : activeSections) {
            Integer dueTick = resyncDueTicks.get(section);
            if (dueTick != null && serverTick >= dueTick) {
                enqueue(section);
                resyncDueTicks.put(section, serverTick + jitteredResyncDelay(section, resyncWindowTicks));
            }
        }
    }

    private void enqueue(SectionKey section) {
        if (pendingSectionSet.add(section)) {
            pendingSections.addLast(section);
        }
    }

    private static int jitteredResyncDelay(SectionKey section, int resyncWindowTicks) {
        int window = Math.max(1, resyncWindowTicks);
        int hash = section.x() * 734_287 + section.y() * 912_271 + section.z() * 438_289;
        return 1 + Math.floorMod(hash, window);
    }
}
