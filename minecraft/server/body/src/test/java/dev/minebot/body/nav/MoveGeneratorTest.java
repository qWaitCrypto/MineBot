package dev.minebot.body.nav;

import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

final class MoveGeneratorTest {
    private static final int FLOOR = 63;
    private static final int STAND = FLOOR + 1;

    private static boolean hasMoveTo(List<MoveGenerator.Move> moves, int x, int y, int z) {
        return moves.stream().anyMatch(m -> m.x() == x && m.y() == y && m.z() == z);
    }

    private static MoveGenerator.Move moveTo(List<MoveGenerator.Move> moves, int x, int y, int z) {
        return moves.stream().filter(m -> m.x() == x && m.y() == y && m.z() == z).findFirst().orElseThrow();
    }

    @Test
    void flatLandOffersFourWalkAndFourDiagonalMoves() {
        MoveGenerator gen = new MoveGenerator(new FakeWorld(FLOOR));
        List<MoveGenerator.Move> moves = gen.movesFrom(0, STAND, 0).moves();
        assertEquals(8, moves.size());
        assertTrue(hasMoveTo(moves, 1, STAND, 0));
        assertTrue(hasMoveTo(moves, 1, STAND, 1));
    }

    @Test
    void hazardIsNeverAMoveTarget() {
        FakeWorld world = new FakeWorld(FLOOR);
        world.set(1, STAND, 0, WorldView.NodeKind.HAZARD);
        world.set(1, FLOOR, 0, WorldView.NodeKind.HAZARD);
        List<MoveGenerator.Move> moves = new MoveGenerator(world).movesFrom(0, STAND, 0).moves();
        assertFalse(hasMoveTo(moves, 1, STAND, 0));
    }

    @Test
    void waterCanBeEnteredHorizontallyAtSwimCost() {
        FakeWorld world = new FakeWorld(FLOOR);
        world.set(1, STAND, 0, WorldView.NodeKind.LIQUID);
        world.set(1, STAND + 1, 0, WorldView.NodeKind.LIQUID);
        List<MoveGenerator.Move> moves = new MoveGenerator(world).movesFrom(0, STAND, 0).moves();
        assertTrue(hasMoveTo(moves, 1, STAND, 0));
        assertEquals(MoveGenerator.SWIM_COST, moveTo(moves, 1, STAND, 0).cost());
    }

    @Test
    void swimUpAndDownAreOfferedInsideAWaterColumn() {
        FakeWorld world = new FakeWorld(FLOOR);
        // A water column at (0, z) from STAND up to STAND+3.
        for (int y = STAND; y <= STAND + 3; y++) {
            world.set(0, y, 0, WorldView.NodeKind.LIQUID);
        }
        // Body floating mid-column.
        List<MoveGenerator.Move> moves = new MoveGenerator(world).movesFrom(0, STAND + 1, 0).moves();
        assertTrue(hasMoveTo(moves, 0, STAND + 2, 0), "swim up available");
        assertTrue(hasMoveTo(moves, 0, STAND, 0), "sink available");
        assertEquals(MoveGenerator.SWIM_UP_COST, moveTo(moves, 0, STAND + 2, 0).cost());
    }

    @Test
    void swimUpToAirSurfaceEscapesTheColumn() {
        FakeWorld world = new FakeWorld(FLOOR);
        world.set(0, STAND, 0, WorldView.NodeKind.LIQUID);
        // Air above the water surface.
        List<MoveGenerator.Move> moves = new MoveGenerator(world).movesFrom(0, STAND, 0).moves();
        assertTrue(hasMoveTo(moves, 0, STAND + 1, 0), "can rise to the surface air cell");
    }

    @Test
    void waterCannotPromiseAnUnexecutableFullBlockStep() {
        FakeWorld world = new FakeWorld(FLOOR);
        world.set(0, STAND, 0, WorldView.NodeKind.LIQUID);
        // The neighboring solid fills the current body-height cell, so its top
        // is one full block above this water node.
        world.set(1, STAND, 0, WorldView.NodeKind.SOLID);
        List<MoveGenerator.Move> moves = new MoveGenerator(world).movesFrom(0, STAND, 0).moves();
        assertFalse(hasMoveTo(moves, 1, STAND + 1, 0));
    }

    @Test
    void waterSurfaceCanUseALevelShore() {
        FakeWorld world = new FakeWorld(FLOOR);
        world.set(0, FLOOR, 0, WorldView.NodeKind.LIQUID);

        List<MoveGenerator.Move> moves = new MoveGenerator(world).movesFrom(0, STAND, 0).moves();

        assertTrue(hasMoveTo(moves, 1, STAND, 0));
    }

    @Test
    void airAboveWaterKeepsSurfaceFootHeight() {
        FakeWorld world = new FakeWorld(FLOOR);
        // East column: air at STAND, water at FLOOR (surface one below).
        world.set(1, FLOOR, 0, WorldView.NodeKind.LIQUID);
        List<MoveGenerator.Move> moves = new MoveGenerator(world).movesFrom(0, STAND, 0).moves();
        assertTrue(hasMoveTo(moves, 1, STAND, 0), "stay above the water surface");
        assertFalse(hasMoveTo(moves, 1, FLOOR, 0), "surface traversal must not become a dive");
    }

