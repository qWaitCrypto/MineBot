package dev.minebot.body.perception;

import com.google.gson.JsonArray;
import com.google.gson.JsonNull;
import com.google.gson.JsonObject;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.server.level.ServerPlayer;

import java.util.ArrayList;
import java.util.List;

/** Maps the neutral Body world-read scopes to direct loaded-world facts. */
public final class WorldReadService {
    public static final int MAX_CELLS = 256;
    public static final int MAX_COLUMNS = 64;

    private final ServerPlayer player;
    private final MinecraftBlockReader reader;

    public WorldReadService(ServerPlayer player, ServerLevel level) {
        this.player = player;
        this.reader = new MinecraftBlockReader(level);
    }

    public Result read(String scope, JsonObject params) {
        return switch (scope) {
            case "blockAt" -> blockAt(params);
            case "blockCells" -> blockCells(params);
            case "surfaceColumns" -> surfaceColumns(params);
            case "nearbyBlocks" -> cube(params, false, 8, 128, false);
            case "debugBlocks" -> cube(params, true, 4, 64, true);
            default -> throw new IllegalArgumentException("unsupported world-read scope: " + scope);
        };
    }

    private Result blockAt(JsonObject params) {
        BlockSnapshot.Fact fact = reader.read(
            floorInt(params, "x"),
            floorInt(params, "y"),
            floorInt(params, "z")
        );
        return Result.complete(factJson(fact));
    }

    private Result blockCells(JsonObject params) {
        JsonArray requested = requiredArray(params, "cells");
        if (requested.size() > MAX_CELLS) {
            throw new IllegalArgumentException("cells exceeds 256 entries");
        }
        List<BlockSnapshot.Position> cells = new ArrayList<>();
        for (var element : requested) {
            if (!element.isJsonArray() || element.getAsJsonArray().size() != 3) {
                throw new IllegalArgumentException("cells entries must be [x,y,z]");
            }
            JsonArray cell = element.getAsJsonArray();
            cells.add(new BlockSnapshot.Position(
                floorNumber(cell.get(0), "cells.x"),
                floorNumber(cell.get(1), "cells.y"),
                floorNumber(cell.get(2), "cells.z")
            ));
        }
        int start = clamp(optionalFloorInt(params, "start", 0), 0, cells.size());
        int limit = clamp(optionalFloorInt(params, "limit", 64), 1, MAX_CELLS);
        int end = Math.min(cells.size(), start + limit);
        JsonArray facts = new JsonArray();
        for (int index = start; index < end; index++) {
            facts.add(factJson(reader.read(cells.get(index))));
        }
        JsonObject data = pageData(start, limit, end >= cells.size() ? null : end, cells.size());
        data.addProperty("count", facts.size());
        data.add("cells", facts);
        return paged(data, end >= cells.size(), end);
    }

    private Result surfaceColumns(JsonObject params) {
        JsonArray requested = requiredArray(params, "columns");
        if (requested.size() > MAX_COLUMNS) {
            throw new IllegalArgumentException("columns exceeds 64 entries");
        }
        List<int[]> columns = new ArrayList<>();
        for (var element : requested) {
            if (!element.isJsonArray() || element.getAsJsonArray().size() != 2) {
                throw new IllegalArgumentException("columns entries must be [x,z]");
            }
            JsonArray column = element.getAsJsonArray();
            columns.add(new int[] {
                floorNumber(column.get(0), "columns.x"),
                floorNumber(column.get(1), "columns.z")
            });
        }
        int start = clamp(optionalFloorInt(params, "start", 0), 0, columns.size());
        int limit = clamp(optionalFloorInt(params, "limit", 64), 1, MAX_COLUMNS);
        int end = Math.min(columns.size(), start + limit);
        JsonArray facts = new JsonArray();
        for (int index = start; index < end; index++) {
            int[] column = columns.get(index);
            facts.add(surfaceJson(reader.readSurfaceColumn(column[0], column[1])));
        }
        JsonObject data = pageData(start, limit, end >= columns.size() ? null : end, columns.size());
        data.addProperty("count", facts.size());
        data.add("columns", facts);
        return paged(data, end >= columns.size(), end);
    }

