---
name: end-dragon-fight
description: Read this first whenever the goal is the Ender Dragon, before or on entering the End. The whole fight - clear the crystals, then kill the perched dragon with the sword - and staying alive against breath, charges, endermen and the void. Mount it on the ladder item that kills the dragon.
tools:
  - read_state
  - move_to
  - ranged_attack
  - engage_entity
  - pillar_up
  - dig_down
  - place_block
  - use_on_block
  - equip_item
---

# End Dragon Fight

## Purpose

Kill the Ender Dragon, the last and hardest step. Use this from entering the
End until the dragon is dead: how to organise the fight as ladder items and
what to watch for; the Body and Decision do the moment-to-moment moves.

## Done When

- The dragon is gone from a fresh entity read and no longer reappears, and the
  exit portal in the middle of the island is open (it lights up when the dragon
  dies).
- A hit, an explosion or a low health number is not a kill. Only the dragon's
  absence counts.

## Approach

1. Arrive prepared (the kit is in `end-preparation`; at least 2 stacks of
   arrows). You cannot go back for more: the only way out before the kill is
   death.
2. On arrival, look before acting. Read the entities: the dragon, the crystals
   on top of the obsidian pillars (each says whether it is `caged`), endermen
   near you. Note where the island edge is. You arrive (also after every
   death) on the obsidian platform floating off the island's outer edge.
   Get off it and 20+ blocks inland before any fight: a dragon hit there
   throws the Body into the void (run-28: two deaths). `move_to` or
   `keep_moving` with an inland circle both get there (they build up off it).
3. Choose a route (see Routes) from what you carry and what you see. Write it
   as ladder items, one item per stage, each with its own "when", and mount
   the matching route skill (`end-crystals`, `end-dragon-beds`) on the item
   that needs it. Example ladder: "clear the open crystals with arrows" →
   "clear the caged crystals" → "kill the dragon with the sword while it
   perches".
4. Two settings on arrival and again after every death (death resets both):
   `set_walk_gaze` with `look_down: true` (the Body looks at the ground
   whenever it does not aim, walking or standing; a level gaze angers
   endermen: staged level 3/3, down 0/3), and `set_movement` with
   `mode: keep_moving` (see Rhythm). STATE shows both; after a death it says
   which one the death turned off.
5. Once, on first arrival (rules outlive a death), write this rule without
   a call (`update_life`), in these words:
   `{"when": "any end crystal still stands", "then": "do not shoot the dragon: arrows are finite and only for the crystals"}`.
   Decision shoots on its own when a bow is carried (run-31: 78 of ~130
   arrows into the dragon without the rule). Keep these exact words and keep
   the rule until no crystal is left: in run-32 rewrites ("do not attack,
   chase or engage the dragon") made it shoot more, and deleting it more
   still. SHOTS in your context says who loosed each arrow at what.
6. While the fight runs, keep one rule in mind for every item: a position that
   is low, covered and away from the edge is where the Body survives. High,
   open and near the edge is where it dies.
7. After each stage, read the result and the entities again before starting
   the next one. If the dragon's health stops falling for a whole stage, the
   route is wrong: change the ladder, do not repeat the same item.

### How to run the fight (lessons from run-11)

The Body cannot fight between plans (run-11: no plan in 835 of 1111 frames).
- **Give a plan time.** Once a plan is running, let it run at least 3 minutes
  before judging it. Judge by what it achieved (crystals gone, dragon health
  over time), not by one bad moment.
- **Do not take control to "win the Body back".** The protective choices
  Decision makes during a plan come from the frame and from your own rules.
  If a rule drags the Body into a fight you did not want, change the rule.
  If the plan is wrong, revise or replace the plan; do not fight the reflex.
- **Always have the next plan running.** Before one stage ends, know the next
  one. A dead time with no plan is the most dangerous state in the End.
- **Return to station.** Plans drift: the Body is pushed, thrown or chases a
  target. Each stage should start by going back to its area (`set_movement`
  with an explicit `center` does this), not by waiting for a condition that can
  only happen at the old spot.
- **How rules work.** A rule with a call is offered to Decision as that call
  when its condition holds. A rule without a call is your standing words:
  Decision reads them in every frame and picks among the concrete actions on
  offer (stepping out of a place that takes health is always among them).
  - Add "and the Body is not losing health" to keep-working rules, and "no
    breath cloud near" to eat rules (eating in breath killed three times in
    run-15; "dragon far → keep working" held while the Body stood in breath).
  - Rules are not attack orders: "hit the dragon within 20 blocks" pulled
    the Body into open melee and killed it twice.

### Facts you can rely on (do not re-derive each time)

- A crystal heals the dragon while it is within about 32 blocks (about 2
  health per second). While any crystal stands, all damage is healed back,
  perch hits too: after each perch it flies past the pillars (run-27: sword
  200 → 168, healed to 200 within a minute). Only with every crystal gone
  does damage stay done.
- Arrows are finite: what you carry into the End is all you have, and a death
  does not bring more. An open crystal takes 1–3; keep enough for every crystal
  still standing. The dragon in flight moves ~25 blocks per second and arrows
  at it miss: do not shoot it (run-29: 165 arrows at it, its health never
  moved, none left for the crystals).
- Crystals on low towers (top below about y 90) can be shot flat from the
  ground at ~40 blocks. Crystals on tall towers (y 90–105) cannot: the arrow
  strikes the tower's edge. For those, `pillar_up` beside the tower (or stand
  on a neighbouring tower top) and shoot level.
