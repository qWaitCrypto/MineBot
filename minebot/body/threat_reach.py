"""Vanilla attack envelopes, in blocks; these are not promises of safety."""

# Vanilla Java 26.1.2 (Mojang class/method names). Melee is a conservative
# horizontal envelope for a .6-wide mob and player: Mob.getAttackBoundingBox
# inflates by DEFAULT_ATTACK_REACH = sqrt(2.04) - .6. Add both half-widths
# and include diagonal contact: sqrt(2) * sqrt(2.04) = sqrt(4.08).
# Projectile values are AI firing ranges, not maximum projectile travel.
ATTACK_REACH = {
    "melee": 4.08 ** 0.5,
    "spider": 3.0,  # 1.4-wide body: diagonal contact envelope rounded up.
    "cave_spider": 2.1,  # EntityType width .7 + the same melee inflation; diagonal envelope rounded up.
    "creeper": 6.0,  # Creeper.explodeCreeper: power 3; ServerExplosion: damage radius = 2 * power.
    "charged_creeper": 12.0,  # explodeCreeper doubles power when powered.
    "skeleton": 15.0,  # AbstractSkeleton: RangedBowAttackGoal(..., 15).
    "stray": 15.0,
    "bogged": 15.0,
    "pillager": 8.0,  # Pillager.registerGoals: RangedCrossbowAttackGoal(..., 8).
    "witch": 10.0,  # Witch.registerGoals: RangedAttackGoal(..., 10).
    "blaze": 48.0,  # Blaze attributes FOLLOW_RANGE=48; BlazeAttackGoal uses follow distance.
    "ghast": 64.0,  # GhastShootFireballGoal: distanceToSqr < 4096.
    "ender_dragon": 64.0,  # DragonStrafePlayerPhase: distanceToSqr < 4096.
    # A dragon can charge from farther away. Its sitting breath cloud has
    # radius 5 (DragonSittingFlamingPhase); actual cloud facts, not distance
    # from the dragon's centre, determine which ground is safe to step on.
}

DODGE_MARGIN = 1.0
# Same timescale as Decision's step_* actions: return control for the next
# frame instead of adding legs when a mob follows. No recovery/replan legs.
DODGE_TIMEOUT_S = 3.0
# The native step fact walks at most this many cells per direction (Java
# MineBotClientBody.STEP_REACH); a dodge leg never goes farther.
DODGE_STEP_REACH = 4


def attack_reach(kind: str) -> float:
    key = kind.lower().removeprefix("minecraft:").replace(" ", "_")
    return ATTACK_REACH.get(key, ATTACK_REACH["melee"])
