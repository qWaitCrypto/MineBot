package dev.minebot.body.perception;

import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Set;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

final class EntitySnapshotTest {
    @Test
    void filtersBeforeCappingAndOrdersByDistance() {
        EntitySnapshot.Page page = EntitySnapshot.select(
            List.of(
                fact("far-cow", "minecraft:cow", "Cow", 9.0),
                fact("near-item", "minecraft:item", "Oak Log", 1.0),
                fact("near-cow", "minecraft:cow", "Cow", 4.0)
            ),
            Set.of("minecraft:cow"),
            null,
            1
        );

        assertEquals(1, page.entities().size());
        assertEquals("near-cow", page.entities().getFirst().id());
        assertEquals(2, page.totalMatches());
        assertFalse(page.complete());
    }

    @Test
    void exactNameFilterKeepsStableIdentity() {
        List<EntitySnapshot.Fact> candidates = List.of(
            fact("other", "minecraft:player", "OtherPlayer", 1.0),
            fact("guide", "minecraft:player", "MineBotGuide", 4.0)
        );
        EntitySnapshot.Page page = EntitySnapshot.select(
            candidates,
            Set.of("minecraft:player"),
            "MineBotGuide",
            8
        );

        assertEquals(List.of("guide"), page.entities().stream().map(EntitySnapshot.Fact::id).toList());
        assertEquals(1, page.totalMatches());
        assertTrue(page.complete());

        EntitySnapshot.Page emptyName = EntitySnapshot.select(
            candidates, Set.of("minecraft:player"), "", 8
        );
        assertEquals(2, emptyName.totalMatches());
        assertTrue(emptyName.complete());
    }

    private static EntitySnapshot.Fact fact(String id, String type, String name, double distanceSquared) {
        return new EntitySnapshot.Fact(id, type, name, 0.0, 64.0, 0.0, null, distanceSquared);
    }
}
