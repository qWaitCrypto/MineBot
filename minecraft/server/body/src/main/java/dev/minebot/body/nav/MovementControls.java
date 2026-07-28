package dev.minebot.body.nav;

/**
 * The movement subset of the public command adapter that navigation needs,
 * kept as a seam so the executor is unit-testable with a recording fake.
 */
public interface MovementControls {
    void lookAt(String botName, double x, double y, double z);

    void moveForward(String botName);

    void stopMovement(String botName);

    void jumpOnce(String botName);

    void jumpContinuous(String botName);

    void sprint(String botName);
}
