package dev.minebot.body.nav;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

final class ApproachControllerTest {
    @Test
    void exactNearGoalKeepsMovingUntilInsideTheRequestedCell() {
        var controller = new ApproachController("Bot", "exact-near", new Goal.Near(0, 64, 0, 0.5),
            new FakeWorld(63), CONTROLS, (bot, tick, name, action, data) -> {}, 5);
        for (int tick = 0; tick < 4; tick++) {
            assertEquals(ApproachController.Status.WORKING,
                controller.tick(tick, 0.5, 64, -0.03).status());
            assertEquals(0, controller.replans(), "nearby but outside the goal cell must continue moving");
        }
        controller.tick(4, 0.5, 64, 0.5);
        assertEquals(ApproachController.Status.COMPLETED,
            controller.tick(5, 0.5, 64, 0.5).status());
    }

    @Test
    void inProgressSearchReportsWorkBeforeItHasAPath() {
        var controller = new ApproachController("Bot", "search", new Goal.Stand(0, 100, 0),
            new FakeWorld(63), CONTROLS, (bot, tick, name, action, data) -> {}, 0);
        assertEquals(ApproachController.Status.WORKING, controller.tick(0, 0.5, 64, 0.5).status());
        assertTrue(controller.expandedNodes() > 0, "a timed out active search must not report zero work");
        int previous = controller.expandedNodes();
        controller.halt();
        assertEquals(previous, controller.expandedNodes());
    }

    private static final MovementControls CONTROLS = new MovementControls() {
        public void lookAt(String bot, double x, double y, double z) {}
        public void moveForward(String bot) {}
        public void stopMovement(String bot) {}
        public void jumpOnce(String bot) {}
        public void jumpContinuous(String bot) {}
        public void sprint(String bot) {}
    };

    private static ApproachController controller(int replans) {
        return new ApproachController("Bot", "arrival", new Goal.Stand(0, 64, 0),
            new FakeWorld(63), CONTROLS, (bot, tick, name, action, data) -> {}, replans);
    }

    @Test
    void unsupportedArrivalExhaustsOnlyTheExistingStuckAndReplanAllowances() {
        for (int replans : new int[] {0, 2}) {
            var controller = controller(replans);
            ApproachController.Outcome outcome = null;
            int bound = (replans + 1) * (PathFollower.STUCK_TICKS + 3);
            for (int tick = 0; tick <= bound; tick++) {
                outcome = controller.tick(tick, 0.5, 64.2, 0.5);
                if (outcome.status() != ApproachController.Status.WORKING) {
                    break;
                }
            }
            assertEquals(ApproachController.Status.FAILED, outcome.status());
            assertEquals("replan_budget_exhausted", outcome.reason());
            assertEquals("arrival_unstable", controller.terminalDetail());
            assertEquals(replans, controller.replans());
        }
    }

    @Test
    void physicalSettlingBeforeTheStuckLimitStillCompletes() {
        var controller = controller(0);
        for (int tick = 0; tick < PathFollower.STUCK_TICKS / 2; tick++) {
            assertEquals(ApproachController.Status.WORKING,
                controller.tick(tick, 0.5, 64.2, 0.5).status());
        }
        assertEquals(ApproachController.Status.WORKING,
            controller.tick(31, 0.5, 64.0, 0.5).status());
        assertEquals(ApproachController.Status.COMPLETED,
            controller.tick(32, 0.5, 64.0, 0.5).status());
        assertEquals(0, controller.replans());
    }
}
