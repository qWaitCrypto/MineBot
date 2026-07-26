package dev.minebot.body.search;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;

public final class SearchSnapshotStore {
    public static final int MAX_SNAPSHOTS = 128;
    private final Map<String, Snapshot> snapshots = new LinkedHashMap<>(MAX_SNAPSHOTS + 1, 0.75F, true) {
        @Override
        protected boolean removeEldestEntry(Map.Entry<String, Snapshot> eldest) {
            return size() > MAX_SNAPSHOTS;
        }
    };

    public synchronized Page first(
        String fingerprint,
        LoadedSearchResult result,
        int limit
    ) {
        Snapshot snapshot = new Snapshot(fingerprint, result.generation(), List.copyOf(result.matches()), result.coverageComplete(), result.resultCapped());
        return page(snapshot, 0, limit, true);
    }

    public synchronized ResumeResult resume(
        String cursor,
        String fingerprint,
        long currentGeneration,
        int limit
    ) {
        Snapshot snapshot = snapshots.remove(cursor);
        if (snapshot == null) {
            return ResumeResult.invalid("cursor_missing");
        }
        if (!snapshot.fingerprint.equals(fingerprint)) {
            return ResumeResult.invalid("cursor_request_mismatch");
        }
        if (snapshot.generation != currentGeneration) {
            return ResumeResult.invalid("cursor_stale");
        }
        return ResumeResult.page(page(snapshot, snapshot.nextOffset, limit, false));
    }

    private Page page(Snapshot snapshot, int start, int limit, boolean initial) {
        int end = Math.min(start + limit, snapshot.matches.size());
        List<SearchMatch> entries = List.copyOf(new ArrayList<>(snapshot.matches.subList(start, end)));
        boolean more = end < snapshot.matches.size();
        String cursor = null;
        if (more) {
            cursor = UUID.randomUUID().toString();
            snapshots.put(cursor, snapshot.withNextOffset(end));
        }
        return new Page(entries, cursor, snapshot.coverageComplete, snapshot.resultCapped, initial);
    }

    public record Page(
        List<SearchMatch> matches,
        String nextCursor,
        boolean coverageComplete,
        boolean resultCapped,
        boolean initial
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
        int nextOffset
    ) {
        private Snapshot(String fingerprint, long generation, List<SearchMatch> matches, boolean coverageComplete, boolean resultCapped) {
            this(fingerprint, generation, matches, coverageComplete, resultCapped, 0);
        }

        private Snapshot withNextOffset(int nextOffset) {
            return new Snapshot(fingerprint, generation, matches, coverageComplete, resultCapped, nextOffset);
        }
    }
}
