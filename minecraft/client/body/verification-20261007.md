# Typed client Body verification — 2026-10-07

All experiments used the disposable `127.0.0.1:25566` server and the pinned
Minecraft 26.1.2 / Fabric 0.19.3 / Fabric API 0.152.1 / Java 25 / upstream
Baritone 1.18.0 tuple. Clients were `MineBotCombat` and `Receiver`. No shared
server was used. RCON prepared fixtures and independently checked results;
physical work was requested through `RealClientBody` and the common `Action`
boundary. These are directed physical tests, not an autonomous Agent run.

| Behavior | Actual result | Duration |
| --- | --- | --- |
| Handoff 2 diamonds | Giver 2 → 0; server recipient inventory 0 → 2; pickup packets identify the recipient and count; no ground item remains | 2.15 s |
| Recipient inventory full | `failed/receiver_pickup_unconfirmed`, pickup count 0, receipt false; no diamonds in recipient inventory | 1.19 s |
| Dig down one block | Grounded at Y=149 from Y=150; server floor is air, server position matches | 0.87 s |
| Dig up two blocks | Grounded at Y=152 from Y=150; server position matches | 1.96 s |
| Return to surface | Grounded at Y=155 from Y=152; server position matches | 2.43 s |
| Vertical target beyond distance | `vertical_target_out_of_bounds`, 0 confirmed mutations | 0.09 s |
| Zero mutation budget | `vertical_mutation_budget_exceeded`, floor intact, 0 confirmed mutations | 0.30 s |
| Water below target floor | `vertical_liquid_hazard`, floor intact, 0 confirmed mutations | 0.09 s |
| Cancel vertical work | `canceled`, no Baritone process/pathing; no resumed objective after a further 0.5 s | 126 ms from stop request to terminal |
| Default vertical mutation permission | `vertical_mutation_not_authorized`; server stone floor and original position intact | 0.32 s |
| Bow, one shot | `completed/damage_observed`; server zombie health 20 → 15.08, shots 1 | 1.21 s |
| Crossbow, one shot | `completed/damage_observed`; server zombie health 20 → 13.112, shots 1 | 1.61 s |
| No ammunition, already damaged target | `failed/no_ammunition`, shots 0; existing damage does not count as a new hit | 0.12 s |
| Cancel charging a bow | `canceled`, shots 0; damaged target does not turn cancellation into success | 137 ms from stop request to terminal |

The shaft fixture supplies a diamond pickaxe and 32 cobblestone in a stone
cube at Y=147–154. The ranged fixture supplies a weapon and 16 arrows, uses a
stationary NoAI zombie at eight blocks, and sets midnight. There is no model
planning or resource preparation in these tests.

Reproduced defects fixed in this change:

- Direct `gameMode.destroyBlock` changed client prediction while the server
  kept stone; flooring the transient falling Y falsely reported completion.
  Vertical work now uses normal Baritone movement and break/place behavior.
- A single disappearing drop could falsely complete a multi-item handoff.
  Server pickup identity/count now owns the receipt, including merged drops.
- Vanilla could fire a charged crossbow between Body ticks, consuming three
  arrows while Body reported zero shots. Native use observation now bounds
  firing and records one requested shot.
- Ranged completion cannot replace a cancellation terminal. An injured target
  alone is not proof of damage from the current action.

Raw local reports:

- `logs/real-client-vertical-1791314034897261609.json`
- `logs/real-client-handoff-1791314107867473294.json`
- `logs/real-client-ranged-1791314229161569971.json`
- `logs/real-client-vertical_denied-1791314512396355173.json`

Java compilation and runtime mixin loading passed. The selected Python suite
reported **122 passed, 4 failed**. The four failures are old Java collection
tests in `test_body_provider.py`, all failing with
`interaction_readiness_missing`; all four were independently reproduced from
an unmodified archive of commit `038b987`. They are not fixed by this change.

The earlier sticks, chest, furnace-output and sword fixtures remain separate
evidence. The menu extension below adds workstation and transfer evidence.
Moving-target combat/handoff, broad natural terrain recovery, Elytra flight,
shared-world protection, and a complete Agent task remain unproven. The generic
`baritoneCommand` remains outside the Agent action surface. Mutation limits in
this experiment bound touched positions; a complete shared-world permission
implementation remains work for actual deployment.

