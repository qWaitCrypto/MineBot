package dev.minebot.body.action;

import com.google.gson.JsonArray;
import com.google.gson.JsonNull;
import com.google.gson.JsonObject;
import dev.minebot.body.control.OwnerPriority;
import dev.minebot.body.nav.ApproachController;
import dev.minebot.body.nav.Goal;
import dev.minebot.body.nav.MovementControls;
import dev.minebot.body.nav.NavigateExecutor;
import dev.minebot.body.nav.WorldView;

import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * Tick-local survival owner for lava, fire, and low-air water hazards.
 *
 * <p>The controller is not a tool. It watches Java-owned FakePlayers, takes
 * SURVIVAL ownership when the server observes danger, neutrally preempts the
 * current action, and drives the shared Java navigation engine toward a
 * server-selected safe stand. An honest failure leaves an unresolved-hazard
 * latch so ordinary actions cannot resume as if the danger disappeared.</p>
 */
public final class SurvivalReflexController {
    public static final int MAX_REFLEX_TICKS = 100;
    public static final int RECOVERY_REPROBE_TICKS = 10;
    public static final int APPROACH_REPLAN_LIMIT = 5;

    public enum Kind {
        LAVA("lava", "lavaReflex"),
        FIRE("fire", "fireReflex"),
        WATER("water", "waterReflex");

        private final String wireName;
        private final String ownerName;

        Kind(String wireName, String ownerName) {
            this.wireName = wireName;
            this.ownerName = ownerName;
        }

        public String wireName() {
            return wireName;
        }

        public String ownerName() {
            return ownerName;
        }
    }

    public record Position(double x, double y, double z) {
        public int blockX() {
            return (int) Math.floor(x);
        }

        public int blockY() {
            double nearestInteger = Math.rint(y);
            if (Math.abs(y - nearestInteger) <= 1.0e-4) {
                return (int) nearestInteger;
            }
            return (int) Math.floor(y);
        }

        public int blockZ() {
            return (int) Math.floor(z);
        }
    }

    public record Target(Position position, boolean dryStand) {
    }

    /** Server facts and target selection, implemented without loading chunks. */
    public interface Environment {
        Position position(String botName);

        Kind detectHazard(String botName, Position position);

        boolean hazardPresent(String botName, Kind kind, Position position);

        Target findEscapeTarget(String botName, Kind kind, Position position, boolean dryOnly);

        boolean isDryStand(String botName, Position position);

        WorldView world(String botName);
    }

    @FunctionalInterface
    public interface EventSink {
        void emit(String bot, int tick, String name, String actionId, JsonObject data);
    }

    private static final class FailureLatch {
        private final Kind kind;
        private final Position origin;
        private final int tick;
        private Target recoveryTarget;
        private int lastProbeTick;

        private FailureLatch(Kind kind, Position origin, int tick, Target recoveryTarget) {
            this.kind = kind;
            this.origin = origin;
            this.tick = tick;
            this.recoveryTarget = recoveryTarget;
            this.lastProbeTick = tick;
        }
    }

    private record Active(String actionId, Kind kind, Position start, Target target) {
    }

    private final ActionRuntime runtime;
    private final MovementControls controls;
    private final Environment environment;
    private final EventSink events;
    private final Set<String> watched = new LinkedHashSet<>();
    private final Map<String, Active> active = new LinkedHashMap<>();
    private final Map<String, FailureLatch> failures = new LinkedHashMap<>();
    private long actionCounter;
    private int currentTick;

    public SurvivalReflexController(
        ActionRuntime runtime,
        MovementControls controls,
        Environment environment,
        EventSink events
    ) {
        this.runtime = runtime;
        this.controls = controls;
        this.environment = environment;
        this.events = events;
    }

    public void watch(String botName) {
        if (botName != null && !botName.isBlank()) {
            watched.add(botName);
        }
    }

    public void forget(String botName) {
        watched.remove(botName);
        active.remove(botName);
        failures.remove(botName);
    }

    public void unwatch(String botName) {
        watched.remove(botName);
        failures.remove(botName);
        Active reflex = active.get(botName);
        if (reflex != null) {
            runtime.requestCancel(reflex.actionId());
        }
    }

    /** Scan hazards before ordinary executors tick, so survival owns the frame. */
    public void tick(int serverTick) {
        currentTick = serverTick;
        for (String bot : List.copyOf(watched)) {
            Position position = environment.position(bot);
            if (position == null) {
                active.remove(bot);
                failures.remove(bot);
                continue;
            }
            clearResolvedFailure(bot, position);
            if (active.containsKey(bot)) {
                continue;
            }
            var owner = runtime.currentOwner(bot);
            if (owner != null && owner.priority() == OwnerPriority.SURVIVAL) {
                continue;
            }
            Kind kind = environment.detectHazard(bot, position);
            if (kind == null || failureStillBlocks(bot, kind, position)) {
                continue;
            }
            start(bot, kind, position, serverTick);
        }
    }

