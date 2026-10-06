# MineBot real-client Body

This module starts a real Fabric client with upstream Baritone and serves the
thin Python `RealClientBody` adapter. It is the client execution backend for
the abstract Body boundary; it does not replace the current Java FakePlayer
backend until a caller selects the `real_client` provider.

The supported tuple is Minecraft `26.1.2`, Fabric Loader `0.19.3`, Fabric API
`0.152.1+26.1.2`, Java `25`, and Baritone `v1.18.0`. The Baritone jar is fetched
from its official release and checked by SHA-1 during the Gradle build.

## Run against the disposable local server

Start MineBot's local test server first, then run from `minecraft/`:

```bash
export MINEBOT_CLIENT_SERVER=127.0.0.1:25566
export MINEBOT_CLIENT_USERNAME=MineBotPhaseA
export MINEBOT_CLIENT_IPC_PORT=8768
# Only for an explicitly disposable loopback world:
export MINEBOT_CLIENT_DISPOSABLE_WORLD=true
xvfb-run -a ./gradlew :client:body:runClient --no-daemon --console=plain
```

After the client reports readiness, `RealClientBody` can submit navigation,
mine, get-to-block, explore, follow/pickup, farm, Builder/Clear and Elytra
objectives, plus typed combat, crafting, container/furnace, handoff and
vertical-recovery actions. Typed client controls and world reads use the same
correlated action id, progress/terminal result, and cancellation path.

The Agent session keeps Java as its default. To select this backend explicitly,
set `MINEBOT_BODY_PROVIDER=real_client`, `MINEBOT_REAL_CLIENT_HOST` (default
`127.0.0.1`), and `MINEBOT_REAL_CLIENT_PORT` (default `8768`) before starting
the Python session; the client must already be running and ready. There is no
automatic fallback to the Java backend.

The first local run may require setting `onboardAccessibility:false` in the
ignored `client/body/run/options.txt`; this is test-client state and is not a
repository setting. The server-name resolver mixin only bypasses Mojang's
external address blocklist for this local test process.

## Current evidence

The loopback Phase A probe now waits for a loaded feet chunk and a grounded
player before accepting work. Python can then send one `goto` or `mine` target
and receive a structured terminal with inventory delta, process state, path
events, and cancellation latency. The controlled fixture proves global
`mine:oak_log` after readiness; starting the same request from the JOIN callback
still reproduces the old search failure.

The evidence record still distinguishes verified process behavior from
client transactions that need dedicated hostile, recipe, menu, multiplayer,
and terrain fixtures. These actions are typed client transactions with
explicit terminal verification; they do not silently fall back to Java. See
the [Body capability matrix](../../../docs/references/real-client-body-capability-matrix-20261006.md)
for the action boundary and the
[conformance record](../../../docs/references/client-body-baritone-capability-evidence-20261006.md)
for measurements.

## Reproduce the typed transaction proof

From the repository root, with the disposable server and client running:

```bash
.venv/bin/python -m tests.e2e_real_client_body_transactions --disposable-local-server --case vertical
.venv/bin/python -m tests.e2e_real_client_body_transactions --disposable-local-server --case ranged
.venv/bin/python -m tests.e2e_real_client_body_transactions --disposable-local-server --case menus
.venv/bin/python -m tests.e2e_real_client_body_transactions --disposable-local-server --case combat_tools
.venv/bin/python -m tests.e2e_real_client_body_transactions --disposable-local-server --case vertical_tools
```

For handoff, launch a second client with username `Receiver` and IPC port
`8769`, then run the same probe with `--case handoff` or `--case handoff_tools`.
The probe checks the server inventory of the actual recipient; a full recipient inventory must
fail without a receipt. Reports are saved as local JSON under `logs/`.

The vertical actions delegate digging, pillar placement, and movement to
upstream Baritone. They wait for a grounded destination and use server block
updates as mutation evidence. Their break/place calls require the disposable
world opt-in; without it, the probe's `--case vertical_denied` verifies the
floor and position remain intact. The distance, liquid, and mutation-position
bounds are enforced before a vertical interaction. This narrow guard does not
provide shared-world protection for the other Baritone processes.

The `menus` fixture supplies resources, unlocks recipes, and exercises the
canonical Agent tool callables for sticks, workstation pickaxe/cake crafting,
bucket remainders, chest transfer in both directions, and smelt/fuel reclaim.
It also checks protocol inventory slots, partial count moves, equipment round
trips and invalid requests against server inventory/equipment reads. Launch
the client and wait for readiness before running the fixture; this is a
directed tool proof, without model planning or autonomous resource gathering.

`combat_tools` calls `equip_item`, `engage_entity`, and `ranged_attack` with
weapons supplied in the backpack. It checks the named target's death/damage,
an untouched closer decoy, and the missing-ammunition reason. `vertical_tools`
calls `dig_down`, `dig_up` with a specified scaffold, and `go_to_surface`, then
checks grounded server positions, the distance refusal, and tool cancellation.
`handoff_tools` calls `give_player` and checks its common pickup receipt and
the full-recipient failure. These use the canonical tool callables without a
planning model. They do not establish a complete autonomous Agent task.

See [the dated verification record](verification-20261007.md) for actual
results, reproduced failures, and the remaining coverage limits.
