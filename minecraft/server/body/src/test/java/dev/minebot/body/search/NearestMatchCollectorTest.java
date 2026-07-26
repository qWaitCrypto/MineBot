package dev.minebot.body.search;

import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.Random;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

final class NearestMatchCollectorTest {
    private static SearchMatch matchAt(int x, int z) {
        return new SearchMatch(x, 64, z, "minecraft:stone", (double) x * x + (double) z * z);
    }

    @Test
    void keepsTheExactNearestSetUnderTheCap() {
        Random random = new Random(20260726);
        List<SearchMatch> all = new ArrayList<>();
        for (int i = 0; i < 500; i++) {
            all.add(matchAt(random.nextInt(-200, 201), random.nextInt(-200, 201)));
        }
        NearestMatchCollector collector = new NearestMatchCollector(50);
        all.forEach(collector::add);

        List<SearchMatch> expected = all.stream()
            .sorted(Comparator.comparingDouble(SearchMatch::distanceSquared))
            .limit(50)
            .toList();
        List<SearchMatch> kept = collector.finish();

        assertEquals(50, kept.size());
        assertTrue(collector.capped());
        for (int i = 0; i < kept.size(); i++) {
            assertEquals(expected.get(i).distanceSquared(), kept.get(i).distanceSquared(), 0.0);
        }
    }

    @Test
    void neverStopsBeforeTheCollectorIsFull() {
        NearestMatchCollector collector = new NearestMatchCollector(3);
        collector.add(matchAt(1, 0));
        collector.add(matchAt(2, 0));
        assertFalse(collector.canStop(0));
        collector.add(matchAt(3, 0));
        assertTrue(collector.canStop(100));
        assertFalse(collector.canStop(8), "a closer future ring must keep the scan alive");
    }

    @Test
    void ringOrderedFeedWithEarlyStopStaysExact() {
        // Simulate outward feeding: ring r contributes matches at distance
        // ~16r; once the collector can stop, remaining rings are skipped and
        // the result must still be the true nearest set.
        NearestMatchCollector collector = new NearestMatchCollector(4);
        List<SearchMatch> all = new ArrayList<>();
        boolean stopped = false;
        for (int ring = 0; ring <= 8 && !stopped; ring++) {
            if (collector.canStop(ChunkRing.minBlockDistanceSquared(ring))) {
                collector.markTruncated();
                stopped = true;
                break;
            }
            for (int i = 0; i < 3; i++) {
                SearchMatch match = matchAt(ring * 16 + i, 0);
                all.add(match);
                collector.add(match);
            }
        }
        assertTrue(stopped, "the collector must stop before ring 8 with cap 4");

        List<SearchMatch> kept = collector.finish();
        assertEquals(4, kept.size());
        assertTrue(collector.capped());
        List<SearchMatch> expected = all.stream()
            .sorted(Comparator.comparingDouble(SearchMatch::distanceSquared))
            .limit(4)
            .toList();
        for (int i = 0; i < 4; i++) {
            assertEquals(expected.get(i).distanceSquared(), kept.get(i).distanceSquared(), 0.0);
        }
    }

    @Test
    void uncappedResultReportsHonestly() {
        NearestMatchCollector collector = new NearestMatchCollector(10);
        collector.add(matchAt(1, 0));
        collector.add(matchAt(2, 0));
        assertFalse(collector.capped());
        assertEquals(2, collector.finish().size());
    }
}