    public String activeOwnerName(String botName) {
        Active reflex = active.get(botName);
        return reflex == null ? null : reflex.kind().ownerName();
    }

    public JsonObject hazardUnresolved(String botName) {
        FailureLatch latch = failures.get(botName);
        Position position = environment.position(botName);
        if (latch == null || position == null) {
            return null;
        }
        if (!environment.hazardPresent(botName, latch.kind, position)) {
            failures.remove(botName);
            return null;
        }
        if (latch.recoveryTarget == null
            && currentTick - latch.lastProbeTick >= RECOVERY_REPROBE_TICKS) {
            latch.lastProbeTick = currentTick;
            latch.recoveryTarget = environment.findEscapeTarget(
                botName, latch.kind, position, true
            );
        }
        JsonObject value = new JsonObject();
        value.addProperty("kind", latch.kind.wireName());
        value.add("pos", positionJson(latch.origin));
        value.addProperty("tick", latch.tick);
        if (latch.recoveryTarget == null) {
            value.add("recovery_target", JsonNull.INSTANCE);
        } else {
            value.add("recovery_target", positionJson(latch.recoveryTarget.position()));
        }
        return value;
    }

    /** Ordinary actions stay blocked while a failed hazard is still present. */
    public boolean actionAllowed(String botName, boolean survivalRecovery) {
        return survivalRecovery || hazardUnresolved(botName) == null;
    }

    private void start(String bot, Kind kind, Position position, int serverTick) {
        String actionId = "survival-" + kind.wireName() + "-" + (++actionCounter);
        ActionRuntime.Submission submission = runtime.submit(
            bot, actionId, "SURVIVAL_REFLEX", OwnerPriority.SURVIVAL, serverTick
        );
        if (!(submission instanceof ActionRuntime.Submission.Accepted)) {
            return;
        }

        Target target = environment.findEscapeTarget(bot, kind, position, false);
        Active reflex = new Active(actionId, kind, position, target);
        active.put(bot, reflex);
        events.emit(bot, serverTick, "reflexTriggered", actionId, triggeredFacts(reflex));
        if (target == null) {
            complete(
                bot,
                reflex,
                target,
                ActionRuntime.CLASS_FAILED,
                "escape_target_unavailable",
                false,
                0,
                serverTick
            );
            return;
        }

        WorldView world = environment.world(bot);
        if (world == null) {
            complete(
                bot,
                reflex,
                target,
                ActionRuntime.CLASS_FAILED,
                "world_unavailable",
                false,
                0,
                serverTick
            );
            return;
        }
        runtime.attachExecutor(
            actionId,
            new ReflexExecutor(bot, reflex, world)
        );
    }

    private boolean failureStillBlocks(String bot, Kind kind, Position position) {
        FailureLatch latch = failures.get(bot);
        return latch != null
            && latch.kind == kind
            && environment.hazardPresent(bot, kind, position);
    }

    private void clearResolvedFailure(String bot, Position position) {
        FailureLatch latch = failures.get(bot);
        if (latch != null && !environment.hazardPresent(bot, latch.kind, position)) {
            failures.remove(bot);
        }
    }

    private void complete(
        String bot,
        Active reflex,
        Target completedTarget,
        String classification,
        String reason,
        boolean escaped,
        int elapsedTicks,
        int serverTick
    ) {
        Position finalPosition = environment.position(bot);
        boolean finalDry = finalPosition != null && environment.isDryStand(bot, finalPosition);
        JsonObject terminal = new JsonObject();
        terminal.addProperty("reason", reason);
        terminal.addProperty("success", escaped);
        terminal.addProperty("kind", reflex.kind().wireName());
        terminal.addProperty("escaped_hazard", escaped);
        terminal.addProperty("elapsed_ticks", elapsedTicks);
        terminal.add("final_pos", positionJson(finalPosition));
        terminal.add("target", targetJson(completedTarget));
        terminal.addProperty(
            "target_is_dry_stand",
            completedTarget != null && completedTarget.dryStand()
        );
        terminal.addProperty("final_is_dry_stand", finalDry);

        if (escaped) {
            failures.remove(bot);
        } else if (finalPosition != null
            && environment.hazardPresent(bot, reflex.kind(), finalPosition)) {
            Target recovery = environment.findEscapeTarget(
                bot, reflex.kind(), finalPosition, true
            );
            failures.put(bot, new FailureLatch(
                reflex.kind(), finalPosition, serverTick, recovery
            ));
        }
        active.remove(bot);
        runtime.finish(bot, reflex.actionId(), classification, terminal, serverTick);
        events.emit(bot, serverTick, "reflexCompleted", reflex.actionId(), terminal.deepCopy());
    }

