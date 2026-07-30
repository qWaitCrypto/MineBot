package dev.minebot.body.nav;

import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Set;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

final class ExcavationPlannerTest {
    private static final int FLOOR = 63;
    private static final int STAND = FLOOR + 1;

    @Test
    void plansACompleteTwoBlockHighTunnel() {
        FakeWorld world = sealedHorizontalTunnelWorld();
        ExcavationPlanner.Result result = ExcavationPlanner.plan(
            world,
            new ExcavationPlanner.Cell(0, STAND, 0),
            List.of(new ExcavationPlanner.Target(4, STAND, 0)),
            Set.of(),
            6
        );

        assertTrue(result.success(), result.reason());
        assertEquals(new ExcavationPlanner.Target(4, STAND, 0), result.plan().target());
        assertEquals(3, result.plan().steps().size());
        assertEquals(6, result.plan().breakCount());
        for (int index = 0; index < 3; index++) {
            ExcavationPlanner.Step step = result.plan().steps().get(index);
            assertEquals(new ExcavationPlanner.Cell(index + 1, STAND, 0), step.stand());
            assertEquals(ExcavationPlanner.StepMode.WALK, step.mode());
            assertEquals(
                List.of(
                    new ExcavationPlanner.Cell(index + 1, STAND + 1, 0),
                    new ExcavationPlanner.Cell(index + 1, STAND, 0)
                ),
                step.blockers()
            );
        }
    }

    @Test
    void refusesATunnelThatExceedsTheExistingBreakBudget() {
        ExcavationPlanner.Result result = ExcavationPlanner.plan(
            sealedHorizontalTunnelWorld(),
            new ExcavationPlanner.Cell(0, STAND, 0),
            List.of(new ExcavationPlanner.Target(4, STAND, 0)),
            Set.of(),
            5
        );

        assertFalse(result.success());
        assertEquals("no_excavation_route", result.reason());
    }

    @Test
    void plansASupportedSameColumnDescentWithoutDoubleBreakingHeadroom() {
        FakeWorld world = new FakeWorld(FLOOR);
        for (int y = STAND; y <= STAND + 4; y++) {
            world.set(0, y, 0, WorldView.NodeKind.SOLID);
        }
        world.set(0, STAND + 4, 0, WorldView.NodeKind.PASSABLE);
        world.set(0, STAND + 5, 0, WorldView.NodeKind.PASSABLE);
        for (int x = -3; x <= 3; x++) {
            for (int y = STAND - 3; y <= STAND + 6; y++) {
                for (int z = -3; z <= 3; z++) {
                    if (x != 0 || z != 0) {
                        world.hazardBodyPosition(x, y, z);
                    }
                }
            }
        }

        ExcavationPlanner.Result result = ExcavationPlanner.plan(
            world,
            new ExcavationPlanner.Cell(0, STAND + 4, 0),
            List.of(new ExcavationPlanner.Target(0, STAND, 0)),
            Set.of(),
            3
        );

        assertTrue(result.success(), result.reason());
        assertEquals(3, result.plan().breakCount());
        assertEquals(3, result.plan().steps().size());
        assertTrue(result.plan().steps().stream().allMatch(
            step -> step.mode() == ExcavationPlanner.StepMode.DESCEND
                && step.blockers().equals(List.of(step.stand()))
        ));
        assertEquals(
            new ExcavationPlanner.Cell(0, STAND + 1, 0),
            result.plan().steps().getLast().stand()
        );
    }

    private static FakeWorld sealedHorizontalTunnelWorld() {
        FakeWorld world = new FakeWorld(FLOOR);
        for (int x = -1; x <= 5; x++) {
            for (int y = STAND; y <= STAND + 2; y++) {
                for (int z = -1; z <= 1; z++) {
                    world.set(x, y, z, WorldView.NodeKind.SOLID);
                }
            }
        }
        world.set(0, STAND, 0, WorldView.NodeKind.PASSABLE);
        world.set(0, STAND + 1, 0, WorldView.NodeKind.PASSABLE);
        for (int x = -8; x <= 8; x++) {
            for (int y = STAND - 4; y <= STAND + 5; y++) {
                for (int z = -8; z <= 8; z++) {
                    boolean intendedTunnel = z == 0 && y == STAND && x >= 0 && x <= 3;
                    if (!intendedTunnel) {
                        world.hazardBodyPosition(x, y, z);
                    }
                }
            }
        }
        return world;
    }
}
