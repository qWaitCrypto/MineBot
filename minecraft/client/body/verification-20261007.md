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
evidence. This change does not prove 3x3 workstation crafting, every transfer
direction, moving-target combat/handoff, broad natural terrain recovery,
Elytra flight, shared-world protection, or a complete Agent task. The generic
`baritoneCommand` remains outside the Agent action surface. Mutation limits in
this experiment bound touched positions; a complete shared-world permission
implementation remains work for actual deployment.
