package dev.minebot.client.body;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class CraftOutputProgressTest {
    @Test
    void waitsPastAnIntermediateMenuResultAndAcceptsTheRequestedOutput() {
        assertFalse(CraftOutputProgress.matches("oak_planks", 4, "crafting_table", 1));
        assertTrue(CraftOutputProgress.matches("crafting_table", 1, "crafting_table", 1));
    }

    @Test
    void rejectsWrongCountEvenWhenTheItemMatches() {
        assertFalse(CraftOutputProgress.matches("crafting_table", 2, "crafting_table", 1));
    }
}