    private JsonObject triggeredFacts(Active reflex) {
        JsonObject facts = new JsonObject();
        facts.addProperty("kind", reflex.kind().wireName());
        facts.addProperty("owner", reflex.kind().ownerName());
        facts.add("start", positionJson(reflex.start()));
        facts.add("target", targetJson(reflex.target()));
        facts.addProperty(
            "target_is_dry_stand",
            reflex.target() != null && reflex.target().dryStand()
        );
        return facts;
    }

    private static JsonArray positionJson(Position position) {
        JsonArray value = new JsonArray();
        if (position == null) {
            return value;
        }
        value.add(position.x());
        value.add(position.y());
        value.add(position.z());
        return value;
    }

    private static com.google.gson.JsonElement targetJson(Target target) {
        return target == null ? JsonNull.INSTANCE : positionJson(target.position());
    }

    private final class ReflexExecutor implements ActionRuntime.TickExecutor {
        private final String bot;
        private final Active reflex;
        private final WorldView world;
        private ApproachController approach;
        private Target target;
        private int elapsedTicks;
        private int retargets;

        private ReflexExecutor(String bot, Active reflex, WorldView world) {
            this.bot = bot;
            this.reflex = reflex;
            this.world = world;
            this.target = reflex.target();
            this.approach = approachFor(target);
        }

        @Override
        public void tick(int serverTick) {
            elapsedTicks++;
            if (runtime.cancelRequested(reflex.actionId())) {
                finish(serverTick, ActionRuntime.CLASS_CANCELED, "cancel_requested", false);
                return;
            }
            if (elapsedTicks > MAX_REFLEX_TICKS) {
                finish(serverTick, ActionRuntime.CLASS_TIMEOUT, "reflex_timeout", false);
                return;
            }
            Position position = environment.position(bot);
            if (position == null) {
                finish(serverTick, ActionRuntime.CLASS_FAILED, "body_missing", false);
                return;
            }
            if (escaped(position)) {
                finish(serverTick, ActionRuntime.CLASS_COMPLETED, "escaped", true);
                return;
            }

            ApproachController.Outcome outcome = approach.tick(
                serverTick, position.x(), position.y(), position.z()
            );
            if (outcome.status() == ApproachController.Status.FAILED) {
                finish(
                    serverTick,
                    ActionRuntime.CLASS_FAILED,
                    "escape_navigation_failed:" + outcome.reason(),
                    false
                );
                return;
            }
            if (outcome.status() == ApproachController.Status.COMPLETED
                && !escaped(position)) {
                Target next = environment.findEscapeTarget(
                    bot, reflex.kind(), position, true
                );
                if (retargets >= 2 || next == null || sameTarget(target, next)) {
                    finish(
                        serverTick,
                        ActionRuntime.CLASS_FAILED,
                        "escape_postcondition_failed",
                        false
                    );
                    return;
                }
                approach.halt();
                target = next;
                retargets++;
                approach = approachFor(target);
            }
        }

        private boolean escaped(Position position) {
            return !environment.hazardPresent(bot, reflex.kind(), position)
                && environment.isDryStand(bot, position);
        }

        private ApproachController approachFor(Target next) {
            Position position = next.position();
            return new ApproachController(
                bot,
                reflex.actionId(),
                new Goal.Near(
                    position.blockX(), position.blockY(), position.blockZ(), 0.5
                ),
                world,
                controls,
                events::emit,
                APPROACH_REPLAN_LIMIT,
                0.35
            );
        }

        private void finish(
            int serverTick,
            String classification,
            String reason,
            boolean escaped
        ) {
            if (approach != null) {
                approach.halt();
            }
            complete(
                bot,
                reflex,
                target,
                classification,
                reason,
                escaped,
                elapsedTicks,
                serverTick
            );
        }
    }

    private static boolean sameTarget(Target left, Target right) {
        if (left == null || right == null) {
            return left == right;
        }
        Position first = left.position();
        Position second = right.position();
        return first.blockX() == second.blockX()
            && first.blockY() == second.blockY()
            && first.blockZ() == second.blockZ();
    }
}
