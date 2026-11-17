---
name: nether-expedition
description: Read this before stepping through a Nether portal, and keep it mounted while the Body is in the Nether - travel, lava, neutral mobs, finding a fortress, blaze rods, piglin bartering for ender pearls, and getting back. Mount it on the ladder item that works in the Nether.
tools:
  - equip_item
  - explore_for
  - search_for_block
  - search_for_entity
  - read_nearby_entities
  - move_to
  - engage_entity
  - ranged_attack
  - raise_shield
  - place_block
  - pillar_up
  - discard_item
  - pickup_items
  - set_walk_gaze
  - consume_item
  - read_state
---

# Nether Expedition

## Purpose

Bring blaze rods home from the Nether, and ender pearls if bartering works.
Get back alive with what you went for. 12 eyes of ender need 6 blaze rods
(12 powder) and 12 pearls. Bring back extra of both, because thrown eyes
sometimes break. This Body has reached the Nether in three runs. None of
those runs has yet found a fortress, killed a blaze, or made a barter trade
live, so every step here is the best plan we have, not a proven one. Record
what actually happens.

## Done When

- The inventory shows `blaze_rod` (at least 6; 7 or more is better), and the
  Body is back in the Overworld (`read_state` dimension) or at its Nether
  camp with the way back known.
- Pearls count only from the inventory. A killed blaze, a seen fortress or a
  piglin that took gold is not a rod or a pearl.
- Blaze rods cannot be had on peaceful, because no blazes spawn there. If the
  world is peaceful, tell the player instead of searching. An earlier Agent
  found this itself and asked the player to change the difficulty.

## Approach

1. **Before entering:**
   - Wear one gold armour piece. A golden helmet is 5 gold ingots; put it on
     with `equip_item`.
   - Carry an iron or diamond sword and pickaxe, a shield if you have one, 2
     or more stacks of cobblestone (netherrack does not stop ghast fireballs)
     and food. Bring flint and steel to relight the portal, and gold ingots
     for bartering.
   - Leave the spares at home. A death in the Nether can drop everything
     into lava.
2. **On arrival:** write down the arrival portal cell and the ledge beside
   it. One Nether block is 8 Overworld blocks. If the portal stands open to
   lava or a cliff, wall it in with cobblestone before leaving it. S7 sealed
   11 lava cells around its camp before working.
3. **Gaze down.** Endermen live in warped forests and soul sand valleys
   here, too. Turn on `set_walk_gaze` (`look_down: true`) before travelling,
   and again after every death, because a death turns it off.
4. **Travel** with long straight `explore_for` legs over solid ground. Keep
   each call under about 90 s; a call that runs longer can hit the tool
   time limit. Do not walk on the lava sea shore (the surface is at y 31),
   and cross gaps by placing blocks, never by jumping.
5. **Find a fortress** (see Routes A). Fortresses are dark red
   `nether_bricks`, with bridges and corridors. Blazes spawn near their
   spawners inside.
6. **Blaze rods** (see Routes B). Fight from cover, kill one blaze at a time,
   pick up the rods (`pickup_items`), and count the inventory.
7. **Pearls by bartering** (see Routes C) if you brought gold. Otherwise,
   take pearls from endermen; read `ender-eyes-stronghold`.
8. **Leave with margin:** go back while you still have food, blocks and
   health. Do not wait until something runs out.

## Routes

### A. Finding a fortress
- S7 swept the Nether 13 times in straight lines (west, east and in four
  directions) and scouted 12 more times over 8.5 hours. It found no
  fortress. It did find a bastion. So short sweeps around the arrival point
  do not work. Go far: several hundred blocks along one line before you turn.
- While you travel, run `search_for_block` for `nether_bricks` regularly.
  A fortress can lie inside the netherrack beside you, not only out in the
  open.
- A fortress is a structure, so its position is fixed. Write each searched
  line into memory, so you do not sweep it again.
- If a long line finds nothing, use a new line from the portal in a clearly
  different direction. Or build a second portal far away in the Overworld,
  and start from there.
- `wiki_read` "Nether Fortress" for this version's generation facts. The
  server runs 26.1.2.

### B. Blazes
- Blazes fly, shoot three fireballs in a burst, and do not take fire damage.
  Each drops 0 to 1 rod. Fireballs set the Body on fire, and water does not
  work in the Nether.
- Make a cover position near a spawner but out of its open room: two blocks
  of wall with a gap you can hit through. Let the blazes come close, then
  `engage_entity`. A raised shield (`raise_shield`) stops the fireballs.
- From cover you can also use `ranged_attack` on a blaze that hovers still.
- Do not stand in the spawner room while live blazes are around it. Kill
  them from cover first.
- Wither skeletons in the fortress are always hostile, hit hard, and cause
  wither. Fight them at a doorway, one at a time.

### C. Piglin bartering
- Barter is a generic action here, not a trade tool. Wear the gold piece,
  stand near an adult piglin, and `discard_item` one gold ingot toward it.
  The piglin looks at the gold for a few seconds and then drops what it
  gives back. Then `pickup_items`.
- A GameTest has proved that the piglin takes the ingot. No live trade has
  been seen, so report what you get.
- Pearls come from only a few trades in a hundred, so it takes many ingots.
  If dozens give no pearls, switch to endermen.
- Opening a chest or mining gold near piglins angers every piglin that sees
  it. Piglin brutes in bastions are always hostile, so avoid bastions for
  barter.

## Problems And Handling

- **A group keeps reaching you** (magma cubes in basalt deltas, a zombified
  piglin group, hoglins). M2 entered the Nether with no deaths. It then died
  5 times in 50 minutes, mostly with Decision choosing move_away over and
  over against a group. When the frame says moving away has not helped, stop
  running. Wall in with cobblestone or `pillar_up` out of reach, then think.
- **You angered zombified piglins.** One hit angers the whole group around
  you. Go back to the portal or into a sealed tunnel. Do not fight a group.
- **In lava or on fire.** Decision is offered leaving the lava. Water cannot
  put the fire out here, so the fire burns on until it ends. Get onto solid
  ground away from the lava and wait it out. Eat if health is low.
- **Never light anything with lava.** In S7 a lava pour killed the Body.
  Use only flint and steel.
- **Ghasts** float over open lava and shoot exploding fireballs. Stay under
  cover and away from open lava lakes. If one shoots, put cobblestone
  between you and it.
- **You died in the Nether.** Your items stay on the ground there. Items in
  chunks no one is near do not tick away, so they stay until someone comes
  close. If you know where you died, and it was not lava, go back with fresh
  gear. M2 rebuilt from zero instead, and lost hours.
- **`explore_for` region mode stalled** about 5 blocks short of the region
  edge for 92 s. If a search stops moving, `move_to` the point you wanted
  instead.
- **The portal is gone or unlit on the way back.** Relight it with flint and
  steel. This is why you carry it.

## Cautions

- Never sleep in the Nether: a bed explodes there.
- Do not dig straight down, and do not mine the block under your feet. Nether
  lava flows far and fast.
- Do not hit zombified piglins, and do not look at endermen with the gaze
  level.
- Count rods and pearls only from the inventory.
- On peaceful there are no blazes, and no Nether progress beyond the
  portal.
