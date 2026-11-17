---
name: end-crystals
description: Read this while End crystals still stand on the obsidian pillars, the first stage of the dragon fight - open ones shot with arrows from the ground, caged ones by pillaring up and breaking the iron bars. Mount it on the ladder item that clears crystals.
tools:
  - read_state
  - move_to
  - ranged_attack
  - pillar_up
  - mine_block_collect
  - dig_down
  - engage_entity
  - read_nearby_blocks
---

# End Crystals

## Purpose

Remove every End crystal so the dragon cannot heal. Use it for the ladder item
"clear the crystals", usually the first stage of `end-dragon-fight`.

## Done When

- No crystal remains in a fresh entity read.
- A shot that "completed", or an explosion you heard, is not enough: read the
  entities.

## Approach

1. Read the crystal entities. Each has a position (on top of a pillar) and
   `caged` (iron bars around it).
2. Do all the open ones first, from the ground; they are fast and safe.
3. Then the caged ones, one pillar at a time.
4. After each crystal, read the entities again and move on to the next one.

## Routes

**Open crystal: arrows from the ground.**
- Stand 15–40 blocks from the pillar on solid ground away from the edge.
- `ranged_attack` at the crystal; the Body aims with arrow drop. One arrow
  usually destroys it.
- If the result says the arrow struck a block, that block is in the way:
  move to another side or farther away, then shoot again.

**Caged crystal: pillar, break the bars, come down.**
- Go to the foot of its pillar. `pillar_up` with carried blocks until the Body
  stands about level with the cage (the crystal's height gives the target).
- Break the iron bars on the side facing you with `mine_block_collect`,
  naming the bars as the target; the Body breaks what it can reach from where
  it stands. No area needs to be written first.
- Step back on your own column, then shoot the crystal or hit it once and
  back off: it explodes and hurts anything close.
- Come down with `dig_down` before the next crystal.

**Melee on a reachable crystal.**
- A crystal on a short pillar can be hit with the sword, but the explosion
  hurts. Prefer one arrow from a few blocks away.

## Problems And Handling

- **Arrow struck iron bars:** the crystal is caged; use the caged route.
- **Pillar or descent stopped:** read the returned position and reason. Pick
  another side of the pillar or another way down; do not repeat the same
  attempt unchanged.
- **Dragon charging while you are high up:** a throw from a pillar top is the
  most common death. If it heads your way, finish the current step quickly or
  dig down, and go back up after the pass.
- **Out of blocks:** you cannot pillar. Collect or plan blocks before the caged
  ones.
- **Being thrown / falling:** the frame offers `cushion_fall` with a water
  bucket. The Body pours, lands in the water and takes it back into the
  bucket in one go. If the result says the water was not taken back, fill the
  bucket again before going back up: the next throw needs it.

## Cautions

- Do not shoot crystals that are close to you; the blast hurts.
- Stay off the island edge while shooting.
- An exploded crystal can respawn only if the dragon is summoned again; you do
  not need to watch them after they are gone.