## Canonical tool menu slice and inventory slots

The same disposable tuple now completes `craft_item`,
`transfer_container_item`, `smelt_item`, and `clear_furnace` through the
existing Agent tool registry → Python transactions → RealClientBody → native
client menus. RCON only supplies resources/workstations and independently
checks the server result. There is no model planning or autonomous gathering;
all recipes are unlocked for the fixture.

| Behavior | Server-confirmed result | Tool duration |
| --- | --- | --- |
| Craft eight sticks from slot 9 | Eight sticks; four source planks remain; separate slot 27 stack intact | 2.140 s |
| Craft stone pickaxe at nearby table | One stone pickaxe; three cobblestone and two sticks consumed | 2.030 s |
| Craft cake at table | One cake and three buckets returned to named inventory slots | 1.908 s |
| Chest → backpack, then backpack → chest | Three stone withdrawn, two returned; chest seven, backpack one | 0.778 / 0.829 s |
| Transfer two stone into a stack of 63 | `transfer_count_or_capacity_mismatch`; both inventories intact | 165 ms action |
| Full raw iron smelt | One iron ingot collected; unused coal reclaimed; furnace empty | 14.374 s |
| Clear idle furnace fuel | Three coal recovered; furnace empty | 1.361 s |

Native `moveItem` also passes partial moves `9 → 2` (three) and `2 → 9`
(two), offhand transfers `27 → 40 → 35`, and a helmet round trip
`12 → 39 → 12`. Independent server reads verify each count; Minecraft 26.1.2
stores worn equipment in `equipment`, separately from `Inventory`.

Crafting rejects equipment/grid/result slots as ingredient sources, output
destinations, or remainder destinations. Container/furnace transfers reject
player slots 36, 40, 41, and 45 before opening a menu. `moveItem` rejects the
crafting result, crafting-input destinations, zero/excess counts, and
incompatible stacks without changing inventory or equipment. Its explicit
personal-grid recovery source remains slots 41–44; the unit suite exercises
Python residue recovery, but a live pre-filled personal-grid fixture has not
been run. Client refusals now retain their specific reason through Body;
transport failures remain distinct.

Raw local reports:

- `logs/real-client-menus-1791322852744099542.json` (30 tool/action records).
- `logs/real-client-menu-refusals-1791322987539686283.json` (five refusals after preserving their Body reasons).

The first attempt started before client readiness and hit the 30 s IPC wait;
the client became ready in 36.567 s. A subsequent proof initially queried the
old offhand NBT location; after correcting the fixture query to `equipment`,
the full slice passed. These were retained as failed local reports rather
than counted as successful tests.

Client Java compilation and runtime mixin loading passed. The final targeted
inventory, container, furnace, client Body and IPC suite passed **163 tests
and 71 subtests**. This proves the supplied-resource menu slice on one tuple;
it does not establish every recipe/container, shared-world protection, or
complete autonomous Agent behavior.

## Canonical combat, vertical work and handoff tools

The existing Agent registry now calls these client actions through Body, in
the same disposable tuple. RCON supplies resources/fixtures and independently
checks the result. There is no model planning in this proof.

| Tool behavior | Server-confirmed result | Duration |
| --- | --- | --- |
| `equip_item` then `engage_entity` | Iron sword selected from backpack slot 27; named target killed; closer decoy remains at 20 HP | 3.365 s combat |
| `ranged_attack`, bow from backpack | One arrow; named target 20 → 12.128 HP; closer decoy remains at 20 HP | 1.454 s |
| `ranged_attack`, crossbow from backpack | One arrow; named target 20 → 9.176 HP; closer decoy remains at 20 HP | 1.872 s |
| `ranged_attack`, no arrows | `no_ammunition`; target and decoy remain at 20 HP | 299 ms |
| `dig_down` | Grounded Y=150 → 149; server shaft floor becomes air | 0.945 s |
| `dig_up`, dirt scaffold | Grounded Y=150 → 152; specified dirt placed at the shaft base | 2.567 s |
| `go_to_surface` | Grounded at Y=155; independently checked server position | 2.487 s |
| `dig_down`, target outside four-step bound | `vertical_target_out_of_bounds`; original stone floor intact | 168 ms |
| Cancel `dig_up` tool | Canceled terminal; no Baritone process/pathing after a further 0.5 s | 134 ms from stop to tool return |
| `give_player`, two diamonds | Common `pickup_receipt` identifies Receiver, diamond and count 2; server recipient inventory contains both diamonds; no ground item remains | 2.625 s |
| `give_player`, full recipient | `receiver_pickup_unconfirmed`; no receipt and no diamonds in receiver inventory | 4.022 s |

