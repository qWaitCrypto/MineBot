package dev.minebot.body.search;

import java.util.List;

public record LoadedSearchResult(
    long generation,
    List<SearchMatch> matches,
    int unloadedChunkCount,
    boolean resultCapped
) {
    public boolean coverageComplete() {
        return unloadedChunkCount == 0;
    }
}
