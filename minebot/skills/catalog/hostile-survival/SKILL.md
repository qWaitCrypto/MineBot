---
name: hostile-survival
description: Read this when the world is not peaceful (easy or harder) - before the first dusk, and again after any death. How this Body survives nights, mobs and deaths, what our tools really do in danger, and the deaths earlier runs suffered. Mount it on the ladder item that keeps the Body alive at night.
tools:
  - read_state
  - observe_environment
  - shelter_in_place
  - go_to_bed
  - place_bed_and_sleep
  - equip_item
  - consume_item
  - engage_entity
  - move_away
  - pillar_up
  - store_items
  - take_items
  - pickup_items
---

# Hostile Survival

## Purpose

Stay alive and keep your progress in a world where mobs spawn. You already
know Minecraft. This skill tells you what earlier runs of this same Body got
wrong, and how our tools and the reflex layer behave when danger comes. On easy
from zero, earlier runs died 4 times in one night (track S, 2026-10-19), 3
times in 6 minutes at a dusk switched from peaceful, and 6 times in one night
of M2. Nearly every death had the same shape. The Body was outside at night,
or went out hungry. It had the wrong item in hand, faced a group instead of one
mob, and Decision kept choosing move_away while the mobs kept reaching it.

You decide strategy. Decision (the reflex layer) picks one concrete action
each second from the options in its frame. It sees your plan, your rules and
the facts, but it does not read this skill. So survival comes from where you
put the Body, and what it has, before the danger arrives.

## Done When

- Each night ends with the Body alive. Either it slept in a placed bed, or it
  waited in a sealed cell or a sealed home.
- After a death, the Body is back at work. It has armour on, a weapon in hand,
  and a respawn point that still exists. You know what was lost and what was
  recovered.
- Survival is not "no mob near". A night spent outside with no attack was
  luck. It does not count.

## Approach

1. **Know the clock.** You are told when the sun sets: a `dusk` BODY_EVENT
   with the time and the seconds left before night. `read_state` reports
   `time_of_day` and a day / dusk / night sentence at any time. Do not guess
   dawn from the mobs you see: an earlier Agent did, and walked out into the
   dark. Dusk starts at 12000; night on the surface runs about 13000 to
   23000. When the sunset notice comes, stop building outside and carry out
   your night item.
2. **Before the first dusk**, have at least:
   - a sealable place, either a small home with a door or enough blocks for
     `shelter_in_place` (it seals the current cell and checks every wall and
     the roof);
   - a sword or axe;
   - food;
   - if you can, a bed (3 wool from sheep, 3 planks).
   No bed after day one? It is the next day's first job: search wider for
   sheep (`search_for_entity`), or craft wool from 4 string each.
3. **Write the night into your ladder**, and do not leave it to Decision. Give
   the item a clear "when", such as "when dusk comes and the Body is not in
   its home or a sealed cell". Make the item go home, sleep in the bed
   (`go_to_bed`), or seal in (`shelter_in_place`). Mount this skill on that
   item. Its condition must also check that the Body is not already inside.
   Otherwise it fires "completed, nothing changed" again and again, which
   happened 18 times in one run.
4. **Sleep in a bed you leave standing.** `go_to_bed` finds a placed bed,
   sleeps, and sets the respawn point there. `place_bed_and_sleep` places a
   carried bed, sleeps, and **breaks it again**. Once that bed is gone, the
   next death wakes you at the world spawn. For a home, place the bed once and
   use `go_to_bed` every night after that.
5. **Equip before trouble, not during it.** Wear the best armour
   (`equip_item`) and keep the weapon in the main hand when you go out at
   dusk. If a pickaxe is in hand while you dig, switch before leaving the
   mine. `engage_entity` now draws the best melee weapon from the bag. Armour
   lying in the bag, though, protects nothing: one Body died in full iron with
   its sword in the bag.
