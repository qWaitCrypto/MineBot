package dev.minebot.body.search;

public record SearchMatch(
    int x,
    int y,
    int z,
    String blockId,
    double distanceSquared
) {
}
