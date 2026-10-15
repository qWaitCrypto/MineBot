package dev.minebot.client.body;

/** Pure checks for the asynchronous crafting result stream. */
final class CraftOutputProgress {
    private CraftOutputProgress() {}

    static boolean matches(String actualItem, int actualCount, String expectedItem, int expectedCount) {
        return actualItem != null
            && actualItem.equals(expectedItem)
            && actualCount == expectedCount;
    }
}
