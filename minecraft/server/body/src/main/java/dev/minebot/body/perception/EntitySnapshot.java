package dev.minebot.body.perception;

import java.util.Comparator;
import java.util.List;
import java.util.Set;

/** Pure filtering, ordering, and capping for moving entity facts. */
public final class EntitySnapshot {
    private EntitySnapshot() {}

    public static Page select(
        List<Fact> candidates,
        Set<String> wantedTypes,
        String wantedName,
        int limit
    ) {
        List<Fact> matches = candidates.stream()
            .filter(fact -> wantedTypes.isEmpty() || wantedTypes.contains(fact.type()))
            .filter(fact -> wantedName == null || wantedName.isEmpty() || wantedName.equals(fact.name()))
            .sorted(Comparator.comparingDouble(Fact::distanceSquared).thenComparing(Fact::id))
            .toList();
        int end = Math.min(Math.max(1, limit), matches.size());
        return new Page(List.copyOf(matches.subList(0, end)), matches.size(), end >= matches.size());
    }

    public record Fact(
        String id,
        String type,
        String name,
        double x,
        double y,
        double z,
        Double health,
        double distanceSquared
    ) {}

    public record Page(List<Fact> entities, int totalMatches, boolean complete) {}
}
