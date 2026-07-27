package dev.minebot.body.search;

import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

final class SearchSnapshotStoreTest {
    private static LoadedSearchResult result(long generation, int unloadedChunks, SearchMatch... matches) {
        return new LoadedSearchResult(generation, List.of(matches), unloadedChunks, false);
    }

    private static SearchMatch match(int x, double distanceSquared) {
        return new SearchMatch(x, 64, x, "minecraft:oak_log", "SOLID", distanceSquared);
    }

    @Test
    void pagesResumeWithoutRebuildingTheSearchResult() {
        SearchSnapshotStore store = new SearchSnapshotStore();
        SearchSnapshotStore.Page first = store.first(
            "query",
            result(7, 0, match(1, 2), match(2, 8), match(3, 18)),
            2
        );

        assertEquals(2, first.matches().size());
        assertEquals(0, first.start());
        assertEquals(3, first.totalMatches());
        assertNotNull(first.nextCursor());
        SearchSnapshotStore.ResumeResult resumed = store.resume(first.nextCursor(), "query", 2);
        assertNull(resumed.error());
        assertEquals(1, resumed.page().matches().size());
        assertEquals(2, resumed.page().start());
        assertEquals(3, resumed.page().totalMatches());
        assertNull(resumed.page().nextCursor());
    }

    @Test
    void snapshotPagesSurviveIndexGenerationAdvances() {
        // Pages come from one immutable snapshot; staleness is reported through
        // the snapshot generation, never enforced by cursor invalidation.
        SearchSnapshotStore store = new SearchSnapshotStore();
        SearchSnapshotStore.Page first = store.first(
            "query",
            result(7, 0, match(1, 2), match(2, 8)),
            1
        );

        SearchSnapshotStore.ResumeResult resumed = store.resume(first.nextCursor(), "query", 1);

        assertNull(resumed.error());
        assertEquals(7, resumed.page().generation());
        assertEquals(List.of(match(2, 8)), resumed.page().matches());
    }

    @Test
    void coverageFactsSurviveResume() {
        SearchSnapshotStore store = new SearchSnapshotStore();
        SearchSnapshotStore.Page first = store.first(
            "query",
            result(11, 3, match(1, 2), match(2, 8)),
            1
        );

        assertFalse(first.coverageComplete());
        assertEquals(3, first.unloadedChunkCount());
        SearchSnapshotStore.ResumeResult resumed = store.resume(first.nextCursor(), "query", 1);
        assertNull(resumed.error());
        assertFalse(resumed.page().coverageComplete());
        assertEquals(3, resumed.page().unloadedChunkCount());
        assertEquals(11, resumed.page().generation());
    }

    @Test
    void mismatchedRequestShapeRejectsTheCursor() {
        SearchSnapshotStore store = new SearchSnapshotStore();
        SearchSnapshotStore.Page first = store.first(
            "query-a",
            result(7, 0, match(1, 2), match(2, 8)),
            1
        );

        SearchSnapshotStore.ResumeResult resumed = store.resume(first.nextCursor(), "query-b", 1);

        assertEquals("cursor_request_mismatch", resumed.error());
        assertNull(resumed.page());
    }

    @Test
    void unknownCursorIsMissingAndCursorsAreSingleUse() {
        SearchSnapshotStore store = new SearchSnapshotStore();
        assertEquals("cursor_missing", store.resume("nope", "query", 1).error());

        SearchSnapshotStore.Page first = store.first(
            "query",
            result(7, 0, match(1, 2), match(2, 8), match(3, 18)),
            1
        );
        SearchSnapshotStore.ResumeResult resumed = store.resume(first.nextCursor(), "query", 1);
        assertNull(resumed.error());
        // A consumed cursor is gone; the page issues a fresh one.
        assertEquals("cursor_missing", store.resume(first.nextCursor(), "query", 1).error());
        assertNotNull(resumed.page().nextCursor());
        assertTrue(store.resume(resumed.page().nextCursor(), "query", 1).error() == null);
    }
}
