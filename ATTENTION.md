# Client attention

Status: implemented in the real client. Directed results are recorded below.

The user should see purposeful movement and attention without losing accurate
digging, placement or combat. Attention belongs to the action already running;
nearby entities do not automatically become targets.

| Situation | Rotation owner and behavior |
| --- | --- |
| Travel/exploration | Baritone's intended path heading, rather than measured velocity; turn into a corner without reacting to a bump or fall. |
| Mining/building | Baritone's block interaction rotation; look at the block being worked on, including upward tree work. |
| Melee/bow/crossbow | Existing combat aim owns yaw and pitch while striking, charging or shooting. Movement can strafe or retreat without replacing that aim. During distant approach, path rotation remains available. |
| Follow | Face the path while catching up. Once within the requested distance and no longer pathing, look toward the resolved target. Keep that identity until the action ends or the target is lost. |
| Handoff/menu interaction | Face the recipient or block. The brief downward drop uses physical throw aim; the sender then leaves the overlapping pickup area using native pathing and looks back at the recipient. Approach uses path rotation. |
| Explicit look/idle | Honor the explicit look. After it finishes, keep the current view until a new action needs it; no compulsory scanning or random target switching. |
| Elytra | Flight keeps its existing yaw/pitch control; ground attention does not alter flight. |

Ground actions temporarily set `freeLook=false`,
`remainWithExistingLookDirection=false`, `blockFreeLook=false` and
`smoothLook=false`. Snapshot and restore all four on completion, failure,
cancellation and server disconnect. Elytra leaves these settings alone.

`AttentionMixin` observes upstream `LookBehavior.updateTarget` and the native
PRE/POST player update. After Baritone applies movement and packet rotation,
only the visible travel yaw is limited to 24 degrees per tick using wrapped
angle differences. Block interaction targets bypass this cosmetic step.
The built-in raw-yaw averaging stays disabled during ground actions, so the
+/-180 transition takes the short turn. Native movement and aiming targets
are unchanged; there is no velocity-based rotation controller.

Near-target following uses a wrapped yaw/pitch turn limit while pathing is
idle. Navigation and attention share the same resolved entity. Nearby decoys
do not replace it. Ranged attacks stop their approach process before charging
or shooting; projectile and melee aim remains precise. Menu opens and native
block/entity interactions face their actual targets. Explicit look is honored,
and idle has no automatic scanning. Ordinary chat does not issue a look action.

The directed `attention` case checks one visible sequence: +/-180 travel,
forced corner, high block, named moving follow target past a decoy, menu,
explicit look, then moving cancellation. It records rotations and restored
settings alongside actual arrival, block/inventory and stopped processes.
Existing combat, menu and handoff cases cover their physical results; no new
test framework is used. The open-ended empty-inventory task to build a 10×10
two-storey house in [TESTING.md](TESTING.md) has started; no completion result yet.

Sources: [Baritone settings](https://github.com/cabaletta/baritone/blob/v1.18.0/src/api/java/baritone/api/Settings.java)
and [LookBehavior](https://github.com/cabaletta/baritone/blob/v1.18.0/src/main/java/baritone/behavior/LookBehavior.java).
