package dev.minebot.body.control;

import net.minecraft.server.MinecraftServer;

/**
 * The public Carpet {@code /player} surface for FakePlayer movement, posture,
 * look, and use commands. Exact governed block breaking is handled separately
 * by {@code ServerPlayerBlockBreaker} through the real player's game-mode
 * mechanics. No Carpet internal class is a control surface. Dispatch is
 * fire-and-observe: a dispatched command is never terminal success; truth
 * comes from observed server state.
 *
 * The adapter records engaged continuous inputs in {@link HeldInputs} so that
 * {@link #clearAll(String)} is a total cleanup: it always issues {@code stop}
 * and additionally releases sneak/sprint toggles when they were engaged.
 */
public final class PlayerCommandAdapter
    implements dev.minebot.body.action.BotControls,
    dev.minebot.body.nav.MovementControls {
    private final MinecraftServer server;
    private final HeldInputs heldInputs;
    private final dev.minebot.body.action.ExactBlockBreaker blockBreaker;

    public PlayerCommandAdapter(
        MinecraftServer server,
        HeldInputs heldInputs,
        dev.minebot.body.action.ExactBlockBreaker blockBreaker
    ) {
        this.server = server;
        this.heldInputs = heldInputs;
        this.blockBreaker = blockBreaker;
    }

    public void moveForward(String botName) {
        heldInputs.engage(botName, HeldInputs.Input.MOVEMENT);
        dispatch(botName, "move forward");
    }

    public void stopMovement(String botName) {
        heldInputs.disengage(botName, HeldInputs.Input.MOVEMENT);
        dispatch(botName, "stop");
    }

    public void lookAt(String botName, double x, double y, double z) {
        dispatch(botName, "look at " + x + " " + y + " " + z);
    }

    public void look(String botName, float yaw, float pitch) {
        dispatch(botName, "look " + yaw + " " + pitch);
    }

    public void jumpOnce(String botName) {
        dispatch(botName, "jump once");
    }

    public void jumpContinuous(String botName) {
        heldInputs.engage(botName, HeldInputs.Input.JUMP);
        dispatch(botName, "jump continuous");
    }

    public void sneak(String botName) {
        heldInputs.engage(botName, HeldInputs.Input.SNEAK);
        dispatch(botName, "sneak");
    }

    public void unsneak(String botName) {
        heldInputs.disengage(botName, HeldInputs.Input.SNEAK);
        dispatch(botName, "unsneak");
    }

    public void sprint(String botName) {
        heldInputs.engage(botName, HeldInputs.Input.SPRINT);
        dispatch(botName, "sprint");
    }

    public void unsprint(String botName) {
        heldInputs.disengage(botName, HeldInputs.Input.SPRINT);
        dispatch(botName, "unsprint");
    }

    public void useOnce(String botName) {
        dispatch(botName, "use once");
    }

    /**
     * Total cleanup of every engaged input. Always issues {@code stop} (which
     * ceases movement and repeating actions) and releases sneak/sprint
     * toggles that were engaged. Must run before an action's terminal result
     * is emitted.
     */
    @Override
    public void clearAll(String botName) {
        blockBreaker.abort(botName);
        var engaged = heldInputs.drain(botName);
        dispatch(botName, "stop");
        if (engaged.contains(HeldInputs.Input.SNEAK)) {
            dispatch(botName, "unsneak");
        }
        if (engaged.contains(HeldInputs.Input.SPRINT)) {
            dispatch(botName, "unsprint");
        }
    }

    private void dispatch(String botName, String subcommand) {
        server.getCommands().performPrefixedCommand(
            server.createCommandSourceStack().withSuppressedOutput(),
            "player " + botName + " " + subcommand
        );
    }
}
