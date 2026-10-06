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
xvfb-run -a ./gradlew :client:body:runClient --no-daemon --console=plain
```

After the client reports readiness, `RealClientBody` can submit navigation,
mine, get-to-block, explore, follow/pickup, farm, Builder/Clear and Elytra
objectives, plus typed client controls and world reads. Every objective has a
correlated action id, progress/terminal result, and cancellation path.

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
callable controls that need a dedicated fixture. Combat transactions,
inventory-screen crafting/container/furnace lifecycles, recipient handoff,
and governed vertical excavation remain explicit capability gaps rather than
silent fallbacks. See the [Body capability matrix](../../../docs/references/real-client-body-capability-matrix-20261006.md)
for the exact reasons and the [conformance record](../../../docs/references/client-body-baritone-capability-evidence-20261006.md)
for measurements.
