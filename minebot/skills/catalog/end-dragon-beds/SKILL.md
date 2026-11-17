---
name: end-dragon-beds
description: Read this only when you choose beds over the sword for the perched dragon - damage it with exploding beds from behind cover, using the head-to-bed distance the frame reports. Mount it on the ladder item that finishes the dragon with beds.
tools:
  - place_block
  - use_on_block
  - equip_item
  - move_to
  - read_state
---

# End Dragon Beds

## Purpose

Deal heavy damage to the dragon with beds, which explode when used in the End.
Use it for the ladder item "finish the dragon with beds", normally after at
least the open crystals are gone (otherwise it heals between perches).

## Done When

- The dragon is dead (gone from the entity read; exit portal open).
- For one perch: the bed cell is empty and the dragon's health is lower.

## Approach

1. Carry several beds (5 or more), obsidian, food and cover blocks.
2. Wait near the exit portal in the island's middle, but not on top of it,
   until the dragon's phase says it is landing or perching there.
3. Build: an obsidian block next to the portal, a bed on or beside it with the
   head end toward where the dragon's head will be. The frame then tells you
   "The dragon's head is d blocks from the bed at [x,y,z]".
4. Stand to the side, below the bed, with one solid block between you and the
   bed but still able to reach it (within 4.5 blocks).
5. When the head is beside the bed (d is small), use the bed: the frame offers
   it as an option and the Body clicks it with `use_on_block`.
6. After each blast read health, phase and bed. While it still perches and you
   still have cover, place the next bed. When it takes off, step back and wait
   for the next perch.

## Routes

**Single bed per perch:** safest. One bed, one blast, back to cover.

**Several beds per perch:** more damage per perch, but you spend health. Only
while health is high and the dragon stays down.

**Bed as finisher only:** clear crystals with arrows (`end-crystals`), let the
dragon drop low with arrows, then one or two beds to end it.

## Problems And Handling

- **The bed is still there after use:** the click did not happen. Check reach
  and the bed position from the frame; do not repeat a click that was not seen.
- **Head never comes close:** move the next bed nearer to the portal centre;
  the distance in the frame is your guide.
- **You take blast damage:** the cover is wrong. Put a solid block between you
  and the bed, stand lower.
- **Dragon takes off:** stop placing beds; retreat from the portal and wait.
- **Health falling:** leave the portal area, eat, come back for the next perch.

## Cautions

- Never sleep in the End or the Nether; beds explode there.
- Keep at least one bed for another perch if health drops.
- Do not stand on top of the exit portal; the dragon lands there.
