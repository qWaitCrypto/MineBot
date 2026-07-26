package dev.minebot.body.search;

import java.util.List;

public record LoadedSearchResult(
    long generation,
    List<SearchMatch> matches,
    int unloadedChunkCount,
    int pendingChunkCount,
    boolean resultCapped
) {
    public boolean coverageComplete() {
        return unloadedChunkCount == 0 && pendingChunkCount == 0;
    }
}