- Endermen hurt by the dragon are angry with the dragon, not with you.
- The sword is the killing weapon. A hit on the dragon's head deals the full
  sword damage; a hit on its body or wings only about a quarter. A perched
  dragon lowers its head toward the ground near the portal: that is where to
  hit.
- The exit portal in the middle is where the dragon lands and breathes fire.
  While any crystal stands, stay 20+ blocks from it; go there only for the
  kill (run-29: six breath deaths beside it with crystals still up).

### Rhythm: keep moving, like a player

A player in the End is never planted. Standing still in the open is how the
Body dies; it is not "waiting safely".
- **Dragon circling (high, far):** never shoot at it (see Facts); use the
  time on crystals or move between covered spots.
- **Dragon diving or charging toward you:** move sideways across its line,
  not straight back, and not toward the edge. Then go on with the work.
- **Dragon perched on the exit portal (its phase says so):** with every
  crystal gone, the kill window (while any stands, shoot crystals: hits on
  the perch are healed back afterwards, see Facts). Sword in
  hand, walk in beside its head (not in front of it: its
  breath goes forward from the head), keep hitting the head with
  `engage_entity` until it takes off, then step away from the portal. Several
  perches with the crystals gone kill it.
- **Breath cloud on the ground:** you are in a place that takes health — step
  out first, then decide. Never finish a shot inside it.
- **The legs: `set_movement`.** With `mode: keep_moving` (a centre on the
  island away from the edge and the exit portal, radius 10–16) the Body walks
  or sprints about the area by itself. A shot, eating or the shield happen
  while it walks; a walk, a dodge, placing or `pillar_up` holds the legs and
  they go on afterwards. It ends only when you set `stand`, at death, or when
  the Body cannot move (you are told where and why). No plan or call is
  needed to keep moving, and nothing has to "resume" it.
- **The moments: rules with a call.** With the legs moving, say when to act;
  Decision offers the call in every frame where its `when` holds and the Body
  does it on the move. Example (`update_life` rules; adapt):

```json
[{"when": "an open end crystal (not caged) is within 40 blocks and the Body is not losing health", "call": {"tool": "ranged_attack", "arguments": {"target": "end_crystal", "weapon": "bow", "max_range": 40}}},
 {"when": "no end crystal is left, the dragon is perched on the exit portal within 6 blocks and health is above 12", "call": {"tool": "engage_entity", "arguments": {"target": "ender_dragon", "disengage_health": 8, "timeout_s": 30}}}]
```

  Stages that are whole tasks (caged crystals, beds) stay ladder items with
  work; the legs go on after each of their actions.
- **Cover:** a 2-high roof (a few obsidian or cobblestone blocks over your
  head, open on one side) blocks breath, stops endermen (they are 3 tall) and
  is where you shoot crystals from. Build one early, before the first crystal,
  not after the first death.

## Routes

**A. Crystals first, then the sword (the main route; use this one).**
- Crystals heal the dragon (see Facts): remove every one of them first.
- Open crystals: shoot from the ground at range (`ranged_attack`; the Body
  aims with arrow drop). One arrow is usually enough.
- Caged crystals (`caged=true`, iron bars around them): pillar up beside the
  pillar, break the bars from where you stand, step back, shoot or hit the
  crystal from a few blocks away, then dig back down. Details in `end-crystals`.
- Then, with no crystal left, the kill: when the dragon perches on the exit
  portal (its phase says so), go in beside the head and hit it with the sword
  (`engage_entity`) for as long as it stays; repeat on every perch. Keep
  health above about 12 for each perch (eat between perches, out of breath).

**B. Beds (a finisher when the sword cannot get close; most dangerous).**
- Beds explode when used in the End. A bed exploding next to the dragon's head
  deals heavy damage.
- Works best while the dragon perches on the exit portal. Place obsidian, put
  a bed so the dragon's head comes beside it, stand behind cover, and use the
  bed when the frame says the head is close to it. Details in
  `end-dragon-beds`.
- Clearing at least the open crystals first still helps: otherwise the dragon
  heals between perches.

## Problems And Handling

- **Endermen.** The End is full of them. A calm enderman does nothing: leave
  it alone, do not flee from it, and never make a rule that moves the Body away
  from every hostile — in the End that walks it off the island (run-9: such a
  rule sent the Body into the void). They anger at the Body when it looks at
  them (walk with `look_down`) or hits them; arrows never hurt them. An angry
  one hits hard (two killed a full-health Body in 7 s): fight it under a
  2-high roof (they are 3 tall), stand in water, or use the sword while
  health is high.
- **Being thrown.** The dragon flings the Body when it charges. Near the edge
  this ends in the void. Stay off pillar tops when the dragon is charging that
  way. While falling, the frame offers `cushion_fall` (water landing) when a
  water bucket is carried; the Body takes the water back after landing. If
  the bucket is still empty afterwards, fill it before going back up.
- **The void.** Falling off the island kills. Never bridge over the void,
  never fight on the outer edge.
- **Death.** The Body respawns, normally in the overworld. Read the inventory,
  equip what you have and rethink the plan, do not repeat it. Where you died
  is where the danger is.

## Cautions

- Never treat a completed movement or attack result as progress; read the
  dragon's health and the crystals.
- Do not wait on a pillar top: shoot, then come down (run-15 waited on a 1×1
  column at y 80 and was thrown off twice).
- Keep at least a few building blocks for cover and for getting down.
- `move_to` a point below the ground you stand on makes the Body dig down to
  it, into a pit it may not get out of (run-29: y 60). Give the surface y.
- The dragon egg and exit portal appear after the kill; there is no need to
  take the egg.
