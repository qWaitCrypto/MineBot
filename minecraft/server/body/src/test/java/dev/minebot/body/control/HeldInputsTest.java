package dev.minebot.body.control;

import org.junit.jupiter.api.Test;

import java.util.Set;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

final class HeldInputsTest {
    @Test
    void drainReturnsExactlyTheEngagedSetAndConsumesIt() {
        HeldInputs inputs = new HeldInputs();
        inputs.engage("Bot", HeldInputs.Input.MOVEMENT);
        inputs.engage("Bot", HeldInputs.Input.SNEAK);

        assertEquals(Set.of(HeldInputs.Input.MOVEMENT, HeldInputs.Input.SNEAK), inputs.drain("Bot"));
        assertTrue(inputs.drain("Bot").isEmpty());
    }

    @Test
    void disengageRemovesOnlyThatInput() {
        HeldInputs inputs = new HeldInputs();
        inputs.engage("Bot", HeldInputs.Input.MOVEMENT);
        inputs.engage("Bot", HeldInputs.Input.SPRINT);
        inputs.disengage("Bot", HeldInputs.Input.MOVEMENT);

        assertEquals(Set.of(HeldInputs.Input.SPRINT), inputs.engaged("Bot"));
    }

    @Test
    void botsTrackIndependently() {
        HeldInputs inputs = new HeldInputs();
        inputs.engage("BotA", HeldInputs.Input.JUMP);

        assertTrue(inputs.engaged("BotB").isEmpty());
        assertEquals(Set.of(HeldInputs.Input.JUMP), inputs.engaged("BotA"));
    }
}
