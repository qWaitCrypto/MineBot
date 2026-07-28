package dev.minebot.body.action;

import com.google.gson.JsonArray;
import com.google.gson.JsonNull;
import com.google.gson.JsonObject;
import dev.minebot.body.nav.ApproachController;
import dev.minebot.body.nav.Goal;
import dev.minebot.body.nav.MovementControls;
import dev.minebot.body.nav.NavigateExecutor;
import dev.minebot.body.nav.WorldView;

/**
 * Runs one target-locked melee objective.
 *
 * <p>Target selection and health are server facts. The executor locks the
 * first resolved UUID, uses the shared Java navigation controller to reach
 * attack range, and sends public {@code /player attack once} commands only
 * on the requested server-tick cooldown. A command is never treated as
 * damage: the terminal is emitted only after the target health/death fact is
 * observed or a typed failure is reached.</p>
 */
public final class EngageExecutor implements ActionRuntime.TickExecutor {
    public static final int APPROACH_REPLAN_LIMIT = 5;
    public static final double TARGET_REPLAN_DISTANCE = 2.0;
    private static final double HEALTH_EPSILON = 1.0e-4;

    /** One stable server-side target snapshot. */
    public record Target(
        String id,
        String type,
        String name,
        double x,
        double y,
        double z,
        Double health,
        boolean alive
    ) {
        public int blockX() {
            return (int) Math.floor(x);
        }

        public int blockY() {
            return (int) Math.floor(y);
        }

        public int blockZ() {
            return (int) Math.floor(z);
        }

        public boolean isPlayer() {
            return "minecraft:player".equals(type) || "player".equals(type);
        }
    }

    /** A lookup returns a typed reason when no target can be resolved. */
    public record Lookup(Target target, String reason) {
        public static Lookup found(Target target) {
            return new Lookup(target, null);
        }

        public static Lookup missing(String reason) {
            return new Lookup(null, reason);
        }

        public String reasonOr(String fallback) {
            return reason == null || reason.isBlank() ? fallback : reason;
        }
    }

    /** Server-authoritative target and body facts. */
    public interface TargetSource {
        Lookup acquire(String botName, String targetSpec, double radius);

        Lookup refresh(String botName, String targetId, double radius);

        Double bodyHealth(String botName);

        boolean hasLineOfSight(String botName, Target target);
    }

    /** The public physical controls needed by combat. */
    public interface CombatControls {
        void lookAt(String botName, double x, double y, double z);

        void attackOnce(String botName);
    }

    private final String bot;
    private final String actionId;
    private final String targetSpec;
    private final double attackRange;
    private final int cooldownTicks;
    private final int acquireRadius;
    private final double disengageHealth;
    private final WorldView world;
    private final MovementControls movement;
    private final CombatControls combat;
    private final TargetSource targets;
    private final NavigateExecutor.PositionSource positions;
    private final NavigateExecutor.EventSink events;
    private final ActionRuntime runtime;
    private final int timeoutTicks;

    private Target lockedTarget;
    private Target plannedTarget;
    private ApproachController approach;
    private int elapsedTicks;
    private int attacks;
    private boolean damageObserved;
    private boolean targetGoneAfterAttack;
    private Double initialHealth;
    private Double lastHealth;
    private int lastAttackTick = Integer.MIN_VALUE;
    private Integer minAttackInterval;
    private Integer maxAttackInterval;

    public EngageExecutor(
        String bot,
        String actionId,
        String targetSpec,
        double attackRange,
        int cooldownTicks,
        int acquireRadius,
        double disengageHealth,
        WorldView world,
        MovementControls movement,
        CombatControls combat,
        TargetSource targets,
        NavigateExecutor.PositionSource positions,
        NavigateExecutor.EventSink events,
        ActionRuntime runtime,
        int timeoutTicks
    ) {
        if (targetSpec == null || targetSpec.isBlank()) {
            throw new IllegalArgumentException("target_spec must not be blank");
        }
        if (attackRange < 1.2 || attackRange > 3.0) {
            throw new IllegalArgumentException("attack_range is out of bounds");
        }
        if (cooldownTicks < 1) {
            throw new IllegalArgumentException("cooldown_ticks must be positive");
        }
        if (acquireRadius < 1) {
            throw new IllegalArgumentException("acquire_radius must be positive");
        }
        if (disengageHealth < 0.0) {
            throw new IllegalArgumentException("disengage_health must not be negative");
        }
        this.bot = bot;
        this.actionId = actionId;
        this.targetSpec = targetSpec;
        this.attackRange = attackRange;
        this.cooldownTicks = cooldownTicks;
        this.acquireRadius = acquireRadius;
        this.disengageHealth = disengageHealth;
        this.world = world;
        this.movement = movement;
        this.combat = combat;
        this.targets = targets;
        this.positions = positions;
        this.events = events;
        this.runtime = runtime;
        this.timeoutTicks = timeoutTicks;
    }