6. **Eat early.** At food 6 or below the Body cannot sprint. Then `move_away`
   cannot outrun a creeper, a zombie or a spider. A creeper killed the Body
   when it went out at food 5 to hunt. Eat when food drops near 14, and
   always before a night trip or a fight.
7. **Store what you cannot afford to lose.** Death drops everything; the
   server has no keepInventory. Before a risky trip (a night out, a lava
   area, the Nether), put spare diamonds, iron, obsidian and the spare bucket
   in a chest at home with `store_items`. A lava death with no chest cost
   one run about 2.5 hours.

### Health, food and fights in numbers
Measured on this Body (zombie benchmark, 2026-11-10) and vanilla easy:
- Health regrows only while food is 18 or more. In easy run-13 the Body went
  down a mine at 8 health with no food; two zombie fights later it died in
  full iron with a diamond pickaxe.
- A zombie's blow: about 2.5 health bare, about 1 through full iron. A
  creeper beside the Body can take over half its health even on easy.
- `engage_entity` with an iron sword kills a zombie in 4 swings, swings from
  3 blocks, and goes on with the next hostile in sight within 6 blocks (never
  a creeper). One zombie cost 0 health, two at once 1 to 2.5.
- `move_away` runs until the nearest hostile is well out of reach (zombie
  about 10 blocks); zombies cannot keep up, spiders can.
- Fight one or two zombies or spiders at 10 health or more. Three or more, a
  creeper inside 6 blocks, a skeleton out of reach, or under 10 health with
  no food: seal in, or run toward home.
- Ready before a trip from home, below y 40 or past dusk: health 14+, 5+
  food in the bag, owned armour worn (the `Wearing` line, not the inventory
  count), a sword, 32+ blocks. Not ready: getting ready is the item's first step.

### Rules to write before the first night
Rules are how you act within one second; you think in tens of seconds.
Write them with `update_life` (`rules`) on the first day, and keep them
across deaths. A rule with a `call` becomes an option Decision can take at
once. Rules that fit this Body (adapt the words to your world):

```json
[
  {"when": "it is dusk or night, the Body is outside a sealed place, and a skeleton is shooting it or two or more hostiles are near",
   "then": "seal in where the Body stands",
   "call": {"tool": "shelter_in_place", "arguments": {"action": "seal"}}},
  {"when": "the Body is out in the open, not sealed in, and a zombie or a spider is within 4 blocks",
   "then": "fight it with the sword: it is as fast as the Body",
   "call": {"tool": "engage_entity", "arguments": {"target": "nearest_hostile"}}},
  {"when": "the Body woke at the world spawn at night and a hostile is within 16 blocks",
   "then": "dig down out of reach and wait for day",
   "call": {"tool": "dig_down", "arguments": {"target_y": 92}}},
  {"when": "the Body is sealing itself in or digging down or up to get away from mobs, and no mob is in the cell or the shaft with it",
   "then": "keep sealing or digging until it is done"}
]
```

- `nearest_hostile` stands for the nearest hostile mob in the frame.
- `dig_down` needs an absolute `target_y`. Use the world spawn's y minus 3;
  the first respawn notice tells you where the spawn is.
- Write each condition as a situation in words (out in the open, sealed in,
  under water, a mob inside the cell), not as numbers. Decision does not
  compare numbers: a rule saying "health 8 of 20 or more" was chosen at 2
  health, and rules written for the surface were chosen while it drowned.
- The last rule has no `call`. Without it the hand left the Agent's own
  seal or dig to flee mobs still 19 and 27 blocks away.
- Do not write "back away when health is low" for every mob. In run-1 of
  easy (2026-11-06), that rule made Decision step 2 to 5 blocks back from a
  skeleton 16 blocks away, about 25 times. It stayed in the skeleton's line
  of fire, from health 15 to 0, with an iron sword in hand and 139
  cobblestone in the bag. Against a skeleton, distance does not help;
  a wall does.

## Routes

