package dev.minebot.bridge.version;

import java.util.List;

public record SectionSnapshot(
    String subId,
    String dimension,
    int sectionX,
    int sectionY,
    int sectionZ,
    List<String> palette,
    int[] indices
) {
    public static final int SECTION_VOLUME = 16 * 16 * 16;

    public SectionSnapshot {
        palette = List.copyOf(palette);
        indices = indices.clone();
        if (indices.length != SECTION_VOLUME) {
            throw new IllegalArgumentException("section indices must have 4096 entries");
        }
    }

    @Override
    public int[] indices() {
        return indices.clone();
    }
}