    @Override
    public void tick(int serverTick) {
        elapsedTicks++;
        if (runtime.cancelRequested(actionId)) {
            finish(serverTick, ActionRuntime.CLASS_CANCELED, "cancel_requested");
            return;
        }
        if (elapsedTicks > timeoutTicks) {
            finish(serverTick, ActionRuntime.CLASS_TIMEOUT, "timeout");
            return;
        }

        NavigateExecutor.PositionSource.Position position = positions.position(bot);
        if (position == null) {
            finish(serverTick, ActionRuntime.CLASS_FAILED, "body_missing");
            return;
        }

        if (lockedTarget == null) {
            Lookup lookup = targets.acquire(bot, targetSpec, acquireRadius);
            if (lookup.target() == null) {
                finish(serverTick, ActionRuntime.CLASS_FAILED, lookup.reasonOr("target_not_found"));
                return;
            }
            lockedTarget = lookup.target();
            initialHealth = lockedTarget.health();
            lastHealth = lockedTarget.health();
            JsonObject data = new JsonObject();
            data.addProperty("target_id", lockedTarget.id());
            data.addProperty("target_type", lockedTarget.type());
            data.addProperty("target_name", lockedTarget.name());
            events.emit(bot, serverTick, "engage_target_acquired", actionId, data);
        } else {
            Lookup lookup = targets.refresh(bot, lockedTarget.id(), acquireRadius);
            if (lookup.target() == null) {
                String reason = missingTargetReason();
                if ("killed".equals(reason)) {
                    targetGoneAfterAttack = true;
                    damageObserved = true;
                }
                finish(serverTick, "killed".equals(reason)
                    ? ActionRuntime.CLASS_COMPLETED : ActionRuntime.CLASS_FAILED, reason);
                return;
            }
            lockedTarget = lookup.target();
            observeHealth(lockedTarget.health());
        }

        if (!lockedTarget.alive() || healthAtOrBelowZero(lockedTarget.health())) {
            finish(serverTick, ActionRuntime.CLASS_COMPLETED, "killed");
            return;
        }

        Double bodyHealth = targets.bodyHealth(bot);
        if (bodyHealth != null && disengageHealth > 0.0 && bodyHealth <= disengageHealth) {
            finish(serverTick, ActionRuntime.CLASS_FAILED, "disengaged_low_health");
            return;
        }

        double distance = distance(position, lockedTarget);
        boolean lineOfSight = targets.hasLineOfSight(bot, lockedTarget);
        if (distance > attackRange || !lineOfSight) {
            approachTick(serverTick, position, lineOfSight);
            return;
        }

        if (approach != null) {
            approach.halt();
            approach = null;
        }
        combat.lookAt(bot, lockedTarget.x(), lockedTarget.y() + 0.8, lockedTarget.z());
        if (lastAttackTick == Integer.MIN_VALUE || elapsedTicks - lastAttackTick >= cooldownTicks) {
            combat.attackOnce(bot);
            attacks++;
            if (lastAttackTick != Integer.MIN_VALUE) {
                int interval = elapsedTicks - lastAttackTick;
                minAttackInterval = minAttackInterval == null
                    ? interval : Math.min(minAttackInterval, interval);
                maxAttackInterval = maxAttackInterval == null
                    ? interval : Math.max(maxAttackInterval, interval);
            }
            lastAttackTick = elapsedTicks;
        }
    }

    private void approachTick(
        int serverTick,
        NavigateExecutor.PositionSource.Position position,
        boolean lineOfSight
    ) {
        if (approach == null || plannedTarget == null || movedEnough(plannedTarget, lockedTarget)) {
            if (approach != null) {
                approach.halt();
            }
            double goalRange = lineOfSight ? attackRange : Math.max(0.5, attackRange - 0.75);
            approach = new ApproachController(
                bot,
                actionId,
                new Goal.Near(
                    lockedTarget.blockX(),
                    lockedTarget.blockY(),
                    lockedTarget.blockZ(),
                    goalRange
                ),
                world,
                movement,
                events,
                APPROACH_REPLAN_LIMIT,
                0.35
            );
            plannedTarget = lockedTarget;
        }
        ApproachController.Outcome outcome = approach.tick(
            serverTick,
            position.x(),
            position.y(),
            position.z()
        );
        if (outcome.status() == ApproachController.Status.FAILED) {
            finish(serverTick, ActionRuntime.CLASS_FAILED, outcome.reason());
        } else if (outcome.status() == ApproachController.Status.COMPLETED) {
            approach.halt();
            approach = null;
        }
    }