    @Test
    void aSurfaceNodeCanDiveVerticallyForASubmergedObjective() {
        FakeWorld world = new FakeWorld(FLOOR - 4);
        for (int y = FLOOR - 3; y <= FLOOR; y++) {
            world.set(0, y, 0, WorldView.NodeKind.LIQUID);
        }

        List<MoveGenerator.Move> moves = new MoveGenerator(world).movesFrom(0, STAND, 0).moves();

        assertTrue(hasMoveTo(moves, 0, FLOOR, 0));
    }

    @Test
    void fallingIntoDeepWaterIsAllowedBeyondSolidFallLimit() {
        FakeWorld world = new FakeWorld(FLOOR);
        // East: a shaft of air with water far below (deeper than MAX_FALL_BLOCKS).
        int waterY = STAND - 8;
        for (int y = FLOOR; y > waterY; y--) {
            world.set(1, y, 0, WorldView.NodeKind.PASSABLE);
        }
        world.set(1, waterY, 0, WorldView.NodeKind.LIQUID);
        List<MoveGenerator.Move> moves = new MoveGenerator(world).movesFrom(0, STAND, 0).moves();
        MoveGenerator.Move fall = moveTo(moves, 1, waterY + 1, 0);
        assertTrue(hasMoveTo(moves, 1, waterY + 1, 0), "a deep water landing is reachable");
        assertEquals(
            MoveGenerator.WALK_COST
                + MoveGenerator.SWIM_COST
                + 7 * MoveGenerator.FALL_PER_BLOCK_COST,
            fall.cost()
        );
    }

    @Test
    void deepSolidDropBeyondFallLimitIsRefused() {
        FakeWorld world = new FakeWorld(FLOOR);
        int solidY = STAND - 8;
        for (int y = FLOOR; y > solidY; y--) {
            world.set(1, y, 0, WorldView.NodeKind.PASSABLE);
        }
        world.set(1, solidY, 0, WorldView.NodeKind.SOLID);
        List<MoveGenerator.Move> moves = new MoveGenerator(world).movesFrom(0, STAND, 0).moves();
        assertFalse(hasMoveTo(moves, 1, solidY + 1, 0), "a lethal solid drop stays refused");
    }

    @Test
    void landOnlyWorldIsUnchangedByLiquidLogic() {
        // Regression: with no liquid anywhere, the move set is exactly walking.
        MoveGenerator gen = new MoveGenerator(new FakeWorld(FLOOR));
        List<MoveGenerator.Move> moves = gen.movesFrom(5, STAND, 5).moves();
        assertEquals(8, moves.size());
        assertTrue(moves.stream().allMatch(m -> m.y() == STAND));
    }

    @Test
    void ladderColumnOffersClimbUpAndDown() {
        FakeWorld world = new FakeWorld(FLOOR);
        for (int y = STAND; y <= STAND + 4; y++) {
            world.set(0, y, 0, WorldView.NodeKind.CLIMBABLE);
        }
        List<MoveGenerator.Move> moves = new MoveGenerator(world).movesFrom(0, STAND + 1, 0).moves();
        assertTrue(hasMoveTo(moves, 0, STAND + 2, 0), "climb up the ladder");
        assertTrue(hasMoveTo(moves, 0, STAND, 0), "climb down the ladder");
        assertEquals(MoveGenerator.CLIMB_UP_COST, moveTo(moves, 0, STAND + 2, 0).cost());
    }

    @Test
    void climbUpToAirExitsAtTheTop() {
        FakeWorld world = new FakeWorld(FLOOR);
        world.set(0, STAND, 0, WorldView.NodeKind.CLIMBABLE);
        // Air above the ladder top.
        List<MoveGenerator.Move> moves = new MoveGenerator(world).movesFrom(0, STAND, 0).moves();
        assertTrue(hasMoveTo(moves, 0, STAND + 1, 0), "step off the ladder top into air");
    }

    @Test
    void ladderIsEnteredHorizontallyAndSupportsAWalkOnTop() {
        FakeWorld world = new FakeWorld(FLOOR);
        world.set(1, STAND, 0, WorldView.NodeKind.CLIMBABLE);
        List<MoveGenerator.Move> moves = new MoveGenerator(world).movesFrom(0, STAND, 0).moves();
        assertTrue(hasMoveTo(moves, 1, STAND, 0), "grab the adjacent ladder");
    }

    @Test
    void landOnlyWorldIsUnchangedByClimbLogic() {
        // Regression: no climbable anywhere -> exactly the 8 walking moves.
        List<MoveGenerator.Move> moves = new MoveGenerator(new FakeWorld(FLOOR)).movesFrom(3, STAND, 3).moves();
        assertEquals(8, moves.size());
    }
}
