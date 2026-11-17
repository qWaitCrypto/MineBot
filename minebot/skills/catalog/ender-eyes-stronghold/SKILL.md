---
name: ender-eyes-stronghold
description: Read this when blaze rods are in hand (or coming) and the next need is ender pearls, eyes of ender and the stronghold - hunting endermen safely, crafting eyes, throwing them and crossing two throws, entering the stronghold and filling the End portal. Mount it on the ladder item that finds the End portal.
tools:
  - set_walk_gaze
  - engage_entity
  - search_for_entity
  - read_nearby_entities
  - pickup_items
  - craft_item
  - use_item
  - move_to
  - explore_for
  - dig_down
  - search_for_block
  - use_on_block
  - read_nearby_blocks
  - read_state
---

# Ender Eyes And Stronghold

## Purpose

Collect enough ender pearls and turn them into eyes of ender. Use the eyes to
find the stronghold, then open its End portal. You need at least 12 eyes if
the frame is empty, and you lose some on the way: about one throw in five
breaks the eye. Aim for 15 or more pearls and 8 rods. No run of this Body has
done these steps live yet. The pieces have only been proven in GameTests:
the eye's flight is reported, and the endermen gaze rule works. Write down
what really happens.

## Done When

- The End portal in the stronghold is lit (all 12 frame blocks hold an eye),
  and the Body stands next to it. `read_nearby_blocks` shows `end_portal`
  blocks inside the frame.
- Pearls, powder and eyes count only from the inventory.
- A crossing point of two throws is a guess about where the stronghold lies,
  not a found stronghold. Only the portal room counts.

## Approach

1. **Pearls from endermen** (Routes A), or from bartering in the Nether (see
   `nether-expedition`). Endermen drop 0 to 1 pearl each.
2. **Eyes:** craft each rod into 2 blaze powder (`craft_item`), then each eye
   from 1 powder and 1 pearl. Craft a few at a time, and keep the rest as
   pearls and rods until you need them.
3. **First throw:** stand in the open, with sky above and no tree or wall in
   the way. `use_item` with `ender_eye`. The result's `thrown_entity` gives
   the direction the eye flew (`direction`, `yaw_degrees`) and how it ended
   (`ended`). An eye that drops can be picked up again (`pickup_items`); one
   that shatters is gone.
4. **Second throw, far away:** walk several hundred blocks **sideways** to
   the first direction, not along it, and throw again. The result of the
   second throw of the same item says where the two flight lines cross:
   "The two throws cross at x=…, z=…, about N blocks from here." That is
   your target. Write it into memory and the life profile.
5. **Go there**, then throw one more eye when close. Near the stronghold the
   eye flies down into the ground instead of forward. Dig down there
   (`dig_down`) with care, or follow a cave, until you see stronghold
   bricks: `search_for_block` for `stone_bricks`, `mossy_stone_bricks` and
   `cracked_stone_bricks`.
6. **Find the portal room:** it has an `end_portal_frame` ring over a lava
   pool, and a silverfish spawner on the stairs. `search_for_block`
   `end_portal_frame` while you walk the corridors.
7. **Fill the frame:** `use_on_block` with an `ender_eye` on each frame
   block that has no eye yet, then read the blocks to check. Count the empty
   frame blocks first, so you know how many eyes you need.
8. **Then** read `end-preparation`. Do not jump into the portal on the spot.

## Routes

### A. Hunting endermen in the Overworld at night
- Endermen spawn in the dark across the Overworld, few at a time. Ender
  pearls drop where they die.
- Set `set_walk_gaze` `look_down: true` first. A level gaze at an
  enderman's head angers it; in a staged test, 3 of 3 got angry with a level
  gaze, 0 of 3 with the gaze down. A death turns the setting off; STATE says
  so; set it again.
- To fight one: health full, armour on, sword in hand. Stand under a roof
  two blocks high, such as a dug-out hole or a doorway with a block above.
  Endermen are three blocks tall and cannot follow you in. Then
  `engage_entity` the one you have angered. Fight one at a time: in the End,
  two angry endermen killed a full-health Body in 7 seconds.
- Arrows do not hurt endermen; they teleport away. Rain and water hurt them,
  and they teleport off.
- Do this from a home or a sealed spot you can go back to. It is still a
  night outside. Read `hostile-survival`.

### B. Warped forest in the Nether
- Many endermen live there, and few other mobs do. The same gaze and roof
  rules apply. Build a 2-high cubby out of netherrack or cobblestone and
  fight from it.
- It is far from home and full of lava around. Use it when the Overworld
  gives too few pearls.

### C. Bartering
Gold to piglins, as in `nether-expedition`. It is slow, because few trades
give pearls, but it carries no gaze risk.

## Problems And Handling

- **An enderman is angry at you and you did not mean it.** Either the gaze
  was level, or you hit it. Get under a 2-high roof, or stand in water if
  any is near. Do not run in the open: it teleports next to you.
- **Decision's own moves may still look up.** The look-down setting covers
  your calls, plans and Decision's moves. Earlier, Decision's step moves
  angered 8 of 31 endermen in one run. If anger keeps happening while you
  stand, look at STATE for the gaze line.
- **Several endermen in view.** Calm endermen need nothing. Do not write a
  rule like "move away from every hostile". That rule walked the Body off
  the End island. Anger one at a time.
- **The eye result says `use_unverified`.** It says this even when the eye
  flew; this is how the result reads a thrown item. Read `thrown_entity` for
  the direction, and the inventory for how many eyes are left.
- **The two throws are nearly parallel** (they were thrown too close
  together, or along the same line). No crossing is given. Walk farther
  sideways and throw again.
- **The eye hits terrain at once.** Throw from open ground or a hilltop. A
  test had to raise the throw to Y 180 to clear terrain.
- **A thrown pearl is a teleport, not an attack.** In the End a pearl thrown
  toward the island centre landed the Body in the void. Throw pearls only at
  solid ground you can see, and never repeat a pearl throw without reading
  where the first one put you.
- **Silverfish** come out of stone bricks and the spawner. Kill the spawner
  room's silverfish, or break the spawner with a pickaxe, before you fill the
  frame.
- **Lava under the portal frame.** The portal room has a lava pool under the
  frame. Do not walk inside the ring until the portal is lit, and fill the
  frame from outside.

## Cautions

- Count eyes before you leave home. Carry spare pearls and powder.
- Mark the stronghold and the portal room in memory and the life profile
  (`outposts`). After a death you will need to come back.
- Do not throw your last eyes for direction. Keep 12 minus the frame's
  filled count for the portal.
- Exact facts (eye break rate, stronghold rings, room layout in 26.1.2):
  `wiki_read` "Eye of Ender" and "Stronghold".
