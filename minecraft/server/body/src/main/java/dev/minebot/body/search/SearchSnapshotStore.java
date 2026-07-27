package dev.minebot.body.search;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;

/**
 * Materialized paging snapshots for search results. Pages resume one immutable
 * snapshot and never rescan, so a cursor stays valid across ordinary world and
 * index changes; staleness is reported through the snapshot's generation, not
 * enforced by invalidation. Cursors die only by eviction ({@code
 * cursor_missing}) or a mismatched request shape ({@code
 * cursor_request_mismatch}).
 */
public final class SearchSnapshotStore {
    public static final int MAX_SNAPSHOTS = 128;
    private final Map<String, Snapshot> snapshots = new LinkedHashMap<>(MAX_SNAPSHOTS + 1, 0.75F, true) {
        @Override
        protected boolean removeEldestEntry(Map.Entry<String, Snapshot> eldest) {
            return size() > MAX_SNAPSHOTS;
        }
    };

    public synchronized Page first(String fingerprint, LoadedSearchResult result, int limit) {
        Snapshot snapshot = new Snapshot(
            fingerprint,
            result.generation(),
            List.copyOf(result.matches()),
            result.coverageComplete(),
            result.resultCapped(),
            result.unloadedChunkCount()
        );
        return page(snapshot, 0, limit);
    }

    public synchronized ResumeResult resume(String cursor, String fingerprint, int limit) {
        Snapshot snapshot = snapshots.remove(cursor);
        if (snapshot == null) {
            return ResumeResult.invalid("cursor_missing");
        }
        if (!snapshot.fingerprint.equals(fingerprint)) {
            return ResumeResult.invalid("cursor_request_mismatch");
        }
        return ResumeResult.page(page(snapshot, snapshot.nextOffset, limit));
    }

    private Page page(Snapshot snapshot, int start, int limit) {
        int end = Math.min(start + limit, snapshot.matches.size());
        List<SearchMatch> entries = List.copyOf(new ArrayList<>(snapshot.matches.subList(start, end)));
        boolean more = end < snapshot.matches.size();
        String cursor = null;
        if (more) {
            cursor = UUID.randomUUID().toString();
            snapshots.put(cursor, snapshot.withNextOffset(end));
        }
        return new Page(
            start,
            snapshot.matches.size(),
            entries,
            cursor,
            snapshot.coverageComplete,
            snapshot.resultCapped,
            snapshot.unloadedChunkCount,
            snapshot.generation
        );
    }

    public record Page(
        int start,
        int totalMatches,
        List<SearchMatch> matches,
        String nextCursor,
        boolean coverageComplete,
        boolean resultCapped,
        int unloadedChunkCount,
        long generation
    ) {
    }

    public record ResumeResult(Page page, String error) {
        public static ResumeResult page(Page page) {
            return new ResumeResult(page, null);
        }

        public static ResumeResult invalid(String error) {
            return new ResumeResult(null, error);
        }
    }

    private record Snapshot(
        String fingerprint,
        long generation,
        List<SearchMatch> matches,
        boolean coverageComplete,
        boolean resultCapped,
        int unloadedChunkCount,
        int nextOffset
    ) {
        private Snapshot(
            String fingerprint,
            long generation,
            List<SearchMatch> matches,
            boolean coverageComplete,
            boolean resultCapped,
            int unloadedChunkCount
        ) {
            this(fingerprint, generation, matches, coverageComplete, resultCapped, unloadedChunkCount, 0);
        }

        private Snapshot withNextOffset(int nextOffset) {
            return new Snapshot(fingerprint, generation, matches, coverageComplete, resultCapped, unloadedChunkCount, nextOffset);
        }
    }
}
