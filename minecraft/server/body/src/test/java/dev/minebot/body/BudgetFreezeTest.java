package dev.minebot.body;

import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import dev.minebot.body.nav.NavigateExecutor;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;

/**
 * Locks the Java-side constants to the frozen budget fixture at
 * tests/fixtures/java_body_budgets.json. A red run here means the freeze is
 * no longer evidenced: no live budget claim can be trusted until fixture,
 * constants, and the design-doc table are re-aligned in one review packet.
 */
final class BudgetFreezeTest {
    private static JsonObject loadBudgets() throws IOException {
        Path dir = Path.of("").toAbsolutePath();
        while (dir != null && !Files.exists(dir.resolve("tests/fixtures/java_body_budgets.json"))) {
            dir = dir.getParent();
        }
        assertNotNull(dir, "repo root with tests/fixtures/java_body_budgets.json not found");
        String text = Files.readString(dir.resolve("tests/fixtures/java_body_budgets.json"));
        return JsonParser.parseString(text).getAsJsonObject().getAsJsonObject("budgets");
    }

    @Test
    void plannerAndTimeoutConstantsMatchTheFrozenFixture() throws IOException {
        JsonObject budgets = loadBudgets();

        assertEquals(budgets.get("planner_nodes_per_tick").getAsInt(), NavigateExecutor.NODES_PER_TICK);
        assertEquals(budgets.get("navigate_default_timeout_ticks").getAsInt(), NavigateExecutor.DEFAULT_TIMEOUT_TICKS);
    }

    @Test
    void frozenBudgetValuesAreExactlyTheContract() throws IOException {
        JsonObject budgets = loadBudgets();

        assertEquals(100.0, budgets.get("find_blocks_p95_ms_radius_32").getAsDouble());
        assertEquals(300.0, budgets.get("find_blocks_p95_ms_radius_128").getAsDouble());
        assertEquals(40.0, budgets.get("search_server_cost_ceiling_ms").getAsDouble());
        assertEquals(100, budgets.get("mutation_verdict_timeout_ticks").getAsInt());
        assertEquals(500.0, budgets.get("mutation_verdict_turnaround_p95_ms").getAsDouble());
        assertEquals(7, budgets.size(), "adding or removing a budget is a contract change");
    }
}
