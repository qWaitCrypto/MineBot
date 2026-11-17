---
name: nether-portal
description: Read this when the next goal is the Nether and no portal exists yet - getting obsidian (mining it, or making it from lava with a water bucket), building and lighting the frame, walking in, and the lava deaths earlier runs had doing exactly this. Mount it on the ladder item that builds the portal.
tools:
  - collect_resource
  - ensure_tool_for
  - search_for_block
  - mine_block_collect
  - use_on_block
  - place_block
  - read_block
  - read_nearby_blocks
  - move_to
  - store_items
  - read_state
---

# Nether Portal

## Purpose

Get the Body from the Overworld into the Nether through a portal it built
itself, without dying in lava on the way. This Body has done it from zero
three times: M1 run-2 took 3 h 47 min, S7 3 h 17 min, and H5 55 min. The
slow runs lost hours to lava deaths and to searching for obsidian. This skill
covers what those runs learned.

## Done When

- After the walk into the portal, the result or `read_state` says the Body
  is in `the_nether`. The result frame says "The Body is now in ..." when the
  dimension changed.
- Both portal positions are written down: the Overworld frame and the Nether
  arrival cell, with the ledge you step out onto. Put them in your life
  profile (`outposts`) and in memory.
- A built frame is not a done portal. A lit-looking frame is not either. Only
  the dimension counts.

## Approach

1. **Prepare the home first.** Store valuables in the home chest
   (`store_items`). Sleep in the home bed once, so a death in lava wakes you
   there and not at the world spawn. S7 died in lava carrying a diamond
   pickaxe, iron, obsidian and the bucket, with no chest at home. It lost about
   2.5 hours.
2. **Tools:** an iron pickaxe to mine diamonds, then a diamond pickaxe to
   mine obsidian (`ensure_tool_for` handles the chain). You also need a water
   bucket (3 iron) and flint and steel (1 iron and 1 flint; flint comes from
   mining gravel). Get the flint and steel **before** the frame is done.
3. **Obsidian:** 10 blocks for a frame without corners; corners may be any
   block. See Routes for how to get it.
4. **Build the frame** where the ground is dry and flat, near home, away from
   any lava. The inside is 2 wide and 3 high. `place_block` each frame block,
   then `read_nearby_blocks` to check the frame really stands, block by block.
5. **Light it** with `use_on_block`, holding flint and steel, on the top
   face of a bottom obsidian block inside the frame. Check with `read_block`
   that a `nether_portal` block appeared. If it did not, a frame block is
   missing or misplaced.
6. **Walk in** with `move_to` to a portal cell inside the frame, **radius 0**.
   With a radius above 0, `move_to` has said "arrived" while the Body still
   stood outside the portal. Then wait a few seconds standing in it. The
   result tells you the new dimension.
7. **On arrival**, write down the arrival cell and the solid ledge next to
   it, then look around before you walk anywhere. Arrival portals can open
   on a cliff or beside a lava sea. From here, read `nether-expedition`.

## Routes

### A. Mine obsidian with a diamond pickaxe (the usual route)
Look for obsidian where water has met lava. You find it in caves and ravines
near diamond depth, beside lava pools, and in dry pits where a lava pool has
already turned. S7 mined 4 obsidian from a dry pit. A diamond pickaxe takes
about 9 seconds per block, so stand still and let it finish.

### B. Make obsidian from a lava pool with a water bucket
Pour water from the bucket (`use_on_block` with the water bucket, aimed at
a block beside the lava) so it flows over lava **source** blocks. Each
source turns into obsidian. Flowing lava turns into cobblestone instead.
M1 run-2 made 11 obsidian this way at y -54, then mined it with the diamond
pickaxe. Scoop the water back with the empty bucket when you are done.
Two cautions make this route safe. Stand on a solid block that does not
touch the pool. Pour from one block above the lava level, not at it.

### C. Cast the frame in place (no diamond pickaxe)
Speedrunners build a mould from other blocks, pour lava into the frame
cells and turn each one with water. It works, but it means carrying lava
buckets and pouring lava near the Body. This Body has never done it, and a
lava pour is what killed S7. Use it only if diamonds are truly out of reach,
and check `wiki_read` "Nether portal" for the mould shape first.

## Problems And Handling

- **Never light the portal with lava.** S7 had no flint and steel. It poured
  lava next to the frame to set it alight, the lava flowed onto the Body, and
  the Body burned to death in 7.7 s. The tool only reported
  `targeted_use_unverified`. Get flint first: gravel drops it sometimes, so
  mine gravel until one drops.
- **Lava flows into the cell you open.** M1 run-2 removed an obsidian block
  next to its standing cell. Lava behind it flowed in and killed the Body 4
  seconds later. Before mining a block over or beside a lava pool, read the
  cells behind it (`read_nearby_blocks`). If lava is behind a block, put a
  block on the far side first, or mine it from another side. Never mine the
  block you stand on over a pool.
- **In lava or on fire:** Decision is offered leaving the lava and putting
  out the fire, which pours water on the Body's own cell. That only helps
  if you carry a water bucket, so keep one in the bag at all times near
  lava.
- **A pour that reports `targeted_use_unverified`** did nothing you can see.
  Read the cell before trying again; do not repeat the same pour blindly.
- **Water poured on flowing lava makes cobblestone, not obsidian.** Only
  lava source blocks turn into obsidian. If you get cobblestone, the source
  is somewhere else in the pool.
- **The frame lights but nothing happens when you walk in.** Stand inside
  for about 4 seconds. Check that the Body's feet are in a `nether_portal`
  cell (`read_state`), not beside it.
- **You come back through a different portal.** The Nether is scaled 1:8
  against the Overworld. A return trip can make a new portal if you lit a
  second one, so keep to the coordinates you wrote down.

## Cautions

- Do not carry everything into a lava area. Take the diamond pickaxe, a
  water bucket, blocks and food, and leave the spares in the chest.
- Do not build the portal on a tree, on grass beside a forest, or next to
  your wooden home. Portal fire and lava can spread.
- Keep the Overworld portal reachable from home. Its position is how you get
  back.
- On easy and harder, a Nether portal can let zombified piglins through.
  Leave them alone; they are neutral until hit.
- For exact facts (mould shapes, minimum sizes, how lava flows in this
  version), use `wiki_search` / `wiki_read`. The server runs 26.1.2.
