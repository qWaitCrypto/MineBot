package dev.minebot.body.action;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

final class TerminalEvidenceContractTest {
    private static JsonObject loadFixture() throws IOException {
        Path dir = Path.of("").toAbsolutePath();
        String relative = "tests/protocol_fixtures/terminal_evidence_contract.json";
        while (dir != null && !Files.exists(dir.resolve(relative))) {
            dir = dir.getParent();
        }
        assertNotNull(dir, "repo root with terminal evidence fixture not found");
        return JsonParser.parseString(Files.readString(dir.resolve(relative))).getAsJsonObject();
    }

    @Test
    void sharedFixtureCarriesTheBoundedDecisionGradeCore() throws IOException {
        JsonObject fixture = loadFixture();
        assertTrue(fixture.get("source_evidence_bytes").getAsInt() > ActionRuntime.MAX_TERMINAL_BYTES);
        JsonArray steps = fixture.getAsJsonArray("steps");
        JsonObject terminal = null;
        for (var step : steps) {
            JsonObject row = step.getAsJsonObject();
            if (row.has("recv") && row.getAsJsonObject("recv").has("terminal")) {
                terminal = row.getAsJsonObject("recv").getAsJsonObject("terminal");
                break;
            }
        }
        assertNotNull(terminal);
        assertTrue(ActionRuntime.serializedBytes(terminal) <= ActionRuntime.MAX_TERMINAL_BYTES);
        assertEquals("blocked", terminal.get("reason_class").getAsString());
        assertTrue(terminal.get("goal").isJsonObject());
        assertTrue(terminal.has("attempts_consumed"));
        assertTrue(terminal.has("budget_remaining"));
        assertTrue(terminal.has("closest_approach_distance"));
        assertTrue(terminal.has("unloaded_chunk_count"));
        assertTrue(terminal.has("elapsed_ticks"));
        assertTrue(terminal.get("evidence_truncated").getAsBoolean());
    }
}
