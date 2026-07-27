package dev.minebot.body.perception;

import com.google.gson.JsonArray;
import com.google.gson.JsonNull;
import com.google.gson.JsonObject;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.entity.LivingEntity;
import net.minecraft.world.entity.monster.Enemy;

import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

/** Direct loaded-world entity facts for neutral nearby-entity perception scopes. */
public final class EntityReadService {
    public static final int MAX_RADIUS = 32;
    public static final int MAX_LIMIT = 128;
    public static final int MAX_TYPES = 64;
    private static final String CAMERA_OBSERVER_TAG = "minebot.camera.observer";

    private final ServerPlayer player;
    private final ServerLevel level;

    public EntityReadService(ServerPlayer player, ServerLevel level) {
        this.player = player;
        this.level = level;
    }

    public Result read(String scope, JsonObject params) {
        if (!Set.of("nearbyEntities", "nearbyHostiles").contains(scope)) {
            throw new IllegalArgumentException("unsupported entity-read scope: " + scope);
        }
        boolean hostilesOnly = "nearbyHostiles".equals(scope);
        int radius = clamp(optionalInt(params, "radius", 1), 1, MAX_RADIUS);
        int limit = clamp(optionalInt(params, "limit", 32), 1, MAX_LIMIT);
        Set<String> wantedTypes = entityTypes(params);
        String wantedName = optionalString(params, "name", 256);
        double radiusSquared = (double) radius * radius;

        List<EntitySnapshot.Fact> candidates = new ArrayList<>();
        for (Entity entity : level.getEntities(
            player,
            player.getBoundingBox().inflate(radius),
            candidate -> !candidate.isRemoved()
                && (!(candidate instanceof LivingEntity living) || living.isAlive())
                && !candidate.entityTags().contains(CAMERA_OBSERVER_TAG)
        )) {
            double distanceSquared = player.distanceToSqr(entity);
            if (distanceSquared > radiusSquared) {
                continue;
            }
            if (hostilesOnly && !(entity instanceof Enemy)) {
                continue;
            }
            String type = BuiltInRegistries.ENTITY_TYPE.getKey(entity.getType()).toString();
            Double health = entity instanceof LivingEntity living ? (double) living.getHealth() : null;
            candidates.add(new EntitySnapshot.Fact(
                entity.getUUID().toString(),
                type,
                entity.getName().getString(),
                entity.getX(),
                entity.getY(),
                entity.getZ(),
                health,
                distanceSquared
            ));
        }

        EntitySnapshot.Page page = EntitySnapshot.select(candidates, wantedTypes, wantedName, limit);
        JsonObject data = new JsonObject();
        JsonArray center = new JsonArray();
        center.add(player.getX());
        center.add(player.getY());
        center.add(player.getZ());
        data.add("center", center);
        data.addProperty("radius", radius);
        data.addProperty("limit", limit);
        data.addProperty("count", page.entities().size());
        data.addProperty("totalMatches", page.totalMatches());
        JsonArray entities = new JsonArray();
        page.entities().forEach(fact -> entities.add(factJson(fact)));
        data.add("entities", entities);

        JsonArray uncertainty = new JsonArray();
        if (!page.complete()) {
            JsonObject reason = new JsonObject();
            reason.addProperty("reason", "limit_exceeded");
            uncertainty.add(reason);
        }
        return new Result(page.complete(), data, uncertainty, page.complete() ? null : "limit");
    }

    private static JsonObject factJson(EntitySnapshot.Fact fact) {
        JsonObject json = new JsonObject();
        json.addProperty("id", fact.id());
        json.addProperty("type", fact.type());
        json.addProperty("name", fact.name());
        JsonArray pos = new JsonArray();
        pos.add(fact.x());
        pos.add(fact.y());
        pos.add(fact.z());
        json.add("pos", pos);
        if (fact.health() == null) {
            json.add("health", JsonNull.INSTANCE);
        } else {
            json.addProperty("health", fact.health());
        }
        json.addProperty("dist2", fact.distanceSquared());
        return json;
    }

    private static Set<String> entityTypes(JsonObject params) {
        Set<String> types = new LinkedHashSet<>();
        if (!params.has("types") || params.get("types").isJsonNull()) {
            return types;
        }
        if (!params.get("types").isJsonArray()) {
            throw new IllegalArgumentException("types must be an array");
        }
        JsonArray raw = params.getAsJsonArray("types");
        if (raw.size() > MAX_TYPES) {
            throw new IllegalArgumentException("types exceeds 64 entries");
        }
        raw.forEach(element -> {
            if (!element.isJsonPrimitive() || !element.getAsJsonPrimitive().isString()) {
                throw new IllegalArgumentException("types must contain strings");
            }
            String type = element.getAsString().trim();
            if (!type.isEmpty()) {
                types.add(type.contains(":") ? type : "minecraft:" + type);
            }
        });
        return types;
    }

    private static int optionalInt(JsonObject params, String name, int fallback) {
        if (!params.has(name) || params.get(name).isJsonNull()) {
            return fallback;
        }
        try {
            double value = params.get(name).getAsDouble();
            if (!Double.isFinite(value) || value < Integer.MIN_VALUE || value > Integer.MAX_VALUE) {
                throw new IllegalArgumentException(name + " is out of bounds");
            }
            return (int) Math.floor(value);
        } catch (NumberFormatException | UnsupportedOperationException error) {
            throw new IllegalArgumentException(name + " must be a number");
        }
    }

    private static String optionalString(JsonObject params, String name, int maxLength) {
        if (!params.has(name) || params.get(name).isJsonNull()) {
            return null;
        }
        if (!params.get(name).isJsonPrimitive() || !params.getAsJsonPrimitive(name).isString()) {
            throw new IllegalArgumentException(name + " must be a string");
        }
        String value = params.get(name).getAsString();
        if (value.length() > maxLength) {
            throw new IllegalArgumentException(name + " is too long");
        }
        return value;
    }

    private static int clamp(int value, int min, int max) {
        return Math.max(min, Math.min(max, value));
    }

    public record Result(boolean complete, JsonObject data, JsonArray uncertainty, String next) {}
}