The fixes close actual incompatibilities between the earlier typed client
actions and the existing tools: combat UUID field mismatch, corpse selection,
missing ranged tool registration, lost terminal reasons, premature synchronous
vertical completion, and handoff receipt/source-slot mismatch. Native selection
now swaps a backpack item into an empty or selected hotbar slot. Combat approach
leaves a block-coordinate margin so actual attack reach is satisfied. Ranged
charging uses the native use duration, including interruption by an inventory
update. Vertical tools forward the step bound and use the requested scaffold;
the previous Baritone throwaway-item setting is restored at the terminal.
Nearby handoff no longer requires stepping away from a recipient who is already
within reach. Its pickup wait and terminal wait now allow native approach,
rotation and stop settling. Zero-radius navigation goals retain radius zero.

The direct ranged regression also passed bow/crossbow hits, no-ammunition and
charging cancellation (zero shots, 104 ms client stop-to-terminal). Its first
attempt hit a leftover `ToolTarget` overlapping the intended fixture zombie;
`no_damage_observed` correctly refused success. The fixture now removes the
other directed combat targets before starting. Earlier failed reports retain
the import, hotbar, approach, charging and handoff incompatibilities; they are
not counted as successful runs.

Raw local reports:

- `logs/real-client-combat_tools-1791324416631010561.json`
- `logs/real-client-vertical_tools-1791324684727561379.json`
- `logs/real-client-handoff_tools-1791324642841859822.json`
- `logs/real-client-ranged-1791324760025175272.json`

The final relevant combat, interaction, inventory, container, furnace, client
Body/IPC/protocol, registry, and synchronous Java vertical compatibility suite
passed **311 tests and 187 subtests**. The formal Agent entrypoint regression
also passed **80 tests and 8 subtests**. Java compilation and actual mixin loading
passed. The previously recorded Java collection baseline failures remain
outside this suite. Moving targets, broad natural terrain, shared-world
protection, Elytra flight and a complete model-driven task remain unverified.

## Test reorganization and fresh task runs

The default and CI now reuse 69 current interface/cancellation/terminal-result
regressions rather than duplicate them in a smoke module. They passed in
1.67 s. The changed registry, boundary, collective, volition and historical
Agent checks passed 91 tests and 116 subtests in 2.59 s. About 2,600 lines of
source-shape, duplicate smoke and old constant checks were removed. Historical
backend regressions remain opt-in. This is test evidence, not game success.

Fresh experiments used the same pinned tuple, loopback server, survival mode
and seed `1470711406163257538`. Body cases used supplied fixtures/resources
without a planning model. Their server checks and final state are in
`logs/test-reorganization-20261007/`:

| Task | Result | Report |
|---|---|---|
| Obstacle navigation and log collection | Passed, 4.826 s | `navigation-final.json` |
| Navigation/exploration cancellation | Passed, 2.557 s | `controls.json` |
| Four-block build and clear | Passed, 6.748 s | `building.json` |
| Wheat harvest | Passed, 3.597 s | `farming-final.json` |
| Menu crafting, chest transfer, smelt/reclaim and slot regressions | Passed, 28.151 s | `menus-retry.json` |
| Named melee and ranged tool targets, decoy preservation | Passed, 8.560 s | `combat_tools.json` |
| Down/up/surface tools, bounds and cancellation | Passed, 9.512 s | `vertical_tools.json` |
| Bow/crossbow action damage, no ammunition and charging cancellation | Passed, 6.471 s | `ranged-retry.json` |
| Food/place/gate/lever sequence | Failed at food: `consume_no_effect`; hunger remained 17; later steps not run | `interactions-final.json` |
| Two-client handoff | Failed: `receiver_not_found`; Receiver launch segfaulted before joining | `handoff_tools-retry.json` |

