package dev.minebot.body.search;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.PriorityQueue;

/**
 * Keeps the nearest {@code cap} matches seen so far. Fed in outward ring order
 * it yields the exact nearest set: {@link #canStop(long)} reports when no
 * later ring can displace a kept match, and {@link #capped()} reports honestly
 * whether more in-radius matches existed than the snapshot retains.
 */
public final class NearestMatchCollector {
    private final int cap;
    private final PriorityQueue<SearchMatch> worstFirst =
        new PriorityQueue<>(Comparator.comparingDouble(SearchMatch::distanceSquared).reversed());
    private boolean droppedAny;
    private boolean truncated;

    public NearestMatchCollector(int cap) {
        if (cap <= 0) {
            throw new IllegalArgumentException("cap must be positive");
        }
        this.cap = cap;
    }

    public void add(SearchMatch match) {
        worstFirst.add(match);
        if (worstFirst.size() > cap) {
            worstFirst.poll();
            droppedAny = true;
        }
    }

    /**
     * True when the collector is full and its worst kept match is at least as
     * close as the nearest possible match of every unvisited ring.
     */
    public boolean canStop(long nextRingMinDistanceSquared) {
        SearchMatch worst = worstFirst.peek();
        return worstFirst.size() == cap && worst != null && worst.distanceSquared() <= nextRingMinDistanceSquared;
    }

    /** Record that scanning stopped before exhausting the region. */
    public void markTruncated() {
        truncated = true;
    }

    public boolean capped() {
        return droppedAny || truncated;
    }

    /** Kept matches in ascending distance order. */
    public List<SearchMatch> finish() {
        List<SearchMatch> matches = new ArrayList<>(worstFirst);
        matches.sort(Comparator.comparingDouble(SearchMatch::distanceSquared));
        return List.copyOf(matches);
    }
}
