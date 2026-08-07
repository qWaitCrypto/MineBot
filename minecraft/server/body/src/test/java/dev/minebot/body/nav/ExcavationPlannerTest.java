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
    void refusesToClearAHeadCellThatWouldExposeAHazard() {
        FakeWorld world = sealedHorizontalTunnelWorld();
        world.hazardBlockClearance(1, STAND + 1, 0);

        ExcavationPlanner.Result result = ExcavationPlanner.plan(
            world,
            new ExcavationPlanner.Cell(0, STAND, 0),
            List.of(new ExcavationPlanner.Target(4, STAND, 0)),
            Set.of(),
            6
        );

        assertFalse(result.success());
        assertEquals("no_excavation_route", result.reason());
    }

    @Test
    void combinesExistingWaterMovementWithGovernedExcavation() {
        FakeWorld world = new FakeWorld(FLOOR);
        for (int x = 1; x <= 3; x++) {
            world.set(x, FLOOR, 0, WorldView.NodeKind.LIQUID);
        }
        for (int x = 4; x <= 5; x++) {
            world.set(x, STAND, 0, WorldView.NodeKind.SOLID);
            world.set(x, STAND + 1, 0, WorldView.NodeKind.SOLID);
        }
        world.set(6, STAND, 0, WorldView.NodeKind.SOLID);
        for (int x = -8; x <= 8; x++) {
            for (int y = STAND - 2; y <= STAND + 3; y++) {
                for (int z = -8; z <= 8; z++) {
                    if (z != 0) {
                        world.hazardBodyPosition(x, y, z);
                    }
                }
            }
        }

        ExcavationPlanner.Result result = ExcavationPlanner.plan(
            world,
            new ExcavationPlanner.Cell(0, STAND, 0),
            List.of(new ExcavationPlanner.Target(6, STAND, 0)),
            Set.of(),
            6
        );

        assertTrue(result.success(), result.reason());
        assertTrue(result.plan().breakCount() > 0);
        assertTrue(result.plan().breakCount() <= 4);
        assertEquals(
            List.of(1, 2, 3),
            result.plan().steps().stream()
                .filter(step -> step.blockers().isEmpty())
                .map(step -> step.stand().x())
                .filter(x -> x >= 1 && x <= 3)
                .toList()
        );
        assertTrue(result.plan().steps().stream().anyMatch(
            step -> step.stand().x() >= 4 && !step.blockers().isEmpty()
        ));
    }

    @Test
    void returnsADistanceReducingReturnSafeCorridorWhenTheTargetExceedsOneSegment() {
        int targetX = 7;
        ExcavationPlanner.Cell start = new ExcavationPlanner.Cell(0, STAND, 0);
        ExcavationPlanner.Result result = ExcavationPlanner.planWithReturnPath(
            sealedHorizontalTunnelWorld(targetX),
            start,
            List.of(new ExcavationPlanner.Target(targetX, STAND, 0)),
            Set.of(),
            8
        );

        assertTrue(result.success(), result.reason());
        assertEquals("partial_corridor", result.reason());
        assertFalse(result.plan().reachesTarget());
        assertEquals(8, result.plan().breakCount());
        assertEquals(new ExcavationPlanner.Cell(4, STAND, 0), result.plan().steps().getLast().stand());
        assertTrue(result.plan().steps().stream().allMatch(
            step -> step.mode() == ExcavationPlanner.StepMode.WALK
        ));
    }

    @Test
    void plansGovernedBreaksForOneDesignatedNavigationPredicate() {
        FakeWorld world = sealedHorizontalTunnelWorld();
        Goal goal = new Goal.Near(3, STAND, 0, 0.5);

        ExcavationPlanner.RouteResult result = ExcavationPlanner.planToGoal(
            world,
            new ExcavationPlanner.Cell(0, STAND, 0),
            goal,
            Set.of(),
            6,
            ExcavationPlanner.DEFAULT_NODE_CAP,
            ExcavationPlanner.DEFAULT_STEP_CAP,
            MoveGenerator.Policy.defaults()
        );

        assertTrue(result.success(), result.reason());
        assertTrue(result.plan().reachesGoal());
        assertTrue(result.plan().breakCount() > 0);
        ExcavationPlanner.Cell terminal = result.plan().steps().getLast().stand();
        assertTrue(goal.isSatisfied(world, terminal.x(), terminal.y(), terminal.z()));
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

        ExcavationPlanner.Result returnSafe = ExcavationPlanner.planWithReturnPath(
            world,
            new ExcavationPlanner.Cell(0, STAND + 4, 0),
            List.of(new ExcavationPlanner.Target(0, STAND, 0)),
            Set.of(),
            3
        );
        assertFalse(returnSafe.success());
        assertEquals("no_excavation_route", returnSafe.reason());
    }

    @Test
    void rejectsAGeometricTerminalWhoseTargetFaceRemainsOccluded() {
        FakeWorld world = new FakeWorld(FLOOR);
        ExcavationPlanner.Target target = new ExcavationPlanner.Target(0, STAND, 0);
        ExcavationPlanner.Cell occludedStart = new ExcavationPlanner.Cell(0, STAND + 1, -1);
        world.set(target.x(), target.y(), target.z(), WorldView.NodeKind.SOLID);
        world.set(0, STAND + 1, 0, WorldView.NodeKind.SOLID);
        world.set(0, STAND, -1, WorldView.NodeKind.SOLID);

        Goal.Interact interaction = new Goal.Interact(
            target.x(), target.y(), target.z(), Goal.Interact.MINE_RANGE
        );
        assertFalse(interaction.isSatisfied(
            world, occludedStart.x(), occludedStart.y(), occludedStart.z()
        ));

        ExcavationPlanner.Result result = ExcavationPlanner.planWithReturnPath(
            world,
            occludedStart,
            List.of(target),
            Set.of(),
            4
        );

        assertTrue(result.success(), result.reason());
        assertFalse(result.plan().steps().isEmpty(), "the occluded current cell must not be a zero-step terminal");
        ExcavationPlanner.Cell terminal = result.plan().steps().getLast().stand();
        assertTrue(interaction.isSatisfied(world, terminal.x(), terminal.y(), terminal.z()));
    }

    private static FakeWorld sealedHorizontalTunnelWorld() {
        return sealedHorizontalTunnelWorld(4);
    }

    private static FakeWorld sealedHorizontalTunnelWorld(int targetX) {
        FakeWorld world = new FakeWorld(FLOOR);
        for (int x = -1; x <= targetX + 1; x++) {
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
                    boolean intendedTunnel = z == 0 && y == STAND && x >= 0 && x < targetX;
                    if (!intendedTunnel) {
                        world.hazardBodyPosition(x, y, z);
                    }
                }
            }
        }
        return world;
    }
}