    private void observeHealth(Double current) {
        if (current != null && lastHealth != null && current < lastHealth - HEALTH_EPSILON) {
            damageObserved = true;
        }
        if (current != null) {
            lastHealth = current;
        }
    }

    private String missingTargetReason() {
        if (attacks == 0) {
            return "target_lost";
        }
        return lockedTarget != null && lockedTarget.isPlayer() ? "target_gone" : "killed";
    }

    private static boolean movedEnough(Target first, Target second) {
        double dx = first.x() - second.x();
        double dy = first.y() - second.y();
        double dz = first.z() - second.z();
        return dx * dx + dy * dy + dz * dz >= TARGET_REPLAN_DISTANCE * TARGET_REPLAN_DISTANCE;
    }

    private static double distance(
        NavigateExecutor.PositionSource.Position position,
        Target target
    ) {
        double dx = position.x() - target.x();
        double dy = position.y() - target.y();
        double dz = position.z() - target.z();
        return Math.sqrt(dx * dx + dy * dy + dz * dz);
    }

    private static boolean healthAtOrBelowZero(Double health) {
        return health != null && health <= 0.0;
    }

    private void finish(int serverTick, String classification, String reason) {
        int approachReplans = approach == null ? 0 : approach.replans();
        int approachExpanded = approach == null ? 0 : approach.expandedNodes();
        int approachUnloaded = approach == null ? 0 : approach.unloadedTouches();
        if (approach != null) {
            approach.halt();
            approach = null;
        }
        JsonObject facts = new JsonObject();
        facts.addProperty("success", ActionRuntime.CLASS_COMPLETED.equals(classification));
        facts.addProperty("target_spec", targetSpec);
        if (lockedTarget == null) {
            facts.add("target_id", JsonNull.INSTANCE);
            facts.add("target_type", JsonNull.INSTANCE);
            facts.add("target_name", JsonNull.INSTANCE);
            facts.add("target_pos", JsonNull.INSTANCE);
            facts.add("target_health", JsonNull.INSTANCE);
        } else {
            facts.addProperty("target_id", lockedTarget.id());
            facts.addProperty("target_type", lockedTarget.type());
            facts.addProperty("target_name", lockedTarget.name());
            JsonArray targetPos = new JsonArray();
            targetPos.add(lockedTarget.x());
            targetPos.add(lockedTarget.y());
            targetPos.add(lockedTarget.z());
            facts.add("target_pos", targetPos);
            if (targetGoneAfterAttack) {
                facts.addProperty("target_health", 0.0);
            } else if (lockedTarget.health() == null) {
                facts.add("target_health", JsonNull.INSTANCE);
            } else {
                facts.addProperty("target_health", lockedTarget.health());
            }
        }
        if (initialHealth == null) {
            facts.add("target_initial_health", JsonNull.INSTANCE);
        } else {
            facts.addProperty("target_initial_health", initialHealth);
        }
        facts.addProperty("damage_observed", damageObserved);
        facts.addProperty("persistent_target", lockedTarget != null);
        facts.addProperty("attacks", attacks);
        facts.addProperty("cooldown_ticks", cooldownTicks);
        if (minAttackInterval == null) {
            facts.add("min_attack_interval_ticks", JsonNull.INSTANCE);
        } else {
            facts.addProperty("min_attack_interval_ticks", minAttackInterval);
        }
        if (maxAttackInterval == null) {
            facts.add("max_attack_interval_ticks", JsonNull.INSTANCE);
        } else {
            facts.addProperty("max_attack_interval_ticks", maxAttackInterval);
        }
        facts.addProperty("reason", reason);
        facts.addProperty("elapsed_ticks", elapsedTicks);
        facts.addProperty("replans", approachReplans);
        facts.addProperty("expanded_nodes", approachExpanded);
        facts.addProperty("unloaded_touches", approachUnloaded);
        NavigateExecutor.PositionSource.Position finalPosition = positions.position(bot);
        if (finalPosition != null) {
            facts.addProperty("final_x", finalPosition.x());
            facts.addProperty("final_y", finalPosition.y());
            facts.addProperty("final_z", finalPosition.z());
        }
        runtime.finish(bot, actionId, classification, facts, serverTick);
    }
}
