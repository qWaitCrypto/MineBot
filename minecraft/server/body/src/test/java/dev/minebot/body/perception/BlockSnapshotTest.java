package dev.minebot.body.perception;

import org.junit.jupiter.api.Test;

import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;

final class BlockSnapshotTest {
    @Test
    void cubeOrderMatchesTheExistingXThenYThenZContract() {
        BlockSnapshot.Position center = new BlockSnapshot.Position(10, 64, -5);

        assertEquals(new BlockSnapshot.Position(9, 63, -6), BlockSnapshot.positionAt(center, 1, 0));
        assertEquals(new BlockSnapshot.Position(9, 63, -5), BlockSnapshot.positionAt(center, 1, 1));
        assertEquals(new BlockSnapshot.Position(9, 64, -6), BlockSnapshot.positionAt(center, 1, 3));
        assertEquals(new BlockSnapshot.Position(11, 65, -4), BlockSnapshot.positionAt(center, 1, 26));
    }

    @Test
    void filteredPagingResumesAtTheFirstUnemittedSolidCell() {
        BlockSnapshot.Position center = new BlockSnapshot.Position(0, 0, 0);
        BlockSnapshot.CubePage first = BlockSnapshot.scanCube(
            center,
            1,
            0,
            2,
            false,
            position -> fact(position, position.z() == 0 ? "SOLID" : "CLEAR")
        );

        assertEquals(2, first.facts().size());
        assertEquals(7, first.nextStart());
        assertEquals(new BlockSnapshot.Position(-1, -1, 0), position(first.facts().get(0)));
        assertEquals(new BlockSnapshot.Position(-1, 0, 0), position(first.facts().get(1)));

        BlockSnapshot.CubePage rest = BlockSnapshot.scanCube(
            center,
            1,
            first.nextStart(),
            20,
            false,
            position -> fact(position, position.z() == 0 ? "SOLID" : "CLEAR")
        );
        assertEquals(7, rest.facts().size());
        assertNull(rest.nextStart());
    }

    @Test
    void unfilteredPagingReturnsEveryCell() {
        BlockSnapshot.CubePage page = BlockSnapshot.scanCube(
            new BlockSnapshot.Position(0, 0, 0),
            1,
            -5,
            4,
            true,
            position -> fact(position, "CLEAR")
        );

        assertEquals(0, page.start());
        assertEquals(4, page.facts().size());
        assertEquals(4, page.nextStart());
        assertEquals(27, page.total());
    }

    private static BlockSnapshot.Fact fact(BlockSnapshot.Position position, String state) {
        return new BlockSnapshot.Fact(
            position.x(), position.y(), position.z(), "minecraft:stone", state, Map.of()
        );
    }

    private static BlockSnapshot.Position position(BlockSnapshot.Fact fact) {
        return new BlockSnapshot.Position(fact.x(), fact.y(), fact.z());
    }
}