Initial navigation/farm assertions used an invalid player selector, omitted
hotbar items, or required an exact message instead of accepting `Test passed.
Count: 1`. Those were verifier failures and were corrected before the passing
reruns. The first menu run reported `recipe_output_mismatch`; after waiting for
the client to observe the crafting table, the complete menu case passed. The
ranged negative fixture now supplies a living injured target so target death
cannot turn the ammunition regression into a missing-target test. All earlier
failed reports remain alongside the reruns.

The new real-model task invokes the production Agent with `collect 3 logs`,
empty inventory, three placed logs and a stone obstacle, with no tools given
and no human action after submission. The fixture parent releases its IPC
connection before launching the Agent; its initial IPC ownership mistake is
retained in the first failed report. The final actual model run failed in
43.763 s: provider `gpt-5.6-luna` returned `InternalServerError`, model transport
recovery exhausted its retries, and the server inventory contained no logs.
The record is `agent-collect-final/result.json`, with `agent.log` and
`trace.jsonl` beside it. It is a failed Agent task, not planning/collection
proof. Chat continuity and strategy adaptation were not exercised by this
collection run. Elytra destination flight is still unverified.

Use [TESTING.md](../../../TESTING.md) for the focused commands and observation
procedure. These current failures are concrete follow-up work; the test
reorganization does not fix the eating action or the model provider.

## Native food use fixed

`useItem` previously returned after a single native click, ignoring the
continuous-use tick duration. It now travels through the existing asynchronous
client action lifecycle and holds the use key until consumption or the requested
duration. Cancellation, timeout and completion release native use and the key.

The `consumption` task passed on the same tuple in 11.296 s: two bread became
one, the independent server `foodLevel` read was 20, a full player kept the
remaining bread, and cancellation stopped eating before consumption. After a
further two seconds the server still had one bread and client use/key flags
were both false. Raw report: `logs/food-flight-20261007/consumption-retry.json`.
The first new task run ate successfully but its second hunger fixture failed
to drain the saturation restored by bread; the stronger hunger fixture corrected
that experiment error. Body mapping regression passed 62 tests; existing use
transaction regression passed 28 tests. Client Java compiled and loaded live.

## Elytra destination flight verified

The selected tuple now passes a real survival Overworld flight through abstract
`Body.execute(Action("elytraTravel", ...))`. The fixture supplies equipped
Elytra and 128 rockets, loads the route chunks and a landing platform, and drops
the player at `(320.5,220,32.5)`. After submission there is no teleport, manual
steering or additional fixture intervention. Baritone handles gliding, boost,
approach and landing; block breaking and placing are disabled during the task.

The client action took **9.128 s** and landed at
`(448.48551581989597,161,32.51589914382871)`, about **0.022 blocks** from the
requested destination center `(448.5,161,32.5)` after roughly **128 blocks** of
horizontal travel. Independent RCON samples observed `FallFlying:1b` 32 times;
the final server distance check passed and the player was grounded. One rocket
was consumed (128 → 127). Body returned `elytra_destination_reached` with
`flight_observed=true`, no active process and no pathing. The complete fixture
and result took 9.861 s. Raw report: `logs/food-flight-20261007/flight.json`.

An Elytra process ending now succeeds only after flight was observed and the
player landed within eight blocks of the requested position. Ending without
flight or away from the destination returns a concrete failure. This removes
the earlier generic `process_finished_verify_world` success for Elytra.

This is the drop-start destination flight proof on the fixed tuple. It does
not test ground-launch site selection or a long natural-terrain journey. The
upstream [Elytra process source](https://github.com/cabaletta/baritone/blob/v1.18.0/src/main/java/baritone/process/ElytraProcess.java)
was inspected to choose its supported falling-start behavior. Reproduce with:

```bash
python -m tests.e2e_real_client_body_transactions --disposable-local-server --case flight
```
