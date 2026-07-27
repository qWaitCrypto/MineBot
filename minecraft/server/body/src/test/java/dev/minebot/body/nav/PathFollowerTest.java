package dev.minebot.body.nav;

import dev.minebot.body.nav.AStarPathfinder.Waypoint;
import dev.minebot.body.nav.PathFollower.State;
import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

final class PathFollowerTest {
    private static final List<Waypoint> STRAIGHT = List.of(
        new Waypoint(0, 64, 0),
        new Waypoint(1, 64, 0),
        new Waypoint(2, 64, 0),
        new Waypoint(3, 64, 0)
    );

    @Test
    void walkingThePathAdvancesWaypointsAndArrives() {
        PathFollower follower = new PathFollower(STRAIGHT);
        PathFollower.Directive directive = null;
        for (double x = 0.5; x <= 3.5; x += 0.25) {
            directive = follower.tick(x, 64, 0.5);
            if (directive.state() == State.ARRIVED) {
                break;
            }
            assertEquals(State.CONTINUE, directive.state());
            assertTrue(directive.forward());
        }
        assertEquals(State.ARRIVED, directive.state());
    }

    @Test
    void tighterFinalReachDoesNotChangeIntermediateWaypointTolerance() {
        PathFollower follower = new PathFollower(STRAIGHT, 0.15);

        assertEquals(State.CONTINUE, follower.tick(0.5, 64, 0.5).state());
        assertEquals(State.CONTINUE, follower.tick(2.5, 64, 0.5).state());
        assertEquals(State.CONTINUE, follower.tick(3.0, 64, 0.5).state());
        assertEquals(State.ARRIVED, follower.tick(3.36, 64, 0.5).state());
    }

    @Test
    void jumpIsRequestedApproachingAStepUp() {
        PathFollower follower = new PathFollower(List.of(
            new Waypoint(0, 64, 0),
            new Waypoint(1, 65, 0)
        ));
        PathFollower.Directive far = follower.tick(0.2, 64, 0.5);
        assertFalse(far.jump(), "no jump while far from the step");
        PathFollower.Directive near = follower.tick(0.8, 64, 0.5);
        assertEquals(State.CONTINUE, near.state());
        assertTrue(near.jump(), "jump when close to a higher waypoint");
    }

    @Test
    void lagResyncSkipsForwardInsteadOfWalkingBack() {
        PathFollower follower = new PathFollower(STRAIGHT);
        follower.tick(0.5, 64, 0.5);
        // The bot ends up near waypoint 2 after lag.
        PathFollower.Directive directive = follower.tick(2.4, 64, 0.5);
        assertEquals(State.CONTINUE, directive.state());
        assertTrue(follower.waypointIndex() >= 2, "resync must move the cursor forward");
    }

    @Test
    void leavingThePathIsDeviationNotSilentWander() {
        PathFollower follower = new PathFollower(STRAIGHT);
        follower.tick(0.5, 64, 0.5);
        PathFollower.Directive directive = follower.tick(0.5, 64, 9.0);
        assertEquals(State.DEVIATED, directive.state());
        assertFalse(directive.forward());
    }

    @Test
    void frozenPositionBecomesStuckAfterTheWindow() {
        PathFollower follower = new PathFollower(STRAIGHT);
        PathFollower.Directive directive = follower.tick(0.9, 64, 0.5);
        for (int i = 0; i < PathFollower.STUCK_TICKS + 5; i++) {
            directive = follower.tick(0.9, 64, 0.5);
            if (directive.state() == State.STUCK) {
                break;
            }
        }
        assertEquals(State.STUCK, directive.state());
    }

    @Test
    void progressKeepsResettingTheStuckWindow() {
        PathFollower follower = new PathFollower(STRAIGHT);
        double x = 0.5;
        PathFollower.Directive directive = follower.tick(x, 64, 0.5);
        for (int i = 0; i < PathFollower.STUCK_TICKS * 3; i++) {
            x += 0.01;
            directive = follower.tick(x, 64, 0.5);
            assertTrue(directive.state() == State.CONTINUE || directive.state() == State.ARRIVED);
            if (directive.state() == State.ARRIVED) {
                return;
            }
        }
    }
}
