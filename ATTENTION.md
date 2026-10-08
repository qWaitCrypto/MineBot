# Client attention design

Status: design, not yet applied to the client. Collection changes are separate.

The user should see purposeful movement and attention without losing accurate
digging, placement or combat. Attention belongs to the action already running;
nearby entities do not automatically become targets.

| Situation | Rotation owner and behavior |
| --- | --- |
| Travel/exploration | Baritone's intended path heading, rather than measured velocity; turn into a corner without reacting to a bump or fall. |
| Mining/building | Baritone's block interaction rotation; look at the block being worked on, including upward tree work. |
| Melee/bow/crossbow | Existing combat aim owns yaw and pitch while striking, charging or shooting. Movement can strafe or retreat without replacing that aim. During distant approach, path rotation remains available. |
| Follow | Face the path while catching up. Once within the requested distance and no longer pathing, look toward the resolved target. Keep that identity until the action ends or the target is lost. |
| Handoff/menu interaction | Face the recipient or block during the actual interaction. Approach still uses path rotation. |
| Explicit look/idle | Honor the explicit look. After it finishes, keep the current view until a new action needs it; no compulsory scanning or random target switching. |
| Elytra | Flight keeps its existing yaw/pitch control; ground attention does not alter flight. |

Start with upstream settings: ground travel uses `freeLook=false` and
`remainWithExistingLookDirection=false`. Keep `blockFreeLook=false`.
Baritone 1.18.0 otherwise defaults to keeping the visible camera separate from
movement. Its own `LookBehavior` already distinguishes travel and interaction
rotations; use that mechanism rather than a parallel per-tick velocity controller.

Smooth visible travel turns only. Precise interaction and projectile aim bypass
cosmetic smoothing. If upstream `smoothLook` is used, check its +/-180-degree
transition on this tuple before enabling it: averaging raw yaw can turn the
wrong way at that boundary. Near-target attention should use wrapped angle
differences and a turn-rate limit, and should stop on cancellation/target loss.

Apply and restore these settings within the owning action. Near-target following
only writes rotation while navigation is idle. Combat and block interactions do
not accept a lower-priority attention update. Ordinary chat does not replace the
action or issue a competing look command.

Acceptance is one short visible sequence: turn around a corner, work on a high
block, approach/follow the selected player past a decoy, shoot the selected
moving target, then cancel. Confirm arrival, actual block/inventory and damage,
target identity, and stopped controls. No new attention framework is needed.

Sources: [Baritone settings](https://github.com/cabaletta/baritone/blob/v1.18.0/src/api/java/baritone/api/Settings.java)
and [LookBehavior](https://github.com/cabaletta/baritone/blob/v1.18.0/src/main/java/baritone/behavior/LookBehavior.java).