### A. Home first (preferred)
Build a small sealed home near where you work: walls, roof, a door, torches
inside, the bed, a chest and a crafting table. Set it as home in your life
profile, on a cell the Body can stand on. A crafting table placed on the
home cell made `home` report "blocked: feet on a crafting table". Each dusk,
the night item walks home and sleeps. This is the route track S survived
with, from the night it had a bed.

### B. Sealed cell where you stand (when home is far)
`shelter_in_place` with `action: seal` uses blocks from the bag; carry 16 or
more cobblestone or dirt. `look_out` opens one head-height port and reports
the hostiles and their distances, then closes the port again. Wait until
`read_state` says day, then `look_out` before you open up. Creepers wait
outside at dawn. Write your stance in the same turn as the seal: in easy
run-5 a cell with no stance yet was left 11 s after sealing to chase a skeleton.
Seal before mobs arrive: 15 of 54 seals and digs in easy runs 3 to 5 failed
on their own. A failed seal says why: `shelter_unsealable` lists
`blocked_cells` (a mob standing in one is named) and `open_cells`;
`not_enough_blocks` gives `blocks_held` and `blocks_needed`. Step into a 1x2
spot with rock around it, or carry more blocks, and seal again.

### C. Underground through the night
Keep mining, but seal the tunnel behind you with a block. Do not come up
during the night. Light the area you work in, because mobs spawn in dark
caves at any hour.

### D. Caught outside with no blocks
`pillar_up` a few blocks (it uses blocks from the bag), or dig two down and
cover the top. Do not try to run home across open ground at night.

## Problems And Handling

- **A group keeps reaching the Body, and move_away runs again and again.**
  This was the most common death: M2 death 1 had 34 magma cubes and 35
  move_aways, and deaths 2 to 6 showed the same pattern. When the frame says
  moving away "has not helped", running more will not help. Seal in, pillar
  up, or go through a door and close it. Write a rule for it, for example:
  "when several hostiles keep reaching the Body, seal in where it stands".
  Do not write a rule like "move away from every hostile". In the End, such a
  rule walked the Body off the island.
- **Creepers.** Decision once judged a creeper at 11 to 14 blocks as "safe".
  The creeper followed through three move_aways and exploded. Give your
  night rules a creeper line, for example: "a creeper inside 10 blocks: put
  a wall or a door between you, or hit it once and step back". A creeper at
  dawn often waits just outside the door.
- **Skeletons** shoot from 12 to 16 blocks. Running in a straight line away
  from one keeps you in its line of fire. Break the line with a wall or a
  corner instead.
- **Phantoms** come after several nights without sleep, and `move_away` does
  not lose them. Sleep in a bed at night.
- **Respawn loop at the world spawn.** In easy run-1, after the first
  death, the Body woke at the world spawn at night with no bed and nothing
  in hand. It died 11 more times, once every 20 s, while the Agent kept
  calling `collect_resource` for wood. Waking empty-handed at night among
  mobs: get out of reach first (the dig-down rule above), and gather in the
  morning.
- **After a death** (the death notice gives the cause, what was carried, the
  place and how long until the drops despawn, about 5 minutes):
  1. Read `read_state`. Check that you are at your bed. If you are at the
     world spawn, your bed is gone.
  2. If the drops are near and the danger has passed, go back and
     `pickup_items` before they despawn. Track S got its pickaxe and sword
     back this way.
  3. Put armour back on, and a weapon in hand. Track S died again with its
     iron chestplate in the bag.
  4. If the drops are lost, take spares from the home chest (`take_items`)
     before rebuilding from wood.
  5. Do not walk back to the same spot at the same hour that killed you.

## Cautions

- Do not go out at night to fetch food or wool. Plan those for the day. The
  creeper death at food 5 was exactly this.
- Do not leave the bed and the chest in the open. A bed you broke, or one a
  creeper blew up, is not a respawn point.
- Sleeping works only at night or in a thunderstorm, and not with a monster
  close by. If `go_to_bed` says it is daytime or the bed is occupied,
  believe it.