    private Result cube(
        JsonObject params,
        boolean includeClear,
        int maxRadius,
        int defaultLimit,
        boolean debug
    ) {
        int radius = clamp(optionalFloorInt(params, "radius", 0), 0, maxRadius);
        int limit = clamp(optionalFloorInt(params, "limit", defaultLimit), 1, MAX_CELLS);
        int start = optionalFloorInt(params, "start", 0);
        BlockSnapshot.Position center = new BlockSnapshot.Position(
            player.blockPosition().getX(),
            player.blockPosition().getY(),
            player.blockPosition().getZ()
        );
        BlockSnapshot.CubePage page = BlockSnapshot.scanCube(
            center, radius, start, limit, includeClear, reader::read
        );
        JsonObject data = pageData(page.start(), page.limit(), page.nextStart(), page.total());
        JsonArray centerJson = new JsonArray();
        centerJson.add(player.getX());
        centerJson.add(player.getY());
        centerJson.add(player.getZ());
        data.add("center", centerJson);
        data.addProperty("radius", radius);
        data.addProperty("count", page.facts().size());
        JsonArray blocks = new JsonArray();
        page.facts().forEach(fact -> blocks.add(factJson(fact)));
        data.add("blocks", blocks);
        if (debug) {
            data.add("cursor", factJson(reader.read(center)));
            data.add("feet", factJson(reader.read(center.x(), center.y() - 1, center.z())));
            data.add("head", factJson(reader.read(center.x(), center.y() + 1, center.z())));
        }
        return paged(data, page.nextStart() == null, page.nextStart());
    }

    private static Result paged(JsonObject data, boolean complete, Integer nextStart) {
        JsonArray uncertainty = new JsonArray();
        if (!complete) {
            JsonObject reason = new JsonObject();
            reason.addProperty("reason", "page_limit");
            uncertainty.add(reason);
        }
        return new Result(complete, data, uncertainty, complete ? null : String.valueOf(nextStart));
    }

    private static JsonObject pageData(int start, int limit, Integer nextStart, int total) {
        JsonObject data = new JsonObject();
        data.addProperty("start", start);
        data.addProperty("limit", limit);
        if (nextStart == null) {
            data.add("nextStart", JsonNull.INSTANCE);
        } else {
            data.addProperty("nextStart", nextStart);
        }
        data.addProperty("total", total);
        return data;
    }

    public static JsonObject factJson(BlockSnapshot.Fact fact) {
        JsonObject json = new JsonObject();
        json.addProperty("x", fact.x());
        json.addProperty("y", fact.y());
        json.addProperty("z", fact.z());
        json.addProperty("type", fact.type());
        json.addProperty("state", fact.state());
        JsonObject properties = new JsonObject();
        fact.properties().forEach(properties::addProperty);
        json.add("properties", properties);
        return json;
    }

    private static JsonObject surfaceJson(BlockSnapshot.SurfaceColumn column) {
        JsonObject json = new JsonObject();
        json.addProperty("x", column.x());
        json.addProperty("z", column.z());
        json.addProperty("feetY", column.feetY());
        json.addProperty("feetType", column.feet().type());
        json.addProperty("feetState", column.feet().state());
        json.addProperty("headType", column.head().type());
        json.addProperty("headState", column.head().state());
        json.addProperty("supportType", column.support().type());
        json.addProperty("supportState", column.support().state());
        return json;
    }

    private static JsonArray requiredArray(JsonObject params, String name) {
        if (!params.has(name) || !params.get(name).isJsonArray()) {
            throw new IllegalArgumentException(name + " must be an array");
        }
        return params.getAsJsonArray(name);
    }

    private static int floorInt(JsonObject params, String name) {
        if (!params.has(name)) {
            throw new IllegalArgumentException(name + " is required");
        }
        return floorNumber(params.get(name), name);
    }

    private static int optionalFloorInt(JsonObject params, String name, int fallback) {
        return params.has(name) ? floorNumber(params.get(name), name) : fallback;
    }

    private static int floorNumber(com.google.gson.JsonElement element, String name) {
        try {
            double value = element.getAsDouble();
            if (!Double.isFinite(value) || value < Integer.MIN_VALUE || value > Integer.MAX_VALUE) {
                throw new IllegalArgumentException(name + " is out of bounds");
            }
            return (int) Math.floor(value);
        } catch (NumberFormatException | UnsupportedOperationException error) {
            throw new IllegalArgumentException(name + " must be a number");
        }
    }

    private static int clamp(int value, int min, int max) {
        return Math.max(min, Math.min(max, value));
    }

    public record Result(boolean complete, JsonObject data, JsonArray uncertainty, String next) {
        static Result complete(JsonObject data) {
            return new Result(true, data, new JsonArray(), null);
        }
    }
}
