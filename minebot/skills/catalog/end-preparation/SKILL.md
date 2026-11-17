---
name: end-preparation
description: Read this once the End portal is found (or nearly) and before jumping in - what to carry, where to set the respawn bed and store spares, and how to come back after a death in the End. Hands over to end-dragon-fight. Mount it on the ladder item that gets ready for the End.
tools:
  - read_inventory
  - craft_item
  - smelt_item
  - collect_resource
  - store_items
  - take_items
  - place_block
  - go_to_bed
  - equip_item
  - read_state
---

# End Preparation

## Purpose

Arrive in the End with what the fight needs, and set things up so that a
death costs minutes rather than the run. Once the Body is in the End, the only
way out before the dragon dies is death. Death drops everything on the
island, and often into the void. Every one of about 24 staged End runs began
from a fixed test kit. That kit is the best evidence we have for what is
enough; the Body has never gathered it from zero.

## Done When

- The inventory holds the kit below, or a version you chose and wrote down,
  with the reasons for what you left out.
- A bed stands near the stronghold portal, and the Body has slept in it.
- A chest near that bed holds the second set: spare armour if you have one,
  a sword, a bow, arrows, blocks and food.
- Then mount `end-dragon-fight` on the ladder item that kills the dragon,
  and go in.

## Approach

1. **The kit the End tests used** (run-1 to run-24 and later):
   - full diamond armour and a diamond sword;
   - a diamond pickaxe: a run without one could not dig out of the arrival
     platform;
   - a bow and 2 stacks of arrows;
   - 4 stacks of cobblestone and 2 stacks of obsidian;
   - 2 water buckets;
   - 1 stack of cooked meat;
   - golden apples (16 in the kit);
   - ender pearls (16 in the kit);
   - beds, only if you plan the bed route (20 in the kit).
   No enchanted gear: this Body cannot use the enchanting table yet.
2. **Make what you lack.** Arrows come from flint, sticks and feathers
   (chickens). Beds need wool (sheep) and planks. A golden apple is 8 gold
   ingots and an apple, so gold is the limit; take what you can. Cooked meat
   comes from animals and a furnace. Do the gathering by day, from home.
3. **Respawn bed by the stronghold.** Place a bed in a safe, lit room near
   the portal room, or in the stronghold itself, and sleep in it once
   (`go_to_bed` at night). Do not use `place_bed_and_sleep` for this. It
   breaks the bed again afterwards, and then a death sends you to the world
   spawn, far away.
4. **Spare chest beside the bed.** `store_items` the second set into it.
   After an End death you wake there, take the set (`take_items`), equip it
   (`equip_item`), and walk back to the portal.
5. **Settings before the jump:** `set_walk_gaze` `look_down: true`. The End
   is full of endermen. Read `end-dragon-fight` now, while it is calm. It
   says how to plan the fight, and that a death turns your settings off.

## Routes

### A. Sword route (the main one)
Clear the crystals with arrows and by climbing to the caged ones, then hit
the dragon's head with the sword while it perches. Needs arrows, blocks to
pillar, and good armour. See `end-crystals` and `end-dragon-fight`.

### B. Bed route
Beds explode in the End. You place one by the perched dragon's head from
cover and use it. It needs many beds and obsidian cover, and the timing is
hard. See `end-dragon-beds`. Only plan it if wool was easy to get.

## Problems And Handling

- **Not enough of something.** Arrows and blocks matter most, then food,
  then pearls. Golden apples are a bonus. A short kit is better than a long
  delay, as long as you have armour, a sword, a bow and arrows, blocks and
  water.
- **The inventory is full.** Leave tools you will not use in the End (axe,
  shovel, hoe, spare pickaxes) in the chest.
- **You died in the End and woke at the world spawn.** The bed by the
  stronghold was gone, or you never slept in it. Walk back with the spares
  from home. Next time sleep in the stronghold bed.
- **You died in the End and woke at the stronghold bed.** Take the spare
  set, equip it, and check STATE for settings the death turned off (the gaze,
  the movement). Go back in only with a plan for what killed you.
- **Endermen near the portal room.** Gaze down, and stand under a 2-high
  roof if one gets angry. Read `ender-eyes-stronghold`.

## Cautions

- Do not jump in with the only set you own. Without a second set, one death
  ends the attempt.
- Do not fill the last portal eye and wait beside an open portal at night
  with no armour on.
- Count everything from the inventory, not from what you crafted.
- `wiki_read` "Ender Dragon" and "End" give the version facts (26.1.2).
