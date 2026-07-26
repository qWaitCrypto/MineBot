package dev.minebot.body.search;

import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;

final class SearchSnapshotStoreTest {
    @Test
    void pagesResumeWithoutRebuildingTheSearchResult() {
        SearchSnapshotStore store = new SearchSnapshotStore();
        LoadedSearchResult result = new LoadedSearchResult(
            7,
            List.of(
                new SearchMatch(1, 64, 1, "minecraft:oak_log", 2),
                new SearchMatch(2, 64, 2, "minecraft:oak_log", 8),
                new SearchMatch(3, 64, 3, "minecraft:oak_log", 18)
            ),
            0,
            0,
            false
        );

        SearchSnapshotStore.Page first = store.first("query", result, 2);

        assertEquals(2, first.matches().size());
        assertNotNull(first.nextCursor());
        SearchSnapshotStore.ResumeResult resumed = store.resume(first.nextCursor(), "query", 7, 2);
        assertNull(resumed.error());
        assertEquals(1, resumed.page().matches().size());
        assertNull(resumed.page().nextCursor());
    }

    @Test
    void generationChangeRejectsTheCursorRatherThanMixingSnapshots() {
        SearchSnapshotStore store = new SearchSnapshotStore();
        SearchSnapshotStore.Page first = store.first(
            "query",
            new LoadedSearchResult(7, List.of(
                new SearchMatch(1, 64, 1, "minecraft:oak_log", 2),
                new SearchMatch(2, 64, 2, "minecraft:oak_log", 8)
            ), 0, 0, false),
            1
        );

        SearchSnapshotStore.ResumeResult resumed = store.resume(first.nextCursor(), "query", 8, 1);

        assertEquals("cursor_stale", resumed.error());
        assertFalse(resumed.page() != null);
    }
}
