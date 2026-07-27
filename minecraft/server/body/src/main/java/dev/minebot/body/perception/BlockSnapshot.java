package dev.minebot.body.perception;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.function.Function;

/** Pure paging and cube-order logic for authoritative block facts. */
public final class BlockSnapshot {
    private BlockSnapshot() {}

    public static CubePage scanCube(
        Position center,
        int radius,
        int requestedStart,
        int requestedLimit,
        boolean includeClear,
        Function<Position, Fact> reader
    ) {
        int side = radius * 2 + 1;
        int total = side * side * side;
        int start = Math.max(0, Math.min(total, requestedStart));
        int limit = Math.max(1, requestedLimit);
        int index = start;
        List<Fact> facts = new ArrayList<>();
        while (index < total) {
            Position position = positionAt(center, radius, index);
            Fact fact = reader.apply(position);
            if (includeClear || !"CLEAR".equals(fact.state())) {
                if (facts.size() >= limit) {
                    break;
                }
                facts.add(fact);
            }
            index += 1;
        }
        return new CubePage(start, limit, index >= total ? null : index, total, List.copyOf(facts));
    }

    public static Position positionAt(Position center, int radius, int index) {
        int side = radius * 2 + 1;
        int plane = side * side;
        int ox = index / plane;
        int remainder = index - ox * plane;
        int oy = remainder / side;
        int oz = remainder - oy * side;
        return new Position(
            center.x() + ox - radius,
            center.y() + oy - radius,
            center.z() + oz - radius
        );
    }

    public record Position(int x, int y, int z) {}

    public record Fact(
        int x,
        int y,
        int z,
        String type,
        String state,
        Map<String, String> properties
    ) {
        public Fact {
            properties = Map.copyOf(properties);
        }
    }

    public record SurfaceColumn(
        int x,
        int z,
        int feetY,
        Fact feet,
        Fact head,
        Fact support
    ) {}

    public record CubePage(
        int start,
        int limit,
        Integer nextStart,
        int total,
        List<Fact> facts
    ) {}
}
